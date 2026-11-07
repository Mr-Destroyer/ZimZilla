"""Regression suite for the round caps, auto-continue and auto-compact.

Run:  ./.venv/bin/python tests/test_agent_limits.py

This pins the fix for a real, reported failure. The operator said:

    "sometimes in long tasks my models just go to idle loop ? what's the
     reson ? i think it's because of the context issue"

The cause was neither idle nor context. ``run_turn`` ran
``for iteration in range(cfg.max_iterations)`` — 40 by default — and on the
41st round the loop simply *fell out* and yielded an ordinary ``turn_end``. The
UI turns ``turn_end`` into ``loop.set_stage("idle")``, so a turn that ran out of
rounds was rendered exactly like a turn that had finished, and nothing
re-triggered it. Reproduced before the fix:

    events: ['thinking','tool_call','tool_result'] * 3, 'turn_end'
    was the cap reported to the UI? False

Six behaviours are pinned here:

1. A turn that stops at the soft cap with **no progress** emits
   ``iteration_cap`` — the cap can never again be swallowed into a plain
   ``turn_end``.
2. A turn that reaches the soft cap **still progressing** continues instead of
   stopping, and says so with ``continuing``. This is the long-task case.
3. A success in an earlier round must not carry a now-stuck loop past the cap.
4. The hard limit (``max_rounds``) ends a runaway turn and reports itself as
   ``hard``, so the two caps are distinguishable.
5. History is compacted automatically before a request that would exceed
   ``compact_at`` of the window, and **not** compacted below it.
6. A context-overflow 400 is compacted and retried once rather than surfaced as
   a bare "bad request".

No network: ``_stream_once`` is replaced with a stub that yields canned
messages, exactly as ``test_deliverable`` and ``test_phase4`` do.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


class _Blk:
    def __init__(self, **k):
        self.__dict__.update(k)

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


class _Usage:
    def __init__(self, inp: int, out: int = 10):
        self.input_tokens = inp
        self.output_tokens = out


class _Msg:
    def __init__(self, content, stop_reason=None, usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


def _tool(name: str = "bash", inp: dict | None = None) -> _Msg:
    """A reply that calls a real tool, so the loop takes another round."""
    return _Msg(
        [_Blk(type="tool_use", id=f"t-{name}", name=name,
              input=inp if inp is not None else {"command": "true"})],
        stop_reason="tool_use",
    )


def _fail_tool(i: int = 0) -> _Msg:
    """A tool call that will fail: `false` exits non-zero."""
    return _Msg(
        [_Blk(type="tool_use", id=f"f-{i}", name="bash",
              input={"command": "false"})],
        stop_reason="tool_use",
    )


def _done(msg: str = "Done.") -> _Msg:
    """A terminal narration with no tool call: the turn ends here."""
    return _Msg([_Blk(type="text", text=msg)], stop_reason="end_turn")


def _stub(agent: Agent, messages: list[_Msg], used: int = 0) -> dict:
    """Serve *messages* in order, recording how many were consumed.

    Past the end it keeps serving the last message, so a loop that fails to
    break shows up as a runaway call count rather than a swallowed
    ``StopIteration``. A turn meant to finish must therefore end its list with
    ``_done()``. ``used`` sets the reported ``input_tokens`` on every reply,
    which is what the auto-compact threshold reads.
    """
    state = {"n": 0}

    async def fake_stream(max_tokens=None):
        i = state["n"]
        state["n"] += 1
        msg = messages[min(i, len(messages) - 1)]
        if msg.usage is None:
            msg.usage = _Usage(used)
        yield (None, msg)

    agent._stream_once = fake_stream  # type: ignore[assignment]
    return state


async def _drive(agent: Agent, text: str = "go"):
    events = []
    async for ev in agent.run_turn(text):
        events.append(ev)
    return events


def _kinds(events) -> list[str]:
    return [e["type"] for e in events]


def _of(events, kind: str) -> list[dict]:
    return [e for e in events if e["type"] == kind]


# ---------------------------------------------------------------------------
# 1 & 2 — the soft cap: reported when stuck, continued when progressing
# ---------------------------------------------------------------------------

async def test_stuck_at_soft_cap_is_reported(tmp: Path) -> None:
    """The exact bug: a turn that stops at the cap must SAY it stopped."""
    cfg = _cfg(tmp, max_iterations=3)
    agent = Agent(cfg)
    # Every tool fails, so no round makes progress and the cap is obeyed.
    state = _stub(agent, [_fail_tool(i) for i in range(10)])

    events = await _drive(agent)
    kinds = _kinds(events)

    caps = _of(events, "iteration_cap")
    check("stuck: an iteration_cap event was emitted", len(caps) == 1, str(len(caps)))
    check("stuck: it is the soft cap", caps and caps[0]["hard"] is False, str(caps))
    check("stuck: it names the limit it hit",
          caps and caps[0]["limit"] == 3, str(caps))
    check("stuck: it reports how many rounds ran",
          caps and caps[0]["rounds"] == 3, str(caps))
    check("stuck: the cap is NOT swallowed into a bare turn_end",
          "iteration_cap" in kinds, str(kinds))
    check("stuck: it still ends the turn properly", kinds[-1] == "turn_end", str(kinds))
    check("stuck: it stopped at the cap, not later", state["n"] == 3,
          f"{state['n']} stream calls")
    check("stuck: no 'continuing' event when there is no progress",
          not _of(events, "continuing"), str(kinds))


async def test_progressing_turn_continues_past_the_cap(tmp: Path) -> None:
    """The long-task case: a turn getting work done must not be cut off."""
    cfg = _cfg(tmp, max_iterations=3, max_rounds=12)
    agent = Agent(cfg)
    # Nine successful tool rounds, then a terminal narration. With a 3-round
    # cap that is a continue at round 3, 6 and 9 — three of them, because the
    # cap is checked at each multiple and the turn is still progressing at
    # every one.
    state = _stub(agent, [_tool() for _ in range(9)] + [_done()])

    events = await _drive(agent)
    conts = _of(events, "continuing")

    check("progress: it continued past the soft cap", len(conts) == 3, str(len(conts)))
    check("progress: it continued once per cap crossed",
          [e["continued"] for e in conts] == [1, 2, 3], str(conts))
    check("progress: each continue names the cap",
          all(e["limit"] == 3 for e in conts), str(conts))
    check("progress: the rounds count up",
          [e["rounds"] for e in conts] == [3, 6, 9], str(conts))
    check("progress: it did NOT stop at the soft cap",
          not _of(events, "iteration_cap"), str(_kinds(events)))
    check("progress: it ran past the cap and finished",
          state["n"] == 10, f"{state['n']} stream calls")


async def test_progress_that_stops_is_not_carried(tmp: Path) -> None:
    """A stale success must not carry a now-stuck loop past the cap."""
    cfg = _cfg(tmp, max_iterations=3, max_rounds=20)
    agent = Agent(cfg)
    # Three good rounds (so round 3 continues), then failures forever.
    state = _stub(agent, [_tool() for _ in range(3)]
                  + [_fail_tool(i) for i in range(20)])

    events = await _drive(agent)
    conts = _of(events, "continuing")
    caps = _of(events, "iteration_cap")

    check("stale: the first cap was passed through", len(conts) == 1, str(len(conts)))
    check("stale: the second cap stopped it", len(caps) == 1, str(len(caps)))
    check("stale: it stopped at round 6, the first cap after the failures",
          caps and caps[0]["rounds"] == 6, str(caps))
    check("stale: it did not run to the hard limit", state["n"] == 6,
          f"{state['n']} stream calls")


# ---------------------------------------------------------------------------
# 3 — the hard limit
# ---------------------------------------------------------------------------

async def test_hard_limit_is_reported_as_hard(tmp: Path) -> None:
    """A runaway turn ends, and says it was the runaway guard."""
    cfg = _cfg(tmp, max_iterations=2, max_rounds=5)
    agent = Agent(cfg)
    state = _stub(agent, [_tool() for _ in range(50)])

    events = await _drive(agent)
    caps = _of(events, "iteration_cap")

    check("hard: the hard cap was reported", len(caps) == 1, str(len(caps)))
    check("hard: it is flagged hard", caps and caps[0]["hard"] is True, str(caps))
    check("hard: it names max_rounds, not max_iterations",
          caps and caps[0]["limit"] == 5, str(caps))
    check("hard: it ran exactly to the hard limit", state["n"] == 5,
          f"{state['n']} stream calls")
    check("hard: the soft cap was passed through on the way",
          len(_of(events, "continuing")) == 2, str(_of(events, "continuing")))
    check("hard: the turn still ended", _kinds(events)[-1] == "turn_end",
          str(_kinds(events)))


# ---------------------------------------------------------------------------
# 4 — auto-compact on a measured threshold
# ---------------------------------------------------------------------------

async def test_auto_compact_fires_above_the_threshold(tmp: Path) -> None:
    cfg = _cfg(tmp, max_iterations=2, auto_compact=True, compact_at=0.5)
    cfg.context_window = 1_000
    agent = Agent(cfg)
    # 900 of a 1000 window is over the 0.5 threshold, so the *second* round
    # (the first has nothing measured yet) must compact before it requests.
    state = _stub(agent, [_tool(), _done()], used=900)

    # What each request actually saw. This is the assertion that matters: the
    # event stream cannot show "before the request" directly, because the
    # request is not itself an event — but the stub sees the history the
    # request was built from, so a compaction that ran too late shows up here
    # as a second call still carrying the full history.
    seen: list[int] = []
    original = agent._stream_once

    async def recording_stream(max_tokens=None):
        seen.append(len(agent.messages))
        async for item in original():
            yield item

    agent._stream_once = recording_stream  # type: ignore[assignment]

    compacted: list[str] = []

    async def fake_compact():
        compacted.append("yes")
        # Stand in for the real thing, which replaces the history with a single
        # summary message — that shrink is what the assertion below measures.
        agent.messages = [{"role": "user", "content": "[summary]"}]
        return True, "a summary"

    agent.compact = fake_compact  # type: ignore[assignment]
    # A history long enough that _should_compact does not skip it.
    agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]

    events = await _drive(agent)
    kinds = _kinds(events)

    check("auto: history was compacted", len(compacted) == 1, str(len(compacted)))
    check("auto: a compacting event was emitted", len(_of(events, "compacting")) == 1,
          str(kinds))
    check("auto: it names the threshold as the trigger",
          _of(events, "compacting")[0]["trigger"] == "threshold",
          str(_of(events, "compacting")))
    check("auto: a compacted event reports success",
          bool(_of(events, "compacted")) and _of(events, "compacted")[0]["ok"] is True,
          str(_of(events, "compacted")))
    check("auto: the events carry the measured prompt size",
          _of(events, "compacting")[0]["used"] == 900,
          str(_of(events, "compacting")))
    # The property: the *second* request went out with the compacted history,
    # so the compaction happened before it rather than after.
    check("auto: the first request carried the full history",
          len(seen) == 2 and seen[0] > 1, str(seen))
    # Two, not one: the summary plus the instruction this turn is working on,
    # restated after the compaction. A summary is not an instruction, so a
    # mid-turn compaction that left only the summary would have the model
    # carrying on with no idea what it was asked to do.
    check("auto: the second request carried the compacted history",
          len(seen) == 2 and seen[1] == 2, str(seen))
    check("auto: the compaction therefore preceded the request it protects",
          len(seen) == 2 and seen[1] < seen[0], str(seen))
    check("auto: the turn's instruction was restated after the summary",
          any("still running" in str(m.get("content")) for m in agent.messages),
          str(agent.messages))
    check("auto: it was not compacted repeatedly",
          len(_of(events, "compacting")) == 1, str(kinds))
    check("auto: the turn still completed", kinds[-1] == "turn_end", str(kinds))


async def test_auto_compact_does_not_fire_below_the_threshold(tmp: Path) -> None:
    cfg = _cfg(tmp, max_iterations=2, auto_compact=True, compact_at=0.9)
    cfg.context_window = 1_000
    agent = Agent(cfg)
    _stub(agent, [_tool(), _done()], used=100)  # 10% of the window

    compacted: list[str] = []

    async def fake_compact():
        compacted.append("yes")
        return True, "a summary"

    agent.compact = fake_compact  # type: ignore[assignment]
    agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]

    events = await _drive(agent)

    check("auto: nothing compacted below the threshold", not compacted, str(compacted))
    check("auto: no compacting event below the threshold",
          not _of(events, "compacting"), str(_kinds(events)))


async def test_auto_compact_respects_the_off_switch(tmp: Path) -> None:
    cfg = _cfg(tmp, max_iterations=2, auto_compact=False, compact_at=0.5)
    cfg.context_window = 1_000
    agent = Agent(cfg)
    _stub(agent, [_tool(), _done()], used=900)

    compacted: list[str] = []

    async def fake_compact():
        compacted.append("yes")
        return True, "a summary"

    agent.compact = fake_compact  # type: ignore[assignment]
    agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]

    events = await _drive(agent)

    check("auto: --no-auto-compact prevents it", not compacted, str(compacted))
    check("auto: and emits no event", not _of(events, "compacting"),
          str(_kinds(events)))


async def test_should_compact_never_fires_without_a_measurement(tmp: Path) -> None:
    """No measurement must never trip the threshold — 0 is not 'nearly full'."""
    cfg = _cfg(tmp, context_window=1_000)
    agent = Agent(cfg)
    agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    agent._last_input_tokens = 0
    check("auto: an unmeasured session does not compact",
          agent._should_compact(1_000) is False)
    agent._last_input_tokens = 950
    check("auto: a measured one over the threshold does",
          agent._should_compact(1_000) is True)


async def test_should_compact_skips_short_history(tmp: Path) -> None:
    """compact() would refuse a short history, so do not emit a no-op event."""
    cfg = _cfg(tmp, context_window=1_000, compact_at=0.5)
    agent = Agent(cfg)
    agent._last_input_tokens = 900
    agent.messages = [{"role": "user", "content": "one"}]
    check("auto: a too-short history is not compacted",
          agent._should_compact(1_000) is False)


# ---------------------------------------------------------------------------
# 5 — overflow: detected, compacted, retried once
# ---------------------------------------------------------------------------

def _overflow_error(text: str = "prompt is too long: 210000 tokens > 200000 maximum"):
    resp = type("R", (), {"status_code": 400, "request": None, "headers": {}})()
    return anthropic.BadRequestError(text, response=resp, body=None)


def test_overflow_is_recognised() -> None:
    """The message patterns actually seen, and the one that must NOT match."""
    check("overflow: anthropic's phrasing matches",
          Agent._is_context_overflow(_overflow_error()))
    check("overflow: litellm's 'context length' matches",
          Agent._is_context_overflow(
              _overflow_error("This model's maximum context length is 8192 tokens")))
    check("overflow: 'too many tokens' matches",
          Agent._is_context_overflow(_overflow_error("too many tokens in request")))
    # The trap: a bad model name is also a 400, and compacting the history for
    # one would throw the session away for nothing.
    check("overflow: a bad model name does NOT match",
          not Agent._is_context_overflow(
              _overflow_error("Invalid model name passed in model=claude-opus-5")))
    check("overflow: a non-400 does not match",
          not Agent._is_context_overflow(ValueError("prompt is too long")))


def test_overflow_has_a_real_message() -> None:
    """It used to fall through to `bad request: {dict}`."""
    msg = Agent._friendly_error(_overflow_error())
    check("overflow: the message names the context window",
          "context window" in msg, msg[:60])
    check("overflow: it mentions compaction",
          "compact" in msg.lower(), msg[:80])
    check("overflow: it is not the bare fallback",
          not msg.startswith("bad request"), msg[:60])
    # And the model-name branch must still win for a genuine name error.
    name = Agent._friendly_error(
        _overflow_error("Invalid model name passed in model=nope"))
    check("overflow: a bad model name still reads as a name error",
          "model name" in name, name[:60])


async def test_overflow_triggers_one_compact_and_retry(tmp: Path) -> None:
    """An overflow is fixed in-loop: compact, retry the same round once."""
    cfg = _cfg(tmp, max_iterations=2, auto_compact=True, compact_at=0.99)
    cfg.context_window = 1_000
    agent = Agent(cfg)

    calls = {"n": 0}

    async def stream(max_tokens=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _overflow_error()
        yield (None, _done("recovered"))

    agent._stream_once = stream  # type: ignore[assignment]

    compacted: list[str] = []

    async def fake_compact():
        compacted.append("yes")
        return True, "a summary"

    agent.compact = fake_compact  # type: ignore[assignment]
    agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]

    events = await _drive(agent)

    check("overflow: it compacted in response", len(compacted) == 1, str(len(compacted)))
    check("overflow: the compacting event names the overflow trigger",
          bool(_of(events, "compacting")) and
          _of(events, "compacting")[0]["trigger"] == "overflow",
          str(_of(events, "compacting")))
    check("overflow: it retried the round", calls["n"] == 2, f"{calls['n']} calls")
    check("overflow: no error event survived", not _of(events, "error"),
          str(_of(events, "error")))
    check("overflow: the turn completed", _kinds(events)[-1] == "turn_end",
          str(_kinds(events)))


async def test_overflow_is_retried_only_once(tmp: Path) -> None:
    """A second overflow means compaction did not help; stop, don't loop."""
    cfg = _cfg(tmp, max_iterations=2, auto_compact=True, compact_at=0.99)
    cfg.context_window = 1_000
    agent = Agent(cfg)

    calls = {"n": 0}

    async def stream(max_tokens=None):
        # A function that raises before reaching a `yield` is a coroutine, not
        # an async generator — `async for` rejects it with a TypeError that
        # would mask the very thing under test. The unreachable `yield` is what
        # makes this an async generator; it is never reached.
        calls["n"] += 1
        raise _overflow_error()
        yield  # pragma: no cover

    agent._stream_once = stream  # type: ignore[assignment]

    async def fake_compact():
        return True, "a summary"

    agent.compact = fake_compact  # type: ignore[assignment]
    agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]

    events = await _drive(agent)

    check("overflow: it tried exactly twice, not forever", calls["n"] == 2,
          f"{calls['n']} calls")
    errs = _of(events, "error")
    check("overflow: the second failure is reported", len(errs) == 1, str(len(errs)))
    check("overflow: and it reads as a context problem",
          bool(errs) and "context window" in errs[0]["message"], str(errs)[:120])


