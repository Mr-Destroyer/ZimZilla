"""Filesystem jail.

All file tools resolve their paths through :func:`resolve_in_jail`, which
refuses anything that escapes the working directory unless the session was
started with ``--unsafe``.
"""

from __future__ import annotations


class SandboxError(Exception):
    """Raised when a path escapes the sandbox."""


def resolve_in_jail(workdir, raw: str, unsafe: bool = False, *, must_exist: bool = False):
    """Resolve *raw* relative to *workdir*, enforcing the jail.

    Returns a resolved ``pathlib.Path``. Symlinks are resolved so a link
    pointing outside the jail is caught. Set ``must_exist`` to require the
    target to already exist (reads).
    """
    from pathlib import Path

    workdir = Path(workdir).resolve()
    if not raw or raw.strip() == "":
        raise SandboxError("empty path")

    p = Path(raw).expanduser()
    candidate = p if p.is_absolute() else workdir / p

    # strict=False so we can validate paths that don't exist yet (writes).
    # resolve() follows symlinks, so a link pointing outside the jail is caught
    # by the containment check rather than slipping through.
    resolved = candidate.resolve()

    # Check containment BEFORE existence, so an escaping path reports the
    # security-relevant reason rather than a misleading "does not exist".
    if not unsafe:
        try:
            resolved.relative_to(workdir)
        except ValueError:
            raise SandboxError(
                f"path escapes the working directory ({workdir}): {raw}\n"
                f"  re-run with --unsafe to permit it"
            ) from None

    if must_exist and not resolved.exists():
        raise SandboxError(f"path does not exist: {raw}")

    return resolved


def contained_in_jail(workdir, path, unsafe: bool = False) -> bool:
    """True if *path* (already constructed) stays inside *workdir*.

    Used to validate the *results* of globbing, which ``resolve_in_jail``
    cannot police because ``Path.glob`` itself follows ``..`` and symlinks.
    """
    from pathlib import Path

    if unsafe:
        return True
    workdir = Path(workdir).resolve()
    try:
        Path(path).resolve().relative_to(workdir)
        return True
    except ValueError:
        return False


def safe_glob_pattern(pattern: str) -> bool:
    """Reject glob patterns that try to climb out or address an absolute path."""
    if not pattern:
        return True
    p = pattern.replace("\\", "/")
    if p.startswith("/") or p.startswith("~"):
        return False
    return not any(part == ".." for part in p.split("/"))


def display_path(workdir, path) -> str:
    """Render *path* relative to *workdir* when possible, for compact output."""
    from pathlib import Path

    workdir = Path(workdir).resolve()
    try:
        return str(Path(path).resolve().relative_to(workdir))
    except ValueError:
        return str(path)
