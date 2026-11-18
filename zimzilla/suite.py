"""ZimSuite backend — a local intercepting proxy with a request workbench.

The design is intentionally small: mitmproxy captures traffic to a JSONL file,
the TUI tails it, and replay/crawl/intruder go through the same store. The UI
never scrapes a screenshot to learn what happened; it reads the requests.

mitmproxy runs as a child process from its own pipx environment. The generated
addon is stdlib-only, so the harness venv does not need mitmproxy installed.
Intercept is file-based: the addon parks a request under ``pending/`` and waits
for the UI to write ``decisions/<id>.json``.
"""

from __future__ import annotations

import html.parser
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MAX_BODY = 12_000
MAX_FLOWS = 2_000
CRAWL_LIMIT = 30


class SuiteError(Exception):
    """A proxy or request failure worth showing."""


@dataclass
class Flow:
    id: str
    method: str
    url: str
    status: int | None = None
    mime: str = ""
    req_headers: dict = field(default_factory=dict)
    resp_headers: dict = field(default_factory=dict)
    req_body: str = ""
    resp_body: str = ""
    ts: float = 0.0
    source: str = "proxy"

    @property
    def host(self) -> str:
        try:
            return urllib.parse.urlsplit(self.url).hostname or ""
        except ValueError:
            return ""

    @property
    def path(self) -> str:
        try:
            parts = urllib.parse.urlsplit(self.url)
            out = parts.path or "/"
            if parts.query:
                out += "?" + parts.query
            return out
        except ValueError:
            return self.url

    @property
    def length(self) -> int:
        return len(self.resp_body or "")

    def summary(self) -> str:
        return f"{self.method} {self.path} → {self.status or '—'}  {self.mime[:40]}"

    def request_text(self) -> str:
        headers = "\n".join(f"{k}: {v}" for k, v in self.req_headers.items())
        return f"{self.method} {self.path}\n{headers}\n\n{self.req_body}"

    def response_text(self) -> str:
        headers = "\n".join(f"{k}: {v}" for k, v in self.resp_headers.items())
        return f"HTTP {self.status or '—'}\n{headers}\n\n{self.resp_body}"


