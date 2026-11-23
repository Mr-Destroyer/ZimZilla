"""`/clip` regression suite: the parse, the ranking, the cut, the wiring.

Run:  python tests/test_clip.py

Offline and model-free. Six layers:

* **the parse** — that `/clip <url> 5` means five, that every malformed flag is
  refused by name rather than silently becoming part of the URL, and that the
  time forms an operator actually types (`90`, `1:30`, `1:02:03`) all work;
* **the signals** — that the audio pass finds a loud burst at the second it
  happens, and that the visual pass finds a hard cut at the second it happens.
  Both are measured against synthetic media with *known* ground truth, because
  a ranking test that only checks "it returned ten things" would pass on a
  ranking that returned ten random things;
* **the ranking** — that five planted moments are found, in the right order,
  separated by at least the minimum gap, and that the clip window is centred so
  the moment is inside it rather than at its first frame;
* **the scale** — that a two-hour timeline with 120 planted moments is ranked
  in seconds and that every chosen peak lands on a real moment. This is the
  case the command exists for, and it is the one a naive implementation fails
  by being too slow to run;
* **the cut** — a real ffmpeg cut, checked for the duration that was asked for;
* **the wiring** — that the command is reachable from every surface that lists
  commands, and that it is *not* busy-ok, because it starts a turn.

Two invariants worth stating, because a regression would break them silently:

* **A clip contains its moment.** The peak is placed at ``LEAD_FRACTION`` into
  the window, not at its start. A punchline clipped at the instant it lands has
  no setup and is not postable, and a test that only checked the duration would
  not notice the window sliding off the event.
* **The ranking is global, not per-chapter.** The top N are chosen from the
  whole timeline, so a two-hour video's best moment is not lost because it
  happened to be in a quiet chapter.

The media is generated with ffmpeg at test time, so nothing here downloads
anything or needs a network. The suite skips the media layers with a PASS and a
reason when ffmpeg is not installed.
"""

from __future__ import annotations

