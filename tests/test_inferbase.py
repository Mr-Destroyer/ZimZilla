"""Regression suite for the Inferbase source.

Run:  ~/.local/share/zimzilla/.venv/bin/python tests/test_inferbase.py

Protects what is particular about this provider:

* **The Models route is public, so the probe is NOT the key check.** Unlike
  InferX's key-gated catalog, ``GET /v1/models`` answers 200 with no credential
  *and* with a wrong one. A switch that probed it would call a rejected key
  healthy and land the session on a route that 401s on the first turn, so
  ``verify_key`` makes one tiny real chat call instead.
* **The catalog is two routes, and neither is complete.** The key-gated
  ``/api/v1/inference/catalog`` carries the type, window, output ceiling and
  both prices; the public ``/v1/models`` carries the served ids, including 37
  ``:fp8``/``:turbo``/``:fp4`` variants and ``auto`` that the catalog does not
  list. Reading only one of them loses either the facts or the ids — and the
  catalog is **paginated**, so a single read truncates it to half.
* **Free is a price band, not a flag.** Models at or below $2.00 per 1M output
  tokens are included; anything above answers 402. ``Model.free`` is that test,
  and a model with no published price is NOT free — a missing price is not a
  zero price.
* **A 402 is not a rejected key, and a 429 is a window.** Measured: the same key
  answered 200 for ``zai-glm-4-7-flash`` and 402 for ``qwen3-8-max`` in the same
  minute, and the free plan's 10-requests-per-minute cap answers 429 with a
  ``retry-after``. ``explain_error`` has to keep all three apart.
* **The key never ships.** ``~/.zimzilla/inferbase/source`` is written by
  ``/inferbase-api-setup``, and nothing under ``packaging/`` contains a
  credential.
* **A remembered selection survives a restart.** Without the launcher branch a
  session last used on Inferbase falls through to the Logfare profile, so the
  launcher would source the wrong endpoint and the session would come up on the
  wrong provider with no indication.

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


#: Shaped like the live rich catalog: ``{"items": [...], "total_pages": 1}``
#: with the fields the endpoint actually sends. The prices are the measured
#: ones, because the free-band test is a comparison against a real number.
CATALOG = {
    "items": [
        {"inference_model_id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash",
         "model_type": "chat", "context_window": 1048576,
         "max_output_tokens": 384000,
         "input_cost_per_1m": "0.090000", "output_cost_per_1m": "0.180000"},
        {"inference_model_id": "zai-glm-4-7-flash", "name": "GLM 4.7 Flash",
         "model_type": "chat", "context_window": 202752,
         "max_output_tokens": 64000,
         "input_cost_per_1m": "0.070000", "output_cost_per_1m": "0.400000"},
        # Exactly on the band's edge: included, because the rule is "up to".
        {"inference_model_id": "bytedance-seed-1-8", "name": "Seed 1.8",
         "model_type": "chat", "context_window": 256000,
         "max_output_tokens": 64000,
         "input_cost_per_1m": "0.250000", "output_cost_per_1m": "2.000000"},
        # One cent over: excluded, and answers 402.
        {"inference_model_id": "deepseek-r1-0528", "name": "DeepSeek R1 0528",
         "model_type": "chat", "context_window": 163840,
         "max_output_tokens": 65536,
         "input_cost_per_1m": "0.500000", "output_cost_per_1m": "2.150000"},
        # A non-chat family, which the picker must not offer.
        {"inference_model_id": "qwen3-embedding-8b", "name": "Qwen 3 Embedding 8B",
         "model_type": "embedding", "context_window": 32768,
         "max_output_tokens": None,
         "input_cost_per_1m": "0.010000", "output_cost_per_1m": "0.000000"},
        # An entry with no price at all: NOT free, and not priceable.
        {"inference_model_id": "mystery-model", "name": "Mystery",
         "model_type": "chat", "context_window": 65536,
         "max_output_tokens": None,
         "input_cost_per_1m": None, "output_cost_per_1m": None},
        # Rows the parser must drop rather than crash on.
        "not-a-row",
        {"no_id": True},
        {"inference_model_id": "   "},
    ],
    "total": 6,
    "page": 1,
    "page_size": 100,
    "total_pages": 1,
}

#: Shaped like the live public Models route: ids and ``supported_parameters``
#: only — no window, no price. ``auto`` and the ``:fp8`` variant are here and
#: absent from the catalog, which is the whole reason both routes are read.
MODELS = {
    "object": "list",
    "data": [
        {"id": "auto", "object": "model", "owned_by": "inferbase",
         "name": "Inferbase Auto - Smart Routing",
         "supported_parameters": ["max_tokens", "tools", "tool_choice"]},
        {"id": "deepseek-v4-flash", "object": "model", "owned_by": "inferbase",
         "name": "DeepSeek V4 Flash",
         "supported_parameters": ["max_tokens", "tools"]},
        {"id": "deepseek-v4-flash:fp8", "object": "model",
         "owned_by": "inferbase", "name": "DeepSeek V4 Flash (fp8)",
         "supported_parameters": ["max_tokens", "tools"]},
        {"id": "zai-glm-4-7-flash", "object": "model", "owned_by": "inferbase",
         "name": "GLM 4.7 Flash", "supported_parameters": ["max_tokens"]},
        {"id": "bytedance-seed-1-8", "object": "model", "owned_by": "inferbase",
         "name": "Seed 1.8", "supported_parameters": ["max_tokens", "tools"]},
        {"id": "deepseek-r1-0528", "object": "model", "owned_by": "inferbase",
         "name": "DeepSeek R1 0528", "supported_parameters": ["max_tokens"]},
        {"id": "qwen3-embedding-8b", "object": "model", "owned_by": "inferbase",
         "name": "Qwen 3 Embedding 8B", "supported_parameters": ["max_tokens"]},
        {"id": "mystery-model", "object": "model", "owned_by": "inferbase",
         "name": "Mystery", "supported_parameters": ["max_tokens"]},
        # Served but absent from the catalog: kept, with no facts.
        {"id": "brand-new-model", "object": "model", "owned_by": "inferbase",
         "name": "Brand New", "supported_parameters": ["max_tokens", "tools"]},
        # Rows the parser must drop.
        "not-a-row",
        {"no_id": True},
        {"id": "   "},
    ],
}

#: Every chat-capable id the two routes together describe, in served order.
CHAT = ["auto", "deepseek-v4-flash", "deepseek-v4-flash:fp8",
        "zai-glm-4-7-flash", "bytedance-seed-1-8", "deepseek-r1-0528",
        "mystery-model", "brand-new-model"]

#: The chat ids the free plan's band covers, cheapest output first.
FREE = ["auto", "deepseek-v4-flash", "deepseek-v4-flash:fp8",
        "zai-glm-4-7-flash", "bytedance-seed-1-8"]


class _Resp(io.BytesIO):
    """A urlopen() result: a context manager that also reports a status."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _router(catalog=None, models=None, catalog_error=None, models_error=None):
    """A urlopen stub that answers each route with its own payload.

    The two routes are different URLs with different shapes, so a single
    payload stub would let a test pass while the module read the wrong one.
    """
    def _open(req, timeout=None):
        url = req.full_url
        if "/catalog" in url:
            if catalog_error is not None:
                raise catalog_error
            return _Resp(json.dumps(catalog if catalog is not None
                                    else CATALOG).encode())
        if models_error is not None:
            raise models_error
        return _Resp(json.dumps(models if models is not None
                                else MODELS).encode())
    return _open


