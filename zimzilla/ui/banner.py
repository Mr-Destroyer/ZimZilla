"""The ZIMZILLA ASCII banner, in a few styles.

Each glyph is 6 rows tall. The boot screen reveals them with a per-column
glitch/typewriter animation.
"""

from __future__ import annotations

import math

from rich.text import Text

from ..theme import Palette

# A compact 5x6 block font for the characters in "ZIMZILLA".
# '#' = lit pixel, ' ' = dark.
FONT: dict[str, list[str]] = {
    "Z": [
        "██████",
        "    ██",
        "   ██ ",
        "  ██  ",
        " ██   ",
        "██████",
    ],
    "I": [
        "██████",
        "  ██  ",
        "  ██  ",
        "  ██  ",
        "  ██  ",
        "██████",
    ],
    "M": [
        "██  ██",
        "██████",
        "██████",
        "██  ██",
        "██  ██",
        "██  ██",
    ],
    "L": [
        "██    ",
        "██    ",
        "██    ",
        "██    ",
        "██    ",
        "██████",
    ],
    "-": [
        "      ",
        "      ",
        "██████",
        "██████",
        "      ",
        "      ",
    ],
    "H": [
        "██  ██",
        "██  ██",
        "██████",
        "██  ██",
        "██  ██",
        "██  ██",
    ],
    "A": [
        " ████ ",
        "██  ██",
        "██████",
        "██  ██",
        "██  ██",
        "██  ██",
    ],
    "R": [
        "█████ ",
        "██  ██",
        "█████ ",
        "██ ██ ",
        "██  ██",
        "██  ██",
    ],
    "N": [
        "██  ██",
        "███ ██",
        "██████",
        "██ ███",
        "██  ██",
        "██  ██",
    ],
    "E": [
        "██████",
        "██    ",
        "█████ ",
        "██    ",
        "██    ",
        "██████",
    ],
    "S": [
        "██████",
        "██    ",
        "██████",
        "    ██",
        "    ██",
        "██████",
    ],
    " ": [
        "   ",
        "   ",
        "   ",
        "   ",
        "   ",
        "   ",
    ],
}

FONT_HEIGHT = 6


def render_word(word: str, spacing: int = 1) -> list[str]:
    """Render *word* as FONT_HEIGHT lines of block text."""
    rows = ["" for _ in range(FONT_HEIGHT)]
    for ch in word.upper():
        glyph = FONT.get(ch, FONT[" "])
        for i in range(FONT_HEIGHT):
            rows[i] += glyph[i] + (" " * spacing)
    return rows


def banner_lines(spacing: int = 1) -> list[str]:
    return render_word("ZIMZILLA", spacing=spacing)


# A second, small style: layered outline for the header bar / splash.
# Box width is derived from the content so the corners always line up.
def outline_lines(width: int = 48) -> list[str]:
    body = "Z I M Z I L L A   //   n e t . s h e l l"
    pad = max(0, width - 2 - len(body))
    left = pad // 2
    right = pad - left
    return [
        "╔" + "═" * (width - 2) + "╗",
        "║" + " " * left + body + " " * right + "║",
        "╚" + "═" * (width - 2) + "╝",
    ]


def banner_subtitle() -> str:
    """One-line tagline shown under the splash banner."""
    return "n e t . s h e l l   //   autonomous coding agent"


def banner_width(spacing: int = 1) -> int:
    lines = banner_lines(spacing)
    return max(len(l) for l in lines)


# ---------------------------------------------------------------------------
# The splash mark: a small emblem with the name beside it.
#
# Deliberately not the block wordmark above. That one is 56 columns of solid
# glyph, which is a wall of green in a transcript whose job is to be read, and
# it has to be centred by hand — a job the transcript's own reflow then undoes
# on the next rail drag. This is four rows of a two-column mark beside text,
# which stays legible in a narrow pane and centres itself at any width.
# ---------------------------------------------------------------------------

#: The mark, four rows. Drawn by hand with half-blocks rather than run through
#: FONT: the 5x6 block font has no concept of them, so there is nothing to
#: reuse. It is a rounded block, the shape a terminal can actually hold —
#: a circular logo would need far more rows to read as one.
EMBLEM: list[str] = [
    " ▄███▄ ",
    "███████",
    "███████",
    " ▀███▀ ",
]

EMBLEM_GAP = "  "

#: The emblem's fixed width, in columns. Every spin frame is padded to this so
#: the boot screen's layout does not jump as the mark turns.
EMBLEM_WIDTH = max(len(row) for row in EMBLEM)

# ---------------------------------------------------------------------------
# The spin. The operator asked for the logo to turn on load ("so it will look
# cool"), so the emblem above is rotated on its vertical axis — a coin-spin.
#
# Two properties matter more than the look, and both are load-bearing:
#
#   * The width narrows and the fill lightens as the mark turns away, so it
#     reads as *turning* rather than shrinking. A frame that only got narrower
#     would look like the logo being squeezed.
#   * The frames facing the viewer and facing away both return ``EMBLEM``
#     itself, so the animation settles onto the static mark with no visible
#     jump. Without that the last frame would snap — a flat ``▄▄▄▄▄`` cap
#     against the rounded ``▄███▄``.
# ---------------------------------------------------------------------------