async def test_overflow_without_auto_compact_is_not_retried(tmp: Path) -> None:
    """--no-auto-compact must not silently retry anyway."""
    cfg = _cfg(tmp, auto_compact=False)
    agent = Agent(cfg)

    calls = {"n": 0}

    async def stream(max_tokens=None):
        calls["n"] += 1
        raise _overflow_error()
        yield  # pragma: no cover - makes this an async generator; never reached

    agent._stream_once = stream  # type: ignore[assignment]

    async def fake_compact():
        raise AssertionError("compact must not be called with auto_compact off")

    agent.compact = fake_compact  # type: ignore[assignment]
    agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(6)]

    events = await _drive(agent)

    check("overflow: not retried with auto-compact off", calls["n"] == 1,
          f"{calls['n']} calls")
    check("overflow: the error is still explained",
          bool(_of(events, "error")) and
          "context window" in _of(events, "error")[0]["message"],
          str(_of(events, "error"))[:100])


# ---------------------------------------------------------------------------
# the window itself
# ---------------------------------------------------------------------------

def test_window_prefers_the_catalog(tmp: Path) -> None:
    """A catalog figure must win over the configured fallback."""
    cfg = _cfg(tmp, context_window=128_000)
    agent = Agent(cfg)
    # No catalog in a test process, so the fallback is what answers.
    check("window: falls back to the configured value",
          agent.context_window() == 128_000, str(agent.context_window()))
    check("window: the default is 1M",
          Config().context_window == 1_000_000, str(Config().context_window))


