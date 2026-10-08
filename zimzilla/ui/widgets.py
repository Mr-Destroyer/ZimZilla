"""Layout widgets: header, chat pane, status bar."""

from __future__ import annotations

import time

from rich.cells import cell_len, get_character_cell_size
from rich.text import Text
from textual import events
from textual.containers import Vertical
from textual.events import Click, MouseDown, MouseMove, MouseUp
from textual.expand_tabs import expand_tabs_inline
from textual.geometry import Offset, clamp
from textual.widgets import Input, Static

from ..theme import Palette
from .rain import RainRichLog

#: A pasted line break is drawn as this glyph. The field is one row tall, so a
#: real ``\n`` in the strip would push the rows below it down the screen; the
#: glyph is one cell wide and keeps the value's character indices 1:1 with the
#: cells they paint into, which is what the cursor and selection math assume.
NEWLINE_GLYPH = "↵"


class PromptInput(Input):
    """The prompt field — a one-row :class:`~textual.widgets.Input` that keeps a
    whole paste.

    Textual's stock ``Input._on_paste`` does ``event.text.splitlines()[0]``: it
    keeps only the *first line* of a paste and silently discards the rest. Any
    prompt copied with a line break in it — a multi-line instruction, a stack
    trace, pasted docs — therefore arrived truncated to its first line.

    This subclass keeps the entire paste. Line endings are normalised to
    ``\\n`` (some terminals bracket-paste CRLF) and stored as real newlines so
    the model receives the text as written; only the *display* folds them to
    :data:`NEWLINE_GLYPH`, because a raw newline inside a one-row field would
    corrupt every row beneath it.
    """

    def _on_paste(self, event: events.Paste) -> None:
        """Insert the full pasted text, not just its first line.

        ``prevent_default`` is what suppresses the base handler — Textual
        dispatches *every* matching handler along the MRO, so stopping the
        event alone would still let ``Input._on_paste`` truncate it after us.
        """
        if event.text:
            selection = self.selection
            if selection.is_empty:
                self.insert_text_at_cursor(event.text)
            else:
                self.replace(event.text, *selection)
        event.prevent_default()
        event.stop()

    def replace(self, text: str, start: int, end: int) -> None:
        """Normalise line endings before the value is written.

        Every path into the value funnels through here — typing, ``insert``,
        and the Ctrl+V ``action_paste`` (which calls ``replace`` directly and
        so never sees ``_on_paste``). Normalising once here means no stray
        ``\\r`` can reach the strip from any of them.
        """
        super().replace(text.replace("\r\n", "\n").replace("\r", "\n"), start, end)

    @property
    def _value(self) -> Text:
        """The value as rendered — newlines shown as a one-cell glyph."""
        if self.password:
            return Text("•" * len(self.value), no_wrap=True, overflow="ignore", end="")
        text = Text(self.value.replace("\n", NEWLINE_GLYPH), no_wrap=True,
                    overflow="ignore", end="")
        if self.highlighter is not None:
            text = self.highlighter(text)
        return text

    def _position_to_cell(self, position: int) -> int:
        """Index → cell offset, counting a newline as the glyph's one cell."""
        return cell_len(expand_tabs_inline(self.value[:position].replace("\n", " "), 4))

    def _cell_offset_to_index(self, offset: int) -> int:
        """Cell offset → index, the inverse of :meth:`_position_to_cell`."""
        cell_offset = 0
        scroll_x, _ = self.scroll_offset
        offset += scroll_x
        for index, char in enumerate(self.value):
            width = 1 if char == "\n" else get_character_cell_size(char)
            if cell_offset <= offset < (cell_offset + width):
                return index
            cell_offset += width
        return clamp(offset, 0, len(self.value))


