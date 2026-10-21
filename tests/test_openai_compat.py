"""Regression suite for the native OpenAI-compatible transport.

Run:  python tests/test_openai_compat.py   (from an activated venv)

The harness grew up on the Anthropic Messages protocol, with a local LiteLLM
proxy translating every request to Chat Completions. That hop costs latency and
loses tool-call fidelity, so ``zimzilla/openai_compat.py`` speaks Chat
Completions directly. This file protects what makes that safe:

* **Protocol resolution is conservative.** An endpoint nobody recognises keeps
  the protocol it always had (Anthropic). A localhost proxy stays Anthropic even
  though a local vLLM on ``/v1`` is not; the path rule is checked first for
  exactly that reason.
* **Translation is lossless where it matters.** ``tool_use`` -> ``tool_calls``
  with the call ``id`` preserved, ``tool_result`` -> one ``role: tool`` message
  per call, and thinking blocks dropped rather than echoed back at servers that
  reject them.
* **Tool calls survive streaming.** Fragmented argument deltas are assembled by
  ``index``, so two parallel calls stay two calls — the failure mode that reads
  as "the model cannot use tools".
* **Errors arrive as ``anthropic`` exceptions.** ``_friendly_error`` and the
  retry policy dispatch on ``isinstance``; a private exception type would turn a
  429 with a reset header into a traceback.
* **The agent loop is unchanged.** A full turn — tool call, execution, result,
  second reply — runs end to end over the new transport with the same events.

No test here touches the network: every request is served by an
``httpx2.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
import httpx2  # noqa: E402

from zimzilla import openai_compat as oc  # noqa: E402
from zimzilla import sources as sources_mod  # noqa: E402
from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _sse(*chunks: dict | str) -> bytes:
    """Render chunks as an SSE body, terminated the way providers do it."""
    lines = []
    for chunk in chunks:
        data = chunk if isinstance(chunk, str) else json.dumps(chunk)
        lines.append(f"data: {data}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> dict:
    body: dict = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _client(handler, base_url: str = "https://api.example.com/v1", key: str = "sk-test") -> oc.Client:
    return oc.Client(base_url, api_key=key,
                     transport=httpx2.MockTransport(handler), timeout=30.0)


async def _collect(client: oc.Client, **kwargs):
    """Run one streamed call; return (events, final message, request bodies)."""
    events = []
    async with client.messages.stream(**kwargs) as stream:
        async for event in stream:
            events.append(event)
        final = await stream.get_final_message()
    return events, final


# ---------------------------------------------------------------------------
# 1 — protocol resolution
# ---------------------------------------------------------------------------


def test_protocol_resolution() -> None:
    cases = [
        ("http://localhost:4001", None, "anthropic",
         "a local LiteLLM proxy speaks Messages"),
        ("http://127.0.0.1:4000", None, "anthropic",
         "loopback with no /v1 is a proxy"),
        ("http://localhost:8000/v1", None, "openai",
         "a local vLLM on /v1 is not a proxy"),
        ("https://llm.onerouter.pro/v1", None, "openai", "/v1 path"),
        ("https://api.iteracompute.com/v1", None, "openai", "/v1 path"),
        ("https://api.openai.com", None, "openai", "known Chat Completions host"),
        ("https://openrouter.ai/api", None, "anthropic",
         "a hosted Messages gateway"),
        ("https://api.unorouter.com", None, "anthropic",
         "a hosted Messages gateway"),
        ("https://some-new-gateway.example", None, "anthropic",
         "unknown host keeps the old behaviour"),
        ("http://localhost:4001", "openai", "openai",
         "an explicit override wins over the heuristic"),
        ("https://api.openai.com/v1", "anthropic", "anthropic",
         "an explicit override wins the other way too"),
    ]
    for url, override, expected, why in cases:
        got = oc.resolve_protocol(url, override)
        check(f"protocol: {url} ({override or 'auto'}) -> {expected}", got == expected,
              f"got {got} — {why}")

    with mock.patch.dict(os.environ, {"ZIMZILLA_PROTOCOL": "openai"}):
        got = oc.resolve_protocol("https://unknown.example", "auto")
        check("protocol: ZIMZILLA_PROTOCOL pins an unknown host", got == "openai", got)
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ZIMZILLA_PROTOCOL", None)
        got = oc.resolve_protocol("https://unknown.example", None)
        check("protocol: with no env, an unknown host is Anthropic",
              got == "anthropic", got)


def test_urls() -> None:
    check("url: /v1 base gains only the route",
          oc.chat_completions_url("https://api.example.com/v1")
          == "https://api.example.com/v1/chat/completions",
          oc.chat_completions_url("https://api.example.com/v1"))
    check("url: a bare host gains /v1/chat/completions",
          oc.chat_completions_url("https://api.example.com")
          == "https://api.example.com/v1/chat/completions",
          oc.chat_completions_url("https://api.example.com"))
    check("url: a full endpoint is left alone",
          oc.chat_completions_url("https://host/openai/chat/completions")
          == "https://host/openai/chat/completions",
          oc.chat_completions_url("https://host/openai/chat/completions"))
    check("url: a trailing slash does not double up",
          oc.chat_completions_url("https://host/v1/")
          == "https://host/v1/chat/completions",
          oc.chat_completions_url("https://host/v1/"))
    check("url: models route is derived from the same base",
          oc.models_url("https://host/v1") == "https://host/v1/models",
          oc.models_url("https://host/v1"))
    check("url: a full chat endpoint yields the models route",
          oc.models_url("https://host/v1/chat/completions")
          == "https://host/v1/models",
          oc.models_url("https://host/v1/chat/completions"))


def test_local_proxy() -> None:
    check("local proxy: localhost:4001 is one", oc.is_local_proxy("http://localhost:4001"))
    check("local proxy: localhost:8000/v1 is not",
          not oc.is_local_proxy("http://localhost:8000/v1"))
    check("local proxy: a remote host is not",
          not oc.is_local_proxy("https://api.openai.com"))


# ---------------------------------------------------------------------------
# 2 — message translation
# ---------------------------------------------------------------------------


def test_message_translation() -> None:
    history = [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "I should read it."},
            {"type": "text", "text": "Reading now."},
            {"type": "tool_use", "id": "toolu_1", "name": "read_file",
             "input": {"path": "a.txt"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": [{"type": "text", "text": "hello"}]},
        ]},
    ]
    out = oc.translate_messages("SYSTEM", history)

    check("translate: system leads", out[0] == {"role": "system", "content": "SYSTEM"},
          str(out[0]))
    check("translate: the user text is a plain message",
          out[1] == {"role": "user", "content": "read the file"}, str(out[1]))

    assistant = out[2]
    check("translate: assistant text is kept", assistant["content"] == "Reading now.",
          str(assistant))
    calls = assistant.get("tool_calls") or []
    check("translate: tool_use becomes a tool_call", len(calls) == 1, str(calls))
    check("translate: the call id is preserved",
          calls and calls[0]["id"] == "toolu_1", str(calls))
    check("translate: the arguments are JSON",
          calls and json.loads(calls[0]["function"]["arguments"]) == {"path": "a.txt"},
          str(calls))
    check("translate: thinking is NOT echoed back",
          "thinking" not in json.dumps(assistant), json.dumps(assistant))

    tool = out[3]
    check("translate: tool_result becomes role:tool",
          tool.get("role") == "tool", str(tool))
    check("translate: it carries the matching tool_call_id",
          tool.get("tool_call_id") == "toolu_1", str(tool))
    check("translate: its text content is flattened",
          tool.get("content") == "hello", str(tool))
    check("translate: no extra user message is invented", len(out) == 4, str(len(out)))

    # An error result must say so: the model reads this text, and "Error:" is
    # the only signal OpenAI's schema has for a failed tool.
    err = oc.translate_messages("", [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "bash", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "boom",
             "is_error": True}]},
    ])
    check("translate: a failed tool result is prefixed",
          err[-1].get("content") == "Error: boom", str(err[-1]))

    # Two consecutive user messages are merged: some strict servers reject
    # them, and this harness can produce them (a notice folds into a turn).
    merged = oc.translate_messages("", [
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ])
    check("translate: consecutive user messages merge", len(merged) == 1, str(merged))
    check("translate: and both survive the merge",
          merged and merged[0]["content"] == "one\n\ntwo", str(merged))

    # An assistant turn with only thinking is not representable; leaving it in
    # would send content=None with no tool_calls, which OpenAI rejects.
    empty = oc.translate_messages("", [
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}]},
    ])
    check("translate: a thinking-only assistant turn is dropped", empty == [], str(empty))

    # Images cannot be carried over this protocol; say so rather than dropping
    # the turn's only content silently.
    image = oc.translate_messages("", [
        {"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image", "source": {"type": "base64"}}]},
    ])
    check("translate: an image is noted, not silently dropped",
          "image omitted" in image[0]["content"], str(image))


def test_tool_translation() -> None:
    schemas = [{
        "name": "bash",
        "description": "run a command",
        "input_schema": {"type": "object",
                         "properties": {"command": {"type": "string"}},
                         "required": ["command"]},
    }, {
        "name": "broken",
        "description": "",
        "input_schema": {"type": "string"},
    }]
    tools = oc.translate_tools(schemas)
    check("tools: one function tool per schema", len(tools) == 2, str(len(tools)))
    first = tools[0]
    check("tools: wrapped as type=function", first["type"] == "function", str(first))
    check("tools: the name and description carry over",
          first["function"]["name"] == "bash"
          and first["function"]["description"] == "run a command", str(first))
    check("tools: input_schema becomes parameters",
          first["function"]["parameters"]["required"] == ["command"], str(first))
    check("tools: a non-object schema is replaced, not forwarded",
          tools[1]["function"]["parameters"] == {"type": "object", "properties": {}},
          str(tools[1]))


def test_arguments() -> None:
    check("args: plain JSON parses", oc.parse_arguments('{"a": 1}') == {"a": 1})
    check("args: a fenced block parses",
          oc.parse_arguments('```json\n{"a": 1}\n```') == {"a": 1},
          str(oc.parse_arguments('```json\n{"a": 1}\n```')))
    check("args: prose around the object is tolerated",
          oc.parse_arguments('here you go: {"a": 1} — done') == {"a": 1},
          str(oc.parse_arguments('here you go: {"a": 1} — done')))
    check("args: truncated JSON becomes empty rather than raising",
          oc.parse_arguments('{"a": ') == {}, str(oc.parse_arguments('{"a": ')))
    check("args: a JSON array is not a tool input",
          oc.parse_arguments('[1,2]') == {}, str(oc.parse_arguments('[1,2]')))
    check("args: empty is empty", oc.parse_arguments("") == {})


# ---------------------------------------------------------------------------
# 3 — streaming, tool-call assembly and usage
# ---------------------------------------------------------------------------


def test_stream_assembly() -> None:
    captured: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        body = _sse(
            _chunk({"reasoning_content": "thinking..."}),
            _chunk({"content": "Let me "}),
            _chunk({"content": "look."}),
            # Two parallel calls, arguments fragmented across chunks — the
            # shape a proxy most often mangles.
            _chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                    "function": {"name": "read_file",
                                                 "arguments": '{"pa'}}]}),
            _chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                                    "function": {"name": "bash",
                                                 "arguments": '{"comm'}}]}),
            _chunk({"tool_calls": [{"index": 0,
                                    "function": {"arguments": 'th": "a.txt"}'}}]}),
            _chunk({"tool_calls": [{"index": 1,
                                    "function": {"arguments": 'and": "ls"}'}}]}),
            _chunk({}, finish="tool_calls"),
            {"choices": [], "usage": {"prompt_tokens": 123, "completion_tokens": 45}},
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=body)

    client = _client(handler)
    events, final = asyncio.run(_collect(
        client,
        model="m",
        max_tokens=1000,
        system="SYS",
        messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "bash", "description": "", "input_schema":
                {"type": "object", "properties": {}}}],
        extra_body={"temperature": 0.0},
    ))

    text = "".join(e.delta.text for e in events
                   if e.delta.type == "text_delta")
    check("stream: text deltas are forwarded in order", text == "Let me look.", text)
    reasoning = "".join(e.delta.thinking for e in events
                        if e.delta.type == "thinking_delta")
    check("stream: reasoning_content becomes a thinking delta",
          reasoning == "thinking...", reasoning)
    check("stream: the final message has a text block",
          any(b.type == "text" and b.text == "Let me look." for b in final.content),
          str(final.content))
    calls = [b for b in final.content if b.type == "tool_use"]
    check("stream: both parallel calls survived", len(calls) == 2, str(len(calls)))
    check("stream: the first call keeps its id and name",
          calls and calls[0].id == "call_a" and calls[0].name == "read_file",
          str(calls[0]) if calls else "")
    check("stream: its fragmented arguments were assembled",
          calls and calls[0].input == {"path": "a.txt"},
          str(calls[0].input) if calls else "")
    check("stream: the second call was not merged into the first",
          len(calls) == 2 and calls[1].input == {"command": "ls"},
          str(calls[1].input) if len(calls) == 2 else "")
    check("stream: finish_reason tool_calls maps to stop_reason tool_use",
          final.stop_reason == "tool_use", str(final.stop_reason))
    check("stream: usage is read from the final chunk",
          final.usage.input_tokens == 123 and final.usage.output_tokens == 45,
          f"{final.usage.input_tokens}/{final.usage.output_tokens}")

    sent = captured[0]
    check("request: the model is sent", sent.get("model") == "m", str(sent.get("model")))
    check("request: streaming is requested", sent.get("stream") is True, str(sent.get("stream")))
    check("request: usage is requested on the stream",
          sent.get("stream_options") == {"include_usage": True},
          str(sent.get("stream_options")))
    check("request: max_tokens is sent as max_tokens", sent.get("max_tokens") == 1000,
          str(sent.get("max_tokens")))
    check("request: temperature comes from extra_body",
          sent.get("temperature") == 0.0, str(sent.get("temperature")))
    check("request: the system prompt leads the messages",
          sent["messages"][0] == {"role": "system", "content": "SYS"},
          str(sent["messages"][:1]))
    check("request: tools are Chat Completions functions",
          sent.get("tools", [{}])[0].get("type") == "function", str(sent.get("tools")))


def test_stream_length_and_empty() -> None:
    def handler(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=_sse(_chunk({"content": "cut"}, finish="length")))

    _, final = asyncio.run(_collect(_client(handler), model="m", max_tokens=5,
                                    messages=[{"role": "user", "content": "x"}]))
    check("stream: finish_reason length maps to max_tokens",
          final.stop_reason == "max_tokens", str(final.stop_reason))

    def reasoning_only(request):
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_sse(_chunk({"reasoning_content": "hmm"}, finish="length"),
                         {"choices": [], "usage": {"prompt_tokens": 9,
                                                   "completion_tokens": 5}}))

    _, final = asyncio.run(_collect(_client(reasoning_only), model="m",
                                    max_tokens=5,
                                    messages=[{"role": "user", "content": "x"}]))
    check("stream: a reasoning-only reply has no visible blocks",
          final.content == [], str(final.content))
    check("stream: and is still reported as truncated",
          final.stop_reason == "max_tokens", str(final.stop_reason))


def test_create() -> None:
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        body = {"choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "summary text"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
        return httpx2.Response(200, json=body)

    client = _client(handler)
    message = asyncio.run(client.messages.create(
        model="m", max_tokens=100, messages=[{"role": "user", "content": "hi"}]))
    check("create: returns one text block",
          [b.text for b in message.content if b.type == "text"] == ["summary text"],
          str(message.content))
    check("create: usage is read", message.usage.input_tokens == 7
          and message.usage.output_tokens == 3, str(message.usage))
    check("create: it is not streamed", "stream" not in captured[0],
          str(captured[0].keys()))


# ---------------------------------------------------------------------------
# 4 — errors and parameter negotiation
# ---------------------------------------------------------------------------


def test_errors() -> None:
    def handler(request):
        return httpx2.Response(
            429, headers={"retry-after": "120"},
            json={"error": {"message": "Hourly rate limit reached", "type": "rate_limit"}})

    client = _client(handler)
    try:
        asyncio.run(_collect(client, model="m", messages=[{"role": "user", "content": "x"}]))
    except anthropic.RateLimitError as e:
        check("error: a 429 is an anthropic.RateLimitError", True)
        check("error: the provider's message survives",
              "Hourly rate limit" in str(e), str(e))
        check("error: the retry-after header survives, so the window is detected",
              e.response.headers.get("retry-after") == "120",
              str(dict(e.response.headers)))
        from zimzilla.agent import _rate_limit_is_a_window
        check("error: the harness reads it as a window, not a burst",
              _rate_limit_is_a_window(e))
    except Exception as e:  # noqa: BLE001
        check("error: a 429 is an anthropic.RateLimitError", False, type(e).__name__)

    def bad(request):
        return httpx2.Response(400, json={"error": {"message": "bad tool schema"}})

    try:
        asyncio.run(_collect(_client(bad), model="m",
                             messages=[{"role": "user", "content": "x"}]))
    except anthropic.BadRequestError as e:
        check("error: a 400 is an anthropic.BadRequestError", True)
        check("error: the body is readable by _friendly_error",
              "bad tool schema" in str(e), str(e))
    except Exception as e:  # noqa: BLE001
        check("error: a 400 is an anthropic.BadRequestError", False, type(e).__name__)

    def auth(request):
        return httpx2.Response(401, json={"error": {"message": "invalid api key"}})

    try:
        asyncio.run(_collect(_client(auth), model="m",
                             messages=[{"role": "user", "content": "x"}]))
    except anthropic.AuthenticationError:
        check("error: a 401 is an anthropic.AuthenticationError", True)
    except Exception as e:  # noqa: BLE001
        check("error: a 401 is an anthropic.AuthenticationError", False, type(e).__name__)


def test_max_tokens_negotiation() -> None:
    """A model that only knows max_completion_tokens is retried, not failed."""
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx2.Response(400, json={"error": {
                "message": "Unsupported parameter: 'max_tokens' is not supported "
                           "with this model. Use 'max_completion_tokens' instead."}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=_sse(_chunk({"content": "ok"}, finish="stop")))

    events, final = asyncio.run(_collect(_client(handler), model="m", max_tokens=64,
                                         messages=[{"role": "user", "content": "x"}]))
    check("retry: the request was sent twice", len(bodies) == 2, str(len(bodies)))
    check("retry: the second used max_completion_tokens",
          bodies[1].get("max_completion_tokens") == 64 and "max_tokens" not in bodies[1],
          str(bodies[1]))
    check("retry: the turn still completed",
          [b.text for b in final.content if b.type == "text"] == ["ok"],
          str(final.content))


def test_output_ceiling_clamp() -> None:
    """A ceiling the model cannot accept is clamped to what it named."""
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("max_tokens", 0) > 16384:
            return httpx2.Response(400, json={"error": {
                "message": "max_tokens is too large: 32768. This model supports "
                           "at most 16384 completion tokens."}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=_sse(_chunk({"content": "ok"}, finish="stop")))

    _, final = asyncio.run(_collect(_client(handler), model="m", max_tokens=32768,
                                    messages=[{"role": "user", "content": "x"}]))
    check("clamp: the request was retried", len(bodies) == 2, str(len(bodies)))
    check("clamp: at the limit the provider named",
          bodies[1].get("max_tokens") == 16384, str(bodies[1].get("max_tokens")))
    check("clamp: and the turn completed",
          [b.text for b in final.content if b.type == "text"] == ["ok"],
          str(final.content))


def test_stream_fallback() -> None:
    """A server that refuses streaming is answered non-streamed, not failed."""
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream"):
            return httpx2.Response(400, json={"error": {
                "message": "streaming is not supported by this model"}})
        return httpx2.Response(200, json={"choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "whole reply"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    events, final = asyncio.run(_collect(_client(handler), model="m",
                                         messages=[{"role": "user", "content": "x"}]))
    check("fallback: it retried without streaming", len(bodies) == 2, str(len(bodies)))
    check("fallback: the reply is still streamed to the UI as one delta",
          "".join(e.delta.text for e in events if e.delta.type == "text_delta")
          == "whole reply", str([e.delta for e in events]))
    check("fallback: and the final message is the whole reply",
          [b.text for b in final.content if b.type == "text"] == ["whole reply"],
          str(final.content))
    check("fallback: usage is read from the non-streamed body",
          final.usage.input_tokens == 5, str(final.usage))


# ---------------------------------------------------------------------------
# 5 — the agent loop, end to end
# ---------------------------------------------------------------------------


def test_agent_turn_over_openai(tmp: Path) -> None:
    """A full turn: tool call, execution, tool message, second reply."""
    requests: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"},
                content=_sse(
                    _chunk({"content": "Running it."}),
                    _chunk({"tool_calls": [{"index": 0, "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": "bash",
                                                "arguments": '{"command": "echo hi"}'}}]}),
                    _chunk({}, finish="tool_calls"),
                    {"choices": [], "usage": {"prompt_tokens": 100,
                                              "completion_tokens": 10}},
                ))
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"},
            content=_sse(_chunk({"content": "Done."}, finish="stop"),
                         {"choices": [], "usage": {"prompt_tokens": 200,
                                                   "completion_tokens": 5}}))

    cfg = Config.from_env(workdir=tmp, api_key="sk-test",
                          base_url="https://api.example.com/v1",
                          protocol="openai", mode="auto", max_rounds=5)
    agent = Agent(cfg)
    agent._client = _client(handler)

    async def drive():
        return [e async for e in agent.run_turn("say hi")]

    events = asyncio.run(drive())
    kinds = [e["type"] for e in events]

    check("agent: the turn ran over the OpenAI transport",
          "turn_end" in kinds, str(kinds))
    calls = [e for e in events if e["type"] == "tool_call"]
    check("agent: the tool call was surfaced to the UI", len(calls) == 1, str(calls))
    check("agent: with the parsed argument",
          calls and calls[0]["args"] == {"command": "echo hi"}, str(calls))
    results = [e for e in events if e["type"] == "tool_result"]
    check("agent: the tool actually executed", len(results) == 1, str(results))
    check("agent: and succeeded", results and not results[0]["is_error"], str(results))
    check("agent: the reply text arrived", any(
        e["type"] == "text_delta" and e["text"] == "Done." for e in events), str(kinds))
    usage = [e for e in events if e["type"] == "usage"]
    check("agent: usage was accounted across both calls",
          sum(u["input"] for u in usage) == 300, str(usage))

    second = requests[1]["messages"]
    roles = [m["role"] for m in second]
    check("agent: the second request carries the assistant tool call",
          "assistant" in roles and any(m.get("tool_calls") for m in second), str(roles))
    tool_msgs = [m for m in second if m["role"] == "tool"]
    check("agent: and one tool message with the matching id",
          len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "call_1",
          str(tool_msgs))
    check("agent: the history is stored Anthropic-shaped",
          any(isinstance(m.get("content"), list)
              and any(b.get("type") == "tool_result" for b in m["content"])
              for m in agent.messages),
          str(agent.messages[-2:]))


def test_connection_errors() -> None:
    """A refused connection is an SDK error, not an httpx traceback."""
    def handler(request):
        raise httpx2.ConnectError("connection refused")

    try:
        asyncio.run(_collect(_client(handler, base_url="https://dead.example/v1"),
                             model="m", messages=[{"role": "user", "content": "x"}]))
        check("connection: a refused connect raises", False, "nothing raised")
    except anthropic.APIConnectionError as e:
        check("connection: a refused connect raises anthropic.APIConnectionError", True)
        message = Agent._friendly_error(e, "https://dead.example/v1")
        check("connection: the hint names the endpoint",
              "dead.example" in message, message)
        check("connection: and does not blame a LiteLLM proxy",
              "LiteLLM" not in message, message)
    except Exception as e:  # noqa: BLE001
        check("connection: a refused connect raises anthropic.APIConnectionError",
              False, type(e).__name__)

    # The Anthropic route keeps its own wording: the proxy really might be down.
    err = anthropic.APIConnectionError(request=httpx2.Request(
        "POST", "http://localhost:4001/v1/messages"))
    message = Agent._friendly_error(err, "http://localhost:4001")
    check("connection: an Anthropic endpoint still mentions the proxy",
          "LiteLLM proxy" in message, message)


def test_model_cache() -> None:
    """A probed endpoint's ids are what `/model` offers for it."""
    from zimzilla import sources as sources_mod

    url = "https://direct.example/v1"
    check("cache: nothing is offered before a probe",
          sources_mod.models_for(url) is None, str(sources_mod.models_for(url)))

    oc._MODEL_CACHE[oc._cache_key(url)] = ("alpha", "beta")
    got = sources_mod.models_for(url)
    check("cache: after a probe, the endpoint's own ids are used",
          got == ("alpha", "beta"), str(got))
    check("cache: a full chat endpoint shares the same entry",
          sources_mod.models_for("https://direct.example/v1/chat/completions")
          == ("alpha", "beta"),
          str(sources_mod.models_for("https://direct.example/v1/chat/completions")))
    check("cache: an unrelated endpoint is unaffected",
          sources_mod.models_for("https://other.example/v1") is None,
          str(sources_mod.models_for("https://other.example/v1")))


