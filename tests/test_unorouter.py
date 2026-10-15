"""Regression suite for the UnoRouter source.

Run:  python tests/test_unorouter.py   (from an activated venv)

Protects what is particular about this provider:

* **``metadata`` is a JSON-encoded STRING.** Not an object. Every capability
  this integration reads — ``supportsTools``, ``outputModalities``,
  ``contextWindow``, ``maxOutputTokens``, ``isReasoning`` — lives inside that
  blob, so failing to decode it does not raise, it silently reports a catalog of
  featureless models. A dict, ``None`` and junk are all asserted to degrade to
  "unknown" rather than to a crash or a wrong answer.
* **The free/usable filter has three clauses, and the modality one is
  load-bearing.** The gateway fronts SDXL image generators, embedding models,
  Whisper transcription and TTS voices — all genuinely free, none able to answer
  a Messages turn. A catalog containing exactly those cases is parsed here, and
  only the free, text, tool-capable chat models may survive.
* **``model_ids`` keeps paid text models.** ``/model <name>`` tests membership
  against this list, so filtering it to the free tier would refuse a paid model
  the operator named deliberately — while still dropping the image and speech
  entries, which cannot answer a turn at all.
* **The composed message URL.** The catalog and the Messages API sit under
  *different roots* here (``/api/pricing`` is a sibling of ``/v1``, not a child),
  so the base URL is the bare host with no ``/v1``. Asserting the constant
  cannot catch getting this wrong; building the request through the real client
  can, and does.
* **The catalog needs no key**, because ``/api/pricing`` answers 200 with no
  credential. That is what makes the keyless listing path worth having.

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


def _meta(**kw) -> str:
    """A ``metadata`` value shaped like the real one: a JSON-encoded string."""
    return json.dumps(kw)


# Shaped like the real payload: ``data`` a list of entries keyed by
# ``model_name``, ``is_free`` a real bool, and ``metadata`` a JSON **string**
# rather than an object. The set covers every case the filter has to separate:
#   - free, text, tools        → free and usable
#   - free, image output       → free but NOT usable
#   - free, embedding output   → free but NOT usable
#   - free, audio output       → free but NOT usable
#   - free, text, no tools     → free but NOT usable
#   - paid, text, tools, :free suffix → free via the suffix
#   - paid, text, tools        → usable, not free (kept by model_ids)
CATALOG = {
    "success": True,
    "data": [
        {"model_name": "deepseek-v4.1-flash:free",
         "description": "DeepSeek V4.1 Flash (free)",
         "is_free": True, "online": True,
         "metadata": _meta(supportsTools=True, outputModalities=["text"],
                           contextWindow=262144, maxOutputTokens=65536,
                           isReasoning=False)},
        {"model_name": "gemini-3.6-flash:free",
         "description": "Gemini 3.6 Flash (free)",
         "is_free": True, "online": True,
         "metadata": _meta(supportsTools=True, outputModalities=["text"],
                           contextWindow=1048576, isReasoning=True)},
        {"model_name": "gpt-oss-20b:free",
         "description": "GPT-OSS 20B (free)",
         "is_free": True, "online": True,
         "metadata": _meta(supportsTools=True, outputModalities=["text"],
                           contextWindow=131072)},
        {"model_name": "wai-nsfw-illustrious-sdxl:free",
         "description": "SDXL image generator",
         "is_free": True, "online": True,
         "metadata": _meta(supportsTools=False, outputModalities=["image"])},
        {"model_name": "jina-embeddings-v4:free",
         "description": "Embedding model",
         "is_free": True, "online": True,
         "metadata": _meta(supportsTools=False, outputModalities=["embedding"])},
        {"model_name": "whisper-large-v3:free",
         "description": "Whisper transcription",
         "is_free": True, "online": True,
         "metadata": _meta(supportsTools=False, outputModalities=["audio"])},
        {"model_name": "mistral-small:free",
         "description": "Mistral Small (free, no tools)",
         "is_free": True, "online": True,
         "metadata": _meta(supportsTools=False, outputModalities=["text"],
                           contextWindow=131072)},
        {"model_name": "qwen3.8-27b:free",
         "description": "Qwen 3.8 27B — zeroed out without a rename",
         "is_free": False, "online": True,
         "metadata": _meta(supportsTools=True, outputModalities=["text"],
                           contextWindow=262144)},
        {"model_name": "claude-opus-5",
         "description": "Claude Opus 5 (paid)",
         "is_free": False, "online": True,
         "metadata": _meta(supportsTools=True, outputModalities=["text"],
                           contextWindow=200000, maxOutputTokens=32000)},
        # Free, text, tool-capable, and deliberately NOT in
        # PREFERRED_FREE_MODELS: without a usable model the shortlist does not
        # name, `default_free_model`'s last branch — "nothing preferred is
        # present, take the catalog's own first" — has no case to exercise, and
        # the test for it would pass by finding a preferred entry instead.
        {"model_name": "claude-haiku-5-5:free",
         "description": "Claude Haiku 5.5 (free)",
         "is_free": True, "online": True,
         "metadata": _meta(supportsTools=True, outputModalities=["text"],
                           contextWindow=1000000, isReasoning=True)},
    ],
}

#: The ids that survive `usable_free_models` — free AND text AND tool-capable.
USABLE_FREE = ["deepseek-v4.1-flash:free", "gemini-3.6-flash:free",
               "gpt-oss-20b:free", "qwen3.8-27b:free", "claude-haiku-5-5:free"]

#: The ids that survive `model_ids` — every text-output model, free or not.
TEXT_IDS = ["deepseek-v4.1-flash:free", "gemini-3.6-flash:free",
            "gpt-oss-20b:free", "mistral-small:free", "qwen3.8-27b:free",
            "claude-opus-5", "claude-haiku-5-5:free"]

PROFILE_TEXT = (
    'export ANTHROPIC_BASE_URL="https://api.unorouter.com"\n'
    'export ANTHROPIC_AUTH_TOKEN="sk-fromprofile0000000000000"\n'
    'export ANTHROPIC_MODEL="deepseek-v4.1-flash:free"\n'
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
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())
    return _open


# ---------------------------------------------------------------------------

def test_metadata_is_a_json_string() -> None:
    """Capabilities live in a JSON-encoded string, and must be decoded.

    The shape invites a silent failure rather than a loud one: reading
    ``entry["metadata"]["supportsTools"]`` against a string raises TypeError, but
    a defensive ``isinstance(..., dict)`` guard that gives up would report every
    model as featureless — no tools, no context window — and the free list would
    come back empty with nothing in the log to explain it.
    """
    from zimzilla import unorouter as u

    models = u._parse_models(CATALOG)
    by_id = {m.id: m for m in models}
    check("metadata: every entry parsed", len(models) == 10, str(len(models)))

    ds = by_id["deepseek-v4.1-flash:free"]
    check("metadata: supportsTools is read from the decoded string", ds.tools)
    check("metadata: outputModalities is read from the decoded string",
          ds.modalities == ("text",), str(ds.modalities))
    check("metadata: contextWindow is read", ds.context_length == 262144,
          str(ds.context_length))
    check("metadata: maxOutputTokens is read", ds.max_output == 65536,
          str(ds.max_output))
    check("metadata: isReasoning is read",
          by_id["gemini-3.6-flash:free"].reasoning)
    check("metadata: isReasoning false is false", not ds.reasoning)
    check("metadata: a missing maxOutputTokens is None, not an error",
          by_id["gemini-3.6-flash:free"].max_output is None)

    # Tolerated shapes: a dict (a future revision decoding it for us), None, a
    # missing key, and junk that is not JSON at all. None of them may raise, and
    # all of them must read as "capabilities unknown".
    for label, value in (
        ("a dict", {"supportsTools": True, "outputModalities": ["text"]}),
        ("None", None),
        ("a non-JSON string", "not json at all"),
        ("a JSON string that is not an object", '"just a string"'),
        ("a number", 7),
        ("an empty string", ""),
    ):
        entry = {"model_name": "x", "metadata": value}
        try:
            parsed = u._parse_models({"data": [entry]})
            ok = len(parsed) == 1
        except Exception as e:                      # noqa: BLE001
            ok, parsed = False, repr(e)
        check(f"metadata: {label} does not raise", ok, str(parsed))

    tolerant = u._parse_models({"data": [
        {"model_name": "withdict",
         "metadata": {"supportsTools": True, "outputModalities": ["text"]}},
        {"model_name": "withnone", "metadata": None},
        {"model_name": "withjunk", "metadata": "{{{"},
        {"model_name": "withnomd"},
        {"model_name": "withlist", "metadata": '["text"]'},
    ]})
    by_id = {m.id: m for m in tolerant}
    check("metadata: a dict already decoded is still read",
          by_id["withdict"].tools and by_id["withdict"].chat)
    check("metadata: None yields an unknown model, not an error",
          not by_id["withnone"].tools and by_id["withnone"].modalities == ())
    check("metadata: junk yields an unknown model",
          not by_id["withjunk"].tools and by_id["withjunk"].modalities == ())
    check("metadata: a missing key yields an unknown model",
          not by_id["withnomd"].tools and by_id["withnomd"].modalities == ())
    check("metadata: a JSON list is not mistaken for a mapping",
          not by_id["withlist"].tools and by_id["withlist"].modalities == ())

    # A malformed entry must not take the catalog down with it.
    junk = u._parse_models({"data": [
        None, 7, {}, {"model_name": ""}, {"model_name": "good"},
    ]})
    check("metadata: junk entries are skipped, not fatal",
          [m.id for m in junk] == ["good"], str([m.id for m in junk]))

    # `model_name` is the id; a missing one must not invent an entry.
    check("metadata: an entry with no name is dropped",
          u._parse_models({"data": [{"is_free": True}]}) == [])

    # A non-list, non-mapping `data` must degrade to an empty catalog.
    check("metadata: a junk `data` yields an empty catalog",
          u._parse_models({"data": "nonsense"}) == [])


def test_free_detection() -> None:
    """``is_free`` is the catalog's own flag; the ``:free`` suffix backs it up."""
    from zimzilla import unorouter as u

    models = u._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("catalog: is_free True is free",
          by_id["deepseek-v4.1-flash:free"].free)
    check("catalog: is_free False with a :free suffix is still free",
          by_id["qwen3.8-27b:free"].free)
    check("catalog: a :free suffix is free",
          by_id["mistral-small:free"].free)
    check("catalog: a paid model is not free",
          not by_id["claude-opus-5"].free)

    free = [m.id for m in models if m.free]
    check("catalog: exactly the nine free ones", sorted(free) == [
        "claude-haiku-5-5:free", "deepseek-v4.1-flash:free",
        "gemini-3.6-flash:free", "gpt-oss-20b:free", "jina-embeddings-v4:free",
        "mistral-small:free", "qwen3.8-27b:free",
        "wai-nsfw-illustrious-sdxl:free", "whisper-large-v3:free"], str(free))
    check("catalog: the paid model is the only non-free one",
          [m.id for m in models if not m.free] == ["claude-opus-5"])


