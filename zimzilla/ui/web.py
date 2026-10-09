"""The wave web — ten agents and the tracker they all report into.

``/bug-hunt`` runs its wave as ten concurrent agents, and until this existed the
operator's only view of that was a scrolling transcript: ten interleaved streams
of tool output, with the findings buried in whichever one happened to print
them. This draws the shape of the wave instead — ten panes around a centre node
named ZIM-TRACK, joined by silk, so it is obvious at a glance which agents are
working and what the engagement has actually found.

**ZIM-TRACK is a view, not an agent.** It is a deterministic tally fed by
``report_finding`` calls as they land (see ``hunt._report_live``). It costs
nothing, it cannot hallucinate a bug, and it updates in the same event that
carries the finding — an eleventh model call would be slower than the findings
it was reporting and could invent conclusions about them.

Everything here is drawn into a character grid and converted to a Rich ``Text``
at the end. That is the only way to get the diagonal spokes: Textual's layout
engine places rectangles, so a line that is not axis-aligned cannot be a widget.
The matrix rain takes the same approach for the same reason (see ``rain.py``).
The grid is also what makes this testable — ``WaveWeb.render`` is a pure
function of the model and a size, so the geometry is asserted on directly
without a terminal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from rich.text import Text

from ..hunt import SEVERITY_ORDER, normalise_severity
from ..theme import Palette

#: The severity ladder, worst first, with the colour each rung is drawn in.
#: Read off the palette rather than invented, so a theme change carries.
_SEVERITY_ATTR = {
    "critical": "red",
    "high": "red",
    "medium": "amber",
    "low": "primary",
    "info": "dim",
}

SEVERITY_LABEL = {
    "critical": "CRIT",
    "high": "HIGH",
    "medium": "MED",
    "low": "LOW",
    "info": "INFO",
}

#: Rungs the tracker shows. INFO is deliberately absent: it is the bucket a
#: finding lands in when a model gave no severity at all, so a lane for it would
#: mostly count parser noise.
LANES = ("critical", "high", "medium", "low")

#: Geometry. The web needs room for two columns, a centre node and the silk
#: between them; below this it degrades to a plain list rather than drawing a
#: web with the boxes overlapping.
MIN_WIDTH = 78
MIN_HEIGHT = 22
SIDE_W = 18          # width of one agent pane, borders included
BOX_H = 3            # top border, one content row, bottom border
CENTRE_H = 15
CENTRE_MAX_W = 34
FEED_LINES = 4       # findings echoed in the centre node

#: How long an agent's pane stays lit after it reports something. Long enough
#: to catch the eye, short enough that a quiet agent does not look active.
FLASH_SECONDS = 2.5


def _clip(text: str, width: int) -> str:
    """Truncate to *width*, with an ellipsis when something was cut."""
    s = str(text or "").replace("\n", " ").strip()
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    return s[: width - 1] + "…"


def severity_style(palette: Palette, severity: str) -> str:
    """The Rich style for a severity rung, on this palette."""
    attr = _SEVERITY_ATTR.get(normalise_severity(severity), "dim")
    return getattr(palette, attr, palette.dim)


@dataclass
class AgentCell:
    """One agent's pane: what it is doing, and what it has found."""

    name: str
    brief: str = ""
    activity: str = "waiting"
    status: str = "idle"          # idle | running | done | failed
    findings: int = 0
    worst: str = ""               # worst severity it has reported
    last: str = ""                # title of its most recent finding
    flash: float = 0.0            # monotonic stamp of its last report

    def lit(self, now: float | None = None) -> bool:
        """Whether the pane is still flashing from a recent report."""
        return (self.flash > 0.0
                and (now if now is not None else time.monotonic()) - self.flash
                < FLASH_SECONDS)


