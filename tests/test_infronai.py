"""Regression suite for the InfronAI source.

Run:  python tests/test_infronai.py   (from an activated venv)

Protects what is particular about this provider:

* **It is a local proxy, not a hosted gateway — even though the upstream speaks
  Messages.** Infron serves ``/v1/messages`` natively, but every ``:free`` chat
  model lists ``supported_endpoint_types: ["openai"]`` and nothing else, so a
  session aimed at ``llm.onerouter.pro`` cannot run the models this source
  exists for. ``is_infronai`` must match ``localhost:4003`` and must NOT match
  the upstream; calling the upstream a working source would hide exactly that.
* **Free is a read 0/0 price, or the ``:free`` suffix when no price was read.**
  A *missing* price is not free — 120 of the 503 live entries carry nulls — so
  ``priced`` is what keeps an unpriced model out of the free list.
* **Three catalog traps are filtered, not named.** Non-text output (the
  free-priced video generators), ``is_display_only`` entries, and LLMs that
  only list ``openai-response`` — none can answer a turn through a Chat
  Completions proxy.
* **Modality strings are not case-consistent.** The catalog carries both
  ``text`` and ``Text``; ``_strings`` lowercases, so the filter survives.
* **The free tier is not free of prerequisites.** Standard free models need a
  $5 balance and share 1000 requests/day per account, so a 402 must be
  explained rather than surfaced raw.
* **The catalog is public**, so the free list works before the key is set.

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


#: Shaped like the live catalog: ``{"data": [...], "success": true}``, prices in
#: USD per Mtok as ``min_prompt_price``/``min_completion_price``, and the
#: per-model ``supported_endpoint_types`` the proxy decision rests on.
CATALOG = {
    "data": [
        {
            "id": "deepseek/deepseek-v4.1-flash:free",
            "display_name": "DeepSeek V4.1 Flash (free)",
            "context_length": 163840, "max_output_tokens": 8192,
            "min_prompt_price": 0.0, "min_completion_price": 0.0,
            "output_modalities": ["text"],
            "supported_endpoint_types": ["openai"],
            "supports_function_calling": True, "supports_vision": False,
            "providers": [{"name": "a"}, {"name": "b"}],
        },
        {
            "id": "qwen/qwen3.8-27b:free",
            "context_length": 131072, "max_output_tokens": 4096,
            "min_prompt_price": 0.0, "min_completion_price": 0.0,
            "output_modalities": ["text"],
            "supported_endpoint_types": ["openai"],
            "supports_function_calling": True,
            "providers": [{"name": "a"}],
        },
        {
            # Paid, chat-capable: kept in model_ids, absent from the free list.
            "id": "minimax/minimax-m2.5",
            "context_length": 204800, "max_output_tokens": 16384,
            "min_prompt_price": 0.27, "min_completion_price": 1.03,
            "output_modalities": ["text"],
            "supported_endpoint_types": ["openai", "anthropic"],
            "supports_function_calling": True,
        },
        {
            # Priced 0/0 and shown in the marketplace, but not served. Without
            # the display_only check this reads as a free chat model.
            "id": "sao10k/l3.1-70b-hanami-x1",
            "context_length": 65536, "max_output_tokens": 4096,
            "min_prompt_price": 0.0, "min_completion_price": 0.0,
            "output_modalities": ["text"],
            "supported_endpoint_types": ["openai"],
            "is_display_only": True,
        },
        {
            # Responses-only. The proxy forwards Chat Completions, so this
            # cannot be served through it even though it is a text LLM.
            "id": "openai/gpt-5-codex",
            "context_length": 400000, "max_output_tokens": 128000,
            "min_prompt_price": 1.25, "min_completion_price": 10.0,
            "output_modalities": ["text"],
            "supported_endpoint_types": ["openai-response"],
        },
        {
            # Free-priced, but a video generator. 52 of these are in the live
            # catalog; the modality is the test, not the name.
            "id": "google/veo-3:free",
            "context_length": None, "max_output_tokens": None,
            "min_prompt_price": 0.0, "min_completion_price": 0.0,
            "output_modalities": ["video"],
            "supported_endpoint_types": ["openai"],
        },
        {
            # No price read and no suffix: NOT free. This is the row a
            # missing-price-reads-as-zero bug would advertise.
            "id": "mystery/thing",
            "context_length": 32000, "max_output_tokens": 2048,
            "output_modalities": ["text"],
            "supported_endpoint_types": ["openai"],
        },
        {
            # No price read, but the documented suffix: free on the suffix.
            "id": "suffix/unpriced:free",
            "context_length": 32000, "max_output_tokens": 2048,
            "output_modalities": ["text"],
            "supported_endpoint_types": ["openai"],
        },
        {
            # Capitalised modality. The live catalog carries both spellings.
            "id": "capital/Text",
            "context_length": 64000, "max_output_tokens": 2048,
            "min_prompt_price": 0.0, "min_completion_price": 0.0,
            "output_modalities": ["Text"],
            "supported_endpoint_types": ["OpenAI"],
        },
        {
            # Deprecated but still priced 0/0: free, and not offered.
            "id": "old/model",
            "context_length": 16000, "max_output_tokens": 1024,
            "min_prompt_price": 0.0, "min_completion_price": 0.0,
            "output_modalities": ["text"],
            "supported_endpoint_types": ["openai"],
            "deprecated": True,
        },
        "not-a-row",
        {"no_id": True},
        {"id": "   "},
    ],
}

#: Every chat-capable id, in catalog order.
CHAT = ["deepseek/deepseek-v4.1-flash:free", "qwen/qwen3.8-27b:free",
        "minimax/minimax-m2.5", "mystery/thing", "suffix/unpriced:free",
        "capital/Text"]

#: Free AND chat, most context first.
USABLE = ["deepseek/deepseek-v4.1-flash:free", "qwen/qwen3.8-27b:free",
          "capital/Text", "suffix/unpriced:free"]


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
    """A stub that records the request, so the URL and headers can be asserted."""
    seen: dict = {}

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())

    return seen, _open


def _clean_env():
    """Context manager that hides any InfronAI credential in the environment."""
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

def test_free_is_a_read_price_or_the_suffix() -> None:
    from zimzilla import infronai as inf

    models = inf._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("catalog: every well-formed row parsed", len(models) == 10,
          str(len(models)))
    check("catalog: a blank id is dropped", "   " not in by_id)
    check("free: a read 0/0 price is free",
          by_id["deepseek/deepseek-v4.1-flash:free"].free)
    check("free: a paid model is not free",
          not by_id["minimax/minimax-m2.5"].free)
    check("free: an unpriced model with no suffix is NOT free",
          not by_id["mystery/thing"].free)
    check("free: an unpriced model WITH the suffix is free",
          by_id["suffix/unpriced:free"].free)

    got = [m.id for m in models if m.free]
    check("free: exactly the free set, in catalog order",
          got == ["deepseek/deepseek-v4.1-flash:free",
                  "qwen/qwen3.8-27b:free",
                  "sao10k/l3.1-70b-hanami-x1",
                  "google/veo-3:free",
                  "suffix/unpriced:free",
                  "capital/Text",
                  "old/model"], str(got))


def test_priced_records_a_read() -> None:
    from zimzilla import infronai as inf

    by_id = {m.id: m for m in inf._parse_models(CATALOG)}
    check("priced: a real pair counts as priced",
          by_id["minimax/minimax-m2.5"].priced)
    check("priced: a free entry's 0/0 is still a read",
          by_id["deepseek/deepseek-v4.1-flash:free"].priced)
    check("priced: an entry with no pricing is not priced",
          not by_id["mystery/thing"].priced)
    check("priced: the price is USD per Mtok, unmodified",
          (by_id["minimax/minimax-m2.5"].price_in,
           by_id["minimax/minimax-m2.5"].price_out) == (0.27, 1.03),
          str((by_id["minimax/minimax-m2.5"].price_in,
               by_id["minimax/minimax-m2.5"].price_out)))
    check("priced: an unpriced entry defaults to 0.0, which is why the flag exists",
          by_id["mystery/thing"].price_in == 0.0)


def test_chat_filters_three_traps() -> None:
    """Modality, display-only and endpoint — each excludes something real."""
    from zimzilla import infronai as inf

    by_id = {m.id: m for m in inf._parse_models(CATALOG)}
    check("chat: a text model is chat",
          by_id["deepseek/deepseek-v4.1-flash:free"].chat)
    check("chat: a video generator is not chat",
          not by_id["google/veo-3:free"].chat)
    check("chat: a display-only entry is not chat",
          not by_id["sao10k/l3.1-70b-hanami-x1"].chat)
    check("chat: a Responses-only LLM is not chat",
          not by_id["openai/gpt-5-codex"].chat)
    check("chat: a deprecated entry is not chat",
          not by_id["old/model"].chat)
    check("chat: a missing endpoint list is permissive",
          by_id["capital/Text"].chat)

    with mock.patch("urllib.request.urlopen", _stub()):
        inf.clear_cache()
        ids, err = inf.model_ids()
    check("chat: model_ids keeps every chat model",
          ids is not None and ids == CHAT, str(ids))
    check("chat: model_ids keeps a PAID chat model",
          ids is not None and "minimax/minimax-m2.5" in ids, str(ids))
    check("chat: model_ids drops the video generator",
          ids is not None and "google/veo-3:free" not in ids)
    check("chat: model_ids drops the Responses-only entry",
          ids is not None and "openai/gpt-5-codex" not in ids)


def test_modalities_are_lowercased() -> None:
    """The catalog carries both `text` and `Text`; the filter must not care."""
    from zimzilla import infronai as inf

    check("strings: a list is lowercased",
          inf._strings(["Text", "IMAGE"]) == ("text", "image"),
          str(inf._strings(["Text", "IMAGE"])))
    check("strings: a non-list yields ()", inf._strings("text") == ())
    check("strings: non-strings are dropped", inf._strings([1, "text"]) == ("text",))

    by_id = {m.id: m for m in inf._parse_models(CATALOG)}
    check("strings: a capitalised modality still reads as chat",
          by_id["capital/Text"].modalities == ("text",),
          str(by_id["capital/Text"].modalities))
    check("strings: a capitalised endpoint still matches",
          by_id["capital/Text"].endpoints == ("openai",),
          str(by_id["capital/Text"].endpoints))


def test_usable_free_models_sorts_by_context() -> None:
    from zimzilla import infronai as inf

    with mock.patch("urllib.request.urlopen", _stub()):
        inf.clear_cache()
        models, err = inf.usable_free_models()
    check("usable: it fetches", models is not None and err == "", err)
    got = [m.id for m in (models or [])]
    check("usable: free and chat only, most context first", got == USABLE, str(got))
    check("usable: the display-only free entry is excluded",
          "sao10k/l3.1-70b-hanami-x1" not in got)
    check("usable: the free video generator is excluded",
          "google/veo-3:free" not in got)
    check("usable: the paid chat model is excluded",
          "minimax/minimax-m2.5" not in got)


def test_context_and_output_come_from_the_catalog() -> None:
    from zimzilla import infronai as inf
    from zimzilla import sources as s

    by_id = {m.id: m for m in inf._parse_models(CATALOG)}
    check("context: the catalog figure is reported unchanged",
          by_id["deepseek/deepseek-v4.1-flash:free"].context_length == 163840,
          str(by_id["deepseek/deepseek-v4.1-flash:free"].context_length))

    with mock.patch("urllib.request.urlopen", _stub()):
        inf.clear_cache()
        inf.fetch_models()
        win = s.context_window_for(inf.DEFAULT_BASE_URL,
                                   "deepseek/deepseek-v4.1-flash:free")
        ceiling = s.max_output_for(inf.DEFAULT_BASE_URL,
                                   "deepseek/deepseek-v4.1-flash:free")
        unknown = s.max_output_for(inf.DEFAULT_BASE_URL, "nope")
    check("context: the registry reports the catalog window", win == 163840,
          str(win))
    check("ceiling: the catalog's max_output_tokens is read", ceiling == 8192,
          str(ceiling))
    check("ceiling: an unknown model has none", unknown is None)
    check("ceiling: a cold catalog has none",
          s.max_output_for("https://api.example.com", "x") is None)


def test_price_for() -> None:
    from zimzilla import infronai as inf
    from zimzilla import sources as s

    models = inf._parse_models(CATALOG)
    check("price: a free model is (0, 0)",
          inf.price_for("deepseek/deepseek-v4.1-flash:free", models) == (0.0, 0.0))
    check("price: a paid model gets the catalog's rate",
          inf.price_for("minimax/minimax-m2.5", models) == (0.27, 1.03),
          str(inf.price_for("minimax/minimax-m2.5", models)))
    check("price: an unpriced model is None, so the estimate table answers",
          inf.price_for("mystery/thing", models) is None)
    check("price: an unknown id is None",
          inf.price_for("nope", models) is None)
    check("price: an empty catalog is None", inf.price_for("x", None) is None)

    with mock.patch("urllib.request.urlopen", _stub()):
        inf.clear_cache()
        inf.fetch_models()
        live = s.catalog_price_for(inf.DEFAULT_BASE_URL,
                                   "deepseek/deepseek-v4.1-flash:free")
        paid = s.catalog_price_for(inf.DEFAULT_BASE_URL, "minimax/minimax-m2.5")
        unpriced = s.catalog_price_for(inf.DEFAULT_BASE_URL, "mystery/thing")
    check("price: the registry reports a free model as free", live == (0.0, 0.0),
          str(live))
    check("price: the registry reports the paid rate", paid == (0.27, 1.03),
          str(paid))
    check("price: the registry falls back on an unpriced model",
          unpriced is None, str(unpriced))


def test_catalog_is_public_and_the_url_is_right() -> None:
    seen, opener = _capture()
    from zimzilla import infronai as inf

    with mock.patch("urllib.request.urlopen", opener):
        inf.clear_cache()
        models, err = inf.fetch_models()

    check("catalog: it fetches without a key", models is not None and err == "",
          err)
    check("catalog: the URL is the OpenAI-compatible Models endpoint",
          seen.get("url") == "https://llm.onerouter.pro/v1/models",
          str(seen.get("url")))
    check("catalog: it is a GET", seen.get("method") == "GET")
    check("catalog: no Authorization header without a key",
          "authorization" not in seen.get("headers", {}),
          str(seen.get("headers")))
    # Load-bearing: Cloudflare answers urllib's default `Python-urllib/3.x`
    # signature with 403 Error 1010. Without this header the public catalog
    # reads as a rejected key.
    check("catalog: a real User-Agent is sent",
          seen.get("headers", {}).get("user-agent") == inf.USER_AGENT,
          str(seen.get("headers", {}).get("user-agent")))

    seen2, opener2 = _capture()
    with mock.patch("urllib.request.urlopen", opener2):
        inf.clear_cache()
        inf.fetch_models("inf_test_token_abcdef")
    check("catalog: a supplied key is sent as a bearer token",
          seen2.get("headers", {}).get("authorization")
          == "Bearer inf_test_token_abcdef", str(seen2.get("headers")))


def test_fetch_is_total() -> None:
    """A network failure is returned, never raised."""
    from zimzilla import infronai as inf

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        inf.clear_cache()
        models, err = inf.fetch_models()
    check("fetch: a connection failure returns None and an error",
          models is None and "onerouter" in err, repr(err))

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.HTTPError("u", 401, "no", {}, None))):
        inf.clear_cache()
        models, err = inf.fetch_models()
    check("fetch: a 401 names the setup command",
          models is None and "/infronai-api-setup" in err, repr(err))

    # A 403 here is Cloudflare's bot rule, not a bad key — saying "check your
    # key" would send the operator to fix something that is not wrong.
    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.HTTPError("u", 403, "no", {}, None))):
        inf.clear_cache()
        models, err = inf.fetch_models()
    check("fetch: a 403 blames Cloudflare, not the key",
          models is None and "Cloudflare" in err
          and "/infronai-api-setup" not in err, repr(err))

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.HTTPError("u", 402, "pay", {}, None))):
        inf.clear_cache()
        models, err = inf.fetch_models()
    # The one thing about this free tier that is easy to get wrong: a `:free`
    # model on an unfunded account answers 402, which reads as nonsense unless
    # the message says why.
    check("fetch: a 402 explains the balance requirement",
          models is None and "balance" in err, repr(err))

    with mock.patch("urllib.request.urlopen", _stub(payload={"data": "junk"})):
        inf.clear_cache()
        models, err = inf.fetch_models()
    check("fetch: a malformed payload is an empty catalog, not a crash",
          models == [] and err == "", repr(models))


def test_cache_is_keyed_and_expires() -> None:
    from zimzilla import infronai as inf

    with mock.patch("urllib.request.urlopen", _stub()):
        inf.clear_cache()
        check("cache: cold is None", inf.cached_models() is None)
        check("cache: cold has no age", inf.catalog_age() is None)
        inf.fetch_models()
        check("cache: warm has models", inf.cached_models() is not None)
        check("cache: cached_ids filters non-chat entries",
              "google/veo-3:free" not in (inf.cached_ids() or []))

        # Keyed by the token the fetch used: a keyed fetch does not overwrite
        # the keyless one, and either can answer `cached_models`.
        inf.fetch_models("other-token")
        check("cache: a keyed fetch is its own entry", len(inf._CACHE) == 2,
              str(len(inf._CACHE)))

        # Age every entry past the TTL; it must read as None, not stale-live.
        for k, (models, when) in list(inf._CACHE.items()):
            inf._CACHE[k] = (models, when - inf.CACHE_TTL - 1)
        check("cache: an expired entry reads as None", inf.cached_models() is None)
        check("cache: but the age is still reported",
              (inf.catalog_age() or 0) > inf.CACHE_TTL, str(inf.catalog_age()))
        inf.clear_cache()


def test_default_free_model() -> None:
    from zimzilla import infronai as inf

    with mock.patch("urllib.request.urlopen", _stub()):
        inf.clear_cache()
        got = inf.default_free_model()
    check("default: it picks the preferred free model", got == USABLE[0], str(got))
    check("default: the preference is a free, tool-capable chat model",
          got in inf.PREFERRED_FREE_MODELS)

    with mock.patch("urllib.request.urlopen", _stub(payload={"data": []})):
        inf.clear_cache()
        check("default: an empty catalog yields None, not a guess",
              inf.default_free_model() is None)


def test_is_infronai_matches_only_the_proxy() -> None:
    """The upstream speaks Messages but not for the free models."""
    from zimzilla import infronai as inf

    check("endpoint: the local proxy matches",
          inf.is_infronai("http://localhost:4003"))
    check("endpoint: 127.0.0.1 matches too",
          inf.is_infronai("http://127.0.0.1:4003"))
    check("endpoint: the upstream does NOT match",
          not inf.is_infronai("https://llm.onerouter.pro"))
    check("endpoint: another local port does not match",
          not inf.is_infronai("http://localhost:4002"))
    check("endpoint: empty is not a match", not inf.is_infronai(""))
    check("endpoint: the generic alias is the same test",
          inf.active_endpoint is inf.is_infronai)

    from zimzilla import sources as s
    check("endpoint: sources identifies it by port",
          s.active_key("http://localhost:4003") == "infronai",
          str(s.active_key("http://localhost:4003")))
    check("endpoint: sources does not identify the upstream",
          s.active_key("https://llm.onerouter.pro") is None)


def test_port_does_not_collide() -> None:
    from zimzilla import infronai as inf

    check("port: 4003 is not Token Juice, Logfare or IteraCompute",
          inf.PROXY_PORT == 4003
          and inf.PROXY_PORT not in (4000, 4001, 4002), str(inf.PROXY_PORT))
    check("port: the default base URL is the local proxy",
          inf.DEFAULT_BASE_URL == "http://localhost:4003", inf.DEFAULT_BASE_URL)


def test_profile_roundtrip(tmp: Path) -> None:
    from zimzilla import infronai as inf

    with _clean_env():
        path = inf.save_key("inf_test_key_1234567890",
                            "deepseek/deepseek-v4.1-flash:free")
        check("profile: written under ZIMZILLA_HOME",
              path == tmp / ".zimzilla" / "infronai" / "source", str(path))
        check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
              oct(path.stat().st_mode & 0o777))
        check("profile: parent is 700",
              oct(path.parent.stat().st_mode & 0o777) == "0o700",
              oct(path.parent.stat().st_mode & 0o777))
        text = path.read_text()
        check("profile: points at the local proxy, not the upstream",
              'ANTHROPIC_BASE_URL="http://localhost:4003"' in text)
        check("profile: it does not contain the upstream URL",
              "onerouter" not in text)

        creds = inf.load_credentials()
        check("profile: the key loads back",
              creds is not None and creds[0] == "inf_test_key_1234567890",
              repr(creds and creds[0][:0]))
        check("profile: the model loads back",
              creds is not None
              and creds[1] == "deepseek/deepseek-v4.1-flash:free")
        check("profile: has_key agrees", inf.has_key())

        path.unlink()


def test_credential_precedence(tmp: Path) -> None:
    from zimzilla import infronai as inf

    with _clean_env():
        check("creds: nothing set is None", inf.load_credentials() is None)
        inf.save_key("inf_from_profile_0001")
        check("creds: the own profile is used",
              inf.load_credentials()[2] == str(inf.PROFILE),
              str(inf.load_credentials()[2]))

        os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:4003"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "inf_from_env_0002"
        check("creds: the environment wins when aimed at the proxy",
              inf.load_credentials()[0] == "inf_from_env_0002")
        check("creds: and it is named as the environment",
              inf.load_credentials()[2] == "environment")

        # A token in the environment aimed elsewhere is not this source's.
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.example.com"
        check("creds: a token for another endpoint is ignored",
              inf.load_credentials()[0] == "inf_from_profile_0001")
        os.environ.pop("ANTHROPIC_BASE_URL")
        os.environ.pop("ANTHROPIC_AUTH_TOKEN")

        inf.PROFILE.unlink()


def test_looks_like_key() -> None:
    from zimzilla import infronai as inf

    check("key: a plausible token passes", inf.looks_like_key("inf_abc123456789XYZ"))
    check("key: a short string fails", not inf.looks_like_key("short"))
    check("key: whitespace fails", not inf.looks_like_key("two words here now"))
    check("key: a whole export line fails",
          not inf.looks_like_key('export ANTHROPIC_AUTH_TOKEN="abc"'))
    check("key: slashes fail", not inf.looks_like_key("provider/model-name"))


def test_sources_registry_dispatch() -> None:
    from zimzilla import infronai as inf
    from zimzilla import sources as s

    with mock.patch("urllib.request.urlopen", _stub()):
        inf.clear_cache()
        check("registry: a cold cache is None",
              s.models_for(inf.DEFAULT_BASE_URL) is None)
        check("registry: and no catalog models either",
              s.catalog_models("infronai") is None)

        inf.fetch_models()
        ids = s.models_for(inf.DEFAULT_BASE_URL)
        check("registry: a warm cache gives the ids",
              ids is not None and ids == tuple(CHAT), str(ids))
        check("registry: the video generator is not offered",
              ids is not None and "google/veo-3:free" not in ids)
        check("registry: catalog_models returns the Model objects",
              len(s.catalog_models("infronai") or []) == 10)
        check("registry: an unknown key is still None",
              s.catalog_models("nope") is None)


def test_discovery_needs_a_profile() -> None:
    """The service and config ship; without the operator's key there is no route."""
    from zimzilla import sources as s

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "zimzilla"
        home.mkdir()
        (zhome / "infronai").mkdir(parents=True)
        for name in ("start-litellm.sh", "litellm-config.yaml"):
            (zhome / "infronai" / name).write_text("# test\n")

        with mock.patch.object(s, "DEFAULT_HOME", home), \
             mock.patch.object(s, "ZIMZILLA_HOME", zhome):
            check("discovery: no profile means no source",
                  s.discover().get("infronai") is None)
            (zhome / "infronai" / "source").write_text(
                'export ANTHROPIC_AUTH_TOKEN="x"\n')
            src = s.discover().get("infronai")
            check("discovery: with a profile it appears", src is not None)
            if src is not None:
                check("discovery: it owns port 4003", src.port == 4003,
                      str(src.port))
                check("discovery: its base URL is the local proxy",
                      src.base_url == "http://localhost:4003", src.base_url)
                check("discovery: its fallback names a free model",
                      "deepseek/deepseek-v4.1-flash:free" in src.models,
                      str(src.models))
                check("discovery: it is not parked",
                      src.under_construction is None)


