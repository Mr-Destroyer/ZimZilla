"""App-level coverage for the deliverable check, driven through the live TUI.

Run:  python tests/test_deliverable_ui.py   (from an activated venv)

``test_deliverable.py`` pins the logic inside ``Agent.run_turn``. This file pins
the half that logic cannot see: that ``/osint`` actually *declares* a deliverable,
and that ``_handle_event`` renders the two new events as the operator's own
sentence rather than leaving them to be inferred from the transcript.

That distinction matters here. The original failure was not a missing branch in
the agent — it was a run that printed ``All phases complete. Writing the
report.`` and stopped, with nothing anywhere saying the file was absent. A
correct agent that the app forgets to pass a ``Deliverable`` to would reproduce
the failure exactly, and ``test_deliverable.py`` would stay green throughout,
because it calls ``run_turn`` itself. So these tests drive ``/osint`` through
``_handle_command`` and read the rendered transcript back.

No network: ``_stream_once`` is replaced with a stub that yields canned messages,
exactly as ``test_osint`` and ``test_phase4`` do.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402

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


class _Msg:
    def __init__(self, content, stop_reason=None, usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


def _text(msg: str, stop_reason: str | None = None) -> _Msg:
    return _Msg([_Blk(type="text", text=msg)], stop_reason=stop_reason)


def _empty() -> _Msg:
    """A truncated reply carrying nothing: the reasoning-exhaustion shape.

    One ``text`` block holding an empty string and no tool call — what a
    reasoning model looks like when its thinking spends the whole ceiling and the
    proxy forwards none of it. See the note on this branch in ``agent.run_turn``.
    """
    return _Msg([_Blk(type="text", text="")], stop_reason="max_tokens")


def _write(path: Path, body: str = "# the report\n") -> _Msg:
    """A reply that calls the real write tool — ``write_file``, not ``write``."""
    return _Msg([
        _Blk(type="text", text="Writing the report."),
        _Blk(type="tool_use", id="t-write", name="write_file",
             input={"path": str(path), "content": body}),
    ], stop_reason="tool_use")


def _done() -> _Msg:
    return _Msg([_Blk(type="text", text="Done.")], stop_reason="end_turn")


def _scripted(app, plan) -> dict:
    """Serve one message per stream call, from the callable *plan*.

    **One final message per call.** ``_stream_once`` yields ``(event, final_msg)``
    pairs and the loop keeps only the *last* ``final_msg`` a call produces, so
    yielding two in one call silently discards the first. That is not
    hypothetical: an earlier draft of this file yielded the write and a terminal
    narration from the same call, and the write was dropped on the floor — the
    loop then saw pure narration, nudged twice, and the test read as a broken
    deliverable check when the stub was what was broken.

    *plan* is a callable ``(call_index) -> _Msg`` rather than a list, so a
    message can be built from state that only exists once the turn is running —
    the timestamped case directory, above all. Past the plan's end it is called
    with a growing index, so a plan that never terminates shows up as a runaway
    call count rather than a swallowed StopIteration.
    """
    state = {"n": 0}

    async def fake_stream(max_tokens=None):
        i = state["n"]
        state["n"] += 1
        yield (None, plan(i))

    app.agent._stream_once = fake_stream
    return state


def _transcript(app) -> str:
    """The rendered transcript text.

    The pane is a ``RainRichLog`` with id ``#transcript``; its ``.lines`` carry
    the rendered strips, so joining ``.text`` is what the operator actually sees.
    """
    return "\n".join(s.text for s in app.query_one("#transcript").lines)


async def _settle(pilot, app, rounds: int = 60) -> None:
    """Pause until the turn's worker has finished and the harness is idle."""
    for _ in range(rounds):
        await pilot.pause()
        if not app.busy:
            return
    await asyncio.sleep(0.25)
    await pilot.pause()


async def test_a_missing_report_is_said_out_loud(wd: Path) -> None:
    """The original failure, re-run through the app.

    The model narrates the exact sentence that ended the real run and stops. The
    deliverable must be declared by ``/osint``, nudged to exhaustion, and then
    reported in the transcript — not left as a transcript that merely stops.
    """
    cfg = _cfg(wd, boot_rain=False)
    cfg.state_dir = wd / "ui-state"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        state = _scripted(
            app, lambda i: _text("All phases complete. Writing the report."))

        app._handle_command("/osint email target@example.com")
        await _settle(pilot, app)

        made = list((cfg.state_dir / "osint").glob("email-target-example.com-*"))
        check("ui: the case directory was created", len(made) == 1,
              f"{[p.name for p in made]}")
        check("ui: the report really was not written",
              bool(made) and not (made[0] / "report.md").exists())

        # Three stream calls: the narration, then one per nudge. Anything fewer
        # means /osint did not declare a deliverable at all.
        check("ui: the turn was nudged to its budget", state["n"] == 3,
              f"{state['n']} stream calls")

        blob = _transcript(app)
        check("ui: the nudge was announced in the transcript",
              "report.md was not written" in blob, blob[-900:])
        check("ui: the missing deliverable was stated plainly",
              "report.md was never written" in blob, blob[-900:])
        check("ui: the message names where it was expected",
              "report.md" in blob and "email-target-example.com" in blob,
              blob[-900:])
        # The failure must not read as a success. The run's own tidy-up keeps
        # the evidence, and nothing archived a report that does not exist.
        check("ui: a run with no report keeps its case dir",
              bool(made) and made[0].is_dir())
        check("ui: nothing was archived for the failed run",
              not list((cfg.state_dir / "reports").glob("email-target-*")))


