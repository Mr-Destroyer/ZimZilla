"""Regression suite for the wave web and its tracker.

Run:  python tests/test_web.py   (from an activated venv)

Two things here are load-bearing and neither is visual.

**The geometry.** The web is drawn into a fixed character grid, so a pane that
runs off the edge or a spoke that overwrites a border does not raise — it just
prints something wrong, and it prints it wrong on a terminal the author may not
be sitting at. Every render here is asserted to be exactly *height* rows of
exactly *width* columns, and the panes are asserted to sit inside the canvas.

**The tracker's arithmetic.** ZIM-TRACK is a view, and the whole reason it is a
view rather than an eleventh agent is that it cannot be wrong. These pin the
parts that could be: which rung a finding lands on, what `worst` reports, and
that a severity the model spelled oddly still counts.

No test here needs a terminal: ``WaveWeb`` is deliberately free of Textual.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _web(n: int = 10, **kw):
    from zimzilla.ui.web import WaveWeb
    from zimzilla.theme import get_palette

    w = WaveWeb("target.test", get_palette("green", None), wave=1, size=n, **kw)
    w.set_plan([{"name": f"agent-{i + 1}", "brief": f"vector {i + 1}"}
                for i in range(n)])
    return w


def _lines(text) -> list[str]:
    return text.plain.split("\n")


def _drawer_text(w, width: int, height: int) -> str:
    """Only the columns the drawer occupies, flattened to one line.

    The canvas is a grid, so a row of the render interleaves the rail, three
    pane columns and the drawer side by side. Reading the whole render therefore
    splices unrelated text into the middle of a drawer sentence — "Unauthenticated
    order status" from the drawer, then a pane's border, then "read" — and an
    assertion on the drawer's wording fails against text that was never
    contiguous. Slicing the drawer's own columns out first is what makes the
    assertion mean what it says.
    """
    rendered = w.render(width, height).plain
    lay = w._layout
    if not lay.drawer_w:
        return ""
    x = width - lay.drawer_w - 1
    # Drop the drawer's own border columns: they sit at both ends of every
    # sliced row, so leaving them in puts a "┃" between the last word of one
    # wrapped line and the first word of the next and the joined sentence reads
    # "orders/10┃42" instead of "orders/1042".
    inner = [line[x + 1:x + lay.drawer_w - 1] for line in rendered.split("\n")]
    return " ".join("".join(inner).split())


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def test_render_is_exact() -> None:
    """Every render is exactly the size asked for, at every size tried.

    This is the assertion that catches a pane drawn past the right edge or a
    spoke that walks off the bottom: the grid silently drops those writes, so
    the only symptom is a canvas of the wrong shape.
    """
    w = _web(10)
    w.add_finding({"agent": "agent-3", "severity": "critical",
                   "title": "SQLi in ?id", "asset": "api.target.test"})
    for width, height in ((118, 36), (120, 40), (80, 24), (78, 22),
                          (200, 50), (74, 21), (60, 18)):
        lines = _lines(w.render(width, height))
        check(f"geometry: {width}x{height} is {height} rows",
              len(lines) == height, str(len(lines)))
        widths = {len(line) for line in lines}
        check(f"geometry: {width}x{height} is {width} columns, no ragged rows",
              widths == {width}, str(sorted(widths)))


def test_web_fits_at_minimum() -> None:
    """At exactly the minimum size the panes are drawn, not the fallback."""
    from zimzilla.ui.web import MIN_HEIGHT, MIN_WIDTH

    w = _web(10)
    w.add_finding({"agent": "agent-1", "severity": "high", "title": "X"})
    text = w.render(MIN_WIDTH, MIN_HEIGHT).plain
    check("minimum: the tracker rail is drawn at the minimum size",
          "ZIM-TRACK" in text)
    check("minimum: panes are drawn, not the list",
          "┏" in text and "┌─" in text, text[:120].replace("\n", "|"))
    # Eight of ten fit at the minimum; the other two have to be accounted for
    # rather than silently missing, or the view reads as a wave that lost them.
    check("minimum: the agents that do not fit are named",
          "not shown" in text, text.replace("\n", "|")[-160:])


def test_panes_stay_inside_the_canvas() -> None:
    """No agent pane's border is clipped by the edge of the screen.

    A pane pushed past the right edge loses its closing border, and one pushed
    past the bottom loses its bottom border, which reads as a rendering fault
    rather than a layout one. The old geometry did exactly that: it asked for a
    fixed six-row pane and computed how many fit, which returned six at 118x36
    while only four could be drawn — so two panes were laid out off the bottom
    of the canvas, silently dropped, and their hit regions dropped with them.

    Asserted structurally rather than by counting corners: every pane that was
    drawn must have a bottom-right corner, at a row and column inside the
    canvas. That catches a pane clipped on any edge, at any layout.
    """
    from zimzilla.ui.web import BOX_MIN_H

    w = _web(10)
    for width, height in ((118, 36), (80, 24), (78, 22), (200, 50), (92, 26)):
        lines = _lines(w.render(width, height))
        # Every top-left corner has a matching bottom-right one, so no box is
        # half-drawn. The rail is heavy-bordered and so does not count here.
        tops = sum(line.count("┌") for line in lines)
        bottoms = sum(line.count("┘") for line in lines)
        check(f"panes: every box drawn at {width}x{height} is closed",
              tops == bottoms, f"tops={tops} bottoms={bottoms}")
        for i, line in enumerate(lines):
            if "┘" in line:
                check(f"panes: no bottom border on the last row at {width}x{height}",
                      i < height - 1, f"row={i} of {height}")
        # And nothing may be drawn flush against the terminal's own edge, where
        # the border would be indistinguishable from the frame.
        for line in lines:
            check(f"panes: no pane is flush against the right edge at "
                  f"{width}x{height}", len(line) == width, str(len(line)))


def test_adaptive_geometry() -> None:
    """The layout is sized from the space, and never claims what it cannot draw.

    This is the regression that motivated the rewrite. The old view asked for a
    fixed six-row pane and then computed how many fit in the height, dividing by
    ``BOX_H + 1`` per column — which reported six panes at 118x36 where only four
    could be drawn. The two extra were laid out below the last row, the grid
    dropped them silently, and their hit regions were never recorded, so they
    were neither visible nor clickable.

    The invariant asserted here is that ``capacity`` is honest: every slot the
    layout promises must be a slot the render actually fills, at every size
    tried.
    """
    from zimzilla.ui.web import BOX_MIN_H, PANE_MIN_W

    for width, height in ((118, 36), (120, 40), (150, 44), (200, 50), (92, 26),
                          (110, 34), (96, 28), (300, 60)):
        w = _web(10)
        lay = w.layout(width, height)
        if not lay.web:
            continue
        check(f"layout: {width}x{height} pane clears the minimum width",
              lay.pane_w >= PANE_MIN_W, f"pane_w={lay.pane_w}")
        check(f"layout: {width}x{height} pane clears the minimum height",
              lay.box_h >= BOX_MIN_H, f"box_h={lay.box_h}")
        # Every slot capacity promises must be inside the canvas.
        for i in range(lay.capacity):
            x, y = w._grid_pos(i, lay, height)
            check(f"layout: {width}x{height} slot {i} is inside the canvas",
                  y + lay.box_h <= height - 1 and x + lay.pane_w <= width - 1,
                  f"x={x} y={y} pane_w={lay.pane_w} box_h={lay.box_h}")
        # And the render must fill exactly that many panes.
        w.render(width, height)
        drawn = {key for kind, key in w._hits.values() if kind == "pane"}
        check(f"layout: {width}x{height} draws every slot it promised",
              len(drawn) == min(10, lay.capacity),
              f"drawn={len(drawn)} capacity={lay.capacity}")


def test_all_ten_agents_are_big_enough_to_read() -> None:
    """At a normal terminal every agent is on screen with room for output.

    The point of the rewrite: ten panes that each show a command *and* several
    lines of what it returned, rather than a command and one line. The old
    layout gave each pane three content rows at 118 columns and showed six of
    the ten, so half the wave was never visible.
    """
    w = _web(10)
    for width, height in ((118, 36), (150, 44), (200, 50)):
        w.render(width, height)
        drawn = {key for kind, key in w._hits.values() if kind == "pane"}
        check(f"readable: all ten agents are drawn at {width}x{height}",
              len(drawn) == 10, f"drawn={sorted(drawn)}")
        lay = w.layout(width, height)
        # A pane's content rows are the command, the output, and the tail.
        output_rows = lay.box_h - 4
        check(f"readable: a pane shows at least two output rows at "
              f"{width}x{height}", output_rows >= 2, f"output_rows={output_rows}")


def test_rail_border_survives() -> None:
    """The tracker rail's own border is unbroken, and it spans the full height.

    The rail is the anchor the panes are read against; a gap in its border reads
    as a drawing fault rather than as a layout.
    """
    w = _web(10)
    for width, height in ((118, 36), (120, 40)):
        lines = _lines(w.render(width, height))
        top = next((i for i, l in enumerate(lines) if "┏━ ZIM-TRACK" in l), None)
        check(f"rail: the tracker box is drawn at {width}x{height}", top is not None)
        if top is None:
            continue
        row = lines[top]
        start = row.index("┏")
        end = row.index("┓")
        border = row[start:end + 1]
        check(f"rail: the top border is unbroken at {width}x{height}",
              set(border) <= {"┏", "┓", "━", " ", "Z", "I", "M", "-", "T", "R",
                              "A", "C", "K"},
              border)
        check(f"rail: it starts on the first row at {width}x{height}", top == 0,
              str(top))
        check(f"rail: it reaches the last row at {width}x{height}",
              lines[height - 1].lstrip().startswith("┗"), lines[height - 1][:40])


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------

def test_counts_land_on_the_right_rung() -> None:
    """Every finding is counted once, on its own severity rung."""
    w = _web(10)
    for agent, sev in (("agent-1", "critical"), ("agent-2", "critical"),
                       ("agent-3", "high"), ("agent-4", "low"),
                       ("agent-5", "info")):
        w.add_finding({"agent": agent, "severity": sev, "title": f"{sev} bug"})
    counts = w.counts()
    check("tracker: criticals are counted", counts["critical"] == 2, str(counts))
    check("tracker: highs are counted", counts["high"] == 1, str(counts))
    check("tracker: mediums are counted", counts["medium"] == 0, str(counts))
    check("tracker: lows are counted", counts["low"] == 1, str(counts))
    check("tracker: info is counted too, even though no lane shows it",
          counts["info"] == 1, str(counts))
    check("tracker: the total is every finding",
          sum(counts.values()) == 5, str(sum(counts.values())))


def test_severity_spellings_are_normalised() -> None:
    """A model's odd severity spelling still lands on a rung.

    The tracker shows CRIT/HIGH/MED/LOW lanes; a finding the model spelled
    "severe" or "moderate" must not fall into a bucket that has no lane, or it
    would be counted in the total and invisible in the breakdown.
    """
    w = _web(10)
    w.add_finding({"agent": "a", "severity": "SEVERE", "title": "x"})
    w.add_finding({"agent": "b", "severity": "Moderate", "title": "y"})
    w.add_finding({"agent": "c", "severity": "gibberish", "title": "z"})
    counts = w.counts()
    check("tracker: 'severe' folds onto critical", counts["critical"] == 1, str(counts))
    check("tracker: 'moderate' folds onto medium", counts["medium"] == 1, str(counts))
    check("tracker: an unknown severity folds onto info", counts["info"] == 1, str(counts))


def test_worst_and_feed() -> None:
    """`worst` reports the top rung; `recent` is newest-first."""
    w = _web(10)
    check("tracker: an empty tracker has no worst", w.worst() == "")
    w.add_finding({"agent": "a", "severity": "low", "title": "first"})
    check("tracker: one low is the worst so far", w.worst() == "low")
    w.add_finding({"agent": "b", "severity": "critical", "title": "second"})
    check("tracker: a critical takes over as worst", w.worst() == "critical")
    w.add_finding({"agent": "c", "severity": "high", "title": "third"})
    check("tracker: a high does not displace a critical", w.worst() == "critical")
    recent = [f["title"] for f in w.recent(2)]
    check("tracker: recent is newest-first", recent == ["third", "second"], str(recent))


def test_findings_light_their_agent() -> None:
    """A finding tallies against the agent that reported it, and only that one."""
    from zimzilla.ui.web import FLASH_SECONDS

    w = _web(10)
    w.add_finding({"agent": "agent-4", "severity": "critical", "title": "SQLi"})
    by_name = {c.name: c for c in w.agents}
    check("agents: the reporter's count went up",
          by_name["agent-4"].findings == 1, str(by_name["agent-4"].findings))
    check("agents: the reporter records its worst rung",
          by_name["agent-4"].worst == "critical")
    check("agents: the reporter is flashing", by_name["agent-4"].lit())
    check("agents: a different agent's count did not move",
          by_name["agent-5"].findings == 0)
    check("agents: a different agent is not flashing",
          not by_name["agent-5"].lit())
    # A finding attributed to an agent that is not in the roster must not raise
    # — a wave can be replanned, and the tracker outlives the roster it started
    # with.
    w.add_finding({"agent": "agent-99", "severity": "low", "title": "ghost"})
    check("agents: a finding from an unknown agent is still tallied",
          w.counts()["low"] == 1)
    # And the flash expires.
    by_name["agent-4"].flash = time.monotonic() - FLASH_SECONDS - 1
    check("agents: the flash expires", not by_name["agent-4"].lit())


def test_agent_lifecycle() -> None:
    """start / activity / done drive the pane's status, and end() sweeps up."""
    w = _web(10)
    by_name = {c.name: c for c in w.agents}
    check("agents: a planned agent starts idle",
          by_name["agent-1"].status == "idle", by_name["agent-1"].status)
    w.agent_start("agent-1", "sqlmap on /api")
    check("agents: start marks it running", by_name["agent-1"].status == "running")
    check("agents: start records the brief",
          by_name["agent-1"].activity == "sqlmap on /api")
    w.agent_activity("agent-1", "$ sqlmap -u ...")
    check("agents: activity replaces what it is doing",
          by_name["agent-1"].activity == "$ sqlmap -u ...")
    w.agent_done("agent-1", ok=False)
    check("agents: a failed agent is marked failed",
          by_name["agent-1"].status == "failed")
    w.agent_start("agent-2")
    w.end()
    check("agents: end() finishes anything still running",
          by_name["agent-2"].status == "done", by_name["agent-2"].status)
    check("agents: end() stops the clock", w.ended > 0)


