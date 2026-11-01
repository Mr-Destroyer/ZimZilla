"""Regression suite for the Zro (MoonMath.ai) source.

Run:  python tests/test_moonmath.py   (from an activated venv)

Protects what is particular about this provider:

* **It is a hosted, Anthropic-native gateway, not a translated one.** Zro
  serves ``POST /v1/messages`` itself, so the session is pointed at the bare
  host — ``https://zro.moonmath.ai`` — and the SDK appends ``/v1/messages``.
  Spelling ``/v1`` in the base URL produces ``/v1/v1/messages`` and a 404 on
  every turn while the catalog still works, which is the one mistake this file
  is most concerned with.
* **The catalog is public, priced, and carries no free flag.** ``GET /models``
  answers 200 with no credential and lists a real per-token rate for every
  model. Free-ness is the Free plan's included daily usage, so
  ``FREE_MODEL_IDS`` is the authority and a *missing* price must never be read
  as free — that is what ``Model.priced`` records.
* **The probe is a real credential test.** ``GET /v1/models`` answers 401
  without a key, so one authenticated GET proves a key without sending a turn.
* **A billing state is not a bad key.** 402 and 429 come back ``ok=True`` from
  ``verify_key``; only 401/403 and an unreachable host fail it.
* **Both spellings price the same turn.** The gateway accepts ``glm-5.3`` and
  ``zro/glm-5.3``; the second must not fall through to the estimate table and
  bill a free model.
* **Nothing ships a credential.** ``~/.zimzilla/moonmath/source`` is written by
  ``/moonmath-api-setup`` and nothing under ``packaging/`` contains a key.

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


#: Shaped like the live payload fetched on 2026-11-01 — ``{"data": [...]}`` with
#: per-*token* prices as strings, so the per-million conversion the module does
#: is exercised rather than assumed. A richer fixture would let the parser pass
#: on data the endpoint never sends.
CATALOG = {
    "data": [
        {
            "id": "deepseek-v4.1-flash",
            "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash",
            "name": "DeepSeek V4.1 Flash",
            "context_length": 1_048_576,
            "max_output_length": 64_000,
            "pricing": {"prompt": "0.0000003", "completion": "0.0000012",
                        "image": "0", "request": "0"},
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "supported_features": ["tools", "json_mode", "logprobs",
                                   "reasoning"],
            "description": "Text model with tool use.",
        },
        {
            "id": "glm-5.3",
            "hugging_face_id": "zai-org/GLM-5.3",
            "name": "GLM-5.3",
            "context_length": 1_048_576,
            "max_output_length": 131_000,
            "pricing": {"prompt": "0.0000014", "completion": "0.0000044"},
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supported_features": ["tools", "reasoning"],
            "description": "Text model.",
        },
        {
            "id": "glm-5.3-flash",
            "hugging_face_id": "zai-org/GLM-5.3-Flash",
            "name": "GLM-5.3 Flash",
            "context_length": 1_048_576,
            "max_output_length": 64_000,
            "pricing": {"prompt": "0.00000015", "completion": "0.0000005"},
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supported_features": ["tools", "reasoning"],
            "description": "Small text model.",
        },
        # An entry with no pricing at all: it must not read as free.
        {
            "id": "dolly1-security",
            "name": "Dolly 1 Security",
            "context_length": 131_072,
            "output_modalities": ["text"],
            "supported_features": [],
        },
        # A malformed row and a non-text model.
        "not-a-row",
        {"no_id": True},
        {"id": "   "},
        {"id": "zro-embed-1", "output_modalities": ["embedding"],
         "context_length": 8192},
    ],
}

CHAT = ["deepseek-v4.1-flash", "glm-5.3", "glm-5.3-flash", "dolly1-security"]


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
    """Context manager that hides any Zro credential in the environment."""
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
    """The base URL is the host, not ``/v1`` — the 404-every-turn trap."""
    from zimzilla import moonmath as mm
    from zimzilla import openai_compat as oa

    check("route: the Messages base URL is the bare host",
          mm.DEFAULT_BASE_URL == "https://zro.moonmath.ai",
          mm.DEFAULT_BASE_URL)
    check("route: it does not spell /v1 (the SDK appends /v1/messages)",
          not mm.DEFAULT_BASE_URL.endswith("/v1"), mm.DEFAULT_BASE_URL)
    check("route: Chat Completions lives under /v1",
          mm.API_BASE_URL == "https://zro.moonmath.ai/v1", mm.API_BASE_URL)
    check("route: protocol auto resolves to anthropic at the root",
          oa.resolve_protocol(mm.DEFAULT_BASE_URL, "auto") == "anthropic",
          oa.resolve_protocol(mm.DEFAULT_BASE_URL, "auto"))
    check("route: and to openai when the operator names /v1",
          oa.resolve_protocol(mm.API_BASE_URL, "auto") == "openai",
          oa.resolve_protocol(mm.API_BASE_URL, "auto"))
    check("route: the host is named as Anthropic-native",
          "zro.moonmath.ai" in oa.ANTHROPIC_HOSTS)


def test_parser_reads_the_catalog() -> None:
    from zimzilla import moonmath as mm

    models = mm._parse_models(CATALOG)
    by_id = {m.id: m for m in models}
    check("parse: every well-formed row kept", len(models) == 5, str(len(models)))
    check("parse: a bare string is dropped", "not-a-row" not in by_id)
    check("parse: a row with no id is dropped", "   " not in by_id)
    check("parse: the owner comes from the weights id",
          by_id["deepseek-v4.1-flash"].owner == "deepseek-ai",
          by_id["deepseek-v4.1-flash"].owner)
    check("parse: the context window is carried",
          by_id["glm-5.3"].context_length == 1_048_576,
          str(by_id["glm-5.3"].context_length))
    check("parse: max output is carried",
          by_id["glm-5.3"].max_output == 131_000,
          str(by_id["glm-5.3"].max_output))
    check("parse: per-token prices become per-Mtok",
          (by_id["deepseek-v4.1-flash"].price_in,
           by_id["deepseek-v4.1-flash"].price_out) == (0.3, 1.2),
          str((by_id["deepseek-v4.1-flash"].price_in,
               by_id["deepseek-v4.1-flash"].price_out)))
    check("parse: a price was read", by_id["deepseek-v4.1-flash"].priced)
    check("parse: an unpriced row says so",
          not by_id["dolly1-security"].priced)
    check("parse: features are carried",
          by_id["deepseek-v4.1-flash"].tools
          and by_id["deepseek-v4.1-flash"].reasoning)
    check("parse: image input does not make a model non-chat",
          by_id["deepseek-v4.1-flash"].chat)
    check("parse: an embedding model is not chat",
          not by_id["zro-embed-1"].chat)


def test_free_is_the_plan_not_a_zero_price() -> None:
    """The catalog prices everything; the Free plan covers two ids."""
    from zimzilla import moonmath as mm
    from zimzilla import sources as s

    models = mm._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("free: DeepSeek V4.1 Flash is on the Free plan",
          by_id["deepseek-v4.1-flash"].free)
    check("free: GLM-5.3 Flash is on the Free plan",
          by_id["glm-5.3-flash"].free)
    check("free: GLM-5.3 is NOT free", not by_id["glm-5.3"].free)
    check("free: an unpriced entry is NOT free — no price is not a zero price",
          not by_id["dolly1-security"].free,
          "a missing pricing object must not read as 0/0")
    check("free: the Free plan set is what the provider documents",
          mm.FREE_MODEL_IDS == {"deepseek-v4.1-flash", "glm-5.3-flash"},
          str(sorted(mm.FREE_MODEL_IDS)))

    check("price: a free model answers (0, 0)",
          mm.price_for("deepseek-v4.1-flash", models) == (0.0, 0.0),
          str(mm.price_for("deepseek-v4.1-flash", models)))
    check("price: a paid model answers the published rate",
          mm.price_for("glm-5.3", models) == (1.4, 4.4),
          str(mm.price_for("glm-5.3", models)))
    check("price: an unpriced model answers None, not (0, 0)",
          mm.price_for("dolly1-security", models) is None)
    check("price: an unknown id answers None",
          mm.price_for("nope", models) is None)

    with mock.patch("urllib.request.urlopen", _stub()):
        mm.clear_cache()
        mm.fetch_models()
        warm_free = s.catalog_price_for(mm.DEFAULT_BASE_URL, "glm-5.3-flash")
        warm_paid = s.catalog_price_for(mm.DEFAULT_BASE_URL, "glm-5.3")
        warm_unpriced = s.catalog_price_for(mm.DEFAULT_BASE_URL, "dolly1-security")
    check("price: the registry reports a free model as free",
          warm_free == (0.0, 0.0), str(warm_free))
    check("price: the registry reports the paid rate",
          warm_paid == (1.4, 4.4), str(warm_paid))
    check("price: an unpriced model falls through to the estimate",
          warm_unpriced is None, str(warm_unpriced))


def test_aliases_resolve_everywhere() -> None:
    """``zro/glm-5.3`` must price and size like ``glm-5.3``."""
    from zimzilla import moonmath as mm
    from zimzilla import sources as s

    models = mm._parse_models(CATALOG)
    check("alias: model_for resolves the prefixed spelling",
          mm.model_for("zro/glm-5.3", models) is not None
          and mm.model_for("zro/glm-5.3", models).id == "glm-5.3")
    check("alias: price_for resolves it too",
          mm.price_for("zro/glm-5.3", models) == (1.4, 4.4),
          str(mm.price_for("zro/glm-5.3", models)))
    check("alias: the alias is declared on the entry",
          "zro/glm-5.3" in models[1].aliases, str(models[1].aliases))

    with mock.patch("urllib.request.urlopen", _stub()):
        mm.clear_cache()
        mm.fetch_models()
        check("alias: the registry prices the prefixed spelling",
              s.catalog_price_for(mm.DEFAULT_BASE_URL, "zro/glm-5.3")
              == (1.4, 4.4),
              str(s.catalog_price_for(mm.DEFAULT_BASE_URL, "zro/glm-5.3")))
        check("alias: the registry sizes it",
              s.context_window_for(mm.DEFAULT_BASE_URL, "zro/glm-5.3")
              == 1_048_576)
        check("alias: and its output ceiling",
              s.max_output_for(mm.DEFAULT_BASE_URL, "zro/glm-5.3") == 131_000)


def test_catalog_is_public_and_unauthenticated() -> None:
    """Public data, and no credential is sent to it — even when one exists."""
    from zimzilla import moonmath as mm

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        mm.clear_cache()
        models, err = mm.fetch_models()

    check("catalog: it fetches without a key",
          models is not None and err == "", err)
    check("catalog: the URL is the public catalog",
          seen.get("url") == "https://zro.moonmath.ai/models",
          str(seen.get("url")))
    check("catalog: it is a GET", seen.get("method") == "GET")
    check("catalog: no Authorization header without a key",
          "authorization" not in seen.get("headers", {}),
          str(seen.get("headers")))
    check("catalog: a User-Agent is sent",
          seen.get("headers", {}).get("user-agent") == "zimzilla",
          str(seen.get("headers", {}).get("user-agent")))

    seen2, opener2 = _capture()
    with mock.patch("urllib.request.urlopen", opener2):
        mm.clear_cache()
        mm.fetch_models("sk-test-key-abcdefghijkl")
    check("catalog: a supplied key is NOT sent to the public catalog",
          "authorization" not in seen2.get("headers", {}),
          str(seen2.get("headers")))


def test_fetch_is_total_and_cached() -> None:
    from zimzilla import moonmath as mm

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        mm.clear_cache()
        models, err = mm.fetch_models()
    check("fetch: a connection failure returns None and an error",
          models is None and "zro.moonmath.ai" in err, repr(err))

    with mock.patch("urllib.request.urlopen",
                    _stub(error=_http_error(429, ""))):
        mm.clear_cache()
        models, err = mm.fetch_models()
    check("fetch: a 429 is reported as rate limiting",
          models is None and "429" in err, repr(err))

    with mock.patch("urllib.request.urlopen", _stub(payload={"data": "junk"})):
        mm.clear_cache()
        models, err = mm.fetch_models()
    check("fetch: a malformed payload is an empty catalog, not a crash",
          models == [] and err == "", repr(models))

    with mock.patch("urllib.request.urlopen", _stub()):
        mm.clear_cache()
        check("cache: cold is None", mm.cached_models() is None)
        check("cache: cold has no age", mm.catalog_age() is None)
        mm.fetch_models()
        check("cache: warm has models", mm.cached_models() is not None)
        check("cache: cached_ids keeps chat entries",
              mm.cached_ids() == CHAT, str(mm.cached_ids()))
        check("cache: an embedding model is not offered",
              "zro-embed-1" not in (mm.cached_ids() or []))
        mm.fetch_models("sk-another-key-abcdefghijkl")
        check("cache: a keyed fetch is its own entry", len(mm._CACHE) == 2,
              str(len(mm._CACHE)))
        for k, (cached, when) in list(mm._CACHE.items()):
            mm._CACHE[k] = (cached, when - mm.CACHE_TTL - 1)
        check("cache: an expired entry reads as None", mm.cached_models() is None)
        check("cache: but the age is still reported",
              (mm.catalog_age() or 0) > mm.CACHE_TTL, str(mm.catalog_age()))
        mm.clear_cache()


def test_verify_key_uses_the_keyed_models_route() -> None:
    from zimzilla import moonmath as mm

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        ok, detail = mm.verify_key("sk-test-key-abcdefghijkl")
    check("verify: a working key is accepted", ok, detail)
    check("verify: it GETs /v1/models",
          seen.get("method") == "GET"
          and seen.get("url") == "https://zro.moonmath.ai/v1/models",
          f"{seen.get('method')} {seen.get('url')}")
    check("verify: it sends the bearer token",
          seen.get("headers", {}).get("authorization")
          == "Bearer sk-test-key-abcdefghijkl")
    check("verify: no body is sent", seen.get("body") is None)

    with mock.patch("urllib.request.urlopen", _stub(error=_http_error(401, ""))):
        ok, detail = mm.verify_key("sk-bad-key-abcdefghijkl")
    check("verify: a 401 fails the key", not ok, detail)
    check("verify: and the message names the setup command",
          "/moonmath-api-setup" in detail, detail)

    with mock.patch("urllib.request.urlopen", _stub(error=_http_error(403, ""))):
        ok, detail = mm.verify_key("sk-bad-key-abcdefghijkl")
    check("verify: a 403 fails the key too", not ok, detail)

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        ok, detail = mm.verify_key("sk-test-key-abcdefghijkl")
    check("verify: an unreachable endpoint fails it", not ok, detail)

    check("verify: an empty key is refused without a request",
          mm.verify_key("") == (False, "no key"))


def test_verify_key_does_not_fail_on_a_billing_state() -> None:
    """402 and 429 say something about the account, never about the key."""
    from zimzilla import moonmath as mm

    cases = {
        402: '{"error": {"message": "Spend quota exhausted for this period"}}',
        429: '{"error": {"message": "Rate limit exceeded"}}',
        500: '{"error": {"message": "upstream exploded"}}',
        404: '{"error": {"message": "Unknown model"}}',
    }
    for status, body in cases.items():
        with mock.patch("urllib.request.urlopen",
                        _stub(error=_http_error(status, body))):
            ok, detail = mm.verify_key("sk-test-key-abcdefghijkl")
        check(f"verify: a {status} still accepts the key", ok, detail)
        check(f"verify: the {status} reason is explained", bool(detail), detail)


def test_explain_error_shapes() -> None:
    from zimzilla import moonmath as mm

    check("explain: 401 blames the key",
          "/moonmath-api-setup" in mm.explain_error(401, ""))
    check("explain: 402 is a billing state, not a bad key",
          "quota" in mm.explain_error(402, "").lower()
          or "budget" in mm.explain_error(402, "").lower(),
          mm.explain_error(402, ""))
    check("explain: 403 says the key is fine but the model is not allowed",
          "not permitted" in mm.explain_error(403, "")
          and "key" in mm.explain_error(403, ""),
          mm.explain_error(403, ""))
    check("explain: 404 says unknown model",
          "unknown model" in mm.explain_error(404, ""))
    check("explain: 429 says rate limited",
          "rate limited" in mm.explain_error(429, ""))
    check("explain: 5xx says retry",
          "retry" in mm.explain_error(503, "") and "503" in mm.explain_error(503, ""))
    check("explain: an unknown status keeps its detail",
          "boom" in mm.explain_error(500, '{"detail":"boom"}'),
          mm.explain_error(500, '{"detail":"boom"}'))
    check("explain: an empty body still answers",
          "500" in mm.explain_error(500, ""), mm.explain_error(500, ""))
    check("explain: the gateway's own message is surfaced",
          "revoked" in mm.explain_error(
              418, '{"error": {"message": "this key was revoked"}}'))


def test_looks_like_key() -> None:
    from zimzilla import moonmath as mm

    check("key: an sk- token passes", mm.looks_like_key("sk-" + "a" * 40))
    check("key: the prefix is NOT required", mm.looks_like_key("a" * 40))
    check("key: a short string fails", not mm.looks_like_key("short"))
    check("key: whitespace fails", not mm.looks_like_key("two words here now"))
    check("key: a whole export line fails",
          not mm.looks_like_key('export ANTHROPIC_AUTH_TOKEN="abc"'))
    check("key: a URL-ish value fails", not mm.looks_like_key("https://zro.moonmath.ai"))
    check("key: empty fails", not mm.looks_like_key(""))


def test_is_moonmath_matches_by_host() -> None:
    from zimzilla import moonmath as mm
    from zimzilla import sources as s

    check("endpoint: the root matches", mm.is_moonmath("https://zro.moonmath.ai"))
    check("endpoint: a trailing slash matches",
          mm.is_moonmath("https://zro.moonmath.ai/"))
    check("endpoint: the API path matches",
          mm.is_moonmath("https://zro.moonmath.ai/v1"))
    check("endpoint: another provider does not match",
          not mm.is_moonmath("https://api.openai.com/v1"))
    check("endpoint: a local proxy does not match",
          not mm.is_moonmath("http://localhost:4001"))
    check("endpoint: empty is not a match", not mm.is_moonmath(""))
    check("endpoint: None is not a match", not mm.is_moonmath(None))
    check("endpoint: the generic alias is the same test",
          mm.active_endpoint is mm.is_moonmath)

    check("endpoint: sources identifies it by host",
          s.active_key("https://zro.moonmath.ai") == "moonmath",
          str(s.active_key("https://zro.moonmath.ai")))
    check("endpoint: and on its API path",
          s.active_key("https://zro.moonmath.ai/v1") == "moonmath")
    check("endpoint: sources does not identify another provider",
          s.active_key("https://api.openai.com/v1") is None)


def test_profile_roundtrip(tmp: Path) -> None:
    from zimzilla import moonmath as mm

    with _clean_env():
        path = mm.save_key("sk-test-key-1234567890", "glm-5.3-flash")
        check("profile: written under ZIMZILLA_HOME",
              path == tmp / ".zimzilla" / "moonmath" / "source", str(path))
        check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
              oct(path.stat().st_mode & 0o777))
        check("profile: parent is 700",
              oct(path.parent.stat().st_mode & 0o777) == "0o700",
              oct(path.parent.stat().st_mode & 0o777))
        text = path.read_text()
        check("profile: it points at the Messages ROOT, not /v1",
              'ANTHROPIC_BASE_URL="https://zro.moonmath.ai"' in text,
              text.splitlines()[2] if len(text.splitlines()) > 2 else "")
        check("profile: the model is preserved",
              'ANTHROPIC_MODEL="glm-5.3-flash"' in text)
        check("profile: ANTHROPIC_API_KEY is blanked, as every profile does",
              'ANTHROPIC_API_KEY=""' in text)

        creds = mm.load_credentials()
        check("profile: the key loads back",
              creds is not None and creds[0] == "sk-test-key-1234567890")
        check("profile: the model loads back",
              creds is not None and creds[1] == "glm-5.3-flash")
        check("profile: the origin is the path",
              creds is not None and creds[2] == str(mm.PROFILE))
        check("profile: has_key agrees", mm.has_key())

        path.unlink()


def test_credential_precedence(tmp: Path) -> None:
    from zimzilla import moonmath as mm

    with _clean_env():
        check("creds: nothing set is None", mm.load_credentials() is None)
        mm.save_key("sk_from_profile_0001")
        check("creds: the own profile is used",
              mm.load_credentials()[2] == str(mm.PROFILE),
              str(mm.load_credentials()[2]))

        os.environ["ANTHROPIC_BASE_URL"] = "https://zro.moonmath.ai"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk_from_env_0002"
        check("creds: the environment wins when aimed at Zro",
              mm.load_credentials()[0] == "sk_from_env_0002")
        check("creds: and it is named as the environment",
              mm.load_credentials()[2] == "environment")

        os.environ["ANTHROPIC_BASE_URL"] = "https://api.example.com"
        check("creds: a token for another endpoint is ignored",
              mm.load_credentials()[0] == "sk_from_profile_0001")
        os.environ.pop("ANTHROPIC_BASE_URL")
        os.environ.pop("ANTHROPIC_AUTH_TOKEN")

        mm.PROFILE.unlink()

        # The `source ~/claude-source/<name>` idiom, which the gateway's own
        # docs publish for Claude Code — read, never written.
        mm.CLAUDE_SOURCE.mkdir(parents=True, exist_ok=True)
        prof = mm.CLAUDE_SOURCE / "zro"
        prof.write_text(
            'export ANTHROPIC_BASE_URL="https://zro.moonmath.ai"\n'
            'export ANTHROPIC_AUTH_TOKEN="sk_from_claude_source_0003"\n'
            'export ANTHROPIC_MODEL="glm-5.3-flash"\n')
        creds = mm.load_credentials()
        check("creds: a sourced ~/claude-source profile is honoured",
              creds is not None and creds[0] == "sk_from_claude_source_0003",
              str(creds))
        check("creds: its origin is named", creds is not None
              and creds[2] == str(prof), str(creds))

        # A profile aimed at another provider is that provider's credential.
        prof.write_text(
            'export ANTHROPIC_BASE_URL="https://api.example.com"\n'
            'export ANTHROPIC_AUTH_TOKEN="sk_someone_else_0004"\n')
        check("creds: a profile for another gateway is not adopted",
              mm.load_credentials() is None, str(mm.load_credentials()))
        prof.unlink()


def test_apply_to_and_default_endpoint(tmp: Path) -> None:
    from zimzilla import moonmath as mm
    from zimzilla.config import Config

    with _clean_env():
        cfg = Config.from_env(workdir=tmp, api_key="stale-key",
                              base_url="https://logfare.ai/v1",
                              boot_rain=False)
        check("apply: no key means no change",
              mm.apply_to(cfg) is None and cfg.base_url == "https://logfare.ai/v1",
              cfg.base_url)

        mm.save_key("sk-test-key-abcdefghijkl")
        applied = mm.apply_to(cfg)
        check("apply: it returns the origin and model",
              applied is not None and applied[0] == str(mm.PROFILE),
              str(applied))
        check("apply: the base URL is the Messages root",
              cfg.base_url == "https://zro.moonmath.ai", cfg.base_url)
        check("apply: the token is in force",
              cfg.auth_token == "sk-test-key-abcdefghijkl")
        check("apply: the stale api_key is cleared", cfg.api_key == "",
              repr(cfg.api_key))

        check("defaults: the built-in default is taken over",
              mm.applies_to_default_endpoint("http://localhost:4001"))
        check("defaults: Logfare's direct endpoint is not (a deliberate host)",
              not mm.applies_to_default_endpoint("https://logfare.ai/v1"))
        check("defaults: its own endpoint is not re-applied",
              not mm.applies_to_default_endpoint("https://zro.moonmath.ai"))
        check("defaults: empty is the default",
              mm.applies_to_default_endpoint(""))

        mm.PROFILE.unlink()


def test_sources_registry_dispatch() -> None:
    from zimzilla import moonmath as mm
    from zimzilla import sources as s

    check("registry: it is a built-in hosted key", "moonmath" in s.BUILTIN_KEYS)
    check("registry: it is not a local proxy",
          "moonmath" not in s.DIRECT_ONLY, str(sorted(s.DIRECT_ONLY)))
    check("registry: it has no declared proxy route",
          "moonmath" not in s.DECLARED_ROUTES)
    check("registry: the curated fallback is offered",
          mm.DEFAULT_MODEL in s.MOONMATH_MODELS, str(s.MOONMATH_MODELS))
    check("registry: the fallback is the documented surface",
          set(s.MOONMATH_MODELS) >= {"deepseek-v4.1-flash", "glm-5.3",
                                     "glm-5.3-flash"},
          str(s.MOONMATH_MODELS))
    check("registry: models_for answers before any fetch",
          s.models_for(mm.DEFAULT_BASE_URL) == s.MOONMATH_MODELS,
          str(s.models_for(mm.DEFAULT_BASE_URL)))

    with mock.patch("urllib.request.urlopen", _stub()):
        mm.clear_cache()
        mm.fetch_models()
        warm = s.models_for(mm.DEFAULT_BASE_URL)
        check("registry: a warm cache gives the live chat ids",
              warm is not None and tuple(warm) == tuple(CHAT), str(warm))
        check("registry: catalog_models returns Model objects",
              len(s.catalog_models("moonmath") or []) == 5)
        check("registry: the free models are marked",
              {m.id for m in (s.catalog_models("moonmath") or []) if m.free}
              == {"deepseek-v4.1-flash", "glm-5.3-flash"})

    check("registry: an unknown key is still None",
          s.catalog_models("nope") is None)

    check("switch: it needs a key before it is suggested",
          "moonmath" not in s.switchable(), str(s.switchable()))
    with _clean_env():
        mm.save_key("sk-test-key-abcdefghijkl")
        check("switch: with a key it is offered",
              "moonmath" in s.switchable(), str(s.switchable()))
        check("switch: but never as the source that just failed",
              "moonmath" not in s.switchable(exclude="moonmath"))
        mm.PROFILE.unlink()


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
                  s.discover().get("moonmath") is None)
            (zhome / "moonmath").mkdir()
            (zhome / "moonmath" / "source").write_text(
                'export ANTHROPIC_AUTH_TOKEN="x"\n')
            check("discovery: a profile does not turn it into one either",
                  s.discover().get("moonmath") is None)


def test_tui_surfaces() -> None:
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main_src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")

    for cmd in ("zim-moonmath", "moonmath-api-setup", "moonmath-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app_src)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp_src)

    # The spellings the operator may reasonably type.
    for cmd in ("zro-moonmath-api-setup", "zro-moonmath-models",
                "zim-zro-moonmath"):
        check(f"tui: /{cmd} is an accepted alias", f'"{cmd}":' in app_src)

    check("tui: the gateway table knows it",
          'return mm_mod, "Zro (MoonMath.ai)"' in app_src)
    check("tui: the status pane lists it",
          '("moonmath", mm_mod, "Zro"' in app_src)
    check("tui: the model picker knows it",
          'if active == "moonmath" else None' in app_src)
    check("tui: /model refetches for it", 'elif active == "moonmath":' in app_src)
    check("tui: /models accepts it", '"moonmath", "iteracompute"' in app_src)
    check("tui: /models dispatches to its free list",
          'return mm_mod.usable_free_models(creds[0] if creds else "")' in app_src)
    check("tui: /models labels the listing",
          '"moonmath": "ZRO (MOONMATH.AI)"' in app_src)
    check("tui: the startup warm-up runs for it",
          "self._warm_moonmath_catalog()" in app_src)
    check("tui: the setup command proves the key it stored",
          "self._verify_moonmath_key(key)" in app_src)
    check("tui: the probe reports through the transcript",
          "def _report_moonmath_key(self, ok: bool, detail: str)" in app_src)
    check("tui: the module is imported by the app",
          "from .. import moonmath as mm_mod" in app_src)
    check("tui: /models completion offers it", '"moonmath"]' in comp_src,
          "the /models source list must offer moonmath")
    check("tui: the setup panel names the alias and where keys come from",
          "/zro-moonmath-api-setup" in app_src)
    check("tui: a fallback message can suggest it",
          "/zim-moonmath" in app_src)

    # A hosted gateway restores through the shared loop.
    check("main: the restore loop includes it",
          '("moonmath", mm_mod)' in main_src)
    check("main: the module is imported", "from . import moonmath as mm_mod" in main_src)


def test_launcher_restores_the_route() -> None:
    """A remembered Zro selection must survive a restart.

    Without a hosted branch the launcher takes the Logfare path: it starts
    LiteLLM for a session that will never use it, and on a machine with no
    Logfare profile dies fail-closed with "profile not found" — a session that
    cannot start, for a reason that is not true.
    """
    launcher = (ROOT / "packaging" / "zimzilla").read_text(encoding="utf-8")

    check("launcher: the hosted branch includes it",
          "tokenharbour|openrouter|moonmath)" in launcher)
    start = launcher.index("case \"$_current_source\" in")
    end = launcher.index("esac", start)
    branch = launcher[start:end]
    check("launcher: it sources the operator's profile",
          '"$ZIMZILLA_HOME/$_current_source/source"' in branch, branch.strip()[:200])
    check("launcher: it turns the proxy off", "ZIMZILLA_NO_PROXY=1" in branch,
          branch.strip()[:200])
    check("launcher: it has no shipped profile to fall back to",
          "packaging/moonmath" not in launcher)


def test_nothing_ships_a_credential() -> None:
    pkg = ROOT / "packaging"
    leaked = [str(p.relative_to(ROOT)) for p in pkg.rglob("*")
              if p.is_file() and "moonmath" in _safe_text(p).lower()
              and re.search(r"sk-[A-Za-z0-9]{20,}", _safe_text(p))]
    check("packaging: no Zro profile or key ships in the tree", not leaked,
          str(leaked))
    check("packaging: no moonmath profile ships",
          not (pkg / "moonmath" / "source").exists())

    mm_src = (ROOT / "zimzilla" / "moonmath.py").read_text(encoding="utf-8")
    pasted = re.findall(r"sk-[A-Za-z0-9]{20,}", mm_src)
    check("module: no literal sk- key in the source", not pasted,
          f"a key is pasted into zimzilla/moonmath.py: {len(pasted)} match(es)")


def _safe_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-mm-test-"))
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
        test_free_is_the_plan_not_a_zero_price()
        test_aliases_resolve_everywhere()
        test_catalog_is_public_and_unauthenticated()
        test_fetch_is_total_and_cached()
        test_verify_key_uses_the_keyed_models_route()
        test_verify_key_does_not_fail_on_a_billing_state()
        test_explain_error_shapes()
        test_looks_like_key()
        test_is_moonmath_matches_by_host()
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
