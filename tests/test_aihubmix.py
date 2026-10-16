"""Regression suite for the AIHubMix source.

Run:  python tests/test_aihubmix.py   (from an activated venv)

Protects what is particular about this provider:

* **The composed URL.** The Anthropic SDK appends ``/v1/messages`` to whatever
  ``base_url`` it is handed, so ``DEFAULT_BASE_URL`` must be the prefix *before*
  ``/v1``. Asserting the constant can only restate the bug — OpenRouter shipped
  a total outage behind a green suite that way — so this builds the request
  through the real client and reads the URL back. The catalog is checked in the
  same breath because it is a *sibling* under a different prefix
  (``/api/v1/models``), and moving the base to please either path breaks the
  other.
* **The price is the free signal, not the suffix.** ``meta-llama/…:free``
  carries a real 0.02 price and is not free; 50 entries are priced 0/0. The
  suffix alone would over-report, so a successful price read is required.
* **``tool_call`` is not a filter.** The catalog leaves it null on
  ``qwen3.8-flash-free``, which then returns a real ``tool_use`` block — so
  filtering on it would hide working models. The test asserts the null-tool
  model survives ``usable_free_models``, because "helpfully" adding that filter
  is the obvious-looking change that breaks it.
* **``types`` IS a filter.** Only something carrying ``llm`` can answer a chat
  request; the free set contains an OCR engine, a transcription endpoint and a
  decision model that must not be offered as chat models.
* **The catalog needs no key.** ``GET /api/v1/models`` is public, so the free
  list is available *before* ``/aihubmix-api-setup`` has run.

No test here touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

import io
import json
import os
import stat
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


# Shaped like the real response: an OpenAI-style `data` list keyed on
# `model_id`, `pricing` as floats, `types` distinguishing modalities, and a
# `tool_call` that is null on models that demonstrably do call tools.
#
# The entries cover every case the module has to get right:
#   - priced 0/0, llm, tool_call true          → free and usable
#   - priced 0/0, llm, tool_call NULL          → free and usable (the trap)
#   - priced 0/0, llm, no tool_call key        → free and usable
#   - `-free` suffix AND a real 0.02 price     → NOT free (the suffix lies)
#   - `:free` suffix AND a real 0.02 price     → NOT free (the suffix lies)
#   - `-free` suffix with a null price         → free (suffix as fallback)
#   - `-free` suffix, null price, no `llm`     → free but NOT usable (ocr)
#   - priced 0/0 but `types` has no llm        → free but NOT usable (stt)
#   - priced 0/0 but `types` has no llm        → free but NOT usable (decision)
#   - paid, llm                                → neither
#   - paid, llm, huge context                  → neither
CATALOG = {
    "data": [
        {"model_id": "coding-glm-5.3-flash-free",
         "model_name": "Coding GLM 5.3 Flash (free)",
         "types": "llm", "tool_call": True,
         "features": "thinking,tools,function_calling,structured_outputs",
         "context_length": 1000000, "max_output": 131072,
         "pricing": {"input": 0, "output": 0}, "reasoning": True},

        # The one that matters: null tool_call, still drives tools.
        {"model_id": "qwen3.8-flash-free", "model_name": "Qwen3.8 Flash (free)",
         "types": "llm", "tool_call": None,
         "context_length": 262144, "max_output": 65536,
         "pricing": {"input": 0, "output": 0}, "reasoning": False},

        # No `tool_call` key at all — a third spelling of "unknown".
        {"model_id": "union-alpha-free", "model_name": "Union Alpha (free)",
         "types": "llm",
         "context_length": 262144, "max_output": 32768,
         "pricing": {"input": 0, "output": 0}},

        # Suffix says free; the price says 0.02. The price wins.
        {"model_id": "meta-llama/llama-3.1-8b-instruct:free",
         "model_name": "Llama 3.1 8B (free)",
         "types": "llm", "tool_call": True,
         "context_length": 131072,
         "pricing": {"input": 0.02, "output": 0.02}},

        {"model_id": "mistralai/mistral-7b-instruct-free",
         "model_name": "Mistral 7B (free)",
         "types": "llm", "tool_call": True,
         "context_length": 32768,
         "pricing": {"input": 0.02, "output": 0.02}},

        # Suffix with a price that could not be read — the fallback path.
        {"model_id": "some-vendor/mystery-model-free",
         "model_name": "Mystery (free)",
         "types": "llm", "tool_call": True,
         "context_length": 8192, "pricing": None},

        # Free, but not a chat model: an OCR engine.
        {"model_id": "jina-ocr-v1-free", "model_name": "Jina OCR",
         "types": "ocr", "tool_call": None,
         "pricing": {"input": 0, "output": 0}},

        # Free, priced 0/0, but speech-to-text.
        {"model_id": "gpt-live-transcribe", "model_name": "Live Transcribe",
         "types": "stt", "tool_call": None,
         "pricing": {"input": 0, "output": 0}},

        # Free, priced 0/0, but a decision model.
        {"model_id": "decision-model-preview", "model_name": "Decision Preview",
         "types": "decision", "tool_call": None,
         "pricing": {"input": 0, "output": 0}},

        # A free image model — free and llm-less, and also a combined type that
        # DOES carry llm, which must be treated as a chat model.
        {"model_id": "seedream-4-free", "model_name": "Seedream 4 (free)",
         "types": "image_generation", "tool_call": None,
         "pricing": {"input": 0, "output": 0}},
        {"model_id": "search-and-chat-free", "model_name": "Search Chat (free)",
         "types": "llm,search", "tool_call": True,
         "context_length": 131072, "pricing": {"input": 0, "output": 0}},

        # Paid, chat-capable.
        {"model_id": "claude-opus-5", "model_name": "Claude Opus 5",
         "types": "llm", "tool_call": True,
         "context_length": 200000, "max_output": 32000,
         "pricing": {"input": 15.0, "output": 75.0}, "reasoning": True},
        {"model_id": "gpt-5.2", "model_name": "GPT 5.2",
         "types": "llm", "tool_call": True,
         "context_length": 400000,
         "pricing": {"input": 2.5, "output": 10.0}},

        # Malformed rows: skipped, not fatal.
        "not-a-row",
        {"model_name": "no id here", "pricing": {"input": 0, "output": 0}},
        {"model_id": "   ", "pricing": {"input": 0, "output": 0}},
    ]
}

#: The ids that are free AND chat-capable, in catalog order.
USABLE = ["coding-glm-5.3-flash-free", "qwen3.8-flash-free", "union-alpha-free",
          "some-vendor/mystery-model-free", "search-and-chat-free"]

#: Every free id, in catalog order — including the four that are not chat
#: models, which is exactly what makes them worth listing separately.
FREE = ["coding-glm-5.3-flash-free", "qwen3.8-flash-free", "union-alpha-free",
        "some-vendor/mystery-model-free", "jina-ocr-v1-free",
        "gpt-live-transcribe", "decision-model-preview", "seedream-4-free",
        "search-and-chat-free"]

#: The whole well-formed catalog. Three rows in CATALOG are malformed and are
#: skipped by the parser, so this is not ``len(CATALOG["data"])``.
ROWS = 13


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
        seen["data"] = req.data
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())

    return seen, _open


def _clean_env():
    """Context manager that hides any AIHubMix credential in the environment."""
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

def test_free_is_the_price_not_the_suffix() -> None:
    """A zero price is the signal; the ``-free``/``:free`` suffix is a hint."""
    from zimzilla import aihubmix as ah

    models = ah._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("catalog: every well-formed row parsed", len(models) == ROWS, str(len(models)))
    check("catalog: a row with no model_id is skipped", "no id here" not in by_id)

    check("free: a zero-priced llm is free",
          by_id["coding-glm-5.3-flash-free"].free)
    check("free: a zero-priced llm with a null tool_call is free",
          by_id["qwen3.8-flash-free"].free)

    # The heart of it: the suffix says free and the price says 0.02.
    check("free: the suffix does NOT make a priced model free",
          not by_id["meta-llama/llama-3.1-8b-instruct:free"].free)
    check("free: nor does a -free suffix with a price",
          not by_id["mistralai/mistral-7b-instruct-free"].free)
    check("free: the suffix DOES stand in when no price could be read",
          by_id["some-vendor/mystery-model-free"].free)

    check("free: a paid llm is not free", not by_id["claude-opus-5"].free)
    check("free: a second paid llm is not free", not by_id["gpt-5.2"].free)

    got = [m.id for m in models if m.free]
    check("free: exactly the free set, in catalog order", got == FREE,
          f"{len(got)} vs {len(FREE)}")


def test_priced_flag_records_a_read_not_a_block() -> None:
    """``priced`` tracks a successful *read*, so a null block is not 0/0.

    Without it, ``pricing: null`` would parse to 0.0/0.0 and mark the model
    free — a paid model silently offered as free.
    """
    from zimzilla import aihubmix as ah

    models = ah._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("priced: a real 0/0 block counts as priced",
          by_id["coding-glm-5.3-flash-free"].priced)
    check("priced: a real paid block counts as priced",
          by_id["claude-opus-5"].priced)
    check("priced: a null pricing block does NOT count as priced",
          not by_id["some-vendor/mystery-model-free"].priced)

    # And the consequence, stated directly: a null price with no suffix must not
    # read as free, or every unpriced model in an 857-row catalog is free.
    no_price = ah._parse_models({"data": [{"model_id": "unpriced/thing",
                                           "types": "llm", "pricing": None}]})
    check("priced: an unpriced model with no suffix is not free",
          not no_price[0].free)

    # A price that is present but unparseable is the same case.
    junk = ah._parse_models({"data": [{"model_id": "junk/thing", "types": "llm",
                                       "pricing": {"input": "n/a", "output": "n/a"}}]})
    check("priced: an unparseable price is not treated as zero",
          not junk[0].free and not junk[0].priced)


def test_types_is_the_chat_filter() -> None:
    """Only ``llm`` can answer a chat request — image/ocr/stt/decision cannot."""
    from zimzilla import aihubmix as ah

    models = ah._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("chat: an llm is chat-capable", by_id["coding-glm-5.3-flash-free"].chat)
    check("chat: a combined llm,search type is chat-capable",
          by_id["search-and-chat-free"].chat)
    check("chat: an ocr model is not", not by_id["jina-ocr-v1-free"].chat)
    check("chat: an stt model is not", not by_id["gpt-live-transcribe"].chat)
    check("chat: a decision model is not", not by_id["decision-model-preview"].chat)
    check("chat: an image model is not", not by_id["seedream-4-free"].chat)
    check("chat: a paid llm is still chat-capable", by_id["claude-opus-5"].chat)


def test_tool_call_is_not_a_filter() -> None:
    """The catalog under-reports tools, so ``tool_call`` must never exclude.

    ``qwen3.8-flash-free`` carries ``tool_call: null`` and returns a real
    ``tool_use`` block when handed a tool — measured against the live gateway.
    Filtering the free list on that field is the obvious-looking change, and it
    would hide a working model, so the case is pinned here by name.
    """
    from zimzilla import aihubmix as ah

    with mock.patch("urllib.request.urlopen", _stub()):
        ah.clear_cache()
        usable, err = ah.usable_free_models()

    check("tools: the fetch succeeded", usable is not None, err)
    ids = [m.id for m in usable] if usable else []
    check("tools: a null-tool_call model survives the free filter",
          "qwen3.8-flash-free" in ids)
    check("tools: a model with no tool_call key survives too",
          "union-alpha-free" in ids)
    check("tools: the usable set is exactly the free chat models",
          ids == USABLE, str(ids))

    # The advisory flag is still readable, just not load-bearing.
    by_id = {m.id: m for m in ah._parse_models(CATALOG)}
    check("tools: tool_call true reads as tools",
          by_id["coding-glm-5.3-flash-free"].tools)
    check("tools: a null tool_call reads as no claim",
          not by_id["qwen3.8-flash-free"].tools)


def test_catalog_needs_no_key() -> None:
    """The catalog is public — a keyless fetch must succeed and send nothing."""
    from zimzilla import aihubmix as ah

    seen, opener = _capture()
    with _clean_env(), mock.patch("urllib.request.urlopen", opener):
        ah.clear_cache()
        models, err = ah.fetch_models("")

    check("keyless: the fetch succeeded with no token", models is not None, err)
    check("keyless: no Authorization header is sent",
          "authorization" not in seen.get("headers", {}),
          str(sorted(seen.get("headers", {}))))
    check("keyless: the catalog lands on /api/v1/models",
          seen.get("url") == "https://aihubmix.com/api/v1/models", seen.get("url"))
    check("keyless: it is a GET", seen.get("method") == "GET", seen.get("method"))

    # With a token the same call carries a Bearer, because the listing should
    # match what this account would be charged.
    seen2, opener2 = _capture()
    with mock.patch("urllib.request.urlopen", opener2):
        ah.clear_cache()
        ah.fetch_models("sk-abcdef0123456789")
    check("keyed: the token is sent as a Bearer",
          seen2.get("headers", {}).get("authorization") == "Bearer sk-abcdef0123456789",
          str(seen2.get("headers", {}).get("authorization")))


def test_no_anthropic_version_header_on_the_catalog() -> None:
    """Sending ``anthropic-version`` to the catalog would flip its shape.

    The same trap OpenRouter hit: that header makes a gateway answer the
    *Anthropic* Models API shape instead of its own catalog. It is asserted
    directly because it is invisible until the model list is silently wrong.
    """
    from zimzilla import aihubmix as ah

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        ah.clear_cache()
        ah.fetch_models(use_cache=False)

    headers = seen.get("headers", {})
    check("headers: no anthropic-version on the catalog",
          "anthropic-version" not in headers, str(sorted(headers)))
    check("headers: no x-api-key on the catalog",
          "x-api-key" not in headers, str(sorted(headers)))


def test_fetch_is_total() -> None:
    """A network failure is returned, never raised — the caller is a TUI."""
    from zimzilla import aihubmix as ah

    for name, exc, want in (
        ("http 500", urllib.error.HTTPError("u", 500, "boom", {}, None), "500"),
        ("http 401", urllib.error.HTTPError("u", 401, "nope", {}, None), "rejected"),
        ("http 403", urllib.error.HTTPError("u", 403, "nope", {}, None), "rejected"),
        ("url error", urllib.error.URLError("dns"), "could not reach"),
        ("os error", OSError("socket"), "could not reach"),
        ("bad json", ValueError("nope"), "could not reach"),
    ):
        with mock.patch("urllib.request.urlopen", _stub(error=exc)):
            ah.clear_cache()
            models, err = ah.fetch_models(use_cache=False)
        check(f"total: {name} returns (None, message)",
              models is None and want in err, f"{models!r} {err!r}")

    # A genuinely empty catalog is [] — distinguishable from a failure.
    with mock.patch("urllib.request.urlopen", _stub({"data": []})):
        ah.clear_cache()
        models, err = ah.fetch_models(use_cache=False)
    check("total: an empty catalog is [] with no error", models == [] and not err,
          f"{models!r} {err!r}")

    # A catalog that is not even an object must not crash the parse.
    with mock.patch("urllib.request.urlopen", _stub({"nope": 1})):
        ah.clear_cache()
        models, err = ah.fetch_models(use_cache=False)
    check("total: a payload with no data key yields []", models == [], repr(models))


def test_cache_is_keyed_and_expires() -> None:
    """Keyed by token so a key change refetches; TTL'd so a stale list is not live."""
    from zimzilla import aihubmix as ah

    calls = {"n": 0}

    def counting(req, timeout=None):
        calls["n"] += 1
        return _Resp(json.dumps(CATALOG).encode())

    ah.clear_cache()
    with mock.patch("urllib.request.urlopen", counting):
        ah.fetch_models("")
        ah.fetch_models("")
        check("cache: the second identical call is served from cache",
              calls["n"] == 1, str(calls["n"]))
        # A different token is a different cache entry.
        ah.fetch_models("sk-other-key-000000")
        check("cache: a different token refetches", calls["n"] == 2, str(calls["n"]))
        # And an explicit bypass always goes out.
        ah.fetch_models("", use_cache=False)
        check("cache: use_cache=False always refetches", calls["n"] == 3, str(calls["n"]))

    # TTL: an entry older than CACHE_TTL reads as cold.
    ah.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub()):
        ah.fetch_models("")
    check("cache: a fresh entry is readable", ah.cached_models() is not None)
    check("cache: its age is a number", isinstance(ah.catalog_age(), float))
    with mock.patch.object(ah, "CACHE_TTL", -1.0):
        check("cache: an expired entry reads as None", ah.cached_models() is None)
        check("cache: an expired entry yields no ids", ah.cached_ids() is None)
    ah.clear_cache()
    check("cache: clearing empties it", ah.cached_models() is None)
    check("cache: and reports no age", ah.catalog_age() is None)