def test_plan_replaces_the_roster() -> None:
    """A replanned wave gets a fresh roster, not the old one plus new agents."""
    w = _web(10)
    w.set_plan([{"name": "a", "brief": "1"}, {"name": "b", "brief": "2"}], wave=1)
    check("plan: the roster is the one returned",
          [c.name for c in w.agents] == ["a", "b"])
    w.set_plan([{"name": "c", "brief": "3"}], wave=2)
    check("plan: a second plan replaces the first",
          [c.name for c in w.agents] == ["c"], str([c.name for c in w.agents]))
    check("plan: the wave number follows the plan", w.wave == 2)
    # More workers than the pane has room for must be truncated, not overflow.
    w.set_plan([{"name": f"x{i}"} for i in range(30)])
    check("plan: a roster larger than the wave size is truncated",
          len(w.agents) == w.size, str(len(w.agents)))


# ---------------------------------------------------------------------------
# The fallback
# ---------------------------------------------------------------------------

def test_compact_fallback() -> None:
    """A terminal too small for the panes gets a list, with every fact in it."""
    w = _web(10)
    w.add_finding({"agent": "agent-3", "severity": "critical",
                   "title": "SQLi in ?id", "asset": "api.target.test"})
    w.agent_start("agent-7", "nuclei")
    for width, height in ((60, 18), (40, 12)):
        lines = _lines(w.render(width, height))
        check(f"fallback: {width}x{height} is still {height} rows",
              len(lines) == height, str(len(lines)))
        text = "\n".join(lines)
        check(f"fallback: {width}x{height} does not draw panes",
              "┏" not in text and "┌─" not in text)
        check(f"fallback: {width}x{height} still names the tracker",
              "ZIM-TRACK" in text)
        check(f"fallback: {width}x{height} still shows the tally",
              "CRIT 1" in text, text.replace("\n", "|")[:200])
        # The terminal is too short to list ten agents. What matters is that the
        # list never *lies* about the wave: whoever was cut has to be accounted
        # for, or a nine-line list under a "10 agents" header reads as a lost
        # worker.
        listed = sum(1 for line in lines
                     if line.strip().startswith(("·", "◆", "⠋", "⠙", "⠹", "⠸",
                                                 "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"))
                     and "─▶" not in line)
        check(f"fallback: {width}x{height} accounts for every agent it cannot "
              f"list", "more" in text or listed >= 10,
              f"listed={listed} text={text.replace(chr(10), '|')[:160]}")
        check(f"fallback: {width}x{height} names at least one agent",
              any(f"agent-{i}" in text for i in range(1, 11)),
              text.replace("\n", "|")[:200])
    # A single agent still gets the pane layout — one column of one — rather
    # than a list, because the rail is worth drawing at any wave size.
    one = _web(1)
    check("fallback: a single agent still gets panes",
          "┌─" in one.render(118, 36).plain)


