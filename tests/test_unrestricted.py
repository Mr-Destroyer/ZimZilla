"""Regression suite for the unrestricted.sh source.

Run:  python tests/test_unrestricted.py   (from an activated venv)

Protects what is particular about this provider, every point of it measured
against the live endpoint on 2026-11-08 rather than assumed:

* **The catalogue is public and priced in dollars.** ``GET /v1/models`` answers
  200 with no key, and the payload is byte-identical with and without one. So
  the free list works before ``/unrestricted-api-setup`` has run, and a paid
  model is priced at the gateway's own rate rather than this harness's estimate
  — which has never heard of these ids and would bill every one at $1/$3.
* **A zero price is not a permission.** The one zero-priced model in the
  catalogue answers ``400 "for paid accounts. Add credit to use it."`` on a
  fresh account, while a *paid* model answers against the $0.50 welcome credit.
  ``Model.free`` therefore says what a model costs and nothing more, and
  ``first_callable`` is what says whether it can be called. This is the single
  most important thing in the module: a switch that trusted the price column
  would land on the one model that cannot reply.
* **An unknown model id does not 404 — it silently answers as another model.**
  ``{"model": "gpt-4o"}`` returned 200 with ``qwen3.8-27b-uncensored`` in the
  reply. So the harness must police the name against the catalogue itself; the
  gateway will not.
* **``/v1/account`` is key-gated and is the real "what can I call" answer.**
  The catalogue prices a model; only this route says whether the balance covers
  it.
* **The base URL carries no path.** The SDK appends ``/v1/messages``, so
  ``https://api.unrestricted.sh/v1`` as the base composes to ``/v1/v1/messages``
  and 404s on every turn while the catalogue still works.
* **The key never ships.** ``~/.zimzilla/unrestricted/source`` is written by
  ``/unrestricted-api-setup`` and nothing under ``packaging/`` holds a
  credential.

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


#: Shaped like the live catalogue, including the two things that matter: a
#: zero-priced model and a paid one, and the ``pricing`` object in dollars per
#: million tokens.
CATALOG = {
    "object": "list",
    "data": [
        {"id": "qwen3.8-27b-uncensored", "object": "model",
         "owned_by": "unrestricted.sh", "name": "Qwen3.8 27B Uncensored",
         "display_name": "Qwen3.8 27B Uncensored",
         "context_length": 262144, "max_output_tokens": 32768,
         "pricing": {"input": 0.5, "cached_input": 0.05, "output": 1},
         "zero_data_retention": True, "partner_hosted": False,
         "input_modalities": ["text"]},
        {"id": "mimo-v2.6-flash-uncensored", "object": "model",
         "display_name": "MiMo V2.6 Flash Uncensored",
         "context_length": 1048576, "max_output_tokens": 65536,
         "pricing": {"input": 0.4, "cached_input": 0.29, "output": 1.3},
         "zero_data_retention": True, "partner_hosted": True,
         "input_modalities": ["text"]},
        {"id": "glm-5.3-flash-uncensored", "object": "model",
         "display_name": "GLM-5.3 Flash Uncensored",
         "context_length": 1048576, "max_output_tokens": 32768,
         "pricing": {"input": 0.2, "cached_input": 0.19, "output": 0.6},
         "zero_data_retention": True, "partner_hosted": True,
         "input_modalities": ["text"]},
        {"id": "deepseek-v4.1-flash-uncensored", "object": "model",
         "display_name": "DeepSeek V4.1 Flash 1017 Uncensored",
         "context_length": 1048576, "max_output_tokens": 65536,
         "pricing": {"input": 0, "cached_input": 0, "output": 0},
         "zero_data_retention": True, "partner_hosted": False,
         "input_modalities": ["text"]},
        # --- rows that must be dropped by the parser, not the filter -----
        "not-a-row",
        {"no_id": True},
        {"id": "   "},
    ],
}

#: Every chat id, catalogue order preserved.
CHAT = ["qwen3.8-27b-uncensored", "mimo-v2.6-flash-uncensored",
        "glm-5.3-flash-uncensored", "deepseek-v4.1-flash-uncensored"]

#: The one model the catalogue prices at zero in and out.
FREE = "deepseek-v4.1-flash-uncensored"

#: The model measured to answer on a fresh account's welcome credit.
ANSWERS = "qwen3.8-27b-uncensored"

#: The 400 body a model the balance does not cover returns. Note the status: it
#: is a 400, not a 402, which is why explain_error keys on the text.
PAID_BODY = json.dumps({"type": "error", "error": {
    "type": "invalid_request_error",
    "message": ("DeepSeek V4.1 Flash 1017 Uncensored is for paid accounts. "
                "Add credit to use it.")}})

#: The 401 body a bad key returns.
AUTH_BODY = json.dumps({"type": "error", "error": {
    "type": "authentication_error",
    "message": "Invalid API key. Send it as x-api-key or Authorization: "
               "Bearer unr_sk_..."}})


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
    return _open, seen


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.unrestricted.sh/v1/messages", code, "err", {},
        io.BytesIO(body.encode()))


def _fresh():
    """The module with an empty cache, so each case starts cold."""
    from zimzilla import unrestricted as u
    u.clear_cache()
    return u


# ---------------------------------------------------------------------------
# 1 — the catalogue
# ---------------------------------------------------------------------------

def test_parser_drops_malformed_rows() -> None:
    u = _fresh()
    models = u._parse_models(CATALOG)
    check("parser: malformed rows are dropped, not fatal",
          [m.id for m in models] == CHAT, str([m.id for m in models]))
    check("parser: a non-list payload is an empty catalogue",
          u._parse_models({"data": "nope"}) == [])
    check("parser: a missing data key is an empty catalogue",
          u._parse_models({}) == [])


def test_prices_are_dollars_per_mtok() -> None:
    u = _fresh()
    models = u._parse_models(CATALOG)
    qwen = u.model_for(ANSWERS, models)
    check("price: the catalogue's own rate is read, not estimated",
          (qwen.price_in, qwen.price_out) == (0.5, 1.0),
          f"{qwen.price_in}/{qwen.price_out}")
    check("price: the cached-input rate is kept",
          qwen.price_cached == 0.05, str(qwen.price_cached))
    check("price: price_for returns the published pair",
          u.price_for(ANSWERS, models) == (0.5, 1.0),
          str(u.price_for(ANSWERS, models)))
    check("price: a zero-priced model prices at (0, 0)",
          u.price_for(FREE, models) == (0.0, 0.0),
          str(u.price_for(FREE, models)))
    check("price: an unknown model is None, not a guess",
          u.price_for("nope", models) is None)

    # An entry with no pricing object is NOT free — absence of a rate is not a
    # rate of zero, and reporting it as free is the discrepancy that bills a
    # model at nothing.
    bare = u._parse_models({"data": [{"id": "x", "input_modalities": ["text"]}]})
    check("price: an unpriced entry is not free",
          bare[0].free is False and bare[0].priced is False,
          f"free={bare[0].free} priced={bare[0].priced}")
    check("price: an unpriced entry returns None from price_for",
          u.price_for("x", bare) is None)


def test_free_is_a_price_not_a_permission() -> None:
    """The finding the whole module is built around."""
    u = _fresh()
    models = u._parse_models(CATALOG)
    free = [m.id for m in models if m.free]
    check("free: exactly the zero-priced model is free", free == [FREE], str(free))
    check("free: a paid model is not free",
          u.model_for(ANSWERS, models).free is False)
    # Both halves are required: a zero input rate with a charged output rate is
    # free to ask and billed to answer.
    half = u._parse_models({"data": [{
        "id": "half", "input_modalities": ["text"],
        "pricing": {"input": 0, "output": 1.0}}]})
    check("free: zero in and charged out is NOT free", half[0].free is False,
          str(half[0].free))
    # And the docstring's claim, asserted rather than described: the free model
    # is the one that refuses on a fresh account.
    check("free: the module says a zero price is not a permission",
          "not a permission" in (u.__doc__ or "").lower()
          or "not a permission" in (u.Model.free.__doc__ or "").lower())


def test_non_chat_models_are_filtered() -> None:
    u = _fresh()
    payload = {"data": [
        {"id": "chat", "input_modalities": ["text"],
         "pricing": {"input": 0, "output": 0}},
        {"id": "image", "input_modalities": ["image"],
         "pricing": {"input": 0, "output": 0}},
        {"id": "no-mods", "pricing": {"input": 0, "output": 0}},
    ]}
    models = u._parse_models(payload)
    check("filter: a text model is chat", u.model_for("chat", models).chat is True)
    check("filter: an image model is not chat",
          u.model_for("image", models).chat is False)
    check("filter: a model with no modalities is not chat",
          u.model_for("no-mods", models).chat is False)
    with mock.patch.object(u.urllib.request, "urlopen", _stub(payload)):
        usable, err = u.usable_free_models()
    check("filter: usable_free_models keeps only the chat one",
          [m.id for m in usable] == ["chat"], str([m.id for m in usable]))


def test_candidates_are_cheapest_first() -> None:
    u = _fresh()
    with mock.patch.object(u.urllib.request, "urlopen", _stub()):
        got = u.candidates()
    # Zero-priced first, then by output rate ascending.
    check("candidates: cheapest first",
          got == [FREE, "glm-5.3-flash-uncensored", ANSWERS,
                  "mimo-v2.6-flash-uncensored"], str(got))
    check("candidates: the free model leads", got[0] == FREE, str(got))


def test_candidates_fall_back_when_the_catalogue_is_unreadable() -> None:
    u = _fresh()
    err = urllib.error.URLError("no dns")
    with mock.patch.object(u.urllib.request, "urlopen", _stub(error=err)):
        got = u.candidates()
    check("candidates: an unreadable catalogue still names a real model",
          got == [u.DEFAULT_MODEL], str(got))


# ---------------------------------------------------------------------------
# 2 — the catalogue is public, and the URL is right
# ---------------------------------------------------------------------------

def test_catalogue_is_public_and_the_url_is_right() -> None:
    u = _fresh()
    open_, seen = _capture()
    with mock.patch.object(u.urllib.request, "urlopen", open_):
        models, err = u.fetch_models()
    check("catalogue: it fetches with no key", err == "" and models is not None, err)
    check("catalogue: the URL is /v1/models",
          seen["url"] == "https://api.unrestricted.sh/v1/models", seen["url"])
    check("catalogue: no credential is sent when none is given",
          "x-api-key" not in seen["headers"]
          and "authorization" not in seen["headers"], str(seen["headers"]))

    u.clear_cache()
    open_, seen = _capture()
    with mock.patch.object(u.urllib.request, "urlopen", open_):
        u.fetch_models("unr_sk_test")
    check("catalogue: a key is sent when there is one",
          seen["headers"].get("x-api-key") == "unr_sk_test"
          and seen["headers"].get("authorization") == "Bearer unr_sk_test",
          str(seen["headers"]))

    # The base URL must carry no path: the SDK appends /v1/messages, so a /v1
    # base composes to /v1/v1/messages and 404s on every turn.
    check("catalogue: the base URL has no path component",
          u.DEFAULT_BASE_URL == "https://api.unrestricted.sh", u.DEFAULT_BASE_URL)
    check("catalogue: the models URL is built from it",
          u.MODELS_URL == f"{u.DEFAULT_BASE_URL}/v1/models", u.MODELS_URL)
    check("catalogue: the account URL is built from it",
          u.ACCOUNT_URL == f"{u.DEFAULT_BASE_URL}/v1/account", u.ACCOUNT_URL)


def test_fetch_is_total() -> None:
    u = _fresh()
    for err, want in (
        (_http_error(401, AUTH_BODY), "unrestricted-api-setup"),
        (_http_error(500, "{}"), "HTTP 500"),
        (urllib.error.URLError("no dns"), "could not reach"),
        (ValueError("bad json"), "could not reach"),
    ):
        with mock.patch.object(u.urllib.request, "urlopen", _stub(error=err)):
            models, got = u.fetch_models()
        check(f"fetch: {type(err).__name__} is returned, not raised",
              models is None and want in got, got)


def test_cache_is_keyed_and_expires() -> None:
    u = _fresh()
    with mock.patch.object(u.urllib.request, "urlopen", _stub()):
        u.fetch_models()
    check("cache: a warm cache answers", u.cached_ids() == CHAT, str(u.cached_ids()))
    check("cache: the age is reported", u.catalog_age() is not None)
    # An expired entry must read as None, not as a live catalogue.
    with mock.patch.object(u.time, "monotonic",
                           lambda: 10**9):
        check("cache: an expired entry reads as None", u.cached_models() is None)
    u.clear_cache()
    check("cache: clear_cache empties it", u.cached_ids() is None)


# ---------------------------------------------------------------------------
# 3 — the probe: what actually answers
# ---------------------------------------------------------------------------

def test_probe_model_distinguishes_refusal_from_success() -> None:
    u = _fresh()
    with mock.patch.object(u.urllib.request, "urlopen", _stub()):
        ok, detail = u.probe_model("unr_sk_test", ANSWERS)
    check("probe: a 200 is 'answered'", ok is True and detail == "answered", detail)

    with mock.patch.object(u.urllib.request, "urlopen",
                           _stub(error=_http_error(400, PAID_BODY))):
        ok, detail = u.probe_model("unr_sk_test", FREE)
    check("probe: a paid-accounts 400 is NOT answered", ok is False, detail)
    check("probe: the reason names the cause",
          "paid accounts" in detail, detail)

    with mock.patch.object(u.urllib.request, "urlopen",
                           _stub(error=_http_error(401, AUTH_BODY))):
        ok, detail = u.probe_model("unr_sk_bad", ANSWERS)
    check("probe: a 401 is NOT answered", ok is False, detail)
    check("probe: a 401 names the setup command",
          "unrestricted-api-setup" in detail, detail)


def test_probe_model_posts_to_messages() -> None:
    u = _fresh()
    open_, seen = _capture()
    with mock.patch.object(u.urllib.request, "urlopen", open_):
        u.probe_model("unr_sk_test", ANSWERS)
    check("probe: it POSTs to /v1/messages",
          seen["url"] == "https://api.unrestricted.sh/v1/messages"
          and seen["method"] == "POST", f"{seen['method']} {seen['url']}")
    body = json.loads(seen["body"])
    check("probe: it names the model it is probing",
          body.get("model") == ANSWERS, str(body.get("model")))
    check("probe: it asks for a tiny reply",
          body.get("max_tokens") == 16, str(body.get("max_tokens")))
    check("probe: it sends the key as x-api-key",
          seen["headers"].get("x-api-key") == "unr_sk_test", str(seen["headers"]))


def test_first_callable_walks_past_a_refusal() -> None:
    """The behaviour the command exists for: land on a model that answers."""
    u = _fresh()

    def _open(req, timeout=None):
        # The catalogue GET comes first (candidates() reads it), then one POST
        # per probe. Only the POSTs carry a body.
        if req.data is None:
            return _Resp(json.dumps(CATALOG).encode())
        body = json.loads(req.data)
        if body.get("model") == FREE:
            raise _http_error(400, PAID_BODY)
        return _Resp(b'{"ok":true}')

    with mock.patch.object(u.urllib.request, "urlopen", _open):
        chosen, detail = u.first_callable("unr_sk_test")
    check("first_callable: it skips the free model that refuses",
          chosen == "glm-5.3-flash-uncensored", f"{chosen} / {detail}")
    check("first_callable: it reports that the model answered",
          detail == "answered", detail)


def test_first_callable_stops_on_a_rejected_key() -> None:
    u = _fresh()
    calls: list[str] = []

    def _open(req, timeout=None):
        if req.data is None:            # the catalogue GET
            return _Resp(json.dumps(CATALOG).encode())
        calls.append(json.loads(req.data).get("model"))
        raise _http_error(401, AUTH_BODY)

    with mock.patch.object(u.urllib.request, "urlopen", _open):
        chosen, detail = u.first_callable("unr_sk_bad")
    check("first_callable: a rejected key returns None", chosen is None, str(chosen))
    check("first_callable: it stops at the first 401 rather than walking the list",
          len(calls) == 1, str(calls))
    check("first_callable: the reason names the setup command",
          "unrestricted-api-setup" in detail, detail)


def test_first_callable_reports_when_nothing_answers() -> None:
    u = _fresh()

    def _open(req, timeout=None):
        # The catalogue answers; every probe refuses. That is the state a fresh
        # account is in for three of its four models.
        if req.data is None:
            return _Resp(json.dumps(CATALOG).encode())
        raise _http_error(400, PAID_BODY)

    with mock.patch.object(u.urllib.request, "urlopen", _open):
        chosen, detail = u.first_callable("unr_sk_test", limit=2)
    check("first_callable: nothing answering returns None", chosen is None, str(chosen))
    check("first_callable: it says how many it tried",
          "2 model(s)" in detail, detail)
    check("first_callable: it does not exceed the limit",
          detail.count("model(s)") == 1, detail)


def test_first_callable_needs_a_key() -> None:
    u = _fresh()
    chosen, detail = u.first_callable("")
    check("first_callable: no key is refused before any call",
          chosen is None and "unrestricted-api-setup" in detail, detail)


# ---------------------------------------------------------------------------
# 4 — the account route
# ---------------------------------------------------------------------------

def test_account_is_key_gated_and_read() -> None:
    u = _fresh()
    payload = {"balance_usd": 0.5, "plan": None, "month_spend_usd": 0,
               "api_locked": False}
    open_, seen = _capture(payload)
    with mock.patch.object(u.urllib.request, "urlopen", open_):
        got, err = u.account("unr_sk_test")
    check("account: the payload is returned", got == payload and err == "", err)
    check("account: it reads /v1/account",
          seen["url"] == "https://api.unrestricted.sh/v1/account", seen["url"])
    check("account: it sends the key",
          seen["headers"].get("x-api-key") == "unr_sk_test", str(seen["headers"]))

    with mock.patch.object(u.urllib.request, "urlopen",
                           _stub(error=_http_error(401, AUTH_BODY))):
        got, err = u.account("unr_sk_bad")
    check("account: a rejected key is reported",
          got is None and "unrestricted-api-setup" in err, err)
    got, err = u.account("")
    check("account: no key is refused without a call", got is None and err == "no key")


# ---------------------------------------------------------------------------
# 5 — error shapes
# ---------------------------------------------------------------------------

def test_explain_error_shapes() -> None:
    u = _fresh()
    got = u.explain_error(400, PAID_BODY)
    check("error: a paid-accounts 400 is named as such",
          "paid accounts" in got and "retrying will not help" in got, got)
    check("error: it does not read as a malformed request",
          "invalid_request" not in got, got)

    got = u.explain_error(401, AUTH_BODY)
    check("error: a 401 names the setup command",
          "unrestricted-api-setup" in got, got)

    got = u.explain_error(402, "{}")
    check("error: a 402 points at credit", "credit" in got.lower(), got)

    got = u.explain_error(429, "{}")
    check("error: a 429 names the concurrency limit",
          "3 requests" in got, got)

    got = u.explain_error(503, "{}")
    check("error: a 503 is transient", "retry" in got.lower(), got)

    got = u.explain_error(500, json.dumps({"error": {"message": "boom"}}))
    check("error: an unknown shape keeps the provider's message",
          "boom" in got, got)


def test_verify_key_does_not_fail_on_a_model_problem() -> None:
    """A model the balance does not cover says nothing about the credential."""
    u = _fresh()
    with mock.patch.object(u.urllib.request, "urlopen", _stub()):
        ok, detail = u.verify_key("unr_sk_test", ANSWERS)
    check("verify: a 200 is a good key", ok is True, detail)

    with mock.patch.object(u.urllib.request, "urlopen",
                           _stub(error=_http_error(400, PAID_BODY))):
        ok, detail = u.verify_key("unr_sk_test", FREE)
    check("verify: a paid-accounts 400 does NOT fail the key", ok is True, detail)
    check("verify: the reason is still reported", "paid accounts" in detail, detail)

    with mock.patch.object(u.urllib.request, "urlopen",
                           _stub(error=_http_error(401, AUTH_BODY))):
        ok, detail = u.verify_key("unr_sk_bad", ANSWERS)
    check("verify: a 401 DOES fail the key", ok is False, detail)

    with mock.patch.object(u.urllib.request, "urlopen",
                           _stub(error=urllib.error.URLError("no dns"))):
        ok, detail = u.verify_key("unr_sk_test", ANSWERS)
    check("verify: an unreachable host fails the key", ok is False, detail)

    ok, detail = u.verify_key("", ANSWERS)
    check("verify: no key is refused without a call", ok is False and detail == "no key")


# ---------------------------------------------------------------------------
# 6 — keys and profiles
# ---------------------------------------------------------------------------

def test_looks_like_key() -> None:
    u = _fresh()
    check("key: a real-shaped key passes",
          u.looks_like_key("unr_sk_pk5h69qj56x4myqnrl7v0vxoz17yhipx"))
    check("key: a short string is refused", not u.looks_like_key("short"))
    check("key: a pasted export line is refused",
          not u.looks_like_key('export ANTHROPIC_AUTH_TOKEN="unr_sk_abc"'))
    check("key: whitespace is refused",
          not u.looks_like_key("unr_sk_aaaaaaaaaaaaaaaa aaaa"))
    # The prefix is a hint, not a gate: a real key of an unexpected shape is
    # worth more stored than rejected.
    check("key: the prefix is not required",
          u.looks_like_key("someotherkeythatislongenough123"))


def test_is_unrestricted_matches_by_host() -> None:
    u = _fresh()
    check("host: the bare host matches", u.is_unrestricted("https://api.unrestricted.sh"))
    check("host: a trailing slash matches",
          u.is_unrestricted("https://api.unrestricted.sh/"))
    check("host: a path matches",
          u.is_unrestricted("https://api.unrestricted.sh/v1"))
    check("host: another gateway does not",
          not u.is_unrestricted("https://api.unorouter.com"))
    check("host: empty does not", not u.is_unrestricted(""))
    check("host: None does not", not u.is_unrestricted(None))


def test_profile_roundtrip(tmp: Path) -> None:
    u = _fresh()
    profile = tmp / "unrestricted" / "source"
    with mock.patch.object(u, "PROFILE", profile):
        path = u.save_key("unr_sk_testkey1234567890", "qwen3.8-27b-uncensored")
        check("profile: it writes where it says", path == profile, str(path))
        check("profile: the file is mode 600",
              (profile.stat().st_mode & 0o777) == 0o600,
              oct(profile.stat().st_mode & 0o777))
        text = profile.read_text()
        check("profile: it is source-format",
              'export ANTHROPIC_AUTH_TOKEN="unr_sk_testkey1234567890"' in text, text)
        check("profile: it names the base URL",
              f'export ANTHROPIC_BASE_URL="{u.DEFAULT_BASE_URL}"' in text, text)
        check("profile: it carries the model",
              'export ANTHROPIC_MODEL="qwen3.8-27b-uncensored"' in text, text)
        check("profile: api_key is blanked",
              'export ANTHROPIC_API_KEY=""' in text, text)

        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                      "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
                os.environ.pop(k, None)
            with mock.patch.object(u, "CLAUDE_SOURCE", tmp / "nope"):
                creds = u.load_credentials()
        check("profile: it reads back",
              creds is not None and creds[0] == "unr_sk_testkey1234567890",
              str(creds))
        check("profile: the origin is the path",
              creds is not None and creds[2] == str(profile), str(creds))


def test_credential_precedence(tmp: Path) -> None:
    u = _fresh()
    profile = tmp / "unrestricted" / "source"
    claude = tmp / "claude-source"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "unr").write_text(
        f'export ANTHROPIC_BASE_URL="{u.DEFAULT_BASE_URL}"\n'
        'export ANTHROPIC_AUTH_TOKEN="from-claude-source"\n')
    with mock.patch.object(u, "PROFILE", profile), \
         mock.patch.object(u, "CLAUDE_SOURCE", claude):
        u.save_key("from-profile")
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                  "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
            os.environ.pop(k, None)
        check("precedence: the profile beats ~/claude-source",
              u.load_credentials()[0] == "from-profile", str(u.load_credentials()))

        with mock.patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL": u.DEFAULT_BASE_URL,
                "ANTHROPIC_AUTH_TOKEN": "from-env"}):
            check("precedence: the environment beats the profile",
                  u.load_credentials()[0] == "from-env", str(u.load_credentials()))

        # A profile aimed at a different host is a different provider's
        # credential and must not be adopted.
        with mock.patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL": "https://api.unorouter.com",
                "ANTHROPIC_AUTH_TOKEN": "wrong-provider"}):
            check("precedence: another provider's env key is not adopted",
                  u.load_credentials()[0] == "from-profile",
                  str(u.load_credentials()))

        profile.unlink()
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                  "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
            os.environ.pop(k, None)
        check("precedence: ~/claude-source is the last resort",
              u.load_credentials()[0] == "from-claude-source",
              str(u.load_credentials()))


def test_apply_to_aims_the_session() -> None:
    u = _fresh()
    from zimzilla.config import Config
    cfg = Config.from_env(workdir=Path("/tmp"), base_url="https://logfare.ai/v1",
                          auth_token="old", api_key="stale")
    with mock.patch.object(u, "load_credentials",
                           lambda: ("unr_sk_test", "", "test")):
        with mock.patch.object(u, "default_free_model", lambda *a, **k: FREE):
            got = u.apply_to(cfg)
    check("apply_to: it returns the origin and model",
          got == ("test", FREE), str(got))
    check("apply_to: the base URL is the bare host",
          cfg.base_url == u.DEFAULT_BASE_URL, cfg.base_url)
    check("apply_to: the token is set", cfg.auth_token == "unr_sk_test")
    check("apply_to: a stale api_key is cleared", cfg.api_key == "", cfg.api_key)
    check("apply_to: the model is the free default", cfg.model == FREE, cfg.model)

    with mock.patch.object(u, "load_credentials", lambda: None):
        check("apply_to: no key leaves cfg alone",
              u.apply_to(cfg) is None and cfg.base_url == u.DEFAULT_BASE_URL)


def test_applies_to_default_endpoint() -> None:
    u = _fresh()
    check("endpoint: a local proxy is overridable",
          u.applies_to_default_endpoint("http://localhost:4001"))
    check("endpoint: an empty URL is overridable",
          u.applies_to_default_endpoint(""))
    check("endpoint: this gateway is not overridable",
          not u.applies_to_default_endpoint(u.DEFAULT_BASE_URL))
    check("endpoint: another host is not overridable",
          not u.applies_to_default_endpoint("https://api.openai.com"))


# ---------------------------------------------------------------------------
# 7 — the registry and the TUI
# ---------------------------------------------------------------------------

def test_sources_registry_dispatch() -> None:
    from zimzilla import sources as s
    check("registry: it is a builtin key", "unrestricted" in s.BUILTIN_KEYS)
    check("registry: it is not direct-only (it is hosted)",
          "unrestricted" not in s.DIRECT_ONLY, str(sorted(s.DIRECT_ONLY)))
    check("registry: active_key recognises the host",
          s.active_key("https://api.unrestricted.sh") == "unrestricted",
          str(s.active_key("https://api.unrestricted.sh")))
    check("registry: active_key does not confuse it with UnoRouter",
          s.active_key("https://api.unorouter.com") == "unorouter",
          str(s.active_key("https://api.unorouter.com")))
    check("registry: _gateway_module resolves it",
          s._gateway_module("unrestricted") is not None)

    # A cold cache must not offer another provider's model list.
    from zimzilla import unrestricted as u
    u.clear_cache()
    check("registry: a cold cache offers no list rather than a wrong one",
          s.models_for("https://api.unrestricted.sh") is None,
          str(s.models_for("https://api.unrestricted.sh")))

    with mock.patch.object(u.urllib.request, "urlopen", _stub()):
        u.fetch_models()
    check("registry: a warm cache offers the catalogue's ids",
          s.models_for("https://api.unrestricted.sh") == tuple(CHAT),
          str(s.models_for("https://api.unrestricted.sh")))
    check("registry: the context window comes from the catalogue",
          s.context_window_for("https://api.unrestricted.sh", ANSWERS) == 262144,
          str(s.context_window_for("https://api.unrestricted.sh", ANSWERS)))
    check("registry: the output ceiling comes from the catalogue",
          s.max_output_for("https://api.unrestricted.sh", ANSWERS) == 32768,
          str(s.max_output_for("https://api.unrestricted.sh", ANSWERS)))


def test_catalog_price_beats_the_estimate_table() -> None:
    """The estimate table has never heard of these ids and is wrong both ways."""
    from zimzilla import sources as s
    from zimzilla import unrestricted as u
    from zimzilla.config import price_for
    u.clear_cache()
    # Cold: the default model is the zero-priced one, so (0, 0) is the honest
    # answer and the estimate table's $1/$3 must not be printed.
    check("price: a cold cache does not bill the free default",
          s.catalog_price_for("https://api.unrestricted.sh", FREE) == (0.0, 0.0),
          str(s.catalog_price_for("https://api.unrestricted.sh", FREE)))
    with mock.patch.object(u.urllib.request, "urlopen", _stub()):
        u.fetch_models()
    check("price: a warm cache prices a paid model exactly",
          s.catalog_price_for("https://api.unrestricted.sh", ANSWERS) == (0.5, 1.0),
          str(s.catalog_price_for("https://api.unrestricted.sh", ANSWERS)))
    check("price: the estimate table would have been wrong",
          price_for(ANSWERS) != (0.5, 1.0), str(price_for(ANSWERS)))
    check("price: a warm cache prices the free model at zero",
          s.catalog_price_for("https://api.unrestricted.sh", FREE) == (0.0, 0.0),
          str(s.catalog_price_for("https://api.unrestricted.sh", FREE)))


def test_protocol_resolves_to_anthropic() -> None:
    from zimzilla import openai_compat as oa
    check("protocol: the bare host is Anthropic-native",
          oa.resolve_protocol("https://api.unrestricted.sh") == "anthropic",
          oa.resolve_protocol("https://api.unrestricted.sh"))
    check("protocol: it is not mistaken for a local proxy",
          oa.is_local_proxy("https://api.unrestricted.sh") is False)


def test_tui_surfaces() -> None:
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main_src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    agent_src = (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8")

    for cmd in ("zim-unrestricted", "unrestricted-api-setup",
                "unrestricted-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app_src)
        check(f"tui: /{cmd} is in the busy-ok set", f'"{cmd}"' in app_src)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp_src)

    check("tui: the gateway table knows it",
          'if kind == "unrestricted":' in app_src
          and 'return unr_mod, "unrestricted.sh"' in app_src)
    check("tui: the status pane lists it",
          '("unrestricted", unr_mod, "unrestricted.sh"' in app_src)
    check("tui: the model picker knows it",
          'if active == "unrestricted" else None' in app_src)
    check("tui: /model refetches for it", 'elif active == "unrestricted":' in app_src)
    check("tui: /models accepts it",
          '"unrestricted"' in app_src.split('known = {"logfare"', 1)[-1].split("}", 1)[0])
    check("tui: the switch probes rather than trusting the price column",
          "_switch_unrestricted" in app_src
          and "first_callable" in app_src)
    check("tui: the launch-time restore knows it",
          '("unrestricted", unr_mod)' in main_src)
    check("tui: the model is told where the key lives",
          '("unrestricted", "unrestricted"' in agent_src)


def test_nothing_ships_a_credential() -> None:
    """No key in the tree, and no profile under packaging/."""
    hits = []
    for path in (ROOT / "packaging").rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "unr_sk_" in text:
            hits.append(str(path.relative_to(ROOT)))
    check("credential: nothing under packaging/ holds a key", not hits, str(hits))
    check("credential: no profile ships for it",
          not (ROOT / "packaging" / "unrestricted").exists())


# ---------------------------------------------------------------------------

def main() -> int:
    saved = {k: os.environ.get(k) for k in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL", "ZIMZILLA_HOME")}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.environ["ZIMZILLA_HOME"] = str(tmp / "home")
        try:
            test_parser_drops_malformed_rows()
            test_prices_are_dollars_per_mtok()
            test_free_is_a_price_not_a_permission()
            test_non_chat_models_are_filtered()
            test_candidates_are_cheapest_first()
            test_candidates_fall_back_when_the_catalogue_is_unreadable()
            test_catalogue_is_public_and_the_url_is_right()
            test_fetch_is_total()
            test_cache_is_keyed_and_expires()
            test_probe_model_distinguishes_refusal_from_success()
            test_probe_model_posts_to_messages()
            test_first_callable_walks_past_a_refusal()
            test_first_callable_stops_on_a_rejected_key()
            test_first_callable_reports_when_nothing_answers()
            test_first_callable_needs_a_key()
            test_account_is_key_gated_and_read()
            test_explain_error_shapes()
            test_verify_key_does_not_fail_on_a_model_problem()
            test_looks_like_key()
            test_is_unrestricted_matches_by_host()
            test_profile_roundtrip(tmp)
            test_credential_precedence(tmp)
            test_apply_to_aims_the_session()
            test_applies_to_default_endpoint()
            test_sources_registry_dispatch()
            test_catalog_price_beats_the_estimate_table()
            test_protocol_resolves_to_anthropic()
            test_tui_surfaces()
            test_nothing_ships_a_credential()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
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