def _capture(catalog=None, models=None):
    """A stub that records every request, so URLs and headers can be asserted."""
    seen: list[dict] = []

    def _open(req, timeout=None):
        seen.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": {k.lower(): v for k, v in req.header_items()},
        })
        if "/catalog" in req.full_url:
            return _Resp(json.dumps(catalog if catalog is not None
                                    else CATALOG).encode())
        return _Resp(json.dumps(models if models is not None
                                else MODELS).encode())
    return seen, _open


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "u", code, "err", {}, io.BytesIO(body.encode()))


def _clean_env():
    """Context manager that hides any Inferbase credential in the environment."""
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
    from zimzilla import inferbase as ib

    rows = ib._parse_catalog(CATALOG)
    ids = [m.id for m in rows]
    check("parse: every well-formed catalog row kept", len(rows) == 6,
          str(len(rows)))
    check("parse: a bare string is dropped", "not-a-row" not in ids)
    check("parse: a whitespace id is dropped", "   " not in ids)
    check("parse: the well-formed ids survive in catalog order",
          ids == ["deepseek-v4-flash", "zai-glm-4-7-flash",
                  "bytedance-seed-1-8", "deepseek-r1-0528",
                  "qwen3-embedding-8b", "mystery-model"], str(ids))
    check("parse: a payload with no items is an empty catalog",
          ib._parse_catalog({"object": "list"}) == [])
    check("parse: a junk items field is an empty catalog, not a crash",
          ib._parse_catalog({"items": "junk"}) == [])

    served = ib._parse_models(MODELS)
    check("parse: the Models route keeps every well-formed row",
          len(served) == 9, str(len(served)))
    check("parse: a variant records its base id",
          {m.id: m.variant_of for m in served}["deepseek-v4-flash:fp8"]
          == "deepseek-v4-flash")
    check("parse: a non-variant has no base id",
          {m.id: m.variant_of for m in served}["deepseek-v4-flash"] == "")
    check("parse: tool support is read from supported_parameters",
          {m.id: m.tools for m in served}["deepseek-v4-flash"] is True
          and {m.id: m.tools for m in served}["zai-glm-4-7-flash"] is False)


def test_the_two_routes_are_merged() -> None:
    """Neither route is complete: the catalog has the facts, the list has the ids."""
    from zimzilla import inferbase as ib

    merged = {m.id: m for m in ib._merge(ib._parse_catalog(CATALOG),
                                         ib._parse_models(MODELS))}
    check("merge: every served id is present", len(merged) == 9, str(len(merged)))
    check("merge: a catalog id keeps its window",
          merged["deepseek-v4-flash"].context == 1_048_576,
          str(merged["deepseek-v4-flash"].context))
    check("merge: a catalog id keeps its price",
          merged["deepseek-v4-flash"].price_out == 0.18,
          str(merged["deepseek-v4-flash"].price_out))
    check("merge: a catalog id keeps its type",
          merged["qwen3-embedding-8b"].model_type == "embedding")
    # The variant is served but not catalogued, so it inherits its base's facts
    # — the gateway serves it as the same model at a different precision.
    check("merge: a variant inherits its base id's window",
          merged["deepseek-v4-flash:fp8"].context == 1_048_576,
          str(merged["deepseek-v4-flash:fp8"].context))
    check("merge: a variant inherits its base id's price",
          merged["deepseek-v4-flash:fp8"].price_out == 0.18,
          str(merged["deepseek-v4-flash:fp8"].price_out))
    check("merge: a variant is still marked as one",
          merged["deepseek-v4-flash:fp8"].variant_of == "deepseek-v4-flash")
    # `auto` is served and has no catalog row at all.
    check("merge: auto survives with no catalog facts",
          "auto" in merged and merged["auto"].context is None)
    check("merge: an uncatalogued id keeps its own empty facts",
          merged["brand-new-model"].context is None
          and merged["brand-new-model"].priced is False)
    check("merge: tool support comes from the Models route",
          merged["brand-new-model"].tools is True)


