"""Native OpenAI-compatible transport — the second wire protocol ZimZilla speaks.

ZimZilla grew up on the Anthropic Messages protocol, and for a while every
upstream was reached through a local LiteLLM proxy that translated Chat
Completions into it. That arrangement works, and it costs two things the
operator can feel:

* **A hop.** Every token is parsed, re-serialised and re-emitted by the proxy
  before it reaches this harness, which is exactly the latency a streaming CLI
  is trying to avoid.
* **Tool-call fidelity.** The translation is lossy in ways that are invisible
  until a turn dies: argument fragments re-assembled by a proxy that does not
  preserve the call ``id``, parallel calls collapsed into one, a reasoning model
  whose thinking is billed against ``max_tokens`` but never forwarded. The
  operator sees "the model cannot use tools" rather than "the bridge mangled
  them".

So this module speaks OpenAI Chat Completions **directly**, for any endpoint
that serves it: ``api.openai.com``, DeepSeek, Groq, vLLM, Ollama, IteraCompute,
InfronAI, and the operator's own logfare/tokenjuice routes. No proxy, no
translation in the middle.

What it is not
--------------
It is not a second agent loop. The loop in :mod:`zimzilla.agent` is written
against the Anthropic message *shape* — ``text`` and ``tool_use`` blocks,
``tool_result`` messages, ``stop_reason``, ``usage.input_tokens`` — and that
shape is a good internal representation regardless of what goes on the wire.
This module translates at the boundary in both directions and hands the loop
the same objects the Anthropic SDK would, so every behaviour already built and
tested there (truncation, reasoning exhaustion, compaction, the round caps,
usage accounting) applies unchanged.

Deliberate details
------------------
* **Errors are raised as ``anthropic`` exceptions.** ``_friendly_error``, the
  retry policy and ``_rate_limit_is_a_window`` all dispatch on
  ``isinstance(e, anthropic.*)``. A private exception type would bypass every
  one of those and turn a 429 with a reset header into a traceback.
* **The HTTP client is the SDK's own.** ``anthropic`` 1.x pins ``httpx2``;
  older releases use ``httpx``. Building an ``anthropic.APIStatusError`` needs
  a ``Response`` of the *same* class the SDK uses, so the import follows the
  SDK rather than pinning one of them here.
* **``max_tokens`` is negotiable.** OpenAI's newer models reject it and want
  ``max_completion_tokens``; vLLM-era servers reject ``stream_options``. Both
  are retried once with the offending key renamed or dropped, because neither
  is something the operator can fix from the prompt.
* **Tool arguments are assembled by index, then parsed defensively.** A
  truncated or fenced JSON blob must not abort a turn that has already been
  paid for.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncIterator
from urllib.parse import urlparse

try:  # anthropic >= 1.x pins its HTTP client under this name
    import httpx2 as httpx
except ModuleNotFoundError:  # pragma: no cover - older SDKs ship plain httpx
    import httpx  # type: ignore[no-redef]

#: Values ``Config.protocol`` and ``ZIMZILLA_PROTOCOL`` accept. ``auto`` means
#: "decide from the endpoint", which is the default so an existing setup keeps
#: working without a flag.
PROTOCOLS = ("auto", "anthropic", "openai")

#: Path appended to a base URL that does not already name a Chat Completions
#: route. ``https://api.deepseek.com/v1`` -> ``.../v1/chat/completions``;
#: ``https://host`` -> ``https://host/v1/chat/completions``.
CHAT_PATH = "/v1/chat/completions"

#: Hosts that speak the Anthropic Messages protocol and NOT Chat Completions —
#: or speak it well enough that switching them silently would be a regression.
#: The hosted gateways in the tree are here; a new one belongs here too.
ANTHROPIC_HOSTS = (
    "api.anthropic.com",
    "openrouter.ai",
    "tokenharbor.ai",
    "unorouter.com",
    "aihubmix.com",
    "beatapi.io",
    "gw.ldxhub.io",
    # OrcaRouter serves the Messages protocol at ``/v1/messages`` — a
    # credential-less POST answers an Anthropic-shaped 401, i.e. the route
    # exists — and its base URL carries no ``/v1`` path for the heuristic to
    # read, so it is named here rather than guessed at.
    "api.orcarouter.ai",
)

#: Hosts that are Chat Completions by construction. Not exhaustive, and not
#: meant to be: the ``/v1`` path rule and the explicit override cover the rest.
#: These are only here so a bare host with no path — ``https://api.openai.com``
#: — resolves to the right protocol instead of falling back to Anthropic.
OPENAI_HOSTS = (
    "api.openai.com",
    "api.deepseek.com",
    "api.groq.com",
    "api.mistral.ai",
    "api.together.xyz",
    "api.fireworks.ai",
    "api.x.ai",
    "api.cerebras.ai",
    "api.perplexity.ai",
    "api.moonshot.ai",
    "api.moonshot.cn",
    "dashscope.aliyuncs.com",
    "api.z.ai",
    "api.nvidia.com",
    "api.sambanova.ai",
    "api.nebius.ai",
    "llm.onerouter.pro",
    "api.iteracompute.com",
    "api.tokenjuice.ai",
    "logfare.ai",
)


def _host_of(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, known: tuple[str, ...]) -> bool:
    return any(host == k or host.endswith("." + k) for k in known)


def resolve_protocol(base_url: str, override: str | None = None) -> str:
    """Which protocol to speak to *base_url*: ``"anthropic"`` or ``"openai"``.

    Precedence, highest first:

    1. *override* — ``--protocol`` / ``Config.protocol``. ``"auto"`` and
       ``None`` mean "decide below".
    2. ``ZIMZILLA_PROTOCOL`` — so a shell profile can pin it for a route the
       heuristics would guess wrong.
    3. A URL that already names the route: ``.../chat/completions`` is OpenAI.
    4. A path ending in ``/v1`` — the near-universal OpenAI convention. This is
       checked before the localhost rule so a local vLLM/Ollama at
       ``localhost:8000/v1`` is not mistaken for one of ZimZilla's LiteLLM
       proxies.
    5. A known Anthropic-native host.
    6. Loopback, with no ``/v1``: that is one of this harness's own proxies,
       which speak Messages by construction.
    7. A known Chat Completions host.
    8. Otherwise Anthropic — the behaviour every existing endpoint had before
       this module existed. Guessing OpenAI for an unknown host would break
       working setups to serve a case nobody reported.
    """
    # An explicit choice wins outright; "auto" is not a choice, it is the
    # absence of one, so the environment still gets a say before the heuristics.
    choice = (override or "").strip().lower()
    if choice in ("openai", "anthropic"):
        return choice
    from_env = (os.environ.get("ZIMZILLA_PROTOCOL") or "").strip().lower()
    if from_env in ("openai", "anthropic"):
        return from_env

    url = (base_url or "").strip()
    lowered = url.lower()
    if "chat/completions" in lowered:
        return "openai"

    parsed = urlparse(lowered)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/v1"):
        return "openai"
    if _host_matches(host, ANTHROPIC_HOSTS):
        return "anthropic"
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return "anthropic"
    if _host_matches(host, OPENAI_HOSTS):
        return "openai"
    return "anthropic"


def is_local_proxy(base_url: str) -> bool:
    """Whether *base_url* is one of this harness's own local LiteLLM proxies.

    Those speak Messages and nothing else, so a session pinned to ``openai``
    against one of them would 404 on every turn. The UI uses this to say so at
    the moment the mistake is made rather than at the first reply.
    """
    parsed = urlparse((base_url or "").lower())
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") \
        and not path.endswith("/v1")


def chat_completions_url(base_url: str) -> str:
    """The Chat Completions endpoint for *base_url*.

    A URL that already ends in ``/chat/completions`` is returned untouched, so
    an operator can paste the exact endpoint from a provider's docs. A URL that
    ends in ``/v1`` only gains the last segment; anything else gains the whole
    ``/v1/chat/completions``.
    """
    url = (base_url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + CHAT_PATH


def models_url(base_url: str) -> str:
    """The Models endpoint for *base_url*, for probing a route."""
    url = (base_url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")] + "/models"
    if url.endswith("/v1"):
        return url + "/models"
    return url + "/v1/models"


#: Model ids learned from a direct endpoint's Models route, keyed by base URL.
#: Written only by :func:`probe`, read by ``sources.models_for`` so ``/model``
#: can offer the endpoint's real ids instead of this harness's default snapshot
#: — which for a hand-typed endpoint is a list of names it has never served.
_MODEL_CACHE: dict[str, tuple[str, ...]] = {}


def _cache_key(base_url: str) -> str:
    return chat_completions_url(base_url).removesuffix("/chat/completions")


def cached_ids(base_url: str) -> tuple[str, ...]:
    """The ids a probed direct endpoint reported, or ``()`` if never probed.

    Never fetches: ``sources.models_for`` calls this on the render path, and a
    blocking network read there would stall the UI thread.
    """
    return _MODEL_CACHE.get(_cache_key(base_url), ())


# ---------------------------------------------------------------------------
# Anthropic-shaped objects handed back to the agent loop
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """One content block, in the shape ``Agent._block_to_dict`` expects."""

    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


@dataclass
class Usage:
    """Token counts under the names the agent loop reads."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Message:
    """The final assistant message, Anthropic-shaped."""

    content: list[Block] = field(default_factory=list)
    stop_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    role: str = "assistant"