def test_proxy_config_agrees_with_the_module() -> None:
    """The yaml and the module must not drift apart on the upstream or the id."""
    config = (ROOT / "packaging" / "infronai" / "litellm-config.yaml").read_text()
    from zimzilla import infronai as inf

    check("proxy: the config points at the same upstream",
          inf.API_BASE_URL in config, inf.API_BASE_URL)
    check("proxy: the free model is registered",
          "model_name: deepseek/deepseek-v4.1-flash:free" in config)
    check("proxy: the id is forwarded verbatim, with no added prefix",
          "model: openai/deepseek/deepseek-v4.1-flash:free" in config)
    check("proxy: a wildcard route exists so new models need no release",
          'model_name: "*"' in config)
    check("proxy: the wildcard forwards the requested name",
          "model: openai/*" in config)
    check("proxy: the master key is the profile's token",
          "master_key: os.environ/ANTHROPIC_AUTH_TOKEN" in config)
    # Load-bearing, and measured: litellm 1.105.1 sends an `openai/`
    # deployment's /v1/messages to /v1/responses unless this is set. Infron
    # serves Chat Completions for all 503 catalog entries and Responses for
    # only a handful, so the default is the wrong path for every free model.
    check("proxy: Messages are routed to Chat Completions, not Responses",
          "use_chat_completions_url_for_anthropic_messages: true" in config,
          "litellm's default would send /v1/responses")

    service = (ROOT / "packaging" / "infronai" / "start-litellm.sh").read_text()
    check("proxy: the manager defaults to :4003",
          'PORT="${LITELLM_PORT:-4003}"' in service, "")
    check("proxy: it has its own pidfile", "/tmp/litellm-infron.pid" in service)
    check("proxy: it has its own log", "/tmp/litellm-infron.log" in service)
    check("proxy: it binds loopback only",
          'HOST="${LITELLM_HOST:-127.0.0.1}"' in service)


