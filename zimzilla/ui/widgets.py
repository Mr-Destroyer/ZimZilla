"""Layout widgets: header, chat pane, activity pane, status bar."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import RichLog, Static

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
        t = Text()
        t.append("◈ ZIMZILLA", style=f"bold {p.primary}")
        t.append("  │  ", style=p.dim)
        from ..config import MODES

        m = MODES.get(self.mode, {})
        # Modes marked "loud" (zim, danger) are the armed states — shout them.
        mstyle = f"bold white on {p.red}" if m.get("loud") else f"bold {p.accent}"
        t.append(f"◆ {m.get('label', self.mode.upper())} ", style=mstyle)
        t.append("  │  ", style=p.dim)
        t.append("◉ ", style=p.accent)
        t.append(self.model, style=f"bold {p.accent}")
        t.append("  │  ", style=p.dim)
        t.append("⌂ ", style=p.dim)
        t.append(self.cwd, style=p.primary)
        t.append("  │  ", style=p.dim)
        t.append("⌖ scope ", style=p.dim)
        t.append(
            self.scope_text,
            style=f"bold {p.accent if self.scope_ok else p.red}",
        )
        t.append("  │  ", style=p.dim)
        if self.unsafe:
            t.append("⚠ SANDBOX OFF", style=f"bold white on {p.red}")
        else:
            t.append("⛨ sandbox on", style=p.dim)
        t.append("  │  ", style=p.dim)
        t.append(f"theme:{self.theme_name}", style=p.dim)
        self.update(t)

    def on_mount(self) -> None:
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
    """

    def __init__(self, palette: Palette, rain: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.rain_on = rain
        self._cursor_on = True

    def compose(self):
        yield RainRichLog(self.palette, rain_on=self.rain_on, id="transcript",
                          markup=False, wrap=True, highlight=False, auto_scroll=True)
        stream = Static(id="stream", classes="hidden")
        yield stream

    # ---- transcript -------------------------------------------------------
    def write_block(self, renderable) -> None:
        log = self.query_one("#transcript", RainRichLog)
        log.write(renderable)
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

    # ---- live streaming line ---------------------------------------------
    def set_stream(self, text: str) -> None:
        from .renderers import agent_text

        w = self.query_one("#stream", Static)
        w.remove_class("hidden")
        body = agent_text(text, self.palette).copy()
        cursor = "▊" if self._cursor_on else " "
        body.append(cursor, style=f"bold {self.palette.accent}")
        w.update(body)

    def clear_stream(self) -> None:
        w = self.query_one("#stream", Static)
        w.update("")
        w.add_class("hidden")

    def flash_cursor(self) -> None:
        self._cursor_on = not self._cursor_on


class ActivityPane(Vertical):
    """Right-hand pane: live tool activity and short output."""

    DEFAULT_CSS = """
    ActivityPane {
        width: 42;
        height: 100%;
        border-left: heavy $secondary;
    }
    ActivityPane #activity-title {
        height: 1;
        padding: 0 1;
        background: transparent;
    }
    ActivityPane #activity {
        height: 1fr;
        background: transparent;
        scrollbar-size-vertical: 1;
        scrollbar-color: $secondary;
        scrollbar-background: transparent;
    }
    """

    def __init__(self, palette: Palette, model: str, rain: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.model = model
        self.rain_on = rain
        self._count = 0

    def compose(self):
        yield Static(id="activity-title")
        yield RainRichLog(self.palette, rain_on=self.rain_on, id="activity",
                          markup=False, wrap=True, highlight=False)

    def set_rain(self, active: bool) -> None:
        self.rain_on = active
        self.query_one("#activity", RainRichLog).set_rain(active)

    def apply_palette(self, palette: Palette) -> None:
        self.palette = palette
        log = self.query_one("#activity", RainRichLog)
        log.palette = palette
        log._canvas.palette = palette
        log.refresh()

    def on_mount(self) -> None:
        self._render_title()

    def _render_title(self) -> None:
        p = self.palette
        t = Text()
        t.append("⚡ ACTIVITY", style=f"bold {p.accent}")
        t.append(f"  [{self._count}]", style=p.dim)
        self.query_one("#activity-title", Static).update(t)

    def log_call(self, name: str, args: dict) -> None:
        from ..tools import summarise_call

        p = self.palette
        self._count += 1
        self._render_title()
        t = Text()
        t.append("▚ ", style=f"bold {p.accent}")
        t.append(name.upper(), style=f"bold {p.primary}")
        t.append("  ", style=p.dim)
        t.append(self._clock(), style=p.dim)
        self.query_one("#activity", RichLog).write(t)

        t2 = Text()
        t2.append("  " + summarise_call(name, args, None, max_len=60), style=p.dim)
        self.query_one("#activity", RichLog).write(t2)

    def log_result(self, name: str, output: str, is_error: bool, meta: dict) -> None:
        p = self.palette
        t = Text()
        status = "✖ FAIL" if is_error else "✔ OK"
        style = f"bold {p.red}" if is_error else f"bold {p.accent}"
        t.append(f"  {status}", style=style)
        exit_code = meta.get("exit")
        if exit_code is not None:
            t.append(f" exit={exit_code}", style=p.dim)
        self.query_one("#activity", RichLog).write(t)

        if output:
            preview = "\n".join(output.splitlines()[:8])
            if len(output.splitlines()) > 8:
                preview += "\n  …"
            body = Text()
            for i, line in enumerate(preview.splitlines()):
                if i:
                    body.append("\n")
                body.append("  │ " + line, style=p.dim)
            self.query_one("#activity", RichLog).write(body)
        self.query_one("#activity", RichLog).write("")

    def log_blocked(self, message: str) -> None:
        p = self.palette
        t = Text()
        t.append("⛔ BLOCKED — out of scope\n", style=f"bold white on {p.red}")
        t.append("  " + message.replace("\n", "\n  "), style=p.red)
        self.query_one("#activity", RichLog).write(t)
        self.query_one("#activity", RichLog).write("")

    def clear(self) -> None:
        self._count = 0
        self._render_title()
        self.query_one("#activity", RichLog).clear()

    @staticmethod
    def _clock() -> str:
        return datetime.now().strftime("%H:%M:%S")


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
