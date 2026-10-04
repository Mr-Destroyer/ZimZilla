"""Command palette — Ctrl+K, every action in one fuzzy-searchable list.

The slash commands are discoverable only by reading ``/help``; this makes them
*findable*. Type a few letters, arrow down, Enter. It also reaches things that
have no slash command at all (jump to a file, switch to a model by name).

The palette itself is dumb: the app builds the entries, each with a callable,
and the palette just filters and returns the chosen one. That keeps every
action's definition next to the thing it acts on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from ..theme import Palette

MAX_ROWS = 10


@dataclass
class PaletteEntry:
    """One selectable action."""

    key: str
    title: str
    hint: str = ""
    group: str = ""
    #: What to do when chosen. Built by the app, so the palette needs no
    #: knowledge of the commands it offers.
    run: Callable[[], None] | None = None


def fuzzy(query: str, text: str) -> tuple[int, list[int]] | None:
    """Subsequence match of *query* in *text*.

    Returns ``(score, matched_indices)`` — higher score is better — or ``None``
    if the query's characters do not all appear, in order. Scores reward
    contiguous runs and word-boundary starts, so ``md`` ranks ``/model`` above
    a title where those letters are scattered.
    """
    if not query:
        return (0, [])
    q = query.lower()
    t = text.lower()
    indices: list[int] = []
    score = 0
    prev = -2
    cursor = 0
    for ch in q:
        found = t.find(ch, cursor)
        if found == -1:
            return None
        indices.append(found)
        if found == prev + 1:
            score += 3  # a contiguous run reads as a real prefix
        if found == 0 or t[found - 1] in " /-_.":
            score += 4  # starting a word beats matching mid-word
        score -= min(found - cursor, 4)  # penalise long skips, but not unbounded
        prev = found
        cursor = found + 1
    score -= len(t) // 12  # ties go to the shorter, more specific title
    return (score, indices)


def rank(entries: list[PaletteEntry], query: str) -> list[tuple[PaletteEntry, list[int]]]:
    """Filter and order *entries* for *query*, best first."""
    query = query.strip()
    if not query:
        return [(e, []) for e in entries]
    scored: list[tuple[int, int, PaletteEntry, list[int]]] = []
    for i, e in enumerate(entries):
        hit = fuzzy(query, e.title)
        if hit is None:
            # Fall back to the hint, so "cost" finds "$0.0142 session total".
            hit = fuzzy(query, e.hint)
        if hit is None:
            continue
        score, indices = hit
        scored.append((-score, i, e, indices))
    scored.sort()
    return [(e, idx) for _, _, e, idx in scored]


class CommandPalette(ModalScreen[PaletteEntry]):
    """A centred fuzzy search over the app's actions."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel", priority=True),
        Binding("ctrl+k", "cancel", "cancel", priority=True),
        Binding("down", "move(1)", "down", priority=True),
        Binding("up", "move(-1)", "up", priority=True),
        Binding("ctrl+n", "move(1)", "down", priority=True),
        Binding("ctrl+p", "move(-1)", "up", priority=True),
    ]

    DEFAULT_CSS = """
    CommandPalette {
        align: center top;
        background: rgba(0,0,0,0.75);
    }
    #pal-box {
        width: 76%;
        max-width: 110;
        height: auto;
        margin-top: 3;
        border: heavy $accent;
        background: $background;
        padding: 0 1;
    }
    /* #pal-query is styled in the app's CSS — see the note there. */
    #pal-list { height: auto; padding: 0; }
    #pal-foot { height: 1; }
    """

    def __init__(self, entries: list[PaletteEntry], palette: Palette) -> None:
        super().__init__()
        self.entries = entries
        self.palette = palette
        self.results: list[tuple[PaletteEntry, list[int]]] = []
        self.index = 0
        self._list = Static(id="pal-list")
        self._foot = Static(id="pal-foot")

    def compose(self) -> ComposeResult:
        with Vertical(id="pal-box"):
            yield Input(placeholder="❯ search commands, models, files…", id="pal-query")
            yield self._list
            yield self._foot

    def on_mount(self) -> None:
        self.refilter("")
        self.query_one("#pal-query", Input).focus()

    # ---- data -------------------------------------------------------------
    def refilter(self, query: str) -> None:
        self.results = rank(self.entries, query)
        self.index = 0
        self.render_list()

    def render_list(self) -> None:
        p = self.palette
        t = Text()
        if not self.results:
            t.append("  no matches\n", style=p.dim)
        for i, (entry, indices) in enumerate(self.results[:MAX_ROWS]):
            selected = i == self.index
            t.append("▌ " if selected else "  ", style=f"bold {p.accent}")
            t.append(self._title(entry, indices, selected))
            if entry.hint:
                t.append("  " + entry.hint, style=p.dim)
            if entry.group:
                t.append(f"   [{entry.group}]", style=p.dim)
            t.append("\n")
        if len(self.results) > MAX_ROWS:
            t.append(f"  … {len(self.results) - MAX_ROWS} more\n", style=p.dim)
        self._list.update(t)

        # Plain ASCII for the key hints: the fancier arrows (⏎, ↵) are missing
        # from many terminal fonts and render as a blank box.
        foot = Text()
        foot.append("  ↑↓", style=f"bold {p.accent}")
        foot.append(" move   ", style=p.dim)
        foot.append("enter", style=f"bold {p.accent}")
        foot.append(" run   ", style=p.dim)
        foot.append("esc", style=f"bold {p.accent}")
        foot.append(" close", style=p.dim)
        self._foot.update(foot)

    def _title(self, entry: PaletteEntry, indices: list[int], selected: bool) -> Text:
        """Title with the matched characters picked out."""
        p = self.palette
        base = f"bold {p.accent}" if selected else p.primary
        if not indices:
            return Text(entry.title, style=base)
        marked = set(indices)
        t = Text()
        for i, ch in enumerate(entry.title):
            t.append(ch, style=f"bold {p.accent}" if i in marked else base)
        return t

    # ---- keys -------------------------------------------------------------
    def action_move(self, delta: int) -> None:
        if not self.results:
            return
        limit = min(len(self.results), MAX_ROWS)
        self.index = (self.index + delta) % limit
        self.render_list()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_choose(self) -> None:
        if not self.results:
            self.dismiss(None)
            return
        self.dismiss(self.results[min(self.index, len(self.results) - 1)][0])

    # ---- events -----------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        self.refilter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_choose()