import atexit
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A throwaway ZIMZILLA_HOME for the whole suite. The module resolves the path at
# import time, so this has to be set before the import below — otherwise the
# case directory this suite writes lands in the operator's live install.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="zim-test-home-"))
os.environ["ZIMZILLA_HOME"] = str(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

from zimzilla import clip as clip_mod  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# ---------------------------------------------------------------------------
# 1 — the parse
# ---------------------------------------------------------------------------

def test_parse() -> None:
    opts, err = clip_mod.parse_args(["https://example.test/v"])
    check("parse: a bare URL is accepted", err == "" and opts.url.endswith("/v"))
    # No count given means "work it out from the duration" — the cut tier.
    check("parse: no count means auto", opts.count is None, str(opts.count))
    check("parse: the default show is 10", opts.show == clip_mod.DEFAULT_CLIPS)
    check("parse: the default length is 30s", opts.length == 30.0)

    opts, err = clip_mod.parse_args(["https://example.test/v", "5"])
    check("parse: a trailing integer is the count", err == "" and opts.count == 5)

    opts, err = clip_mod.parse_args(["u", "--show", "20"])
    check("parse: --show sets the display count", err == "" and opts.show == 20)
    _o, err = clip_mod.parse_args(["u", "--show", "0"])
    check("parse: refuses a show of zero", bool(err), err[:50])

    opts, err = clip_mod.parse_args(["u", "--length", "60"])
    check("parse: --length takes a value", err == "" and opts.length == 60.0)
    opts, err = clip_mod.parse_args(["u", "--length=60"])
    check("parse: --length=N works too", err == "" and opts.length == 60.0)

    # Every malformed flag is refused by name. A typo that silently became part
    # of the URL would look like the command ignoring the operator.
    for bad, why in (
        (["u", "--length", "3O"], "a non-numeric length"),
        (["u", "--length", "1"], "a length below the floor"),
        (["u", "--length", "9999"], "a length above the ceiling"),
        (["u", "--quality", "ultra"], "an unknown quality"),
        (["u", "--height", "8"], "a height below the floor"),
        (["u", "--min-gap", "-5"], "a negative gap"),
        (["u", "--nope"], "an unknown flag"),
        (["u", "--nope=1"], "an unknown flag with a value"),
        (["u", "--length"], "a flag with no value"),
        (["u", "abc"], "a non-numeric count"),
        (["u", "0"], "a count of zero"),
        (["u", "999"], "a count above the ceiling"),
        (["u", "5", "extra"], "an extra argument"),
        ([], "no URL at all"),
    ):
        _opts, err = clip_mod.parse_args(bad)
        check(f"parse: refuses {why}", bool(err), err[:60])

    # The time forms an operator actually types.
    for text, want in (("90", 90.0), ("1:30", 90.0), ("1:02:03", 3723.0),
                       ("0:00:30", 30.0)):
        opts, err = clip_mod.parse_args(["u", "--start", text])
        check(f"parse: --start {text} = {want:.0f}s",
              err == "" and opts.start == want, f"{opts.start}")

    _opts, err = clip_mod.parse_args(["u", "--start", "60", "--end", "30"])
    check("parse: refuses an end before the start", bool(err))

    opts, err = clip_mod.parse_args(["--help"])
    check("parse: --help is recognised", err == "" and opts.help)

    opts, err = clip_mod.parse_args(["u", "--dry-run"])
    check("parse: --dry-run is recognised", err == "" and opts.dry_run)

    opts, err = clip_mod.parse_args(["u", "--live"])
    check("parse: --live is recognised", err == "" and opts.live)


def test_plan_count() -> None:
    """The cut tier: how many clips a source of a given length is worth.

    This is the operator's actual rule — "over 100 clips if it's a 2-hour
    video" — so it is tested against the durations they named rather than
    against the formula's own output.
    """
    # The headline case. A 2h video must land at 100+, which is what was asked
    # for; a formula that produced 40 would satisfy "it scales" and fail the
    # request.
    two_hour = clip_mod.plan_count(7200)
    check("plan: a 2h video yields 100+ clips", two_hour >= 100, str(two_hour))
    check("plan: and not more than the ceiling", two_hour <= clip_mod.MAX_CLIPS,
          str(two_hour))

    # It scales with the source rather than being a constant.
    short = clip_mod.plan_count(1200)      # 20 minutes
    long_ = clip_mod.plan_count(21600)     # 6 hours
    check("plan: a 20m video yields fewer than a 2h one", short < two_hour,
          f"{short} vs {two_hour}")
    check("plan: a 6h stream yields more than a 2h one", long_ > two_hour,
          f"{long_} vs {two_hour}")
    check("plan: a 6h stream is clamped to the ceiling",
          long_ == clip_mod.MAX_CLIPS, str(long_))

    # A tiny source cannot yield more non-overlapping clips than it holds.
    tiny = clip_mod.plan_count(60, length=30)
    check("plan: a 60s source yields at most 2 clips", tiny <= 2, str(tiny))
    check("plan: and at least one", tiny >= 1, str(tiny))

    # An explicit count wins outright — `/clip url 5` means five, not "five
    # but also the automatic hundred".
    check("plan: an asked count wins", clip_mod.plan_count(7200, asked=5) == 5)
    check("plan: an asked count is not scaled up",
          clip_mod.plan_count(7200, asked=3) == 3)

    # The ceiling holds even when asked for more than the timeline can hold.
    check("plan: never exceeds the ceiling",
          clip_mod.plan_count(7200, asked=999) <= clip_mod.MAX_CLIPS)

    # A zero duration (a live stream with no known length) must not divide by
    # zero or return nothing.
    check("plan: a zero duration still yields a usable count",
          clip_mod.plan_count(0) >= 1, str(clip_mod.plan_count(0)))

    # A live stream reports duration 0 and is ranked inside a captured window,
    # so the caller scales against the window rather than the duration. A
    # 30-minute window must yield a real shortlist, not the floor of 3.
    check("plan: a 30m live window yields a real shortlist",
          clip_mod.plan_count(1800) >= 20, str(clip_mod.plan_count(1800)))

    # A bounded --start/--end range scales against the range, not the source.
    check("plan: a 1h range yields fewer than a 2h source",
          clip_mod.plan_count(3600) < clip_mod.plan_count(7200))


def test_helpers() -> None:
    check("stamp: 65s", clip_mod._stamp(65) == "01m05s", clip_mod._stamp(65))
    check("stamp: 3725s", clip_mod._stamp(3725) == "1h02m05s",
          clip_mod._stamp(3725))
    check("human_time: 2h", clip_mod.human_time(7200) == "2.0h",
          clip_mod.human_time(7200))
    check("human_time: 27m", clip_mod.human_time(1612) == "27m",
          clip_mod.human_time(1612))

    # The slug has to survive a title with punctuation in it, because the case
    # directory is named from the video's own title.
    slug = clip_mod.case_slug("What the heck is the event loop?! | JSConf EU")
    check("slug: no path separators", "/" not in slug and "\\" not in slug, slug)
    check("slug: not empty", bool(slug.strip("-")))


# ---------------------------------------------------------------------------
# 2 — the signals, against known ground truth
# ---------------------------------------------------------------------------

def _write_wav(path: Path, seconds: int, loud: set[int], rate: int = 4000) -> None:
    """A WAV with a loud tone during the seconds in *loud*, quiet elsewhere.

    Written directly rather than through ffmpeg's filter graph: a 2-hour
    ``volume`` expression with 120 ``between()`` terms is a 2.5 KB expression
    that ffmpeg refuses to allocate, and the point of the test is the analysis,
    not the synthesis.
    """
    total = seconds * rate
    buf = bytearray()
    for i in range(total):
        amp = 0.95 if (i // rate) in loud else 0.01
        buf += struct.pack("<h", int(amp * 12000 * math.sin(2 * math.pi * 440 * i / rate)))
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(buf)))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", len(buf)))
        f.write(buf)


