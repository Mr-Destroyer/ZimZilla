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

import base64
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
    # unrestricted.sh serves the Messages protocol natively at its bare host —
    # measured: POST /v1/messages answers an Anthropic-shaped 401 with no key,
    # and a real tool-using turn round-trips. The base URL carries no path, so
    # without this entry the bare host would fall through to the final
    # "otherwise Anthropic" rule and be right by accident; naming it here makes
    # that deliberate rather than incidental.
    "unrestricted.sh",
    "aihubmix.com",
    "beatapi.io",
    "gw.ldxhub.io",
    # Zro serves the Messages protocol natively at its bare host; the OpenAI
    # surfaces live under /v1, which the path rule already covers. Naming the
    # host here keeps ``https://zro.moonmath.ai`` from ever being read as a Chat
    # Completions endpoint by a future heuristic.
    "zro.moonmath.ai",
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
    "model.inferx.net",
    "api.inferbase.ai",
    # Flex's playground. Its route is the endpoint itself
    # (``/api/playground/chat``), so the path rule would already resolve it —
    # naming the host keeps a bare ``https://eu.api.flex.ai`` from being read as
    # an Anthropic endpoint by a future heuristic. See :mod:`zimzilla.flex`.
    "eu.api.flex.ai",
    # Gemini's OpenAI-compatible surface. Its base is ``/v1beta/openai``, not
    # ``/v1``, so the path rule below would append a second path and 404. See
    # :mod:`zimzilla.gemini`.
    "generativelanguage.googleapis.com",
)

#: Hosts whose Chat Completions route is a *playground* rather than an API, and
#: which therefore need three adjustments this transport would not otherwise
#: make. Measured on Flex's route, 2026-11-07:
#:
#: * **The reply is always SSE**, even for ``stream: false`` — so the
#:   non-streaming path would try to parse an event stream as one JSON object
#:   and read an empty reply.
#: * **Failures arrive as 200** with an SSE frame
#:   ``{"error": {"code": "rate_limit" | "generic", …}}``. A status check sees
#:   success, so the frame has to be mapped onto a status the retry policy
#:   understands.
#: * **The history is capped** at 10 messages (system included) and 2000
#:   characters in the last one, enforced with a 400.
PLAYGROUND_HOSTS = ("eu.api.flex.ai",)

#: Hosts whose OpenAI-compatible base is not ``/v1``. Gemini serves it at
#: ``/v1beta/openai``, so the ``/v1`` rule in :func:`chat_completions_url` and
#: :func:`models_url` would append a second path and 404. Named here rather than
#: inlined so the two functions cannot drift apart. See :mod:`zimzilla.gemini`.
GEMINI_HOSTS = ("generativelanguage.googleapis.com",)

#: The playground's published limits, used to fit a request before it is sent.
#: Read from ``/api/playground/config`` on 2026-11-07; ``zimzilla.flex`` carries
#: the same two numbers and the command that shows them.
PLAYGROUND_MAX_MESSAGES = 10
PLAYGROUND_MAX_MESSAGE_CHARS = 2000

#: Appended to a message the playground cap forced us to cut, so a model reading
#: a truncated tool result knows it is reading one.
PLAYGROUND_TRIM_MARK = (
    "\n…[trimmed: this playground caps the last message at "
    f"{PLAYGROUND_MAX_MESSAGE_CHARS} chars]"
)

#: Error codes an SSE frame uses for a condition that clears on its own. Mapped
#: onto 429 so ``_stream_once``'s backoff retries them instead of failing the
#: turn — measured, Flex's busy condition clears in seconds and the identical
#: request succeeds on the next attempt.
_TRANSIENT_FRAME_CODES = frozenset({"rate_limit", "rate_limited", "overloaded",
                                    "busy", "capacity"})


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


