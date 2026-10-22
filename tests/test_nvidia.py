"""Regression suite for the NVIDIA NIM source.

Run:  python tests/test_nvidia.py   (from an activated venv)

Protects what is particular about this provider:

* **The Models route is public, so it cannot certify a key.** ``/v1/models``
  answers 200 with no credential *and* with a wrong one. A probe against it
  would call a rejected key healthy, let ``/zim-nvidia`` succeed, and leave the
  operator to meet the 401 on their first turn. ``verify_key`` therefore makes
  one real, tiny Chat Completions call — that is the only thing that proves
  authentication.
* **A model problem is not a key problem.** A 404 ``Not found for account``, a
  410 end-of-life and a 503 ``ResourceExhausted`` all say something about the
  *model*, never about the credential, so ``verify_key`` returns ok=True on
  them. Failing the switch there would refuse a working key over a model
  choice. Only 401/403 (and an unreachable endpoint) fail it.
* **Free is the developer tier, not a per-model price.** The catalog publishes
  no price at all, so ``Model.free`` is True by construction — and
  ``sources.catalog_price_for`` must answer (0, 0) *even on a cold cache*.
  Returning None there would fall through to the estimate table and bill a free
  model at ``DEFAULT_PRICE``, which is the exact discrepancy the function
  exists to prevent.
* **Non-chat families are filtered by marker, not by an allowlist.** The
  catalog carries embeddings, rerankers, safety guards, parsers and image
  generators; a chat request against any of them is a 400 at best.
* **``context_length`` is never None.** The catalog publishes no window, and
  the harness default is 1M — guessing high against a 128K model is the
  unrecoverable direction. Unknown ids get 128K, not the harness fallback.
* **The key never ships.** ``~/.zimzilla/nvidia/source`` is written by
  ``/nvidia-api-setup`` and nothing under ``packaging/`` contains a credential.

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


#: Shaped like the live catalog: ``{"object": "list", "data": [...]}``, and
#: nothing else — no price, no context window, no capability flags. That
#: sparseness is the point; a fixture richer than the real payload would let a
#: test pass on data the endpoint never sends.
CATALOG = {
    "object": "list",
    "data": [
        {"id": "nvidia/nemotron-3.5-lightning-30b-a3b", "object": "model",
         "created": 1760000000, "owned_by": "nvidia"},
        {"id": "openai/gpt-oss-20b", "object": "model",
         "created": 1750000000, "owned_by": "openai"},
        {"id": "stepfun-ai/step-5-preview", "object": "model",
         "created": 1751000000, "owned_by": "stepfun-ai"},
        {"id": "poolside/laguna-xs-2.1", "object": "model",
         "created": 1752000000, "owned_by": "poolside"},
        {"id": "meta/muse-glimmer-30b", "object": "model",
         "created": 1753000000, "owned_by": "meta"},
        # --- non-chat families, one per marker shape ---------------------
        {"id": "nvidia/nv-embedqa-e5-v5", "object": "model",
         "owned_by": "nvidia"},
        {"id": "baai/bge-m3", "object": "model", "owned_by": "baai"},
        {"id": "nvidia/llama-3.2-nv-rerankqa-1b-v2", "object": "model",
         "owned_by": "nvidia"},
        {"id": "meta/llama-guard-4-12b", "object": "model",
         "owned_by": "meta"},
        {"id": "nvidia/nemoretriever-parse", "object": "model",
         "owned_by": "nvidia"},
        {"id": "nvidia/cosmos-predict-1.0", "object": "model",
         "owned_by": "nvidia"},
        {"id": "nvidia/gliner-pii", "object": "model", "owned_by": "nvidia"},
        {"id": "google/neva-22b", "object": "model", "owned_by": "google"},
        # --- rows that must be dropped by the parser, not the filter -----
        "not-a-row",
        {"no_id": True},
        {"id": "   "},
    ],
}

#: Every chat-capable id, catalog order preserved.
CHAT = ["nvidia/nemotron-3.5-lightning-30b-a3b", "openai/gpt-oss-20b",
        "stepfun-ai/step-5-preview", "poolside/laguna-xs-2.1",
        "meta/muse-glimmer-30b"]

#: The non-chat ids, each of which a marker-only filter must catch.
NON_CHAT = ["nvidia/nv-embedqa-e5-v5", "baai/bge-m3",
            "nvidia/llama-3.2-nv-rerankqa-1b-v2", "meta/llama-guard-4-12b",
            "nvidia/nemoretriever-parse", "nvidia/cosmos-predict-1.0",
            "nvidia/gliner-pii", "google/neva-22b"]


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
    """Context manager that hides any NVIDIA credential in the environment."""
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
    from zimzilla import nvidia as nv

    models = nv._parse_models(CATALOG)
    ids = [m.id for m in models]
    check("parse: every well-formed row kept", len(models) == 13, str(len(models)))
    check("parse: a bare string is dropped", "not-a-row" not in ids)
    check("parse: a row with no id is dropped", len(ids) == len(set(ids)))
    check("parse: a whitespace id is dropped", "   " not in ids)
    check("parse: owned_by is carried", models[0].owned_by == "nvidia",
          models[0].owned_by)
    check("parse: created is carried",
          models[0].created == 1760000000, str(models[0].created))


def test_non_chat_families_are_filtered() -> None:
    """Marker-based, so a new embedding model needs no code change."""
    from zimzilla import nvidia as nv

    by_id = {m.id: m for m in nv._parse_models(CATALOG)}
    check("chat: a text LLM is chat",
          by_id["nvidia/nemotron-3.5-lightning-30b-a3b"].chat)
    check("chat: a gpt-oss model is chat", by_id["openai/gpt-oss-20b"].chat)
    for mid in NON_CHAT:
        check(f"chat: {mid} is not chat", not by_id[mid].chat)

    with mock.patch("urllib.request.urlopen", _stub()):
        nv.clear_cache()
        ids, err = nv.model_ids()
    check("chat: model_ids keeps exactly the chat set",
          ids is not None and ids == CHAT, str(ids))
    check("chat: model_ids reports no error", err == "", err)
    check("chat: an embedding model is not offered",
          ids is not None and "nvidia/nv-embedqa-e5-v5" not in ids)
    check("chat: a safety guard is not offered",
          ids is not None and "meta/llama-guard-4-12b" not in ids)


def test_free_is_the_developer_tier() -> None:
    """The catalog publishes no price, so the tier is the only true answer."""
    from zimzilla import nvidia as nv
    from zimzilla import sources as s

    models = nv._parse_models(CATALOG)
    check("free: every entry is free on the developer tier",
          all(m.free for m in models))
    check("price: price_for says (0, 0), not None",
          nv.price_for("openai/gpt-oss-20b", models) == (0.0, 0.0),
          str(nv.price_for("openai/gpt-oss-20b", models)))
    check("price: an unknown id is still None",
          nv.price_for("nope", models) is None)

    with mock.patch("urllib.request.urlopen", _stub()):
        nv.clear_cache()
        nv.fetch_models()
        warm = s.catalog_price_for(nv.API_BASE_URL, "openai/gpt-oss-20b")
        # Load-bearing: the estimate table would bill this at $1.00/$3.00.
        cold_after_clear = None
        nv.clear_cache()
        cold_after_clear = s.catalog_price_for(nv.API_BASE_URL, "openai/gpt-oss-20b")
    check("price: the registry reports a free model as free", warm == (0.0, 0.0),
          str(warm))
    check("price: and it does so on a COLD cache too", cold_after_clear == (0.0, 0.0),
          str(cold_after_clear))
    check("price: the estimate table would have said otherwise",
          __import__("zimzilla.config", fromlist=["x"]).price_for(
              "openai/gpt-oss-20b") != (0.0, 0.0))


def test_context_is_never_none() -> None:
    """Unknown ids get 128K, not the harness's 1M fallback."""
    from zimzilla import nvidia as nv

    by_id = {m.id: m for m in nv._parse_models(CATALOG)}
    check("context: a measured id reports its page figure",
          by_id["nvidia/nemotron-3.5-lightning-30b-a3b"].context_length
          == 1_048_576,
          str(by_id["nvidia/nemotron-3.5-lightning-30b-a3b"].context_length))
    check("context: gpt-oss-20b reports 128K",
          by_id["openai/gpt-oss-20b"].context_length == 131_072,
          str(by_id["openai/gpt-oss-20b"].context_length))
    check("context: an unmeasured id gets DEFAULT_CONTEXT",
          by_id["meta/muse-glimmer-30b"].context_length == nv.DEFAULT_CONTEXT,
          str(by_id["meta/muse-glimmer-30b"].context_length))
    check("context: DEFAULT_CONTEXT is 128K, not 1M",
          nv.DEFAULT_CONTEXT == 131_072, str(nv.DEFAULT_CONTEXT))
    check("context: it is never None",
          all(isinstance(m.context_length, int) for m in by_id.values()))
    check("blurb: it names the tier and the window",
          "dev tier" in by_id["openai/gpt-oss-20b"].blurb
          and "ctx" in by_id["openai/gpt-oss-20b"].blurb,
          by_id["openai/gpt-oss-20b"].blurb)
    check("blurb: the window it prints is the one it plans against",
          f"{by_id['openai/gpt-oss-20b'].context_length // 1000}k ctx"
          in by_id["openai/gpt-oss-20b"].blurb,
          by_id["openai/gpt-oss-20b"].blurb)