#: Frames per revolution, and how long each is held. Twelve frames over 1.08s
#: reads as a smooth turn. The duration is not arbitrary: the boot screen ticks
#: every 0.045s, and 1.08 / 0.045 / 12 is exactly 2, so each frame is held for
#: a whole number of ticks and the two clocks agree. At 0.96s they did not —
#: the tick loop could only manage 1.08s, so the constant was a lie.
SPIN_FRAMES = 12
SPIN_SECONDS = 1.08
SPIN_STEP = SPIN_SECONDS / SPIN_FRAMES

#: Geometry for one half-turn, indexed by how far round the mark is: entry 0 is
#: facing the viewer, the middle entry is edge-on, and the second half of the
#: revolution is this table read backwards. Deriving the second half rather
#: than tabulating it is what makes the spin *exactly* symmetric.
#:
#: That symmetry is not a nicety. Scaling the width by ``|cos theta|`` directly
#: was tried first and produced a visible artifact: the mirror-image frames are
#: both ``|cos| == 0.5``, but ``cos(pi/3)`` and ``cos(2pi/3)`` land either side
#: of 0.5 in floating point, so one frame rendered three columns wide and its
#: own mirror rendered four — and the shading glyph differed too. Folding the
#: index removes the float boundary from the picture entirely.
SPIN_WIDTHS = (7, 5, 3, 1, 3, 5, 7)
SPIN_SHADES = "██▒▒██"


def spin_frame(index: int) -> list[str]:
    """The emblem at frame *index* of ``SPIN_FRAMES``, as 4 rows of EMBLEM_WIDTH.

    The two "facing" frames — toward the viewer and away from it — return
    ``EMBLEM`` itself. A symmetric mark has the same silhouette at 0 and 180
    degrees, so that is both correct and what lets the animation settle onto
    the static mark with no visible jump.
    """
    i = index % SPIN_FRAMES
    half = SPIN_FRAMES // 2
    fold = i if i <= half else SPIN_FRAMES - i
    if fold in (0, half):
        return list(EMBLEM)

    outw = SPIN_WIDTHS[fold]
    shade = SPIN_SHADES[fold]
    rows: list[str] = []
    for r in range(len(EMBLEM)):
        if r in (0, len(EMBLEM) - 1):
            # Top and bottom keep their half-block caps — that rounding is what
            # makes it read as the same mark at any width.
            cap = "▄" if r == 0 else "▀"
            n = max(1, outw - 2)
            pad = " " * ((outw - n) // 2)
            seg = pad + cap * n + pad
        else:
            seg = shade * outw
        rows.append(seg.center(EMBLEM_WIDTH))
    return rows


def emblem_frame(theta: float) -> list[str]:
    """The emblem at angle *theta* (radians), as 4 rows of EMBLEM_WIDTH cols.

    A coin-spin: the mark narrows and lightens as it turns edge-on, then widens
    again. Rounds to the nearest frame rather than flooring, so a caller
    stepping by ``2*pi / SPIN_FRAMES`` lands on each frame exactly once.
    """
    return spin_frame(int(round(theta / (2 * math.pi) * SPIN_FRAMES)))


def spin_frame_index(elapsed: float) -> int:
    """Which of ``SPIN_FRAMES`` is showing at *elapsed* seconds.

    Pure and explicit — the time is a parameter, never read from a clock
    inside. That is the ``spinner_at(now)`` idiom from ``web.py``, and it is
    deliberate: the one flaky check in the suite is flaky precisely because the
    other animation reads ``time.monotonic()`` internally with no injectable
    ``now``. Taking the time as an argument means this cannot join it.
    """
    return int(elapsed / SPIN_STEP) % SPIN_FRAMES


def splash_mark(palette: Palette, model: str = "", base_url: str = "") -> Text:
    """The splash banner: emblem and name side by side, as one renderable.

    Returned as a single ``Text`` rather than a list of lines so the caller can
    hand it to ``Align.center`` — which is what centres it, and re-centres it
    every time the transcript reflows to a new width. Centring this by padding
    the strings instead would look right at launch and drift off-centre the
    first time a rail was dragged, which is the fault this replaces.

    The lower rows carry the model and the endpoint, so the banner is the one
    place that says what this session is actually pointed at.
    """
    right = [
        ("ZIMZILLA", f"bold {palette.accent}"),
        (banner_subtitle(), palette.primary),
        (model, palette.dim),
        ("type /help for commands" + (f"   ·   {base_url}" if base_url else ""),
         palette.dim),
    ]
    width = max(len(row) for row in EMBLEM)
    t = Text()
    for i, row in enumerate(EMBLEM):
        t.append(row.ljust(width) + EMBLEM_GAP, style=f"bold {palette.accent}")
        t.append(*right[i])
        if i < len(EMBLEM) - 1:
            t.append("\n")
    return t