def test_the_composed_message_url_is_right() -> None:
    """The URL the SDK actually builds — not the constant it is built from.

    This is the test whose absence let a total outage ship behind a green suite
    on OpenRouter: every turn 404'd while the assertions, which checked the
    constant, passed. The SDK appends ``/v1/messages`` to whatever prefix it is
    handed, so asserting the constant can only restate the bug.

    The catalog is checked in the same breath, because the two paths sit under
    *different* prefixes — messages at ``{base}/v1/messages`` (the SDK's doing),
    models at ``{base}/api/v1/models`` (built by hand). Correcting either one
    alone breaks the other, and a fix that moved the base to ``/v1`` to please
    the messages path would silently 404 the catalog.
    """
    import anthropic
    from anthropic._models import FinalRequestOptions

    from zimzilla import aihubmix as ah
    from zimzilla.agent import Agent
    from zimzilla.config import Config

    cfg = Config(base_url=ah.DEFAULT_BASE_URL, auth_token="sk-x0000000000000000")
    agent = Agent(cfg)
    opts = FinalRequestOptions(method="post", url="/v1/messages",
                               json_data={}, headers={})
    composed = str(agent.client._build_request(opts).url)

    check("url: the message request lands on /v1/messages",
          composed == "https://aihubmix.com/v1/messages", composed)
    check("url: there is no doubled /v1 in the path",
          "/v1/v1/" not in composed, composed)
    check("url: the base is the prefix BEFORE /v1, not the API root",
          ah.DEFAULT_BASE_URL == "https://aihubmix.com", ah.DEFAULT_BASE_URL)

    seen, opener = _capture()
    ah.clear_cache()
    with mock.patch("urllib.request.urlopen", opener):
        ah.fetch_models(use_cache=False)
    check("url: the catalog lands on /api/v1/models, not /v1/models",
          seen.get("url") == "https://aihubmix.com/api/v1/models", seen.get("url"))