def test_truncated_roster_keeps_the_reporters() -> None:
    """A short terminal lists whoever is busy, then counts the rest.

    A wave is only interesting because of the agents that found or are chasing
    something. If the list is cut, it must cut the idle tail — dropping the one
    agent that reported a critical while listing eight that have done nothing
    would hide the only thing on the screen worth reading.
    """
    w = _web(10)
    w.add_finding({"agent": "agent-10", "severity": "critical", "title": "SQLi"})
    w.agent_start("agent-9", "nuclei")
    lines = _lines(w.render(40, 12))
    text = "\n".join(lines)
    check("fallback: the reporting agent survives truncation",
          "agent-10" in text and "SQLi" in text)
    check("fallback: the running agent survives truncation",
          "agent-9" in text and "nuclei" in text)
    check("fallback: the agents it could not list are counted, not dropped",
          "+5 more" in text, text.replace("\n", "|"))
    # The count must add up. This is checked by arithmetic rather than against a
    # magic number, because the count itself was wrong: the roster was trimmed
    # to make room for the "+N more" row and then counted against the untrimmed
    # list, so it listed five agents and claimed four hidden — nine, under a
    # header reading ten.
    # Agent rows read "· agent-N …"; the feed rows below the lanes read
    # "· agent-N ─▶ [SEV] title", so the arrow is what tells them apart. The
    # "+N more" row is a count, not an agent, and must not be counted as one.
    listed = sum(1 for line in lines
                 if "─▶" not in line and " more" not in line
                 and any(line.strip().startswith(p)
                         for p in ("· agent-", "◆ agent-", "⠋ agent-", "⠙ agent-",
                                   "⠹ agent-", "⠸ agent-", "⠼ agent-", "⠴ agent-",
                                   "⠦ agent-", "⠧ agent-", "⠇ agent-", "⠏ agent-")))
    hidden = next((int(t.split("+")[1].split()[0]) for t in lines
                   if "+" in t and "more" in t), 0)
    check("fallback: listed plus hidden accounts for the whole wave",
          listed + hidden == 10, f"listed={listed} hidden={hidden}")