def test_pricing_case() -> None:
    """A provider's mixed-case id must not fall through to DEFAULT_PRICE."""
    from zimzilla.config import DEFAULT_PRICE, price_for

    check("price: the alias is priced", price_for("deepseek-v4.1-flash") == (0.28, 0.42),
          str(price_for("deepseek-v4.1-flash")))
    check("price: the upstream's mixed-case id is priced the same",
          price_for("deepseek-ai/DeepSeek-V4.1-Flash-1017") == (0.28, 0.42),
          str(price_for("deepseek-ai/DeepSeek-V4.1-Flash-1017")))
    check("price: it is not billed at the default",
          price_for("deepseek-ai/DeepSeek-V4.1-Flash-1017") != DEFAULT_PRICE,
          str(price_for("deepseek-ai/DeepSeek-V4.1-Flash-1017")))
    check("price: a free suffix still wins",
          price_for("Qwen/Qwen3.8-27B:free") == (0.0, 0.0),
          str(price_for("Qwen/Qwen3.8-27B:free")))


def test_ui_commands(tmp: Path) -> None:
    """The two new commands run inside the real app without raising.

    A command that crashes on the UI thread is not caught by any transport
    test, and both of these touch widgets (status bar, header, completer) as
    well as the config. So they are driven through a mounted app.
    """
    from zimzilla.ui.app import ZimZillaApp

    cfg = Config.from_env(workdir=tmp, api_key="sk-test",
                          base_url="http://localhost:4001")
    app = ZimZillaApp(cfg)

    async def go():
        async with app.run_test(size=(110, 34)) as pilot:
            app._cmd_protocol([])                  # display, no argument
            await pilot.pause()
            check("ui: /protocol with no argument does not raise", True)

            app._cmd_protocol(["openai"])
            await pilot.pause()
            pinned = (cfg.protocol, app.agent.protocol)

            app._cmd_protocol(["anthropic"])
            await pilot.pause()
            other = app.agent.protocol

            app._cmd_protocol(["nonsense"])
            await pilot.pause()
            kept = cfg.protocol

            app._cmd_protocol(["auto"])
            await pilot.pause()

            # The endpoint command is a @work worker; its body is what touches
            # the widgets, so it is awaited directly with the probe stubbed.
            with mock.patch.object(oc, "probe", return_value=(True, "2 models")):
                await ZimZillaApp._cmd_openai_endpoint.__wrapped__(
                    app, ["https://direct.example/v1", "alpha"])
            await pilot.pause()
            return pinned, other, kept, (cfg.base_url, cfg.protocol, cfg.model,
                                         app.agent.protocol)

    pinned, other, kept, after = asyncio.run(go())
    check("ui: /protocol openai pins it and resolves to openai",
          pinned == ("openai", "openai"), str(pinned))
    check("ui: /protocol anthropic resolves to anthropic",
          other == "anthropic", str(other))
    check("ui: an unknown protocol is refused, not stored",
          kept == "anthropic", str(kept))
    check("ui: /openai-endpoint sets the URL, protocol and model",
          after == ("https://direct.example/v1", "openai", "alpha", "openai"),
          str(after))


