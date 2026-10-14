"""Regression suite for the OpenRouter source.

Run:  python tests/test_openrouter.py   (from an activated venv)

Protects what is particular about this provider:

* **The catalog needs no key.** ``GET /api/v1/models`` is public, so the free
  list is available *before* ``/openrouter-api-setup`` has run. That is what
  makes the keyless path of the setup command useful rather than a placeholder,
  and it is asserted directly: a fetch with an empty token must succeed and must
  send no credential.
* **The ``anthropic-version`` trap.** Sending that header flips ``/models`` into
  the *Anthropic* Models API shape — 20 mangled ``anthropic/``-prefixed ids
  instead of the full catalog. Measured on this box, and the kind of thing that
  regresses silently the moment someone "tidies up" the headers. It gets its own
  test.
* **Two free signals.** The ``:free`` suffix *and* a zero price — and the price
  arrives as a **string** (``"0"``), so a naive ``== 0`` comparison would find
  no free models at all. Both halves are asserted.
* **Tool support is a filter, not decoration.** A free model that cannot call a
  tool cannot drive this harness. ``usable_free_models`` is what ``/models``
  reads, so the filter is tested against a catalog containing exactly that case.

No test here touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

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


# Shaped like the real response: an OpenAI-style `data` list, prices as STRINGS,
# `supported_parameters` carrying "tools", and `name` as a display label. The
# free set covers all four cases that matter:
#   - `:free` suffix AND 0/0 price AND tools   → free and usable
#   - `:free` suffix, tools, but a non-zero price listed → suffix wins
#   - no suffix, 0/0 price (zeroed without a rename)  → price wins
#   - `:free` suffix, no tools  → free but NOT usable
CATALOG = {
    "data": [
        {"id": "deepseek/deepseek-v4.1:free", "name": "DeepSeek V4.1 (free)",
         "context_length": 163840,
         "pricing": {"prompt": "0", "completion": "0"},
         "supported_parameters": ["tools", "temperature", "top_p"]},
        {"id": "qwen/qwen4-72b:free", "name": "Qwen4 72B (free)",
         "context_length": 32768,
         "pricing": {"prompt": "0", "completion": "0"},
         "supported_parameters": ["tools"]},
        {"id": "meta/llama-5-70b:free", "name": "Llama 5 70B (free)",
         "context_length": 131072,
         "pricing": {"prompt": "0.0000001", "completion": "0"},
         "supported_parameters": ["tools"]},
        {"id": "z-ai/glm-6-air", "name": "GLM 6 Air",
         "context_length": 131072,
         "pricing": {"prompt": "0", "completion": "0"},
         "supported_parameters": ["tools"]},
        {"id": "nvidia/nemotron-3.5-content-safety:free",
         "name": "Nemotron Content Safety (free)",
         "context_length": 128000,
         "pricing": {"prompt": "0", "completion": "0"},
         "supported_parameters": ["temperature"]},
        {"id": "anthropic/claude-opus-5.5", "name": "Claude Opus 5.5",
         "context_length": 200000,
         "pricing": {"prompt": "0.000015", "completion": "0.000075"},
         "supported_parameters": ["tools", "vision"]},
        {"id": "openai/gpt-5.2", "name": "GPT-5.2",
         "context_length": 400000,
         "pricing": {"prompt": "0.00001", "completion": "0.00003"},
         "supported_parameters": ["tools"]},
    ],
}

PROFILE_TEXT = (
    'export ANTHROPIC_BASE_URL="https://openrouter.ai/api"\n'
    'export ANTHROPIC_AUTH_TOKEN="sk-or-fromprofile000000000000"\n'
    'export ANTHROPIC_MODEL="deepseek/deepseek-v4.1:free"\n'
    'export ANTHROPIC_API_KEY=""\n'
)


class _Resp(io.BytesIO):
    """A urlopen() result: a context manager that also reports a status."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(payload: dict | None = None, error: Exception | None = None):
    def _open(req, timeout=None):
        if error is not None:
            raise error
        return _Resp(json.dumps(payload or CATALOG).encode())
    return _open


# ---------------------------------------------------------------------------