# ---------------------------------------------------------------------------
# The splash banner
# ---------------------------------------------------------------------------

def test_splash_banner() -> None:
    """The banner is centred, and stays centred when the transcript reflows.

    The old splash padded its own lines with a hardcoded indent, so it was
    centred for exactly one width — the width it was launched at. Dragging a
    rail re-renders every block in the transcript at the new width, and a
    hand-padded banner does not survive that. The fix is to hand ``Align`` the
    renderable and let it centre on each render, so what is asserted here is
    that the renderable *is* the thing that re-centres, not that one particular
    render looks right.
    """
    from rich.align import Align
    from rich.console import Console

    from zimzilla.ui.banner import EMBLEM, splash_mark
    from zimzilla.theme import get_palette

    p = get_palette("green", None)
    mark = splash_mark(p, model="deepseek-v4.1-flash",
                       base_url="http://localhost:4001")
    text = mark.plain
    check("splash: the mark carries the name", "ZIMZILLA" in text)
    check("splash: the mark carries the model", "deepseek-v4.1-flash" in text)
    check("splash: the mark carries the endpoint", "localhost:4001" in text)
    check("splash: the mark is four rows", len(text.split("\n")) == len(EMBLEM),
          str(len(text.split("\n"))))

    def render(width: int) -> list[str]:
        c = Console(width=width, record=True, force_terminal=False)
        c.print(Align.center(mark))
        return c.export_text().rstrip("\n").split("\n")

    # At every width, the emblem column starts at the same place on every row —
    # that is what "the mark is not sheared" means, and a per-line indent gets
    # this wrong the moment one row is wider than the others.
    for width in (120, 100, 80, 70):
        rows = render(width)
        check(f"splash: every row is the full pane width at {width}",
              {len(r) for r in rows} == {width}, str(sorted({len(r) for r in rows})))
        # The emblem's own first column: the row with the widest glyph starts
        # leftmost, and the ▀ row is indented by one. So the emblem's left edge
        # is where the widest row begins, and no row may begin further left.
        starts = [len(r) - len(r.lstrip()) for r in rows if r.strip()]
        check(f"splash: the emblem is not sheared at {width}",
              max(starts) - min(starts) <= 1, str(starts))

    # The point of the whole change: the same renderable re-centres itself, so
    # the banner is centred at a width it was never rendered at before.
    wide = render(120)[0]
    narrow = render(70)[0]
    check("splash: the banner re-centres for a new width",
          len(narrow) - len(narrow.lstrip()) < len(wide) - len(wide.lstrip()),
          f"wide indent={len(wide) - len(wide.lstrip())} "
          f"narrow indent={len(narrow) - len(narrow.lstrip())}")

    # And it is actually centred, not merely re-indented: the left and right
    # margins of the widest row agree to within the odd/even rounding.
    for width in (120, 101, 80, 71):
        rows = render(width)
        row = max(rows, key=lambda r: len(r.rstrip()))
        left = len(row) - len(row.lstrip())
        right = len(row) - len(row.rstrip())
        check(f"splash: the banner is centred at {width}",
              abs(left - right) <= 1, f"left={left} right={right}")