def test_free_is_the_price_band() -> None:
    """The plan's rule is a price, not a per-model flag."""
    from zimzilla import inferbase as ib

    merged = {m.id: m for m in ib._merge(ib._parse_catalog(CATALOG),
                                         ib._parse_models(MODELS))}
    check("free: a cheap model is inside the band",
          merged["deepseek-v4-flash"].free)
    check("free: a model exactly ON the band is included",
          merged["bytedance-seed-1-8"].price_out == 2.0
          and merged["bytedance-seed-1-8"].free)
    check("free: a model one cent over the band is excluded",
          not merged["deepseek-r1-0528"].free)
    check("free: auto is free by construction",
          merged["auto"].free)
    check("free: an unpriced model is NOT free",
          not merged["mystery-model"].free)
    check("free: the band is the published $2.00",
          ib.FREE_BAND == 2.00, str(ib.FREE_BAND))

    check("price: a free-band model prices at (0, 0)",
          ib.price_for("deepseek-v4-flash", list(merged.values())) == (0.0, 0.0))
    check("price: a model above the band prices at its published rate",
          ib.price_for("deepseek-r1-0528", list(merged.values()))
          == (0.5, 2.15),
          str(ib.price_for("deepseek-r1-0528", list(merged.values()))))
    check("price: an unpriced model is None, not (0, 0)",
          ib.price_for("mystery-model", list(merged.values())) is None)
    check("price: an unknown id is None",
          ib.price_for("nope", list(merged.values())) is None)


def test_non_chat_families_are_filtered() -> None:
    """The catalog's own model_type decides, not a name list."""
    from zimzilla import inferbase as ib

    merged = {m.id: m for m in ib._merge(ib._parse_catalog(CATALOG),
                                         ib._parse_models(MODELS))}
    check("chat: a chat model is chat", merged["deepseek-v4-flash"].chat)
    check("chat: an embedding model is not chat",
          not merged["qwen3-embedding-8b"].chat)
    check("chat: auto is chat", merged["auto"].chat)
    check("chat: an uncatalogued id falls back to the family markers",
          merged["brand-new-model"].chat)
    check("chat: the marker list still catches an uncatalogued embedding",
          not ib.Model(id="some-bge-embedding").chat)
    check("chat: the three chat types are the catalog's own vocabulary",
          ib.CHAT_TYPES == frozenset({"chat", "code", "vision"}),
          str(sorted(ib.CHAT_TYPES)))

    with mock.patch("urllib.request.urlopen", _router()):
        ib.clear_cache()
        ids, err = ib.model_ids("inf_test-key-abcdefghijklmnop")
    check("chat: model_ids keeps exactly the chat set", ids == CHAT, str(ids))
    check("chat: model_ids reports no error", err == "", err)
    check("chat: an embedding model is not offered",
          ids is not None and "qwen3-embedding-8b" not in ids)


def test_context_is_never_none() -> None:
    """The catalog's window when it has one; 131K otherwise, never the 1M default."""
    from zimzilla import inferbase as ib

    merged = {m.id: m for m in ib._merge(ib._parse_catalog(CATALOG),
                                         ib._parse_models(MODELS))}
    check("context: a catalogued model reports its own window",
          merged["deepseek-v4-flash"].context_length == 1_048_576,
          str(merged["deepseek-v4-flash"].context_length))
    check("context: an uncatalogued id gets DEFAULT_CONTEXT",
          merged["brand-new-model"].context_length == ib.DEFAULT_CONTEXT,
          str(merged["brand-new-model"].context_length))
    check("context: DEFAULT_CONTEXT is 131K, not the harness's 1M",
          ib.DEFAULT_CONTEXT == 131_072, str(ib.DEFAULT_CONTEXT))
    check("context: it is never None",
          all(isinstance(m.context_length, int) for m in merged.values()))
    check("context: context_known distinguishes a figure from a guess",
          merged["deepseek-v4-flash"].context_known
          and not merged["brand-new-model"].context_known)
    check("blurb: it names the plan and the window",
          "free plan" in merged["deepseek-v4-flash"].blurb
          and "1048k ctx" in merged["deepseek-v4-flash"].blurb,
          merged["deepseek-v4-flash"].blurb)
    check("blurb: a paid model shows its rate",
          "$0.5/$2.15" in merged["deepseek-r1-0528"].blurb,
          merged["deepseek-r1-0528"].blurb)
    check("blurb: an unpriced model says so rather than reading free",
          "unpriced" in merged["mystery-model"].blurb,
          merged["mystery-model"].blurb)


