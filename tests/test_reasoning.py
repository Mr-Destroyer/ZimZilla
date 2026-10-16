"""Regression suite for the model-reasoning display.

Run:  ./.venv/bin/python tests/test_reasoning.py

The operator asked:

    "so in the deepseek or opencode when I use this deepseek v 4.1 flash model
     they show the thinking too , like what is the model thinking which is
     usesful , I want you to add a thinking part in the ui where it shows what
     the model is thinking"

What that required, and what is pinned here:

The Anthropic protocol carries a model's reasoning as a ``thinking`` content
block — a ``content_block_start`` of type ``thinking``, then a run of
``content_block_delta`` events whose delta type is ``thinking_delta`` and whose
text lives on ``delta.thinking`` (NOT ``delta.text``; the two deltas are
different classes). ``_stream_once`` handled exactly one delta type —
``text_delta`` — so every ``thinking_delta`` was discarded before the UI could
see it. Measured against OpenRouter 2026-10-16: 107 ``thinking_delta`` events
for one prompt, all silently dropped.

So the checks below are about two things:

1. **Forwarding.** ``_stream_once`` routes ``thinking_delta`` to a
   ``reasoning_delta`` event, keeps ``text_delta`` on ``text_delta``, and
   preserves their order. The event is deliberately NOT called ``thinking``:
   that name is already taken by the spinner stage ("a turn is in flight"), and
   reusing it would light the rail instead of showing content.
2. **Rendering.** The live form shows the tail and elides the rest; the
   committed form keeps every line. Both must be dim/italic and neither may
   raise on empty input.

Not covered here, and deliberately so: whether a given endpoint *sends*
thinking blocks. That is a property of the endpoint, not of this code — an
Anthropic-native one (OpenRouter) does, while LiteLLM's ``/v1/messages``
bridge drops reasoning entirely, so Token Juice's ``deepseek-v4.1-flash``
sends none. The code must therefore be correct and silent when none arrives,
which check 4 covers.

No network: the SDK stream is faked, as ``test_agent_limits`` does.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.theme import get_palette  # noqa: E402
from zimzilla.ui import renderers as R  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


# ---------------------------------------------------------------------------
# Fakes — the shape the SDK actually hands `_stream_once`
# ---------------------------------------------------------------------------

class _Delta:
    """One `content_block_delta` payload. Only the fields its type carries."""

    def __init__(self, dtype: str, **fields):
        self.type = dtype
        self.__dict__.update(fields)


class _Event:
    def __init__(self, etype: str, delta=None):
        self.type = etype
        self.delta = delta


class _Blk:
    def __init__(self, **k):
        self.__dict__.update(k)

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


class _Msg:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = None


class _Stream:
    """A stand-in for `client.messages.stream(...)`'s async context manager."""

    def __init__(self, events, final):
        self._events = events
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for e in self._events:
            yield e

    async def get_final_message(self):
        return self._final


class _Messages:
    def __init__(self, stream):
        self._stream = stream

    def stream(self, **kwargs):
        return self._stream


class _Client:
    def __init__(self, stream):
        self.messages = _Messages(stream)


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


def _drain(agent: Agent, **kw):
    """Run the real `_stream_once` and collect just the events it yields."""
    async def run():
        out = []
        async for ev, _final in agent._stream_once(**kw):
            if ev is not None:
                out.append(ev)
        return out
    return asyncio.run(run())


def _agent_with(stream, tmp: Path, **kw) -> Agent:
    agent = Agent(_cfg(tmp, **kw))
    agent._client = _Client(stream)
    return agent


# ---------------------------------------------------------------------------
# 1 — a thinking block is forwarded, as its own event type
# ---------------------------------------------------------------------------

def test_thinking_is_forwarded(tmp: Path) -> None:
    """The core of the ask: `thinking_delta` must reach the UI as content."""
    events = [
        _Event("content_block_start"),
        _Event("content_block_delta", _Delta("thinking_delta", thinking="We need ")),
        _Event("content_block_delta", _Delta("thinking_delta", thinking="to parse this.")),
        _Event("content_block_stop"),
    ]
    agent = _agent_with(_Stream(events, _Msg([_Blk(type="text", text="18")])), tmp)
    got = _drain(agent)

    kinds = [e["type"] for e in got]
    check("thinking_delta yields reasoning_delta",
          kinds == ["reasoning_delta", "reasoning_delta"], f"got {kinds}")
    check("reasoning text comes off delta.thinking",
          "".join(e["text"] for e in got) == "We need to parse this.",
          repr("".join(e["text"] for e in got)))
    check("the event is NOT named 'thinking' (spinner collision)",
          all(e["type"] != "thinking" for e in got), f"got {kinds}")


# ---------------------------------------------------------------------------
# 2 — text is untouched, and the two interleave in arrival order
# ---------------------------------------------------------------------------

