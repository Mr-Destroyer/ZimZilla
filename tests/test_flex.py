"""Regression suite for the Flex playground source.

Run:  .venv/bin/python tests/test_flex.py

Protects what is particular about this provider — and it is the most particular
source in the tree, because it is a *playground* rather than an API:

* **It is unauthenticated.** The route answers 200 with no ``Authorization``
  header, with an expired one and with a bogus one. So there is no key to store,
  no ``/flex-api-setup`` command, and ``load_credentials`` returns an empty
  token on purpose. A test that demanded a key here would be testing a
  requirement the endpoint does not have.
* **The URL is the endpoint, not a base.** ``chat_completions_url`` must return
  it untouched; appending the usual ``/v1/chat/completions`` produces
  ``…/playground/chat/v1/chat/completions``, which answers 404.
* **The history is capped at 10 messages, system included, and the last message
  at 2000 characters.** Both are enforced by the endpoint with a 400, so the
  payload builder fits the request before it is sent — and the system message
  survives the trim, because dropping it changes the model's instructions
  rather than shortening the history.
* **Failures arrive as 200 with an SSE error frame.** A status check sees
  success. ``rate_limit`` has to become a 429 so the retry policy backs off and
  retries; ``generic`` has to become a 400 so it does not.
* **The reply is always SSE**, even for ``stream: false``, so the non-streaming
  path has to notice and read the stream rather than parse it as one object.
* **There are no tool calls.** A ``tools`` array is accepted and the model
  answers with a call-shaped string in ordinary content. The module says so
  rather than advertising a capability the route does not have.
* **The catalog is declared, not fetched.** There is no Models route to probe,
  so ``/model`` must offer the module's list and ``PaddleOCR-VL`` must be
  filtered out of it as a non-chat model.
* **A remembered selection survives a restart.** Without the launcher branch a
  session last used on Flex falls through to the Logfare profile.

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


#: The playground's published limits, as ``/api/playground/config`` returns them.
CONFIG = {"max_messages": 10, "max_message_chars": 2000}

#: A real SSE body from the route: a role frame, two content frames, a stop
#: frame, a trailing empty frame and ``[DONE]``. Copied from a live reply rather
#: than invented, because the trailing empty frame is the part a hand-written
#: fixture would leave out and the accumulator has to tolerate.
SSE_OK = (
    'data: {"choices": [{"index": 0, "delta": {"role": "assistant", '
    '"content": ""}, "finish_reason": null}]}\n\n'
    'data: {"choices": [{"index": 0, "delta": {"content": "OK"}, '
    '"finish_reason": null}]}\n\n'
    'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\n'
    'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": null}]}\n\n'
    'data: [DONE]\n\n'
)

#: The busy frame. Note the status: this arrives as a **200**.
SSE_BUSY = (
    'data: {"error": {"code": "rate_limit", "message": "The playground is busy '
    'right now. Please try again in a moment."}}\n\n'
)

#: The unknown-model frame, also a 200.
SSE_GENERIC = (
    'data: {"error": {"code": "generic", "message": "Something went wrong '
    'generating a response. Please try again."}}\n\n'
)


class _Resp(io.BytesIO):
    """A urlopen() result: a context manager that also reports a status."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub(body: str = SSE_OK, error=None):
    def _open(req, timeout=None):
        if error is not None:
            raise error
        return _Resp(body.encode())
    return _open


def _capture(body: str = SSE_OK, error=None):
    """A stub that records the request, so URL, method and body can be asserted."""
    seen: dict = {}

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = req.data
        if error is not None:
            raise error
        return _Resp(body.encode())

    return seen, _open


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "u", code, "err", {}, io.BytesIO(body.encode()))


def _clean_env():
    """Context manager that hides any Flex route in the environment."""
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
    return _C()


# ---------------------------------------------------------------------------
# the module
# ---------------------------------------------------------------------------


