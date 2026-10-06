"""Mission Control rails: the loop state machine and the telemetry column.

These sit either side of the transcript and are the difference between a log
viewer and an instrument panel. Both are plain ``Static`` widgets that
``update()`` a Rich ``Text`` — the same pattern ``HeaderBar``/``StatusBar``
already use in ``widgets.py`` — so they add no rendering machinery.

Deliberately *not* rain-aware: ``RainRichLog`` paints rain inside its own blank
cells because Textual's compositor does not blend widgets across layers, so a
lower layer would simply be hidden. The rails are crisp instead, which also
keeps the numbers legible.

The loop rail is comprehension, not information. It tells the operator nothing
they could not read off the transcript; its job is to make the agent's cycle
legible at a glance.
"""

from __future__ import annotations

from collections import deque

from rich.text import Text
from textual.events import MouseDown, MouseMove, MouseUp
from textual.geometry import Offset
from textual.widgets import Static

from ..theme import Palette

# The cycle, in order. `idle` is the resting state between turns.
STAGES: tuple[str, ...] = ("thinking", "calling", "observing", "idle")
STAGE_LABEL = {
    "thinking": "THINK",
    "calling": "CALL",
    "observing": "OBSERVE",
    "idle": "IDLE",
}

# Block glyphs, low to high. Index = intensity.
_SPARK = " ▁▂▃▄▅▆▇█"
# Fill glyphs for the context gauge.
_FULL, _EMPTY = "▓", "░"


class RailResizeMixin:
    """Drag a rail's inner border to resize it with the mouse.

    Mixed into both side rails so either edge of the transcript can be dragged.
    ``RESIZE_EDGE`` names the border carrying the handle: ``"right"`` for a rail
    on the left of the layout, ``"left"`` for one on the right. Subclasses call
    ``_init_resize()`` from their own ``__init__``.
    """

    RESIZE_EDGE = "right"
    MIN_WIDTH = 12
    MAX_WIDTH = 48
    DEFAULT_WIDTH = 14
    #: Columns along the inner edge that start a drag.
    RESIZE_GUTTER = 2

    def _init_resize(self) -> None:
        self._rail_width = self.DEFAULT_WIDTH
        self._rail_dragging = False
        self._rail_origin_x = 0
        self._rail_origin_width = self.DEFAULT_WIDTH

    def _rail_handle_at(self, offset: Offset) -> bool:
        """Is *offset* on the draggable edge of this rail?"""
        width = self.size.width or self._rail_width
        if self.RESIZE_EDGE == "right":
            return offset.x >= width - self.RESIZE_GUTTER
        return offset.x < self.RESIZE_GUTTER

    def _apply_rail_width(self, width: int) -> None:
        width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, int(width)))
        self._rail_width = width
        self.styles.width = width

    def on_mouse_down(self, event: MouseDown) -> None:
        if event.button != 1 or not self._rail_handle_at(event.offset):
            return
        event.stop()
        # The screen arms a text selection on mouse-down *before* the widget
        # sees the event, so without this a resize drag would end in a
        # TextSelected that copies the whole rail to the clipboard.
        self.screen.clear_selection()
        self._rail_dragging = True
        self._rail_origin_x = event.screen_x
        self._rail_origin_width = self.size.width or self._rail_width
        self.add_class("-resizing")
        self.capture_mouse()

    def on_mouse_move(self, event: MouseMove) -> None:
        if not self._rail_dragging:
            return
        event.stop()
        delta = event.screen_x - self._rail_origin_x
        if self.RESIZE_EDGE == "left":
            delta = -delta
        self._apply_rail_width(self._rail_origin_width + delta)

    def on_mouse_up(self, event: MouseUp) -> None:
        if not self._rail_dragging:
            return
        event.stop()
        self._rail_dragging = False
        self.remove_class("-resizing")
        self.release_mouse()


def _fmt_k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


