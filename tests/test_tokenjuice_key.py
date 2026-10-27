"""Regression suite for `/tokenjuice-api-setup` — your key, or the shared one.

Run:  ~/.local/share/zimzilla/.venv/bin/python tests/test_tokenjuice_key.py

The operator asked for a command that lets anyone set up their own Token Juice
key, "but keep the public api keys public". Those are two requirements, and the
second is the one that can be broken by accident:

1. **A personal key wins, locally.** Written to
   ``$ZIMZILLA_HOME/tokenjuice/source``, mode 600, read before anything shipped
   in the tree. ``tokenjuice_status()`` has to say *which* file is in force,
   because "a key is set" and "you are spending the shared one" are different
   answers and only one of them is visible otherwise.
2. **The shipped key is never written.** ``packaging/tokenjuice/source`` is
   deliberately committed so a fresh clone works with no signup. Every check
   here compares its bytes before and after a save and a clear.
3. **``clear`` goes back, it does not break.** Only the personal file is
   removed; the shared key takes over again.
4. **The key is never echoed.** The confirmation carries the last four
   characters, and the transcript is checked for the whole key.

No network: the UI checks drive `_handle_command` with the client untouched.
"""

from __future__ import annotations

import asyncio
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import sources  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.complete import SLASH_COMMANDS  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

#: A shape-valid key that is not a real credential. Never used against anything.
KEY = "tj_test_0000000000000000abcd"
SHIPPED = (
    "# ZimZilla — Token Juice profile (shared).\n"
    'export ANTHROPIC_BASE_URL="https://api.tokenjuice.ai/v1"\n'
    'export ANTHROPIC_AUTH_TOKEN="tj_shared_public_0000wxyz"\n'
    'export ANTHROPIC_MODEL="deepseek-ai/DeepSeek-V4.1-Flash-1017"\n'
    'export ANTHROPIC_API_KEY=""\n'
)


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _sandbox(tmp: Path) -> tuple[Path, Path]:
    """Point every profile rung at the temp dir, and ship the shared key.

    All three module globals are patched, not just ``ZIMZILLA_HOME``: the
    candidate chain is personal → legacy → shipped, and a test that patched one
    of them would read the real ``~/.zimzilla`` and pass for the wrong reason.
    """
    zhome = tmp / "zimzilla"
    home = tmp / "home"
    root = tmp / "root"
    (root / "packaging" / "tokenjuice").mkdir(parents=True, exist_ok=True)
    (root / "packaging" / "tokenjuice" / "source").write_text(
        SHIPPED, encoding="utf-8")
    sources.ZIMZILLA_HOME = zhome
    sources.DEFAULT_HOME = home
    sources.DEFAULT_ROOT = root
    return zhome, root


def _shipped(root: Path) -> Path:
    return root / "packaging" / "tokenjuice" / "source"


# ---------------------------------------------------------------------------
# 1 — the key shape
# ---------------------------------------------------------------------------

def test_key_shape(tmp: Path) -> None:
    check("shape: a tj_ key is accepted", sources.is_tokenjuice_key(KEY))
    check("shape: an export line is not a key",
          not sources.is_tokenjuice_key(f'export ANTHROPIC_AUTH_TOKEN="{KEY}"'))
    check("shape: another prefix is not a key",
          not sources.is_tokenjuice_key("sk-" + "a" * 40))
    check("shape: too short is not a key", not sources.is_tokenjuice_key("tj_"))
    check("shape: empty is not a key", not sources.is_tokenjuice_key(""))
    check("shape: None is not a key", not sources.is_tokenjuice_key(None))


# ---------------------------------------------------------------------------
# 2 — which credential is in force
# ---------------------------------------------------------------------------

def test_status(tmp: Path) -> None:
    zhome, root = _sandbox(tmp / "a")
    st = sources.tokenjuice_status()
    check("status: the shipped key is reported as shipped",
          st.origin == "shipped", st.origin)
    check("status: the path is the one in the tree",
          st.path == _shipped(root), str(st.path))
    check("status: the tail is the last four, not the key",
          st.tail == "wxyz", st.tail)
    check("status: the shipped key is not personal", not st.is_personal)
    check("status: the upstream is read from the profile",
          st.base_url == "https://api.tokenjuice.ai/v1", st.base_url)

    # A personal key on disk wins over the shipped one, with no repository
    # change at all.
    path = sources.save_token(KEY)
    st = sources.tokenjuice_status()
    check("status: a personal key takes over", st.origin == "personal",
          st.origin)
    check("status: the personal file is the one named",
          st.path == path == zhome / "tokenjuice" / "source", str(st.path))
    check("status: the tail follows the personal key", st.tail == "abcd",
          st.tail)
    check("status: the personal key is personal", st.is_personal)

    # The older layout is still read, so an existing install keeps working.
    sources.clear_token()
    legacy = tmp / "a" / "home" / "claude-source"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "deepseek-claude").write_text(
        'export ANTHROPIC_AUTH_TOKEN="tj_legacy_0000000000009999"\n',
        encoding="utf-8")
    st = sources.tokenjuice_status()
    check("status: the older layout is still found", st.origin == "legacy",
          st.origin)
    check("status: a legacy key counts as personal", st.is_personal)