def is_playground(base_url: str) -> bool:
    """Whether *base_url* is a playground route rather than an API.

    Playgrounds need three adjustments this transport does not otherwise make —
    always-SSE replies, failures delivered as a 200 with an error frame, and a
    capped history. See :data:`PLAYGROUND_HOSTS`.
    """
    return _host_matches(_host_of(base_url), PLAYGROUND_HOSTS)


def chat_completions_url(base_url: str) -> str:
    """The Chat Completions endpoint for *base_url*.

    A URL that already ends in ``/chat/completions`` is returned untouched, so
    an operator can paste the exact endpoint from a provider's docs. A URL that
    ends in ``/v1`` only gains the last segment; anything else gains the whole
    ``/v1/chat/completions``.

    A playground is the exception: its route *is* the endpoint
    (``/api/playground/chat``), with no ``/v1`` convention to append to, so the
    URL is returned as given. Appending would produce
    ``…/playground/chat/v1/chat/completions``, which answers 404.
    """
    url = (base_url or "").strip().rstrip("/")
    if is_playground(url):
        return url
    if url.endswith("/chat/completions"):
        return url
    # Gemini's OpenAI-compatible base is ``/v1beta/openai``, which is neither a
    # ``/v1`` base nor the endpoint itself. The generic rule would produce
    # ``.../v1beta/openai/v1/chat/completions`` and 404, so the last segment is
    # appended to the base as given. A bare host (or the native ``/v1beta``
    # surface) is rewritten to the ``/openai`` path that speaks this protocol.
    if _host_matches(_host_of(url), GEMINI_HOSTS):
        if url.endswith("/v1beta/openai"):
            return url + "/chat/completions"
        if url.endswith("/v1beta"):
            return url + "/openai/chat/completions"
        return url + "/v1beta/openai/chat/completions"
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + CHAT_PATH


def models_url(base_url: str) -> str:
    """The Models endpoint for *base_url*, for probing a route.

    A playground has none: its route is the chat endpoint itself and there is no
    catalog to list (Flex answers 404 on ``/api/playground/models`` and 401 on
    the authenticated ``/v1/models``). The chat URL is returned so a caller that
    probes anyway gets a 405 from the route rather than a 404 from a path that
    was invented here — and ``probe`` short-circuits playgrounds before it ever
    gets this far.
    """
    url = (base_url or "").strip().rstrip("/")
    if is_playground(url):
        return url
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")] + "/models"
    # Same exception as chat_completions_url, for the same reason: Gemini's
    # base is ``/v1beta/openai`` and the generic rule would append ``/v1``.
    if _host_matches(_host_of(url), GEMINI_HOSTS):
        if url.endswith("/v1beta/openai"):
            return url + "/models"
        if url.endswith("/v1beta"):
            return url + "/openai/models"
        return url + "/v1beta/openai/models"
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


def _image_part(block: dict) -> dict | None:
    """One Anthropic image block -> a Chat Completions ``image_url`` part.

    A block stored in the conversation carries a path, not bytes — see
    ``zimzilla.images``. The data is read here, at the boundary, which keeps
    the history small and means a screenshot written to a case directory is
    still the file that gets sent. A missing file drops the part rather than
    failing the request: the text the tool also returned is the useful half.
    """
    from . import images as images_mod

    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    data = source.get("data")
    media_type = source.get("media_type") or block.get("media_type")
    if not data:
        prepared = images_mod.prepare(block.get("_path") or "")
        if prepared is None:
            return None
        data = base64.b64encode(prepared.data).decode("ascii")
        media_type = prepared.media_type
    if not media_type:
        return None
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{data}",
                      "detail": "auto"},
    }