def test_route_is_the_endpoint_not_a_base() -> None:
    """The URL must not gain a ``/v1/chat/completions`` suffix."""
    from zimzilla import flex as fx
    from zimzilla import openai_compat as oa

    check("route: the declared URL is the chat endpoint",
          fx.API_BASE_URL == "https://eu.api.flex.ai/api/playground/chat")
    check("route: chat_completions_url returns it untouched",
          oa.chat_completions_url(fx.API_BASE_URL) == fx.API_BASE_URL,
          oa.chat_completions_url(fx.API_BASE_URL))
    check("route: a bare host is not mangled into a path",
          oa.chat_completions_url("https://eu.api.flex.ai")
          == "https://eu.api.flex.ai")
    check("route: the protocol resolves to openai",
          oa.resolve_protocol(fx.API_BASE_URL) == "openai")
    check("route: it is recognised as a playground",
          oa.is_playground(fx.API_BASE_URL))
    check("route: another direct source is not a playground",
          not oa.is_playground("https://api.inferbase.ai/v1"))
    # The regression this pins: appending the usual suffix 404s.
    check("route: no /v1/chat/completions is appended",
          "/v1/chat/completions" not in oa.chat_completions_url(fx.API_BASE_URL))


def test_no_credential_exists() -> None:
    """There is no key, and the module must not pretend there is one."""
    from zimzilla import flex as fx

    check("auth: looks_like_key is always False",
          fx.looks_like_key("anything") is False)
    check("auth: the shipped profile carries an empty token",
          'export ANTHROPIC_AUTH_TOKEN=""'
          in (ROOT / "packaging" / "flex" / "source").read_text(encoding="utf-8"))
    check("auth: the shipped profile names the route",
          fx.API_BASE_URL
          in (ROOT / "packaging" / "flex" / "source").read_text(encoding="utf-8"))
    with _clean_env():
        creds = fx.load_credentials()
        check("auth: the shipped profile is discovered", creds is not None)
        if creds is not None:
            check("auth: the token is empty, not a placeholder",
                  creds[0] == "", repr(creds[0]))
            check("auth: the model comes from the profile",
                  creds[1] == fx.DEFAULT_MODEL, creds[1])


def test_catalog_is_declared_not_fetched() -> None:
    """No Models route exists, so the list is a constant and never a fetch."""
    from zimzilla import flex as fx

    def _boom(*a, **k):
        raise AssertionError("the playground has no Models route to fetch")

    with mock.patch("urllib.request.urlopen", _boom):
        models, err = fx.fetch_models()
        ids, _ = fx.model_ids()
    check("catalog: fetch_models makes no network call", models is not None)
    check("catalog: it returns the declared twelve", len(models or []) == 12,
          str(len(models or [])))
    check("catalog: model_ids filters the OCR model out",
          "PaddleOCR-VL" not in (ids or []), str(ids))
    check("catalog: the chat ids are eleven", len(ids or []) == 11,
          str(len(ids or [])))
    check("catalog: the default model is in the list",
          fx.DEFAULT_MODEL in (ids or []))
    check("catalog: cached_ids is warm with no fetch",
          fx.cached_ids() == ids)
    check("catalog: catalog_age is 0.0 — a constant is never stale",
          fx.catalog_age() == 0.0)
    check("catalog: every entry is free",
          all(m.free for m in (models or [])))
    check("catalog: every entry plans against 32K",
          all(m.context_length == 32_768 for m in (models or [])))


def test_limits_are_the_published_ones() -> None:
    """The two caps are the endpoint's, and the module carries the same numbers."""
    from zimzilla import flex as fx
    from zimzilla import openai_compat as oa

    check("limits: 10 messages", fx.MAX_MESSAGES == 10)
    check("limits: 2000 characters", fx.MAX_MESSAGE_CHARS == 2000)
    check("limits: the transport agrees about messages",
          oa.PLAYGROUND_MAX_MESSAGES == fx.MAX_MESSAGES)
    check("limits: the transport agrees about characters",
          oa.PLAYGROUND_MAX_MESSAGE_CHARS == fx.MAX_MESSAGE_CHARS)
    check("limits: the window is 32K, not the 1M harness default",
          fx.DEFAULT_CONTEXT == 32_768)


