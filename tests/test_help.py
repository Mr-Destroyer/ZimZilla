"""Regression suite for `/help` — the grouped command reference.

Run:  ~/.local/share/zimzilla/.venv/bin/python tests/test_help.py

The flat list had grown to eighty-odd rows and had no structure: the operator
read all of it to find one command, and related commands were scattered in the
order they happened to be added. What this pins:

1. **The data.** Every group has a key, a label and a blurb; no entry is
   duplicated; every command the completion popup offers is documented, and
   every documented command is offered. That parity is what stops the two
   surfaces drifting apart again.
2. **The wide layout.** A two-column grid, with every description starting in
   the same column, and the footer (MODES / MENTIONS / KEYS) present.
3. **The narrow layout.** Below the width where two columns fit, the command
   leads and the description wraps underneath with a hanging indent — never a
   description squeezed into a word per line.
4. **The filter.** `/help <group>` shows one section, `/help all` shows every
   one, and an unknown group is refused by name.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.app import HELP_GROUPS  # noqa: E402
from zimzilla.ui.complete import SLASH_COMMANDS  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(tmp: Path) -> Config:
    cfg = Config.from_env(workdir=tmp, base_url="https://logfare.ai/v1",
                          auth_token="x")
    cfg.state_dir = tmp / "state"
    return cfg


def _lines(app) -> list[str]:
    return [s.text for s in app.query_one("#transcript").lines]


# ---------------------------------------------------------------------------
# 1 — the data
# ---------------------------------------------------------------------------

def test_data() -> None:
    keys = [g[0] for g in HELP_GROUPS]
    labels = [g[1] for g in HELP_GROUPS]
    check("data: every group has a unique key", len(keys) == len(set(keys)), str(keys))
    check("data: every group has a unique label",
          len(labels) == len(set(labels)), str(labels))
    check("data: every group has a blurb",
          all(blurb.strip() for _k, _l, blurb, _r in HELP_GROUPS))
    check("data: every group has rows",
          all(rows for _k, _l, _b, rows in HELP_GROUPS))

    entries = [name for _k, _l, _b, rows in HELP_GROUPS for name, _d in rows]
    check("data: no command is listed twice",
          len(entries) == len(set(entries)),
          str([n for n in set(entries) if entries.count(n) > 1]))
    check("data: every entry has a description",
          all(desc.strip() for _k, _l, _b, rows in HELP_GROUPS
              for _name, desc in rows))

    # The completion popup and the reference are two views of one command set.
    documented = {name.split()[0] for name in entries}
    offered = {name for name, _desc in SLASH_COMMANDS}
    check("data: every offered command is documented",
          not (offered - documented), str(sorted(offered - documented)))
    check("data: every documented command is offered",
          not (documented - offered), str(sorted(documented - offered)))


# ---------------------------------------------------------------------------
# 2/3 — the two layouts
# ---------------------------------------------------------------------------

async def test_wide(tmp: Path) -> None:
    from zimzilla.ui.app import ZimZillaApp

    app = ZimZillaApp(_cfg(tmp))
    async with app.run_test(size=(160, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()
        app._handle_command("/help")
        await pilot.pause()
        lines = _lines(app)
        blob = "\n".join(lines)

        check("wide: every group label is present",
              all(label in blob for _k, label, _b, _r in HELP_GROUPS))
        check("wide: the footer is present", "MODES" in blob and "MENTIONS" in blob)
        check("wide: a command is listed", "/mode [name]" in blob)
        check("wide: the title and count are present",
              "COMMAND REFERENCE" in blob and "commands" in blob)

        help_line = next((l for l in lines if l.strip().startswith("/help ")), "")
        mode_line = next((l for l in lines if l.strip().startswith("/mode [name]")), "")
        check("wide: descriptions start in one column",
              help_line and mode_line
              and help_line.index("this reference") == mode_line.index("switch mode"),
              f"{help_line!r} / {mode_line!r}")


async def test_narrow(tmp: Path) -> None:
    from zimzilla.ui.app import ZimZillaApp

    app = ZimZillaApp(_cfg(tmp))
    async with app.run_test(size=(100, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()
        app._handle_command("/help")
        await pilot.pause()
        lines = _lines(app)
        blob = "\n".join(lines)

        check("narrow: every group label is still present",
              all(label in blob for _k, label, _b, _r in HELP_GROUPS))
        check("narrow: the footer is present", "MODES" in blob and "MENTIONS" in blob)

        mode_at = next((i for i, l in enumerate(lines)
                        if l.strip().startswith("/mode [name]")), -1)
        check("narrow: the command leads the line", mode_at >= 0,
              str(mode_at))
        if mode_at >= 0:
            check("narrow: the description follows the command",
                  " — " in lines[mode_at], lines[mode_at])
            check("narrow: a wrapped description is indented",
                  mode_at + 1 < len(lines)
                  and lines[mode_at + 1].startswith("    ")
                  and not lines[mode_at + 1].startswith("     "),
                  repr(lines[mode_at + 1] if mode_at + 1 < len(lines) else ""))


# ---------------------------------------------------------------------------
# 4 — the filter
# ---------------------------------------------------------------------------

async def test_filter(tmp: Path) -> None:
    from zimzilla.ui.app import ZimZillaApp

    app = ZimZillaApp(_cfg(tmp))
    async with app.run_test(size=(160, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        def fresh() -> str:
            app.query_one("#transcript").clear()
            return ""

        fresh()
        app._handle_command("/help recon")
        await pilot.pause()
        blob = "\n".join(_lines(app))
        check("filter: the recon section is shown",
              "RECON" in blob and "/osint" in blob and "/shodan-cam-search" in blob,
              blob[-300:])
        check("filter: another section is not",
              "/phish" not in blob and "OFFENSIVE" not in blob, blob[-300:])
        check("filter: the count is the section's",
              "11 commands · 1 group" in blob, blob[:300])

        fresh()
        app._handle_command("/help all")
        await pilot.pause()
        blob = "\n".join(_lines(app))
        check("filter: all restores every section",
              "OFFENSIVE" in blob and "RECON" in blob and "86 commands" in blob,
              blob[:300])

        fresh()
        app._handle_command("/help nonsense")
        await pilot.pause()
        blob = "\n".join(_lines(app))
        check("filter: an unknown group is refused by name",
              "no help group: nonsense" in blob, blob[-300:])


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_data()
        asyncio.run(test_wide(tmp))
        asyncio.run(test_narrow(tmp))
        asyncio.run(test_filter(tmp))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