def test_catalog_is_public_and_the_url_is_right() -> None:
    seen, opener = _capture()
    from zimzilla import nvidia as nv

    with mock.patch("urllib.request.urlopen", opener):
        nv.clear_cache()
        models, err = nv.fetch_models()

    check("catalog: it fetches without a key",
          models is not None and err == "", err)
    check("catalog: the URL is the OpenAI-compatible Models endpoint",
          seen.get("url") == "https://integrate.api.nvidia.com/v1/models",
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
        nv.clear_cache()
        nv.fetch_models("nvapi-test-key-abcdefghijkl")
    check("catalog: a supplied key is sent as a bearer token",
          seen2.get("headers", {}).get("authorization")
          == "Bearer nvapi-test-key-abcdefghijkl", str(seen2.get("headers")))


def test_fetch_is_total() -> None:
    """A network failure is returned, never raised."""
    from zimzilla import nvidia as nv

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        nv.clear_cache()
        models, err = nv.fetch_models()
    check("fetch: a connection failure returns None and an error",
          models is None and "integrate.api.nvidia.com" in err, repr(err))

    with mock.patch("urllib.request.urlopen", _stub(error=_http_error(401))):
        nv.clear_cache()
        models, err = nv.fetch_models()
    check("fetch: a 401 names the setup command",
          models is None and "/nvidia-api-setup" in err, repr(err))

    with mock.patch("urllib.request.urlopen",
                    _stub(error=_http_error(503, "busy"))):
        nv.clear_cache()
        models, err = nv.fetch_models()
    check("fetch: a 503 is reported with its status",
          models is None and "503" in err, repr(err))

    with mock.patch("urllib.request.urlopen", _stub(payload={"data": "junk"})):
        nv.clear_cache()
        models, err = nv.fetch_models()
    check("fetch: a malformed payload is an empty catalog, not a crash",
          models == [] and err == "", repr(models))


def test_cache_is_keyed_and_expires() -> None:
    from zimzilla import nvidia as nv

    with mock.patch("urllib.request.urlopen", _stub()):
        nv.clear_cache()
        check("cache: cold is None", nv.cached_models() is None)
        check("cache: cold has no age", nv.catalog_age() is None)
        nv.fetch_models()
        check("cache: warm has models", nv.cached_models() is not None)
        check("cache: cached_ids filters non-chat entries",
              "baai/bge-m3" not in (nv.cached_ids() or []))
        check("cache: cached_ids keeps chat entries",
              nv.cached_ids() == CHAT, str(nv.cached_ids()))

        # Keyed by the token the fetch used: the catalog is public and identical
        # for every caller, so the keyless fetch is its own entry.
        nv.fetch_models("other-token")
        check("cache: a keyed fetch is its own entry", len(nv._CACHE) == 2,
              str(len(nv._CACHE)))

        for k, (models, when) in list(nv._CACHE.items()):
            nv._CACHE[k] = (models, when - nv.CACHE_TTL - 1)
        check("cache: an expired entry reads as None", nv.cached_models() is None)
        check("cache: but the age is still reported",
              (nv.catalog_age() or 0) > nv.CACHE_TTL, str(nv.catalog_age()))
        nv.clear_cache()


def test_verify_key_makes_a_chat_call() -> None:
    """The Models route is public; only a chat call proves a credential."""
    from zimzilla import nvidia as nv

    seen, opener = _capture(payload={"choices": [{"message": {"content": "ok"}}]})
    with mock.patch("urllib.request.urlopen", opener):
        ok, detail = nv.verify_key("nvapi-test-key-abcdefghijkl")
    check("verify: a working key is accepted", ok, detail)
    check("verify: it POSTs to /chat/completions, not GET /models",
          seen.get("method") == "POST"
          and seen.get("url") == "https://integrate.api.nvidia.com/v1/chat/completions",
          f"{seen.get('method')} {seen.get('url')}")
    check("verify: it sends the bearer token",
          seen.get("headers", {}).get("authorization")
          == "Bearer nvapi-test-key-abcdefghijkl")
    body = json.loads((seen.get("body") or b"{}").decode())
    check("verify: the request is the smallest that proves auth",
          body.get("max_tokens") == 8 and body.get("messages"),
          str(body.get("max_tokens")))
    check("verify: it names a model", bool(body.get("model")), str(body.get("model")))

    with mock.patch("urllib.request.urlopen", _stub(error=_http_error(401, ""))):
        ok, detail = nv.verify_key("nvapi-bad-key-abcdefghijkl")
    check("verify: a 401 fails the key", not ok, detail)
    check("verify: and the message names the setup command",
          "/nvidia-api-setup" in detail, detail)

    with mock.patch("urllib.request.urlopen", _stub(error=_http_error(403, ""))):
        ok, detail = nv.verify_key("nvapi-bad-key-abcdefghijkl")
    check("verify: a 403 fails the key too", not ok, detail)

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        ok, detail = nv.verify_key("nvapi-test-key-abcdefghijkl")
    check("verify: an unreachable endpoint fails it", not ok, detail)

    check("verify: an empty key is refused without a request",
          nv.verify_key("") == (False, "no key"))


def test_verify_key_does_not_fail_on_a_model_problem() -> None:
    """404/410/503 say something about the model, never about the credential."""
    from zimzilla import nvidia as nv

    cases = {
        404: '{"detail": "Function \'abc\': Not found for account \'xyz\'"}',
        410: '{"detail": "The model has reached its end of life on 2026-08-26"}',
        503: '{"error": {"message": "ResourceExhausted: Worker local total '
             'request limit reached"}}',
        429: '{"detail": "rate limited"}',
    }
    for status, body in cases.items():
        with mock.patch("urllib.request.urlopen",
                        _stub(error=_http_error(status, body))):
            ok, detail = nv.verify_key("nvapi-test-key-abcdefghijkl")
        check(f"verify: a {status} still accepts the key", ok, detail)
        check(f"verify: the {status} reason is explained", bool(detail), detail)


def test_explain_error_shapes() -> None:
    from zimzilla import nvidia as nv

    check("explain: 401 blames the key",
          "/nvidia-api-setup" in nv.explain_error(401, ""))
    check("explain: 404 names the account, not the key",
          "account" in nv.explain_error(
              404, '{"detail":"Function \'a\': Not found for account \'b\'"}')
          and "key is fine" in nv.explain_error(
              404, '{"detail":"Function \'a\': Not found for account \'b\'"}'))
    check("explain: 410 says retired",
          "retired" in nv.explain_error(410, ""))
    check("explain: 429 names the tier limit",
          "40" in nv.explain_error(429, ""))
    check("explain: 503 says capacity",
          "capacity" in nv.explain_error(503, ""))
    check("explain: an unknown status keeps its detail",
          "boom" in nv.explain_error(500, '{"detail":"boom"}'))
    check("explain: an empty body still answers",
          nv.explain_error(500, "") == "HTTP 500", nv.explain_error(500, ""))


def test_looks_like_key() -> None:
    from zimzilla import nvidia as nv

    check("key: an nvapi- token passes",
          nv.looks_like_key("nvapi-" + "a" * 50))
    check("key: the prefix is NOT required",
          nv.looks_like_key("a" * 40))
    check("key: a short string fails", not nv.looks_like_key("short"))
    check("key: whitespace fails", not nv.looks_like_key("two words here now"))
    check("key: a whole export line fails",
          not nv.looks_like_key('export ANTHROPIC_AUTH_TOKEN="abc"'))
    check("key: a provider/model id fails", not nv.looks_like_key("openai/gpt-oss-20b"))


def test_is_nvidia_matches_by_host() -> None:
    """No port to match on, so this is a host test."""
    from zimzilla import nvidia as nv
    from zimzilla import sources as s

    check("endpoint: the documented host matches",
          nv.is_nvidia("https://integrate.api.nvidia.com/v1"))
    check("endpoint: a bare trailing slash matches",
          nv.is_nvidia("https://integrate.api.nvidia.com/v1/"))
    check("endpoint: the short host matches",
          nv.is_nvidia("https://api.nvidia.com/v1"))
    check("endpoint: another provider does not match",
          not nv.is_nvidia("https://api.openai.com/v1"))
    check("endpoint: a local proxy does not match",
          not nv.is_nvidia("http://localhost:4001"))
    check("endpoint: empty is not a match", not nv.is_nvidia(""))
    check("endpoint: None is not a match", not nv.is_nvidia(None))
    check("endpoint: the generic alias is the same test",
          nv.active_endpoint is nv.is_nvidia)

    check("endpoint: sources identifies it by host",
          s.active_key("https://integrate.api.nvidia.com/v1") == "nvidia",
          str(s.active_key("https://integrate.api.nvidia.com/v1")))
    check("endpoint: sources does not identify another provider",
          s.active_key("https://api.openai.com/v1") is None)


def test_profile_roundtrip(tmp: Path) -> None:
    from zimzilla import nvidia as nv

    with _clean_env():
        path = nv.save_key("nvapi-test-key-1234567890", "openai/gpt-oss-20b")
        check("profile: written under ZIMZILLA_HOME",
              path == tmp / ".zimzilla" / "nvidia" / "source", str(path))
        check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
              oct(path.stat().st_mode & 0o777))
        check("profile: parent is 700",
              oct(path.parent.stat().st_mode & 0o777) == "0o700",
              oct(path.parent.stat().st_mode & 0o777))
        text = path.read_text()
        check("profile: it points at the upstream itself, with no proxy",
              'ANTHROPIC_BASE_URL="https://integrate.api.nvidia.com/v1"' in text,
              text.splitlines()[2] if len(text.splitlines()) > 2 else "")
        check("profile: the model is preserved",
              'ANTHROPIC_MODEL="openai/gpt-oss-20b"' in text)

        creds = nv.load_credentials()
        check("profile: the key loads back",
              creds is not None and creds[0] == "nvapi-test-key-1234567890")
        check("profile: the model loads back",
              creds is not None and creds[1] == "openai/gpt-oss-20b")
        check("profile: the origin is the path",
              creds is not None and creds[2] == str(nv.PROFILE))
        check("profile: has_key agrees", nv.has_key())

        # A second save with no model must not blank the stored one.
        nv.save_key("nvapi-test-key-1234567890")
        check("profile: no model argument falls back to DEFAULT_MODEL",
              'ANTHROPIC_MODEL="%s"' % nv.DEFAULT_MODEL in path.read_text())

        path.unlink()


