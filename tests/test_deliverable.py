"""Regression suite for the deliverable check in ``Agent.run_turn``.

Run:  python tests/test_deliverable.py   (from an activated venv)

This is the fix for a real failure. ``/osint instagram zimthegoat`` ran every
phase, wrote 6 MB of real evidence into the case directory, printed
``All phases complete. Writing the report.`` as plain text — and stopped. No
``report.md`` was ever written, so ``finalise_case`` correctly refused to delete
anything and the operator got ``no report found``.

The loop broke on ``if not tool_calls``, which is right for a chat turn and
wrong for a turn whose whole purpose is to leave a file behind. Nothing
distinguished "the model finished" from "the model stopped mid-task", and
``stop_reason`` was read nowhere — so a ``Write`` cut off at ``max_tokens`` and a
``Write`` never attempted looked identical.

Four behaviours are pinned here:

1. A turn that declares a deliverable and stops without writing it is **nudged**,
   up to its budget, and no further.
2. When the budget is spent the turn says plainly that the file is missing —
   ``written: False`` — rather than ending as though it had succeeded.
3. A model that writes the file after a nudge ends with ``written: True``.
4. A finish at the token ceiling is reported as **truncated** and is **not**
   nudged — re-prompting a model that was cut off mid-``Write`` just spends
   another ceiling on the same truncation.
5. A finish at the token ceiling that carried **nothing at all** is a different
   shape and is handled differently: it is reported as ``reasoning_exhausted``
   and retried once with a doubled ceiling. This is the reasoning-model case —
   the budget went to thinking the proxy never forwards, so there is no dropped
   work to protect and the ceiling is known to be too small. See the note on the
   branch in ``agent.run_turn``.

No network: ``_stream_once`` is replaced with a stub that yields canned
messages, exactly as ``test_phase4`` does.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla.agent import Agent, Deliverable  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


class _Blk:
    """A content block: text, or a tool_use."""

    def __init__(self, **k):
        self.__dict__.update(k)

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


class _Msg:
    """A final message, with the ``stop_reason`` the fix depends on."""

    def __init__(self, content, stop_reason=None, usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


def _text(msg: str, stop_reason: str | None = None) -> _Msg:
    """A reply that is pure narration — no tool call. The failing shape."""
    return _Msg([_Blk(type="text", text=msg)], stop_reason=stop_reason)


def _empty(stop_reason: str = "max_tokens") -> _Msg:
    """A truncated reply that carries nothing: the reasoning-exhaustion shape.

    What a reasoning model looks like through a proxy that does not forward its
    thinking — one ``text`` block holding an empty string, and no tool call. The
    budget went somewhere the harness never sees. Measured, not imagined: see the
    note on this branch in ``agent.run_turn``.
    """
    return _Msg([_Blk(type="text", text="")], stop_reason=stop_reason)


def _write(path: Path, body: str = "the report") -> _Msg:
    """A reply that calls the real write tool.

    The tool is ``write_file``, not ``write``: naming a tool that does not exist
    is not a silent no-op here, it produces an unknown-tool error the model
    retries forever, which is exactly what the 40-call runaway this test first
    showed was.
    """
    return _Msg([
        _Blk(type="text", text="Writing the report."),
        _Blk(type="tool_use", id="t-write", name="write_file",
             input={"path": str(path), "content": body}),
    ], stop_reason="tool_use")


def _done(msg: str = "Done — the report is written.") -> _Msg:
    """A terminal narration with no tool call: the turn ends here.

    A real model stops after a successful write; a stub that keeps serving the
    write call does not. Every turn expected to *finish* must end its list with
    this, or the loop correctly keeps going and the call count is meaningless.
    """
    return _Msg([_Blk(type="text", text=msg)], stop_reason="end_turn")


def _stub(agent: Agent, messages: list[_Msg]) -> dict:
    """Serve *messages* in order, recording how many were consumed.

    Past the end it keeps serving the last message. That is deliberate: a loop
    that fails to break then shows up as a runaway call count rather than a
    StopIteration swallowed into an "error" event. The consequence is that a
    test whose turn is meant to *finish* must end its list with a turn that
    carries no tool call — a real model stops after writing, and a stub that
    keeps offering another ``write_file`` does not. See ``_done``.
    """
    state = {"n": 0, "ceilings": []}

    async def fake_stream(max_tokens=None):
        # The ceiling is recorded, not ignored: the reasoning-exhaustion retry is
        # only correct if the second call actually asks for more room than the
        # first, and a stub that swallows the argument would hide a retry that
        # re-sent the same ceiling.
        state["ceilings"].append(max_tokens)
        i = state["n"]
        state["n"] += 1
        if i < len(messages):
            yield (None, messages[i])
        else:
            yield (None, messages[-1])

    agent._stream_once = fake_stream
    return state


async def _drive(agent: Agent, text: str, deliverable: Deliverable | None):
    """Run one turn and collect every event."""
    events = []
    async for ev in agent.run_turn(text, deliverable=deliverable):
        events.append(ev)
    return events


def _kinds(events) -> list[str]:
    return [e.get("type", "") for e in events]


# ---------------------------------------------------------------------------

async def test_narrate_then_narrate_gives_up_honestly(tmp: Path) -> None:
    """The exact failure: narration, no write. Nudged twice, then honest."""
    report = tmp / "report.md"
    agent = Agent(_cfg(tmp))
    deliverable = Deliverable(path=report, label="report.md", max_nudges=2)
    state = _stub(agent, [_text("All phases complete. Writing the report.")])

    events = await _drive(agent, "do the recon", deliverable)
    kinds = _kinds(events)

    nudges = [e for e in events if e["type"] == "nudged"]
    check("narrate: the model was nudged", len(nudges) == 2, str(len(nudges)))
    check("narrate: the nudge names the label",
          all(e["label"] == "report.md" for e in nudges))
    check("narrate: the nudge counts up",
          [e["attempt"] for e in nudges] == [1, 2], str([e["attempt"] for e in nudges]))
    check("narrate: it stopped after the budget, not before",
          state["n"] == 3, f"{state['n']} stream calls")

    final = [e for e in events if e["type"] == "deliverable"]
    check("narrate: a deliverable event was emitted", len(final) == 1, str(len(final)))
    check("narrate: the deliverable is reported NOT written",
          final and final[0]["written"] is False, str(final))
    check("narrate: the deliverable names the path",
          final and final[0]["path"] == str(report), str(final))
    check("narrate: the nudge count is reported",
          final and final[0]["nudges"] == 2, str(final))
    check("narrate: the file really was not written", not report.exists())
    check("narrate: the turn still ended cleanly",
          kinds.count("deliverable") == 1 and "error" not in kinds, str(kinds))


async def test_a_nudged_model_can_recover(tmp: Path) -> None:
    """Narration, then a nudge, then the file — written: True, one nudge."""
    report = tmp / "report.md"
    agent = Agent(_cfg(tmp))
    deliverable = Deliverable(path=report, label="report.md", max_nudges=2)
    state = _stub(agent, [
        _text("All phases complete. Writing the report."),
        _write(report),
        _done(),
    ])

    events = await _drive(agent, "do the recon", deliverable)
    nudges = [e for e in events if e["type"] == "nudged"]
    final = [e for e in events if e["type"] == "deliverable"]

    check("recover: exactly one nudge was needed", len(nudges) == 1, str(len(nudges)))
    check("recover: the file exists on disk", report.exists())
    check("recover: it holds what the tool call said",
          report.exists() and "the report" in report.read_text())
    check("recover: the deliverable is reported written",
          final and final[0]["written"] is True, str(final))
    check("recover: the nudge count is reported",
          final and final[0]["nudges"] == 1, str(final))
    check("recover: no wasted extra call", state["n"] == 3, str(state["n"]))


async def test_a_first_try_write_is_not_nudged(tmp: Path) -> None:
    """The happy path must be untouched by any of this."""
    report = tmp / "report.md"
    agent = Agent(_cfg(tmp))
    deliverable = Deliverable(path=report, label="report.md", max_nudges=2)
    state = _stub(agent, [_write(report), _done()])

    events = await _drive(agent, "do the recon", deliverable)
    final = [e for e in events if e["type"] == "deliverable"]

    check("happy: no nudge on a first-try write",
          "nudged" not in _kinds(events), str(_kinds(events)))
    check("happy: write then stop — two calls, no retry loop",
          state["n"] == 2, str(state["n"]))
    check("happy: the file exists", report.exists())
    check("happy: the deliverable is reported written",
          final and final[0]["written"] is True, str(final))
    check("happy: the nudge count is zero", final and final[0]["nudges"] == 0, str(final))


async def test_a_truncated_finish_is_not_nudged(tmp: Path) -> None:
    """A ceiling hit is reported as truncation, never re-prompted.

    This is the branch that makes the two failures distinguishable. The model
    was cut off mid-``Write``, so the tool call never reached ``final.content``
    and the loop sees no tool call — exactly like a model that stopped. Without
    the ``stop_reason`` check the two are the same thing, and nudging just burns
    another ceiling producing the same truncation.
    """
    report = tmp / "report.md"
    agent = Agent(_cfg(tmp))
    deliverable = Deliverable(path=report, label="report.md", max_nudges=2)
    state = _stub(agent, [_text("The report is lengthy and I was cut",
                                stop_reason="max_tokens")])

    events = await _drive(agent, "do the recon", deliverable)
    kinds = _kinds(events)
    trunc = [e for e in events if e["type"] == "truncated"]
    final = [e for e in events if e["type"] == "deliverable"]

    check("truncated: a truncated event was emitted", len(trunc) == 1, str(len(trunc)))
    check("truncated: it names the ceiling",
          trunc and trunc[0]["max_tokens"] == agent.cfg.max_tokens, str(trunc))
    check("truncated: it was NOT nudged",
          "nudged" not in kinds, str(kinds))
    check("truncated: it did not spend a second call",
          state["n"] == 1, str(state["n"]))
    check("truncated: the deliverable is still reported missing",
          final and final[0]["written"] is False, str(final))
    check("truncated: zero nudges are reported",
          final and final[0]["nudges"] == 0, str(final))
    check("truncated: a mid-Write cut is NOT the empty shape",
          "reasoning_exhausted" not in kinds, str(kinds))


async def test_an_empty_truncation_retries_with_more_room(tmp: Path) -> None:
    """A truncated reply carrying nothing is retried, not reported and dropped.

    The failure this pins: ``so where are we ?`` came back as
    "reply truncated at 32768 output tokens" with no reply above it. The model
    was a reasoning model, its thinking is billed against ``max_tokens``, and the
    proxy forwards none of it — so the harness was handed an empty turn. Nothing
    the model asked for was dropped, so re-sending costs only the ceiling that
    was already known to be too small.
    """
    agent = Agent(_cfg(tmp))
    state = _stub(agent, [
        _empty(),
        _done("We are on wave 7 of the x.test campaign."),
    ])

    events = await _drive(agent, "so where are we ?", None)
    kinds = _kinds(events)
    exh = [e for e in events if e["type"] == "reasoning_exhausted"]

    check("empty: the empty truncation was named", len(exh) == 1, str(exh))
    check("empty: it is not called a mid-Write truncation",
          "truncated" not in kinds, str(kinds))
    check("empty: it names the ceiling that was spent",
          exh and exh[0]["max_tokens"] == agent.cfg.max_tokens, str(exh))
    check("empty: it says what it is retrying with",
          exh and exh[0]["retry_tokens"] == agent.cfg.max_tokens * 2, str(exh))
    check("empty: it spent exactly two calls", state["n"] == 2, str(state["n"]))
    check("empty: the retry actually asked for more room",
          len(state["ceilings"]) == 2
          and state["ceilings"][1] > state["ceilings"][0], str(state["ceilings"]))
    check("empty: the blank turn was dropped from the history",
          len(agent.messages) == 2, str(agent.messages))
    check("empty: the turn ended without error", "error" not in kinds, str(kinds))


async def test_a_second_empty_truncation_stops_honestly(tmp: Path) -> None:
    """The retry is bounded: double once, then say so — never a loop.

    A model that spends a doubled ceiling on reasoning and still emits nothing is
    not going to answer on a third attempt, so the second one is reported rather
    than retried. The history must be left clean too: the turn ends on the user's
    message, with no blank assistant reply carried into the next request.
    """
    agent = Agent(_cfg(tmp))
    state = _stub(agent, [_empty()])

    events = await _drive(agent, "so where are we ?", None)
    kinds = _kinds(events)
    exh = [e for e in events if e["type"] == "reasoning_exhausted"]

    check("empty x2: both attempts were reported", len(exh) == 2, str(len(exh)))
    check("empty x2: the first offered the retry",
          exh and exh[0]["retry_tokens"] == agent.cfg.max_tokens * 2, str(exh))
    check("empty x2: the second offered no further retry",
          len(exh) == 2 and exh[1]["retry_tokens"] is None, str(exh))
    check("empty x2: it stopped after two calls", state["n"] == 2, str(state["n"]))
    check("empty x2: the history is left clean",
          len(agent.messages) == 1, str(agent.messages))
    check("empty x2: the turn ended without error", "error" not in kinds, str(kinds))


async def test_a_truncation_after_a_nudge_still_stops(tmp: Path) -> None:
    """Nudge once, then truncate: it must stop, not nudge again."""
    report = tmp / "report.md"
    agent = Agent(_cfg(tmp))
    deliverable = Deliverable(path=report, label="report.md", max_nudges=2)
    state = _stub(agent, [
        _text("All phases complete. Writing the report."),
        _text("Now writing the full report, which is long",
              stop_reason="max_tokens"),
    ])

    events = await _drive(agent, "do the recon", deliverable)
    kinds = _kinds(events)
    check("nudge+truncate: one nudge was spent", kinds.count("nudged") == 1, str(kinds))
    check("nudge+truncate: the truncation was reported",
          kinds.count("truncated") == 1, str(kinds))
    check("nudge+truncate: the truncation ended it", state["n"] == 2, str(state["n"]))
    check("nudge+truncate: the file is reported missing",
          [e for e in events if e["type"] == "deliverable"][0]["written"] is False)


async def test_no_deliverable_means_no_change(tmp: Path) -> None:
    """A plain chat turn must behave exactly as it did before the fix.

    The whole risk of this change is that it perturbs ordinary turns. A turn
    with no deliverable that narrates and stops must end after one call, with no
    nudge, no truncation and no deliverable event.
    """
    agent = Agent(_cfg(tmp))
    state = _stub(agent, [_text("Sure — here is the answer, no files needed.")])

    events = await _drive(agent, "just tell me something", None)
    kinds = _kinds(events)

    check("plain: one stream call", state["n"] == 1, str(state["n"]))
    check("plain: no nudge", "nudged" not in kinds, str(kinds))
    check("plain: no deliverable event", "deliverable" not in kinds, str(kinds))
    check("plain: no truncation event", "truncated" not in kinds, str(kinds))
    check("plain: it ended without error", "error" not in kinds, str(kinds))


async def test_a_zero_nudge_budget_never_nudges(tmp: Path) -> None:
    """max_nudges=0 must mean zero — the boundary the caller could set."""
    report = tmp / "report.md"
    agent = Agent(_cfg(tmp))
    deliverable = Deliverable(path=report, label="report.md", max_nudges=0)
    state = _stub(agent, [_text("All phases complete. Writing the report.")])

    events = await _drive(agent, "do the recon", deliverable)
    final = [e for e in events if e["type"] == "deliverable"]

    check("budget 0: no nudge at all", "nudged" not in _kinds(events))
    check("budget 0: one call only", state["n"] == 1, str(state["n"]))
    check("budget 0: the miss is still reported",
          final and final[0]["written"] is False and final[0]["nudges"] == 0,
          str(final))


async def test_the_nudge_message_is_a_real_user_turn(tmp: Path) -> None:
    """The nudge is appended as a user message, not smuggled into a system line.

    The history has to stay a clean user/assistant alternation, or the next API
    call is rejected. The nudge must therefore be a genuine user message — the
    same constraint ``notice`` documents for why it folds rather than appends.
    """
    report = tmp / "report.md"
    agent = Agent(_cfg(tmp))
    deliverable = Deliverable(path=report, label="report.md", max_nudges=1)
    _stub(agent, [_text("All phases complete. Writing the report.")])

    await _drive(agent, "do the recon", deliverable)

    roles = [m["role"] for m in agent.messages]
    check("history: it alternates user/assistant",
          all(a != b for a, b in zip(roles, roles[1:])), str(roles))
    check("history: it starts with the user's request",
          roles[0] == "user" and agent.messages[0]["content"] == "do the recon",
          str(roles))

    # The nudge is a real user message. It is NOT the last entry: the model's
    # final narration is appended before the loop breaks on a spent budget, so
    # the nudge sits second-to-last. Asserting its content rather than its
    # position is the point — the invariant is the alternation, not the tail.
    nudges = [m for m in agent.messages
              if m["role"] == "user" and isinstance(m["content"], str)
              and "You stopped before writing" in m["content"]]
    check("history: the nudge is a real user message", len(nudges) == 1,
          str(len(nudges)))
    check("history: the nudge names the label and the path",
          nudges and "report.md" in nudges[0]["content"]
          and str(report) in nudges[0]["content"],
          nudges[0]["content"] if nudges else "")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-deliverable-test-"))

    async def run_all() -> None:
        # Each test gets its own directory: the deliverable check keys on the
        # file existing, so a shared one would leak between tests.
        for i, fn in enumerate((
            test_narrate_then_narrate_gives_up_honestly,
            test_a_nudged_model_can_recover,
            test_a_first_try_write_is_not_nudged,
            test_a_truncated_finish_is_not_nudged,
            test_an_empty_truncation_retries_with_more_room,
            test_a_second_empty_truncation_stops_honestly,
            test_a_truncation_after_a_nudge_still_stops,
            test_no_deliverable_means_no_change,
            test_a_zero_nudge_budget_never_nudges,
            test_the_nudge_message_is_a_real_user_turn,
        )):
            case = tmp / f"case{i}"
            case.mkdir(parents=True, exist_ok=True)
            await fn(case)

    asyncio.run(run_all())

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
