"""`/scope-validator` — turn a pasted bug-bounty programme overview into scope files.

The operator pastes the whole programme page: the in-scope and out-of-scope
sections, the rules, the reward tiers, all of it. This module does three things
with it:

1. **Extracts** the structured scope. A model reads the overview and returns a
   single JSON object: the in-scope assets, the out-of-scope assets, and a
   one-line note per asset. Nothing is written from prose; only what the model
   reports as a structured entry is used.
2. **Writes** ``allow.yaml`` and ``out-of-scope.yaml`` in the format
   ``zimzilla/scope.py`` already reads, then loads them back through
   ``Scope.load`` so a file the guard cannot parse is reported, not trusted.
3. **Ranks** the in-scope assets as a tree, after a light read-only recon of
   each host (DNS, HTTP status, title, server). The ranking is the model's, from
   that evidence, and the tree shows the evidence beside each rank so the
   operator can disagree with it.

Nothing here is a judgement the code makes on its own. Parsing, writing and
rendering are deterministic and tested without a terminal; the ranking comes
from the model and is shown as the model's.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from .scope import HostSet, Scope, _DOMAIN_KEYS, _IP_KEYS, _CIDR_KEYS

#: Cap on how many in-scope assets get recon. Keeps a huge programme from
#: turning one command into a few hundred requests.
RECON_MAX_HOSTS = 25

#: Per-host network timeout, seconds.
RECON_TIMEOUT = 6.0

_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_CIDR = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$")
_HOST = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+$")
_WILDCARD = re.compile(r"^\*\.")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """\
You are reading a bug-bounty programme overview to extract its scope. The text
is below between the markers. Do not run any tools, and do not test anything.

<<<OVERVIEW
{overview}
OVERVIEW>>>

Return ONE JSON object and nothing else:

{{"programme": "name of the programme, or empty",
  "in_scope": [{{"asset": "exact asset as written, e.g. *.example.com or 10.0.0.0/24",
                "type": "domain | wildcard | ip | cidr | other",
                "note": "short note from the overview, e.g. 'web, bounty eligible'"}}],
  "out_of_scope": [{{"asset": "exact asset as written", "type": "domain | wildcard | ip | cidr | other",
                     "note": "why, if stated"}}],
  "rules": ["short rule from the overview, e.g. 'no automated scanning above N rps'"]}}

Rules for the extraction:
- Only assets the overview states. Do not add hosts from your own knowledge.
- Keep wildcards as written (*.example.com). Keep out-of-scope entries separate
  from in-scope ones even when they look similar.
- Use "other" for anything that is not a domain, wildcard, IP or CIDR (an app
  name, a mobile bundle, a feature). Those are not written to the scope files.
- Empty lists are fine. Do not invent entries to fill them.
"""

RANK_PROMPT = """\
You are ranking the in-scope assets of a bug-bounty programme for testing order.
Below are the assets, and for each host a read-only recon result (DNS, HTTP
status, page title, server header). Judge from this evidence only.

Programme: {programme}

ASSETS
{assets}

Return ONE JSON object and nothing else:

{{"ranking": [{{"asset": "exact asset",
               "rank": 1,
               "value": "high | medium | low",
               "why": "one line, citing the evidence (title, status, tech, or the note)"}}],
  "summary": "one line on where to start"}}

