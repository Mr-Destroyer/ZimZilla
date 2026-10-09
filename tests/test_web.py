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
    """At exactly the minimum size the web is drawn, not the fallback."""
    from zimzilla.ui.web import MIN_HEIGHT, MIN_WIDTH

    w = _web(10)
    w.add_finding({"agent": "agent-1", "severity": "high", "title": "X"})
    text = w.render(MIN_WIDTH, MIN_HEIGHT).plain
    check("minimum: the tracker node is drawn at the minimum size",
          "ZIM-TRACK" in text)
    check("minimum: all ten agents are named", all(
        f"agent-{i}" in text for i in range(1, 11)), text[:120].replace("\n", "|"))


def test_panes_stay_inside_the_canvas() -> None:
    """No agent pane's border is clipped by the edge of the screen.

    A pane pushed past the right edge loses its closing border, which reads as a
    rendering fault rather than a layout one. The panes are inset by one column
    on each side so they are not flush against the terminal edge, so this finds
    the outermost columns by looking for the corners rather than assuming a
    position.
    """
    w = _web(10)
    for width, height in ((118, 36), (80, 24), (78, 22)):
        lines = _lines(w.render(width, height))
        # The panes are centred vertically, so the corners are not on row 0.
        # Collect the columns the corners actually land in, across every row.
        cols = {i for line in lines for i, ch in enumerate(line) if ch == "┌"}
        ends = {i for line in lines for i, ch in enumerate(line) if ch == "┐"}
        check(f"panes: both columns of panes have a top-left corner at "
              f"{width}x{height}", len(cols) == 2, str(sorted(cols)))
        check(f"panes: both columns of panes have a top-right corner at "
              f"{width}x{height}", len(ends) == 2, str(sorted(ends)))
        if len(cols) != 2 or len(ends) != 2:
            continue
        # A closing border is only drawn if the pane fit: if the right-hand pane
        # overflowed, its ┐ would have been written past the edge and dropped.
        check(f"panes: the right pane's border is inside the canvas at "
              f"{width}x{height}", max(ends) <= width - 2, str(max(ends)))
        check(f"panes: the left pane's border is inside the canvas at "
              f"{width}x{height}", min(cols) >= 1, str(min(cols)))


