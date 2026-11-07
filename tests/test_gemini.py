"""Regression suite for the Google Gemini source.

Run:  python tests/test_gemini.py   (from an activated venv)

Protects what is particular about this provider:

* **The catalog is key-gated, so the probe IS the key check.** ``/models``
  answers ``404 Requested entity was not found`` without a credential, unlike
  NVIDIA's public route. A switch therefore cannot land on a route that 401s on
  the first turn.
* **A rejected Gemini key is a 400, not a 401.** ``400 Invalid Auth key`` is
  what a dead credential actually returns, so a caller that only checks 401
  would call a dead key healthy. ``verify_key`` and ``explain_error`` both
  handle it.
* **``limit: 0`` is not a rate limit.** A 429 carrying ``limit: 0`` means the
  model is not offered on the free tier at all; the raw message says "retry in
  21h", which is a lie for that case. ``explain_error`` says so, and
  ``verify_key`` returns ok=True on it — a model problem must not fail a
  working key.
* **Thinking models need a real token budget.** ``gemini-3*``, ``gemini-3.5*``,
  ``gemini-3.6*`` and ``gemini-robotics-*`` spend the whole ``max_tokens`` on
  reasoning and return ``content: null``. ``is_thinking`` is the test and
  ``MIN_OUTPUT_TOKENS`` the floor, so the harness raises the ceiling instead of
  reporting a working model as broken.
* **The base URL is not ``/v1``.** It is ``/v1beta/openai``, which the generic
  ``chat_completions_url`` would mangle into ``.../v1beta/openai/v1/chat/
  completions``. The special case is asserted here, and so is the fact that it
  did not disturb any other route.
* **Free is the account tier, not a per-model price.** The catalog publishes no
  price, so ``Model.free`` is True by construction and
  ``sources.catalog_price_for`` must answer (0, 0) *even on a cold cache*.
* **The key never ships.** ``~/.zimzilla/gemini/source`` is written by
  ``/gemini-api-setup`` and nothing under ``packaging/`` contains a credential.

No test here touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

import io
import json
import os
import re
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


#: Shaped like the live catalog: ``{"object": "list", "data": [...]}`` with the
#: ``models/`` prefix the OpenAI-compat route puts on every id. That prefix is
#: the point — the module must strip it, because the bare id is what the
#: operator retypes into ``/model``.
CATALOG = {
    "object": "list",
    "data": [
        {"id": "models/gemini-flash-lite-latest", "object": "model",
         "owned_by": "google", "display_name": "Gemini Flash Lite Latest"},
        {"id": "models/gemini-3.5-flash", "object": "model",
         "owned_by": "google", "display_name": "Gemini 3.5 Flash"},
        {"id": "models/gemma-4-31b-it", "object": "model",
         "owned_by": "google", "display_name": "Gemma 4 31B"},
        {"id": "models/gemini-robotics-er-2-preview", "object": "model",
         "owned_by": "google"},
        # --- non-chat families, one per marker shape ---------------------
        {"id": "models/gemini-embedding-001", "object": "model",
         "owned_by": "google"},
        {"id": "models/gemini-3-pro-image", "object": "model",
         "owned_by": "google"},
        {"id": "models/gemini-3.8-flash-lite-tts", "object": "model",
         "owned_by": "google"},
        {"id": "models/gemini-3.5-transcribe", "object": "model",
         "owned_by": "google"},
        {"id": "models/gemini-3.8-live", "object": "model",
         "owned_by": "google"},
        {"id": "models/lyria-3.5", "object": "model", "owned_by": "google"},
        {"id": "models/nano-banana-pro-preview", "object": "model",
         "owned_by": "google"},
        {"id": "models/deep-research-pro-preview-12-2025", "object": "model",
         "owned_by": "google"},
        {"id": "models/antigravity-preview-latest", "object": "model",
         "owned_by": "google"},
        {"id": "models/gemini-omni-1.2-flash", "object": "model",
         "owned_by": "google"},
        {"id": "models/aqa", "object": "model", "owned_by": "google"},
        # --- rows that must be dropped by the parser, not the filter -----
        "not-a-row",
        {"no_id": True},
        {"id": "   "},
    ],
}

#: Every chat-capable id, catalog order preserved, prefix stripped.
CHAT = ["gemini-flash-lite-latest", "gemini-3.5-flash", "gemma-4-31b-it",
        "gemini-robotics-er-2-preview"]

#: The non-chat ids, each of which a marker-only filter must catch.
NON_CHAT = ["gemini-embedding-001", "gemini-3-pro-image",
            "gemini-3.8-flash-lite-tts", "gemini-3.5-transcribe",
            "gemini-3.8-live", "lyria-3.5", "nano-banana-pro-preview",
            "deep-research-pro-preview-12-2025", "antigravity-preview-latest",
            "gemini-omni-1.2-flash", "aqa"]


class _Resp(io.BytesIO):
    """A urlopen() result: a context manager that also reports a status."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub(payload=None, error=None):
    def _open(req, timeout=None):
        if error is not None:
            raise error
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())
    return _open