def test_payload_fits_the_caps() -> None:
    """The builder trims history and the tail, and keeps the system message."""
    from zimzilla import openai_compat as oa

    msgs = [{"role": "system", "content": "be terse"}]
    msgs += [{"role": "user", "content": f"turn {i}"} for i in range(14)]
    payload = oa._playground_payload({"messages": list(msgs), "stream": False,
                                      "stream_options": {"include_usage": True}})
    out = payload["messages"]
    check("payload: history is trimmed to the cap", len(out) == 10, str(len(out)))
    check("payload: the system message survives the trim",
          out[0]["role"] == "system", out[0]["role"])
    check("payload: the newest turn survives the trim",
          out[-1]["content"] == "turn 13", out[-1]["content"])
    check("payload: stream is forced on", payload["stream"] is True)
    check("payload: stream_options is dropped", "stream_options" not in payload)

    long_tail = oa._playground_payload(
        {"messages": [{"role": "user", "content": "x" * 5000}]})
    content = long_tail["messages"][-1]["content"]
    check("payload: the last message is trimmed to the cap",
          len(content) == 2000, str(len(content)))
    check("payload: a trimmed message says so",
          content.endswith(oa.PLAYGROUND_TRIM_MARK))

    # A short request is left alone.
    short = oa._playground_payload(
        {"messages": [{"role": "user", "content": "hi"}]})
    check("payload: a short request is untouched",
          short["messages"][-1]["content"] == "hi")


def test_error_frames_map_to_statuses() -> None:
    """A 200 carrying an error frame is a failure, and its code decides which."""
    from zimzilla import openai_compat as oa

    busy = {"error": {"code": "rate_limit", "message": "busy"}}
    generic = {"error": {"code": "generic", "message": "nope"}}
    check("frames: rate_limit becomes 429 so the retry policy backs off",
          oa._frame_status(busy, 200) == 429)
    check("frames: generic becomes 400 so it does not retry",
          oa._frame_status(generic, 200) == 400)
    check("frames: a real error status is kept",
          oa._frame_status(generic, 400) == 400)
    check("frames: an unknown code is a bad request",
          oa._frame_status({"error": {"code": "weird"}}, 200) == 400)


def test_sse_body_is_read_by_the_non_streaming_path() -> None:
    """``create()`` must read an event stream rather than parse it as one object."""
    from zimzilla import openai_compat as oa

    check("sse: an event stream is recognised", oa._looks_like_sse(SSE_OK.encode()))
    check("sse: a JSON object is not",
          not oa._looks_like_sse(b'{"choices": []}'))
    acc = oa.Accumulator()
    oa._feed_sse_body(acc, SSE_OK.encode(), "u", 200, {})
    msg = acc.message()
    check("sse: the text is assembled",
          "".join(b.text for b in msg.content if b.type == "text") == "OK")
    check("sse: the stop reason is read", msg.stop_reason == "end_turn")

    # An error frame inside the body must raise, not return an empty reply.
    raised = None
    try:
        oa._feed_sse_body(oa.Accumulator(), SSE_BUSY.encode(), "u", 200, {})
    except Exception as e:  # noqa: BLE001 - the type is the assertion
        raised = e
    import anthropic
    check("sse: a busy frame raises RateLimitError",
          isinstance(raised, anthropic.RateLimitError), type(raised).__name__)
    raised = None
    try:
        oa._feed_sse_body(oa.Accumulator(), SSE_GENERIC.encode(), "u", 200, {})
    except Exception as e:  # noqa: BLE001
        raised = e
    check("sse: a generic frame raises BadRequestError",
          isinstance(raised, anthropic.BadRequestError), type(raised).__name__)


def test_retry_reads_the_endpoints_own_numbers() -> None:
    """A moved cap is read out of the 400's sentence, not assumed."""
    from zimzilla import openai_compat as oa

    check("retry: 'Maximum 10 messages allowed' parses",
          oa._count_limit_from("Maximum 10 messages allowed") == 10)
    check("retry: 'max 2000 characters' parses",
          oa._count_limit_from("Message content too long (max 2000 characters)")
          == 2000)
    check("retry: a sentence with no number parses to None",
          oa._count_limit_from("something else entirely") is None)

    # A payload over a *smaller* cap than the published one is shrunk to fit.
    payload = {"messages": [{"role": "user", "content": f"m{i}"} for i in range(8)]}
    stream = oa._Stream(oa.Client(base_url="https://eu.api.flex.ai/api/playground/chat"),
                        {})
    changed = stream._retry_payload(
        payload, b'{"detail":"Maximum 4 messages allowed"}')
    check("retry: an over-long history is shrunk", changed and len(payload["messages"]) == 4,
          str(len(payload["messages"])))

    payload = {"messages": [{"role": "user", "content": "x" * 500}]}
    changed = stream._retry_payload(
        payload, b'{"detail":"Message content too long (max 100 characters)"}')
    check("retry: an over-long tail is cut",
          changed and len(payload["messages"][-1]["content"]) == 100,
          str(len(payload["messages"][-1]["content"])))


