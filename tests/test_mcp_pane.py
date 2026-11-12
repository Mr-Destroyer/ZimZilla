"""Regression suite for the live MCP activity pane.

Run:  python tests/test_mcp_pane.py

What this pins:

1. **The call appears live.** A `tool_call` event opens the pane, names the MCP
   tool and shows its arguments before the result exists.
2. **The result and the diff.** A local file named by the call is snapshotted
   before execution and diffed after it; the pane shows the unified diff, not
   just "done".
3. **The panel is usable.** Clear, resize, close, and the mutual exclusion with
   the image pane.
4. **Wiring.** The app routes only MCP tool events to it, and the pane is in the
   layout.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-mcp-pane-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)

from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.widgets import McpPane  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(tmp: Path) -> Config:
    cfg = Config.from_env(workdir=tmp, base_url="https://logfare.ai/v1",
                          api_key="test")
    cfg.state_dir = tmp / "state"
    cfg.boot_rain = False
    return cfg


def _text(pane: McpPane) -> str:
    return "\n".join(strip.text for strip in pane._log.lines)


async def test_pane(tmp: Path) -> None:
    from zimzilla.ui.app import ZimZillaApp
    from zimzilla.ui.widgets import ImagePane

    target = tmp / "main.py"
    target.write_text("def main():\n    return 1\n", encoding="utf-8")
    from PIL import Image
    logo = tmp / "logo.png"
    Image.new("RGB", (32, 32), (0, 200, 80)).save(logo)
    cfg = _cfg(tmp)
    app = ZimZillaApp(cfg)
    async with app.run_test(size=(150, 45)) as pilot:
        app.pop_screen()
        await pilot.pause()
        pane = app.query_one(McpPane)
        check("pane: it starts hidden", not pane.has_class("visible"))

        app._mcp_call({
            "type": "tool_call", "name": "mcp_neovim_edit_file",
            "args": {"path": str(target), "new_string": "return 2"},
            "id": "call-1", "gated": False,
        })
        await pilot.pause()
        check("pane: a call opens it", pane.has_class("visible"))
        text = _text(pane)
        check("pane: it names the tool", "mcp_neovim_edit_file" in text, text[:120])
        check("pane: it shows the arguments", "new_string" in text, text[:200])
        check("pane: the header counts the call", "1 call" in pane._header.render().plain,
              pane._header.render().plain)

        target.write_text("def main():\n    return 2\n", encoding="utf-8")
        app._mcp_result({
            "type": "tool_result", "name": "mcp_neovim_edit_file",
            "id": "call-1", "args": {"path": str(target)},
            "output": "buffer written", "is_error": False, "meta": {},
            "diff": None,
        })
        await pilot.pause()
        text = _text(pane)
        check("pane: the result arrives", "buffer written" in text, text[-300:])
        check("pane: the on-disk diff arrives",
              "-    return 1" in text and "+    return 2" in text, text[-800:])
        check("pane: the header shows the call finished",
              "running" not in pane._header.render().plain)

        # An image shown through the other pane takes the column back.
        image = app.query_one(ImagePane)
        app._show_image_pane({"path": str(logo)})
        await pilot.pause()
        check("pane: an image replaces it in the shared column",
              not pane.has_class("visible"))

        app._mcp_call({"type": "tool_call", "name": "mcp_neovim_command",
                       "args": {"command": ":w"}, "id": "call-2"})
        await pilot.pause()
        check("pane: the next MCP call brings it back", pane.has_class("visible"))
        pane.action_clear_log()
        check("pane: clear resets the log", _text(pane).strip() == "")
        check("pane: clear resets the counter", "0 call" in pane._header.render().plain)
        pane.action_wider()
        await pilot.pause()
        check("pane: it can resize", pane.size.width >= McpPane.MIN_WIDTH,
              str(pane.size.width))
        pane.action_close()
        await pilot.pause()
        check("pane: close hides it", not pane.has_class("visible"))

        # Non-MCP tools must not open it.
        app._handle_command("/cost")
        await pilot.pause()
        check("pane: ordinary tools do not open it", not pane.has_class("visible"))


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    widgets = (ROOT / "zimzilla" / "ui" / "widgets.py").read_text(encoding="utf-8")
    check("wiring: the pane is in the layout", "yield McpPane(self.palette)" in app)
    check("wiring: MCP tool calls are routed",
          'startswith("mcp_")' in app and "self._mcp_call(ev)" in app)
    check("wiring: MCP results are routed", "self._mcp_result(ev)" in app)
    check("wiring: the diff is snapshotted before the call",
          "before = (path.read_text" in app)
    check("wiring: the pane has clear/resize/close",
          "action_clear_log" in widgets and "action_wider" in widgets
          and "action_close" in widgets)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zim-mcp-pane-") as td:
        asyncio.run(test_pane(Path(td)))
        test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