def test_profile_roundtrip(tmp: Path) -> None:
    """The key is written mode 600 and read back byte-identical."""
    from zimzilla import aihubmix as ah

    path = ah.save_key("sk-test-key-abcdefghijklmnop", "qwen3.8-flash-free")
    check("profile: written to the aihubmix profile", path == ah.PROFILE, str(path))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    check("profile: mode is 600", mode == 0o600, oct(mode))
    check("profile: the directory is 700",
          stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700,
          oct(stat.S_IMODE(os.stat(path.parent).st_mode)))

    creds = ah.load_credentials()
    check("profile: the key round-trips", creds and creds[0] == "sk-test-key-abcdefghijklmnop")
    check("profile: the model round-trips", creds and creds[1] == "qwen3.8-flash-free")
    check("profile: the origin is the profile path", creds and creds[2] == str(path))

    text = path.read_text(encoding="utf-8")
    check("profile: the base URL is the SDK prefix",
          'ANTHROPIC_BASE_URL="https://aihubmix.com"' in text, text)
    check("profile: api_key is cleared, not left to shadow auth_token",
          'ANTHROPIC_API_KEY=""' in text)
    check("profile: has_key agrees", ah.has_key())


def test_read_exports_is_total(tmp: Path) -> None:
    """The profile parser handles quotes, junk and a missing file."""
    from zimzilla import aihubmix as ah

    p = tmp / "exports-test"
    p.write_text(
        'export A="quoted"\n'
        "export B=bare\n"
        "export C='single'\n"
        "# export D=commented\n"
        "D=no-export-prefix\n"
        "export E=with=equals\n"
        "export F=\n"
        "not a line at all\n",
        encoding="utf-8")
    got = ah._read_exports(p)
    check("exports: double quotes stripped", got.get("A") == "quoted", repr(got.get("A")))
    check("exports: bare value kept", got.get("B") == "bare")
    check("exports: single quotes stripped", got.get("C") == "single")
    check("exports: a commented line is ignored", "D" not in got)
    check("exports: a value may contain =", got.get("E") == "with=equals")
    check("exports: an empty value is empty", got.get("F") == "")
    check("exports: a missing file is {}",
          ah._read_exports(tmp / "does-not-exist") == {})


