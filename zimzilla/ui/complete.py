"""Inline completion popup for the prompt.

Two triggers:

* a leading ``/``       → slash-command menu (opens as soon as the slash is typed)
* ``@`` anywhere in the line → file / directory picker rooted at the working
  directory, opened by the bare ``@`` and then narrowed as the path is typed

The popup renders as a bordered panel docked just above the prompt, and never
takes focus — the ``Input`` keeps it, so typing continues to work normally.
Up/Down move the selection, Tab/Enter accept, Esc dismisses.

The selection logic — what a query offers and in what order — is module-level
and free of widget state, so it can be exercised without a running app; the
class below is only the data holder and the renderer.
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..osint import KIND_ORDER as OSINT_KIND_ORDER
from ..theme import THEME_NAMES, Palette

# (command, description)
SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/help", "show the command reference"),
    ("/mode", "switch mode: auto | edits | plan | zim | danger | uncensored"),
    ("/model", "show or switch the model"),
    ("/cost", "session token and cost breakdown"),
    ("/scope", "show scope guard status"),
    ("/rain", "toggle the matrix-rain background"),
    ("/think", "toggle showing the model's own reasoning"),
    ("/effort", "raise or lower how hard the model thinks: low | medium | high | off"),
    ("/theme", "switch palette: green | amber | cyan | system"),
    ("/zim-logfare", "switch upstream to Logfare (direct, all models)"),
    ("/zim-tokenjuice", "Token Juice — unlimited quota paused, back in a few days"),
    ("/tokenjuice-api-setup", "your own tj_ key — or fall back to the shared one"),
    ("/zim-source", "show upstream sources and status"),
    ("/zim-tokenharbour", "switch upstream to TokenHarbour (hosted)"),
    ("/zim-openrouter", "switch upstream to OpenRouter (hosted)"),
    ("/zim-unorouter", "switch upstream to UnoRouter (hosted, free models)"),
    ("/zim-aihubmix", "switch upstream to AIHubMix (hosted, free models)"),
    ("/zim-beatapi", "switch upstream to BeatAPI (hosted, free models)"),
    ("/zim-iteracompute", "switch upstream to IteraCompute (local proxy, free models)"),
    ("/zim-infronai", "switch upstream to InfronAI (local proxy, free models)"),
    ("/zim-nvidia", "switch upstream to NVIDIA NIM (direct, free dev tier)"),
    ("/zim-antseed", "switch upstream to AntSeed (local buyer, free peers)"),
    ("/tokenharbour-api-setup", "store your TokenHarbour API key"),
    ("/tokenharbour-models", "fetch the live catalog; show what is free"),
    ("/openrouter-api-setup", "store your OpenRouter API key"),
    ("/unorouter-api-setup", "store your UnoRouter API key"),
    ("/unorouter-models", "the live free catalog — what is free and usable"),
    ("/aihubmix-api-setup", "store your AIHubMix API key"),
    ("/aihubmix-models", "the live free catalog — what is free and usable"),
    ("/beatapi-api-setup", "store your BeatAPI API key"),
    ("/beatapi-models", "the live free catalog — what is free and usable"),
    ("/iteracompute-api-setup", "store your IteraCompute API key"),
    ("/iteracompute-models", "the live free catalog — what is free and usable"),
    ("/infronai-api-setup", "store your InfronAI API key"),
    ("/infronai-models", "the live free catalog — what is free and usable"),
    ("/nvidia-api-setup", "store your NVIDIA API key (free developer tier)"),
    ("/nvidia-models", "the live catalog — what this harness can drive"),
    ("/antseed-models", "list AntSeed's free models, with the peer to pin"),
    ("/ldxhub-api-setup", "store your LDXHub API key (document jobs)"),
    ("/ldxhub-models", "LDXHub models/engines — pick the default per service"),
    ("/models", "free models of an upstream — /models [source]"),
    ("/zim-opencode", "open opencode behind a fresh rotating IP"),
    ("/save", "write the session to disk"),
    ("/load", "restore a saved session"),
    ("/compact", "summarise history to free context"),
    ("/limits", "context window, round caps and timeouts"),
    ("/team", "fan the task out across parallel agents"),
    ("/osint", "open-source recon on a target"),
    ("/osint-tree", "re-show a finished recon as a result tree"),
    ("/leadgen", "local leads: passive audit + outreach drafts"),
    ("/shodan-api-setup", "store your Shodan API key"),
    ("/shodan-cam-search", "exposed IP cameras, then analyse the result"),
    ("/shodan-scada-search", "exposed ICS / SCADA — PLCs, RTUs, HMIs"),
    ("/shodan-db-search", "exposed databases — Mongo, ES, Redis, SQL"),
    ("/shodan-rdp-search", "exposed RDP and VNC services"),
    ("/shodan-router-search", "exposed routers, firewalls and VPNs"),
    ("/shodan-vuln-search", "hosts Shodan flagged with a known CVE"),
    ("/shodan-search", "run any Shodan query verbatim, then analyse"),
    ("/phish", "clone a login page and harvest creds"),
    ("/image-gen", "generate an image from a prompt — saves under images/"),
    ("/payload", "build a payload for a file extension — apk, exe, elf, php…"),
    ("/obfuscate", "run a payload through one or more msfvenom encoders"),
    ("/scope-validator", "paste a programme overview → scope files + ranked targets"),
    ("/bug-hunt", "recon, then one wave of 10 agents"),
    ("/ad", "Active Directory campaign — gated and interlocked"),
    ("/reverse-engineer", "map an artifact, then choose what to change"),
    ("/reverse-engineer-tools", "RE tooling: what is here, what is missing"),
    ("/crack", "crack hashes offline — CPU only, budgeted"),
    ("/stop-hunt", "end a running hunt"),
    ("/summary-hunt", "the hunt report — what each wave did"),
    ("/report-bug-hunt", "evidence-only report, every wave"),
    ("/findings", "the hunt finding tracker"),
    ("/clear", "wipe transcript and history"),
    ("/exit", "leave the harness"),
]

MODE_NAMES = ["auto", "edits", "plan", "zim", "danger", "uncensored"]
# THEME_NAMES is imported from theme.py rather than declared here, so the popup
# and `/theme` cannot disagree about which themes exist — the two lists had
# already drifted apart once.

MAX_ROWS = 8
MAX_FILES = 200

#: Never descended into. These are the directories whose contents are not what
#: anyone means by "the files in this project", and on a real repo they are the
#: bulk of it.
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".tox", ".idea",
})


def _walk(base: Path, label: str = "", limit: int = MAX_FILES) -> list[str]:
    """Up to *limit* entries under *base*, each prefixed with *label*.

    Walks with ``os.walk`` rather than ``sorted(base.rglob("*"))``: sorting a
    generator materialises the WHOLE tree first, so the ``limit`` check could
    only ever run after every path had been visited — on a large repo that is a
    visible stall the first time the popup opens. os.walk prunes as it goes, so
    the work is genuinely bounded by *limit*.

    The result is still sorted for a stable display order, which sorts only the
    (at most *limit*) entries actually collected. Directories carry a trailing
    slash, which is how the rest of this module tells them from files.
    """
    out: list[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(base):
            # Prune in place so os.walk never descends into heavy directories.
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            here = Path(dirpath)
            for name in dirnames:
                if len(out) >= limit:
                    return sorted(out)
                try:
                    out.append(label + str((here / name).relative_to(base)) + "/")
                except ValueError:
                    continue
            for name in sorted(filenames):
                if len(out) >= limit:
                    return sorted(out)
                try:
                    out.append(label + str((here / name).relative_to(base)))
                except ValueError:
                    continue
    except Exception:
        pass
    return sorted(out)


def _iter_files(root: Path, limit: int = MAX_FILES) -> list[str]:
    """Up to *limit* paths under *root*, relative to it, heavy directories skipped."""
    return _walk(Path(root).resolve(), "", limit)


def _iter_under(root: Path, prefix: str, limit: int = MAX_FILES) -> list[str]:
    """Up to *limit* paths under ``root/prefix``, still relative to *root*.

    This exists because the flat pool is capped. On a tree larger than
    ``MAX_FILES`` the entries a typed path segment is aiming at can be missing
    from that pool entirely, so ``@zimzilla/ui/app`` would offer nothing at all
    — the one query where the operator knows exactly what they want. Walking
    the named subtree is bounded by the same *limit*: it visits one directory
    branch, not the tree.

    Anything that is not a directory inside *root* returns ``[]``. The prefix is
    operator-typed text, and ``/etc`` or ``../../..`` has to mean "no such
    directory here" rather than "walk the filesystem". Resolving before the
    containment check is what makes a symlink out of the tree read the same way.
    """
    root = Path(root).resolve()
    segments = prefix.split("/")
    # The skip list is a prune on the walk, and a prefix that names one of
    # those directories is asking for exactly the thing the prune refuses —
    # `.git/` would otherwise offer 168 object files, and `node_modules/` is
    # worse. Refused here rather than filtered out later, so the walk that
    # would have produced them never starts.
    if (not prefix or prefix.startswith("/") or ".." in segments
            or segments[0] in _SKIP_DIRS):
        return []
    try:
        base = (root / prefix).resolve()
    except OSError:
        return []
    if base == root or not base.is_dir() or not base.is_relative_to(root):
        return []
    return _walk(base, prefix + "/", limit)


def _split(path: str) -> tuple[str, str]:
    """``(name, parent)`` for display, keeping a directory's trailing slash.

    Rows render as the name with its directory dimmed beside it. The row is the
    same path either way, but a deep tree is unreadable when every entry opens
    with the same four segments and the part that differs sits off the edge.
    """
    is_dir = path.endswith("/")
    body = path[:-1] if is_dir else path
    if "/" not in body:
        return (path, "")
    parent, _, name = body.rpartition("/")
    return (name + ("/" if is_dir else ""), parent + "/")


def _is_subsequence(needle: str, haystack: str) -> bool:
    """Whether every character of *needle* appears in *haystack*, in order."""
    it = iter(haystack)
    # `ch in it` scans the iterator and consumes what it passes, so each
    # character is only ever matched against what follows the previous one.
    return all(ch in it for ch in needle)


def _match_rank(path: str, query: str) -> tuple[int, int] | None:
    """How well *path* answers a lowercased *query*. Lower is better.

    Plain substring matching — what this did before — ranks every hit equally
    and then falls back on walk order, so the file the operator is aiming at
    can sit below the fold behind one they were not. The tiers are ordered by
    how much of the typed text the *name* itself accounts for: a name starting
    with it beats a name containing it, which beats a match anywhere in the
    path. The second element breaks ties toward the shallower path.
    """
    name = path[:-1] if path.endswith("/") else path
    base = name.rsplit("/", 1)[-1].lower()
    low = path.lower()
    if base.startswith(query):
        return (0, len(path))
    if query in base:
        return (1, len(path))
    if low.startswith(query):
        return (2, len(path))
    if query in low:
        return (3, len(path))
    # Last resort, and only for a query long enough to mean something: a single
    # character is a subsequence of nearly every path, which fills the popup
    # with noise instead of narrowing it.
    if len(query) > 1 and _is_subsequence(query, low):
        return (4, len(path))
    return None


def _at_token(text: str, cursor: int) -> int | None:
    """Index of the ``@`` whose mention the cursor sits in, or ``None``.

    Scans back from the cursor, not from the end of the line, so the popup
    follows the mention being edited rather than whichever ``@`` happens to be
    furthest right — and so editing the middle of a prompt still offers the
    token under the caret.
    """
    for i in range(min(cursor, len(text)) - 1, -1, -1):
        ch = text[i]
        if ch == "@":
            return i
        if ch in " \t":
            return None
    return None


def file_candidates(pool: list[str], root: Path,
                    query: str) -> tuple[list[tuple[str, str]], str]:
    """What to offer for a file *query*: ``(value, subject)`` pairs and what to score.

    A query naming a real directory descends into it and scores only the rest,
    so ``@zimzilla/ui/ap`` ranks ``.../app.py`` on ``ap`` rather than on the
    whole typed string. That is what keeps the popup usable past the first
    level. Anything else is scored against the flat pool exactly as typed, so a
    partial path segment with no directory behind it (``@ui/comp``) still finds
    its file.
    """
    if "/" in query:
        head, _, tail = query.rpartition("/")
        sub = _iter_under(root, head)
        if sub:
            cut = len(head) + 1
            return ([(v, v[cut:]) for v in sub], tail)
    return ([(v, v) for v in pool], query)


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

    def refresh_for(self, text: str, workdir: Path,
                    cursor: int | None = None) -> bool:
        """Recompute completions for the prompt text. Returns visibility.

        *cursor* is where the caret is, which for a mention is the only place
        that matters; ``None`` means "at the end", which is what a caller with
        no Input to hand can honestly say.
        """
        if cursor is None or cursor < 0 or cursor > len(text):
            cursor = len(text)
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
                    # Only `tools` — the rest of `/ad`'s arguments are flags,
                    # and the target is a domain the operator has to type
                    # anyway. Offering the flags here would put a `--ack` in
                    # the popup that cannot be accepted without the domain it
                    # is a confirmation of.
                    "ad": ["tools"],
                    # The argument exists for OpenRouter, UnoRouter and AntSeed,
                    # whose catalogs are public and worth seeing before a key is
                    # set (AntSeed has no key at all). Offering all of them rather
                    # than just those three keeps the popup honest about what
                    # /models accepts.
                    "models": ["logfare", "tokenjuice", "tokenharbour",
                               "openrouter", "unorouter", "antseed", "aihubmix",
                               "beatapi", "iteracompute", "infronai", "nvidia"],
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
            at = _at_token(text, cursor)
            if at is not None:
                kind = "file"
                query = text[at + 1:cursor]
                # A quote or a shell metacharacter in the token means this is
                # prose or a pipeline, not a mention being typed out.
                if not any(c in query for c in "\"'|;&"):
                    # A file the agent wrote since the popup last closed has to
                    # become mentionable, so the walk is redone each time the
                    # popup opens rather than cached for the session. Only on
                    # open: re-walking while it is already up would put a
                    # directory scan between the operator and the character
                    # they just pressed.
                    if not self.has_class("visible"):
                        self._files = None
                    cands, subject = file_candidates(
                        self._file_pool(workdir), workdir, query)
                    items = self._rank(cands, subject.lower())

        self.kind = kind
        self.items = items[:MAX_ROWS]
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
    @staticmethod
    def _rank(cands: list[tuple[str, str]], subject: str) -> list[tuple[str, str]]:
        """Order ``(value, subject)`` candidates for *subject*, best first.

        An empty subject keeps the walk's own order — depth-first and
        alphabetical, which is the shape of the tree and what someone who has
        typed a bare ``@`` is looking at. A real subject is ranked by match
        quality and then by path, and anything that does not match is dropped.
        """
        if not subject:
            return [(v, _split(v)[1]) for v, _ in cands]
        scored: list[tuple[tuple[int, int], str]] = []
        for value, subj in cands:
            rank = _match_rank(subj, subject)
            if rank is not None:
                scored.append((rank, value))
        scored.sort(key=lambda rv: (rv[0], rv[1]))
        return [(v, _split(v)[1]) for _, v in scored]

    def move(self, delta: int) -> None:
        if not self.items:
            return
        self.index = (self.index + delta) % len(self.items)
        self._paint()

    def current(self) -> str | None:
        if not self.items:
            return None
        return self.items[self.index][0]

    def accept(self, text: str, cursor: int | None = None) -> str | None:
        """Return the new prompt text after accepting the selection."""
        value = self.current()
        if value is None:
            return None
        if self.kind == "slash":
            if " " in text.strip() and text.strip().split()[0].startswith("/"):
                parts = text.split()
                return " ".join([parts[0], value]) + " "
            return value + " "
        # file: replace the mention the cursor is in, keeping whatever follows
        # it — the mention belongs where the operator is typing, not appended
        # past the end of a line they have already written beyond.
        if cursor is None or cursor < 0 or cursor > len(text):
            cursor = len(text)
        at = _at_token(text, cursor)
        if at is None:
            return None
        # A directory is a step, not a destination: no trailing space, so the
        # next Changed event reopens the popup on that directory's children and
        # the operator keeps descending. A file ends the mention, so it takes a
        # space — unless one is already sitting at the caret, which would leave
        # the sentence with two.
        rest = text[cursor:]
        tail = "" if value.endswith("/") or rest[:1] in (" ", "\t") else " "
        return text[: at + 1] + value + tail + rest

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
            if self.kind == "file":
                # Name first, directory dimmed beside it — the same path, with
                # the end that differs where the eye lands.
                value, desc = _split(value)
            prefix = "▸ " if selected else "  "
            t.append(prefix, style=f"bold {p.accent}" if selected else p.dim)
            t.append(value, style=f"bold {p.accent}" if selected else p.primary)
            if desc:
                t.append("  " + desc, style=p.primary if selected else p.dim)
            if i < len(self.items) - 1:
                t.append("\n")
        hint = "  ↑↓ select · tab accept · esc close"
        t.append("\n" + hint, style=p.dim)
        self._body.update(t)
        try:
            self.scroll_to(y=self.index, animate=False)
        except Exception:
            pass