class _Grid:
    """A mutable character canvas. Out-of-bounds writes are dropped."""

    def __init__(self, width: int, height: int) -> None:
        self.width = max(0, width)
        self.height = max(0, height)
        self.cells: list[list[tuple[str, str]]] = [
            [(" ", "") for _ in range(self.width)] for _ in range(self.height)
        ]

    def put(self, x: int, y: int, ch: str, style: str = "") -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            self.cells[y][x] = (ch, style)

    def text(self, x: int, y: int, s: str, style: str = "") -> None:
        for i, ch in enumerate(s):
            self.put(x + i, y, ch, style)

    def box(self, x: int, y: int, w: int, h: int, title: str = "",
            style: str = "", heavy: bool = False) -> None:
        """A bordered pane, optionally with a title in its top border."""
        if w < 2 or h < 2:
            return
        if heavy:
            tl, tr, bl, br, hz, vt = "┏", "┓", "┗", "┛", "━", "┃"
        else:
            tl, tr, bl, br, hz, vt = "┌", "┐", "└", "┘", "─", "│"
        self.put(x, y, tl, style)
        self.put(x + w - 1, y, tr, style)
        self.put(x, y + h - 1, bl, style)
        self.put(x + w - 1, y + h - 1, br, style)
        for i in range(1, w - 1):
            self.put(x + i, y, hz, style)
            self.put(x + i, y + h - 1, hz, style)
        for j in range(1, h - 1):
            self.put(x, y + j, vt, style)
            self.put(x + w - 1, y + j, vt, style)
        if title and w >= 8:
            label = f" {_clip(title, w - 6)} "
            self.text(x + 2, y, label, style)

    def vline(self, x: int, y0: int, y1: int, ch: str = "│", style: str = "") -> None:
        for y in range(min(y0, y1), max(y0, y1) + 1):
            self.put(x, y, ch, style)

    def hline(self, x0: int, x1: int, y: int, ch: str = "─", style: str = "") -> None:
        for x in range(min(x0, x1), max(x0, x1) + 1):
            self.put(x, y, ch, style)

    def spoke(self, x0: int, y0: int, x1: int, y1: int, style: str = "") -> None:
        """A line from one cell to another, as a staircase of box glyphs.

        Bresenham, with the glyph chosen from the step direction: a terminal
        cell cannot hold a true diagonal, so a run of ``╲`` reading down-right
        is what a diagonal *is* here. The endpoints are skipped — they land on a
        box border or a strand, and overwriting that with a spoke glyph would
        punch a hole in the pane it attaches to.
        """
        dx, dy = x1 - x0, y1 - y0
        if dx == 0 and dy == 0:
            return
        if dx == 0:
            ch = "│"
        elif dy == 0:
            ch = "─"
        elif (dx > 0) == (dy > 0):
            ch = "╲"
        else:
            ch = "╱"

        steps = max(abs(dx), abs(dy))
        sx = dx / steps if steps else 0
        sy = dy / steps if steps else 0
        for i in range(1, steps):          # endpoints excluded, see above
            x = round(x0 + sx * i)
            y = round(y0 + sy * i)
            self.put(x, y, ch, style)

    def to_text(self) -> Text:
        """The grid as Rich text, merging runs that share a style.

        Merging matters for size, not looks: a 100x40 canvas is 4,000 cells, and
        one ``Text`` span per cell would be 4,000 segments for the compositor to
        walk on every repaint.
        """
        out = Text()
        for row in self.cells:
            run: list[str] = []
            run_style: str | None = None
            for ch, style in row:
                if run_style is None or style == run_style:
                    run_style = style
                    run.append(ch)
                else:
                    out.append("".join(run), style=run_style)
                    run, run_style = [ch], style
            if run:
                out.append("".join(run), style=run_style or "")
            out.append("\n")
        # Drop the trailing newline: the widget supplies its own row breaks.
        if out.plain.endswith("\n"):
            out = out[:-1]
        return out