def _fake_source(tmp: Path, key: str = "fake", *, model: str = "alias-one",
                 body: str | None = None):
    """A Source whose proxy config names an OpenAI-compatible upstream."""
    cfg = tmp / f"{key}-litellm.yaml"
    cfg.write_text(body if body is not None else (
        "model_list:\n"
        "  - model_name: alias-one\n"
        "    litellm_params:\n"
        "      model: openai/upstream-one\n"
        "      api_base: https://upstream.example/v1\n"
        "  - model_name: '*'\n"
        "    litellm_params:\n"
        "      model: openai/*\n"
        "      api_base: https://upstream.example/v1\n"
    ), encoding="utf-8")
    profile = tmp / "source"
    profile.write_text('export ANTHROPIC_AUTH_TOKEN="sk-test"\n'
                       'export ANTHROPIC_MODEL="alias-one"\n', encoding="utf-8")
    return sources_mod.Source(
        key=key, label="Fake", port=4099, profile=profile,
        service=tmp / "start-litellm.sh", config=cfg, model=model, root=tmp)


def test_direct_route(tmp: Path) -> None:
    """Each shipped proxy config yields the upstream it forwards to.

    The config is the authority on purpose: it already spells out which id each
    proxy alias maps to, so a model added there is reachable directly with no
    second edit here. Token Juice has no config — its route is declared in
    ``sources.TOKENJUICE_DIRECT`` — so it is asserted from the declaration.
    """
    shipped = ROOT / "packaging"
    for key, model, url in (
        ("logfare", "deepseek-v4.1-flash", "https://logfare.ai/v1"),
        ("iteracompute", "qwen/qwen3.8-27b", "https://api.iteracompute.com/v1"),
        ("infronai", "deepseek/deepseek-v4.1-flash:free", "https://llm.onerouter.pro/v1"),
    ):
        src = sources_mod.Source(
            key=key, label=key, port=0, profile=Path("/dev/null"),
            service=Path("/dev/null"), config=shipped / key / "litellm-config.yaml",
            model=model, root=ROOT)
        route = sources_mod.direct_route(src)
        check(f"route: {key} points at its own upstream",
              route is not None and route.base_url == url,
              str(route and route.base_url))

    # Token Juice: no proxy, no config, no service — the route is declared.
    check("route: tokenjuice has no proxy files in the tree",
          not (shipped / "tokenjuice" / "litellm-config.yaml").exists()
          and not (shipped / "tokenjuice" / "start-litellm.sh").exists(),
          str(sorted(p.name for p in (shipped / "tokenjuice").iterdir())))

    tj = sources_mod.Source(
        key="tokenjuice", label="Token Juice", port=0, profile=Path("/dev/null"),
        model="deepseek-v4.1-flash", root=ROOT,
        direct=sources_mod.TOKENJUICE_DIRECT)
    route = sources_mod.direct_route(tj)
    check("route: tokenjuice is direct-only", tj.is_direct_only,
          str(tj.is_direct_only))
    assert route is not None
    check("route: tokenjuice points at its own upstream",
          route.base_url == "https://api.tokenjuice.ai/v1", route.base_url)
    check("route: the old proxy alias is translated to the upstream id",
          route.upstream_model("deepseek-v4.1-flash")
          == "deepseek-ai/DeepSeek-V4.1-Flash-1017",
          route.upstream_model("deepseek-v4.1-flash"))
    check("route: the upstream id is passed through unchanged",
          route.upstream_model("deepseek-ai/DeepSeek-V4.1-Flash-1017")
          == "deepseek-ai/DeepSeek-V4.1-Flash-1017")
    check("route: an unmapped name is passed through",
          route.upstream_model("other") == "other")

    # Nothing to go direct to: no api_base, or a deployment that is not openai/.
    for name, body in (
        ("no api_base", "model_list:\n  - model_name: a\n    litellm_params:\n"
                        "      model: openai/b\n"),
        ("anthropic deployment", "model_list:\n  - model_name: a\n    litellm_params:\n"
                                 "      model: anthropic/b\n"
                                 "      api_base: https://x.example\n"),
        ("not yaml at all", "\x00\x01 not: [yaml"),
    ):
        src = _fake_source(tmp, key="nope", body=body)
        check(f"route: {name} yields no direct route",
              sources_mod.direct_route(src) is None,
              str(sources_mod.direct_route(src)))

    src = _fake_source(tmp)
    route = sources_mod.direct_route(src)
    assert route is not None
    check("route: the wildcard entry is not a model", "*" not in route.models,
          str(route.models))
    check("route: the alias maps to its upstream id",
          route.models.get("alias-one") == "upstream-one", str(route.models))