def test_both_routes_are_read_and_the_urls_are_right() -> None:
    seen, opener = _capture()
    from zimzilla import inferbase as ib

    with mock.patch("urllib.request.urlopen", opener):
        ib.clear_cache()
        models, err = ib.fetch_models("inf_test-key-abcdefghijklmnop")

    urls = [s["url"] for s in seen]
    check("catalog: a keyed fetch succeeds", models is not None and err == "",
          err)
    check("catalog: the priced catalog is read",
          any("/api/v1/inference/catalog" in u for u in urls), str(urls))
    check("catalog: the public model list is read too",
          any(u.endswith("/v1/models") for u in urls), str(urls))
    check("catalog: the catalog is paginated explicitly",
          any("page=1" in u and "page_size=100" in u for u in urls), str(urls))
    check("catalog: both are GETs",
          all(s["method"] == "GET" for s in seen), str(seen))
    check("catalog: the key is sent as a bearer token",
          all(s["headers"].get("authorization")
              == "Bearer inf_test-key-abcdefghijklmnop" for s in seen),
          str([s["headers"] for s in seen]))
    check("catalog: a User-Agent is sent",
          all(s["headers"].get("user-agent") == "zimzilla" for s in seen))
    check("catalog: the merged list is the served ids",
          [m.id for m in models] == [m.id for m in ib._parse_models(MODELS)],
          str([m.id for m in models]))
    check("catalog: and it is marked as the rich one", ib.catalog_is_rich())


def test_pagination_is_followed() -> None:
    """A single read truncates the catalog to one page — the bug this prevents."""
    from zimzilla import inferbase as ib

    page1 = {"items": [{"inference_model_id": "one", "model_type": "chat",
                        "context_window": 1000, "input_cost_per_1m": "0",
                        "output_cost_per_1m": "0"}],
             "total_pages": 2, "page": 1}
    page2 = {"items": [{"inference_model_id": "two", "model_type": "chat",
                        "context_window": 2000, "input_cost_per_1m": "0",
                        "output_cost_per_1m": "0"}],
             "total_pages": 2, "page": 2}

    def _open(req, timeout=None):
        if "/catalog" in req.full_url:
            return _Resp(json.dumps(page2 if "page=2" in req.full_url
                                    else page1).encode())
        return _Resp(json.dumps({"data": [
            {"id": "one"}, {"id": "two"}]}).encode())

    with mock.patch("urllib.request.urlopen", _open):
        ib.clear_cache()
        models, err = ib.fetch_models("inf_test-key-abcdefghijklmnop")
    check("pagination: both pages are read",
          models is not None and [m.id for m in models] == ["one", "two"],
          str([m.id for m in (models or [])]))
    check("pagination: the second page's facts are present",
          models is not None and models[1].context == 2000,
          str(models[1].context if models else None))


def test_public_list_answers_without_a_key() -> None:
    """The Models route is public, so the picker is never empty before setup."""
    from zimzilla import inferbase as ib

    with mock.patch("urllib.request.urlopen", _router()):
        ib.clear_cache()
        models, err = ib.fetch_models("")
    check("keyless: the public list still answers",
          models is not None and err == "", err)
    check("keyless: every chat id is offered",
          models is not None and [m.id for m in models if m.chat] == CHAT,
          str([m.id for m in (models or []) if m.chat]))
    check("keyless: the list is NOT marked as the rich catalog",
          not ib.catalog_is_rich())
    check("keyless: a window falls back to DEFAULT_CONTEXT",
          models is not None and models[0].context_length == ib.DEFAULT_CONTEXT)
    check("keyless: nothing is priced, so nothing reads free",
          models is not None and not any(m.free for m in models
                                         if m.id != "auto"))


def test_fetch_is_total() -> None:
    """A network failure is returned, never raised."""
    from zimzilla import inferbase as ib

    with mock.patch("urllib.request.urlopen",
                    _router(catalog_error=urllib.error.URLError("down"),
                            models_error=urllib.error.URLError("down"))):
        ib.clear_cache()
        models, err = ib.fetch_models("inf_test-key-abcdefghijklmnop")
    check("fetch: both routes down returns None and an error",
          models is None and "api.inferbase.ai" in err, repr(err))

    # The catalog refusing is survivable: the public list still answers.
    with mock.patch("urllib.request.urlopen",
                    _router(catalog_error=_http_error(401, "no"))):
        ib.clear_cache()
        models, err = ib.fetch_models("inf_test-key-abcdefghijklmnop")
    check("fetch: a refused catalog falls back to the public list",
          models is not None and err == "", repr(err))
    check("fetch: and the fallback is not called rich",
          not ib.catalog_is_rich())

    with mock.patch("urllib.request.urlopen",
                    _router(models_error=_http_error(500, "boom"))):
        ib.clear_cache()
        models, err = ib.fetch_models("inf_test-key-abcdefghijklmnop")
    check("fetch: a 500 on the list still yields the catalog",
          models is not None and len(models) == 6, str(len(models or [])))

    with mock.patch("urllib.request.urlopen",
                    _router(catalog={"items": "junk"}, models={"data": "junk"})):
        ib.clear_cache()
        models, err = ib.fetch_models("inf_test-key-abcdefghijklmnop")
    check("fetch: a malformed payload is an empty catalog, not a crash",
          models == [] and err == "", repr(models))