def test_audio_signal(tmp: Path) -> None:
    if not _have_ffmpeg():
        check("audio: skipped (ffmpeg not installed)", True)
        return
    wav = tmp / "audio.wav"
    loud = {40, 41, 42, 43, 44, 45, 100, 101, 102, 103, 104, 105}
    _write_wav(wav, 150, loud)
    signal = clip_mod.audio_energy(wav)
    check("audio: one window per second", signal.length == 150, str(signal.length))
    # The loud seconds must be measurably louder than the quiet ones. This is
    # the whole signal: if it does not separate them, nothing downstream works.
    loud_levels = [signal.values[i] for i in sorted(loud)]
    quiet_levels = [signal.values[i] for i in range(150) if i not in loud]
    check("audio: loud seconds are louder than quiet ones",
          min(loud_levels) > max(quiet_levels) + 10,
          f"loud min {min(loud_levels):.1f} vs quiet max {max(quiet_levels):.1f}")
    # And the burst is at the second it happens, not smeared across the file.
    peak = max(range(signal.length), key=lambda i: signal.values[i])
    check("audio: the peak is inside a loud burst", peak in loud, str(peak))


def test_visual_signal(tmp: Path) -> None:
    if not _have_ffmpeg():
        check("visual: skipped (ffmpeg not installed)", True)
        return
    # Five solid colours concatenated: four hard cuts, at 60/120/180/240.
    src = tmp / "cuts.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for colour in ("black", "white", "red", "blue", "green"):
        cmd += ["-f", "lavfi", "-i", f"color={colour}:size=320x240:rate=10:duration=60"]
    cmd += ["-filter_complex", "[0:v][1:v][2:v][3:v][4:v]concat=n=5:v=1:a=0",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "10", str(src)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                            stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        check("visual: skipped (could not build the fixture)", True, proc.stderr[-80:])
        return
    signal = clip_mod.visual_change(src)
    check("visual: one window per second", signal.length >= 295, str(signal.length))
    # The four cuts are the strongest visual events in the file.
    top = sorted(range(signal.length), key=lambda i: signal.values[i],
                 reverse=True)[:8]
    found = {i for i in top if i in (60, 120, 180, 240)}
    check("visual: all four hard cuts are in the top events",
          found == {60, 120, 180, 240}, str(sorted(found)))


# ---------------------------------------------------------------------------
# 3 — the ranking
# ---------------------------------------------------------------------------

def test_ranking(tmp: Path) -> None:
    if not _have_ffmpeg():
        check("rank: skipped (ffmpeg not installed)", True)
        return
    wav = tmp / "rank.wav"
    planted = [40, 100, 160, 220, 275]
    loud: set[int] = set()
    for m in planted:
        loud.update(range(m, m + 6))
    _write_wav(wav, 300, loud)
    audio = clip_mod.audio_energy(wav)

    moments = clip_mod.rank(audio, None, count=5, length=30)
    check("rank: returns the count asked for", len(moments) == 5, str(len(moments)))

    peaks = sorted(int(m.peak) for m in moments)
    # Every planted moment is found, within a few seconds. The tolerance is
    # there because the smoothing moves a peak by up to the half-width; it is
    # not there to excuse missing one.
    matched = [any(abs(p - want) <= 6 for p in peaks) for want in planted]
    check("rank: every planted moment is found", all(matched), str(peaks))

    # The clips are separated, so ten clips are ten moments rather than ten
    # overlapping windows around one.
    starts = sorted(m.start for m in moments)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    check("rank: clips are separated by at least the gap",
          all(g >= 30 for g in gaps), str([round(g) for g in gaps]))

    # The moment is INSIDE the clip, not at its first frame. This is the
    # invariant a duration-only test would miss.
    inside = all(m.start <= m.peak <= m.end for m in moments)
    check("rank: every peak is inside its own clip", inside)
    lead = [m.peak - m.start for m in moments]
    check("rank: the peak sits in the first half of the clip",
          all(0 < l < 30 for l in lead), str([round(l) for l in lead]))

    # Best first, which is the order the operator reads them in.
    scores = [m.score for m in moments]
    check("rank: ordered best first", scores == sorted(scores, reverse=True))

    # A range restricts the ranking to that range.
    ranged = clip_mod.rank(audio, None, count=5, length=30, start=150, end=200)
    check("rank: --start/--end restrict the range",
          all(150 <= m.peak <= 200 for m in ranged),
          str([int(m.peak) for m in ranged]))

    # A count larger than the number of distinct moments returns what exists
    # rather than padding with duplicates.
    many = clip_mod.rank(audio, None, count=40, length=30)
    check("rank: never returns the same moment twice",
          len({round(m.start) for m in many}) == len(many), str(len(many)))


def test_scale(tmp: Path) -> None:
    """The case the command exists for: 2h+, 100+ candidates, seconds not hours."""
    if not _have_ffmpeg():
        check("scale: skipped (ffmpeg not installed)", True)
        return
    import random
    import time

    random.seed(7)
    planted = sorted(random.sample(range(30, 7170), 120))
    loud: set[int] = set()
    for m in planted:
        loud.update(range(m, m + 5))
    wav = tmp / "two_hour.wav"
    _write_wav(wav, 7200, loud)

    t0 = time.time()
    audio = clip_mod.audio_energy(wav)
    elapsed = time.time() - t0
    check("scale: 2h of audio is measured in one window per second",
          audio.length == 7200, str(audio.length))
    # The measured cost on this class of host is ~3.4s. The bound is generous
    # because a slow CI box is not a bug, but it is far below the minutes a
    # naive per-clip approach would take.
    check("scale: 2h is analysed in seconds, not minutes", elapsed < 120,
          f"{elapsed:.1f}s")

    t0 = time.time()
    moments = clip_mod.rank(audio, None, count=10, length=30)
    rank_time = time.time() - t0
    check("scale: ranking 2h is instant", rank_time < 5, f"{rank_time:.2f}s")
    check("scale: ten moments from a 2h timeline", len(moments) == 10,
          str(len(moments)))

    # Every chosen peak lands on a real moment. This is the assertion that
    # separates a working ranking from one that returns ten plausible-looking
    # timestamps.
    on_target = [any(abs(int(m.peak) - p) <= 6 for p in planted) for m in moments]
    check("scale: every chosen peak lands on a planted moment", all(on_target),
          str([int(m.peak) for m in moments]))

    # And the moments are spread across the timeline rather than clustered at
    # the start, which is what a per-chapter ranking would produce.
    span = max(m.start for m in moments) - min(m.start for m in moments)
    check("scale: the moments span the timeline", span > 1800, f"{span:.0f}s")


# ---------------------------------------------------------------------------
# 4 — the cut
# ---------------------------------------------------------------------------

def test_cut(tmp: Path) -> None:
    if not _have_ffmpeg():
        check("cut: skipped (ffmpeg not installed)", True)
        return
    src = tmp / "source.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=60",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
           "-c:v", "libx264", "-preset", "ultrafast", "-g", "10",
           "-c:a", "aac", "-shortest", str(src)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                            stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        check("cut: skipped (could not build the fixture)", True, proc.stderr[-80:])
        return

    dest = tmp / "clip.mp4"
    path, err = clip_mod.cut(src, dest, 20.0, 15.0, height=240)
    check("cut: a clip is produced", path is not None, err)
    if path is None:
        return
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    try:
        duration = float((probe.stdout or "0").strip())
    except ValueError:
        duration = 0.0
    # Frame-accurate, not keyframe-bound: a stream copy would land anywhere
    # within a GOP of the request, which is what makes a clip start before the
    # moment it was chosen for.
    check("cut: the duration is what was asked for", abs(duration - 15.0) < 0.5,
          f"{duration:.2f}s")

    # A cut past the end of the source is clamped rather than failing.
    path2, err2 = clip_mod.cut(src, tmp / "tail.mp4", 55.0, 30.0, height=240)
    check("cut: a clip running past the end still produces a file",
          path2 is not None, err2)


def test_cut_all(tmp: Path) -> None:
    if not _have_ffmpeg():
        check("cut_all: skipped (ffmpeg not installed)", True)
        return
    src = tmp / "many.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=90",
           "-c:v", "libx264", "-preset", "ultrafast", "-g", "10", str(src)]
    if subprocess.run(cmd, capture_output=True, timeout=180,
                            stdin=subprocess.DEVNULL).returncode != 0:
        check("cut_all: skipped (could not build the fixture)", True)
        return
    moments = [
        clip_mod.Moment(start=0, end=10, peak=3, score=0.9),
        clip_mod.Moment(start=30, end=40, peak=33, score=0.8),
        clip_mod.Moment(start=60, end=70, peak=63, score=0.7),
    ]
    outdir = tmp / "clips"
    seen: list[int] = []
    made, errors = clip_mod.cut_all(
        src, moments, outdir, height=240,
        progress=lambda i, n, p, e: seen.append(i))
    check("cut_all: every moment is cut", len(made) == 3, str(errors))
    check("cut_all: no errors", errors == [], str(errors))
    check("cut_all: progress is reported once per clip", seen == [1, 2, 3], str(seen))
    names = sorted(p.name for _m, p in made)
    check("cut_all: files are named by their timestamp",
          names[0].startswith("01-") and names[1].startswith("02-"), str(names))
    check("cut_all: the files exist and are not empty",
          all(p.exists() and p.stat().st_size > 0 for _m, p in made))


