"""Regression suite for `/osint`.

Run:  python tests/test_osint.py   (from an activated venv)

Two layers, matching the split in zimzilla/osint.py itself:

* the pure logic — registry shape, target validation, slugging, the case
  directory, the playbook prompt — exercised directly and with no terminal;
* the wiring — that the slash dispatch, the help table, the palette and the
  completion popup all actually expose the command, so a rename in one place
  cannot silently orphan it in another.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import osint  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402
from zimzilla.ui.complete import SLASH_COMMANDS  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry() -> None:
    # Every kind the operator named is present, and the display order is the
    # order they asked for — a missing entry here is a missing menu row.
    want = ["email", "phone", "facebook", "tiktok", "instagram", "discord", "github"]
    check("registry: all seven kinds exist", set(osint.OSINT_KINDS) == set(want),
          f"{sorted(osint.OSINT_KINDS)}")
    check("registry: KIND_ORDER lists them in the operator's order",
          osint.KIND_ORDER == want, f"{osint.KIND_ORDER}")

    # Exactly one kind is built. If a second is flipped on later, this test
    # should be updated deliberately rather than passing by accident.
    built = [n for n, k in osint.OSINT_KINDS.items() if k.built]
    check("registry: email is the built kind", built == ["email"], f"{built}")

    # The stubs must NOT carry a playbook — otherwise a stub would look
    # runnable to build_prompt and the "not built" guard would be the only
    # thing standing between the operator and an empty briefing.
    for name, kind in osint.OSINT_KINDS.items():
        if not kind.built:
            check(f"registry: stub '{name}' has no playbook", not kind.playbook)

    # Names are unique keys with non-empty blurbs, so the menu renders.
    for name, kind in osint.OSINT_KINDS.items():
        check(f"registry: '{name}' is well-formed",
              kind.name == name and bool(kind.blurb) and bool(kind.target_label))


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------

def test_validation() -> None:
    email = osint.OSINT_KINDS["email"]

    good = [
        "a@b.co",
        "first.last@example.com",
        "user+tag@gmail.com",
        "x@sub.domain.example.org",
    ]
    for addr in good:
        ok, err = osint.validate(email, addr)
        check(f"validate: accepts {addr}", ok, err)

    bad = [
        "",              # empty
        "nope",          # no @
        "a@b",           # no dot in domain
        "a b@c.com",     # whitespace in local part
        "a@b..com",      # empty label
        "@example.com",  # no local part
        "a@.com",        # empty domain label
    ]
    for addr in bad:
        ok, _ = osint.validate(email, addr)
        check(f"validate: rejects {addr!r}", not ok)

    # A missing target is reported against the kind's own label, so the
    # operator reads "no email address given", not a generic message.
    ok, err = osint.validate(email, "   ")
    check("validate: empty target names the kind's label",
          not ok and "email address" in err, err)


def test_normalise() -> None:
    # Domain lowercased, local part's case preserved — the local part is
    # technically case-sensitive and lowercasing it would change the address.
    check("normalise: lowercases the domain only",
          osint.normalise_email("  Bob.Smith@GMail.COM ") == "Bob.Smith@gmail.com",
          osint.normalise_email("  Bob.Smith@GMail.COM "))
    check("normalise: leaves a non-address untouched",
          osint.normalise_email("  just-a-handle ") == "just-a-handle")
    # rpartition, not split: a quoted local part may itself contain '@'.
    check("normalise: splits on the last '@'",
          osint.normalise_email('"a@b"@Example.com') == '"a@b"@example.com')


# ---------------------------------------------------------------------------
# Slug and case directory
# ---------------------------------------------------------------------------

def test_case_dir(tmp: Path) -> None:
    email = osint.OSINT_KINDS["email"]
    cfg = _cfg(tmp)
    cfg.state_dir = tmp / "state"

    slug = osint.case_slug("Bob.Smith+news@Gmail.com")
    check("slug: keeps safe characters, drops the rest",
          slug == "bob.smith-news-gmail.com", slug)

    # A target that is all symbols must not produce an empty path segment.
    check("slug: never empty", osint.case_slug("!!!") == "target",
          osint.case_slug("!!!"))

    d = osint.case_dir(cfg, email, "Bob@Example.com")
    check("case_dir: is created", d.is_dir(), str(d))
    check("case_dir: lives under state_dir/osint",
          d.parent == cfg.state_dir / "osint", str(d.parent))
    check("case_dir: named <kind>-<slug>-<stamp>",
          d.name.startswith("email-bob-example.com-"), d.name)

    # The timestamp is second-resolution; two calls in the same second collide
    # by design (same case), so assert the shape rather than uniqueness. The
    # stamp is the last TWO dash-separated segments (the slug itself contains
    # dashes, so a plain rsplit("-", 1) would return only the clock half).
    stamp = "-".join(d.name.rsplit("-", 2)[-2:])
    check("case_dir: stamp is YYYYMMDD-HHMMSS",
          len(stamp) == 15 and stamp[8] == "-" and stamp.replace("-", "").isdigit(),
          stamp)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def test_prompt(tmp: Path) -> None:
    email = osint.OSINT_KINDS["email"]
    case = tmp / "case"
    p = osint.build_prompt(email, "Bob@Example.com", case)

    # The target must be normalised in the prompt, and the case dir must be an
    # absolute path the agent can write to from its first tool call.
    check("prompt: carries the normalised target", "Bob@example.com" in p)
    check("prompt: carries the case dir", str(case) in p)

    # The placeholders must be fully substituted — a stray brace would reach
    # the model as literal text.
    check("prompt: no unsubstituted placeholders",
          "{target}" not in p and "{case_dir}" not in p)

    # The authorised-use condition is stated in every prompt, not just the UI.
    check("prompt: states the authorised-use condition",
          "Authorised investigation only" in p)

    # Spot-check that the phases the operator asked for actually survive into
    # the briefing, so an edit that drops one is caught here.
    for needle in ("Validation", "Gravatar", "Breach", "Account discovery",
                   "Identity correlation", "Domain intelligence", "Paste"):
        check(f"prompt: covers the {needle.split()[0]} phase", needle in p)
    check("prompt: asks for report.md", "report.md" in p)
    check("prompt: defines the confidence levels",
          "CONFIRMED" in p and "PROBABLE" in p and "UNVERIFIED" in p)

    # An unbuilt kind still yields a string rather than raising on .format().
    stub = osint.OSINT_KINDS["phone"]
    s = osint.build_prompt(stub, "+15551234567", case)
    check("prompt: an unbuilt kind degrades gracefully",
          "phone" in s and "+15551234567" in s)


# ---------------------------------------------------------------------------
# Wiring — the command must be reachable from every surface
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    # Completion popup lists it...
    names = [c for c, _ in SLASH_COMMANDS]
    check("wiring: /osint is in the completion menu", "/osint" in names, f"{names}")

    # ...and the dispatch, help and palette tables reference it. Read from the
    # source rather than importing a copy, so a rename cannot drift past this.
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text()
    check("wiring: dispatch has an osint entry", '"osint": lambda a:' in app_src)
    check("wiring: _cmd_osint is defined", "def _cmd_osint(" in app_src)
    check("wiring: help table lists it", '("/osint [kind] <target>"' in app_src)
    check("wiring: palette lists it",
          '("/osint", "open-source recon on a target", "osint")' in app_src)


class _Blk:
    def __init__(self, **k):
        self.__dict__.update(k)

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


class _Msg:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage


async def test_ui(wd: Path) -> None:
    """Drive the command through the live app: menu, refusal, bad target.

    The model is stubbed so no request leaves the machine, and the turn is
    held open on an event so `busy` can be observed while it is genuinely in
    flight rather than raced against an instant completion.
    """
    cfg = _cfg(wd, boot_rain=False)
    # Point state_dir at the temp dir — the default is ~/.zimzilla, and this
    # test must not scatter case directories into the operator's home.
    cfg.state_dir = wd / "state"

    app = ZimZillaApp(cfg)
    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        gate = asyncio.Event()

        async def held_stream():
            await gate.wait()
            yield (None, _Msg([_Blk(type="text", text="done")]))

        app.agent._stream_once = held_stream

        # ---- every refusal path must leave the harness idle ----------------
        app._handle_command("/osint")
        await pilot.pause()
        check("ui: bare /osint does not start a turn", app.busy is False)

        app._handle_command("/osint nosuchkind foo")
        await pilot.pause()
        check("ui: unknown kind is refused", app.busy is False)

        app._handle_command("/osint phone +15551234567")
        await pilot.pause()
        check("ui: a stub kind does not start a turn", app.busy is False)

        app._handle_command("/osint email not-an-address")
        await pilot.pause()
        check("ui: a bad email is refused", app.busy is False)
        # No case dir for the *refused* target — checked by its own glob, not
        # by the parent existing, which an earlier test in this file shares.
        check("ui: a refused run creates no case dir",
              not list((cfg.state_dir / "osint").glob("email-not-an-address-*")))

        # ---- the happy path launches a turn --------------------------------
        app._handle_command("/osint email target@example.com")
        await pilot.pause()
        check("ui: a valid email launches the recon turn", app.busy is True)

        made = list((cfg.state_dir / "osint").glob("email-target-example.com-*"))
        check("ui: the case directory was created", len(made) == 1,
              f"{[p.name for p in made]}")

        # Let the stubbed turn finish and confirm the harness returns to idle.
        gate.set()
        for _ in range(5):
            await pilot.pause()
        check("ui: the turn completes and the harness idles", app.busy is False)


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_registry()
        test_validation()
        test_normalise()
        test_case_dir(tmp)
        test_prompt(tmp)
        test_wiring()
        await test_ui(tmp)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
