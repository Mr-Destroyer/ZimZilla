"""Rich renderables: tool panels, diffs, code, and the banner."""

from __future__ import annotations

import re

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
def _header(name: str, args: dict, palette: Palette, status: str = "") -> Text:
    icon = TOOL_ICONS.get(name, "▸")
    label = name.upper().replace("_", " ")
    t = Text()
    t.append(f"[ {icon} {label} ]", style=f"bold {palette.primary}")
    detail = _detail(name, args)
    if detail:
        t.append("  " + detail, style=palette.accent)
    if status:
        t.append("  " + status, style=palette.dim)
    return t


def _detail(name: str, args: dict) -> str:
    if name == "bash":
        cmd = args.get("command", "")
        return "$ " + (cmd if len(cmd) <= 200 else cmd[:199] + "…")
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

    if name == "bash":
        lang = detect_lang_for_command(args.get("command", ""))
        code = output
        body: RenderableType = hacker_syntax(code, lang, palette, line_numbers=False)
        exit_code = meta.get("exit")
        if meta.get("timeout"):
            status = Text(" TIMEOUT ", style=f"bold white on {palette.amber}")
        elif exit_code is None:
            status = Text(" EXEC ", style=f"bold white on {palette.dim}")
        elif exit_code == 0:
            status = Text(f" EXIT 0 ", style=f"bold black on {palette.accent}")
        else:
            status = Text(f" EXIT {exit_code} ", style=f"bold white on {palette.red}")
        return Panel(
            body,
            title=_header(name, args, palette),
            subtitle=status,
            subtitle_align="right",
            title_align="left",
            border_style=border,
            padding=(0, 1),
        )

    # diff-ish tools and reads: plain highlighted text
    lang = lang_for_path(args.get("path", "")) if name == "read_file" else "text"
    if name == "read_file":
        body = hacker_syntax(output, lang, palette, line_numbers=False)
    else:
        body = render_output_text(output, palette)
    return Panel(
        body,
        title=_header(name, args, palette),
        title_align="left",
        border_style=border,
        padding=(0, 1),
    )


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
