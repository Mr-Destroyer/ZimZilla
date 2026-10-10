"""The wave view — ten agents, a tracker rail, and the detail drawer.

``/bug-hunt`` runs its wave as ten concurrent agents, and until this existed the
operator's only view of that was a scrolling transcript: ten interleaved streams
of tool output, with the findings buried in whichever one happened to print
them. This draws the shape of the wave instead: one pane per agent showing the
command it is running and what that command returned, beside a rail that tallies
what the engagement has actually found.

**Why a rail and not a web.** The first cut of this drew the tracker as a centre
node with ten panes around it joined by diagonal silk. It looked right and it
worked badly: the diagonals cost a column of gutter on each side, the centre node
took 30 columns out of the middle, and the panes were left 30 columns wide by six
rows tall — one row of command and three of output, which is not enough to read
an HTTP response. The silk was also the only thing on screen that could not carry
information. The tracker is a rail on the left now, the panes get every column
that is left and every row there is, and the width the web spent on gutter and
spokes pays for the command and its output.

**ZIM-TRACK is a view, not an agent.** It is a deterministic tally fed by
``report_finding`` calls as they land (see ``hunt._report_live``). It costs
nothing, it cannot hallucinate a bug, and it updates in the same event that
carries the finding — an eleventh model call would be slower than the findings
it was reporting and could invent conclusions about them.

**Everything is drawn into a character grid** and converted to a Rich ``Text``
at the end. The grid is what makes this testable: ``WaveWeb.render`` is a pure
function of the model and a size, so the geometry is asserted on directly without
a terminal. It is also the hit map — the grid records what each cell belongs to
as it paints, which is what lets a click on a severity lane or an agent's pane
resolve to the thing that was drawn there (``WaveWeb.hit``). The model predicts
nothing about its own layout; the drawing *is* the layout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

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

#: Rungs the rail shows. INFO is deliberately absent: it is the bucket a
#: finding lands in when a model gave no severity at all, so a lane for it would
#: mostly count parser noise.
LANES = ("critical", "high", "medium", "low")

#: Geometry. The view needs room for the rail, at least one column of panes and
#: a pane wide enough to read a command in; below this it degrades to a plain
#: list rather than drawing panes that overlap.
MIN_WIDTH = 92
MIN_HEIGHT = 26

#: The tracker rail. Narrow enough to leave the panes most of a 118-column
#: terminal, wide enough for a lane bar and a truncated finding title. The
#: default is the narrow end of that range on purpose: at 118 columns the four
#: columns a 30-wide rail would cost come straight out of the panes, and the
#: panes are where the commands and output are — the rail only has to fit
#: "ZIM-TRACK" and a severity label, which 26 columns does with room to spare.
RAIL_W = 26
RAIL_MIN_W = 22
RAIL_MAX_W = 44

#: The detail drawer, opened by clicking a lane or a pane. It takes width from
#: the right; the panes are re-laid out around whatever is left, so opening it
#: never covers the work it is describing.
DRAWER_W = 54
DRAWER_MIN_W = 34
DRAWER_MAX_W = 76

#: Pane geometry. ``PANE_PREF_W`` is the width a pane prefers — enough for a full
#: ``curl`` command without eliding the interesting flag — and the layout only
#: goes below it when the terminal leaves no choice.
PANE_PREF_W = 32
PANE_MIN_W = 22
#: The width past which a pane is only holding empty space. A command is
#: clipped to the pane either way, so a 60-column pane with a 26-column command
#: in it is a command and a hole — and a two-agent wave would otherwise draw two
#: 80-column panes and leave the middle of the screen blank.
PANE_MAX_W = 44
BOX_MIN_H = 5         # top border, command, one output row, tail, bottom border
#: The height below which a pane is not worth drawing: a command and three lines
#: of what it returned. This is the number the column choice is built around —
#: a 28-column pane that shows four output rows beats a 40-column one that shows
#: one, because the output is the thing that was unreadable.
BOX_GOOD_H = 7
BOX_MAX_H = 16

#: Findings echoed in the rail under the lanes.
FEED_LINES = 3

#: How long an agent's pane stays lit after it reports something. Long enough
#: to catch the eye, short enough that a quiet agent does not look active.
FLASH_SECONDS = 2.5

#: Command history kept per agent, for the drawer that is opened on it later.
HISTORY_MAX = 40

#: How long the hidden-agent window holds still before rotating to the next
#: quiet agents. Slow enough to read a pane, fast enough that a stalled agent is
#: not invisible for long.
ROTATE_SECONDS = 8.0

#: How many output lines a pane keeps from the last result. The pane draws what
#: its height allows; the rest are for the drawer.
OUTPUT_MAX = 60

#: Braille spinner. One glyph per tick, so a running agent is visibly running
#: even when its command and output have not changed for a while — a frozen pane
#: is otherwise indistinguishable from a stalled agent, which is the fault this
#: view exists to expose.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPINNER_SECONDS = 0.09

#: How long a command takes to type itself into its pane. Fast enough not to
#: delay the information, slow enough that a change of command is caught by the
#: eye while reading a different pane.
REVEAL_SECONDS = 0.45

#: A finding's pulse travelling along the rail. Cosmetic, but it is the one
#: animation here that points at *where* the news landed.
PULSE_SECONDS = 1.4


def _clip(text: str, width: int) -> str:
    """Truncate to *width*, with an ellipsis when something was cut."""
    s = str(text or "").replace("\n", " ").strip()
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    return s[: width - 1] + "…"


def _wrap_asset(asset: str, width: int) -> list[str]:
    """Wrap a URL or host so it stays recognisable when it does not fit.

    A URL broken at an arbitrary column reads as two URLs — ``orders/10`` on one
    line and ``42`` on the next is not the asset that was found, and the drawer
    is the one place the operator goes to copy it down. So the scheme goes first
    (it says nothing about *which* asset this is), then breaks happen at path
    separators, so every line after the first is still a real path prefix.
    """
    if width <= 0:
        return []
    asset = str(asset or "").strip()
    if not asset:
        return [""]
    # Fits as it is: left exactly as found, scheme and all. Dropping the scheme
    # here would be a loss for no gain — a URL the operator copies out of the
    # drawer should be the URL that was found.
    if len(asset) <= width:
        return [asset]
    # It does not fit, so something has to go. The scheme is the least
    # informative part and the easiest to drop, and dropping it alone often makes
    # the rest fit. Dropped up front rather than left on the first line: at a
    # narrow width the first break would otherwise land right after it and leave
    # a line reading "http://" on its own, which is worse than useless.
    for scheme in ("https://", "http://"):
        if asset.startswith(scheme):
            asset = asset[len(scheme):]
            break
    if len(asset) <= width:
        return [asset]
    out: list[str] = []
    rest = asset
    while len(rest) > width:
        # Prefer a separator, so the line is a prefix of the real path. A host
        # with no separator at all still has to be split, or it would overflow.
        cut = rest.rfind("/", 0, width + 1)
        if cut <= 0:
            cut = rest.rfind("?", 0, width + 1)
        if cut <= 0:
            cut = width
        else:
            cut += 1
        out.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        out.append(rest)
    return out


def _wrap(text: str, width: int) -> list[str]:
    """Wrap *text* to *width*, on words where it can.

    The planner's prose is the only account of why a wave is aimed where it is,
    so it is wrapped rather than clipped — a line cut at the pane edge is not an
    account of anything. A word longer than the line is split rather than
    allowed to overflow.
    """
    if width <= 0:
        return []
    out: list[str] = []
    for para in str(text or "").splitlines():
        para = para.strip()
        if not para:
            out.append("")
            continue
        line = ""
        for word in para.split():
            while len(word) > width:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line = f"{line} {word}"
            else:
                out.append(line)
                line = word
        if line:
            out.append(line)
    return out


def severity_style(palette: Palette, severity: str) -> str:
    """The Rich style for a severity rung, on this palette."""
    attr = _SEVERITY_ATTR.get(normalise_severity(severity), "dim")
    return getattr(palette, attr, palette.dim)


def spinner_at(now: float | None = None) -> str:
    """The spinner glyph for this instant. Driven off the clock, not a counter.

    ``render`` runs several times a second and is also called directly by tests
    and by the findings path, so a frame counter would advance at a rate that
    depends on how often the view happened to be painted. The clock does not.
    """
    t = now if now is not None else time.monotonic()
    return SPINNER[int(t / SPINNER_SECONDS) % len(SPINNER)]


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
    #: The command this agent is running, one line — `$ curl -s $U/admin`.
    command: str = ""
    #: The last few lines of what that command returned. Bounded, and the pane
    #: draws only as many of them as its height allows.
    output: list[str] = field(default_factory=list)
    #: ``(command, output)`` pairs, oldest first, for the drawer. This is the
    #: agent's whole run, not only where it ended up.
    history: list[tuple[str, str]] = field(default_factory=list)
    #: Monotonic stamp of the last thing this agent did — a call or a result.
    #: Drives ``visible_agents``, which shows the busiest panes first.
    activity_at: float = 0.0
    #: Monotonic stamp the current command was set, so it can type itself in.
    command_at: float = 0.0
    #: Index into ``history`` of a tool call whose result has not arrived yet,
    #: or -1. The call is recorded when it starts so the drawer does not lag the
    #: pane, and the result fills that same entry in rather than appending a
    #: second one — otherwise every step appears twice.
    pending: int = -1

    def lit(self, now: float | None = None) -> bool:
        """Whether the pane is still flashing from a recent report."""
        return (self.flash > 0.0
                and (now if now is not None else time.monotonic()) - self.flash
                < FLASH_SECONDS)

    def pulse(self, now: float | None = None) -> bool:
        """Whether the finding pulse is still travelling for this agent."""
        return (self.flash > 0.0
                and (now if now is not None else time.monotonic()) - self.flash
                < PULSE_SECONDS)

    def revealed(self, now: float | None = None) -> str:
        """The command, typed in as far as this instant has got.

        A command appearing whole in a pane that the eye is not currently on is
        easy to miss; typing it in over half a second is the difference between
        noticing the agent moved on and assuming it is stuck.
        """
        if not self.command:
            return ""
        t = now if now is not None else time.monotonic()
        if not self.command_at:
            return self.command
        age = t - self.command_at
        if age >= REVEAL_SECONDS:
            return self.command
        n = int(len(self.command) * (age / REVEAL_SECONDS))
        return self.command[: max(1, n)]


@dataclass
class Detail:
    """What the drawer is showing, if anything.

    ``kind`` is ``"findings"`` (the list behind one severity lane) or ``"agent"``
    (one agent's command history). ``key`` is the severity or the agent name.
    """

    kind: str = ""
    key: str = ""

    def __bool__(self) -> bool:
        return bool(self.kind)


class _Grid:
    """A mutable character canvas. Out-of-bounds writes are dropped.

    Also the hit map. The grid is the only thing that knows where anything was
    drawn, so it records what each cell belongs to as it paints: a lane row in
    the rail, an agent's pane, a row in the drawer. ``WaveWeb.hit`` reads it
    back, which is what makes the view clickable without the model having to
    predict its own layout.
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = max(0, width)
        self.height = max(0, height)
        self.cells: list[list[tuple[str, str]]] = [
            [(" ", "") for _ in range(self.width)] for _ in range(self.height)
        ]
        #: (x, y) -> (kind, key). Kinds: "lane", "pane", "drawer", "agent".
        self.hits: dict[tuple[int, int], tuple[str, str]] = {}

    def region(self, x: int, y: int, w: int, h: int,
               kind: str, key: str) -> None:
        """Mark every cell of a rectangle as belonging to *kind*/*key*.

        Called before the content is drawn, so a later write over one of these
        cells does not erase the hit — the whole pane stays clickable, not only
        the glyphs that happen to sit on it.
        """
        for j in range(max(0, y), min(self.height, y + h)):
            for i in range(max(0, x), min(self.width, x + w)):
                self.hits[(i, j)] = (kind, key)

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

    def hline(self, x0: int, x1: int, y: int, ch: str = "─", style: str = "") -> None:
        for x in range(min(x0, x1), max(x0, x1) + 1):
            self.put(x, y, ch, style)

    def vline(self, x: int, y0: int, y1: int, ch: str = "│", style: str = "") -> None:
        for y in range(min(y0, y1), max(y0, y1) + 1):
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


@dataclass
class Layout:
    """Where everything goes at one size. Computed once per render.

    Kept as a value rather than recomputed at each draw site so the drawing code
    and the hit map can never disagree about where a pane was: both read this.
    """

    rail_w: int = 0
    drawer_w: int = 0
    pane_w: int = 0
    box_h: int = 0
    cols: int = 0
    rows: int = 0
    panes_x: int = 0
    panes_w: int = 0
    capacity: int = 0
    #: How far the block of panes is inset from ``panes_x``, so a wave that does
    #: not fill the width is centred in it rather than stretched across it.
    inset: int = 0

    @property
    def web(self) -> bool:
        """Whether there is room for the rail-and-panes layout at all."""
        return self.cols >= 1 and self.box_h >= BOX_MIN_H


class WaveWeb:
    """The model behind the wave view: ten agents, a rail, and a drawer.

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
        #: The planner's reasoning while the roster is being written. Bounded
        #: like the recon log — a planner turn can run long and the view only
        #: has room for the tail.
        self.planner: list[tuple[str, str]] = []
        #: What the drawer is showing. Empty means closed.
        self.detail = Detail()
        #: How far the drawer's contents are scrolled, in rows.
        self.detail_scroll = 0
        #: (x, y) of the mouse, for the hover highlight. -1 means no mouse.
        self.hover: tuple[int, int] = (-1, -1)
        #: Rail and drawer widths, as the operator has resized them. None means
        #: "whatever fits", which is what the layout falls back to.
        self.rail_w: int | None = None
        self.drawer_w: int | None = None
        #: Set when the operator has asked the campaign to stop from the wave
        #: view. The wave ends at its next boundary rather than instantly, so
        #: without this the key would look like it did nothing — the transcript
        #: line it writes is on the screen this one is covering.
        self.stopping = False
        #: (x, y) -> (kind, key), rebuilt on every render. See ``_Grid.region``.
        self._hits: dict[tuple[int, int], tuple[str, str]] = {}
        #: The previous frame's hit map, used to resolve the hover — see
        #: ``hovered``.
        self._prev_hits: dict[tuple[int, int], tuple[str, str]] = {}
        #: The layout the last render used, so a click can be interpreted
        #: against the same geometry that was drawn.
        self._layout = Layout()

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
        cell.activity_at = time.monotonic()

    def agent_activity(self, name: str, activity: str) -> None:
        """Record what an agent is doing now — a tool call, usually.

        The call is recorded in ``history`` as it *starts*, so the drawer shows
        what the agent is doing now rather than only what it has finished, and
        ``agent_result`` fills that same entry in when the output arrives so the
        step is not listed twice.
        """
        cell = self._cell(name)
        if cell is None:
            return
        cell.status = "running"
        cell.activity = activity
        cell.command = activity
        cell.command_at = time.monotonic()
        # The previous command's output belongs to the previous command. Left
        # up, a pane would show the new command over the old command's results,
        # which reads as though this call returned them.
        cell.output = []
        cell.activity_at = cell.command_at
        cell.history.append((activity, ""))
        cell.pending = len(cell.history) - 1
        if len(cell.history) > HISTORY_MAX:
            cell.history = cell.history[-HISTORY_MAX:]
            cell.pending = max(-1, cell.pending - 1)

    def agent_result(self, name: str, command: str, output: str) -> None:
        """Record what an agent's last command returned.

        This is the half of a tool call the pane never had: it showed the
        command and then replaced it with the next one, so an agent's actual
        findings — the HTTP response, the directory listing, the error — were
        only legible in the scrolling transcript. Kept bounded, and kept in
        ``history`` as well so the drawer can show the whole run.
        """
        cell = self._cell(name)
        if cell is None:
            return
        lines = [ln.rstrip() for ln in str(output or "").splitlines()]
        # Leading blank lines are noise from a command that printed a banner
        # first; trailing ones are the shell's final newline.
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        cell.output = lines[:OUTPUT_MAX]
        if command:
            cell.command = command
            # A result is the end of a command, so it is drawn whole — the
            # typing animation is for the moment a command *starts*.
            cell.command_at = 0.0
            entry = (command, "\n".join(lines[:OUTPUT_MAX]))
            if 0 <= cell.pending < len(cell.history):
                # Fill in the step this result belongs to, so a call and its
                # output are one entry in the drawer rather than two.
                cell.history[cell.pending] = entry
                cell.pending = -1
            else:
                cell.history.append(entry)
            if len(cell.history) > HISTORY_MAX:
                cell.history = cell.history[-HISTORY_MAX:]
        cell.activity_at = time.monotonic()

    def agent_text(self, name: str, text: str) -> None:
        """Record an agent's own reasoning, shown in its drawer.

        The pane has no room for prose — it is a command and its output — but an
        agent's reasoning is the only account of *why* a command was run, and
        until this existed it was visible only in the transcript.
        """
        cell = self._cell(name)
        text = str(text or "").strip()
        if cell is None or not text:
            return
        cell.history.append(("", text))
        if len(cell.history) > HISTORY_MAX:
            cell.history = cell.history[-HISTORY_MAX:]
        cell.activity_at = time.monotonic()

    def planner_text(self, text: str) -> None:
        """Append a block of the planner's reasoning to the planning feed."""
        text = str(text or "").strip()
        if text:
            self.planner.append(("text", text))

    def planner_tool(self, tool: str, args: dict | None = None,
                     cfg=None) -> None:
        """Append a planner tool call to the planning feed.

        Summarised with its arguments rather than recorded as a bare tool name:
        "grep" says nothing about what the planner is looking for, and the
        arguments are the only part of the call that explains where the wave is
        being aimed. ``cfg`` is optional so a caller with no config to hand still
        gets a usable summary.
        """
        name = str(tool or "")
        if not name:
            return
        label = name
        if args:
            try:
                from ..tools import summarise_call
                label = summarise_call(name, args, cfg, max_len=200) or name
            except Exception:  # noqa: BLE001
                # The summary is a nicety; a malformed call must not blank the
                # feed, so fall back to the bare tool name.
                label = name
        self.planner.append(("tool", label))

    def agent_done(self, name: str, *, ok: bool = True) -> None:
        cell = self._cell(name)
        if cell is None:
            return
        cell.status = "done" if ok else "failed"
        cell.activity = "finished" if ok else "failed"
        cell.activity_at = time.monotonic()

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

    # ---- interaction ------------------------------------------------------
    def open_findings(self, severity: str) -> None:
        """Open the drawer on every finding on one rung."""
        sev = normalise_severity(severity)
        self.detail = Detail("findings", sev)

    def open_agent(self, name: str) -> None:
        """Open the drawer on one agent's command history."""
        if self._cell(name) is not None:
            self.detail = Detail("agent", name)

    def close_detail(self) -> None:
        """Close the drawer. Returns whether anything was open."""
        self.detail = Detail()
        self.detail_scroll = 0

    def scroll_detail(self, delta: int) -> None:
        """Scroll the drawer's contents by *delta* rows.

        The drawer holds a whole agent run, which can be longer than the screen,
        and there is no scroll widget under it to do this — the drawer is drawn
        into the same character grid as everything else. Clamped at zero and at
        the end of the content so a wheel that keeps turning does not scroll the
        list into blank space.
        """
        self.detail_scroll = max(0, min(self.detail_max_scroll(), 
                                        self.detail_scroll + delta))

    def detail_max_scroll(self) -> int:
        """How far the drawer's contents can scroll before running out."""
        if self.detail.kind == "agent":
            cell = self._cell(self.detail.key)
            if cell is None:
                return 0
            # Two rows of header, then each step is its command plus its output.
            used = 4
            for cmd, out in cell.history:
                used += max(1, len(_wrap(cmd, 40)) if cmd else 1)
                used += len([ln for ln in str(out or "").splitlines() if ln.strip()])
            return max(0, used - 10)
        return 0

    def click(self, x: int, y: int) -> tuple[str, str] | None:
        """Act on a click at *(x, y)* and return what was hit.

        The one place that decides what a click means, so the screen does not
        have to know which regions exist. ``render`` must have run at the same
        size first — the hit map is a by-product of drawing, and there is no
        other honest way to know where a pane ended up.
        """
        found = self.hit(x, y)
        if found is None:
            # A click on bare canvas closes the drawer, which is the only
            # dismiss gesture available on a screen that must not take focus.
            self.close_detail()
            return None
        kind, key = found
        if kind == "lane":
            self.open_findings(key)
        elif kind == "pane":
            self.open_agent(key)
        return found

    def hovered(self) -> tuple[str, str] | None:
        """What the pointer is over, resolved against the last completed frame.

        See ``_render_panes`` for why the previous frame's map is the correct
        one to ask.
        """
        if self.hover[0] < 0:
            return None
        return self._prev_hits.get(self.hover)

    def hit(self, x: int, y: int) -> tuple[str, str] | None:
        """What sits at *(x, y)* in the last render: ``(kind, key)`` or None.

        The kinds are ``"lane"`` (a severity lane in the rail, keyed by
        severity), ``"pane"`` (an agent, keyed by name), ``"agent"`` (an agent
        row in the drawer, keyed by name) and ``"drawer"`` (the drawer body,
        keyed by ``"close"``).
        """
        return self._hits.get((x, y))

    def resize_rail(self, delta: int) -> None:
        """Widen or narrow the rail by *delta* columns, within its bounds."""
        # Grows from the width actually in use, not from the stored preference:
        # an unset preference is 0, and ``0 + delta`` clamps straight to the
        # minimum, so the first press of "wider" would snap the rail to its
        # narrowest setting instead of widening it.
        base = self.rail_w or self._layout.rail_w or RAIL_W
        self.rail_w = max(RAIL_MIN_W, min(RAIL_MAX_W, base + delta))

    def resize_drawer(self, delta: int) -> None:
        """Widen or narrow the drawer by *delta* columns, within its bounds.

        The drawer is only ever open while there is something in it, so a resize
        that arrives with it closed is remembered rather than applied — the
        layout gives the drawer no width until a detail is open.
        """
        base = self.drawer_w or self._layout.drawer_w or DRAWER_W
        self.drawer_w = max(DRAWER_MIN_W, min(DRAWER_MAX_W, base + delta))

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

    def on_rung(self, severity: str) -> list[dict]:
        """Every finding on one rung, worst-first within it by arrival.

        Newest first, because the drawer is opened to read the finding that just
        landed, and the older ones on the same rung are context.
        """
        sev = normalise_severity(severity)
        return [f for f in reversed(self.findings)
                if normalise_severity(f.get("severity")) == sev]

    def visible_agents(self, limit: int) -> list[AgentCell]:
        """The agents whose panes to draw, busiest first.

        Ten panes of the size that shows a command and its output do not fit on
        every terminal, so the view shows a window of them. The window is chosen
        rather than truncated: an agent that has reported something, or is
        running, is worth more of the screen than one that has not started, and
        sorting by "most recently active" is what keeps the operator looking at
        the work in progress.

        The tail is rotated rather than dropped, so an agent that has been quiet
        still comes round. Without that a wave where four agents do all the work
        would never show the other six again, and a stalled agent would be
        invisible — which is exactly the fault this view exists to expose.

        The rotation is keyed off the clock, not off a call counter: ``render``
        runs on a timer several times a second, and a counter would cycle the
        window so fast the panes flickered.

        A pane that is open in the drawer is never rotated out — the drawer
        describes the pane beside it.
        """
        if limit <= 0:
            return []
        if len(self.agents) <= limit:
            return list(self.agents)

        pinned = [c for c in self.agents if self.detail.kind == "agent"
                  and c.name == self.detail.key]
        rest = [c for c in self.agents if c not in pinned]
        active = sorted(
            (c for c in rest if c.status != "idle" or c.findings),
            key=lambda c: c.activity_at, reverse=True,
        )
        idle = [c for c in rest if c not in active]

        chosen = pinned + active[: max(0, limit - len(pinned))]
        room = limit - len(chosen)
        if room > 0 and idle:
            # Rotate through the quiet ones so each gets a turn on screen.
            start = int(time.monotonic() / ROTATE_SECONDS) % len(idle)
            spun = idle[start:] + idle[:start]
            chosen += spun[:room]
        # Drawn in the original roster order, so a pane does not jump between
        # cells from one repaint to the next.
        order = {c.name: i for i, c in enumerate(self.agents)}
        return sorted(chosen, key=lambda c: order[c.name])

    # ---- layout -----------------------------------------------------------
    def layout(self, width: int, height: int) -> Layout:
        """Where everything goes at *width* x *height*.

        Panes are sized from the space that is left after the rail and the
        drawer, and the count is derived from the size rather than the other way
        round. The previous version asked for a fixed six-row pane and then
        computed how many fit — which returned six at 118x36 while only four
        could be drawn, so two panes were laid out off the bottom of the canvas
        and silently dropped, taking their hit regions with them. Nothing is
        ever laid out off-canvas here: the grid is divided, and the division is
        what gets drawn.
        """
        lay = Layout()
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            return lay

        lay.rail_w = max(RAIL_MIN_W, min(RAIL_MAX_W,
                                        self.rail_w if self.rail_w is not None
                                        else RAIL_W))
        # The drawer only opens when it can pay for itself. It has to leave two
        # columns of panes behind it: at one column the wave stops reading as a
        # wave and the operator is looking at a list beside a panel, which is
        # what the drawer was supposed to save them from. So its width is
        # computed from what is left, not requested and then hoped for — a
        # terminal too narrow to hold both gets no drawer, rather than panes
        # squashed to nothing.
        if self.detail:
            want = max(DRAWER_MIN_W, min(DRAWER_MAX_W,
                                         self.drawer_w if self.drawer_w is not None
                                         else DRAWER_W))
            # Exactly what two minimum-width columns and the margins around them
            # need: the pane width is a floor division, so two columns occupy
            # ``2 * PANE_MIN_W + 1`` of pane space, and the block is inset by one
            # column from the rail and from the drawer. Reserving one column too
            # few is the difference between a drawer at 118 columns leaving two
            # columns of panes and leaving one.
            room = (width - lay.rail_w) - (2 * PANE_MIN_W + 1) - 2
            if room >= DRAWER_MIN_W:
                lay.drawer_w = min(want, room)

        lay.panes_x = lay.rail_w + 1
        lay.panes_w = width - lay.panes_x - (lay.drawer_w + 1 if lay.drawer_w else 1)
        if lay.panes_w < PANE_MIN_W:
            return Layout(rail_w=lay.rail_w)

        # Columns. The choice is made to get every agent on screen at a height
        # that can show its output, and only then to make the panes wide: at
        # 118 columns a two-column layout gives 40-column panes but only five
        # rows tall, which is one row of output — so the layout takes three
        # columns and 26x7 instead, and all ten agents are readable at once.
        cols = self._choose_columns(lay.panes_w, height,
                                    at_least=2 if lay.drawer_w else 1)
        if cols < 1:
            return Layout(rail_w=lay.rail_w)
        lay.cols = cols
        lay.pane_w = min(PANE_MAX_W, (lay.panes_w - (lay.cols - 1)) // lay.cols)

        # Leftover width is not spent on making panes wider than they are
        # useful — see ``PANE_MAX_W``. It goes into the margins, so the block
        # stays centred under the rail's eye line.
        block = lay.cols * lay.pane_w + (lay.cols - 1)
        lay.inset = max(0, (lay.panes_w - block) // 2)
        lay.panes_x += lay.inset

        # Rows: enough to hold every agent, and never more rows than there is
        # height for. Both numbers come out of the same division, so a pane
        # cannot be laid out past the bottom of the canvas — see the note above.
        rows_needed = max(1, -(-(self.size or 1) // lay.cols))       # ceil
        lay.box_h = min(BOX_MAX_H, max(BOX_MIN_H, (height - 2) // rows_needed - 1))
        lay.rows = max(1, (height - 2) // (lay.box_h + 1))
        # Capacity is what the height can actually hold, not what the division
        # hoped for. The two differ whenever ``box_h`` was floored at
        # ``BOX_MIN_H``: three columns needing four rows each is twelve slots on
        # paper, but at 36 rows only three rows of panes fit, so the view has to
        # say nine rather than claim twelve and draw six. It is also capped at
        # the number of agents, because a slot no agent will ever occupy is not
        # a slot the window can show.
        rows_that_fit = max(1, (height - 2 + 1) // (lay.box_h + 1))
        slots = lay.cols * min(lay.rows, rows_that_fit)
        lay.capacity = min(slots, max(1, self.size or 1))
        return lay

    def _choose_columns(self, panes_w: int, height: int,
                        *, at_least: int = 1) -> int:
        """How many panes across, given the room and how many agents there are.

        Scored rather than derived, because "how many fit" and "how many are
        worth drawing" are different questions. A candidate is only considered
        if its panes clear ``PANE_MIN_W``, and the ladder is: readable output
        first, every agent on screen second, room for the output third, and more
        panes across last. Ranking columns above width would split a four-agent
        wave into three narrow columns with a column of the terminal going
        spare; ranking width above readability would give ten agents 40-column
        panes five rows tall, which is one row of output each.

        ``at_least`` forces a floor on the count. It is how the drawer asks for
        its space without flattening the wave into one column: with a drawer
        open the widest pane that fits is often a single 44-column one, and a
        single column beside a panel does not read as a wave at all. The floor
        is applied by discarding candidates below it rather than by scoring
        them down, because "wide" outranks "many" in the ladder below and one
        44-column pane would otherwise always beat two 22-column ones.
        """
        n = max(1, self.size or 1)
        # The most columns that could ever clear ``PANE_MIN_W``, by the same
        # arithmetic the loop below uses. A simpler ``panes_w // (PANE_MIN_W + 1)``
        # is off by one here — it counts the separator between two panes as a
        # whole extra pane — which capped the drawer's two-column request at one
        # column at 118 wide.
        ceiling = max(1, (panes_w + 1) // (PANE_MIN_W + 1))
        floor = max(1, min(at_least, ceiling))

        cands = []
        for cols in range(1, min(n, ceiling) + 1):
            # Capped the same way ``layout`` caps it, so the two cannot disagree
            # about how wide a pane would actually be: scoring the uncapped
            # width here would have this choose one 44-column column while the
            # layout drew two 22-column ones.
            pane_w = min(PANE_MAX_W, (panes_w - (cols - 1)) // cols)
            if pane_w < PANE_MIN_W:
                continue
            rows_needed = max(1, -(-n // cols))
            box_h = min(BOX_MAX_H, max(BOX_MIN_H, (height - 2) // rows_needed - 1))
            fits = box_h * rows_needed + (rows_needed - 1) <= height - 2
            shown = min(n, cols * max(1, (height - 2) // (box_h + 1)))
            cands.append((cols, pane_w, box_h, fits, shown))
        if not cands:
            return 0
        at_floor = [c for c in cands if c[0] >= floor]
        pool = at_floor or cands

        best = None
        for cols, pane_w, box_h, fits, shown in pool:
            score = (
                # Readability of the output, which is what the pane is for.
                1 if (fits and box_h >= BOX_GOOD_H) else 0,
                1 if fits else 0,
                # Then the room left over for it, counted only up to the width
                # a pane can actually use: a 60-column pane with a 26-column
                # command in it is a command and a hole.
                min(pane_w, PANE_PREF_W) if fits else 0,
                # Ties go to the layout that shows more agents. Once the panes
                # are as wide as they are useful the extra width is worth
                # nothing, so splitting it into another column is strictly
                # better — and this has to run the other way round from the
                # width term above, or a two-column layout that fits just as
                # well as a three-column one loses for being wider.
                cols if fits else 0,
                # Nothing fits at a readable height: show as many as possible.
                shown,
            )
            if best is None or score > best[0]:
                best = (score, cols)
        return best[1] if best else 0

    def _grid_pos(self, index: int, lay: Layout, height: int) -> tuple[int, int]:
        """The top-left cell of the *index*-th pane, in the computed layout.

        Laid out row-major, and the block is centred vertically in whatever rows
        are available so a short wave does not cling to the top of the screen.
        """
        col = index % lay.cols
        row = index // lay.cols
        x = lay.panes_x + col * (lay.pane_w + 1)
        used_rows = max(1, -(-min(self.size or 1, lay.capacity) // lay.cols))
        block = used_rows * (lay.box_h + 1) - 1
        top = max(1, (height - block) // 2)
        y = top + row * (lay.box_h + 1)
        return x, y

    # ---- render -----------------------------------------------------------
    def render(self, width: int, height: int) -> Text:
        """Draw the wave. Degrades to a list when there is no room for panes."""
        lay = self.layout(width, height)
        self._layout = lay
        if not lay.web:
            return self._render_compact(width, height)
        return self._render_panes(width, height, lay)

    @property
    def planning(self) -> bool:
        """Whether the wave is still being planned — no roster has arrived.

        The screen is pushed at ``hunt_wave_start``, but the roster only lands
        with ``hunt_plan``, which is emitted after the planner's turn returns.
        That turn is a real model call over recon, findings, wave history and a
        long instruction, so it runs for tens of seconds — and for all of it the
        view held zero agents and drew "WAVE 1 · 0 agents" under a header
        claiming a wave. That is not a cosmetic gap: it reads as a wave that
        started with nobody in it, which is exactly the fault this class was
        built to make visible.
        """
        return not self.agents and not self.findings

    # -- the panes ----------------------------------------------------------
    def _render_panes(self, width: int, height: int, lay: Layout) -> Text:
        p = self.palette
        g = _Grid(width, height)
        now = time.monotonic()
        # The hit map is a by-product of drawing, so at this instant it is still
        # the *previous* frame's. That is the right map to resolve the hover
        # against: the thing under the pointer is wherever the last frame drew
        # it, and the panes are laid out identically from one frame to the next
        # unless the terminal was resized — in which case the pointer is over
        # something else anyway. Reading the new (empty) map instead meant no
        # hover ever resolved and nothing ever highlighted.
        self._prev_hits = self._hits
        self._hits = g.hits

        self._draw_rail(g, lay, height, p, now)

        if self.planning:
            # No roster yet, so there are no panes to draw and the planner's
            # reasoning is the only thing there is to show. It gets the whole
            # width the panes would have had.
            self._draw_planning(g, lay.panes_x, lay.panes_w, height, p, now)
        else:
            shown = self.visible_agents(lay.capacity)
            drawn: list[AgentCell] = []
            for i, cell in enumerate(shown):
                x, y = self._grid_pos(i, lay, height)
                # A pane that would run off the bottom is not drawn at all: a
                # half-drawn box reads as a rendering fault, and its hit region
                # would be a lie about what is clickable. ``capacity`` is meant
                # to make this unreachable, so it is a backstop rather than the
                # mechanism — see the note in ``layout``.
                if y + lay.box_h > height - 1:
                    break
                self._draw_agent(g, cell, x, y, lay, p, now)
                drawn.append(cell)
            hidden = len(self.agents) - len(drawn)
            if hidden > 0:
                self._draw_hidden(g, lay, height, hidden, drawn, p)

        if lay.drawer_w:
            self._draw_drawer(g, lay, height, p, now)
        return g.to_text()

    # -- the tracker rail ---------------------------------------------------
    def _draw_rail(self, g: _Grid, lay: Layout, height: int,
                   p: Palette, now: float) -> None:
        """The tracker: liveness, the severity lanes, and the feed.

        A rail rather than a centre node. The lanes are buttons — their whole
        row is marked clickable, so the operator goes from "MED 1" to the finding
        behind it — and the rail is the one region on screen that never rotates,
        which makes it the right place to put the controls.
        """
        x, w = 1, lay.rail_w
        worst = self.worst()
        border = severity_style(p, worst) if worst else p.accent
        g.region(x, 0, w, height, "rail", "")
        g.box(x, 0, w, height, "ZIM-TRACK", style=f"bold {border}", heavy=True)

        secs = self.elapsed()
        head = f"● LIVE  {secs // 60}:{secs % 60:02d}"
        if self.ended:
            head = f"● CLOSED  {secs // 60}:{secs % 60:02d}"
        if self.planning:
            head = f"{spinner_at(now)} PLANNING  {secs // 60}:{secs % 60:02d}"
        g.text(x + 2, 1, _clip(head, w - 4), f"bold {p.accent}")

        total = sum(self.counts().values())
        g.text(x + 2, 2,
               _clip(f"{total} finding{'s' if total != 1 else ''}", w - 4),
               p.primary)
        g.hline(x + 2, x + w - 3, 3, "─", p.dim)

        # The lanes. The bar is scaled to the *lane* count, not to the busiest
        # lane: normalising across lanes made a single LOW draw the same length
        # as a single CRIT, which is the one comparison the bar exists to make.
        # A fixed width also means a lane only changes when its own count does.
        counts = self.counts()
        bar_w = max(0, w - 16)
        span = max(1, max((counts.get(s, 0) for s in LANES), default=0))
        hovered = self.hovered()
        rule = 4 + len(LANES)
        for i, sev in enumerate(LANES):
            row = 4 + i
            n = counts.get(sev, 0)
            if n:
                g.region(x + 2, row, w - 4, 1, "lane", sev)
            sel = self.detail.kind == "findings" and self.detail.key == sev
            on = hovered == ("lane", sev) or sel
            g.text(x + 2, row, f"{SEVERITY_LABEL[sev]:<5}", severity_style(p, sev))
            g.text(x + 8, row, f"{n:>2}",
                   f"bold {severity_style(p, sev)}" if n else p.dim)
            if n:
                filled = max(1, int(round(bar_w * (n / span))))
                g.text(x + 11, row, "█" * filled, severity_style(p, sev))
                g.text(x + 11 + filled, row, " ▸" if not sel else " ◂",
                       f"bold {p.accent}")
            if on:
                # The hover/selection highlight, drawn last so the lane's own
                # content is not written over. A marker in the gutter rather
                # than a background colour: no palette here defines a surface
                # colour to fill with, and the rail is only 34 columns wide, so
                # a rule on a row of its own would either overwrite the lane
                # below or push the lanes down a row.
                g.text(x + 1, row, "▸", f"bold {severity_style(p, sev)}")

        g.hline(x + 2, x + w - 3, rule, "─", p.dim)
        # The feed. Newest first: who reported what, then the asset under it.
        # No blank row between entries — at three entries that is two rows of
        # padding spent on nothing, and the severity colour already separates
        # them.
        feed = self.recent(FEED_LINES)
        row = rule + 1
        if not feed:
            g.text(x + 2, row, "nothing confirmed yet", p.dim)
            row += 1
        text_w = max(4, w - 6)
        for f in feed:
            # Each entry is two rows, and the footer below is now four — rule,
            # three hints — so the last row an entry may claim is height - 6.
            if row + 1 > height - 6:
                break
            sev = normalise_severity(f.get("severity"))
            g.text(x + 2, row, f"{_clip(str(f.get('agent', '?')), text_w)}", p.dim)
            g.text(x + 2, row + 1,
                   _clip(str(f.get("title", "")), text_w),
                   f"bold {severity_style(p, sev)}")
            row += 2

        # The controls, pinned to the bottom of the rail so they do not move
        # when the feed grows. Three lines rather than two since the stop key
        # was added: the prompt underneath is covered by this screen, so a key
        # that ends the campaign is only usable if it is written down here.
        hint = height - 4
        g.hline(x + 2, x + w - 3, hint - 1, "─", p.dim)
        g.text(x + 2, hint, "click lane ▸ findings", p.dim)
        g.text(x + 2, hint + 1, "click pane ▸ agent run", p.dim)
        if self.stopping:
            # Confirmation, because the /stop-hunt line the app writes goes to
            # the transcript this screen is covering. Without it the key looks
            # inert until the wave actually ends.
            g.text(x + 2, hint + 2, _clip("stopping — wave ends", w - 4),
                   f"bold {p.amber}")
        else:
            g.text(x + 2, hint + 2, "ctrl+x  stop hunt", p.dim)

    def _draw_planning(self, g: _Grid, x: int, w: int, height: int,
                       p: Palette, now: float) -> None:
        """What fills the screen while the roster is being written.

        The planner's turn is a real model call over the whole recon, every
        finding so far and the wave history — tens of seconds during which this
        view had no roster to draw and showed the operator nothing at all. The
        planner's own reasoning and its real tool calls fill that gap, because
        they are the account of *why* the wave is aimed where it is, and they
        are already being streamed by the time they arrive here.
        """
        if w < 20:
            return
        g.box(x, 1, w, height - 2, "PLANNING", style=f"bold {p.accent}")

        # The header line: a sweep, so a planner that is thinking without
        # emitting anything still reads as alive rather than as a hang.
        sweep_w = max(4, w - 24)
        pos = int((now % 2.0) / 2.0 * sweep_w)
        g.text(x + 2, 2, f"{spinner_at(now)}", f"bold {p.accent}")
        g.text(x + 4, 2, f"{len(self.planner)} block(s)", p.primary)
        for i in range(sweep_w):
            ch = "━" if i == pos else "┈"
            g.put(x + 18 + i, 2, ch, f"bold {p.accent}" if i == pos else p.dim)

        top, bottom = 4, height - 4
        if not self.planner:
            g.text(x + 2, top, "the planner is reading recon and the findings "
                               "so far…", p.dim)
            g.text(x + 2, top + 2, "its reasoning appears here as it writes it",
                   p.dim)
            g.text(x + 2, top + 3, "— the roster lands when the turn returns",
                   p.dim)
            return
        self._draw_planner(g, w, top, bottom, p, x0=x + 2)

    def _draw_hidden(self, g: _Grid, lay: Layout, height: int,
                     hidden: int, shown: list[AgentCell], p: Palette) -> None:
        """Name the agents the window is not showing.

        A pane that simply is not there reads as a wave that lost a worker;
        "+4 not shown" reads as a view that is windowing, which is what it is.
        """
        on_screen = {c.name for c in shown}
        names = [c.name for c in self.agents if c.name not in on_screen]
        y = height - 2
        g.text(lay.panes_x, y,
               _clip(f"+{hidden} not shown: {', '.join(names)}", lay.panes_w),
               p.dim)

    # -- one agent ----------------------------------------------------------
    def _draw_agent(self, g: _Grid, cell: AgentCell, x: int, y: int,
                    lay: Layout, p: Palette, now: float) -> None:
        lit = cell.lit(now)
        selected = self.detail.kind == "agent" and self.detail.key == cell.name
        hovered = self.hovered() == ("pane", cell.name)
        if selected:
            border = f"bold {p.accent}"
        elif hovered:
            # Bold, not merely accent: a running pane's border is already
            # accent, so hovering one would otherwise change nothing and the
            # pane would look inert exactly when the pointer says it is live.
            border = f"bold {p.accent}"
        elif lit and cell.worst:
            border = f"bold {severity_style(p, cell.worst)}"
        elif cell.status == "failed":
            border = p.red
        elif cell.status == "done":
            border = p.dim
        elif cell.status == "running":
            border = p.accent
        else:
            border = p.dim

        # Marked before it is drawn, so the whole pane is clickable — not only
        # the glyphs that happen to land on it.
        g.region(x, y, lay.pane_w, lay.box_h, "pane", cell.name)

        # The marker rides in the title; the count is right-aligned in the same
        # border. A running agent spins, so a pane whose command and output have
        # not changed for a while is still visibly working.
        if selected:
            mark = "▶"
        elif cell.findings:
            mark = "◆"
        elif cell.status == "running":
            mark = spinner_at(now)
        else:
            mark = "·"
        title = f"{mark} {cell.name}"
        if len(cell.history) > 1:
            title = f"{title} ·{len(cell.history)}"
        g.box(x, y, lay.pane_w, lay.box_h, title, style=border)
        if cell.findings:
            g.text(x + lay.pane_w - 4, y, f"×{cell.findings}",
                   f"bold {severity_style(p, cell.worst or 'info')}")

        inner = max(0, lay.pane_w - 4)
        # Row 1: the command, in the agent's own colour, typed in as it starts.
        cmd_style = (severity_style(p, cell.worst) if (lit and cell.worst)
                     else (f"bold {p.primary}" if cell.status == "running"
                           else p.primary))
        command = cell.revealed(now) or cell.activity
        g.text(x + 2, y + 1, _clip(command, inner), cmd_style)

        # The middle rows: what the command returned, dim — reference rather
        # than news. The last row carries a `>N` when there is more output than
        # fits, so a clipped listing does not read as a short one.
        tail = y + lay.box_h - 2
        avail = max(0, tail - (y + 2))
        rows = cell.output[:avail]
        more = len(cell.output) - len(rows)
        for i in range(avail):
            row = y + 2 + i
            if i < len(rows):
                text = rows[i]
                if i == len(rows) - 1 and more > 0:
                    text = f"{text} >{more}"
                g.text(x + 2, row, _clip(text, inner), p.dim)
            elif i == 0 and not rows and cell.status == "running":
                g.text(x + 2, row, "…", p.dim)

        # The tail: what this agent has to show for itself. A finding leads —
        # that is the news — and the command count follows, so the pane says
        # both what was found and how much was run to find it.
        if lit and cell.last:
            g.text(x + 2, tail, f"◆ {_clip(cell.last, inner - 2)}",
                   f"bold {severity_style(p, cell.worst or 'info')}")
        elif cell.findings:
            g.text(x + 2, tail,
                   f"{cell.findings} finding{'s' if cell.findings != 1 else ''}",
                   f"bold {severity_style(p, cell.worst or 'info')}")
        elif cell.status == "failed":
            g.text(x + 2, tail, "failed", p.red)
        elif len(cell.history) > 1:
            g.text(x + 2, tail, f"+{len(cell.history) - 1} earlier step(s)",
                   p.dim)
        elif cell.status == "done":
            g.text(x + 2, tail, "finished", p.dim)

        # The pulse: a finding travelling from the pane that reported it to the
        # rail that tallied it. The one animation here that points at where the
        # news came from.
        if cell.pulse(now):
            age = (now - cell.flash) / PULSE_SECONDS
            col = x + int(lay.pane_w * (1 - age))
            g.put(max(0, min(g.width - 1, col)), y, "◆",
                  f"bold {severity_style(p, cell.worst or 'info')}")

    # -- the drawer ---------------------------------------------------------
    def _draw_drawer(self, g: _Grid, lay: Layout, height: int,
                     p: Palette, now: float) -> None:
        """The detail behind a lane or a pane, on the right.

        Opened by a click, closed by a click on bare canvas or ``escape``. It
        takes width rather than covering anything: the panes are laid out around
        it, so reading a finding never hides the agent that is still working.
        """
        x = g.width - lay.drawer_w - 1
        w = lay.drawer_w
        if self.detail.kind == "findings":
            title = f"{SEVERITY_LABEL.get(self.detail.key, self.detail.key)} findings"
            rows = self.on_rung(self.detail.key)
        else:
            title = f"agent {self.detail.key}"
            rows = []
        g.region(x, 0, w, height, "drawer", "close")
        g.box(x, 0, w, height, title, style=f"bold {p.accent}", heavy=True)
        # The dismiss affordance, inside the top border rather than over it —
        # a click anywhere in the drawer also closes it, so this is a label
        # rather than the only way out.
        if w >= 12:
            g.text(x + w - 5, 0, " × ", f"bold {p.dim}")

        if self.detail.kind == "findings":
            self._draw_findings(g, x, w, height, rows, p)
        else:
            self._draw_agent_run(g, x, w, height, p)

    def _draw_findings(self, g: _Grid, x: int, w: int, height: int,
                       rows: list[dict], p: Palette) -> None:
        """One severity rung, every finding on it, with the evidence.

        This is the answer to "MED 1 — but *what*": the title, the asset it was
        found on, the agent and wave, and the evidence that makes it a finding
        rather than an opinion.
        """
        if not rows:
            g.text(x + 2, 2, "nothing on this rung", p.dim)
            return
        y = 2
        # The body is indented one column past the list number, not five: the
        # drawer is narrow by design (it exists to leave the panes their width),
        # so every column it spends on indentation is a column off the asset URL
        # — and a URL broken across three lines is not readable as a URL.
        text_w = max(4, w - 5)
        for i, f in enumerate(rows):
            if y > height - 4:
                g.text(x + 2, height - 3, f"+{len(rows) - i} more", p.dim)
                break
            sev = normalise_severity(f.get("severity"))
            g.text(x + 2, y, f"{i + 1:>2} ", p.dim)
            for ln in _wrap(str(f.get("title") or "(untitled)"), text_w)[:2]:
                g.text(x + 5, y, ln,
                       f"bold {severity_style(p, sev)}")
                y += 1
            asset = str(f.get("asset") or "").strip()
            if asset:
                for ln in _wrap_asset(asset, text_w)[:2]:
                    g.text(x + 5, y, ln, p.primary)
                    y += 1
            who = f"agent {f.get('agent', '?')} · wave {f.get('wave', '?')}"
            g.text(x + 5, y, _clip(who, text_w), p.dim)
            y += 1
            for label, value in (("why", f.get("summary")),
                                 ("proof", f.get("evidence")),
                                 ("fix", f.get("remediation"))):
                value = str(value or "").strip()
                if not value:
                    continue
                for j, ln in enumerate(_wrap(value, text_w - 7)[:4]):
                    if y > height - 4:
                        break
                    g.text(x + 5, y, f"{label:<5} " if j == 0 else "      ",
                           p.dim)
                    g.text(x + 11, y, ln, p.dim if label != "proof" else p.primary)
                    y += 1
            y += 1

    def _draw_agent_run(self, g: _Grid, x: int, w: int, height: int,
                        p: Palette) -> None:
        """One agent's whole run: every command it ran, and what came back.

        The pane shows the agent's last command and a few lines of its output.
        This is the rest of it — the commands that led there, and the responses
        the pane had no room for. It is the view that answers "what has this
        agent actually been doing", which the scrolling transcript answers only
        by being read backwards.
        """
        cell = self._cell(self.detail.key)
        if cell is None:
            g.text(x + 2, 2, "no such agent", p.dim)
            return
        text_w = max(4, w - 4)
        y = 2
        head = (f"{cell.status} · {cell.findings} finding(s) · "
                f"{len(cell.history)} step(s)")
        g.text(x + 2, y, _clip(head, text_w), p.primary)
        y += 1
        if cell.brief:
            for ln in _wrap(cell.brief, text_w - 2)[:3]:
                g.text(x + 2, y, ln, p.dim)
                y += 1
        g.hline(x + 2, x + w - 3, y, "─", p.dim)
        y += 1
        if not cell.history:
            g.text(x + 2, y, "nothing run yet", p.dim)
            return

        # Oldest first: a run reads as a sequence, and the newest step is
        # already on the pane the operator clicked. ``detail_scroll`` skips
        # whole steps from the top rather than lines, so a scrolled view never
        # starts in the middle of a command.
        steps = cell.history[self.detail_scroll:]
        if self.detail_scroll:
            g.text(x + 2, y, f"↑ {self.detail_scroll} step(s) above", p.dim)
            y += 1
        for cmd, out in steps:
            if y > height - 3:
                g.text(x + 2, height - 2, "↓ more below — scroll", p.dim)
                break
            if cmd:
                for ln in _wrap(cmd, text_w - 2)[:3]:
                    g.text(x + 2, y, ln, f"bold {p.primary}")
                    y += 1
            else:
                g.text(x + 2, y, "(reasoning)", p.dim)
                y += 1
            body = [ln for ln in str(out or "").splitlines() if ln.strip()]
            for ln in body[:8]:
                if y > height - 3:
                    break
                g.text(x + 4, y, _clip(ln, text_w - 4), p.dim)
                y += 1
            if len(body) > 8:
                g.text(x + 4, y, f"… +{len(body) - 8} line(s)", p.dim)
                y += 1
            y += 1

    # -- the planning feed --------------------------------------------------
    def _draw_planner(self, g: _Grid, width: int, top: int, bottom: int,
                      p: Palette, *, x0: int = 0) -> None:
        """The planner's reasoning, wrapped into rows ``top``..``bottom``.

        Wrapped rather than clipped: the planner's prose is the only account of
        *why* the wave is aimed where it is, and a line cut at the pane edge is
        not an account of anything. Newest first, so the live edge is at the top
        where the eye already is.

        ``x0`` shifts the whole block right, so the same renderer serves the
        full-width compact view and the panes region.
        """
        label_w = 9
        text_w = max(10, width - label_w - 1)
        row = top
        for kind, text in reversed(self.planner):
            if row >= bottom:
                break
            if kind == "tool":
                style, lines = p.accent, [f"$ {text}"]
            else:
                style, lines = p.primary, _wrap(text, text_w)
            for i, ln in enumerate(lines):
                if row >= bottom:
                    break
                g.region(x0, row, width, 1, "planner", "")
                if i == 0:
                    g.text(x0, row, " planner ", f"bold {p.accent}")
                else:
                    g.text(x0, row, " " * label_w, p.dim)
                g.text(x0 + label_w, row, _clip(ln, text_w), style)
                row += 1
            # A blank row between blocks, so two turns do not run together.
            row += 1

    # -- the fallback -------------------------------------------------------
    def _render_compact(self, width: int, height: int) -> Text:
        """The panes at a size that cannot hold them: a list, in the same order.

        A terminal too small for the geometry still gets every fact — which
        agents are running, the command each is on, what it returned, and the
        tally — laid out as lines instead of panes. Drawing the panes anyway
        would overlap the boxes and produce something unreadable, which is worse
        than not drawing them.

        While the planner is running there is no roster, so this shows the
        planning feed instead — the same reasoning the full view shows, at
        whatever width there is.
        """
        p = self.palette
        g = _Grid(width, height)
        self._hits = g.hits
        row = 0

        def line(s: str, style: str = "") -> None:
            nonlocal row
            if row < height:
                g.text(0, row, _clip(s, width), style)
                row += 1

        counts = self.counts()
        secs = self.elapsed()
        rule_w = max(0, min(width, 60) - 2)
        if self.planning:
            # No roster yet. Say so, rather than counting zero agents under a
            # header that announces a wave — see ``planning``.
            line(f" WAVE {self.wave} · planning · {self.target}",
                 f"bold {p.accent}")
        else:
            line(f" WAVE {self.wave} · {len(self.agents)} agents · {self.target}",
                 f"bold {p.accent}")
        line(f" ZIM-TRACK  {secs // 60}:{secs % 60:02d}  "
             f"{sum(counts.values())} finding(s)", p.primary)
        line(" " + "─" * rule_w, p.dim)

        if self.planning and self.planner:
            # The planner is thinking and there is no roster to list. Show what
            # it is thinking, which is the whole point of this branch. It fills
            # the gap between the header rule and the lanes at the bottom.
            bottom = height - len(LANES) - 1
            self._draw_planner(g, width, row, max(row, bottom), p)
            row = max(row, bottom)
        elif self.planning:
            line(" · planning the wave — the roster lands when the "
                 "planner returns", p.dim)
        else:
            # The roster shares the height with the header, the tally and the
            # feed, so a short terminal cannot list every agent. Whoever is cut
            # is shown as a count rather than dropped: a list that stops at
            # agent-9 under a header reading "10 agents" reads as a wave that
            # lost a worker.
            #
            # The count row is reserved *before* the roster is chosen, not after
            # it is drawn, and only when something is actually hidden. Trimming
            # afterwards and then counting against the untrimmed list
            # under-reported by one: at 40x12 it listed five agents and said
            # "+5 more". Reserving unconditionally over-corrects the other way,
            # spending a row on a count of zero.
            tail = height - len(LANES) - 2
            roster = self.visible_agents(max(0, tail))
            hidden = len(self.agents) - len(roster)
            if hidden > 0 and tail >= 1:
                roster = roster[:max(0, tail - 1)]
                hidden = len(self.agents) - len(roster)
            listed: list[AgentCell] = []
            for cell in roster:
                mark = "◆" if cell.findings else (
                    spinner_at() if cell.status == "running" else "·")
                style = (severity_style(p, cell.worst) if cell.findings
                         else (p.primary if cell.status == "running" else p.dim))
                body = cell.command or cell.activity
                line(f" {mark} {cell.name:<12} "
                     f"{_clip(body, max(0, width - 17))}", style)
                listed.append(cell)
                # One line of output under the command, so the compact view
                # carries the same information as a pane rather than less.
                if cell.output and row < height - len(LANES) - 2:
                    line(f"     {_clip(cell.output[0], max(0, width - 7))}", p.dim)
            hidden = len(self.agents) - len(listed)
            if hidden > 0:
                line(f" · +{hidden} more", p.dim)
            if not listed:
                line(" · no agents", p.dim)

        line(" " + "─" * rule_w, p.dim)
        lanes = "  ".join(f"{SEVERITY_LABEL[s]} {counts.get(s, 0)}" for s in LANES)
        line(" " + lanes, p.primary)
        for f in self.recent(2):
            sev = normalise_severity(f.get("severity"))
            line(f" · {f.get('agent', '?')} ─▶ "
                 f"[{SEVERITY_LABEL.get(sev, sev)}] {f.get('title', '')}",
                 severity_style(p, sev))
        return g.to_text()