class WaveWeb:
    """The model behind the wave view: ten agents and the tracker.

    Deliberately free of Textual. Every mutation here is driven by a hunt event
    and every read is a pure render, which is what lets the whole thing be
    tested by feeding it events and asserting on the text it draws.
    """

    def __init__(self, target: str, palette: Palette, *, wave: int = 1,
                 size: int = 10) -> None:
        self.target = target
        self.palette = palette
        self.wave = wave
        self.size = size
        self.agents: list[AgentCell] = []
        self.findings: list[dict] = []
        self.started = time.monotonic()
        self.ended = 0.0

    # ---- mutation ---------------------------------------------------------
    def set_plan(self, workers: list[dict], *, wave: int | None = None) -> None:
        """Lay out the roster the planner returned, one pane per worker."""
        if wave is not None:
            self.wave = wave
        self.agents = [
            AgentCell(name=str(w.get("name", f"agent-{i + 1}")),
                      brief=str(w.get("brief", "")))
            for i, w in enumerate(workers or [])
        ][: self.size]

    def agent_start(self, name: str, brief: str = "") -> None:
        cell = self._cell(name)
        if cell is None:
            return
        cell.status = "running"
        cell.activity = brief or cell.brief or "starting"
        if brief:
            cell.brief = brief

    def agent_activity(self, name: str, activity: str) -> None:
        """Record what an agent is doing now — a tool call, usually."""
        cell = self._cell(name)
        if cell is None:
            return
        cell.status = "running"
        cell.activity = activity

    def agent_done(self, name: str, *, ok: bool = True) -> None:
        cell = self._cell(name)
        if cell is None:
            return
        cell.status = "done" if ok else "failed"
        cell.activity = "finished" if ok else "failed"

    def add_finding(self, finding: dict) -> None:
        """Tally a finding and light the pane of the agent that reported it.

        Called for every finding the run accepts — including the ones the
        post-wave harvest finds, which is why the flash is keyed off the event
        rather than off the wave boundary.
        """
        sev = normalise_severity(finding.get("severity"))
        agent = str(finding.get("agent", "") or "?")
        self.findings.append({**finding, "severity": sev})
        cell = self._cell(agent)
        if cell is not None:
            cell.findings += 1
            cell.last = str(finding.get("title", "") or "")
            if not cell.worst or SEVERITY_ORDER.get(sev, 9) < SEVERITY_ORDER.get(cell.worst, 9):
                cell.worst = sev
            cell.flash = time.monotonic()

    def end(self) -> None:
        self.ended = time.monotonic()
        for cell in self.agents:
            if cell.status == "running":
                cell.status = "done"
                cell.activity = "finished"

    def _cell(self, name: str) -> AgentCell | None:
        for cell in self.agents:
            if cell.name == name:
                return cell
        return None

    # ---- reads ------------------------------------------------------------
    def counts(self) -> dict[str, int]:
        """Findings per severity rung, including the rungs with none."""
        out = {sev: 0 for sev in SEVERITY_ORDER}
        for f in self.findings:
            sev = normalise_severity(f.get("severity"))
            out[sev] = out.get(sev, 0) + 1
        return out

    def elapsed(self) -> int:
        end = self.ended or time.monotonic()
        return int(end - self.started)

    def recent(self, limit: int = FEED_LINES) -> list[dict]:
        """The most recently reported findings, newest first."""
        return list(reversed(self.findings[-limit:]))

    def worst(self) -> str:
        """The worst severity reported so far, or '' if nothing has been."""
        if not self.findings:
            return ""
        return min((normalise_severity(f.get("severity")) for f in self.findings),
                   key=lambda s: SEVERITY_ORDER.get(s, 9))

    # ---- render -----------------------------------------------------------
    def render(self, width: int, height: int) -> Text:
        """Draw the wave. Degrades to a list when there is no room for a web."""
        p = self.palette
        if width < MIN_WIDTH or height < MIN_HEIGHT or len(self.agents) < 2:
            return self._render_compact(width, height)
        return self._render_web(width, height, p)

    # -- the web ------------------------------------------------------------
    def _render_web(self, width: int, height: int, p: Palette) -> Text:
        g = _Grid(width, height)
        now = time.monotonic()

        left = self.agents[: (len(self.agents) + 1) // 2]
        right = self.agents[(len(self.agents) + 1) // 2:]

        left_x = 1
        right_x = width - 1 - SIDE_W
        left_strand = left_x + SIDE_W
        right_strand = right_x - 1

        centre_w = max(20, min(CENTRE_MAX_W, width - 2 * (SIDE_W + 4) - 4))
        centre_x = (width - centre_w) // 2
        centre_y = max(1, (height - CENTRE_H) // 2)

        self._draw_column(g, left, left_x, left_strand, height, p, now, side="left")
        self._draw_column(g, right, right_x, right_strand, height, p, now, side="right")

        # The tracker's own pane. Its border takes the worst severity found so
        # far, so the centre of the web reads red the moment anything critical
        # lands — before anyone has read a word of it.
        worst = self.worst()
        border = severity_style(p, worst) if worst else p.accent
        g.box(centre_x, centre_y, centre_w, CENTRE_H, "ZIM-TRACK",
              style=f"bold {border}", heavy=True)
        self._draw_centre(g, centre_x, centre_y, centre_w, p, now)

        # Silk: one spoke per agent, from its column strand to the centre node.
        # The attach row is spread down the centre pane's edge so the spokes fan
        # out rather than converging on one point, which is what makes it read
        # as a web instead of a bundle of arrows.
        for side, cells, strand, tip in (
            ("left", left, left_strand, centre_x - 1),
            ("right", right, right_strand, centre_x + centre_w),
        ):
            for i, cell in enumerate(cells):
                row = self._box_row(i, len(cells), height)
                attach = min(centre_y + 2 + i * 2, centre_y + CENTRE_H - 3)
                style = (severity_style(p, cell.worst)
                         if cell.lit(now) and cell.worst else p.dim)
                g.spoke(strand + (1 if side == "left" else -1), row, tip, attach,
                        style)
                # A hub where the spoke meets the centre: the mock-up's ●, and
                # the thing that stops the diagonal reading as a stray glyph.
                g.put(tip, attach, "●", f"bold {p.accent}")
        return g.to_text()

    def _draw_column(self, g: _Grid, cells: list[AgentCell], x: int, strand: int,
                     height: int, p: Palette, now: float, *, side: str) -> None:
        """One column of agent panes, plus the strand that ties them together."""
        if not cells:
            return
        rows = [self._box_row(i, len(cells), height) for i in range(len(cells))]
        g.vline(strand, rows[0], rows[-1] + BOX_H - 1, "│", p.dim)
        for cell, row in zip(cells, rows):
            self._draw_agent(g, cell, x, row, p, now)
            # A short tick from the pane to the strand, so the column is joined
            # up rather than ten panes that happen to be stacked.
            mid = row + 1
            g.put(strand, mid, "├" if side == "left" else "┤", p.dim)

    def _draw_agent(self, g: _Grid, cell: AgentCell, x: int, y: int,
                    p: Palette, now: float) -> None:
        lit = cell.lit(now)
        if lit and cell.worst:
            border = f"bold {severity_style(p, cell.worst)}"
        elif cell.status == "failed":
            border = p.red
        elif cell.status == "done":
            border = p.dim
        elif cell.status == "running":
            border = p.accent
        else:
            border = p.dim

        # The marker rides in the title; the count is right-aligned in the same
        # border. Both live in the top border rather than the body because the
        # one content row is usually full of command text — and an agent's tally
        # has to be readable without waiting for it to report again.
        mark = "◆" if cell.findings else ("▸" if cell.status == "running" else "·")
        g.box(x, y, SIDE_W, BOX_H, f"{mark} {cell.name}", style=border)
        if cell.findings:
            g.text(x + SIDE_W - 4, y, f"×{cell.findings}",
                   f"bold {severity_style(p, cell.worst or 'info')}")

        # The activity line: the last tool call, or the finding if there is one
        # newer. A pane that just reported something is more interesting than
        # the command it ran to find it.
        body = cell.last if (lit and cell.last) else cell.activity
        if cell.status == "done" and cell.findings:
            body = f"{cell.findings} finding{'s' if cell.findings != 1 else ''}"
        style = (severity_style(p, cell.worst) if (lit and cell.worst)
                 else (p.primary if cell.status == "running" else p.dim))
        g.text(x + 2, y + 1, _clip(body, SIDE_W - 4), style)

    def _box_row(self, index: int, total: int, height: int) -> int:
        """The top row of the *index*-th pane in a column of *total*."""
        block = total * BOX_H + max(0, total - 1)
        top = max(1, (height - block) // 2)
        return top + index * (BOX_H + 1)

    def _draw_centre(self, g: _Grid, x: int, y: int, w: int,
                     p: Palette, now: float) -> None:
        """The tracker's contents: liveness, severity lanes, and the feed."""
        counts = self.counts()
        secs = self.elapsed()

        head = f"● LIVE  {secs // 60}:{secs % 60:02d}"
        if self.ended:
            head = f"● CLOSED  {secs // 60}:{secs % 60:02d}"
        g.text(x + 2, y + 1, _clip(head, w - 4), f"bold {p.accent}")

        total = sum(counts.values())
        summary = f"{total} finding{'s' if total != 1 else ''}"
        g.text(x + 2, y + 2, _clip(summary, w - 4), p.primary)

        # The lanes. The bar is scaled to the *lane* count, not to the busiest
        # lane: normalising across lanes made a single LOW draw the same length
        # as a single CRIT, which is the one comparison the bar exists to make.
        # A fixed width also means a lane only changes when its own count does.
        bar_w = max(0, w - 13)
        span = max(1, max((counts.get(s, 0) for s in LANES), default=0))
        for i, sev in enumerate(LANES):
            row = y + 4 + i
            n = counts.get(sev, 0)
            g.text(x + 2, row, f"{SEVERITY_LABEL[sev]:<5}", severity_style(p, sev))
            g.text(x + 8, row, f"{n:>2}",
                   f"bold {severity_style(p, sev)}" if n else p.dim)
            if n:
                filled = max(1, int(round(bar_w * (n / span))))
                g.text(x + 11, row, "█" * filled, severity_style(p, sev))

        rule = y + 4 + len(LANES)
        g.hline(x + 2, x + w - 3, rule, "─", p.dim)

        # The feed. Newest first, each finding as two lines: who reported what,
        # then the asset under it. No blank row between entries — at four
        # entries that is two rows of padding spent on nothing, and the severity
        # colour already separates them.
        feed = self.recent(FEED_LINES)
        if not feed:
            g.text(x + 2, rule + 1, "nothing confirmed yet", p.dim)
        text_w = max(0, w - 16)
        for i, f in enumerate(feed):
            row = rule + 1 + i * 2
            if row + 1 > y + CENTRE_H - 2:
                break
            sev = normalise_severity(f.get("severity"))
            g.text(x + 2, row, f"{_clip(str(f.get('agent', '?')), 8):<8}", p.dim)
            g.text(x + 11, row, "─▶", severity_style(p, sev))
            g.text(x + 13, row, _clip(str(f.get("title", "")), text_w),
                   f"bold {severity_style(p, sev)}")
            asset = _clip(str(f.get("asset", "") or ""), text_w)
            if asset:
                g.text(x + 13, row + 1, asset, p.dim)

    # -- the fallback -------------------------------------------------------
    def _render_compact(self, width: int, height: int) -> Text:
        """The web at a size that cannot hold it: a list, in the same order.

        A terminal too small for the geometry still gets every fact — which
        agents are running, what they are doing, and the tally — laid out as
        lines instead of panes. Drawing the web anyway would overlap the boxes
        and produce something unreadable, which is worse than not drawing it.
        """
        p = self.palette
        g = _Grid(width, height)
        row = 0

        def line(s: str, style: str = "") -> None:
            nonlocal row
            if row < height:
                g.text(0, row, _clip(s, width), style)
                row += 1

        counts = self.counts()
        secs = self.elapsed()
        rule_w = max(0, min(width, 60) - 2)
        line(f" WAVE {self.wave} · {len(self.agents)} agents · {self.target}",
             f"bold {p.accent}")
        line(f" ZIM-TRACK  {secs // 60}:{secs % 60:02d}  "
             f"{sum(counts.values())} finding(s)", p.primary)
        line(" " + "─" * rule_w, p.dim)

        # The roster shares the height with the header, the tally and the feed,
        # so a short terminal cannot list every agent. Whoever is cut is shown
        # as a count rather than dropped: a list that stops at agent-9 under a
        # header reading "10 agents" reads as a wave that lost a worker.
        tail = height - len(LANES) - 2
        busy = [i for i, c in enumerate(self.agents)
                if c.findings or c.status != "idle"]
        idle = [i for i, c in enumerate(self.agents) if i not in busy]
        order = busy + idle
        roster = [self.agents[i] for i in order[:max(0, tail)]]
        hidden = len(self.agents) - len(roster)
        if hidden > 0:
            roster = roster[:max(0, tail - 1)]

        for cell in roster:
            mark = "◆" if cell.findings else ("▸" if cell.status == "running" else "·")
            style = (severity_style(p, cell.worst) if cell.findings
                     else (p.primary if cell.status == "running" else p.dim))
            body = cell.last if cell.findings else cell.activity
            line(f" {mark} {cell.name:<10} {_clip(body, max(0, width - 15))}",
                 style)
        if hidden > 0:
            line(f" · +{hidden} more", p.dim)

        line(" " + "─" * rule_w, p.dim)
        lanes = "  ".join(f"{SEVERITY_LABEL[s]} {counts.get(s, 0)}" for s in LANES)
        line(" " + lanes, p.primary)
        for f in self.recent(2):
            sev = normalise_severity(f.get("severity"))
            line(f" · {f.get('agent', '?')} ─▶ "
                 f"[{SEVERITY_LABEL.get(sev, sev)}] {f.get('title', '')}",
                 severity_style(p, sev))
        return g.to_text()