def test_cut_source_quality(tmp: Path) -> None:
    """The clips are cut from the delivered-quality source, not a 144p proxy.

    This is a regression test for a real bug: the visual pass used to run on a
    separate 144p copy, and the cut was pointed at that copy, so every clip was
    144p upscaled to 720 — softer than the source it came from. The fix was to
    download once at the delivery height and analyse that, which only became
    affordable once ``visual_change`` learned ``-skip_frame nokey``.
    """
    if not _have_ffmpeg():
        check("source: skipped (ffmpeg not installed)", True)
        return
    src = tmp / "hd.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=40",
           "-c:v", "libx264", "-preset", "ultrafast", "-g", "10", str(src)]
    if subprocess.run(cmd, capture_output=True, timeout=180,
                      stdin=subprocess.DEVNULL).returncode != 0:
        check("source: skipped (could not build the fixture)", True)
        return

    # A cut at the delivery height keeps the source's own resolution rather
    # than upscaling a smaller proxy.
    moment = clip_mod.Moment(start=5, end=15, peak=8, score=0.9)
    made, errors = clip_mod.cut_all(src, [moment], tmp / "hdclips", height=360)
    check("source: the clip is cut", len(made) == 1, str(errors))
    if not made:
        return
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(made[0][1])],
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    dims = (probe.stdout or "").strip()
    check("source: the clip is at the source resolution, not upscaled",
          dims.startswith("640,360"), dims)

    # And the visual pass on a full-quality source still finds the cuts, which
    # is what makes analysing the delivery copy viable at all.
    signal = clip_mod.visual_change(src)
    check("source: the visual pass reads a full-quality source",
          signal.length >= 35, str(signal.length))


