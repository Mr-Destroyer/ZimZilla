"""A small voice for the shell: launch greeting and end-of-turn recap.

The voice is deliberately *not* another model call. A turn already produces
everything needed to say what happened — the tools it called, the files it
touched, whether anything failed, and the answer it ended on. Summarising that
locally is instant, free, and cannot invent work.

Speech is optional and degrades to silence. The primary backend is Edge's
neural TTS (``edge-tts``) played through ``mpv`` or ``ffplay``; ``spd-say`` and
``espeak-ng`` are fallbacks for a machine that has them. Nothing blocks the
UI: synthesis and playback run on a daemon thread, and a newer utterance
interrupts the old one. A machine with no backend, no audio server, or no TTY
simply stays quiet.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

DEFAULT_VOICE = "en-GB-RyanNeural"
DEFAULT_RATE = "+6%"

#: Spoken text is bounded. A recap is a sentence, not the transcript.
MAX_SPEECH_CHARS = 320

#: A secret accidentally quoted in a final answer must not be read aloud.
_SECRET_RE = re.compile(
    r"\b(?:sk-|lfu_|tj_|unr_sk_|nvapi-|ix_|inf_|rp_|AIza)[A-Za-z0-9._\-]{8,}",
    re.I,
)


def clean_speech(text: str, limit: int = MAX_SPEECH_CHARS) -> str:
    """Turn a markdown answer into one speakable paragraph."""
    text = str(text or "")
    text = re.sub(r"```.*?```", " code block ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = _SECRET_RE.sub("a secret", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", text, flags=re.M)
    text = re.sub(r"^[ \t]*[>*+\-][ \t]+", "", text, flags=re.M)
    text = text.replace("**", "").replace("__", "").replace("*", "")
    text = re.sub(r"\|", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # Prefer ending on a sentence boundary; otherwise a word boundary.
    for mark in (". ", "! ", "? ", "; "):
        at = cut.rfind(mark)
        if at >= limit // 2:
            return cut[: at + 1].strip()
    at = cut.rfind(" ")
    return (cut[:at] if at > 0 else cut).strip() + "…"


def _first_sentence(text: str, limit: int = 180) -> str:
    text = clean_speech(text, limit=limit * 2)
    match = re.match(r"(.+?[.!?])(?:\s|$)", text)
    sentence = match.group(1) if match else text
    if len(sentence) > limit:
        sentence = sentence[:limit].rsplit(" ", 1)[0] + "…"
    return sentence.strip()


def turn_summary(facts: dict, final_text: str = "", *,
                 cancelled: bool = False, error: str = "") -> str:
    """The sentence spoken when a turn goes idle.

    ``facts`` carries ``tools`` and ``files`` collected by the UI. The result
    is intentionally plain: "Sir, I did the work. I edited 2 files and ran 4
    tools. The tests pass."
    """
    if cancelled:
        return "Sir, I stopped as you asked."
    if error:
        clean = clean_speech(error, limit=140)
        if clean:
            clean = clean[0].upper() + clean[1:]
        return f"Sir, I hit a problem. {clean}".strip()
    tools = [str(t) for t in (facts.get("tools") or []) if t]
    files = [str(f) for f in (facts.get("files") or []) if f]
    bits: list[str] = []
    if files:
        bits.append(f"I worked on {len(files)} file"
                    f"{'s' if len(files) != 1 else ''}")
    if tools:
        bits.append(f"I ran {len(tools)} tool{'s' if len(tools) != 1 else ''}")
    sentence = _first_sentence(final_text)
    if sentence:
        bits.append(sentence)
    if not bits:
        bits.append("the task is complete")
    lead = "Sir, I did the work." if (files or tools) else "Sir, I finished."
    body = ". ".join(bits)
    if not body.endswith((".", "!", "?", "…")):
        body += "."
    return clean_speech(lead + " " + body)


class Voice:
    """One non-blocking speaker, newest utterance wins."""

    def __init__(self, *, enabled: bool = True, state_dir=None,
                 voice_name: str = DEFAULT_VOICE, rate: str = DEFAULT_RATE):
        self.enabled = bool(enabled)
        self.voice_name = voice_name or DEFAULT_VOICE
        self.rate = rate or DEFAULT_RATE
        self.state_dir = Path(state_dir) if state_dir else None
        self._gen = 0
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._error_reported = False
        self._edge = self._load_edge()
        self._player = self._find_player()
        self._fallback = self._find_fallback()

    # ---- capability -------------------------------------------------------
    @staticmethod
    def _load_edge():
        try:
            import edge_tts
            return edge_tts
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _find_player() -> list[str] | None:
        mpv = shutil.which("mpv")
        if mpv:
            return [mpv, "--no-video", "--really-quiet", "--no-terminal"]
        ffplay = shutil.which("ffplay")
        if ffplay:
            return [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet"]
        return None

    @staticmethod
    def _find_fallback() -> str | None:
        return shutil.which("spd-say") or shutil.which("espeak-ng")

    def available(self) -> bool:
        return bool((self._edge and self._player) or self._fallback)

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        if not self.enabled:
            self.stop()

    def _can_speak(self) -> bool:
        if not self.enabled or not self.available():
            return False
        # A redirected stdout is a test, a pipe, or a service — none of which
        # wants a voice. The real TUI runs on a tty.
        try:
            return bool(sys.__stdout__ and sys.__stdout__.isatty())
        except Exception:  # noqa: BLE001
            return False

    # ---- speaking ---------------------------------------------------------
    def say(self, text: str, *, interrupt: bool = True) -> bool:
        """Speak *text* without blocking. Returns whether speech started."""
        text = clean_speech(text)
        if not text or not self._can_speak():
            return False
        with self._lock:
            self._gen += 1
            gen = self._gen
            if interrupt:
                self._kill_player()
            if self._edge and self._player:
                threading.Thread(target=self._edge_worker, args=(text, gen),
                                 daemon=True).start()
                return True
            if self._fallback:
                threading.Thread(target=self._fallback_worker,
                                 args=(text, gen), daemon=True).start()
                return True
        return False

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self._kill_player()

    def shutdown(self) -> None:
        self.stop()

    def _kill_player(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _edge_worker(self, text: str, gen: int) -> None:
        path = None
        try:
            fd, name = tempfile.mkstemp(prefix="zimzilla-voice-", suffix=".mp3")
            path = Path(name)
            # mkstemp opened it; close before edge-tts writes it.
            try:
                import os
                os.close(fd)
            except OSError:
                pass
            asyncio.run(self._synthesise(text, path))
            with self._lock:
                if gen != self._gen:
                    return
                proc = subprocess.Popen(
                    [*self._player, str(path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL)
                self._proc = proc
            proc.wait()
        except Exception as e:  # noqa: BLE001 - a voice must never break a turn
            self._note_error(e)
        finally:
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
            with self._lock:
                if self._proc is not None and self._proc.poll() is not None:
                    self._proc = None

    async def _synthesise(self, text: str, path: Path) -> None:
        communicator = self._edge.Communicate(
            text, self.voice_name, rate=self.rate)
        await communicator.save(str(path))

    def _fallback_worker(self, text: str, gen: int) -> None:
        try:
            with self._lock:
                if gen != self._gen:
                    return
                proc = subprocess.Popen(
                    [self._fallback, text],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL)
                self._proc = proc
            proc.wait()
        except Exception as e:  # noqa: BLE001
            self._note_error(e)
        finally:
            with self._lock:
                if self._proc is not None and self._proc.poll() is not None:
                    self._proc = None

    def _note_error(self, exc: Exception) -> None:
        if self._error_reported:
            return
        self._error_reported = True
        # No transcript noise: a missing audio backend is not a failed turn.
        # The first failure is kept for /voice status.
        self.last_error = f"{type(exc).__name__}: {exc}"

    last_error: str = ""
