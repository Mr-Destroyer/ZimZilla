"""Regression suite for the spoken launch greeting and idle recap.

Run:  python tests/test_voice.py

No audio is played here: ``Voice.say`` refuses a non-TTY stdout, which is what
a test or a service is. What is tested is the part that decides *what* would be
said, plus the wiring that makes a turn speak when it goes idle.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-voice-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)

from zimzilla import voice  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def test_summary() -> None:
    facts = {"tools": ["read_file", "edit_file", "bash"],
             "files": ["/tmp/a.py", "/tmp/b.py"], "errors": 1,
             "seconds": 12, "cost": 0.0042}
    got = voice.turn_summary(facts, "Tests pass. I also updated the docs.")
    check("summary: it starts with Sir", got.startswith("Sir,"), got)
    check("summary: it says what was done", "2 files" in got and "3 tools" in got, got)
    check("summary: it carries the final result", "Tests pass" in got, got)
    check("summary: it goes beyond the first sentence",
          "updated the docs" in got, got)
    check("summary: it names the files", "a.py" in got and "b.py" in got, got)
    check("summary: it is a full recap", len(got) > 180, str(len(got)))
    check("summary: it reports a recovered error", "1 tool error" in got, got)
    check("summary: it reports elapsed time", "12 seconds" in got, got)
    check("summary: it reports cost", "less than a cent" in got, got)

    stopped = voice.turn_summary(facts, "", cancelled=True)
    check("summary: cancellation is honest", "stopped" in stopped, stopped)
    failed = voice.turn_summary({}, "", error="the endpoint was unreachable")
    check("summary: a failure says so", "problem" in failed, failed)

    empty = voice.turn_summary({}, "")
    check("summary: an empty turn is still complete", "finished" in empty, empty)


def test_clean() -> None:
    dirty = "# Done\n\n**Result:** `pytest` passes.\n```py\nprint('x')\n```\nSee [docs](https://x.test)."
    clean = voice.clean_speech(dirty)
    check("clean: code fences are gone", "print" not in clean, clean)
    check("clean: markdown is gone", "**" not in clean and "`" not in clean, clean)
    check("clean: the sentence survives", "pytest passes" in clean, clean)
    secret = voice.clean_speech("key lfu_abcdefghijklmnop should not be read")
    check("clean: a credential is redacted", "lfu_" not in secret, secret)
    check("clean: it is bounded", len(voice.clean_speech("word " * 400)) <=
          voice.MAX_SPEECH_CHARS + 1,
          str(len(voice.clean_speech("word " * 400))))


def test_voice_object() -> None:
    v = voice.Voice(enabled=True)
    check("voice: it reports a backend", v.available(), str(v.available()))
    # stdout is a pipe here, so it must refuse to speak rather than spawn mpv.
    check("voice: a non-TTY stays silent", v.say("hello sir") is False)
    check("voice: disabled is silent", voice.Voice(enabled=False).say("hello") is False)
    check("config: voice is on by default", Config().voice is True)


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    check("wiring: the launch greeting exists", "def _voice_welcome" in app)
    check("wiring: the turn recap exists", "def _voice_recap" in app)
    check("wiring: it runs when a turn ends", "self._voice_recap(cancelled=" in app)
    check("wiring: /voice is dispatched", '"voice": lambda a:' in app)
    check("wiring: /voice is documented", '"/voice [on|off]"' in app)
    check("wiring: /voice is completed", '("/voice",' in comp)
    check("config: --no-voice is wired",
          "--no-voice" in (ROOT / "zimzilla" / "__main__.py").read_text())


def main() -> int:
    test_summary()
    test_clean()
    test_voice_object()
    test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
