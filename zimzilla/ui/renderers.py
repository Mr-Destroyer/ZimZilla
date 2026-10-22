"""Rich renderables: tool panels, diffs, code, and the banner."""

from __future__ import annotations

import re
from datetime import datetime

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from ..theme import TOOL_ICONS, Palette
from .markdown import markdown_line, markdown_text

# ---------------------------------------------------------------------------
# Language detection for syntax highlighting
# ---------------------------------------------------------------------------
_EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "bash",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".rs": "rust", ".go": "go", ".rb": "ruby", ".php": "php",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".xml": "xml", ".sql": "sql", ".md": "markdown", ".java": "java",
    ".kt": "kotlin", ".swift": "swift", ".lua": "lua", ".pl": "perl",
    ".ini": "ini", ".cfg": "ini", ".conf": "ini", ".dockerfile": "docker",
}


def lang_for_path(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name in {"dockerfile", "makefile", "rakefile"}:
        return "docker" if name == "dockerfile" else "make"
    for ext, lang in _EXT_LANG.items():
        if name.endswith(ext):
            return lang
    return "text"


def detect_lang_for_command(cmd: str) -> str:
    first = cmd.strip().split()[0] if cmd.strip() else ""
    mapping = {
        "python": "python", "python3": "python", "pip": "bash", "pytest": "python",
        "node": "javascript", "npm": "bash", "npx": "bash", "yarn": "bash",
        "cargo": "rust", "go": "go", "git": "bash", "docker": "bash",
        "make": "make", "curl": "bash", "nmap": "bash",
    }
    return mapping.get(first, "bash")


# ---------------------------------------------------------------------------
# Syntax highlight theme for the "console" — keeps everything green-tinted.
# ---------------------------------------------------------------------------
def hacker_syntax(code: str, lang: str, palette: Palette, *, line_numbers: bool = True) -> Syntax:
    theme = _mono_theme(palette)
    return Syntax(
        code,
        lang,
        theme=theme,
        line_numbers=line_numbers,
        word_wrap=True,
        background_color=palette.bg,
    )


def _mono_theme(palette: Palette):
    """A minimal green/amber monochrome pygments theme."""
    from pygments.style import Style as PygStyle
    from pygments.token import (
        Comment, Error, Generic, Keyword, Name, Number, Operator, String, Token,
    )

    p, dim, accent, red, amber = (
        palette.primary, palette.dim, palette.accent, palette.red, palette.amber,
    )

    class Mono(PygStyle):
        background_color = palette.bg
        styles = {
            Token: p,
            Comment: dim,
            Keyword: f"bold {accent}",
            Operator: p,
            Name: p,
            Name.Builtin: accent,
            Name.Function: f"bold {p}",
            Name.Class: f"bold {accent}",
            String: amber,
            Number: accent,
            Generic: p,
            Generic.Deleted: red,
            Generic.Inserted: accent,
            Error: red,
        }

    return Mono


# ---------------------------------------------------------------------------
# Tool panels
# ---------------------------------------------------------------------------
def _header(name: str, args: dict, palette: Palette, status: str = "",
            max_len: int | None = 200, tag: str = "",
            tag_color: str = "") -> Text:
    """``[ TOOL ]  tag  detail  status``.

    ``tag`` is the agent a hunt call belongs to; it sits directly after the tool
    label so the attribution reads first and the command second.
    """
    icon = TOOL_ICONS.get(name, "▸")
    label = name.upper().replace("_", " ")
    t = Text()
    t.append(f"[ {icon} {label} ]", style=f"bold {palette.primary}")
    if tag:
        t.append("  " + tag, style=f"bold {tag_color or palette.accent}")
    detail = _detail(name, args, max_len=max_len)
    if detail:
        t.append("  " + detail, style=palette.accent)
    if status:
        t.append("  " + status, style=palette.dim)
    return t


def _detail(name: str, args: dict, max_len: int | None = 200) -> str:
    """The one-line gist of a tool call.

    ``max_len`` of None means no truncation at all — used by the hunt panels,
    where a long pipeline has to be readable in full rather than cut off at the
    point the interesting part starts.
    """
    if name == "bash":
        cmd = args.get("command", "")
        if not cmd:
            # No command to show — a bare "$" in a title is noise, and the hunt
            # panels put the command in the body and pass empty args here.
            return ""
        if max_len is None or len(cmd) <= max_len:
            return "$ " + cmd
        return "$ " + cmd[: max_len - 1] + "…"
    if name in {"read_file", "write_file", "edit_file", "list_dir"}:
        return str(args.get("path", ""))
    if name == "glob":
        return f"{args.get('pattern','')} @ {args.get('path','.')}"
    if name == "grep":
        return f"/{args.get('pattern','')}/ @ {args.get('path','.')}"
    return ""


def tool_call_panel(name: str, args: dict, palette: Palette) -> Panel:
    """Panel shown when a tool is *requested*."""
    body = Text()
    if name == "bash":
        body.append(_detail("bash", args), style=palette.primary)
    elif name in {"write_file", "edit_file"}:
        body.append(_detail(name, args), style=palette.accent)
    else:
        body.append(_detail(name, args), style=palette.accent)
    return Panel(
        body,
        title=_header(name, args, palette),
        title_align="left",
        border_style=palette.dim,
        padding=(0, 1),
    )


def _tool_body(name: str, args: dict, output: str,
               palette: Palette) -> RenderableType:
    """The body of a finished tool call: bash gets shell highlighting, a read
    gets its file's language, everything else gets the diff/error heuristic."""
    if name == "bash":
        lang = detect_lang_for_command(args.get("command", ""))
        return hacker_syntax(output, lang, palette, line_numbers=False)
    if name == "read_file":
        lang = lang_for_path(args.get("path", ""))
        return hacker_syntax(output, lang, palette, line_numbers=False)
    return render_output_text(output, palette)


def _exit_badge(meta: dict, palette: Palette) -> Text:
    """The right-hand status chip on a bash panel."""
    exit_code = meta.get("exit")
    if meta.get("timeout"):
        return Text(" TIMEOUT ", style=f"bold white on {palette.amber}")
    if exit_code is None:
        return Text(" EXEC ", style=f"bold white on {palette.dim}")
    if exit_code == 0:
        return Text(" EXIT 0 ", style=f"bold black on {palette.accent}")
    return Text(f" EXIT {exit_code} ", style=f"bold white on {palette.red}")


def tool_result_panel(
    name: str,
    args: dict,
    output: str,
    palette: Palette,
    is_error: bool = False,
    meta: dict | None = None,
) -> Panel:
    """Panel shown when a tool *finishes*, with syntax-highlighted output."""
    meta = meta or {}
    border = palette.red if is_error else palette.dim
    body = _tool_body(name, args, output, palette)

    if name == "bash":
        return Panel(
            body,
            title=_header(name, args, palette),
            subtitle=_exit_badge(meta, palette),
            subtitle_align="right",
            title_align="left",
            border_style=border,
            padding=(0, 1),
        )

    return Panel(
        body,
        title=_header(name, args, palette),
        title_align="left",
        border_style=border,
        padding=(0, 1),
    )


def hunt_tool_panel(
    name: str,
    args: dict,
    output: str,
    palette: Palette,
    agent: str = "",
    color: str = "",
    is_error: bool = False,
    meta: dict | None = None,
) -> Panel:
    """One finished tool call from a hunt worker, rendered in full.

    Same treatment a normal turn gives a call — the operator's real view of the
    engagement — with two differences that matter when ten agents run at once:

    * the command is **never truncated**. A recon or exploitation one-liner is
      routinely 200+ characters and the part that says what is being tested is
      at the end of it, not the beginning. It goes in the *body*, not the
      title: a Rich panel title is one line and gets clipped at the border, so
      a long command there is exactly the truncation this exists to remove.
    * the agent's name is in the title. Interleaved, unattributed panels would
      be unreadable, and attributing them is what lets the operator follow one
      thread through the noise.
    """
    meta = meta or {}
    border = palette.red if is_error else palette.dim

    if name == "bash":
        # The full command leads, then a blank line, then the output — so the
        # two are never mistaken for each other. Group rather than one Text:
        # the output half is a Syntax renderable, which cannot be appended to.
        cmd = Text(_detail("bash", args, max_len=None), style=palette.primary)
        body: RenderableType = Group(
            cmd, Text(""), _tool_body(name, args, output, palette)
        )
        return Panel(
            body,
            title=_header(name, {}, palette, tag=agent, tag_color=color),
            subtitle=_exit_badge(meta, palette),
            subtitle_align="right",
            title_align="left",
            border_style=border,
            padding=(0, 1),
        )

    return Panel(
        _tool_body(name, args, output, palette),
        title=_header(name, args, palette, tag=agent, tag_color=color),
        title_align="left",
        border_style=border,
        padding=(0, 1),
    )


# Braille spinner, advanced one frame per rail tick (~0.25s).
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def tool_running_panel(
    name: str,
    args: dict,
    palette: Palette,
    elapsed: float = 0.0,
    frame: int = 0,
    targets: list[str] | None = None,
) -> Panel:
    """The *live* card for a tool that is still running.

    Same shape as :func:`tool_call_panel`, but the border is lit and the title
    carries a spinner and a running clock, so a slow command is visibly in
    flight rather than indistinguishable from a hung one.
    """
    body = Text()
    body.append(_detail(name, args), style=palette.primary if name == "bash" else palette.accent)
    if targets:
        # The hosts the command names, read straight off the command text. This
        # is the same best-effort scan the scope guard uses — shown, not
        # enforced, because the gate is unreachable in the shipped modes.
        body.append("\n")
        body.append("⌖ ", style=palette.dim)
        body.append(", ".join(targets), style=palette.amber)

    spin = _SPINNER[frame % len(_SPINNER)]
    status = f"{spin} {elapsed:.1f}s"
    return Panel(
        body,
        title=_header(name, args, palette, status=status),
        title_align="left",
        border_style=palette.accent,
        padding=(0, 1),
    )


def turn_marker(turn: int, palette: Palette, width: int = 58) -> Text:
    """The spine joint that opens a turn in the transcript."""
    stamp = datetime.now().strftime("%H:%M")
    head = f"├─ turn {turn} ─ {stamp} "
    t = Text()
    t.append(head, style=f"bold {palette.accent}")
    t.append("─" * max(0, width - len(head)), style=palette.dim)
    return t


def render_output_text(output: str, palette: Palette) -> Text:
    """Render plain tool output with heuristic red/green for diffs and errors."""
    t = Text()
    for i, line in enumerate(output.splitlines()):
        if i:
            t.append("\n")
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            t.append(line, style=f"bold {palette.accent}")
        elif line.startswith("+"):
            t.append(line, style=palette.accent)
        elif line.startswith("-"):
            t.append(line, style=palette.red)
        elif re.search(r"\b(error|Error|ERROR|failed|FAILED|Traceback)\b", line):
            t.append(line, style=palette.red)
        elif re.search(r"\b(ok|OK|passed|PASSED|success|done)\b", line):
            t.append(line, style=palette.accent)
        else:
            t.append(line, style=palette.primary)
    return t


def diff_panel(path: str, diff: str, palette: Palette, verb: str = "EDIT") -> Panel:
    """Render a unified diff with green adds / red removes."""
    t = Text()
    added = removed = 0
    for i, line in enumerate(diff.splitlines()):
        if i:
            t.append("\n")
        if line.startswith("+++") or line.startswith("---"):
            t.append(line, style=f"bold {palette.primary}")
        elif line.startswith("@@"):
            t.append(line, style=f"bold {palette.accent}")
        elif line.startswith("+"):
            t.append(line, style=palette.accent)
            added += 1
        elif line.startswith("-"):
            t.append(line, style=palette.red)
            removed += 1
        else:
            t.append(line, style=palette.dim)

    title = Text()
    title.append(f"[ ± {verb} ]", style=f"bold {palette.primary}")
    title.append("  " + path, style=palette.accent)
    subtitle = Text()
    subtitle.append(f"+{added} ", style=palette.accent)
    subtitle.append(f"-{removed}", style=palette.red)

    return Panel(
        t,
        title=title,
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        border_style=palette.dim,
        padding=(0, 1),
    )


def user_prompt_block(text: str, palette: Palette) -> Panel:
    t = Text()
    t.append("❯ ", style=f"bold {palette.accent}")
    t.append(text, style=f"bold {palette.primary}")
    return Panel(t, border_style=palette.primary, padding=(0, 1))


def error_block(message: str, palette: Palette) -> Panel:
    t = Text(message, style=palette.red)
    title = Text("[ ✖ ERROR ]", style=f"bold {palette.red}")
    return Panel(t, title=title, title_align="left", border_style=palette.red, padding=(0, 1))


def warning_block(message: str, palette: Palette) -> Panel:
    t = Text(message, style=palette.amber)
    title = Text("[ ⚠ WARNING ]", style=f"bold {palette.amber}")
    return Panel(t, title=title, title_align="left", border_style=palette.amber, padding=(0, 1))


def blocked_block(message: str, palette: Palette) -> Panel:
    t = Text()
    t.append("SCOPE GUARD — HARD BLOCK\n", style=f"bold {palette.red}")
    t.append(message, style=palette.red)
    title = Text("[ ⛔ BLOCKED ]", style=f"bold white on {palette.red}")
    return Panel(t, title=title, title_align="left", border_style=palette.red, padding=(0, 1))


def banner_renderable(palette: Palette, revealed: int | None = None) -> Text:
    """The ASCII banner; *revealed* limits how many columns are lit."""
    from .banner import banner_lines

    lines = banner_lines()
    t = Text()
    for row_i, line in enumerate(lines):
        for col_i, ch in enumerate(line):
            if revealed is not None and col_i >= revealed:
                t.append(" " if ch != " " else " ")
                continue
            if ch == " ":
                t.append(" ")
            else:
                t.append(ch, style=f"bold {palette.primary}")
        if row_i < len(lines) - 1:
            t.append("\n")
    return t


# ---- model reasoning ------------------------------------------------------
# What the model is thinking, when the endpoint forwards it. Kept deliberately
# quieter than the answer: dim and italic, so a transcript where the model
# reasoned at length still reads as one answer with commentary, not as two
# competing replies. Nothing here is a finding or a tool result — it is the
# model talking to itself, and the styling says so.
#
# `THINK_LIVE_LINES` is the live slot's window. The live view shows the *tail*
# of the reasoning rather than the head, because the interesting part is what it
# is thinking *now* — the same reason a log tail is more useful than a log head.
# The committed block below keeps every line, so nothing is lost by the window.

#: Lines of reasoning the live (still-streaming) slot shows.
THINK_LIVE_LINES = 14


def reasoning_live(text: str, palette: Palette, cursor: str = "") -> Text:
    """The cheap, per-delta form of in-flight reasoning: the last few lines.

    Called once per ``reasoning_delta``, so it does no markdown parse — a
    reasoning stream is prose full of stray symbols (``**``, ``\\(``, ``->``)
    that would only be misread as markup, and the cost of parsing it several
    times a second buys nothing.
    """
    lines = (text or "").splitlines() or [""]
    tail = lines[-THINK_LIVE_LINES:]
    elided = len(lines) - len(tail)
    t = Text()
    t.append("✻ ", style=f"bold {palette.accent}")
    t.append("thinking", style=f"bold {palette.accent}")
    if elided > 0:
        t.append(f"  (+{elided} earlier)", style=palette.dim)
    t.append("\n")
    for i, line in enumerate(tail):
        if i:
            t.append("\n")
        t.append(line, style=f"italic {palette.dim}")
    if cursor:
        t.append(cursor, style=f"bold {palette.accent}")
    return t


def reasoning_panel(text: str, palette: Palette) -> Panel:
    """The committed form: the model's reasoning, whole, in the transcript.

    Every line is kept. The live slot above shows only a window so it cannot
    push the answer off screen mid-stream; this is where the rest of it lives,
    and the transcript scrolls, so the operator can read what was elided.
    """
    t = Text()
    for i, line in enumerate((text or "").splitlines()):
        if i:
            t.append("\n")
        t.append(line, style=f"italic {palette.dim}")
    title = Text()
    title.append("[ ✻ THINKING ]", style=f"bold {palette.accent}")
    subtitle = Text()
    subtitle.append(f"{len((text or '').splitlines())} lines", style=palette.dim)
    return Panel(
        t,
        title=title,
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        border_style=palette.dim,
        padding=(0, 1),
    )


def agent_text(text: str, palette: Palette) -> Text:
    """Assistant prose, markdown resolved.

    This used to handle one construct — inline backticks — and hand everything
    else through as its own source text, so a model's ``**Asset:**`` and
    ``- item`` reached the operator with the markup still on. It now delegates
    to ``markdown_text``, which parses with CommonMark rather than a regex.

    The signature and the return type are unchanged on purpose: six call sites
    hand this function a model's answer and expect a ``Text`` back, and the
    streaming path calls it per delta. See ``ui/markdown.py`` for the two entry
    points and why this one is the cheap one.
    """
    return markdown_text(text, palette)


# ---- team workers ---------------------------------------------------------
# `/team` interleaves several workers into one transcript, so every line they
# write has to say who wrote it. The label carries the worker's own colour and
# the body keeps the normal prose styling, so a worker's text reads exactly
# like the main agent's — it is only the prefix that differs.

def agent_label(name: str, color: str, palette: Palette, width: int = 14) -> Text:
    """The `[name]` tag that opens every worker line."""
    t = Text()
    t.append(f"[{name}]", style=f"bold {color}")
    pad = max(1, width - len(name))
    t.append(" " * pad, style=palette.dim)
    return t


def agent_line(name: str, text: str, color: str, palette: Palette,
               width: int = 14) -> Text:
    """A labelled block of worker prose, one per flush.

    Body text goes through ``markdown_line`` so backticked code reads the same
    as it does anywhere else in the transcript — and, unlike ``agent_text``,
    flattened onto one line. The ``[name]`` tag labels the line that follows
    it, so a worker's markdown must not break that line in two.
    """
    t = agent_label(name, color, palette, width)
    t.append_text(markdown_line(text, palette))
    return t


def agent_event_line(name: str, color: str, verb: str, detail: str,
                     palette: Palette, ok: bool | None = None,
                     width: int = 14) -> Text:
    """A labelled one-liner for a worker's tool call or result.

    ``ok`` colours the verb: None keeps it neutral (a call), True green, False
    red. Used instead of the live tool card, which has only one slot and would
    be overwritten several times a second by concurrent workers.
    """
    if ok is None:
        vstyle = f"bold {color}"
    elif ok:
        vstyle = f"bold {palette.accent}"
    else:
        vstyle = f"bold {palette.red}"

    t = agent_label(name, color, palette, width)
    t.append(f"{verb:<9}", style=vstyle)
    t.append(detail, style=palette.dim if ok is None else palette.primary)
    return t


# ---- hunt findings ---------------------------------------------------------
# The findings panel and the zim-pane's summary both show the same thing, so the
# severity colour lives here once. Two copies of this map is how a CRITICAL ends
# up red in the panel and amber in the rail.

#: Severity -> the palette attribute that carries it. `info` and anything
#: unrecognised fall through to `dim` in severity_style.
SEVERITY_PALETTE = {
    "critical": "red",
    "high": "amber",
    "medium": "primary",
    "low": "primary",
    "info": "dim",
}

#: The display order, worst first.
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


def severity_style(severity: str, palette: Palette, *, bold: bool = False) -> str:
    """The Rich style for one severity, from the palette.

    Unrecognised severities read as `info` rather than raising: a model that
    invents a sixth word should produce a dim line, not break the pane.
    """
    attr = SEVERITY_PALETTE.get(str(severity or "").strip().lower(), "dim")
    colour = getattr(palette, attr, palette.dim)
    return f"bold {colour}" if bold else colour


def finding_line(f: dict, palette: Palette, *, width: int = 9) -> Text:
    """One finding as a single line: severity, title, asset, wave.

    ``f`` is a finding dict as the hunt events carry it — the panel and the
    transcript both build from that, so neither needs the Finding dataclass.
    """
    sev = str(f.get("severity") or "info").strip().lower()
    t = Text()
    t.append(f"{sev.upper():<{width}}", style=severity_style(sev, palette, bold=True))
    t.append(str(f.get("title") or "(untitled)"), style=f"bold {palette.primary}")
    asset = str(f.get("asset") or "").strip()
    if asset:
        t.append("  ·  ", style=palette.dim)
        t.append(asset, style=palette.primary)
    wave = f.get("wave")
    if wave:
        t.append(f"  wave {wave}", style=palette.dim)
    return t


def finding_detail(f: dict, palette: Palette, *, indent: int = 2) -> Text:
    """The evidence and summary under a finding line, indented and dim.

    Evidence leads: it is the part that makes a finding a finding rather than an
    opinion, and it is what an operator copies into a report.
    """
    pad = " " * indent
    t = Text()
    evidence = str(f.get("evidence") or "").strip()
    summary = str(f.get("summary") or "").strip()
    if evidence:
        t.append(f"{pad}evidence  ", style=palette.dim)
        t.append(evidence, style=palette.primary)
        t.append("\n")
    if summary:
        t.append(f"{pad}summary   ", style=palette.dim)
        t.append(summary, style=palette.dim)
        t.append("\n")
    agent = str(f.get("agent") or "").strip()
    if agent:
        t.append(f"{pad}found by  ", style=palette.dim)
        t.append(agent, style=palette.dim)
        t.append("\n")
    return t


# ---- context compaction ---------------------------------------------------
# Auto-compaction is the one long operation with no output of its own: the
# summariser is a single request, so the operator sees a still screen for as
# long as it takes. This panel is the answer — it animates the *shape* of what
# is happening (a four-stage cascade draining the transcript into a summary)
# rather than claiming progress it cannot know, which is why the cascade loops
# instead of filling to 100%.
#
# The numbers are real: the start size is the measured prompt the endpoint
# reported, the target is the size a summary is expected to land at, and the
# elapsed clock is the wall time of the request. Only the motion is decoration.

#: Braille spinner, the same family the rest of the harness uses.
COMPACT_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
#: The four things compaction does, top to bottom. The bar per row is the
#: transcript shrinking, and each row lags the one above it so the field reads
#: as a wave folding down the column rather than four independent gauges.
COMPACT_STAGES = ("read", "fold", "summarise", "rebuild")


def _fmt_tokens(n: int) -> str:
    """`182.4k` / `1.0M` — one decimal, never a bare long integer."""
    n = max(0, int(n))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _compact_bar(ratio: float, cells: int, palette: Palette,
                 bright: bool) -> Text:
    """One row of the cascade: filled cells, a lit leading edge, then slack."""
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * cells))
    body = f"bold {palette.accent}" if bright else palette.primary
    t = Text()
    if filled:
        # The last filled cell is the "edge" of the collapse — it is where the
        # tokens are going, so it is the one cell that is always bright.
        t.append("▓" * (filled - 1), style=body)
        t.append("▓", style=f"bold {palette.accent}")
    if filled < cells:
        t.append("░" * (cells - filled), style=palette.dim)
    return t


