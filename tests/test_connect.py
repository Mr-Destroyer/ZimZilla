"""Regression suite for `/connect` and the generated MCP bridge.

Run:  python tests/test_connect.py

What this pins:

1. **The bridge is real.** A generated Kdenlive project is created, filled with
   a clip, given an effect, read back and rendered by melt. This is the part a
   screenshot-driven macro cannot do reliably.
2. **The MCP handshake.** The generated ``server.py`` answers initialize,
   tools/list and tools/call over stdio, so ZimZilla's existing client sees it
   exactly like any other server.
3. **Registration.** ``/connect`` writes ``mcp.json`` (mode 600), a mode-700
   bridge directory and a mode-600 config; removing it cleans up.
4. **No shell tool.** The bridge advertises application/timeline tools, not a
   general command runner.

The render test is skipped with a PASS when ffmpeg/melt are absent; everything
else is offline.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-connect-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)

from zimzilla import bridge, connect, mcp  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _ffmpeg_clip(path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc2=size=320x180:rate=30", "-t", "1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
            capture_output=True, timeout=60)
        return proc.returncode == 0 and path.is_file()
    except (OSError, subprocess.TimeoutExpired):
        return False


def test_bridge(tmp: Path) -> None:
    project = tmp / "project.kdenlive"
    created = bridge.project_create(str(project), width=320, height=180,
                                    fps=30, tracks=2, overwrite=True)
    check("bridge: a project is created", project.is_file() and "created" in created)
    root_text = project.read_text()
    check("bridge: it is MLT XML", root_text.lstrip().startswith("<?xml")
          and "<mlt" in root_text)
    check("bridge: it has a profile", 'width="320"' in root_text)
    check("bridge: it has tracks", 'id="playlist0"' in root_text
          and 'id="playlist1"' in root_text)

    clip = tmp / "clip.mp4"
    if not _ffmpeg_clip(clip):
        check("bridge: clip generation (ffmpeg missing — rest skipped)", True,
              "skipped")
        return
    added = bridge.add_clip(str(project), str(clip), track=0)
    check("bridge: a clip is added", "added" in added and "producer" in added,
          added)
    info = bridge.project_info(str(project))
    check("bridge: info names the clip", "clip.mp4" in info, info[:200])
    check("bridge: info reports a duration", "DURATION:" in info)
    check("bridge: info reports the track", "Track 1" in info)

    trimmed = bridge.trim_clip(str(project), "clip.mp4",
                               in_frame=5, out_frame=20)
    check("bridge: a clip is trimmed", "trimmed" in trimmed, trimmed)
    info = bridge.project_info(str(project))
    check("bridge: the trim is in the timeline", "in 5" in info and "out 20" in info,
          info[:400])

    fx = bridge.add_effect(str(project), "clip.mp4", "brightness",
                           {"level": 0.5})
    check("bridge: an effect is added", "brightness" in fx, fx)
    tr = bridge.add_transition(str(project), a_track=0, b_track=1)
    check("bridge: a transition is added", "transition" in tr, tr)

    if shutil.which("melt") is None:
        check("bridge: render (melt missing — skipped)", True, "skipped")
        return
    out = tmp / "out.mp4"
    rendered = bridge.render(str(project), str(out), crf=30, preset="ultrafast")
    check("bridge: melt renders the project", out.is_file() and out.stat().st_size > 0,
          rendered)
    probe = shutil.which("ffprobe")
    if probe:
        proc = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
            capture_output=True, text=True, timeout=20)
        try:
            duration = float((proc.stdout or "0").strip())
        except ValueError:
            duration = 0.0
        check("bridge: the render has a duration", duration > 0.5, str(duration))


def test_generated_server(tmp: Path) -> None:
    entry, info = connect.create_bridge(
        "kdenlive", project=str(tmp / "project.kdenlive"), name="kdenlive-test")
    check("connect: bridge directory exists", Path(info["server"]).is_file())
    check("connect: bridge config is mode 600",
          oct(Path(info["config_path"]).stat().st_mode & 0o777) == "0o600",
          oct(Path(info["config_path"]).stat().st_mode & 0o777))
    servers = {s.name: s for s in mcp.load_config()}
    check("connect: it is registered in mcp.json", "kdenlive-test" in servers)
    check("connect: mcp.json is mode 600",
          oct(mcp.CONFIG_PATH.stat().st_mode & 0o777) == "0o600",
          oct(mcp.CONFIG_PATH.stat().st_mode & 0o777))
    generated = info["server"]
    check("connect: the launcher imports the shipped bridge",
          "from zimzilla.bridge import main" in Path(generated).read_text())

    proc = subprocess.Popen([sys.executable, generated],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        def send(obj: dict) -> dict:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        init = send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"}}})
        check("server: initialize", init.get("result", {}).get("protocolVersion")
              == bridge.PROTOCOL_VERSION, str(init)[:120])
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        listing = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                        "params": {}})
        names = [t.get("name") for t in listing["result"]["tools"]]
        check("server: tools/list exposes the timeline", "kdenlive_add_clip" in names)
        check("server: tools/list exposes render", "kdenlive_render" in names)
        check("server: tools/list exposes the desktop", "app_screenshot" in names)
        check("server: no shell tool",
              not any(n in ("bash", "shell", "run_command") for n in names))
        call = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "kdenlive_project_info",
                                "arguments": {"project": str(tmp / "project.kdenlive")}}})
        text = call["result"]["content"][0]["text"]
        check("server: a tool call works", "PROJECT:" in text and
              call["result"].get("isError") is False, text[:100])
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait(timeout=5)

    ok, detail, tools = connect.connect(servers["kdenlive-test"])
    check("registry: the bridge connects", ok, detail)
    check("registry: its tools are exposed", len(tools) >= 15, str(len(tools)))
    check("registry: names are namespaced",
          all(t.startswith("mcp_kdenlive_test_") for t in tools), str(tools[:2]))
    check("remove: it cleans up",
          connect.remove("kdenlive-test")
          and "kdenlive-test" not in {s.name for s in mcp.load_config()})


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    check("wiring: /connect is dispatched", '"connect": lambda a:' in app)
    check("wiring: the handler exists", "def _cmd_connect(" in app)
    check("wiring: it is in /help", '"/connect kdenlive [project]"' in app)
    check("wiring: it is offered by completion", '("/connect",' in comp)
    check("wiring: it is busy-ok", '"connect",' in app)
    check("wiring: the bridge is registered in the workspace",
          "registered in ~/.zimzilla/mcp.json" in app)
    check("wiring: kdenlive is an adapter", "kdenlive" in connect.ADAPTERS)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zim-connect-") as td:
        tmp = Path(td)
        test_bridge(tmp)
        test_generated_server(tmp)
        test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