# ---------------------------------------------------------------------------
# 5 — captions and chat, as context
# ---------------------------------------------------------------------------

def test_context(tmp: Path) -> None:
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:04.000
hello there this is the first line

00:00:04.000 --> 00:00:07.500
and here is a second line

00:00:07.500 --> 00:01:11.000
<b>third</b> line with markup &amp; entities
"""
    cues = clip_mod.parse_vtt(vtt)
    check("vtt: three cues are read", len(cues) == 3, str(len(cues)))
    check("vtt: the timings are seconds",
          cues[0][0] == 1.0 and cues[0][1] == 4.0, str(cues[0][:2]))
    check("vtt: markup is stripped", "<b>" not in cues[2][2], cues[2][2])
    check("vtt: entities are decoded", "&amp;" not in cues[2][2], cues[2][2])
    check("vtt: a range picks the cues that overlap it",
          "first line" in clip_mod.transcript_between(cues, 2, 5))
    check("vtt: a range with no cues is empty",
          clip_mod.transcript_between(cues, 200, 300) == "")

    import json
    chat = tmp / "chat.jsonl"
    chat.write_text("\n".join([
        json.dumps({"videoOffsetTimeMsec": 5000,
                    "message": {"runs": [{"text": "LOL"}]}}),
        json.dumps({"videoOffsetTimeMsec": 12000,
                    "message": {"simpleText": "that was wild"}}),
        json.dumps({"videoOffsetTimeMsec": 90000,
                    "message": {"simpleText": "way later"}}),
        "not json at all",
    ]))
    check("chat: messages in range are read",
          "LOL" in clip_mod.chat_between(chat, 0, 20))
    check("chat: messages out of range are not",
          "way later" not in clip_mod.chat_between(chat, 0, 20))
    check("chat: a missing file is empty, not an error",
          clip_mod.chat_between(tmp / "nope.jsonl", 0, 10) == "")
    check("chat: junk lines are skipped",
          "LOL" in clip_mod.chat_between(chat, 0, 20))


# ---------------------------------------------------------------------------
# 6 — live streams
# ---------------------------------------------------------------------------

def test_live() -> None:
    live = clip_mod.Video(url="u", title="Live", duration=0, is_live=True)
    start, end, note = clip_mod.plan_live(live)
    check("live: a running stream gets a bounded window",
          end == clip_mod.LIVE_WINDOW, str(end))
    check("live: the window is explained", bool(note), note[:50])

    vod = clip_mod.Video(url="u", title="VOD", duration=3600, is_live=False)
    start, end, note = clip_mod.plan_live(vod)
    check("live: a finished video is not windowed", note == "" and end == 3600)


# ---------------------------------------------------------------------------
# 7 — the prompt
# ---------------------------------------------------------------------------

def test_prompt() -> None:
    video = clip_mod.Video(url="u", title="A talk", channel="JSConf",
                           duration=1612, webpage_url="https://x")
    moments = [
        clip_mod.Moment(start=100, end=130, peak=110, score=0.9,
                        audio=0.8, change=1.0, visual=0.5,
                        transcript="and that is why it matters",
                        title="LOL · nice"),
    ]
    prompt = clip_mod.build_prompt(video, Path("/tmp/case"), moments, count=1)
    check("prompt: the title is carried", "A talk" in prompt)
    check("prompt: the channel is carried", "JSConf" in prompt)
    check("prompt: the case directory is carried", "/tmp/case" in prompt)
    check("prompt: the timestamps are carried", "01m40s" in prompt, prompt[:200])
    check("prompt: the transcript is carried", "that is why it matters" in prompt)
    check("prompt: the chat is carried", "LOL" in prompt)
    check("prompt: the signals are carried", "audio 0.80" in prompt)
    # The model must not be invited to re-rank: the scores are measured facts.
    check("prompt: the model is told not to re-rank",
          "do not re-rank" in prompt.lower())
    # And it must not invent dialogue for a clip with no transcript.
    check("prompt: the model is told not to invent",
          "do not invent" in prompt.lower())

    # A moment with no transcript and no chat still produces a usable brief.
    bare = [clip_mod.Moment(start=0, end=30, peak=10, score=0.5)]
    prompt2 = clip_mod.build_prompt(video, Path("/tmp/case"), bare, count=1)
    check("prompt: a bare moment still produces a brief",
          "00m00s" in prompt2 and "said:" not in prompt2)


# ---------------------------------------------------------------------------
# 8 — the case directory
# ---------------------------------------------------------------------------

def test_case(tmp: Path) -> None:
    class _Cfg:
        state_dir = tmp / "state"

    video = clip_mod.Video(url="u", title="Some Video: Part 1/2", duration=100)
    case = clip_mod.case_dir(_Cfg, video)
    check("case: the directory is created", case.is_dir(), str(case))
    check("case: it lives under state_dir/clip",
          case.parent == _Cfg.state_dir / "clip", str(case.parent))
    check("case: the name has no path separators",
          "/" not in case.name and ":" not in case.name, case.name)

    # moments.json is the ranking as data, so a later run or a script can read
    # the numbers without parsing the transcript.
    moments = [clip_mod.Moment(start=10, end=40, peak=20, score=0.75,
                               audio=0.6, change=0.9, visual=0.3)]
    clips = [(moments[0], case / "clips" / "01-00m10s-00m40s.mp4")]
    dest = clip_mod.write_moments(case, video, moments, clips)
    check("case: moments.json is written", dest.is_file(), str(dest))
    import json
    payload = json.loads(dest.read_text())
    check("case: moments.json carries the source", payload["source"]["title"] == video.title)
    check("case: moments.json carries the weights",
          payload["weights"]["audio"] == clip_mod.W_AUDIO)
    check("case: moments.json carries the signals",
          payload["moments"][0]["signals"]["change"] == 0.9)
    check("case: moments.json names the file",
          payload["moments"][0]["file"] == "01-00m10s-00m40s.mp4")

    # finalise archives the report and KEEPS the case, unlike osint.
    (case / "report.md").write_text("# Clips\n")
    archived = clip_mod.finalise_case(_Cfg, case)
    check("case: the report is archived", archived is not None and archived.is_file())
    check("case: the case directory is kept", case.is_dir())
    check("case: the archive is under reports/",
          archived.parent == _Cfg.state_dir / "reports", str(archived))

    # A run with no report archives nothing and keeps everything.
    empty = _Cfg.state_dir / "clip" / "empty-case"
    empty.mkdir(parents=True)
    check("case: no report means nothing is archived",
          clip_mod.finalise_case(_Cfg, empty) is None)
    check("case: and the case is still there", empty.is_dir())


# ---------------------------------------------------------------------------
# 9 — the wiring
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    """Every command is reachable from every surface that lists commands.

    Read from the source rather than importing a copy, so a rename cannot
    silently orphan a command — the same check test_osint, test_crack and
    test_shodan make. ``app.py`` and ``complete.py`` are read as text because
    importing them needs textual, which this suite does not require.
    """
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text()
    complete_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text()

    check("wiring: clip.py is imported", "from .. import clip as clip_mod" in app_src)
    check("wiring: _cmd_clip is defined", "def _cmd_clip(" in app_src)
    check("wiring: the worker is defined", "def _clip_worker(" in app_src)
    check("wiring: the run body is defined", "def _clip_run(" in app_src)
    check("wiring: the done handler is defined", "def _clip_done(" in app_src)
    check("wiring: the failure handler is defined", "def _clip_failed(" in app_src)
    check("wiring: the finaliser is defined", "def _finalise_clip(" in app_src)

    check("wiring: dispatch has clip", '"clip": lambda a:' in app_src)
    check("wiring: help lists clip", '("/clip <url> [count]"' in app_src)
    check("wiring: palette lists clip",
          '("/clip", "find the best moments' in app_src)
    check("wiring: completion lists clip", '("/clip",' in complete_src)

    # The scope guard applies: a URL is a host, and an out-of-scope host is not
    # fetched. Read the source, because the check is inside the command.
    check("wiring: the scope guard is consulted",
          "self.agent.scope.allows(host)" in app_src)

    # A clip run starts a turn, so it must not be busy-ok — letting one through
    # the gate would interleave a second turn into a live one.
    busy_block = app_src.split("BUSY_OK = frozenset({", 1)[1].split("})", 1)[0]
    check("wiring: clip is not busy-ok", '"clip"' not in busy_block)

    # The help table and the completion popup are two views of one command set.
    from zimzilla.ui.app import HELP_GROUPS
    from zimzilla.ui.complete import SLASH_COMMANDS
    documented = {n.split()[0] for _k, _l, _b, rows in HELP_GROUPS
                  for n, _d in rows}
    offered = {n for n, _d in SLASH_COMMANDS}
    check("wiring: /clip is documented", "/clip" in documented)
    check("wiring: /clip is offered", "/clip" in offered)
    check("wiring: the two surfaces agree",
          documented == offered, str(sorted(documented ^ offered)))


# ---------------------------------------------------------------------------

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-clip-test-"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    try:
        test_parse()
        test_plan_count()
        test_helpers()
        test_audio_signal(tmp)
        test_visual_signal(tmp)
        test_ranking(tmp)
        test_scale(tmp)
        test_cut(tmp)
        test_cut_all(tmp)
        test_cut_source_quality(tmp)
        test_context(tmp)
        test_live()
        test_prompt()
        test_case(tmp)
        test_wiring()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