def test_the_free_usable_filter() -> None:
    """Free AND tools AND text output — all three, and the third is the point.

    Without the modality clause this list fills with what the gateway also
    fronts for free: SDXL image generators, embedding models, Whisper
    transcription, TTS voices. All genuinely free, none able to answer a
    Messages turn. Measured against the live catalog on 2026-10-16: 97 free
    entries, 46 usable here — the clause and the tool test together drop 51, so
    between them they are doing most of the work. (The exact counts drift
    between runs; the filter's behaviour is what this test pins.)
    """
    from zimzilla import unorouter as u

    u.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        usable, err = u.usable_free_models(use_cache=False)
    ids = [m.id for m in (usable or [])]
    check("filter: only the free, text, tool-capable models survive",
          ids == USABLE_FREE, str(ids))
    check("filter: the free image model is excluded",
          "wai-nsfw-illustrious-sdxl:free" not in ids)
    check("filter: the free embedding model is excluded",
          "jina-embeddings-v4:free" not in ids)
    check("filter: the free audio model is excluded",
          "whisper-large-v3:free" not in ids)
    check("filter: a free text model without tools is excluded",
          "mistral-small:free" not in ids)
    check("filter: a paid model is excluded", "claude-opus-5" not in ids)
    check("filter: everything surviving is free and tool-capable",
          all(m.free and m.tools and m.chat for m in (usable or [])))
    check("filter: no error is reported on a good fetch", err == "", err)

    # The modality clause on its own, isolated from the free and tools clauses:
    # a PAID image model must still be excluded, so the exclusion is the
    # modality rather than the price.
    paid_image = u._parse_models({"data": [
        {"model_name": "paid-image", "is_free": False,
         "metadata": _meta(supportsTools=True, outputModalities=["image"])},
    ]})
    check("filter: the modality clause excludes a paid image model too",
          paid_image[0].tools and not paid_image[0].chat)

    # A model declaring several modalities is a chat model if text is one.
    multi = u._parse_models({"data": [
        {"model_name": "multimodal", "is_free": True,
         "metadata": _meta(supportsTools=True,
                           outputModalities=["text", "image"])},
    ]})
    check("filter: text among several output modalities counts as chat",
          multi[0].chat)

    # A failed fetch is None and an error, NOT an empty list — the caller can
    # tell "nothing is free" from "we could not ask".
    u.clear_cache()
    with mock.patch("urllib.request.urlopen",
                    _stub_urlopen(error=urllib.error.URLError("down"))):
        usable, err = u.usable_free_models(use_cache=False)
    check("filter: a failed fetch is None, not an empty list",
          usable is None and err != "", repr((usable, err)))