def test_text_still_works_and_order_holds(tmp: Path) -> None:
    """A thinking block must not disturb the answer that follows it."""
    events = [
        _Event("content_block_start"),
        _Event("content_block_delta", _Delta("thinking_delta", thinking="reasoning")),
        _Event("content_block_stop"),
        _Event("content_block_start"),
        _Event("content_block_delta", _Delta("text_delta", text="The answer ")),
        _Event("content_block_delta", _Delta("text_delta", text="is 18.")),
        _Event("content_block_stop"),
    ]
    agent = _agent_with(_Stream(events, _Msg([_Blk(type="text", text="The answer is 18.")])), tmp)
    got = _drain(agent)

    kinds = [e["type"] for e in got]
    check("order preserved across the two delta types",
          kinds == ["reasoning_delta", "text_delta", "text_delta"], f"got {kinds}")
    check("text_delta still reads delta.text",
          "".join(e["text"] for e in got if e["type"] == "text_delta") == "The answer is 18.",
          "")


# ---------------------------------------------------------------------------
# 3 — a reasoning-only reply (no text at all) does not crash or invent text
# ---------------------------------------------------------------------------

def test_reasoning_only(tmp: Path) -> None:
    """A model that thinks and then stops: no text block, no exception."""
    events = [
        _Event("content_block_delta", _Delta("thinking_delta", thinking="Hmm.")),
    ]
    agent = _agent_with(_Stream(events, _Msg([_Blk(type="thinking", thinking="Hmm.")])), tmp)
    got = _drain(agent)

    kinds = [e["type"] for e in got]
    check("a reasoning-only stream yields only reasoning_delta",
          kinds == ["reasoning_delta"], f"got {kinds}")


# ---------------------------------------------------------------------------
# 4 — an endpoint that sends no thinking behaves exactly as before
# ---------------------------------------------------------------------------

def test_no_thinking_endpoint_is_unchanged(tmp: Path) -> None:
    """The LiteLLM-proxy case: nothing new arrives, nothing new is emitted."""
    events = [
        _Event("content_block_delta", _Delta("text_delta", text="391")),
    ]
    agent = _agent_with(_Stream(events, _Msg([_Blk(type="text", text="391")])), tmp)
    got = _drain(agent)

    kinds = [e["type"] for e in got]
    check("no thinking in -> no reasoning_delta out",
          kinds == ["text_delta"], f"got {kinds}")


# ---------------------------------------------------------------------------
# 5 — the ceiling override still works (the reasoning-exhaustion retry path)
# ---------------------------------------------------------------------------

def test_ceiling_override_still_accepted(tmp: Path) -> None:
    """`_stream_once(max_tokens=...)` must survive the added branch."""
    captured = {}

    class _Capturing(_Messages):
        def stream(self, **kwargs):
            captured.update(kwargs)
            return self._stream

    agent = Agent(_cfg(tmp))
    agent._client = _Client(_Stream([], _Msg([_Blk(type="text", text="ok")])))
    agent._client.messages = _Capturing(agent._client.messages._stream)

    _drain(agent, max_tokens=65536)
    check("explicit max_tokens reaches the request",
          captured.get("max_tokens") == 65536, f"got {captured.get('max_tokens')}")


# ---------------------------------------------------------------------------
# 6 — the renderables
# ---------------------------------------------------------------------------

def test_renderables(tmp: Path) -> None:
    """The live form windows the tail; the committed form keeps everything."""
    p = get_palette("green")

    long_text = "\n".join(f"line {i}" for i in range(1, 41))
    live = R.reasoning_live(long_text, p)
    flat = live.plain
    check("live view elides the head and says how much",
          "(+26 earlier)" in flat and "line 40" in flat, repr(flat[:80]))
    check("live view shows at most THINK_LIVE_LINES lines",
          flat.count("\n") <= R.THINK_LIVE_LINES + 1,
          f"{flat.count(chr(10))} newlines")
    check("live view is labelled",
          flat.startswith("✻ thinking"), repr(flat[:20]))

    panel = R.reasoning_panel(long_text, p)
    body = panel.renderable.plain
    check("committed panel keeps every line",
          all(f"line {i}" in body for i in (1, 20, 40)), "")
    check("committed panel reports its line count",
          "40 lines" in str(panel.subtitle), "")

    # Empty input must not raise — a turn can end with a blank buffer.
    try:
        R.reasoning_live("", p)
        R.reasoning_panel("", p)
        check("renderables tolerate empty reasoning", True, "")
    except Exception as e:  # noqa: BLE001
        check("renderables tolerate empty reasoning", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 7 — the config toggle
# ---------------------------------------------------------------------------

def test_show_thinking_default(tmp: Path) -> None:
    """On by default: it costs nothing where the endpoint sends none."""
    check("show_thinking defaults on", Config().show_thinking is True, "")
    check("--no-think maps to False",
          _cfg(tmp, show_thinking=False).show_thinking is False, "")


# ---------------------------------------------------------------------------

def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_thinking_is_forwarded(tmp)
        test_text_still_works_and_order_holds(tmp)
        test_reasoning_only(tmp)
        test_no_thinking_endpoint_is_unchanged(tmp)
        test_ceiling_override_still_accepted(tmp)
        test_renderables(tmp)
        test_show_thinking_default(tmp)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
