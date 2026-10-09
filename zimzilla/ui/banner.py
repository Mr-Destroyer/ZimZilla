"""The ZIMZILLA ASCII banner, in a few styles.

Each glyph is 6 rows tall. The boot screen reveals them with a per-column
glitch/typewriter animation.
"""

from __future__ import annotations

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
