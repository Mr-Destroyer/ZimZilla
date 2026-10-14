"""Hacker themes for the TUI.

Three fixed variants, each a tight palette so the whole UI reads as one neon
system — no stray colors anywhere. Plus ``system``, which has no colours of its
own: it takes the terminal's.

The background is the terminal's own rather than a hardcoded black. A terminal
paints its padding in its own background colour, and a TUI can only paint inside
the text grid, so a hardcoded black leaves a frame of the terminal's colour
around the whole interface — clearest at the corners, where the terminal's own
rounding curves it. `__main__` asks the terminal what colour that is (OSC 11)
and hands it to `get_palette`; the black below is the fallback for terminals
that do not answer.

``system`` goes further and adopts the terminal's whole palette — background,
foreground and the sixteen ANSI slots — so a scheme generator that re-themes
the terminal (omarchy's ``aether``, which derives a scheme from each wallpaper)
re-themes ZimZilla with it. See ``termcolors.py`` for how those are read. A slot
the terminal does not report falls back to the green palette's value, so a
partial answer degrades to something readable rather than to a hole.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import graph flat
    from .termcolors import TerminalColors

#: Where the last-picked theme is remembered. Mirrors the ``current-source``
#: file the upstream selection uses (``sources.py``), for the same reason: the
#: choice should survive a restart without a config file to edit.
ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))


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

#: The theme that takes its colours from the terminal rather than declaring
#: them. It is not a key in ``PALETTES`` because there is nothing to look up —
#: see ``system_palette``.
SYSTEM_THEME = "system"

#: Every theme name, in the order they are offered. The single source of truth:
#: the CLI's ``--theme`` choices, ``/theme``'s validation and help, the
#: completion popup and the palette menu all read this, because the names had
#: already been written out in six places and drifted.
THEME_NAMES: list[str] = [*PALETTES, SYSTEM_THEME]


def system_palette(colors: "TerminalColors | None" = None) -> Palette:
    """A palette built from the terminal's own colours.

    The role mapping is the conventional one, so a scheme generator's intent
    comes through: the foreground becomes ``primary``, ANSI 8 (bright black) is
    the natural ``dim``, cyan and yellow are ``accent`` and ``amber``, and red
    is ``red``. ``glow`` follows ``primary`` exactly as the green palette's does.

    Every slot falls back to the green palette's value independently, so a
    terminal that answers only some of the sixteen degrades field by field
    rather than all at once — and a terminal that answers nothing gives exactly
    the green palette, which is the honest default and not a broken one.
    """
    base = PALETTES[DEFAULT_THEME]
    if colors is None:
        return base

    primary = colors.fg or base.primary
    return Palette(
        name="zim-system",
        # ``.bg``, not ``.dark_bg``: a system palette is readable on a light
        # terminal because its text colour came from the same place. The three
        # fixed palettes are the ones that must refuse light, and they still do.
        bg=colors.bg or base.bg,
        primary=primary,
        dim=colors.ansi.get(8) or base.dim,
        accent=colors.ansi.get(6) or colors.ansi.get(4) or base.accent,
        red=colors.ansi.get(1) or base.red,
        amber=colors.ansi.get(3) or base.amber,
        glow=primary,
    )


def get_palette(name: str, bg: str | None = None, *,
                colors: "TerminalColors | None" = None) -> Palette:
    """The palette for ``name``, on ``bg`` if given.

    ``bg`` comes from the terminal itself (see the module docstring). Omitting it
    — or passing None — keeps the palette's own black, which is what happens on
    a terminal that does not answer OSC 11.

    ``colors`` is the full terminal palette, used only by ``system``. It is
    keyword-only and optional so every existing caller — and every test that
    calls ``get_palette("green", "#101315")`` — keeps its exact meaning.
    """
    if name == SYSTEM_THEME:
        return system_palette(colors)
    palette = PALETTES.get(name, PALETTES[DEFAULT_THEME])
    return dataclasses.replace(palette, bg=bg) if bg else palette


# ---------------------------------------------------------------------------
# the remembered theme (persisted so a restart keeps the last choice)
# ---------------------------------------------------------------------------

def _theme_file() -> Path:
    return ZIMZILLA_HOME / "current-theme"


def current_theme() -> str | None:
    """The theme last selected, if it is still one we know.

    ``None`` when nothing was ever saved, when the file cannot be read, or when
    it names a theme that no longer exists — an unknown name is ignored rather
    than obeyed, so a file left by an older or newer build cannot put the UI
    into a state it has no palette for.
    """
    try:
        name = _theme_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name if name in THEME_NAMES else None


def set_current_theme(name: str) -> None:
    """Record the choice. Never fatal — a read-only home must not break the TUI."""
    if name not in THEME_NAMES:
        return
    try:
        ZIMZILLA_HOME.mkdir(parents=True, exist_ok=True)
        _theme_file().write_text(name + "\n", encoding="utf-8")
    except OSError:
        pass


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

# One colour per team worker, assigned by roster position so a worker keeps the
# same colour for its whole run. `/team` interleaves every worker into a single
# transcript, and the colour is what tells them apart at a glance.
#
# These are deliberately NOT drawn from the active palette: with three workers
# on screen the point is that they differ from each other, and a palette's own
# primary/accent/dim are already spoken for by the surrounding chrome. Each is
# bright enough to read on any of the palettes' backgrounds.
AGENT_COLORS = [
    "#00ffaa",  # aqua
    "#ffb000",  # amber
    "#7dfff0",  # ice
    "#ff8fd0",  # rose
    "#b6ff6b",  # lime
    "#ff6b6b",  # red
]


def agent_color(index: int, palette: Palette | None = None) -> str:
    """The colour for worker ``index``. Wraps, so any roster size is covered."""
    return AGENT_COLORS[index % len(AGENT_COLORS)]


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
