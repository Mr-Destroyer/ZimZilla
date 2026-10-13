"""Regression suite for the `/osint` result tree.

Run:  python tests/test_osint_tree.py   (from an activated venv)

Two layers, matching the split in zimzilla/osint_tree.py itself:

* the pure parsing — the ladder, confidence reading, table handling, report
  resolution — exercised directly and with no terminal;
* the wiring — that the slash dispatch, the help table, the palette, the
  completion popup and the finalise hook all actually expose the tree, so a
  rename in one place cannot silently orphan it in another.

The fixtures below are written to reproduce the *shapes* of the three real
reports on this box, because the whole difficulty of this module is that those
three reports do not agree on a structure. A test written against one shape
would pass while two thirds of real runs rendered an empty tree. Where the real
reports are still on disk they are parsed too, and that pass is reported
separately so a skipped check is never mistaken for a passing one.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import osint_tree as ot  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.theme import get_palette  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402
from zimzilla.ui.complete import SLASH_COMMANDS  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


# ---------------------------------------------------------------------------
# Fixtures — the three shapes the real reports come in
# ---------------------------------------------------------------------------

# Shape A: conformant. Numbered sections, every finding a `###` heading with the
# five-line What/Where/Confidence/Means/Verify block underneath.
SHAPE_A = """\
# Email OSINT — demo@example.com

## Headline

This is a demo address belonging to **Demo Person**, used to exercise the tree.

## Coverage

| Phase | Ran? | Result |
|-------|------|--------|
| 1. Validation | yes | Free provider; not disposable |
| 6. Domain | skipped | Free provider — out of scope |

## 1. VALIDATION

### Example Mail is a free consumer provider, not corporate
**What**       — the domain resolves to a free provider.
**Where**      — `01-dns.txt`, MX records.
**Confidence** — CONFIRMED
**Means**      — this is an identity problem, not a domain problem.
**Verify**     — `dig MX example.com`.

### Address is not disposable
**What**       — Disify reports `disposable: false`.
**Where**      — `01-dns.txt`.
**Confidence** — CONFIRMED
**Means**      — the mailbox is likely a real person's.
**Verify**     — re-run Disify.

## 2. IDENTITY

### Full name is Demo Person
**What**       — the portfolio names its owner.
**Where**      — `05-portfolio.txt`.
**Confidence** — CONFIRMED
**Means**      — the address maps to a named individual.
**Verify**     — fetch the portfolio page.

### Alias demoguy is unconfirmed
**What**       — a handle appears in one commit only.
**Where**      — git author string.
**Confidence** — UNVERIFIED
**Means**      — not yet tied to the same person.
**Verify**     — search the handle elsewhere.

## 3. LEAKS

Nothing found — no paste or code exposure surfaced for this address.
"""

# Shape B: table-only. Same numbered sections, but every finding is a table row
# and there is not a single `**What**` block. This is the shape a strict parser
# renders as an empty tree.
SHAPE_B = """\
# Email OSINT — demo@example.com

## 1. VALIDATION

| Item | Value | Source |
|---|---|---|
| Provider | **Example Mail — free consumer provider** | `01-dns.txt` |
| Disposable? | **No** — mainstream provider | — |

**Classification: free provider.** Phase 6 is out of scope.

## 2. IDENTITY

| Attribute | Value | Confidence | Source |
|---|---|---|---|
| Full name | **Demo Person** | CONFIRMED | `05-portfolio.txt` |
| Alias | **demoguy** | UNVERIFIED | git author string |

## 3. CONTACT

| Type | Value | Confidence | Source |
|---|---|---|---|
| Email (secondary) | `second@example.com` | CONFIRMED | README `mailto:` |
| Phone | **none found** | — | — |
"""

# Shape C: the github report — a different section vocabulary entirely, plus
# `###` sub-blocks whose titles cite evidence files, and a `Source` column that
# leads its table.
SHAPE_C = """\
# GitHub OSINT — demohandle

## 1. ACCOUNT

| Field | Value | Source |
|---|---|---|
| login | `demohandle` | `raw_user.json` |
| name | **Demo Person** | `raw_user.json` |

## 2. EMAILS

