"""Layout widgets: header, chat pane, status bar."""

from __future__ import annotations

import time

from rich.text import Text
from textual.containers import Vertical
from textual.events import Click, MouseDown, MouseMove, MouseUp
from textual.geometry import Offset
from textual.widgets import Static

from ..theme import Palette
from .rain import RainRichLog


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
        # RichLog.write clamps the render width up to `min_width`, which
        # defaults to 78 — so a Panel written into a narrower pane renders at
        # 78 columns, overflows, and switches on the horizontal scrollbar. Pass
        # the pane's real width instead; that is the only way to get a box that
        # fits. Falls back to the default when the size is not known yet.
        width = log.scrollable_content_region.width or None
        log.write(renderable, width=width)
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
    """Live campaign rail: hits, credentials, local + public URLs.

    Hidden until `/phish` starts a campaign. The engine pushes events onto
    a thread-safe queue; the app drains that queue on the 0.25s rail tick
    and calls ``note()``, so the HTTP thread never touches Textual.

    The chrome is mouse-driven: ``[–]`` minimizes to a stub, ``[×]`` hides
    the pane (the campaign keeps running), and dragging the left gutter
    resizes the width. ``show_campaign`` restores a closed or minimized
    pane so a new ``/phish`` is never invisible.
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
    MIN_WIDTH = 18
    MAX_WIDTH = 80
    DEFAULT_WIDTH = 36
    MINIMIZED_WIDTH = 14
    # Columns on the left edge that start a resize drag.
    RESIZE_GUTTER = 2

    def __init__(self, palette: Palette, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.host = ""
        self.local_url = ""
        self.public_url = ""
        self.tunnel_tool = ""
        self.clone_note = ""
        self.cloned = False
        self.alive = False
        self.hits: list[dict] = []
        self.creds: list[dict] = []
        self.minimized = False
        self._width = self.DEFAULT_WIDTH
        self._dragging = False
        self._drag_origin_x = 0
        self._drag_origin_width = self.DEFAULT_WIDTH

    def show_campaign(self, host: str) -> None:
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

    def hide(self) -> None:
        self.alive = False
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
            if self.host:
                t.append(" ", style=p.dim)
                t.append((self.host[:10] + "…") if len(self.host) > 10
                         else self.host, style=p.primary)
                t.append("\n")
            t.append(" LIVE\n" if self.alive else " ---\n",
                     style=f"bold {p.accent}" if self.alive else p.dim)
            t.append(f" {len(self.creds)} cred\n", style=p.primary)
            self.update(t)
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