def test_cache_is_keyed_and_expires() -> None:
    from zimzilla import inferbase as ib

    with mock.patch("urllib.request.urlopen", _router()):
        ib.clear_cache()
        check("cache: cold is None", ib.cached_models() is None)
        check("cache: cold has no age", ib.catalog_age() is None)
        ib.fetch_models("inf_test-key-abcdefghijklmnop")
        check("cache: warm has models", ib.cached_models() is not None)
        check("cache: cached_ids filters non-chat entries",
              "qwen3-embedding-8b" not in (ib.cached_ids() or []))
        check("cache: cached_ids keeps chat entries", ib.cached_ids() == CHAT,
              str(ib.cached_ids()))

        ib.fetch_models("inf_other-key-abcdefghijklmnop")
        check("cache: a keyed fetch is its own entry", len(ib._CACHE) == 2,
              str(len(ib._CACHE)))

        for k, (models, when, rich) in list(ib._CACHE.items()):
            ib._CACHE[k] = (models, when - ib.CACHE_TTL - 1, rich)
        check("cache: an expired entry reads as None", ib.cached_models() is None)
        check("cache: but the age is still reported",
              (ib.catalog_age() or 0) > ib.CACHE_TTL, str(ib.catalog_age()))
        ib.clear_cache()


def test_explain_error_shapes() -> None:
    """A 402, a 429 window and a rejected key call for different actions."""
    from zimzilla import inferbase as ib

    check("explain: 401 blames the key",
          "/inferbase-api-setup" in ib.explain_error(401, ""))
    check("explain: 403 blames the key too",
          "/inferbase-api-setup" in ib.explain_error(403, ""))

    band = ib.explain_error(402, json.dumps({
        "detail": "This model is outside the free plan's price band ($2.00 per "
                  "1M output tokens at the provider)."}))
    check("explain: a 402 with the band wording says the key is fine",
          "key is fine" in band, band)
    check("explain: and it points at the command that lists the band",
          "/inferbase-models" in band, band)
    check("explain: a plain 402 is a credit state, not a band",
          "no credit" in ib.explain_error(402, "insufficient_credits"))

    window = ib.explain_error(429, json.dumps({
        "detail": "Free plan limit: 10 requests per minute on Inferbase's own "
                  "models for this organization. Retry in 14s."}))
    check("explain: a 429 naming the per-minute cap is a window",
          "per-minute" in window and "key is fine" in window, window)
    check("explain: a plain 429 is a rate limit, not a window",
          "rate limited" in ib.explain_error(429, "slow down"))

    notchat = ib.explain_error(404, json.dumps({
        "detail": "'qwen3-embedding-8b' is an embedding model; it cannot "
                  "answer chat prompts."}))
    check("explain: a 404 for a non-chat model says so",
          "not a chat model" in notchat, notchat)
    check("explain: a plain 404 names the model, not the key",
          "not served" in ib.explain_error(404, "")
          and "key" not in ib.explain_error(404, ""))

    # A 422's `detail` is a LIST of validation objects, not a string. Reading it
    # as one produced a repr of the list at the operator.
    validation = ib.explain_error(422, json.dumps({
        "detail": [{"type": "extra_forbidden", "loc": ["body", "bogus_field"],
                    "msg": "Extra inputs are not permitted"}],
        "error": {"message": "bogus_field: Extra inputs are not permitted"}}))
    check("explain: a 422 carries the gateway's own sentence",
          "bogus_field" in validation and "[" not in validation, validation)
    check("explain: a JSON detail is carried",
          "boom" in ib.explain_error(500, '{"detail": "boom"}'))
    check("explain: an empty body still answers",
          ib.explain_error(500, "") == "HTTP 500", ib.explain_error(500, ""))


def test_verify_key_is_a_real_call() -> None:
    """The Models route is public, so only a chat call proves a key."""
    from zimzilla import inferbase as ib

    seen: list[dict] = []

    def _ok(req, timeout=None):
        seen.append({"url": req.full_url, "method": req.get_method(),
                     "headers": {k.lower(): v for k, v in req.header_items()},
                     "body": req.data})
        return _Resp(b'{"choices": []}')

    with mock.patch("urllib.request.urlopen", _ok):
        ok, detail = ib.verify_key("inf_test-key-abcdefghijklmnop")
    check("verify: a working key is accepted", ok and detail == "key accepted",
          detail)
    check("verify: it POSTs to chat/completions, not the Models route",
          seen and seen[0]["url"].endswith("/v1/chat/completions"),
          str(seen[0]["url"] if seen else None))
    check("verify: it is a POST", seen and seen[0]["method"] == "POST")
    check("verify: the key is sent as a bearer token",
          seen and seen[0]["headers"].get("authorization")
          == "Bearer inf_test-key-abcdefghijklmnop")
    body = json.loads(seen[0]["body"]) if seen else {}
    check("verify: the probe is tiny", body.get("max_tokens") == 8, str(body))
    check("verify: it defaults to auto", body.get("model") == "auto", str(body))

    with mock.patch("urllib.request.urlopen",
                    lambda req, timeout=None: (_ for _ in ()).throw(
                        _http_error(401, '{"detail": "Could not validate credentials"}'))):
        ok, detail = ib.verify_key("inf_bogus")
    check("verify: a rejected key fails", not ok and "401" in detail, detail)
    check("verify: and the message names the setup command",
          "/inferbase-api-setup" in detail, detail)

    # A 402 is a model choice, not a credential problem: the key is fine.
    with mock.patch("urllib.request.urlopen",
                    lambda req, timeout=None: (_ for _ in ()).throw(
                        _http_error(402, '{"detail": "outside the free plan\'s price band"}'))):
        ok, detail = ib.verify_key("inf_test-key-abcdefghijklmnop")
    check("verify: a 402 still counts as a working key", ok, detail)
    check("verify: and it says why", "price band" in detail, detail)

    check("verify: no key is a failure", ib.verify_key("")[0] is False)