def test_model_ids_keep_paid_text_models() -> None:
    """``/model`` tests membership against this list, so it must not be free-only.

    A list filtered to the free tier would refuse a paid model the operator
    named deliberately, with "not served by unorouter" against a list that was
    never meant to be complete. The image and speech entries are still dropped —
    those cannot answer a turn, so offering them would be offering a failure.
    """
    from zimzilla import unorouter as u

    u.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        ids, err = u.model_ids(use_cache=False)
    check("ids: every text model is listed, in catalog order",
          ids == TEXT_IDS, str(ids))
    check("ids: the paid model is present despite being paid",
          "claude-opus-5" in (ids or []))
    check("ids: the image model is dropped",
          "wai-nsfw-illustrious-sdxl:free" not in (ids or []))
    check("ids: the embedding model is dropped",
          "jina-embeddings-v4:free" not in (ids or []))
    check("ids: the audio model is dropped",
          "whisper-large-v3:free" not in (ids or []))

    # The cached accessor must agree with the fetched one, since /model reads
    # the cache and /model <name> reads the fetch.
    check("ids: cached_ids agrees with the fetched list",
          u.cached_ids() == TEXT_IDS, str(u.cached_ids()))


def test_catalog_needs_no_key() -> None:
    """``/api/pricing`` is public, so the free list precedes setup."""
    from zimzilla import unorouter as u

    u.clear_cache()
    seen = {}

    def capture(req, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        seen["url"] = req.full_url
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", capture):
        models, err = u.fetch_models(use_cache=False)   # no token at all
    check("keyless: a fetch with no key succeeds",
          err == "" and models is not None and len(models) == 10, err)
    check("keyless: hits the public pricing endpoint",
          seen["url"] == "https://api.unorouter.com/api/pricing", seen.get("url"))
    check("keyless: sends NO credential",
          "authorization" not in seen["headers"]
          and "x-api-key" not in seen["headers"],
          str(sorted(seen["headers"])))
    check("keyless: the catalog is cached under the empty token",
          u.cached_ids() is not None)

    u.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        free, err = u.usable_free_models()              # again, no token
    check("keyless: the free list is available before setup",
          free is not None and [m.id for m in free] == USABLE_FREE,
          str([m.id for m in (free or [])]))


def test_keyed_fetch_sends_the_credential() -> None:
    """With a key, the listing matches what this account would be charged."""
    from zimzilla import unorouter as u

    u.clear_cache()
    seen = {}

    def capture(req, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", capture):
        u.fetch_models("sk-key000000000000000000000", use_cache=False)
    check("keyed: the bearer token is sent",
          seen["headers"].get("authorization")
          == "Bearer sk-key000000000000000000000",
          str(sorted(seen["headers"])))


def test_fetch_is_total() -> None:
    """fetch_models returns errors; it never raises into the TUI."""
    from zimzilla import unorouter as u

    u.clear_cache()
    with mock.patch("urllib.request.urlopen",
                    _stub_urlopen(error=urllib.error.URLError("no route"))):
        models, err = u.fetch_models(use_cache=False)
    check("fetch: a network failure returns None and a message",
          models is None and "could not reach" in err, err)

    u.clear_cache()
    http = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    with mock.patch("urllib.request.urlopen", _stub_urlopen(error=http)):
        models, err = u.fetch_models("sk-bad00000000000000000000",
                                     use_cache=False)
    check("fetch: a rejected key says so", models is None and "401" in err, err)
    check("fetch: the rejection names the setup command",
          "unorouter-api-setup" in err, err)

    # A 500 is not a rejected key, and must not claim to be one.
    u.clear_cache()
    boom = urllib.error.HTTPError("u", 500, "server error", {}, None)
    with mock.patch("urllib.request.urlopen", _stub_urlopen(error=boom)):
        models, err = u.fetch_models(use_cache=False)
    check("fetch: a 500 is reported as HTTP 500, not a key problem",
          models is None and "500" in err and "unorouter-api-setup" not in err,
          err)

    # An empty catalog is [] and NOT an error — the caller can tell them apart.
    u.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen({"data": []})):
        models, err = u.fetch_models(use_cache=False)
    check("fetch: an empty catalog is [], not an error",
          models == [] and err == "", repr((models, err)))

    # `data` arrives as a list today and as a mapping on some deployments; both
    # must parse, because the mapping form is what an earlier probe captured.
    u.clear_cache()
    as_dict = {"data": {m["model_name"]: m for m in CATALOG["data"]}}
    with mock.patch("urllib.request.urlopen", _stub_urlopen(as_dict)):
        models, err = u.fetch_models(use_cache=False)
    check("fetch: a mapping-shaped `data` parses too",
          models is not None and len(models) == 10, str(len(models or [])))

    # A body that is not JSON at all degrades rather than raising.
    u.clear_cache()

    def not_json(req, timeout=None):
        return _Resp(b"<html>gateway error</html>")

    with mock.patch("urllib.request.urlopen", not_json):
        models, err = u.fetch_models(use_cache=False)
    check("fetch: a non-JSON body returns an error, not a traceback",
          models is None and err != "", repr((models, err)))


def test_cache_expiry() -> None:
    """An expired catalog must not be served as if it were live.

    A free tier turns over, so a stale list is exactly the wrong list this
    picker exists to avoid. Nothing sleeps: the entry's timestamp is moved
    backwards, which is the same thing to ``monotonic()``.
    """
    import time

    from zimzilla import unorouter as u

    u.clear_cache()
    check("expiry: a cold cache has no age", u.catalog_age() is None,
          str(u.catalog_age()))

    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        u.fetch_models()
    check("expiry: a fresh catalog is served", u.cached_ids() is not None)
    check("expiry: its age is reported, and small",
          (u.catalog_age() or 999) < 5.0, str(u.catalog_age()))

    models, at = u._CACHE[""]
    u._CACHE[""] = (models, time.monotonic() - (u.CACHE_TTL + 60))
    check("expiry: an expired catalog reads as None",
          u.cached_models() is None, str(u.cached_models()))
    check("expiry: cached_ids agrees", u.cached_ids() is None)
    check("expiry: the real age is still reported, unclamped",
          (u.catalog_age() or 0) >= u.CACHE_TTL, str(u.catalog_age()))

    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        u.fetch_models()
    check("expiry: refetching restores it", u.cached_ids() is not None)
    u.clear_cache()
    check("expiry: clear_cache empties it", u.cached_models() is None
          and u.catalog_age() is None)


def test_cache_is_keyed_by_token() -> None:
    """A key change refetches rather than serving another account's list."""
    from zimzilla import unorouter as u

    u.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        u.fetch_models()                 # keyless
    check("cache: the keyless list is cached", u.cached_ids() is not None)

    calls = []

    def counting(req, timeout=None):
        calls.append(req)
        return _Resp(json.dumps({"data": []}).encode())

    with mock.patch("urllib.request.urlopen", counting):
        u.fetch_models("sk-key000000000000000000000")
    check("cache: a keyed fetch does not reuse the keyless list",
          len(calls) == 1, str(len(calls)))

    calls.clear()
    with mock.patch("urllib.request.urlopen", counting):
        u.fetch_models("sk-key000000000000000000000")
    check("cache: a repeat keyed fetch is served from cache",
          len(calls) == 0, str(len(calls)))

    # Both slots coexist, so /model on one key is not disturbed by a fetch for
    # another — but the newest wins for the display read.
    check("cache: both slots are populated", len(u._CACHE) == 2,
          str(len(u._CACHE)))


def test_default_free_model() -> None:
    """A fresh switch must land on a free model, not the last source's name.

    This is the failure ``default_free_model`` exists to prevent, measured on
    the sibling gateways: with no model named, ``apply_to`` left ``cfg.model``
    on whatever the previous source called its model, and the next turn sent
    that name to a provider that does not serve it.
    """
    from zimzilla import unorouter as u

    u.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        picked = u.default_free_model()
    check("default: the TOP preferred entry is chosen, not merely a preferred one",
          picked == u.PREFERRED_FREE_MODELS[0], str(picked))
    check("default: and that top entry is a free, usable model",
          picked in USABLE_FREE, str(picked))

    # A preferred name that has since gone must fall through to the NEXT
    # preferred entry, not to the catalog's first row and not to nothing.
    # The expected name is computed from the shortlist rather than written as
    # an index, so reordering PREFERRED_FREE_MODELS cannot silently turn this
    # into a test of the wrong entry.
    u.clear_cache()
    without = {"data": [m for m in CATALOG["data"]
                        if m["model_name"] != u.PREFERRED_FREE_MODELS[0]]}
    still_present = [m["model_name"] for m in without["data"]]
    expected_next = next((p for p in u.PREFERRED_FREE_MODELS[1:]
                          if p in still_present), None)
    with mock.patch("urllib.request.urlopen", _stub_urlopen(without)):
        picked = u.default_free_model()
    check("default: a stale preferred name falls through to the next preferred",
          expected_next is not None and picked == expected_next, str(picked))

    # With no preferred name present at all, the catalog's own first usable
    # entry is used rather than nothing. `claude-haiku-5-5:free` is the only
    # free usable model left in this reduced catalog, so it is what "first"
    # must mean — which is exactly why it is in the fixture and not on the
    # shortlist.
    u.clear_cache()
    none_preferred = {"data": [m for m in CATALOG["data"]
                               if m["model_name"] not in
                               u.PREFERRED_FREE_MODELS]}
    with mock.patch("urllib.request.urlopen", _stub_urlopen(none_preferred)):
        picked = u.default_free_model()
    check("default: with no preferred entry, the first usable one is used",
          picked == "claude-haiku-5-5:free", str(picked))

    # Nothing free at all leaves the caller's model alone rather than inventing
    # one — an empty catalog is not a reason to name a model.
    u.clear_cache()
    paid_only = {"data": [m for m in CATALOG["data"]
                          if m["model_name"] == "claude-opus-5"]}
    with mock.patch("urllib.request.urlopen", _stub_urlopen(paid_only)):
        check("default: nothing free yields None",
              u.default_free_model() is None)

    # A failed fetch yields None too, not a crash.
    u.clear_cache()
    with mock.patch("urllib.request.urlopen",
                    _stub_urlopen(error=urllib.error.URLError("down"))):
        check("default: an unreachable gateway yields None",
              u.default_free_model() is None)

    # The cache is read first, so the common path (startup, every switch) does
    # not touch the network.
    u.clear_cache()
    calls = []

    def counting(req, timeout=None):
        calls.append(req)
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", counting):
        u.fetch_models()
    calls.clear()
    with mock.patch("urllib.request.urlopen", counting):
        u.default_free_model()
    check("default: a warm cache means no fetch", len(calls) == 0,
          str(len(calls)))


def test_profile_roundtrip() -> None:
    """save_key writes mode 600, in a format the parser reads back."""
    from zimzilla import unorouter as u

    token = "sk-saved0000000000000000000"
    path = u.save_key(token, "deepseek-v4.1-flash:free")
    check("key: written where load_credentials looks", path == u.PROFILE)
    mode = path.stat().st_mode & 0o777
    check("key: the file is mode 600", mode == 0o600, oct(mode))
    parent_mode = path.parent.stat().st_mode & 0o777
    check("key: the directory is mode 700", parent_mode == 0o700,
          oct(parent_mode))

    exports = u._read_exports(path)
    check("key: the token round-trips",
          exports.get("ANTHROPIC_AUTH_TOKEN") == token)
    check("key: the model round-trips",
          exports.get("ANTHROPIC_MODEL") == "deepseek-v4.1-flash:free")
    check("key: the endpoint is UnoRouter",
          u.is_unorouter(exports.get("ANTHROPIC_BASE_URL")))
    check("key: the endpoint is the SDK prefix, not the API root",
          exports.get("ANTHROPIC_BASE_URL") == "https://api.unorouter.com")
    check("key: the file is source-format",
          path.read_text().count("export ") == 4)

    # A saved key is readable straight back through the real loader.
    check("key: load_credentials reads it back",
          (u.load_credentials() or ("", "", ""))[0] == token)


def test_looks_like_key() -> None:
    from zimzilla import unorouter as u

    # A shape-only sample, not a live credential: `looks_like_key` is a syntax
    # check, so a fabricated token exercises it exactly as a real one would —
    # and a real one in a tracked file is a secret in the repo forever.
    check("key: a real-looking key passes",
          u.looks_like_key("sk-" + "0" * 48))
    check("key: a short string is rejected", not u.looks_like_key("abc"))
    check("key: a pasted export line is rejected",
          not u.looks_like_key('export ANTHROPIC_AUTH_TOKEN="sk-xxxx"'))
    check("key: whitespace is rejected",
          not u.looks_like_key("sk-aaaaaaaaaaaaaaaaaaaa bbbb"))


def test_active_key_recognises_host() -> None:
    """The hosted URL is matched by host, and the path is not confused."""
    from zimzilla import sources as s

    check("sources: the UnoRouter host is the unorouter source",
          s.active_key("https://api.unorouter.com") == "unorouter")
    check("sources: the bare host too",
          s.active_key("https://unorouter.com") == "unorouter")
    check("sources: with a trailing slash too",
          s.active_key("https://api.unorouter.com/") == "unorouter")
    check("sources: it is a builtin, so it survives a restart",
          "unorouter" in s.BUILTIN_KEYS)
    check("sources: a local proxy is still itself",
          s.active_key("http://localhost:4001") == "logfare")
    check("sources: an unrelated host is unknown",
          s.active_key("https://api.example.com") is None)

    # The gateways must not be confused for each other.
    check("sources: unorouter is not openrouter",
          s.active_key("https://api.unorouter.com") != "openrouter")
    check("sources: openrouter is not unorouter",
          s.active_key("https://openrouter.ai/api") != "unorouter")
    check("sources: unorouter is not tokenharbour",
          s.active_key("https://api.unorouter.com") != "tokenharbour")

    # The catalog is reachable through the generic accessor, and a cold cache
    # reports None rather than an empty list.
    from zimzilla import unorouter as u
    u.clear_cache()
    check("sources: models_for returns None on a cold cache",
          s.models_for("https://api.unorouter.com") is None)
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        u.fetch_models()
    check("sources: models_for returns the cached text ids",
          s.models_for("https://api.unorouter.com") == tuple(TEXT_IDS),
          str(s.models_for("https://api.unorouter.com")))
    u.clear_cache()


def test_credential_precedence(tmp: Path) -> None:
    """Environment beats the profile; the profile beats ~/claude-source."""
    from zimzilla import unorouter as u

    claude_source = tmp / "claude-source"
    claude_source.mkdir(parents=True, exist_ok=True)
    (claude_source / "unorouter").write_text(PROFILE_TEXT)

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        with mock.patch.object(u, "CLAUDE_SOURCE", claude_source), \
             mock.patch.object(u, "PROFILE", tmp / "nonexistent" / "source"):
            creds = u.load_credentials()
            check("creds: falls back to ~/claude-source",
                  creds is not None and creds[2].endswith("unorouter"),
                  str(creds and creds[2]))
            check("creds: the model comes from that profile",
                  creds is not None and creds[1] == "deepseek-v4.1-flash:free")

            own = tmp / "own" / "source"
            own.parent.mkdir(parents=True, exist_ok=True)
            own.write_text(
                'export ANTHROPIC_AUTH_TOKEN="sk-ownprofile000000000000"\n')
            with mock.patch.object(u, "PROFILE", own):
                creds = u.load_credentials()
                check("creds: ZimZilla's own profile beats ~/claude-source",
                      creds is not None and creds[2] == str(own),
                      str(creds and creds[2]))

                os.environ["ANTHROPIC_BASE_URL"] = u.DEFAULT_BASE_URL
                os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-fromenv0000000000000"
                os.environ["ANTHROPIC_MODEL"] = "gemini-3.6-flash:free"
                creds = u.load_credentials()
                check("creds: the environment beats every file",
                      creds is not None and creds[2] == "environment"
                      and creds[0] == "sk-fromenv0000000000000",
                      str(creds and creds[2]))

                # An env key pointed at ANOTHER host must not be mistaken for ours.
                os.environ["ANTHROPIC_BASE_URL"] = "https://elsewhere.example.com"
                creds = u.load_credentials()
                check("creds: an env key for another host is ignored",
                      creds is not None and creds[2] == str(own),
                      str(creds and creds[2]))

                # And a sibling gateway's endpoint must not be read as ours.
                os.environ["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api"
                creds = u.load_credentials()
                check("creds: an OpenRouter endpoint is not UnoRouter",
                      creds is not None and creds[2] == str(own),
                      str(creds and creds[2]))

                # A profile with a token but no base URL must not be claimed.
                os.environ.pop("ANTHROPIC_BASE_URL", None)
                creds = u.load_credentials()
                check("creds: no env base URL means the env is not consulted",
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
    from zimzilla import unorouter as u

    class _Cfg:
        base_url = "http://localhost:4001"
        auth_token = "leftover-from-logfare"
        api_key = "sk-leftover"
        model = "deepseek-v4.1-flash"

    with mock.patch.object(u, "load_credentials",
                           lambda: ("sk-x000000000000000000000",
                                    "gemini-3.6-flash:free", "test")):
        cfg = _Cfg()
        applied = u.apply_to(cfg)
        check("apply: returns the origin and model",
              applied == ("test", "gemini-3.6-flash:free"), str(applied))
        check("apply: the endpoint is UnoRouter",
              cfg.base_url == u.DEFAULT_BASE_URL)
        check("apply: the token is set", cfg.auth_token.startswith("sk-"))
        check("apply: the stale api_key is cleared", cfg.api_key == "")
        check("apply: the model is applied", cfg.model == "gemini-3.6-flash:free")

    # With no model named, a FREE one is chosen rather than leaving the previous
    # source's name in place — the failure this whole path guards against.
    with mock.patch.object(u, "load_credentials",
                           lambda: ("sk-x000000000000000000000", "", "test")), \
         mock.patch.object(u, "default_free_model",
                           lambda *a, **k: "deepseek-v4.1-flash:free"):
        cfg = _Cfg()
        applied = u.apply_to(cfg)
        check("apply: an unnamed model lands on a free one",
              cfg.model == "deepseek-v4.1-flash:free", str(applied))

    # And when nothing free exists, the previous model is left alone rather than
    # being blanked — a wrong name is better than no name for the next turn.
    with mock.patch.object(u, "load_credentials",
                           lambda: ("sk-x000000000000000000000", "", "test")), \
         mock.patch.object(u, "default_free_model", lambda *a, **k: None):
        cfg = _Cfg()
        u.apply_to(cfg)
        check("apply: nothing free leaves the previous model in place",
              cfg.model == "deepseek-v4.1-flash", cfg.model)

    with mock.patch.object(u, "load_credentials", lambda: None):
        cfg = _Cfg()
        check("apply: no key leaves the config alone",
              u.apply_to(cfg) is None
              and cfg.base_url == "http://localhost:4001")

    check("guard: the default local proxy is overridable",
          u.applies_to_default_endpoint("http://localhost:4001"))
    check("guard: 127.0.0.1 is overridable",
          u.applies_to_default_endpoint("http://127.0.0.1:4001"))
    check("guard: an empty endpoint is overridable",
          u.applies_to_default_endpoint(""))
    check("guard: UnoRouter itself is NOT overridden",
          not u.applies_to_default_endpoint(u.DEFAULT_BASE_URL))
    check("guard: another host is NOT overridden",
          not u.applies_to_default_endpoint("https://api.example.com"))
    check("guard: a sibling gateway is NOT overridden",
          not u.applies_to_default_endpoint("https://openrouter.ai/api"))

    # The generic name must be the same predicate, so the shared command path
    # in ui/app.py treats this gateway like its siblings.
    check("guard: active_endpoint is is_unorouter",
          u.active_endpoint is u.is_unorouter)


def test_price_for() -> None:
    """Only the free tier can be priced; a paid model must fall through.

    UnoRouter publishes ``model_ratio``/``completion_ratio`` — dimensionless
    multipliers against the gateway's own credit unit — not dollars per million
    tokens. Converting them would be inventing a rate and printing it as the
    gateway's own, so a paid model returns ``None`` and the caller uses the
    local estimate table instead, which is labelled as an estimate.
    """
    from zimzilla import unorouter as u

    models = u._parse_models(CATALOG)
    check("price: a free model prices at exactly zero",
          u.price_for("deepseek-v4.1-flash:free", models) == (0.0, 0.0))
    check("price: a paid model is NOT priced from this catalog",
          u.price_for("claude-opus-5", models) is None)
    check("price: an unknown model is not priced",
          u.price_for("no-such-model", models) is None)
    check("price: a cold catalog prices nothing",
          u.price_for("deepseek-v4.1-flash:free", None) is None)

    # A model free only via the suffix still prices at zero.
    check("price: a suffix-free model prices at zero",
          u.price_for("qwen3.8-27b:free", models) == (0.0, 0.0))


def test_selection_file_is_shared() -> None:
    """The selection file is the same one sources.py reads."""
    from zimzilla import sources as s, unorouter as u

    u.set_current("unorouter")
    check("selection: sources.py sees the unorouter choice",
          s.current_key() == "unorouter", str(s.current_key()))
    check("selection: unorouter sees it too", u.current_key() == "unorouter")


def test_no_key_is_echoed(tmp: Path) -> None:
    """The display value is a path, never the key itself."""
    from zimzilla import unorouter as u

    own = tmp / "own2" / "source"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text('export ANTHROPIC_AUTH_TOKEN="sk-secret0000000000000000"\n')
    with mock.patch.object(u, "PROFILE", own):
        creds = u.load_credentials()
    origin = creds[2]
    check("display: the origin is a path, not a key", "sk-" not in origin, origin)

    # And the module's own constants carry no credential.
    check("display: the module holds no key material",
          "sk-" not in u.DEFAULT_BASE_URL and "sk-" not in u.PRICING_URL)

    # apply_to returns the origin, never the token.
    class _Cfg:
        base_url = "http://localhost:4001"
        auth_token = ""
        api_key = ""
        model = ""
    with mock.patch.object(u, "PROFILE", own):
        applied = u.apply_to(_Cfg())
    check("display: apply_to returns no key",
          applied is not None and "sk-" not in applied[0], str(applied))


def test_the_composed_message_url_is_right() -> None:
    """The URL the SDK actually builds — not the constant it is built from.

    The catalog and the Messages API sit under *different roots* here:
    ``/api/pricing`` is a sibling of ``/v1``, not a child of it. So the base URL
    must be ``https://api.unorouter.com`` with no ``/v1`` — the SDK appends
    ``/v1/messages`` itself, and a base carrying ``/v1`` composes to
    ``/v1/v1/messages`` and 404s every turn while the catalog still works.

    Asserting the constant can only restate the bug; building the request
    through the real client is the only check that can catch it. The catalog
    path is asserted in the same breath, because a "fix" that moved the base to
    ``/v1`` to please the messages path would silently break the catalog — the
    two are not siblings under one prefix the way OpenRouter's are.
    """
    import anthropic
    from anthropic._models import FinalRequestOptions

    from zimzilla import unorouter as u
    from zimzilla.agent import Agent
    from zimzilla.config import Config

    cfg = Config(base_url=u.DEFAULT_BASE_URL,
                 auth_token="sk-x000000000000000000000")
    agent = Agent(cfg)
    opts = FinalRequestOptions(method="post", url="/v1/messages",
                               json_data={}, headers={})
    composed = str(agent.client._build_request(opts).url)

    check("url: the message request lands on /v1/messages",
          composed == "https://api.unorouter.com/v1/messages", composed)
    check("url: there is no doubled /v1 in the path",
          "/v1/v1/" not in composed, composed)
    check("url: the base carries no /v1 at all",
          "/v1" not in u.DEFAULT_BASE_URL, u.DEFAULT_BASE_URL)
    check("url: the base is the bare host",
          u.DEFAULT_BASE_URL == "https://api.unorouter.com", u.DEFAULT_BASE_URL)

    # The catalog must resolve to its own root, which is NOT under /v1.
    seen = {}

    def capture(req, timeout=None):
        seen["url"] = req.full_url
        return _Resp(json.dumps(CATALOG).encode())

    u.clear_cache()
    with mock.patch("urllib.request.urlopen", capture):
        u.fetch_models(use_cache=False)
    check("url: the catalog lands on /api/pricing, not under /v1",
          seen.get("url") == "https://api.unorouter.com/api/pricing",
          seen.get("url"))
    check("url: PRICING_URL agrees with what was fetched",
          u.PRICING_URL == seen.get("url"), u.PRICING_URL)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-uno-test-"))
    # Pin the module's paths to the temp tree before anything imports it.
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    test_metadata_is_a_json_string()
    test_free_detection()
    test_the_free_usable_filter()
    test_model_ids_keep_paid_text_models()
    test_catalog_needs_no_key()
    test_keyed_fetch_sends_the_credential()
    test_fetch_is_total()
    test_cache_is_keyed_by_token()
    test_cache_expiry()
    test_default_free_model()
    test_profile_roundtrip()
    test_looks_like_key()
    test_active_key_recognises_host()
    test_credential_precedence(tmp)
    test_apply_and_guard()
    test_price_for()
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