| # | Address | Source | Commit date | Confidence |
|---|---|---|---|---|
| 1 | `demo@example.com` | git author, 12 commits | 2026-01-02 | **CONFIRMED** |

## 4. BREACH EXPOSURE

### 4a. Breach list (Example) — `03-xon.json`

| Breach | Date | Password? |
|---|---|---|
| **DemoLeak** | 2023-09 | **Yes — plaintext** |

### 4b. Stealer logs — `03-stealer.json`

| Date | Machine | Malware |
|---|---|---|
| 2024-07-27 | `DEMO-PC` | `MSBuild.exe` |

## 6. KEYS

| # | Type | Fingerprint |
|---|---|---|
| 1 | ED25519 | `SHA256:abc123` |

## 8. LEAKS

| Source | Result | Evidence |
|---|---|---|
| GitHub code search demo@example.com | 2 commits | `04-search.txt` |

## 9. SELF-DESCRIPTION

| Attribute | Value | Confidence | Source |
|---|---|---|---|
| Title | Ethical Hacker \\| Red Team Expert | CONFIRMED | portfolio |
"""


def _tree(text: str) -> ot.Node:
    return ot.parse_report(text)


def _labels(node: ot.Node) -> list[str]:
    """Every leaf label in the tree, flattened."""
    out: list[str] = []
    for c in node.children:
        if c.is_leaf:
            out.append(c.label)
        else:
            out.extend(_labels(c))
    return out


def _section(node: ot.Node, name: str) -> ot.Node | None:
    return next((c for c in node.children if c.label == name), None)


# ---------------------------------------------------------------------------
# Shape A — the conformant report
# ---------------------------------------------------------------------------

def test_shape_a() -> None:
    node = _tree(SHAPE_A)

    check("A: the H1 becomes the root", node.label == "Email OSINT — demo@example.com",
          repr(node.label))

    names = [c.label for c in node.children]
    check("A: every section is a branch",
          names == ["Headline", "Coverage", "1. VALIDATION", "2. IDENTITY", "3. LEAKS"],
          f"{names}")

    # The five-line blocks are the richest rung: a written title, a real
    # confidence read off the Confidence line.
    val = _section(node, "1. VALIDATION")
    check("A: finding blocks become leaves under their section",
          val is not None and len(val.children) == 2,
          f"{len(val.children) if val else 'missing'}")
    check("A: the leaf is the ### title, not the What line",
          val.children[0].label == "Example Mail is a free consumer provider, not corporate",
          repr(val.children[0].label))
    check("A: CONFIRMED is read from the Confidence line",
          val.children[0].confidence == "CONFIRMED",
          repr(val.children[0].confidence))

    ident = _section(node, "2. IDENTITY")
    check("A: UNVERIFIED is read too, not normalised away",
          ident.children[1].confidence == "UNVERIFIED",
          repr(ident.children[1].confidence))

    # A section that found nothing is a finding in its own right and must not
    # be dropped — the report guide says so explicitly.
    leaks = _section(node, "3. LEAKS")
    check("A: a 'nothing found' section still appears", leaks is not None)
    check("A: its prose is kept as a leaf",
          bool(leaks.children) and "no paste or code exposure" in leaks.children[0].label,
          f"{leaks.children[0].label[:60] if leaks.children else 'none'}")

    # Headline is the run's most important paragraph — losing it would be the
    # worst thing this view could do.
    head = _section(node, "Headline")
    check("A: the Headline is rendered, not dropped",
          head is not None and bool(head.children)
          and "Demo Person" in head.children[0].label)

    cov = _section(node, "Coverage")
    check("A: the coverage table is rendered",
          cov is not None and len(cov.children) == 2,
          f"{len(cov.children) if cov else 'missing'}")


# ---------------------------------------------------------------------------
# Shape B — the table-only report (the regression a strict parser fails)
# ---------------------------------------------------------------------------

def test_shape_b() -> None:
    node = _tree(SHAPE_B)

    total, _ = ot.count_findings(node)
    check("B: a table-only report still yields findings", total >= 6, f"{total}")

    val = _section(node, "1. VALIDATION")
    check("B: table rows become leaves", val is not None and len(val.children) == 2,
          f"{len(val.children) if val else 'missing'}")

    # The header row names the columns; counting it would inflate every section.
    labels = " | ".join(_labels(node))
    check("B: the header row is not counted as a finding",
          "Item · Value" not in labels and "Attribute · Value" not in labels,
          labels[:90])

    # A trailing source column is provenance, not a finding. `01-dns.txt` must
    # not survive into a label.
    check("B: a trailing evidence-filename column is dropped",
          "01-dns.txt" not in labels, labels[:120])

    # ...but the *value* survives. Over-trimming here would silently gut the tree.
    check("B: the row's value survives",
          "Provider · Example Mail — free consumer provider" in labels,
          labels[:120])

    ident = _section(node, "2. IDENTITY")
    check("B: confidence is read from a table cell",
          ident.children[0].confidence == "CONFIRMED"
          and ident.children[1].confidence == "UNVERIFIED",
          f"{[c.confidence for c in ident.children]}")

    # A `—` source cell means "no source"; the value before it must remain.
    contact = _section(node, "3. CONTACT")
    check("B: a row with an em-dash source keeps its value",
          any("Phone · none found" in c.label for c in contact.children),
          f"{[c.label for c in contact.children]}")


# ---------------------------------------------------------------------------
# Shape C — the github report's different vocabulary
# ---------------------------------------------------------------------------

def test_shape_c() -> None:
    node = _tree(SHAPE_C)

    names = [c.label for c in node.children]
    # The parser keys on `## `, never on a hard-coded section list — which is
    # the only reason a report with a different vocabulary works at all.
    check("C: sections are whatever the report calls them",
          names == ["1. ACCOUNT", "2. EMAILS", "4. BREACH EXPOSURE", "6. KEYS",
                    "8. LEAKS", "9. SELF-DESCRIPTION"],
          f"{names}")

    breach = _section(node, "4. BREACH EXPOSURE")
    check("C: a ### sub-block becomes a nested branch",
          breach is not None and len(breach.children) == 2,
          f"{len(breach.children) if breach else 'missing'}")
    check("C: the sub-block keeps its own title",
          breach.children[0].label == "4a. Breach list (Example)",
          repr(breach.children[0].label))
    check("C: its evidence filename is stripped from the title",
          "03-xon.json" not in breach.children[0].label,
          repr(breach.children[0].label))
    check("C: the rows nest under the sub-block",
          bool(breach.children[0].children)
          and "DemoLeak" in breach.children[0].children[0].label,
          f"{[c.label for c in breach.children[0].children]}")

    # A `Source` column that *leads* is the row's subject, not provenance.
    leaks = _section(node, "8. LEAKS")
    check("C: a leading Source column is kept as the subject",
          bool(leaks.children) and "GitHub code search" in leaks.children[0].label,
          repr(leaks.children[0].label if leaks.children else "none"))

    # An escaped pipe inside a cell is a literal pipe, not a column break.
    selfdesc = _section(node, "9. SELF-DESCRIPTION")
    check("C: an escaped pipe does not split the cell",
          bool(selfdesc.children)
          and "Ethical Hacker | Red Team Expert" in selfdesc.children[0].label,
          repr(selfdesc.children[0].label if selfdesc.children else "none"))

    # Confidence buried mid-row, with the source column after it.
    emails = _section(node, "2. EMAILS")
    check("C: a mid-row confidence is still read",
          emails.children[0].confidence == "CONFIRMED",
          repr(emails.children[0].confidence))


# ---------------------------------------------------------------------------
# The ladder's bottom rung
# ---------------------------------------------------------------------------

def test_unparsed_sections() -> None:
    node = _tree("""# T

