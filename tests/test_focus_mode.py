"""Regression suite for the header focus toggle.

Run:  python tests/test_focus_mode.py

The button hides the loop and telemetry rails, gives the width to the harness,
and restores both on the second press.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-focus-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)

from textual.widgets import Button  # noqa: E402

from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402
from zimzilla.ui.rails import LoopRail, TelemetryRail  # noqa: E402
from zimzilla.ui.widgets import ChatPane, HeaderBar  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


async def test_toggle(tmp: Path) -> None:
    cfg = Config.from_env(workdir=tmp, base_url="https://logfare.ai/v1",
                          api_key="test")
    cfg.state_dir = tmp / "state"
    cfg.boot_rain = False
    app = ZimZillaApp(cfg)
    async with app.run_test(size=(140, 34)) as pilot:
        app.pop_screen()
        await pilot.pause()
        loop = app.query_one(LoopRail)
        tele = app.query_one(TelemetryRail)
        chat = app.query_one(ChatPane)
        header = app.query_one(HeaderBar)
        button = app.query_one("#focus-toggle", Button)
        wide = chat.size.width
        check("initial: the loop rail is visible", loop.display is True)
        check("initial: the telemetry rail is visible", tele.display is True)
        check("initial: the toggle is in the header", button in header.children)
        check("initial: the toggle shows the expand glyph", button.label == "⛶",
              str(button.label))
        check("initial: the button sits at the right edge",
              button.region.right >= app.size.width - 2,
              f"{button.region} / {app.size}")

        await pilot.click("#focus-toggle")
        await pilot.pause()
        check("focus: the loop rail is hidden", loop.display is False)
        check("focus: the telemetry rail is hidden", tele.display is False)
        check("focus: the screen carries the focus class",
              app.screen.has_class("focus-mode"))
        check("focus: the harness takes the freed width", chat.size.width > wide,
              f"{wide} -> {chat.size.width}")
        check("focus: the toggle flips glyph", button.label == "⊡",
              str(button.label))

        await pilot.click("#focus-toggle")
        await pilot.pause()
        check("restore: the loop rail returns", loop.display is True)
        check("restore: the telemetry rail returns", tele.display is True)
        check("restore: the focus class is gone",
              not app.screen.has_class("focus-mode"))
        check("restore: the harness width returns", chat.size.width == wide,
              f"{chat.size.width} vs {wide}")
        check("restore: the toggle glyph returns", button.label == "⛶",
              str(button.label))
        check("header: still renders its text", "ZIMZILLA" in str(header.render()))


def test_wiring() -> None:
    widgets = (ROOT / "zimzilla" / "ui" / "widgets.py").read_text()
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text()
    check("wiring: the button exists", 'id="focus-toggle"' in widgets)
    check("wiring: the focus CSS exists", "Screen.focus-mode LoopRail" in app)
    check("wiring: the click handler exists", "def on_button_pressed" in app)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zim-focus-") as td:
        asyncio.run(test_toggle(Path(td)))
    test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
