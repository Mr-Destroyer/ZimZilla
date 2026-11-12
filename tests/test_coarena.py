"""Regression suite for the Coarena source.

Run:  python tests/test_coarena.py   (from an activated venv)

Protects what is particular about this provider:

* **It is a hosted, Anthropic-native gateway, not a translated one.** Coarena
  serves ``POST /api/v1/messages`` itself, so the session is pointed at
  ``https://coarena.ai/api`` and the SDK appends ``/v1/messages``. Spelling
  ``/api/v1`` in the base URL produces ``/api/v1/v1/messages`` and a 404 on
  every turn while the catalog still works — the one mistake this file is most
  concerned with, and the one the provider's own docs warn about.
* **The catalog is public and prices everything at zero.** ``GET
  /api/v1/models`` answers 200 with no credential *and with a bad one*, so it
  is a public list rather than a credential test. Every entry is priced
  ``{"prompt": "0", "completion": "0"}`` while the account is metered in real
  dollars against a daily allowance — so "free" means no per-token charge, and
  a *missing* price must never be read as free. That is what ``Model.priced``
  records.
* **The probe is a real credential test.** ``GET /api/v1/limits`` answers 401
  without a key and 200 with one, so one authenticated GET proves a key without
  sending a turn — and the same reply carries the day's allowance.
* **A billing state is not a bad key.** 429 and 5xx come back ``ok=True`` from
  ``verify_key``; only 401/403 and an unreachable host fail it.
* **The provider's error codes are the specific half.** ``model_off`` and
  ``invalid_api_key`` are both a 400/401 and call for completely different
  actions, so the code is preferred over the status.
* **Nothing ships a credential.** ``~/.zimzilla/coarena/source`` is written by
  ``/coarena-api-setup`` and nothing under ``packaging/`` contains a key.

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


#: Shaped like the live payload fetched on 2026-11-12 — ``{"object": "list",
#: "data": [...]}`` with per-*token* prices as strings and a nested
#: ``capabilities`` object, so the per-million conversion and the capability
#: read are exercised rather than assumed. A richer fixture would let the
#: parser pass on data the endpoint never sends.
CATALOG = {
    "object": "list",
    "data": [
        {
            "id": "claude-sonnet-5-5",
            "object": "model",
            "owned_by": "anthropic",
            "type": "model",
            "display_name": "Claude Sonnet 5.5",
            "context_length": 128000,
            "max_completion_tokens": 16384,
            "description": "Claude Sonnet 5.5 by Anthropic, through the "
                           "Coarena Model API.",
            "pricing": {"prompt": "0", "completion": "0"},
            "capabilities": {"vision": True, "tools": True, "json_schema": True,
                             "reasoning_text": True,
                             "efforts": ["low", "medium", "high", "xhigh",
                                         "max"]},
            "aliases": ["sonnet-5-5", "anthropic/claude-sonnet-5-5"],
        },
        {
            "id": "deepseek-v4-1-flash-1017",
            "owned_by": "deepseek",
            "display_name": "DeepSeek V4.1 Flash 1017",
            "context_length": 128000,
            "max_completion_tokens": 16384,
            "pricing": {"prompt": "0", "completion": "0"},
            "capabilities": {"vision": True, "tools": True,
                             "reasoning_text": True,
                             "efforts": ["low", "medium", "high"]},
            "aliases": ["deepseek-v4p1-flash-1017",
                        "deepseek/deepseek-v4-1-flash-1017"],
        },
        # A model with a real per-token rate: it must NOT read as free.
        {
            "id": "paid-model-1",
            "owned_by": "someone",
            "display_name": "Paid Model",
            "context_length": 32000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "capabilities": {"tools": True},
        },
        # An entry with no pricing at all: it must not read as free either.
        {
            "id": "unpriced-model",
            "display_name": "Unpriced",
            "context_length": 8000,
            "capabilities": {},
        },
        # A model with no tool calling, so the ordering is exercised.
        {
            "id": "no-tools-model",
            "display_name": "No Tools",
            "context_length": 8000,
            "pricing": {"prompt": "0", "completion": "0"},
            "capabilities": {"tools": False},
        },
        # Malformed rows.
        "not-a-row",
        {"no_id": True},
        {"id": "   "},
    ],
}

LIMITS = {
    "data": {
        "free": True,
        "usd_per_day": 5,
        "requests_per_minute_key": 30,
        "concurrency": 2,
        "today": {"used_usd": 0.000083, "left_usd": 4.999917, "requests": 5},
    }
}


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
    """A stub that records the request, so the URL, method and headers can be asserted."""
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
    """Context manager that hides any Coarena credential in the environment."""
    class _C:
        keys = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                "ANTHROPIC_MODEL", "ZIMZILLA_PROTOCOL")

        def __enter__(self):
            self.saved = {k: os.environ.pop(k, None) for k in self.keys}
            return self

        def __exit__(self, *exc):
            for k, v in self.saved.items():
                if v is not None:
                    os.environ[k] = v
            return False
    return _C()


# ---------------------------------------------------------------------------

def test_route_is_the_messages_root() -> None:
    """The base URL is ``/api``, not ``/api/v1`` — the 404-every-turn trap."""
    from zimzilla import coarena as co
    from zimzilla import openai_compat as oa

    check("route: the Messages base URL is /api",
          co.DEFAULT_BASE_URL == "https://coarena.ai/api", co.DEFAULT_BASE_URL)
    check("route: it does not spell /v1 (the SDK appends /v1/messages)",
          not co.DEFAULT_BASE_URL.endswith("/v1"), co.DEFAULT_BASE_URL)
    check("route: Chat Completions lives under /api/v1",
          co.API_BASE_URL == "https://coarena.ai/api/v1", co.API_BASE_URL)
    check("route: protocol auto resolves to anthropic at /api",
          oa.resolve_protocol(co.DEFAULT_BASE_URL, "auto") == "anthropic",
          oa.resolve_protocol(co.DEFAULT_BASE_URL, "auto"))
    check("route: and to openai when the operator names /api/v1",
          oa.resolve_protocol(co.API_BASE_URL, "auto") == "openai",
          oa.resolve_protocol(co.API_BASE_URL, "auto"))
    check("route: the host is named as Anthropic-native",
          "coarena.ai" in oa.ANTHROPIC_HOSTS)
    check("route: the catalog is under /api/v1",
          co.CATALOG_URL == "https://coarena.ai/api/v1/models", co.CATALOG_URL)
    check("route: the probe is the keyed limits route",
          co.LIMITS_URL == "https://coarena.ai/api/v1/limits", co.LIMITS_URL)


def test_parser_reads_the_catalog() -> None:
    from zimzilla import coarena as co

    models = co._parse_models(CATALOG)
    by_id = {m.id: m for m in models}
    check("parse: every well-formed row kept", len(models) == 5, str(len(models)))
    check("parse: a bare string is dropped", "not-a-row" not in by_id)
    check("parse: a row with no id is dropped", "   " not in by_id)
    check("parse: the owner is carried",
          by_id["claude-sonnet-5-5"].owner == "anthropic",
          by_id["claude-sonnet-5-5"].owner)
    check("parse: the display name is carried",
          by_id["claude-sonnet-5-5"].name == "Claude Sonnet 5.5",
          by_id["claude-sonnet-5-5"].name)
    check("parse: the context window is carried",
          by_id["claude-sonnet-5-5"].context_length == 128_000,
          str(by_id["claude-sonnet-5-5"].context_length))
    check("parse: max output is carried",
          by_id["claude-sonnet-5-5"].max_output == 16_384,
          str(by_id["claude-sonnet-5-5"].max_output))
    check("parse: per-token prices become per-Mtok",
          (by_id["paid-model-1"].price_in,
           by_id["paid-model-1"].price_out) == (3.0, 15.0),
          str((by_id["paid-model-1"].price_in,
               by_id["paid-model-1"].price_out)))
    check("parse: a price was read", by_id["paid-model-1"].priced)
    check("parse: an unpriced row says so",
          not by_id["unpriced-model"].priced)
    check("parse: capabilities are carried",
          by_id["claude-sonnet-5-5"].tools
          and by_id["claude-sonnet-5-5"].vision
          and by_id["claude-sonnet-5-5"].reasoning
          and by_id["claude-sonnet-5-5"].json_schema)
    check("parse: a model without tools says so",
          not by_id["no-tools-model"].tools)
    check("parse: the efforts list is carried",
          by_id["claude-sonnet-5-5"].efforts == ("low", "medium", "high",
                                                 "xhigh", "max"),
          str(by_id["claude-sonnet-5-5"].efforts))
    check("parse: aliases are carried",
          "sonnet-5-5" in by_id["claude-sonnet-5-5"].aliases,
          str(by_id["claude-sonnet-5-5"].aliases))
    check("parse: every entry is chat", all(m.chat for m in models))


def test_free_is_a_zero_price_not_a_missing_one() -> None:
    """The catalog prices everything at 0; a missing price is not a zero price."""
    from zimzilla import coarena as co

    models = co._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("free: a zero-priced model is free", by_id["claude-sonnet-5-5"].free)
    check("free: a paid model is NOT free", not by_id["paid-model-1"].free)
    check("free: an unpriced entry is NOT free — no price is not a zero price",
          not by_id["unpriced-model"].free,
          "a missing pricing object must not read as 0/0")
    check("free: the blurb says free",
          by_id["claude-sonnet-5-5"].blurb.startswith("free"),
          by_id["claude-sonnet-5-5"].blurb)
    check("free: the blurb prices a paid model",
          "$3/$15" in by_id["paid-model-1"].blurb,
          by_id["paid-model-1"].blurb)
    check("free: the blurb flags a model with no tools",
          "no tools" in by_id["no-tools-model"].blurb,
          by_id["no-tools-model"].blurb)


def test_aliases_resolve_everywhere() -> None:
    """A turn sent under an alias must price and size like the bare id."""
    from zimzilla import coarena as co

    models = co._parse_models(CATALOG)
    check("alias: the bare id resolves",
          co.model_for("claude-sonnet-5-5", models) is not None)
    check("alias: a published alias resolves",
          co.model_for("sonnet-5-5", models) is not None)
    check("alias: a namespaced alias resolves",
          co.model_for("anthropic/claude-sonnet-5-5", models) is not None)
    check("alias: an unknown id is None",
          co.model_for("nope", models) is None)
    check("alias: an alias prices like the bare id",
          co.price_for("sonnet-5-5", models) == co.price_for(
              "claude-sonnet-5-5", models))
    check("alias: a free model prices at (0, 0)",
          co.price_for("claude-sonnet-5-5", models) == (0.0, 0.0))
    check("alias: an unpriced model returns None, not (0, 0)",
          co.price_for("unpriced-model", models) is None)
    check("alias: an unknown model returns None",
          co.price_for("nope", models) is None)


def test_catalog_is_public_and_unauthenticated() -> None:
    """The catalog needs no key — and sending one would be a habit worth not forming."""
    from zimzilla import coarena as co

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        co.clear_cache()
        models, err = co.fetch_models()
    check("catalog: it fetched", models is not None and not err, err)
    check("catalog: the URL is the public Models route",
          seen["url"] == co.CATALOG_URL, seen["url"])
    check("catalog: it is a GET", seen["method"] == "GET", seen["method"])
    check("catalog: no credential is sent",
          "authorization" not in seen["headers"]
          and "x-api-key" not in seen["headers"],
          str(sorted(seen["headers"])))

    # A key passed in only keys the cache; it is still not transmitted.
    seen2, opener2 = _capture()
    with mock.patch("urllib.request.urlopen", opener2):
        co.clear_cache()
        co.fetch_models("coa_abcdefghijklmnop")
    check("catalog: a key is not transmitted either",
          "authorization" not in seen2["headers"],
          str(sorted(seen2["headers"])))


def test_fetch_is_total_and_cached() -> None:
    """A network failure is returned, never raised — this runs in a TUI command."""
    from zimzilla import coarena as co

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        co.clear_cache()
        models, err = co.fetch_models()
    check("fetch: a network failure returns None", models is None)
    check("fetch: and says so", "could not reach" in err, err)

    with mock.patch("urllib.request.urlopen",
                    _stub(error=_http_error(500, "boom"))):
        co.clear_cache()
        models, err = co.fetch_models()
    check("fetch: an HTTP failure returns None", models is None)
    check("fetch: and names the status", "500" in err, err)

    with mock.patch("urllib.request.urlopen", _stub({"data": []})):
        co.clear_cache()
        models, err = co.fetch_models()
    check("fetch: an empty catalog is [] not None", models == [], str(models))

    with mock.patch("urllib.request.urlopen", _stub()):
        co.clear_cache()
        co.fetch_models()
        check("cache: a warm cache answers", co.cached_models() is not None)
        check("cache: cached_ids is the id list",
              co.cached_ids() == [m.id for m in co._parse_models(CATALOG)],
              str(co.cached_ids()))
        check("cache: catalog_age is a number",
              isinstance(co.catalog_age(), float), str(co.catalog_age()))
        co.clear_cache()
        check("cache: clear_cache empties it", co.cached_models() is None)

    # An expired entry must read as None, not as a live catalog.
    with mock.patch("urllib.request.urlopen", _stub()):
        co.clear_cache()
        co.fetch_models()
        with mock.patch.object(co, "CACHE_TTL", -1.0):
            check("cache: an expired entry reads as None",
                  co.cached_models() is None)


def test_free_lists() -> None:
    from zimzilla import coarena as co

    with mock.patch("urllib.request.urlopen", _stub()):
        co.clear_cache()
        free, err = co.free_models()
        check("free list: only zero-priced models",
              {m.id for m in free} == {"claude-sonnet-5-5",
                                       "deepseek-v4-1-flash-1017",
                                       "no-tools-model"},
              str(sorted(m.id for m in free)))
        usable, err = co.usable_free_models()
        check("free list: tool-capable models lead",
              usable[0].tools and not usable[-1].tools,
              str([(m.id, m.tools) for m in usable]))
        ids, err = co.model_ids()
        check("free list: model_ids is every model, not only the free ones",
              "paid-model-1" in ids, str(ids))


def test_verify_key_uses_the_keyed_limits_route() -> None:
    """``/api/v1/limits`` is the one route that answers 401 without a key."""
    from zimzilla import coarena as co

    seen, opener = _capture(LIMITS)
    with mock.patch("urllib.request.urlopen", opener):
        ok, detail = co.verify_key("coa_abcdefghijklmnop")
    check("probe: a good key is accepted", ok, detail)
    check("probe: it hits the limits route",
          seen["url"] == co.LIMITS_URL, seen["url"])
    check("probe: it sends the key as a bearer token",
          seen["headers"].get("authorization") == "Bearer coa_abcdefghijklmnop",
          str(seen["headers"].get("authorization")))
    check("probe: it is a GET", seen["method"] == "GET", seen["method"])
    check("probe: the allowance is reported",
          "$5/day" in detail and "left today" in detail, detail)

    with mock.patch("urllib.request.urlopen",
                    _stub(error=_http_error(401, json.dumps(
                        {"type": "error", "error": {
                            "type": "authentication_error",
                            "message": "That API key is not valid.",
                            "code": "invalid_api_key"}})))):
        ok, detail = co.verify_key("coa_bogus")
    check("probe: a rejected key fails", not ok, detail)
    check("probe: and names the setup command",
          "/coarena-api-setup" in detail, detail)

    check("probe: no key fails without a request",
          co.verify_key("") == (False, "no key"))

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        ok, detail = co.verify_key("coa_abcdefghijklmnop")
    check("probe: an unreachable host fails", not ok, detail)


def test_verify_key_does_not_fail_on_a_billing_state() -> None:
    """A 429 or 5xx is the account's state, not the credential's."""
    from zimzilla import coarena as co

    for code in (429, 500, 503):
        with mock.patch("urllib.request.urlopen",
                        _stub(error=_http_error(code, "{}"))):
            ok, detail = co.verify_key("coa_abcdefghijklmnop")
        check(f"probe: a {code} does not fail the key", ok, detail)


def test_explain_error_shapes() -> None:
    """The provider's code is the specific half; the status is the fallback."""
    from zimzilla import coarena as co

    off = json.dumps({"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "GPT-6.1 Sol (gpt-6-1-sol) is turned off on the Coarena "
                   "Model API right now.",
        "code": "model_off"}})
    check("error: model_off names the model, not the key",
          "switched off" in co.explain_error(400, off)
          and "key" not in co.explain_error(400, off).lower(),
          co.explain_error(400, off))

    bad = json.dumps({"type": "error", "error": {
        "type": "authentication_error", "message": "No API key was sent.",
        "code": "invalid_api_key"}})
    check("error: invalid_api_key names the setup command",
          "/coarena-api-setup" in co.explain_error(401, bad),
          co.explain_error(401, bad))

    quota = json.dumps({"error": {"code": "insufficient_quota"}})
    check("error: insufficient_quota names the allowance",
          "allowance" in co.explain_error(429, quota),
          co.explain_error(429, quota))

    terms = json.dumps({"error": {"code": "model_api_terms_required"}})
    check("error: the terms code names the page",
          "coarena.ai/account/api" in co.explain_error(403, terms),
          co.explain_error(403, terms))

    check("error: an unmapped 401 still names the setup command",
          "/coarena-api-setup" in co.explain_error(401, ""),
          co.explain_error(401, ""))
    check("error: an unmapped 5xx says retry",
          "retry" in co.explain_error(500, ""), co.explain_error(500, ""))
    check("error: an unmapped status carries the detail",
          "teapot" in co.explain_error(418, '{"error": {"message": "teapot"}}'),
          co.explain_error(418, '{"error": {"message": "teapot"}}'))


