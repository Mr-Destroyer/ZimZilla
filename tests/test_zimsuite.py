"""Regression suite for ZimSuite — proxy history, screen and suite tools.

Run:  python tests/test_zimsuite.py
"""

from __future__ import annotations

import asyncio
import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-suite-test-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)

from zimzilla import suite as sm  # noqa: E402
from zimzilla import tools  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.suiteui import SuiteScreen  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = (b"<html><a href='/next'>next</a><a href='/two'>two</a></html>"
                if self.path == "/" else b"ok")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _server():
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _cfg(tmp: Path) -> Config:
    cfg = Config.from_env(workdir=tmp, base_url="https://logfare.ai/v1",
                          api_key="test")
    cfg.state_dir = tmp / "state"
    return cfg


def test_backend(tmp: Path) -> None:
    srv, url = _server()
    cfg = _cfg(tmp)
    flows = []
    suite = sm.Suite(url, cfg, on_flow=lambda f: flows.append(f))
    try:
        check("suite: proxy starts", suite.start(), suite.error)
        check("suite: it exposes a local proxy", suite.proxy_url.startswith("http://127.0.0.1:"))
        got = suite.request("GET", url + "/", source="repeater")
        check("suite: a request is captured", got.status == 200 and got.method == "GET")
        check("suite: history receives the flow", len(flows) >= 1)
        crawled = suite.crawl(url + "/")
        check("suite: crawler follows links", len(crawled) >= 2, str(len(crawled)))
        # A tiny campaign file proves URLs are imported into history without
        # any proxy traffic.
        campaign = Path(cfg.state_dir) / "hunts" / "example.com"
        campaign.mkdir(parents=True, exist_ok=True)
        (campaign / "findings.md").write_text(
            "Endpoint /login\nAlso https://example.com/next\n")
        seed_suite = sm.Suite("https://example.com", cfg)
        seeded = seed_suite.seed_hunt_flows()
        check("suite: hunt URLs seed history", len(seeded) >= 1, str(len(seeded)))
        check("suite: seeded URLs are absolute",
              all(f.url.startswith(("http://", "https://")) for f in seeded))
        check("suite: hunt extraction finds the path",
              "https://example.com/login" in {f.url for f in seeded})
        check("suite: a flow has request text", "GET /" in got.request_text())
        check("suite: a flow has response text", "HTTP 200" in got.response_text())
        suite.set_intercept(True)
        check("suite: intercept can be armed", suite.intercept_file.exists())
        suite.set_intercept(False)
        check("suite: intercept can be disarmed", not suite.intercept_file.exists())
    finally:
        suite.stop()
        srv.shutdown()


def test_tools(tmp: Path) -> None:
    srv, url = _server()
    cfg = _cfg(tmp)
    suite = sm.Suite(url, cfg)
    try:
        cfg._suite = suite
        got = suite.request("GET", url + "/")
        history = tools.execute("suite_history", {"limit": 10}, cfg)
        check("tools: history lists the flow", not history.is_error and got.id in history.output,
              history.output[:200])
        full = tools.execute("suite_flow", {"id": got.id}, cfg)
        check("tools: flow returns request and response",
              not full.is_error and "RESPONSE" in full.output)
        rep = tools.execute("suite_repeater", {"url": url + "/"}, cfg)
        check("tools: repeater works", not rep.is_error and "200" in rep.output,
              rep.output[:200])
        cfg._suite = None
        unavailable = tools.execute("suite_history", {}, cfg)
        check("tools: outside the suite they fail honestly",
              unavailable.is_error and "not open" in unavailable.output)
    finally:
        cfg._suite = None
        suite.stop()
        srv.shutdown()


async def test_screen(tmp: Path) -> None:
    cfg = _cfg(tmp)
    from zimzilla.ui.app import ZimZillaApp
    app = ZimZillaApp(cfg)
    # The screen is tested without a child mitmdump; the backend is covered
    # above. This keeps the UI regression fast and deterministic.
    with mock.patch.object(sm.Suite, "start", return_value=True), \
         mock.patch.object(sm.Suite, "stop", return_value=None):
        async with app.run_test(size=(140, 40)) as pilot:
            app.pop_screen()
            await pilot.pause()
            app._handle_command("/zimsuite http://example.com")
            await asyncio.sleep(0.5)
            check("screen: /zimsuite opens the workbench",
                  isinstance(app.screen, SuiteScreen))
            check("screen: the target is carried",
                  "example.com" in app.screen.target, app.screen.target)
            check("screen: history table exists",
                  len(app.screen.query_one("#flow-table").columns) == 6)
            check("screen: the mini harness is on the right",
                  app.screen.query_one("#suite-side") is not None)
            await pilot.press("escape")
            await asyncio.sleep(0.3)
            check("screen: Escape closes it", not isinstance(app.screen, SuiteScreen))


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    check("wiring: /zimsuite is dispatched", '"zimsuite": lambda a:' in app)
    check("wiring: /zimsuite is completed", '("/zimsuite",' in comp)
    check("wiring: suite tools are hidden outside", "SUITE_TOOLS" in
          (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8"))
    check("wiring: suite tools are registered", "suite_history" in
          (ROOT / "zimzilla" / "tools.py").read_text(encoding="utf-8"))
    check("wiring: Ctrl+S closes the suite", 'Binding("ctrl+s"' in
          (ROOT / "zimzilla" / "ui" / "suiteui.py").read_text(encoding="utf-8"))
    check("wiring: hunt URLs seed on mount", "seed_hunt_flows()" in
          (ROOT / "zimzilla" / "ui" / "suiteui.py").read_text(encoding="utf-8"))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zim-suite-") as td:
        base = Path(td)
        test_backend(base / "backend")
        test_tools(base / "tools")
        asyncio.run(test_screen(base / "screen"))
    test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