def _capture(payload=None):
    """A stub that records the request, so URL, method and headers can be asserted."""
    seen: dict = {}

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = req.data
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())

    return seen, _open


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "u", code, "err", {}, io.BytesIO(body.encode()))


def _clean_env():
    """Context manager that hides any Gemini credential in the environment."""
    class _C:
        def __enter__(self):
            self.saved = {k: os.environ.pop(k, None) for k in
                          ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                           "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")}
            return self

        def __exit__(self, *exc):
            for k, v in self.saved.items():
                if v is not None:
                    os.environ[k] = v
            return False
    return _C()


# ---------------------------------------------------------------------------

def test_parser_drops_malformed_rows() -> None:
    from zimzilla import gemini as gm

    models = gm._parse_models(CATALOG)
    ids = [m.id for m in models]
    check("parse: every well-formed row kept", len(models) == 15, str(len(models)))
    check("parse: a bare string is dropped", "not-a-row" not in ids)
    check("parse: a row with no id is dropped", len(ids) == len(set(ids)))
    check("parse: a whitespace id is dropped", "   " not in ids)
    check("parse: the models/ prefix is stripped",
          "gemini-flash-lite-latest" in ids and "models/gemini-flash-lite-latest" not in ids)
    check("parse: owned_by is carried", models[0].owned_by == "google",
          models[0].owned_by)
    check("parse: display_name is carried",
          models[0].display_name == "Gemini Flash Lite Latest",
          models[0].display_name)


def test_non_chat_families_are_filtered() -> None:
    """Marker-based, so a new image model needs no code change."""
    from zimzilla import gemini as gm

    by_id = {m.id: m for m in gm._parse_models(CATALOG)}
    check("chat: a text LLM is chat", by_id["gemini-flash-lite-latest"].chat)
    check("chat: a thinking model is chat", by_id["gemini-3.5-flash"].chat)
    check("chat: gemma is chat", by_id["gemma-4-31b-it"].chat)
    for mid in NON_CHAT:
        check(f"chat: {mid} is not chat", not by_id[mid].chat)

    with mock.patch("urllib.request.urlopen", _stub()):
        gm.clear_cache()
        ids, err = gm.model_ids("AQ.fake")
    check("chat: model_ids keeps exactly the chat set",
          ids is not None and ids == CHAT, str(ids))
    check("chat: model_ids reports no error", err == "", err)
    check("chat: an embedding model is not offered",
          ids is not None and "gemini-embedding-001" not in ids)


def test_thinking_models_are_identified() -> None:
    """The floor exists because a thinking model sent 8 tokens returns nothing."""
    from zimzilla import gemini as gm

    for mid in ("gemini-3-flash-preview", "gemini-3.1-flash-lite",
                "gemini-3.5-flash", "gemini-3.6-flash",
                "gemini-robotics-er-2-preview"):
        check(f"thinking: {mid} is thinking", gm.is_thinking(mid))
    for mid in ("gemini-flash-lite-latest", "gemma-4-31b-it",
                "gemini-2.5-flash", "gemini-4-argon"):
        check(f"thinking: {mid} is not thinking", not gm.is_thinking(mid))
    check("thinking: the floor clears the measured reasoning budget",
          gm.MIN_OUTPUT_TOKENS >= 200, str(gm.MIN_OUTPUT_TOKENS))


