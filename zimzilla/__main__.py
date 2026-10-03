"""Command-line entrypoint: ``python -m zimzilla`` / ``zimzilla``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import KNOWN_MODELS, Config, DEFAULT_MODEL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zimzilla",
        description="ZimZilla — an agentic coding CLI with a green-on-black hacker TUI.",
    )
    parser.add_argument("--model", "-m", default=None, help=f"model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--workdir", "-C", default=None, help="working directory (default: cwd)")
    parser.add_argument("--scope", default=None, help="path to a scope.yaml (enables the scope guard)")
    parser.add_argument("--unsafe", action="store_true",
                        help="disable the filesystem sandbox (paths may escape the workdir)")
    parser.add_argument("--theme", default=None, choices=["green", "amber", "cyan"],
                        help="colour theme")
    parser.add_argument("--rain", action="store_true",
                        help="start with matrix rain on in the shell (off by default for readability)")
    parser.add_argument("--no-rain", action="store_true",
                        help="disable matrix rain entirely, including the boot screen")
    parser.add_argument("--mode", default=None,
                        choices=["auto", "edits", "plan", "zim", "danger"],
                        help="agent mode: auto | edits | plan | zim | danger (default: auto)")
    parser.add_argument("--agents", default=None,
                        help="path to AGENTS.md used by zim mode (default: next to the workdir)")
    parser.add_argument("--max-tokens", type=int, default=None, help="max output tokens per reply")
    parser.add_argument("--list-models", action="store_true", help="list known models and exit")
    parser.add_argument("--version", action="version", version=f"zimzilla {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_models:
        for m in KNOWN_MODELS:
            print(m)
        return 0

    workdir = Path(args.workdir).expanduser() if args.workdir else Path.cwd()

    scope_path = None
    if args.scope:
        scope_path = Path(args.scope).expanduser()
    else:
        # Auto-discover ./scope.yaml or ./scope.yml in the working directory.
        for candidate in ("scope.yaml", "scope.yml"):
            p = Path(workdir) / candidate
            if p.exists():
                scope_path = p
                break
        if scope_path is None:
            home_scope = Path.home() / ".zimzilla" / "scope.yaml"
            if home_scope.exists():
                scope_path = home_scope

    # Zim mode follows the operator's AGENTS.md. Default location: the working
    # directory, then its parent, then ~.
    agents_path = None
    if args.agents:
        agents_path = Path(args.agents).expanduser()
    else:
        for candidate in (Path(workdir) / "AGENTS.md", Path(workdir).parent / "AGENTS.md"):
            if candidate.exists():
                agents_path = candidate
                break
        if agents_path is None:
            home_agents = Path.home() / "AGENTS.md"
            if home_agents.exists():
                agents_path = home_agents

    # Shell rain is off unless requested. --no-rain kills rain everywhere,
    # including the boot screen.
    cfg = Config.from_env(
        model=args.model,
        workdir=workdir,
        scope_path=scope_path,
        unsafe=args.unsafe or None,
        theme=args.theme,
        max_tokens=args.max_tokens,
        mode=args.mode,
        agents_path=agents_path,
        rain=True if args.rain else None,
        boot_rain=False if args.no_rain else None,
    )

    if not cfg.workdir.exists():
        print(f"zimzilla: working directory does not exist: {cfg.workdir}", file=sys.stderr)
        return 2

    if not cfg.api_key_present:
        print(
            "zimzilla: no credentials found.\n"
            "  Set ANTHROPIC_AUTH_TOKEN (preferred) or ANTHROPIC_API_KEY.\n"
            "  For the local Logfare route, source ZimZilla's own profile:\n"
            "      set -a; . ~/.zimzilla/logfare/source; set +a\n"
            "  Or just run `./setup.sh` once, then launch with `zimzilla`.",
            file=sys.stderr,
        )
        return 2

    from .ui.app import ZimZillaApp

    app = ZimZillaApp(cfg)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