def test_looks_like_key() -> None:
    from zimzilla import inferbase as ib

    check("key: an inf_ token passes", ib.looks_like_key("inf_" + "a" * 60))
    check("key: the prefix is NOT required", ib.looks_like_key("a" * 40))
    check("key: a short string fails", not ib.looks_like_key("short"))
    check("key: whitespace fails", not ib.looks_like_key("two words here now"))
    check("key: a whole export line fails",
          not ib.looks_like_key('export ANTHROPIC_AUTH_TOKEN="abc"'))
    check("key: a model id fails", not ib.looks_like_key("deepseek-v4-flash"))


def test_is_inferbase_matches_by_host() -> None:
    """No port to match on, so this is a host test."""
    from zimzilla import inferbase as ib
    from zimzilla import sources as s

    check("endpoint: the /v1 root matches",
          ib.is_inferbase("https://api.inferbase.ai/v1"))
    check("endpoint: the /api/v1/inference root matches too",
          ib.is_inferbase("https://api.inferbase.ai/api/v1/inference"))
    check("endpoint: a bare trailing slash matches",
          ib.is_inferbase("https://api.inferbase.ai/v1/"))
    check("endpoint: another provider does not match",
          not ib.is_inferbase("https://model.inferx.net/endpoints/v1"))
    check("endpoint: a local proxy does not match",
          not ib.is_inferbase("http://localhost:4001"))
    check("endpoint: empty is not a match", not ib.is_inferbase(""))
    check("endpoint: None is not a match", not ib.is_inferbase(None))
    check("endpoint: the generic alias is the same test",
          ib.active_endpoint is ib.is_inferbase)

    check("endpoint: sources identifies it by host",
          s.active_key("https://api.inferbase.ai/v1") == "inferbase",
          str(s.active_key("https://api.inferbase.ai/v1")))
    check("endpoint: sources does not confuse it with InferX",
          s.active_key("https://model.inferx.net/endpoints/v1") == "inferx")
    check("endpoint: the protocol heuristic calls it OpenAI-compatible",
          __import__("zimzilla.openai_compat", fromlist=["x"]).resolve_protocol(
              "https://api.inferbase.ai/v1") == "openai")


def test_profile_roundtrip(tmp: Path) -> None:
    from zimzilla import inferbase as ib

    with _clean_env():
        path = ib.save_key("inf_test-key-1234567890", "deepseek-v4-flash")
        check("profile: written under ZIMZILLA_HOME",
              path == tmp / ".zimzilla" / "inferbase" / "source", str(path))
        check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
              oct(path.stat().st_mode & 0o777))
        check("profile: parent is 700",
              oct(path.parent.stat().st_mode & 0o777) == "0o700",
              oct(path.parent.stat().st_mode & 0o777))
        text = path.read_text()
        check("profile: it points at the gateway, with no proxy",
              'ANTHROPIC_BASE_URL="https://api.inferbase.ai/v1"' in text,
              text.splitlines()[2] if len(text.splitlines()) > 2 else "")
        check("profile: the model is preserved",
              'ANTHROPIC_MODEL="deepseek-v4-flash"' in text)
        check("profile: ANTHROPIC_API_KEY is blanked so the token wins",
              'export ANTHROPIC_API_KEY=""' in text)

        creds = ib.load_credentials()
        check("profile: the key loads back",
              creds is not None and creds[0] == "inf_test-key-1234567890")
        check("profile: the model loads back",
              creds is not None and creds[1] == "deepseek-v4-flash")
        check("profile: the origin is the path",
              creds is not None and creds[2] == str(ib.PROFILE))
        check("profile: has_key agrees", ib.has_key())

        ib.save_key("inf_test-key-1234567890")
        check("profile: no model argument falls back to DEFAULT_MODEL",
              'ANTHROPIC_MODEL="%s"' % ib.DEFAULT_MODEL in path.read_text())
        check("profile: the default is auto", ib.DEFAULT_MODEL == "auto")

        path.unlink()


def test_credential_precedence(tmp: Path) -> None:
    from zimzilla import inferbase as ib

    with _clean_env():
        check("creds: nothing set is None", ib.load_credentials() is None)
        ib.save_key("inf_from_profile_0001")
        check("creds: the own profile is used",
              ib.load_credentials()[2] == str(ib.PROFILE),
              str(ib.load_credentials()[2]))

        os.environ["ANTHROPIC_BASE_URL"] = "https://api.inferbase.ai/v1"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "inf_from_env_0002"
        check("creds: the environment wins when aimed at Inferbase",
              ib.load_credentials()[0] == "inf_from_env_0002")
        check("creds: and it is named as the environment",
              ib.load_credentials()[2] == "environment")

        os.environ["ANTHROPIC_BASE_URL"] = "https://api.example.com"
        check("creds: a token for another endpoint is ignored",
              ib.load_credentials()[0] == "inf_from_profile_0001")
        os.environ.pop("ANTHROPIC_BASE_URL")
        os.environ.pop("ANTHROPIC_AUTH_TOKEN")

        ib.PROFILE.unlink()


