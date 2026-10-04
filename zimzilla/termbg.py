"""Ask the terminal what colour its background is (OSC 11).

A terminal emulator paints its padding — the gutter between the window edge and
the text grid — in *its own* background colour. A TUI can only paint inside the
grid, so an app that hardcodes a background differing from the terminal's gets a
frame of the wrong colour around the whole interface, most visible at the
rounded corners where the terminal's own rounding curves that outer edge.

Terminals expose that colour over OSC 11, so the honest fix is to ask.

Textual has an ``ansi_default`` keyword that looks like it would do this, but it
queries nothing: it resolves to a hardcoded #0c0c0c, which just swaps one wrong
colour for another. Hence this module.

Everything here is best-effort. A terminal that does not answer, answers
garbage, is light-themed, or is not a tty at all must cost nothing but the
timeout — the caller falls back to the palette's own background.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time

#: How long to wait for a reply. Long enough for a real terminal, short enough
#: that one which ignores OSC 11 does not visibly stall startup.
_TIMEOUT = 0.25

#: `rgb:RRRR/GGGG/BBBB` (1-4 hex digits per channel) or `#RRGGBB`.
_RGB = re.compile(rb"\]11;rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})")
_HASH = re.compile(rb"\]11;#([0-9a-fA-F]{6})")


def _scale(value: bytes) -> int:
    """Scale one OSC-11 channel to 0-255.

    The channel width is not fixed: most terminals send four hex digits
    (16-bit), some send two (8-bit). Both mean full scale, so divide by the
    largest value that width can hold rather than assuming 16-bit.
    """
    return round(int(value, 16) * 255 / (16 ** len(value) - 1))


def parse_background(reply: bytes) -> str | None:
    """Pull ``#rrggbb`` out of an OSC 11 reply, or None if there isn't one."""
    m = _RGB.search(reply)
    if m:
        return "#%02x%02x%02x" % tuple(_scale(m.group(i)) for i in (1, 2, 3))
    m = _HASH.search(reply)
    if m:
        return "#" + m.group(1).decode().lower()
    return None


def is_dark(hex_color: str) -> bool:
    """Whether a colour is dark enough to keep this palette readable.

    The palettes are neon-on-black by design, so adopting a light terminal
    background would leave bright green text on white. A light reply is refused
    and the caller keeps the palette's own black.

    Rec. 601 luma, scaled to integers: the float form lands on 127.999… for the
    exact midpoint (#808080) and so decides mid-grey by rounding error. Integer
    arithmetic makes the boundary land where it reads — 128 is not dark.
    """
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 299 * r + 587 * g + 114 * b < 128_000


def detect_background() -> str | None:
    """The terminal's background as ``#rrggbb``, or None.

    None when stdin is not a tty (a pipe, a test harness, a cron job), when the
    terminal does not answer within the timeout, when the reply is unparseable,
    or when the colour is too light to use — see :func:`is_dark`.
    """
    if not (sys.stdin and sys.stdin.isatty()):
        return None
    try:
        import termios
        import tty
    except ImportError:  # not POSIX
        return None

    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except Exception:  # noqa: BLE001 — any failure means "cannot query"
        return None

    reply = b""
    try:
        tty.setraw(fd)
        os.write(fd, b"\x1b]11;?\x1b\\")
        deadline = time.monotonic() + _TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(fd, 256)
            if not chunk:
                break
            reply += chunk
            if b"\x1b\\" in reply or b"\x07" in reply:
                break
        # Anything past the terminator is not ours. Drain it, or Textual reads
        # it as keystrokes once it takes the tty over.
        while select.select([fd], [], [], 0)[0]:
            if not os.read(fd, 256):
                break
    except Exception:  # noqa: BLE001 — a failed query is not an error
        return None
    finally:
        # Restore before anything else can read the terminal: Textual takes the
        # tty over immediately after this and must not inherit raw mode.
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:  # noqa: BLE001
            pass

    bg = parse_background(reply)
    return bg if bg and is_dark(bg) else None
