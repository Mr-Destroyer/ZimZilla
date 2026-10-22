"""App-level coverage for the auto-compact animation, driven through the TUI.

Run:  python tests/test_compact_ui.py   (from an activated venv)

``test_compact_trigger.py`` pins the threshold arithmetic and the renderers in
isolation. This file pins the half those cannot see: that the app actually
*wires* the animation — that a `compacting` event opens the panel in the
transcript, that its timer advances the frame, that the gauge goes into its
folding sweep, and that the whole thing tears itself down afterwards instead of
leaving a timer painting a panel forever.

The failure this guards against is a silent one: an animation that never starts
looks exactly like a compaction that happened too fast to see, and a timer that
never stops looks like a terminal that got slower. Both are invisible to a unit
test of the renderer.

No network: ``_stream_once`` is replaced with a stub and ``compact`` with a fake
summariser, exactly as ``test_deliverable_ui`` does.
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
from zimzilla.ui.rails import ContextGauge  # noqa: E402
from zimzilla.ui.widgets import ChatPane, StatusBar  # noqa: E402

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


def _tool(used: int) -> _Msg:
    """A reply that calls a real tool and reports a measured prompt size."""
    return _Msg(
        [_Blk(type="tool_use", id="t-1", name="bash", input={"command": "true"})],
        stop_reason="tool_use", usage=_Usage(used),
    )


def _done() -> _Msg:
    return _Msg([_Blk(type="text", text="Done.")], stop_reason="end_turn",
                usage=_Usage(4_000))


def _scripted(app, plan) -> dict:
    state = {"n": 0}

    async def fake_stream(max_tokens=None):
        i = state["n"]
        state["n"] += 1
        yield (None, plan(i))

    app.agent._stream_once = fake_stream
    return state


def _transcript(app) -> str:
    return "\n".join(s.text for s in app.query_one("#transcript").lines)


async def _settle(pilot, app, rounds: int = 200) -> None:
    for _ in range(rounds):
        await pilot.pause()
        if not app.busy:
            return
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.25)
    await pilot.pause()


async def test_the_animation_runs_and_cleans_up(wd: Path) -> None:
    cfg = _cfg(wd, boot_rain=False, context_window=1_000_000,
               compact_at=0.8, compact_tokens=200_000)
    cfg.state_dir = wd / "ui-state"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        # Two tool rounds first, so the history is long enough for `compact()`
        # to have something to summarise (it refuses a history under four
        # messages, and `_should_compact` honours that). The second call reports
        # a 210k prompt on a window that claims 1M: under the old
        # percentage-only rule that is 21% of the window and nothing would ever
        # fire, so the 200k cap is what makes this compact at all.
        state = _scripted(
            app,
            lambda i: _tool(100_000) if i == 0
            else (_tool(210_000) if i == 1 else _done()),
        )

        compacted: list[str] = []

        async def fake_compact():
            # Slow enough that the animation is observably running while it is
            # awaited, which is the whole point of the panel.
            await asyncio.sleep(0.30)
            compacted.append("yes")
            app.agent.messages = [
                {"role": "user", "content": "[session summary] " + "x" * 800},
            ]
            return True, "the session built a thing and ran the tests"

        app.agent.compact = fake_compact  # type: ignore[assignment]

        app._submit_text("go")
        chat = app.query_one(ChatPane)
        opened = False
        for _ in range(120):
            await pilot.pause()
            if chat.compact_active:
                opened = True
                break
            await asyncio.sleep(0.01)

        check("ui: the compaction panel opened", opened)

        if opened:
            frame_a = str(app.query_one("#compact").render())
            await asyncio.sleep(0.12)
            await pilot.pause()
            frame_b = str(app.query_one("#compact").render())
            check("ui: the panel names the operation",
                  "CONTEXT COMPACTION" in frame_a, frame_a[:80])
            check("ui: the panel shows the measured prompt",
                  "210.0k" in frame_a, frame_a[:80])
            check("ui: the panel animates", frame_a != frame_b)
            check("ui: the timer is running while it compacts",
                  app._compact_timer is not None)
            bar = app.query_one(StatusBar)
            check("ui: the status bar is held on the compaction",
                  bar.hold is True and "compacting" in bar.activity,
                  f"{bar.hold} {bar.activity!r}")
            gauge = app.query_one(ContextGauge)
            check("ui: the gauge knows the 200k threshold",
                  gauge.threshold == 200_000, str(gauge.threshold))
            check("ui: the gauge is folding", gauge.compacting is True)

        await _settle(pilot, app)
        blob = _transcript(app)
        check("ui: the compaction ran once", len(compacted) == 1, str(compacted))
        check("ui: the compaction was announced in the transcript",
              "compacting history automatically" in blob, blob[-600:])
        check("ui: and its outcome was announced",
              "history compacted" in blob, blob[-400:])
        check("ui: the turn still finished", state["n"] == 3, str(state["n"]))

        # The panel lingers to show what was freed, then closes itself and the
        # timer stops. Give it longer than COMPACT_LINGER.
        for _ in range(60):
            await pilot.pause()
            if not chat.compact_active:
                break
            await asyncio.sleep(0.05)
        check("ui: the panel closed itself", not chat.compact_active)
        check("ui: the animation timer stopped", app._compact_timer is None)
        check("ui: the status bar is no longer held",
              app.query_one(StatusBar).hold is False)
        check("ui: the gauge stopped folding",
              app.query_one(ContextGauge).compacting is False)


async def main() -> int:
    wd = Path(tempfile.mkdtemp(prefix="zz-compact-ui-"))
    await test_the_animation_runs_and_cleans_up(wd)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