def test_credential_precedence(tmp: Path) -> None:
    from zimzilla import nvidia as nv

    with _clean_env():
        check("creds: nothing set is None", nv.load_credentials() is None)
        nv.save_key("nvapi_from_profile_0001")
        check("creds: the own profile is used",
              nv.load_credentials()[2] == str(nv.PROFILE),
              str(nv.load_credentials()[2]))

        os.environ["ANTHROPIC_BASE_URL"] = "https://integrate.api.nvidia.com/v1"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "nvapi_from_env_0002"
        check("creds: the environment wins when aimed at NVIDIA",
              nv.load_credentials()[0] == "nvapi_from_env_0002")
        check("creds: and it is named as the environment",
              nv.load_credentials()[2] == "environment")

        # A token in the environment aimed elsewhere is not this source's.
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.example.com"
        check("creds: a token for another endpoint is ignored",
              nv.load_credentials()[0] == "nvapi_from_profile_0001")
        os.environ.pop("ANTHROPIC_BASE_URL")
        os.environ.pop("ANTHROPIC_AUTH_TOKEN")

        nv.PROFILE.unlink()


def test_sources_registry_dispatch() -> None:
    from zimzilla import nvidia as nv
    from zimzilla import sources as s

    check("registry: it is direct-only",
          "nvidia" in s.DIRECT_ONLY, str(sorted(s.DIRECT_ONLY)))
    check("registry: it declares the upstream route",
          s.DECLARED_ROUTES["nvidia"].base_url
          == "https://integrate.api.nvidia.com/v1",
          s.DECLARED_ROUTES["nvidia"].base_url)
    check("registry: the id goes on the wire unchanged",
          s.DECLARED_ROUTES["nvidia"].upstream_model("openai/gpt-oss-20b")
          == "openai/gpt-oss-20b")
    check("registry: the curated fallback is offered",
          nv.DEFAULT_MODEL in s.NVIDIA_MODELS, str(s.NVIDIA_MODELS))

    with mock.patch("urllib.request.urlopen", _stub()):
        nv.clear_cache()
        cold = s.models_for(nv.API_BASE_URL)
        check("registry: a cold cache falls back to the curated list",
              cold is not None and nv.DEFAULT_MODEL in cold, str(cold))
        nv.fetch_models()
        warm = s.models_for(nv.API_BASE_URL)
        check("registry: a warm cache gives the chat ids",
              warm is not None and tuple(warm) == tuple(CHAT), str(warm))
        check("registry: the non-chat entries are not offered",
              warm is not None and "baai/bge-m3" not in warm)
        check("registry: catalog_models returns Model objects",
              len(s.catalog_models("nvidia") or []) == 13)
        check("registry: an unknown key is still None",
              s.catalog_models("nope") is None)