def _block_images(block: dict) -> list[dict]:
    """Every image inside one Anthropic block, flattened."""
    if block.get("type") == "image":
        return [block]
    if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
        return [b for b in block["content"]
                if isinstance(b, dict) and b.get("type") == "image"]
    return []


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

        # user: text, tool results, and images. Chat Completions has no image
        # in a ``tool`` message, so an image that arrived inside a tool result
        # is lifted out and sent in the user message that follows the tool
        # messages — the same conversation, in the only shape this protocol
        # can carry it.
        texts = []
        tool_messages: list[dict] = []
        image_blocks: list[dict] = []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                if block.get("text"):
                    texts.append(block["text"])
            elif kind == "tool_result":
                body = _text_of(block.get("content"))
                image_blocks.extend(_block_images(block))
                if block.get("is_error"):
                    body = f"Error: {body}" if body else "Error"
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or "",
                    "content": body or "(no output)",
                })
            elif kind == "image":
                image_blocks.append(block)
            elif kind == "document":
                texts.append("[document omitted — this endpoint takes text and "
                             "images only]")

        if tool_messages:
            out.extend(tool_messages)
        if image_blocks:
            parts: list[dict] = []
            if texts:
                parts.append({"type": "text", "text": "\n\n".join(texts)})
            for image in image_blocks:
                part = _image_part(image)
                if part is not None:
                    parts.append(part)
                else:
                    # The block is there but its file is gone or it carries no
                    # data. Saying so keeps the turn's shape visible instead of
                    # silently dropping the only thing that was attached.
                    parts.append({"type": "text",
                                  "text": "[image omitted — not readable]"})
            if parts:
                out.append({"role": "user", "content": parts})
        elif texts:
            _append_text(out, "user", "\n\n".join(texts))

    return out


def wire_message_count(system: Any, messages: list[dict]) -> int:
    """How many messages the wire will actually carry for this history.

    Not ``len(messages)``, and the difference is what a capped route trips on.
    One Anthropic user message can become several Chat Completions messages:
    each ``tool_result`` block is its own ``role: tool`` message, and the system
    prompt is a message of its own. Measured on a three-message history holding
    three tool results, the wire carried six.

    A playground caps the *wire* count (Flex: ten, system included), so a
    trigger that counted the Anthropic history would under-count by one per
    parallel tool call and let the transport trim the oldest turns silently —
    the exact loss the count trigger exists to prevent. Counting by running the
    same translation the request will run is the only way the two cannot
    disagree.
    """
    return len(translate_messages(system, messages))


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


#: Tool calls a model writes as *text* rather than through ``delta.tool_calls``.
#:
#: Some routes have no tool-call channel at all. Flex's playground accepts a
#: ``tools`` array and then answers with prose — measured, zero
#: ``delta.tool_calls`` frames on every model it serves. A model that has been
#: told about tools anyway does the only thing it can: it writes the call out in
#: the markup its training used. Measured on DeepSeek-V4-Flash through Flex:
#:
#:     <tool_calls>
#:     <invoke name="exec_command">
#:     <parameter name="cmd">ls -la /home/zim/bug-hunt/web/plscr/</parameter>
#:     </invoke>
#:     </tool_calls>
#:
#: Left alone, that reaches the transcript as literal markup and the tool never
#: runs — the operator sees the agent "talking about" running a command instead
#: of running it. So the text is parsed back into real calls.
#:
#: Deliberately narrow: it matches the ``<invoke>``/``<parameter>`` shape only,
#: because that is the one measured. A looser pattern would turn any prose that
#: happens to mention a tag into a tool call, which is a worse failure than
#: showing the markup.
#: The tag prefix is matched rather than assumed. DeepSeek writes its calls with
#: a DSML namespace and fullwidth bars — ``<｜DSML｜invoke name="bash">`` — which
#: is the same shape with a prefix in front of the keyword. Matching the prefix
#: as "anything that is not a tag delimiter" covers both that and the plain
#: ``<invoke>`` form, without a second pattern to keep in step.
_INVOKE_RE = re.compile(
    r"<([^<>]*?)invoke\s+name=\"([^\"]+)\"\s*>(.*?)</([^<>]*?)invoke\s*>",
    re.S | re.I)
_PARAM_RE = re.compile(
    r"<([^<>]*?)parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)</([^<>]*?)parameter\s*>",
    re.S | re.I)
