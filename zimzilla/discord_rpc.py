"""Discord Rich Presence — show ZimZilla on the user's Discord profile.

Rich Presence talks to the **local Discord client** over its IPC socket. It is
not an OAuth link and it does not read the user's account: the activity appears
on whichever account that client is signed into. Discord requires an
*Application ID* to label the activity and host its assets, so this module
cannot invent one — `/discord-rpc <application-id>` stores it, and
`/discord-rpc logo` prepares the image to upload as the `zimzilla` asset.

The presence sender is a daemon thread. If Discord is closed, it keeps trying
quietly; if the library or socket is missing, the command reports why. Nothing
here blocks the TUI.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
PROFILE = ZIMZILLA_HOME / "discord" / "source"
LOGO = ZIMZILLA_HOME / "discord" / "zimzilla.png"
REPO_LOGO = Path(__file__).resolve().parent.parent / "docs" / "zimzilla-rpc.png"
DOWNLOADS_LOGO = Path.home() / "Downloads" / "zimzilla.png"

CLIENT_ID_RE = re.compile(r"^\d{17,20}$")
ASSET_KEY = "zimzilla"
TAGLINE = "The most powerful hacking agent"
ASSET_FILE = ZIMZILLA_HOME / "discord" / "asset"
ASSET_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_IMAGE_BYTES = 512 * 1024   # Discord's Rich Presence asset ceiling


def valid_client_id(value: str) -> bool:
    return bool(CLIENT_ID_RE.fullmatch((value or "").strip()))


def _read_exports(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        name, _, value = line[len("export "):].partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name.strip():
            out[name.strip()] = value
    return out


def load_client_id() -> str:
    env = os.environ.get("DISCORD_CLIENT_ID", "").strip()
    if valid_client_id(env):
        return env
    value = _read_exports(PROFILE).get("DISCORD_CLIENT_ID", "").strip()
    return value if valid_client_id(value) else ""


def save_client_id(client_id: str) -> Path:
    client_id = (client_id or "").strip()
    if not valid_client_id(client_id):
        raise ValueError("a Discord application id is 17-20 digits")
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        "# ZimZilla — Discord Rich Presence (written by /discord-rpc)\n"
        "# This is the Discord *Application ID*, not an account token.\n"
        f'export DISCORD_CLIENT_ID="{client_id}"\n'
    )
    fd = os.open(PROFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        os.chmod(PROFILE, 0o600)
    except OSError:
        pass
    return PROFILE


def load_asset_key() -> str:
    """The Rich Presence asset key. Must match the portal exactly."""
    env = os.environ.get("DISCORD_ASSET_KEY", "").strip()
    if ASSET_KEY_RE.fullmatch(env):
        return env
    try:
        text = ASSET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ASSET_KEY
    return text if ASSET_KEY_RE.fullmatch(text) else ASSET_KEY


def save_asset_key(key: str) -> Path:
    key = (key or "").strip()
    if not ASSET_KEY_RE.fullmatch(key):
        raise ValueError("asset keys are 1-64 letters, digits, . _ or -")
    ASSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ASSET_FILE.parent, 0o700)
    except OSError:
        pass
    fd = os.open(ASSET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")
    try:
        os.chmod(ASSET_FILE, 0o600)
    except OSError:
        pass
    return ASSET_FILE


def prepare_logo(source: str | Path | None = None) -> Path:
    """Resize the supplied logo to Discord's 512px/512KB asset limits."""
    from PIL import Image

    candidates = [Path(source)] if source else [DOWNLOADS_LOGO, REPO_LOGO]
    src = next((p for p in candidates if p.is_file()), None)
    if src is None:
        raise FileNotFoundError(f"no logo found at {DOWNLOADS_LOGO}")
    image = Image.open(src).convert("RGBA")
    image.thumbnail((512, 512), Image.Resampling.LANCZOS)
    # Discord's Rich Presence artwork is square. Pad rather than hand it a
    # 512x492 image, which some clients treat as too small.
    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    canvas.alpha_composite(image, ((512 - image.width) // 2,
                                   (512 - image.height) // 2))
    LOGO.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(LOGO, optimize=True)
    # If optimize still leaves it over the ceiling, quantise to a palette.
    if LOGO.stat().st_size > MAX_IMAGE_BYTES:
        canvas.convert("P", palette=Image.Palette.ADAPTIVE).save(LOGO, optimize=True)
    try:
        os.chmod(LOGO, 0o644)
    except OSError:
        pass
    return LOGO


def ipc_socket() -> Path | None:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    for name in ("discord-ipc-0", "discord-ipc-1", "discord-ipc-2",
                 "discord-ipc-3", "discord-ipc-4", "discord-ipc-5"):
        path = Path(runtime) / name
        if path.exists():
            return path
    return None


def display_path(path: str | Path | None) -> str:
    """A compact, speakable working directory for the card."""
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = Path(raw).expanduser()
    try:
        rel = resolved.relative_to(Path.home())
    except ValueError:
        shown = str(resolved)
    else:
        shown = "~" if not rel.parts else "~/" + str(rel)
    if len(shown) <= 128:
        return shown
    # Keep the leaf: a long path is more useful as ".../project" than as a
    # truncated prefix that hides where the work actually is.
    return "…" + shown[-(127):]


def _state_line(path: str, activity: str) -> str:
    """Directory + action in Discord's 128-character state field."""
    activity = activity[:128]
    if not path:
        return activity
    full = f"{path} · {activity}"
    if len(full) <= 128:
        return full
    room = 128 - len(activity) - 3
    if room >= 8:
        leaf = path if len(path) <= room else "…" + path[-(room - 1):]
        return f"{leaf} · {activity}"
    return activity


def payload(state: str, model: str, *, started: int | None = None,
            asset_key: str = ASSET_KEY,
            workdir: str | Path | None = None) -> dict:
    """The activity object sent to Discord. Kept pure for tests.

    The app name is the *title* of the card. ``details`` is the tagline;
    ``state`` carries the working directory, what the model is doing and which
    model is doing it.
    """
    activity = (state or "Idle").strip()
    model = (model or "").strip()
    if model:
        activity = f"{activity} · {model}"
    path = display_path(workdir or Path.cwd())
    return {
        "details": TAGLINE[:128],
        "state": _state_line(path, activity),
        "start": started or int(time.time()),
        "large_image": (asset_key or ASSET_KEY)[:64],
        "large_text": f"ZimZilla — {TAGLINE}"[:128],
        "instance": False,
    }


class DiscordRPC:
    """A reconnecting, non-blocking Rich Presence sender."""

    def __init__(self, client_id: str = "", *, enabled: bool = True,
                 started: int | None = None, workdir: str | Path | None = None) -> None:
        self.client_id = client_id if valid_client_id(client_id) else ""
        self.enabled = bool(enabled)
        self.started = started or int(time.time())
        self.asset_key = load_asset_key()
        self.workdir = Path(workdir or Path.cwd())
        self._activity = payload("Idle", "", started=self.started,
                                 asset_key=self.asset_key, workdir=self.workdir)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.last_error = ""

    @classmethod
    def from_config(cls, workdir: str | Path | None = None) -> "DiscordRPC":
        return cls(load_client_id(), enabled=bool(load_client_id()),
                   workdir=workdir)

    def set_activity(self, state: str, model: str = "") -> None:
        with self._lock:
            self._activity = payload(state, model, started=self.started,
                                     asset_key=self.asset_key,
                                     workdir=self.workdir)
        self._wake.set()

    def start(self) -> bool:
        if not self.enabled:
            return False
        if not self.client_id:
            self.last_error = "no Discord application id — run /discord-rpc <id>"
            return False
        if self._thread is not None and self._thread.is_alive():
            self._wake.set()
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self, *, clear: bool = True) -> None:
        self._stop.set()
        self._wake.set()
        if clear:
            # Best-effort: the worker clears on its way out when connected.
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
        self.connected = False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                from pypresence import Presence
            except Exception as e:  # noqa: BLE001
                self.last_error = f"pypresence is not installed: {e}"
                return
            if ipc_socket() is None:
                self.last_error = "Discord is not running (no IPC socket)"
                self._stop.wait(10)
                continue
            rpc = None
            try:
                rpc = Presence(self.client_id)
                rpc.connect()
                self.connected = True
                self.last_error = ""
                while not self._stop.is_set():
                    with self._lock:
                        activity = dict(self._activity)
                    try:
                        from pypresence import ActivityType
                        rpc.update(activity_type=ActivityType.PLAYING, **activity)
                    except Exception as e:  # noqa: BLE001 - reconnect below
                        self.last_error = f"update failed: {e}"
                        break
                    # Discord rate-limits presence updates; 15s is its safe
                    # window and a state change waits for the next slot.
                    self._wake.wait(15)
                    self._wake.clear()
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
            finally:
                self.connected = False
                if rpc is not None:
                    try:
                        rpc.clear()
                        rpc.close()
                    except Exception:  # noqa: BLE001
                        pass
            if not self._stop.is_set():
                self._stop.wait(10)