def test_active_key_direct(tmp: Path) -> None:
    """A directly-reached source is still identified as that source."""
    zhome = tmp / "zhome"
    (zhome / "iteracompute").mkdir(parents=True)
    (zhome / "iteracompute" / "start-litellm.sh").write_text("#!/bin/sh\n",
                                                             encoding="utf-8")
    (zhome / "iteracompute" / "source").write_text(
        'export ANTHROPIC_AUTH_TOKEN="sk-test"\n', encoding="utf-8")
    with mock.patch.object(sources_mod, "ZIMZILLA_HOME", zhome), \
         mock.patch.object(sources_mod, "DEFAULT_HOME", tmp / "home"):
        found = sources_mod.discover()
        check("active: the source is discovered", "iteracompute" in found,
              str(sorted(found)))
        check("active: the upstream host identifies the source",
              sources_mod.active_key("https://api.iteracompute.com/v1") == "iteracompute",
              str(sources_mod.active_key("https://api.iteracompute.com/v1")))
        check("active: a path on the same host too",
              sources_mod.active_key("https://api.iteracompute.com/v1/chat/completions")
              == "iteracompute")
        check("active: an unrelated host is still unknown",
              sources_mod.active_key("https://elsewhere.example/v1") is None,
              str(sources_mod.active_key("https://elsewhere.example/v1")))
        check("active: the proxy port still resolves by port",
              sources_mod.active_key("http://localhost:4002") == "iteracompute",
              str(sources_mod.active_key("http://localhost:4002")))