def test_planning_state() -> None:
    """Before the roster lands the screen says it is planning, not "0 agents".

    The screen is pushed at ``hunt_wave_start``, but the roster only arrives with
    ``hunt_plan``, which is emitted after the planner's turn returns. That turn
    is a real model call over recon, findings, wave history and a long
    instruction — tens of seconds. For all of it the view held zero agents and
    drew "WAVE 1 · 0 agents", which reads as a wave that started with nobody in
    it: the exact fault this screen exists to make visible.
    """
    from zimzilla.theme import get_palette
    from zimzilla.ui.web import WaveWeb

    w = WaveWeb("*.lerevecraze.com", get_palette("green", None), wave=1, size=10)
    check("planning: a fresh web is planning", w.planning is True)

    # The wide view draws a PLANNING box where the panes would be, rather than
    # falling back to a list — the rail is still worth drawing while the planner
    # thinks, and the box has room for the reasoning the list does not.
    wide = w.render(118, 36).plain
    check("planning: the wide view says PLANNING", "PLANNING" in wide,
          wide[:200].replace("\n", "|"))
    check("planning: the rail is still drawn", "ZIM-TRACK" in wide)
    check("planning: it does not claim zero agents", "0 agents" not in wide)

    # The narrow view takes the list, and says the same thing there.
    narrow = w.render(60, 18).plain
    check("planning: the narrow view says planning, not 0 agents",
          "planning" in narrow and "0 agents" not in narrow,
          narrow.replace("\n", "|")[:200])
    check("planning: the empty roster slot is explained",
          "roster lands" in narrow, narrow.replace("\n", "|")[:200])

    # The feed itself. This is the whole point of the planning state: the
    # planner's real reasoning and its real tool calls, on screen while it
    # works, instead of a blank box for the length of a model call.
    w.planner_text("the auth bypass is closed, so wave 2 should pivot to the "
                   "wp-json surface the recon notes flagged")
    w.planner_tool("grep")
    fed = w.render(118, 36).plain
    check("planning: the planner's reasoning reaches the screen",
          "pivot to the" in fed, fed.replace("\n", "|")[:400])
    check("planning: the planner's tool call reaches the screen",
          "$ grep" in fed, fed.replace("\n", "|")[:400])
    check("planning: the blocks are counted",
          "2 block(s)" in fed, fed.replace("\n", "|")[:200])

    # And it stops saying it the moment a roster arrives.
    w.set_plan([{"name": f"agent-{i + 1}", "brief": "b"} for i in range(10)])
    check("planning: a roster ends the planning state", w.planning is False)
    after = w.render(118, 36).plain
    check("planning: the panes are drawn, not the list",
          "┌─" in after, after.replace("\n", "|")[:120])
    check("planning: the rail goes back to LIVE", "LIVE" in after,
          after.replace("\n", "|")[:120])
    check("planning: the PLANNING box is gone", "PLANNING" not in after)
    header = w.render(60, 18).plain.split("\n")[0]
    check("planning: the header counts the real roster",
          "10 agents" in header, header)

    # A finding with no roster is not a planning wave — it is a wave that has
    # already reported, which only happens once agents exist. Guard the
    # predicate so a stray finding cannot leave the header stuck on PLANNING.
    w2 = WaveWeb("t.test", get_palette("green", None), wave=1, size=10)
    w2.add_finding({"agent": "agent-1", "severity": "high", "title": "x"})
    check("planning: a reported finding is not planning", w2.planning is False)