# ---------------------------------------------------------------------------
# 3 — writing a key
# ---------------------------------------------------------------------------

def test_save(tmp: Path) -> None:
    zhome, root = _sandbox(tmp / "b")
    before = _shipped(root).read_bytes()

    path = sources.save_token(KEY)
    check("save: the profile is written where it is read from",
          path == zhome / "tokenjuice" / "source", str(path))
    check("save: the file is mode 600",
          stat.S_IMODE(path.stat().st_mode) == 0o600,
          oct(stat.S_IMODE(path.stat().st_mode)))
    check("save: the directory is mode 700",
          stat.S_IMODE(path.parent.stat().st_mode) == 0o700,
          oct(stat.S_IMODE(path.parent.stat().st_mode)))
    body = path.read_text(encoding="utf-8")
    check("save: the key is in the file", KEY in body)
    check("save: the upstream came from the shipped template",
          "https://api.tokenjuice.ai/v1" in body)
    check("save: ANTHROPIC_API_KEY stays empty so the token wins",
          'export ANTHROPIC_API_KEY=""' in body)
    check("save: the shipped key is byte-for-byte untouched",
          _shipped(root).read_bytes() == before)

    # Twice is a rewrite, not an append: two AUTH_TOKEN lines would leave the
    # shell and the harness disagreeing about which key is in force.
    sources.save_token(KEY + "9")
    body = path.read_text(encoding="utf-8")
    check("save: a second key replaces the first",
          body.count("ANTHROPIC_AUTH_TOKEN=") == 1
          and (KEY + "9") in body, str(body.count("ANTHROPIC_AUTH_TOKEN=")))


# ---------------------------------------------------------------------------
# 4 — going back to the shared key
# ---------------------------------------------------------------------------

def test_clear(tmp: Path) -> None:
    zhome, root = _sandbox(tmp / "c")
    before = _shipped(root).read_bytes()
    check("clear: nothing to clear is not an error",
          sources.clear_token() is None)

    path = sources.save_token(KEY)
    removed = sources.clear_token()
    check("clear: the personal file is removed", removed == path, str(removed))
    check("clear: the file is gone", not path.exists())
    check("clear: the shared key is in force again",
          sources.tokenjuice_status().origin == "shipped")
    check("clear: the shipped key survives the clear",
          _shipped(root).read_bytes() == before)
    check("clear: clearing twice is not an error",
          sources.clear_token() is None)


# ---------------------------------------------------------------------------
# 5 — wiring: every surface a grep finds
# ---------------------------------------------------------------------------

def test_wiring(tmp: Path) -> None:
    from zimzilla.ui.app import ZimZillaApp

    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    check("wiring: dispatch has the canonical spelling",
          '"tokenjuice-api-setup": lambda a: self._cmd_tokenjuice_key(a)'
          in app_src)
    check("wiring: dispatch has the short spelling",
          '"tokenjuic-api-setup": lambda a: self._cmd_tokenjuice_key(a)'
          in app_src)
    check("wiring: the help table names it",
          '("/tokenjuice-api-setup [key|clear]"' in app_src)
    check("wiring: the palette names it",
          '"/tokenjuice-api-setup", "your own Token Juice key' in app_src)
    check("wiring: it answers while busy",
          "tokenjuice-api-setup" in ZimZillaApp.BUSY_OK)
    check("wiring: the completion popup lists it",
          "/tokenjuice-api-setup" in [n for n, _ in SLASH_COMMANDS])
    check("wiring: the completion popup does not list the typo",
          "/tokenjuic-api-setup" not in [n for n, _ in SLASH_COMMANDS])

    # The agent has to be able to *name* the command, or "set up my token juice
    # key" sends it looking for a file to edit. The environment note is where it
    # learns that.
    from zimzilla.agent import Agent

    cfg = Config.from_env(workdir=tmp, api_key="x",
                          base_url="http://localhost:4001")
    note = Agent(cfg).system_prompt()
    check("wiring: the environment note names the command",
          "/tokenjuice-api-setup" in note)
    check("wiring: the environment note names the profile",
          str(sources.tokenjuice_profile()) in note)


# ---------------------------------------------------------------------------
# 6 — driven through the live TUI
# ---------------------------------------------------------------------------

