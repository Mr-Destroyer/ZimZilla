"""ZimSuite — the graphical workbench for the intercepting proxy.

One screen: request history and a request/response viewer on the left, a live
agent harness on the right, and tabs for Repeater, Intruder, Intercept, Crawler
and existing hunt reports. The backend (``zimzilla.suite``) owns mitmproxy; this
module only renders flows and sends instructions to a dedicated agent.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Input, RichLog, Static,
                             TabbedContent, TabPane, TextArea)

from .. import suite as suite_mod
from ..agent import Agent


class SuiteScreen(ModalScreen):
    """Full-screen ZimSuite. Esc closes it and stops the proxy."""

    BINDINGS = [
        Binding("escape", "close_suite", "close", priority=True, show=False),
        Binding("ctrl+c", "stop_agent", "stop agent", priority=True,
                show=False),
    ]

    DEFAULT_CSS = """
    SuiteScreen {
        background: $background;
    }
    #suite-root { width: 100%; height: 100%; }
    #suite-header {
        height: 2; padding: 0 1; border-bottom: heavy $secondary;
    }
    #suite-body { height: 1fr; }
    #suite-main { width: 3fr; height: 100%; }
    #suite-side {
        width: 42; height: 100%; border-left: heavy $secondary;
    }
    #suite-side-title { height: 1; padding: 0 1; }
    #suite-chat { height: 1fr; background: transparent; }
    #suite-input-row { height: 3; }
    #suite-input { width: 1fr; }
    #suite-side-buttons { height: 1; }
    #suite-side-buttons Button { min-width: 8; height: 1; }
    #suite-tabs { height: 1fr; }
    #flow-table { height: 1fr; }
    #flow-detail { height: 14; border-top: heavy $secondary; }
    #repeater-request { height: 12; }
    #repeater-response { height: 1fr; }
    #intruder-request { height: 8; }
    #intruder-response { height: 1fr; }
    #crawler-log { height: 1fr; }
    #intercept-log { height: 1fr; }
    #findings-log { height: 1fr; }
    TextArea { background: $surface; color: $foreground; }
    """

    def __init__(self, target: str, cfg, palette) -> None:
        super().__init__()
        self.target = target
        self.cfg = cfg
        self.palette = palette
        self.suite = suite_mod.Suite(
            target, cfg,
            on_flow=self._thread_flow,
            on_pending=self._thread_pending,
            on_log=self._thread_log,
        )
        self._flows: dict[str, suite_mod.Flow] = {}
        self._selected: suite_mod.Flow | None = None
        self._agent: Agent | None = None
        self._agent_task: asyncio.Task | None = None
        self._closed = False
        self._proxy_ready = False
        self._started_at = 0.0

    # ---- compose ----------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="suite-root"):
            yield Static(self._header_text("starting proxy…"), id="suite-header")
            with Horizontal(id="suite-body"):
                with Vertical(id="suite-main"):
                    with TabbedContent(initial="tab-history", id="suite-tabs"):
                        with TabPane("HISTORY", id="tab-history"):
                            yield DataTable(id="flow-table")
                            yield RichLog(id="flow-detail", markup=False, wrap=True)
                        with TabPane("REPEATER", id="tab-repeater"):
                            yield TextArea(
                                "GET / HTTP/1.1\nHost: example.com\n\n",
                                id="repeater-request")
                            with Horizontal():
                                yield Button("SEND", id="repeater-send")
                                yield Button("LOAD FLOW", id="repeater-load")
                            yield RichLog(id="repeater-response", markup=False,
                                          wrap=True)
                        with TabPane("INTRUDER", id="tab-intruder"):
                            yield Input(placeholder="URL with FUZZ marker",
                                        id="intruder-url")
                            yield Input(placeholder="payloads: one per line or comma-separated",
                                        id="intruder-payloads")
                            yield TextArea("GET / HTTP/1.1\nHost: \n\n",
                                           id="intruder-request")
                            with Horizontal():
                                yield Button("RUN", id="intruder-run")
                            yield RichLog(id="intruder-response", markup=False,
                                          wrap=True)
                        with TabPane("INTERCEPT", id="tab-intercept"):
                            with Horizontal():
                                yield Button("INTERCEPT OFF",
                                             id="intercept-toggle")
                                yield Button("FORWARD", id="intercept-forward")
                                yield Button("DROP", id="intercept-drop")
                            yield RichLog(id="intercept-log", markup=False,
                                          wrap=True)
                        with TabPane("CRAWLER", id="tab-crawler"):
                            yield Input(placeholder="start URL",
                                        id="crawler-url")
                            with Horizontal():
                                yield Button("CRAWL", id="crawler-run")
                            yield RichLog(id="crawler-log", markup=False,
                                          wrap=True)
                        with TabPane("FINDINGS", id="tab-findings"):
                            yield RichLog(id="findings-log", markup=False,
                                          wrap=True)
                with Vertical(id="suite-side"):
                    yield Static("  ⟩ MINI HARNESS", id="suite-side-title")
                    yield RichLog(id="suite-chat", markup=False, wrap=True)
                    with Horizontal(id="suite-input-row"):
                        yield Input(placeholder="guide the agent…",
                                    id="suite-input")
                    with Horizontal(id="suite-side-buttons"):
                        yield Button("STOP", id="suite-stop")
                        yield Button("CLOSE", id="suite-close")

    def on_mount(self) -> None:
        table = self.query_one("#flow-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("METHOD", "PATH", "STATUS", "TYPE", "SIZE")
        self.query_one("#crawler-url", Input).value = self.target
        self.cfg._suite = self.suite
        self._agent = Agent(self.cfg, permission_handler=self._allow)
        self.suite.start()
        self._proxy_ready = not self.suite.error
        self.query_one("#suite-header", Static).update(
            self._header_text("proxy ready" if self._proxy_ready
                              else self.suite.error))
        self.query_one("#suite-chat", RichLog).write(
            "  suite open — ask me to inspect, replay or crawl.\n")
        self._load_findings()
        self.query_one("#suite-input", Input).focus()

    def _header_text(self, state: str) -> Text:
        p = self.palette
        t = Text()
        t.append("  ZIMSUITE  ", style=f"bold {p.bg} on {p.accent}")
        t.append("  target ", style=p.dim)
        t.append(self.target, style=f"bold {p.primary}")
        t.append("   proxy ", style=p.dim)
        t.append(self.suite.proxy_url if self._proxy_ready else "—",
                 style=p.primary)
        t.append("   ", style=p.dim)
        t.append(state[:90], style=p.amber if "error" in state.lower()
                 else p.dim)
        return t

    async def _allow(self, name, args, preview):
        return "yes"

    # ---- proxy wiring -----------------------------------------------------
    def _thread_flow(self, flow) -> None:
        if self._closed:
            return
        try:
            self.app.call_from_thread(self._add_flow, flow)
        except Exception:  # noqa: BLE001
            pass

    def _thread_pending(self, pending) -> None:
        if self._closed:
            return
        try:
            self.app.call_from_thread(self._paint_pending, pending)
        except Exception:  # noqa: BLE001
            pass

    def _thread_log(self, text: str) -> None:
        if self._closed:
            return
        try:
            self.app.call_from_thread(self._chat_line, "  " + text)
        except Exception:  # noqa: BLE001
            pass

    def _add_flow(self, flow) -> None:
        if self._closed:
            return
        self._flows[flow.id] = flow
        table = self.query_one("#flow-table", DataTable)
        table.add_row(
            flow.method, flow.path, str(flow.status or "—"),
            flow.mime.split(";")[0][:24], str(flow.length),
            key=flow.id)
        if not self._selected:
            self._select_flow(flow)

    def _select_flow(self, flow) -> None:
        self._selected = flow
        log = self.query_one("#flow-detail", RichLog)
        log.clear()
        log.write(flow.request_text())
        log.write("\n" + "─" * 40 + "\n")
        log.write(flow.response_text())
        try:
            self.query_one("#repeater-request", TextArea).load_text(
                flow.request_text())
        except Exception:  # noqa: BLE001
            pass

    def _paint_pending(self, pending) -> None:
        log = self.query_one("#intercept-log", RichLog)
        log.clear()
        for flow in pending:
            log.write(f"  {flow.method} {flow.url}\n")
        if pending:
            log.write("  waiting: FORWARD or DROP the first request\n")

    # ---- tabs -------------------------------------------------------------
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        flow = self._flows.get(str(event.row_key.value))
        if flow:
            self._select_flow(flow)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "repeater-load":
            if self._selected:
                self.query_one("#repeater-request", TextArea).load_text(
                    self._selected.request_text())
        elif bid == "repeater-send":
            self._send_repeater()
        elif bid == "intruder-run":
            self._run_intruder()
        elif bid == "crawler-run":
            self._run_crawler()
        elif bid == "intercept-toggle":
            self._toggle_intercept()
        elif bid == "intercept-forward":
            self._decide(True)
        elif bid == "intercept-drop":
            self._decide(False)
        elif bid == "suite-stop":
            self._stop_agent()
        elif bid == "suite-close":
            self.action_close_suite()

    def action_close_suite(self) -> None:
        self._stop_agent()
        try:
            self.cfg._suite = None  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        self.suite.stop()
        # Dismiss from inside the screen's own action. `await pop_screen()`
        # deadlocks here: it waits for this screen to finish while this screen
        # is the thing handling the action.
        self.dismiss(None)

    def action_stop_agent(self) -> None:
        self._stop_agent()

    def _toggle_intercept(self) -> None:
        on = not self.suite.intercept_file.exists()
        self.suite.set_intercept(on)
        self.query_one("#intercept-toggle", Button).label = (
            "INTERCEPT ON" if on else "INTERCEPT OFF")
        self._chat_line("  intercept " + ("on" if on else "off"))

    def _decide(self, forward: bool) -> None:
        pending = list(self.suite.pending.values())
        if not pending:
            self.query_one("#intercept-log", RichLog).write("  no pending request\n")
            return
        for flow in pending:
            self.suite.decide(flow.id, "forward" if forward else "drop")
        self.query_one("#intercept-log", RichLog).write(
            "  " + ("forwarded" if forward else "dropped") + "\n")

    # ---- requests ---------------------------------------------------------
    def _parse_raw(self, raw: str, fallback_url: str) -> tuple[str, str, dict, str]:
        lines = raw.splitlines()
        first = lines[0].strip() if lines else "GET / HTTP/1.1"
        parts = first.split()
        method = parts[0].upper() if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"
        base = urllib.parse.urlsplit(fallback_url)
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = urllib.parse.urlunsplit(
                (base.scheme or "http", base.netloc, path.split("?")[0],
                 path.split("?")[1] if "?" in path else "", ""))
        headers: dict[str, str] = {}
        body_start = len(lines)
        for i, line in enumerate(lines[1:], 1):
            if not line.strip():
                body_start = i + 1
                break
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip()] = v.strip()
        body = "\n".join(lines[body_start:])
        return method, url, headers, body

    def _send_repeater(self) -> None:
        raw = self.query_one("#repeater-request", TextArea).text
        fallback = self._selected.url if self._selected else self.target
        try:
            method, url, headers, body = self._parse_raw(raw, fallback)
            flow = self.suite.replay(method, url, headers, body)
        except Exception as e:  # noqa: BLE001
            self.query_one("#repeater-response", RichLog).write(
                f"  error: {type(e).__name__}: {e}\n")
            return
        self.query_one("#repeater-response", RichLog).write(
            f"  {flow.status} {flow.url}\n\n{flow.response_text()}\n")
        self._add_flow(flow)

    def _run_intruder(self) -> None:
        url = self.query_one("#intruder-url", Input).value.strip()
        raw = self.query_one("#intruder-request", TextArea).text
        payload_text = self.query_one("#intruder-payloads", Input).value
        payloads = [p.strip() for p in payload_text.replace(",", "\n").splitlines()
                    if p.strip()]
        if not url or not payloads:
            self.query_one("#intruder-response", RichLog).write(
                "  needs a URL with FUZZ and at least one payload\n")
            return
        self.query_one("#intruder-response", RichLog).write(
            f"  running {len(payloads)} payload(s)…\n")

        @work(thread=True)
        def run():
            try:
                method, _, headers, body = self._parse_raw(raw, url)
                results = self.suite.intruder(url, payloads, method=method,
                                              headers=headers, body=body)
                def paint():
                    log = self.query_one("#intruder-response", RichLog)
                    for f in results:
                        log.write(f"  {f.status}  {f.url}\n")
                self.app.call_from_thread(paint)
            except Exception as e:  # noqa: BLE001
                self.app.call_from_thread(
                    lambda: self.query_one("#intruder-response", RichLog).write(
                        f"  error: {e}\n"))
        run()

    def _run_crawler(self) -> None:
        url = self.query_one("#crawler-url", Input).value.strip() or self.target
        self.query_one("#crawler-log", RichLog).write(f"  crawling {url}…\n")

        @work(thread=True)
        def run():
            found = self.suite.crawl(url)
            def paint():
                log = self.query_one("#crawler-log", RichLog)
                for f in found:
                    log.write(f"  {f.status}  {f.url}\n")
                log.write(f"  {len(found)} URL(s)\n")
            self.app.call_from_thread(paint)
        run()

    # ---- findings ---------------------------------------------------------
    def _load_findings(self) -> None:
        log = self.query_one("#findings-log", RichLog)
        files = self.suite.hunt_files()
        if not files:
            log.write("  no bug-hunt campaign for this target yet\n")
            return
        log.write(f"  {len(files)} hunt file(s)\n")
        for path in files:
            log.write(f"\n  {path.name}\n")
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            log.write(text[:6000])

    # ---- mini agent -------------------------------------------------------
    def _chat_line(self, text: str) -> None:
        try:
            self.query_one("#suite-chat", RichLog).write(text)
        except Exception:  # noqa: BLE001
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "suite-input":
            return
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        self._chat_line(f"\n  you: {text}\n")
        self._agent_task = asyncio.create_task(self._agent_turn(text))

    async def _agent_turn(self, text: str) -> None:
        if self._agent is None:
            return
        self._chat_line("  agent: working…\n")
        prompt = (
            f"ZimSuite target: {self.target}. You are the live suite operator. "
            f"Use suite_history, suite_flow, suite_repeater, suite_intruder and "
            f"suite_crawl to inspect and act. Do not invent a request that is "
            f"not in the history; if you need one, use the selected flow. "
            f"Operator instruction: {text}"
        )
        try:
            async for ev in self._agent.run_turn(prompt):
                kind = ev.get("type")
                if kind == "text_delta":
                    self._chat_line(ev.get("text", ""))
                elif kind == "tool_call":
                    self._chat_line(
                        f"\n  ▸ {ev.get('name')} {json.dumps(ev.get('args') or {})[:220]}\n")
                elif kind == "tool_result":
                    out = str(ev.get("output") or "")
                    self._chat_line(f"  {'✗' if ev.get('is_error') else '✓'} "
                                    f"{out[:500]}\n")
                elif kind == "error":
                    self._chat_line(f"  error: {ev.get('message')}\n")
        except asyncio.CancelledError:
            self._chat_line("  agent stopped\n")
            raise
        except Exception as e:  # noqa: BLE001
            self._chat_line(f"  agent failed: {type(e).__name__}: {e}\n")

    def _stop_agent(self) -> None:
        task = self._agent_task
        if task is not None and not task.done():
            task.cancel()
        try:
            if self._agent is not None:
                self._agent.cancel_turn()
        except Exception:  # noqa: BLE001
            pass

    def on_unmount(self) -> None:
        self._closed = True
        try:
            self.suite.stop()
        except Exception:  # noqa: BLE001
            pass