def test_explain_error_shapes() -> None:
    """Each failure shape gets the sentence that says what to do about it."""
    from zimzilla import flex as fx

    busy = fx.explain_error(200, SSE_BUSY.replace("data: ", "").strip())
    check("errors: a busy frame says retry", "busy" in busy.lower(), busy[:60])
    generic = fx.explain_error(200, SSE_GENERIC.replace("data: ", "").strip())
    check("errors: a generic frame names the model list",
          "/flex-models" in generic, generic[:60])
    check("errors: a 401 is a route change, not a credential",
          "no key" in fx.explain_error(401, "").lower()
          or "needs no key" in fx.explain_error(401, "").lower(),
          fx.explain_error(401, ""))
    check("errors: a 404 names the endpoint",
          fx.API_BASE_URL in fx.explain_error(404, ""))
    check("errors: a message cap is named",
          "10 messages" in fx.explain_error(
              400, '{"detail":"Maximum 10 messages allowed"}'))
    check("errors: a character cap is named",
          "2000" in fx.explain_error(
              400, '{"detail":"Message content too long (max 2000 characters)"}'))


def test_verify_proves_the_route() -> None:
    """There is no key and no catalog, so a real chat call is the only proof."""
    from zimzilla import flex as fx

    seen, stub = _capture(SSE_OK)
    with mock.patch("urllib.request.urlopen", stub):
        ok, detail = fx.verify()
    check("verify: a chat frame is success", ok, detail)
    check("verify: it posts to the chat endpoint", seen["url"] == fx.API_BASE_URL,
          seen["url"])
    check("verify: it sends no Authorization header",
          "authorization" not in seen["headers"], str(seen["headers"]))
    body = json.loads(seen["body"])
    check("verify: it asks for a tiny reply", body["max_tokens"] == 8)

    with mock.patch("urllib.request.urlopen", _stub(SSE_BUSY)):
        ok, detail = fx.verify()
    check("verify: a busy playground is not a working route", ok is False, detail)
    check("verify: the busy reason is the endpoint's own",
          "busy" in detail.lower(), detail[:60])

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("no route"))):
        ok, detail = fx.verify()
    check("verify: an unreachable host is a failure", ok is False)
    check("verify: the host is named", "eu.api.flex.ai" in detail, detail[:60])


def test_config_reads_the_published_limits() -> None:
    """The limits are read from the endpoint, not only hardcoded here."""
    from zimzilla import flex as fx

    seen, stub = _capture(json.dumps(CONFIG))
    with mock.patch("urllib.request.urlopen", stub):
        got = fx.config()
    check("config: the limits are read", got == CONFIG, str(got))
    check("config: it reads the config route", seen["url"] == fx.CONFIG_URL,
          seen["url"])
    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        check("config: an unreachable route is None", fx.config() is None)


