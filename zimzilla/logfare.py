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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

#: The canonical host. Matching is by this substring, so ``https://logfare.ai``,
#: ``https://logfare.ai/`` and any subdomain path all count as the same source.
HOST = "logfare.ai"
DEFAULT_BASE_URL = "https://logfare.ai"

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


def cached_models() -> list[Model] | None:
    """The last-fetched catalog, or ``None`` if nothing has been fetched.

    ``/model`` runs on the UI thread and must not block on a fetch, so it reads
    whatever the last ``/logfare-models`` (or the startup warm-up) left behind.
    A cold cache returns ``None`` and the caller falls back to its own list.
    """
    return list(_CACHE[0][0]) if _CACHE else None


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
