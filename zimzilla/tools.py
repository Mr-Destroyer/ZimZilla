"""The tool layer: JSON schemas, permission previews and execution.

Nine tools are exposed to the model: bash, read_file, write_file, edit_file,
glob, grep, list_dir, search_web and web_fetch. Reads are auto-approved; writes
and bash go through the permission gate in the UI. The network tools are not
gated — they are bounded by the scope guard instead, so an out-of-scope host is
refused no matter which mode is armed.
"""

from __future__ import annotations

import difflib
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .sandbox import (
    SandboxError,
    contained_in_jail,
    display_path,
    resolve_in_jail,
    safe_glob_pattern,
)
from .websearch import WebError, fetch as _web_fetch, search as _web_search

# ---------------------------------------------------------------------------
# JSON schemas advertised to the model
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "bash",
        "description": (
            "Run a shell command in the working directory. Returns combined "
            "stdout/stderr and the exit code. Use for builds, tests and git. "
            "Commands touching a host listed in out-of-scope.yaml are blocked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command to run."},
                "timeout": {
                    "type": "integer",
                    "description": "Optional timeout in seconds (default 120).",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file. Optionally a line range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
                "offset": {"type": "integer", "description": "1-based first line."},
                "limit": {"type": "integer", "description": "Max lines to read."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
                "content": {"type": "string", "description": "Full file contents."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file. old_string must occur exactly "
            "once unless replace_all is true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path."},
                "old_string": {"type": "string", "description": "Exact text to find."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "glob",
        "description": "Find files by glob pattern (e.g. '**/*.py').",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern."},
                "path": {"type": "string", "description": "Directory to search (default '.')."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep",
        "description": "Search file contents with a regular expression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex to search for."},
                "path": {"type": "string", "description": "File or directory (default '.')."},
                "glob": {"type": "string", "description": "Only search files matching this glob."},
                "ignore_case": {"type": "boolean", "description": "Case-insensitive."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_dir",
        "description": "List the entries of a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory (default '.')."},
            },
            "required": [],
        },
    },
    {
        # Named search_web, NOT web_search: LiteLLM special-cases the literal
        # name "web_search" and rewrites it into OpenAI's built-in
        # web_search_preview tool, which the upstream adapter then rejects with
        # a 400 ("Unsupported Responses tools type"). Any other name is passed
        # through as an ordinary function tool. Do not rename this back.
        "name": "search_web",
        "description": (
            "Search the web and return titles, URLs and snippets. Use it to "
            "look up anything not in this repository — library APIs, error "
            "messages, current versions, unfamiliar terms. Follow up with "
            "web_fetch on a result to read the page in full."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "max_results": {
                    "type": "integer",
                    "description": "How many results to return (default 5).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch a URL and return its readable text. Use it to read a page "
            "found with search_web, or any documentation URL. Hosts listed in "
            "out-of-scope.yaml are blocked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The http(s) URL to read."},
                "max_chars": {
                    "type": "integer",
                    "description": "Truncate the text at this many characters.",
                },
            },
            "required": ["url"],
        },
    },
    {
        # The one tool that exists only during a hunt. A wave agent confirms a
        # bug in the middle of its turn and, until this existed, had no way to
        # say so: findings were read out of the agent's FINAL text by
        # ``hunt.parse_findings`` after the whole wave had finished, so a
        # confirmed SQLi at minute two stayed invisible until minute twenty.
        # Calling this publishes the finding the instant it is confirmed, which
        # is what lets the live tracker show it while the hunt is still running.
        #
        # Withheld from an ordinary session by ``Agent.can_report`` — see the
        # note there for why it must not be advertised outside a hunt.
        "name": "report_finding",
        "description": (
            "Report a confirmed vulnerability to the engagement tracker, the "
            "moment you confirm it. Use this DURING your work, not only at the "
            "end: it is how the operator's live tracker learns what you found "
            "while the wave is still running. Call it once per distinct bug, as "
            "soon as you have the evidence — do not wait until you are done, "
            "and do not batch them up for your final message. Reporting a bug "
            "you cannot evidence is worse than not reporting it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string",
                          "description": "Short name for the bug, e.g. 'SQL injection in ?id'."},
                "severity": {
                    "type": "string",
                    # Spelled out rather than imported from hunt.VALID_SEVERITIES:
                    # hunt imports agent, which imports this module, so reaching
                    # back the other way would close an import cycle. The two are
                    # pinned together by a test instead — see test_hunt.py's
                    # "report_finding" suite.
                    "enum": ["critical", "high", "medium", "low", "info"],
                    "description": "Judge it honestly: a theoretical weakness with "
                                   "no demonstrated impact is 'low', not 'critical'.",
                },
                "asset": {"type": "string",
                          "description": "The host, URL or path affected."},
                "summary": {"type": "string",
                            "description": "What the bug is, in two or three sentences."},
                "evidence": {"type": "string",
                             "description": "The exact request, output or file:line "
                                            "that proves it. Required — a finding "
                                            "with no evidence is a guess."},
                "remediation": {"type": "string",
                                "description": "How to fix it."},
            },
            "required": ["title", "severity", "evidence"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOL_SCHEMAS]