def test_discovery_needs_a_profile() -> None:
    """Without the operator's key there is no route — that is what makes the
    switch name /nvidia-api-setup instead of failing at request time."""
    from zimzilla import sources as s

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "zimzilla"
        home.mkdir()
        zhome.mkdir()

        with mock.patch.object(s, "DEFAULT_HOME", home), \
             mock.patch.object(s, "ZIMZILLA_HOME", zhome):
            check("discovery: no profile means no source",
                  s.discover().get("nvidia") is None)
            (zhome / "nvidia").mkdir()
            (zhome / "nvidia" / "source").write_text(
                'export ANTHROPIC_AUTH_TOKEN="x"\n')
            src = s.discover().get("nvidia")
            check("discovery: with a profile it appears", src is not None)
            if src is not None:
                check("discovery: it owns no port", src.port == 0, str(src.port))
                check("discovery: its base URL is the upstream",
                      src.base_url == "https://integrate.api.nvidia.com/v1",
                      src.base_url)
                check("discovery: its fallback names the measured default",
                      nv_default_in(src.models), str(src.models))
                check("discovery: it is direct-only", src.is_direct_only)
                check("discovery: it is not parked",
                      src.under_construction is None)


def nv_default_in(models) -> bool:
    from zimzilla import nvidia as nv
    return nv.DEFAULT_MODEL in models