#: The wrapper the calls sit inside, removed with them so no empty shell is left
#: in the transcript. Same prefix tolerance, for the same reason.
_TOOLCALLS_RE = re.compile(
    r"<([^<>]*?)tool_calls\s*>(.*?)</([^<>]*?)tool_calls\s*>", re.S | re.I)

#: How many trailing characters are held back from the live stream so a marker
#: split across two chunks is never emitted as prose. Sized for the longest
#: marker *including* a namespace prefix — DeepSeek writes
#: ``<｜DSML｜tool_calls>``, which is 20 characters before the keyword even starts.
#: Generous on purpose: holding back a few extra characters costs nothing, and
#: a marker that slips through is shown to the operator as literal markup.
_MARKER_HOLDBACK = 32


#: The keywords that open a call. Matched as *keywords*, never as a literal
#: ``<invoke``: DeepSeek writes ``<｜DSML｜invoke``, where the namespace is part
#: of the tag and not part of the keyword. A literal test rejects the very shape
#: the regexes below accept, which is how the parser came to be dead code for
#: the one model it was written for — the guard returned early, the markup went
#: to the transcript as prose, and the tool never ran.
_CALL_KEYWORDS = ("invoke", "tool_calls")


def _has_call_markup(text: str) -> bool:
    """Whether *text* mentions a call keyword at all, prefix or not."""
    lowered = text.lower()
    return any(keyword in lowered for keyword in _CALL_KEYWORDS)


def _looks_like_markup(text: str) -> bool:
    """Whether *text* is tool-call markup, even if it did not parse.

    A call the model wrote badly — truncated, or with a missing closing tag —
    is still markup, and streaming it puts a wall of tags in the transcript. The
    test is deliberately loose: it only has to catch text that is *mostly* tags,
    so ordinary prose that mentions a tag is not swallowed.
    """
    if not _has_call_markup(text):
        return False
    return "<" in text and text.count("<") >= 2