class LoopRail(RailResizeMixin, Static):
    """The agent's cycle as a four-node state machine, with the active node lit."""

    RESIZE_EDGE = "right"
    MIN_WIDTH = 12
    MAX_WIDTH = 44
    DEFAULT_WIDTH = 14

    DEFAULT_CSS = """
    LoopRail {
        width: 14;
        height: 100%;
        padding: 0 1;
        border-right: heavy $secondary;
    }
    /* Light the edge while it is being dragged, so the handle is discoverable. */
    LoopRail.-resizing {
        border-right: heavy $accent;
    }
    """

    def __init__(self, palette: Palette, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.stage = "idle"
        self.turn = 0
        self.iteration = 0
        self.pulse = False
        #: Last few stages, newest last — a short breadcrumb of the cycle.
        self.trail: deque[str] = deque(maxlen=6)
        self._init_resize()

    def set_stage(self, stage: str) -> None:
        if stage not in STAGE_LABEL:
            return
        if stage != self.stage:
            self.trail.append(stage)
        self.stage = stage
        self.render_rail()

    def set_counters(self, turn: int, iteration: int) -> None:
        self.turn = turn
        self.iteration = iteration
        self.render_rail()

    def tick_pulse(self) -> None:
        """Called on a timer; makes the active node breathe."""
        self.pulse = not self.pulse
        self.render_rail()

    def render_rail(self) -> None:
        p = self.palette
        t = Text()
        t.append("LOOP\n\n", style=f"bold {p.accent}")

        for i, stage in enumerate(STAGES):
            active = stage == self.stage
            last = i == len(STAGES) - 1
            # The lit node alternates between two marks so it reads as alive
            # without needing a colour Textual would blend off-palette.
            if active:
                mark = "◉" if self.pulse else "◎"
                style = f"bold {p.accent}"
            else:
                mark = "○"
                style = p.dim
            t.append(f" {mark} ", style=style)
            t.append(STAGE_LABEL[stage], style=style)
            t.append("\n")
            if not last:
                # Connector: bright only up to the active node.
                below = STAGES[i + 1] == self.stage
                t.append("  │\n", style=p.accent if below else p.dim)

        t.append("\n")
        t.append("turn ", style=p.dim)
        t.append(f"{self.turn}\n", style=p.primary)
        t.append("iter ", style=p.dim)
        t.append(f"{self.iteration}\n", style=p.primary)

        if self.trail:
            t.append("\n")
            t.append("│", style=p.dim)
            t.append("".join(_glyph_for(s) for s in self.trail), style=p.dim)

        self.update(t)

    def apply_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.render_rail()

    def on_mount(self) -> None:
        self.render_rail()


def _glyph_for(stage: str) -> str:
    return {"thinking": "▪", "calling": "▸", "observing": "▪", "idle": "·"}.get(stage, "·")


class Waveform(Static):
    """Throughput as a scrolling signal — dense while streaming, flat when idle."""

    DEFAULT_CSS = """
    Waveform {
        height: 3;
        padding: 0 1;
    }
    """

    def __init__(self, palette: Palette, width: int = 26, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.width = width
        self.samples: deque[float] = deque([0.0] * width, maxlen=width)
        #: The newest sample, kept so the caption can name the live rate.
        self.latest = 0.0

    def push(self, sample: float) -> None:
        self.latest = max(0.0, float(sample))
        self.samples.append(self.latest)
        self.render_wave()

    def render_wave(self) -> None:
        p = self.palette
        peak = max(self.samples) or 1.0
        t = Text()
        for v in self.samples:
            level = int(round((v / peak) * (len(_SPARK) - 1)))
            t.append(_SPARK[level], style=p.accent if level > 2 else p.dim)
        # Name the signal: an unlabelled graph reads as decoration. The number
        # is the newest sample, which is the same thing the rightmost glyph
        # shows, said in digits.
        t.append("\n")
        t.append("tok/s ", style=p.dim)
        t.append(f"{self.latest:5.1f}", style=f"bold {p.accent}" if self.latest else p.dim)
        t.append("  peak ", style=p.dim)
        t.append(f"{peak:5.1f}", style=p.primary)
        self.update(t)

    def apply_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.render_wave()

    def on_mount(self) -> None:
        self.render_wave()


class ContextGauge(Static):
    """How full the model's context window is."""

    DEFAULT_CSS = """
    ContextGauge {
        height: 3;
        padding: 0 1;
    }
    """

    def __init__(self, palette: Palette, total: int = 128_000, width: int = 10, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.total = max(1, total)
        self.width = width
        self.used = 0

    def set(self, used: int, total: int | None = None) -> None:
        self.used = max(0, int(used))
        if total:
            self.total = max(1, int(total))
        self.render_gauge()

    @property
    def ratio(self) -> float:
        return min(1.0, self.used / self.total)

    def render_gauge(self) -> None:
        p = self.palette
        ratio = self.ratio
        filled = int(round(ratio * self.width))
        # Amber past 70%, red past 90% — the same escalation the modes use.
        if ratio >= 0.9:
            bar_style = f"bold {p.red}"
        elif ratio >= 0.7:
            bar_style = f"bold {p.amber}"
        else:
            bar_style = p.accent

        t = Text()
        t.append("context\n", style=p.dim)
        t.append(_FULL * filled, style=bar_style)
        t.append(_EMPTY * (self.width - filled), style=p.dim)
        t.append(f" {int(ratio * 100)}%\n", style=bar_style)
        t.append(f"{_fmt_k(self.used)} / {_fmt_k(self.total)}", style=p.dim)
        self.update(t)

    def apply_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.render_gauge()

    def on_mount(self) -> None:
        self.render_gauge()


class FileRiver(Static):
    """Files touched this session, with a bar per file showing how often."""

    DEFAULT_CSS = """
    FileRiver {
        height: auto;
        max-height: 14;
        padding: 0 1;
    }
    """

    MAX_ROWS = 10

    def __init__(self, palette: Palette, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        #: path -> touch count, in first-seen order.
        self.touched: dict[str, int] = {}

    def touch(self, path: str) -> None:
        if not path:
            return
        self.touched[path] = self.touched.get(path, 0) + 1
        self.render_river()

    def clear(self) -> None:
        self.touched.clear()
        self.render_river()

    def render_river(self) -> None:
        p = self.palette
        t = Text()
        t.append("FILES\n", style=f"bold {p.accent}")
        if not self.touched:
            t.append("(none yet)\n", style=p.dim)
            self.update(t)
            return

        # Newest first, capped — the rail is a live view, not a full audit.
        items = list(self.touched.items())[-self.MAX_ROWS:]
        width = max(len(_short(path)) for path, _ in items)
        # Bars are absolute (one per touch, capped at 4), not normalised to the
        # busiest file: a lone touch is one bar, not a full gauge. A relative
        # scale would draw a single edit as if the file were hot.
        for path, count in reversed(items):
            bars = min(count, 4)
            t.append("▎" * bars + " " * (4 - bars + 1),
                     style=p.accent if count > 1 else p.dim)
            t.append(_short(path).ljust(width) + " ", style=p.primary)
            if count > 1:
                t.append(f"×{count}", style=p.dim)
            t.append("\n")
        self.update(t)

    def apply_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.render_river()

    def on_mount(self) -> None:
        self.render_river()


def _short(path: str, limit: int = 18) -> str:
    """A path short enough for the rail: basename, prefixed if ambiguous."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if len(name) > limit:
        return name[: limit - 1] + "…"
    return name


class CallStrip(Static):
    """The last few tool calls, newest first, with their outcome.

    The file river only knows about paths, so a session spent in bash would
    leave the rail blank. This is the condensed call log the old ActivityPane
    carried — the full output still lives in the transcript, where it belongs.
    """

    DEFAULT_CSS = """
    CallStrip {
        height: auto;
        max-height: 7;
        padding: 0 1;
    }
    """

    MAX_ROWS = 6
    #: glyph per outcome
    _MARK = {"run": ("⟳", "accent"), "ok": ("✔", "accent"), "fail": ("✖", "red")}

    def __init__(self, palette: Palette, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        #: (name, outcome) newest last; outcome ∈ run | ok | fail
        self.calls: deque[tuple[str, str]] = deque(maxlen=self.MAX_ROWS)

    def note_call(self, name: str) -> None:
        """A tool just started."""
        self.calls.append((name, "run"))
        self.render_calls()

    def note_result(self, name: str, ok: bool) -> None:
        """The newest still-running call for *name* finished."""
        for i in range(len(self.calls) - 1, -1, -1):
            if self.calls[i] == (name, "run"):
                self.calls[i] = (name, "ok" if ok else "fail")
                break
        self.render_calls()

    def clear(self) -> None:
        self.calls.clear()
        self.render_calls()

    def render_calls(self) -> None:
        p = self.palette
        t = Text()
        t.append("RECENT\n", style=f"bold {p.accent}")
        if not self.calls:
            t.append("(none yet)\n", style=p.dim)
            self.update(t)
            return
        for name, outcome in reversed(self.calls):
            glyph, colour = self._MARK.get(outcome, ("·", "dim"))
            t.append(f" {glyph} ", style=f"bold {getattr(p, colour)}")
            t.append(name, style=p.primary if outcome != "fail" else p.red)
            t.append("\n")
        self.update(t)

    def apply_palette(self, palette: Palette) -> None:
        self.palette = palette
        self.render_calls()

    def on_mount(self) -> None:
        self.render_calls()


class TelemetryRail(Static):
    """The right-hand column: throughput, context, cost, files.

    Composes the sub-widgets rather than drawing them itself, so each can be
    updated independently and the rail stays cheap to refresh.
    """

    DEFAULT_CSS = """
    TelemetryRail {
        width: 32;
        height: 100%;
        border-left: heavy $secondary;
        /* Pack the sections from the top. Without this the last child absorbs
           the leftover height and FILES drifts to the bottom of the column,
           disconnected from the readout it belongs to. */
        align: left top;
    }
    TelemetryRail #tel-title {
        height: 1;
        padding: 0 1;
    }
    TelemetryRail #tel-cost {
        height: 1;
        padding: 0 1;
    }
    """

    def __init__(self, palette: Palette, total_context: int = 128_000, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.total_context = total_context

    def compose(self):
        yield Static(id="tel-title")
        yield Waveform(self.palette, id="tel-wave")
        yield ContextGauge(self.palette, total=self.total_context, id="tel-gauge")
        yield Static(id="tel-cost")
        yield CallStrip(self.palette, id="tel-calls")
        yield FileRiver(self.palette, id="tel-files")

    def on_mount(self) -> None:
        self._render_title()
        self._render_cost()

    def _render_title(self) -> None:
        p = self.palette
        t = Text()
        t.append("⚡ TELEMETRY", style=f"bold {p.accent}")
        self.query_one("#tel-title", Static).update(t)

    def set_cost(self, cost: float, tok_in: int, tok_out: int) -> None:
        self._cost = (cost, tok_in, tok_out)
        self._render_cost()

    def _render_cost(self) -> None:
        p = self.palette
        cost, tok_in, tok_out = getattr(self, "_cost", (0.0, 0, 0))
        t = Text()
        t.append("$ ", style=p.dim)
        t.append(f"{cost:.4f}", style=f"bold {p.accent}")
        t.append("   ", style=p.dim)
        t.append(f"↑{_fmt_k(tok_in)}", style=p.primary)
        t.append(" ", style=p.dim)
        t.append(f"↓{_fmt_k(tok_out)}", style=p.accent)
        try:
            self.query_one("#tel-cost", Static).update(t)
        except Exception:
            pass

    # ---- pass-throughs ----------------------------------------------------
    def push_wave(self, sample: float) -> None:
        self.query_one("#tel-wave", Waveform).push(sample)

    def set_context(self, used: int) -> None:
        self.query_one("#tel-gauge", ContextGauge).set(used, self.total_context)

    def touch_file(self, path: str) -> None:
        if path:
            self.query_one("#tel-files", FileRiver).touch(path)

    def clear_files(self) -> None:
        self.query_one("#tel-files", FileRiver).clear()
        self.query_one("#tel-calls", CallStrip).clear()

    def note_call(self, name: str) -> None:
        self.query_one("#tel-calls", CallStrip).note_call(name)

    def note_result(self, name: str, ok: bool) -> None:
        self.query_one("#tel-calls", CallStrip).note_result(name, ok)

    def apply_palette(self, palette: Palette) -> None:
        self.palette = palette
        for w in self.query(Waveform):
            w.apply_palette(palette)
        for w in self.query(ContextGauge):
            w.apply_palette(palette)
        for w in self.query(FileRiver):
            w.apply_palette(palette)
        for w in self.query(CallStrip):
            w.apply_palette(palette)
        self.styles.border_left = ("heavy", palette.dim)
        self._render_title()
        self._render_cost()