def test_sources_registry_dispatch() -> None:
    from zimzilla import inferbase as ib
    from zimzilla import sources as s

    check("registry: it is direct-only", "inferbase" in s.DIRECT_ONLY,
          str(sorted(s.DIRECT_ONLY)))
    check("registry: it declares the gateway route",
          s.DECLARED_ROUTES["inferbase"].base_url
          == "https://api.inferbase.ai/v1",
          s.DECLARED_ROUTES["inferbase"].base_url)
    check("registry: the id goes on the wire unchanged",
          s.DECLARED_ROUTES["inferbase"].upstream_model("deepseek-v4-flash")
          == "deepseek-v4-flash")
    check("registry: the declared fallback is the measured free band",
          len(s.INFERBASE_MODELS) == 49, f"{len(s.INFERBASE_MODELS)} ids")
    check("registry: the fallback is chat models only",
          "qwen3-embedding-8b" not in s.INFERBASE_MODELS
          and "bfl-flux-1-dev" not in s.INFERBASE_MODELS)
    check("registry: auto is not in the fallback (it is the default, not a row)",
          "auto" not in s.INFERBASE_MODELS)
    check("registry: it is a built-in key, so a selection survives a restart",
          "inferbase" in s.BUILTIN_KEYS)

    # Placement is load-bearing: a branch written *after* the generic
    # DIRECT_ONLY block is dead code, because that block returns first.
    import inspect
    body = inspect.getsource(s.models_for)
    check("registry: the Inferbase branch precedes the generic direct-only one",
          'if key == "inferbase"' in body
          and body.index('if key == "inferbase"')
          < body.index("if key in DIRECT_ONLY"))

    with mock.patch("urllib.request.urlopen", _router()):
        ib.clear_cache()
        cold = s.models_for(ib.API_BASE_URL)
        check("registry: a cold cache falls back to the declared list",
              cold is not None and tuple(cold) == tuple(s.INFERBASE_MODELS),
              str(len(cold or ())))
        ib.fetch_models("inf_test-key-abcdefghijklmnop")
        warm = s.models_for(ib.API_BASE_URL)
        check("registry: a warm cache gives the chat ids",
              warm is not None and tuple(warm) == tuple(CHAT), str(warm))
        check("registry: the non-chat entries are not offered",
              warm is not None and "qwen3-embedding-8b" not in warm)
        check("registry: catalog_models returns Model objects",
              len(s.catalog_models("inferbase") or []) == 9)
        check("registry: an unknown key is still None",
              s.catalog_models("nope") is None)

        # The window and the price both come from the catalog, which is the
        # whole reason this source reads the rich route.
        check("registry: the window comes from the catalog",
              s.context_window_for(ib.API_BASE_URL, "deepseek-v4-flash")
              == 1_048_576,
              str(s.context_window_for(ib.API_BASE_URL, "deepseek-v4-flash")))
        check("registry: the output ceiling comes from the catalog",
              s.max_output_for(ib.API_BASE_URL, "deepseek-v4-flash") == 384_000,
              str(s.max_output_for(ib.API_BASE_URL, "deepseek-v4-flash")))
        check("registry: a free-band model prices at (0, 0)",
              s.catalog_price_for(ib.API_BASE_URL, "deepseek-v4-flash")
              == (0.0, 0.0))
        check("registry: a model above the band prices at its rate",
              s.catalog_price_for(ib.API_BASE_URL, "deepseek-r1-0528")
              == (0.5, 2.15),
              str(s.catalog_price_for(ib.API_BASE_URL, "deepseek-r1-0528")))
        ib.clear_cache()
        check("registry: a COLD cache still calls auto free",
              s.catalog_price_for(ib.API_BASE_URL, "auto") == (0.0, 0.0),
              str(s.catalog_price_for(ib.API_BASE_URL, "auto")))


def test_discovery_needs_a_profile() -> None:
    """Without the operator's key there is no route — that is what makes the
    switch name /inferbase-api-setup instead of failing at request time."""
    from zimzilla import sources as s

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "zimzilla"
        home.mkdir()
        zhome.mkdir()

        with mock.patch.object(s, "DEFAULT_HOME", home), \
             mock.patch.object(s, "ZIMZILLA_HOME", zhome):
            check("discovery: no profile means no source",
                  s.discover().get("inferbase") is None)
            check("discovery: and it is not offered as a switch target",
                  "inferbase" not in s.switchable())
            (zhome / "inferbase").mkdir()
            (zhome / "inferbase" / "source").write_text(
                'export ANTHROPIC_AUTH_TOKEN="inf_x"\n')
            src = s.discover().get("inferbase")
            check("discovery: with a profile it appears", src is not None)
            if src is not None:
                check("discovery: it owns no port", src.port == 0, str(src.port))
                check("discovery: its base URL is the gateway",
                      src.base_url == "https://api.inferbase.ai/v1",
                      src.base_url)
                check("discovery: it is direct-only", src.is_direct_only)
                check("discovery: it is not parked",
                      src.under_construction is None)
                check("discovery: its default model is auto",
                      src.model == "auto", src.model)
                check("discovery: its fallback is the measured free band",
                      "deepseek-v4-flash" in src.models, str(src.models[:2]))
            check("discovery: and now it IS offered as a switch target",
                  "inferbase" in s.switchable())