def test_switch_direct(tmp: Path) -> None:
    """Switching a source goes direct: no proxy start, upstream URL, alias map."""
    from zimzilla.ui.app import ZimZillaApp

    src = _fake_source(tmp, key="fake")
    route = sources_mod.direct_route(src)
    assert route is not None
    cfg = Config.from_env(workdir=tmp, api_key="k", base_url="http://localhost:4001",
                          model="alias-one")
    app = ZimZillaApp(cfg)

    def no_proxy(*a, **k):
        raise AssertionError("the direct switch must not start a proxy")

    async def go():
        async with app.run_test(size=(110, 34)) as pilot:
            with mock.patch.object(sources_mod, "ZIMZILLA_HOME", zhome), \
                 mock.patch.object(sources_mod, "start_proxy", no_proxy), \
                 mock.patch.object(oc, "probe", return_value=(True, "1 models")):
                oc._MODEL_CACHE[oc._cache_key(route.base_url)] = ("upstream-one",)
                await app._switch_direct(src, route)
            await pilot.pause()
            remembered = (zhome / "current-source").read_text(encoding="utf-8").strip()
            return (cfg.base_url, cfg.protocol, cfg.model, cfg.auth_token,
                    app.agent.protocol, remembered)

    zhome = tmp / "zhome"
    zhome.mkdir(exist_ok=True)

    base_url, protocol, model, token, resolved, remembered = asyncio.run(go())
    check("switch: the endpoint is the upstream, not the proxy",
          base_url == "https://upstream.example/v1", base_url)
    check("switch: the protocol is Chat Completions", protocol == "openai", protocol)
    check("switch: the alias became the upstream id", model == "upstream-one", model)
    check("switch: the key came from the profile", token == "sk-test", str(bool(token)))
    check("switch: the agent resolves to openai", resolved == "openai", resolved)
    check("switch: the choice is remembered for the next launch",
          remembered == "fake", str(remembered))

    # `/zim-<source> proxy` still asks for the hop, and the argument reaches the
    # switch. `_switch_source` is stubbed: starting a proxy is not this test.
    async def go2():
        # Back on a proxy port, so the "already on it" guard cannot short-circuit
        # the call before it reaches the switch.
        cfg.base_url = "http://localhost:4001"
        cfg.protocol = "auto"
        async with app.run_test(size=(110, 34)) as pilot:
            with mock.patch.object(sources_mod, "discover", return_value={"fake": src}), \
                 mock.patch.object(ZimZillaApp, "_switch_source") as switch:
                app._cmd_zim_source("fake", ["proxy"])
                await pilot.pause()
                with_proxy = switch.call_args
                app._cmd_zim_source("fake", [])
                await pilot.pause()
                without = switch.call_args
            return with_proxy, without

    with_proxy, without = asyncio.run(go2())
    check("switch: `proxy` asks for the proxy",
          with_proxy is not None and with_proxy.kwargs.get("use_proxy") is True,
          str(with_proxy))
    check("switch: no argument goes direct",
          without is not None and without.kwargs.get("use_proxy") is False,
          str(without))


