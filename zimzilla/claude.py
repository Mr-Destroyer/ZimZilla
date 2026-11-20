"""Run the `claude` CLI inside ZimZilla, using the connected upstream.

``/zim-claude`` suspends the TUI, exports the active source's credentials as
environment variables, and hands the terminal to Claude Code. When the child
exits, ZimZilla resumes exactly where it left off.

The upstream that was active when the command ran is what Claude Code talks to:
its base URL, auth token and API key are exported as ``ANTHROPIC_BASE_URL``,
``ANTHROPIC_AUTH_TOKEN`` and ``ANTHROPIC_API_KEY`` so the CLI picks them up
without a separate sign-in. A model can be pinned with ``--model <id>``.

Sources whose auth shape is not standard bearer-token (DeepAI Code's custom
headers, for example) cannot be driven by Claude Code — the command says so
rather than opening a session that will fail on its first turn.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

#: The binary Claude Code installs.
BINARY = "claude"

#: Header names that are not standard bearer auth and therefore cannot be
#: exported to the Claude CLI. A source using these is refused up front.
NON_EXPORTABLE_HEADERS = frozenset(
    {"api-key", "x-deepai-code-theos", "x-deepai-code-instance",
     "x-deepai-code-machine"}
)


def find_binary() -> str | None:
    """Path to the claude binary, or None when it is not installed."""
    found = shutil.which(BINARY)
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / BINARY,
        home / ".npm" / "bin" / BINARY,
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def is_installed() -> bool:
    return find_binary() is not None


def child_env(base_url: str | None, auth_token: str | None,
              api_key: str | None,
              extra_headers: dict[str, str] | None) -> dict[str, str] | None:
    """Build the child process environment for Claude Code.

    Returns ``None`` when the active source's auth shape cannot be exported
    (custom headers the CLI does not understand), so the caller can refuse
    before suspending the TUI.
    """
    if extra_headers:
        lowered = {k.lower() for k in extra_headers}
        if lowered & NON_EXPORTABLE_HEADERS:
            return None

    env = dict(os.environ)
    # Make sure no stale credential or model preference from a previous source
    # leaks in — Claude Code would otherwise try a model id it doesn't know.
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_MODEL", None)
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    if auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = auth_token
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


def run(binary: str, args: list[str] | None = None,
        model: str | None = None, cwd: str | None = None,
        base_url: str | None = None, auth_token: str | None = None,
        api_key: str | None = None,
        extra_headers: dict[str, str] | None = None) -> int:
    """Run claude, returning its exit status.

    The child inherits the current environment with the upstream's credentials
    layered on top. With no proxy concerns here (unlike opencode), the env is
    always passed.
    """
    env = child_env(base_url, auth_token, api_key, extra_headers)
    if env is None:
        return 126

    argv = [binary]
    if model:
        argv.extend(["--model", model])
    if args:
        argv.extend(args)
    try:
        return subprocess.call(argv, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127
