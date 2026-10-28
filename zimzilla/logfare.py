"""Logfare's live catalog — what the upstream serves *right now*.

Logfare's model line-up moves faster than this repo does. A list baked into
``sources.py`` was correct the day it was written and wrong a month later: ten
of the fourteen models it named had been retired upstream, so ``/model``
offered them, the proxy accepted them, and the request 404'd. Nothing in the
client could tell, because a name on a list and a name the upstream answers to
look identical from here.

So the list is fetched, not declared. Logfare publishes per-model health at

    https://logfare.ai/v1/status?hours=24

— the JSON API behind https://logfare.ai/status — and each entry names the
endpoints that model serves. Filtering on ``chat/completions`` yields exactly
the models that can answer a Messages request, which is the only question this
module exists to answer.

The status payload is also the health report: ``status``, ``uptime_percent``
and a real-traffic success rate, so the picker can say a model is degraded
rather than letting the operator find out mid-turn.

Nothing here needs a key: the status API is public, so the catalog can be
refreshed before the operator has authenticated anything.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: The canonical host. Matching is by this substring, so ``https://logfare.ai``,
#: ``https://logfare.ai/`` and any subdomain path all count as the same source.
HOST = "logfare.ai"
DEFAULT_BASE_URL = "https://logfare.ai"
#: The Chat Completions base the harness talks to directly. Logfare serves it
#: itself, so no local translation proxy is involved.
DIRECT_BASE_URL = f"{DEFAULT_BASE_URL}/v1"

#: The port the retired local proxy listened on. Kept because a profile written
#: by an older install still names it, and ``active_endpoint`` has to recognise
#: that session as Logfare so it can be moved onto the direct route rather than
#: being reported as an unknown endpoint.
PROXY_PORT = 4001

#: Where the operator's own key lives. The launcher reads this first, then
#: ``$ANTHROPIC_AUTH_TOKEN``, and only then the template shipped in
#: ``packaging/logfare/``.
ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
PROFILE = ZIMZILLA_HOME / "logfare" / "source"

#: The committed profile, seeded into the operator's own the first time a key is
#: set. It is a template: the token is a placeholder, and no credential ships.
DEFAULT_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_PROFILE = DEFAULT_ROOT / "packaging" / "logfare" / "source"

#: Logfare issues keys with this prefix. Treated as a hint, never a gate: a real
#: key of an unexpected shape is worth more stored than refused.
KEY_PREFIX = "lfu_"

#: Strings that mean "nobody has put a key here yet". The shipped profile is a
#: template now, so its token is one of these — and a template must never be
#: mistaken for a credential, or the harness sends `lfu_REPLACE_ME` upstream and
#: reports the 401 as if the account were broken.
PLACEHOLDERS = ("replace_me", "your_key", "yourkey", "example", "changeme")


def is_placeholder_token(value: str | None) -> bool:
    """Whether *value* is a template marker rather than a real key."""
    low = (value or "").strip().lower()
    if not low:
        return True
    return any(marker in low for marker in PLACEHOLDERS)

#: The endpoint that answers "what is up". ``hours`` only sets the uptime window.
STATUS_PATH = "/v1/status"

#: The endpoint every entry must advertise to be offered as a chat model.
CHAT_ENDPOINT = "chat/completions"

#: A fetched catalog older than this is refetched rather than trusted. Logfare's
#: line-up changes on the order of weeks, but its *health* changes by the hour,
#: so this is short enough for the status column to mean something.
CACHE_TTL = 300.0


@dataclass(frozen=True)
class Model:
    """One chat-capable entry from Logfare's status API."""

    id: str
    label: str = ""
    status: str = ""
    uptime: float = 0.0
    traffic: int = 0
    success_rate: float = 0.0

    @property
    def healthy(self) -> bool:
        """Whether Logfare calls this model operational."""
        return self.status == "operational"

    @property
    def blurb(self) -> str:
        """A one-line health note for the picker."""
        bits = []
        if self.status:
            bits.append(self.status)
        if self.uptime:
            bits.append(f"{self.uptime:g}% up")
        if self.traffic:
            bits.append(f"{self.traffic:,} req/24h")
        return " · ".join(bits)


#: The last-fetched catalog, with the monotonic time it was fetched at. One
#: entry: the status API is public, so unlike TokenHarbour's there is no key to
#: partition it by.
_CACHE: list[tuple[list[Model], float]] = []


