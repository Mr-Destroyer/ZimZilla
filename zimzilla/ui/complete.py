"""Inline completion popup for the prompt.

Two triggers:

* a leading ``/``  → slash-command menu (opens as soon as the slash is typed)
* an ``@token``    → file / directory picker rooted at the working directory

The popup renders as a bordered panel docked just above the prompt, and never
takes focus — the ``Input`` keeps it, so typing continues to work normally.
Up/Down move the selection, Tab/Enter accept, Esc dismisses.
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..osint import KIND_ORDER as OSINT_KIND_ORDER
from ..theme import Palette

# (command, description)
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "show the command reference"),
    ("/mode", "switch mode: auto | edits | plan | zim | danger | uncensored"),
    ("/model", "show or switch the model"),
    ("/cost", "session token and cost breakdown"),
    ("/scope", "show scope guard status"),
    ("/rain", "toggle the matrix-rain background"),
    ("/theme", "switch palette: green | amber | cyan"),
    ("/zim-logfare", "switch upstream to Logfare (:4001)"),
    ("/zim-tokenjuice", "switch upstream to Token Juice (:4000)"),
    ("/zim-source", "show upstream sources and status"),
    ("/zim-tokenharbour", "switch upstream to TokenHarbour (hosted)"),
    ("/tokenharbour-api-setup", "store your TokenHarbour API key"),
    ("/tokenharbour-models", "fetch the live catalog; show what is free"),
    ("/save", "write the session to disk"),
    ("/load", "restore a saved session"),
    ("/compact", "summarise history to free context"),
    ("/team", "fan the task out across parallel agents"),
    ("/osint", "open-source recon on a target"),
    ("/phish", "clone a login page and harvest creds"),
    ("/bug-hunt", "recon, then waves of 10 agents until stopped"),
    ("/stop-hunt", "end a running hunt"),
    ("/summary-hunt", "write the hunt report"),
    ("/findings", "the hunt finding tracker"),
    ("/clear", "wipe transcript and history"),
    ("/exit", "leave the harness"),
]

MODE_NAMES = ["auto", "edits", "plan", "zim", "danger", "uncensored"]
THEME_NAMES = ["green", "amber", "cyan"]

MAX_ROWS = 8
MAX_FILES = 200


def _iter_files(root: Path, limit: int = MAX_FILES) -> list[str]:
    """Up to *limit* relative paths under *root*, skipping heavy directories.

    Walks with ``os.walk`` rather than ``sorted(root.rglob("*"))``: sorting a
    generator materialises the WHOLE tree first, so the ``limit`` check could
    only ever run after every path had been visited — on a large repo that is a
    visible stall the first time the popup opens. os.walk prunes as it goes, so
    the work is genuinely bounded by *limit*.

    The result is still sorted for a stable display order, which sorts only the
    (at most *limit*) entries actually collected.
    """
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
            ".pytest_cache", ".ruff_cache", "dist", "build", ".tox", ".idea"}
    out: list[str] = []
    root = root.resolve()
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune in place so os.walk never descends into heavy directories.
            dirnames[:] = sorted(d for d in dirnames if d not in skip)
            here = Path(dirpath)
            for name in dirnames:
                if len(out) >= limit:
                    return sorted(out)
                try:
                    out.append(str((here / name).relative_to(root)) + "/")
                except ValueError:
                    continue
            for name in sorted(filenames):
                if len(out) >= limit:
                    return sorted(out)
                try:
                    out.append(str((here / name).relative_to(root)))
                except ValueError:
                    continue
    except Exception:
        pass
    return sorted(out)


class CompletionPopup(VerticalScroll):
    """Floating list of completions; does not take focus."""

    DEFAULT_CSS = """
    CompletionPopup {
        display: none;
        height: auto;
        max-height: 10;
        margin: 0 1;
        border: round $secondary;
        background: $background;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        scrollbar-color: $secondary;
    }
    CompletionPopup.visible { display: block; }
    CompletionPopup Static { width: 100%; height: auto; }
    """

    can_focus = False

    def __init__(self, palette: Palette, **kwargs) -> None:
        super().__init__(**kwargs)
        self.palette = palette
        self.items: list[tuple[str, str]] = []  # (value, description)
        self.index = 0
        self.kind = ""  # "slash" | "file"
        #: The live endpoint, so `/model <TAB>` can offer that upstream's
        #: catalog. Set by the app whenever the source changes.
        self.base_url = ""
        self._body = Static(id="complete-body")
        self._files: list[str] | None = None

    def compose(self):
        yield self._body

    # ---- data -------------------------------------------------------------
    def _file_pool(self, workdir: Path) -> list[str]:
        if self._files is None:
            self._files = _iter_files(workdir)
        return self._files

    def refresh_for(self, text: str, workdir: Path) -> bool:
        """Recompute completions for the current prompt text. Returns visibility."""
        kind = ""
        query = ""
        items: list[tuple[str, str]] = []

        if text.startswith("/"):
            kind = "slash"
            query = text.split()[0].lower() if text.split() else text.lower()
            if " " in text.strip():
                # past the command word — offer sub-arguments
                parts = text[1:].split()
                cmd = "/" + parts[0].lower() if parts else "/"
                arg = parts[1].lower() if len(parts) > 1 else ""
                pool = {
                    "mode": MODE_NAMES,
                    "theme": THEME_NAMES,
                    "osint": OSINT_KIND_ORDER,
                    "phish": ["stop", "status"],
                }.get(parts[0].lower(), [])
                if parts[0].lower() == "model":
                    # Offer whatever the active upstream actually serves, so
                    # Logfare's and TokenHarbour's fetched catalogs show up here
                    # too. Imported locally to keep this module free of a
                    # package-level cycle.
                    from .. import sources as sources_mod
                    from ..config import KNOWN_MODELS
                    # None means "no live catalog" — a cold cache or an offline
                    # box. Falling back to the declared list keeps the popup
                    # useful there; an empty popup would look like a bug.
                    pool = list(sources_mod.models_for(self.base_url)
                                or KNOWN_MODELS)
                items = [(a, "") for a in pool if a.startswith(arg) and a != arg]
            else:
                items = [(c, d) for c, d in SLASH_COMMANDS if c.startswith(query)]
        else:
            # Find an @token that the cursor is inside.
            at = None
            for i in range(len(text) - 1, -1, -1):
                ch = text[i]
                if ch == "@":
                    at = i
                    break
                if ch in " \t":
                    break
            if at is not None:
                kind = "file"
                query = text[at + 1:]
                if query and not any(c in query for c in "\"'|;&"):
                    pool = self._file_pool(workdir)
                    q = query.lower()
                    items = [(f, "") for f in pool if q in f.lower()][:MAX_FILES]

        self.kind = kind
        self.items = items[:MAX_ROWS] if kind == "slash" else items[:MAX_ROWS]
        if not items:
            self._hide()
            return False
        self.index = min(self.index, len(self.items) - 1)
        if self.index < 0:
            self.index = 0
        self._paint()
        if not self.has_class("visible"):
            self.add_class("visible")
        return True

    # ---- selection --------------------------------------------------------
    def move(self, delta: int) -> None:
        if not self.items:
            return
        self.index = (self.index + delta) % len(self.items)
        self._paint()

    def current(self) -> str | None:
        if not self.items:
            return None
        return self.items[self.index][0]

    def accept(self, text: str) -> str | None:
        """Return the new prompt text after accepting the selection."""
        value = self.current()
        if value is None:
            return None
        if self.kind == "slash":
            if " " in text.strip() and text.strip().split()[0].startswith("/"):
                parts = text.split()
                return " ".join([parts[0], value]) + " "
            return value + " "
        # file: replace the @token at/after the last '@'
        at = text.rfind("@")
        if at < 0:
            return None
        return text[: at + 1] + value + " "

    def _hide(self) -> None:
        if self.has_class("visible"):
            self.remove_class("visible")

    def set_palette(self, palette: Palette) -> None:
        self.palette = palette
        self._paint()

    # ---- rendering --------------------------------------------------------
    def _paint(self) -> None:
        p = self.palette
        t = Text()
        for i, (value, desc) in enumerate(self.items):
            selected = i == self.index
            prefix = "▸ " if selected else "  "
            if selected:
                t.append(prefix, style=f"bold {p.accent}")
                t.append(value, style=f"bold {p.accent}")
                if desc:
                    t.append("  " + desc, style=p.primary)
            else:
                t.append(prefix, style=p.dim)
                t.append(value, style=p.primary)
                if desc:
                    t.append("  " + desc, style=p.dim)
            if i < len(self.items) - 1:
                t.append("\n")
        hint = "  ↑↓ select · tab accept · esc close"
        t.append("\n" + hint, style=p.dim)
        self._body.update(t)
        try:
            self.scroll_to(y=self.index, animate=False)
        except Exception:
            pass