def _jsonable(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


#: The addon is generated with absolute paths. It must not import zimzilla:
#: mitmdump runs from a separate pipx Python.
_ADDON = r'''# ZimSuite capture addon — generated. Stdlib only.
import json, os, time
from pathlib import Path
from mitmproxy import http

OUT = Path(__PATH_OUT__)
PENDING = Path(__PATH_PENDING__)
DECISIONS = Path(__PATH_DECISIONS__)
INTERCEPT = Path(__PATH_INTERCEPT__)
MAX = 12000

def _save(rec):
    try:
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        try:
            with OUT.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"error": str(e)}) + "\n")
        except Exception:
            pass

def _body(msg):
    try:
        return msg.get_text(strict=False)[:MAX]
    except Exception:
        return ""

def request(flow: http.HTTPFlow):
    if not INTERCEPT.exists():
        return
    fid = flow.id
    rec = {
        "id": fid, "ts": time.time(), "method": flow.request.method,
        "url": flow.request.pretty_url,
        "req_headers": dict(flow.request.headers),
        "req_body": _body(flow.request),
    }
    try:
        PENDING.mkdir(parents=True, exist_ok=True)
        DECISIONS.mkdir(parents=True, exist_ok=True)
        (PENDING / (fid + ".json")).write_text(json.dumps(rec))
        deadline = time.time() + 120
        decision = DECISIONS / (fid + ".json")
        while time.time() < deadline:
            if decision.exists():
                action = json.loads(decision.read_text()).get("action", "forward")
                decision.unlink(missing_ok=True)
                (PENDING / (fid + ".json")).unlink(missing_ok=True)
                if action == "drop":
                    flow.response = http.Response.make(
                        403, b"ZimSuite: request dropped", {"Content-Type": "text/plain"})
                return
            time.sleep(0.1)
    except Exception as e:
        _save({"error": str(e)})

def response(flow: http.HTTPFlow):
    try:
        resp = flow.response
        _save({
            "id": flow.id, "ts": time.time(),
            "method": flow.request.method, "url": flow.request.pretty_url,
            "status": resp.status_code if resp else None,
            "mime": (resp.headers.get("content-type", "") if resp else ""),
            "req_headers": dict(flow.request.headers),
            "resp_headers": dict(resp.headers) if resp else {},
            "req_body": _body(flow.request),
            "resp_body": _body(resp) if resp else "",
        })
    except Exception as e:
        _save({"error": str(e)})
'''


class _LinkParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        elif tag in ("form",) and values.get("action"):
            self.links.append(values["action"])


class Suite:
    """One proxy + flow store for one target."""

    def __init__(self, target: str, cfg, *, port: int | None = None,
                 on_flow=None, on_pending=None, on_log=None) -> None:
        self.target = target
        # The operator types a host; the suite uses a URL. Keep the host for
        # campaign lookup so `/zimsuite example.com` finds
        # `hunts/example.com`, not `hunts/https-example-com`.
        try:
            parts = urllib.parse.urlsplit(target)
            self.scope_target = parts.hostname or target
        except ValueError:
            self.scope_target = target
        self.cfg = cfg
        self.port = port or _free_port()
        self.on_flow = on_flow
        self.on_pending = on_pending
        self.on_log = on_log
        self.root = Path(cfg.state_dir) / "suite"
        self.root.mkdir(parents=True, exist_ok=True)
        self.flows_path = self.root / "flows.jsonl"
        self.addon_path = self.root / "addon.py"
        self.pending_dir = self.root / "pending"
        self.decisions_dir = self.root / "decisions"
        self.intercept_file = self.root / "intercept.on"
        self.log_path = self.root / "mitm.log"
        self.flows: list[Flow] = []
        self.pending: dict[str, Flow] = {}
        self.proc: subprocess.Popen | None = None
        self._log_fh = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_size = 0
        self.error = ""

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def log(self, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:  # noqa: BLE001
                pass

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        binary = shutil.which("mitmdump")
        if not binary:
            self.error = "mitmdump not found — install mitmproxy"
            return False
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        self.flows_path.touch(exist_ok=True)
        self.addon_path.write_text(
            _ADDON.replace("__PATH_OUT__", repr(str(self.flows_path)))
            .replace("__PATH_PENDING__", repr(str(self.pending_dir)))
            .replace("__PATH_DECISIONS__", repr(str(self.decisions_dir)))
            .replace("__PATH_INTERCEPT__", repr(str(self.intercept_file))),
            encoding="utf-8",
        )
        cmd = [binary, "-q", "--listen-host", "127.0.0.1",
               "-p", str(self.port), "-s", str(self.addon_path)]
        try:
            self._log_fh = self.log_path.open("ab")
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=self._log_fh,
                stdin=subprocess.DEVNULL)
        except OSError as e:
            self.error = f"could not start mitmdump: {e}"
            return False
        # mitmdump takes a moment to bind; without this the first browser
        # request races the listener and looks like a dead proxy.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.25):
                    break
            except OSError:
                time.sleep(0.15)
        if self.proc.poll() is not None:
            try:
                self.error = self.log_path.read_text(errors="replace")[-500:]
            except OSError:
                self.error = "mitmdump exited immediately"
            return False
        if not _port_open(self.port):
            self.error = "mitmdump started but is not listening"
            try:
                self.proc.terminate()
            except OSError:
                pass
            return False
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        self.log(f"proxy listening on {self.proxy_url}")
        return True

    def stop(self) -> None:
        self._stop.set()
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                except OSError:
                    pass
        try:
            self.intercept_file.unlink(missing_ok=True)
        except OSError:
            pass
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    # ---- flow tail --------------------------------------------------------
    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                size = self.flows_path.stat().st_size
            except OSError:
                time.sleep(0.25)
                continue
            if size > self._last_size:
                try:
                    with self.flows_path.open("r", encoding="utf-8") as f:
                        f.seek(self._last_size)
                        chunk = f.read()
                    self._last_size = size
                    for line in chunk.splitlines():
                        self._consume(line)
                except OSError:
                    pass
            self._check_pending()
            time.sleep(0.25)

    def _consume(self, line: str) -> None:
        try:
            rec = json.loads(line)
        except ValueError:
            return
        if rec.get("error"):
            self.log("proxy error: " + str(rec["error"]))
            return
        flow = Flow(
            id=str(rec.get("id") or uuid.uuid4().hex),
            method=str(rec.get("method") or "GET"),
            url=str(rec.get("url") or ""),
            status=rec.get("status"),
            mime=str(rec.get("mime") or ""),
            req_headers=dict(rec.get("req_headers") or {}),
            resp_headers=dict(rec.get("resp_headers") or {}),
            req_body=str(rec.get("req_body") or ""),
            resp_body=str(rec.get("resp_body") or ""),
            ts=float(rec.get("ts") or time.time()),
        )
        self.flows.append(flow)
        if len(self.flows) > MAX_FLOWS:
            self.flows = self.flows[-MAX_FLOWS:]
        if self.on_flow:
            try:
                self.on_flow(flow)
            except Exception:  # noqa: BLE001
                pass

    def _check_pending(self) -> None:
        if not self.pending_dir.exists():
            return
        for path in sorted(self.pending_dir.glob("*.json")):
            try:
                rec = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            fid = str(rec.get("id") or path.stem)
            if fid in self.pending:
                continue
            flow = Flow(
                id=fid, method=str(rec.get("method") or "GET"),
                url=str(rec.get("url") or ""),
                req_headers=dict(rec.get("req_headers") or {}),
                req_body=str(rec.get("req_body") or ""),
                source="intercept",
            )
            self.pending[fid] = flow
            if self.on_pending:
                try:
                    self.on_pending(list(self.pending.values()))
                except Exception:  # noqa: BLE001
                    pass

    # ---- intercept --------------------------------------------------------
    def set_intercept(self, on: bool) -> None:
        if on:
            self.intercept_file.write_text("1\n")
        else:
            self.intercept_file.unlink(missing_ok=True)

    def decide(self, flow_id: str, action: str) -> None:
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        (self.decisions_dir / f"{flow_id}.json").write_text(
            json.dumps({"action": action}))
        self.pending.pop(flow_id, None)
        if self.on_pending:
            self.on_pending(list(self.pending.values()))

    # ---- requests ---------------------------------------------------------
    def request(self, method: str, url: str, headers: dict | None = None,
                body: str = "", *, source: str = "repeater",
                timeout: float = 30.0) -> Flow:
        method = (method or "GET").upper()
        hdrs = {str(k): str(v) for k, v in (headers or {}).items()}
        # A bare urllib request is an instant 403 on many WAFs. Behave like the
        # browser whose traffic Burp would capture, unless the operator supplied
        # their own identity.
        if not any(k.lower() == "user-agent" for k in hdrs):
            hdrs["User-Agent"] = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
        if not any(k.lower() == "accept" for k in hdrs):
            hdrs["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        data = body.encode("utf-8", "replace") if body else None
        req = urllib.request.Request(url, data=data, headers=hdrs,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read(MAX_BODY)
                status, rh, final_url = resp.status, dict(resp.headers), resp.url
        except urllib.error.HTTPError as e:
            payload = e.read(MAX_BODY)
            status, rh, final_url = e.code, dict(e.headers), e.url
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SuiteError(f"{method} {url}: {e}") from e
        flow = Flow(
            id=f"local-{uuid.uuid4().hex[:12]}", method=method,
            url=final_url or url, status=status,
            mime=rh.get("Content-Type", ""), req_headers=hdrs,
            resp_headers=rh,
            req_body=(body or "")[:MAX_BODY],
            resp_body=payload.decode("utf-8", "replace"),
            ts=time.time(), source=source,
        )
        self.flows.append(flow)
        if self.on_flow:
            try:
                self.on_flow(flow)
            except Exception:  # noqa: BLE001
                pass
        return flow

    def replay(self, method: str, url: str, headers: dict | None = None,
               body: str = "") -> Flow:
        return self.request(method, url, headers, body, source="repeater")

    def intruder(self, url: str, payloads: list[str], *, marker: str = "FUZZ",
                 method: str = "GET", headers: dict | None = None,
                 body: str = "") -> list[Flow]:
        results: list[Flow] = []
        for payload in payloads:
            target = url.replace(marker, urllib.parse.quote(payload, safe=""))
            try:
                results.append(self.request(
                    method, target, headers, body.replace(marker, payload),
                    source="intruder"))
            except SuiteError as e:
                self.log(str(e))
        return results

    # ---- crawl ------------------------------------------------------------
    def crawl(self, start_url: str, *, limit: int = CRAWL_LIMIT,
              seeds: list[str] | None = None) -> list[Flow]:
        if not start_url.startswith(("http://", "https://")):
            start_url = "https://" + start_url
        root = urllib.parse.urlsplit(start_url)
        host = root.hostname or ""
        seen: set[str] = set()
        queue = [start_url]
        for seed in seeds or []:
            if not seed.startswith(("http://", "https://")):
                seed = "https://" + seed
            queue.append(seed)
        found: list[Flow] = []
        while queue and len(seen) < limit:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                flow = self.request("GET", url, source="crawler")
            except SuiteError:
                continue
            found.append(flow)
            parser = _LinkParser()
            try:
                parser.feed(flow.resp_body)
            except Exception:  # noqa: BLE001
                pass
            for href in parser.links:
                absolute = urllib.parse.urljoin(url, href)
                parts = urllib.parse.urlsplit(absolute)
                if parts.scheme in ("http", "https") and parts.hostname == host:
                    clean = urllib.parse.urlunsplit(
                        (parts.scheme, parts.netloc, parts.path, parts.query, ""))
                    if clean not in seen:
                        queue.append(clean)
        return found

    # ---- findings ---------------------------------------------------------
    _ABS_RE = re.compile(r"https?://[^\s)\]}\"'<>]+", re.I)
    _HOSTPATH_RE = re.compile(
        r"\b(?:www\.)?[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}(?::\d+)?"
        r"(?:/[^\s)\]}\"'<>]*)?", re.I)
    _PATH_RE = re.compile(
        r"(?<![\w:/])/[A-Za-z0-9._~%!$&'()*+,;=:@/-]+"
        r"(?:\?[^\s)\]}\"'<>]*)?", re.I)

    def _normalise_url(self, value: str) -> str | None:
        value = (value or "").strip().strip(".,;:)]}\"'`")
        if not value:
            return None
        if value.startswith("//"):
            value = "https:" + value
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        try:
            parts = urllib.parse.urlsplit(value)
        except ValueError:
            return None
        host = (parts.hostname or "").lower()
        if not host or "." not in host:
            return None
        if host in ("localhost",) or host.startswith("127.") or host.endswith(".md"):
            return None
        path = parts.path or "/"
        if path.startswith(("/home/", "/tmp/", "/usr/", "/etc/")):
            return None
        if path.lower().split("?")[0].endswith((".md", ".markdown")):
            return None
        if path in ("/script", "/style", "/link"):
            return None
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, path, parts.query, ""))

    def hunt_urls(self, limit: int = 250) -> list[str]:
        """Absolute URLs and endpoint paths extracted from the campaign."""
        base = self.target if "://" in self.target else "https://" + self.target
        base_parts = urllib.parse.urlsplit(base)
        host = base_parts.hostname or self.scope_target
        bases = [f"https://{host}", f"http://{host}"]
        found: list[str] = []
        seen: set[str] = set()
        base_host = (host or "").lower()
        root_host = base_host[4:] if base_host.startswith("www.") else base_host

        def in_scope(value: str) -> bool:
            try:
                candidate = (urllib.parse.urlsplit(value).hostname or "").lower()
            except ValueError:
                return False
            if not candidate:
                return False
            return (candidate == root_host or candidate.endswith("." + root_host)
                    or candidate == base_host)

        def add(value: str, *, path_only: bool = False) -> None:
            if path_only:
                if not value.startswith("/"):
                    value = "/" + value
                for b in bases:
                    url = self._normalise_url(b + value)
                    if url and url not in seen and in_scope(url):
                        seen.add(url)
                        found.append(url)
                        return
                return
            url = self._normalise_url(value)
            if url and url not in seen and in_scope(url):
                seen.add(url)
                found.append(url)

        for path in self.hunt_files():
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for match in self._ABS_RE.finditer(text):
                add(match.group(0))
            for match in self._HOSTPATH_RE.finditer(text):
                value = match.group(0)
                if value.lower().startswith(("http", "www.")) or "/" in value:
                    add(value)
            for match in self._PATH_RE.finditer(text):
                value = match.group(0)
                # A markdown heading or local path is not an endpoint.
                if value.startswith(("//", "/home", "/tmp", "/usr", "/etc")):
                    continue
                if value.count("/") < 1 or len(value) > 300:
                    continue
                add(value, path_only=True)
            if len(found) >= limit:
                break
        return found[:limit]

    def seed_hunt_flows(self, *, limit: int = 250) -> list[Flow]:
        """Put the campaign's URLs into the history without sending anything."""
        existing = {f.url for f in self.flows if f.source == "hunt"}
        seeded: list[Flow] = []
        for n, url in enumerate(self.hunt_urls(limit=limit), 1):
            if url in existing:
                continue
            flow = Flow(
                id=f"hunt-{n}", method="GET", url=url, status=None,
                mime="endpoint", source="hunt", ts=time.time(),
            )
            self.flows.append(flow)
            seeded.append(flow)
            if self.on_flow:
                try:
                    self.on_flow(flow)
                except Exception:  # noqa: BLE001
                    pass
        if seeded:
            self.log(f"seeded {len(seeded)} endpoint(s) from the hunt")
        return seeded

    def hunt_files(self) -> list[Path]:
        from . import hunt as hunt_mod
        try:
            slug = hunt_mod.scope_slug(self.scope_target)
        except ValueError:
            return []
        base = Path(self.cfg.state_dir) / "hunts"
        candidates = [slug]
        if slug.startswith("www."):
            candidates.append(slug[4:])
        if slug.startswith("*."):
            candidates.append(slug[2:])
        campaign = next((base / c for c in candidates if (base / c).exists()),
                        base / slug)
        if not campaign.exists():
            return []
        files = []
        for pattern in ("*.md", "**/*.md", "**/*.json"):
            files.extend(campaign.glob(pattern))
        return sorted({p for p in files if p.is_file()},
                      key=lambda p: p.stat().st_mtime, reverse=True)[:20]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), 0.3):
            return True
    except OSError:
        return False
