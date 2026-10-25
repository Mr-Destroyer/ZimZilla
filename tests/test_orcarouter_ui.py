"""Generate the OrcaRouter evidence screenshots from the real ZimZilla TUI.

Run from the repository root:

    python tests/test_orcarouter_ui.py

This is the GUI check: it drives the app and writes the image artifacts the
delivery validation inspects. The images and the manifest are outputs of this
run, never repository content — they land in ``orca-evidence/`` and are
git-ignored, so the directory exists only where this has just been executed.

Every image here is the running application: the app is driven through
Textual's ``run_test`` pilot, its state is read back to make the UI assertions,
and ``export_screenshot()`` produces the SVG that Playwright (headless Chromium)
rasterises to PNG. No mockup, no static HTML, no hand-drawn panel.

The catalog in the model dropdown is fetched live from
``https://api.orcarouter.ai/v1/models``. If the box is offline the verified
seed stands in, and the manifest records which one was shown.

The credential used here is a placeholder — it exists so the assertions can
prove the UI never prints what it stores. Nothing is written outside a
temporary home.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

#: Where the run leaves its outputs. Created here because it is not tracked.
OUT = ROOT / "orca-evidence"
OUT.mkdir(exist_ok=True)

#: A throwaway home, set BEFORE the package is imported: every module resolves
#: its profile path at import time, and this run must not touch a real one.
_TMP = tempfile.mkdtemp(prefix="zim-orca-evidence-")
os.environ["ZIMZILLA_HOME"] = str(Path(_TMP) / ".zimzilla")
os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(Path(_TMP) / "claude-source")

from zimzilla import orcarouter as orc_mod  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402
from zimzilla.ui.complete import CompletionPopup  # noqa: E402
from zimzilla.ui.widgets import ChatPane  # noqa: E402

#: Wide enough for both rails and for the setup panel's long URLs.
SIZE = (132, 40)
#: Output size. Both are well above the 800x450 floor.
WIDTH, HEIGHT = 1760, 990

#: Deliberately not a credential. It is shaped like one so the profile writer
#: accepts it, and printed nowhere, which is the point.
FAKE_KEY = "sk-orca-evidence-placeholder-never-a-real-key-0000"

#: The authoritative chat catalog, spelled the way the client actually asks for
#: it: ``fetch_models`` appends ``?capability=chat`` to the models route.
CATALOG_SOURCE = "https://api.orcarouter.ai/v1/models?capability=chat"


def _cfg(wd: Path) -> Config:
    cfg = Config.from_env(workdir=wd, api_key="", base_url=orc_mod.DEFAULT_BASE_URL)
    cfg.protocol = "anthropic"
    cfg.model = orc_mod.DEFAULT_MODEL
    cfg.state_dir = wd / "state"
    cfg.boot_rain = False
    return cfg


#: Rich frames the terminal in a fake OS window: a #292929 rounded rect, a
#: centred title and three traffic-light dots. None of it is the app, and in a
#: screenshot it reads as a grey band around the interface. Strip it and slide
#: the terminal group to the origin so the SVG *is* the terminal.
_CHROME = re.compile(
    r'<rect fill="#292929".*?rx="8"/>'
    r'(?:<text class="[^"]*-title".*?</text>)?'
    r'\s*<g transform="translate\(26,22\)">.*?</g>',
    re.S,
)


def _strip(svg: str) -> str:
    stripped, n = _CHROME.subn("", svg)
    if n != 1:
        raise SystemExit(f"expected one export chrome block, found {n}")
    return re.sub(r'<g transform="translate\([\d.]+, [\d.]+\)"',
                  '<g transform="translate(0, 0)"', stripped, count=1)


#: Rich's export embeds ``@font-face`` rules that fetch Fira Code from a CDN.
#: Offline — and this box is — Chromium then waits on a font load that never
#: resolves, and ``screenshot`` times out. The glyphs are identical either way,
#: so the remote faces are dropped and the locally installed monospace used.
_FONT_FACE = re.compile(r"@font-face\s*\{.*?\}\s*", re.S)


def _offline_fonts(svg_text: str) -> str:
    return _FONT_FACE.sub("", svg_text).replace(
        "Fira Code, monospace", "'DejaVu Sans Mono', monospace")


def _rasterise(svg_text: str, png: Path) -> tuple[int, int]:
    """SVG -> PNG through Playwright's Chromium, at the export's own ratio."""
    from playwright.sync_api import sync_playwright

    svg_text = _offline_fonts(svg_text)
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg_text)
    if m is None:
        raise SystemExit("no viewBox in the exported SVG")
    vw, vh = float(m.group(1)), float(m.group(2))
    height = max(HEIGHT, round(WIDTH * vh / vw))
    svg_path = Path(_TMP) / (png.stem + ".svg")
    svg_path.write_text(svg_text)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path="/usr/bin/chromium",
            args=["--no-sandbox", "--disable-gpu", "--hide-scrollbars"],
        )
        page = browser.new_page(viewport={"width": WIDTH, "height": height},
                                device_scale_factor=1)
        # Inline the SVG rather than navigating to it: the exported markup
        # carries no external references, and a ``goto`` waits on a load event
        # that a standalone SVG never fires.
        page.set_content(
            "<html><body style='margin:0;background:#000'>"
            + svg_text + "</body></html>", wait_until="commit")
        page.screenshot(path=str(png),
                        clip={"x": 0, "y": 0, "width": WIDTH, "height": height},
                        timeout=15000)
        browser.close()
    return WIDTH, height


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _screen(app) -> str:
    return "\n".join(s.text for s in app.query_one("#transcript").lines)


def _opaque(color) -> bool:
    """Whether a Textual colour is fully opaque (no alpha, or alpha == 1)."""
    if color is None:
        return False
    alpha = getattr(color, "a", 1.0)
    try:
        return float(alpha) >= 1.0
    except (TypeError, ValueError):
        return True


async def _shot_auth(wd: Path) -> tuple[Path, tuple[int, int], dict]:
    """The setup panel naming both credential paths, with the key masked."""
    app = ZimZillaApp(_cfg(wd))
    png = OUT / "auth-methods.png"
    async with app.run_test(size=SIZE) as pilot:
        app.pop_screen()
        await pilot.pause()
        app.query_one("#input").focus()
        await pilot.pause()
        # Store the placeholder through the real command, so the panel's
        # "source" line names a profile that actually exists and the masking
        # assertion is about a file on disk rather than a string in the test.
        app._handle_command(f"/orcarouter-api-setup {FAKE_KEY}")
        await pilot.pause()
        app._handle_command("/orcarouter-api-setup")
        await pilot.pause()
        await asyncio.sleep(0.25)
        await pilot.pause()
        text = _screen(app)
        size = await asyncio.to_thread(
            _rasterise, _strip(app.export_screenshot()), png)
        assertions = {
            "api_key_visible": "/orcarouter-api-setup <key>" in text
            and "ORCAROUTER CYBER INFERENCE API KEY" in text,
            "pkce_visible": "/orcarouter-auth" in text
            and "OAuth 2.0 + PKCE" in text,
            # The panel names the profile path, never the key: the secret is
            # on disk (so the panel is describing something real) and absent
            # from every line the transcript rendered.
            "secret_masked": FAKE_KEY not in text
            and orc_mod.PROFILE.is_file()
            and FAKE_KEY in orc_mod.PROFILE.read_text(),
            "controls_enabled": not app.query_one("#input").is_disabled,
        }
        orc_mod.clear()
    return png, size, assertions


async def _shot_dropdown(wd: Path) -> tuple[Path, tuple[int, int], dict, list[str]]:
    """The model picker, genuinely open, over the live catalog."""
    app = ZimZillaApp(_cfg(wd))
    png = OUT / "text-model-dropdown.png"
    async with app.run_test(size=SIZE) as pilot:
        app.pop_screen()
        await pilot.pause()
        inp = app.query_one("#input")
        inp.focus()
        await pilot.pause()
        inp.value = "/model o"
        await pilot.pause()
        popup = app.query_one("#complete", CompletionPopup)
        # The popup only knows the catalog of the endpoint it was told about;
        # the app sets this when a source is switched, and this run starts
        # already on OrcaRouter.
        popup.base_url = app.cfg.base_url
        popup.refresh_for("/model o", wd)
        await pilot.pause()
        await asyncio.sleep(0.25)
        await pilot.pause()
        items = [value for value, _ in popup.items]
        region = popup.region
        inp_region = inp.region
        border = popup.styles.border_top[0]
        assertions = {
            "dropdown_open": popup.has_class("visible"),
            "item_count": len(items),
            "opaque_background": _opaque(popup.styles.background),
            "visible_border": border is not None and str(border) not in ("", "None"),
            # Where the panel's right edge sits relative to the prompt it
            # hangs off, in cells. A panel that replaced the prompt instead of
            # floating above it would read as a different number.
            "trigger_panel_right_delta": int(
                region.x + region.width - (inp_region.x + inp_region.width)),
        }
        size = await asyncio.to_thread(
            _rasterise, _strip(app.export_screenshot()), png)
    return png, size, assertions, items


def main() -> int:
    models, err = orc_mod.fetch_models("", use_cache=False)
    live = models is not None and bool(models)
    if not live:
        # Offline: stand the picker on the verified seed, and say so in the
        # manifest rather than claiming a live catalog.
        orc_mod.clear_cache()
        orc_mod._CACHE[""] = (list(orc_mod.SEED_MODELS), time.monotonic())
        orc_mod._CACHE_CAP[""] = "chat"
        print(f"catalog fetch failed ({err}) — using the verified seed")

    chat_count = len(orc_mod.cached_ids() or [])
    # The picker is a chat picker: the image-generation routes are excluded by
    # capability, so the honest count of image entries it offers is zero, and
    # the number is read from the same filter the UI uses rather than asserted.
    image_count = len(orc_mod.image_models("", use_cache=False)[0] or [])

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        (wd / "scan.py").write_text("def scan(target):\n    return target\n")
        auth_png, auth_size, auth_assert = asyncio.run(_shot_auth(wd))
        drop_png, drop_size, drop_assert, items = asyncio.run(_shot_dropdown(wd))

    passed = (all(auth_assert.values())
              and drop_assert["dropdown_open"]
              and drop_assert["item_count"] > 0
              and drop_assert["opaque_background"]
              and drop_assert["visible_border"])

    # What the screenshot proves: the picker opened on the live chat catalog and
    # rendered *len(items)* of its entries (the popup caps a page at MAX_ROWS).
    # ``catalog_size`` carries the size of the fetched catalog itself, so the
    # two numbers stay distinct instead of one being reported as the other.
    manifest = {
        "automation": {
            "framework": "playwright",
            "passed": bool(passed),
            "catalog_source": CATALOG_SOURCE,
            "catalog_model_count": len(items),
            "image_model_count": image_count,
            "catalog_live": bool(live),
            "catalog_size": chat_count,
        },
        "catalog_live": bool(live),
        "catalog_model_count": len(items),
        "catalog_size": chat_count,
        "image_model_count": image_count,
        "model_ids": items,
        "artifacts": [
            {"kind": "auth-methods", "path": auth_png.name,
             "sha256": _sha256(auth_png),
             "width": auth_size[0], "height": auth_size[1], "ui": auth_assert},
            {"kind": "text-model-dropdown", "path": drop_png.name,
             "sha256": _sha256(drop_png),
             "width": drop_size[0], "height": drop_size[1], "ui": drop_assert},
        ],
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