def test_allowance_line() -> None:
    from zimzilla import coarena as co

    line = co.allowance_line(LIMITS["data"])
    check("allowance: the daily cap is shown", "$5/day" in line, line)
    check("allowance: the remaining budget is shown",
          "$5.00 left today" in line, line)
    check("allowance: the request count is shown", "5 requests" in line, line)
    check("allowance: the rate limit is shown", "30/min" in line, line)
    check("allowance: the concurrency is shown", "2 concurrent" in line, line)
    check("allowance: an empty payload says nothing",
          co.allowance_line(None) == "" and co.allowance_line({}) == "")


def test_looks_like_key() -> None:
    from zimzilla import coarena as co

    check("key: a coa_ key is accepted",
          co.looks_like_key("coa_7c40d4bfab57141ff8b60efc36129a4e"))
    check("key: a pasted export line is refused",
          not co.looks_like_key('export ANTHROPIC_AUTH_TOKEN="coa_x"'))
    check("key: a bare word is refused", not co.looks_like_key("hello"))
    check("key: whitespace is refused", not co.looks_like_key("coa_abc def ghi"))
    check("key: the prefix is not required",
          co.looks_like_key("abcdefghijklmnop"))


def test_is_coarena_matches_by_host() -> None:
    from zimzilla import coarena as co

    check("host: the Messages root matches", co.is_coarena("https://coarena.ai/api"))
    check("host: the OpenAI root matches", co.is_coarena("https://coarena.ai/api/v1"))
    check("host: a trailing slash matches", co.is_coarena("https://coarena.ai/api/"))
    check("host: another host does not",
          not co.is_coarena("https://zro.moonmath.ai"))
    check("host: empty does not", not co.is_coarena("") and not co.is_coarena(None))
    check("host: active_endpoint is the same test",
          co.active_endpoint is co.is_coarena)