def _parse_models(payload: dict) -> list[Model]:
    """The chat-capable entries, in the order the API returned them.

    A malformed entry is skipped rather than allowed to take the catalog down:
    this runs inside a TUI command, and half a model list beats an exception.
    """
    out: list[Model] = []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("model_id") or "").strip()
        if not mid:
            continue
        endpoints = entry.get("endpoints") or []
        if not isinstance(endpoints, list) or CHAT_ENDPOINT not in endpoints:
            continue
        try:
            uptime = float(entry.get("uptime_percent") or 0.0)
        except (TypeError, ValueError):
            uptime = 0.0
        try:
            traffic = int(entry.get("real_traffic_total") or 0)
        except (TypeError, ValueError):
            traffic = 0
        try:
            rate = float(entry.get("real_traffic_success_rate") or 0.0)
        except (TypeError, ValueError):
            rate = 0.0
        out.append(Model(
            id=mid,
            label=str(entry.get("display_name") or ""),
            status=str(entry.get("status") or ""),
            uptime=uptime,
            traffic=traffic,
            success_rate=rate,
        ))
    return out


def fetch_models(*, hours: int = 24, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch Logfare's chat catalog. Returns ``(models, error)``.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach Logfare" rather than take the session down.
    ``models`` is ``None`` on failure, ``[]`` for a genuinely empty catalog —
    the caller can tell the two apart.
    """
    if use_cache and _CACHE:
        models, at = _CACHE[0]
        if (time.monotonic() - at) < CACHE_TTL:
            return models, ""

    url = f"{DEFAULT_BASE_URL}{STATUS_PATH}?hours={int(hours)}"
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"

    models = _parse_models(payload)
    _CACHE[:] = [(models, time.monotonic())]
    return models, ""


def model_ids(**kw) -> tuple[list[str] | None, str]:
    """Every chat model id, catalog order preserved — what ``/model`` offers."""
    models, err = fetch_models(**kw)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def catalog_age() -> float | None:
    """Seconds since the cache was filled, or ``None`` if it never was.

    The age is what lets a caller say *how* stale a list is instead of only
    whether one exists. It is deliberately not clamped to ``CACHE_TTL``: an
    expired catalog is exactly the case worth reporting, and a caller that
    cannot see the real number cannot tell "fetched a minute ago" from
    "fetched when this session started, hours ago".
    """
    return (time.monotonic() - _CACHE[0][1]) if _CACHE else None


def cached_models() -> list[Model] | None:
    """The last-fetched catalog, if it is still fresh — else ``None``.

    ``/model`` runs on the UI thread and must not block on a fetch, so it reads
    whatever the last ``/logfare-models`` (or the startup warm-up) left behind.

    **Expiry is checked here, and that is load-bearing.** This used to return
    the last catalog however old it was, while ``fetch_models`` refused to
    reuse anything past ``CACHE_TTL`` — so a cache filled once at startup was
    served for the rest of the session, and ``/model`` called it live. Logfare
    retires models without notice; the whole reason the catalog is fetched
    rather than declared is that it changes. An expired cache now reads as
    ``None``, which every caller already treats as "no catalog", so the
    fallback and the honest "not fetched yet" wording both follow for free.
    """
    if not _CACHE:
        return None
    models, at = _CACHE[0]
    if (time.monotonic() - at) >= CACHE_TTL:
        return None
    return list(models)


def cached_ids() -> list[str] | None:
    """The last-fetched ids, without touching the network."""
    models = cached_models()
    return [m.id for m in models] if models is not None else None


def clear_cache() -> None:
    """Drop the cached catalog. Called when the endpoint changes."""
    _CACHE.clear()


def health_for(model: str) -> Model | None:
    """The catalog's entry for *model*, if the catalog knows it."""
    for m in cached_models() or []:
        if m.id == model:
            return m
    return None


def is_logfare(base_url: str | None) -> bool:
    """Whether an endpoint points at Logfare itself rather than a local proxy."""
    return HOST in (base_url or "")


# ---- the credential ---------------------------------------------------------
#
# No Logfare key ships in this repository. A key committed to a public
# repository is a shared budget anyone can spend, and it cannot be taken back
# out of everyone's clone, so the shipped profile is a template with a
# placeholder. The operator's own key goes in ``~/.zimzilla/logfare/source``,
# written by ``/logfare-api-setup`` or by setup.sh. Nothing here echoes a key:
# the confirmation names the last four characters, which tells two keys apart
# and is not one.


def _export_line(line: str) -> tuple[str, str]:
    """``(name, value)`` for one ``export VAR="value"`` line, else two blanks."""
    line = line.strip()
    if not line.startswith("export ") or "=" not in line:
        return "", ""
    name, _, value = line[len("export "):].partition("=")
    name, value = name.strip(), value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return name, value


def _read_exports(path: Path) -> dict[str, str]:
    """Parse a profile's exports without a shell. Never raises, never prints."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        name, value = _export_line(line)
        if name:
            out[name] = value
    return out


def load_credentials() -> tuple[str, str, str] | None:
    """``(token, model, origin)`` for the key in force, or ``None``.

    The same precedence the launcher uses, in the same order: the operator's own
    profile, then ``$ANTHROPIC_AUTH_TOKEN``, then the shared profile in the tree.
    ``origin`` is a path or an env var name, for showing where a key came from
    without showing the key.
    """
    values = _read_exports(PROFILE)
    token = values.get("ANTHROPIC_AUTH_TOKEN", "")
    if token and not is_placeholder_token(token):
        return token, values.get("ANTHROPIC_MODEL", ""), str(PROFILE)
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    if token and not is_placeholder_token(token):
        return token, os.environ.get("ANTHROPIC_MODEL", ""), "$ANTHROPIC_AUTH_TOKEN"
    values = _read_exports(SHIPPED_PROFILE)
    token = values.get("ANTHROPIC_AUTH_TOKEN", "")
    if token and not is_placeholder_token(token):
        return token, values.get("ANTHROPIC_MODEL", ""), str(SHIPPED_PROFILE)
    return None


def has_key() -> bool:
    """Whether any Logfare key is in force, shared or own."""
    return load_credentials() is not None


def looks_like_key(value: str) -> bool:
    """A bare token, not a pasted shell line or a fragment of one."""
    value = (value or "").strip()
    if not 16 <= len(value) <= 200:
        return False
    return all(ch.isalnum() or ch in "_-." for ch in value)


def active_endpoint(base_url: str | None) -> bool:
    """Whether a session endpoint is Logfare — the host or the retired proxy.

    ``:4001`` is kept because a profile written by an older install still names
    it; recognising it is what lets the session be moved onto the direct route
    instead of being reported as pointing at an unknown endpoint.
    """
    base = (base_url or "").rstrip("/")
    return is_logfare(base) or base.endswith(f":{PROXY_PORT}")


def save_key(token: str, model: str = "") -> Path:
    """Write *token* to the operator's profile, mode 600, and return its path.

    Rewritten rather than appended, so setting a key twice cannot leave two
    ``ANTHROPIC_AUTH_TOKEN`` lines for the shell to disagree about. Everything
    else in the file is kept — the model aliases, and the empty
    ``ANTHROPIC_API_KEY`` that is what keeps the token authoritative when a real
    key is inherited from the shell. A profile that does not exist yet is seeded
    from the shipped one for the same reason.
    """
    token = token.strip()
    lines: list[str] = []
    for source in (PROFILE, SHIPPED_PROFILE):
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
            break
        except OSError:
            continue
    if not lines:
        lines = [
            "# ZimZilla — Logfare profile (written by /logfare-api-setup)",
            "# Treat this file as a credential. Never commit it.",
            "",
        ]

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        name, _ = _export_line(line)
        if name == "ANTHROPIC_AUTH_TOKEN":
            out.append(f'export ANTHROPIC_AUTH_TOKEN="{token}"')
            seen.add(name)
        elif name == "ANTHROPIC_BASE_URL":
            # Always the direct route. A profile written before the proxy was
            # retired still says localhost:4001; leaving that in place would
            # write a working key into a file that points at nothing.
            out.append(f'export ANTHROPIC_BASE_URL="{DIRECT_BASE_URL}"')
            seen.add(name)
        elif name == "ANTHROPIC_MODEL" and model:
            out.append(f'export ANTHROPIC_MODEL="{model}"')
            seen.add(name)
        else:
            out.append(line)
            if name:
                seen.add(name)
    if "ANTHROPIC_BASE_URL" not in seen:
        out.insert(0, f'export ANTHROPIC_BASE_URL="{DIRECT_BASE_URL}"')
    if "ANTHROPIC_AUTH_TOKEN" not in seen:
        out.append(f'export ANTHROPIC_AUTH_TOKEN="{token}"')
    if "ANTHROPIC_API_KEY" not in seen:
        # Load-bearing: an empty string is what stops a real API key in the
        # environment from taking precedence over the token above.
        out.append('export ANTHROPIC_API_KEY=""')

    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(PROFILE.parent, 0o700)
    PROFILE.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(PROFILE, 0o600)
    return PROFILE
