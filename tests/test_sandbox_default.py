"""The filesystem jail is opt-in: --sandbox, not the default.

Run:  python tests/test_sandbox_default.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import __main__ as main_mod  # noqa: E402
from zimzilla.sandbox import SandboxError, resolve_in_jail  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def main() -> int:
    parser = main_mod.build_parser()
    args = parser.parse_args([])
    check("cli: unrestricted is the default", args.sandbox is False)
    check("cli: --sandbox opts in", parser.parse_args(["--sandbox"]).sandbox is True)
    check("cli: legacy --unsafe is accepted",
          parser.parse_args(["--unsafe"]).unsafe is True)

    src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    check("cli: it passes unsafe=not sandbox", "unsafe=not args.sandbox" in src)

    tmp = Path(tempfile.mkdtemp(prefix="zim-sandbox-"))
    outside = Path("/etc/hostname")
    if outside.exists():
        allowed = resolve_in_jail(tmp, str(outside), True, must_exist=True)
        check("resolver: unrestricted permits an outside path", allowed == outside.resolve())
        try:
            resolve_in_jail(tmp, str(outside), False, must_exist=True)
        except SandboxError:
            denied = True
        else:
            denied = False
        check("resolver: --sandbox still refuses it", denied)
    else:
        check("resolver: /etc/hostname exists", True, "skipped")

    ui = (ROOT / "zimzilla" / "ui" / "widgets.py").read_text(encoding="utf-8")
    check("ui: the header says unrestricted", "⛨ unrestricted" in ui)
    boot = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    check("boot: it says unrestricted", "UNRESTRICTED (default)" in boot)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
