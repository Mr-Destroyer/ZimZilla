"""The ZIMZILLA ASCII banner, in a few styles.

Each glyph is 6 rows tall. The boot screen reveals them with a per-column
glitch/typewriter animation.
"""

from __future__ import annotations

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
