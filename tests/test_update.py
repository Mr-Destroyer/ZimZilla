"""Regression suite for the launch-time self-update.

Run:  python tests/test_update.py   (from an activated venv)

Real git, real repositories, no network. The suite builds a bare "origin" and
a clone of it in a temp directory, then drives ``zimzilla.update`` against the
clone — the same shape as the checkout ZimZilla actually runs from (editable
install, HEAD tracking an upstream).

What is being protected here is not "does git pull work" but the safety
contract around it: a dirty tree is never touched, a diverged branch is never
merged, a broken remote never raises and never blocks the launch. Those are
the properties that make it acceptable to run this automatically at startup,
so they are asserted directly — by checking the commit HEAD points at, not
merely the returned action string.

Every fixture is hermetic: the repos get a pinned identity and signing off, so
the suite does not depend on the operator's global git config. Nothing here
calls ``run()`` against a live environment, because that would fetch a real
remote over the network.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import update  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


# ---------------------------------------------------------------------------
# git plumbing for the fixtures
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str, check_ok: bool = True) -> str:
    """Run git in *repo* and return stdout. Assert success unless told not to."""
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if check_ok and proc.returncode != 0:
        raise AssertionError(f"git {args} failed in {repo}:\n{proc.stderr}")
    return proc.stdout.strip()


def _ident(repo: Path) -> None:
    """Pin an identity and disable signing, so the fixtures are hermetic.

    Without this the suite inherits the operator's global config: a commit
    would fail on a machine with no user.email set, and `commit.gpgsign = true`
    would hang waiting for a passphrase. Neither is a property of the code
    under test.
    """
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "tag.gpgsign", "false")


def make_repos(base: Path) -> tuple[Path, Path]:
    """A bare origin plus a clone tracking it. Returns (origin, clone)."""
    base.mkdir(parents=True, exist_ok=True)
    origin = base / "origin.git"
    clone = base / "clone"

    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")

    # Build the initial commit in a scratch worktree, then push it, so the
    # clone starts with main already existing on the remote.
    seed = base / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch=main")
    _ident(seed)
    (seed / "README.md").write_text("hello\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "initial")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-u", "origin", "main")

    subprocess.run(["git", "clone", str(origin), str(clone)],
                   capture_output=True, text=True, check=True)
    _ident(clone)
    return origin, clone


def commit_to_origin(seed: Path, origin: Path, message: str,
                     files: dict[str, str] | None = None) -> str:
    """Land a new commit on origin's main, as another developer would."""
    for name, content in (files or {"work.txt": message}).items():
        (seed / name).write_text(content)
    git(seed, "add", "-A")
    git(seed, "commit", "-m", message)
    git(seed, "push", "origin", "main")
    return git(seed, "rev-parse", "HEAD")


