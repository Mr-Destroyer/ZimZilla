"""Self-update on launch — detect new commits on the upstream and apply them.

ZimZilla is installed editable from its own checkout (``requirements.txt``
ends in ``-e .``, and ``packaging/zimzilla`` points ``PYTHONPATH`` at the
checkout root before ``exec``-ing ``python -m zimzilla``). So the code that
runs *is* the git working tree, and "updating ZimZilla" means fast-forwarding
that tree — not reinstalling a package.

This module answers one question at launch: is ``origin`` ahead of me, and if
so, can I move to it safely? It is pure logic with no Textual, the same shape
as ``zimzilla/osint.py`` and ``zimzilla/team.py``, so the whole thing is
testable against real throwaway repositories without a terminal.

The design is built around not surprising the operator, because applying an
update means executing code fetched from the remote at startup:

* ``--ff-only`` — never a merge commit, never a rebase. A diverged branch is
  reported, not resolved.
* A dirty tree means no pull at all. This checkout is also the operator's
  development tree, so uncommitted work is normal, not an error, and nothing
  here may stash, reset or otherwise touch it.
* Every git call carries a timeout. A hung fetch must cost a couple of
  seconds, never a stalled launch.
* Every failure path is non-fatal: offline, no upstream, not a repository,
  git absent, pull refused — each returns a result and the harness boots
  normally.

Nothing in this module blocks. It reports what it found and what it did, and
leaves the printing and the re-exec decision to the caller.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Seconds any single git invocation may take. A fetch of a small repo is
#: well under a second on a working connection; this only has to be long
#: enough to survive a slow link and short enough that a black-holed one does
#: not hold the launch hostage.
GIT_TIMEOUT = 10.0

#: The file whose change means the Python dependencies moved and the venv
#: needs rebuilding. Its content is compared across the pull.
DEPS_FILE = "requirements.txt"


@dataclass
class UpdateResult:
    """What the update check found and did.

    ``action`` is the whole story in one word:

    ``none``     up to date, not a checkout, or disabled — nothing to say.
    ``notify``   an update exists and is safe to apply.
    ``updated``  pulled successfully; ``should_reexec`` is set.
    ``blocked``  an update exists but applying it was refused (dirty tree,
                 diverged branch) — reported, never forced.
    ``error``    the check itself failed (offline, no git, no upstream).
    """

    action: str = "none"
    behind: int = 0
    commits: list[str] = field(default_factory=list)
    message: str = ""
    deps_changed: bool = False
    should_reexec: bool = False

    @property
    def noteworthy(self) -> bool:
        """Whether the operator should be told about this at all."""
        return self.action in ("updated", "blocked", "error")


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT) -> tuple[int, str]:
    """Run one git command in *root*. Returns (returncode, captured output).

    Never raises. A missing git binary, a timeout and a non-zero exit all come
    back as a return code and whatever was captured, so callers branch on the
    code rather than guarding every call with try/except.

    ``stderr`` is folded into the captured text: git's useful diagnostics
    ("not a git repository", "couldn't find remote ref") go there, and the
    caller wants them for the message.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # FileNotFoundError (no git), TimeoutExpired, PermissionError.
        return 127, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _head(root: Path, timeout: float = GIT_TIMEOUT) -> str:
    """The current HEAD sha, or "" if it could not be read."""
    code, out = _git(root, "rev-parse", "HEAD", timeout=timeout)
    if code != 0:
        return ""
    return out.strip().splitlines()[-1].strip() if out.strip() else ""


def find_checkout() -> Path | None:
    """The git checkout this package is running from, or ``None``.

    ``__file__`` is ``<root>/zimzilla/update.py`` for an editable install and
    for a run straight out of the checkout, so the parent of the package
    directory is the repository root. A wheel installed into site-packages has
    no repository above it, and ``rev-parse`` says so — the whole feature then
    no-ops, which is correct: there is nothing to fast-forward.
    """
    root = Path(__file__).resolve().parent.parent
    code, out = _git(root, "rev-parse", "--is-inside-work-tree")
    if code == 0 and out.strip().splitlines()[-1:] == ["true"]:
        return root
    return None


def _upstream(root: Path, timeout: float = GIT_TIMEOUT) -> str | None:
    """The tracking branch of HEAD (``origin/main``), or ``None`` if unset."""
    code, out = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
                     timeout=timeout)
    if code != 0 or not out.strip():
        return None
    name = out.strip().splitlines()[-1].strip()
    return name or None


def _is_dirty(root: Path, timeout: float = GIT_TIMEOUT) -> bool:
    """Whether the tree has uncommitted changes that a pull could disturb.

    Only *tracked* modifications count. ``--untracked-files=no`` is deliberate:
    a fast-forward cannot lose an untracked file — git either leaves it alone or
    refuses the pull outright because the incoming commit would overwrite it,
    and that refusal is caught and reported. Counting untracked files as dirty
    instead would mean any stray scratch file in the checkout disables the
    feature for good, which is the wrong trade for a check that is meant to
    run unattended.

    A modified tracked file is different: it is work the operator would lose or
    have to reconcile, so it stops the pull dead.

    Shared by ``inspect`` and ``apply`` so the two cannot disagree — the tree
    can change between them, and the decision to pull must be made on what is
    true at the moment of the pull, not on what was true when we looked.
    """
    code, status = _git(root, "status", "--porcelain", "--untracked-files=no",
                        timeout=timeout)
    return code == 0 and bool(status.strip())


