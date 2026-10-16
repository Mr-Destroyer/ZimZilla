"""LDXHub — a document-processing job gateway. Not a chat source.

Measured against https://gw.ldxhub.io (docs at gw.portal.ldxhub.io):

* There is no Messages or chat endpoint. ``/v1/messages`` and
  ``/v1/chat/completions`` both 404, and the docs index lists only
  jobs, files, uploads and mcp. LDXHub therefore never becomes
  ``ANTHROPIC_BASE_URL`` and is deliberately *not* wired into
  ``sources.py`` — this module exposes no ``apply_to``, no
  ``active_endpoint`` and no ``is_ldxhub`` predicate.
* Work runs as jobs: ``POST /{service}/jobs`` → 202 ``{job_id}``,
  then poll ``GET /{service}/jobs/{job_id}`` until a terminal status,
  then download ``GET /files/{output_file_id}/content`` when the
  result is a file rather than inline JSON.
* Catalogs are public and unauthenticated: model services
  (structflow / analyzedoc / refineloop) at ``GET /{service}/models``,
  engine services (renderocr / castdoc / extractdoc) at
  ``GET /{service}/engines``. The wrong kind 404s.
* Files: ``POST /files`` multipart field ``file``, 200MB cap,
  201 → ``file_id``.

Six services are exposed to the agent as tools (see ``tools.py``):

    structflow   free-form document generation (model)
    analyzedoc   structured analysis of a PDF/image (model)
    extractdoc   text/jsonl extraction from office/PDF (engine)
    renderocr    OCR → docx/xlsx/pptx, 128 languages (engine)
    castdoc      PDF → docx/xlsx/pptx conversion (engine)
    refineloop   XLIFF translation revision (model)

Credentials live in ``ZIMZILLA_HOME/ldxhub/source``, mode 600, in
the same ``export VAR="value"`` shape the other profiles use.
Nothing here echoes a key.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))

HOST = "gw.ldxhub.io"
BASE_URL = f"https://{HOST}"
PROFILE = ZIMZILLA_HOME / "ldxhub" / "source"

CACHE_TTL = 300.0
JOB_TIMEOUT = 300.0
POLL_INTERVAL = 2.0

#: service → catalog kind. The path segment is the same either way;
#: only ``models`` vs ``engines`` distinguishes them, and the wrong
#: kind returns 404 problem+json.
SERVICES: dict[str, str] = {
    "structflow": "models",
    "analyzedoc": "models",
    "refineloop": "models",
    "renderocr": "engines",
    "castdoc": "engines",
    "extractdoc": "engines",
}

#: service → profile env var holding the per-service default pick.
DEFAULT_VAR: dict[str, str] = {
    "structflow": "LDXHUB_STRUCTFLOW_MODEL",
    "analyzedoc": "LDXHUB_ANALYZEDOC_MODEL",
    "refineloop": "LDXHUB_REFINELOOP_MODEL",
    "renderocr": "LDXHUB_RENDEROCR_ENGINE",
    "castdoc": "LDXHUB_CASTDOC_ENGINE",
    "extractdoc": "LDXHUB_EXTRACTDOC_ENGINE",
}


@dataclass(frozen=True)
class Entry:
    """One model or engine from a service's live catalog."""

    id: str
    label: str
    provider: str = ""
    blurb: str = ""
    conversions: tuple[tuple[str, str], ...] = ()
    languages: tuple[tuple[str, str], ...] = ()

    @property
    def summary(self) -> str:
        bits = [self.id]
        if self.provider and self.provider not in self.id:
            bits.append(f"({self.provider})")
        if self.label and self.label != self.id:
            bits.append(f"— {self.label}")
        if self.conversions:
            pairs = ", ".join(f"{a}→{b}" for a, b in self.conversions[:4])
            more = len(self.conversions) - 4
            bits.append(f"[{pairs}{' …' if more > 0 else ''}]")
        if self.languages:
            bits.append(f"[{len(self.languages)} languages]")
        return " ".join(bits)


# ---------------------------------------------------------------------------
# profile (mode 600; never echoed)
# ---------------------------------------------------------------------------

