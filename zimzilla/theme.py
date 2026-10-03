"""Hacker themes for the TUI.

Three variants, all black-background. Each defines a tight palette so the
whole UI reads as one neon system — no stray colors anywhere.
"""

from __future__ import annotations

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


def get_palette(name: str) -> Palette:
    return PALETTES.get(name, PALETTES[DEFAULT_THEME])


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
}