## 1. STRUCTURE ONLY

> a blockquote, which is not a finding

---

## 2. TOTALLY EMPTY
""")

    # A section of pure structure — a quote, a rule — has nothing to show. It
    # must say so, because a bare branch reads as "nothing was there".
    empty = _section(node, "1. STRUCTURE ONLY")
    check("unparsed: a section with no findings still appears", empty is not None)
    check("unparsed: it says so rather than rendering bare",
          empty is not None and "no findings parsed" in empty.note,
          repr(empty.note if empty else ""))
    check("unparsed: the count is right",
          empty is not None and "(2 lines" in empty.note,
          repr(empty.note if empty else ""))

    blank = _section(node, "2. TOTALLY EMPTY")
    check("unparsed: a genuinely empty section says that instead",
          blank is not None and blank.note == "(empty section)",
          repr(blank.note if blank else ""))

    # But prose *is* a finding at the bottom rung: "Nothing found — <why>" is
    # the shape the report guide mandates for an empty section, and dropping it
    # would lose the reason.
    prose = _tree("# T\n\n## 1. LEAKS\n\nNothing found — every source was key-gated.\n")
    leaks = _section(prose, "1. LEAKS")
    check("unparsed: a 'nothing found' sentence is kept as a leaf",
          bool(leaks.children) and "key-gated" in leaks.children[0].label,
          repr(leaks.children[0].label if leaks.children else ""))
    check("unparsed: a kept prose leaf carries no note",
          leaks.children[0].note == "", repr(leaks.children[0].note))

    # A report with no H1 at all still parses, falling back to the filename.
    bare = ot.parse_report("## 1. X\n\n| a | b |\n|---|---|\n| k | v |\n",
                           fallback_title="fallback.md")
    check("unparsed: a report with no H1 falls back to the given title",
          bare.label == "fallback.md", repr(bare.label))

    check("unparsed: an empty string does not raise",
          ot.parse_report("").label == "OSINT report")


# ---------------------------------------------------------------------------
# Confidence is read, never invented
# ---------------------------------------------------------------------------

def test_confidence_never_invented() -> None:
    node = _tree("""# T

