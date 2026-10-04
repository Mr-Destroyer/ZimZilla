"""Layout widgets: header, chat pane, status bar."""

from __future__ import annotations

import time

from rich.text import Text
from textual.containers import Vertical
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
        # Modes marked "loud" (zim, danger) are the armed states — shout them.
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


def _k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)