def _read_exports(path: Path) -> dict[str, str]:
    """Parse ``export VAR="value"`` lines without a shell."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        body = line[len("export "):]
        if "=" not in body:
            continue
        name, _, value = body.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    return out


def load_credentials() -> tuple[str, dict[str, str], str] | None:
    """``(token, {service: default_id}, origin)`` or ``None``.

    ``LDXHUB_API_KEY`` in the environment wins, then the profile.
    Origin is where the token came from — ``env`` or ``profile`` —
    so callers can say where a key is missing from. The token
    itself is never logged or returned anywhere but here.
    """
    env_token = (os.environ.get("LDXHUB_API_KEY") or "").strip()
    if env_token:
        defaults = {
            svc: os.environ.get(var, "").strip()
            for svc, var in DEFAULT_VAR.items()
            if os.environ.get(var, "").strip()
        }
        return env_token, defaults, "env"
    raw = _read_exports(PROFILE)
    token = (raw.get("LDXHUB_API_KEY") or "").strip()
    if not token:
        return None
    defaults = {
        svc: (raw.get(var) or "").strip()
        for svc, var in DEFAULT_VAR.items()
        if (raw.get(var) or "").strip()
    }
    return token, defaults, "profile"


def save_key(token: str, defaults: dict[str, str] | None = None) -> Path:
    """Write the key (and any default picks) to the mode-600 profile.

    Existing ``DEFAULT_VAR`` lines are merged in, so replacing the
    key never silently drops a chosen engine — ``/ldxhub-models``
    can set defaults without re-supplying the key.
    """
    existing = _read_exports(PROFILE) if PROFILE.is_file() else {}
    merged: dict[str, str] = {}
    for svc, var in DEFAULT_VAR.items():
        val = ""
        if defaults and defaults.get(svc):
            val = defaults[svc]
        elif (existing.get(var) or "").strip():
            val = existing[var].strip()
        if val:
            merged[var] = val

    lines = [f'export LDXHUB_API_KEY="{token}"']
    for svc, var in DEFAULT_VAR.items():
        if merged.get(var):
            lines.append(f'export {var}="{merged[var]}"')
    body = "\n".join(lines) + "\n"

    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    fd = os.open(PROFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    return PROFILE


def save_default(service: str, entry_id: str) -> Path | None:
    """Pin the default model/engine for one service. Needs a key."""
    creds = load_credentials()
    if not creds:
        return None
    token, defaults, _origin = creds
    defaults = dict(defaults)
    defaults[service] = entry_id
    return save_key(token, defaults)


def has_key() -> bool:
    return load_credentials() is not None


def looks_like_key(value: str) -> bool:
    """``zpka_…`` and friends — underscore included, unlike openrouter's."""
    value = (value or "").strip()
    if len(value) < 20 or any(ch.isspace() for ch in value):
        return False
    return re.fullmatch(r"[A-Za-z0-9._\-]+", value) is not None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _headers(token: str, *, json_body: bool = False) -> dict[str, str]:
    """Bearer auth, omitted entirely when there is no token.

    The six catalogs are public and answer fine with no Authorization
    header at all, so an empty token must mean *absent*, not
    ``Bearer `` — a literal empty bearer is a different request.
    """
    h: dict[str, str] = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _request(method: str, url: str, token: str, *,
             data: bytes | None = None, headers: dict[str, str] | None = None,
             timeout: float = 20.0):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or _headers(token))
    return urllib.request.urlopen(req, timeout=timeout)


def _map_http_error(exc: urllib.error.HTTPError) -> str:
    if exc.code in (401, 403):
        return f"key rejected ({exc.code}) — check it with /ldxhub-api-setup"
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace") or "")
        detail = str(payload.get("detail") or payload.get("title") or "")
    except Exception:
        pass
    return detail or f"HTTP {exc.code}"