## 1. A

| Item | Value |
|---|---|
| Provider | Some Mail |
| Host | mail.example.com |
""")
    sec = _section(node, "1. A")
    check("confidence: a row with no confidence gets no tag",
          all(c.confidence == "" for c in sec.children),
          f"{[c.confidence for c in sec.children]}")

    total, tally = ot.count_findings(node)
    check("confidence: untagged findings are not tallied as anything",
          total == 2 and tally == {}, f"{total} {tally}")

    # `CONFIRMED (exists) / PROBABLE (his)` is a CONFIRMED finding with a
    # caveat; reading the PROBABLE would downgrade it.
    check("confidence: the first level wins, not the last",
          ot._confidence_in("CONFIRMED (exists) / PROBABLE (his)") == "CONFIRMED")
    check("confidence: an unknown word is not a confidence",
          ot._confidence_in("probably fine") == "")


def test_summary_line() -> None:
    node = _tree(SHAPE_A)
    line = ot.summary_line(node)
    check("summary: it counts findings", "finding" in line, line)
    check("summary: it breaks out the levels",
          "CONFIRMED" in line and "UNVERIFIED" in line, line)

    check("summary: an empty tree says so plainly",
          ot.summary_line(ot.Node(label="x")) == "no findings parsed")

    one = ot.Node(label="r", children=(ot.Node(label="only"),))
    check("summary: singular reads correctly",
          ot.summary_line(one) == "1 finding", ot.summary_line(one))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def test_find_report(tmp: Path) -> None:
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(tmp)
    cfg.state_dir = tmp / "state"
    base = cfg.state_dir / "reports"
    base.mkdir(parents=True)

    check("find: no reports at all returns None", ot.find_report(cfg) is None)

    empty_cfg = _cfg(tmp / "nope")
    empty_cfg.state_dir = tmp / "nope" / "state"
    check("find: a missing reports dir returns None",
          ot.find_report(empty_cfg) is None)

    a = base / "email-alpha-20260101-000000.md"
    b = base / "github-beta-20260102-000000.md"
    a.write_text("# A\n")
    b.write_text("# B\n")
    # mtime, not name: make the *older* name the newer file.
    now = time.time()
    os.utime(a, (now - 100, now - 100))
    os.utime(b, (now, now))

    check("find: with no argument it takes the newest by mtime",
          ot.find_report(cfg) == b, f"{ot.find_report(cfg)}")

    os.utime(a, (now + 100, now + 100))
    check("find: newest is mtime, not the name's stamp",
          ot.find_report(cfg) == a, f"{ot.find_report(cfg)}")

    check("find: a bare kind filters to that kind",
          ot.find_report(cfg, "github") == b, f"{ot.find_report(cfg, 'github')}")
    check("find: an unknown kind matches nothing",
          ot.find_report(cfg, "nosuch") is None)
    check("find: an exact path is honoured", ot.find_report(cfg, str(b)) == b)
    check("find: a case name resolves",
          ot.find_report(cfg, "github-beta-20260102-000000") == b)

    # A non-markdown file in the store is not a report.
    (base / "notes.txt").write_text("x")
    os.utime(base / "notes.txt", (now + 500, now + 500))
    check("find: only .md files are considered",
          ot.find_report(cfg) == a, f"{ot.find_report(cfg)}")

    # `<state_dir>/reports/` is shared with /bug-hunt and /crack — all three
    # move their finished report into the same flat directory. A newer hunt or
    # crack report must not be handed back as "the latest recon": it parses to
    # nothing, so the operator would get an empty tree for a run that never
    # happened, which reads exactly like "the recon found nothing".
    h = base / "hunt-x.test-20260103-000000.md"
    c = base / "some-tgs.txt-20260104-000000.md"
    h.write_text("---\ntarget: x.test\n---\n")
    c.write_text("# Password cracking — tgs.txt\n")
    os.utime(h, (now + 900, now + 900))
    os.utime(c, (now + 950, now + 950))
    check("find: a newer hunt report is not a recon",
          ot.find_report(cfg) == a, f"{ot.find_report(cfg)}")
    check("find: a bare kind does not match a hunt report",
          ot.find_report(cfg, "hunt") is None)
    check("find: a bare kind does not match a crack report",
          ot.find_report(cfg, "some-tgs.txt") is None)


def test_find_report_live_case(tmp: Path) -> None:
    """A case directory that never finalised is still a readable recon.

    ``finalise_case`` runs in the turn's ``finally`` block, so an app killed
    outright — Ctrl+C twice, a crash, a closed terminal — leaves the case (and
    its ``report.md``) on disk forever. This is not hypothetical: it is how all
    three real reports on this box ended up in case directories rather than in
    the archive, and the whole reason ``/osint-tree`` searches both.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    cfg = _cfg(tmp)
    cfg.state_dir = tmp / "state"
    root = cfg.state_dir / "osint"
    root.mkdir(parents=True)

    check("live: no case dirs, no archive, returns None",
          ot.find_report(cfg) is None)

    old = root / "email-alpha-20260101-000000"
    new = root / "github-beta-20260102-000000"
    for d, title in ((old, "# Old\n\n## 1. X\n\n- one\n"),
                     (new, "# New\n\n## 1. Y\n\n- two\n")):
        d.mkdir()
        (d / "report.md").write_text(title)
        (d / "raw.json").write_text("{}")  # the bulky evidence, still there
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))

    check("live: an unfinalised case is found",
          ot.find_report(cfg) == new / "report.md", f"{ot.find_report(cfg)}")
    check("live: a bare kind filters case dirs too",
          ot.find_report(cfg, "email") == old / "report.md",
          f"{ot.find_report(cfg, 'email')}")
    check("live: a case name resolves",
          ot.find_report(cfg, "github-beta-20260102-000000") == new / "report.md",
          f"{ot.find_report(cfg, 'github-beta-20260102-000000')}")

    # A case dir with no report in it is a failed run, not a result — it must
    # not shadow a real one, and must not be returned at all.
    empty = root / "email-gamma-20260103-000000"
    empty.mkdir()
    (empty / "raw.json").write_text("{}")
    os.utime(empty, (now + 500, now + 500))
    check("live: a case with no report is skipped",
          ot.find_report(cfg) == new / "report.md", f"{ot.find_report(cfg)}")

    # The archive wins a tie: a finalised report is the tidied one.
    base = cfg.state_dir / "reports"
    base.mkdir(parents=True)
    arch = base / "email-delta-20260104-000000.md"
    arch.write_text("# Archived\n\n## 1. Z\n\n- three\n")
    os.utime(arch, (now + 900, now + 900))
    check("live: the archive is searched alongside case dirs",
          ot.find_report(cfg) == arch, f"{ot.find_report(cfg)}")
    check("live: a kind matches in either store",
          ot.find_report(cfg, "github") == new / "report.md",
          f"{ot.find_report(cfg, 'github')}")