def test_tui_surfaces() -> None:
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main_src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    launcher = (ROOT / "packaging" / "zimzilla").read_text(encoding="utf-8")
    setup = (ROOT / "setup.sh").read_text(encoding="utf-8")

    for cmd in ("zim-infronai", "infronai-api-setup", "infronai-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app_src)
        check(f"tui: /{cmd} is in the busy-ok set", f'"{cmd}"' in app_src)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp_src)

    check("tui: the gateway table knows it",
          'if kind == "infronai":' in app_src
          and 'return inf_mod, "InfronAI"' in app_src)
    check("tui: the status pane lists it",
          '("infronai", inf_mod, "InfronAI"' in app_src)
    check("tui: the model picker knows it",
          'if active == "infronai" else None' in app_src)
    check("tui: /model refetches for it", 'elif active == "infronai":' in app_src)
    # Membership, not the exact closing spelling: the set and the list this
    # lives in grow with each new gateway, and the next one must not fail this
    # suite for being appended after it.
    check("tui: /models accepts it", '"infronai"' in app_src)
    check("tui: /models dispatches to its free list",
          'return inf_mod.usable_free_models(creds[0] if creds else "")' in app_src)
    check("tui: /models labels the listing", '"infronai": "INFRONAI"' in app_src)
    check("tui: the startup warm-up runs for it",
          "self._warm_infronai_catalog()" in app_src)
    check("tui: the module is imported by the app",
          "from .. import infronai as inf_mod" in app_src)
    # Membership, not adjacency — see the same note in test_nvidia.py.
    check("tui: /models completion offers it", '"infronai"' in comp_src,
          "the /models source list must offer infronai")

    check("launcher: it starts the :4003 proxy",
          'LITELLM_PORT="${_port_override:-4003}"' in launcher)
    check("launcher: it sources the operator's profile",
          '"$ZIMZILLA_HOME/infronai/source"' in launcher)
    check("setup: it installs the route",
          'packaging/infronai/litellm-config.yaml' in setup)
    check("setup: it does not ship a credential",
          not (ROOT / "packaging" / "infronai" / "source").exists())

    # The launch-time restore loop applies hosted gateways only. InfronAI is
    # restored by the launcher sourcing its profile, so it must NOT be in that
    # tuple — the module has no ``apply_to`` and the loop would crash.
    check("main: the restore loop is left to the launcher",
          '("infronai", inf_mod)' not in main_src)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-inf-test-"))
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
        test_free_is_a_read_price_or_the_suffix()
        test_priced_records_a_read()
        test_chat_filters_three_traps()
        test_modalities_are_lowercased()
        test_usable_free_models_sorts_by_context()
        test_context_and_output_come_from_the_catalog()
        test_price_for()
        test_catalog_is_public_and_the_url_is_right()
        test_fetch_is_total()
        test_cache_is_keyed_and_expires()
        test_default_free_model()
        test_is_infronai_matches_only_the_proxy()
        test_port_does_not_collide()
        test_profile_roundtrip(tmp)
        test_credential_precedence(tmp)
        test_looks_like_key()
        test_sources_registry_dispatch()
        test_discovery_needs_a_profile()
        test_proxy_config_agrees_with_the_module()
        test_tui_surfaces()
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
