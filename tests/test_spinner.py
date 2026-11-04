"""The status bar's busy spinner: a wheel that turns, not text that flickers.

Run:  python tests/test_spinner.py   (from an activated venv)

The old bar swapped hacker verbs every 0.35s behind a static ``⟳``. Nothing
moved between swaps, so a turn waiting on the upstream looked frozen — and a
frozen bar is indistinguishable from a hung request. This suite pins the three
things that make it read as motion instead:

1. The wheel is a real frame sequence — every frame distinct, one cell wide,
   so it cannot jitter the text beside it.
2. It advances on its own tick while busy, and does no work while idle or
   while compaction owns the bar.
3. The words rotate slowly, and never over a concrete activity: a running
   tool's name is worth more than a mood word, and it is the only thing on
   screen while that tool runs.

No network: the app is driven through ``run_test`` with the bar poked
directly, the same way ``test_compact_ui`` drives the compaction panel.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla.config import Config  # noqa: E402
from zimzilla.theme import THINKING_VERBS  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402
from zimzilla.ui.renderers import COMPACT_SPINNER, SPINNER_FRAMES  # noqa: E402
from zimzilla.ui.widgets import StatusBar  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _frame_in(text: str) -> str:
    """The wheel glyph in *text*, or "" when the bar is not showing one."""
    for glyph in SPINNER_FRAMES:
        if glyph in text:
            return glyph
    return ""


# ---------------------------------------------------------------------------
# 1 — the wheel itself
# ---------------------------------------------------------------------------

def test_wheel_shape() -> None:
    check("wheel: enough frames to read as rotation", len(SPINNER_FRAMES) >= 8,
          str(len(SPINNER_FRAMES)))
    check("wheel: every frame is distinct",
          len(set(SPINNER_FRAMES)) == len(SPINNER_FRAMES))
    check("wheel: every frame is one cell wide",
          all(len(f) == 1 for f in SPINNER_FRAMES), repr(SPINNER_FRAMES))
    check("wheel: compaction and the bar share one animation",
          COMPACT_SPINNER == SPINNER_FRAMES)

    check("cadence: the wheel turns faster than the eye can count",
          0 < ZimZillaApp.SPIN_TICK <= 0.12, str(ZimZillaApp.SPIN_TICK))
    check("cadence: the words change slowly enough to read",
          ZimZillaApp.VERB_SECONDS >= 1.5, str(ZimZillaApp.VERB_SECONDS))


# ---------------------------------------------------------------------------
# 2/3 — driven through the live bar
# ---------------------------------------------------------------------------

async def test_bar_turns(wd: Path) -> None:
    cfg = Config.from_env(workdir=wd, api_key="x",
                          base_url="http://localhost:4001", boot_rain=False)
    cfg.state_dir = wd / "ui-state"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()
        bar = app.query_one(StatusBar)

        # ---- busy: a frame, then a different frame ----
        bar.set_activity("thinking", busy=True)
        await pilot.pause()
        frames: list[str] = []
        for _ in range(4):
            app._tick_spin()
            await pilot.pause()
            frames.append(_frame_in(str(bar.render())))
        check("busy: the bar shows a wheel frame", all(frames), str(frames))
        check("busy: every tick is a different frame",
              len(set(frames)) == len(frames), str(frames))
        check("busy: the static glyph is retired",
              "⟳" not in str(bar.render()))

        # The wheel is on its own counter, so a repaint for another reason (a
        # token delta, a tool activity) cannot reset it mid-spin.
        before = bar._spin_frame
        bar.render_bar()
        check("busy: repainting for other reasons does not rewind the wheel",
              bar._spin_frame == before, f"{before} -> {bar._spin_frame}")

        # ---- idle: no work, and no wheel ----
        idle_before = bar._spin_frame
        bar.set_activity("idle", busy=False)
        app._tick_spin()
        await pilot.pause()
        idle_text = str(bar.render())
        check("idle: the wheel does not advance", bar._spin_frame == idle_before,
              f"{idle_before} -> {bar._spin_frame}")
        check("idle: the idle dot is what shows", "●" in idle_text and
              not _frame_in(idle_text), idle_text[-60:])

        # ---- compaction holds the bar: its own spinner owns the readout ----
        bar.set_activity("compacting history", busy=True, hold=True)
        held_before = bar._spin_frame
        for _ in range(3):
            app._tick_spin()
        await pilot.pause()
        check("held: the busy wheel stays out of compaction's way",
              bar._spin_frame == held_before,
              f"{held_before} -> {bar._spin_frame}")
        check("held: the compaction readout is still up",
              "compacting history" in str(bar.render()))

        # ---- the words: slow, and never over a running tool ----
        app.busy = True
        app.VERB_SECONDS = 0.02          # so the loop turns inside the test

        bar.set_activity("bash grep -r secret /srv", busy=True)
        task = asyncio.create_task(app._verb_spinner())
        await asyncio.sleep(0.09)
        task.cancel()
        await asyncio.sleep(0)
        check("verbs: a running tool's name is not replaced",
              bar.activity == "bash grep -r secret /srv", bar.activity)

        bar.set_activity("thinking", busy=True)
        task = asyncio.create_task(app._verb_spinner())
        await asyncio.sleep(0.09)
        task.cancel()
        await asyncio.sleep(0)
        check("verbs: a thinking state is renamed", bar.activity in THINKING_VERBS,
              bar.activity)
        app.busy = False


async def main() -> int:
    test_wheel_shape()
    with tempfile.TemporaryDirectory() as td:
        await test_bar_turns(Path(td))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
