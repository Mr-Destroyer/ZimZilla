"""Hacker themes for the TUI.

Three variants, each a tight palette so the whole UI reads as one neon system —
no stray colors anywhere.

The background is the terminal's own rather than a hardcoded black. A terminal
paints its padding in its own background colour, and a TUI can only paint inside
the text grid, so a hardcoded black leaves a frame of the terminal's colour
around the whole interface — clearest at the corners, where the terminal's own
rounding curves it. `__main__` asks the terminal what colour that is (OSC 11)
and hands it to `get_palette`; the black below is the fallback for terminals
that do not answer.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    primary: str
    dim: str
    accent: str
    red: str
    amber: str
    glow: str

    def rich_theme(self):
        """Build a Textual Theme from this palette."""
        from textual.theme import Theme

        return Theme(
            name=self.name,
            primary=self.primary,
            secondary=self.dim,
            accent=self.accent,
            foreground=self.primary,
            background=self.bg,
            success=self.accent,
            warning=self.amber,
            error=self.red,
            surface=self.bg,
            panel=self.bg,
            dark=True,
        )


PALETTES: dict[str, Palette] = {
    "green": Palette(
        name="zim-green",
        bg="#000000",
        primary="#00ff41",
        dim="#0b6e2a",
        accent="#00ffaa",
        red="#ff2d2d",
        amber="#ffb000",
        glow="#00ff41",
    ),
    "amber": Palette(
        name="zim-amber",
        bg="#000000",
        primary="#ffb000",
        dim="#7a5200",
        accent="#ffd166",
        red="#ff2d2d",
        amber="#ffe08a",
        glow="#ffb000",
    ),
    "cyan": Palette(
        name="zim-cyan",
        bg="#000000",
        primary="#00ffd5",
        dim="#00706a",
        accent="#7dfff0",
        red="#ff2d2d",
        amber="#ffb000",
        glow="#00ffd5",
    ),
}

DEFAULT_THEME = "green"


def get_palette(name: str, bg: str | None = None) -> Palette:
    """The palette for ``name``, on ``bg`` if given.

    ``bg`` comes from the terminal itself (see the module docstring). Omitting it
    — or passing None — keeps the palette's own black, which is what happens on
    a terminal that does not answer OSC 11.
    """
    palette = PALETTES.get(name, PALETTES[DEFAULT_THEME])
    return dataclasses.replace(palette, bg=bg) if bg else palette


# Hacker verbs cycled by the "thinking" spinner.
THINKING_VERBS = [
    "decrypting",
    "probing",
    "injecting",
    "enumerating",
    "sniffing",
    "triangulating",
    "fuzzing",
    "exfiltrating",
    "obfuscating",
    "handshaking",
    "compiling",
    "negotiating",
    "tracing",
    "resolving",
    "mutating",
    "hashing",
]

# Icons per tool, for panel headers.
TOOL_ICONS = {
    "bash": "▚",
    "read_file": "≡",
    "write_file": "✎",
    "edit_file": "±",
    "glob": "✳",
    "grep": "⌕",
    "list_dir": "▤",
    "search_web": "⌕",
    "web_fetch": "⇣",
}