def parse_text_tool_calls(text: str) -> tuple[str, list[dict]]:
    """Split *text* into (prose, tool calls written as markup).

    Returns the text with the call markup removed, and the calls it contained as
    ``{"name": ..., "input": {...}}``. A reply with no markup comes back
    unchanged with an empty list, so this is safe to run on every response.

    The parameters are kept as strings: the model writes them as text, and a
    value like ``"100"`` is a string in the markup but an integer in the tool's
    schema. Coercing here would guess; the tool's own validation is the place
    that knows, and it reports a wrong type far more usefully than a silent
    conversion would.
    """
    if not text or not _has_call_markup(text):
        return text, []
    calls: list[dict] = []
    for match in _INVOKE_RE.finditer(text):
        name = (match.group(2) or "").strip()
        if not name:
            continue
        body = match.group(3) or ""
        args: dict = {}
        for param in _PARAM_RE.finditer(body):
            key = (param.group(2) or "").strip()
            if key:
                args[key] = (param.group(3) or "").strip()
        calls.append({"name": name, "input": args})
    if not calls:
        return text, []
    # Remove the calls, then the wrapper they sat in, then tidy the blank lines
    # the removal leaves behind — a transcript with a hole in it reads as a
    # truncated reply.
    prose = _INVOKE_RE.sub("", text)
    prose = _TOOLCALLS_RE.sub("", prose)
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
    return prose, calls


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
        #: Text held back from the live stream because it might be the start of
        #: a tool call written as markup. See :meth:`_emit_text`.
        self._held = ""
        #: Set once a call marker has been seen: from then on nothing is
        #: streamed as text, because everything after it belongs to the call.
        self._in_call = False

    # -- text, with the markup held back ---------------------------------
    def _emit_text(self, text: str) -> list[SimpleNamespace]:
        """Stream *text*, holding back anything that might be a tool call.

        A route with no tool-call channel makes the model write its call as
        markup in the text, and that markup arrives through the same
        ``text_delta`` path as ordinary prose. Streaming it straight out puts
        ``<tool_calls><invoke name="bash">…`` in the transcript, which is what
        the operator sees instead of the command running.

        So the tail is held back by the width of the longest marker prefix, and
        once a marker is seen the rest of the reply is held entirely — it is
        parsed into real calls by :meth:`message`. Prose before the first marker
        still streams live, which is the part worth watching.
        """
        if self._in_call:
            self._held += text
            return []
        self._held += text
        lowered = self._held.lower()
        # The marker may carry a namespace prefix (DeepSeek's DSML), so the
        # search is for the keyword rather than for a literal "<invoke". The
        # opening angle bracket is then walked back to, so the prefix goes with
        # the call instead of being left in the prose.
        cut = -1
        for keyword in ("invoke", "tool_calls"):
            found = lowered.find(keyword)
            if found < 0:
                continue
            start = lowered.rfind("<", 0, found)
            if start < 0:
                continue
            if cut < 0 or start < cut:
                cut = start
        if cut >= 0:
            # Everything before the marker is prose and is safe to send.
            safe, self._held = self._held[:cut], self._held[cut:]
            self._in_call = True
            if not safe:
                return []
            self.text.append(safe)
            return [_event("text_delta", text=safe)]
        # No marker yet. Emit all but the last few characters, so a marker that
        # is still arriving is never cut in half.
        keep = _MARKER_HOLDBACK
        if len(self._held) <= keep:
            return []
        safe, self._held = self._held[:-keep], self._held[-keep:]
        if not safe:
            return []
        self.text.append(safe)
        return [_event("text_delta", text=safe)]

    def flush_text(self) -> list[SimpleNamespace]:
        """Emit whatever is still held back, once the stream has ended.

        A reply shorter than the holdback is *entirely* held, so without this a
        one-word answer would stream as nothing at all. The held text is scanned
        for a call first: a call that fits inside the holdback never left the
        buffer, and emitting it as prose is the bug this whole path exists to
        avoid.
        """
        held, self._held = self._held, ""
        if not held:
            return []
        # The held text is always recorded, because `message()` parses the calls
        # out of it. What is *streamed* is only the prose: emitting the markup
        # here is the bug this whole path exists to prevent, and it is easy to
        # reintroduce by returning `held` when no call parses — a truncated or
        # malformed call is still markup, and still must not be shown.
        self.text.append(held)
        prose, calls = parse_text_tool_calls(held)
        if calls:
            return [_event("text_delta", text=prose)] if prose else []
        # No call parsed. If the text still looks like markup, it is a call the
        # model wrote badly — shown as nothing rather than as a wall of tags.
        if _looks_like_markup(held):
            return []
        return [_event("text_delta", text=held)]

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
                # Held back rather than streamed straight out: the model may be
                # writing a tool call as markup. See `_emit_text`.
                events.extend(self._emit_text(text))
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
                    events.extend(self._emit_text(text))
                    events.extend(self.flush_text())
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
        elif isinstance(arguments, dict):
            # Some servers send the arguments as an object rather than the JSON
            # string the spec calls for. Concatenating would raise; serialising
            # keeps the call usable, and ``parse_arguments`` reads it back.
            slot["arguments"] += json.dumps(arguments, ensure_ascii=False)

    # -- result ----------------------------------------------------------
    def message(self) -> Message:
        blocks: list[Block] = []
        # Whatever the holdback is still sitting on belongs in the reply. The
        # streaming path flushes it when the stream ends, but the non-streaming
        # one (``_feed_sse_body``, and any caller that only wants the message)
        # never does — and a reply shorter than the holdback is *entirely* held,
        # so without this a two-word answer comes back empty. Idempotent: after
        # a flush there is nothing left to append.
        if self._held:
            self.text.append(self._held)
            self._held = ""
        text = "".join(self.text)
        # A route with no tool-call channel makes the model write its call as
        # markup in the text. Parsed back into real calls here, so the tool
        # actually runs instead of the operator being shown the markup. See
        # ``parse_text_tool_calls``.
        text, text_calls = parse_text_tool_calls(text)
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
        # Calls recovered from the text come after the real ones, and are keyed
        # past them so their ids cannot collide with a server-assigned id.
        for offset, call in enumerate(text_calls):
            blocks.append(Block(
                type="tool_use",
                id=f"call_text_{offset}",
                name=call["name"],
                input=call["input"],
            ))
        stop = STOP_REASONS.get((self.finish_reason or "").lower(), "end_turn")
        # A server that streams calls but never sends a finish_reason still
        # asked for tools; treating that as end_turn would end the turn with
        # the work undone. The same is true of a call recovered from the text:
        # the model asked for a tool, so the turn is not finished.
        if any(b.type == "tool_use" for b in blocks) and stop == "end_turn":
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