async def test_ui(tmp: Path) -> None:
    from zimzilla.ui.app import ZimZillaApp

    zhome, root = _sandbox(tmp / "d")
    before = _shipped(root).read_bytes()

    cfg = Config.from_env(workdir=tmp, api_key="x",
                          base_url="http://localhost:4001", boot_rain=False)
    cfg.state_dir = tmp / "ui-state"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        app._handle_command("/tokenjuice-api-setup")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("ui: bare command reports the key in force",
              "the shared key" in blob, blob[-500:])
        check("ui: it says the shared key is public by design",
              "public by design" in blob, blob[-500:])

        # The short spelling, deliberately: it is the name that was asked for.
        app._handle_command(f"/tokenjuic-api-setup {KEY}")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        personal = zhome / "tokenjuice" / "source"
        check("ui: the short spelling saves the key", personal.exists())
        check("ui: the confirmation names the last four",
              "…abcd" in blob, blob[-500:])
        check("ui: the whole key is never echoed", KEY not in blob)
        check("ui: the shipped key is untouched by the save",
              _shipped(root).read_bytes() == before)
        check("ui: the session is told to switch route",
              "/zim-tokenjuice" in blob, blob[-500:])

        app._handle_command("/tokenjuice-api-setup clear")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("ui: clear removes the personal key", not personal.exists())
        check("ui: clear says the shared key is back",
              "shared key is in force again" in blob, blob[-500:])
        check("ui: clear says the shipped key was not touched",
              "not touched" in blob, blob[-500:])

        app._handle_command("/tokenjuice-api-setup not-a-key")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("ui: a non-key is refused", "does not look like a Token Juice key"
              in blob, blob[-400:])


async def test_ui_applies_to_the_live_route(tmp: Path) -> None:
    """Setting a key while already on Token Juice must not need a restart.

    The client holds the token it read at switch time, so writing the file and
    saying "saved" would leave the old key in force until the next launch.
    """
    from zimzilla.ui.app import ZimZillaApp

    _sandbox(tmp / "e")
    cfg = Config.from_env(workdir=tmp, api_key="", auth_token="tj_old_0000zzzz",
                          base_url="https://api.tokenjuice.ai/v1",
                          protocol="openai", boot_rain=False)
    cfg.state_dir = tmp / "ui-state2"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()
        app._handle_command(f"/tokenjuice-api-setup {KEY}")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("live: the session token is replaced", cfg.auth_token == KEY,
              str(cfg.auth_token))
        check("live: the inherited API key is cleared", cfg.api_key == "",
              repr(cfg.api_key))
        check("live: the client is rebuilt on the next request",
              app.agent._client is None)
        check("live: the operator is told no restart is needed",
              "no restart" in blob, blob[-500:])


async def test_switch_paused(tmp: Path) -> None:
    """``/zim-tokenjuice`` refuses the switch while the quota is finished.

    The route must not move: the message carries the whole story (the quota, the
    wait, and which sources to use meanwhile), and a ``tj_`` key handed over
    during the pause is still stored for the day the route comes back.
    """
    from zimzilla.ui.app import ZimZillaApp

    zhome, _root = _sandbox(tmp / "p")
    cfg = Config.from_env(workdir=tmp, api_key="x",
                          base_url="http://localhost:4001", boot_rain=False)
    cfg.state_dir = tmp / "ui-state3"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        app._handle_command("/zim-tokenjuice")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("pause: the quota is named",
              "unlimited quota finished" in blob, blob[-500:])
        check("pause: it says when the route returns",
              "back in a few days" in " ".join(blob.split()), blob[-500:])
        check("pause: it names the sources to use meanwhile",
              "/zim-tokenharbour" in blob and "/zim-nvidia" in blob
              and "/zim-beatapi" in blob, blob[-500:])
        check("pause: the session did not move",
              "localhost:4001" in str(cfg.base_url), str(cfg.base_url))

        # The old one-step "set it and switch" still sets it, for the return.
        app._handle_command(f"/zim-tokenjuice {KEY}")
        await pilot.pause()
        personal = zhome / "tokenjuice" / "source"
        check("pause: a key handed over during it is still saved",
              personal.exists())
        check("pause: and it says the key waits for the route's return",
              "when the quota returns" in "\n".join(
                  s.text for s in app.query_one("#transcript").lines),
              "\n".join(s.text for s in app.query_one("#transcript").lines)[-500:])
        check("pause: still no switch", "localhost:4001" in str(cfg.base_url),
              str(cfg.base_url))
        check("pause: the whole key is never echoed",
              KEY not in "\n".join(s.text for s in
                                   app.query_one("#transcript").lines))


def test_pause_wiring() -> None:
    """Every surface that promises a switch has to stop promising one."""
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    check("wiring: the dispatch reaches the pause",
          '"zim-tokenjuice": lambda a: self._cmd_tokenjuice_paused(a)' in app_src)
    check("wiring: the pause carries the message",
          "unlimited quota finished" in app_src
          and "/zim-tokenharbour, /zim-nvidia, /zim-beatapi" in app_src)
    check("wiring: the help table no longer advertises a switch",
          '"/zim-tokenjuice", "switch upstream to Token Juice' not in app_src)
    check("wiring: the completion popup no longer advertises a switch",
          '("/zim-tokenjuice", "switch upstream to Token Juice'
          not in (ROOT / "zimzilla" / "ui" / "complete.py").read_text(
              encoding="utf-8"))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_key_shape(tmp)
        test_status(tmp)
        test_save(tmp)
        test_clear(tmp)
        test_wiring(tmp)
        test_pause_wiring()
        asyncio.run(test_ui(tmp))
        asyncio.run(test_switch_paused(tmp))
        asyncio.run(test_ui_applies_to_the_live_route(tmp))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