def head_of(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# find_checkout
# ---------------------------------------------------------------------------

def test_find_checkout() -> None:
    # Running from this repo, __file__ is <root>/zimzilla/update.py, so the
    # parent of the package dir is the checkout — and it is one.
    root = update.find_checkout()
    check("find_checkout: locates this repository", root == ROOT, f"{root}")

    # A directory that is not a repo must come back as None, not raise. This
    # is the site-packages install path.
    with tempfile.TemporaryDirectory() as td:
        real = update.__file__
        try:
            (Path(td) / "zimzilla").mkdir()
            update.__file__ = str(Path(td) / "zimzilla" / "update.py")
            check("find_checkout: None outside a repo", update.find_checkout() is None)
        finally:
            update.__file__ = real


# ---------------------------------------------------------------------------
# The states
# ---------------------------------------------------------------------------

def test_up_to_date(base: Path) -> None:
    _, clone = make_repos(base)
    r = update.inspect(clone)
    check("up to date: action is none", r.action == "none", r.action)
    check("up to date: not noteworthy", not r.noteworthy)
    check("up to date: behind is zero", r.behind == 0, f"{r.behind}")


def test_behind_clean(base: Path) -> None:
    origin, clone = make_repos(base)
    seed = base / "seed"
    commit_to_origin(seed, origin, "second commit")
    want = commit_to_origin(seed, origin, "third commit")
    before = head_of(clone)

    r = update.inspect(clone)
    check("behind+clean: inspect notifies", r.action == "notify", r.action)
    check("behind+clean: counts the commits", r.behind == 2, f"{r.behind}")
    check("behind+clean: lists the commits",
          len(r.commits) == 2 and all(c for c in r.commits), f"{r.commits}")
    # inspect must not move HEAD — only apply does.
    check("behind+clean: inspect leaves HEAD alone", head_of(clone) == before)

    r = update.apply(clone, r)
    check("behind+clean: apply updates", r.action == "updated", r.action)
    check("behind+clean: apply asks for a re-exec", r.should_reexec is True)
    check("behind+clean: HEAD moved to the new tip", head_of(clone) == want,
          f"{head_of(clone)[:8]} != {want[:8]}")
    check("behind+clean: no merge commit was created",
          git(clone, "rev-list", "--count", "--merges", "HEAD") == "0")


def test_behind_modified_tracked(base: Path) -> None:
    origin, clone = make_repos(base)
    seed = base / "seed"
    commit_to_origin(seed, origin, "remote work")
    before = head_of(clone)

    # An edit to a file git is tracking — work the operator would lose or have
    # to reconcile. This is the case that must stop the pull dead.
    (clone / "README.md").write_text("edited by hand\n")

    r = update.inspect(clone)
    check("behind+modified: inspect blocks", r.action == "blocked", r.action)
    check("behind+modified: still reports how far behind",
          r.behind == 1, f"{r.behind}")
    check("behind+modified: does not ask for a re-exec", r.should_reexec is False)
    check("behind+modified: HEAD is untouched", head_of(clone) == before)

    # apply() re-checks for itself: a caller that hands it a stale notify
    # result must not get a pull. This is the regression the suite exists for.
    stale = update.UpdateResult(action="notify", behind=1, commits=["x"])
    r2 = update.apply(clone, stale)
    check("behind+modified: apply refuses on its own evidence",
          r2.action == "blocked", r2.action)
    check("behind+modified: HEAD is still untouched", head_of(clone) == before)
    check("behind+modified: the edit survives",
          (clone / "README.md").read_text() == "edited by hand\n")


def test_behind_untracked(base: Path) -> None:
    origin, clone = make_repos(base)
    seed = base / "seed"
    want = commit_to_origin(seed, origin, "remote work")

    # A stray untracked file — a scratch note, a build artefact. A
    # fast-forward cannot lose it, so it must not disable the update.
    (clone / "scratch.txt").write_text("notes\n")

    r = update.inspect(clone)
    check("behind+untracked: inspect does not block", r.action == "notify", r.action)

    r = update.apply(clone, r)
    check("behind+untracked: the update proceeds", r.action == "updated", r.action)
    check("behind+untracked: HEAD moved", head_of(clone) == want)
    check("behind+untracked: the untracked file survives",
          (clone / "scratch.txt").read_text() == "notes\n")


def test_diverged(base: Path) -> None:
    origin, clone = make_repos(base)
    seed = base / "seed"

    # A local commit on the clone...
    (clone / "mine.txt").write_text("local\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-m", "local commit")
    local_tip = head_of(clone)

    # ...and a different commit on origin.
    commit_to_origin(seed, origin, "remote commit")

    r = update.inspect(clone)
    # Both sides moved, so HEAD is not an ancestor of upstream; --ff-only is
    # the only thing standing between this and an unwanted merge commit.
    check("diverged: inspect does not report a fast-forwardable update",
          r.action in ("blocked", "notify"), r.action)

    if r.action == "notify":
        r = update.apply(clone, r)
        check("diverged: apply refuses to merge", r.action == "blocked", r.action)

    check("diverged: the local commit is still HEAD", head_of(clone) == local_tip,
          head_of(clone)[:8])
    check("diverged: no merge commit exists",
          git(clone, "rev-list", "--count", "--merges", "HEAD") == "0")
    check("diverged: the local file survives", (clone / "mine.txt").exists())


def test_deps_changed(base: Path) -> None:
    origin, clone = make_repos(base)
    seed = base / "seed"

    # An ordinary commit must NOT flag a dependency change...
    commit_to_origin(seed, origin, "docs only", files={"NOTES.md": "hi\n"})
    r = update.apply(clone, update.inspect(clone))
    check("deps: an unrelated commit leaves deps_changed False",
          r.deps_changed is False, f"{r.deps_changed}")

    # ...but a commit touching requirements.txt must.
    commit_to_origin(seed, origin, "add a dependency",
                     files={"requirements.txt": "textual>=0.80\n"})
    r = update.apply(clone, update.inspect(clone))
    check("deps: a requirements.txt change is flagged", r.deps_changed is True)
    check("deps: the update still succeeded", r.action == "updated", r.action)


def test_not_a_repo(base: Path) -> None:
    plain = base / "plain"
    plain.mkdir(parents=True)
    r = update.inspect(plain)
    check("not-a-repo: inspect reports none, not an exception",
          r.action == "none", r.action)


def test_no_upstream(base: Path) -> None:
    # A repo with no remote at all: there is nothing to be behind.
    base.mkdir(parents=True, exist_ok=True)
    solo = base / "solo"
    solo.mkdir()
    git(solo, "init", "--initial-branch=main")
    _ident(solo)
    (solo / "a.txt").write_text("x\n")
    git(solo, "add", "-A")
    git(solo, "commit", "-m", "only")

    r = update.inspect(solo)
    check("no-upstream: reported as none", r.action == "none", r.action)
    check("no-upstream: not noteworthy", not r.noteworthy)


def test_unreachable_remote(base: Path) -> None:
    _, clone = make_repos(base)
    # Point the remote at somewhere that cannot answer, without touching the
    # network: a path that does not exist fails the fetch immediately.
    git(clone, "remote", "set-url", "origin", str(base / "does-not-exist.git"))

    r = update.inspect(clone)
    check("unreachable: reported as error, not raised", r.action == "error", r.action)
    check("unreachable: noteworthy", r.noteworthy)

    # And the error must format cleanly rather than throwing on a None field.
    buf = io.StringIO()
    update.format_result(r, stream=buf)
    check("unreachable: formats without raising", "update check skipped" in buf.getvalue(),
          buf.getvalue().strip())


def test_disabled() -> None:
    # enabled=False must not shell out to git at all. Prove it by breaking the
    # one function that runs git: if run() invoked it, this would raise.
    real_git = update._git

    def explode(*a, **k):
        raise AssertionError("git was invoked while disabled")

    try:
        update._git = explode
        r = update.run(enabled=False)
        check("disabled: run(enabled=False) returns none", r.action == "none", r.action)
        check("disabled: nothing to report", not r.noteworthy)
    finally:
        update._git = real_git


def test_format(base: Path) -> None:
    # An up-to-date result prints nothing at all — a clean launch is silent.
    quiet = update.UpdateResult(action="none", message="up to date")
    buf = io.StringIO()
    update.format_result(quiet, stream=buf)
    check("format: a quiet result prints nothing", buf.getvalue() == "", repr(buf.getvalue()))

    origin, clone = make_repos(base)
    commit_to_origin(base / "seed", origin, "remote work")
    r = update.apply(clone, update.inspect(clone))
    buf = io.StringIO()
    update.format_result(r, stream=buf)
    out = buf.getvalue()
    check("format: an update announces itself", "UPDATE" in out, out.strip())
    check("format: names the commit", "remote work" in out, out.strip())
    check("format: says it is restarting", "restarting" in out, out.strip())

    # A dependency change must add the setup.sh line.
    commit_to_origin(base / "seed", origin, "bump deps",
                     files={"requirements.txt": "textual>=0.81\n"})
    r = update.apply(clone, update.inspect(clone))
    buf = io.StringIO()
    update.format_result(r, stream=buf)
    check("format: flags the venv refresh on a dep change",
          "setup.sh" in buf.getvalue(), buf.getvalue().strip())


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        test_find_checkout()
        test_up_to_date(base / "a")
        test_behind_clean(base / "b")
        test_behind_modified_tracked(base / "c")
        test_behind_untracked(base / "c2")
        test_diverged(base / "d")
        test_deps_changed(base / "e")
        test_not_a_repo(base / "f")
        test_no_upstream(base / "g")
        test_unreachable_remote(base / "h")
        test_disabled()
        test_format(base / "j")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