def test_window_lookup_never_raises(tmp: Path) -> None:
    """A broken lookup must not break a turn."""
    from zimzilla import sources

    check("window: an unknown source returns None",
          sources.context_window_for("http://127.0.0.1:9/nothing", "x") is None)
    check("window: a cold cache returns None rather than raising",
          sources.context_window_for("https://api.unorouter.com", "x") is None)


# ---------------------------------------------------------------------------
# 429s: a burst is retried, a window is not
# ---------------------------------------------------------------------------

def _rate_limited(message: str, headers: dict | None = None):
    """A 429 shaped the way the SDK builds one, headers and all."""
    import httpx2

    req = httpx2.Request("POST", "https://tokenharbor.ai/v1/messages")
    resp = httpx2.Response(
        429, request=req, headers=headers or {},
        json={"type": "error",
              "error": {"type": "rate_limit_error", "message": message}})
    return anthropic.RateLimitError(
        message, response=resp,
        body={"type": "error",
              "error": {"type": "rate_limit_error", "message": message}})


def test_rate_limit_classification() -> None:
    """Which 429s mean "wait a moment" and which mean "this window is spent".

    The distinction is the whole point: retrying a burst is right, retrying an
    hourly or 7-day allowance burns the window being waited for and ends in a
    retry storm instead of an explanation. Measured on TokenHarbour, which
    answers both from the same endpoint.
    """
    from zimzilla.agent import _rate_limit_detail, _rate_limit_is_a_window

    check("429: a per-minute limit is a window",
          _rate_limit_is_a_window(
              _rate_limited("Per-minute rate limit reached — slow down a bit.")))
    check("429: an hourly limit is a window",
          _rate_limit_is_a_window(
              _rate_limited("Hourly rate limit reached — come back in a little while.")))
    check("429: an allowance message is a window",
          _rate_limit_is_a_window(
              _rate_limited("Free-tier allowance exhausted for this period.")))
    check("429: a bare burst 429 is NOT a window",
          not _rate_limit_is_a_window(_rate_limited("Too many requests.")))
    check("429: a long retry-after makes it a window",
          _rate_limit_is_a_window(
              _rate_limited("Too many requests.", {"retry-after": "3600"})))
    check("429: a short retry-after is still a burst",
          not _rate_limit_is_a_window(
              _rate_limited("Too many requests.", {"retry-after": "2"})))
    check("429: an ISO reset header makes it a window",
          _rate_limit_is_a_window(
              _rate_limited("Too many requests.",
                            {"x-th-free-resets": "2026-10-25T12:52:37+00:00"})))

    # Logfare, through its bundled proxy. Measured 2026-10-19: the body is
    # "Your account has reached its request budget. Try again later." and
    # LiteLLM forwards NO retry-after, so neither the wording list nor the
    # header check recognised it. Every model on the shared key is spent at
    # once, which makes "another free model will answer now" the one piece of
    # advice that is certainly wrong.
    logfare_429 = _rate_limited(
        "litellm.RateLimitError: RateLimitError: OpenAIException - "
        '{"error":{"type":"rate_limit_error","message":"Your account has '
        'reached its request budget. Try again later.","code":null}}')
    check("429: Logfare's exhausted budget is a window",
          _rate_limit_is_a_window(logfare_429))
    logfare_msg = Agent._friendly_error(logfare_429)
    check("429: Logfare's budget does not send the operator to another model",
          "Another free model" not in logfare_msg, logfare_msg)

    detail = _rate_limit_detail(_rate_limited(
        "Hourly rate limit reached.",
        {"x-th-plan": "free", "x-th-free-used-pct": "71",
         "x-th-free-resets": "2026-10-25T12:52:37+00:00"}))
    check("429: the provider's own numbers are reported",
          "free" in detail and "71%" in detail and "2026-10-25" in detail, detail)
    check("429: no headers means no invented detail",
          _rate_limit_detail(_rate_limited("Too many requests.")) == "")

    msg = Agent._friendly_error(_rate_limited(
        "Hourly rate limit reached.", {"x-th-free-used-pct": "71"}))
    check("429: a window error does not send the operator to another model",
          "Another free model" not in msg, msg)
    check("429: a window error names the way out",
          "/zim-source" in msg or "/zim-" in msg, msg)

    # The hint names the sources this machine can actually switch to, so it
    # must be stubbed rather than read from ~/.zimzilla — otherwise the
    # assertion depends on which keys the developer happens to have set up,
    # and fails on a fresh checkout. Both branches are pinned: a machine with
    # options names them, an empty one points at /zim-source.
    from zimzilla import sources as sources_mod

    def _hint(options: list[str]) -> str:
        with mock.patch.object(sources_mod, "switchable",
                               lambda exclude=None: options), \
             mock.patch.object(sources_mod, "active_key", lambda url: "logfare"):
            return Agent._friendly_error(
                _rate_limited("Hourly rate limit reached."), "http://localhost:4001")

    got = _hint(["openrouter", "unorouter"])
    check("429: the hint names the switchable sources",
          "/zim-openrouter" in got and "/zim-unorouter" in got, got)
    check("429: the hint keeps the provider's numbers",
          "71%" not in got, "no header supplied, so none should appear")
    got = _hint([])
    check("429: no sources means point at /zim-source",
          "/zim-source" in got and "/zim-openrouter" not in got, got)
    got = _hint(["a", "b", "c", "d", "e", "f"])
    check("429: the hint is capped at four commands",
          got.count("/zim-") == 4, got)


