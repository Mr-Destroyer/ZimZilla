"""`/osint` — read a finished report back as a tree.

`osint.py` *runs* a recon. This module *reads the result*, which turns out to be
a separate problem with its own failure modes, so it lives on its own.

Why it exists: a finished `/osint` run used to end with a path and nothing else.
The product of twenty minutes of recon — who the target is, which breach carries
a plaintext password, which handle is confirmed on four platforms — sat in 300
lines of markdown the operator had to open in another terminal and read top to
bottom. The tree is the shape of that result, legible at a glance.

The hard part is that the reports are **not uniform**. The playbook specifies a
shape (a `## Headline`, a coverage table, numbered sections, and every finding as
a five-line What/Where/Confidence/Means/Verify block), and the model follows it
when it follows it — but of the three real reports on this machine, one is
conformant, one is tables with no finding blocks at all, and one uses a
completely different set of section names. A parser that understood only the
specified shape would render an *empty tree* for two of the three, which is the
worst possible failure: it reads as "the run found nothing" rather than "this
view could not read the report".

So parsing is a ladder, and each rung is exercised by a real report:

  1. **Finding blocks** — a `###` heading whose block carries a
     `**Confidence** — CONFIRMED` line. The richest rung: a real title and a
     real confidence.
  2. **Table rows** — the row's cells are the finding. Used when a block has no
     confidence line but does have a markdown table, which is how two of the
     three reports are written throughout.
  3. **Prose** — a block with neither, such as `## Headline`, becomes a single
     truncated leaf. Losing the headline would lose the run's most important
     paragraph.
  4. **Unparsed** — a section that yields nothing at all is rendered with a
     `(N lines — no findings parsed)` note. Never silently empty, for the same
     reason the playbook insists an empty section in the report is itself a
     finding: silence and "nothing there" must not look alike.

Confidence is only ever *read*. A finding with no confidence marker gets no tag
rather than a guessed `UNVERIFIED` — inventing a confidence level is exactly the
error this module exists to avoid.

No Textual and no model, like `osint.py` and `crack.py`, so the whole parser is
testable against the real reports with no terminal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .osint import KIND_ORDER, case_slug, normalise, reports_dir

#: ``<state_dir>/reports/`` is a *shared* archive — ``/osint``, ``/bug-hunt``
#: and ``/crack`` all move their finished report into the same flat directory
#: (``osint.reports_dir``, ``hunt.reports_dir`` and ``crack.reports_dir`` are
#: three identical functions). So a bare ``*.md`` glob there is not a list of
#: recons: it also holds ``hunt-<target>-<stamp>.md`` and
#: ``<hashfile>-<stamp>.md``, and the newest of *those* would be picked as
#: "the latest recon" on a box where a hunt ran more recently than a recon —
#: which is exactly what happened here, and why this filter exists.
#:
#: An osint report is named ``<kind>-<slug>-<stamp>.md`` (see
#: ``osint.case_dir``), and a kind is one of ``KIND_ORDER``. Matching the
#: first dash-separated token against that list is exact; ``email-`` cannot
#: collide with ``hunt-`` or a hash filename.
_OSINT_KINDS = frozenset(KIND_ORDER)

# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """One node of the result tree.

    ``children`` is a tuple, not a list, so a parsed tree is immutable and can
    be compared and reused; ``note`` carries the dim marker shown instead of
    children when a section parsed to nothing.
    """

    label: str
    confidence: str = ""
    children: tuple["Node", ...] = ()
    note: str = ""

    @property
    def is_leaf(self) -> bool:
        return not self.children


# ---------------------------------------------------------------------------
# Confidence — read, never inferred
# ---------------------------------------------------------------------------

#: The three levels the playbook defines, and the only strings this module will
#: accept as a confidence. Anything else is not a confidence and is left alone.
CONFIDENCES = ("CONFIRMED", "PROBABLE", "UNVERIFIED")

#: Which palette attribute each level is drawn in. Deliberately not the hunt's
#: `severity_style` table: that one is keyed on critical/high/medium/low/info and
#: would fall through to `dim` for every confidence, which would silently erase
#: the distinction the tags exist to make.
CONFIDENCE_STYLE = {
    "CONFIRMED": "primary",
    "PROBABLE": "accent",
    "UNVERIFIED": "dim",
}

_CONF_RE = re.compile(
    r"\b(" + "|".join(CONFIDENCES) + r")\b", re.IGNORECASE)

#: `**Confidence** — CONFIRMED`, but tolerant: the bold may be missing, and the
#: separator may be an em dash, a hyphen, a colon or nothing much at all.
_CONF_LINE_RE = re.compile(
    r"^\s*\**\s*Confidence\s*\**\s*[—:\-–]?\s*\**\s*"
    r"(CONFIRMED|PROBABLE|UNVERIFIED)\b",
    re.IGNORECASE,
)

#: `**What**` — the marker that a block is a finding block even when the model
#: forgot the Confidence line.
_WHAT_RE = re.compile(r"^\s*\**\s*What\s*\**\s*[—:\-–]", re.IGNORECASE)


def confidence_style(confidence: str, palette, *, bold: bool = False) -> str:
    """The Rich style for one confidence level, from the palette."""
    attr = CONFIDENCE_STYLE.get(str(confidence or "").strip().upper(), "dim")
    colour = getattr(palette, attr, palette.dim)
    return f"bold {colour}" if bold else colour


def _confidence_in(text: str) -> str:
    """The first confidence word in *text*, uppercased, or ''.

    First, not any: `CONFIRMED (exists) / PROBABLE (his)` is a CONFIRMED finding
    with a caveat, and reading the PROBABLE would downgrade it.
    """
    m = _CONF_RE.search(text or "")
    return m.group(1).upper() if m else ""


# ---------------------------------------------------------------------------
# Markdown scraping
# ---------------------------------------------------------------------------

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]*)`")
_WS_RE = re.compile(r"\s+")

#: An evidence filename — `01-dns.txt`, `03-xon.json`. The `Source` column of
#: every table is made of these, and a source is not a finding: it says where a
#: finding came from, not what it is. Matching them anywhere in a cell (not just
#: as the whole cell) is what catches `05-portfolio-repo.txt (about.html: …)`.
_SOURCE_TOKEN_RE = re.compile(
    r"[\w./\-]+\.(txt|json|html|log|md|csv|xml|png|jpg)\b", re.IGNORECASE)

#: A whole cell that is nothing but one evidence filename.
_SOURCE_CELL_RE = re.compile(
    r"^[\w./\-]+\.(txt|json|html|log|md|csv|xml|png|jpg)$", re.IGNORECASE)

#: Cells that mean "there is no source" rather than carrying content.
_EMPTY_CELLS = {"", "—", "-", "–", "n/a", "none", "nil"}

#: Column names that hold provenance rather than content. A table's header row
#: says which column is which, so the column is dropped by *name* wherever it
#: sits — far more reliable than guessing from the cell, which cannot tell
#: `GitHub login, Medium byline` (a source) from `Dhaka, Bangladesh` (a value).
_SOURCE_COLUMNS = {"source", "sources", "evidence", "where", "reference", "ref"}

#: Column names that hold a confidence. Read by name for the same reason.
_CONFIDENCE_COLUMNS = {"confidence", "conf", "certainty", "level"}


def _looks_like_source(cell: str) -> bool:
    """True if *cell* is a source column rather than content.

    Used only on a table's *last* content cell, where the source column sits.
    Applying it to every cell would eat a value that merely mentions a filename
    — `Gravatar | **None** — d=404; /<hash>.json → "User not found"` is a
    finding whose text happens to contain `.json`.
    """
    plain = _plain(cell)
    if plain.lower() in _EMPTY_CELLS:
        return True
    return bool(_SOURCE_TOKEN_RE.search(plain))


def _plain(text: str) -> str:
    """Markdown out, readable text in — for a label, not for fidelity."""
    s = _CODE_RE.sub(r"\1", text or "")
    s = _BOLD_RE.sub(r"\1", s)
    s = s.replace("\\|", "|")
    return _WS_RE.sub(" ", s).strip()


def _truncate(text: str, limit: int = 96) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _is_separator_row(line: str) -> bool:
    """`|---|---|` — the row that makes a markdown table a table."""
    inner = line.strip().strip("|")
    return bool(inner) and set(inner) <= set("-: |")


#: A pipe that is not escaped — the cell separator. `\|` inside a cell is a
#: literal pipe (`Ethical Hacker \| Red Team Expert`), and splitting on it would
#: cut a value in half and invent a column that does not exist.
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [p.strip() for p in _CELL_SPLIT_RE.split(inner) if p.strip()]


def _row_to_node(line: str, header: list[str] | None = None) -> Node | None:
    """A table row as a finding, or None if the row says nothing.

    With a *header* (the column names from the row above the separator) the
    source and confidence columns are dropped **by name**, which is exact. Two
    of the three real reports put free-text in their source column —
    `GitHub login, Medium byline, README footer` — and no amount of guessing at
    the cell can tell that from a value like `Dhaka, Bangladesh`. The header is
    the only thing that actually knows.

    Without a header the fallback is to drop a trailing cell that looks like an
    evidence filename, which covers the simpler tables.

    What survives is joined with ` · `, which keeps a
    `Provider · Google / Gmail — free consumer provider` row readable without
    the em dashes in the values colliding with the separator.
    """
    cells = _cells(line)
    if not cells:
        return None

    # A source column is provenance when it *trails* the last value column, and
    # the row's subject when it leads. `Attribute | Value | Source` ends in a
    # citation to drop; `Source | Result | Evidence` opens with the thing being
    # tested, and dropping it would leave `2 commits` with no clue which search
    # found them. `Source | Result | Evidence` in the middle is provenance too.
    last_value = len(header) - 1 if header else 0
    while last_value > 0 and _plain(header[last_value]).strip().lower() in (
            _SOURCE_COLUMNS | _CONFIDENCE_COLUMNS):
        last_value -= 1

    drop: set[int] = set()
    if header:
        for i, name in enumerate(header):
            key = _plain(name).strip().lower()
            if key in _CONFIDENCE_COLUMNS:
                drop.add(i)
            elif key in _SOURCE_COLUMNS and i > last_value:
                drop.add(i)

    confidence = ""
    content: list[str] = []
    for i, cell in enumerate(cells):
        conf = _confidence_in(cell)
        if i in drop:
            # A dropped confidence column still supplies the tag — that is what
            # the column is for.
            if conf and not confidence:
                confidence = conf
            continue
        # Without a header, a cell that is *only* a confidence is the
        # confidence column; one that merely mentions the word is content.
        if conf and _plain(cell).upper().startswith(conf):
            if not confidence:
                confidence = conf
            continue
        content.append(cell)

    if not content:
        return None

    if not header and len(content) > 1 and _looks_like_source(content[-1]):
        content.pop()

    label = _truncate(" · ".join(_plain(c) for c in content if _plain(c)))
    if not label:
        return None
    return Node(label=label, confidence=confidence)


def _header_of(lines: list[str], i: int) -> list[str] | None:
    """The column names of the table whose header row is at *lines[i]*."""
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    return _cells(lines[i]) if _is_separator_row(nxt) else None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class _Block:
    """A `###` block inside a section, or the anonymous run before the first."""

    title: str = ""
    lines: list[str] = field(default_factory=list)


def _blocks(body: list[str]) -> list[_Block]:
    """Split a section body on its `###` headings.

    Content before the first `###` becomes an anonymous block, so a section
    written as bare tables (which two of the three real reports are) is handled
    by the same path as one written as subsections.
    """
    out: list[_Block] = [_Block()]
    for line in body:
        if line.startswith("### "):
            out.append(_Block(title=_heading(line[4:])))
        elif line.startswith("###"):
            out.append(_Block(title=_heading(line[3:])))
        else:
            out[-1].lines.append(line)
    return [b for b in out if b.title or any(l.strip() for l in b.lines)]


#: A trailing `— 03-xon-analytics.json` on a heading, or the comma-separated
#: list of them a heading sometimes cites. The heading is a label, and the
#: evidence files belong in the report, not in the tree.
_TITLE_SOURCE_RE = re.compile(
    r"\s*[—–\-]\s*`?[\w./\-]+\.(txt|json|html|log|md|csv|xml|png|jpg)`?"
    r"(?:\s*,\s*`?[\w./\-]+\.(txt|json|html|log|md|csv|xml|png|jpg)`?)*\s*$",
    re.IGNORECASE)


def _heading(text: str) -> str:
    """A heading as a label: markdown off, trailing evidence file off."""
    s = _plain(text)
    prev = None
    while prev != s:
        prev = s
        s = _TITLE_SOURCE_RE.sub("", s).strip()
    return s


def _prose(lines: list[str]) -> str:
    """The readable sentences of a block, with tables and markup stripped."""
    kept = [
        _plain(l) for l in lines
        if l.strip() and not l.strip().startswith(("#", ">", "|", "---"))
    ]
    return _WS_RE.sub(" ", " ".join(k for k in kept if k)).strip()


def _block_to_nodes(block: _Block) -> list[Node]:
    """One block, by the ladder: finding block, then table, then prose."""
    conf = ""
    has_what = False
    for line in block.lines:
        m = _CONF_LINE_RE.match(line)
        if m and not conf:
            conf = m.group(1).upper()
        elif _WHAT_RE.match(line):
            has_what = True

    # Rung 1 — a real finding block. The `###` heading is the title, and that is
    # the whole point of this rung: a written title beats a scraped cell.
    if block.title and (conf or has_what):
        return [Node(label=block.title, confidence=conf)]

    # Rung 2 — table rows. A titled block keeps its rows underneath it as
    # children, because `### 4a. Breach list` and `### 4b. Infostealer
    # infections` are different sources and flattening them would lose that.
    #
    # A row immediately followed by `|---|` is a table *header* — `Item | Value
    # | Source` names the columns, it does not report anything, and counting it
    # as a finding would inflate every section that opens with a table. It is
    # kept as the column list instead, so the rows under it can be trimmed by
    # name.
    rows: list[Node] = []
    header: list[str] | None = None
    for i, line in enumerate(block.lines):
        if not _is_table_row(line) or _is_separator_row(line):
            continue
        found = _header_of(block.lines, i)
        if found is not None:
            header = found
            continue
        node = _row_to_node(line, header)
        if node is not None:
            rows.append(node)
    if rows:
        if block.title:
            return [Node(label=block.title, children=tuple(rows))]
        return rows

    # Rung 3 — prose. The headline is the run's most important paragraph; it
    # gets a leaf even though it is not a "finding" in the strict sense.
    text = _prose(block.lines)
    if block.title:
        if text:
            return [Node(label=block.title, note=_truncate(text, 200))]
        return [Node(label=block.title)]
    if text:
        return [Node(label=_truncate(text, 200))]
    return []


def parse_report(text: str, *, fallback_title: str = "OSINT report") -> Node:
    """A report's markdown as a tree.

    The root is the `# H1`; its children are the `## ` sections in document
    order — including `## Headline` and `## Coverage`, which are not dropped
    because they are the two sections an operator reads first. Section names are
    whatever the report happens to use: the parser keys on `## ` and never on a
    hard-coded list, which is what lets it survive the github report's entirely
    different sections.
    """
    lines = (text or "").splitlines()

    title = ""
    sections: list[tuple[str, list[str]]] = []
    for line in lines:
        if line.startswith("### "):
            if sections:
                sections[-1][1].append(line)
            continue
        if line.startswith("## "):
            sections.append((_plain(line[3:]), []))
            continue
        if line.startswith("##"):
            sections.append((_plain(line[2:]), []))
            continue
        if line.startswith("# "):
            if not title:
                title = _plain(line[2:])
            continue
        if sections:
            sections[-1][1].append(line)

    children: list[Node] = []
    for name, body in sections:
        found: list[Node] = []
        for block in _blocks(body):
            found.extend(_block_to_nodes(block))
        if found:
            children.append(Node(label=name, children=tuple(found)))
        else:
            # Rung 4 — say it parsed nothing, rather than showing a bare branch
            # that reads as "nothing was there".
            n = sum(1 for l in body if l.strip())
            children.append(Node(
                label=name,
                note=f"({n} line{'s' if n != 1 else ''} — no findings parsed)"
                     if n else "(empty section)"))

    return Node(label=title or fallback_title, children=tuple(children))


def parse_report_file(path: Path) -> Node:
    """Read and parse an archived report.

    ``errors="replace"``: the reports carry em dashes, `…` and the occasional
    stray byte from a scraped page, and a summary view must not be the thing
    that raises on a report the operator can otherwise read.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Node(label=path.stem, note="(could not read the report)")
    return parse_report(text, fallback_title=path.stem)


