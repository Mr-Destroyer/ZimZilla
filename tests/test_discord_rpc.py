"""Regression suite for Discord Rich Presence and /discord-rpc.

Run:  python tests/test_discord_rpc.py

No connection is opened: the module is exercised through its pure payload,
profile and logo paths, plus the TUI wiring. A real presence needs a Discord
Application ID and the logo uploaded as the `zimzilla` asset.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-discord-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)

from zimzilla import discord_rpc as dr  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def test_client_id() -> None:
    check("id: 18 digits accepted", dr.valid_client_id("123456789012345678"))
    check("id: letters refused", not dr.valid_client_id("1234abcd"))
    check("id: too short refused", not dr.valid_client_id("1234"))
    path = dr.save_client_id("123456789012345678")
    check("profile: written where it says", path == dr.PROFILE)
    check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
          oct(path.stat().st_mode & 0o777))
    check("profile: source shape", "export DISCORD_CLIENT_ID=" in path.read_text())
    os.environ.pop("DISCORD_CLIENT_ID", None)
    check("profile: it loads back", dr.load_client_id() == "123456789012345678")
    os.environ["DISCORD_CLIENT_ID"] = "987654321098765432"
    check("env: the environment wins", dr.load_client_id() == "987654321098765432")
    os.environ.pop("DISCORD_CLIENT_ID", None)


def test_payload() -> None:
    got = dr.payload("Thinking", "deepseek-v4.1-flash", started=1234,
                     workdir=_HOME / "Projects" / "ZimZilla")
    check("payload: details are the tagline",
          got["details"] == dr.TAGLINE, got["details"])
    check("payload: state carries the directory, action and model",
          "Projects/ZimZilla" in got["state"] and
          "Thinking · deepseek-v4.1-flash" in got["state"], got["state"])
    check("payload: logo asset key", got["large_image"] == "zimzilla")
    check("payload: logo hover text",
          got["large_text"] == "ZimZilla — " + dr.TAGLINE, got["large_text"])
    check("payload: custom asset key", dr.payload(
        "Idle", "", asset_key="logo")["large_image"] == "logo")
    check("path: home is abbreviated", dr.display_path(Path.home()) == "~",
          dr.display_path(Path.home()))
    check("path: a long path keeps its leaf",
          dr.display_path("/x/" + "a" * 300).endswith("a" * 127))
    squeezed = dr.payload("Thinking", "m" * 120,
                          workdir="/x/" + "a" * 300)
    check("payload: state is bounded and keeps the action",
          len(squeezed["state"]) <= 128 and "Thinking" in squeezed["state"],
          str(len(squeezed["state"])))
    check("payload: start timestamp", got["start"] == 1234)
    check("payload: it is not an instance", got["instance"] is False)
    long_state = dr.payload("x" * 400, "m" * 400)
    check("payload: state is bounded", len(long_state["state"]) == 128)
    check("payload: model is bounded", len(long_state["details"]) <= 128)


def test_logo() -> None:
    source = _HOME / "logo.png"
    Image.new("RGBA", (1400, 1200), (10, 20, 30, 255)).save(source)
    path = dr.prepare_logo(source)
    check("logo: it writes the profile asset", path == dr.LOGO)
    check("logo: it is a PNG", path.is_file())
    image = Image.open(path)
    check("logo: it is exactly 512x512", image.size == (512, 512), str(image.size))
    check("logo: under Discord's 512KB ceiling",
          path.stat().st_size <= dr.MAX_IMAGE_BYTES,
          f"{path.stat().st_size} bytes")
    check("logo: the repo asset exists", dr.REPO_LOGO.is_file(), str(dr.REPO_LOGO))


def test_asset_key() -> None:
    path = dr.save_asset_key("logo")
    check("asset: written where it says", path == dr.ASSET_FILE)
    check("asset: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
          oct(path.stat().st_mode & 0o777))
    check("asset: it loads back", dr.load_asset_key() == "logo")
    check("asset: junk is refused", not dr.ASSET_KEY_RE.fullmatch("has space"))
    dr.save_asset_key("zimzilla")


def test_sender() -> None:
    off = dr.DiscordRPC("123456789012345678", enabled=False)
    check("sender: disabled does not start", off.start() is False)
    blank = dr.DiscordRPC("", enabled=True)
    check("sender: no id does not start", blank.start() is False)
    check("sender: no id explains itself", "application id" in blank.last_error,
          blank.last_error)
    good = dr.DiscordRPC("123456789012345678", enabled=True)
    check("sender: a valid id is accepted", good.client_id == "123456789012345678")
    # Do not call start(): that would connect to the real client under a fake app.


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    check("wiring: the command is dispatched", '"discord-rpc": lambda a:' in app)
    check("wiring: the command is in help",
          '"/discord-rpc [id|on|off|asset|logo]"' in app)
    check("wiring: the command is completed", '("/discord-rpc",' in comp)
    check("wiring: the setup wizard exists", "def _discord_setup_text" in app)
    check("wiring: the logo command exists", 'if low == "logo":' in app)
    check("wiring: presence starts at boot", "self._discord.start()" in app)
    check("wiring: turn activity is published",
          'set_activity("Thinking"' in app and 'set_activity("Idle"' in app)
    check("wiring: it stops on quit", "self._discord.stop()" in app)
    check("launch: no import crash", "discord_rpc" not in main)  # imported by app


def main() -> int:
    test_client_id()
    test_payload()
    test_logo()
    test_asset_key()
    test_sender()
    test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
