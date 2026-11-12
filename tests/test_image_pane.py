"""Regression suite for the image pane and the ``show_image`` tool.

Run:  python tests/test_image_pane.py

What this pins:

1. **The renderer.** An image becomes coloured half blocks at exactly the
   requested cell size; zoom changes the crop rather than the geometry.
2. **The pane.** It is hidden until shown, carries the picture, zooms, pans,
   resizes, and closes. The model-facing side effect is exercised through the
   same ``tool_result`` event the app handles.
3. **The tool.** ``show_image`` resolves a local path through the jail, refuses
   a denied URL, returns the pane descriptor, and does *not* attach the picture
   to the conversation (that is ``view_image``'s job).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-image-pane-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)

from zimzilla import tools  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.scope import Scope  # noqa: E402
from zimzilla.ui.imageview import halfblocks, load  # noqa: E402
from zimzilla.ui.widgets import ImagePane  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _png(width: int = 96, height: int = 64) -> bytes:
    """A red/blue split image, so a crop is visibly different from a fit."""
    rows = []
    for y in range(height):
        row = bytearray(b"\x00")
        for x in range(width):
            row += bytes((220, 20, 20)) if x < width // 2 else bytes((20, 20, 220))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _cfg(tmp: Path) -> Config:
    cfg = Config.from_env(workdir=tmp, base_url="https://logfare.ai/v1",
                          api_key="test")
    cfg.state_dir = tmp / "state"
    return cfg


# ---------------------------------------------------------------------------
# 1 — renderer
# ---------------------------------------------------------------------------

def test_renderer(tmp: Path) -> None:
    path = tmp / "split.png"
    path.write_bytes(_png())
    image = load(path)
    check("render: the image loads", image.size == (96, 64), str(image.size))

    text = halfblocks(image, 40, 12)
    lines = text.plain.split("\n")
    check("render: one line per row", len(lines) == 12, str(len(lines)))
    check("render: one cell per column", all(len(line) == 40 for line in lines),
          str([len(line) for line in lines]))
    check("render: it uses the half block", "▀" in text.plain)
    check("render: the split is visible",
          "#dc1414" in str(text.spans) and "#1414dc" in str(text.spans))

    zoomed = halfblocks(image, 40, 12, zoom=2.0)
    check("render: zoom changes the picture",
          str(zoomed.spans) != str(text.spans))
    check("render: zoom keeps the geometry",
          len(zoomed.plain.split("\n")) == 12
          and all(len(line) == 40 for line in zoomed.plain.split("\n")))

    small = halfblocks(image, 20, 6)
    check("render: a small pane still draws", len(small.plain.split("\n")) == 6)
    check("render: a clamped zoom still draws",
          len(halfblocks(image, 20, 6, zoom=0.01).plain.split("\n")) == 6)


# ---------------------------------------------------------------------------
# 2 — the widget
# ---------------------------------------------------------------------------

async def test_pane(tmp: Path) -> None:
    from zimzilla.ui.app import ZimZillaApp

    path = tmp / "split.png"
    path.write_bytes(_png())
    cfg = _cfg(tmp)
    cfg.boot_rain = False
    app = ZimZillaApp(cfg)
    async with app.run_test(size=(140, 45)) as pilot:
        app.pop_screen()
        await pilot.pause()
        pane = app.query_one(ImagePane)
        check("pane: it starts hidden", not pane.has_class("visible"))
        pane.show(str(path), caption="a split image")
        await pilot.pause()
        check("pane: show makes it visible", pane.has_class("visible"))
        check("pane: it loaded the image", pane.image is not None)
        check("pane: it renders blocks", "▀" in pane.render().plain)
        start_zoom = pane.zoom
        pane.action_zoom_in()
        check("pane: zoom in raises the zoom", pane.zoom > start_zoom, str(pane.zoom))
        pane.action_zoom_out()
        check("pane: zoom out lowers it", pane.zoom < start_zoom * 1.25)
        pane.action_fit()
        check("pane: fit restores 100%", pane.zoom == 1.0)
        pane.action_wider()
        await pilot.pause()
        check("pane: it can grow", pane.size.width >= ImagePane.MIN_WIDTH,
              str(pane.size.width))
        pane.action_narrower()
        check("pane: it clamps at the minimum",
              pane._width >= ImagePane.MIN_WIDTH, str(pane._width))
        pane.action_close()
        await pilot.pause()
        check("pane: close hides it", not pane.has_class("visible"))

        # The model's route: a tool_result carrying the pane descriptor. This
        # is the actual event branch, not a direct call to the helper.
        await app._handle_event({
            "type": "tool_result", "name": "show_image",
            "args": {"path": str(path)}, "id": "t1",
            "output": "showing the image", "is_error": False,
            "meta": {"image_pane": {"path": str(path), "caption": "from a tool",
                                    "zoom": 1.5}},
            "diff": None,
        })
        await pilot.pause()
        check("pane: a tool result opens it", pane.has_class("visible"))
        check("pane: the tool's zoom is applied", pane.zoom == 1.5)
        out = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("pane: the transcript says it was shown", "image shown" in out,
              out[-200:])


# ---------------------------------------------------------------------------
# 3 — the tool
# ---------------------------------------------------------------------------

def test_tool(tmp: Path) -> None:
    names = {s["name"] for s in tools.TOOL_SCHEMAS}
    check("tool: show_image is advertised", "show_image" in names)
    check("tool: show_image is dispatched", "show_image" in tools._DISPATCH)
    check("tool: aliases resolve", tools.resolve_tool_name("display_image") ==
          "show_image")

    path = tmp / "split.png"
    path.write_bytes(_png())
    cfg = _cfg(tmp)
    got = tools.execute("show_image", {"path": str(path),
                                       "caption": "hello", "zoom": 2}, cfg)
    check("tool: it succeeds", not got.is_error, got.output)
    pane = got.meta.get("image_pane") or {}
    check("tool: it returns a pane descriptor", pane.get("path") == str(path),
          str(pane))
    check("tool: it carries the caption", pane.get("caption") == "hello")
    check("tool: it carries the zoom", pane.get("zoom") == 2.0)
    check("tool: it does not attach the image to the model",
          not got.meta.get("images"))

    with mock.patch("zimzilla.browser.download_image", return_value=path):
        remote = tools.execute("show_image", {"path": "https://example.com/x.png"},
                               cfg)
    check("tool: an http(s) URL is downloaded and shown",
          not remote.is_error and remote.meta.get("image_pane"), remote.output)

    denied = tmp / "out-of-scope.yaml"
    denied.write_text("domain:\n  - blocked.example\n", encoding="utf-8")
    cfg._scope = Scope.load(None, denied)
    blocked = tools.execute("show_image", {"path": "https://blocked.example/a.png"},
                            cfg)
    check("tool: the scope guard stops a URL",
          blocked.is_error and blocked.meta.get("blocked"), blocked.output)

    bad = tools.execute("show_image", {"path": str(tmp / "nope.png")}, cfg)
    check("tool: a missing image is an error", bad.is_error, bad.output)


# ---------------------------------------------------------------------------
# 4 — wiring
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    widgets = (ROOT / "zimzilla" / "ui" / "widgets.py").read_text(encoding="utf-8")
    check("wiring: the pane is in the layout", "yield ImagePane(self.palette)" in app)
    check("wiring: a tool result can open it", 'image_pane' in app)
    check("wiring: the widget has zoom keys", "action_zoom_in" in widgets)
    check("wiring: the widget can resize", "action_wider" in widgets)
    check("wiring: dragging the edge resizes", "RESIZE_GUTTER" in widgets)
    check("deps: Pillow is declared",
          "Pillow" in (ROOT / "requirements.txt").read_text()
          or "pillow" in (ROOT / "requirements.txt").read_text().lower())


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zim-image-pane-") as td:
        tmp = Path(td)
        test_renderer(tmp)
        asyncio.run(test_pane(tmp))
        test_tool(tmp)
        test_wiring()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