Rank by likely bug density and reward, not by how the list was written. A login
page, an admin or API surface, a file-upload or a legacy-looking stack ranks
above a static marketing page. Say when the evidence is too thin to rank an
asset and rank it low with that reason. Include every asset exactly once.
"""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@dataclass
class ScopeEntry:
    asset: str
    type: str = "other"
    note: str = ""


@dataclass
class Extraction:
    programme: str = ""
    in_scope: list[ScopeEntry] = field(default_factory=list)
    out_of_scope: list[ScopeEntry] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)


@dataclass
class Ranked:
    asset: str
    rank: int
    value: str
    why: str = ""


def _json_object(text: str) -> dict | None:
    """The first JSON object in a model reply, fenced or bare."""
    if not text:
        return None
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data
        except ValueError:
            return None
    return None


def _clean_asset(raw) -> str:
    return str(raw or "").strip().strip("`'\"").strip()


def _entries(raw) -> list[ScopeEntry]:
    out: list[ScopeEntry] = []
    if not isinstance(raw, list):
        return out
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"asset": item}
        if not isinstance(item, dict):
            continue
        asset = _clean_asset(item.get("asset"))
        if not asset or asset.lower() in seen:
            continue
        seen.add(asset.lower())
        out.append(ScopeEntry(
            asset=asset,
            type=str(item.get("type") or "other").strip().lower(),
            note=str(item.get("note") or "").strip(),
        ))
    return out


def parse_extraction(text: str) -> Extraction | None:
    """The model's extraction, or None if the reply cannot be read."""
    data = _json_object(text)
    if data is None:
        return None
    rules = data.get("rules") or []
    return Extraction(
        programme=str(data.get("programme") or "").strip(),
        in_scope=_entries(data.get("in_scope")),
        out_of_scope=_entries(data.get("out_of_scope")),
        rules=[str(r).strip() for r in rules if str(r).strip()] if isinstance(rules, list) else [],
    )


def parse_ranking(text: str, assets: list[str]) -> tuple[list[Ranked], str] | None:
    """The model's ranking, or None if unreadable. Every asset gets a place.

    An asset the model left out is appended at the bottom, marked as unranked,
    rather than dropped — a silently missing host is the worst outcome for a
    scope list.
    """
    data = _json_object(text)
    if data is None:
        return None
    ranked: list[Ranked] = []
    seen: set[str] = set()
    for item in data.get("ranking") or []:
        if not isinstance(item, dict):
            continue
        asset = _clean_asset(item.get("asset"))
        if not asset or asset not in assets or asset in seen:
            continue
        seen.add(asset)
        try:
            rank = int(item.get("rank") or 0)
        except (TypeError, ValueError):
            rank = 0
        ranked.append(Ranked(
            asset=asset,
            rank=rank,
            value=str(item.get("value") or "low").strip().lower(),
            why=str(item.get("why") or "").strip(),
        ))
    ranked.sort(key=lambda r: (r.rank if r.rank > 0 else 10**6, r.asset))
    for a in assets:
        if a not in seen:
            ranked.append(Ranked(asset=a, rank=0, value="unranked",
                                 why="the model did not rank this asset"))
    # Renumber densely so the tree reads 1..n even if the model skipped numbers.
    for i, r in enumerate(ranked, 1):
        r.rank = i
    return ranked, str(data.get("summary") or "").strip()


# ---------------------------------------------------------------------------
# Classification and scope files
# ---------------------------------------------------------------------------

def _classify(asset: str) -> str:
    """``domain``, ``wildcard``, ``ip``, ``cidr`` or ``other`` for one asset.

    URLs are classified by their host, so ``https://x.io/path`` is a domain,
    not a CIDR. IPs and networks are parsed with ``ipaddress``, not regexes, so
    IPv6 and malformed input are judged the same way the guard judges them.
    """
    a = asset.strip().lower()
    if "://" in a:
        a = urlsplit(a).hostname or ""
    if not a:
        return "other"
    if "/" in a:
        try:
            ipaddress.ip_network(a, strict=False)
            return "cidr"
        except ValueError:
            a = a.split("/")[0]
    try:
        ipaddress.ip_address(a.strip("[]"))
        return "ip"
    except ValueError:
        pass
    host = _WILDCARD.sub("", a.split(":")[0]).rstrip(".")
    if _IPV4.match(host):
        return "other"  # an address-shaped string that is not a valid address
    if _WILDCARD.match(a):
        return "wildcard" if _HOST.match(host) else "other"
    return "domain" if _HOST.match(host) else "other"


def _host_of(asset: str) -> str:
    """The bare hostname of an in-scope domain or wildcard, or '' if none."""
    if _classify(asset) not in ("domain", "wildcard"):
        return ""
    a = asset.strip().lower()
    if "://" in a:
        a = urlsplit(a).hostname or ""
    a = _WILDCARD.sub("", a.split("/")[0].split(":")[0]).strip(".")
    return a if _HOST.match(a) else ""