def test_profile_roundtrip(tmp: Path) -> None:
    from zimzilla import coarena as co

    with _clean_env():
        path = co.save_key("coa_7c40d4bfab57141ff8b60efc36129a4e",
                           "claude-sonnet-5-5")
        check("profile: it is written where the module says",
              path == co.PROFILE, str(path))
        check("profile: it is mode 600",
              oct(path.stat().st_mode & 0o777) == "0o600",
              oct(path.stat().st_mode & 0o777))
        text = path.read_text()
        check("profile: the base URL is the Messages root",
              'ANTHROPIC_BASE_URL="https://coarena.ai/api"' in text, text)
        check("profile: the key is stored",
              "coa_7c40d4bfab57141ff8b60efc36129a4e" in text)
        check("profile: ANTHROPIC_API_KEY is emptied",
              'ANTHROPIC_API_KEY=""' in text,
              "an inherited key would take precedence over the token")

        creds = co.load_credentials()
        check("profile: it reads back", creds is not None)
        check("profile: the token is the one stored",
              creds[0] == "coa_7c40d4bfab57141ff8b60efc36129a4e")
        check("profile: the model is the one stored",
              creds[1] == "claude-sonnet-5-5", creds[1])
        check("profile: the origin is the profile path",
              creds[2] == str(co.PROFILE), creds[2])
        check("profile: has_key agrees", co.has_key())

        # Rewriting must not leave two token lines for the shell to disagree on.
        co.save_key("coa_secondkeyabcdefghijkl", "")
        text = co.PROFILE.read_text()
        check("profile: a rewrite leaves one token line",
              text.count("ANTHROPIC_AUTH_TOKEN=") == 1,
              str(text.count("ANTHROPIC_AUTH_TOKEN=")))
        check("profile: the new key is in force",
              co.load_credentials()[0] == "coa_secondkeyabcdefghijkl")
        co.PROFILE.unlink()
        check("profile: with no file there is no key",
              co.load_credentials() is None)