# ---------------------------------------------------------------------------
# Finding a report to show
# ---------------------------------------------------------------------------


def _is_osint_report(path: Path) -> bool:
    """Is *path* a report this module can read — i.e. an archived recon?

    The archive is shared with hunt and crack, so "is a .md under reports/" is
    not the question. It is whether the filename's leading token is a recon
    kind, which is the only thing that distinguishes
    ``email-target-example.com-20261005-193807.md`` from
    ``hunt-x.test-20261012-194709.md`` and ``some-tgs.txt-20261013-181853.md``.
    """
    return path.name.split("-", 1)[0].lower() in _OSINT_KINDS


def _case_dirs(cfg) -> list[Path]:
    """Every recon case directory still on disk, newest first.

    Normally empty: a finished run archives its report and deletes the case.
    But ``finalise_case`` is called from the turn's ``finally`` block, so an app
    killed outright — Ctrl+C twice, a crash, a closed terminal — never gets
    there, and the case survives with a perfectly readable ``report.md`` in it.
    That is not a hypothetical: it is the state of this machine, where all three
    real reports are sitting in case directories that were never finalised.

    Ignoring those would mean ``/osint-tree`` says "no archived reports yet"
    while three recons are on disk, which is the same class of lie as an empty
    tree — the operator cannot tell "there is nothing" from "I did not look".
    """
    root = Path(cfg.state_dir) / "osint"
    if not root.is_dir():
        return []
    dirs = [d for d in root.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return dirs


def _report_in(case: Path) -> Path | None:
    """The report inside an unfinalised *case*, if it has one.

    Same question ``osint._find_report`` answers for the archive step, minus
    the git-clone guard: that guard exists so ``finalise_case`` does not
    *delete* a cloned repo's own ``report.md`` along with the evidence, which
    is a destructive decision. Reading one back is not destructive, and a
    case directory here is a recon case by construction.
    """
    direct = case / "report.md"
    if direct.is_file():
        return direct
    for p in (*sorted(case.glob("*.md")), *sorted(case.glob("*/*.md"))):
        if "report" in p.name.lower():
            return p
    return None


def find_report(cfg, which: str | None = None) -> Path | None:
    """Resolve *which* to a recon report, or the newest one.

    Newest by mtime, not by name: the filenames embed a timestamp and would
    usually sort correctly, but a report copied in by hand would not, and "the
    latest run" is a question about time.

    Two stores are searched, because a recon can be in either: the archive
    (``<state_dir>/reports/``, where a finished run leaves it) and any case
    directory that never finalised (see ``_case_dirs``). The archive wins a
    tie on mtime — a finalised report is the one that got tidied.

    ``which`` may be a path (taken as-is — an explicit path is the operator
    overriding both stores, so it is not second-guessed), a kind, or a case
    name. Kinds and case names are matched against **recon** reports only; see
    ``_is_osint_report`` for why the shared archive makes that necessary.
    """
    base = reports_dir(cfg)
    archived = ([q for q in base.glob("*.md")
                 if q.is_file() and _is_osint_report(q)]
                if base.is_dir() else [])
    # (report, name-to-match-on) — a live report is matched on its *directory*
    # name, since the file inside is just `report.md` and carries no target.
    live = [(r, d.name) for d in _case_dirs(cfg)
            if (r := _report_in(d)) is not None]

    if which:
        p = Path(which).expanduser()
        if p.is_file():
            return p

        def _matches(name: str) -> bool:
            # A case name or a bare kind: `email`, `github`, `github-torvalds`.
            # `which-*` first, so `/osint-tree email` prefers `email-…` over a
            # case name that merely contains "email" somewhere in the middle.
            return name.startswith(f"{which}-") or which in name

        hits = [q for q in archived if _matches(q.name)]
        hits += [r for r, name in live if _matches(name)]
        if not hits:
            return None
    else:
        hits = archived + [r for r, _ in live]

    return max(hits, key=lambda q: q.stat().st_mtime) if hits else None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def count_findings(node: Node) -> tuple[int, dict[str, int]]:
    """(total leaves, {confidence: count}) for the whole tree."""
    total = 0
    tally: dict[str, int] = {}
    for child in node.children:
        if child.is_leaf:
            total += 1
            if child.confidence:
                tally[child.confidence] = tally.get(child.confidence, 0) + 1
        else:
            sub_total, sub_tally = count_findings(child)
            total += sub_total
            for k, v in sub_tally.items():
                tally[k] = tally.get(k, 0) + v
    return total, tally


def summary_line(node: Node) -> str:
    """`12 findings · 9 CONFIRMED · 2 PROBABLE · 1 UNVERIFIED`."""
    total, tally = count_findings(node)
    if not total:
        return "no findings parsed"
    parts = [f"{total} finding{'s' if total != 1 else ''}"]
    for level in CONFIDENCES:
        if tally.get(level):
            parts.append(f"{tally[level]} {level}")
    return " · ".join(parts)


def _label_text(node: Node, palette):
    """One node's label as a Rich Text, confidence tagged and coloured."""
    from rich.text import Text

    t = Text()
    if node.confidence:
        t.append(f"[{node.confidence}] ",
                 style=confidence_style(node.confidence, palette, bold=True))
    t.append(node.label, style=f"bold {palette.primary}" if node.children
             else palette.primary)
    if node.note:
        t.append(f"  {node.note}", style=palette.dim)
    return t


def render_tree(node: Node, palette):
    """The parsed tree as a `rich.tree.Tree`, ready for the transcript.

    Colours come from the palette, so the tree reads in whatever theme the
    operator is running — the same rule every other renderable follows.
    """
    from rich.tree import Tree

    root = Tree(_label_text(node, palette))
    for child in node.children:
        _attach(root, child, palette)
    return root


def _attach(parent, node: Node, palette) -> None:
    branch = parent.add(_label_text(node, palette))
    for child in node.children:
        _attach(branch, child, palette)