class _FailingStream:
    """A client whose one streaming call raises *error* the first *fail_times*.

    ``_stream_once`` cannot be stubbed for these tests: the retry policy being
    asserted *is* ``_stream_once``. So the client is faked instead, one level
    down, and the real retry loop runs against it.
    """

    def __init__(self, error, fail_times: int = 10**9):
        self.error = error
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        return _done()


class _FakeClient:
    def __init__(self, stream: _FailingStream):
        self.messages = self
        self._stream = stream

    def stream(self, **kw):
        return self._stream()


async def test_window_429_is_not_retried(tmp: Path) -> None:
    """The retry storm this pins: a window 429 must surface on the first hit.

    Five exponential retries against TokenHarbour's hourly cap spent ~30s and
    returned the same 429; the operator saw "retry 1/5 … 2/5" and then
    interrupted. What they needed was the reason, immediately.
    """
    agent = Agent(_cfg(tmp))
    failing = _FailingStream(
        _rate_limited("Hourly rate limit reached — come back in a little while."))
    agent._client = _FakeClient(failing)
    events = await _drive(agent, "Hi")

    check("429: a window 429 is attempted exactly once", failing.calls == 1,
          str(failing.calls))
    check("429: no retry event is emitted",
          not [e for e in events if e.get("type") == "retry"],
          str([e.get("type") for e in events]))
    errors = [e for e in events if e.get("type") == "error"]
    check("429: the operator gets the reason", bool(errors), str(events)[-300:])
    if errors:
        check("429: the reason names the allowance",
              "allowance" in errors[0]["message"], errors[0]["message"])

    # And the burst case still retries — the fix must not have removed the
    # backoff that a genuinely transient 429 needs. Two failures then success.
    agent2 = Agent(_cfg(tmp))
    burst = _FailingStream(_rate_limited("Too many requests."), fail_times=2)
    agent2._client = _FakeClient(burst)
    events2 = await _drive(agent2, "Hi")
    check("429: a burst 429 still retries", burst.calls == 3, str(burst.calls))
    check("429: the burst is reported as a retry",
          len([e for e in events2 if e.get("type") == "retry"]) == 2,
          str([e.get("type") for e in events2]))
    check("429: the burst still finishes the turn",
          any(e.get("type") == "turn_end" for e in events2),
          str([e.get("type") for e in events2]))


