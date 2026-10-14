"""Ask the terminal for its whole palette, not just its background.

``termbg.py`` asks one question — OSC 11, "what is your background?" — because
that is all the fixed palettes needed: they know their own colours and only
want the padding to match. A *system* theme needs the opposite: the terminal's
colours are the theme, and the harness should adopt them.

Themes like omarchy's ``aether`` generate a full scheme from a wallpaper and
install it as the terminal's palette, so the terminal is the single place the
current colours can be read from. Three queries cover it:

===================  ==========================  =========================
OSC 11               ``]11;rgb:1010/1313/1515``  the background
OSC 10               ``]10;rgb:...``             the foreground
OSC 4;N  (N=0..15)   ``]4;N;rgb:...``            each ANSI slot
===================  ==========================  =========================

All three are answered by the terminals omarchy drives (kitty, alacritty,
ghostty, wezterm), and all three are sent in **one** raw-mode session — see
``termbg.query``, which owns the tty handling and takes a terminator count.

Everything here is best-effort in the same way ``termbg`` is: a terminal that
answers nothing yields ``None``, and a terminal that answers only some slots
yields those, leaving the rest for the caller to fall back on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .termbg import _scale, is_dark, query

#: OSC code, optional per-slot index, then the colour in either form a
#: terminal sends it. One pattern for all three queries rather than three,
#: because the only difference between them is the OSC number in group 1.
_OSC_RGB = re.compile(
    rb"\](\d+)(?:;(\d+))?;rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})"
)
_OSC_HASH = re.compile(rb"\](\d+)(?:;(\d+))?;#([0-9a-fA-F]{6})")

#: How many ANSI slots to ask for. 0-15 is the whole standard palette — 0-7
#: normal, 8-15 bright — and is what a theme generator writes.
ANSI_SLOTS = 16


@dataclass(frozen=True)
class TerminalColors:
    """What the terminal reported. Any field may be missing."""

    bg: str | None = None
    fg: str | None = None
    ansi: dict[int, str] = field(default_factory=dict)

    @property
    def dark_bg(self) -> str | None:
        """The background, but only if it is dark enough for the fixed palettes.

        The three neon palettes refuse a light background (see
        ``termbg.is_dark``); a system palette does not, because its colours come
        from the terminal and are readable on whatever it paints. So the filter
        lives here as an opt-in property rather than being applied on the way
        in — ``system`` reads ``.bg`` directly, everything else reads this.
        """
        return self.bg if self.bg and is_dark(self.bg) else None

    def any(self) -> bool:
        """Whether the terminal told us anything at all."""
        return bool(self.bg or self.fg or self.ansi)


def _hex_of_rgb(m: re.Match) -> str:
    return "#%02x%02x%02x" % tuple(_scale(m.group(i)) for i in (3, 4, 5))


def parse_colors(reply: bytes) -> TerminalColors:
    """Turn a batch of OSC replies into a :class:`TerminalColors`.

    Pure — no tty, no I/O — so the suite can test it against captured replies.
    A reply for a slot that was not asked about, or a malformed one, is simply
    absent from the result.
    """
    found: dict[tuple[str, str | None], str] = {}

    for m in _OSC_RGB.finditer(reply):
        found[(m.group(1).decode(), m.group(2).decode() if m.group(2) else None)] = _hex_of_rgb(m)
    # The #rrggbb form. rgb: wins where both are present for a slot, which
    # cannot happen from a single terminal but keeps the precedence defined.
    for m in _OSC_HASH.finditer(reply):
        found.setdefault(
            (m.group(1).decode(), m.group(2).decode() if m.group(2) else None),
            "#" + m.group(3).decode().lower(),
        )

    ansi: dict[int, str] = {}
    for (code, index), color in found.items():
        if code == "4" and index is not None:
            try:
                slot = int(index)
            except ValueError:
                continue
            if 0 <= slot < ANSI_SLOTS:
                ansi[slot] = color

    return TerminalColors(bg=found.get(("11", None)), fg=found.get(("10", None)), ansi=ansi)


def _payload() -> bytes:
    """The three queries, batched into one escape sequence."""
    parts = [b"\x1b]11;?\x1b\\", b"\x1b]10;?\x1b\\"]
    parts += [b"\x1b]4;%d;?\x1b\\" % i for i in range(ANSI_SLOTS)]
    return b"".join(parts)


def detect_colors(timeout: float = 0.4) -> TerminalColors | None:
    """Ask the terminal for its palette. None when it tells us nothing.

    One round-trip for all eighteen queries, so a terminal that does not
    implement OSC 4 costs one timeout rather than eighteen. The timeout is
    slightly longer than ``termbg``'s single-query one because there is more
    to send and more to read back.
    """
    colors = parse_colors(query(_payload(), timeout=timeout))
    return colors if colors.any() else None
