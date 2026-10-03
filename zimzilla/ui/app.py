"""The main Textual application."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .. import session as session_mod
from ..agent import Agent
from ..config import KNOWN_MODELS, MODES, Config, price_for
from ..theme import THINKING_VERBS, get_palette
from . import renderers as R
from .boot import BootScreen
from .complete import CompletionPopup
from .widgets import ActivityPane, ChatPane, HeaderBar, StatusBar


class PermissionModal(ModalScreen[str]):
    """Confirmation panel for gated tools. Dismisses with yes/no/always."""

    BINDINGS = [
        Binding("y", "choose('yes')", "yes"),
        Binding("n", "choose('no')", "no"),
        Binding("a", "choose('always')", "always"),
        Binding("escape", "choose('no')", "no"),
        Binding("enter", "choose('yes')", "yes"),
    ]

    def __init__(self, name: str, preview, palette, remaining: int = 0) -> None:
        super().__init__()
        self.tool_name = name
        self.preview = preview
        self.palette = palette
        self.remaining = remaining

    def compose(self) -> ComposeResult:
        with Vertical(id="perm-box"):
            yield Static(id="perm-title")
            with VerticalScroll(id="perm-body"):
                yield Static(id="perm-content")
            yield Static(id="perm-keys")
            yield Static(id="perm-progress")

    def on_mount(self) -> None:
        p = self.palette
        # Trap focus inside the modal so stray keys never reach the prompt.
        box = self.query_one("#perm-box")
        box.can_focus = True
        box.focus()
        title = Text()
        title.append("⚠ PERMISSION REQUIRED  ", style=f"bold black on {p.amber}")
        title.append("  " + self.preview.title, style=f"bold {p.primary}")
        self.query_one("#perm-title", Static).update(title)

        if self.preview.kind == "diff":
            verb = self.preview.title.split(" ", 1)[0]
            content = R.diff_panel(
                self.preview.title.split(" ", 1)[-1], self.preview.body, p, verb
            )
        else:
            content = R.render_output_text(self.preview.body, p)
        self.query_one("#perm-content", Static).update(content)

        keys = Text()
        keys.append("[y]", style=f"bold {p.accent}")
        keys.append(" execute   ", style=p.dim)
        keys.append("[n]", style=f"bold {p.red}")
        keys.append(" deny      ", style=p.dim)
        keys.append("[a]", style=f"bold {p.primary}")
        keys.append(" always this session", style=p.dim)
        self.query_one("#perm-keys", Static).update(keys)

        if self.remaining:
            self.query_one("#perm-progress", Static).update(
                Text(f"  ({self.remaining} more tool call(s) queued this turn)", style=p.dim)
            )

    def action_choose(self, value: str) -> None:
        self.dismiss(value)


class ZimZillaApp(App):
    """Top-level application: boot screen, then the shell."""

    CSS = """
    Screen {
        background: $background;
        layers: rain main;
    }
    #rain {
        layer: rain;
        width: 100%;
        height: 100%;
    }
    #main {
        layer: main;
        width: 100%;
        height: 1fr;
    }
    #bottom-dock {
        dock: bottom;
        height: auto;
        layer: main;
    }
    HeaderBar, StatusBar, Input { layer: main; }

    /* Colours come from theme variables, so /theme re-skins these too. */
    #input {
        height: 3;
        border: round $primary;
        background: $background;
        color: $primary;
        margin: 0;
        padding: 0 1;
    }
    Input:focus {
        border: round $accent;
        /* Textual tints the focused input with $foreground 5%, which renders
           as a shade outside the palette. Keep it pure black. */
        background-tint: transparent;
    }
    Input > .input--placeholder, Input > .input--suggestion {
        color: $secondary;
        background: $background;
    }
    Input > .input--cursor {
        color: $background;
        background: $accent;
        text-style: bold;
    }
    Input > .input--selection {
        background: $secondary;
        color: $primary;
    }
    PermissionModal {
        align: center middle;
        background: rgba(0,0,0,0.75);
    }
    #perm-box {
        width: 84%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: heavy $warning;
        background: $background;
        padding: 1 2;
    }
    #perm-body {
        height: auto;
        max-height: 24;
    }
    #perm-title { height: 1; }
    #perm-content { height: auto; }
    #perm-keys { height: 1; margin-top: 1; }
    #perm-progress { height: 1; color: #0b6e2a; }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "interrupt", priority=True),
        Binding("ctrl+d", "quit", "quit", priority=True),
        Binding("ctrl+l", "clear", "clear", priority=True),
    ]

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.palette = get_palette(cfg.theme)
        self.theme_name = cfg.theme
        self.rain_on = cfg.rain
        self.agent = Agent(cfg, permission_handler=self.request_permission)
        # While a modal (permission gate) is up, the prompt must be disabled so
        # approval keystrokes like "y" don't leak into it.
        self._modal_depth = 0
        self.busy = False
        self._cancelled = False
        self._stream_buf = ""

    # ---- compose ----------------------------------------------------------
    def compose(self) -> ComposeResult:
        # The matrix rain is painted *inside* the transcript and activity panes
        # (see RainRichLog) — Textual's compositor does not blend widgets across
        # layers, so a widget on a lower layer would be hidden by the panes.
        yield HeaderBar(self.palette, self.cfg.model, str(self.cfg.workdir),
                        self.cfg.unsafe, self.theme_name, mode=self.cfg.mode)
        with Horizontal(id="main"):
            yield ChatPane(self.palette, rain=self.rain_on)
            yield ActivityPane(self.palette, self.cfg.model, rain=self.rain_on)
        with Vertical(id="bottom-dock"):
            yield CompletionPopup(self.palette, id="complete")
            yield StatusBar(self.palette)
            yield Input(placeholder="❯ message ZimZilla…   ( / for commands, @ for files )",
                        id="input")

    def on_mount(self) -> None:
        self.title = "ZIMZILLA"
        try:
            theme = self.palette.rich_theme()
            self.register_theme(theme)
            self.theme = theme.name
        except Exception:
            pass

        self.push_screen(
            BootScreen(self.palette, self._boot_checks(), self._boot_done, rain=self.cfg.boot_rain)
        )

    def _boot_checks(self) -> list[tuple[str, str, bool]]:
        cfg = self.cfg
        checks: list[tuple[str, str, bool]] = [
            ("API KEY", "present" if cfg.api_key_present else "MISSING", cfg.api_key_present),
            ("ENDPOINT", cfg.redacted_endpoint(), True),
            ("MODEL", cfg.model, True),
            ("CWD", str(cfg.workdir), True),
        ]
        if self.agent.scope.loaded:
            checks.append(("SCOPE", self.agent.scope.describe(), not self.agent.scope.empty))
        else:
            checks.append(("SCOPE", "not loaded — guard off", True))
        checks.append(("SANDBOX", "DISABLED (--unsafe)" if cfg.unsafe else "enabled", not cfg.unsafe))
        checks.append(("TOOLS", "7 registered (bash, io, search)", True))
        return checks

    def _boot_done(self, typed: list[str] | None = None, submit: bool = False) -> None:
        self.pop_screen()
        # Apply the palette now, not just on /theme — otherwise the input
        # border and scrollbars keep the default green under --theme amber/cyan.
        self._apply_palette()
        # Seed the status bar so it reflects the configured model from the start.
        bar = self.query_one(StatusBar)
        bar.model = self.cfg.model
        bar.render_bar()
        self.query_one(HeaderBar).set_scope(
            self.agent.scope.describe(),
            (not self.agent.scope.empty) if self.agent.scope.loaded else True,
        )
        self._splash()
        inp = self.query_one("#input", Input)
        # Replay anything the user typed while the boot animation was running.
        text = "".join(typed or [])
        inp.focus()
        self.set_interval(1.0, self._tick_status)
        self.set_interval(0.55, self._flash_cursor)
        if text and submit:
            # Enter arrived during boot: deliver the whole buffered line now.
            self.call_after_refresh(self._submit_text, text)
        elif text:
            inp.value = text
            inp.cursor_position = len(inp.value)

    def _submit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.startswith("/"):
            self._handle_command(text)
        else:
            self._run_turn(self._expand_at_refs(text))

    def _expand_at_refs(self, text: str) -> str:
        """Turn ``@path`` mentions into an attached-files block for the model.

        The visible prompt keeps the short form; the model receives the file
        contents inline so a mention is immediately actionable.
        """
        import re

        from ..sandbox import SandboxError, resolve_in_jail

        tokens = re.findall(r"(?<!\w)@([^\s@]+)", text)
        if not tokens:
            return text
        attached: list[str] = []
        for tok in dict.fromkeys(tokens):  # de-dupe, keep order
            raw = tok.rstrip(".,;:)")
            try:
                path = resolve_in_jail(self.cfg.workdir, raw, self.cfg.unsafe, must_exist=True)
            except SandboxError:
                continue
            if path.is_dir():
                try:
                    listing = "\n".join(
                        sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
                    )
                except Exception:
                    listing = "(unreadable)"
                attached.append(f"--- {raw}/ (directory) ---\n{listing}")
                continue
            try:
                body = path.read_text(errors="replace")
            except Exception:
                continue
            limit = self.cfg.max_output_chars
            if len(body) > limit:
                body = body[:limit] + f"\n... [truncated at {limit} chars] ..."
            attached.append(f"--- {raw} ---\n{body}")
        if not attached:
            return text
        return text + "\n\n[referenced files]\n" + "\n\n".join(attached)

    def _splash(self) -> None:
        p = self.palette
        from .banner import banner_lines, banner_subtitle

        t = Text()
        for line in banner_lines():
            t.append(line + "\n", style=f"bold {p.primary}")
        self.query_one(ChatPane).write_block(t)

        sub = Text()
        sub.append(f"       {banner_subtitle()}\n", style=p.accent)
        sub.append("       ", style=p.dim)
        sub.append(f"model {self.cfg.model}", style=p.dim)
        sub.append("   ·   ", style=p.dim)
        sub.append(self.cfg.base_url, style=p.dim)
        sub.append("   ·   ", style=p.dim)
        sub.append("type /help for commands\n", style=p.dim)
        sub.append("       by ", style=p.dim)
        sub.append("Mr-Destroyer / ZIM", style=p.primary)
        sub.append("   ·   yt @Study_Hard69   ·   ig zimthegoat", style=p.dim)
        self.query_one(ChatPane).write_block(sub)

    # ---- timers -----------------------------------------------------------
    def _status_bar(self) -> StatusBar | None:
        try:
            return self.query_one(StatusBar)
        except Exception:
            # Interval timers can fire while a screen is being swapped or the
            # app is tearing down; a missing bar is never fatal.
            return None

    def _tick_status(self) -> None:
        bar = self._status_bar()
        if bar is not None:
            bar.render_bar()

    def _flash_cursor(self) -> None:
        if not self.busy:
            return
        try:
            chat = self.query_one(ChatPane)
        except Exception:
            return
        chat.flash_cursor()
        if self._stream_buf:
            chat.set_stream(self._stream_buf)

    async def _verb_spinner(self) -> None:
        bar = self._status_bar()
        i = 0
        while self.busy:
            if bar is not None:
                bar.activity = THINKING_VERBS[i % len(THINKING_VERBS)]
                bar.render_bar()
            i += 1
            await asyncio.sleep(0.35)

    # ---- input ------------------------------------------------------------
    def _completer(self) -> CompletionPopup:
        return self.query_one("#complete", CompletionPopup)

    def _visible_completions(self) -> bool:
        try:
            return self._completer().has_class("visible")
        except Exception:
            return False

    @on(Input.Changed, "#input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        try:
            self._completer().refresh_for(event.value, self.cfg.workdir)
        except Exception:
            pass

    def on_key(self, event) -> None:
        """Route completion keys before the Input sees them."""
        if not self._visible_completions():
            return
        comp = self._completer()
        key = event.key
        if key == "down":
            comp.move(1)
            event.stop(); event.prevent_default()
        elif key == "up":
            comp.move(-1)
            event.stop(); event.prevent_default()
        elif key in ("tab", "enter", "return"):
            inp = self.query_one("#input", Input)
            new = comp.accept(inp.value)
            if new is not None:
                inp.value = new
                inp.cursor_position = len(new)
            comp.remove_class("visible")
            if key == "tab":
                event.stop(); event.prevent_default()
            # Enter with no completion left should submit, so let it through.
        elif key == "escape":
            comp.remove_class("visible")
            event.stop(); event.prevent_default()

    @on(Input.Submitted, "#input")
    def _on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#input", Input).value = ""
        self._completer().remove_class("visible")
        if not text:
            return
        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return
        if text.startswith("/"):
            self._handle_command(text)
        else:
            self._run_turn(self._expand_at_refs(text))

    # ---- turn worker ------------------------------------------------------
    @work(exclusive=True)
    async def _run_turn(self, text: str) -> None:
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)

        self.busy = True
        self._cancelled = False
        self._stream_buf = ""
        self._set_rain(False)  # keep the transcript readable during a turn

        chat.write_block(R.user_prompt_block(text, p))
        bar.set_activity("thinking", busy=True)
        spinner = asyncio.create_task(self._verb_spinner())

        try:
            async for ev in self.agent.run_turn(text):
                if self._cancelled:
                    break
                await self._handle_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            chat.write_block(R.error_block(f"{type(e).__name__}: {e}", p))
        finally:
            self.busy = False
            spinner.cancel()
            chat.clear_stream()
            self._stream_buf = ""
            bar.set_activity("idle", busy=False)
            bar.input_tokens = self.agent.session_input_tokens
            bar.output_tokens = self.agent.session_output_tokens
            bar.cost = self.agent.session_cost
            bar.turns = self.agent.turn_count
            bar.model = self.cfg.model
            bar.render_bar()
            self._set_rain(self.rain_on)
            if self._cancelled:
                self.agent.cancel_turn()
                self._sys_line("turn interrupted", warn=True)
            self.query_one("#input", Input).focus()

    async def _handle_event(self, ev: dict) -> None:
        p = self.palette
        chat = self.query_one(ChatPane)
        act = self.query_one(ActivityPane)
        bar = self.query_one(StatusBar)
        etype = ev["type"]

        if etype == "thinking":
            bar.iterations = ev["iteration"] + 1
            bar.turns = self.agent.turn_count
            bar.set_activity("thinking", busy=True)

        elif etype == "text_delta":
            self._stream_buf += ev["text"]
            chat.set_stream(self._stream_buf)

        elif etype == "retry":
            self._flush_stream(chat, p)
            self._sys_line(
                f"transport error ({ev['reason']}) — retry {ev['attempt']}/{ev['max']} "
                f"in {ev['delay']:.1f}s",
                warn=True,
            )
            bar.set_activity(f"retry {ev['attempt']}/{ev['max']}", busy=True)

        elif etype == "usage":
            # The agent has not yet folded this turn's usage into its session
            # totals, so add it here for a live running figure.
            bar.input_tokens = self.agent.session_input_tokens + ev["input"]
            bar.output_tokens = self.agent.session_output_tokens + ev["output"]
            bar.cost = self.agent.session_cost + ev["cost"]
            bar.render_bar()

        elif etype == "tool_call":
            self._flush_stream(chat, p)
            act.log_call(ev["name"], ev["args"])
            if ev.get("gated"):
                bar.set_activity(f"awaiting permission: {ev['name']}", busy=True)

        elif etype == "tool_result":
            args = ev.get("args") or {}
            diff = ev.get("diff")
            if diff and not ev["is_error"] and diff.strip() != "(no textual change)":
                verb = "WRITE" if ev["name"] == "write_file" else "EDIT"
                chat.write_block(R.diff_panel(args.get("path", "?"), diff, p, verb))
            else:
                chat.write_block(
                    R.tool_result_panel(ev["name"], args, ev["output"], p,
                                        ev["is_error"], ev.get("meta"))
                )
            act.log_result(ev["name"], ev["output"], ev["is_error"], ev.get("meta") or {})

        elif etype == "blocked":
            chat.write_block(R.blocked_block(ev["targets"], p))
            act.log_blocked(ev["targets"])

        elif etype == "error":
            self._flush_stream(chat, p)
            chat.write_block(R.error_block(ev["message"], p))

        elif etype == "turn_end":
            self._flush_stream(chat, p)
            self._cost_line(ev)

    def _flush_stream(self, chat: ChatPane, palette) -> None:
        if self._stream_buf.strip():
            chat.clear_stream()
            chat.write_block(R.agent_text(self._stream_buf, palette))
            self._stream_buf = ""

    def _cost_line(self, ev) -> None:
        p = self.palette
        t = Text()
        t.append("  ⤷ ", style=p.dim)
        t.append(f"+{ev['input']}", style=p.dim)
        t.append(" in / ", style=p.dim)
        t.append(f"+{ev['output']}", style=p.dim)
        t.append(" out tok  ·  ", style=p.dim)
        t.append(f"${ev['cost']:.4f}", style=p.accent)
        t.append(f"  ·  {ev['model']}", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    # ---- permission gate --------------------------------------------------
    async def request_permission(self, name, args, preview):
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        inp = self.query_one("#input", Input)
        inp.disabled = True  # keep approval keys out of the prompt

        def _done(decision):
            inp.disabled = False
            if not fut.done():
                fut.set_result(decision or "no")

        self.push_screen(PermissionModal(name, preview, self.palette), _done)
        return await fut

    # ---- system lines -----------------------------------------------------
    def _sys_line(self, message: str, warn: bool = False, ok: bool = False) -> None:
        p = self.palette
        t = Text()
        if warn:
            t.append("  ⚠ ", style=f"bold {p.amber}")
            t.append(message, style=p.amber)
        elif ok:
            t.append("  ✔ ", style=f"bold {p.accent}")
            t.append(message, style=p.accent)
        else:
            t.append("  · ", style=p.dim)
            t.append(message, style=p.dim)
        self.query_one(ChatPane).write_block(t)

    # ---- slash commands ---------------------------------------------------
    def _handle_command(self, text: str) -> None:
        parts = text[1:].split()
        cmd = parts[0].lower() if parts else "help"
        args = parts[1:]
        dispatch = {
            "help": self._cmd_help,
            "clear": self._cmd_clear,
            "mode": self._cmd_mode,
            "model": self._cmd_model,
            "cost": self._cmd_cost,
            "scope": self._cmd_scope,
            "rain": self._cmd_rain,
            "theme": self._cmd_theme,
            "save": self._cmd_save,
            "load": self._cmd_load,
            "compact": lambda a: self._cmd_compact(),
            "exit": lambda a: self.exit(),
            "quit": lambda a: self.exit(),
        }
        fn = dispatch.get(cmd)
        if fn is None:
            self._sys_line(f"unknown command: /{cmd}  — try /help", warn=True)
            return
        fn(args)

    def _cmd_help(self, args=None) -> None:
        p = self.palette
        rows = [
            ("/help", "this help"),
            ("/mode [name]", "switch mode: auto | edits | plan | zim | danger"),
            ("/clear", "wipe the transcript and conversation history"),
            ("/model [name]", "show or switch the model"),
            ("/cost", "session token and cost breakdown"),
            ("/scope", "show scope guard status and entries"),
            ("/rain", "toggle the matrix-rain background"),
            ("/theme green|amber|cyan", "switch the color theme"),
            ("/save [name]", "write the session to disk"),
            ("/load [name]", "restore a saved session"),
            ("/compact", "summarise history to free context"),
            ("/exit", "leave the harness        (Ctrl+D also works)"),
        ]
        t = Text()
        t.append("  COMMANDS\n\n", style=f"bold {p.accent}")
        for name, desc in rows:
            t.append(f"  {name:<24}", style=f"bold {p.primary}")
            t.append(desc + "\n", style=p.dim)
        t.append("\n  MODES\n\n", style=f"bold {p.accent}")
        for name, spec in MODES.items():
            mstyle = f"bold {p.red}" if spec.get("loud") else f"bold {p.primary}"
            t.append(f"  {name:<10}", style=mstyle)
            t.append(spec["blurb"] + "\n", style=p.dim)
        t.append("\n  MENTIONS  ", style=f"bold {p.accent}")
        t.append("@path attaches a file's contents   ·   type / or @ for the popup\n", style=p.dim)
        t.append("  KEYS  ", style=f"bold {p.accent}")
        t.append("↑↓ select · Tab accept · Esc close · Ctrl+C interrupt · Ctrl+D exit\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    def _cmd_mode(self, args) -> None:
        p = self.palette
        if not args:
            t = Text()
            t.append("  AGENT MODE\n\n", style=f"bold {p.accent}")
            for name, spec in MODES.items():
                active = name == self.cfg.mode
                mark = "◉" if active else "○"
                if active and spec.get("loud"):
                    style = f"bold white on {p.red}"
                elif active:
                    style = f"bold {p.accent}"
                else:
                    style = p.primary
                t.append(f"  {mark} {name:<8}", style=style)
                t.append(spec["blurb"] + "\n", style=p.dim)
            if self.cfg.mode == "zim":
                src = self.cfg.agents_path or "(AGENTS.md not found)"
                t.append("\n  following  ", style=p.dim)
                t.append(str(src) + "\n", style=p.primary)
            t.append("\n  usage: /mode <name>\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return
        name = args[0].lower()
        if name not in MODES:
            self._sys_line(
                f"unknown mode: {name} — try " + " | ".join(MODES), warn=True
            )
            return
        self._set_mode(name)

    def _set_mode(self, name: str) -> None:
        self.cfg.mode = name
        self.agent.set_mode(name)
        self.query_one(HeaderBar).set_mode(name)
        if name == "zim":
            src = self.cfg.agents_path
            if src:
                self._sys_line(f"ZIM MODE ARMED — following {src} · full auto", warn=True)
            else:
                self._sys_line(
                    "ZIM MODE ARMED — no AGENTS.md found (set --agents PATH)", warn=True
                )
        elif name == "danger":
            self._sys_line(
                "DANGER MODE — every tool runs unattended; the operator's word is law",
                warn=True,
            )
        else:
            self._sys_line(f"mode → {MODES[name]['label']} ({MODES[name]['blurb']})", ok=True)

    def _cmd_clear(self, args=None) -> None:
        self.query_one(ChatPane).clear()
        self.query_one(ActivityPane).clear()
        self.agent.clear()
        self._splash()
        self._sys_line("transcript and history cleared", ok=True)

    def _cmd_model(self, args) -> None:
        p = self.palette
        if not args:
            t = Text()
            t.append("  current model: ", style=p.dim)
            t.append(self.cfg.model + "\n\n", style=f"bold {p.accent}")
            for m in KNOWN_MODELS:
                mark = "◉" if m == self.cfg.model else "○"
                style = f"bold {p.accent}" if m == self.cfg.model else p.primary
                pin, pout = price_for(m)
                t.append(f"  {mark} {m:<22}", style=style)
                t.append(f"${pin:.2f}/${pout:.2f} per Mtok\n", style=p.dim)
            t.append("\n  usage: /model <name>\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return
        name = args[0]
        self.cfg.model = name
        self.agent.set_model(name)
        self.query_one(HeaderBar).set_model(name)
        self.query_one(StatusBar).model = name
        self.query_one(StatusBar).render_bar()
        self._sys_line(f"model switched to {name}", ok=True)

    def _cmd_cost(self, args=None) -> None:
        p = self.palette
        a = self.agent
        pin, pout = price_for(self.cfg.model)
        t = Text()
        t.append("  SESSION ACCOUNTING\n\n", style=f"bold {p.accent}")
        for label, value in [
            ("model", self.cfg.model),
            ("turns", str(a.turn_count)),
            ("input tokens", f"{a.session_input_tokens:,}"),
            ("output tokens", f"{a.session_output_tokens:,}"),
            ("rate (in/out)", f"${pin:.2f} / ${pout:.2f} per Mtok"),
            ("last turn", f"${a.last_turn_cost:.4f}"),
        ]:
            t.append(f"  {label:<18}", style=p.dim)
            t.append(value + "\n", style=p.primary)
        t.append(f"  {'session total':<18}", style=p.dim)
        t.append(f"${a.session_cost:.4f}\n", style=f"bold {p.accent}")
        self.query_one(ChatPane).write_block(t)

    def _cmd_scope(self, args=None) -> None:
        p = self.palette
        s = self.agent.scope
        t = Text()
        t.append("  SCOPE GUARD\n\n", style=f"bold {p.accent}")
        t.append(f"  {'file':<14}", style=p.dim)
        t.append(f"{s.path if s.path else '(none)'}\n", style=p.primary)
        t.append(f"  {'status':<14}", style=p.dim)
        if not s.loaded:
            t.append("inactive — no scope file\n", style=p.amber)
        elif s.empty:
            t.append("EMPTY — all network targets blocked\n", style=f"bold {p.red}")
        else:
            t.append("enforcing\n", style=f"bold {p.accent}")
        if s.domains:
            t.append(f"  {'domains':<14}", style=p.dim)
            t.append(", ".join(sorted(s.domains)) + "\n", style=p.primary)
        if s.ips:
            t.append(f"  {'ips':<14}", style=p.dim)
            t.append(", ".join(sorted(s.ips)) + "\n", style=p.primary)
        if s.cidrs:
            t.append(f"  {'cidrs':<14}", style=p.dim)
            t.append(", ".join(str(c) for c in s.cidrs) + "\n", style=p.primary)
        self.query_one(ChatPane).write_block(t)

    def _set_rain(self, active: bool) -> None:
        """Turn the rain animation on/off (paused during active turns)."""
        self.query_one(ChatPane).set_rain(active)
        self.query_one(ActivityPane).set_rain(active)

    def _cmd_rain(self, args=None) -> None:
        self.rain_on = not self.rain_on
        self._set_rain(self.rain_on and not self.busy)
        self._sys_line(f"matrix rain {'ON' if self.rain_on else 'OFF'}", ok=True)

    def _cmd_theme(self, args) -> None:
        if not args:
            self._sys_line("usage: /theme green | amber | cyan", warn=True)
            return
        name = args[0].lower()
        if name not in ("green", "amber", "cyan"):
            self._sys_line(f"unknown theme: {name}", warn=True)
            return
        self.theme_name = name
        self.palette = get_palette(name)
        self._apply_palette()
        self._sys_line(f"theme switched to {name}", ok=True)

    def _apply_palette(self) -> None:
        p = self.palette
        try:
            theme = p.rich_theme()
            self.register_theme(theme)
            self.theme = theme.name
        except Exception:
            pass

        self.query_one(ChatPane).apply_palette(p)
        act = self.query_one(ActivityPane)
        act.apply_palette(p)
        act.styles.border_left = ("heavy", p.dim)

        h = self.query_one(HeaderBar)
        h.palette = p
        h.set_theme_name(self.theme_name)

        b = self.query_one(StatusBar)
        b.palette = p
        b.render_bar()

        inp = self.query_one(Input)
        inp.styles.border = ("round", p.primary)
        inp.styles.color = p.primary

    def _cmd_save(self, args) -> None:
        try:
            path = session_mod.save(self.cfg, self.agent, args[0] if args else None)
            self._sys_line(f"saved session → {path}", ok=True)
        except Exception as e:  # noqa: BLE001
            self._sys_line(f"save failed: {e}", warn=True)

    def _cmd_load(self, args) -> None:
        p = self.palette
        if not args:
            listing = session_mod.list_sessions(self.cfg)
            t = Text()
            t.append("  SAVED SESSIONS\n\n", style=f"bold {p.accent}")
            if not listing:
                t.append("  (none)\n", style=p.dim)
            for s in listing[:20]:
                ts = datetime.fromtimestamp(s["saved_at"]).strftime("%Y-%m-%d %H:%M")
                t.append(f"  {s['file']:<34}", style=p.primary)
                t.append(f"{ts}  {s['model']}  {s['messages']} msgs\n", style=p.dim)
            t.append("\n  usage: /load <name>\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return
        ok, msg = session_mod.load(self.cfg, self.agent, args[0])
        if ok:
            self.query_one(HeaderBar).set_model(self.cfg.model)
            self.query_one(StatusBar).model = self.cfg.model
            self._sys_line(msg, ok=True)
        else:
            self._sys_line(msg, warn=True)

    @work(exclusive=True)
    async def _cmd_compact(self) -> None:
        self.busy = True
        bar = self.query_one(StatusBar)
        bar.set_activity("compacting history", busy=True)
        spinner = asyncio.create_task(self._verb_spinner())
        self._sys_line("compacting conversation…")
        try:
            ok, result = await self.agent.compact()
        finally:
            spinner.cancel()
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self.query_one("#input", Input).focus()
        p = self.palette
        if ok:
            t = Text()
            t.append("  ✔ history compacted\n\n", style=f"bold {p.accent}")
            t.append(result, style=p.primary)
            self.query_one(ChatPane).write_block(t)
        else:
            self._sys_line(f"compact failed: {result}", warn=True)

    # ---- key actions ------------------------------------------------------
    def action_interrupt(self) -> None:
        if not self.busy:
            self._sys_line("nothing to interrupt")
            return
        self._cancelled = True
        # If a permission modal is open, dismiss it so the turn can unwind
        # instead of hanging on an unanswered future.
        if isinstance(self.screen, PermissionModal):
            self.query_one("#input", Input).disabled = False
            self.screen.dismiss("no")
        # Also stop a pending turn worker promptly.
        try:
            self.workers.cancel_all()
        except Exception:
            pass
        self._sys_line("interrupt requested…", warn=True)

    def action_clear(self) -> None:
        self._cmd_clear()

    def action_quit(self) -> None:
        self.exit()
