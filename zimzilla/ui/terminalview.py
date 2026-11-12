"""A PTY-backed terminal widget for the MCP panel.

Neovim is a terminal application. To show the real thing in a Textual pane,
this runs the app on a pseudo-terminal, emulates the screen with ``pyte``, and
paints the result as Rich text. Keys typed while the widget is focused go back
up the PTY, so the embedded editor is interactive.

It is deliberately small: one screen, no scrollback ring, no mouse protocol.
The purpose is to watch and drive a TUI that an MCP bridge is operating, not to
replace the terminal the operator launched ZimZilla from.

The same `write` path is used by the bridge control socket: a text/key command
from ``app_type``/``app_keys`` is encoded here exactly as a keypress would be,
so the model and the operator act on the same process.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
from pathlib import Path

import pyte
from rich.color import ColorParseError
from rich.style import Style
from rich.text import Text
from textual import events
from textual.widget import Widget

#: Terminal key names, in the encoding a VT100-family app expects.
_KEYS = {
    "enter": b"\r",
    "tab": b"\t",
    "backspace": b"\x7f",
    "escape": b"\x1b",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "insert": b"\x1b[2~",
    "delete": b"\x1b[3~",
    "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS",
    "f5": b"\x1b[15~", "f6": b"\x1b[17~", "f7": b"\x1b[18~",
    "f8": b"\x1b[19~", "f9": b"\x1b[20~", "f10": b"\x1b[21~",
    "f11": b"\x1b[23~", "f12": b"\x1b[24~",
}

_MODS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
         "super": "super", "meta": "meta"}

#: TUI-only commands we are willing to run inside the panel. A GUI command
#: launched in a PTY would produce a process with no visible window and a
#: confused terminal; the activity log is the honest view for those.
TUI_COMMANDS = frozenset({
    "nvim", "neovim", "vim", "vi", "nano", "pico", "micro", "helix", "hx",
    "htop", "btop", "top", "less", "more", "most", "tmux", "screen",
    "bash", "zsh", "fish", "sh", "python", "python3", "ipython", "lazygit",
    "lazydocker", "ranger", "nnn", "mc", "weechat", "irssi", "newsboat",
    "mutt", "neomutt", "k9s", "ncdu", "ncmpcpp", "cmus", "calcurse",
})


def is_tui(command: list[str]) -> bool:
    """Whether *command* is a terminal application we can embed."""
    if not command:
        return False
    base = Path(command[0]).name
    return base in TUI_COMMANDS


def encode_chord(chord: str) -> bytes:
    """Encode ``ctrl+s`` / ``Return`` / ``a`` the way a terminal expects."""
    parts = [p for p in (chord or "").split("+") if p]
    if not parts:
        return b""
    mods = [_MODS[p.lower()] for p in parts[:-1] if p.lower() in _MODS]
    key = parts[-1]
    lower = key.lower()
    if lower in _KEYS:
        data = _KEYS[lower]
    elif len(key) == 1:
        data = key.encode("utf-8")
    else:
        # A named key libxkbcommon knows but this table does not. Send the
        # name spelled as it is; the PTY's app decides what it means.
        data = key.encode("utf-8")
    if "alt" in mods or "meta" in mods:
        data = b"\x1b" + data
    if "ctrl" in mods and len(data) == 1:
        code = data[0]
        if 0x40 <= code <= 0x7F:
            data = bytes([code & 0x1F])
    if "shift" in mods and len(data) == 1 and 0x61 <= data[0] <= 0x7A:
        data = data.upper()
    return data


def encode_keys(keys: str) -> bytes:
    """Encode a space-separated sequence of chords."""
    return b"".join(encode_chord(part) for part in (keys or "").split())


class TerminalView(Widget, can_focus=True):
    """One embedded terminal process, rendered with pyte."""

    DEFAULT_CSS = """
    TerminalView {
        height: 1fr;
        width: 100%;
        background: $background;
        color: $foreground;
        overflow: hidden;
    }
    TerminalView:focus { border: none; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._master: int | None = None
        self._proc: subprocess.Popen | None = None
        self._screen: pyte.Screen | None = None
        self._stream: pyte.ByteStream | None = None
        self._cols = 80
        self._rows = 24
        self._loop = None
        self._pending = bytearray()
        self.command: list[str] = []
        self.cwd = ""
        self.error = ""

    # ---- process ----------------------------------------------------------
    def start(self, command: list[str], *, cwd: str = "") -> None:
        self.stop()
        if not command:
            raise ValueError("no command")
        exe = command[0]
        resolved = exe if os.path.isabs(exe) else self._which(exe)
        if resolved is None:
            raise FileNotFoundError(f"command not found: {exe}")
        self.command = [resolved, *command[1:]]
        self.cwd = cwd or os.getcwd()
        cols, rows = self._measure()
        self._cols, self._rows = cols, rows
        master, slave = pty.openpty()
        self._set_winsize(master, cols, rows)
        env = dict(os.environ)
        env.update({
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "LINES": str(rows),
            "COLUMNS": str(cols),
        })
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=slave, stdout=slave, stderr=slave,
                cwd=self.cwd, env=env, close_fds=True,
                preexec_fn=os.setsid,
            )
        except OSError:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        os.set_blocking(master, False)
        self._master = master
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(master, self._on_readable)
        if self._pending:
            try:
                os.write(master, bytes(self._pending))
            except OSError:
                pass
            self._pending.clear()
        self.refresh()

    @staticmethod
    def _which(name: str) -> str | None:
        import shutil
        return shutil.which(name)

    def stop(self) -> None:
        if self._master is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self._master)
            except Exception:  # noqa: BLE001
                pass
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
        self._master = None
        if self._proc is not None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        self._proc = None
        self._screen = None
        self._stream = None

    def _on_readable(self) -> None:
        if self._master is None:
            return
        try:
            data = os.read(self._master, 65536)
        except (BlockingIOError, OSError):
            return
        if not data:
            if self._master is not None:
                try:
                    self._loop.remove_reader(self._master)
                except Exception:  # noqa: BLE001
                    pass
            return
        if self._stream is not None:
            self._stream.feed(data)
            self.refresh()

    def _measure(self) -> tuple[int, int]:
        size = self.content_size
        cols = max(10, size.width or 80)
        rows = max(4, size.height or 24)
        return cols, rows

    @staticmethod
    def _set_winsize(fd: int, cols: int, rows: int) -> None:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def on_resize(self, _event) -> None:
        cols, rows = self._measure()
        if cols == self._cols and rows == self._rows:
            return
        self._cols, self._rows = cols, rows
        if self._screen is not None:
            self._screen.resize(rows, cols)
        if self._master is not None:
            self._set_winsize(self._master, cols, rows)
        self.refresh()

    def on_unmount(self) -> None:
        self.stop()

    # ---- input ------------------------------------------------------------
    def write_bytes(self, data: bytes) -> None:
        if not data:
            return
        if self._master is None:
            # A control command can arrive between the event that opens the
            # pane and the PTY being ready; holding it is better than dropping
            # the keystrokes the model just sent.
            self._pending.extend(data[-8192:])
            return
        try:
            os.write(self._master, data)
        except OSError:
            pass

    def send_text(self, text: str) -> None:
        self.write_bytes((text or "").encode("utf-8"))

    def send_keys(self, keys: str) -> None:
        self.write_bytes(encode_keys(keys))

    def on_key(self, event: events.Key) -> None:
        if self._master is None:
            return
        data = self._encode_event(event)
        if data:
            event.stop()
            event.prevent_default()
            self.write_bytes(data)

    @staticmethod
    def _encode_event(event: events.Key) -> bytes:
        key = (event.key or "").lower()
        char = event.character
        if key in _KEYS:
            return _KEYS[key]
        if key.startswith("ctrl+") and len(key) == 6:
            code = ord(key[-1].upper()) - 0x40
            return bytes([code & 0x1F])
        if key.startswith("alt+") and len(key) == 5:
            return b"\x1b" + (char or key[-1]).encode("utf-8")
        if char:
            return char.encode("utf-8")
        if len(key) == 1:
            return key.encode("utf-8")
        return b""

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.focus()

    # ---- rendering --------------------------------------------------------
    def render(self) -> Text:
        if self._screen is None:
            if self.error:
                return Text(self.error, style="red")
            return Text("(terminal not started)", style="dim")
        out = Text()
        cursor = self._screen.cursor
        focused = self.has_focus
        for y in range(self._rows):
            row = self._screen.buffer[y]
            for x in range(self._cols):
                char = row[x]
                data = char.data or " "
                style = _style(char)
                if focused and x == cursor.x and y == cursor.y:
                    style = _merge_style(style, Style(reverse=True))
                out.append(data, style=style)
            if y < self._rows - 1:
                out.append("\n")
        return out


def _color(value: str):
    """A pyte colour name/hex string in the form Rich understands."""
    if not value or value == "default":
        return None
    low = str(value).lower().lstrip("#")
    # pyte reports true-colour components as bare hex ("e0e2ea"); Rich wants a
    # leading hash. This was the crash that made a coloured Neovim buffer
    # unrenderable.
    if len(low) in (3, 6) and all(c in "0123456789abcdef" for c in low):
        return "#" + low
    if low.isdigit():
        return f"color({low})"
    if low.startswith("bright"):
        return "bright_" + low[6:]
    return low


def _style(char) -> Style:
    fg = _color(char.fg)
    bg = _color(char.bg)
    if char.reverse:
        fg, bg = bg, fg
    try:
        return Style(color=fg, bgcolor=bg, bold=char.bold,
                     italic=char.italics, underline=char.underscore,
                     strike=char.strikethrough)
    except ColorParseError:
        return Style(bold=char.bold, italic=char.italics,
                     underline=char.underscore, strike=char.strikethrough)


def _merge_style(base: Style, extra: Style) -> Style:
    try:
        return base + extra
    except Exception:  # noqa: BLE001
        return extra