async def test_a_written_report_is_confirmed_and_finalised(wd: Path) -> None:
    """The happy path through the app: written, said so, case dir cleared."""
    cfg = _cfg(wd, boot_rain=False)
    cfg.state_dir = wd / "ui-state"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        # The plan reads the case dir that /osint creates — globbed at call time
        # rather than predicted, because the name carries a timestamp. It also
        # records the path, since by the time the turn settles the finalise hook
        # has removed it and a fresh glob would find nothing to assert about.
        seen: dict = {}

        def plan(i: int) -> _Msg:
            if i == 0:
                case = next(
                    (cfg.state_dir / "osint").glob("email-target-example.com-*"))
                seen["case"] = case
                return _write(case / "report.md")
            return _done()

        state = _scripted(app, plan)

        app._handle_command("/osint email target@example.com")
        await _settle(pilot, app)

        case = seen.get("case")
        blob = _transcript(app)
        check("ui: the write ran and the turn stopped — two calls",
              state["n"] == 2, f"{state['n']} stream calls")
        check("ui: the written deliverable was confirmed",
              "report.md written:" in blob, blob[-900:])
        check("ui: the confirmation was not a warning",
              "report.md was never written" not in blob, blob[-900:])
        # Written means finalised: the report archived, the bulky case dir gone.
        check("ui: the plan saw the case dir the run created",
              case is not None, str(case))
        check("ui: the case dir was cleared after a reported run",
              case is not None and not case.exists(), str(case))
        archived = list((cfg.state_dir / "reports").glob("email-target-example.com-*.md"))
        check("ui: the report was archived under reports/",
              len(archived) == 1, f"{[p.name for p in archived]}")


async def test_a_truncated_finish_is_named_as_truncation(wd: Path) -> None:
    """A ceiling hit reaches the transcript as truncation, not as silence.

    The whole reason ``stop_reason`` is now read: a ``Write`` cut off at
    ``max_tokens`` and a ``Write`` never attempted produced identical transcripts.
    Through the app, the operator must be able to tell them apart.
    """
    cfg = _cfg(wd, boot_rain=False)
    cfg.state_dir = wd / "ui-state"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        state = _scripted(
            app, lambda i: _text("The report is lengthy and I was cut",
                                 stop_reason="max_tokens"))

        app._handle_command("/osint email target@example.com")
        await _settle(pilot, app)

        blob = _transcript(app)
        check("ui: the truncation was named", "truncated at" in blob, blob[-900:])
        check("ui: the ceiling was given a number",
              f"truncated at {app.cfg.max_tokens}" in blob, blob[-900:])
        check("ui: the ceiling is well clear of a real report",
              app.cfg.max_tokens >= 32768, str(app.cfg.max_tokens))
        # Truncation must NOT be nudged — re-prompting spends another ceiling.
        check("ui: a truncation spent no extra call", state["n"] == 1,
              f"{state['n']} stream calls")
        check("ui: a truncation was not announced as a nudge",
              "report.md was not written" not in blob, blob[-900:])


async def test_an_empty_truncation_is_named_and_retried(wd: Path) -> None:
    """A ceiling hit that carried nothing is said out loud, then retried.

    The reported failure as the operator met it: ``so where are we ?`` rendered
    ``reply truncated at 32768 output tokens`` with no reply above it, because
    the model reasoned its way to the ceiling and the proxy forwarded none of it.
    Two things must be visible here — that the reply was *empty* rather than cut
    mid-work, and that a retry with more room is what happens next, rather than a
    silent second call that leaves the operator guessing why the bill grew.
    """
    cfg = _cfg(wd, boot_rain=False)
    cfg.state_dir = wd / "ui-state"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        state = _scripted(app, lambda i: _empty() if i == 0 else _done())

        app._submit_text("so where are we ?")
        await _settle(pilot, app)

        blob = _transcript(app)
        check("ui empty: the empty reply was named",
              "empty reply at" in blob, blob[-900:])
        check("ui empty: it names the ceiling that was spent",
              f"empty reply at {app.cfg.max_tokens:,}" in blob, blob[-900:])
        check("ui empty: it says what it is retrying with",
              f"{app.cfg.max_tokens * 2:,}" in blob, blob[-900:])
        check("ui empty: it is not reported as a mid-Write cut",
              "truncated at" not in blob, blob[-900:])
        check("ui empty: the retry really happened", state["n"] == 2,
              f"{state['n']} stream calls")


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for i, fn in enumerate((
            test_a_missing_report_is_said_out_loud,
            test_a_written_report_is_confirmed_and_finalised,
            test_a_truncated_finish_is_named_as_truncation,
            test_an_empty_truncation_is_named_and_retried,
        )):
            wd = tmp / f"ui{i}"
            wd.mkdir(parents=True, exist_ok=True)
            await fn(wd)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed — FAILED: "
              + ", ".join(failed))
        return 1
    print(f"{len(RESULTS)}/{len(RESULTS)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