def _json(method: str, url: str, token: str, *,
          payload: dict | None = None, timeout: float = 20.0):
    """``(obj, error)`` — never raises. ``obj`` is None on error."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        with _request(method, url, token, data=data,
                      headers=_headers(token, json_body=True) if data else _headers(token),
                      timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        return None, _map_http_error(e)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"
    try:
        return json.loads(body.decode("utf-8")), ""
    except (ValueError, UnicodeDecodeError):
        return None, "response was not JSON"


# ---------------------------------------------------------------------------
# catalogs
# ---------------------------------------------------------------------------

_CACHE: dict[tuple[str, str], tuple[list[Entry], float]] = {}


def _parse_entries(payload) -> list[Entry]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[Entry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        eid = str(row.get("id") or "").strip()
        if not eid:
            continue
        conv = tuple(
            (str(c.get("from", "")), str(c.get("to", "")))
            for c in row.get("supported_conversions") or []
            if isinstance(c, dict)
        )
        langs = tuple(
            (str(l.get("code", "")), str(l.get("display_name", "")))
            for l in row.get("supported_languages") or []
            if isinstance(l, dict)
        )
        out.append(Entry(
            id=eid,
            label=str(row.get("display_name") or eid),
            provider=str(row.get("provider") or ""),
            blurb=str(row.get("description") or ""),
            conversions=conv,
            languages=langs,
        ))
    return out


def fetch_catalog(service: str, token: str = "", *, timeout: float = 20.0,
                  use_cache: bool = True) -> tuple[list[Entry] | None, str]:
    """``(entries, error)`` — live fetch of one service's catalog.

    Keyless is a first-class case, not an error: the six catalogs are
    public and unauthenticated, so browsing them is useful *before*
    ``/ldxhub-api-setup`` has run. A key is only needed to run a job.
    """
    kind = SERVICES.get(service)
    if kind is None:
        return None, f"unknown service {service!r}"
    if not token:
        creds = load_credentials()
        if creds:
            token = creds[0]
        # No credential is fine here — the request simply goes out
        # unauthenticated.
    if use_cache:
        hit = _CACHE.get((token, service))
        if hit and time.monotonic() - hit[1] < CACHE_TTL:
            return hit[0], ""
    obj, err = _json("GET", f"{BASE_URL}/{service}/{kind}", token, timeout=timeout)
    if err:
        return None, err
    entries = _parse_entries(obj or {})
    _CACHE[(token, service)] = (entries, time.monotonic())
    return entries, ""


def cached_catalog(service: str) -> list[Entry] | None:
    """Non-blocking UI read. TTL checked on *read* — an expired entry
    reads as None so the next UI action re-fetches instead of serving
    stale models forever.

    Keyed on the token, and a keyless session keys on ``""`` — the
    catalogs are public, so browsing before ``/ldxhub-api-setup`` is a
    supported state rather than a miss.
    """
    creds = load_credentials()
    token = creds[0] if creds else ""
    hit = _CACHE.get((token, service))
    if not hit:
        return None
    if time.monotonic() - hit[1] >= CACHE_TTL:
        return None
    return hit[0]


def clear_cache() -> None:
    _CACHE.clear()


def catalog_age(service: str) -> float | None:
    creds = load_credentials()
    token = creds[0] if creds else ""
    hit = _CACHE.get((token, service))
    return time.monotonic() - hit[1] if hit else None


def default_for(service: str, creds=None) -> str:
    if creds is None:
        creds = load_credentials()
    if not creds:
        return ""
    return (creds[1].get(service) or "").strip()


def resolve_entry(service: str, explicit: str = "", creds=None,
                  token: str = "") -> tuple[Entry | None, str]:
    """explicit → profile default → first live catalog entry."""
    explicit = (explicit or "").strip()
    if creds is None:
        creds = load_credentials()
    if creds and not token:
        token = creds[0]
    entries, err = fetch_catalog(service, token)
    if err:
        return None, err
    if not entries:
        return None, f"{service} catalog is empty"
    if explicit:
        for e in entries:
            if e.id == explicit:
                return e, ""
        return None, f"{explicit} not in {service} catalog"
    pinned = default_for(service, creds)
    if pinned:
        for e in entries:
            if e.id == pinned:
                return e, ""
    return entries[0], ""


# ---------------------------------------------------------------------------
# files & jobs
# ---------------------------------------------------------------------------

def upload_file(path: str | Path, token: str, *, timeout: float = 60.0) -> tuple[str, str]:
    """``(file_id, error)`` — multipart POST /files, field ``file``."""
    p = Path(path)
    if not p.is_file():
        return "", f"no such file: {p}"
    boundary = f"----zimzilla{uuid.uuid4().hex}"
    ctype = "application/octet-stream"
    lower = p.suffix.lower()
    if lower == ".pdf":
        ctype = "application/pdf"
    elif lower in (".png",):
        ctype = "image/png"
    elif lower in (".jpg", ".jpeg"):
        ctype = "image/jpeg"
    elif lower in (".docx",):
        ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif lower in (".xlsx",):
        ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif lower in (".pptx",):
        ctype = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    elif lower in (".txt", ".md"):
        ctype = "text/plain"
    elif lower in (".jsonl", ".json"):
        ctype = "application/json"
    elif lower in (".xliff", ".xlf"):
        ctype = "application/xliff+xml"

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{p.name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode("utf-8") + p.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")

    try:
        with _request("POST", f"{BASE_URL}/files", token, data=body,
                      headers={**_headers(token),
                               "Content-Type": f"multipart/form-data; boundary={boundary}"},
                      timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return "", _map_http_error(e)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return "", f"could not reach {HOST}: {e}"
    fid = str(payload.get("file_id") or payload.get("id") or "").strip()
    if not fid:
        return "", "upload response had no file_id"
    return fid, ""


def download_file(file_id: str, token: str, dest: str | Path,
                  *, timeout: float = 60.0) -> str:
    """Save a result file. Returns ``""`` on success, else an error."""
    dest = Path(dest)
    try:
        with _request("GET", f"{BASE_URL}/files/{file_id}/content", token,
                      timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        return _map_http_error(e)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"could not fetch result: {e}"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    except OSError as e:
        return f"could not write {dest}: {e}"
    return ""


def create_job(service: str, payload: dict, token: str,
               *, timeout: float = 30.0) -> tuple[str, str]:
    """``(job_id, error)`` — POST /{service}/jobs → 202 {job_id}."""
    obj, err = _json("POST", f"{BASE_URL}/{service}/jobs", token,
                     payload=payload, timeout=timeout)
    if err:
        return "", err
    jid = str((obj or {}).get("job_id") or (obj or {}).get("id") or "").strip()
    if not jid:
        return "", f"no job_id in response: {str(obj)[:200]}"
    return jid, ""


def get_job(service: str, job_id: str, token: str,
            *, timeout: float = 20.0) -> tuple[dict | None, str]:
    return _json("GET", f"{BASE_URL}/{service}/jobs/{job_id}", token, timeout=timeout)


_TERMINAL = {"completed", "complete", "succeeded", "done", "failed", "error"}


def run_job(service: str, payload: dict, token: str, *,
            timeout: float = JOB_TIMEOUT, poll: float = POLL_INTERVAL,
            sleep=time.sleep) -> tuple[dict | None, str]:
    """Create a job and poll it to a terminal status.

    ``sleep`` is injectable so tests never wait. Safe to block:
    tool handlers run via ``asyncio.to_thread`` (agent.py), not on
    the UI thread. Returns ``(last_job_dict, error)``.
    """
    jid, err = create_job(service, payload, token)
    if err:
        return None, err
    deadline = time.monotonic() + timeout
    last: dict | None = None
    last_err = ""
    while True:
        obj, err = get_job(service, jid, token)
        if err:
            last_err = err
        elif obj is not None:
            last = obj
            last_err = ""
            status = str(obj.get("status") or "").lower()
            if status in _TERMINAL:
                return last, ""
        if time.monotonic() >= deadline:
            if last is not None:
                return last, f"timed out after {timeout:.0f}s (status {last.get('status')})"
            return None, last_err or f"timed out after {timeout:.0f}s"
        sleep(poll)


def job_output(job: dict) -> tuple[str | None, str | None, str]:
    """``(inline_text, output_file_id, error)`` from a terminal job.

    File is authoritative when present; otherwise inline ``summary``
    + ``results[]`` is rendered as JSON. ``failed`` jobs report their
    problem as the error.
    """
    if not isinstance(job, dict):
        return None, None, "no job payload"
    status = str(job.get("status") or "").lower()
    if status in ("failed", "error"):
        detail = job.get("error") or job.get("detail") or job.get("message") or status
        return None, None, f"job {status}: {detail}"

    fid = job.get("output_file_id") or job.get("file_id")
    fid = str(fid).strip() if fid else ""

    summary = job.get("summary")
    results = job.get("results")
    if isinstance(summary, str) and summary.strip():
        if isinstance(results, list) and results:
            return json.dumps({"summary": summary, "results": results},
                              ensure_ascii=False, indent=2), fid or None, ""
        return summary, fid or None, ""
    if isinstance(results, list) and results:
        return json.dumps(results, ensure_ascii=False, indent=2), fid or None, ""

    for key in ("output", "result", "content"):
        val = job.get(key)
        if isinstance(val, str) and val.strip():
            return val, fid or None, ""
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False, indent=2), fid or None, ""

    if fid:
        return None, fid, ""
    return None, None, "job finished with no output"


def service_help() -> str:
    return (
        "structflow — free-form document generation from a prompt (model)\n"
        "analyzedoc — structured JSON analysis of a PDF/image (model)\n"
        "extractdoc — text or jsonl extraction from office/PDF (engine)\n"
        "renderocr — OCR to docx/xlsx/pptx, 128 languages (engine)\n"
        "castdoc — PDF to docx/xlsx/pptx conversion (engine)\n"
        "refineloop — revise an XLIFF translation (model)"
    )