def test_credential_precedence(tmp: Path) -> None:
    """Environment first, then the profile, then ~/claude-source."""
    from zimzilla import coarena as co

    with _clean_env():
        co.save_key("coa_profilekeyabcdefghij")
        os.environ["ANTHROPIC_BASE_URL"] = "https://coarena.ai/api"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "coa_envkeyabcdefghijklm"
        creds = co.load_credentials()
        check("creds: the environment wins when it points here",
              creds[0] == "coa_envkeyabcdefghijklm", creds[0])
        check("creds: and the origin says so", creds[2] == "environment", creds[2])

        # An environment pointing somewhere else must not be adopted.
        os.environ["ANTHROPIC_BASE_URL"] = "https://zro.moonmath.ai"
        creds = co.load_credentials()
        check("creds: an environment aimed elsewhere is ignored",
              creds[0] == "coa_profilekeyabcdefghij", creds[0])
        check("creds: and the profile answers", creds[2] == str(co.PROFILE))

        os.environ.pop("ANTHROPIC_BASE_URL")
        os.environ.pop("ANTHROPIC_AUTH_TOKEN")
        co.PROFILE.unlink()

        # A ~/claude-source profile aimed at this host is adopted.
        src = Path(os.environ["ZIMZILLA_CLAUDE_SOURCE"])
        src.mkdir(parents=True, exist_ok=True)
        (src / "coarena").write_text(
            'export ANTHROPIC_BASE_URL="https://coarena.ai/api"\n'
            'export ANTHROPIC_AUTH_TOKEN="coa_claudesourcekeyabc"\n'
            'export ANTHROPIC_MODEL="claude-opus-5-5"\n')
        creds = co.load_credentials()
        check("creds: a claude-source profile is adopted",
              creds is not None and creds[0] == "coa_claudesourcekeyabc",
              str(creds))
        check("creds: with its model", creds[1] == "claude-opus-5-5", creds[1])

        # One aimed elsewhere is not.
        (src / "coarena").write_text(
            'export ANTHROPIC_BASE_URL="https://zro.moonmath.ai"\n'
            'export ANTHROPIC_AUTH_TOKEN="sk_wrongprovider"\n')
        check("creds: a profile aimed elsewhere is not adopted",
              co.load_credentials() is None)
        (src / "coarena").unlink()