# Tools that require an explicit user confirmation.
GATED_TOOLS = {"bash", "write_file", "edit_file"}

#: The tools a hunt agent may call but an ordinary session may not. See
#: ``Agent.can_report``: advertising report_finding to a session that has no
#: tracker to report into would let a model call a tool whose result goes
#: nowhere, which reads as the harness losing the finding.
HUNT_ONLY_TOOLS = {"report_finding"}

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", "dist", "build", ".idea", ".tox"}


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    meta: dict = field(default_factory=dict)


@dataclass
class Preview:
    """A permission-gate preview for a gated tool call."""

    kind: str  # "command" | "diff" | "text"
    title: str
    body: str


class ToolError(Exception):
    pass


def needs_permission(name: str) -> bool:
    return name in GATED_TOOLS


# ---------------------------------------------------------------------------
# Preview builders (used by the permission gate)
# ---------------------------------------------------------------------------

def _text(value) -> str:
    """Coerce a tool argument to text.

    Tool inputs come straight from the model and are never schema-validated,
    so a field the schema declares as a string can arrive as an int, list or
    None. Coercing here keeps the preview builders from raising.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def build_preview(name: str, args: dict, cfg) -> Preview:
    if name == "bash":
        return Preview("command", "BASH", _text(args.get("command", "")))
    if name == "write_file":
        path = _text(args.get("path", "?"))
        content = _text(args.get("content", ""))
        existing = ""
        try:
            p = resolve_in_jail(cfg.workdir, path, cfg.unsafe)
            if p.exists() and p.is_file():
                existing = p.read_text(errors="replace")
        except Exception:
            pass
        diff = _unified_diff(existing, content, path, new_file=not existing)
        return Preview("diff", f"WRITE {path}", diff)
    if name == "edit_file":
        path = _text(args.get("path", "?"))
        old = _text(args.get("old_string", ""))
        new = _text(args.get("new_string", ""))
        diff = _unified_diff(old, new, path)
        return Preview("diff", f"EDIT {path}", diff)
    return Preview("text", name.upper(), str(args))


def _unified_diff(old: str, new: str, label: str, new_file: bool = False) -> str:
    old = _text(old)
    new = _text(new)
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    fromfile = "/dev/null" if new_file else f"a/{label}"
    tofile = f"b/{label}"
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=fromfile, tofile=tofile, n=3)
    text = "".join(diff)
    if not text.strip():
        return "(no textual change)"
    return text


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute(name: str, args: dict, cfg) -> ToolResult:
    """Run a tool. Never raises for ordinary failures — returns ToolResult."""
    try:
        fn = _DISPATCH.get(name)
        if fn is None:
            return ToolResult(f"unknown tool: {name}", is_error=True)
        return fn(args, cfg)
    except SandboxError as e:
        return ToolResult(f"sandbox: {e}", is_error=True)
    except ToolError as e:
        return ToolResult(str(e), is_error=True)
    except Exception as e:  # noqa: BLE001 - surface any tool failure to the model
        return ToolResult(f"{type(e).__name__}: {e}", is_error=True)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n\n... [truncated {len(text) - limit} chars] ...\n\n{tail}"


# ---- bash -----------------------------------------------------------------

def _tool_bash(args: dict, cfg) -> ToolResult:
    command = _text(args.get("command", ""))
    if not command.strip():
        return ToolResult("empty command", is_error=True)

    try:
        timeout = int(args.get("timeout") or cfg.bash_timeout)
    except (TypeError, ValueError):
        return ToolResult("timeout must be an integer number of seconds", is_error=True)
    if timeout <= 0:
        return ToolResult("timeout must be a positive number of seconds", is_error=True)

    # Scope guard is enforced by the caller (agent) before we ever get here,
    # but re-check defensively for programmatic use.
    from .scope import Scope

    scope = getattr(cfg, "_scope", None)
    if isinstance(scope, Scope) and scope.loaded:
        violations = scope.check_command(command)
        if violations:
            detail = "\n".join(f"  • {v}" for v in violations)
            return ToolResult(
                "BLOCKED by scope guard — command targets out-of-scope host(s):\n"
                f"{detail}",
                is_error=True,
                meta={"blocked": True},
            )

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cfg.workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") + (e.stderr or "")
        return ToolResult(
            _truncate(f"[timeout after {timeout}s]\n{partial}", cfg.max_output_chars),
            is_error=True,
            meta={"exit": None, "timeout": True},
        )

    out = proc.stdout or ""
    err = proc.stderr or ""
    combined = out
    if err:
        combined += ("\n" if out else "") + "[stderr]\n" + err
    if not combined.strip():
        combined = "(no output)"
    combined = _truncate(combined, cfg.max_output_chars)
    return ToolResult(
        combined,
        is_error=proc.returncode != 0,
        meta={"exit": proc.returncode, "timeout": False},
    )


# ---- read_file ------------------------------------------------------------

def _tool_read_file(args: dict, cfg) -> ToolResult:
    path = resolve_in_jail(cfg.workdir, args.get("path", ""), cfg.unsafe, must_exist=True)
    if path.is_dir():
        return ToolResult(f"{display_path(cfg.workdir, path)} is a directory", is_error=True)
    try:
        text = path.read_text(errors="replace")
    except Exception as e:
        return ToolResult(f"cannot read: {e}", is_error=True)

    lines = text.splitlines()
    offset = args.get("offset")
    limit = args.get("limit")
    start = max((int(offset) - 1) if offset else 0, 0)
    end = start + int(limit) if limit else len(lines)
    chunk = lines[start:end]

    width = len(str(start + len(chunk)))
    body = "\n".join(f"{i + start + 1:>{width}} │ {ln}" for i, ln in enumerate(chunk))
    if end < len(lines):
        body += f"\n... ({len(lines) - end} more lines)"
    body = _truncate(body, cfg.max_output_chars)
    return ToolResult(
        body or "(empty file)",
        meta={"path": display_path(cfg.workdir, path), "lines": len(lines)},
    )


# ---- write_file -----------------------------------------------------------

def _tool_write_file(args: dict, cfg) -> ToolResult:
    path = resolve_in_jail(cfg.workdir, args.get("path", ""), cfg.unsafe)
    content = args.get("content", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content)
    verb = "overwrote" if existed else "created"
    return ToolResult(
        f"{verb} {display_path(cfg.workdir, path)} ({len(content)} bytes)",
        meta={
            "path": display_path(cfg.workdir, path),
            "bytes": len(content),
            "created": not existed,
            "old": "" if not existed else None,
        },
    )


# ---- edit_file ------------------------------------------------------------

def _tool_edit_file(args: dict, cfg) -> ToolResult:
    path = resolve_in_jail(cfg.workdir, args.get("path", ""), cfg.unsafe, must_exist=True)
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = bool(args.get("replace_all"))

    if old == "":
        return ToolResult("old_string is empty", is_error=True)

    text = path.read_text(errors="replace")
    count = text.count(old)
    if count == 0:
        return ToolResult("old_string not found in file", is_error=True)
    if count > 1 and not replace_all:
        return ToolResult(
            f"old_string occurs {count} times; pass replace_all=True or add context",
            is_error=True,
        )

    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    path.write_text(updated)
    return ToolResult(
        f"edited {display_path(cfg.workdir, path)} ({count if replace_all else 1} replacement(s))",
        meta={"path": display_path(cfg.workdir, path), "replacements": count if replace_all else 1},
    )


# ---- glob -----------------------------------------------------------------

def _tool_glob(args: dict, cfg) -> ToolResult:
    pattern = args.get("pattern", "")
    base_arg = args.get("path") or "."
    base = resolve_in_jail(cfg.workdir, base_arg, cfg.unsafe)
    if not base.exists():
        return ToolResult(f"path does not exist: {base_arg}", is_error=True)
    if not safe_glob_pattern(pattern):
        return ToolResult(
            f"sandbox: glob pattern escapes the working directory: {pattern}", is_error=True
        )

    matches = []
    for p in sorted(base.glob(pattern)):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        # Path.glob follows '..' and symlinks; re-validate every hit.
        if not contained_in_jail(cfg.workdir, p, cfg.unsafe):
            continue
        if p.is_file():
            matches.append(display_path(cfg.workdir, p))
    if not matches:
        return ToolResult("(no matches)")
    shown = matches[:500]
    body = "\n".join(shown)
    if len(matches) > len(shown):
        body += f"\n... ({len(matches) - len(shown)} more)"
    return ToolResult(body, meta={"count": len(matches)})


# ---- grep -----------------------------------------------------------------

def _tool_grep(args: dict, cfg) -> ToolResult:
    pattern = args.get("pattern", "")
    if not pattern:
        return ToolResult("empty pattern", is_error=True)
    base = resolve_in_jail(cfg.workdir, args.get("path") or ".", cfg.unsafe, must_exist=True)
    file_glob = args.get("glob") or "**/*"
    if not safe_glob_pattern(file_glob):
        return ToolResult(
            f"sandbox: glob pattern escapes the working directory: {file_glob}", is_error=True
        )
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return ToolResult(f"bad regex: {e}", is_error=True)

    files: list[Path] = []
    if base.is_file():
        files = [base]
    else:
        for p in base.glob(file_glob):
            # Path.glob follows '..' and symlinks; re-validate every hit.
            if not contained_in_jail(cfg.workdir, p, cfg.unsafe):
                continue
            if p.is_file() and not any(part in _SKIP_DIRS for part in p.parts):
                files.append(p)

    hits: list[str] = []
    files_hit: set[str] = set()
    for f in sorted(files):
        try:
            with open(f, errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if rx.search(line):
                        rel = display_path(cfg.workdir, f)
                        hits.append(f"{rel}:{lineno}:{line.rstrip()}")
                        files_hit.add(rel)
                        if len(hits) >= 400:
                            break
        except Exception:
            continue
        if len(hits) >= 400:
            break

    if not hits:
        return ToolResult("(no matches)", meta={"count": 0})
    body = "\n".join(hits)
    if len(hits) >= 400:
        body += "\n... (truncated at 400 matches)"
    return ToolResult(body, meta={"count": len(hits), "files": len(files_hit)})


# ---- list_dir -------------------------------------------------------------

def _tool_list_dir(args: dict, cfg) -> ToolResult:
    path = resolve_in_jail(cfg.workdir, args.get("path") or ".", cfg.unsafe, must_exist=True)
    if not path.is_dir():
        return ToolResult(f"not a directory: {args.get('path')}", is_error=True)
    entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines = []
    for e in entries:
        if e.name in _SKIP_DIRS:
            continue
        if e.is_dir():
            lines.append(f"{e.name}/")
        else:
            try:
                size = e.stat().st_size
            except OSError:
                size = 0
            lines.append(f"{e.name}  ({_human(size)})")
    return ToolResult("\n".join(lines) or "(empty directory)")


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n}{unit}"
        n //= 1024
    return f"{n}T"


# ---- web ------------------------------------------------------------------

def _scope_of(cfg):
    """The session scope guard, or None when there is nothing to enforce."""
    from .scope import Scope

    scope = getattr(cfg, "_scope", None)
    if isinstance(scope, Scope) and scope.loaded:
        return scope
    return None


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url if "//" in url else "https://" + url).hostname or ""
    except ValueError:
        return ""


def _tool_web_search(args: dict, cfg) -> ToolResult:
    query = _text(args.get("query", "")).strip()
    if not query:
        return ToolResult("empty query", is_error=True)

    try:
        max_results = int(args.get("max_results") or 5)
    except (TypeError, ValueError):
        return ToolResult("max_results must be an integer", is_error=True)
    max_results = max(1, min(max_results, 20))

    # No scope check here: a search query names no host, and the scope guard
    # only ever blocks named hosts. The engine itself is the operator's own
    # choice of endpoint, not a target.
    try:
        results = _web_search(query, max_results=max_results)
    except WebError as e:
        return ToolResult(f"web search failed: {e}", is_error=True)

    if not results:
        return ToolResult(f"no results for {query!r}")

    lines = [f"{len(results)} result(s) for {query!r}:", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.title}")
        lines.append(f"   {r.url}")
        if r.snippet:
            lines.append(f"   {r.snippet}")
        lines.append("")
    lines.append("Use web_fetch on a URL above to read the page in full.")
    return ToolResult(
        _truncate("\n".join(lines), cfg.max_output_chars),
        meta={"results": len(results)},
    )


def _tool_web_fetch(args: dict, cfg) -> ToolResult:
    url = _text(args.get("url", "")).strip()
    if not url:
        return ToolResult("empty url", is_error=True)

    try:
        max_chars = int(args.get("max_chars") or min(cfg.max_output_chars, 20_000))
    except (TypeError, ValueError):
        return ToolResult("max_chars must be an integer", is_error=True)
    max_chars = max(200, min(max_chars, cfg.max_output_chars))

    # Fetching a URL is reaching a host, so it answers to the deny-list. The
    # allow-list is a declaration and never blocks, so it is not consulted.
    scope = _scope_of(cfg)
    if scope is not None:
        host = _host_of(url)
        allowed, reason = scope.allows(host)
        if not allowed:
            return ToolResult(
                f"BLOCKED by scope guard — {host}: {reason}",
                is_error=True,
                meta={"blocked": True},
            )

    try:
        final_url, text = _web_fetch(url, max_chars=max_chars)
    except WebError as e:
        return ToolResult(f"web fetch failed: {e}", is_error=True)

    header = f"# {final_url}\n\n" if final_url != url else ""
    return ToolResult(
        _truncate(header + text, cfg.max_output_chars),
        meta={"url": final_url},
    )


def _tool_report_finding(args: dict, cfg) -> ToolResult:
    """Accept a finding from a hunt agent and hand it back for publication.

    Nothing is written here. The finding travels out on the *tool_result*
    event's ``meta``, which is where ``hunt._run_agent`` picks it up and
    deduplicates it into the run. Keeping the side effect out of the tool is
    what lets it be tested without a campaign, and what stops a report from
    being written twice — once here and once when the wave harvests the agent's
    final text.

    The tool returns a confirmation the agent can read, because a model that
    cannot tell whether its report landed will report the same bug again on the
    next turn.
    """
    title = _text(args.get("title", "")).strip()
    evidence = _text(args.get("evidence", "")).strip()
    if not title:
        return ToolResult("report_finding: 'title' is required", is_error=True)
    if not evidence:
        # The contract already demands evidence. Enforcing it here rather than
        # only in the prompt is what keeps the tracker free of guesses: a model
        # that skips the evidence is told so, and the finding does not publish.
        return ToolResult(
            "report_finding: 'evidence' is required — quote the request, output "
            "or file:line that proves the bug. An unevidenced finding is a guess.",
            is_error=True,
        )

    severity = _text(args.get("severity", "")).strip().lower() or "info"
    return ToolResult(
        f"recorded: [{severity}] {title}",
        meta={"finding": {
            "title": title[:200],
            "severity": severity,
            "asset": _text(args.get("asset", "")).strip()[:300],
            "summary": _text(args.get("summary", "")).strip(),
            "evidence": evidence,
            "remediation": _text(args.get("remediation", "")).strip(),
        }},
    )


_DISPATCH = {
    "bash": _tool_bash,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "glob": _tool_glob,
    "grep": _tool_grep,
    "list_dir": _tool_list_dir,
    "search_web": _tool_web_search,
    "web_fetch": _tool_web_fetch,
    "report_finding": _tool_report_finding,
}


def summarise_call(name: str, args: dict, cfg, max_len: int = 70) -> str:
    """One-line human label for a tool call, e.g. `$ nmap -sV host`."""
    if name == "bash":
        cmd = args.get("command", "")
        return "$ " + (cmd if len(cmd) <= max_len else cmd[: max_len - 1] + "…")
    if name in {"read_file", "write_file", "edit_file", "list_dir"}:
        return str(args.get("path", ""))
    if name == "glob":
        return f"{args.get('pattern','')}  in {args.get('path','.')}"
    if name == "grep":
        return f"/{args.get('pattern','')}/  in {args.get('path','.')}"
    if name == "search_web":
        q = str(args.get("query", ""))
        return q if len(q) <= max_len else q[: max_len - 1] + "…"
    if name == "web_fetch":
        u = str(args.get("url", ""))
        return u if len(u) <= max_len else u[: max_len - 1] + "…"
    if name == "report_finding":
        sev = str(args.get("severity", "info")).upper()
        title = str(args.get("title", ""))
        label = f"[{sev}] {title}"
        return label if len(label) <= max_len else label[: max_len - 1] + "…"
    return shlex.quote(str(args))[:max_len]