def compaction_panel(state: dict, palette: Palette, width: int = 72) -> Text:
    """The live compaction animation, as a block of text.

    ``state`` keys, all optional except ``used``:

    ``used``      the measured prompt size, in tokens
    ``window``    the resolved context window the threshold is a share of
    ``threshold`` the token count that tripped the compaction
    ``trigger``   ``"threshold"`` or ``"overflow"``
    ``frame``     monotonic frame counter, drives the animation
    ``elapsed``   seconds since the compaction started
    ``done``      True once the summariser returned (or failed)
    ``ok``        whether it worked; only meaningful with ``done``
    ``note``      the summariser's note or the error, shown on the last line
    """
    p = palette
    used = max(0, int(state.get("used") or 0))
    window = max(0, int(state.get("window") or 0))
    threshold = max(0, int(state.get("threshold") or 0))
    trigger = str(state.get("trigger") or "threshold")
    frame = int(state.get("frame") or 0)
    elapsed = float(state.get("elapsed") or 0.0)
    done = bool(state.get("done"))
    ok = bool(state.get("ok"))
    note = str(state.get("note") or "")

    # The size a summary lands at. Not knowable in advance, so the estimate
    # below is labelled as one while the compaction runs; when it finishes the
    # loop's measured figure (`after`) replaces it, so the "freed" line is a
    # measurement rather than a guess.
    after = max(0, int(state.get("after") or 0))
    target = max(1_200, min(int(used * 0.08) if used else 1_200, 20_000))
    if done and ok:
        target = after or max(600, min(target, used))

    # A looping sawtooth, deliberately not a progress bar: the summariser gives
    # no progress, so a bar that filled would be inventing one.
    phase = ((frame * 0.055) % 1.0) if not done else 1.0

    cells = max(10, min(48, width - 30))
    t = Text()

    # ---- header ----
    t.append(" ⟲ ", style=f"bold {p.accent}")
    t.append("CONTEXT COMPACTION", style=f"bold {p.accent}")
    t.append("  ")
    if trigger == "overflow":
        t.append("prompt overflow", style=f"bold {p.red}")
        if window:
            t.append(f" · {_fmt_tokens(window)} window", style=p.dim)
    else:
        t.append(f"at {_fmt_tokens(threshold)}", style=p.amber)
        if window:
            t.append(f" of {_fmt_tokens(window)}", style=p.dim)
    t.append("\n")

    rule = "─" * max(8, min(width, 78))
    t.append(" " + rule, style=p.dim)
    t.append("\n")

    # ---- the cascade ----
    for i, stage in enumerate(COMPACT_STAGES):
        # Each row starts a little after the one above it and runs a little
        # further, so the collapse travels down the column.
        lag = i * 0.17
        row_phase = max(0.0, min(1.0, phase * 1.55 - lag))
        floor = target / used if used else 0.0
        ratio = 1.0 - row_phase * (1.0 - floor)
        current = int(used * ratio)
        bright = row_phase < 0.98
        t.append(f" {stage:<10}", style=p.dim if done else p.primary)
        t.append_text(_compact_bar(ratio, cells, p, bright))
        t.append("  ")
        t.append(f"{_fmt_tokens(current):>7}", style=p.primary if not done else p.dim)
        t.append("\n")

    t.append(" " + rule, style=p.dim)
    t.append("\n")

    # ---- footer: what is actually happening ----
    if not done:
        spin = COMPACT_SPINNER[frame % len(COMPACT_SPINNER)]
        t.append(f" {spin} ", style=f"bold {p.accent}")
        t.append("summarising history", style=f"bold {p.primary}")
        t.append(f"  {elapsed:4.1f}s", style=p.dim)
        t.append("  → ≈", style=p.dim)
        t.append(_fmt_tokens(target), style=p.accent)
        t.append(" after", style=p.dim)
    elif ok:
        t.append(" ✔ ", style=f"bold {p.accent}")
        t.append("history compacted", style=f"bold {p.accent}")
        t.append(f"  {_fmt_tokens(used)} → {_fmt_tokens(target)}", style=p.primary)
        t.append(f"  freed {_fmt_tokens(max(0, used - target))}", style=p.dim)
        t.append(f"  {elapsed:4.1f}s", style=p.dim)
    else:
        t.append(" ✖ ", style=f"bold {p.red}")
        t.append("compaction failed", style=f"bold {p.red}")
        t.append(f"  {elapsed:4.1f}s", style=p.dim)
    t.append("\n")

    if note:
        # One line of the summariser's own words: a summary that came back
        # about something else is a real failure mode, and it is invisible
        # unless the note is shown.
        first = note.strip().splitlines()[0] if note.strip() else ""
        if first:
            if len(first) > max(20, width - 4):
                first = first[: max(20, width - 5)] + "…"
            t.append("   " + first, style=p.dim)
            t.append("\n")
    return t