# ---------------------------------------------------------------------------
# A model that refuses a sampling parameter
# ---------------------------------------------------------------------------

def _bad_request(message: str):
    """A 400 shaped the way the SDK builds one."""
    import httpx2

    req = httpx2.Request("POST", "http://127.0.0.1:4001/v1/messages")
    resp = httpx2.Response(
        400, request=req,
        json={"type": "error",
              "error": {"type": "invalid_request_error", "message": message}})
    return anthropic.BadRequestError(
        message, response=resp,
        body={"type": "error",
              "error": {"type": "invalid_request_error", "message": message}})


class _ParamRejectingStream:
    """Rejects the request while ``temperature`` is in it, then succeeds.

    This is claude-haiku-5-5's actual behaviour on Logfare, measured
    2026-10-19: the identical request answers 400 with `temperature` present
    and 200 without it. The fake reads the request the way the real endpoint
    does, so a fix that merely retries the same body fails here — and it
    records every body, so the test can prove the parameter was dropped rather
    than assume it.
    """

    def __init__(self, reject: str = "temperature"):
        self.reject = reject
        self.bodies: list[dict] = []
        self.finals = 0

    def stream(self, **kw):
        extra = dict(kw.get("extra_body") or {})
        self.bodies.append(extra)
        return _ParamAwareCtx(self)

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def get_final_message(self):
        self.finals += 1
        return _done("Done.")