def _looks_like_sse(raw: bytes) -> bool:
    """Whether a body is an event stream rather than one JSON object.

    A playground answers SSE even to a non-streaming request, so the
    non-streaming path has to notice. Checked on the first non-blank line rather
    than on the content type: the route sends
    ``text/event-stream; charset=utf-8``, but a proxy in front of one may not,
    and the bytes are the thing that cannot be wrong.
    """
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        return line.startswith("data:") or line.startswith(":")
    return False


def _feed_sse_body(accumulator: "Accumulator", raw: bytes, url: str,
                   status: int, headers: dict) -> None:
    """Feed a whole SSE body into *accumulator*, raising on an error frame.

    The non-streaming twin of ``_Stream._iterate``: same frame handling, same
    error mapping, no iteration. Used when a server answers an event stream to a
    request that asked for one object.
    """
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        data = line[len("data:"):].strip() if line.startswith("data:") else line
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("error"):
            raise error_for(_frame_status(obj, status),
                            json.dumps(obj).encode(), headers, url)
        accumulator.feed_chunk(obj)


def _frame_status(obj: dict, status: int) -> int:
    """The status an in-stream error frame should be treated as.

    A playground delivers failures as a **200** with an SSE frame
    ``{"error": {"code": …, "message": …}}``, so the HTTP status says success
    and the frame is the whole story. Mapping the frame's own code onto a status
    is what lets the existing retry policy act on it: ``rate_limit`` becomes a
    429, which ``_stream_once`` backs off and retries — measured on Flex, the
    busy condition clears in seconds and the identical request then succeeds.

    A frame on a real error status keeps that status: a 400 that also carries a
    frame is a bad request, not a transient condition.
    """
    if status and status >= 400:
        return status
    error = obj.get("error")
    code = ""
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip().lower()
    if code in _TRANSIENT_FRAME_CODES:
        return 429
    return 400


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


def _count_limit_from(message: str) -> int | None:
    """The count a provider's 400 names, for a message or character cap.

    Matched on ``max N`` and ``maximum N`` — the two spellings measured on Flex's
    playground (``"Maximum 10 messages allowed"``, ``"Message content too long
    (max 2000 characters)"``). Narrow on purpose: the same sentence often names
    a *context* window, which is not the number to clamp a message to.
    """
    for pattern in (r"maximum\s+(\d{1,7})", r"max\s+(\d{1,7})"):
        match = re.search(pattern, message, re.IGNORECASE)
        if match is None:
            continue
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
            continue
        if value > 0:
            return value
    return None


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
        # A playground's own caps, read out of its sentence rather than assumed:
        # "Maximum 10 messages allowed" and "Message content too long (max 2000
        # characters)". The payload builder already fits a request to the
        # published numbers, so reaching here means the endpoint moved them —
        # and the sentence is the only place the new number is stated.
        if "messages" in message and "maximum" in message:
            limit = _count_limit_from(message)
            messages = payload.get("messages")
            if limit and isinstance(messages, list) and len(messages) > limit:
                start = 1 if messages and messages[0].get("role") == "system" else 0
                del messages[start:start + (len(messages) - limit)]
                return True
        if "too long" in message and "characters" in message:
            limit = _count_limit_from(message)
            messages = payload.get("messages")
            if limit and isinstance(messages, list) and messages:
                last = messages[-1]
                content = last.get("content")
                if isinstance(content, str) and len(content) > limit:
                    last["content"] = content[:limit]
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
                    raise error_for(_frame_status(obj, self._resp.status_code),
                                    json.dumps(obj).encode(), dict(self._resp.headers),
                                    self._client.url)
                for event in self._acc.feed_chunk(obj):
                    yield event
        except httpx.HTTPError as e:
            # A connection dropped mid-reply is a transient failure like any
            # other: the loop retries it, and the operator is told which endpoint
            # went quiet rather than handed an httpx traceback.
            raise _connection_error(e, self._client.url) from e
        # Whatever the holdback kept back is emitted now. A reply shorter than
        # the marker is entirely held, so without this a one-word answer would
        # stream as nothing at all.
        for event in self._acc.flush_text():
            yield event
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
                    # A playground answers SSE whatever was asked for, so the
                    # body may be an event stream even though this call wanted
                    # one object. Reading it as JSON would yield an empty reply
                    # and no error, which is the worst of both.
                    if _looks_like_sse(raw):
                        _feed_sse_body(accumulator, raw, self._client.url,
                                       response.status_code,
                                       dict(response.headers))
                    else:
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

        if is_playground(self.base_url):
            payload = _playground_payload(payload)
        return payload


