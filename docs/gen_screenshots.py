"""Regenerate the README screenshots (docs/screenshots/*.png).

Run:  python docs/gen_screenshots.py   (from an activated venv)

Each shot is a real ZimZilla app driven by a stub agent, exported with
Textual's own ``export_screenshot()`` and rasterised with headless chromium.
Nothing here is mocked up by hand — if the UI changes, re-run this and the
README follows.

The stub agent is deliberately *slow* (see ``_slow_execute``): a tool that
returns instantly flashes straight through its running state, and the live
tool card is the thing worth photographing.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from zimzilla import agent as agent_mod  # noqa: E402
from zimzilla import tools as tools_mod  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402

from test_phase4 import _Blk, _Msg, _Usage  # noqa: E402

OUT = Path(__file__).resolve().parent / "screenshots"

#: Wide enough for both rails; the shell shot is the full Mission Control.
SHELL_SIZE = (128, 36)
#: Boot is a centred card, so it needs less width and more height.
BOOT_SIZE = (104, 32)
#: The list shots are transcript-only — no rails to show off. The header
#: needs ~120 cols to show every field, so keep the width above that.
LIST_SIZE = (124, 34)


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


def _stub(workdir: Path):
    """An agent that runs a bash command, reads, edits, then summarises.

    Shaped like a real debugging turn so the rails have something to say:
    two bash calls (one that fails), a read, an edit.
    """
    agent = agent_mod.Agent(
        _cfg(workdir, mode="auto"),
        permission_handler=lambda *a: asyncio.sleep(0, result="yes"),
    )
    state = {"n": 0}

    async def fake_stream():
        state["n"] += 1
        n = state["n"]
        if n == 1:
            text = "Let me run the suite and see what breaks."
            yield ({"type": "text_delta", "text": text}, None)
            yield (None, _Msg([
                _Blk(type="text", text=text),
                _Blk(type="tool_use", id="t1", name="bash",
                     input={"command": "curl -s https://api.example.com/health | head -3"}),
            ], _Usage(38_400, 96)))
            return
        if n == 2:
            text = "Two failures in the auth module. Let me look at the check."
            yield ({"type": "text_delta", "text": text}, None)
            yield (None, _Msg([
                _Blk(type="text", text=text),
                _Blk(type="tool_use", id="t2", name="bash",
                     input={"command": "pytest tests/ -k auth"}),
                _Blk(type="tool_use", id="t3", name="read_file",
                     input={"path": "auth.py"}),
            ], _Usage(71_900, 210)))
            return
        if n == 3:
            text = "The TTL comparison used the wrong unit. Patching it."
            yield ({"type": "text_delta", "text": text}, None)
            yield (None, _Msg([
                _Blk(type="text", text=text),
                _Blk(type="tool_use", id="t4", name="edit_file",
                     input={"path": "auth.py",
                            "old_string": "    return t > 0",
                            "new_string": "    return t > TOKEN_TTL"}),
            ], _Usage(88_100, 140)))
            return
        yield (None, _Msg([
            _Blk(type="text", text="Fixed — the comparison was against zero "
                                   "instead of the TTL. Re-ran the suite: 9 passed."),
        ], _Usage(91_300, 88)))

    agent._stream_once = fake_stream
    return agent


def _slow_execute(real, delay: float = 1.1):
    """Wrap tools.execute so a tool stays 'running' long enough to photograph."""

    def wrapper(name, args, cfg):
        time.sleep(delay)
        return real(name, args, cfg)

    return wrapper


# Rich's ``export_svg`` frames the terminal in a fake OS window: a #292929
# rounded rect, a centred title, and three macOS traffic-light dots. None of it
# is the app — the compositor fills every cell with the palette background, so
# the terminal is uniformly black. In a README the frame reads as a grey band
# around the whole interface, and the rect's rounded corners show through
# behind the corner glyphs of every bordered box. Strip the chrome, and slide
# the terminal group up to the origin so the SVG *is* the terminal.
_CHROME = re.compile(
    r'<rect fill="#292929".*?rx="8"/>'
    r'(?:<text class="[^"]*-title".*?</text>)?'
    r'\s*<g transform="translate\(26,22\)">.*?</g>',
    re.S,
)
_TERM_GROUP = re.compile(r'<g transform="translate\([\d.]+, [\d.]+\)"')


def _strip_export_chrome(svg: str) -> str:
    stripped, n = _CHROME.subn("", svg)
    if n != 1:
        raise SystemExit(f"expected one export chrome block, found {n}")
    return _TERM_GROUP.sub('<g transform="translate(0, 0)"', stripped, count=1)


#: Textual exports one SVG user-unit per terminal cell, and the SVG's viewBox is
#: sized in those units — so the window has to match that shape or chromium
#: letterboxes it. With the window chrome stripped a letterbox is black on
#: black, which silently pads every shot with a dead margin.
_VIEWBOX = re.compile(r'viewBox="0 0 ([\d.]+) ([\d.]+)"')

#: The README shows these at 880px, so render ~2x that and let the SVG scale the
#: rest of the way. A fixed target width keeps every shot the same size on the
#: page and the PNGs small; the SVG scales to whatever window it is given, so
#: only the *ratio* has to come from the viewBox.
_TARGET_PX = 1800
_SCALE = 2


def _rasterise(svg_text: str, png: Path) -> None:
    """SVG -> PNG via headless chromium, at the SVG's own aspect ratio."""
    chrome = (shutil.which("chromium") or shutil.which("chromium-browser")
              or shutil.which("google-chrome"))
    if chrome is None:
        raise SystemExit("chromium not found — cannot rasterise the SVGs")
    m = _VIEWBOX.search(svg_text)
    if m is None:
        raise SystemExit("no viewBox in the exported SVG")
    vw, vh = float(m.group(1)), float(m.group(2))
    w = _TARGET_PX // _SCALE
    h = round(w * vh / vw)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "shot.svg"
        tmp.write_text(svg_text)
        subprocess.run(
            [chrome, "--headless", "--no-sandbox", "--disable-gpu",
             "--hide-scrollbars", f"--force-device-scale-factor={_SCALE}",
             f"--screenshot={png}", f"--window-size={w},{h}",
             # Opaque black rather than transparent: the terminal is black, so
             # any rounding sliver blends in instead of showing the page behind.
             "--default-background-color=FF000000", f"file://{tmp}"],
            check=True, capture_output=True,
        )


