"""The boot screen: ASCII banner reveal + fake system check, then fade to UI."""

from __future__ import annotations

import random
from typing import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.screen import Screen
from textual.widgets import Static

from ..theme import Palette
from .banner import (
    EMBLEM,
    FONT_HEIGHT,
    SPIN_FRAMES,
    SPIN_SECONDS,
    banner_lines,
    banner_width,
    spin_frame,
)
from .rain import MatrixRain

GLITCH = "!<>-_\\/[]{}—=+*^?#@$%&01"

SPINNER_STEPS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: How many 0.045s ticks each spin frame is held. Derived from SPIN_SECONDS so
#: the two cannot drift apart; at the current values it is exactly 2, giving a
#: full revolution in 1.08s — a deliberate turn rather than a flicker.
TICKS_PER_FRAME = max(1, round(SPIN_SECONDS / 0.045 / SPIN_FRAMES))


class BootScreen(Screen):
    """Animated boot sequence. Calls ``on_done`` when finished."""

    BINDINGS = [("escape", "skip", "skip")]

    CSS = """
    BootScreen { layers: rain main; }
    BootScreen MatrixRain { layer: rain; }
    BootScreen Middle { layer: main; }
    BootScreen #boot-emblem { height: auto; width: auto; }
    BootScreen #boot-banner { height: auto; width: auto; }
    BootScreen #boot-checks { height: auto; width: auto; margin-top: 1; }
    """

    def __init__(
        self,
        palette: Palette,
        checks: list[tuple[str, str, bool]],
        on_done: Callable[..., None],
        rain: bool = True,
    ):
        super().__init__()
        self.palette = palette
        self.checks = checks
        self.on_done = on_done
        self.rain = rain
        # Printable keys seen while booting; the app replays them into the
        # prompt so a fast typist loses nothing.
        self.typed: list[str] = []
        self.submit = False
        self._revealed = 0
        self._width = banner_width()
        self._check_index = 0
        self._phase = "banner"
        self._ticks = 0
        self._spinner = 0
        self._finished = False

    def compose(self) -> ComposeResult:
        if self.rain:
            yield MatrixRain(self.palette, ascii_only=True)
        with Middle():
            with Center():
                yield Static(id="boot-emblem")
            with Center():
                yield Static(id="boot-banner")
            with Center():
                yield Static(id="boot-checks")

    def on_mount(self) -> None:
        self.set_interval(0.045, self._tick)
        self._render_emblem()
        self._render_banner()
        self._render_checks()

    # ---- rendering --------------------------------------------------------
    def _spin_index(self) -> int:
        """Which spin frame the current tick is showing.

        Driven off the existing tick counter — no second timer, and no clock
        read. TICKS_PER_FRAME is derived from the two constants rather than
        hardcoded so changing either one keeps them in step.
        """
        return (self._ticks // TICKS_PER_FRAME) % SPIN_FRAMES

    def _render_emblem(self) -> None:
        """Draw the mark at its current angle, or the static one once settled.

        The spin belongs to the "logo is arriving" moment. Leaving it turning
        through the system check would be a distraction next to text the
        operator is meant to read, so once the wordmark is fully revealed it
        settles on ``EMBLEM`` and stays there.
        """
        rows = EMBLEM if self._phase != "banner" else spin_frame(self._spin_index())
        # Style the frame as a unit, taken from its own fill glyph, so it tracks
        # SPIN_SHADES rather than inventing a second brightness curve. Testing
        # row by row instead lit the caps and dimmed the fill on an edge-on
        # frame, which reads as a mistake rather than as a turn.
        lit = rows[1].strip()[:1] == "█"
        style = f"bold {self.palette.accent}" if lit else self.palette.dim
        t = Text(justify="center")
        for i, row in enumerate(rows):
            t.append(row, style=style)
            if i < len(rows) - 1:
                t.append("\n")
        try:
            self.query_one("#boot-emblem", Static).update(t)
        except Exception:
            pass

    def _render_banner(self) -> None:
        lines = banner_lines()
        t = Text()
        for row_i, line in enumerate(lines):
            for col_i, ch in enumerate(line):
                if ch == " ":
                    t.append(" ")
                    continue
                if col_i < self._revealed:
                    # settled glyph; tail glyphs glow with the accent color
                    near = col_i > self._revealed - 6
                    style = f"bold {self.palette.accent if near else self.palette.primary}"
                    t.append(ch, style=style)
                elif col_i < self._revealed + 4:
                    # glitch head just ahead of the reveal edge
                    t.append(random.choice(GLITCH), style=f"bold {self.palette.accent}")
                else:
                    t.append(" ")
            if row_i < len(lines) - 1:
                t.append("\n")
        try:
            self.query_one("#boot-banner", Static).update(t)
        except Exception:
            pass

    def _render_checks(self) -> None:
        t = Text()
        t.append("  ⟩ SYSTEM CHECK\n\n", style=f"bold {self.palette.accent}")
        for i, (label, value, ok) in enumerate(self.checks):
            if i < self._check_index:
                mark = "[ OK ]" if ok else "[ !! ]"
                mstyle = f"bold {self.palette.accent}" if ok else f"bold {self.palette.red}"
                t.append(f"  {label:<14}", style=self.palette.dim)
                t.append("·" * max(2, 18 - len(label)), style=self.palette.dim)
                t.append(f" {mark} ", style=mstyle)
                t.append(f" {value}\n", style=self.palette.primary)
            elif i == self._check_index and self._phase == "checks":
                spin = SPINNER_STEPS[self._spinner % len(SPINNER_STEPS)]
                t.append(f"  {label:<14}", style=self.palette.dim)
                t.append("·" * max(2, 18 - len(label)), style=self.palette.dim)
                t.append(f" {spin} ", style=self.palette.primary)
                t.append(f" {value}\n", style=self.palette.dim)
            else:
                t.append(f"  {label:<14}\n", style=self.palette.dim)

        if self.typed:
            t.append("\n  ❯ ", style=f"bold {self.palette.accent}")
            t.append("".join(self.typed), style=self.palette.primary)
            t.append("▊", style=f"bold {self.palette.accent}")

        if self._phase == "done":
            t.append("\n\n")
            t.append("  ▸ SHELL READY — handshake complete\n", style=f"bold {self.palette.accent}")
            t.append("  ▸ press enter to continue\n", style=self.palette.dim)

        # Author credits — shown for the whole check phase, not just the tail,
        # so they are actually readable before the splash hands off to the UI.
        if self._phase != "banner":
            t.append("\n")
            t.append("  author     ", style=self.palette.dim)
            t.append("Mr-Destroyer / ZIM\n", style=self.palette.primary)
            t.append("  youtube    ", style=self.palette.dim)
            t.append("@Study_Hard69\n", style=self.palette.primary)
            t.append("  instagram  ", style=self.palette.dim)
            t.append("zimthegoat\n", style=self.palette.primary)
        try:
            self.query_one("#boot-checks", Static).update(t)
        except Exception:
            pass

    # ---- animation loop ---------------------------------------------------
    def _tick(self) -> None:
        self._ticks += 1
        self._spinner += 1

        if self._phase == "banner":
            self._revealed += max(1, self._width // 26)
            if self._revealed > self._width + 6:
                self._revealed = self._width + 6
                self._phase = "checks"
            self._render_emblem()
            self._render_banner()
            self._render_checks()
            return

        if self._phase == "checks":
            # advance one check roughly every 4 ticks (≈180ms)
            if self._ticks % 4 == 0:
                self._check_index += 1
            if self._check_index >= len(self.checks):
                self._check_index = len(self.checks)
                self._phase = "fade"
                self._ticks = 0
            self._render_emblem()
            self._render_banner()
            self._render_checks()
            return

        if self._phase == "fade":
            if self._ticks >= 8:
                self._phase = "done"
                self._render_checks()
                # Auto-continue. Any buffered text is preserved in the prompt,
                # so finishing here never truncates what the user typed. The
                # dwell is long enough to read the credits and the ready line.
                self.set_timer(2.2, self._finish)

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.on_done(self.typed, self.submit)

    def action_skip(self) -> None:
        self._finish()

    def on_key(self, event) -> None:
        # Printable characters are buffered and replayed into the prompt by the
        # app, so a fast typist loses nothing. Enter finishes boot AND marks the
        # line for submission (otherwise the Enter that submits a buffered
        # prompt would be swallowed as "skip").
        ch = getattr(event, "character", None)
        key = getattr(event, "key", None)
        if ch and ch.isprintable():
            self.typed.append(ch)
            self._render_checks()
            return
        # A terminal may deliver Return as key "enter" / "return" / "ctrl+m",
        # or as the raw characters \r / \n. Accept all of them.
        if key in ("enter", "return", "ctrl+m", "ctrl+j") or ch in ("\r", "\n"):
            self.submit = True
            self._finish()
