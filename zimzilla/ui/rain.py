"""Matrix rain.

Two render paths share one model (``RainCanvas``):

* ``MatrixRain`` — a full-screen widget used by the *boot* screen, where the
  layer stack works because the boot widgets only occupy part of the canvas.
* ``RainRichLog`` — a ``RichLog`` subclass that paints rain into its own blank
  cells. Textual's compositor paints front-to-back and gives every cell to the
  frontmost widget ("first segment wins"); it does not alpha-blend widgets, so
  an opaque pane hides anything on a lower layer. Painting rain *inside* the
  pane is therefore the only way to get a rain background behind the
  transcript and activity panes.
"""

from __future__ import annotations

import random

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import RenderResult
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import RichLog

from ..theme import Palette

GLYPHS = "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワン0123456789"
ASCII_GLYPHS = "01ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿ<>[]{}/\\|+=*#$%&@"

TAIL_LEN = 9  # glyphs trailing behind a drop head


def _style_for(intensity: int, palette: Palette, subtle: bool = False) -> Style:
    """0 = brightest head, larger = dimmer tail.

    ``subtle`` is used for rain painted behind the transcript, where anything
    bright would fight the text: heads drop to the primary colour and the tail
    is uniformly dim.
    """
    if subtle:
        # Exactly one palette colour: no text-style dimming (Textual would blend
        # it into a shade outside the palette) and no bright head to fight the
        # transcript text.
        return Style(color=palette.dim)
    if intensity <= 0:
        return Style(color=palette.accent, bold=True)
    if intensity <= 2:
        return Style(color=palette.primary)
    if intensity <= 5:
        return Style(color=palette.dim)
    return Style(color=palette.dim, dim=True)


class RainCanvas:
    """The rain model: one falling drop per column, ticked on a timer."""

    def __init__(self, palette: Palette, density: float = 0.55, ascii_only: bool = True) -> None:
        self.palette = palette
        self.density = density
        self.ascii_only = ascii_only
        self.width = 0
        self.height = 0
        self.drops: list[int] = []
        self.frame = 0
        self.active = True

    @property
    def glyphs(self) -> str:
        return ASCII_GLYPHS if self.ascii_only else GLYPHS

    def resize(self, width: int, height: int) -> None:
        if (width, height) == (self.width, self.height):
            return
        self.width = max(0, width)
        self.height = max(0, height)
        self.drops = [
            random.randint(-self.height, 0) if random.random() < self.density else -10_000
            for _ in range(self.width)
        ]

    def tick(self) -> None:
        if not self.active or not self.drops:
            return
        self.frame += 1
        if self.frame % 3 == 0:
            self.drops = [d + 1 for d in self.drops]
            for i in range(len(self.drops)):
                if random.random() < 0.03:
                    self.drops[i] = random.randint(-8, 0)

    def cells_for_row(self, y: int) -> dict[int, tuple[str, int]]:
        """Rain glyphs sitting on row ``y``: ``{x: (glyph, intensity)}``."""
        if not self.active or not self.drops:
            return {}
        cells: dict[int, tuple[str, int]] = {}
        gl = self.glyphs
        for x in range(min(self.width, len(self.drops))):
            head = self.drops[x]
            if head - TAIL_LEN <= y <= head:
                cells[x] = (random.choice(gl), head - y)
        return cells

    def grid(self) -> list[dict[int, tuple[str, int]]]:
        return [self.cells_for_row(y) for y in range(self.height)]