def test_the_agent_compacts_before_the_wire_trims() -> None:
    """A capped route must compact on message count, not wait for a token wall.

    The failure this pins is silent: the transport trims the history to fit the
    playground's ten-message cap, so without a count trigger the oldest turns
    would vanish at the wire with no compaction event and no transcript line.
    Ten messages of 2000 characters never approach a 32K window, so the token
    threshold would never fire either.
    """
    from zimzilla import sources as s
    from zimzilla.agent import Agent
    from zimzilla.config import Config

    check("cap: the registry reports the playground's ten",
          s.message_cap_for("https://eu.api.flex.ai/api/playground/chat") == 10)
    check("cap: an uncapped route reports None",
          s.message_cap_for("https://api.inferbase.ai/v1") is None)

    agent = Agent(Config(base_url="https://eu.api.flex.ai/api/playground/chat",
                         model="DeepSeek-V4-Flash-0731"))
    check("cap: the agent reads it", agent.message_cap() == 10)
    # Below the cap, with no measured tokens, nothing compacts.
    agent.messages = [{"role": "user", "content": "x"},
                      {"role": "assistant", "content": "y"}] * 2
    check("cap: a short history does not compact",
          agent._should_compact(32_768) is False)
    # At the cap it must, even though the token count is zero.
    agent.messages = [{"role": "user", "content": "x"},
                      {"role": "assistant", "content": "y"}] * 4
    check("cap: a full history compacts on count alone",
          agent._should_compact(32_768) is True)
    # An uncapped route is unaffected: no count trigger, and no measured tokens
    # means no compaction.
    other = Agent(Config(base_url="https://api.inferbase.ai/v1", model="auto"))
    other.messages = [{"role": "user", "content": "x"},
                      {"role": "assistant", "content": "y"}] * 4
    check("cap: an uncapped route is not compacted on count",
          other._should_compact(32_768) is False)