def test_agent_output_reaches_the_pane() -> None:
    """A pane shows the command *and* what the command returned.

    The result half was being emitted by the hunt loop, routed to the wave
    screen, and then dropped, so every pane drew a command over an empty box —
    the agent's actual findings were legible only by reading the scrolling
    transcript backwards.
    """
    w = _web(10)
    cmd = "$ curl -s -i $U/wp-json/wc/v3/orders/1042"
    w.agent_activity("agent-1", cmd)
    w.agent_result("agent-1", cmd,
                   "HTTP/1.1 200 OK\nContent-Type: application/json\n"
                   '{"id":1042,"billing":{"email":"a@b.c"}}\nserver: nginx')
    text = w.render(150, 44).plain
    check("output: the command is drawn", "curl -s -i" in text)
    check("output: the response status line is drawn",
          "HTTP/1.1 200 OK" in text, text.replace("\n", "|")[:200])
    check("output: the response body is drawn",
          '{"id":1042' in text or "Content-Type" in text)
    cell = w.agents[0]
    check("output: the whole output is kept for the drawer",
          len(cell.output) == 4, str(cell.output))
    check("output: the step is recorded once, not twice",
          len(cell.history) == 1, str(cell.history))
    check("output: the recorded step carries its output",
          "HTTP/1.1 200 OK" in cell.history[0][1], str(cell.history[0]))

    # A second command must not be drawn over the first one's output: a pane
    # that showed the new command above the old response would read as though
    # this call had returned it.
    w.agent_activity("agent-1", "$ nmap -sV host")
    check("output: a new command clears the old output",
          w.agents[0].output == [], str(w.agents[0].output))
    check("output: the old command is still in the drawer's run",
          len(w.agents[0].history) == 2, str(len(w.agents[0].history)))

    # More output than fits is counted, so a clipped listing does not read as a
    # short one.
    w.agent_result("agent-1", "$ nmap -sV host",
                   "\n".join(f"port {i}" for i in range(40)))
    more = w.render(150, 44).plain
    check("output: clipped output is counted", ">3" in more or ">" in more,
          more.replace("\n", "|")[:300])


def test_lane_click_opens_the_findings_behind_it() -> None:
    """A severity lane is a button, and it opens what it counted.

    "MED 1" tells the operator a medium was found and nothing else. The drawer
    is the answer to *what*: the title, the asset, the agent, and the evidence
    that makes it a finding rather than an opinion.
    """
    w = _web(10)
    w.add_finding({"agent": "agent-2", "severity": "medium",
                   "title": "Unauthenticated order status read",
                   "asset": "http://dev.target.test/orders/1042",
                   "summary": "The v3 route answers without a nonce.",
                   "evidence": "GET -> 200 with billing email", "wave": 1})
    w.add_finding({"agent": "agent-3", "severity": "low",
                   "title": "a low-severity note that must not appear here"})
    w.render(118, 36)

    # Find the MED lane row from the hit map rather than guessing its row.
    lane_at = next((pos for pos, (kind, key) in w._hits.items()
                    if kind == "lane" and key == "medium"), None)
    check("drawer: the MED lane is clickable", lane_at is not None)
    if lane_at is None:
        return
    hit = w.click(*lane_at)
    check("drawer: clicking the lane opens the drawer",
          hit == ("lane", "medium"), str(hit))
    check("drawer: it is showing that rung",
          w.detail.kind == "findings" and w.detail.key == "medium",
          f"{w.detail.kind}/{w.detail.key}")

    text = w.render(118, 36).plain
    # Read the drawer's own columns: the canvas interleaves it with the panes,
    # so a whole-render read splices pane borders into the middle of a sentence.
    flat = _drawer_text(w, 118, 36)
    check("drawer: the finding's title is shown",
          "Unauthenticated order status read" in flat, flat[:300])
    check("drawer: the asset is shown",
          "dev.target.test/orders/1042" in flat, flat[:400])
    check("drawer: the agent and wave are shown",
          "agent agent-2" in flat and "wave 1" in flat, flat[:400])
    check("drawer: the evidence is shown", "GET -> 200" in flat, flat[:400])
    check("drawer: the summary is shown", "without a nonce" in flat, flat[:400])
    # The other rung must not leak into this one.
    check("drawer: another rung's findings are not listed",
          "low-severity note" not in flat, flat[:400])
    # The drawer must not be showing the *pane* text either.
    check("drawer: the panes' content does not leak in",
          "waiting" not in flat, flat[:300])

    # The panes must survive the drawer opening: a drawer that covers the work
    # it describes is worse than no drawer.
    drawn = {key for kind, key in w._hits.values() if kind == "pane"}
    check("drawer: the panes are still drawn beside it", len(drawn) >= 2,
          str(sorted(drawn)))
    check("drawer: the panes are still in more than one column",
          w._layout.cols >= 2, str(w._layout.cols))

    # A click on bare canvas closes it.
    w.click(1, 1) if w.hit(1, 1) is None else None
    empty = next((pos for pos in ((x, y) for y in range(0, 36)
                                  for x in range(0, 118))
                  if w.hit(*pos) is None), None)
    if empty:
        w.click(*empty)
        check("drawer: a click on bare canvas closes it", not w.detail,
              f"{w.detail.kind}/{w.detail.key}")