def test_tui_surfaces() -> None:
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main_src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")

    for cmd in ("zim-nvidia", "nvidia-api-setup", "nvidia-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app_src)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp_src)

    check("tui: the gateway table knows it",
          'return nv_mod, "NVIDIA NIM"' in app_src)
    check("tui: the status pane lists it",
          '("nvidia", nv_mod, "NVIDIA NIM"' in app_src)
    check("tui: the status pane prints direct, not a port",
          "'direct'" in app_src)
    check("tui: the model picker knows it",
          'if active == "nvidia" else None' in app_src)
    check("tui: /model refetches for it", 'elif active == "nvidia":' in app_src)
    check("tui: /models accepts it", '"nvidia"' in app_src)
    check("tui: /models dispatches to its chat list",
          'return nv_mod.chat_models(creds[0] if creds else "")' in app_src)
    check("tui: /models labels the listing", '"nvidia": "NVIDIA NIM"' in app_src)
    check("tui: the startup warm-up runs for it",
          "self._warm_nvidia_catalog()" in app_src)
    check("tui: the module is imported by the app",
          "from .. import nvidia as nv_mod" in app_src)
    check("tui: /models completion offers it",
          '"iteracompute", "infronai", "nvidia"' in comp_src)
    check("tui: a 401 hint names the NVIDIA command, not Token Juice",
          "/nvidia-api-setup" in app_src)

    # NVIDIA is direct-only, so the launch-time restore loop — which applies
    # hosted gateways — must not list it: the module has no ``apply_to``.
    check("main: the restore loop is not used for it",
          '("nvidia", nv_mod)' not in main_src)


def test_launcher_restores_the_route() -> None:
    """A remembered NVIDIA selection must survive a restart.

    Without its own branch the launcher takes the Logfare path: it starts
    LiteLLM for a session that will never use it, and on a machine with no
    Logfare profile dies fail-closed with "profile not found" — a session that
    cannot start, for a reason that is not true.
    """
    launcher = (ROOT / "packaging" / "zimzilla").read_text(encoding="utf-8")

    check("launcher: it has its own branch",
          'if [[ "$_current_source" == "nvidia" ]]' in launcher)
    check("launcher: it sources the operator's profile",
          '_nv_profile="$ZIMZILLA_HOME/nvidia/source"' in launcher)
    check("launcher: it turns the proxy off",
          launcher.index('_nv_profile="$ZIMZILLA_HOME/nvidia/source"')
          < launcher.index("ZIMZILLA_NO_PROXY=1",
                           launcher.index('_nv_profile="$ZIMZILLA_HOME/nvidia/source"')),
          "the branch must set ZIMZILLA_NO_PROXY=1")
    check("launcher: it has no shipped profile to fall back to",
          "packaging/nvidia/source" not in launcher)
    # Guarded on the profile being readable, so a missing key falls through to
    # Logfare rather than dying fail-closed on an unfinished route.
    start = launcher.index('if [[ "$_current_source" == "nvidia" ]]')
    end = launcher.index("LITELLM_ENV_FILE=", start)
    branch = launcher[start:end]
    check("launcher: the branch is guarded on the profile existing",
          'if [[ -r "$_nv_profile" ]]' in branch, branch.strip()[:200])


def test_nothing_ships_a_credential() -> None:
    pkg = ROOT / "packaging"
    leaked = [str(p.relative_to(ROOT)) for p in pkg.rglob("*")
              if p.is_file() and "nvapi-" in _safe_text(p)]
    check("packaging: no NVIDIA key ships in the tree", not leaked, str(leaked))
    check("packaging: no nvidia profile ships",
          not (pkg / "nvidia" / "source").exists())

    # And the module must not embed one either. The prefix appears in the
    # docstrings on purpose; what must not appear is a *key* — the prefix
    # followed by the length of token NVIDIA actually issues.
    nv_src = (ROOT / "zimzilla" / "nvidia.py").read_text(encoding="utf-8")
    pasted = re.findall(r"nvapi-[A-Za-z0-9_\-]{16,}", nv_src)
    check("module: no literal nvapi- key in the source", not pasted,
          f"a key is pasted into zimzilla/nvidia.py: {len(pasted)} match(es)")


def _safe_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-nv-test-"))
    # Pin the module's paths to the temp tree before anything imports it.
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "ANTHROPIC_MODEL")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        test_parser_drops_malformed_rows()
        test_non_chat_families_are_filtered()
        test_free_is_the_developer_tier()
        test_context_is_never_none()
        test_catalog_is_public_and_the_url_is_right()
        test_fetch_is_total()
        test_cache_is_keyed_and_expires()
        test_verify_key_makes_a_chat_call()
        test_verify_key_does_not_fail_on_a_model_problem()
        test_explain_error_shapes()
        test_looks_like_key()
        test_is_nvidia_matches_by_host()
        test_profile_roundtrip(tmp)
        test_credential_precedence(tmp)
        test_sources_registry_dispatch()
        test_discovery_needs_a_profile()
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
