"""The main Textual application."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import MouseUp, TextSelected
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .. import hunt as hunt_mod
from .. import osint as osint_mod
from .. import phish as phish_mod
from .. import session as session_mod
from .. import sources as sources_mod
from .. import team as team_mod
from .. import tokenharbour as th_mod
from .. import tools as tools_mod
from ..agent import Agent
from ..config import KNOWN_MODELS, MODES, Config, find_agents_file, price_for
from ..theme import THINKING_VERBS, agent_color, get_palette
from . import renderers as R
from .boot import BootScreen
from .complete import CompletionPopup, _iter_files
from .palette_cmd import CommandPalette, PaletteEntry
from .rails import LoopRail, TelemetryRail
from .widgets import ChatPane, HeaderBar, PromptInput, StatusBar, ZimPane


def _dedupe(items) -> list[str]:
    """Order-preserving de-dupe.

    ``Scope.extract_targets`` can return the same host twice — its URL pass and
    its bare-token pass both match — and the operator should read the target
    once, not twice.
    """
    return list(dict.fromkeys(items))


class PermissionModal(ModalScreen[str]):
    """Confirmation panel for gated tools. Dismisses with yes/no/always."""

    BINDINGS = [
        Binding("y", "choose('yes')", "yes"),
        Binding("n", "choose('no')", "no"),
        Binding("a", "choose('always')", "always"),
        Binding("escape", "choose('no')", "no"),
        Binding("enter", "choose('yes')", "yes"),
    ]

    def __init__(self, name: str, preview, palette, remaining: int = 0,
                 radius: str = "") -> None:
        super().__init__()
        self.tool_name = name
        self.preview = preview
        self.palette = palette
        self.remaining = remaining
        self.radius = radius

    def compose(self) -> ComposeResult:
        with Vertical(id="perm-box"):
            yield Static(id="perm-title")
            yield Static(id="perm-radius")
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

        # Blast radius: what this call would actually touch, before you answer.
        if self.radius:
            rad = Text()
            rad.append("⌖ ", style=p.dim)
            rad.append(self.radius, style=p.amber)
            self.query_one("#perm-radius", Static).update(rad)

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


class ReconOverlay(ModalScreen[None]):
    """A centred window showing the recon phase working, live.

    Recon can run for minutes against a live target, and until it finishes the
    operator has nothing to look at — the zim-pane belongs to the waves, which
    have not started. So this floats over the middle of the screen and narrates
    what recon is actually doing: each tool call as it is made, and the model's
    prose as it is written.

    It is deliberately not a gate. There is nothing to confirm, so it takes no
    input, has no keys and cannot be dismissed by the operator — ``run_hunt``
    pops it when recon ends. ``can_focus = False`` keeps the prompt underneath
    alive, which is what lets ``/stop-hunt`` and the other busy-permitted
    commands still be typed while it is up.
    """

    can_focus = False

    def __init__(self, target: str, palette, cfg: Config) -> None:
        super().__init__()
        self.target = target
        self.palette = palette
        self.cfg = cfg
        #: The tail of the activity log. Bounded: a recon run makes dozens of
        #: calls, and the window only has room for the last few.
        self.lines: list[Text] = []
        self.calls = 0
        self.started = time.monotonic()

    def compose(self) -> ComposeResult:
        with Vertical(id="recon-box"):
            yield Static(id="recon-title")
            yield Static(id="recon-status")
            with VerticalScroll(id="recon-log"):
                yield Static(id="recon-lines")

    def on_mount(self) -> None:
        # A recon that finishes before this screen has mounted is not
        # hypothetical: a target that answers instantly runs recon to its
        # conclusion in the same tick it was pushed from, and `run_hunt` pops
        # the window as soon as recon is done. The pop tears the widget tree
        # down before the queued Mount message lands, so the children are
        # already gone by the time this runs — `query_one` then raises out of a
        # message handler, which Textual treats as an app crash. An overlay
        # that is on its way off screen has nothing to paint.
        p = self.palette
        t = Text()
        t.append("  ⟩ RECON  ", style=f"bold {p.bg} on {p.accent}")
        t.append("  mapping ", style=p.dim)
        t.append(self.target, style=f"bold {p.primary}")
        try:
            self.query_one("#recon-title", Static).update(t)
            self._paint_status()
        except NoMatches:
            return

    def note(self, event: dict) -> None:
        """Apply one recon event and repaint."""
        kind = event.get("type")
        p = self.palette
        if kind == "hunt_recon_tool":
            self.calls += 1
            tool = event.get("tool", "?")
            detail = tools_mod.summarise_call(tool, event.get("args") or {}, self.cfg)
            if len(detail) > 72:
                detail = detail[:71] + "…"
            t = Text()
            t.append(f"  {self.calls:>3}  ", style=p.dim)
            t.append("✗ " if event.get("blocked") else "▸ ", style=p.red if event.get("blocked") else p.accent)
            t.append(f"{tool:<12}", style=f"bold {p.primary}")
            t.append(detail, style=p.dim)
            self._push(t)
        elif kind == "hunt_recon_text":
            text = (event.get("text") or "").strip()
            if text:
                t = Text()
                t.append("       ", style=p.dim)
                t.append(text, style=p.primary)
                self._push(t)
        self._paint_status()

    def _push(self, line: Text) -> None:
        self.lines.append(line)
        if len(self.lines) > 200:
            self.lines = self.lines[-200:]
        try:
            self.query_one("#recon-lines", Static).update(
                Text("\n").join(self.lines[-40:])
            )
            self.query_one("#recon-log", VerticalScroll).scroll_end(animate=False)
        except NoMatches:
            # Torn down before it could mount — see on_mount. The lines are
            # still kept, so nothing is lost if it does come up.
            pass

    def _paint_status(self) -> None:
        p = self.palette
        elapsed = int(time.monotonic() - self.started)
        t = Text()
        t.append("  ", style=p.dim)
        t.append(f"{self.calls} call{'s' if self.calls != 1 else ''}", style=p.primary)
        t.append("   ·   ", style=p.dim)
        t.append(f"{elapsed // 60}:{elapsed % 60:02d}", style=p.primary)
        t.append("   ·   ", style=p.dim)
        t.append("working", style=f"bold {p.accent}")
        try:
            self.query_one("#recon-status", Static).update(t)
        except NoMatches:
            pass


class FindingsPanel(ModalScreen[None]):
    """The hunt's finding tracker, as its own window.

    The transcript shows a finding the moment it lands and then scrolls past it;
    the zim-pane shows only counts. Neither answers "what has this engagement
    actually found", which is the question the operator asks while it runs and
    again when it is over. So this floats a worst-first list over the screen:
    severity, title, asset, wave, and the evidence that proves it.

    It is a *view*, not a gate — ``can_focus = False`` keeps the prompt alive so
    ``/stop-hunt`` stays typeable while it is open, and ``escape`` (or
    ``/findings`` again) closes it. ``set_findings`` repaints it in place, so the
    hunt worker can push each new finding in without reopening the window.

    There is deliberately no click-to-dismiss: a click anywhere inside the
    panel bubbles to this screen, so handling clicks here would close the
    window the moment the operator clicked a finding to read it.
    """

    can_focus = False

    BINDINGS = [Binding("escape", "dismiss_panel", "close", show=False)]

    def __init__(self, palette, *, target: str = "", live: bool = False) -> None:
        super().__init__()
        self.palette = palette
        self.target = target
        #: Whether a campaign is still running, so the header can say LIVE vs a
        #: finished campaign read back off disk.
        self.live = live
        self.findings: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="findings-box"):
            yield Static(id="findings-title")
            with VerticalScroll(id="findings-log"):
                yield Static(id="findings-body")

    def on_mount(self) -> None:
        self._paint()

    def set_findings(self, findings: list[dict]) -> None:
        """Replace the list and repaint. Called as each finding lands."""
        self.findings = list(findings or [])
        self._paint()

    def action_dismiss_panel(self) -> None:
        self.dismiss(None)

    def _paint(self) -> None:
        try:
            self.query_one("#findings-title", Static).update(self._title())
            self.query_one("#findings-body", Static).update(self._body())
        except NoMatches:
            # Torn down before it mounted — the panel is dismissable and a hunt
            # can end in the same tick it was opened. Nothing to paint.
            return

    def _title(self) -> Text:
        p = self.palette
        t = Text()
        t.append("  ⟩ FINDINGS  ", style=f"bold {p.bg} on {p.accent}")
        t.append("  ", style=p.dim)
        t.append(self.target or "?", style=f"bold {p.primary}")
        t.append("   ", style=p.dim)
        t.append(f"{len(self.findings)}", style=f"bold {p.primary}")
        t.append(" found", style=p.dim)
        if self.live:
            t.append("   ·   ", style=p.dim)
            t.append("LIVE", style=f"bold {p.accent}")
        t.append("      ", style=p.dim)
        t.append("[esc] close", style=p.dim)
        return t

    def _body(self) -> Text:
        p = self.palette
        t = Text()
        if not self.findings:
            t.append("\n  nothing confirmed yet.\n\n", style=p.dim)
            t.append(
                "  A finding appears here the moment an agent reports one —\n"
                "  a title, the asset, and the evidence that proves it.\n",
                style=p.dim,
            )
            return t
        for i, f in enumerate(self.findings):
            if i:
                t.append("\n")
            t.append_text(R.finding_line(f, p))
            t.append("\n")
            t.append_text(R.finding_detail(f, p))
        return t


class ZimZillaApp(App):
    """Top-level application: boot screen, then the shell."""

    # Textual already highlights on drag. We copy the highlight as soon as
    # the mouse is released so the operator never has to remember a binding.
    ALLOW_SELECT = True

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
    /* Below ~100 cols the rails are hidden by on_resize and the layout is
       today's two-pane shell. The transcript is never the thing that shrinks. */
    Screen.narrow LoopRail, Screen.narrow TelemetryRail { display: none; }
    /* ZimPane stays up on a narrow terminal: the operator opened it on
       purpose, and close/minimize/resize are how they put it away. */
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
        /* Textual tints the focused input with $foreground 5%, which renders as
           a shade outside the palette. Turn the tint off so the field keeps the
           palette background exactly. */
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
        /* Dim toward the palette background, not toward black: on a terminal
           whose background is not black, rgba(0,0,0,…) would tint the scrim a
           different colour from the rest of the interface. */
        background: $background 75%;
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
    /* The palette's query field lives in the app CSS, not the screen's
       DEFAULT_CSS: the global `Input:focus` rule below would otherwise give it
       a border, and a 1-row field with a border has no content area at all. */
    #pal-query, #pal-query:focus {
        height: 1;
        border: none;
        background: $background;
        color: $primary;
        padding: 0;
        background-tint: transparent;
    }
    #pal-query > .input--placeholder { color: $secondary; background: $background; }
    #pal-query > .input--cursor { color: $background; background: $accent; }

    /* NOTE: no `Input:focus` rule here. The global rule above matches the
       palette's query field too and, at height 1, leaves no content area — so
       the typed query would be invisible. #pal-query resets it explicitly. */

    #perm-title { height: 1; }
    #perm-radius { height: auto; color: $warning; }
    #perm-content { height: auto; }
    #perm-keys { height: 1; margin-top: 1; }
    #perm-progress { height: 1; color: #0b6e2a; }

    /* The recon window. Centred and floating over the shell, so the operator
       watches the mapping happen instead of staring at a frozen status line.
       Its scrim is lighter than the permission modal's: this is a window you
       look past, not one you answer, and the shell underneath is still live. */
    ReconOverlay {
        align: center middle;
        background: $background 55%;
    }
    #recon-box {
        width: 72%;
        max-width: 96;
        height: auto;
        max-height: 70%;
        border: heavy $accent;
        background: $background;
        padding: 1 2;
    }
    #recon-title { height: 1; }
    #recon-status { height: 1; }
    #recon-log {
        height: auto;
        max-height: 20;
        margin-top: 1;
        scrollbar-size-vertical: 1;
        scrollbar-color: $secondary;
    }
    #recon-lines { height: auto; }
    FindingsPanel {
        align: center middle;
        background: $background 55%;
    }
    #findings-box {
        width: 84%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: heavy $accent;
        background: $background;
        padding: 1 2;
    }
    #findings-title { height: 1; }
    #findings-log {
        height: auto;
        max-height: 28;
        margin-top: 1;
        scrollbar-size-vertical: 1;
        scrollbar-color: $secondary;
    }
    #findings-body { height: auto; }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "interrupt", priority=True),
        Binding("ctrl+d", "quit", "quit", priority=True),
        Binding("ctrl+l", "clear", "clear", priority=True),
        Binding("ctrl+k", "palette", "commands", priority=True),
    ]

    def __init__(self, cfg: Config, term_bg: str | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        #: The terminal's own background, detected over OSC 11 by __main__. None
        #: on a terminal that does not answer, in which case the palette keeps
        #: its own black. Stored so /theme can re-skin onto the same colour.
        self.term_bg = term_bg
        self.palette = get_palette(cfg.theme, term_bg)
        self.theme_name = cfg.theme
        self.rain_on = cfg.rain
        self.agent = Agent(cfg, permission_handler=self.request_permission)
        # While a modal (permission gate) is up, the prompt must be disabled so
        # approval keystrokes like "y" don't leak into it.
        self._modal_depth = 0
        # Serialises permission modals. One modal can be up at a time, and the
        # disable/restore bookkeeping in request_permission is not re-entrant —
        # so with /team's parallel workers two gated calls could otherwise stack
        # modals and leave the prompt permanently disabled.
        self._perm_lock = asyncio.Lock()
        self.busy = False
        self._cancelled = False
        self._stream_buf = ""
        # Tokens-per-second is derived from the delta in session output tokens
        # across successive 1s ticks — no separate timer, and no rate to report
        # while nothing is streaming.
        self._last_output_tokens = 0
        # The prompt size of the most recent API call. That — not the session
        # cumulative input total — is how full the context window actually is:
        # the cumulative figure counts every turn's prompt and grows forever.
        self._last_call_input = 0
        # Team workers' usage, carried into the bar on top of the main agent's
        # own totals. _handle_event computes the bar from self.agent alone, so
        # without this the synthesis turn would overwrite the bar and drop the
        # workers' tokens and cost — the team's spend would vanish from view.
        self._team_tokens: tuple[int, int, float] = (0, 0, 0.0)
        # ---- /bug-hunt campaign state.
        # The stop flag is created once and reused, so /stop-hunt can set it
        # from the prompt while the hunt worker is mid-wave. _hunt_run is the
        # live campaign (None when idle); _hunt_summary_queued is what
        # /summary-hunt sets when it is asked for mid-run — the summary runs at
        # the next wave boundary, because two turns streaming through the same
        # transcript at once corrupts it.
        self._hunt_stop = asyncio.Event()
        self._hunt_run = None
        self._hunt_summary_queued = False
        self._hunt_tokens: tuple[int, int, float] = (0, 0, 0.0)
        #: The findings window, while it is open. Held so `/findings` toggles it
        #: and so the hunt worker can push each new finding into it live.
        self._findings_panel: FindingsPanel | None = None
        # `/phish` events arrive on the HTTP thread. The queue is the only
        # crossing: the 0.25s rail tick drains it onto ZimPane on the UI
        # thread. A lock is enough — these are tiny dicts, not renderables.
        self._phish_events: list[dict] = []
        self._phish_q_lock = threading.Lock()

    # ---- compose ----------------------------------------------------------
    def compose(self) -> ComposeResult:
        # The matrix rain is painted *inside* the transcript pane (see
        # RainRichLog) — Textual's compositor does not blend widgets across
        # layers, so a widget on a lower layer would be hidden by the panes.
        # The rails are deliberately crisp: no rain, so the numbers stay legible.
        yield HeaderBar(self.palette, self.cfg.model, str(self.cfg.workdir),
                        self.cfg.unsafe, self.theme_name, mode=self.cfg.mode)
        with Horizontal(id="main"):
            yield LoopRail(self.palette)
            yield ChatPane(self.palette, rain=self.rain_on)
            yield ZimPane(self.palette)
            yield TelemetryRail(self.palette, total_context=self.cfg.context_window)
        with Vertical(id="bottom-dock"):
            popup = CompletionPopup(self.palette, id="complete")
            popup.base_url = self.cfg.base_url
            yield popup
            yield StatusBar(self.palette)
            yield PromptInput(placeholder="❯ message ZimZilla…   ( / commands · @ files · ^K palette )",
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

    def _copy_selection(self, text: str) -> None:
        """Put *text* on the clipboard. Never fatal — a headless or
        OSC-52-less terminal just keeps the in-app clipboard."""
        text = (text or "").rstrip("\n")
        if not text.strip():
            return
        try:
            self.copy_to_clipboard(text)
        except Exception:
            return
        try:
            bar = self.query_one(StatusBar)
            n = len(text)
            bar.set_activity(f"copied {n} char{'s' if n != 1 else ''}", busy=False)
            bar.render_bar()
        except Exception:
            pass

    def on_text_selected(self, event: TextSelected) -> None:
        """Mouse-up after a drag across the transcript, rails, or zim-pane."""
        try:
            text = self.screen.get_selected_text()
        except Exception:
            text = None
        if text:
            self._copy_selection(text)

    def on_mouse_up(self, event: MouseUp) -> None:
        """Input has its own selection and does not emit TextSelected.

        Copy whatever is highlighted in the focused input when the mouse
        is released, so the prompt field behaves like the rest of the UI.
        """
        focused = self.focused
        if not isinstance(focused, Input):
            return
        selected = getattr(focused, "selected_text", "") or ""
        if selected:
            self._copy_selection(selected)

    def _boot_checks(self) -> list[tuple[str, str, bool]]:
        cfg = self.cfg
        checks: list[tuple[str, str, bool]] = [
            ("API KEY", "present" if cfg.api_key_present else "MISSING", cfg.api_key_present),
            ("ENDPOINT", cfg.redacted_endpoint(), True),
            ("MODEL", cfg.model, True),
            ("CWD", str(cfg.workdir), True),
        ]
        scope = self.agent.scope
        if scope.armed:
            checks.append((
                "ALLOW.YAML",
                f"{scope.allow.describe()} — session ARMED, tools run unattended",
                True,
            ))
        else:
            checks.append(("ALLOW.YAML", "not loaded — session not armed", True))
        if scope.deny_path is not None:
            checks.append(("OUT-OF-SCOPE", f"{scope.deny.describe()} — blocked", True))
        else:
            checks.append(("OUT-OF-SCOPE", "not loaded — nothing blocked", True))
        # A stale scope.yaml is the one genuinely alarming state: the operator
        # may believe a guard is armed when nothing at all is loaded.
        if cfg.legacy_scope_path is not None:
            checks.append((
                "SCOPE.YAML",
                f"{cfg.legacy_scope_path} ignored — split into allow.yaml / "
                "out-of-scope.yaml; nothing is armed",
                False,
            ))
        checks.append(("SANDBOX", "DISABLED (--unsafe)" if cfg.unsafe else "enabled", not cfg.unsafe))
        checks.append((
            "TOOLS",
            f"{len(tools_mod.TOOL_SCHEMAS)} registered "
            "(bash, io, search, web)",
            True,
        ))
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
            self.agent.scope.badge(),
            self.agent.scope.armed,
        )
        self._splash()
        # The split is a deliberate weakening on upgrade, so it is said out
        # loud in the transcript too — not just the boot checks, which scroll
        # away the moment the first prompt is typed.
        if self.cfg.legacy_scope_path is not None:
            self._sys_line(
                f"{self.cfg.legacy_scope_path} is ignored — the scope guard was "
                "split into allow.yaml (declared targets, arms the session) and "
                "out-of-scope.yaml (never touch). Nothing is armed or blocked.",
                warn=True,
            )
        inp = self.query_one("#input", Input)
        # Replay anything the user typed while the boot animation was running.
        text = "".join(typed or [])
        inp.focus()
        self.set_interval(1.0, self._tick_status)
        self.set_interval(0.55, self._flash_cursor)
        self.set_interval(0.25, self._tick_rails)
        self._sync_rails()
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
        self._sample_throughput()

    def _sample_throughput(self) -> None:
        """Feed the waveform a tokens-per-second sample from the token delta.

        Sampled on the existing 1s status tick rather than a timer of its own:
        the delta over one second *is* the rate, so a second timer would only
        measure the same thing more often.
        """
        now = self.agent.session_output_tokens
        delta = max(0, now - self._last_output_tokens)
        self._last_output_tokens = now
        if not self.busy:
            return
        try:
            self.query_one(TelemetryRail).push_wave(float(delta))
        except Exception:
            pass

    # ---- rails ------------------------------------------------------------
    def _tick_rails(self) -> None:
        """0.25s heartbeat: the loop node breathes, the live card's clock runs."""
        try:
            self.query_one(LoopRail).tick_pulse()
        except Exception:
            return
        try:
            self.query_one(ChatPane).card_tick()
        except Exception:
            pass
        self._drain_phish()

    def _sync_rails(self) -> None:
        """Push the agent's session totals into the rails.

        Called at boot and after every turn — anywhere the numbers change
        without an event to carry them.
        """
        try:
            rail = self.query_one(TelemetryRail)
            loop = self.query_one(LoopRail)
        except Exception:
            return
        a = self.agent
        rail.set_cost(a.session_cost, a.session_input_tokens, a.session_output_tokens)
        rail.set_context(self._last_call_input)
        loop.set_counters(a.turn_count, self.query_one(StatusBar).iterations)

    def on_resize(self, event) -> None:
        """Drop the rails on a narrow terminal rather than squeezing the chat."""
        screen = self.screen
        if screen is None:
            return
        if event.size.width < 100:
            screen.add_class("narrow")
        else:
            screen.remove_class("narrow")

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
        # A slash command is dispatched *before* the busy check, because some
        # commands have to work while a run is in flight — `/stop-hunt` and
        # `/summary-hunt` are useless otherwise, and a hunt that cannot be
        # stopped from the prompt is a hunt you can only kill with Ctrl+C.
        # _handle_command decides which commands those are; everything else
        # still refuses when busy.
        if text.startswith("/"):
            self._handle_command(text)
            return
        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return
        self._run_turn(self._expand_at_refs(text))

    # ---- turn worker ------------------------------------------------------
    @work(exclusive=True)
    async def _run_turn(
        self,
        text: str,
        display: str | None = None,
        osint_case: Path | None = None,
    ) -> None:
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)

        self.busy = True
        self._cancelled = False
        self._stream_buf = ""
        self._set_rain(False)  # keep the transcript readable during a turn

        loop = self._loop_rail()
        if loop is not None:
            loop.set_stage("thinking")
            loop.set_counters(self.agent.turn_count + 1, 0)

        # The spine joint that opens the turn, then the prompt it belongs to.
        # `display` lets a caller show a short label for a long synthetic prompt
        # (/osint's playbook is thousands of words) while the agent still
        # receives the full text — the transcript shows what the operator
        # typed, not the briefing behind it.
        chat.write_block(R.turn_marker(self.agent.turn_count + 1, p))
        chat.write_block(R.user_prompt_block(display or text, p))
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
            chat.card_finish()
            self._stream_buf = ""
            bar.set_activity("idle", busy=False)
            bar.input_tokens = self.agent.session_input_tokens
            bar.output_tokens = self.agent.session_output_tokens
            bar.cost = self.agent.session_cost
            bar.turns = self.agent.turn_count
            bar.model = self.cfg.model
            bar.render_bar()
            loop = self._loop_rail()
            if loop is not None:
                loop.set_stage("idle")
                loop.set_counters(self.agent.turn_count, bar.iterations)
            self._sync_rails()
            self._set_rain(self.rain_on)
            if self._cancelled:
                self.agent.cancel_turn()
                self._sys_line("turn interrupted", warn=True)
            # An osint run leaves its whole case directory behind — clones,
            # raw API dumps, avatars. Once the report is written, archive it
            # and delete the rest, so ~/.zimzilla does not grow without bound.
            # Runs in the finally block, so an interrupted run is tidied too.
            # finalise_case deletes NOTHING unless it found a report, so a run
            # that failed or was cut short keeps its evidence for inspection.
            if osint_case is not None:
                self._finalise_osint(osint_case)
            self.query_one("#input", Input).focus()

    async def _handle_event(self, ev: dict) -> None:
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)
        loop = self._loop_rail()
        tele = self._telemetry_rail()
        etype = ev["type"]

        if etype == "thinking":
            bar.iterations = ev["iteration"] + 1
            bar.turns = self.agent.turn_count
            bar.set_activity("thinking", busy=True)
            if loop is not None:
                loop.set_stage("thinking")
                loop.set_counters(self.agent.turn_count, bar.iterations)

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
            # totals, so add it here for a live running figure. Any team
            # workers' usage rides on top — they spent against the same session
            # and dropping them here would make the total jump backwards when
            # the synthesis turn starts.
            team_in, team_out, team_cost = self._team_tokens
            bar.input_tokens = self.agent.session_input_tokens + team_in + ev["input"]
            bar.output_tokens = self.agent.session_output_tokens + team_out + ev["output"]
            bar.cost = self.agent.session_cost + team_cost + ev["cost"]
            bar.render_bar()
            # ev["input"] is this one call's prompt size — the closest thing to
            # a live context reading available.
            self._last_call_input = ev["input"]
            if tele is not None:
                tele.set_context(self._last_call_input)

        elif etype == "tool_call":
            self._flush_stream(chat, p)
            args = ev["args"] or {}
            # The live card: one slot, transitioning requested → running. It is
            # only *visible* for a slow tool — an instant read finishes before
            # the next paint, which is the correct behaviour, not a bug.
            chat.card_finish()
            chat.card_begin(ev["name"], args, self._targets_for(ev["name"], args))
            if tele is not None:
                tele.touch_file(self._file_for(ev["name"], args))
                tele.note_call(ev["name"])
            if loop is not None:
                loop.set_stage("calling")
            if ev.get("gated"):
                bar.set_activity(f"awaiting permission: {ev['name']}", busy=True)

        elif etype == "tool_result":
            args = ev.get("args") or {}
            diff = ev.get("diff")
            chat.card_finish()
            if diff and not ev["is_error"] and diff.strip() != "(no textual change)":
                verb = "WRITE" if ev["name"] == "write_file" else "EDIT"
                chat.write_block(R.diff_panel(args.get("path", "?"), diff, p, verb))
            else:
                chat.write_block(
                    R.tool_result_panel(ev["name"], args, ev["output"], p,
                                        ev["is_error"], ev.get("meta"))
                )
            if tele is not None:
                tele.note_result(ev["name"], not ev["is_error"])
            if loop is not None:
                loop.set_stage("observing")

        elif etype == "blocked":
            chat.card_finish()
            chat.write_block(R.blocked_block(ev["targets"], p))
            if tele is not None:
                tele.note_result(ev.get("name", "bash"), False)
            if loop is not None:
                loop.set_stage("observing")

        elif etype == "error":
            self._flush_stream(chat, p)
            chat.card_finish()
            chat.write_block(R.error_block(ev["message"], p))

        elif etype == "turn_end":
            self._flush_stream(chat, p)
            chat.card_finish()
            self._cost_line(ev)
            if tele is not None:
                tele.set_cost(ev["cost"] + self.agent.session_cost, bar.input_tokens,
                              bar.output_tokens)
            if loop is not None:
                loop.set_stage("idle")

    # ---- rails helpers ----------------------------------------------------
    def _loop_rail(self) -> LoopRail | None:
        try:
            return self.query_one(LoopRail)
        except Exception:
            return None

    def _telemetry_rail(self) -> TelemetryRail | None:
        try:
            return self.query_one(TelemetryRail)
        except Exception:
            return None

    def _file_for(self, name: str, args: dict) -> str:
        """The path a tool acts on, for the file river. Empty when there is none."""
        if name in {"read_file", "write_file", "edit_file", "list_dir"}:
            return str(args.get("path", "") or "")
        if name in {"glob", "grep"}:
            return str(args.get("path", "") or "")
        return ""

    def _targets_for(self, name: str, args: dict) -> list[str]:
        """Hosts a bash command names — shown on the card, never enforced here.

        Reuses the scope guard's extractor, so the card shows exactly what the
        deny-list would have looked at.
        """
        if name != "bash":
            return []
        command = args.get("command", "")
        if not command:
            return []
        try:
            return _dedupe(self.agent.scope.extract_targets(command))[:4]
        except Exception:
            return []

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
        # One modal at a time. Without the lock, /team's parallel workers could
        # each push a modal; the second would sit on top of the first and the
        # `inp.disabled = False` in whichever finishes first would re-enable the
        # prompt while a modal is still up.
        async with self._perm_lock:
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            inp = self.query_one("#input", Input)
            inp.disabled = True  # keep approval keys out of the prompt

            def _done(decision):
                inp.disabled = False
                if not fut.done():
                    fut.set_result(decision or "no")

            self.push_screen(
                PermissionModal(name, preview, self.palette,
                                radius=self._blast_radius(name, args)),
                _done,
            )
            return await fut

    def _blast_radius(self, name: str, args: dict) -> str:
        """One line naming what a gated call would touch.

        Latent by design: every shipped mode lists its gated tools in `auto` or
        `deny`, so this modal never fires today. It is correct if a mode is ever
        added that leaves a tool gated — and the same target scan is shown on
        the live tool card, which *is* reachable.
        """
        from ..sandbox import SandboxError, resolve_in_jail

        args = args or {}
        if name == "bash":
            command = args.get("command", "")
            try:
                targets = self.agent.scope.extract_targets(command)
            except Exception:
                targets = []
            if targets:
                return "targets: " + ", ".join(_dedupe(targets))
            return "no network targets named"
        path = str(args.get("path", "") or "")
        if not path:
            return ""
        try:
            resolved = resolve_in_jail(self.cfg.workdir, path, self.cfg.unsafe)
        except SandboxError as e:
            return f"REFUSED — {e}"
        exists = "exists" if resolved.exists() else "new file"
        return f"{resolved}  ({exists})"

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
    #: Commands that answer even while the harness is busy. These are the ones
    #: that either report state, control a running campaign, or shut the session
    #: down — none of them starts a turn on `self.agent`, which is what makes
    #: running them mid-flight safe. Everything else is a launcher and would
    #: interleave a second turn into the transcript of the first.
    BUSY_OK = frozenset({
        "help", "cost", "scope", "mode", "model",
        "stop-hunt", "summary-hunt", "findings", "exit", "quit",
        # Setting a key or reading a catalog does not touch the running turn, so
        # both stay typeable mid-answer — the same reason /mode and /model do.
        "tokenharbour-api-setup", "tokenharbour-models", "zim-tokenharbour",
    })

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
            "team": lambda a: self._cmd_team(a),
            "osint": lambda a: self._cmd_osint(a),
            "phish": lambda a: self._cmd_phish(a),
            "bug-hunt": lambda a: self._cmd_bug_hunt(a),
            "stop-hunt": lambda a: self._cmd_stop_hunt(a),
            "summary-hunt": lambda a: self._cmd_summary_hunt(a),
            "findings": lambda a: self._cmd_findings(a),
            "zim-logfare": lambda a: self._cmd_zim_source("logfare"),
            "zim-tokenjuice": lambda a: self._cmd_zim_source("tokenjuice"),
            "zim-tokenharbour": lambda a: self._cmd_zim_tokenharbour(a),
            "zim-source": lambda a: self._cmd_zim_source(None),
            "tokenharbour-api-setup": lambda a: self._cmd_tokenharbour_key(a),
            "tokenharbour-models": lambda a: self._cmd_tokenharbour_models(a),
            "exit": lambda a: self.exit(),
            "quit": lambda a: self.exit(),
        }
        fn = dispatch.get(cmd)
        if fn is None:
            self._sys_line(f"unknown command: /{cmd}  — try /help", warn=True)
            return
        # The gate lives here rather than in _on_submit so a busy-permitted
        # command can still be dispatched while a run is cooking. See BUSY_OK.
        if self.busy and cmd not in self.BUSY_OK:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return
        fn(args)

    def _cmd_help(self, args=None) -> None:
        p = self.palette
        rows = [
            ("/help", "this help"),
            ("/mode [name]", "switch mode: auto | edits | plan | zim | danger | uncensored"),
            ("/clear", "wipe the transcript and conversation history"),
            ("/model [name]", "show or switch the model"),
            ("/cost", "session token and cost breakdown"),
            ("/scope", "show allow.yaml / out-of-scope.yaml status"),
            ("/rain", "toggle the matrix-rain background"),
            ("/theme green|amber|cyan", "switch the color theme"),
            ("/zim-logfare", "switch upstream to Logfare        (:4001)"),
            ("/zim-tokenjuice", "switch upstream to Token Juice    (:4000)"),
            ("/zim-tokenharbour", "switch upstream to TokenHarbour (hosted)"),
            ("/zim-source", "show upstream sources and their status"),
            ("/tokenharbour-api-setup <key>", "store your TokenHarbour API key"),
            ("/tokenharbour-models", "fetch the live catalog; show what is free"),
            ("/save [name]", "write the session to disk"),
            ("/load [name]", "restore a saved session"),
            ("/compact", "summarise history to free context"),
            ("/team <task>", "fan the task out across parallel agents"),
            ("/osint [kind] <target>", "open-source recon — email, phone, socials"),
            ("/phish <host>", "clone a login page, serve it, harvest creds"),
            ("/bug-hunt <target>", "recon, then waves of 10 agents until stopped"),
            ("/stop-hunt", "end a running hunt       (works while busy)"),
            ("/summary-hunt", "write the hunt report    (works while busy)"),
            ("/findings", "the finding tracker      (works while busy)"),
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
            # Re-resolve rather than trusting the launch-time value: a session
            # started in one directory and switched to zim from another (or
            # launched before the doctrine was installed) would otherwise arm
            # with no file at all. An explicit --agents path is never overridden.
            if self.cfg.agents_path is None or not Path(self.cfg.agents_path).is_file():
                found = find_agents_file(self.cfg.workdir, self.cfg.state_dir)
                if found is not None:
                    self.cfg.agents_path = found
            src = self.cfg.agents_path
            if src:
                self._sys_line(f"ZIM MODE ARMED — following {src} · full auto", warn=True)
            else:
                self._sys_line(
                    "ZIM MODE ARMED — no AGENTS.md found (set --agents PATH)", warn=True
                )
        elif name == "danger":
            self._sys_line(
                "DANGER MODE ARMED — every tool runs unattended and the "
                "operator's word is law; nothing is questioned",
                warn=True,
            )
        elif name == "uncensored":
            self._sys_line(
                "UNCENSORED MODE ARMED — the default prompt is gone; the "
                "model cannot refuse and will only think how to do it",
                warn=True,
            )
        else:
            self._sys_line(f"mode → {MODES[name]['label']} ({MODES[name]['blurb']})", ok=True)

    def _cmd_clear(self, args=None) -> None:
        self.query_one(ChatPane).clear()
        self.agent.clear()
        tele = self._telemetry_rail()
        if tele is not None:
            tele.clear_files()
        loop = self._loop_rail()
        if loop is not None:
            loop.trail.clear()
            loop.set_stage("idle")
            loop.set_counters(0, 0)
        self._last_output_tokens = 0
        self._last_call_input = 0
        self._sync_rails()
        self._splash()
        self._sys_line("transcript and history cleared", ok=True)

    def _cmd_model(self, args) -> None:
        p = self.palette
        # Offer only what the active upstream actually serves — Logfare and
        # Token Juice have very different catalogs, and a model the upstream
        # does not know is a 404 at request time, not a switch.
        active = sources_mod.active_key(self.cfg.base_url)
        models = sources_mod.models_for(self.cfg.base_url) or tuple(KNOWN_MODELS)
        # A hosted gateway's catalog is fetched, so it also carries a free flag
        # the other sources have no equivalent of. Read once, here. Both gateways
        # expose cached_models(), so the live catalog is chosen by source key.
        hosted = HOSTED_SOURCES.get(active or "")
        live_catalog = None
        if active == "tokenharbour":
            live_catalog = th_mod.cached_models()
        elif active == "opencode":
            live_catalog = oc_mod.cached_models()

        if not args:
            t = Text()
            t.append("  current model: ", style=p.dim)
            t.append(self.cfg.model + "\n", style=f"bold {p.accent}")
            if active:
                src = sources_mod.discover().get(active)
                label = src.label if src else (hosted[0] if hosted else active)
                t.append("  source: ", style=p.dim)
                t.append(label, style=p.dim)
                t.append(f"   ·   {self.cfg.base_url}\n", style=p.dim)
            t.append("\n", style=p.dim)
            # Free first, then the rest. A hosted gateway's list is long and
            # changes; the operator almost always wants the free ones, so they
            # lead — with the model in use never hidden, whatever it costs.
            order = self._model_display_order(models, live_catalog)
            for m in order:
                mark = "◉" if m == self.cfg.model else "○"
                style = f"bold {p.accent}" if m == self.cfg.model else p.primary
                t.append(f"  {mark} {m:<26}", style=style)
                note = self._model_note(m, live_catalog)
                t.append(note + "\n", style=p.dim)
            t.append("\n  usage: /model <name>\n", style=p.dim)
            if hosted:
                t.append(f"  the catalog is live — {hosted[1]} refetches it\n",
                         style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return
        name = args[0]
        # A hosted gateway's list is fetched, so an un-warmed cache would leave
        # `models` as the Logfare fallback and reject a model the gateway does
        # serve. Fetch it here instead — this is an explicit switch, so one
        # network round-trip is the right price for a correct answer.
        if hosted and live_catalog is None:
            creds = (th_mod.load_credentials() if active == "tokenharbour"
                     else oc_mod.load_credentials())
            token = creds[0] if creds else ""
            fetched, _ = (th_mod.fetch_models(token) if active == "tokenharbour"
                          else oc_mod.fetch_models(token))
            if fetched is not None:
                live_catalog = fetched
                models = tuple(m.id for m in fetched)
        # Only police this on a known source. A custom endpoint (ZIMZILLA_NO_PROXY,
        # a direct ANTHROPIC_BASE_URL) has no catalog to check against, and the
        # old behaviour — accept anything — is right there.
        if active and name not in models:
            self._sys_line(
                f"{name} is not served by {active} — see /model for the list",
                warn=True,
            )
            return
        self.cfg.model = name
        self.agent.set_model(name)
        self.query_one(HeaderBar).set_model(name)
        self.query_one(StatusBar).model = name
        self.query_one(StatusBar).render_bar()
        self._sys_line(f"model switched to {name}", ok=True)

    def _model_display_order(self, models, catalog) -> list[str]:
        """The /model list, free models first.

        Only TokenHarbour has a free tier, and only its catalog can tell free
        from paid. For every other source the catalog is None and this is the
        declared order unchanged — so the sort never reorders a list it has no
        prices for, which would look like a bug in Logfare's catalog.
        """
        ids = list(models)
        if not catalog:
            return ids
        free = {m.id for m in catalog if m.free}
        if not free:
            return ids
        # Stable: free models keep catalog order, then the rest keep theirs.
        # The current model is pinned to the top so it is never scrolled off.
        head = [m for m in ids if m == self.cfg.model]
        frees = [m for m in ids if m in free and m != self.cfg.model]
        rest = [m for m in ids if m not in free and m != self.cfg.model]
        return head + frees + rest

    def _model_note(self, name: str, catalog) -> str:
        """The price/context note beside a model in /model.

        A hosted gateway's catalog is authoritative for its own models, so it
        answers first. The two gateways differ in what they carry: TokenHarbour
        prices each entry (so a zero price and a ``free`` flag agree), while
        OpenCode Zen lists no prices at all and marks a free model only by its
        ``-free`` id. So the free flag is read from the model itself, and a model
        the catalog prices is shown with that price. A model the catalog knows
        but does not price is called "paid" rather than dressed up with this
        harness's own estimate, which would be a guess about another party's
        bill. Only a model absent from the catalog falls through to the local
        estimate table, as every source did before.
        """
        if catalog:
            m = next((x for x in catalog if x.id == name), None)
            if m is not None:
                if m.free:
                    return "free"
                if m.price_in or m.price_out:
                    return f"${m.price_in:g}/${m.price_out:g} per Mtok"
                return "paid"
        pin, pout = price_for(name)
        return f"${pin:.2f}/${pout:.2f} per Mtok"

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

        if self.cfg.legacy_scope_path is not None:
            t.append("  ⚠ ", style=f"bold {p.amber}")
            t.append(f"{self.cfg.legacy_scope_path} is ignored\n", style=p.amber)
            t.append(
                "    the guard was split into allow.yaml / out-of-scope.yaml\n\n",
                style=p.dim,
            )

        def listing(title: str, path, hosts) -> None:
            t.append(f"  {title}\n", style=f"bold {p.accent}")
            t.append(f"    {'file':<10}", style=p.dim)
            t.append(f"{path if path else '(not found)'}\n", style=p.primary)
            if path is None:
                return
            for label, values in (
                ("domains", sorted(hosts.domains)),
                ("ips", sorted(hosts.ips)),
                ("cidrs", sorted(str(c) for c in hosts.cidrs)),
            ):
                if values:
                    t.append(f"    {label:<10}", style=p.dim)
                    t.append(", ".join(values) + "\n", style=p.primary)
            if hosts.empty:
                t.append(f"    {'entries':<10}", style=p.dim)
                t.append("(none)\n", style=p.amber)
            t.append("\n")

        t.append(f"  {'status':<12}", style=p.dim)
        if s.armed:
            t.append("ARMED", style=f"bold white on {p.red}")
            t.append(" — tools run unattended\n\n", style=p.primary)
        else:
            t.append("not armed", style=p.amber)
            t.append(" — normal mode gating applies\n\n", style=p.dim)

        listing("ALLOW  (declared targets — a declaration, not a fence)",
                s.allow_path, s.allow)
        listing("DENY   (never touched — enforced, beats allow)",
                s.deny_path, s.deny)

        t.append(
            "  Allow-list hosts are not restricted to: anything not in\n"
            "  out-of-scope.yaml is reachable. The deny-list is a static text\n"
            "  scan of the command, so obfuscated or runtime-built targets\n"
            "  (python3 -c, $(), encoded hosts) are not detected.\n",
            style=p.dim,
        )
        self.query_one(ChatPane).write_block(t)

    def _set_rain(self, active: bool) -> None:
        """Turn the rain animation on/off (paused during active turns)."""
        self.query_one(ChatPane).set_rain(active)

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
        self.palette = get_palette(name, self.term_bg)
        self._apply_palette()
        self._sys_line(f"theme switched to {name}", ok=True)

    # ---- upstream sources ------------------------------------------------
    # /zim-logfare and /zim-tokenjuice repoint the session at the other local
    # LiteLLM proxy. Both upstreams go down independently, so this is the
    # failover: the key, base_url and client are all rebuilt in place, and the
    # choice is persisted so the next launch starts on the one that worked.
    def _cmd_zim_source(self, key: str | None) -> None:
        p = self.palette
        found = sources_mod.discover()

        if key is None:
            # Bare /zim-source: just show what is available and which is live.
            active = sources_mod.active_key(self.cfg.base_url)
            t = Text()
            t.append("  UPSTREAM SOURCES\n\n", style=f"bold {p.accent}")
            for k, src in found.items():
                mark = "◉" if k == active else "○"
                style = f"bold {p.accent}" if k == active else p.primary
                up = sources_mod.proxy_healthy(src.port)
                state = "proxy up" if up else "proxy down"
                t.append(f"  {mark} {k:<12}", style=style)
                t.append(f":{src.port}  {src.label:<12} ", style=p.primary)
                t.append(f"{state}\n", style=p.accent if up else p.amber)
            # The hosted gateways have no port and no local proxy, so they are
            # listed from their credential rather than from discover() — "proxy
            # up" is not a question you can ask a remote host.
            for key, (label, _cmd) in HOSTED_SOURCES.items():
                creds = (th_mod.load_credentials() if key == "tokenharbour"
                         else oc_mod.load_credentials())
                mark = "◉" if active == key else "○"
                style = f"bold {p.accent}" if active == key else p.primary
                t.append(f"  {mark} {key:<12}", style=style)
                t.append(f"hosted  {label:<12} ", style=p.primary)
                if creds is None:
                    t.append(f"no key — /{key}-api-setup\n", style=p.amber)
                else:
                    t.append(f"key from {creds[2]}\n", style=p.accent)
            t.append("\n  switch with  /zim-logfare,  /zim-tokenjuice,  "
                     "/zim-tokenharbour  or  /opencode\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return

        src = found.get(key)
        if src is None:
            self._sys_line(
                f"source '{key}' is not available — profile, config or service missing",
                warn=True,
            )
            return

        if sources_mod.active_key(self.cfg.base_url) == key:
            self._sys_line(f"already on {src.label} (:{src.port})", ok=True)
            return

        self._switch_source(src)

    @work(exclusive=True)
    async def _switch_source(self, src) -> None:
        """Start the target proxy if needed, then rebuild the client on it."""
        p = self.palette
        self.busy = True
        bar = self.query_one(StatusBar)
        bar.set_activity(f"switching to {src.label}", busy=True)
        self._sys_line(f"switching to {src.label} (:{src.port})…")
        try:
            ok, detail = await asyncio.to_thread(sources_mod.start_proxy, src)
        finally:
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self.query_one("#input", Input).focus()

        if not ok:
            self._sys_line(f"{src.label} proxy is not up: {detail}", warn=True)
            self._sys_line(
                f"start it with: {src.service} start", warn=True
            )
            return

        # The profile carries the key for this upstream. It is read here, never
        # printed — the transcript only ever sees the label and the port.
        profile = sources_mod.load_profile(src)
        token = profile.get("ANTHROPIC_AUTH_TOKEN") or None
        if not token:
            self._sys_line(
                f"{src.label} profile has no ANTHROPIC_AUTH_TOKEN: {src.profile}",
                warn=True,
            )
            return

        self.cfg.base_url = src.base_url
        self.cfg.auth_token = token
        self.cfg.api_key = ""          # empty, so the auth_token stays authoritative
        self.cfg.model = profile.get("ANTHROPIC_MODEL") or src.model
        # Drop the cached client so the next turn rebuilds against the new host.
        self.agent._client = None

        sources_mod.set_current(src.key)
        self._refresh_after_source_change(src.label, f":{src.port}, {detail}")

    # ---- TokenHarbour ----------------------------------------------------
    # A hosted Anthropic-protocol gateway, not a local proxy: the key is set by
    # the operator (/tokenharbour-api-setup) rather than discovered in a shipped
    # profile, and the catalog is fetched live rather than declared in a table.
    def _cmd_tokenharbour_key(self, args) -> None:
        """/tokenharbour-api-setup {key} — store the TokenHarbour API key.

        Written to ~/.zimzilla/tokenharbour/source, mode 600, in the same
        profile format every other source uses. The key is never echoed back —
        the confirmation names the file and the last four characters, which is
        enough to tell two keys apart and not enough to leak one.
        """
        p = self.palette
        if not args:
            creds = th_mod.load_credentials()
            t = Text()
            t.append("  TOKENHARBOUR API KEY\n\n", style=f"bold {p.accent}")
            if creds is None:
                t.append("  no key set.\n\n", style=p.amber)
            else:
                _, _, origin = creds
                t.append("  source: ", style=p.dim)
                t.append(origin + "\n\n", style=p.primary)
            t.append("  set it with  ", style=p.dim)
            t.append("/tokenharbour-api-setup <key>\n", style=f"bold {p.primary}")
            t.append("  get a key at  ", style=p.dim)
            t.append("https://tokenharbor.ai\n", style=p.primary)
            t.append(f"  stored in     {th_mod.PROFILE}\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return

        key = args[0].strip()
        if not th_mod.looks_like_key(key):
            self._sys_line(
                "that does not look like an API key — paste just the key, "
                "not the whole `export` line", warn=True)
            return

        # Keep whatever model is current, so setting a key does not silently
        # reset the model the operator was on.
        current = self.cfg.model if th_mod.is_tokenharbour(self.cfg.base_url) else ""
        path = th_mod.save_key(key, current)
        th_mod.clear_cache()          # a new key invalidates the old catalog
        tail = key[-4:]
        self._sys_line(f"TokenHarbour key saved (…{tail}) to {path}", ok=True)
        self._sys_line("switch to it with /zim-tokenharbour", )

    def _cmd_tokenharbour_models(self, args) -> None:
        """/tokenharbour-models — fetch the catalog and show what is free, now.

        Runs the fetch off the UI thread so a slow gateway cannot freeze the
        session, then warms the cache /model reads from.
        """
        creds = th_mod.load_credentials()
        if creds is None:
            self._sys_line(
                "no TokenHarbour key — set one with /tokenharbour-api-setup <key>",
                warn=True)
            return
        token, _, origin = creds
        self._sys_line(f"fetching the TokenHarbour catalog ({origin})…")
        self._fetch_tokenharbour_models(token)

    @work(exclusive=True, thread=True)
    def _fetch_tokenharbour_models(self, token: str) -> None:
        """The blocking fetch, off the UI thread, then a call back on it."""
        models, err = th_mod.fetch_models(token)
        self.call_from_thread(self._show_tokenharbour_models, models, err)

    def _show_tokenharbour_models(self, models, err: str) -> None:
        p = self.palette
        if models is None:
            self._sys_line(f"TokenHarbour: {err}", warn=True)
            return
        free = [m for m in models if m.free]
        t = Text()
        t.append("  TOKENHARBOUR — FREE MODELS\n\n", style=f"bold {p.accent}")
        if not free:
            t.append("  nothing is free right now.\n", style=p.amber)
            t.append(f"  {len(models)} models are served, none at zero cost.\n",
                     style=p.dim)
        else:
            for m in free:
                mark = "◉" if m.id == self.cfg.model else "○"
                style = f"bold {p.accent}" if m.id == self.cfg.model else p.primary
                t.append(f"  {mark} {m.id:<26}", style=style)
                t.append(m.blurb + "\n", style=p.dim)
            t.append("\n  pick one with  ", style=p.dim)
            t.append("/model <name>\n", style=f"bold {p.primary}")
        t.append(f"  {len(models)} models served in total · "
                 "see them all with /model\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)
        # The transcript is not a picker: offer the free ones as the live list
        # /model reads, so the two commands agree.
        self._refresh_model_choices()

    def _cmd_zim_tokenharbour(self, args) -> None:
        """/zim-tokenharbour — repoint the session at the hosted gateway.

        Unlike /zim-logfare and /zim-tokenjuice there is no local proxy to start
        and no shipped profile: the key comes from wherever the operator put it
        — the environment (a sourced ~/claude-source profile), ZimZilla's own
        profile, or a ~/claude-source file — and the model defaults to a free
        one so a fresh switch costs nothing.
        """
        creds = th_mod.load_credentials()
        if creds is None:
            self._sys_line(
                "no TokenHarbour key found. Set one with "
                "/tokenharbour-api-setup <key>, or source a profile first "
                "(source ~/claude-source/haiku-5.5)", warn=True)
            return

        token, model, origin = creds
        if sources_mod.active_key(self.cfg.base_url) == "tokenharbour":
            # Already pointed here — most often because a profile was sourced
            # before launch. Record it anyway: the environment does not survive
            # the next launch, and the whole point of the selection file is that
            # the choice does.
            th_mod.set_current("tokenharbour")
            self._sys_line(f"already on TokenHarbour ({origin})", ok=True)
            return

        applied = th_mod.apply_to(self.cfg)
        if applied is None:            # unreachable: creds was just checked
            self._sys_line("no TokenHarbour key found", warn=True)
            return
        origin, model = applied
        self.agent._client = None      # rebuild against the new host
        th_mod.set_current("tokenharbour")
        self._refresh_after_source_change("TokenHarbour", f"key from {origin}")

        # A first switch with no model chosen lands on whatever the profile
        # named; with none, offer the free list rather than guessing a paid one.
        if not model:
            self._sys_line("fetching the free model list…")
            self._fetch_tokenharbour_models(token)

    def _refresh_model_choices(self) -> None:
        """Re-render the completion popup against the live catalog.

        `/model <TAB>` offers whatever ``sources_mod.models_for`` reports, which
        for TokenHarbour is the fetched list. Nothing is cached in the popup, so
        there is nothing to invalidate — but the popup may be open with the old
        list showing, and this repaints it so the two agree.
        """
        try:
            popup = self._completer()
        except NoMatches:
            return
        try:
            inp = self.query_one("#input", Input)
            popup.refresh_for(inp.value, self.cfg.workdir)
        except Exception:
            pass

    def _refresh_after_source_change(self, label: str, detail: str) -> None:
        """Repaint everything that names the model or the endpoint.

        Takes a label and a detail string rather than a Source: the local
        proxies have a port and a dataclass to describe them, TokenHarbour is a
        remote host with neither, and this only ever needed the two words.
        """
        p = self.palette
        # Same refresh _cmd_model does — HeaderBar + StatusBar carry the model.
        self.query_one(HeaderBar).set_model(self.cfg.model)
        self.query_one(StatusBar).model = self.cfg.model
        self.query_one(StatusBar).render_bar()
        # The popup offers this endpoint's catalog on `/model <TAB>`, so it has
        # to learn the new host at the same moment the client does.
        try:
            self._completer().base_url = self.cfg.base_url
        except NoMatches:
            pass
        self._sys_line(f"source switched to {label} ({detail})", ok=True)
        t = Text()
        t.append("  ↳ ", style=p.dim)
        t.append(f"model {self.cfg.model}", style=p.primary)
        t.append("   ·   ", style=p.dim)
        t.append(self.cfg.base_url, style=p.dim)
        t.append("   ·   saved for next launch\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    def _apply_palette(self) -> None:
        p = self.palette
        try:
            theme = p.rich_theme()
            self.register_theme(theme)
            self.theme = theme.name
        except Exception:
            pass

        self.query_one(ChatPane).apply_palette(p)
        try:
            self.query_one(LoopRail).apply_palette(p)
            self.query_one(TelemetryRail).apply_palette(p)
        except Exception:
            pass
        try:
            self.query_one(ZimPane).apply_palette(p)
        except Exception:
            pass

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

    # ---- /team ------------------------------------------------------------
    def _cmd_team(self, args) -> None:
        """/team <task> — plan, fan out, synthesise. Long-running, so it runs
        as a worker like _cmd_compact rather than inline."""
        task = " ".join(args).strip() if args else ""
        if not task:
            self._sys_line("usage: /team <task>", warn=True)
            return
        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return
        self._run_team(task)

    @work(exclusive=True)
    async def _run_team(self, task: str) -> None:
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)

        self.busy = True
        self._cancelled = False
        self._set_rain(False)
        self._team_tokens = (0, 0, 0.0)   # fresh run, fresh team totals

        # name -> (index, colour). Built from the roster so a worker keeps one
        # colour for its whole run, and every one of its lines is tagged with it.
        colors: dict[str, str] = {}
        counter = {"running": 0, "total": 0}

        async def on_event(ev: dict) -> None:
            if self._cancelled:
                return
            etype = ev.get("type")

            if etype == "team_plan":
                for i, w in enumerate(ev.get("workers") or []):
                    colors[w["name"]] = agent_color(i, p)
                counter["total"] = len(ev.get("workers") or [])
                if ev.get("summary"):
                    self._sys_line(ev["summary"])
                if not ev.get("workers"):
                    self._sys_line(
                        "no team needed — running it directly" if ev.get("parsed")
                        else "could not read a roster — running it directly",
                        warn=True,
                    )
                else:
                    names = ", ".join(w["name"] for w in ev["workers"])
                    self._sys_line(f"launching {len(ev['workers'])} agents — {names}", ok=True)

            elif etype == "team_start":
                counter["running"] += 1
                col = colors.get(ev["name"], p.accent)
                brief = ev.get("brief", "")
                if len(brief) > 96:
                    brief = brief[:95] + "…"
                chat.write_block(R.agent_event_line(ev["name"], col, "▸", brief, p))
                bar.set_activity(
                    f"team {counter['running']}/{counter['total'] or '?'}", busy=True)

            elif etype == "team_text":
                col = colors.get(ev["name"], p.accent)
                chat.write_block(R.agent_line(ev["name"], ev["text"], col, p))

            elif etype == "team_tool":
                col = colors.get(ev["name"], p.accent)
                chat.write_block(R.agent_event_line(
                    ev["name"], col, ev.get("tool", "?"),
                    tools_mod.summarise_call(ev.get("tool", ""), ev.get("args") or {}, self.cfg),
                    p))

            elif etype == "team_result":
                col = colors.get(ev["name"], p.accent)
                output = (ev.get("output") or "").strip().splitlines()
                first = output[0][:96] if output else "(no output)"
                if len(output) > 1:
                    first += f"  … +{len(output) - 1} lines"
                chat.write_block(R.agent_event_line(
                    ev["name"], col, "✔" if ev.get("ok") else "✘", first, p,
                    ok=bool(ev.get("ok"))))

            elif etype == "team_done":
                counter["running"] = max(0, counter["running"] - 1)
                col = colors.get(ev["name"], p.accent)
                if ev.get("ok"):
                    detail = f"done · ${ev.get('cost', 0.0):.4f}"
                else:
                    detail = f"failed — {ev.get('error', 'unknown')}"
                chat.write_block(R.agent_event_line(
                    ev["name"], col, "■", detail, p, ok=bool(ev.get("ok"))))
                bar.set_activity(
                    f"team {counter['running']}/{counter['total'] or '?'}", busy=True)

            elif etype == "team_end":
                # Bank the workers' usage so the synthesis turn's `usage` events
                # add to it rather than replacing it.
                self._team_tokens = (
                    ev.get("input") or 0,
                    ev.get("output") or 0,
                    ev.get("cost") or 0.0,
                )
                bar.input_tokens = self.agent.session_input_tokens + self._team_tokens[0]
                bar.output_tokens = self.agent.session_output_tokens + self._team_tokens[1]
                bar.cost = self.agent.session_cost + self._team_tokens[2]
                bar.render_bar()

        spinner = asyncio.create_task(self._verb_spinner())
        chat.write_block(R.turn_marker(self.agent.turn_count + 1, p))
        chat.write_block(R.user_prompt_block(f"⚑ team: {task}", p))
        self._sys_line("analysing the task…")

        try:
            results = await team_mod.run_team(
                self.cfg, task,
                on_event=on_event,
                agent_factory=self._team_agent_factory,
            )
            if self._cancelled:
                return

            if not results:
                # No roster — do the task the ordinary way, on the main agent.
                # The planner cost a team, not the task.
                bar.set_activity("working", busy=True)
                async for ev in self.agent.run_turn(task):
                    if self._cancelled:
                        break
                    await self._handle_event(ev)
                return

            # ---- synthesis: the main brain folds the reports into one answer.
            good = sum(1 for r in results if r.ok)
            self._sys_line(f"synthesising {good}/{len(results)} results…")
            bar.set_activity("synthesising", busy=True)
            prompt = team_mod.synthesis_prompt(task, results)
            async for ev in self.agent.run_turn(prompt):
                if self._cancelled:
                    break
                await self._handle_event(ev)

        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            chat.write_block(R.error_block(f"team failed: {type(e).__name__}: {e}", p))
        finally:
            spinner.cancel()
            self.busy = False
            chat.clear_stream()
            chat.card_finish()
            self._stream_buf = ""
            bar.set_activity("idle", busy=False)
            bar.turns = self.agent.turn_count
            bar.model = self.cfg.model
            bar.render_bar()
            self._sync_rails()
            self._set_rain(self.rain_on)
            if self._cancelled:
                self.agent.cancel_turn()
                self._sys_line("team interrupted", warn=True)
            self.query_one("#input", Input).focus()

    # ---- osint ------------------------------------------------------------
    def _cmd_osint(self, args) -> None:
        """/osint [kind] <target> — recon on a declared target.

        A router, not an engine: pick the kind, validate the target, open a
        case directory, then hand that kind's playbook to the main agent as a
        single turn. The playbook is long, so the transcript shows the short
        `/osint email <target>` label while the agent receives the briefing
        behind it — see _run_turn's `display`.
        """
        p = self.palette

        # Bare `/osint` → the menu, so the surface is discoverable without the
        # README.
        if not args:
            self._osint_menu()
            return

        kind_name = args[0].lower()
        kind = osint_mod.OSINT_KINDS.get(kind_name)
        if kind is None:
            self._sys_line(
                f"unknown osint kind: {kind_name} — try "
                + " | ".join(osint_mod.KIND_ORDER),
                warn=True,
            )
            return

        target = " ".join(args[1:]).strip()

        if not kind.built:
            self._sys_line(
                f"/osint {kind.name} is not built yet — email is the one that "
                "works today",
                warn=True,
            )
            return

        if not target:
            self._sys_line(f"usage: /osint {kind.name} <{kind.target_label}>", warn=True)
            return

        ok, err = osint_mod.validate(kind, target)
        if not ok:
            self._sys_line(
                f"usage: /osint {kind.name} <{kind.target_label}>  ({err})", warn=True
            )
            return

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        case = osint_mod.case_dir(self.cfg, kind, target)
        prompt = osint_mod.build_prompt(kind, target, case)
        label = osint_mod.normalise(kind, target)

        t = Text()
        t.append("  ◈ CASE      ", style=f"bold {p.accent}")
        t.append(f"{kind.name} · {label}\n", style=p.primary)
        t.append("  ◈ EVIDENCE  ", style=f"bold {p.accent}")
        t.append(str(case) + "\n", style=p.dim)
        t.append("  ◈ NOTICE    ", style=f"bold {p.amber}")
        t.append(
            "authorised use only — your own footprint, a consented audit, "
            "or a declared engagement\n",
            style=p.dim,
        )
        self.query_one(ChatPane).write_block(t)

        self._run_turn(
            prompt,
            display=f"⚑ /osint {kind.name} {label}",
            osint_case=case,
        )

    def _finalise_osint(self, case: Path) -> None:
        """Archive the run's report and clear the case directory.

        Reports are the one artefact worth keeping, so they move to
        ``<state_dir>/reports/`` and everything else — the clones, the raw API
        responses, the avatars — is deleted. A run that produced no report
        (failed, or interrupted before the write) is left untouched so the
        operator can see what it managed to collect.
        """
        report, cleaned = osint_mod.finalise_case(self.cfg, case)
        if report is not None:
            self._sys_line(f"report archived: {report}", ok=True)
            if cleaned:
                self._sys_line("case directory cleared (evidence discarded)")
        else:
            self._sys_line(
                f"no report found — case directory kept for inspection: {case}",
                warn=True,
            )

    def _osint_menu(self) -> None:
        """The kind list, shown by a bare `/osint`."""
        p = self.palette
        t = Text()
        t.append("  OSINT\n\n", style=f"bold {p.accent}")
        for name in osint_mod.KIND_ORDER:
            kind = osint_mod.OSINT_KINDS[name]
            built = kind.built
            t.append(f"  {'◉' if built else '○'} {name:<10}",
                     style=f"bold {p.accent}" if built else p.dim)
            t.append(kind.blurb + "\n", style=p.primary if built else p.dim)
        t.append("\n  usage: ", style=p.dim)
        t.append("/osint <kind> <target>\n", style=p.primary)
        # Read from cfg rather than hard-coding ~/.zimzilla: state_dir is
        # configurable, and a menu that names the wrong directory is worse
        # than one that names none.
        t.append("  evidence lands in ", style=p.dim)
        t.append(f"{Path(self.cfg.state_dir) / 'osint'}/<kind>-<target>-<stamp>/\n",
                 style=p.primary)
        self.query_one(ChatPane).write_block(t)

    # ---- /phish -----------------------------------------------------------
    def _cmd_phish(self, args) -> None:
        """/phish <host> — clone, serve, tunnel, harvest.

        Subcommands: ``stop`` tears the campaign down, ``status`` reprints
        the live URLs. A second ``/phish <host>`` replaces the running one.
        """
        if not args:
            self._phish_usage()
            return
        verb = args[0].lower()
        if verb in {"stop", "off", "kill"}:
            self._phish_stop()
            return
        if verb in {"status", "info"}:
            self._phish_status()
            return

        target = " ".join(args).strip()
        ok, err = phish_mod.validate_target(target)
        if not ok:
            self._sys_line(f"usage: /phish <host>  ({err})", warn=True)
            return

        host = phish_mod.normalise_target(target)
        allowed, reason = self.agent.scope.allows(host)
        if not allowed:
            self._sys_line(f"⛔ BLOCKED — {host} — {reason}", warn=True)
            return

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        pane = self._zim_pane()
        if pane is not None:
            pane.show_campaign(host)

        p = self.palette
        t = Text()
        t.append("  ◈ PHISH     ", style=f"bold {p.accent}")
        t.append(f"{host}\n", style=p.primary)
        t.append("  ◈ NOTICE    ", style=f"bold {p.amber}")
        t.append(
            "authorised use only — your own property, a consented audit, "
            "or a declared engagement\n",
            style=p.dim,
        )
        self.query_one(ChatPane).write_block(t)
        self._launch_phish(target)

    def _phish_usage(self) -> None:
        p = self.palette
        t = Text()
        t.append("  PHISH\n\n", style=f"bold {p.accent}")
        t.append("  /phish <host>     ", style=f"bold {p.primary}")
        t.append("clone the login page, serve it, tunnel it\n", style=p.dim)
        t.append("  /phish status     ", style=f"bold {p.primary}")
        t.append("show the live URLs and capture counts\n", style=p.dim)
        t.append("  /phish stop       ", style=f"bold {p.primary}")
        t.append("tear the campaign down\n", style=p.dim)
        t.append("\n  hits and credentials stream into ", style=p.dim)
        t.append("zim-pane", style=p.primary)
        t.append(" on the right.\n", style=p.dim)
        t.append("  campaigns land in ", style=p.dim)
        t.append(f"{Path(self.cfg.state_dir) / 'phish'}/<host>-<stamp>/\n",
                 style=p.primary)
        self.query_one(ChatPane).write_block(t)

    def _phish_status(self) -> None:
        camp = phish_mod.active()
        if camp is None or not camp.alive:
            self._sys_line("no phishing campaign is running", warn=True)
            return
        p = self.palette
        t = Text()
        t.append("  ◈ PHISH     ", style=f"bold {p.accent}")
        t.append(f"{camp.host}  ({'LIVE' if camp.alive else 'stopped'})\n",
                 style=p.primary)
        t.append("  ◈ LOCAL     ", style=f"bold {p.accent}")
        t.append(camp.local_url + "\n", style=p.primary)
        t.append("  ◈ PUBLIC    ", style=f"bold {p.accent}")
        t.append((camp.public_url or "none — LAN only") + "\n",
                 style=p.primary if camp.public_url else p.amber)
        t.append("  ◈ CLONE     ", style=f"bold {p.accent}")
        t.append(camp.clone_note + "\n", style=p.dim)
        t.append("  ◈ CAPTURED  ", style=f"bold {p.accent}")
        t.append(f"{len(camp.creds)} cred  ·  {len(camp.hits)} hit\n",
                 style=p.primary)
        self.query_one(ChatPane).write_block(t)

    def _phish_stop(self) -> None:
        camp = phish_mod.stop()
        pane = self._zim_pane()
        if pane is not None:
            pane.note({"kind": "stopped"})
        if camp is None:
            self._sys_line("no phishing campaign is running", warn=True)
            return
        self._sys_line(
            f"phish stopped — {camp.host}  ·  {len(camp.creds)} cred  ·  "
            f"{len(camp.hits)} hit  ·  {camp.directory}",
            ok=True,
        )

    @work(exclusive=True)
    async def _launch_phish(self, target: str) -> None:
        """Build the campaign off the UI thread: fetch + bind + tunnel."""
        self.busy = True
        bar = self.query_one(StatusBar)
        bar.set_activity("phish: cloning", busy=True)
        try:
            camp = await asyncio.to_thread(
                phish_mod.start,
                self.cfg,
                target,
                self._on_phish_event,
                tunnel=True,
            )
        except Exception as e:  # noqa: BLE001
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self._sys_line(f"phish failed: {type(e).__name__}: {e}", warn=True)
            pane = self._zim_pane()
            if pane is not None:
                pane.hide()
            self.query_one("#input", Input).focus()
            return

        self.busy = False
        bar.set_activity("idle", busy=False)
        bar.render_bar()

        p = self.palette
        t = Text()
        t.append("  ◈ LOCAL     ", style=f"bold {p.accent}")
        t.append(camp.local_url + "\n", style=p.primary)
        t.append("  ◈ PUBLIC    ", style=f"bold {p.accent}")
        if camp.public_url:
            t.append(camp.public_url, style=f"bold {p.accent}")
            if camp.tunnel.tool:
                t.append(f"  via {camp.tunnel.tool}", style=p.dim)
            t.append("\n")
        else:
            t.append("none — LAN only", style=p.amber)
            if camp.tunnel.error:
                t.append(f"  ({camp.tunnel.error})", style=p.dim)
            t.append("\n")
        t.append("  ◈ CLONE     ", style=f"bold {p.accent}")
        t.append(camp.clone_note + "\n", style=p.dim)
        t.append("  ◈ LOGS      ", style=f"bold {p.accent}")
        t.append(str(camp.directory) + "\n", style=p.dim)
        t.append("  · watch zim-pane for hits and credentials\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)
        self.query_one("#input", Input).focus()

    def _on_phish_event(self, event: dict) -> None:
        """Called from the HTTP thread. Queue only — never touch widgets."""
        with self._phish_q_lock:
            self._phish_events.append(event)

    def _drain_phish(self) -> None:
        with self._phish_q_lock:
            batch = list(self._phish_events)
            self._phish_events.clear()
        if not batch:
            return
        pane = self._zim_pane()
        if pane is None:
            return
        for ev in batch:
            pane.note(ev)
            if ev.get("kind") == "cred":
                user = ev.get("user") or "?"
                self._sys_line(f"phish cred  {user}  ·  {self._mask(ev.get('password') or '')}")

    @staticmethod
    def _mask(secret: str) -> str:
        if not secret:
            return "(empty)"
        if len(secret) <= 2:
            return "•" * len(secret)
        return secret[0] + "•" * (len(secret) - 2) + secret[-1]

    def _zim_pane(self) -> ZimPane | None:
        try:
            return self.query_one(ZimPane)
        except Exception:
            return None

    # ---- /bug-hunt --------------------------------------------------------
    def _cmd_bug_hunt(self, args) -> None:
        """/bug-hunt <target> — recon, then waves of 10 agents until stopped.

        The campaign is a loop, not a turn: recon maps the target, a read-only
        planner writes ten briefs, all ten run at once, their findings feed the
        next planner, and it repeats until /stop-hunt. See zimzilla/hunt.py.
        """
        if not args:
            self._hunt_usage()
            return

        target = " ".join(args).strip()

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        # Warn but proceed. AGENTS.md treats any target the operator declares as
        # authorised, so an unarmed session is a notice, not a refusal — but the
        # out-of-scope guard still hard-blocks mid-hunt, and an operator who
        # does not know the session is unarmed would read that as a failure.
        allowed, reason = self.agent.scope.allows(target)
        p = self.palette
        if not allowed:
            self._sys_line(f"⚠ not in allow.yaml — {reason}", warn=True)
            self._sys_line("hunting anyway; out-of-scope hosts stay hard-blocked")

        directory = hunt_mod.case_dir(self.cfg, target)

        t = Text()
        t.append("  ◈ HUNT      ", style=f"bold {p.accent}")
        t.append(f"{target}\n", style=f"bold {p.primary}")
        t.append("  ◈ WAVE      ", style=f"bold {p.accent}")
        t.append(f"{hunt_mod.HUNT_WAVE_SIZE} agents, all concurrent\n", style=p.primary)
        t.append("  ◈ EVIDENCE  ", style=f"bold {p.accent}")
        t.append(str(directory) + "\n", style=p.dim)
        t.append("  ◈ NOTICE    ", style=f"bold {p.amber}")
        t.append(
            "authorised use only — your own asset, a consented audit, "
            "or a declared engagement\n",
            style=p.dim,
        )
        t.append("  · ", style=p.dim)
        t.append("/stop-hunt", style=p.primary)
        t.append(" ends the campaign   ·   ", style=p.dim)
        t.append("/summary-hunt", style=p.primary)
        t.append(" writes the report   ·   ", style=p.dim)
        t.append("/findings", style=p.primary)
        t.append(" shows the tracker\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

        pane = self._zim_pane()
        if pane is not None:
            pane.show_hunt(target, hunt_mod.HUNT_WAVE_SIZE)

        self._run_hunt(target)

    def _hunt_usage(self) -> None:
        p = self.palette
        t = Text()
        t.append("  BUG HUNT\n\n", style=f"bold {p.accent}")
        t.append("  /bug-hunt <target>   ", style=f"bold {p.primary}")
        t.append("recon, then waves of 10 agents until you stop it\n", style=p.dim)
        t.append("  /stop-hunt           ", style=f"bold {p.primary}")
        t.append("end the campaign and print what it found\n", style=p.dim)
        t.append("  /summary-hunt        ", style=f"bold {p.primary}")
        t.append("write the closing report (works mid-hunt)\n", style=p.dim)
        t.append("  /findings            ", style=f"bold {p.primary}")
        t.append("the tracker — every bug found, worst first\n", style=p.dim)
        t.append("\n  Each wave: the planner reads what every earlier wave found and\n",
                 style=p.dim)
        t.append("  aims the next ten somewhere new. Run it under ", style=p.dim)
        t.append("/mode zim", style=p.primary)
        t.append(" so the\n  agents follow your AGENTS.md doctrine.\n", style=p.dim)
        t.append("\n  evidence lands in ", style=p.dim)
        t.append(f"{Path(self.cfg.state_dir) / 'hunts'}/<target>-<stamp>/\n",
                 style=p.primary)
        self.query_one(ChatPane).write_block(t)

    def _cmd_stop_hunt(self, args) -> None:
        """/stop-hunt — end the campaign. Works while the harness is busy."""
        run = self._hunt_run
        if run is None:
            self._sys_line("no hunt is running", warn=True)
            return
        self._hunt_stop.set()
        self._sys_line("stop requested — cancelling the wave…", warn=True)

    def _cmd_summary_hunt(self, args) -> None:
        """/summary-hunt — the closing report.

        Mid-hunt this only *queues* the summary: it runs at the next wave
        boundary, because a second turn streaming into the same transcript
        would corrupt the one already running. With no hunt live, it writes the
        report straight from the archived findings of the most recent campaign.
        """
        if self._hunt_run is not None:
            self._hunt_summary_queued = True
            self._sys_line(
                f"summary queued — it runs at the end of wave "
                f"{self._hunt_run.wave}", ok=True,
            )
            return

        latest = self._latest_hunt_dir()
        if latest is None:
            self._sys_line("no hunt has run in this session yet", warn=True)
            return
        run = hunt_mod.load_run(latest)
        if run is None:
            self._sys_line(f"no findings archived in {latest}", warn=True)
            return
        self._run_summary(run)

    def _cmd_findings(self, args) -> None:
        """/findings — the tracker, as its own window.

        Mid-hunt it opens the live campaign's findings; with no hunt running it
        reads the most recent campaign off disk, the same way `/summary-hunt`
        does, so the tracker is still answerable after the campaign has ended.
        Opening it again closes it, so the command toggles.
        """
        panel = self._findings_panel
        if panel is not None:
            self._close_findings()
            return

        if self._hunt_run is not None:
            run = self._hunt_run
            target, live = run.target, True
        else:
            latest = self._latest_hunt_dir()
            run = hunt_mod.load_run(latest) if latest is not None else None
            if run is None:
                self._sys_line(
                    "no findings yet — run /bug-hunt, or /findings after one "
                    "has finished", warn=True)
                return
            target, live = run.target, False

        # The panel reads finding *dicts*, the same shape the hunt events carry,
        # so one renderer serves both the panel and the transcript line.
        findings = [hunt_mod._finding_dict(f) for f in run.ranked()]
        screen = FindingsPanel(self.palette, target=target, live=live)
        screen.set_findings(findings)
        self._findings_panel = screen
        try:
            self.push_screen(screen, self._findings_closed)
        except Exception:
            self._findings_panel = None

    def _findings_closed(self, _result=None) -> None:
        self._findings_panel = None

    def _refresh_findings(self) -> None:
        """Repaint the open tracker from the live run, if there is one.

        Reads the run rather than the event so the panel and the transcript
        cannot disagree about what was found, and a duplicate the tracker
        dropped stays dropped.
        """
        panel = self._findings_panel
        run = self._hunt_run
        if panel is None or run is None:
            return
        try:
            panel.set_findings([hunt_mod._finding_dict(f) for f in run.ranked()])
        except Exception:
            pass

    def _close_findings(self) -> None:
        panel = self._findings_panel
        self._findings_panel = None
        if panel is not None:
            try:
                panel.dismiss(None)
            except Exception:
                pass

    def _latest_hunt_dir(self) -> Path | None:
        """The most recent campaign directory, or None."""
        root = Path(self.cfg.state_dir) / "hunts"
        if not root.is_dir():
            return None
        dirs = [d for d in root.iterdir() if d.is_dir()]
        if not dirs:
            return None
        return max(dirs, key=lambda d: d.stat().st_mtime)

    @work(exclusive=True)
    async def _run_summary(self, run) -> None:
        """Write a closing report for a finished campaign, off the UI thread."""
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)
        self.busy = True
        bar.set_activity("writing the hunt report", busy=True)
        chat.write_block(R.turn_marker(self.agent.turn_count + 1, p))
        chat.write_block(R.user_prompt_block("⚑ /summary-hunt", p))
        try:
            text = ""
            async for ev in self.agent.run_turn(hunt_mod.summary_prompt(run)):
                if ev.get("type") == "text_delta":
                    text += ev.get("text", "")
                await self._handle_event(ev)
            path = hunt_mod.write_summary(self.cfg, run, text)
            self._sys_line(f"report archived: {path}", ok=True)
        except Exception as e:  # noqa: BLE001
            chat.write_block(R.error_block(
                f"summary failed: {type(e).__name__}: {e}", p))
        finally:
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self._sync_rails()
            self.query_one("#input", Input).focus()

    @work(exclusive=True)
    async def _run_hunt(self, target: str) -> None:
        """The campaign worker: recon, then waves until /stop-hunt.

        Shaped like _run_team — busy flag, an on_event sink, a spinner, and a
        finally that restores idle state — but the loop inside lives in
        zimzilla/hunt.py so it can be tested without a terminal.
        """
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)
        pane = self._zim_pane()

        self.busy = True
        self._cancelled = False
        self._set_rain(False)
        self._hunt_stop = asyncio.Event()
        self._hunt_summary_queued = False
        self._hunt_tokens = (0, 0, 0.0)
        # Publish the run *before* the loop starts, not after it returns. This
        # is what `/stop-hunt` and `/summary-hunt` read, and the loop does not
        # return until the campaign is over — so publishing it at the end, as
        # this once did, left both commands looking at None for the entire hunt
        # and quietly refusing to do anything. The run object is handed to
        # run_hunt rather than built there so there is exactly one of it.
        self._hunt_run = hunt_mod.HuntRun(
            target=target, directory=hunt_mod.case_dir(self.cfg, target))

        counter = {"running": 0, "total": 0, "wave": 0, "done": 0}
        #: name -> colour, so a tool line, its prose and its result panel all
        #: agree on which agent they belong to. Ten interleaved agents are
        #: unreadable without that. Keyed by name because the result event
        #: carries the name and not the index.
        colors: dict[str, str] = {}

        # Light the loop rail's HACK node for the whole campaign. A hunt is not
        # a turn, so nothing else sets a stage for it — without this the rail
        # sat on IDLE from recon to the closing report, which reads as an idle
        # harness while ten agents are attacking the target.
        loop = self._loop_rail()
        if loop is not None:
            loop.set_hunt(wave=0)
            loop.set_stage("hacking")
        #: The live recon window, while recon is running. Held here so the
        #: worker can pop exactly the screen it pushed, and so a stop mid-recon
        #: can take it down from the `finally`.
        overlay: dict = {"screen": None}

        async def on_event(ev: dict) -> None:
            if self._cancelled:
                return
            etype = ev.get("type")

            if pane is not None:
                pane.note_hunt(ev)

            # Recon narrates into its own window. Every recon event is consumed
            # here and returns — nothing recon does belongs in the transcript,
            # which is where the waves go.
            if etype in ("hunt_recon_tool", "hunt_recon_text"):
                screen = overlay["screen"]
                if screen is not None:
                    try:
                        screen.note(ev)
                    except Exception:
                        pass
                return

            if etype == "hunt_recon_start":
                self._sys_line("recon — mapping the target…")
                bar.set_activity("hunt: recon", busy=True)
                # can_focus is False, so this does not steal the prompt: the
                # operator can still type /stop-hunt while recon runs.
                try:
                    screen = ReconOverlay(ev.get("target", target), p, self.cfg)
                    overlay["screen"] = screen
                    self.push_screen(screen)
                except Exception:
                    overlay["screen"] = None

            elif etype == "hunt_recon_done":
                self._close_recon(overlay)
                text = (ev.get("text") or "").strip()
                if text:
                    chat.write_block(R.agent_line("recon", text, p.accent, p))
                self._sys_line("recon done — planning wave 1", ok=True)

            elif etype == "hunt_wave_start":
                counter["wave"] = ev.get("wave", 0)
                # `size` is the concurrency cap the planner was *asked* for, not
                # the roster it returned, so it is the bar's denominator but not
                # the agent count — that only arrives with the plan, below.
                counter["total"] = ev.get("size", 0)
                counter["running"] = 0
                counter["done"] = 0
                counter["agents"] = 0
                if loop is not None:
                    loop.set_hunt(wave=counter["wave"], agents=0, done=0)
                chat.write_block(R.turn_marker(
                    self.agent.turn_count + counter["wave"], p))
                chat.write_block(R.user_prompt_block(
                    f"⚑ wave {counter['wave']} · {counter['total']} agents", p))

            elif etype == "hunt_plan":
                if ev.get("fell_back"):
                    self._sys_line(
                        "planner reply unreadable — using the fixed vector matrix",
                        warn=True)
                    # Show what it actually said. Without this the operator
                    # cannot tell a prose-wrapped reply from an empty turn from
                    # a prompt that needs rewriting.
                    raw = (ev.get("raw") or "").strip()
                    if raw:
                        head = raw[:400] + ("…" if len(raw) > 400 else "")
                        chat.write_block(R.agent_line("planner", head, p.amber, p))
                    else:
                        self._sys_line("  (the planner returned nothing)", warn=True)
                if ev.get("summary"):
                    self._sys_line(ev["summary"])
                # The roster is the only place the real agent count appears.
                counter["agents"] = len(ev.get("workers") or [])
                if loop is not None:
                    loop.set_hunt(wave=counter["wave"],
                                  agents=counter["agents"], done=0)
                names = ", ".join(w["name"] for w in ev.get("workers") or [])
                if names:
                    self._sys_line(f"launching {len(ev['workers'])} — {names}", ok=True)

            elif etype == "hunt_agent_start":
                counter["running"] += 1
                color = agent_color(ev.get("index", 0), p)
                colors[ev.get("name", "")] = color
                if loop is not None:
                    loop.set_hunt(wave=counter["wave"],
                                  agents=counter["agents"],
                                  done=counter["done"])
                brief = ev.get("brief", "")
                if len(brief) > 96:
                    brief = brief[:95] + "…"
                chat.write_block(R.agent_event_line(
                    ev["name"], color, "▸", brief, p))
                bar.set_activity(
                    f"hunt w{counter['wave']} {counter['running']}/{counter['total']}",
                    busy=True)

            elif etype == "hunt_agent_tool":
                chat.write_block(R.agent_event_line(
                    ev["name"], colors.get(ev.get("name", ""), p.dim),
                    ev.get("tool", "?"),
                    tools_mod.summarise_call(
                        ev.get("tool", ""), ev.get("args") or {}, self.cfg), p))

            elif etype == "hunt_agent_text":
                # The worker's own reasoning. Dropped before, which meant the
                # transcript showed commands with no explanation of why they
                # were run.
                chat.write_block(R.agent_line(
                    ev["name"], ev.get("text", ""), p.dim, p))

            elif etype == "hunt_agent_result":
                # The other half of the call above: what the command actually
                # returned. Rendered as the same panel a normal turn uses, so
                # the operator sees the real command and the real response.
                if ev.get("blocked"):
                    chat.write_block(R.blocked_block(ev.get("output", ""), p))
                else:
                    chat.write_block(R.hunt_tool_panel(
                        ev.get("tool", "?"), ev.get("args") or {},
                        ev.get("output", ""), p,
                        agent=ev.get("name", ""),
                        color=colors.get(ev.get("name", ""), ""),
                        is_error=not ev.get("ok"),
                        meta=ev.get("meta") or {}))

            elif etype == "hunt_agent_done":
                counter["running"] = max(0, counter["running"] - 1)
                counter["done"] += 1
                if loop is not None:
                    loop.set_hunt(wave=counter["wave"],
                                  agents=counter["agents"],
                                  done=counter["done"])
                if not ev.get("ok"):
                    self._sys_line(
                        f"  {ev['name']} failed — {ev.get('error', 'unknown')}",
                        warn=True)

            elif etype == "hunt_finding":
                f = ev.get("finding") or {}
                sev = str(f.get("severity") or "info").upper()
                chat.write_block(R.agent_event_line(
                    f.get("agent", "?"), p.accent,
                    "◆" if ev.get("saved") else "·",
                    f"[{sev}] {f.get('title', '')}"
                    + (f"  ·  saved" if ev.get("saved") else ""),
                    p, ok=bool(ev.get("saved"))))
                # Keep the tracker in step with the transcript. It reads the
                # run's own list, so a finding the tracker already had is not
                # added twice.
                self._refresh_findings()

            elif etype == "hunt_wave_end":
                # Every agent has reported, so `done` is already the roster
                # size. Leave it — forcing it to the concurrency cap would show
                # a wave that planned three agents as 10/10.
                if loop is not None:
                    loop.set_hunt(wave=counter["wave"],
                                  agents=counter["agents"],
                                  done=counter["done"])
                self._hunt_tokens = (
                    self._hunt_tokens[0] + (ev.get("input") or 0),
                    self._hunt_tokens[1] + (ev.get("output") or 0),
                    self._hunt_tokens[2] + (ev.get("cost") or 0.0),
                )
                self._sys_line(
                    f"wave {ev['wave']} done — {ev['ok']}/{ev['agents']} ok, "
                    f"{ev['found']} finding(s)  ·  ${ev['cost']:.4f}",
                    ok=True)

            elif etype == "hunt_summary_done":
                chat.write_block(R.agent_line("summary", ev.get("text", ""), p.accent, p))
                self._sys_line(f"report archived: {ev.get('path')}", ok=True)

            elif etype == "hunt_end":
                self._sys_line(
                    f"hunt ended — {ev['wave']} wave(s), {ev['findings']} finding(s), "
                    f"{ev['saved']} report(s) in {ev['directory']}",
                    ok=True)
                # The campaign is over; if the tracker is open it becomes a
                # record of the run rather than a live view.
                if self._findings_panel is not None:
                    self._findings_panel.live = False
                    self._refresh_findings()

        spinner = asyncio.create_task(self._verb_spinner())

        try:
            await hunt_mod.run_hunt(
                self.cfg, target,
                on_event=on_event,
                agent_factory=self._team_agent_factory,
                stop=self._hunt_stop,
                summary_flag=lambda: self._hunt_summary_queued,
                run=self._hunt_run,
            )
            # A summary asked for but never delivered — the hunt was stopped
            # between the request and a wave boundary. Offer it now rather than
            # silently dropping it.
            if self._hunt_summary_queued:
                self._hunt_summary_queued = False
                self._sys_line("summary was queued but never ran — use /summary-hunt")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            chat.write_block(R.error_block(
                f"hunt failed: {type(e).__name__}: {e}", p))
        finally:
            spinner.cancel()
            # A stop or a crash during recon must not leave the window up over
            # a dead campaign.
            self._close_recon(overlay)
            self.busy = False
            self._hunt_run = None
            chat.clear_stream()
            chat.card_finish()
            self._stream_buf = ""
            bar.set_activity("idle", busy=False)
            bar.input_tokens = self.agent.session_input_tokens + self._hunt_tokens[0]
            bar.output_tokens = self.agent.session_output_tokens + self._hunt_tokens[1]
            bar.cost = self.agent.session_cost + self._hunt_tokens[2]
            bar.turns = self.agent.turn_count
            bar.model = self.cfg.model
            bar.render_bar()
            # Hand the rail back to the main agent. Three separate calls
            # because each owns one thing: set_hunt drops the campaign
            # counters, set_stage clears HACK, and _sync_rails pushes the
            # session totals back in. Nothing else does the second one — the
            # stage is set by the turn runner, and the hunt is not a turn, so
            # without it the rail stayed lit on HACK after the campaign ended.
            if loop is not None:
                loop.set_hunt(None)
                loop.set_stage("idle")
            self._sync_rails()
            self._set_rain(self.rain_on)
            self.query_one("#input", Input).focus()

    def _close_recon(self, overlay: dict) -> None:
        """Take the recon window down, if it is still up.

        Popped by identity rather than with a bare ``pop_screen()``: between the
        push and this call the operator can open the command palette or hit a
        permission modal, and a blind pop would dismiss *that* instead — leaving
        the recon window stranded over the shell. Idempotent, because both the
        recon-done event and the worker's ``finally`` call it.

        The pop is deferred a tick when the screen has not mounted yet. A fast
        target finishes recon before its window is on screen, and popping in the
        same tick it was pushed tears the tree down ahead of the queued Mount
        message — the overlay then raises on a child that is already gone. One
        tick lets it come up and go down cleanly, and the operator never sees it
        because nothing paints in between.
        """
        screen = overlay.get("screen")
        overlay["screen"] = None
        if screen is None:
            return
        try:
            if not screen.is_mounted:
                self.call_after_refresh(self._close_recon_screen, screen)
            elif self.screen is screen:
                self.pop_screen()
        except Exception:
            pass

    def _close_recon_screen(self, screen) -> None:
        """The deferred half of ``_close_recon`` — pop it if it is still up."""
        try:
            if self.screen is screen:
                self.pop_screen()
        except Exception:
            pass

    def _team_agent_factory(self, cfg: Config, **kw) -> Agent:
        """Build a team Agent. The seam tests replace to stub the model.

        Every worker gets its own Agent and its own Config copy — see
        zimzilla/team.py for why sharing one would clobber the scope guard.
        """
        return Agent(cfg, permission_handler=self.request_permission, **kw)

    # ---- command palette --------------------------------------------------
    def action_palette(self) -> None:
        """Ctrl+K: every action, fuzzy-searchable."""
        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return
        self.push_screen(CommandPalette(self._palette_entries(), self.palette),
                         self._palette_done)

    def _palette_entries(self) -> list[PaletteEntry]:
        """Build the action list. Each entry closes over what it needs."""
        entries: list[PaletteEntry] = []

        # Commands — derived from the same table _handle_command dispatches on,
        # so the palette cannot drift out of sync with the slash commands.
        commands = [
            ("/help", "command reference", "help"),
            ("/mode", "switch mode", "mode"),
            ("/model", "show or switch the model", "model"),
            ("/cost", "session token and cost breakdown", "cost"),
            ("/scope", "scope guard status", "scope"),
            ("/rain", "toggle the matrix rain", "rain"),
            ("/theme", "switch palette", "theme"),
            ("/zim-source", "upstream sources and status", "zim-source"),
            ("/zim-tokenharbour", "switch upstream to TokenHarbour", "zim-tokenharbour"),
            ("/tokenharbour-api-setup", "store your TokenHarbour API key",
             "tokenharbour-api-setup"),
            ("/tokenharbour-models", "fetch the live catalog; show what is free",
             "tokenharbour-models"),
            ("/save", "write the session to disk", "save"),
            ("/load", "restore a saved session", "load"),
            ("/compact", "summarise history to free context", "compact"),
            ("/team", "fan the task out across parallel agents", "team"),
            ("/osint", "open-source recon on a target", "osint"),
            ("/phish", "clone a login page and harvest creds", "phish"),
            ("/bug-hunt", "recon, then waves of 10 agents until stopped", "bug-hunt"),
            ("/stop-hunt", "end a running hunt", "stop-hunt"),
            ("/summary-hunt", "write the hunt report", "summary-hunt"),
            ("/findings", "the hunt finding tracker", "findings"),
            ("/clear", "wipe transcript and history", "clear"),
            ("/exit", "leave the harness", "exit"),
        ]
        for title, hint, key in commands:
            entries.append(PaletteEntry(
                key=f"cmd:{key}", title=title, hint=hint, group="command",
                run=(lambda k=key: self._handle_command("/" + k)),
            ))

        # Modes.
        for name, spec in MODES.items():
            entries.append(PaletteEntry(
                key=f"mode:{name}", title=f"mode {name}",
                hint=spec["blurb"], group="mode",
                run=(lambda n=name: self._set_mode(n)),
            ))

        # Themes.
        for name in ("green", "amber", "cyan"):
            entries.append(PaletteEntry(
                key=f"theme:{name}", title=f"theme {name}", group="theme",
                run=(lambda n=name: self._cmd_theme([n])),
            ))

        # Models, capped: the catalog can be long and the palette is a shortcut,
        # not a replacement for /model.
        try:
            models = sources_mod.models_for(self.cfg.base_url) or tuple(KNOWN_MODELS)
        except Exception:
            models = tuple(KNOWN_MODELS)
        for name in models[:24]:
            pin, pout = price_for(name)
            entries.append(PaletteEntry(
                key=f"model:{name}", title=name,
                hint=f"${pin:.2f}/${pout:.2f} per Mtok", group="model",
                run=(lambda n=name: self._cmd_model([n])),
            ))

        # Files, so the palette can also be a "jump to" — the action inserts the
        # @mention into the prompt rather than submitting it.
        try:
            for path in _iter_files(self.cfg.workdir, limit=120)[:120]:
                entries.append(PaletteEntry(
                    key=f"file:{path}", title=path, group="file",
                    run=(lambda pth=path: self._mention_file(pth)),
                ))
        except Exception:
            pass

        return entries

    def _mention_file(self, path: str) -> None:
        """Drop an @mention into the prompt, cursor at the end."""
        inp = self.query_one("#input", Input)
        inp.value = f"@{path} "
        inp.cursor_position = len(inp.value)
        inp.focus()

    def _palette_done(self, entry: PaletteEntry | None) -> None:
        if entry is None or entry.run is None:
            self.query_one("#input", Input).focus()
            return
        # The screen is gone by the time this runs; run the action directly.
        entry.run()
        self.query_one("#input", Input).focus()

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
        try:
            phish_mod.stop()
        except Exception:
            pass
        self.exit()