def test_free_detection() -> None:
    """Free from the ``:free`` suffix, and from a zero price with no suffix."""
    from zimzilla import openrouter as o

    models = o._parse_models(CATALOG)
    by_id = {m.id: m for m in models}
    check("catalog: every entry parsed", len(models) == 7, str(len(models)))

    check("catalog: a :free suffix is free",
          by_id["deepseek/deepseek-v4.1:free"].free)
    check("catalog: a second :free suffix is free",
          by_id["qwen/qwen4-72b:free"].free)

    # The suffix wins even when the listing carries a small non-zero price —
    # the model is named free, so it is free.
    check("catalog: the suffix beats a listed non-zero price",
          by_id["meta/llama-5-70b:free"].free)

    # And the price wins with no suffix at all: a model zeroed out without a
    # rename is still free.
    check("catalog: a zero price with no suffix is free",
          by_id["z-ai/glm-6-air"].free)

    check("catalog: a paid model is not free",
          not by_id["anthropic/claude-opus-5.5"].free)
    check("catalog: a second paid model is not free",
          not by_id["openai/gpt-5.2"].free)

    free = sorted(m.id for m in models if m.free)
    check("catalog: exactly the five free ones", free == [
        "deepseek/deepseek-v4.1:free", "meta/llama-5-70b:free",
        "nvidia/nemotron-3.5-content-safety:free", "qwen/qwen4-72b:free",
        "z-ai/glm-6-air"], str(free))


def test_prices_arrive_as_strings() -> None:
    """The catalog sends prices as strings; they must be parsed, not compared.

    This is the bug the shape invites: ``pricing["prompt"] == 0`` is False for
    the string ``"0"``, so a naive check finds *no* free models in a catalog
    full of them. Parsing happens in ``_parse_models``, and this asserts the
    floats actually came out.
    """
    from zimzilla import openrouter as o

    models = o._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("pricing: a string '0' becomes 0.0",
          by_id["qwen/qwen4-72b:free"].price_in == 0.0)
    check("pricing: a string price becomes a float",
          by_id["anthropic/claude-opus-5.5"].price_in == 0.000015,
          str(by_id["anthropic/claude-opus-5.5"].price_in))
    check("pricing: the output price is read too",
          by_id["anthropic/claude-opus-5.5"].price_out == 0.000075)

    # An entry with no pricing block at all must not read as free.
    unpriced = o._parse_models({"data": [{"id": "mystery-model"}]})
    check("pricing: an unpriced entry is not free", not unpriced[0].free)
    check("pricing: an unpriced entry reports no price",
          unpriced[0].price_in == 0.0 and not unpriced[0].priced)

    # A malformed price must degrade rather than raise.
    messy = o._parse_models({"data": [
        {"id": "bad-price", "pricing": {"prompt": "free!", "completion": None}},
    ]})
    check("pricing: a malformed price degrades to 0.0",
          messy[0].price_in == 0.0, str(messy[0].price_in))
    check("pricing: a malformed entry with a pricing block is not free",
          not messy[0].free)


def test_tool_support() -> None:
    """`tools` is read from supported_parameters and filters the free list."""
    from zimzilla import openrouter as o

    models = o._parse_models(CATALOG)
    by_id = {m.id: m for m in models}
    check("tools: a tool-capable model reports it",
          by_id["deepseek/deepseek-v4.1:free"].tools)
    check("tools: a model without tools reports it",
          not by_id["nvidia/nemotron-3.5-content-safety:free"].tools)
    check("tools: the parameter list is preserved",
          "temperature" in by_id["deepseek/deepseek-v4.1:free"].supported)
    check("tools: a missing parameter list is empty, not an error",
          o._parse_models({"data": [{"id": "bare"}]})[0].supported == ())

    o.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        usable, err = o.usable_free_models(use_cache=False)
    check("tools: usable_free_models keeps only the tool-capable free ones",
          usable is not None
          and sorted(m.id for m in usable) == [
              "deepseek/deepseek-v4.1:free", "meta/llama-5-70b:free",
              "qwen/qwen4-72b:free", "z-ai/glm-6-air"],
          str([m.id for m in (usable or [])]))
    check("tools: the no-tools free model is excluded, not merely ranked last",
          all(m.tools for m in (usable or [])))