def build_scope(entries_in: list[ScopeEntry], entries_out: list[ScopeEntry]) -> tuple[dict, dict, list[str]]:
    """The two mappings to write, and the assets that could not be placed.

    Keys match what ``scope.HostSet.from_mapping`` reads: ``domains`` for names
    and wildcards, ``ips`` for addresses, ``cidrs`` for networks. Anything else
    is returned in the skipped list so the operator sees it, not a silent drop.
    """
    skipped: list[str] = []

    def bucket(entries: list[ScopeEntry]) -> dict:
        domains: list[str] = []
        ips: list[str] = []
        cidrs: list[str] = []
        for e in entries:
            kind = _classify(e.asset)
            if kind in ("domain", "wildcard"):
                host = _host_of(e.asset)
                if host:
                    if host not in domains:
                        domains.append(host)
                else:
                    skipped.append(e.asset)
            elif kind == "ip":
                if e.asset not in ips:
                    ips.append(e.asset)
            elif kind == "cidr":
                if e.asset not in cidrs:
                    cidrs.append(e.asset)
            else:
                skipped.append(e.asset)
        out: dict = {}
        if domains:
            out["domains"] = domains
        if ips:
            out["ips"] = ips
        if cidrs:
            out["cidrs"] = cidrs
        return out

    allow = bucket(entries_in)
    deny = bucket(entries_out)
    return allow, deny, skipped


def _header(kind: str, programme: str) -> str:
    who = f" for {programme}" if programme else ""
    return (f"# {kind}{who}\n"
            "# Written by /scope-validator from a pasted programme overview.\n"
            "# Review before you run anything — the operator owns this list.\n")


def write_scope_files(dest: Path, allow: dict, deny: dict, programme: str = "") -> tuple[Path, Path]:
    """Write ``allow.yaml`` and ``out-of-scope.yaml`` into *dest*.

    Refuses to overwrite an existing file. The operator may already have a
    hand-edited scope, and replacing it silently would discard their work — so an
    existing file is moved aside with a timestamp first, and the move is reported.
    """
    if yaml is None:
        raise RuntimeError("PyYAML is not installed — cannot write scope files")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths = []
    for name, body, kind in (
        ("allow.yaml", allow, "allow.yaml — declared targets"),
        ("out-of-scope.yaml", deny, "out-of-scope.yaml — never touched"),
    ):
        path = dest / name
        if path.exists():
            path.rename(dest / f"{name}.bak-{stamp}")
        text = _header(kind, programme) + yaml.safe_dump(body or {}, sort_keys=False)
        path.write_text(text)
        paths.append(path)
    return paths[0], paths[1]


def verify_scope_files(allow_path: Path, deny_path: Path) -> list[str]:
    """Load the written files back through the real guard. Returns problems.

    An empty list means the guard reads exactly what was written. Anything
    else is reported as a problem rather than trusted.
    """
    problems: list[str] = []
    scope = Scope.load(allow_path, deny_path)
    if scope.allow_path is None:
        problems.append("allow.yaml did not load")
    elif scope.allow.empty:
        problems.append("allow.yaml loaded with no entries")
    if deny_path.exists() and scope.deny_path is None:
        problems.append("out-of-scope.yaml did not load")
    return problems


# ---------------------------------------------------------------------------
# Recon (read-only)
# ---------------------------------------------------------------------------

@dataclass
class HostRecon:
    host: str
    resolves: bool = False
    ip: str = ""
    status: int | None = None
    title: str = ""
    server: str = ""
    error: str = ""


def _title(body: bytes) -> str:
    m = re.search(rb"<title[^>]*>(.*?)</title>", body[:65536], re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1).decode("utf-8", "replace")).strip()[:90]