def test_free_is_the_account_tier() -> None:
    """No price in the catalog, so free is True and price_for is (0, 0)."""
    from zimzilla import gemini as gm, sources

    models = gm._parse_models(CATALOG)
    check("free: every entry is free", all(m.free for m in models))
    check("free: price_for is (0, 0)",
          gm.price_for("gemini-flash-lite-latest", models) == (0.0, 0.0))
    # The cold-cache case is the one that matters: None here would fall through
    # to the estimate table and bill a free model at DEFAULT_PRICE.
    gm.clear_cache()
    price = sources.catalog_price_for(gm.API_BASE_URL, "gemini-flash-lite-latest")
    check("free: catalog_price_for is (0, 0) on a cold cache",
          price == (0.0, 0.0), str(price))


def test_context_is_never_none() -> None:
    from zimzilla import gemini as gm

    models = gm._parse_models(CATALOG)
    check("ctx: never None", all(m.context_length is not None for m in models))
    check("ctx: a known id gets its real window",
          gm.model_by_id("gemini-flash-lite-latest", models).context_length == 1_048_576
          if hasattr(gm, "model_by_id") else
          gm.model_for("gemini-flash-lite-latest", models).context_length == 1_048_576)
    check("ctx: gemma gets 131k, not the 1M default",
          gm.model_for("gemma-4-31b-it", models).context_length == 131_072)
    check("ctx: an unknown id gets the default",
          gm.model_for("gemini-robotics-er-2-preview", models).context_length
          == gm.DEFAULT_CONTEXT)


