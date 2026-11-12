"""Regression suite for the visual browser — /see, browse, view_image.

Run:  python tests/test_vision.py

What this pins, and why each half is worth a test:

1. **The image layer.** A placeholder carries a path; ``materialise`` turns it
   into the Anthropic shape and Chat Completions gets an ``image_url`` part.
   The failure this guards is silent: an image that is dropped in translation
   looks exactly like a model that ignored the picture.
2. **The browser.** A real page is rendered and read: title, visible text,
   images, a GIF, a video, a comment, a screenshot. The fixture is served from
   a local HTTP server, so this needs no network and no site's markup.
3. **The tools.** ``browse`` renders and returns attachments; a denied host is
   refused before Chromium opens; ``view_image`` can read the evidence the
   browser just wrote under the state directory.
4. **The wiring.** ``/see`` is dispatched, documented in ``/help``, offered by
   completion, and starts a turn with ``image_paths``.
"""

from __future__ import annotations

import atexit
import base64
import http.server
import os
import shutil
import struct
import sys
import tempfile
import threading
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-vision-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)

from zimzilla import browser, images, openai_compat, tools  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.scope import Scope  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _png(width: int = 64, height: int = 64,
         rgb: tuple[int, int, int] = (220, 20, 20)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


#: A 1×1 transparent GIF. The extractor classifies by URL/host, so the bytes
#: only need to be a real image for ``view_image`` to read back.
_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Vision Fixture Page</title>
<meta name="description" content="A page for the vision test.">
</head><body>
<h1>Rendered heading</h1>
<p id="body">Visible body text that only JavaScript could have left here.</p>
<img src="/pixel.png" width="64" height="64" alt="a red pixel">
<img src="/anim.gif" width="32" height="32" alt="an animation">
<video src="/clip.mp4" poster="/pixel.png" width="320" height="180"></video>
<div class="comment"><span class="author">Ada</span>
<p>First comment body, long enough to be a comment.</p></div>
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body, ctype = _HTML, "text/html; charset=utf-8"
        elif self.path == "/pixel.png":
            body, ctype = _png(), "image/png"
        elif self.path == "/anim.gif":
            body, ctype = _GIF, "image/gif"
        else:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # keep the suite quiet
        pass


def _serve() -> tuple[http.server.ThreadingHTTPServer, str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _cfg(tmp: Path) -> Config:
    cfg = Config.from_env(workdir=tmp, base_url="https://logfare.ai/v1",
                          api_key="test")
    cfg.state_dir = tmp / "state"
    cfg.max_output_chars = 30_000
    return cfg


# ---------------------------------------------------------------------------
# 1 — the image layer
# ---------------------------------------------------------------------------

def test_images(tmp: Path) -> None:
    png = tmp / "dot.png"
    png.write_bytes(_png())
    check("image: a PNG is recognised", images.sniff(png.read_bytes()) == "image/png")
    check("image: its dimensions are read", images.dimensions(png.read_bytes()) == (64, 64))
    prepared = images.prepare(png)
    check("image: prepare returns the file", prepared is not None and prepared.path == png)
    block = images.placeholder(png)
    check("image: the placeholder is a path, not base64",
          block is not None and "_path" in block and "data" not in block)
    check("image: the placeholder knows its type",
          block is not None and block.get("media_type") == "image/png")

    messages = [{"role": "user", "content": [{"type": "text", "text": "look"},
                                             block]}]
    anthropic = images.materialise(messages)
    inner = anthropic[0]["content"][1]
    check("image: materialise builds a Messages image block",
          inner.get("type") == "image" and inner["source"]["type"] == "base64"
          and inner["source"]["media_type"] == "image/png")

    chat = openai_compat.translate_messages("sys", messages)
    check("image: Chat Completions gets a data URI",
          chat[-1]["role"] == "user"
          and chat[-1]["content"][-1]["type"] == "image_url"
          and chat[-1]["content"][-1]["image_url"]["url"].startswith(
              "data:image/png;base64,"))

    tool_messages = [{"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "t",
        "content": images.tool_content("shot", [png]),
    }]}]
    mat = images.materialise(tool_messages)
    check("image: a tool result carries the image",
          [b.get("type") for b in mat[0]["content"][0]["content"]]
          == ["text", "image"])
    oa = openai_compat.translate_messages("", tool_messages)
    check("image: OpenAI lifts it out of the tool role",
          [m["role"] for m in oa] == ["tool", "user"]
          and oa[-1]["content"][0]["type"] == "image_url")

    stripped, removed = images.strip_images(messages)
    check("image: a blind route drops it",
          removed == 1 and stripped[0]["content"][-1]["type"] == "text")

    # A tool result that was only an image must not become an empty result.
    bare = [{"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "t", "content": [block]}]}]
    stripped, removed = images.strip_images(bare)
    check("image: an image-only result leaves a note",
          removed == 1 and stripped[0]["content"][0]["content"][0]["type"] == "text")

    check("image: a provider's image rejection is recognised",
          images.image_error(Exception(
              "Invalid image: image dimensions must be at least 10px")))
    check("image: a model-name 404 is not an image rejection",
          not images.image_error(Exception("Model 'nope' not found")))

    history = [{"role": "user", "content": [{"type": "text", "text": str(i)},
                                            block]} for i in range(10)]
    trimmed, removed = images.trim_images(history, keep=3)
    kept = sum(1 for m in trimmed for b in m["content"]
               if isinstance(b, dict) and b.get("type") == "image")
    check("image: history keeps only the newest pictures",
          removed == 7 and kept == 3, f"{removed} removed, {kept} kept")


# ---------------------------------------------------------------------------
# 2 — the browser
# ---------------------------------------------------------------------------

def test_browser(tmp: Path, url: str) -> None:
    outdir = tmp / "capture"
    cap = browser.capture(url, outdir, scroll=0, wait_ms=100,
                          full_page=False, load_more=False)
    check("browser: the page renders", cap.ok, cap.error)
    check("browser: the title is read", cap.title == "Vision Fixture Page", cap.title)
    check("browser: the description is read",
          "vision test" in (cap.description or "").lower(), cap.description)
    check("browser: the rendered text is read",
          "Visible body text" in cap.text, cap.text[:120])
    check("browser: the heading is read", "Rendered heading" in cap.headings,
          str(cap.headings))
    check("browser: an image is found", any("pixel.png" in m.url for m in cap.images),
          str([m.url for m in cap.images]))
    check("browser: a GIF is classified", any("anim.gif" in m.url for m in cap.gifs),
          str([m.url for m in cap.gifs]))
    check("browser: a video is found", any("clip.mp4" in m.url for m in cap.videos),
          str([m.url for m in cap.videos]))
    check("browser: the comment is read",
          any("First comment" in c.text for c in cap.comments),
          str([c.text for c in cap.comments]))
    check("browser: a screenshot is taken",
          bool(cap.screenshots) and cap.screenshots[0].exists(),
          str(cap.screenshots))
    check("browser: the screenshot is attached", cap.screenshots[0] in cap.attachments)
    check("browser: the HTML evidence is saved",
          cap.html_path is not None and cap.html_path.exists())
    check("browser: the report names the page",
          "PAGE: Vision Fixture Page" in cap.report())


# ---------------------------------------------------------------------------
# 3 — the tools
# ---------------------------------------------------------------------------

def test_tools(tmp: Path, url: str) -> None:
    names = {s["name"] for s in tools.TOOL_SCHEMAS}
    check("tools: browse is advertised", "browse" in names)
    check("tools: view_image is advertised", "view_image" in names)
    check("tools: watch_video is advertised", "watch_video" in names)
    check("tools: browse is dispatched", "browse" in tools._DISPATCH)
    check("tools: view_image is dispatched", "view_image" in tools._DISPATCH)
    check("tools: watch_video is dispatched", "watch_video" in tools._DISPATCH)

    cfg = _cfg(tmp)
    got = tools.execute("browse", {"url": url, "scroll": 0, "wait_ms": 100,
                                   "full_page": False}, cfg)
    check("tools: browse renders", not got.is_error, got.output[:120])
    check("tools: browse returns images",
          bool(got.meta.get("images")), str(got.meta.get("images")))
    check("tools: browse names the page",
          "Vision Fixture Page" in got.output, got.output[:120])

    if got.meta.get("images"):
        shot = got.meta["images"][0]
        view = tools.execute("view_image", {"path": shot}, cfg)
        check("tools: view_image reads the state-dir evidence",
              not view.is_error and bool(view.meta.get("images")), view.output)
    else:
        check("tools: view_image reads the state-dir evidence", False,
              "no screenshot returned")

    # A denied host is refused before Chromium opens.
    deny = tmp / "out-of-scope.yaml"
    deny.write_text("domain:\n  - blocked.example\n", encoding="utf-8")
    cfg._scope = Scope.load(None, deny)
    blocked = tools.execute("browse", {"url": "https://blocked.example/"}, cfg)
    check("tools: the scope guard stops browse",
          blocked.is_error and blocked.meta.get("blocked"), blocked.output)
    check("tools: the refusal names the guard",
          "BLOCKED" in blocked.output and "blocked.example" in blocked.output)


# ---------------------------------------------------------------------------
# 4 — the command and UI wiring
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    check("wiring: /see is dispatched", '"see": lambda a:' in app)
    check("wiring: the handler exists", "def _cmd_see(" in app)
    check("wiring: it starts a turn with images", "image_paths=image_paths" in app)
    check("wiring: /see is documented", '"/see <url> [question]"' in app)
    check("wiring: /see is offered by completion", '("/see",' in comp)
    check("wiring: run_turn accepts images",
          "image_paths: list[str] | None = None" in
          (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8"))


def main() -> int:
    server, url = _serve()
    try:
        with tempfile.TemporaryDirectory(prefix="zim-vision-") as td:
            tmp = Path(td)
            test_images(tmp)
            test_browser(tmp, url)
            test_tools(tmp, url)
            test_wiring()
    finally:
        server.shutdown()
        server.server_close()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