def inspect(root: Path, timeout: float = GIT_TIMEOUT) -> UpdateResult:
    """Fetch the upstream and report how far behind HEAD is.

    Fetch first so the comparison is against what is actually on the remote
    now, not against a stale remote-tracking ref. The fetch is the only step
    that touches the network, so its failure is the ordinary offline case and
    is reported as ``error`` rather than raising.
    """
    if _upstream(root, timeout) is None:
        return UpdateResult(action="none", message="no upstream tracking branch configured")

    code, out = _git(root, "fetch", "--quiet", timeout=timeout)
    if code != 0:
        # Offline, no credentials, no such remote — all the same to us: we
        # could not find out, so we say nothing and boot.
        return UpdateResult(action="error", message=f"could not reach the remote: {out}")

    code, out = _git(root, "rev-list", "--count", "HEAD..@{u}", timeout=timeout)
    if code != 0:
        return UpdateResult(action="error", message=f"could not compare with upstream: {out}")
    try:
        behind = int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return UpdateResult(action="error", message="could not read the commit count")

    if behind == 0:
        return UpdateResult(action="none", message="up to date")

    code, out = _git(root, "log", "--oneline", "--no-decorate", "HEAD..@{u}", timeout=timeout)
    commits = out.splitlines() if code == 0 else []

    # A dirty tree is not an error here, it is the normal state of a
    # development checkout. It does mean we must not pull.
    if _is_dirty(root, timeout):
        return UpdateResult(
            action="blocked",
            behind=behind,
            commits=commits,
            message="local changes present — not updating (commit or stash them to update)",
        )

    return UpdateResult(
        action="notify",
        behind=behind,
        commits=commits,
        message=f"{behind} new commit{'s' if behind != 1 else ''} available",
    )


def apply(root: Path, result: UpdateResult, timeout: float = GIT_TIMEOUT) -> UpdateResult:
    """Fast-forward onto the upstream and record what changed.

    Normally reached only for a ``notify`` result, which by construction means
    the tree was clean when it was inspected. That is not relied on: a fetch
    sits between the two calls, and the operator can edit a file in that
    window, so the dirty check is repeated here against the state of the tree
    right now. ``apply`` refuses on its own evidence rather than on the
    caller's memory of it.

    ``--ff-only`` is the second guard: if anything has diverged the pull
    refuses and we report that instead of creating a merge commit the operator
    did not ask for.
    """
    if _is_dirty(root, timeout):
        return UpdateResult(
            action="blocked",
            behind=result.behind,
            commits=result.commits,
            message="local changes present — not updating (commit or stash them to update)",
        )

    before = _head(root, timeout)

    code, out = _git(root, "pull", "--ff-only", "--quiet", timeout=timeout)
    if code != 0:
        return UpdateResult(
            action="blocked",
            behind=result.behind,
            commits=result.commits,
            message=f"could not fast-forward: {out}",
        )

    after = _head(root, timeout)

    # Did the dependency file move across the range we just pulled? Compared
    # over old..new rather than against the working tree, so an unrelated local
    # edit to requirements.txt cannot produce a false positive.
    deps_changed = False
    if before and after and before != after:
        code, _ = _git(root, "diff", "--quiet", before, after, "--", DEPS_FILE,
                       timeout=timeout)
        # diff --quiet exits 1 when there IS a difference.
        deps_changed = code == 1

    return UpdateResult(
        action="updated",
        behind=result.behind,
        commits=result.commits,
        message=f"updated by {result.behind} commit{'s' if result.behind != 1 else ''}",
        deps_changed=deps_changed,
        should_reexec=True,
    )


def run(*, enabled: bool = True, timeout: float = GIT_TIMEOUT) -> UpdateResult:
    """The whole check: find the checkout, inspect it, and apply if we may.

    This is the one function ``__main__`` calls. It never raises and never
    blocks on the network for longer than *timeout*; when anything at all goes
    wrong it returns an ``error``/``none`` result and the caller boots as
    normal.
    """
    if not enabled:
        return UpdateResult(action="none", message="disabled")

    root = find_checkout()
    if root is None:
        return UpdateResult(action="none", message="not a git checkout")

    result = inspect(root, timeout=timeout)
    if result.action == "notify":
        return apply(root, result, timeout=timeout)
    return result


def format_result(result: UpdateResult, stream=None) -> None:
    """Print a result in the harness's own voice, to stderr by default.

    Kept beside the logic so the wording lives with the thing it describes.
    Nothing is printed for a quiet result — an up-to-date launch says nothing
    at all, which is what a launch should look like.
    """
    stream = stream if stream is not None else sys.stderr
    if not result.noteworthy:
        return

    def line(text: str) -> None:
        print(text, file=stream)

    if result.action == "error":
        line(f"  · update check skipped — {result.message}")
        return

    noun = f"commit{'s' if result.behind != 1 else ''}"

    if result.action == "blocked":
        line("  ⟩ UPDATE")
        line(f"  ◈ {result.behind} new {noun} on the remote")
        for c in result.commits[:10]:
            line(f"    {c}")
        line(f"  · {result.message}")
        return

    line("  ⟩ UPDATE")
    line(f"  ◈ {result.message}")
    for c in result.commits[:10]:
        line(f"    {c}")
    if result.deps_changed:
        line(f"  · {DEPS_FILE} changed — re-run ./setup.sh to refresh the venv")
    line("  · restarting on the new code…")