def test_parse_report_file(tmp: Path) -> None:
    p = tmp / "a report with spaces.md"
    p.write_text(SHAPE_A)
    node = ot.parse_report_file(p)
    check("file: a path with spaces reads",
          node.label.startswith("Email OSINT"), node.label)

    missing = ot.parse_report_file(tmp / "nope.md")
    check("file: a missing file returns a note, not a raise",
          "could not read" in missing.note, repr(missing.note))

    # Reports carry em dashes and stray bytes from scraped pages; a summary
    # view must not be the thing that raises on them.
    bad = tmp / "bad.md"
    bad.write_bytes(b"# T\n\n## 1. X\n\n| a | b |\n|---|---|\n| k\xff\xfe | v |\n")
    node = ot.parse_report_file(bad)
    check("file: invalid utf-8 does not raise", node.label == "T", repr(node.label))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_render() -> None:
    from rich.tree import Tree

    pal = get_palette("green")
    node = _tree(SHAPE_C)
    tree = ot.render_tree(node, pal)
    check("render: it builds a rich Tree", isinstance(tree, Tree))

    def nodes(t) -> int:
        return sum(1 + nodes(c) for c in t.children)

    parsed, _ = ot.count_findings(node)
    # Every section, every sub-block and every finding is a node in the tree.
    check("render: the tree carries every parsed node",
          nodes(tree) >= parsed, f"{nodes(tree)} nodes vs {parsed} findings")

    # Colours come from the palette, so the tree reads in any theme.
    for theme in ("green", "amber", "cyan"):
        t = ot.render_tree(node, get_palette(theme))
        check(f"render: it renders under the {theme} palette", t is not None)

    # Confidence styling must actually differ per level — the failure this
    # guards is every tag falling through to `dim`.
    styles = {c: ot.confidence_style(c, pal) for c in ot.CONFIDENCES}
    check("render: the three confidence levels are styled distinctly",
          len(set(styles.values())) == 3, f"{styles}")
    check("render: an unknown confidence falls back rather than raising",
          ot.confidence_style("MAYBE", pal) == pal.dim)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text()

    check("wiring: dispatch has an osint-tree entry",
          '"osint-tree": lambda a:' in app_src)
    check("wiring: _cmd_osint_tree is defined", "def _cmd_osint_tree(" in app_src)
    check("wiring: _show_osint_tree is defined", "def _show_osint_tree(" in app_src)
    check("wiring: the finalise hook renders the tree",
          "_show_osint_tree(report)" in app_src)
    check("wiring: help table lists it", '("/osint-tree [kind|path]"' in app_src)
    check("wiring: palette lists it",
          '("/osint-tree", "re-show a finished recon as a result tree", "osint")' in app_src)
    check("wiring: it is permitted while busy", '"osint-tree",' in app_src)

    cmds = dict(SLASH_COMMANDS)
    check("wiring: completion lists it", "/osint-tree" in cmds)
    check("wiring: completion describes it",
          "tree" in cmds.get("/osint-tree", ""), cmds.get("/osint-tree", ""))

    # The command must be its own dispatch key, not an argument to /osint.
    dispatch = app_src.split("dispatch = {", 1)[1].split("}", 1)[0]
    check("wiring: /osint-tree is a dispatch key of its own",
          '"osint-tree"' in dispatch and '"osint"' in dispatch)