def test_credential_precedence(tmp: Path) -> None:
    """environment → own profile → ~/claude-source, first hit wins."""
    from zimzilla import aihubmix as ah

    ah.PROFILE.unlink(missing_ok=True)
    with _clean_env():
        check("precedence: nothing anywhere is None", ah.load_credentials() is None)
        check("precedence: has_key agrees", not ah.has_key())

        # 3. A claude-source profile, with a foreign one that must be skipped.
        foreign = ah.CLAUDE_SOURCE / "other"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text('export ANTHROPIC_BASE_URL="https://openrouter.ai/api"\n'
                           'export ANTHROPIC_AUTH_TOKEN="sk-or-should-be-skipped"\n')
        src = ah.CLAUDE_SOURCE / "aimix"
        src.write_text('export ANTHROPIC_BASE_URL="https://aihubmix.com"\n'
                       'export ANTHROPIC_AUTH_TOKEN="sk-from-claude-source"\n'
                       'export ANTHROPIC_MODEL="qwen3.8-flash-free"\n')
        creds = ah.load_credentials()
        check("precedence: a claude-source profile is found",
              creds and creds[0] == "sk-from-claude-source", str(creds and creds[2]))
        check("precedence: a profile for another host is skipped",
              creds and creds[0] != "sk-or-should-be-skipped")

        # 2. ZimZilla's own profile beats it.
        ah.save_key("sk-from-own-profile", "coding-glm-5.3-flash-free")
        creds = ah.load_credentials()
        check("precedence: the own profile beats claude-source",
              creds and creds[0] == "sk-from-own-profile", str(creds and creds[2]))

        # 1. The environment beats everything — but only when the base matches.
        os.environ["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-or-env-wrong-host"
        creds = ah.load_credentials()
        check("precedence: an env token for another host is ignored",
              creds and creds[0] == "sk-from-own-profile", str(creds and creds[2]))

        os.environ["ANTHROPIC_BASE_URL"] = "https://aihubmix.com"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-from-environment"
        creds = ah.load_credentials()
        check("precedence: the environment wins when the host matches",
              creds and creds[0] == "sk-from-environment", str(creds and creds[2]))
        check("precedence: the origin says environment", creds and creds[2] == "environment")

        # ANTHROPIC_API_KEY is the fallback spelling.
        os.environ.pop("ANTHROPIC_AUTH_TOKEN")
        os.environ["ANTHROPIC_API_KEY"] = "sk-from-api-key-var"
        creds = ah.load_credentials()
        check("precedence: ANTHROPIC_API_KEY is the fallback spelling",
              creds and creds[0] == "sk-from-api-key-var", str(creds and creds[0][:6]))


def test_no_key_is_echoed(tmp: Path) -> None:
    """Nothing a caller can reach returns or prints the key."""
    from zimzilla import aihubmix as ah

    secret = "sk-SUPERSECRETVALUE0123456789abcdef"
    ah.save_key(secret, "qwen3.8-flash-free")

    creds = ah.load_credentials()
    # load_credentials must return it (that is its job) — but the *origin* it
    # reports is a path, never the key, and that is what the TUI prints.
    check("echo: origin is a path, not the key",
          creds and secret not in creds[2], str(creds and creds[2]))

    with mock.patch("urllib.request.urlopen", _stub()):
        ah.clear_cache()
        models, err = ah.fetch_models(secret)
    check("echo: no error string carries the key", secret not in (err or ""))

    # The blurb and the label are rendered in the transcript; neither may leak.
    for m in ah._parse_models(CATALOG):
        check(f"echo: blurb of {m.id[:18]} does not carry a key",
              "sk-" not in m.blurb, m.blurb)
    check("echo: price_for does not carry a key",
          secret not in str(ah.price_for("claude-opus-5", ah._parse_models(CATALOG))))

    ah.PROFILE.unlink(missing_ok=True)


def test_looks_like_key() -> None:
    from zimzilla import aihubmix as ah

    # A synthetic key in the real shape: 51 chars of hex behind an `sk-`. The
    # operator's actual key is never written into a test — a fixture that
    # asserted against the live credential would put it in the repository.
    check("key: a real-shaped key passes",
          ah.looks_like_key("sk-" + "0123456789abcdef" * 3 + "012"))
    check("key: a short string fails", not ah.looks_like_key("sk-short"))
    check("key: an empty string fails", not ah.looks_like_key(""))
    check("key: a whole export line fails",
          not ah.looks_like_key('export ANTHROPIC_AUTH_TOKEN="sk-abcdefghijklmnopqrst"'))
    check("key: an embedded space fails",
          not ah.looks_like_key("sk-abcdefghijklmno pqrstuvwxyz"))
    check("key: a newline fails", not ah.looks_like_key("sk-abcdefghijklmnop\nqrstuvwxyz"))
    check("key: surrounding whitespace is tolerated",
          ah.looks_like_key("  sk-abcdefghijklmnopqrst  "))


def test_active_key_recognises_host() -> None:
    """The gateway is identified by host, before the port regex can miss it."""
    from zimzilla import aihubmix as ah
    from zimzilla import sources as s

    check("host: the bare host matches", ah.is_aihubmix("https://aihubmix.com"))
    check("host: a path under it matches", ah.is_aihubmix("https://aihubmix.com/v1"))
    check("host: the SDK prefix matches", ah.is_aihubmix(ah.DEFAULT_BASE_URL))
    check("host: another gateway does not", not ah.is_aihubmix("https://openrouter.ai/api"))
    check("host: None does not", not ah.is_aihubmix(None))
    check("host: empty does not", not ah.is_aihubmix(""))
    check("host: active_endpoint is the same predicate",
          ah.active_endpoint is ah.is_aihubmix)

    check("host: sources.active_key finds it", s.active_key("https://aihubmix.com") == "aihubmix")
    check("host: it is a builtin, so a selection survives a launch",
          "aihubmix" in s.BUILTIN_KEYS)
    check("host: the port path still works",
          s.active_key("http://localhost:4001") == "logfare")


def test_apply_and_guard(tmp: Path) -> None:
    """``apply_to`` aims the session; the guard refuses to hijack a deliberate host."""
    from zimzilla import aihubmix as ah
    from zimzilla.config import Config

    # Earlier tests leave credentials behind — a key in ZimZilla's own profile
    # and an AIHubMix profile under ~/claude-source. The first assertion here is
    # about having *none*, so both are cleared rather than inherited. (That the
    # claude-source fallback is what gets found is the previous test's business.)
    ah.PROFILE.unlink(missing_ok=True)
    empty_source = tmp / "no-claude-source"
    empty_source.mkdir(exist_ok=True)
    with _clean_env(), mock.patch.object(ah, "CLAUDE_SOURCE", empty_source):
        cfg = Config(base_url="http://localhost:4001", auth_token="", api_key="stale-key")
        cfg.model = "old-model"
        check("apply: with no key it declines", ah.apply_to(cfg) is None)
        check("apply: and leaves the base alone",
              cfg.base_url == "http://localhost:4001", cfg.base_url)

        ah.save_key("sk-apply-test-0123456789", "qwen3.8-flash-free")
        cfg = Config(base_url="http://localhost:4001", auth_token="", api_key="stale-key")
        cfg.model = "old-model"
        applied = ah.apply_to(cfg)
        check("apply: it reports the origin and model",
              applied == (str(ah.PROFILE), "qwen3.8-flash-free"), str(applied))
        check("apply: the base is the SDK prefix",
              cfg.base_url == "https://aihubmix.com", cfg.base_url)
        check("apply: the token is set", cfg.auth_token == "sk-apply-test-0123456789")
        # The stale key must go: the client sends both headers, and a key for
        # another provider sitting in memory is a credential for the wrong host.
        check("apply: a stale api_key is cleared", cfg.api_key == "", repr(cfg.api_key))
        check("apply: the model is taken from the profile",
              cfg.model == "qwen3.8-flash-free", cfg.model)

        # A profile with no model must not blank the model in use.
        ah.save_key("sk-apply-test-0123456789", "")
        cfg = Config(base_url="http://localhost:4001", auth_token="")
        cfg.model = "keep-me"
        ah.apply_to(cfg)
        check("apply: an empty profile model leaves the current one",
              cfg.model == "keep-me", cfg.model)

    check("guard: the default empty endpoint may be overridden",
          ah.applies_to_default_endpoint(""))
    check("guard: a local proxy may be overridden",
          ah.applies_to_default_endpoint("http://localhost:4001"))
    check("guard: 127.0.0.1 may be overridden",
          ah.applies_to_default_endpoint("http://127.0.0.1:4000"))
    check("guard: a deliberate remote host is left alone",
          not ah.applies_to_default_endpoint("https://openrouter.ai/api"))
    check("guard: itself is left alone",
          not ah.applies_to_default_endpoint("https://aihubmix.com"))


def test_selection_file_is_shared(tmp: Path) -> None:
    """The selection is written through sources, so two writers cannot disagree."""
    from zimzilla import aihubmix as ah
    from zimzilla import sources as s

    ah.set_current("aihubmix")
    check("selection: written through sources", s.current_key() == "aihubmix")
    check("selection: the module reads it back", ah.current_key() == "aihubmix")

    s.set_current("openrouter")
    check("selection: a switch elsewhere is seen here", ah.current_key() == "openrouter")

    ah.set_current("aihubmix")
    check("selection: and back again", s.current_key() == "aihubmix")


def test_free_and_id_helpers() -> None:
    """The three catalog views agree with each other."""
    from zimzilla import aihubmix as ah

    with mock.patch("urllib.request.urlopen", _stub()):
        ah.clear_cache()
        free, err = ah.free_models()
        check("views: free_models succeeds", free is not None, err)
        check("views: free_models includes non-chat models",
              [m.id for m in free] == FREE, str([m.id for m in free]))

        usable, err = ah.usable_free_models()
        check("views: usable_free_models is the chat subset",
              [m.id for m in usable] == USABLE)

        ids, err = ah.model_ids()
        check("views: model_ids is every chat model, not just free",
              ids is not None and "claude-opus-5" in ids and "qwen3.8-flash-free" in ids,
              str(len(ids or [])))
        check("views: model_ids excludes the ocr model", "jina-ocr-v1-free" not in ids)
        # Nine, not five: model_ids is every chat model, free or not. The two
        # priced `llm` entries with a misleading -free/:free suffix are chat
        # models too — they are just not free, which is a different question.
        check("views: model_ids is every chat model, free or not",
              len(ids) == 9, str(len(ids or [])))

        check("views: cached_ids matches, filtered to chat",
              ah.cached_ids() == ids, str(ah.cached_ids()))
        check("views: cached_models is the full catalog",
              len(ah.cached_models()) == ROWS, str(len(ah.cached_models())))

        # price_for reads the catalog, and is None for an unknown model.
        models = ah.cached_models()
        check("views: price_for finds a paid model",
              ah.price_for("claude-opus-5", models) == (15.0, 75.0),
              str(ah.price_for("claude-opus-5", models)))
        check("views: price_for finds a free model",
              ah.price_for("qwen3.8-flash-free", models) == (0.0, 0.0))
        check("views: price_for is None for an unknown model",
              ah.price_for("nope/not-here", models) is None)
        check("views: price_for is None with no catalog",
              ah.price_for("claude-opus-5", None) is None)


def test_blurb_says_what_matters() -> None:
    """The one line the picker renders, for each interesting shape."""
    from zimzilla import aihubmix as ah

    by_id = {m.id: m for m in ah._parse_models(CATALOG)}

    check("blurb: a free model says free",
          "free" in by_id["coding-glm-5.3-flash-free"].blurb,
          by_id["coding-glm-5.3-flash-free"].blurb)
    check("blurb: a reasoning model says so",
          "reasoning" in by_id["coding-glm-5.3-flash-free"].blurb)
    check("blurb: the context window is shown",
          "1000k ctx" in by_id["coding-glm-5.3-flash-free"].blurb,
          by_id["coding-glm-5.3-flash-free"].blurb)
    check("blurb: a paid model shows its price",
          "$15/$75" in by_id["claude-opus-5"].blurb,
          by_id["claude-opus-5"].blurb)
    check("blurb: a non-llm type is surfaced",
          "ocr" in by_id["jina-ocr-v1-free"].blurb,
          by_id["jina-ocr-v1-free"].blurb)
    check("blurb: a plain llm does not repeat its type",
          "llm" not in by_id["union-alpha-free"].blurb,
          by_id["union-alpha-free"].blurb)


def test_sources_registry_dispatch(tmp: Path) -> None:
    """``sources`` routes aihubmix through the catalog, not the declared list."""
    from zimzilla import aihubmix as ah
    from zimzilla import sources as s

    with mock.patch("urllib.request.urlopen", _stub()):
        ah.clear_cache()
        # A cold cache yields None, so the caller falls back to KNOWN_MODELS.
        check("registry: a cold cache is None", s.models_for("https://aihubmix.com") is None)
        check("registry: and no catalog models either",
              s.catalog_models("aihubmix") is None)

        ah.fetch_models("")
        ids = s.models_for("https://aihubmix.com")
        check("registry: a warm cache gives the chat ids",
              ids is not None and "qwen3.8-flash-free" in ids, str(ids and len(ids)))
        check("registry: the ids exclude non-chat models",
              ids is not None and "jina-ocr-v1-free" not in ids)
        check("registry: catalog_models returns the Model objects",
              len(s.catalog_models("aihubmix") or []) == ROWS)
        check("registry: an unknown key is still None",
              s.catalog_models("nope") is None)
        # The window is read off the module's own Model by name.
        win = s.context_window_for("https://aihubmix.com", "coding-glm-5.3-flash-free")
        check("registry: the context window is read from the catalog",
              win == 1000000, str(win))
        check("registry: an unknown model has no window",
              s.context_window_for("https://aihubmix.com", "nope") is None)


def test_tui_surfaces() -> None:
    """The commands, help rows and status row are wired, not just the module."""
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main_src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")

    for cmd in ("zim-aihubmix", "aihubmix-api-setup", "aihubmix-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app_src)
        check(f"tui: /{cmd} is in the busy-ok set", f'"{cmd}"' in app_src)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp_src)

    check("tui: the gateway table knows aihubmix",
          'if kind == "aihubmix":' in app_src and 'return ah_mod, "AIHubMix"' in app_src)
    check("tui: the status pane lists it",
          '("aihubmix", ah_mod, "AIHubMix", "/aihubmix-api-setup")' in app_src)
    check("tui: the model picker knows it",
          'if active == "aihubmix" else None' in app_src)
    check("tui: /model refetches for it", 'elif active == "aihubmix":' in app_src)
    check("tui: /models accepts it", '"unorouter", "antseed", "aihubmix"' in app_src)
    check("tui: the launcher restores it",
          '("aihubmix", ah_mod)' in main_src)
    check("tui: the module is imported by the app",
          "from .. import aihubmix as ah_mod" in app_src)
    check("tui: /models completion offers it",
          '"antseed", "aihubmix"' in comp_src)
    check("tui: it is a known source for the guard",
          '"logfare", "aihubmix"' in app_src)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-ah-test-"))
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
        test_free_is_the_price_not_the_suffix()
        test_priced_flag_records_a_read_not_a_block()
        test_types_is_the_chat_filter()
        test_tool_call_is_not_a_filter()
        test_catalog_needs_no_key()
        test_no_anthropic_version_header_on_the_catalog()
        test_fetch_is_total()
        test_cache_is_keyed_and_expires()
        test_the_composed_message_url_is_right()
        test_profile_roundtrip(tmp)
        test_read_exports_is_total(tmp)
        test_credential_precedence(tmp)
        test_no_key_is_echoed(tmp)
        test_looks_like_key()
        test_active_key_recognises_host()
        test_apply_and_guard(tmp)
        test_selection_file_is_shared(tmp)
        test_free_and_id_helpers()
        test_blurb_says_what_matters()
        test_sources_registry_dispatch(tmp)
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