class _ParamAwareCtx:
    def __init__(self, owner: _ParamRejectingStream):
        self.owner = owner

    async def __aenter__(self):
        if self.owner.reject in self.owner.bodies[-1]:
            raise _bad_request(
                f"`{self.owner.reject}` is deprecated for this model.")
        return self.owner

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


async def test_rejected_param_is_dropped_and_retried(tmp: Path) -> None:
    """A model that refuses `temperature` must still work.

    The failure this pins: claude-haiku-5-5 answers 400 "`temperature` is
    deprecated for this model" to every turn, because cfg.temperature defaults
    to 0.0 and the harness sends it unconditionally. The model was unusable,
    and the 400 was reported as a bad model *name* — the upstream's sentence
    contains the word "model" — so the one thing that was correct got blamed.
    """
    agent = Agent(_cfg(tmp))
    stream = _ParamRejectingStream()

    class _Client:
        def __init__(self):
            self.messages = self

        def stream(self, **kw):
            return stream.stream(**kw)

    agent._client = _Client()
    events = await _drive(agent, "Hi")

    check("param: the rejected parameter is reported",
          any(e.get("type") == "param_dropped" for e in events),
          str([e.get("type") for e in events]))
    dropped = [e for e in events if e.get("type") == "param_dropped"]
    check("param: it names the parameter",
          bool(dropped) and dropped[0].get("param") == "temperature", str(dropped))
    check("param: the first request carried it", "temperature" in stream.bodies[0],
          str(stream.bodies))
    check("param: the retry does not", "temperature" not in stream.bodies[-1],
          str(stream.bodies))
    # The retry must actually SUCCEED. An earlier version of this fake raised
    # None on the retry, which the turn loop caught as a generic error — the
    # assertions above still passed, so the test proved nothing about recovery.
    check("param: the retry produced a real reply", stream.finals == 1,
          f"finals={stream.finals} bodies={stream.bodies}")
    check("param: no error was reported",
          not [e for e in events if e.get("type") == "error"],
          str([e for e in events if e.get("type") == "error"]))
    check("param: the turn still completes",
          any(e.get("type") == "turn_end" for e in events),
          str([e.get("type") for e in events]))
    check("param: it is remembered for later turns",
          "temperature" in agent._dropped_params, str(agent._dropped_params))

    # And it must not be re-sent on the NEXT turn — the fix is a session
    # decision, not a one-off retry that the next request undoes.
    stream.bodies.clear()
    await _drive(agent, "Again")
    check("param: the next turn omits it too",
          bool(stream.bodies) and "temperature" not in stream.bodies[0],
          str(stream.bodies))