# ---------------------------------------------------------------------------
# The live app
# ---------------------------------------------------------------------------

class _Blk:
    def __init__(self, **k):
        self.__dict__.update(k)

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


class _Msg:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage


async def test_ui(wd: Path) -> None:
    """Drive /osint end to end and confirm the tree reaches the transcript."""
    from rich.tree import Tree

    from zimzilla.ui.widgets import ChatPane

    cfg = _cfg(wd, boot_rain=False)
    cfg.state_dir = wd / "ui-state"

    captured: list = []
    orig = ChatPane.write_block

    def spy(self, renderable, *a, **k):
        captured.append(renderable)
        return orig(self, renderable, *a, **k)

    ChatPane.write_block = spy
    try:
        app = ZimZillaApp(cfg)
        async with app.run_test(size=(120, 44)) as pilot:
            app.pop_screen()
            await pilot.pause()

            gate = asyncio.Event()

            async def reporting_stream():
                await gate.wait()
                case = next((cfg.state_dir / "osint").glob("email-demo-example.com-*"))
                (case / "report.md").write_text(SHAPE_A)
                yield (None, _Msg([_Blk(type="text", text="done")]))

            app.agent._stream_once = reporting_stream

            captured.clear()
            app._handle_command("/osint email demo@example.com")
            await pilot.pause()
            check("ui: the recon turn launches", app.busy is True)

            gate.set()
            for _ in range(8):
                await pilot.pause()
            check("ui: the turn completes", app.busy is False)

            trees = [c for c in captured if isinstance(c, Tree)]
            check("ui: the result tree reaches the transcript", len(trees) == 1,
                  f"{len(trees)} trees in {len(captured)} blocks")

            # And the summary line, so the operator gets the count before the shape.
            texts = " ".join(c.plain for c in captured if hasattr(c, "plain"))
            check("ui: the summary line is printed",
                  "RESULT" in texts and "finding" in texts, texts[:90])

            # The archive still happens — the tree is added to that path, not
            # instead of it.
            archived = list((cfg.state_dir / "reports").glob("email-demo-example.com-*.md"))
            check("ui: the report is still archived", len(archived) == 1,
                  f"{[p.name for p in archived]}")

            # ---- /osint-tree re-reads it -------------------------------
            captured.clear()
            app._handle_command("/osint-tree")
            await pilot.pause()
            trees = [c for c in captured if isinstance(c, Tree)]
            check("ui: /osint-tree re-renders the archived report",
                  len(trees) == 1, f"{len(trees)} trees")

            captured.clear()
            app._handle_command("/osint-tree email")
            await pilot.pause()
            check("ui: /osint-tree by kind resolves",
                  any(isinstance(c, Tree) for c in captured))

            captured.clear()
            app._handle_command("/osint-tree nosuchkind")
            await pilot.pause()
            check("ui: an unmatched kind is refused, not rendered",
                  not any(isinstance(c, Tree) for c in captured))

            # ---- and it is typeable while busy -------------------------
            gate.clear()

            async def held_stream():
                await gate.wait()
                yield (None, _Msg([_Blk(type="text", text="done")]))

            app.agent._stream_once = held_stream
            app._handle_command("/osint github demohandle")
            await pilot.pause()
            check("ui: a second turn is running", app.busy is True)

            captured.clear()
            app._handle_command("/osint-tree")
            await pilot.pause()
            check("ui: /osint-tree works while the harness is busy",
                  any(isinstance(c, Tree) for c in captured))

            gate.set()
            for _ in range(8):
                await pilot.pause()
    finally:
        ChatPane.write_block = orig


