"""Regression suite for draw_image — local SVG/HTML creation.

Run:  python tests/test_draw_image.py

This renders real images in Chromium (two of them), so it takes a few seconds.
The pixels are asserted, not just the file's existence: a drawing that save
a blank page is not an image tool.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-draw-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)

from zimzilla import tools  # noqa: E402
from zimzilla.config import MODES, Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(tmp: Path) -> Config:
    cfg = Config.from_env(workdir=tmp, base_url="https://logfare.ai/v1",
                          api_key="test")
    cfg.state_dir = tmp / "state"
    return cfg


def test_svg(tmp: Path) -> None:
    cfg = _cfg(tmp)
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">
      <rect width="400" height="300" fill="#dc1414"/>
      <circle cx="200" cy="150" r="80" fill="#1414dc"/>
      <text x="20" y="280" font-size="24" fill="white">ZIM</text>
    </svg>'''
    got = tools.execute("draw_image", {
        "spec": svg, "format": "svg", "width": 400, "height": 300,
        "dest": "images/test-svg.png",
    }, cfg)
    check("svg: the tool succeeds", not got.is_error, got.output)
    path = Path(got.meta.get("image", ""))
    check("svg: the PNG exists", path.is_file(), str(path))
    from PIL import Image
    image = Image.open(path).convert("RGB")
    check("svg: the dimensions are exact", image.size == (400, 300), str(image.size))
    check("svg: the canvas is full bleed (no 8px margin)",
          image.getpixel((0, 0)) == (220, 20, 20),
          str(image.getpixel((0, 0))))
    check("svg: the drawing is not blank",
          image.getpixel((200, 150)) == (20, 20, 220),
          str(image.getpixel((200, 150))))
    check("svg: the source is kept for editing",
          Path(got.meta.get("source", "")).is_file(), str(got.meta.get("source")))
    check("svg: it does not attach the picture to the model",
          not got.meta.get("images"))


def test_html(tmp: Path) -> None:
    cfg = _cfg(tmp)
    html = '''<!doctype html><html><body style="background:#071007;color:#00ff41;
      font-family:monospace;display:flex;align-items:center;justify-content:center;
      height:100vh"><h1>ZIMZILLA</h1></body></html>'''
    got = tools.execute("draw_image", {
        "spec": html, "format": "html", "width": 640, "height": 360,
        "dest": "images/test-html.png",
    }, cfg)
    check("html: the tool succeeds", not got.is_error, got.output)
    path = Path(got.meta.get("image", ""))
    check("html: the PNG exists", path.is_file(), str(path))
    from PIL import Image
    image = Image.open(path).convert("RGB")
    check("html: the dimensions are exact", image.size == (640, 360), str(image.size))
    check("html: the page background rendered",
          image.getpixel((5, 5)) == (7, 16, 7), str(image.getpixel((5, 5))))


def test_guards(tmp: Path) -> None:
    cfg = _cfg(tmp)
    check("guards: plan mode denies drawing",
          "draw_image" in MODES["plan"]["deny"])
    bad = tools.execute("draw_image", {"spec": ""}, cfg)
    check("guards: an empty spec is refused", bad.is_error, bad.output)
    outside = tools.execute("draw_image", {
        "spec": "<svg xmlns='http://www.w3.org/2000/svg'/>",
        "dest": "/etc/zimzilla-should-not-write.png"}, cfg)
    check("guards: the filesystem jail applies", outside.is_error, outside.output)


def test_wiring() -> None:
    names = {s["name"] for s in tools.TOOL_SCHEMAS}
    check("wiring: draw_image is advertised", "draw_image" in names)
    check("wiring: draw_image is dispatched", "draw_image" in tools._DISPATCH)
    check("wiring: aliases resolve", tools.resolve_tool_name("paint") ==
          "draw_image")
    agent = (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8")
    check("wiring: the model is taught to research then draw",
          "default to draw_image" in agent and "find references" in agent)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zim-draw-") as td:
        tmp = Path(td)
        test_svg(tmp)
        test_html(tmp)
        test_guards(tmp)
        test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