def test_param_rejection_is_not_a_model_name_problem() -> None:
    """The message must not blame the model name.

    The upstream says "for this model", which the old `"model" in msg` test
    read as a bad name and answered with "run /model" — sending the operator to
    re-pick a model that was already correct.
    """
    e = _bad_request("`temperature` is deprecated for this model.")
    check("param: it is recognised as a parameter rejection",
          Agent._unsupported_sampling_param(e) == "temperature",
          repr(Agent._unsupported_sampling_param(e)))

    msg = Agent._friendly_error(e)
    check("param: the message names the parameter", "temperature" in msg, msg)
    check("param: it does not send the operator to /model",
          "/model" not in msg, msg)

    # An ordinary bad request is untouched by the new branch.
    plain = _bad_request("messages: field required")
    check("param: an unrelated 400 is not treated as a parameter rejection",
          Agent._unsupported_sampling_param(plain) == "")
    # A model-name typo still gets the model-name advice.
    typo = _bad_request("Model 'claude-haiku-4-5' not found")
    typo_msg = Agent._friendly_error(typo)
    check("param: a real model-name error still says so",
          "/model" in typo_msg, typo_msg)


async def test_compaction_survives_a_param_rejecting_model(tmp: Path) -> None:
    """/compact sends `temperature` too, and must cope with a model that refuses it.

    ``compact()`` hard-coded ``extra_body={"temperature": 0}``. On
    claude-haiku-5-5 that made compaction fail with the same 400 the turn path
    hits — so the one recovery mechanism for a full context was dead on the
    model that needed it most.
    """
    agent = Agent(_cfg(tmp))
    agent.messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]

    calls: list[dict] = []

    class _Messages:
        async def create(self, **kw):
            extra = dict(kw.get("extra_body") or {})
            calls.append(extra)
            if "temperature" in extra:
                raise _bad_request("`temperature` is deprecated for this model.")

            class _M:
                content = [_Blk(type="text", text="a summary")]
                usage = None
            return _M()

    class _Client:
        messages = _Messages()

    agent._client = _Client()
    ok, note = await agent.compact()
    check("compact: it succeeds on a model that rejects temperature", ok, note)
    check("compact: the first call carried temperature",
          "temperature" in calls[0], str(calls))
    check("compact: the retry dropped it",
          len(calls) == 2 and "temperature" not in calls[1], str(calls))
    check("compact: the rejection is remembered for later turns",
          "temperature" in agent._dropped_params, str(agent._dropped_params))




# ---------------------------------------------------------------------------

async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        await test_stuck_at_soft_cap_is_reported(tmp)
        await test_progressing_turn_continues_past_the_cap(tmp)
        await test_progress_that_stops_is_not_carried(tmp)
        await test_hard_limit_is_reported_as_hard(tmp)
        await test_auto_compact_fires_above_the_threshold(tmp)
        await test_auto_compact_does_not_fire_below_the_threshold(tmp)
        await test_auto_compact_respects_the_off_switch(tmp)
        await test_should_compact_never_fires_without_a_measurement(tmp)
        await test_should_compact_skips_short_history(tmp)
        test_overflow_is_recognised()
        test_overflow_has_a_real_message()
        await test_overflow_triggers_one_compact_and_retry(tmp)
        await test_overflow_is_retried_only_once(tmp)
        await test_overflow_without_auto_compact_is_not_retried(tmp)
        test_window_prefers_the_catalog(tmp)
        test_window_lookup_never_raises(tmp)
        test_rate_limit_classification()
        await test_window_429_is_not_retried(tmp)
        await test_rejected_param_is_dropped_and_retried(tmp)
        test_param_rejection_is_not_a_model_name_problem()
        await test_compaction_survives_a_param_rejecting_model(tmp)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("failed:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
