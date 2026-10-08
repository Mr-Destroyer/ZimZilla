"""Rich renderables: tool panels, diffs, code, and the banner."""

from __future__ import annotations

import re
from datetime import datetime

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from ..theme import TOOL_ICONS, Palette

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


def agent_text(text: str, palette: Palette) -> Text:
    """Assistant prose, dim-green for body text with brighter code ticks."""
    t = Text()
    parts = re.split(r"(`[^`]*`)", text)
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            t.append(part.strip("`"), style=f"bold {palette.accent}")
        else:
            t.append(part, style=palette.primary)
    return t


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

    Body text goes through ``agent_text`` so backticked code reads the same as
    it does anywhere else in the transcript.
    """
    t = agent_label(name, color, palette, width)
    t.append_text(agent_text(text, palette))
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