# ---------------------------------------------------------------------------
# The real reports, when they are still on disk
# ---------------------------------------------------------------------------

def test_real_reports() -> None:
    """Parse the actual archived reports, if this box still has them.

    Reported as SKIP rather than PASS when absent: a check that silently passes
    without running is worse than no check, and these are the only inputs that
    prove the ladder against markdown nobody wrote for a test.
    """
    base = Path.home() / ".zimzilla" / "osint"
    found = sorted(base.glob("*/report.md")) if base.is_dir() else []
    if not found:
        print("SKIP  real: no archived reports on this box to parse")
        return

    for path in found:
        node = ot.parse_report_file(path)
        total, _ = ot.count_findings(node)
        tag = path.parent.name[:42]
        check(f"real: {tag} parses to findings",
              total > 0 and len(node.children) > 0,
              f"{total} findings across {len(node.children)} sections")

        # Every section must be accounted for — rendered with children, or with
        # the explicit note. A bare branch is the silent-empty failure.
        bare = [c.label for c in node.children if not c.children and not c.note]
        check(f"real: {tag} has no silent empty sections", not bare, f"{bare}")


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_shape_a()
        test_shape_b()
        test_shape_c()
        test_unparsed_sections()
        test_confidence_never_invented()
        test_summary_line()
        test_find_report(tmp / "find")
        test_find_report_live_case(tmp / "live")
        test_parse_report_file(tmp)
        test_render()
        test_wiring()
        await test_ui(tmp)
        test_real_reports()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