def _write_shot(app, name: str) -> None:
    _rasterise(_strip_export_chrome(app.export_screenshot()), OUT / f"{name}.png")
    print(f"  wrote {name}.png")


async def _shot_boot(wd: Path) -> None:
    """The boot screen with every check resolved, just before it hands over.

    The animation runs at 0.045s/tick: the banner reveal takes ~0.2s, the
    eight check rows resolve one per ~0.18s (~1.6s all told), and the screen
    auto-continues 2.2s after the fade — so ~2.1s in is the frame where the
    whole checklist is legible and the ready line is up.
    """
    app = ZimZillaApp(_cfg(wd, theme="green", allow_path=wd / "allow.yaml"))
    async with app.run_test(size=BOOT_SIZE) as pilot:
        # Do NOT pop the boot screen: this shot *is* the boot screen.
        await pilot.pause()
        await asyncio.sleep(2.9)
        await pilot.pause()
        _write_shot(app, "01-boot")


async def _shot_shell(wd: Path) -> None:
    """A turn in flight: card live, loop on CALL, rails populated."""
    real = tools_mod.execute
    tools_mod.execute = _slow_execute(real)
    try:
        app = ZimZillaApp(_cfg(wd, boot_rain=False, theme="green"))
        app.agent = _stub(wd)
        async with app.run_test(size=SHELL_SIZE) as pilot:
            app.pop_screen()
            await pilot.pause()
            app.query_one("#input").focus()
            await pilot.pause()

            # Seed the waveform and the file river so the rails read as busy
            # rather than as a session that just started.
            from zimzilla.ui.rails import TelemetryRail
            tele = app.query_one(TelemetryRail)
            for v in (42, 88, 130, 96, 168, 205, 152, 118, 176):
                tele.push_wave(float(v))
            tele.touch_file("auth.py")
            tele.touch_file("auth.py")
            tele.touch_file("config.py")
            await pilot.pause()

            app._run_turn("fix the auth bug")
            from zimzilla.ui.widgets import ChatPane
            for _ in range(40):
                await pilot.pause()
                if app.query_one(ChatPane).card_active:
                    break
            await asyncio.sleep(0.35)
            await pilot.pause()
            _write_shot(app, "02-shell")

            # Let it finish so the process exits cleanly.
            for _ in range(400):
                await pilot.pause()
                if not app.busy:
                    break
    finally:
        tools_mod.execute = real


async def _shot_list(wd: Path, name: str, command: str) -> None:
    """A transcript-only shot: the /model or /mode listing."""
    app = ZimZillaApp(_cfg(wd, boot_rain=False, theme="green"))
    app.agent = _stub(wd)
    async with app.run_test(size=LIST_SIZE) as pilot:
        app.pop_screen()
        await pilot.pause()
        app.query_one("#input").focus()
        await pilot.pause()
        app._handle_command(command)
        await pilot.pause()
        await asyncio.sleep(0.2)
        await pilot.pause()
        _write_shot(app, name)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        # A tiny project for the agent to work in, so paths in the transcript
        # look like real files rather than a bare temp dir.
        (wd / "auth.py").write_text(
            "TOKEN_TTL = 3600\n\n\ndef check(t):\n    return t > 0\n")
        (wd / "config.py").write_text("TTL = 3600\nDEBUG = False\n")
        (wd / "alpha.py").write_text("print('a')\n")
        (wd / "beta.md").write_text("# beta\n")
        # A declared scope, so the boot checklist shows an armed session
        # rather than "not loaded". Presence of the file is what arms it.
        # Note: allow.yaml is discovered in __main__, not Config.from_env, so
        # it has to be passed to the app explicitly here (see _shot_boot).
        allow = wd / "allow.yaml"
        allow.write_text("in_scope:\n  - api.example.com\n  - 10.0.0.0/24\n")

        print("generating screenshots…")
        await _shot_boot(wd)
        await _shot_shell(wd)
        await _shot_list(wd, "03-models", "/model")
        await _shot_list(wd, "04-modes", "/mode")
    print("done.")


if __name__ == "__main__":
    asyncio.run(main())