def _blend(strip, cells: dict[int, tuple[str, int]], palette: Palette, subtle: bool = True):
    """Return a copy of ``strip`` with rain glyphs painted into its blank cells.

    Only cells whose current character is a space are touched, so text is never
    obscured. Adjacent cells sharing a style are merged to keep the strip
    compact.
    """
    if not cells:
        return strip
    new_segments: list[Segment] = []
    run_chars: list[str] = []
    run_style: Style | None = None
    run_control = None

    def flush() -> None:
        if run_chars:
            new_segments.append(Segment("".join(run_chars), run_style, run_control))

    x = 0
    for segment in strip:
        text, style, control = segment
        if not text:
            new_segments.append(segment)
            continue
        for ch in text:
            cell = cells.get(x)
            if cell is not None and ch == " " and control is None:
                style = _style_for(cell[1], palette, subtle=subtle)
                ch = cell[0]
            if run_style is None or (style == run_style and control == run_control):
                run_style, run_control = style, control
                run_chars.append(ch)
            else:
                flush()
                run_style, run_control = style, control
                run_chars = [ch]
            x += 1
    flush()
    return Strip(new_segments, cell_length=strip.cell_length)


class MatrixRain(Widget):
    """Full-screen rain widget (used on the boot screen)."""

    DEFAULT_CSS = """
    MatrixRain {
        layer: rain;
        width: 100%;
        height: 100%;
    }
    """

    def __init__(self, palette: Palette, ascii_only: bool = True, density: float = 0.55, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self._canvas = RainCanvas(palette, density=density, ascii_only=ascii_only)

    def set_active(self, active: bool) -> None:
        if active != self._canvas.active:
            self._canvas.active = active
            self.refresh()

    def on_mount(self) -> None:
        self.set_interval(0.11, self._tick)

    def on_resize(self, event) -> None:
        self._canvas.resize(event.size.width, event.size.height)

    def _tick(self) -> None:
        was = self._canvas.active
        self._canvas.tick()
        if was:
            self.refresh()

    def render(self) -> RenderResult:
        c = self._canvas
        if not c.active or c.width <= 0 or c.height <= 0 or not c.drops:
            return Text("")
        out = Text()
        bg = self.palette.bg
        for y in range(c.height):
            cells = c.cells_for_row(y)
            for x in range(c.width):
                cell = cells.get(x)
                if cell is None:
                    out.append(" ")
                else:
                    out.append(cell[0], _style_for(cell[1], self.palette) + Style(bgcolor=bg))
            if y < c.height - 1:
                out.append("\n")
        return out


class RainRichLog(RichLog):
    """A RichLog that renders matrix rain behind its own blank cells."""

    def __init__(self, palette: Palette, ascii_only: bool = True, rain_on: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.rain_on = rain_on
        # Sparse and dim: this rain sits behind live text, so it is texture,
        # not a focal point.
        self._canvas = RainCanvas(palette, density=0.18, ascii_only=ascii_only)

    def on_mount(self) -> None:
        super().on_mount()
        self._canvas.active = self.rain_on
        self.set_interval(0.11, self._tick_rain)

    def on_resize(self, event) -> None:
        # Content size ignores the pane border, so drops line up with the text.
        size = self.content_size
        self._canvas.resize(size.width, size.height or event.size.height)

    def _ensure_size(self) -> None:
        size = self.content_size
        if size.width and size.height:
            self._canvas.resize(size.width, size.height)

    def _tick_rain(self) -> None:
        if not self.rain_on:
            return
        self._ensure_size()
        self._canvas.tick()
        if self._canvas.active:
            self.refresh()

    def set_rain(self, active: bool) -> None:
        self.rain_on = active
        self._canvas.active = active
        self.refresh()

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        if self.rain_on and self._canvas.active:
            self._ensure_size()
            strip = _blend(strip, self._canvas.cells_for_row(y), self.palette)
        # Annotate every cell with its content offset so Textual can map a
        # mouse highlight back onto this text. RichLog never does this itself,
        # which is exactly why a RichLog cannot be copied from.
        scroll_x, scroll_y = self.scroll_offset
        return strip.apply_offsets(scroll_x, scroll_y + y)

    # ---- copy -------------------------------------------------------------
    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract the highlighted transcript text.

        The offsets annotated in ``render_line`` are content offsets, which
        index ``self.lines`` directly, so the joined text of those strips is the
        space the selection addresses. Trailing padding is stripped so a copied
        panel does not come with a right margin of spaces.
        """
        text = "\n".join(strip.text.rstrip() for strip in self.lines)
        return selection.extract(text), "\n"
