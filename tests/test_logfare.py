"""Regression suite for the live Logfare catalog.

Run:  python tests/test_logfare.py   (from an activated venv)

Protects the thing that made the baked-in model list wrong: it offered models
Logfare had already retired, so /model accepted a name the proxy passed through
and the upstream answered with a 404. The catalog is now fetched from

    https://logfare.ai/v1/status?hours=24

and the load-bearing rule is the endpoint filter — an entry is a chat model only
if it advertises ``chat/completions``. Get that wrong and the picker offers an
image or TTS model, which is the same bug in a new costume.

No test here touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


# A payload shaped like the real one: two healthy chat models, one degraded chat
# model, and three that do NOT serve chat (image, TTS, STT) — those must be
# filtered out, and they are the whole point of the suite.
CATALOG = {
    "object": "list",
    "overall_status": "operational",
    "overall_uptime_percent": 83.6,
    "window_hours": 24,
    "data": [
        {"model_id": "deepseek-v4.1-flash", "display_name": "Deepseek v4.1 Flash",
         "status": "operational", "uptime_percent": 93.6, "checks": 0,
         "last_checked": None, "endpoints": ["chat/completions"],
         "real_traffic_total": 27701, "real_traffic_success_rate": 0.936},
        {"model_id": "qwen-3.8-27b", "display_name": "Qwen 3.8 27B",
         "status": "operational", "uptime_percent": 83.7,
         "endpoints": ["chat/completions"],
         "real_traffic_total": 4000, "real_traffic_success_rate": 0.835},
        {"model_id": "gemma-4-26b", "display_name": "Gemma 4 26B",
         "status": "unstable", "uptime_percent": 40.7,
         "endpoints": ["chat/completions"],
         "real_traffic_total": 3981, "real_traffic_success_rate": 0.407},
        # Not chat — must never reach /model.
        {"model_id": "flux-1.1-pro", "display_name": "Flux",
         "status": "operational", "uptime_percent": 99.0,
         "endpoints": ["images/generations"], "real_traffic_total": 120},
        {"model_id": "aura-2-en", "display_name": "Aura",
         "status": "operational", "uptime_percent": 99.0,
         "endpoints": ["audio/speech"], "real_traffic_total": 90},
        {"model_id": "whisper-large-v3-turbo", "display_name": "Whisper",
         "status": "operational", "uptime_percent": 99.0,
         "endpoints": ["audio/transcriptions"], "real_traffic_total": 40},
    ],
}


class _Resp(io.BytesIO):
    """Enough of an http.client.HTTPResponse for urlopen's context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(payload: dict | None = None, error: Exception | None = None):
    def _open(req, timeout=None):
        if error is not None:
            raise error
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())
    return _open


def test_chat_filter() -> None:
    """Only entries advertising chat/completions are offered as models."""
    from zimzilla import logfare as lf

    lf.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        models, err = lf.fetch_models(use_cache=False)
    check("fetch: a good response parses", err == "" and models is not None, err)
    ids = [m.id for m in models or []]
    check("filter: exactly the three chat models",
          ids == ["deepseek-v4.1-flash", "qwen-3.8-27b", "gemma-4-26b"], str(ids))
    for gone in ("flux-1.1-pro", "aura-2-en", "whisper-large-v3-turbo"):
        check(f"filter: {gone} (non-chat) is excluded", gone not in ids)


def test_health_is_read() -> None:
    """Status, uptime and traffic come through, and drive `healthy`."""
    from zimzilla import logfare as lf

    lf.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        models, _ = lf.fetch_models(use_cache=False)
    by_id = {m.id: m for m in models or []}

    ds = by_id["deepseek-v4.1-flash"]
    check("health: status is read", ds.status == "operational")
    check("health: uptime is read", ds.uptime == 93.6)
    check("health: traffic is read", ds.traffic == 27701)
    check("health: success rate is read", ds.success_rate == 0.936)
    check("health: an operational model is healthy", ds.healthy)

    gemma = by_id["gemma-4-26b"]
    check("health: an unstable model is not healthy", not gemma.healthy)
    check("health: the blurb names the problem", "unstable" in gemma.blurb,
          gemma.blurb)


def test_parse_is_total() -> None:
    """A malformed entry degrades; it never takes the catalog down."""
    from zimzilla import logfare as lf

    messy = lf._parse_models({"data": [
        {"model_id": "ok", "endpoints": ["chat/completions"],
         "uptime_percent": "nonsense", "real_traffic_total": "nonsense"},
        {"no_id": True, "endpoints": ["chat/completions"]},
        "not-a-dict",
        {"model_id": "no-endpoints"},
        {"model_id": "endpoints-not-a-list", "endpoints": "chat/completions"},
        {"model_id": "", "endpoints": ["chat/completions"]},
    ]})
    check("parse: only the one usable entry survives",
          [m.id for m in messy] == ["ok"], str([m.id for m in messy]))
    check("parse: a bad uptime degrades to 0, not a crash", messy[0].uptime == 0.0)
    check("parse: a bad traffic count degrades to 0", messy[0].traffic == 0)