def test_spokes_do_not_punch_the_centre() -> None:
    """The centre node's border survives the spokes attaching to it.

    A spoke that overwrites the border it attaches to leaves a gap in the
    tracker's box, which looks like a bug in the drawing rather than a
    connection. The attach point is deliberately one column outside the border.
    """
    w = _web(10)
    for width, height in ((118, 36), (120, 40)):
        lines = _lines(w.render(width, height))
        top = next((i for i, l in enumerate(lines) if "┏━ ZIM-TRACK" in l), None)
        check(f"centre: the tracker box is drawn at {width}x{height}", top is not None)
        if top is None:
            continue
        row = lines[top]
        start = row.index("┏")
        end = row.index("┓")
        border = row[start:end + 1]
        check(f"centre: the top border is unbroken at {width}x{height}",
              set(border) <= {"┏", "┓", "━", " ", "Z", "I", "M", "-", "T", "R",
                              "A", "C", "K"},
              border)


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
    """A terminal too small for the web gets a list, with every fact in it."""
    w = _web(10)
    w.add_finding({"agent": "agent-3", "severity": "critical",
                   "title": "SQLi in ?id", "asset": "api.target.test"})
    w.agent_start("agent-7", "nuclei")
    for width, height in ((60, 18), (40, 12)):
        lines = _lines(w.render(width, height))
        check(f"fallback: {width}x{height} is still {height} rows",
              len(lines) == height, str(len(lines)))
        text = "\n".join(lines)
        check(f"fallback: {width}x{height} does not draw the web",
              "╲" not in text and "╱" not in text)
        check(f"fallback: {width}x{height} still names the tracker",
              "ZIM-TRACK" in text)
        check(f"fallback: {width}x{height} still shows the tally",
              "CRIT 1" in text, text.replace("\n", "|")[:200])
        # The terminal is too short to list ten agents. What matters is that the
        # list never *lies* about the wave: whoever was cut has to be accounted
        # for, or a nine-line list under a "10 agents" header reads as a lost
        # worker.
        listed = sum(1 for line in lines
                     if line.strip().startswith(("·", "◆", "▸")) and "─▶" not in line)
        check(f"fallback: {width}x{height} accounts for every agent it cannot "
              f"list", "more" in text or listed >= 10,
              f"listed={listed} text={text.replace(chr(10), '|')[:160]}")
        check(f"fallback: {width}x{height} names at least one agent",
              "agent-1" in text)
    # A one-agent wave has no web to draw, so it falls back too.
    one = _web(1)
    check("fallback: a single agent is a list, not a web",
          "╲" not in one.render(118, 36).plain)


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
          "+4 more" in text, text.replace("\n", "|"))
    # The count must add up: 10 agents, 5 listed plus 4 hidden is 9, so the
    # arithmetic is checked against the header rather than a magic number.
    listed = sum(1 for line in lines
                 if line.strip().startswith(("·", "◆", "▸")) and "─▶" not in line)
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

    The web is pushed at ``hunt_wave_start``, but the roster only arrives with
    ``hunt_plan``, which is emitted after the planner's turn returns. That turn
    is a real model call over recon, findings, wave history and a long
    instruction — tens of seconds. For all of it the web held zero agents and
    drew "WAVE 1 · 0 agents", which reads as a wave that started with nobody in
    it: the exact fault this screen exists to make visible.
    """
    from zimzilla.theme import get_palette
    from zimzilla.ui.web import WaveWeb

    w = WaveWeb("*.lerevecraze.com", get_palette("green", None), wave=1, size=10)
    check("planning: a fresh web is planning", w.planning is True)

    # A planning wave always takes the list: `render` needs two agents to draw
    # a web, so there is no wide layout to check while the roster is empty. The
    # list is what the operator sees for the whole planner call, so that is
    # where the state has to be legible.
    narrow = w.render(118, 36).plain
    check("planning: the header says planning, not 0 agents",
          "planning" in narrow and "0 agents" not in narrow,
          narrow.replace("\n", "|")[:200])
    check("planning: the empty roster slot is explained",
          "roster lands" in narrow, narrow.replace("\n", "|")[:200])

    # And it stops saying it the moment a roster arrives.
    w.set_plan([{"name": f"agent-{i + 1}", "brief": "b"} for i in range(10)])
    check("planning: a roster ends the planning state", w.planning is False)
    after = w.render(118, 36).plain
    check("planning: the web is drawn, not the list",
          "╲" in after or "╱" in after, after.replace("\n", "|")[:120])
    check("planning: the web goes back to LIVE", "LIVE" in after,
          after.replace("\n", "|")[:120])
    header = w.render(60, 18).plain.split("\n")[0]
    check("planning: the header counts the real roster",
          "10 agents" in header, header)

    # A finding with no roster is not a planning wave — it is a wave that has
    # already reported, which only happens once agents exist. Guard the
    # predicate so a stray finding cannot leave the header stuck on PLANNING.
    w2 = WaveWeb("t.test", get_palette("green", None), wave=1, size=10)
    w2.add_finding({"agent": "agent-1", "severity": "high", "title": "x"})
    check("planning: a reported finding is not planning", w2.planning is False)


def main() -> int:
    test_render_is_exact()
    test_web_fits_at_minimum()
    test_panes_stay_inside_the_canvas()
    test_spokes_do_not_punch_the_centre()
    test_counts_land_on_the_right_rung()
    test_severity_spellings_are_normalised()
    test_worst_and_feed()
    test_findings_light_their_agent()
    test_agent_lifecycle()
    test_plan_replaces_the_roster()
    test_compact_fallback()
    test_truncated_roster_keeps_the_reporters()
    test_planning_state()
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