def test_tui_surfaces() -> None:
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    agent_src = (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8")

    for cmd in ("zim-inferbase", "inferbase-api-setup", "inferbase-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app_src)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp_src)
        check(f"tui: /{cmd} is documented in /help", f"/{cmd}" in app_src)
        check(f"tui: /{cmd} is in the command palette", f'"{cmd}"' in app_src)

    check("tui: the gateway table knows it",
          'return ib_mod, "Inferbase"' in app_src)
    check("tui: the status pane lists it",
          '("inferbase", ib_mod, "Inferbase", "/inferbase-api-setup")' in app_src)
    check("tui: the status pane prints direct, not a port",
          '_kind in ("nvidia", "inferx", "inferbase")' in app_src)
    check("tui: the model picker reads its catalog",
          'if active == "inferbase" else None' in app_src)
    check("tui: /model refetches for it", 'elif active == "inferbase":' in app_src)
    check("tui: /model prices it from the catalog",
          "ib_mod.price_for(name, ib_catalog)" in app_src)
    check("tui: /model names the plan when the catalog is cold",
          'return "free plan (auto routes within the band)"' in app_src)
    check("tui: /models accepts it", '"inferbase"' in app_src)
    check("tui: /models dispatches to its free-band list",
          'return ib_mod.usable_free_models(creds[0] if creds else "")' in app_src)
    check("tui: /models labels the listing", '"inferbase": "INFERBASE"' in app_src)
    check("tui: the startup warm-up runs for it",
          "self._warm_inferbase_catalog()" in app_src)
    check("tui: the switch proves the key with a real call",
          'ok, detail = ib_mod.verify_key(token or "", chosen)' in app_src)
    check("tui: the module is imported by the app",
          "from .. import inferbase as ib_mod" in app_src)
    check("tui: /models completion offers it",
          '"inferx", "inferbase"' in comp_src)
    check("tui: a 401 hint names the Inferbase command",
          "/inferbase-api-setup <inf_ key>" in app_src)
    check("tui: the inside-session switch refuses with the setup command",
          "/inferbase-api-setup <inf_ key> (from" in app_src)
    check("tui: a new key is applied to the running session",
          'if kind == "inferbase" and ib_mod.is_inferbase(self.cfg.base_url):'
          in app_src)
    check("agent: the environment note lists the profile",
          '("inferbase", "Inferbase", "ANTHROPIC_AUTH_TOKEN")' in agent_src)
    check("sources: the host is a known Chat Completions host",
          '"api.inferbase.ai"' in
          (ROOT / "zimzilla" / "openai_compat.py").read_text(encoding="utf-8"))

    # Inferbase is direct-only, so the launch-time restore loop — which applies
    # hosted gateways — must not list it: the module has no ``apply_to``.
    main_src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    check("main: the restore loop is not used for it",
          '("inferbase", ib_mod)' not in main_src)


def test_launcher_restores_the_route() -> None:
    """A remembered Inferbase selection must survive a restart.

    Without its own branch the launcher takes the Logfare path: it sources the
    Logfare profile, so a session last used on Inferbase would come up on
    Logfare with no indication — the same failure the NVIDIA branch documents.
    """
    launcher = (ROOT / "packaging" / "zimzilla").read_text(encoding="utf-8")

    check("launcher: it has its own branch",
          'if [[ "$_current_source" == "inferbase" ]]' in launcher)
    check("launcher: it sources the operator's profile",
          '_ib_profile="$ZIMZILLA_HOME/inferbase/source"' in launcher)
    check("launcher: it turns the proxy off",
          launcher.index('_ib_profile="$ZIMZILLA_HOME/inferbase/source"')
          < launcher.index("ZIMZILLA_NO_PROXY=1",
                           launcher.index('_ib_profile="$ZIMZILLA_HOME/inferbase/source"')),
          "the branch must set ZIMZILLA_NO_PROXY=1")
    check("launcher: it has no shipped profile to fall back to",
          "packaging/inferbase/source" not in launcher)
    start = launcher.index('if [[ "$_current_source" == "inferbase" ]]')
    end = launcher.index("LITELLM_ENV_FILE=", start)
    branch = launcher[start:end]
    check("launcher: the branch is guarded on the profile existing",
          'if [[ -r "$_ib_profile" ]]' in branch, branch.strip()[:200])


def test_nothing_ships_a_credential() -> None:
    pkg = ROOT / "packaging"
    leaked = [str(p.relative_to(ROOT)) for p in pkg.rglob("*")
              if p.is_file() and re.search(r"inf_[A-Za-z0-9]{20,}", _safe_text(p))]
    check("packaging: no Inferbase key ships in the tree", not leaked, str(leaked))
    check("packaging: no inferbase profile ships",
          not (pkg / "inferbase" / "source").exists())

    mod_src = (ROOT / "zimzilla" / "inferbase.py").read_text(encoding="utf-8")
    pasted = re.findall(r"inf_[A-Za-z0-9]{20,}", mod_src)
    check("module: no literal inf_ key in the source", not pasted,
          f"a key is pasted into zimzilla/inferbase.py: {len(pasted)} match(es)")


def _safe_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-ib-test-"))
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
        test_the_two_routes_are_merged()
        test_free_is_the_price_band()
        test_non_chat_families_are_filtered()
        test_context_is_never_none()
        test_both_routes_are_read_and_the_urls_are_right()
        test_pagination_is_followed()
        test_public_list_answers_without_a_key()
        test_fetch_is_total()
        test_cache_is_keyed_and_expires()
        test_explain_error_shapes()
        test_verify_key_is_a_real_call()
        test_looks_like_key()
        test_is_inferbase_matches_by_host()
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