def test_fetch_is_total() -> None:
    """fetch_models returns errors; it never raises into the TUI."""
    from zimzilla import logfare as lf

    lf.clear_cache()
    with mock.patch("urllib.request.urlopen",
                    _stub_urlopen(error=urllib.error.URLError("no route"))):
        models, err = lf.fetch_models(use_cache=False)
    check("fetch: a network failure returns None and a message",
          models is None and "could not reach" in err, err)
    check("fetch: a failed fetch does not populate the cache",
          lf.cached_ids() is None)

    http = urllib.error.HTTPError("u", 503, "unavailable", {}, None)
    with mock.patch("urllib.request.urlopen", _stub_urlopen(error=http)):
        models, err = lf.fetch_models(use_cache=False)
    check("fetch: an HTTP error says so", models is None and "503" in err, err)


def test_cache() -> None:
    """A warm cache answers without a second request; clear_cache empties it."""
    from zimzilla import logfare as lf

    lf.clear_cache()
    calls = []

    def counting(req, timeout=None):
        calls.append(req)
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", counting):
        lf.fetch_models()
        lf.fetch_models()          # must be served from the cache
    check("cache: a second fetch does not hit the network", len(calls) == 1,
          f"{len(calls)} calls")
    check("cache: cached_ids reports the catalog",
          lf.cached_ids() == ["deepseek-v4.1-flash", "qwen-3.8-27b", "gemma-4-26b"],
          str(lf.cached_ids()))
    lf.clear_cache()
    check("cache: clear_cache empties it", lf.cached_ids() is None)


def test_sources_prefers_live() -> None:
    """sources.models_for("logfare") is the fetched list, not the declaration."""
    from zimzilla import logfare as lf
    from zimzilla import sources as s

    proxy = "http://localhost:4001"
    lf.clear_cache()
    check("sources: a cold cache falls back to None (caller uses LOGFARE_MODELS)",
          s.models_for(proxy) is None)
    check("sources: catalog_for is None when cold", s.catalog_for(proxy) is None)

    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        lf.fetch_models()
    live = s.models_for(proxy)
    check("sources: a warm cache serves the live ids",
          live == ("deepseek-v4.1-flash", "qwen-3.8-27b", "gemma-4-26b"), str(live))
    check("sources: the live list wins over the declared one",
          live != s.LOGFARE_MODELS)
    catalog = s.catalog_for(proxy)
    check("sources: catalog_for serves the health objects",
          catalog is not None and len(catalog) == 3)
    check("sources: catalog_for is None for a source with no catalog",
          s.catalog_for("http://localhost:4000") is None)
    check("sources: catalog_for is None for an unknown endpoint",
          s.catalog_for("https://api.anthropic.com") is None)


def test_declared_fallback_is_plausible() -> None:
    """The declared fallback must not name models Logfare has retired.

    This is the regression that started all of it: LOGFARE_MODELS listed ten
    models that 404'd upstream, so /model offered them. The list cannot be kept
    honest by a test — only a fetch can — but it can be kept *small*, and this
    pins it to the set that was live when the fetch was added.
    """
    from zimzilla import sources as s
    from zimzilla.config import KNOWN_MODELS, PRICING

    check("fallback: LOGFARE_MODELS is the four live models",
          set(s.LOGFARE_MODELS) ==
          {"deepseek-v4.1-flash", "gemma-4-26b", "logfare/auto", "qwen-3.8-27b"},
          str(s.LOGFARE_MODELS))
    check("fallback: KNOWN_MODELS agrees", set(KNOWN_MODELS) == set(s.LOGFARE_MODELS))
    # A retired model must not still carry a price, or /cost quotes a 404.
    for dead in ("claude-opus-4.6", "deepseek-v3.2", "glm-5", "grok-4.6",
                 "kimi-k2.5", "kimi-k2-thinking", "gpt-oss-120b",
                 "gemma-4-31b", "space-bunny-alpha", "claude-sonnet-4.6"):
        check(f"fallback: {dead} is gone from PRICING", dead not in PRICING)
    # The default model must be one the upstream actually serves.
    check("fallback: DEFAULT_MODEL is in the fallback list",
          "deepseek-v4.1-flash" in s.LOGFARE_MODELS)


def test_is_logfare() -> None:
    """The host test distinguishes the gateway from the local proxy."""
    from zimzilla import logfare as lf

    check("host: a local proxy is not the gateway",
          not lf.is_logfare("http://localhost:4001"))
    check("host: the gateway URL is", lf.is_logfare("https://logfare.ai/v1"))
    check("host: the bare host is", lf.is_logfare("https://logfare.ai"))
    check("host: None is not", not lf.is_logfare(None))


def main() -> int:
    test_chat_filter()
    test_health_is_read()
    test_parse_is_total()
    test_fetch_is_total()
    test_cache()
    test_sources_prefers_live()
    test_declared_fallback_is_plausible()
    test_is_logfare()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed — FAILED: "
              + ", ".join(failed))
        return 1
    print(f"{len(RESULTS)}/{len(RESULTS)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