def _playground_payload(payload: dict) -> dict:
    """Fit a request to a playground's published limits, in place.

    Three adjustments, each measured against Flex's route on 2026-11-07 and each
    enforced by the endpoint rather than by this harness:

    * **``stream`` is forced on.** The route answers SSE whatever is asked for,
      so a non-streaming request would be parsed as one JSON object and read as
      an empty reply. ``stream_options`` is dropped with it: the route sends no
      usage block, and asking for one is a field it does not implement.
    * **The history is trimmed to the message cap.** The system message counts
      toward it, so the oldest *conversation* turns go first and the system
      message is kept — dropping it would silently change the model's
      instructions, which is a worse failure than losing old context.
    * **The last message is trimmed to the character cap.** Only the last one is
      checked by the endpoint (5000-character earlier messages passed), so this
      is the only place a cut is needed. A tool result is the message that
      usually overflows, and a cut one is marked so the model knows.

    A playground that changes its limits will answer 400 with a sentence naming
    the new number; ``_retry_payload`` reads it and shrinks to fit, so this is a
    starting point rather than a hardcoded truth.
    """
    payload["stream"] = True
    payload.pop("stream_options", None)

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload

    # The cap counts every message, system included.
    overflow = len(messages) - PLAYGROUND_MAX_MESSAGES
    if overflow > 0:
        # Drop from the front, but never the system message: it is the model's
        # instructions, and losing it changes the answer rather than shortening
        # the history. A leading system message is the only one
        # ``translate_messages`` ever emits, so index 0 is the whole test.
        start = 1 if messages and messages[0].get("role") == "system" else 0
        del messages[start:start + overflow]

    if messages:
        last = messages[-1]
        content = last.get("content")
        if isinstance(content, str) and len(content) > PLAYGROUND_MAX_MESSAGE_CHARS:
            keep = PLAYGROUND_MAX_MESSAGE_CHARS - len(PLAYGROUND_TRIM_MARK)
            last["content"] = content[:max(0, keep)] + PLAYGROUND_TRIM_MARK
    return payload


def probe(base_url: str, api_key: str | None = None, timeout: float = 8.0) -> tuple[bool, str]:
    """Ask an endpoint's Models route whether it is there and what it serves.

    Deliberately synchronous and short: the caller runs it off the UI thread,
    and its job is to turn "I typed the URL wrong" from a failed first turn
    into an immediate, specific answer.

    A playground has no Models route, so it is checked with a real chat call
    instead — the only thing that proves a route with no catalog and no key.
    """
    if is_playground(base_url):
        from . import flex as flex_mod
        return flex_mod.verify(timeout=timeout)
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