def test_apply_to_and_default_endpoint(tmp: Path) -> None:
    from zimzilla import coarena as co
    from zimzilla.config import Config

    with _clean_env():
        cfg = Config()
        check("apply: with no key it returns None", co.apply_to(cfg) is None)

        co.save_key("coa_7c40d4bfab57141ff8b60efc36129a4e", "claude-opus-5-5")
        cfg = Config()
        cfg.api_key = "sk-stale-from-another-provider"
        applied = co.apply_to(cfg)
        check("apply: it returns the origin and model",
              applied is not None and applied[1] == "claude-opus-5-5", str(applied))
        check("apply: the base URL is the Messages root",
              cfg.base_url == co.DEFAULT_BASE_URL, cfg.base_url)
        check("apply: the token is set",
              cfg.auth_token == "coa_7c40d4bfab57141ff8b60efc36129a4e")
        check("apply: a stale api_key is cleared",
              cfg.api_key == "",
              "a credential for the wrong service must not sit in memory")
        check("apply: the model is the profile's",
              cfg.model == "claude-opus-5-5", cfg.model)

        # A profile with no model must not override an explicit --model.
        co.save_key("coa_7c40d4bfab57141ff8b60efc36129a4e", "")
        cfg = Config()
        cfg.model = "explicit-choice"
        co.apply_to(cfg)
        check("apply: an empty profile model leaves the choice alone",
              cfg.model == "explicit-choice", cfg.model)

        check("default: a local proxy is overridable",
              co.applies_to_default_endpoint("http://localhost:4000"))
        check("default: an empty endpoint is overridable",
              co.applies_to_default_endpoint(""))
        check("default: this gateway is not",
              not co.applies_to_default_endpoint(co.DEFAULT_BASE_URL))
        check("default: another host is not",
              not co.applies_to_default_endpoint("https://zro.moonmath.ai"))
        co.PROFILE.unlink()


