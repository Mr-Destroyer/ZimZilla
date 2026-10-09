"""Command-line entrypoint: ``python -m zimzilla`` / ``zimzilla``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from . import sources as sources_mod
from . import tokenharbour as th_mod
from . import update as update_mod
from .config import KNOWN_MODELS, Config, DEFAULT_MODEL, find_agents_file
from .scope import find_legacy_scope


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zimzilla",
        description="ZimZilla — an agentic coding CLI with a green-on-black hacker TUI.",
    )
    parser.add_argument("--model", "-m", default=None, help=f"model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--workdir", "-C", default=None, help="working directory (default: cwd)")
    parser.add_argument("--allow", default=None,
                        help="path to allow.yaml — declared targets; its presence runs tools unattended")
    parser.add_argument("--deny", default=None,
                        help="path to out-of-scope.yaml — hosts that are never touched")
    parser.add_argument("--scope", default=None,
                        help="DEPRECATED — replaced by --allow / --deny; ignored with a warning")
    parser.add_argument("--unsafe", action="store_true",
                        help="disable the filesystem sandbox (paths may escape the workdir)")
    parser.add_argument("--theme", default=None, choices=["green", "amber", "cyan"],
                        help="colour theme")
    parser.add_argument("--rain", action="store_true",
                        help="start with matrix rain on in the shell (off by default for readability)")
    parser.add_argument("--no-rain", action="store_true",
                        help="disable matrix rain entirely, including the boot screen")
    parser.add_argument("--mode", default=None,
                        choices=["auto", "edits", "plan", "zim", "danger", "uncensored"],
                        help="agent mode: auto | edits | plan | zim | danger | uncensored (default: auto)")
    parser.add_argument("--agents", default=None,
                        help="path to AGENTS.md used by zim mode (default: workdir, "
                             "its parent, ~/.zimzilla, then ~)")
    parser.add_argument("--max-tokens", type=int, default=None, help="max output tokens per reply")
    parser.add_argument("--no-update", action="store_true",
                        help="skip the launch-time check for new commits on the upstream")
    parser.add_argument("--list-models", action="store_true", help="list known models and exit")
    parser.add_argument("--version", action="version", version=f"zimzilla {__version__}")
    return parser


def _launcher_path() -> str | None:
    """The launcher script this run came through, if there is one.

    ``packaging/zimzilla`` exports ``ZIMZILLA_ROOT`` before it execs the
    interpreter, so the script is discoverable from there. A bare
    ``python -m zimzilla`` — no launcher — leaves the variable unset and gets
    ``None``.
    """
    root = os.environ.get("ZIMZILLA_ROOT")
    if not root:
        return None
    launcher = Path(root) / "packaging" / "zimzilla"
    if launcher.is_file() and os.access(launcher, os.X_OK):
        return str(launcher)
    return None


def main(argv: list[str] | None = None) -> int:
    # Captured before parsing so the re-exec below can reproduce the original
    # invocation exactly, whatever it was.
    raw_argv = list(argv) if argv is not None else sys.argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_models:
        for m in KNOWN_MODELS:
            print(m)
        return 0

    workdir = Path(args.workdir).expanduser() if args.workdir else Path.cwd()

    # Target scope: two files with one job each. allow.yaml arms the session
    # (its presence means the operator has declared their targets, so tools run
    # unattended); out-of-scope.yaml is the never-touch list. Each is looked for
    # in the working directory first, then in ~/.zimzilla.
    def _find(explicit: str | None, names: tuple[str, ...]) -> Path | None:
        if explicit:
            return Path(explicit).expanduser()
        for base in (workdir, Path.home() / ".zimzilla"):
            for name in names:
                candidate = Path(base) / name
                if candidate.exists():
                    return candidate
        return None

    allow_path = _find(args.allow, ("allow.yaml", "allow.yml"))
    deny_path = _find(args.deny, ("out-of-scope.yaml", "out-of-scope.yml"))

    # A leftover scope.yaml is never read: the old file was an allow-list that
    # also blocked, so reinterpreting it as a deny-list would block the very
    # hosts the operator had declared. Report it instead of acting on it.
    legacy_scope = None
    if args.scope:
        legacy_scope = Path(args.scope).expanduser()
    else:
        legacy_scope = find_legacy_scope(workdir, Path.home())

    # Zim mode follows the operator's AGENTS.md. The search order lives in
    # config.find_agents_file so the CLI and the runtime /mode zim path cannot
    # drift apart: workdir, its parent, the installed doctrine in ~/.zimzilla,
    # then ~. The installed copy is what makes zim mode work from any directory.
    agents_path = None
    if args.agents:
        agents_path = Path(args.agents).expanduser()
    else:
        agents_path = find_agents_file(workdir)

    # Shell rain is off unless requested. --no-rain kills rain everywhere,
    # including the boot screen.
    cfg = Config.from_env(
        model=args.model,
        workdir=workdir,
        allow_path=allow_path,
        deny_path=deny_path,
        legacy_scope_path=legacy_scope,
        unsafe=args.unsafe or None,
        theme=args.theme,
        max_tokens=args.max_tokens,
        mode=args.mode,
        agents_path=agents_path,
        rain=True if args.rain else None,
        boot_rain=False if args.no_rain else None,
    )

    # A remembered TokenHarbour choice has to be re-applied here, not in the
    # launcher: the key can live in ZimZilla's own profile, in ~/claude-source,
    # or only in the environment, and only the harness knows how to find all
    # three. The launcher deliberately does not source a TokenHarbour profile it
    # cannot find, so without this a session would come back up on the default
    # Logfare endpoint with the wrong credential.
    #
    # Guarded by applies_to_default_endpoint: an endpoint the operator pointed
    # somewhere deliberate — the gateway itself, or any other host — is left
    # exactly as they set it, so a stale selection file cannot hijack it.
    if sources_mod.current_key() == "tokenharbour" \
            and th_mod.applies_to_default_endpoint(cfg.base_url):
        th_mod.apply_to(cfg)

    # The split is not silent. A stale scope.yaml would otherwise leave the
    # operator believing the guard is armed when nothing is loaded at all —
    # and the old file's meaning cannot be carried over, because an allow-list
    # read as a deny-list would block exactly the hosts that were declared.
    if legacy_scope is not None and legacy_scope.exists():
        print(
            f"zimzilla: {legacy_scope} is no longer read — the scope guard was "
            "split into two files:\n"
            "  allow.yaml          declared targets; its presence runs tools "
            "unattended\n"
            "  out-of-scope.yaml   hosts that are never touched\n"
            "  Nothing is armed or blocked right now. Rename the file to one of "
            "those to re-enable it.",
            file=sys.stderr,
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

    # Launch-time self-update. This runs after the preflight checks, so a
    # launch that cannot proceed anyway never pulls, and before the TUI takes
    # the terminal, so the notice is plain text on a normal screen.
    #
    # An applied update must re-exec rather than continue: zimzilla.config,
    # zimzilla.scope and the rest are already imported, and the fast-forward
    # just replaced their files on disk. Running on would be a mix of old and
    # new modules. execv replaces the process image outright, which is exactly
    # what packaging/zimzilla already does with its own `exec "$PY" -m`.
    #
    # ZIMZILLA_UPDATED guards the loop: once we have re-exec'd, the next launch
    # of main() skips the check entirely, so a flaky fetch cannot make the
    # harness restart itself repeatedly.
    if not os.environ.get("ZIMZILLA_UPDATED"):
        result = update_mod.run(enabled=not args.no_update
                                and os.environ.get("ZIMZILLA_NO_UPDATE") != "1")
        if result.noteworthy:
            update_mod.format_result(result)
        if result.should_reexec:
            os.environ["ZIMZILLA_UPDATED"] = "1"
            # Re-exec through the launcher when there is one, so the venv
            # dependency sync it performs runs against the requirements.txt the
            # update just pulled. Going straight to sys.executable would skip
            # that and boot the new code against the old venv — exactly the
            # case the update flagged. A bare `python -m zimzilla` has no
            # launcher and re-execs the interpreter, as before.
            launcher = _launcher_path()
            if launcher is not None:
                os.execv(launcher, [launcher, *raw_argv])
            os.execv(sys.executable, [sys.executable, "-m", "zimzilla", *raw_argv])

    from .ui.app import ZimZillaApp
    from .termbg import detect_background

    # Ask the terminal for its own background and paint the whole interface in
    # it. A terminal draws its padding — the gutter around the text grid — in
    # that colour, and a TUI cannot paint outside the grid, so a hardcoded
    # background leaves a frame of the wrong colour around everything. This is
    # best-effort: a terminal that does not answer costs one short timeout and
    # the palette keeps its own black.
    app = ZimZillaApp(cfg, term_bg=detect_background())
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