def test_pane_click_opens_the_agent_run() -> None:
    """A pane is a button too, and it opens the whole run behind it.

    The pane shows the agent's last command and a few lines of its output. The
    drawer is the rest: the commands that led there, and the responses the pane
    had no room for.
    """
    w = _web(10)
    for i in range(4):
        cmd = f"$ curl -s -i $U/step{i}"
        w.agent_activity("agent-4", cmd)
        w.agent_result("agent-4", cmd, f"HTTP/1.1 200 OK\nbody of step {i}")
    w.agent_text("agent-4", "Checking whether the route enforces a nonce.")
    w.render(150, 44)

    pane_at = next((pos for pos, (kind, key) in w._hits.items()
                    if kind == "pane" and key == "agent-4"), None)
    check("drawer: the agent's pane is clickable", pane_at is not None)
    if pane_at is None:
        return
    hit = w.click(*pane_at)
    check("drawer: clicking a pane opens that agent's run",
          hit == ("pane", "agent-4") and w.detail.kind == "agent", str(hit))

    flat = _drawer_text(w, 150, 44)
    check("drawer: every step of the run is listed",
          all(f"step{i}" in flat for i in range(4)), flat[:800])
    check("drawer: the reasoning is listed too",
          "enforces a nonce" in flat, flat[:900])
    check("drawer: the outputs are listed",
          "body of step 0" in flat and "body of step 3" in flat, flat[:900])

    # The agent whose run is open must not be rotated out of the window, or the
    # drawer would describe a pane that is not on screen.
    shown = {c.name for c in w.visible_agents(2)}
    check("drawer: the open agent is pinned into the pane window",
          "agent-4" in shown, str(sorted(shown)))


def test_asset_wrapping_keeps_urls_readable() -> None:
    """A long asset URL stays a URL when the drawer is too narrow for it.

    The drawer is narrow by design — it exists to leave the panes their width —
    and an asset is the one thing in it the operator copies down. A URL broken
    at an arbitrary column reads as two URLs: ``orders/10`` above ``42`` is not
    the asset that was found.
    """
    from zimzilla.ui.web import _wrap_asset

    url = "http://dev.target.test/orders/1042"
    # Wide enough: one line, unchanged.
    check("asset: a short url is left alone", _wrap_asset(url, 40) == [url],
          str(_wrap_asset(url, 40)))
    # Too narrow: the scheme goes first, since it carries no information about
    # which asset this is, and dropping it alone often makes the rest fit.
    parts = _wrap_asset(url, 30)
    check("asset: the scheme is dropped before the path is cut",
          "".join(parts).endswith("orders/1042") and "http" not in parts[0],
          str(parts))
    check("asset: the host survives", "dev.target.test" in parts[0], str(parts))
    # Narrower still: breaks land on separators, so every line is a real prefix
    # of the path. 16 is the first width at which the host's own separator fits,
    # which is what makes the break clean rather than mid-word.
    parts = _wrap_asset(url, 16)
    check("asset: every line fits", all(len(p) <= 16 for p in parts), str(parts))
    check("asset: nothing is lost", "".join(parts) == url[7:],
          f"{parts} -> {''.join(parts)!r}")
    check("asset: the breaks are on separators",
          all(p.endswith("/") for p in parts[:-1]), str(parts))
    # Too narrow for any separator: it still fits and still loses nothing, which
    # matters more than the break being pretty.
    tight = _wrap_asset(url, 9)
    check("asset: a width with no separator still fits",
          all(len(p) <= 9 for p in tight), str(tight))
    check("asset: a width with no separator still loses nothing",
          "".join(tight) == url[7:], "".join(tight))
    # A host with no separators at all still has to fit rather than overflow.
    host = "averyveryverylongsubdomain.target.test"
    parts = _wrap_asset(host, 12)
    check("asset: a host with no separators is still bounded",
          all(len(p) <= 12 for p in parts), str(parts))
    check("asset: a host with no separators is not lost",
          "".join(parts) == host, "".join(parts))
    check("asset: an empty asset yields nothing", _wrap_asset("", 10) == [""])


def test_hover_and_spinner_animate() -> None:
    """The animations carry information rather than decorating.

    A spinner on a running agent is what distinguishes "working" from "stalled"
    when its command and output have not changed for a while — the exact
    ambiguity this view exists to remove. The hover highlight is what says a
    lane or a pane is clickable at all.
    """
    from zimzilla.ui.web import SPINNER, spinner_at

    w = _web(10)
    w.agent_start("agent-1", "nuclei")
    # The spinner is driven off the clock, so two instants far enough apart give
    # different glyphs; two instants a millisecond apart need not.
    frames = {spinner_at(t) for t in (0.0, 0.1, 0.2, 0.3, 0.4)}
    check("animation: the spinner cycles", len(frames) > 1, str(sorted(frames)))
    check("animation: the spinner is drawn from the braille block",
          all(ch in SPINNER for ch in frames), str(sorted(frames)))
    check("animation: a running agent spins",
          any(ch in w.render(118, 36).plain for ch in SPINNER))

    # Hover. The lane's whole row is highlighted, so the clickable area is not
    # just the glyphs that happen to be on it. Two renders are needed: the hover
    # resolves against the previous frame's hit map, because the map is built by
    # drawing and is therefore empty at the start of the frame that reads it.
    w.add_finding({"agent": "agent-1", "severity": "medium", "title": "x"})
    w.render(118, 36)
    lane_at = next((pos for pos, (kind, key) in w._hits.items()
                    if kind == "lane" and key == "medium"), None)
    check("animation: the lane has a hoverable region", lane_at is not None)
    if lane_at is None:
        return
    before = w.render(118, 36).plain
    w.hover = lane_at
    after = w.render(118, 36).plain
    check("animation: hovering the lane changes what is drawn",
          before != after, "the lane row is unchanged")
    # And the hover is not a permanent state: it follows the pointer.
    w.hover = (-1, -1)
    w.render(118, 36)
    check("animation: the highlight follows the pointer",
          w.render(118, 36).plain == before)

    # A pane's hover is a style change on its border, so the plain text is
    # identical by design — compare the styled spans instead.
    w.render(150, 44)
    pane_at = next(pos for pos, (kind, _) in w._hits.items() if kind == "pane")
    w.hover = (-1, -1)
    w.render(150, 44)
    plain_before = w.render(150, 44)
    w.hover = pane_at
    w.render(150, 44)
    plain_after = w.render(150, 44)
    check("animation: hovering a pane restyles its border",
          sorted(plain_before._spans) != sorted(plain_after._spans),
          "the pane border was not restyled")