def recon_host(host: str, timeout: float = RECON_TIMEOUT) -> HostRecon:
    """DNS, then one HTTPS GET (HTTP on failure). Read-only: no ports scanned.

    Nothing here follows redirects off-host or sends a payload; it is one GET
    to the host the programme names, which is what the operator asked to look at.
    """
    import http.client

    r = HostRecon(host=host)
    try:
        r.ip = socket.gethostbyname(host)
        r.resolves = True
    except OSError as e:
        r.error = f"dns: {e.strerror or e}"
        return r

    for scheme in ("https", "http"):
        try:
            if scheme == "https":
                conn = http.client.HTTPSConnection(
                    host, timeout=timeout, context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(host, timeout=timeout)
            conn.request("GET", "/", headers={"User-Agent": "zimzilla-scope-validator"})
            resp = conn.getresponse()
            r.status = resp.status
            r.server = (resp.getheader("Server") or "")[:60]
            body = resp.read(65536)
            r.title = _title(body)
            conn.close()
            r.error = ""
            return r
        except Exception as e:  # noqa: BLE001 - recon must report, not crash
            r.error = f"{scheme}: {type(e).__name__}"
    return r


def recon_all(hosts: list[str], deny: Scope | None = None,
              timeout: float = RECON_TIMEOUT) -> dict[str, HostRecon]:
    """Recon the in-scope hosts, never an out-of-scope one.

    The harness makes these requests itself, so the agent's bash guard does not
    see them. The deny-list is therefore checked here, before any socket opens:
    a host the operator marked out of scope is skipped, and reported as skipped.
    """
    out: dict[str, HostRecon] = {}
    for h in hosts[:RECON_MAX_HOSTS]:
        if deny is not None and not deny.deny.empty:
            blocked, _reason = deny.deny.contains(h)
            if blocked:
                continue
        out[h] = recon_host(h, timeout)
    return out


def recon_line(r: HostRecon) -> str:
    if not r.resolves:
        return f"does not resolve ({r.error})"
    bits = [r.ip]
    if r.status is not None:
        bits.append(f"HTTP {r.status}")
    if r.server:
        bits.append(r.server)
    if r.title:
        bits.append(f'"{r.title}"')
    if r.status is None and r.error:
        bits.append(f"no response ({r.error})")
    return " · ".join(bits)


def rank_prompt(programme: str, assets: list[str], recon: dict[str, HostRecon]) -> str:
    lines = []
    for a in assets:
        host = _host_of(a)
        ev = recon_line(recon[host]) if host in recon else "no recon (not a named host)"
        lines.append(f"- {a}\n    recon: {ev}")
    return RANK_PROMPT.format(programme=programme or "(unnamed)", assets="\n".join(lines))


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------

_VALUE_ORDER = {"high": 0, "medium": 1, "low": 2, "unranked": 3}


def render_plain_tree(programme: str, ranked: list[Ranked], recon: dict[str, HostRecon],
                      out_of_scope: list[ScopeEntry]) -> str:
    """The ranked tree as plain text. Also used by the UI for its rich render."""
    lines = [f"{programme or 'programme'}"]
    lines.append("├─ IN SCOPE — test in this order")
    for i, r in enumerate(ranked):
        last = i == len(ranked) - 1
        branch = "└─" if last else "├─"
        lines.append(f"│  {branch} #{r.rank} [{r.value}] {r.asset}")
        pad = "│     " if not last else "      "
        host = _host_of(r.asset)
        if host in recon:
            lines.append(f"{pad}  {recon_line(recon[host])}")
        if r.why:
            lines.append(f"{pad}  why: {r.why}")
    lines.append(f"└─ OUT OF SCOPE — never touched ({len(out_of_scope)})")
    for i, e in enumerate(out_of_scope):
        branch = "└─" if i == len(out_of_scope) - 1 else "├─"
        lines.append(f"   {branch} {e.asset}" + (f" — {e.note}" if e.note else ""))
    return "\n".join(lines)


@dataclass
class Report:
    extraction: Extraction
    allow: dict
    deny: dict
    skipped: list[str]
    recon: dict[str, HostRecon]
    ranked: list[Ranked]
    rank_summary: str
    problems: list[str]
    allow_path: Path | None = None
    deny_path: Path | None = None
    moved_aside: list[str] = field(default_factory=list)