def _text_of(content: Any) -> str:
    """Flatten Anthropic content (str or block list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "\n\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def _append_text(out: list[dict], role: str, text: str) -> None:
    """Append a plain message, merging with a same-role neighbour.

    Strict Chat Completions servers (vLLM among them) reject two consecutive
    user messages, and this harness can produce them: a ``notice`` folds into a
    turn, and a deliverable nudge appends a second user message. Anthropic
    merges those itself; OpenAI's schema does not require it but some
    implementations enforce it, so the merge happens here.
    """
    if not text:
        return
    if out and out[-1].get("role") == role and isinstance(out[-1].get("content"), str):
        out[-1]["content"] = f"{out[-1]['content']}\n\n{text}"
        return
    out.append({"role": role, "content": text})


def translate_messages(system: Any, messages: list[dict]) -> list[dict]:
    """Anthropic history -> OpenAI Chat Completions messages.

    The two shapes differ in three places that matter:

    * ``tool_use`` blocks become ``tool_calls`` on the assistant message, with
      the arguments serialised back to JSON — the model produced them as JSON
      and the server expects JSON, so anything else round-trips wrong.
    * ``tool_result`` blocks become one ``role: tool`` message *per call*, and
      they must directly follow the assistant message that made the call.
      They lead the translated user message for that reason; any text in the
      same message follows them.
    * ``thinking`` / ``redacted_thinking`` blocks are dropped. They are this
      harness's record of what the model said to itself, and several servers
      reject them outright when echoed back. The reasoning is still streamed
      to the transcript when the endpoint sends it.
    """
    out: list[dict] = []
    system_text = _text_of(system)
    if system_text.strip():
        out.append({"role": "system", "content": system_text})

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role") or "user"
        content = message.get("content")

        if isinstance(content, str) or content is None:
            _append_text(out, role, content or "")
            continue

        blocks = [b for b in content if isinstance(b, dict)]

        if role == "assistant":
            texts: list[str] = []
            calls: list[dict] = []
            for block in blocks:
                kind = block.get("type")
                if kind == "text":
                    if block.get("text"):
                        texts.append(block["text"])
                elif kind == "tool_use":
                    calls.append({
                        "id": block.get("id") or f"call_{len(calls)}",
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "",
                            "arguments": json.dumps(block.get("input") or {},
                                                    ensure_ascii=False),
                        },
                    })
            text = "\n\n".join(texts)
            # An assistant turn that carried neither text nor a call is not
            # representable: OpenAI rejects a message with null content and no
            # tool_calls. It happens when a reasoning-only reply was recorded,
            # so the honest translation is to leave it out.
            if not text and not calls:
                continue
            entry: dict = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            continue

        # user: text, tool results, and images (which this transport cannot
        # carry — said in the transcript rather than silently dropped).
        texts = []
        tool_messages: list[dict] = []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                if block.get("text"):
                    texts.append(block["text"])
            elif kind == "tool_result":
                body = _text_of(block.get("content"))
                if block.get("is_error"):
                    body = f"Error: {body}" if body else "Error"
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or "",
                    "content": body or "(no output)",
                })
            elif kind in ("image", "document"):
                texts.append(f"[{kind} omitted — this endpoint takes text only]")

        if tool_messages:
            out.extend(tool_messages)
            _append_text(out, "user", "\n\n".join(texts))
        else:
            _append_text(out, "user", "\n\n".join(texts))

    return out


def translate_tools(schemas: list[dict] | None) -> list[dict]:
    """Anthropic tool schemas -> OpenAI function tools.

    ``input_schema`` maps straight onto ``parameters``. A schema without a
    usable object shape is replaced with an empty object schema rather than
    forwarded: some servers validate it and answer 400 for the whole request,
    which would take the other tools down with it.
    """
    tools: list[dict] = []
    for schema in schemas or []:
        if not isinstance(schema, dict) or not schema.get("name"):
            continue
        params = schema.get("input_schema")
        if not isinstance(params, dict) or params.get("type") != "object":
            params = {"type": "object", "properties": {}}
        tools.append({
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema.get("description") or "",
                "parameters": params,
            },
        })
    return tools


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

#: ``finish_reason`` -> the ``stop_reason`` the agent loop compares against.
#: Only ``length`` and ``tool_calls`` change behaviour there; the rest exist so
#: the transcript is not told a completed turn was cut off.
STOP_REASONS = {
    "stop": "end_turn",
    "end_turn": "end_turn",
    "length": "max_tokens",
    "max_tokens": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "tool_use": "tool_use",
    "content_filter": "end_turn",
    "stop_sequence": "end_turn",
}


def _delta_text(delta: dict) -> str:
    """The visible text in one streamed delta (or a whole message)."""
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text") or ""
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text", "output_text")
        )
    return ""


def _delta_reasoning(delta: dict) -> str:
    """The model's own thinking in one delta, under any of its names.

    ``reasoning_content`` is DeepSeek/vLLM, ``reasoning`` is OpenRouter and
    several gateways. Both are forwarded when present, which is the difference
    between watching the model work and watching a blank line for a minute.
    """
    for key in ("reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _usage_from(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return Usage()
    return Usage(
        input_tokens=int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or raw.get("output_tokens") or 0),
    )


def parse_arguments(raw: str) -> dict:
    """Best-effort parse of a tool call's JSON arguments.

    A turn that has already been paid for must not die because a model emitted
    a fenced block, a trailing comma, or was cut off mid-object. The ladder is:
    parse as-is, strip a markdown fence, then take the outermost brace pair.
    Anything still unparseable becomes ``{}`` — the tool then reports its own
    missing argument, which is a clearer signal than a JSON traceback.
    """
    if not raw or not raw.strip():
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            value = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


class Accumulator:
    """Turns streamed chunks (or a whole message) into one Anthropic message.

    Tool calls are keyed by their ``index`` because that is the only field a
    server is required to send on every fragment. Everything else — ``id``,
    ``name``, ``arguments`` — arrives split across chunks in ways that differ
    per implementation: arguments always fragment, names sometimes do, and the
    ``id`` is usually only on the first chunk. Assembling by index is what
    keeps parallel calls from being merged into one, which is the failure mode
    that reads as "the model cannot use tools".
    """

    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage = Usage()

    # -- feeding ---------------------------------------------------------
    def feed_chunk(self, obj: dict) -> list[SimpleNamespace]:
        """Consume one streamed chunk; return the events worth emitting."""
        events: list[SimpleNamespace] = []
        if isinstance(obj.get("usage"), dict):
            self.usage = _usage_from(obj["usage"])
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                continue
            text = _delta_text(delta)
            if text:
                self.text.append(text)
                events.append(_event("text_delta", text=text))
            reasoning = _delta_reasoning(delta)
            if reasoning:
                self.reasoning.append(reasoning)
                events.append(_event("thinking_delta", thinking=reasoning))
            for call in delta.get("tool_calls") or []:
                self._feed_call(call)
            finish = choice.get("finish_reason")
            if finish:
                self.finish_reason = finish
        return events

    def feed_message(self, obj: dict) -> list[SimpleNamespace]:
        """Consume a whole (non-streamed) response body."""
        events: list[SimpleNamespace] = []
        if isinstance(obj.get("usage"), dict):
            self.usage = _usage_from(obj["usage"])
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if isinstance(message, dict):
                text = _delta_text(message)
                if text:
                    self.text.append(text)
                    events.append(_event("text_delta", text=text))
                reasoning = _delta_reasoning(message)
                if reasoning:
                    self.reasoning.append(reasoning)
                    events.append(_event("thinking_delta", thinking=reasoning))
                for call in message.get("tool_calls") or []:
                    self._feed_call(call)
            finish = choice.get("finish_reason")
            if finish:
                self.finish_reason = finish
        return events

    def _feed_call(self, call: Any) -> None:
        if not isinstance(call, dict):
            return
        index = call.get("index")
        if not isinstance(index, int):
            # No index: the call is complete in one piece (some servers, and
            # every non-streamed response). Key it by its id, or by count.
            index = len(self.calls)
        slot = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call.get("id"):
            slot["id"] = call["id"]
        function = call.get("function") or {}
        if not isinstance(function, dict):
            return
        name = function.get("name")
        if isinstance(name, str) and name:
            # Appended only when it is genuinely new: a server that resends the
            # whole name on every chunk would otherwise produce
            # "bashbashbash".
            if not slot["name"].endswith(name):
                slot["name"] += name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["arguments"] += arguments

    # -- result ----------------------------------------------------------
    def message(self) -> Message:
        blocks: list[Block] = []
        text = "".join(self.text)
        if text:
            blocks.append(Block(type="text", text=text))
        for index in sorted(self.calls):
            call = self.calls[index]
            if not call["name"]:
                continue
            blocks.append(Block(
                type="tool_use",
                id=call["id"] or f"call_{index}",
                name=call["name"],
                input=parse_arguments(call["arguments"]),
            ))
        stop = STOP_REASONS.get((self.finish_reason or "").lower(), "end_turn")
        # A server that streams calls but never sends a finish_reason still
        # asked for tools; treating that as end_turn would end the turn with
        # the work undone.
        if self.finish_reason is None and any(b.type == "tool_use" for b in blocks):
            stop = "tool_use"
        return Message(content=blocks, stop_reason=stop, usage=self.usage)


def _event(delta_type: str, **attrs: Any) -> SimpleNamespace:
    """One ``content_block_delta`` in the shape ``_stream_once`` reads.

    ``_stream_once`` does ``getattr(delta, "type")`` and ``delta.text``, so the
    delta has to be an object with attributes, not a dict — a dict would read
    as an event with no type and be silently dropped.
    """
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type=delta_type, **attrs),
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

_STATUS_ERRORS = {
    400: "BadRequestError",
    401: "AuthenticationError",
    403: "PermissionDeniedError",
    404: "NotFoundError",
    409: "ConflictError",
    413: "RequestTooLargeError",
    422: "UnprocessableEntityError",
    429: "RateLimitError",
}


def _decode_body(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return {"error": {"message": raw.decode("utf-8", "replace")[:600]}}


def _error_message(body: Any) -> str:
    if not isinstance(body, dict):
        return str(body or "")[:600]
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")[:600]
    if isinstance(error, str):
        return error[:600]
    return str(body.get("message") or body.get("detail") or "")[:600]


def _output_limit_from(message: str) -> int | None:
    """The output ceiling named in a provider's 400, if it names one.

    Matched narrowly — ``at most N`` and ``maximum ... N`` — because the same
    sentence often also names the *context* window, which is not the number to
    clamp the reply to.
    """
    match = re.search(r"at most\s+(\d{2,7})", message)
    if match is None:
        match = re.search(r"maximum[^.]{0,40}?(\d{2,7})", message)
    if match is None:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
        return None
    return value or None


def _connection_error(exc: Exception, url: str) -> Exception:
    """Wrap an ``httpx`` transport failure in the SDK's own exception type.

    The retry policy and ``_friendly_error`` both dispatch on
    ``isinstance(e, anthropic.APIConnectionError)``. A raw ``httpx`` error
    would skip both: a refused connection or a stalled stream would surface as
    an unhandled traceback instead of "cannot reach the endpoint" and a retry.
    """
    import anthropic

    request = httpx.Request("POST", url)
    if isinstance(exc, httpx.TimeoutException):
        return anthropic.APITimeoutError(request=request)
    return anthropic.APIConnectionError(request=request)


def error_for(status: int, raw: bytes, headers: Any, url: str) -> Exception:
    """Build the ``anthropic`` exception a Chat Completions error maps onto.

    The class matters, not just the status: ``_friendly_error`` and the retry
    policy in ``_stream_once`` both dispatch on ``isinstance``. The response
    object is constructed without ``content-encoding``/``content-length``
    because the bytes are already decoded; leaving them would make the SDK's
    own readers try to gunzip plain JSON.
    """
    import anthropic

    body = _decode_body(raw)
    message = _error_message(body) or f"HTTP {status}"
    clean = {}
    try:
        clean = {k: v for k, v in dict(headers or {}).items()
                 if k.lower() not in ("content-encoding", "content-length",
                                      "transfer-encoding")}
    except (TypeError, ValueError):
        clean = {}
    response = httpx.Response(
        status, headers=clean, content=raw,
        request=httpx.Request("POST", url),
    )
    name = _STATUS_ERRORS.get(status)
    if name is None:
        name = "InternalServerError" if status >= 500 else "APIStatusError"
    cls = getattr(anthropic, name, anthropic.APIStatusError)
    return cls(message, response=response, body=body)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class _Stream:
    """Async context manager + async iterator, like ``sdk.messages.stream``.

    The agent loop enters it, iterates the deltas for the transcript, then asks
    for the final message. Both halves matter: the deltas are what the operator
    watches, and the final message is what the tool loop acts on.
    """

    def __init__(self, client: "Client", kwargs: dict) -> None:
        self._client = client
        self._kwargs = kwargs
        self._http = None
        self._cm = None
        self._resp = None
        self._acc = Accumulator()
        self._final: Message | None = None
        self._preset_events: list[SimpleNamespace] | None = None

    # -- lifecycle -------------------------------------------------------
    async def __aenter__(self) -> "_Stream":
        payload = self._client.payload(self._kwargs, stream=self._client.stream_enabled)
        for _ in range(3):
            try:
                await self._open(payload)
            except httpx.HTTPError as e:
                await self._close()
                raise _connection_error(e, self._client.url) from e
            if self._resp.status_code < 400:
                break
            raw = await self._resp.aread()
            headers = dict(self._resp.headers)
            status = self._resp.status_code
            await self._close()
            if self._retry_payload(payload, raw):
                continue
            raise error_for(status, raw, headers, self._client.url)
        else:  # pragma: no cover - the loop always breaks or raises
            raise RuntimeError("Chat Completions request could not be sent")

        if not payload.get("stream"):
            # The server refused streaming. Read the whole reply and synthesise
            # one delta per channel, so the caller still sees the text and the
            # loop still gets its message.
            raw = await self._resp.aread()
            if self._resp.status_code >= 400:  # pragma: no cover - checked above
                raise error_for(self._resp.status_code, raw, dict(self._resp.headers),
                                self._client.url)
            self._preset_events = self._acc.feed_message(_decode_body(raw))
            self._final = self._acc.message()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close()

    async def _open(self, payload: dict) -> None:
        self._http = httpx.AsyncClient(
            timeout=self._client.timeout,
            transport=self._client.transport,
        )
        if payload.get("stream"):
            self._cm = self._http.stream(
                "POST", self._client.url,
                headers=self._client.headers, json=payload,
            )
            self._resp = await self._cm.__aenter__()
        else:
            self._resp = await self._http.post(
                self._client.url, headers=self._client.headers, json=payload,
            )

    async def _close(self) -> None:
        try:
            if self._cm is not None:
                await self._cm.__aexit__(None, None, None)
            elif self._resp is not None:
                await self._resp.aclose()
        finally:
            self._cm = None
            self._resp = None
            if self._http is not None:
                await self._http.aclose()
                self._http = None

    def _retry_payload(self, payload: dict, raw: bytes) -> bool:
        """Rename, drop or shrink a knob this server does not accept.

        These are all harness-side mistakes rather than anything the operator
        wrote, so failing the turn for them would be blaming the wrong party.
        """
        message = _error_message(_decode_body(raw)).lower()
        if "max_completion_tokens" in message and "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
            return True
        if "stream_options" in message and "stream_options" in payload:
            payload.pop("stream_options", None)
            return True
        if payload.get("stream") and "stream" in message and any(
            word in message for word in ("not supported", "unsupported",
                                         "not allowed", "disabled")
        ):
            payload["stream"] = False
            return True
        # An output ceiling larger than the model allows. The harness default
        # (32768) is bigger than several models' real maximum, and the catalog
        # that would have clamped it is not there for a hand-typed endpoint.
        # Read the limit out of the provider's own sentence rather than halving
        # blindly — "supports at most 16384 completion tokens" is exact.
        key = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
        requested = payload.get(key)
        if (key in message or "max_tokens" in message) and isinstance(requested, int) \
                and any(word in message for word in ("too large", "at most", "exceed")):
            limit = _output_limit_from(message)
            if limit and 0 < limit < requested:
                payload[key] = limit
                return True
        return False

    # -- iteration -------------------------------------------------------
    def __aiter__(self) -> AsyncIterator[SimpleNamespace]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[SimpleNamespace]:
        if self._preset_events is not None:
            for event in self._preset_events:
                yield event
            return
        try:
            async for line in self._resp.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":"):
                    # Blank keep-alive or an SSE comment. Both are routine.
                    continue
                if line.startswith("data:"):
                    data = line[len("data:"):].strip()
                else:
                    # A few servers omit the ``data:`` prefix. Accept the JSON
                    # rather than silently discarding the whole stream.
                    data = line
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("error"):
                    raise error_for(self._resp.status_code or 400,
                                    json.dumps(obj).encode(), dict(self._resp.headers),
                                    self._client.url)
                for event in self._acc.feed_chunk(obj):
                    yield event
        except httpx.HTTPError as e:
            # A connection dropped mid-reply is a transient failure like any
            # other: the loop retries it, and the operator is told which endpoint
            # went quiet rather than handed an httpx traceback.
            raise _connection_error(e, self._client.url) from e
        self._final = self._acc.message()

    async def get_final_message(self) -> Message:
        if self._final is None:
            self._final = self._acc.message()
        return self._final


class _Messages:
    """The ``client.messages`` namespace, mirroring the Anthropic SDK."""

    def __init__(self, client: "Client") -> None:
        self._client = client

    def stream(self, **kwargs: Any) -> _Stream:
        return _Stream(self._client, kwargs)

    async def create(self, **kwargs: Any) -> Message:
        payload = self._client.payload(kwargs, stream=False)
        for _ in range(3):
            async with httpx.AsyncClient(
                timeout=self._client.timeout,
                transport=self._client.transport,
            ) as http:
                try:
                    response = await http.post(
                        self._client.url, headers=self._client.headers, json=payload,
                    )
                except httpx.HTTPError as e:
                    raise _connection_error(e, self._client.url) from e
                raw = await response.aread()
                if response.status_code < 400:
                    accumulator = Accumulator()
                    accumulator.feed_message(_decode_body(raw))
                    return accumulator.message()
                stream = _Stream(self._client, kwargs)
                if not stream._retry_payload(payload, raw):
                    raise error_for(response.status_code, raw,
                                    dict(response.headers), self._client.url)
        raise RuntimeError("Chat Completions request could not be sent")  # pragma: no cover


class Client:
    """A minimal OpenAI-compatible client with an Anthropic-shaped surface.

    Only the two calls the harness makes are implemented — ``messages.stream``
    and ``messages.create`` — because this is a transport, not an SDK.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 120.0,
        stream: bool = True,
        transport: Any = None,
        extra_headers: dict | None = None,
    ) -> None:
        self.base_url = base_url
        self.url = chat_completions_url(base_url)
        self.api_key = api_key or ""
        self.stream_enabled = stream
        self.transport = transport
        self.extra_headers = dict(extra_headers or {})
        # Connect and pool waits are bounded tightly and separately from the
        # read: the read timeout is the gap between streamed chunks, which is
        # what a stalled endpoint looks like, and it should not also govern how
        # long a TCP connect may take.
        self.timeout = httpx.Timeout(connect=15.0, read=timeout,
                                     write=30.0, pool=15.0)
        self.messages = _Messages(self)

    @property
    def headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            # Some gateways sit behind a CDN that buffers a response with no
            # user agent, which turns a stream into one late blob.
            "User-Agent": "zimzilla",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def payload(self, kwargs: dict, stream: bool) -> dict:
        """Translate the Anthropic-shaped call into a Chat Completions body."""
        payload: dict = {
            "model": kwargs.get("model") or "",
            "messages": translate_messages(kwargs.get("system") or "",
                                           kwargs.get("messages") or []),
        }
        if stream:
            payload["stream"] = True
            # Without this, OpenAI (and anything copying it) sends no usage on a
            # streamed reply, and the context gauge and cost line would read
            # zero for every turn.
            payload["stream_options"] = {"include_usage": True}

        max_tokens = kwargs.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            payload["max_tokens"] = max_tokens

        tools = translate_tools(kwargs.get("tools"))
        if tools:
            payload["tools"] = tools

        # ``temperature``/``top_p`` arrive through extra_body because they are
        # not in the typed Anthropic signature. They are top-level fields here.
        for key, value in (kwargs.get("extra_body") or {}).items():
            if value is not None:
                payload[key] = value
        return payload


def probe(base_url: str, api_key: str | None = None, timeout: float = 8.0) -> tuple[bool, str]:
    """Ask an endpoint's Models route whether it is there and what it serves.

    Deliberately synchronous and short: the caller runs it off the UI thread,
    and its job is to turn "I typed the URL wrong" from a failed first turn
    into an immediate, specific answer.
    """
    url = models_url(base_url)
    headers = {"Accept": "application/json", "User-Agent": "zimzilla"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except OSError:
            pass
        detail = _error_message(_decode_body(body.encode())) if body else ""
        return False, f"{url} answered HTTP {e.code}" + (f": {detail}" if detail else "")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"cannot reach {url}: {e}"
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return False, f"{url} answered with something that is not JSON"
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return True, f"{url} answered"
    ids = [str(e.get("id")) for e in entries if isinstance(e, dict) and e.get("id")]
    if ids:
        # Warm the cache `sources.models_for` reads, so `/model` offers this
        # endpoint's own ids rather than the harness's default snapshot.
        _MODEL_CACHE[_cache_key(base_url)] = tuple(ids)
    preview = ", ".join(ids[:4]) + (" …" if len(ids) > 4 else "")
    return True, f"{len(ids)} models" + (f" ({preview})" if preview else "")