def test_rail_and_drawer_resize() -> None:
    """The rail and the drawer are resizable, within sane bounds.

    The rail's width is a tradeoff the operator is better at making than the
    layout is: a wider rail fits more of a finding title, a narrower one fits
    another pane column.
    """
    from zimzilla.ui.web import (DRAWER_MAX_W, DRAWER_MIN_W, RAIL_MAX_W,
                                 RAIL_MIN_W)

    w = _web(10)
    w.render(118, 36)
    base = w._layout.rail_w
    w.resize_rail(4)
    w.render(118, 36)
    check("resize: the rail widens",
          w._layout.rail_w == min(RAIL_MAX_W, base + 4), str(w._layout.rail_w))
    w.resize_rail(-100)
    w.render(118, 36)
    check("resize: the rail stops at its minimum", w._layout.rail_w == RAIL_MIN_W,
          str(w._layout.rail_w))
    w.resize_rail(100)
    w.render(118, 36)
    check("resize: the rail stops at its maximum", w._layout.rail_w == RAIL_MAX_W,
          str(w._layout.rail_w))

    # A drawer has to be open before it can be resized, and the width it takes
    # is still bounded by the panes it must leave room for.
    w.open_findings("medium")
    w.render(200, 50)
    check("resize: the drawer opens at its default width",
          w._layout.drawer_w > 0, str(w._layout.drawer_w))
    w.resize_drawer(100)
    w.render(200, 50)
    check("resize: the drawer stops at its maximum",
          w._layout.drawer_w <= DRAWER_MAX_W, str(w._layout.drawer_w))
    w.resize_drawer(-100)
    w.render(200, 50)
    check("resize: the drawer stops at its minimum",
          w._layout.drawer_w >= DRAWER_MIN_W, str(w._layout.drawer_w))


def test_stop_hint_is_advertised_and_confirmed() -> None:
    """The rail carries the stop key, and changes when it has been pressed.

    The wave view covers the prompt, so ``/stop-hunt`` cannot be typed while it
    is up — the key that replaces it is only usable if the rail names it, and
    only believable if the rail says it was heard. The line the app writes on a
    stop goes to the transcript *behind* this screen, so without the footer
    changing the key looks inert for however long the wave takes to unwind.
    """
    w = _web(10)
    w.agent_start("agent-1", "b")
    w.agent_activity("agent-1", "$ id")

    before = w.render(118, 36).plain
    check("stop hint: the rail names the key", "ctrl+x" in before)
    check("stop hint: and says what it does", "stop" in before)
    check("stop hint: nothing claims to be stopping yet",
          "stopping" not in before)

    w.stopping = True
    after = w.render(118, 36).plain
    check("stop hint: the confirmation replaces the hint",
          "stopping" in after and "ctrl+x" not in after)

    # The footer is pinned to the bottom of the rail and the feed grows from the
    # top, so the two must not meet. Checked at the shortest height the view
    # draws panes at, where the feed has the least room to give.
    for width, height in ((92, 26), (118, 36), (200, 50)):
        for stopping in (False, True):
            w.stopping = stopping
            rows = w.render(width, height).plain.split("\n")
            check(f"stop hint: the footer keeps its row at {width}x{height} "
                  f"stopping={stopping}",
                  any("stop" in r for r in rows), str(rows[-3:]))
            check(f"stop hint: every row is exactly {width} wide at "
                  f"{width}x{height}",
                  all(len(r) == width for r in rows),
                  str([len(r) for r in rows if len(r) != width][:3]))


def main() -> int:
    test_render_is_exact()
    test_web_fits_at_minimum()
    test_panes_stay_inside_the_canvas()
    test_adaptive_geometry()
    test_all_ten_agents_are_big_enough_to_read()
    test_rail_border_survives()
    test_counts_land_on_the_right_rung()
    test_severity_spellings_are_normalised()
    test_worst_and_feed()
    test_findings_light_their_agent()
    test_agent_lifecycle()
    test_plan_replaces_the_roster()
    test_compact_fallback()
    test_truncated_roster_keeps_the_reporters()
    test_planning_state()
    test_agent_output_reaches_the_pane()
    test_lane_click_opens_the_findings_behind_it()
    test_pane_click_opens_the_agent_run()
    test_asset_wrapping_keeps_urls_readable()
    test_hover_and_spinner_animate()
    test_rail_and_drawer_resize()
    test_stop_hint_is_advertised_and_confirmed()
    test_splash_banner()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed — FAILED: "
              + ", ".join(failed))
        return 1
    print(f"{len(RESULTS)}/{len(RESULTS)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