def test_catalog_needs_no_key() -> None:
    """The whole point of this provider: /models works before setup."""
    from zimzilla import openrouter as o

    o.clear_cache()
    seen = {}

    def capture(req, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        seen["url"] = req.full_url
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", capture):
        models, err = o.fetch_models(use_cache=False)  # no token at all
    check("keyless: a fetch with no key succeeds", err == ""
          and models is not None and len(models) == 7, err)
    check("keyless: hits the public models endpoint",
          seen["url"] == "https://openrouter.ai/api/v1/models", seen.get("url"))
    check("keyless: sends NO credential",
          "authorization" not in seen["headers"]
          and "x-api-key" not in seen["headers"],
          str(sorted(seen["headers"])))
    check("keyless: the catalog is cached under the empty token",
          o.cached_ids() is not None and len(o.cached_ids() or []) == 7)

    o.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        free, err = o.usable_free_models()   # again, no token
    check("keyless: the free list is available before setup",
          free is not None and len(free) == 4, str([m.id for m in (free or [])]))


def test_anthropic_version_header_is_not_sent() -> None:
    """Sending `anthropic-version` to /models mangles the catalog.

    Measured: with the header, ``/api/v1/models`` answers in the *Anthropic*
    Models API shape — paginated at 20 entries with ``has_more``/``first_id``/
    ``last_id`` and every id prefixed ``anthropic/``. 20 ids instead of 463.
    The header belongs on ``/v1/messages``, not here. This test is the guard
    against a future tidy-up putting it back.
    """
    from zimzilla import openrouter as o

    o.clear_cache()
    seen = {}

    def capture(req, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", capture):
        o.fetch_models("sk-or-key000000000000000000", use_cache=False)
    check("header: no anthropic-version is sent to /models",
          "anthropic-version" not in seen["headers"],
          str(sorted(seen["headers"])))
    check("header: a keyed fetch still sends its credential",
          seen["headers"].get("authorization")
          == "Bearer sk-or-key000000000000000000")


def test_fetch_is_total() -> None:
    """fetch_models returns errors; it never raises into the TUI."""
    from zimzilla import openrouter as o

    o.clear_cache()
    with mock.patch("urllib.request.urlopen",
                    _stub_urlopen(error=urllib.error.URLError("no route"))):
        models, err = o.fetch_models(use_cache=False)
    check("fetch: a network failure returns None and a message",
          models is None and "could not reach" in err, err)

    o.clear_cache()
    http = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    with mock.patch("urllib.request.urlopen", _stub_urlopen(error=http)):
        models, err = o.fetch_models("sk-or-bad000000000000000000",
                                     use_cache=False)
    check("fetch: a rejected key says so", models is None and "401" in err, err)
    check("fetch: the rejection names the setup command",
          "openrouter-api-setup" in err, err)

    # An empty catalog is [] and NOT an error — the caller can tell them apart.
    o.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen({"data": []})):
        models, err = o.fetch_models(use_cache=False)
    check("fetch: an empty catalog is [], not an error",
          models == [] and err == "", repr((models, err)))


def test_cache_expiry() -> None:
    """An expired catalog must not be served as if it were live.

    ``fetch_models`` has always refused to *reuse* an entry past ``CACHE_TTL``,
    but ``cached_models`` read it back regardless — so a catalog fetched once
    was reported by ``/model`` for the rest of the session. OpenRouter's list
    turns over constantly, free tiers especially, so a stale one is exactly the
    wrong list this picker exists to avoid. Nothing sleeps: the entry's
    timestamp is moved backwards, which is the same thing to ``monotonic()``.
    """
    import time

    from zimzilla import openrouter as o

    o.clear_cache()
    check("expiry: a cold cache has no age", o.catalog_age() is None,
          str(o.catalog_age()))

    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        o.fetch_models()
    check("expiry: a fresh catalog is served", o.cached_ids() is not None)
    check("expiry: its age is reported, and small",
          (o.catalog_age() or 999) < 5.0, str(o.catalog_age()))

    models, at = o._CACHE[""]
    o._CACHE[""] = (models, time.monotonic() - (o.CACHE_TTL + 60))
    check("expiry: an expired catalog reads as None",
          o.cached_models() is None, str(o.cached_models()))
    check("expiry: cached_ids agrees", o.cached_ids() is None)
    check("expiry: the real age is still reported, unclamped",
          (o.catalog_age() or 0) >= o.CACHE_TTL, str(o.catalog_age()))

    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        o.fetch_models()
    check("expiry: refetching restores it", o.cached_ids() is not None)
    o.clear_cache()


def test_cache_is_keyed_by_token() -> None:
    """A key change refetches rather than serving another account's list."""
    from zimzilla import openrouter as o

    o.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        o.fetch_models()                 # keyless
    check("cache: the keyless list is cached", o.cached_ids() is not None)

    calls = []

    def counting(req, timeout=None):
        calls.append(req)
        return _Resp(json.dumps({"data": []}).encode())

    with mock.patch("urllib.request.urlopen", counting):
        o.fetch_models("sk-or-key000000000000000000")
    check("cache: a keyed fetch does not reuse the keyless list",
          len(calls) == 1, str(len(calls)))

    # But the keyless fetch IS reused, or /models would hit the network on every
    # keystroke.
    calls.clear()
    with mock.patch("urllib.request.urlopen", counting):
        o.fetch_models("sk-or-key000000000000000000")
    check("cache: a repeat keyed fetch is served from cache",
          len(calls) == 0, str(len(calls)))


def test_profile_roundtrip() -> None:
    """save_key writes mode 600, in a format the parser reads back."""
    from zimzilla import openrouter as o

    token = "sk-or-saved00000000000000000"
    path = o.save_key(token, "deepseek/deepseek-v4.1:free")
    check("key: written where load_credentials looks", path == o.PROFILE)
    mode = path.stat().st_mode & 0o777
    check("key: the file is mode 600", mode == 0o600, oct(mode))
    parent_mode = path.parent.stat().st_mode & 0o777
    check("key: the directory is mode 700", parent_mode == 0o700, oct(parent_mode))

    exports = o._read_exports(path)
    check("key: the token round-trips", exports.get("ANTHROPIC_AUTH_TOKEN") == token)
    check("key: the model round-trips",
          exports.get("ANTHROPIC_MODEL") == "deepseek/deepseek-v4.1:free")
    check("key: the endpoint is OpenRouter",
          o.is_openrouter(exports.get("ANTHROPIC_BASE_URL")))
    check("key: the endpoint is the SDK prefix, not the API root",
          exports.get("ANTHROPIC_BASE_URL") == "https://openrouter.ai/api")
    check("key: the file is source-format", path.read_text().count("export ") == 4)


def test_looks_like_key() -> None:
    from zimzilla import openrouter as o

    check("key: a real-looking key passes",
          o.looks_like_key("sk-or-v1-0000000000000000000000000000000000"))
    check("key: a short string is rejected", not o.looks_like_key("abc"))
    check("key: a pasted export line is rejected",
          not o.looks_like_key('export ANTHROPIC_AUTH_TOKEN="sk-or-xxxx"'))
    check("key: whitespace is rejected",
          not o.looks_like_key("sk-or-aaaaaaaaaaaaaaaaaaaa bbbb"))


def test_active_key_recognises_host() -> None:
    """The hosted URL is matched by host, and the path is not confused."""
    from zimzilla import sources as s

    check("sources: the OpenRouter host is the openrouter source",
          s.active_key("https://openrouter.ai/api") == "openrouter")
    # Recognition is by host, so a profile written before the base URL was
    # corrected still resolves to this source — and that matters, because such a
    # profile is exactly what is sitting in an operator's environment right now.
    check("sources: a pre-fix /api/v1 profile still resolves here",
          s.active_key("https://openrouter.ai/api/v1") == "openrouter")
    check("sources: the bare host too",
          s.active_key("https://openrouter.ai") == "openrouter")
    check("sources: with a trailing slash too",
          s.active_key("https://openrouter.ai/") == "openrouter")
    check("sources: it is a builtin, so it survives a restart",
          "openrouter" in s.BUILTIN_KEYS)
    check("sources: a local proxy is still itself",
          s.active_key("http://localhost:4001") == "logfare")
    check("sources: an unrelated host is unknown",
          s.active_key("https://api.example.com") is None)

    # The two gateways must not be confused for each other.
    check("sources: openrouter is not tokenharbour",
          s.active_key("https://openrouter.ai/api/v1") != "tokenharbour")
    check("sources: tokenharbour is not openrouter",
          s.active_key("https://tokenharbor.ai") != "openrouter")


def test_credential_precedence(tmp: Path) -> None:
    """Environment beats the profile; the profile beats ~/claude-source."""
    from zimzilla import openrouter as o

    claude_source = tmp / "claude-source"
    claude_source.mkdir(parents=True, exist_ok=True)
    (claude_source / "openrouter").write_text(PROFILE_TEXT)

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        with mock.patch.object(o, "CLAUDE_SOURCE", claude_source), \
             mock.patch.object(o, "PROFILE", tmp / "nonexistent" / "source"):
            creds = o.load_credentials()
            check("creds: falls back to ~/claude-source",
                  creds is not None and creds[2].endswith("openrouter"),
                  str(creds and creds[2]))
            check("creds: the model comes from that profile",
                  creds is not None and creds[1] == "deepseek/deepseek-v4.1:free")

            own = tmp / "own" / "source"
            own.parent.mkdir(parents=True, exist_ok=True)
            own.write_text('export ANTHROPIC_AUTH_TOKEN="sk-or-ownprofile0000000"\n')
            with mock.patch.object(o, "PROFILE", own):
                creds = o.load_credentials()
                check("creds: ZimZilla's own profile beats ~/claude-source",
                      creds is not None and creds[2] == str(own),
                      str(creds and creds[2]))

                os.environ["ANTHROPIC_BASE_URL"] = o.DEFAULT_BASE_URL
                os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-or-fromenv000000000000"
                os.environ["ANTHROPIC_MODEL"] = "qwen/qwen4-72b:free"
                creds = o.load_credentials()
                check("creds: the environment beats every file",
                      creds is not None and creds[2] == "environment"
                      and creds[0] == "sk-or-fromenv000000000000",
                      str(creds and creds[2]))

                # An env key pointed at ANOTHER host must not be mistaken for ours.
                os.environ["ANTHROPIC_BASE_URL"] = "https://elsewhere.example.com"
                creds = o.load_credentials()
                check("creds: an env key for another host is ignored",
                      creds is not None and creds[2] == str(own),
                      str(creds and creds[2]))

                # And a TokenHarbour endpoint must not be read as an OpenRouter one.
                os.environ["ANTHROPIC_BASE_URL"] = "https://tokenharbor.ai"
                creds = o.load_credentials()
                check("creds: a TokenHarbour endpoint is not OpenRouter",
                      creds is not None and creds[2] == str(own),
                      str(creds and creds[2]))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_apply_and_guard() -> None:
    """apply_to aims a config; the guard keeps a stale choice from hijacking."""
    from zimzilla import openrouter as o

    class _Cfg:
        base_url = "http://localhost:4001"
        auth_token = "leftover-from-logfare"
        api_key = "sk-leftover"
        model = "deepseek-v4.1-flash"

    with mock.patch.object(o, "load_credentials",
                           lambda: ("sk-or-x00000000000000000",
                                    "qwen/qwen4-72b:free", "test")):
        cfg = _Cfg()
        applied = o.apply_to(cfg)
        check("apply: returns the origin and model",
              applied == ("test", "qwen/qwen4-72b:free"), str(applied))
        check("apply: the endpoint is OpenRouter",
              cfg.base_url == o.DEFAULT_BASE_URL)
        check("apply: the token is set", cfg.auth_token.startswith("sk-or-"))
        check("apply: the stale api_key is cleared", cfg.api_key == "")
        check("apply: the model is applied", cfg.model == "qwen/qwen4-72b:free")

    with mock.patch.object(o, "load_credentials", lambda: None):
        cfg = _Cfg()
        check("apply: no key leaves the config alone",
              o.apply_to(cfg) is None and cfg.base_url == "http://localhost:4001")

    check("guard: the default local proxy is overridable",
          o.applies_to_default_endpoint("http://localhost:4001"))
    check("guard: an empty endpoint is overridable",
          o.applies_to_default_endpoint(""))
    check("guard: OpenRouter itself is NOT overridden",
          not o.applies_to_default_endpoint(o.DEFAULT_BASE_URL))
    check("guard: another host is NOT overridden",
          not o.applies_to_default_endpoint("https://api.example.com"))


def test_selection_file_is_shared() -> None:
    """The selection file is the same one sources.py reads."""
    from zimzilla import sources as s, openrouter as o

    o.set_current("openrouter")
    check("selection: sources.py sees the openrouter choice",
          s.current_key() == "openrouter", str(s.current_key()))
    check("selection: openrouter sees it too", o.current_key() == "openrouter")


def test_no_key_is_echoed(tmp: Path) -> None:
    """The display value is a path, never the key itself."""
    from zimzilla import openrouter as o

    own = tmp / "own2" / "source"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text('export ANTHROPIC_AUTH_TOKEN="sk-or-secret0000000000000"\n')
    with mock.patch.object(o, "PROFILE", own):
        creds = o.load_credentials()
    origin = creds[2]
    check("display: the origin is a path, not a key", "sk-" not in origin, origin)


def test_the_composed_message_url_is_right() -> None:
    """The URL the SDK actually builds — not the constant it is built from.

    This is the test that was missing, and its absence is why a total failure
    shipped: every turn 404'd while the suite stayed green. The old assertions
    checked ``DEFAULT_BASE_URL == "https://openrouter.ai/api/v1"``, which was
    *true* and also *wrong* — the SDK appends ``/v1/messages`` to whatever prefix
    it is handed, so that constant composed to ``/api/v1/v1/messages``. Asserting
    a constant can only ever restate the bug; asserting the composed URL is the
    only thing that can catch it.

    So this builds the request through the real client, exactly as a turn does,
    and reads the resulting URL. It is the difference between "we configured the
    string we meant to" and "a message can be sent".

    The catalog is checked in the same breath, because the two paths are siblings
    under one version segment and correcting either one alone breaks the other:
    the base is ``/api``, messages are ``{base}/v1/messages`` (the SDK's doing),
    models are ``{base}/v1/models`` (built by hand). A fix that moved the base to
    ``/api/v1`` to please the messages path would silently 404 the catalog.
    """
    import anthropic
    from anthropic._models import FinalRequestOptions

    from zimzilla import openrouter as o
    from zimzilla.agent import Agent
    from zimzilla.config import Config

    cfg = Config(base_url=o.DEFAULT_BASE_URL, auth_token="sk-or-x0000000000000000")
    agent = Agent(cfg)
    opts = FinalRequestOptions(method="post", url="/v1/messages",
                               json_data={}, headers={})
    composed = str(agent.client._build_request(opts).url)

    check("url: the message request lands on /api/v1/messages",
          composed == "https://openrouter.ai/api/v1/messages", composed)
    check("url: there is no doubled /v1 in the path",
          "/v1/v1/" not in composed, composed)
    # The host and the single version segment are the whole assertion; spelling
    # out what a regression would look like keeps the failure message useful.
    check("url: the base is the prefix BEFORE /v1, not the API root",
          o.DEFAULT_BASE_URL == "https://openrouter.ai/api", o.DEFAULT_BASE_URL)

    # The catalog must still resolve, and to the same place it always did — the
    # fix moved the base, so this is the half that could have been broken by it.
    seen = {}

    def capture(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp(json.dumps(CATALOG).encode())

    o.clear_cache()
    with mock.patch("urllib.request.urlopen", capture):
        o.fetch_models(use_cache=False)
    check("url: the catalog still lands on /api/v1/models",
          seen.get("url") == "https://openrouter.ai/api/v1/models", seen.get("url"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-or-test-"))
    # Pin the module's paths to the temp tree before anything imports it.
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    test_free_detection()
    test_prices_arrive_as_strings()
    test_tool_support()
    test_catalog_needs_no_key()
    test_anthropic_version_header_is_not_sent()
    test_fetch_is_total()
    test_cache_is_keyed_by_token()
    test_cache_expiry()
    test_profile_roundtrip()
    test_looks_like_key()
    test_active_key_recognises_host()
    test_credential_precedence(tmp)
    test_apply_and_guard()
    test_selection_file_is_shared()
    test_no_key_is_echoed(tmp)
    test_the_composed_message_url_is_right()

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
