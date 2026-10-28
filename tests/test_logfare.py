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

import asyncio
import io
import json
import os
import sys
import tempfile
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


def test_cache_expiry() -> None:
    """An expired catalog must not be served as if it were live.

    The bug this pins: ``fetch_models`` has always refused to *reuse* an entry
    past ``CACHE_TTL``, but ``cached_models`` read it back regardless — so a
    catalog fetched once at startup was reported by ``/model`` for the rest of
    the session, under the words "the catalog is live". Logfare retires models
    without notice, which is the entire reason the list is fetched rather than
    declared, so a list that never expires is the original bug wearing a hat.

    Nothing here sleeps: the cache entry's timestamp is moved backwards instead,
    which is the same thing to a ``monotonic()`` comparison and costs no time.
    """
    import time

    from zimzilla import logfare as lf
    from zimzilla import sources as s

    proxy = "http://localhost:4001"
    lf.clear_cache()
    check("expiry: a cold cache has no age", lf.catalog_age() is None,
          str(lf.catalog_age()))

    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        lf.fetch_models()
    check("expiry: a fresh catalog is served", lf.cached_ids() is not None)
    check("expiry: its age is reported, and small",
          (lf.catalog_age() or 999) < 5.0, str(lf.catalog_age()))

    # Age it well past the TTL — the state a long-running session reaches on
    # its own, with no code path having cleared the cache in between.
    lf._CACHE[:] = [(lf._CACHE[0][0], time.monotonic() - (lf.CACHE_TTL + 60))]
    check("expiry: an expired catalog reads as None",
          lf.cached_models() is None, str(lf.cached_models()))
    check("expiry: cached_ids agrees", lf.cached_ids() is None,
          str(lf.cached_ids()))
    check("expiry: the real age is still reported, unclamped",
          (lf.catalog_age() or 0) >= lf.CACHE_TTL, str(lf.catalog_age()))
    check("expiry: sources.models_for falls back rather than serving it",
          s.models_for(proxy) is None, str(s.models_for(proxy)))
    check("expiry: sources.catalog_for agrees", s.catalog_for(proxy) is None)
    check("expiry: health_for finds nothing in an expired catalog",
          lf.health_for("deepseek-v4.1-flash") is None)

    # A re-fetch is what makes it live again — and fetch_models must not have
    # been serving the stale entry to itself either.
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        lf.fetch_models()
    check("expiry: refetching restores it", lf.cached_ids() is not None)
    lf.clear_cache()


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
    honest by a test — only a fetch can — so this pins it to the set that was
    live on a known date, and the *chat* filter is what keeps image, TTS and STT
    entries out. A model that appears upstream after that date is picked up by
    the fetch (and routed by the proxy's wildcard), not by editing this test.
    """
    from zimzilla import sources as s
    from zimzilla.config import KNOWN_MODELS, PRICING

    # Every chat model Logfare served on 2026-10-19, and nothing else. The list
    # is the cold-cache half of /model, so a served model missing here is one
    # the picker hides until the fetch lands.
    served = {
        "claude-haiku-5-5", "deepseek-v4-pro-0813", "deepseek-v4.1-flash",
        "gemma-4-26b", "glm-5.3", "glm-5.3-flash", "kimi-k2.6",
        "logfare/auto", "qwen-3.8-27b",
    }
    check("fallback: LOGFARE_MODELS is the live chat catalog",
          set(s.LOGFARE_MODELS) == served,
          str(sorted(set(s.LOGFARE_MODELS) ^ served)))
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


def test_profile_key(tmp: Path) -> None:
    """Where the key comes from, and what writing one does to the profile."""
    from zimzilla import logfare as lf

    profile = tmp / "zhome" / "logfare" / "source"
    # Nothing anywhere: no operator file, no shipped file, no environment token.
    with mock.patch.object(lf, "PROFILE", profile), \
         mock.patch.object(lf, "SHIPPED_PROFILE", tmp / "absent"), \
         mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": ""}):
        check("key: nothing set -> None", lf.load_credentials() is None,
              str(lf.load_credentials()))
        check("key: has_key agrees", not lf.has_key())

    with mock.patch.object(lf, "PROFILE", profile), \
         mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": ""}):
        # packaging/logfare/source ships a placeholder, and a placeholder must
        # never be handed upstream as if it were a key: that turns "you have not
        # set one yet" into a 401 that reads like a broken account.
        check("key: the shipped template is not a credential",
              lf.load_credentials() is None, str(lf.load_credentials()))
        check("key: has_key agrees", not lf.has_key())

    # A shipped profile that carries a real token is still honoured, and says so
    # by origin — that is how a maintainer ships a key on purpose, with its own
    # budget and the knowledge that it is public.
    shipped_real = tmp / "shipped-with-key"
    shipped_real.parent.mkdir(parents=True, exist_ok=True)
    shipped_real.write_text(
        'export ANTHROPIC_AUTH_TOKEN="lfu_shipped_key_0123456789abcd"\n'
        'export ANTHROPIC_MODEL="deepseek-v4.1-flash"\n'
        'export ANTHROPIC_API_KEY=""\n'
        'export MODEL_QWEN="qwen-3.8-27b"\n', encoding="utf-8")
    with mock.patch.object(lf, "PROFILE", profile), \
         mock.patch.object(lf, "SHIPPED_PROFILE", shipped_real), \
         mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": ""}):
        # The shipped profile is the fallback, and says so by origin.
        creds = lf.load_credentials()
        check("key: a shipped key with a real token is found", creds is not None)
        check("key: reported as the shipped one",
              creds is not None and creds[2] == str(shipped_real),
              str(creds and creds[2]))

        # An environment token beats the shipped key...
        with mock.patch.dict(os.environ,
                             {"ANTHROPIC_AUTH_TOKEN": "env-token-0123456789"}):
            creds = lf.load_credentials()
            check("key: the environment beats the shipped profile",
                  creds is not None and creds[2] == "$ANTHROPIC_AUTH_TOKEN",
                  str(creds and creds[2]))

        # ...and the operator's own file beats both.
        first = "lfu_first_key_0123456789abcd"
        path = lf.save_key(first, "deepseek-v4.1-flash")
        check("key: saved where the launcher looks", path == profile, str(path))
        text = profile.read_text(encoding="utf-8")
        check("key: the token is in the file",
              f'ANTHROPIC_AUTH_TOKEN="{first}"' in text)
        check("key: the profile names the direct route",
              'ANTHROPIC_BASE_URL="https://logfare.ai/v1"' in text, text[:200])
        check("key: the model aliases came along", "MODEL_QWEN" in text)
        check("key: the empty API key came along",
              'ANTHROPIC_API_KEY=""' in text)
        check("key: mode is 600",
              (profile.stat().st_mode % 0o1000) == 0o600,
              oct(profile.stat().st_mode % 0o1000))
        check("key: the directory is 700",
              (profile.parent.stat().st_mode % 0o1000) == 0o700,
              oct(profile.parent.stat().st_mode % 0o1000))
        with mock.patch.dict(os.environ,
                             {"ANTHROPIC_AUTH_TOKEN": "env-token-0123456789"}):
            creds = lf.load_credentials()
            check("key: the operator's file beats the environment",
                  creds is not None and creds[0] == first
                  and creds[2] == str(profile), str(creds))

        # Twice must not leave two token lines for the shell to disagree about.
        second = "lfu_second_key_0123456789ab"
        lf.save_key(second, "")
        text = profile.read_text(encoding="utf-8")
        check("key: one token line after two writes",
              text.count("ANTHROPIC_AUTH_TOKEN=") == 1,
              str(text.count("ANTHROPIC_AUTH_TOKEN=")))
        check("key: the second key is the stored one", second in text)
        check("key: the model is kept when none is given",
              'ANTHROPIC_MODEL="deepseek-v4.1-flash"' in text)

        # A profile written before the proxy was retired names :4001. Writing a
        # new key must move it to the direct route rather than leave a working
        # credential pointing at an address nothing answers.
        profile.write_text(
            'export ANTHROPIC_BASE_URL="http://localhost:4001"\n'
            'export ANTHROPIC_AUTH_TOKEN="lfu_old_key_0123456789abcd"\n'
            'export ANTHROPIC_MODEL="deepseek-v4.1-flash"\n'
            'export ANTHROPIC_API_KEY=""\n', encoding="utf-8")
        lf.save_key("lfu_third_key_0123456789abc", "")
        text = profile.read_text(encoding="utf-8")
        check("key: an old :4001 profile is moved to the direct route",
              'ANTHROPIC_BASE_URL="https://logfare.ai/v1"' in text, text[:200])
        check("key: the new key is the stored one",
              "lfu_third_key_0123456789abc" in text, text[:200])

    check("key: lfu_ and a long body is accepted",
          lf.looks_like_key("lfu_" + "a" * 32))
    check("key: a pasted export line is rejected",
          not lf.looks_like_key('export ANTHROPIC_AUTH_TOKEN="x"'))
    check("key: something short is rejected", not lf.looks_like_key("short"))
    check("key: an empty string is rejected", not lf.looks_like_key(""))

    check("key: :4001 is a Logfare endpoint",
          lf.active_endpoint("http://localhost:4001"))
    check("key: logfare.ai is one too", lf.active_endpoint("https://logfare.ai/v1"))
    check("key: :4002 is not", not lf.active_endpoint("http://localhost:4002"))
    check("key: None is not", not lf.active_endpoint(None))


def test_key_command(tmp: Path) -> None:
    """`/logfare-api-setup` stores the key, applies it, and never echoes it."""
    from zimzilla import logfare as lf
    from zimzilla import sources as sources_mod
    from zimzilla.config import Config
    from zimzilla.ui.app import ZimZillaApp

    profile = tmp / "zhome" / "logfare" / "source"
    key = "lfu_live_0123456789abcdef"
    src = sources_mod.Source(key="logfare", label="Logfare", port=4001,
                             profile=profile, service=None, config=None,
                             model="deepseek-v4.1-flash", root=ROOT,
                             direct=sources_mod.LOGFARE_DIRECT)

    def run(base_url: str, args: list[str]) -> tuple[list[str], Config, list[str]]:
        said: list[str] = []
        restarted: list[str] = []
        cfg = Config.from_env(workdir=tmp, api_key="old-key", base_url=base_url,
                              model="deepseek-v4.1-flash")
        app = ZimZillaApp(cfg)
        app._sys_line = lambda msg, **kw: said.append(str(msg))

        async def go() -> None:
            async with app.run_test(size=(110, 34)) as pilot:
                # The restart runs in a worker, so the patches have to outlive
                # the command call itself.
                with mock.patch.object(lf, "PROFILE", profile), \
                     mock.patch.object(sources_mod, "discover",
                                       return_value={"logfare": src}), \
                     mock.patch.object(sources_mod, "restart_proxy",
                                       side_effect=lambda s, **k: (
                                           restarted.append(s.key),
                                           (True, "restart"))[1]):
                    app._cmd_logfare_key(args)
                    await pilot.pause()
                    await pilot.pause()

        asyncio.run(go())
        return said, cfg, restarted

    # Straight at logfare.ai: this client sends the key, so no restart is needed.
    said, cfg, restarted = run("https://logfare.ai/v1", [key])
    check("cmd: the key is stored", profile.exists()
          and key in profile.read_text(encoding="utf-8"))
    check("cmd: it is applied to the live session", cfg.auth_token == key,
          str(cfg.auth_token))
    check("cmd: the stale api_key is cleared", cfg.api_key == "")
    check("cmd: no proxy restart on a direct endpoint", restarted == [], str(restarted))
    check("cmd: it says no restart was needed",
          any("no restart" in m for m in said), str(said))
    check("cmd: the key itself is never echoed",
          all(key not in m for m in said), str(said))
    check("cmd: the confirmation names the last four characters",
          any(key[-4:] in m for m in said), str(said))

    # A session launched on the retired :4001 profile is moved onto the direct
    # route: there is no proxy there any more, and the key is sent by this
    # client rather than held by a bridge that would need a restart.
    said, cfg, restarted = run("http://localhost:4001", [key])
    check("cmd: a legacy proxy endpoint is moved direct",
          cfg.base_url == "https://logfare.ai/v1", cfg.base_url)
    check("cmd: no proxy restart is attempted",
          restarted == [], str(restarted))
    check("cmd: the client sends the new key after the move",
          cfg.auth_token == key, str(cfg.auth_token))
    check("cmd: still no echo of the key", all(key not in m for m in said), str(said))

    # A pasted shell line is refused, and nothing is written.
    profile.unlink(missing_ok=True)
    said, _, _ = run("https://logfare.ai/v1", ['export ANTHROPIC_AUTH_TOKEN="x"'])
    check("cmd: a pasted export line is refused", not profile.exists(), str(said))
    check("cmd: and it says why", any("does not look like" in m for m in said), str(said))

    # The wiring that makes the command reachable at all.
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    check("cmd: dispatch entry",
          '"logfare-api-setup": lambda a: self._cmd_logfare_key(a)' in app_src)
    check("cmd: stays typeable mid-turn",
          '"logfare-api-setup", "logfare-models",' in app_src)
    check("cmd: help row", '("/logfare-api-setup [key]"' in app_src)
    check("cmd: palette entry",
          '"/logfare-api-setup", "use your own Logfare key' in app_src)


def main() -> int:
    test_chat_filter()
    test_health_is_read()
    test_parse_is_total()
    test_fetch_is_total()
    test_cache()
    test_cache_expiry()
    test_sources_prefers_live()
    test_declared_fallback_is_plausible()
    test_is_logfare()
    with tempfile.TemporaryDirectory() as td:
        test_profile_key(Path(td) / "a")
        test_key_command(Path(td) / "b")

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