def test_sources_registry_dispatch() -> None:
    from zimzilla import coarena as co
    from zimzilla import sources as s

    check("registry: active_key recognises the Messages root",
          s.active_key(co.DEFAULT_BASE_URL) == "coarena",
          str(s.active_key(co.DEFAULT_BASE_URL)))
    check("registry: and the OpenAI root",
          s.active_key(co.API_BASE_URL) == "coarena")
    check("registry: it is a built-in key", "coarena" in s.BUILTIN_KEYS)
    check("registry: it is not a direct-only source",
          "coarena" not in s.DIRECT_ONLY, str(sorted(s.DIRECT_ONLY)))
    check("registry: it has no declared proxy route",
          "coarena" not in s.DECLARED_ROUTES)
    check("registry: the curated fallback is offered",
          co.DEFAULT_MODEL in s.COARENA_MODELS, str(s.COARENA_MODELS))
    check("registry: the fallback is the documented surface",
          set(s.COARENA_MODELS) >= {"claude-sonnet-5-5", "claude-opus-5-5",
                                    "claude-haiku-5-5", "grok-4-7"},
          str(s.COARENA_MODELS))
    # Cold cache on purpose: an earlier test in this file leaves one warm, and
    # the question here is what /model offers *before* any fetch has happened.
    co.clear_cache()
    check("registry: models_for answers before any fetch",
          s.models_for(co.DEFAULT_BASE_URL) == s.COARENA_MODELS,
          str(s.models_for(co.DEFAULT_BASE_URL)))
    check("registry: a cold cache still prices at (0, 0)",
          s.catalog_price_for(co.DEFAULT_BASE_URL, "claude-sonnet-5-5")
          == (0.0, 0.0),
          "a cold cache must not fall through to the estimate table")

    with mock.patch("urllib.request.urlopen", _stub()):
        co.clear_cache()
        co.fetch_models()
        warm = s.models_for(co.DEFAULT_BASE_URL)
        check("registry: a warm cache gives the live ids",
              warm is not None and tuple(warm) == tuple(
                  m.id for m in co._parse_models(CATALOG)), str(warm))
        check("registry: catalog_models returns Model objects",
              len(s.catalog_models("coarena") or []) == 5)
        check("registry: the free models are marked",
              {m.id for m in (s.catalog_models("coarena") or []) if m.free}
              == {"claude-sonnet-5-5", "deepseek-v4-1-flash-1017",
                  "no-tools-model"})
        check("registry: the window is read from the catalog",
              s.context_window_for(co.DEFAULT_BASE_URL, "claude-sonnet-5-5")
              == 128_000,
              str(s.context_window_for(co.DEFAULT_BASE_URL,
                                       "claude-sonnet-5-5")))
        check("registry: an alias sizes like the bare id",
              s.context_window_for(co.DEFAULT_BASE_URL, "sonnet-5-5") == 128_000)

    check("registry: an unknown key is still None",
          s.catalog_models("nope") is None)

    check("switch: it needs a key before it is suggested",
          "coarena" not in s.switchable(), str(s.switchable()))
    with _clean_env():
        co.save_key("coa_7c40d4bfab57141ff8b60efc36129a4e")
        check("switch: with a key it is offered",
              "coarena" in s.switchable(), str(s.switchable()))
        check("switch: but never as the source that just failed",
              "coarena" not in s.switchable(exclude="coarena"))
        co.PROFILE.unlink()