def test_switch_direct_only(tmp: Path) -> None:
    """A source with no proxy is switched directly, whatever the arguments say.

    Token Juice has no LiteLLM proxy: no service, no config, no port. The
    switch must never try to start one, and `proxy` — an argument that means
    something for the other local routes — must not send it down that path.
    """
    from zimzilla.ui.app import ZimZillaApp

    profile = tmp / "tj-source"
    profile.write_text('export ANTHROPIC_AUTH_TOKEN="tj-test"\n'
                       'export ANTHROPIC_MODEL="deepseek-v4.1-flash"\n',
                       encoding="utf-8")
    src = sources_mod.Source(
        key="directfake", label="Direct Fake", port=0, profile=profile,
        model="deepseek-v4.1-flash", root=tmp,
        direct=sources_mod.DirectRoute(
            base_url="https://direct.example/v1",
            models={"deepseek-v4.1-flash": "upstream-one"}))
    check("direct-only: the source has no proxy", src.is_direct_only,
          str(src.is_direct_only))

    cfg = Config.from_env(workdir=tmp, api_key="k", base_url="http://localhost:4001",
                          model="deepseek-v4.1-flash")
    app = ZimZillaApp(cfg)
    zhome = tmp / "zhome-direct"
    zhome.mkdir(exist_ok=True)

    def no_proxy(*a, **k):
        raise AssertionError("a direct-only source must not start a proxy")

    async def go():
        async with app.run_test(size=(110, 34)) as pilot:
            with mock.patch.object(sources_mod, "ZIMZILLA_HOME", zhome), \
                 mock.patch.object(sources_mod, "start_proxy", no_proxy), \
                 mock.patch.object(oc, "probe", return_value=(True, "1 models")):
                oc._MODEL_CACHE[oc._cache_key("https://direct.example/v1")] = (
                    "upstream-one",)
                # `proxy` is asked for and there is nothing to ask: it must
                # still land on the upstream rather than refuse the command.
                await ZimZillaApp._switch_source.__wrapped__(
                    app, src, use_proxy=True)
            await pilot.pause()
            return (cfg.base_url, cfg.protocol, cfg.model,
                    app.agent.protocol)

    base_url, protocol, model, resolved = asyncio.run(go())
    check("direct-only: the endpoint is the upstream", base_url == "https://direct.example/v1",
          base_url)
    check("direct-only: the protocol is Chat Completions", protocol == "openai",
          protocol)
    check("direct-only: the alias became the upstream id", model == "upstream-one",
          model)
    check("direct-only: the agent resolves to openai", resolved == "openai", resolved)

    # Already on it: `proxy` cannot move a direct-only route onto a proxy, so
    # the command says so and leaves the session alone.
    async def go2():
        async with app.run_test(size=(110, 34)) as pilot:
            with mock.patch.object(sources_mod, "discover",
                                   return_value={"directfake": src}), \
                 mock.patch.object(ZimZillaApp, "_switch_source") as switch:
                app._cmd_zim_source("directfake", ["proxy"])
                await pilot.pause()
                return switch.called

    check("direct-only: `proxy` does not re-switch an already-direct session",
          asyncio.run(go2()) is False, "the switch was called")


def main() -> int:
    test_protocol_resolution()
    test_urls()
    test_local_proxy()
    test_message_translation()
    test_tool_translation()
    test_arguments()
    test_stream_assembly()
    test_stream_length_and_empty()
    test_create()
    test_errors()
    test_max_tokens_negotiation()
    test_output_ceiling_clamp()
    test_stream_fallback()
    test_connection_errors()
    test_model_cache()
    test_pricing_case()
    with tempfile.TemporaryDirectory() as tmp:
        test_agent_turn_over_openai(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_ui_commands(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_direct_route(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_active_key_direct(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_switch_direct(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_switch_direct_only(Path(tmp))

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    for name, ok, extra in RESULTS:
        if not ok:
            print(f"FAIL  {name}" + (f"  {extra}" if extra else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