def test_usage_falls_back_to_the_estimate() -> None:
    """A route that sends no usage block must still feed the compaction check."""
    agent_src = (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8")
    check("usage: the measured figure falls back to the estimate",
          "self._last_input_tokens = tin or self.history_tokens()" in agent_src)
    check("usage: the reason is stated where it is done",
          "playground sends no usage block" in agent_src)


def test_is_flex_matches_by_host() -> None:
    from zimzilla import flex as fx

    check("host: the playground matches", fx.is_flex(fx.API_BASE_URL))
    check("host: a bare host matches", fx.is_flex("https://eu.api.flex.ai"))
    check("host: another host does not",
          not fx.is_flex("https://api.inferbase.ai/v1"))
    check("host: None does not", not fx.is_flex(None))
    check("host: the generic alias is the same test",
          fx.active_endpoint is fx.is_flex)


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_sources_registry_dispatch() -> None:
    """Every dispatch in sources.py knows flex, and none of them fetches."""
    from zimzilla import sources as s

    check("registry: it is direct-only", "flex" in s.DIRECT_ONLY)
    check("registry: it has a declared route", "flex" in s.DECLARED_ROUTES)
    check("registry: the route is the chat endpoint",
          s.DECLARED_ROUTES["flex"].base_url
          == "https://eu.api.flex.ai/api/playground/chat")
    check("registry: it has a declared catalog", "flex" in s.MODELS_BY_KEY)
    check("registry: it is a builtin key", "flex" in s.BUILTIN_KEYS)
    check("registry: active_key identifies it",
          s.active_key("https://eu.api.flex.ai/api/playground/chat") == "flex")
    check("registry: models_for offers the chat ids",
          "PaddleOCR-VL" not in (s.models_for(
              "https://eu.api.flex.ai/api/playground/chat") or ()))
    check("registry: catalog_models answers",
          s.catalog_models("flex") is not None)
    check("registry: the price is free, warm or cold",
          s.catalog_price_for("https://eu.api.flex.ai/api/playground/chat",
                              "DeepSeek-V4-Flash-0731") == (0.0, 0.0))
    check("registry: the window is 32K",
          s.context_window_for("https://eu.api.flex.ai/api/playground/chat",
                               "DeepSeek-V4-Flash-0731") == 32_768)
    check("registry: the display name is unchanged",
          s.display_model("https://eu.api.flex.ai/api/playground/chat",
                          "DeepSeek-V4-Flash-0731") == "DeepSeek-V4-Flash-0731")


def test_discovery_needs_no_key() -> None:
    """The profile ships, so the source is present on a fresh clone."""
    from zimzilla import sources as s

    found = s.discover()
    check("discovery: flex is discovered", "flex" in found, str(sorted(found)))
    if "flex" in found:
        src = found["flex"]
        check("discovery: it is direct-only", src.is_direct_only)
        check("discovery: it has no port", src.port == 0)
        check("discovery: its route is the playground",
              s.direct_route(src).base_url
              == "https://eu.api.flex.ai/api/playground/chat")
        check("discovery: its default model is DeepSeek",
              src.model == "DeepSeek-V4-Flash-0731")
    check("discovery: it is offered as a switch target",
          "flex" in s.switchable())


def test_tui_surfaces() -> None:
    """The commands exist, are documented, and the switch has its own branch."""
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")

    check("tui: /zim-flex is dispatched",
          '"zim-flex": lambda a: self._cmd_zim_source("flex", a)' in app_src)
    check("tui: /flex-models is dispatched",
          '"flex-models": lambda a: self._cmd_models(["flex"])' in app_src)
    check("tui: there is no /flex-api-setup, because there is no key",
          "flex-api-setup" not in app_src)
    check("tui: /zim-flex is in the help reference", "/zim-flex" in app_src)
    check("tui: /flex-models is in the help reference", "/flex-models" in app_src)
    check("tui: /zim-flex is in the completion list", "/zim-flex" in comp_src)
    check("tui: /flex-models is in the completion list", "/flex-models" in comp_src)
    check("tui: flex is offered as a /models argument",
          '"flex"' in comp_src)
    check("tui: the switch tolerates an empty token for flex",
          'src.key != "flex"' in app_src)
    check("tui: the probe has a flex branch",
          'elif src.key == "flex":' in app_src)
    check("tui: the 401 hint says there is no key to replace",
          "the Flex playground takes no key" in app_src)
    check("tui: the models screen explains the three limits",
          "no tool calls" in app_src and "at most" in app_src)


def test_launcher_restores_the_route() -> None:
    """A remembered Flex selection must survive a restart."""
    launcher = (ROOT / "packaging" / "zimzilla").read_text(encoding="utf-8")

    check("launcher: it has its own branch",
          'if [[ "$_current_source" == "flex" ]]' in launcher)
    check("launcher: it prefers the operator's profile",
          '_fx_profile="$ZIMZILLA_HOME/flex/source"' in launcher)
    check("launcher: it falls back to the shipped one",
          'packaging/flex/source' in launcher)
    start = launcher.index('if [[ "$_current_source" == "flex" ]]')
    # The branch ends at the next top-level `fi`, not at the first
    # LITELLM_ENV_FILE= — that assignment is *inside* the branch, before the
    # proxy flag, so slicing there would cut the assertion off from the line it
    # is looking for.
    end = launcher.index("\nfi\n", start)
    branch = launcher[start:end]
    check("launcher: the branch turns the proxy off",
          "ZIMZILLA_NO_PROXY=1" in branch)
    check("launcher: the branch is guarded on the profile existing",
          '[[ -r "$_fx_profile" ]]' in branch)


def test_nothing_ships_a_credential() -> None:
    """The shipped profile is a route record, and it holds no secret."""
    text = (ROOT / "packaging" / "flex" / "source").read_text(encoding="utf-8")
    check("packaging: the token line is empty",
          'export ANTHROPIC_AUTH_TOKEN=""' in text)
    check("packaging: the api key line is empty",
          'export ANTHROPIC_API_KEY=""' in text)
    check("packaging: it says it is not a credential",
          "NOT a credential" in text)
    # No long opaque string anywhere in the file.
    import re
    check("packaging: no key-shaped string is present",
          not re.search(r"[A-Za-z0-9_\-]{32,}", text.replace(
              "https://eu.api.flex.ai/api/playground/chat", "")))


# ---------------------------------------------------------------------------


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zimzilla-flex-"))
    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "ANTHROPIC_MODEL")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        test_route_is_the_endpoint_not_a_base()
        test_no_credential_exists()
        test_catalog_is_declared_not_fetched()
        test_limits_are_the_published_ones()
        test_payload_fits_the_caps()
        test_error_frames_map_to_statuses()
        test_sse_body_is_read_by_the_non_streaming_path()
        test_retry_reads_the_endpoints_own_numbers()
        test_explain_error_shapes()
        test_verify_proves_the_route()
        test_config_reads_the_published_limits()
        test_the_agent_compacts_before_the_wire_trims()
        test_usage_falls_back_to_the_estimate()
        test_is_flex_matches_by_host()
        test_sources_registry_dispatch()
        test_discovery_needs_no_key()
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