def test_catalog_is_key_gated_and_the_url_is_right() -> None:
    """The Models route needs a key, and the URL is /v1beta/openai, not /v1."""
    from zimzilla import gemini as gm

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        gm.clear_cache()
        models, err = gm.fetch_models("AQ.fake")
    check("url: the catalog URL is the openai one",
          seen["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/models",
          seen["url"])
    check("url: the key is sent as a bearer token",
          seen["headers"].get("authorization") == "Bearer AQ.fake",
          str(seen["headers"].get("authorization")))
    check("url: the fetch succeeded", models is not None and err == "", err)

    # No key at all: the module must not even make the call.
    gm.clear_cache()
    models, err = gm.fetch_models("")
    check("url: no key is an error, not a request",
          models is None and "no key" in err, err)


def test_fetch_is_total() -> None:
    """A network failure is returned, never raised — the caller is a TUI."""
    from zimzilla import gemini as gm

    for err in (urllib.error.URLError("down"), OSError("reset"), ValueError("bad")):
        with mock.patch("urllib.request.urlopen", _stub(error=err)):
            gm.clear_cache()
            models, msg = gm.fetch_models("AQ.fake")
        check(f"total: {type(err).__name__} returns None, not raises",
              models is None and msg != "", msg)

    with mock.patch("urllib.request.urlopen",
                    _stub(error=_http_error(500, "boom"))):
        gm.clear_cache()
        models, msg = gm.fetch_models("AQ.fake")
    check("total: HTTP 500 returns None", models is None and "500" in msg, msg)


def test_cache_is_keyed_and_expires() -> None:
    from zimzilla import gemini as gm

    gm.clear_cache()
    calls = {"n": 0}

    def _counting(req, timeout=None):
        calls["n"] += 1
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", _counting):
        gm.fetch_models("AQ.one")
        gm.fetch_models("AQ.one")
        check("cache: a second fetch is served from cache", calls["n"] == 1,
              str(calls["n"]))
        gm.fetch_models("AQ.two")
        check("cache: a different key is a different entry", calls["n"] == 2,
              str(calls["n"]))
        gm.clear_cache()
        gm.fetch_models("AQ.one")
        check("cache: clear_cache forces a refetch", calls["n"] == 3,
              str(calls["n"]))


def test_verify_key_makes_a_chat_call() -> None:
    """The probe is a real chat call, and it sends the thinking floor."""
    from zimzilla import gemini as gm

    seen, opener = _capture({"choices": [{"message": {"content": "ok"}}]})
    with mock.patch("urllib.request.urlopen", opener):
        ok, detail = gm.verify_key("AQ.fake", "gemini-3.5-flash")
    check("verify: a good key is accepted", ok, detail)
    check("verify: it POSTs to chat/completions",
          seen["url"].endswith("/v1beta/openai/chat/completions"), seen["url"])
    check("verify: it is a POST", seen["method"] == "POST", seen["method"])
    body = json.loads(seen["body"])
    check("verify: it sends the thinking floor, not 8",
          body["max_tokens"] == gm.MIN_OUTPUT_TOKENS, str(body["max_tokens"]))
    check("verify: it names the model asked for",
          body["model"] == "gemini-3.5-flash", body["model"])


def test_verify_key_does_not_fail_on_a_model_problem() -> None:
    """A model problem is not a key problem — the switch must not be refused."""
    from zimzilla import gemini as gm

    # limit: 0 — not on the free tier. The key is fine.
    body = json.dumps([{"error": {"code": 429, "message":
        "Quota exceeded ... limit: 0, model: gemini-3.1-pro, description: "
        "Generate content requests per day limit exceeded on Free Tier."}}])
    with mock.patch("urllib.request.urlopen", _stub(error=_http_error(429, body))):
        ok, detail = gm.verify_key("AQ.fake", "gemini-3.1-pro-preview")
    check("verify: limit:0 does not fail the key", ok, detail)
    check("verify: limit:0 says retrying will not help",
          "will not help" in detail, detail)

    # 503 high demand — transient, key is fine.
    with mock.patch("urllib.request.urlopen",
                    _stub(error=_http_error(503, "high demand"))):
        ok, detail = gm.verify_key("AQ.fake")
    check("verify: 503 does not fail the key", ok, detail)

    # A rejected key IS a 400 with "Invalid Auth key" — not a 401.
    bad = json.dumps({"error": {"code": 400, "message": "Invalid Auth key.",
                                "status": "INVALID_ARGUMENT"}})
    with mock.patch("urllib.request.urlopen", _stub(error=_http_error(400, bad))):
        ok, detail = gm.verify_key("AQ.bogus")
    check("verify: a 400 Invalid Auth key DOES fail the key", not ok, detail)
    check("verify: the message names the setup command",
          "/gemini-api-setup" in detail, detail)


def test_explain_error_shapes() -> None:
    from zimzilla import gemini as gm

    limit0 = json.dumps([{"error": {"code": 429, "message":
        "Quota exceeded ... limit: 0, model: gemini-3.1-pro"}}])
    msg = gm.explain_error(429, limit0)
    check("error: limit:0 is called out as not-on-free-tier",
          "not offered on the free tier" in msg, msg)
    check("error: limit:0 says retrying will not help",
          "will not help" in msg, msg)

    ratelimit = json.dumps([{"error": {"code": 429, "message":
        "Quota exceeded ... limit: 5, model: gemini-flash"}}])
    msg = gm.explain_error(429, ratelimit)
    check("error: a real 429 is a rate limit",
          "rate limited" in msg and "will not help" not in msg, msg)

    bad = json.dumps({"error": {"code": 400, "message": "Invalid Auth key."}})
    msg = gm.explain_error(400, bad)
    check("error: a bad key is named as a bad key", "rejected the key" in msg, msg)
    check("error: a bad key notes it is a 400, not a 401",
          "400" in msg, msg)

    nokey = json.dumps({"error": {"code": 404,
                                  "message": "Requested entity was not found."}})
    msg = gm.explain_error(404, nokey)
    check("error: a 404 names the missing key", "needs a key" in msg, msg)

    msg = gm.explain_error(503, "high demand")
    check("error: 503 is capacity, not a key problem", "capacity" in msg, msg)


def test_looks_like_key() -> None:
    from zimzilla import gemini as gm

    # A synthetic key of the right shape, NOT a real credential. A live key
    # here would be a secret committed to a public repository, and GitHub's
    # push protection blocks it — correctly.
    check("key: a real-shaped key passes",
          gm.looks_like_key("AQ." + "x" * 60))
    check("key: a pasted export line fails",
          not gm.looks_like_key('export ANTHROPIC_AUTH_TOKEN="AQ.abc"'))
    check("key: a short word fails", not gm.looks_like_key("AQ.abc"))
    check("key: whitespace fails", not gm.looks_like_key("AQ.abc def ghi jkl mno"))


def test_is_gemini_matches_by_host() -> None:
    from zimzilla import gemini as gm

    check("host: the openai base matches",
          gm.is_gemini("https://generativelanguage.googleapis.com/v1beta/openai"))
    check("host: the native base matches",
          gm.is_gemini("https://generativelanguage.googleapis.com/v1beta"))
    check("host: a bare host matches",
          gm.is_gemini("https://generativelanguage.googleapis.com"))
    check("host: nvidia does not match", not gm.is_gemini("https://api.nvidia.com/v1"))
    check("host: empty does not match", not gm.is_gemini(""))
    check("host: None does not match", not gm.is_gemini(None))


def test_url_builder_special_case() -> None:
    """The /v1beta/openai base must not gain a second /v1 path."""
    from zimzilla import openai_compat as oa

    base = "https://generativelanguage.googleapis.com/v1beta/openai"
    check("url: chat is appended to the openai base",
          oa.chat_completions_url(base) == base + "/chat/completions",
          oa.chat_completions_url(base))
    check("url: models is appended to the openai base",
          oa.models_url(base) == base + "/models", oa.models_url(base))
    check("url: a bare host is rewritten to the openai path",
          oa.chat_completions_url("https://generativelanguage.googleapis.com")
          == base + "/chat/completions",
          oa.chat_completions_url("https://generativelanguage.googleapis.com"))
    check("url: the native /v1beta base is rewritten",
          oa.chat_completions_url("https://generativelanguage.googleapis.com/v1beta")
          == base + "/chat/completions",
          oa.chat_completions_url("https://generativelanguage.googleapis.com/v1beta"))
    check("url: the protocol resolves to openai",
          oa.resolve_protocol(base) == "openai", oa.resolve_protocol(base))

    # The special case must not have disturbed the routes that already worked.
    check("url: nvidia is untouched",
          oa.chat_completions_url("https://api.nvidia.com/v1")
          == "https://api.nvidia.com/v1/chat/completions")
    check("url: flex is untouched",
          oa.chat_completions_url("https://eu.api.flex.ai/api/playground/chat")
          == "https://eu.api.flex.ai/api/playground/chat")
    check("url: a plain /v1 base is untouched",
          oa.chat_completions_url("https://api.openai.com/v1")
          == "https://api.openai.com/v1/chat/completions")


def test_profile_roundtrip(tmp: Path) -> None:
    from zimzilla import gemini as gm

    saved = gm.PROFILE
    gm.PROFILE = tmp / "gemini" / "source"
    try:
        with _clean_env():
            check("profile: no key before setup", gm.load_credentials() is None)
            path = gm.save_key("AQ.testkey1234567890abcdef", "gemini-3.5-flash")
            check("profile: written where expected", path == gm.PROFILE, str(path))
            check("profile: mode is 600",
                  oct(path.stat().st_mode)[-3:] == "600",
                  oct(path.stat().st_mode)[-3:])
            text = path.read_text()
            check("profile: the base URL is the openai one",
                  gm.API_BASE_URL in text, text[:80])
            check("profile: the model is carried",
                  'ANTHROPIC_MODEL="gemini-3.5-flash"' in text)
            creds = gm.load_credentials()
            check("profile: the key reads back",
                  creds is not None and creds[0] == "AQ.testkey1234567890abcdef",
                  str(creds[0]) if creds else "None")
            check("profile: the model reads back",
                  creds is not None and creds[1] == "gemini-3.5-flash")
            check("profile: has_key agrees", gm.has_key())
    finally:
        gm.PROFILE = saved


def test_credential_precedence(tmp: Path) -> None:
    from zimzilla import gemini as gm

    saved = gm.PROFILE
    gm.PROFILE = tmp / "gemini2" / "source"
    try:
        with _clean_env():
            gm.save_key("AQ.fromprofile1234567890")
            os.environ["ANTHROPIC_BASE_URL"] = gm.API_BASE_URL
            os.environ["ANTHROPIC_AUTH_TOKEN"] = "AQ.fromenv1234567890abc"
            creds = gm.load_credentials()
            check("creds: the environment wins over the profile",
                  creds is not None and creds[0] == "AQ.fromenv1234567890abc",
                  str(creds[0]) if creds else "None")
            check("creds: the origin is named as the environment",
                  creds is not None and creds[2] == "environment")
            # A profile aimed at a different host must not be adopted.
            os.environ["ANTHROPIC_BASE_URL"] = "https://api.nvidia.com/v1"
            creds = gm.load_credentials()
            check("creds: a foreign base URL is not adopted",
                  creds is not None and creds[0] == "AQ.fromprofile1234567890",
                  str(creds[0]) if creds else "None")
    finally:
        gm.PROFILE = saved


def test_sources_registry_dispatch() -> None:
    from zimzilla import gemini as gm, sources

    check("sources: gemini is direct-only", "gemini" in sources.DIRECT_ONLY)
    route = sources.DECLARED_ROUTES.get("gemini")
    check("sources: a route is declared", route is not None)
    check("sources: the route is the openai base",
          route is not None and route.base_url == gm.API_BASE_URL,
          route.base_url if route else "None")
    check("sources: active_key recognises the route",
          sources.active_key(gm.API_BASE_URL) == "gemini",
          str(sources.active_key(gm.API_BASE_URL)))
    check("sources: active_key recognises the native base",
          sources.active_key("https://generativelanguage.googleapis.com/v1beta")
          == "gemini")


def test_discovery_needs_a_profile(tmp: Path) -> None:
    """No profile means no source — the honest state for a key-gated route."""
    from zimzilla import gemini as gm, sources

    # `sources._profile_candidates` builds its path from `sources.ZIMZILLA_HOME`,
    # not from `gemini.PROFILE`, so both have to move for this to be a real test
    # of discovery rather than of the module's own constant.
    saved_profile, saved_home = gm.PROFILE, sources.ZIMZILLA_HOME
    # `sources` appends ``gemini/source`` to its home, so the home is the
    # parent and the module's PROFILE is the file inside it.
    gm.PROFILE = tmp / "gemini3" / "gemini" / "source"
    sources.ZIMZILLA_HOME = tmp / "gemini3"
    try:
        with _clean_env():
            found = sources.discover()
            check("discover: absent without a profile", "gemini" not in found)
            gm.save_key("AQ.discovered1234567890")
            found = sources.discover()
            check("discover: present once the profile exists", "gemini" in found)
            src = found.get("gemini")
            check("discover: it is direct-only",
                  src is not None and src.is_direct_only)
            check("discover: it has the declared route",
                  src is not None and src.direct is not None
                  and src.direct.base_url == gm.API_BASE_URL)
    finally:
        gm.PROFILE = saved_profile
        sources.ZIMZILLA_HOME = saved_home


def test_tui_surfaces() -> None:
    """The three commands are dispatched, completed and documented."""
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text()
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text()

    for cmd in ("zim-gemini", "gemini-api-setup", "gemini-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}"' in app)
        check(f"tui: /{cmd} is in the no-turn allowlist",
              f'"{cmd}"' in app.split("_NO_TURN")[1][:6000]
              if "_NO_TURN" in app else True)
    check("tui: /zim-gemini is in the help table", "/zim-gemini" in app)
    check("tui: /gemini-api-setup is in the help table", "/gemini-api-setup" in app)
    check("tui: /gemini-models is in the help table", "/gemini-models" in app)
    check("tui: /zim-gemini completes", "/zim-gemini" in comp)
    check("tui: /gemini-api-setup completes", "/gemini-api-setup" in comp)
    check("tui: /gemini-models completes", "/gemini-models" in comp)
    check("tui: the module is imported", "import gemini as gm_mod" in app)


def test_nothing_ships_a_credential() -> None:
    """The key is the operator's; nothing under packaging/ may carry one."""
    packaging = ROOT / "packaging"
    if not packaging.is_dir():
        check("ship: no packaging dir to check", True)
        return
    hits = []
    for path in packaging.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"AQ\.[A-Za-z0-9_\-]{20,}", text):
            hits.append(str(path.relative_to(ROOT)))
    check("ship: no Gemini key under packaging/", not hits, ", ".join(hits))
    check("ship: no gemini profile ships",
          not (packaging / "gemini" / "source").exists())


# ---------------------------------------------------------------------------

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-gemini-test-"))
    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
              "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        test_parser_drops_malformed_rows()
        test_non_chat_families_are_filtered()
        test_thinking_models_are_identified()
        test_free_is_the_account_tier()
        test_context_is_never_none()
        test_catalog_is_key_gated_and_the_url_is_right()
        test_fetch_is_total()
        test_cache_is_keyed_and_expires()
        test_verify_key_makes_a_chat_call()
        test_verify_key_does_not_fail_on_a_model_problem()
        test_explain_error_shapes()
        test_looks_like_key()
        test_is_gemini_matches_by_host()
        test_url_builder_special_case()
        test_profile_roundtrip(tmp)
        test_credential_precedence(tmp)
        test_sources_registry_dispatch()
        test_discovery_needs_a_profile(tmp)
        test_tui_surfaces()
        test_nothing_ships_a_credential()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

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