class HeaderBar(Static):
    """Top bar: model, cwd, scope status, theme name."""

    DEFAULT_CSS = """
    HeaderBar {
        dock: top;
        height: 1;
        padding: 0 1;
    }
    """

    def __init__(self, palette: Palette, model: str, cwd: str, unsafe: bool, theme: str,
                 mode: str = "auto", **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.model = model
        self.cwd = cwd
        self.unsafe = unsafe
        self.theme_name = theme
        self.mode = mode
        self.scope_text = "off"
        self.scope_ok = True

    def set_scope(self, text: str, ok: bool) -> None:
        self.scope_text = text
        self.scope_ok = ok
        self.refresh_content()

    def set_model(self, model: str) -> None:
        self.model = model
        self.refresh_content()

    def set_theme_name(self, name: str) -> None:
        self.theme_name = name
        self.refresh_content()

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.refresh_content()

    def refresh_content(self) -> None:
        p = self.palette
        from ..config import MODES

        m = MODES.get(self.mode, {})
        # Modes marked "loud" (zim, danger, uncensored) are the armed states — shout them.
        mstyle = f"bold white on {p.red}" if m.get("loud") else f"bold {p.accent}"
        sep = ("  │  ", p.dim)

        head = Text()
        head.append("◈ ZIMZILLA", style=f"bold {p.primary}")
        head.append(*sep)
        head.append(f"◆ {m.get('label', self.mode.upper())} ", style=mstyle)
        head.append(*sep)
        head.append("◉ ", style=p.accent)
        head.append(self.model, style=f"bold {p.accent}")

        # Everything after the model is optional, in order of how little it
        # matters. The bar is one row and Textual clips the overflow, so an
        # over-long cwd would silently eat the scope status — the one field
        # that says whether the sandbox is armed. So: build the tail, then
        # drop the least important pieces until the whole thing fits the row
        # we actually have. Measured, not guessed: a fixed width threshold is
        # wrong the moment the cwd is a real project path rather than /tmp.
        # Ordered least-important-LAST, because the fit loop pops from the end:
        # the theme goes first, then the cwd, and the sandbox indicator — which
        # is a security fact, not decoration — goes last.
        tail: list[tuple[str, object]] = [
            ("⛨ sandbox on", p.dim),
            (f"⌂ {self.cwd}", p.primary),
            (f"theme:{self.theme_name}", p.dim),
        ]
        # The sandbox being OFF is not cosmetic — it never gets dropped.
        if self.unsafe:
            tail.insert(0, ("⚠ SANDBOX OFF", f"bold white on {p.red}"))
            tail = [f for f in tail if f[0] != "⛨ sandbox on"]

        # Scope sits between the fixed head and the droppable tail.
        fixed = head.copy()
        fixed.append(*sep)
        fixed.append("⌖ scope ", style=p.dim)
        fixed.append(self.scope_text, style=f"bold {p.accent if self.scope_ok else p.red}")

        limit = self.size.width or 200
        while tail:
            t = fixed.copy()
            for text, style in tail:
                t.append(*sep)
                t.append(text, style=style)
            if t.cell_len <= limit:
                self.update(t)
                return
            tail.pop()  # shed the least important field and try again
        self.update(fixed)

    def on_mount(self) -> None:
        self.refresh_content()

    def on_resize(self) -> None:
        # The bar sheds fields by measured fit, so a resize can change what
        # fits — re-lay it out rather than waiting for the next scope update.
        self.refresh_content()


class ChatPane(Vertical):
    """Scrollable transcript plus the live streaming line."""

    DEFAULT_CSS = """
    ChatPane {
        width: 1fr;
        height: 100%;
    }
    ChatPane #transcript {
        height: 1fr;
        background: transparent;
        scrollbar-size-vertical: 1;
        scrollbar-color: $secondary;
        scrollbar-background: transparent;
    }
    ChatPane #stream {
        height: auto;
        max-height: 60%;
        padding: 0 1;
        background: transparent;
    }
    ChatPane #stream.hidden {
        display: none;
    }
    ChatPane #card {
        height: auto;
        padding: 0 1;
        background: transparent;
    }
    ChatPane #card.hidden {
        display: none;
    }
    """

    def __init__(self, palette: Palette, rain: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.rain_on = rain
        self._cursor_on = True
        #: The tool call currently in flight, or None. Set by card_begin and
        #: cleared by card_finish — this is the whole card lifecycle.
        self._card: dict | None = None

    def compose(self):
        # min_width=1 because RichLog's default of 78 clamps the render width
        # *up* to 78 even when the pane is narrower, which overflows the pane
        # and switches on the horizontal scrollbar. With it lowered, `shrink`
        # does its job and panels fit whatever width they are given — including
        # writes deferred until the size is known.
        yield RainRichLog(self.palette, rain_on=self.rain_on, id="transcript",
                          markup=False, wrap=True, highlight=False, auto_scroll=True,
                          min_width=1)
        stream = Static(id="stream", classes="hidden")
        yield stream
        card = Static(id="card", classes="hidden")
        yield card

    # ---- transcript -------------------------------------------------------
    def write_block(self, renderable) -> None:
        log = self.query_one("#transcript", RainRichLog)
        # `expand` fills the pane's content width, so a panel always fits the
        # width the pane has *now*. RichLog bakes that width into the strip at
        # write time, so an explicit `width=` here would pin the block to the
        # width it was written at and leave it ragged after a resize —
        # RainRichLog re-renders its blocks from the renderable, which is only
        # possible if the width is not baked in up front.
        log.write(renderable, expand=True)
        log.write("")

    def set_rain(self, active: bool) -> None:
        self.rain_on = active
        self.query_one("#transcript", RainRichLog).set_rain(active)

    def apply_palette(self, palette: Palette) -> None:
        self.palette = palette
        log = self.query_one("#transcript", RainRichLog)
        log.palette = palette
        log._canvas.palette = palette
        log.refresh()

    def clear(self) -> None:
        self.query_one("#transcript", RainRichLog).clear()
        self.clear_stream()
        self.card_finish()

    # ---- live tool card ---------------------------------------------------
    def card_begin(self, name: str, args: dict, targets: list[str] | None = None) -> None:
        """Open the live card for a tool that just started.

        Only one card exists at a time: a tool call flushes the previous card
        first, so the slot always belongs to the newest call.
        """
        self._card = {
            "name": name,
            "args": args or {},
            "targets": targets or [],
            "started": time.monotonic(),
            "frame": 0,
        }
        self.card_tick()

    def card_tick(self) -> None:
        """Repaint the running card — spinner frame and elapsed clock."""
        if self._card is None:
            return
        from .renderers import tool_running_panel

        c = self._card
        w = self.query_one("#card", Static)
        w.remove_class("hidden")
        w.update(tool_running_panel(
            c["name"], c["args"], self.palette,
            elapsed=time.monotonic() - c["started"],
            frame=c["frame"],
            targets=c["targets"],
        ))
        c["frame"] += 1

    def card_finish(self) -> None:
        """Close the card slot. The finished panel is written by the caller.

        The slot is emptied rather than reused for the result: the result is a
        transcript entry (it scrolls, it is selectable, it survives a clear),
        and the card is not.
        """
        self._card = None
        try:
            w = self.query_one("#card", Static)
        except Exception:
            return
        w.update("")
        w.add_class("hidden")

    @property
    def card_active(self) -> bool:
        return self._card is not None

    # ---- live streaming line ---------------------------------------------
    # These are called from the 0.55s cursor timer and from the streaming
    # handler, either of which can fire in the gap between the app tearing
    # down and the pane unmounting. Tolerate a missing child, like
    # card_finish does, rather than raising NoMatches out of a paint tick.
    def _stream_widget(self) -> Static | None:
        try:
            return self.query_one("#stream", Static)
        except Exception:
            return None

    def set_stream(self, text: str) -> None:
        from .renderers import agent_text

        w = self._stream_widget()
        if w is None:
            return
        w.remove_class("hidden")
        body = agent_text(text, self.palette).copy()
        cursor = "▊" if self._cursor_on else " "
        body.append(cursor, style=f"bold {self.palette.accent}")
        w.update(body)

    def clear_stream(self) -> None:
        w = self._stream_widget()
        if w is None:
            return
        w.update("")
        w.add_class("hidden")

    def flash_cursor(self) -> None:
        self._cursor_on = not self._cursor_on


class StatusBar(Static):
    """Bottom bar: tokens, cost, turns, iteration, elapsed, activity."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        padding: 0 1;
        background: $background;
    }
    """

    def __init__(self, palette: Palette, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.model = "?"
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = 0.0
        self.turns = 0
        self.iterations = 0
        self.activity = "idle"
        self.started = time.monotonic()
        self.busy = False
        self._verb_index = 0

    def set_activity(self, activity: str, busy: bool = False) -> None:
        self.activity = activity
        self.busy = busy
        self.render_bar()

    def tick_verb(self, verbs: list[str]) -> None:
        if self.busy:
            self._verb_index = (self._verb_index + 1) % len(verbs)
            self.activity = verbs[self._verb_index]

    def render_bar(self) -> None:
        p = self.palette
        elapsed = time.monotonic() - self.started
        mm, ss = divmod(int(elapsed), 60)

        t = Text()
        t.append("◉ ", style=f"bold {p.accent}")
        t.append(self.model, style=p.primary)
        t.append("  │  ", style=p.dim)
        t.append("tok ", style=p.dim)
        t.append(f"↑{_k(self.input_tokens)}", style=p.primary)
        t.append(" ", style=p.dim)
        t.append(f"↓{_k(self.output_tokens)}", style=p.accent)
        t.append("  │  ", style=p.dim)
        t.append("$ ", style=p.dim)
        t.append(f"{self.cost:.4f}", style=f"bold {p.accent}")
        t.append("  │  ", style=p.dim)
        t.append(f"turn {self.turns}", style=p.primary)
        t.append("  │  ", style=p.dim)
        t.append(f"iter {self.iterations}", style=p.dim)
        t.append("  │  ", style=p.dim)
        t.append(f"{mm:02d}:{ss:02d}", style=p.primary)
        t.append("  │  ", style=p.dim)
        if self.busy:
            t.append(f"⟳ {self.activity}", style=f"bold {p.accent}")
        else:
            t.append(f"● {self.activity}", style=p.dim)
        self.update(t)

    def on_mount(self) -> None:
        self.render_bar()


class ZimPane(Static):
    """Live campaign rail: two views, one pane.

    **phish** (default) — hits, credentials, local + public URLs. Hidden until
    `/phish` starts a campaign. The engine pushes events onto a thread-safe
    queue; the app drains that queue on the 0.25s rail tick and calls
    ``note()``, so the HTTP thread never touches Textual.

    **hunt** — what `/bug-hunt`'s agents are doing right now. Ten agents run
    concurrently but only ``HUNT_SLOTS`` of them fit legibly in 36 columns, so
    the pane shows the most recently active ones with their current activity;
    the status bar carries the aggregate for all ten. ``show_hunt`` switches
    the pane into this view.

    The chrome is mouse-driven: ``[–]`` minimizes to a stub, ``[×]`` hides
    the pane (the campaign keeps running), and dragging the left gutter
    resizes the width. ``show_campaign`` and ``show_hunt`` restore a closed or
    minimized pane so a new run is never invisible.
    """

    DEFAULT_CSS = """
    ZimPane {
        display: none;
        width: 36;
        min-width: 18;
        max-width: 80;
        height: 100%;
        padding: 0 1;
        border-left: heavy $secondary;
    }
    ZimPane.visible { display: block; }
    ZimPane.minimized {
        width: 14;
        min-width: 14;
        max-width: 14;
        padding: 0;
    }
    ZimPane.-resizing {
        border-left: heavy $accent;
    }
    """

    MAX_HITS = 8
    MAX_CREDS = 6
    #: Hunt agents shown at once. Ten run concurrently, but ten live activity
    #: lines do not fit a 36-column pane — the most recently active five do,
    #: and the status bar carries the count for the rest.
    HUNT_SLOTS = 5
    MIN_WIDTH = 18
    MAX_WIDTH = 80
    DEFAULT_WIDTH = 36
    MINIMIZED_WIDTH = 14
    # Columns on the left edge that start a resize drag.
    RESIZE_GUTTER = 2

    def __init__(self, palette: Palette, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        #: Which campaign the pane is showing: "phish" or "hunt". The two views
        #: share the chrome (minimize, close, resize) and nothing else, so a
        #: hunt never renders phish labels or vice versa.
        self.view = "phish"
        self.host = ""
        self.local_url = ""
        self.public_url = ""
        self.tunnel_tool = ""
        self.clone_note = ""
        self.cloned = False
        self.alive = False
        self.hits: list[dict] = []
        self.creds: list[dict] = []
        # ---- hunt view state
        self.hunt_target = ""
        self.hunt_wave = 0
        self.hunt_size = 0
        self.hunt_done = 0
        self.hunt_running = False
        #: name -> {activity, status, index}. Ordered by first appearance; the
        #: pane renders the tail, so the most recently active agents are the
        #: ones on screen.
        self.hunt_agents: dict[str, dict] = {}
        self.hunt_severity: dict[str, int] = {}
        self.minimized = False
        self._width = self.DEFAULT_WIDTH
        self._dragging = False
        self._drag_origin_x = 0
        self._drag_origin_width = self.DEFAULT_WIDTH

    def show_campaign(self, host: str) -> None:
        self.view = "phish"
        self.host = host
        self.local_url = ""
        self.public_url = ""
        self.tunnel_tool = ""
        self.clone_note = "starting…"
        self.cloned = False
        self.alive = True
        self.hits.clear()
        self.creds.clear()
        self.minimized = False
        self.remove_class("minimized")
        self.add_class("visible")
        self._apply_width(self._width or self.DEFAULT_WIDTH)
        self.render_pane()

    def show_hunt(self, target: str, size: int = 0) -> None:
        """Switch the pane into the hunt view and make it visible."""
        self.view = "hunt"
        self.hunt_target = target
        self.hunt_wave = 0
        self.hunt_size = size
        self.hunt_done = 0
        self.hunt_running = True
        self.hunt_agents.clear()
        self.hunt_severity.clear()
        self.minimized = False
        self.remove_class("minimized")
        self.add_class("visible")
        self._apply_width(self._width or self.DEFAULT_WIDTH)
        self.render_pane()

    def note_hunt(self, event: dict) -> None:
        """Apply one hunt event and repaint.

        Called on the UI thread from the app's hunt worker, so unlike phish's
        ``note`` there is no queue in front of it.
        """
        kind = event.get("type")

        if kind == "hunt_wave_start":
            self.hunt_wave = event.get("wave", self.hunt_wave)
            self.hunt_size = event.get("size", self.hunt_size) or self.hunt_size
            self.hunt_done = 0
            self.hunt_running = True
            # A new wave re-briefs the roster, so the previous wave's activity
            # lines are stale. Keep the map but mark everything as queued; the
            # agents that actually start will overwrite their own entry.
            for state in self.hunt_agents.values():
                if state.get("status") == "running":
                    state["status"] = "queued"
                    state["activity"] = "waiting"

        elif kind == "hunt_agent_start":
            name = event.get("name", "?")
            state = self.hunt_agents.setdefault(
                name, {"activity": "", "status": "running", "index": event.get("index", 0)}
            )
            state["status"] = "running"
            state["activity"] = "starting"
            # Re-insert so the dict's order reflects activity, not first sight:
            # the pane shows the tail, and the agent that just moved is the one
            # the operator wants to see.
            self.hunt_agents.pop(name, None)
            self.hunt_agents[name] = state

        elif kind == "hunt_agent_tool":
            state = self._hunt_state(event.get("name"))
            if state is not None:
                tool = event.get("tool", "?")
                state["activity"] = f"{tool} {self._brief_hint(event.get('args'))}".strip()
                self._touch(event.get("name"))

        elif kind == "hunt_agent_text":
            state = self._hunt_state(event.get("name"))
            if state is not None:
                line = (event.get("text") or "").strip().splitlines()
                if line:
                    state["activity"] = line[-1][:40]
                self._touch(event.get("name"))

        elif kind == "hunt_agent_done":
            state = self._hunt_state(event.get("name"))
            if state is not None:
                state["status"] = "done" if event.get("ok") else "failed"
                state["activity"] = "done" if event.get("ok") else "failed"
                self._touch(event.get("name"))
            self.hunt_done = min(self.hunt_done + 1, self.hunt_size or self.hunt_done + 1)

        elif kind == "hunt_finding":
            f = event.get("finding") or {}
            sev = str(f.get("severity") or "info").lower()
            self.hunt_severity[sev] = self.hunt_severity.get(sev, 0) + 1

        elif kind == "hunt_end":
            self.hunt_running = False
            self.alive = False

        self.render_pane()

    def _hunt_state(self, name) -> dict | None:
        if not name:
            return None
        return self.hunt_agents.get(str(name))

    def _touch(self, name) -> None:
        """Move *name* to the end of the activity order."""
        if not name:
            return
        state = self.hunt_agents.pop(str(name), None)
        if state is not None:
            self.hunt_agents[str(name)] = state

    @staticmethod
    def _brief_hint(args) -> str:
        """A one-glance hint of what a tool call is aimed at."""
        if not isinstance(args, dict):
            return ""
        for key in ("command", "path", "url", "query", "pattern"):
            value = args.get(key)
            if value:
                text = str(value).strip().splitlines()[0]
                return text[:24]
        return ""

    def hide(self) -> None:
        self.alive = False
        self.hunt_running = False
        self.minimized = False
        self.remove_class("visible")
        self.remove_class("minimized")
        self.remove_class("-resizing")
        self._dragging = False
        self.render_pane()

    def close_pane(self) -> None:
        """Hide the pane without stopping the campaign."""
        self.minimized = False
        self.remove_class("visible")
        self.remove_class("minimized")
        self.remove_class("-resizing")
        self._dragging = False
        self.render_pane()

    def minimize(self) -> None:
        if self.minimized:
            return
        self.minimized = True
        self.add_class("minimized")
        self.styles.width = self.MINIMIZED_WIDTH
        self.render_pane()

    def restore(self) -> None:
        if not self.minimized:
            return
        self.minimized = False
        self.remove_class("minimized")
        self._apply_width(self._width or self.DEFAULT_WIDTH)
        self.render_pane()

    def toggle_minimized(self) -> None:
        if self.minimized:
            self.restore()
        else:
            self.minimize()

    def _apply_width(self, width: int) -> None:
        width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, int(width)))
        self._width = width
        if not self.minimized:
            self.styles.width = width

    def note(self, event: dict) -> None:
        """Apply one engine event and repaint."""
        kind = event.get("kind")
        if kind == "ready":
            self.host = event.get("host") or self.host
            self.local_url = event.get("local") or ""
            self.public_url = event.get("public") or ""
            self.tunnel_tool = event.get("tool") or ""
            self.clone_note = event.get("clone") or ""
            self.cloned = bool(event.get("cloned"))
            self.alive = True
        elif kind == "stopped":
            self.alive = False
        elif kind == "cred":
            self.creds.append(event)
            if len(self.creds) > 40:
                self.creds = self.creds[-40:]
            self.hits.append(event)
            if len(self.hits) > 40:
                self.hits = self.hits[-40:]
        elif kind == "hit":
            self.hits.append(event)
            if len(self.hits) > 40:
                self.hits = self.hits[-40:]
        self.render_pane()

    def apply_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.render_pane()

    def render_pane(self) -> None:
        p = self.palette
        t = Text()
        if self.minimized:
            t.append(" ZIM\n", style=f"bold {p.accent}")
            t.append(" [+][×]\n", style=p.dim)
            label = self.hunt_target if self.view == "hunt" else self.host
            if label:
                t.append(" ", style=p.dim)
                t.append((label[:10] + "…") if len(label) > 10 else label,
                         style=p.primary)
                t.append("\n")
            live = self.hunt_running if self.view == "hunt" else self.alive
            t.append(" LIVE\n" if live else " ---\n",
                     style=f"bold {p.accent}" if live else p.dim)
            if self.view == "hunt":
                t.append(f" {sum(self.hunt_severity.values())} find\n", style=p.primary)
            else:
                t.append(f" {len(self.creds)} cred\n", style=p.primary)
            self.update(t)
            return

        if self.view == "hunt":
            self._render_hunt(t)
            return

        t.append("ZIM-PANE", style=f"bold {p.accent}")
        t.append("  [–][×]\n", style=p.dim)
        if not self.host and not self.alive:
            t.append("idle\n", style=p.dim)
            self.update(t)
            return

        t.append("phish  ", style=p.dim)
        t.append(self.host or "?", style=f"bold {p.primary}")
        t.append("\n")
        state = "LIVE" if self.alive else "stopped"
        t.append("state  ", style=p.dim)
        t.append(state + "\n", style=f"bold {p.accent}" if self.alive else p.amber)

        if self.clone_note:
            t.append("clone  ", style=p.dim)
            note = self.clone_note
            if len(note) > 28:
                note = note[:27] + "…"
            t.append(note + "\n", style=p.primary if self.cloned else p.amber)

        if self.local_url:
            t.append("local  ", style=p.dim)
            t.append(self.local_url + "\n", style=p.primary)
        if self.public_url:
            t.append("public ", style=p.dim)
            t.append(self.public_url + "\n", style=f"bold {p.accent}")
            if self.tunnel_tool:
                t.append("via    ", style=p.dim)
                t.append(self.tunnel_tool + "\n", style=p.dim)
        elif self.alive:
            t.append("public ", style=p.dim)
            t.append("none — LAN only\n", style=p.amber)

        t.append("\n")
        t.append("CREDS", style=f"bold {p.accent}")
        t.append(f"  {len(self.creds)}\n", style=p.dim)
        if not self.creds:
            t.append("  (waiting)\n", style=p.dim)
        else:
            for ev in self.creds[-self.MAX_CREDS:]:
                user = str(ev.get("user") or "?")
                password = str(ev.get("password") or "")
                if len(user) > 18:
                    user = user[:17] + "…"
                t.append("  ▸ ", style=f"bold {p.red}")
                t.append(user, style=f"bold {p.primary}")
                t.append("  ", style=p.dim)
                t.append(password if password else "(no pass)",
                         style=p.amber if password else p.dim)
                t.append("\n")

        t.append("\n")
        t.append("HITS", style=f"bold {p.accent}")
        t.append(f"  {len(self.hits)}\n", style=p.dim)
        if not self.hits:
            t.append("  (none yet)\n", style=p.dim)
        else:
            for ev in self.hits[-self.MAX_HITS:]:
                method = str(ev.get("method") or "?")
                path = str(ev.get("path") or "/")
                if len(path) > 18:
                    path = path[:17] + "…"
                mark = "●" if ev.get("kind") == "cred" else "·"
                style = f"bold {p.red}" if ev.get("kind") == "cred" else p.dim
                t.append(f"  {mark} ", style=style)
                t.append(f"{method:<4} ", style=p.primary)
                t.append(path + "\n", style=p.dim)

        t.append("\n")
        t.append("drag left edge to resize\n", style=p.dim)
        t.append("/phish stop  to tear down\n", style=p.dim)
        self.update(t)

    # ---- hunt view --------------------------------------------------------
    def _render_hunt(self, t: Text) -> None:
        """The `/bug-hunt` view: what each agent is doing, right now."""
        p = self.palette
        t.append("ZIM-PANE", style=f"bold {p.accent}")
        t.append("  [–][×]\n", style=p.dim)

        t.append("hunt   ", style=p.dim)
        t.append(self.hunt_target or "?", style=f"bold {p.primary}")
        t.append("\n")

        state = "LIVE" if self.hunt_running else "stopped"
        t.append("state  ", style=p.dim)
        t.append(state + "\n", style=f"bold {p.accent}" if self.hunt_running else p.amber)

        if self.hunt_wave:
            total = self.hunt_size or len(self.hunt_agents) or 0
            t.append("wave   ", style=p.dim)
            t.append(f"{self.hunt_wave}  ", style=f"bold {p.primary}")
            t.append(f"{self.hunt_done}/{total} done\n", style=p.dim)

        # Findings, worst first — the whole point of the pane during a hunt.
        found = sum(self.hunt_severity.values())
        t.append("\n")
        t.append("FINDINGS", style=f"bold {p.accent}")
        t.append(f"  {found}\n", style=p.dim)
        if not found:
            t.append("  (none yet)\n", style=p.dim)
        else:
            for sev in ("critical", "high", "medium", "low", "info"):
                n = self.hunt_severity.get(sev, 0)
                if not n:
                    continue
                style = {
                    "critical": f"bold {p.red}",
                    "high": p.amber,
                    "medium": p.primary,
                }.get(sev, p.dim)
                t.append(f"  {sev:<9}", style=style)
                t.append(f"{n}\n", style=p.dim)

        # The agents, most recently active last. Ten run at once but only
        # HUNT_SLOTS fit; the tail is what is moving.
        t.append("\n")
        t.append("AGENTS", style=f"bold {p.accent}")
        t.append(f"  {len(self.hunt_agents)}\n", style=p.dim)
        if not self.hunt_agents:
            t.append("  (waiting)\n", style=p.dim)
        else:
            for name, st in list(self.hunt_agents.items())[-self.HUNT_SLOTS:]:
                status = st.get("status", "running")
                mark, style = {
                    "running": ("▸", f"bold {p.accent}"),
                    "queued": ("·", p.dim),
                    "done": ("✔", p.primary),
                    "failed": ("✘", p.amber),
                }.get(status, ("·", p.dim))
                t.append(f"  {mark} ", style=style)
                t.append(name[:14] + "\n", style=f"bold {p.primary}"
                         if status == "running" else p.dim)
                activity = st.get("activity") or ""
                if activity and status == "running":
                    if len(activity) > 30:
                        activity = activity[:29] + "…"
                    t.append(f"    {activity}\n", style=p.dim)

        t.append("\n")
        t.append("drag left edge to resize\n", style=p.dim)
        t.append("/stop-hunt  to end the hunt\n", style=p.dim)
        self.update(t)

    # ---- mouse chrome -----------------------------------------------------
    def _hit_chrome(self, offset: Offset) -> str | None:
        """Which chrome control is under *offset*, if any."""
        if offset.y != 0:
            return None
        line = self._chrome_line()
        x = offset.x
        if self.minimized:
            plus = line.find("[+]")
            cross = line.find("[×]")
            if plus >= 0 and plus <= x < plus + 3:
                return "restore"
            if cross >= 0 and cross <= x < cross + 3:
                return "close"
            return None
        minus = line.find("[–]")
        cross = line.find("[×]")
        if minus >= 0 and minus <= x < minus + 3:
            return "minimize"
        if cross >= 0 and cross <= x < cross + 3:
            return "close"
        return None

    def _chrome_line(self) -> str:
        if self.minimized:
            return " [+][×]"
        return "ZIM-PANE  [–][×]"

    def on_click(self, event: Click) -> None:
        hit = self._hit_chrome(event.offset)
        if hit == "close":
            event.stop()
            self.close_pane()
            return
        if hit == "minimize":
            event.stop()
            self.minimize()
            return
        if hit == "restore":
            event.stop()
            self.restore()

    def on_mouse_down(self, event: MouseDown) -> None:
        if event.button != 1:
            return
        if self._hit_chrome(event.offset):
            return
        if self.minimized:
            return
        if event.offset.x > self.RESIZE_GUTTER:
            return
        event.stop()
        # The screen arms a text selection on mouse-down before the widget sees
        # the event; without this, releasing a resize drag copies the whole
        # pane to the clipboard.
        self.screen.clear_selection()
        self._dragging = True
        self._drag_origin_x = event.screen_x
        self._drag_origin_width = self.size.width or self._width
        self.add_class("-resizing")
        self.capture_mouse()

    def on_mouse_move(self, event: MouseMove) -> None:
        if not self._dragging:
            return
        event.stop()
        # Pane sits on the right: dragging the left edge leftward grows it.
        delta = self._drag_origin_x - event.screen_x
        self._apply_width(self._drag_origin_width + delta)

    def on_mouse_up(self, event: MouseUp) -> None:
        if not self._dragging:
            return
        event.stop()
        self._dragging = False
        self.remove_class("-resizing")
        self.release_mouse()
        self.render_pane()

    def on_mount(self) -> None:
        self.render_pane()


def _k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)