def test_discovery_is_a_gateway_not_a_source() -> None:
    """It is hosted: no port, no service, no discover() entry."""
    from zimzilla import sources as s

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "zimzilla"
        home.mkdir()
        zhome.mkdir()
        with mock.patch.object(s, "DEFAULT_HOME", home), \
             mock.patch.object(s, "ZIMZILLA_HOME", zhome):
            check("discovery: it never appears as a local source",
                  s.discover().get("coarena") is None)
            (zhome / "coarena").mkdir()
            (zhome / "coarena" / "source").write_text(
                'export ANTHROPIC_AUTH_TOKEN="x"\n')
            check("discovery: a profile does not turn it into one either",
                  s.discover().get("coarena") is None)


def test_tui_surfaces() -> None:
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main_src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")

    for cmd in ("zim-coarena", "coarena-api-setup", "coarena-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app_src)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp_src)

    check("tui: a switch with no model lands on the free default",
          "self.cfg.model = co_mod.default_free_model()" in app_src)
    check("tui: the gateway table knows it",
          'return co_mod, "Coarena"' in app_src)
    check("tui: the status pane lists it",
          '("coarena", co_mod, "Coarena"' in app_src)
    check("tui: the model picker knows it",
          'if active == "coarena" else None' in app_src)
    check("tui: /model refetches for it", 'elif active == "coarena":' in app_src)
    check("tui: /models accepts it", '"beatapi", "coarena",' in app_src)
    check("tui: /models dispatches to its free list",
          'return co_mod.usable_free_models(creds[0] if creds else "")' in app_src)
    check("tui: /models labels the listing",
          '"coarena": "COARENA"' in app_src)
    check("tui: the startup warm-up runs for it",
          "self._warm_coarena_catalog()" in app_src)
    check("tui: the setup command proves the key it stored",
          "self._verify_coarena_key(key)" in app_src)
    check("tui: the probe reports through the transcript",
          "def _report_coarena_key(self, ok: bool, detail: str)" in app_src)
    check("tui: the module is imported by the app",
          "from .. import coarena as co_mod" in app_src)
    check("tui: /models completion offers it", '"coarena"' in comp_src,
          "the /models source list must offer coarena")
    check("tui: the setup panel names where keys come from",
          "https://coarena.ai/account/api/keys" in app_src)
    check("tui: the setup panel warns that prompts are stored",
          "datasets it licenses" in app_src)
    check("tui: a fallback message can suggest it",
          "/zim-coarena" in app_src)
    check("tui: the price note calls it free",
          'return "free (daily allowance)"' in app_src)

    # A hosted gateway restores through the shared loop.
    check("main: the restore loop includes it",
          '("coarena", co_mod)' in main_src)
    check("main: the module is imported",
          "from . import coarena as co_mod" in main_src)

    # The agent's credential table names it, so a turn can find the key.
    agent_src = (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8")
    check("agent: the credential table includes it",
          '("coarena", "Coarena", "ANTHROPIC_AUTH_TOKEN")' in agent_src)


def test_launcher_restores_the_route() -> None:
    """A remembered Coarena selection must survive a restart.

    Without a hosted branch the launcher takes the Logfare path: it starts
    LiteLLM for a session that will never use it, and on a machine with no
    Logfare profile dies fail-closed with "profile not found" — a session that
    cannot start, for a reason that is not true.
    """
    launcher = (ROOT / "packaging" / "zimzilla").read_text(encoding="utf-8")

    check("launcher: the hosted branch includes it",
          "tokenharbour|openrouter|moonmath|coarena)" in launcher)
    start = launcher.index('case "$_current_source" in')
    end = launcher.index("esac", start)
    branch = launcher[start:end]
    check("launcher: it sources the operator's profile",
          '"$ZIMZILLA_HOME/$_current_source/source"' in branch,
          branch.strip()[:200])
    check("launcher: it turns the proxy off", "ZIMZILLA_NO_PROXY=1" in branch,
          branch.strip()[:200])
    check("launcher: it has no shipped profile to fall back to",
          "packaging/coarena" not in launcher)


def test_nothing_ships_a_credential() -> None:
    pkg = ROOT / "packaging"
    leaked = [str(p.relative_to(ROOT)) for p in pkg.rglob("*")
              if p.is_file() and "coarena" in _safe_text(p).lower()
              and re.search(r"coa_[A-Za-z0-9]{20,}", _safe_text(p))]
    check("packaging: no Coarena profile or key ships in the tree", not leaked,
          str(leaked))
    check("packaging: no coarena profile ships",
          not (pkg / "coarena" / "source").exists())

    co_src = (ROOT / "zimzilla" / "coarena.py").read_text(encoding="utf-8")
    pasted = re.findall(r"coa_[A-Za-z0-9]{20,}", co_src)
    check("module: no literal coa_ key in the source", not pasted,
          f"a key is pasted into zimzilla/coarena.py: {len(pasted)} match(es)")


def _safe_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-co-test-"))
    # Pin the module's paths to the temp tree before anything imports it.
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "ANTHROPIC_MODEL", "ZIMZILLA_PROTOCOL")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        test_route_is_the_messages_root()
        test_parser_reads_the_catalog()
        test_free_is_a_zero_price_not_a_missing_one()
        test_aliases_resolve_everywhere()
        test_catalog_is_public_and_unauthenticated()
        test_fetch_is_total_and_cached()
        test_free_lists()
        test_verify_key_uses_the_keyed_limits_route()
        test_verify_key_does_not_fail_on_a_billing_state()
        test_explain_error_shapes()
        test_allowance_line()
        test_looks_like_key()
        test_is_coarena_matches_by_host()
        test_profile_roundtrip(tmp)
        test_credential_precedence(tmp)
        test_apply_to_and_default_endpoint(tmp)
        test_sources_registry_dispatch()
        test_discovery_is_a_gateway_not_a_source()
        test_tui_surfaces()
        test_launcher_restores_the_route()
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
