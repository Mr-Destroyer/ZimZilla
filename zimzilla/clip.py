"""`/clip` — find the moments worth clipping in a video or a finished stream.

Why this is not "cut the video into N pieces"
---------------------------------------------
A two-hour lecture has maybe eight moments anyone would post. Cutting it into
120 equal slices produces 120 clips and no answer, and the operator then has to
watch all of them — which is the work the command was supposed to remove. So the
product here is the **ranking**, and the clips are the evidence for it.

The ranking is computed from the media itself, in two cheap passes, before a
single frame is cut:

* **Audio energy** — one RMS level per second, from a 4 kHz mono downmix. This
  is the single most useful signal for the content people actually clip: a
  laugh, a shout, a punchline, a raised voice, a burst of applause. It costs
  ~2s per ten minutes of video because ``-vn`` skips video decode entirely.
* **Visual change** — scene cuts and frame-to-frame motion, at 1 fps on a
  96px-wide copy, decoding only keyframes. A cut, a slide change, a screen
  share starting, a camera switch: these are where a *visual* moment begins,
  and they are invisible to the audio pass.

Both are measured on the whole timeline, so the ranking is global rather than
per-chapter, and the top N are cut only after the scores are known. That is what
makes "over 100 clips from a 2h+ video" affordable: the expensive step (cutting
and encoding) runs N times, not 100 times.

How many clips
--------------
Two numbers, because the operator asked for two things at once — "cut over 100
clips if it's a 2-hour video" and "show 10":

* **how many are cut** scales with the source, at ``CLIPS_PER_MINUTE``. A
  20-minute video yields ~17, a 2-hour one ~103, a 6-hour stream ~200 (the
  ceiling). Naming a count — ``/clip <url> 5`` — overrides it and cuts exactly
  that many. See :func:`plan_count`.
* **how many are shown** is ``DEFAULT_CLIPS`` (10), or ``--show N``. The rest
  are on disk and in ``moments.json``; the shortlist is the ten worth reading.

One download
------------
The video is fetched once, at the delivery height, and serves three jobs: the
audio ranking, the visual pass, and the cut itself. The visual pass decodes only
keyframes (``-skip_frame nokey``), which measured 10x faster than a full decode
and still finds every hard cut, because a cut is a keyframe by definition. That
is what makes analysing the delivery copy affordable — and it is why the clips
are cut from a real 720p source rather than a 144p analysis proxy.

Why the audio pass is the primary signal
----------------------------------------
Measured on this class of host, per ten minutes of source:

    audio energy, 4 kHz mono, 1s windows     2.2s
    keyframe-only scene detect               2.4s
    keyframe-only motion, 96px, 1 fps        1.4s
    720p download (the one download)        38.0s
    one 30s clip, ultrafast re-encode        2.7s

So scoring a two-hour video costs about a minute, and cutting ten clips costs
about half a minute. The alternative — cutting 100 candidates and ranking the
files — costs five minutes of encoding to produce 100 files nobody asked for.

The keyframe-only passes are what make the *delivery* copy affordable to
analyse. A full decode of 720p measured 13.8s for two minutes; keyframe-only
measured 1.4s, and still found every hard cut in a fixture with four of them at
known timestamps. That is the difference between analysing a 2h video in ~85s
and in ~14 minutes, and it is why there is no separate 144p proxy any more.

What is deliberately *not* a signal
-----------------------------------
**Live chat.** It is the best signal for a livestream and it is not used here,
because it is not reliably available: yt-dlp's ``live_chat`` subtitle track is
absent on ordinary uploads, and on a stream whose chat replay YouTube has
dropped it fails with an extractor error rather than an empty track. A ranking
that silently degrades to "no signal" on half the inputs is worse than one that
never claimed the signal. The chat is still *fetched* when it exists and handed
to the model as context — see :func:`fetch_chat` — but it does not move a score.

**Captions.** Same reasoning, one step weaker: auto-captions are usually there,
but YouTube rate-limits the subtitle endpoint hard (a 429 on the second request
in a row was reproducible while this was written), and a transcript that arrives
for one video in three cannot be the basis of a ranking. When they do arrive
they are used for the clip *title* and for the model's context, not for the
score.

So the score is built only from signals that are always present in the media
itself. Everything else is enrichment.

The score
---------
Each second gets a weighted sum of normalised signals, then the timeline is
smoothed and peaks are picked with a minimum separation, so ten clips are ten
*different* moments rather than ten overlapping windows around one loud one::

    score = 0.55 · audio_energy      (loudness above the video's own median)
          + 0.25 · audio_change      (a rise — the onset of a moment)
          + 0.20 · visual_change     (scene cut or motion spike)

The weights are a judgement, not a measurement, and they are stated here so they
can be argued with. ``audio_energy`` dominates because it is the signal that
survives every content type; ``audio_change`` is what distinguishes a *moment*
from a long loud passage; ``visual_change`` breaks ties on silent content
(slides, gameplay, screen recordings) where audio says nothing.

A clip is centred on its peak and padded, so the moment is inside the clip
rather than at its first frame — a punchline clipped at the instant it lands has
no setup and is not postable.

Live streams
------------
A stream that is still running has no end, so there is nothing to rank against.
``/clip <url>`` on a live URL therefore refuses and says so, rather than
downloading an unbounded stream. A *finished* stream (a VOD, including a
premiere that has ended) is an ordinary video and works normally. The one
exception is ``--live``, which captures a bounded window from the live edge and
ranks inside it — see :func:`plan_live`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: How many clips a bare `/clip <url>` *shows*. Ten is the number the operator
#: named, and it is also about the most a person will actually watch through.
#: This is the display tier, not the cut tier — see ``plan_count``.
DEFAULT_CLIPS = 10

#: The ceiling on what one run will *cut*. A two-hour stream has more than this
#: many good moments, but past a few hundred files the directory is a triage
#: problem rather than a shortlist, and the encode time grows linearly. The
#: operator asked for 100+ on a 2h source, so the ceiling is set where a 2h
#: video's natural count lands rather than below it.
MAX_CLIPS = 200

#: The cut tier: how many clips a source of a given length is worth cutting,
#: before the operator's own count is applied. The operator's rule was "over
#: 100 clips if it's a 2-hour video", which is one clip per ~70s of source.
#: Expressed as a rate so it scales: a 20-minute video yields ~17, a 2-hour one
#: ~103, a 6-hour stream ~309 (clamped to ``MAX_CLIPS``).
CLIPS_PER_MINUTE = 0.86

#: The floor and ceiling on the automatic cut count, so a 90-second clip does
#: not ask for one clip and a 12-hour stream does not ask for 600.
AUTO_MIN_CLIPS = 3

#: Clip length bounds, in seconds. The default is 30s because that is the shape
#: of a postable moment; `--length` moves it, and the bounds stop a typo from
#: asking for a 4-hour clip or a 0.2s one.
DEFAULT_LENGTH = 30.0
MIN_LENGTH = 5.0
MAX_LENGTH = 300.0

#: How much of the clip sits *before* its peak. 0.35 puts the moment in the
#: first third, which leaves the setup in and the reaction after it — a clip
#: that starts on the punchline has no joke in it.
LEAD_FRACTION = 0.35

#: Minimum distance between two chosen peaks, as a fraction of clip length.
#: Without this, the ten highest-scoring seconds of one loud passage become ten
#: overlapping clips of the same moment.
MIN_GAP_FRACTION = 1.0

#: Smoothing half-width, in seconds. A single loud second is usually a door
#: slam; a moment is loud for several seconds. Averaging over ±3s is what makes
#: the peak land on the event rather than on its loudest sample.
SMOOTH_HALF_WIDTH = 3

#: The audio analysis rate. 4 kHz is far below anything musical and far above
#: what is needed for a loudness envelope — it is what makes the pass cheap.
AUDIO_RATE = 4000

#: The video analysis rate and width. 1 fps at 96px wide is enough to see a cut
#: or a slide change and cheap enough to run over two hours in seconds.
VIDEO_FPS = 1
VIDEO_WIDTH = 96

#: Scene-change threshold, in scdet's own units (0-100). 8 was measured to fire
#: on a real cut and not on camera noise; the default of 10 missed soft
#: crossfades, and 5 fired on compression artefacts.
SCENE_THRESHOLD = 8.0

#: The weights. See the module docstring — these are a judgement, stated so
#: they can be argued with, not a fitted model.
W_AUDIO = 0.55
W_CHANGE = 0.25
W_VISUAL = 0.20

#: How long a single yt-dlp call may take. Generous: a 2h 480p download on a
#: slow line is minutes, and a timeout that fires mid-download wastes all of it.
DOWNLOAD_TIMEOUT = 1800.0

#: How long an ffmpeg analysis pass may take. The measured cost is ~2s per ten
#: minutes, so this is ~100x headroom and exists only to stop a wedged process.
ANALYSE_TIMEOUT = 900.0

#: How long one clip encode may take.
CUT_TIMEOUT = 300.0

#: The quality ladder for the delivered clips. `--quality` picks one; the
#: numbers are CRF values, where lower is better and bigger is smaller.
QUALITY = {
    "high": 20,
    "medium": 23,
    "low": 28,
}
DEFAULT_QUALITY = "medium"

#: The height cap for a delivered clip. 720 is the shape people post; a 4K
#: source re-encoded to 720 is a fraction of the size and indistinguishable on
#: a phone.
DEFAULT_HEIGHT = 720

#: How many seconds of a live stream `--live` captures from the live edge.
LIVE_WINDOW = 1800.0

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TS_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[.,](\d{3})")
_TAG_RE = re.compile(r"<[^>]+>")

#: The flags that take a value. Anything else beginning with ``--`` is refused
#: by name, so a typo is reported as a typo rather than as a missing argument.
_VALUE_FLAGS = frozenset({
    "--length", "--quality", "--height", "--min-gap", "--start", "--end", "--out",
    "--show",
})


class ClipError(Exception):
    """A failure the operator should read, not a traceback."""


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

def ytdlp() -> str | None:
    return shutil.which("yt-dlp")


def ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe() -> str | None:
    return shutil.which("ffprobe")


def missing_tools() -> list[str]:
    """The external programs this command needs and cannot find.

    Returned as a list rather than raised, so the caller can name all of them
    at once — an operator who installs one of two and is told about the second
    only on the next run has been made to do the work twice.
    """
    out = []
    if ytdlp() is None:
        out.append("yt-dlp")
    if ffmpeg() is None:
        out.append("ffmpeg")
    if ffprobe() is None:
        out.append("ffprobe")
    return out


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

@dataclass
class Options:
    """What `/clip` was asked for."""

    url: str = ""
    #: ``None`` means "work it out from the duration" — see ``plan_count``.
    #: An explicit count from the operator is stored here and wins outright.
    count: int | None = None
    #: How many of the cut clips to *show* and have the model name. Defaults to
    #: ``DEFAULT_CLIPS``; ``--show N`` moves it. Kept separate from ``count``
    #: because the operator asked to cut 100+ and display 10.
    show: int = DEFAULT_CLIPS
    length: float = DEFAULT_LENGTH
    quality: str = DEFAULT_QUALITY
    height: int = DEFAULT_HEIGHT
    min_gap: float | None = None
    start: float | None = None
    end: float | None = None
    live: bool = False
    dry_run: bool = False
    out: Path | None = None
    help: bool = False


def parse_args(args) -> tuple[Options, str]:
    """Parse `/clip`'s arguments. Returns ``(opts, err)``.

    Split out of the command so the parse is testable on its own, the same shape
    as ``_parse_crack_args`` and ``_parse_payload_args``. Every malformed flag is
    refused here with the value named, rather than silently becoming part of the
    URL — a mistyped ``--length 3O`` that quietly became a 30-second clip would
    look like the command ignoring the operator.
    """
    opts = Options()
    rest: list[str] = []
    argv = [str(a) for a in (args or [])]
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            opts.help = True
            i += 1
            continue
        if a in ("--dry-run", "-n"):
            opts.dry_run = True
            i += 1
            continue
        if a == "--live":
            opts.live = True
            i += 1
            continue
        if a.startswith("--"):
            name, _, inline = a.partition("=")
            # A flag that takes no value is checked before the value is read,
            # so `--nope` is reported as an unknown flag rather than as a flag
            # that is missing its argument — the second message sends the
            # operator looking for a value to supply to a flag that does not
            # exist.
            if name not in _VALUE_FLAGS:
                return opts, f"unknown flag {name} — /clip --help lists them"
            if inline:
                value = inline
                i += 1
            else:
                if i + 1 >= len(argv):
                    return opts, f"{name} needs a value"
                value = argv[i + 1]
                i += 2
            if name == "--length":
                try:
                    opts.length = float(value)
                except ValueError:
                    return opts, f"--length needs seconds, not {value!r}"
                if not (MIN_LENGTH <= opts.length <= MAX_LENGTH):
                    return opts, (f"--length must be between {MIN_LENGTH:.0f}s "
                                  f"and {MAX_LENGTH:.0f}s")
            elif name == "--quality":
                q = value.lower()
                if q not in QUALITY:
                    return opts, (f"--quality is one of "
                                  f"{' | '.join(QUALITY)}, not {value!r}")
                opts.quality = q
            elif name == "--height":
                try:
                    opts.height = int(value)
                except ValueError:
                    return opts, f"--height needs pixels, not {value!r}"
                if not (144 <= opts.height <= 2160):
                    return opts, "--height must be between 144 and 2160"
            elif name == "--min-gap":
                try:
                    opts.min_gap = float(value)
                except ValueError:
                    return opts, f"--min-gap needs seconds, not {value!r}"
                if opts.min_gap < 0:
                    return opts, "--min-gap cannot be negative"
            elif name == "--start":
                opts.start = _parse_time(value)
                if opts.start is None:
                    return opts, f"--start needs a time, not {value!r}"
            elif name == "--end":
                opts.end = _parse_time(value)
                if opts.end is None:
                    return opts, f"--end needs a time, not {value!r}"
            elif name == "--out":
                opts.out = Path(value).expanduser()
            elif name == "--show":
                try:
                    opts.show = int(value)
                except ValueError:
                    return opts, f"--show needs a number, not {value!r}"
                if opts.show < 1:
                    return opts, "--show must be at least 1"
                if opts.show > MAX_CLIPS:
                    return opts, (f"--show is capped at {MAX_CLIPS} — that is "
                                  f"already more than one run will cut")
            continue
        rest.append(a)
        i += 1

    if opts.help:
        return opts, ""

    if not rest:
        return opts, "no URL — usage: /clip <url> [count]"

    # The URL is the first non-flag argument. A bare trailing integer is the
    # count, which is the form the operator asked for: `/clip <url> 5`.
    opts.url = rest[0]
    if len(rest) > 1:
        try:
            opts.count = int(rest[1])
        except ValueError:
            return opts, f"the count must be a number, not {rest[1]!r}"
        if len(rest) > 2:
            return opts, f"unexpected extra argument {rest[2]!r}"

    if opts.count is not None:
        if opts.count < 1:
            return opts, "the count must be at least 1"
        if opts.count > MAX_CLIPS:
            return opts, (f"{opts.count} clips is more than this will cut in "
                          f"one run (max {MAX_CLIPS}) — raise --min-gap and run "
                          f"again for different moments")
    if opts.start is not None and opts.end is not None and opts.end <= opts.start:
        return opts, "--end must be after --start"
    return opts, ""


def _parse_time(value: str) -> float | None:
    """``90``, ``1:30`` or ``1:02:03`` → seconds. ``None`` if it is not a time."""
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        return None
    total = 0.0
    for part in parts:
        try:
            n = float(part)
        except ValueError:
            return None
        if n < 0:
            return None
        total = total * 60 + n
    return total


def usage_lines() -> list[str]:
    """The help text, as lines the caller can print."""
    return [
        "/clip <url> [count] — find the best moments and cut them",
        "",
        "  /clip <url>              cut by duration, show the top 10",
        "  /clip <url> 5            cut and show exactly 5",
        "  /clip <url> --show 20    cut by duration, show the top 20",
        "  /clip <url> --length 60  a minute per clip",
        "  /clip <url> --dry-run    rank and print, cut nothing",
        "",
        "  How many are cut scales with the source: a 20-minute video yields",
        "  about 17, a 2-hour one about 100, a 6-hour stream about 200 (the",
        "  ceiling). Naming a count overrides that and cuts exactly that many.",
        "",
        "  --show N         how many of the cut clips to show (default 10)",
        "  --length S       seconds per clip (default 30, 5-300)",
        "  --quality Q      high | medium | low (default medium)",
        "  --height N       cap the output height (default 720)",
        "  --min-gap S      minimum seconds between clips (default = length)",
        "  --start T        only rank after T (90, 1:30, 1:02:03)",
        "  --end T          only rank before T",
        "  --out DIR        where the clips land (default: the case dir)",
        "  --live           capture a bounded window from a running stream",
        "  --dry-run        rank and print the shortlist, cut nothing",
        "",
        "  The ranking is measured from the media: audio energy, audio change",
        "  and visual change. Nothing is uploaded and no model is called to",
        "  score it — the model is used afterwards, to name the clips.",
    ]


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

@dataclass
class Video:
    """What yt-dlp says about the source."""

    url: str
    title: str = ""
    channel: str = ""
    duration: float = 0.0
    view_count: int | None = None
    upload_date: str = ""
    is_live: bool = False
    was_live: bool = False
    extractor: str = ""
    thumbnail: str = ""
    description: str = ""
    webpage_url: str = ""

    @property
    def label(self) -> str:
        return self.title or self.webpage_url or self.url


def probe(url: str, *, timeout: float = 120.0) -> tuple[Video | None, str]:
    """Metadata for *url*, via yt-dlp's JSON. ``(video, error)``.

    ``--no-playlist`` is load-bearing: a channel URL or a link with a ``list=``
    parameter would otherwise expand to every video on it, and the operator
    asked to clip one thing.
    """
    exe = ytdlp()
    if exe is None:
        return None, "yt-dlp is not installed — /clip needs it to read the video"
    cmd = [exe, "-J", "--no-warnings", "--no-playlist", "--skip-download", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"yt-dlp timed out after {timeout:.0f}s"
    except OSError as e:
        return None, f"could not run yt-dlp: {e}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, (detail[-1] if detail else f"yt-dlp exited {proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None, "yt-dlp returned something that is not JSON"
    if not isinstance(data, dict):
        return None, "yt-dlp returned no video record"
    # A playlist that slipped through despite --no-playlist: take the first
    # entry rather than failing, because the operator's intent is unambiguous.
    if data.get("_type") == "playlist":
        entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
        if not entries:
            return None, "that URL is a playlist with no videos in it"
        data = entries[0]
    duration = data.get("duration")
    try:
        duration = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        duration = 0.0
    return Video(
        url=url,
        title=str(data.get("title") or ""),
        channel=str(data.get("channel") or data.get("uploader") or ""),
        duration=duration,
        view_count=data.get("view_count"),
        upload_date=str(data.get("upload_date") or ""),
        is_live=bool(data.get("is_live")),
        was_live=bool(data.get("was_live")),
        extractor=str(data.get("extractor_key") or data.get("extractor") or ""),
        thumbnail=str(data.get("thumbnail") or ""),
        description=str(data.get("description") or "")[:2000],
        webpage_url=str(data.get("webpage_url") or url),
    ), ""


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, timeout: float) -> tuple[int, str]:
    """Run a command, returning ``(returncode, last line of output)``."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except OSError as e:
        return 127, str(e)
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return proc.returncode, (detail[-1] if detail else "")


def download_audio(url: str, outdir: Path, *,
                   timeout: float = DOWNLOAD_TIMEOUT) -> tuple[Path | None, str]:
    """Fetch the audio track only. ``(path, error)``.

    Audio-only is what makes a two-hour source affordable: 2h of opus is ~30 MB
    against ~1.5 GB for the video, and the analysis pass needs nothing else.
    The file is left in its native container — ffmpeg decodes webm/opus
    directly, so there is no transcode step to pay for.
    """
    exe = ytdlp()
    if exe is None:
        return None, "yt-dlp is not installed"
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    template = str(outdir / "audio.%(ext)s")
    cmd = [exe, "--no-warnings", "--no-playlist", "--no-progress",
           "-f", "bestaudio/best", "-o", template, url]
    code, detail = _run(cmd, timeout=timeout)
    if code != 0:
        return None, detail or f"yt-dlp exited {code}"
    found = [p for p in outdir.glob("audio.*") if p.stat().st_size > 0]
    if not found:
        return None, "the download produced no audio file"
    return max(found, key=lambda p: p.stat().st_size), ""


def download_video(url: str, outdir: Path, *, height: int = DEFAULT_HEIGHT,
                   timeout: float = DOWNLOAD_TIMEOUT) -> tuple[Path | None, str]:
    """Fetch the video, for the visual pass *and* for cutting. ``(path, error)``.

    One download serves both jobs, at the height the clips are delivered in.
    That is deliberate: the visual pass used to run on a separate 144p copy,
    which was cheap to analyse but meant the clips were cut from 144p and
    upscaled to 720 — a delivered clip softer than the source it came from.
    ``visual_change`` decodes only keyframes, so analysing this copy costs
    seconds rather than minutes, and the download is paid once instead of
    twice.

    A source with no stream at or below *height* falls back to the smallest it
    does have, so a 480p-only video still works.
    """
    exe = ytdlp()
    if exe is None:
        return None, "yt-dlp is not installed"
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    template = str(outdir / "video.%(ext)s")
    # ``+ba`` is load-bearing now that this is the only download: a video-only
    # stream has no audio track, and the ranking is mostly audio. The fallbacks
    # keep a source that only offers a muxed stream working.
    selector = (f"bv*[height<={height}]+ba/b[height<={height}]/"
                f"wv*[height<={height}]+ba/w")
    cmd = [exe, "--no-warnings", "--no-playlist", "--no-progress",
           "-f", selector, "--merge-output-format", "mp4",
           "-o", template, url]
    code, detail = _run(cmd, timeout=timeout)
    if code != 0:
        return None, detail or f"yt-dlp exited {code}"
    found = [p for p in outdir.glob("video.*")
             if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov", ".flv")
             and p.stat().st_size > 0]
    if not found:
        return None, "the download produced no video file"
    return max(found, key=lambda p: p.stat().st_size), ""


def download_section(url: str, outdir: Path, start: float, end: float, *,
                     height: int = DEFAULT_HEIGHT,
                     timeout: float = DOWNLOAD_TIMEOUT) -> tuple[Path | None, str]:
    """Fetch one section of a video, for `--live` and for a bounded range.

    ``--force-keyframes-at-cuts`` is **not** used. It re-encodes the section on
    the fly, which measured 64s for a 15s clip — and without it the section
    starts at the previous keyframe, which measured a 25s file for a 15s
    request. Neither is acceptable, so this is only used where the caller can
    tolerate the drift (a live window, where the exact frame does not matter),
    and the ordinary path downloads once and cuts locally.
    """
    exe = ytdlp()
    if exe is None:
        return None, "yt-dlp is not installed"
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    template = str(outdir / "section.%(ext)s")
    selector = f"bv*[height<={height}]+ba/b[height<={height}]/b"
    section = f"*{max(0.0, start):.1f}-{max(0.0, end):.1f}"
    cmd = [exe, "--no-warnings", "--no-playlist", "--no-progress",
           "-f", selector, "--download-sections", section,
           "-o", template, url]
    code, detail = _run(cmd, timeout=timeout)
    if code != 0:
        return None, detail or f"yt-dlp exited {code}"
    found = [p for p in outdir.glob("section.*")
             if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov")
             and p.stat().st_size > 0]
    if not found:
        return None, "the download produced no video file"
    return max(found, key=lambda p: p.stat().st_size), ""


def fetch_chat(url: str, outdir: Path, *,
               timeout: float = 120.0) -> tuple[Path | None, str]:
    """Fetch a stream's live-chat replay, if YouTube still has one.

    Not a ranking signal — see the module docstring — but it is the best
    *context* a model can be given about what happened at a timestamp, so it is
    fetched when it exists and handed over as evidence. A video with no chat is
    the normal case, not an error, so the caller treats a failure as "no chat".
    """
    exe = ytdlp()
    if exe is None:
        return None, "yt-dlp is not installed"
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    template = str(outdir / "chat.%(ext)s")
    cmd = [exe, "--no-warnings", "--no-playlist", "--no-progress",
           "--skip-download", "--write-subs", "--sub-langs", "live_chat",
           "-o", template, url]
    code, detail = _run(cmd, timeout=timeout)
    if code != 0:
        return None, detail or f"yt-dlp exited {code}"
    found = [p for p in outdir.glob("chat*") if p.stat().st_size > 0]
    if not found:
        return None, "no live chat replay"
    return max(found, key=lambda p: p.stat().st_size), ""


def fetch_captions(url: str, outdir: Path, *,
                   timeout: float = 120.0) -> tuple[Path | None, str]:
    """Fetch auto-captions, if the endpoint will serve them.

    YouTube rate-limits this hard — a 429 on the second request in a row was
    reproducible — so a failure here is expected and is not an error. The
    captions are used for clip titles and model context, never for the score.
    """
    exe = ytdlp()
    if exe is None:
        return None, "yt-dlp is not installed"
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    template = str(outdir / "captions.%(ext)s")
    cmd = [exe, "--no-warnings", "--no-playlist", "--no-progress",
           "--skip-download", "--write-auto-subs", "--write-subs",
           "--sub-langs", "en.*,en", "--sub-format", "vtt",
           "-o", template, url]
    code, detail = _run(cmd, timeout=timeout)
    if code != 0:
        return None, detail or f"yt-dlp exited {code}"
    found = [p for p in outdir.glob("captions*") if p.stat().st_size > 0]
    if not found:
        return None, "no captions"
    return max(found, key=lambda p: p.stat().st_size), ""


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """One measured series, sampled once per second."""

    name: str
    values: list[float] = field(default_factory=list)

    def at(self, second: int) -> float:
        if 0 <= second < len(self.values):
            return self.values[second]
        return 0.0

    @property
    def length(self) -> int:
        return len(self.values)


def audio_energy(path: Path, *, timeout: float = ANALYSE_TIMEOUT) -> Signal:
    """One RMS level per second, in dBFS, from a 4 kHz mono downmix.

    ``-vn`` is what makes this cheap: without it ffmpeg decodes the video
    stream too, and the pass measured 14s per ten minutes instead of 2.2s.
    ``asetnsamples`` cuts the stream into exact one-second blocks so the
    timestamps are the second index rather than whatever the codec's frame
    size happened to be.
    """
    exe = ffmpeg()
    if exe is None:
        raise ClipError("ffmpeg is not installed")
    cmd = [
        exe, "-nostdin", "-hide_banner", "-nostats", "-vn", "-i", str(path),
        "-af", (f"aresample={AUDIO_RATE},asetnsamples=n={AUDIO_RATE},"
                "astats=metadata=1:reset=1,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"),
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ClipError(f"audio analysis timed out after {timeout:.0f}s")
    except OSError as e:
        raise ClipError(f"could not run ffmpeg: {e}")
    values: list[float] = []
    for line in (proc.stdout or "").splitlines():
        if "RMS_level=" not in line:
            continue
        raw = line.split("RMS_level=", 1)[1].strip()
        try:
            values.append(float(raw))
        except ValueError:
            # `-inf` is what astats reports for digital silence. It is a real
            # measurement, not a parse failure, and it must not become 0 dBFS.
            values.append(-90.0 if "inf" in raw.lower() else -90.0)
    if not values:
        raise ClipError("the audio pass produced no measurements "
                        "(the file may have no audio track)")
    return Signal("audio", values)


def visual_change(path: Path, *, timeout: float = ANALYSE_TIMEOUT) -> Signal:
    """One visual-change score per second, from scene cuts and frame motion.

    Two measurements, combined:

    * ``scdet`` fires on a hard cut and reports a score; a cut is where a new
      visual moment *begins*, which is exactly what a clip should start near.
    * ``signalstats``' ``YDIF`` is the mean absolute luma difference between
      consecutive frames — a cheap motion proxy that catches the things scdet
      does not, like a screen recording scrolling or a camera panning.

    Both are measured at 1 fps on a 96px-wide copy, which is enough to see a
    slide change and cheap enough to run over two hours in seconds.

    ``-skip_frame nokey`` is what makes this affordable on a *full-quality*
    source rather than a 144p analysis copy: it decodes only keyframes, which
    measured 10x faster (0.7s against 7.0s for two minutes of 720p) and still
    finds every hard cut, because a cut is a keyframe by definition. That is
    what lets one download serve both the analysis and the cut — cutting from
    the 144p copy would have delivered 144p clips.
    """
    exe = ffmpeg()
    if exe is None:
        raise ClipError("ffmpeg is not installed")
    # Scene cuts first: they are sparse, so they are collected as timestamps
    # and folded into the per-second series afterwards.
    scene_cmd = [
        exe, "-nostdin", "-hide_banner", "-nostats", "-skip_frame", "nokey",
        "-i", str(path),
        "-vf", f"fps={VIDEO_FPS},scale={VIDEO_WIDTH}:-2,"
               f"scdet=threshold={SCENE_THRESHOLD}",
        "-f", "null", "-",
    ]
    cuts: dict[int, float] = {}
    try:
        proc = subprocess.run(scene_cmd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        for line in (proc.stderr or "").splitlines():
            if "lavfi.scd.time" not in line:
                continue
            try:
                at = float(line.rsplit("lavfi.scd.time:", 1)[1].strip())
                score = 0.0
                if "lavfi.scd.score:" in line:
                    score = float(line.split("lavfi.scd.score:", 1)[1]
                                  .split(",", 1)[0].strip())
            except (ValueError, IndexError):
                continue
            second = int(at)
            cuts[second] = max(cuts.get(second, 0.0), score)
    except subprocess.TimeoutExpired:
        raise ClipError(f"scene analysis timed out after {timeout:.0f}s")
    except OSError as e:
        raise ClipError(f"could not run ffmpeg: {e}")

    # Motion second: YDIF per frame, at the same 1 fps.
    motion_cmd = [
        exe, "-nostdin", "-hide_banner", "-nostats", "-skip_frame", "nokey",
        "-i", str(path),
        "-vf", f"fps={VIDEO_FPS},scale=64:-2,signalstats,"
               "metadata=print:key=lavfi.signalstats.YDIF:file=-",
        "-f", "null", "-",
    ]
    motion: list[float] = []
    try:
        proc = subprocess.run(motion_cmd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
        for line in (proc.stdout or "").splitlines():
            if "YDIF=" not in line:
                continue
            try:
                motion.append(float(line.split("YDIF=", 1)[1].strip()))
            except ValueError:
                motion.append(0.0)
    except subprocess.TimeoutExpired:
        raise ClipError(f"motion analysis timed out after {timeout:.0f}s")
    except OSError as e:
        raise ClipError(f"could not run ffmpeg: {e}")

    length = max(len(motion), (max(cuts) + 1) if cuts else 0)
    if length == 0:
        raise ClipError("the video pass produced no measurements")
    values: list[float] = []
    for second in range(length):
        # A cut is a strong, sparse signal; motion is a weak, dense one. They
        # are put on the same 0-1 scale before being added, so a single cut
        # outweighs a second of ordinary motion but not a whole minute of it.
        cut = min(1.0, cuts.get(second, 0.0) / 30.0)
        move = min(1.0, motion[second] / 12.0) if second < len(motion) else 0.0
        values.append(min(1.0, cut + 0.5 * move))
    return Signal("visual", values)


def _normalise(values: list[float]) -> list[float]:
    """Map a series onto 0-1 by its own range, robustly.

    Percentile-based rather than min/max: one door slam at -3 dBFS would
    otherwise compress every real moment into the bottom of the scale, and one
    second of digital silence would do the same from the other end.
    """
    if not values:
        return []
    ordered = sorted(values)
    n = len(ordered)
    lo = ordered[int(n * 0.05)]
    hi = ordered[min(n - 1, int(n * 0.95))]
    if hi - lo < 1e-9:
        return [0.0 for _ in values]
    return [max(0.0, min(1.0, (v - lo) / (hi - lo))) for v in values]


def _smooth(values: list[float], half: int = SMOOTH_HALF_WIDTH) -> list[float]:
    """A centred moving average, so a peak lands on an event not a sample."""
    if not values or half <= 0:
        return list(values)
    out: list[float] = []
    n = len(values)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        window = values[lo:hi]
        out.append(sum(window) / len(window))
    return out


def _change(values: list[float]) -> list[float]:
    """How much a series *rose* at each point, on 0-1.

    Only rises count. A moment is the onset of something — a laugh starting, a
    slide appearing — and a long loud passage that then goes quiet is not a
    moment, it is the end of one. Clamping at zero is what makes that
    distinction.
    """
    if not values:
        return []
    out = [0.0]
    for i in range(1, len(values)):
        out.append(max(0.0, values[i] - values[i - 1]))
    return _normalise(out)


@dataclass
class Moment:
    """One candidate clip, before it is cut."""

    start: float
    end: float
    peak: float
    score: float
    audio: float = 0.0
    change: float = 0.0
    visual: float = 0.0
    title: str = ""
    transcript: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def plan_count(duration: float, *, asked: int | None = None,
               length: float = DEFAULT_LENGTH) -> int:
    """How many clips to *cut* from a source of *duration* seconds.

    Two tiers, because the operator asked for two things at once: "cut over 100
    clips if it's a 2-hour video" and "show 10". The number cut scales with the
    source; the number shown is ``DEFAULT_CLIPS`` unless the operator names one.

    *asked* is the operator's own count, and it wins outright — ``/clip url 5``
    means five, not "five but also the automatic hundred". When it is ``None``
    the count is derived from the duration at ``CLIPS_PER_MINUTE``, clamped to
    ``[AUTO_MIN_CLIPS, MAX_CLIPS]``.

    The count is also bounded by what the timeline can hold: with a minimum gap
    of one clip length, a source of *duration* seconds cannot yield more than
    ``duration / length`` non-overlapping clips, and asking for more would make
    the peak-picker return fewer than requested and look broken.
    """
    if asked is not None:
        want = asked
    else:
        want = int(round((duration / 60.0) * CLIPS_PER_MINUTE))
        want = max(AUTO_MIN_CLIPS, min(MAX_CLIPS, want))
    # Never ask for more clips than the timeline can hold without overlap.
    if duration > 0 and length > 0:
        want = min(want, max(1, int(duration // length)))
    return max(1, min(MAX_CLIPS, want))


def rank(audio: Signal, visual: Signal | None, *, count: int,
         length: float, min_gap: float | None = None,
         start: float | None = None, end: float | None = None) -> list[Moment]:
    """Pick the *count* best moments. The whole product is in this function.

    The timeline is scored per second, smoothed, and then peaks are taken
    greedily with a minimum separation — so the result is *count* distinct
    moments rather than *count* overlapping windows around the loudest one.
    """
    if not audio.values:
        return []
    total = audio.length
    lo = int(max(0.0, start or 0.0))
    hi = int(min(total, end)) if end is not None else total
    if hi - lo < 1:
        return []

    # Audio energy is in dBFS, which is logarithmic and negative. It is
    # normalised on its own range, so a quiet lecture and a loud stream are
    # both ranked against themselves rather than against each other.
    energy = _normalise(audio.values)
    # Smooth the *energy* before the change is taken from it, not after. A
    # moment's onset is a one-second event, and averaging the combined score
    # over ±3s dilutes that spike sevenfold — measured: the onset scored 1.0
    # and arrived at the peak-picker as 0.14, so the peak landed in the middle
    # of the loud passage instead of on the event that started it. Smoothing
    # the energy first gives a change signal that is already the shape of a
    # rise, and leaves it intact.
    energy = _smooth(energy)
    change = _change(energy)
    visual_norm = _normalise(visual.values) if visual is not None else []

    combined: list[float] = []
    for i in range(total):
        v = W_AUDIO * energy[i] + W_CHANGE * change[i]
        if visual_norm:
            v += W_VISUAL * (visual_norm[i] if i < len(visual_norm) else 0.0)
        combined.append(v)

    gap = min_gap if min_gap is not None else max(length, 1.0) * MIN_GAP_FRACTION
    lead = length * LEAD_FRACTION

    # Greedy peak picking. Sorting every second by score and taking the best
    # that is far enough from everything already taken is O(n log n) and gives
    # the same answer as a proper non-maximum suppression for this shape of
    # signal, without the tuning a window-based NMS would need.
    order = sorted(range(lo, hi), key=lambda i: combined[i], reverse=True)
    chosen: list[int] = []
    for i in order:
        if len(chosen) >= count:
            break
        if any(abs(i - j) < gap for j in chosen):
            continue
        chosen.append(i)
    chosen.sort()

    moments: list[Moment] = []
    for i in chosen:
        begin = max(0.0, i - lead)
        finish = min(float(total), begin + length)
        # A peak near the end would otherwise produce a short clip; slide the
        # window back so every clip is the length that was asked for.
        if finish - begin < length:
            begin = max(0.0, finish - length)
        moments.append(Moment(
            start=begin,
            end=finish,
            peak=float(i),
            score=combined[i],
            audio=energy[i],
            change=change[i],
            visual=(visual_norm[i] if visual_norm and i < len(visual_norm) else 0.0),
        ))
    # Best first, which is the order the operator reads them in.
    moments.sort(key=lambda m: m.score, reverse=True)
    return moments


# ---------------------------------------------------------------------------
# Captions and chat, as context
# ---------------------------------------------------------------------------

def parse_vtt(text: str) -> list[tuple[float, float, str]]:
    """``[(start, end, text), ...]`` from a WebVTT file.

    Deliberately small: it reads the cue timings and the cue text and ignores
    everything else, because the only use here is "what was said around 12:30".
    Markup and entities are stripped — a clip title built from ``<b>third</b>``
    is not a title.
    """
    cues: list[tuple[float, float, str]] = []
    current: list | None = None
    for line in (text or "").splitlines():
        match = _TS_RE.search(line)
        if match:
            g = match.groups()
            begin = _hms(g[0], g[1], g[2], g[3])
            finish = _hms(g[4], g[5], g[6], g[7])
            current = [begin, finish, ""]
            cues.append(current)  # type: ignore[arg-type]
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(("WEBVTT", "Kind:", "Language:",
                                                 "NOTE", "STYLE", "REGION")):
            continue
        if current is not None:
            piece = _TAG_RE.sub("", stripped)
            piece = (piece.replace("&amp;", "&").replace("&lt;", "<")
                     .replace("&gt;", ">").replace("&nbsp;", " ")
                     .replace("&#39;", "'").replace("&quot;", '"'))
            current[2] = (current[2] + " " + piece).strip()
    return [(float(a), float(b), c) for a, b, c in cues if c]


def _hms(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def transcript_between(cues: list[tuple[float, float, str]],
                       start: float, end: float, *,
                       limit: int = 400) -> str:
    """What was said between *start* and *end*, as one line."""
    said = [text for (a, b, text) in cues if b >= start and a <= end]
    joined = " ".join(said).strip()
    if len(joined) > limit:
        joined = joined[: limit - 1].rstrip() + "…"
    return joined


def chat_between(path: Path, start: float, end: float, *,
                 limit: int = 300) -> str:
    """The live-chat messages around a moment, as one line.

    The replay is JSON-lines, one object per message with a ``videoOffsetTimeMsec``
    or a ``time`` field depending on the extractor version, so both are read.
    A file that will not parse yields an empty string rather than an error: the
    chat is enrichment, and losing it must not lose the clip.
    """
    try:
        raw = Path(path).read_text(errors="replace")
    except OSError:
        return ""
    picked: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        offset = obj.get("videoOffsetTimeMsec")
        if offset is None:
            offset = obj.get("time")
        try:
            at = float(offset) / 1000.0
        except (TypeError, ValueError):
            continue
        if not (start <= at <= end):
            continue
        text = ""
        for key in ("message", "text", "body"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
            if isinstance(value, dict):
                inner = value.get("simpleText") or value.get("runs")
                if isinstance(inner, str):
                    text = inner.strip()
                elif isinstance(inner, list):
                    text = "".join(str(r.get("text", "")) for r in inner
                                   if isinstance(r, dict)).strip()
                if text:
                    break
        if text:
            picked.append(text)
    joined = " · ".join(picked)
    if len(joined) > limit:
        joined = joined[: limit - 1].rstrip() + "…"
    return joined


# ---------------------------------------------------------------------------
# Cutting
# ---------------------------------------------------------------------------

def cut(source: Path, dest: Path, start: float, duration: float, *,
        height: int = DEFAULT_HEIGHT, crf: int = 23,
        timeout: float = CUT_TIMEOUT) -> tuple[Path | None, str]:
    """Cut one clip, frame-accurately. ``(path, error)``.

    ``-ss`` before ``-i`` is the fast seek: ffmpeg jumps to the nearest
    keyframe before the requested time and then decodes forward, which is what
    makes a cut from the middle of a two-hour file take a second rather than
    minutes. Re-encoding is not optional — a stream copy can only cut on
    keyframes, and a clip that starts up to 10s before the moment it was chosen
    for is not the clip that was ranked.
    """
    exe = ffmpeg()
    if exe is None:
        return None, "ffmpeg is not installed"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe, "-nostdin", "-y", "-loglevel", "error",
        "-ss", f"{max(0.0, start):.3f}",
        "-i", str(source),
        "-t", f"{max(0.1, duration):.3f}",
        "-vf", f"scale=-2:'min({height},ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dest),
    ]
    code, detail = _run(cmd, timeout=timeout)
    if code != 0 or not dest.exists() or dest.stat().st_size == 0:
        return None, detail or f"ffmpeg exited {code}"
    return dest, ""


def cut_all(source: Path, moments: list[Moment], outdir: Path, *,
            height: int = DEFAULT_HEIGHT, quality: str = DEFAULT_QUALITY,
            timeout: float = CUT_TIMEOUT,
            progress=None) -> tuple[list[tuple[Moment, Path]], list[str]]:
    """Cut every moment. Returns ``([(moment, path)], [errors])``.

    Sequential on purpose. Measured on a 4-core host, cutting 20 clips in
    parallel took 56.9s against 53.8s sequentially — the encoder already
    saturates the cores, so parallelism buys nothing and makes the progress
    reporting a lie. One at a time is also what lets a Ctrl+C leave a clean
    set of finished clips rather than a directory of half-written files.
    """
    crf = QUALITY.get(quality, QUALITY[DEFAULT_QUALITY])
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    made: list[tuple[Moment, Path]] = []
    errors: list[str] = []
    for index, moment in enumerate(moments, 1):
        name = f"{index:02d}-{_stamp(moment.start)}-{_stamp(moment.end)}.mp4"
        dest = outdir / name
        path, err = cut(source, dest, moment.start, moment.duration,
                        height=height, crf=crf, timeout=timeout)
        if path is None:
            errors.append(f"clip {index} ({_stamp(moment.start)}): {err}")
        else:
            made.append((moment, path))
        if progress is not None:
            try:
                progress(index, len(moments), path, err)
            except Exception:  # noqa: BLE001 - a reporter must not stop the cut
                pass
    return made, errors


def _stamp(seconds: float) -> str:
    """``3725`` → ``1h02m05s``, for a filename that sorts and reads."""
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m:02d}m{s:02d}s"


def human_time(seconds: float) -> str:
    """A duration a human reads at a glance."""
    if seconds < 1:
        return "<1s"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def human_size(path: Path) -> str:
    try:
        size = Path(path).stat().st_size
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


# ---------------------------------------------------------------------------
# Case directory
# ---------------------------------------------------------------------------

def case_slug(target) -> str:
    """A filesystem-safe, recognisable label."""
    s = _SLUG_RE.sub("-", str(target).strip()).strip("-.")
    return (s[:64] or "clip").lower()


def case_dir(cfg, video: Video) -> Path:
    """Create and return this run's directory.

    ``<state_dir>/clip/<slug>-<stamp>/`` — beside the osint cases, the crack
    runs and the hunt campaigns, under state_dir so the clips land with the
    sessions rather than in whatever repo the operator happens to be in.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = case_slug(video.title or video.url)
    d = Path(cfg.state_dir) / "clip" / f"{label}-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def reports_dir(cfg) -> Path:
    """Where finished reports are kept, beside the osint, hunt and crack ones."""
    return Path(cfg.state_dir) / "reports"


def finalise_case(cfg, case: Path) -> Path | None:
    """Archive the run's report into the flat report store.

    The case directory is **not** deleted, unlike osint's: the clips are the
    product, and a report that outlived the files it describes would be a
    catalogue of things that no longer exist. Only the report is copied out.
    """
    report = case / "report.md"
    if not report.is_file():
        candidates = [p for p in sorted(case.glob("*.md"))
                      if "report" in p.name.lower()]
        report = candidates[0] if candidates else None
    if report is None or not report.is_file():
        return None
    dest_dir = reports_dir(cfg)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{case.name}.md"
        shutil.copy2(report, dest)
    except OSError:
        return None
    return dest


# ---------------------------------------------------------------------------
# Live streams
# ---------------------------------------------------------------------------

def plan_live(video: Video, *, window: float = LIVE_WINDOW) -> tuple[float, float, str]:
    """The bounded range to capture from a running stream.

    A live stream has no end, so there is nothing to rank against and no
    "best moment" to find — the ranking is a comparison across a whole
    timeline. What *can* be done is capture a bounded window from the live edge
    and rank inside it, which is what this returns. The window is stated in the
    result so the operator knows what they got.
    """
    if not video.is_live:
        return 0.0, video.duration, ""
    note = (f"this stream is live — capturing the last {human_time(window)} "
            f"from the live edge and ranking inside that window")
    return 0.0, window, note


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------

PLAYBOOK = """\
You are naming and describing the clips that were just cut from a video. The
cutting and the ranking are already done — do not re-rank, do not re-cut, and do
not second-guess the scores. Your job is the part a signal cannot do: say what
each moment *is*.

SOURCE
  title     {title}
  channel   {channel}
  duration  {duration}
  url       {url}

CASE DIRECTORY
  {case_dir}

  clips/            the {cut} clips that were cut, best first, named by their
                    timestamp. You are being shown the top {count} of them.
  moments.json      the ranking of all {cut}: start, end, peak, score, signals
  report.md         write this

THE CLIPS, IN RANKED ORDER
{moments}

WHAT TO WRITE
For each clip, in the same order, give:

  - a title of at most 60 characters that says what happens, not what it is
    ("the bit where the demo catches fire", not "clip 3")
  - one sentence on why this moment is worth posting
  - the timestamp range, as it appears in the filename

Then a short section at the end: which of the {count} you would post first and
why, and which one you would drop.

RULES
  - Work only from what is in this brief: the transcript excerpts, the chat
    excerpts, the signal values and the timestamps. If a clip has no transcript
    and no chat, say what the signals show and do not invent dialogue.
  - Do not claim a clip contains something you cannot see evidence for. "Loud
    and a scene cut at 12:31, no transcript available" is an honest description;
    "the speaker reveals the secret" is not.
  - Write report.md into the case directory above. Nothing else.
"""


def build_prompt(video: Video, case: Path, moments: list[Moment], *,
                 count: int, cut: int | None = None) -> str:
    """The turn the agent actually runs.

    The ranking is interpolated rather than left for the agent to work out: the
    scores are facts this module already measured, and asking a model to
    rediscover them from filenames is asking it to guess. What the model is
    given is the evidence — timestamps, signals, transcript and chat excerpts —
    and what it is asked for is the naming, which is the part that needs
    judgement.
    """
    lines: list[str] = []
    for index, moment in enumerate(moments, 1):
        lines.append(
            f"  {index:02d}. {_stamp(moment.start)} → {_stamp(moment.end)}  "
            f"score {moment.score:.2f}  "
            f"(audio {moment.audio:.2f} · change {moment.change:.2f} · "
            f"visual {moment.visual:.2f})")
        if moment.transcript:
            lines.append(f"      said: {moment.transcript}")
        if moment.title:
            lines.append(f"      chat: {moment.title}")
    if not lines:
        lines.append("  (no moments were ranked)")
    return PLAYBOOK.format(
        title=video.title or "(untitled)",
        channel=video.channel or "(unknown)",
        duration=human_time(video.duration) if video.duration else "(unknown)",
        url=video.webpage_url or video.url,
        case_dir=case,
        count=count,
        cut=cut if cut is not None else count,
        moments="\n".join(lines),
    )


def write_moments(case: Path, video: Video, moments: list[Moment],
                  clips: list[tuple[Moment, Path]]) -> Path:
    """Write ``moments.json`` — the ranking, as data rather than as prose.

    The report is for a person; this is for the next run, a script, or a model
    that wants the numbers without parsing markdown.
    """
    by_start = {round(m.start, 3): p.name for m, p in clips}
    payload = {
        "source": {
            "url": video.webpage_url or video.url,
            "title": video.title,
            "channel": video.channel,
            "duration": video.duration,
            "extractor": video.extractor,
            "was_live": video.was_live,
        },
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "weights": {"audio": W_AUDIO, "change": W_CHANGE, "visual": W_VISUAL},
        "moments": [
            {
                "rank": i,
                "start": round(m.start, 3),
                "end": round(m.end, 3),
                "peak": m.peak,
                "score": round(m.score, 4),
                "signals": {
                    "audio": round(m.audio, 4),
                    "change": round(m.change, 4),
                    "visual": round(m.visual, 4),
                },
                "transcript": m.transcript,
                "chat": m.title,
                "file": by_start.get(round(m.start, 3), ""),
            }
            for i, m in enumerate(moments, 1)
        ],
    }
    dest = Path(case) / "moments.json"
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return dest


def summary_lines(video: Video, moments: list[Moment],
                  clips: list[tuple[Moment, Path]]) -> list[str]:
    """The shortlist, as lines the caller can print.

    This is what the operator reads before the model says anything: the
    timestamps, the scores and the files, so the ranking can be judged on its
    own terms rather than only through the model's description of it.
    """
    out = [f"{video.title or video.url}"]
    if video.channel:
        out.append(f"  {video.channel} · {human_time(video.duration)}")
    out.append("")
    by_start = {round(m.start, 3): p for m, p in clips}
    for index, moment in enumerate(moments, 1):
        path = by_start.get(round(moment.start, 3))
        size = f"  {human_size(path)}" if path is not None else ""
        out.append(
            f"  {index:02d}  {_stamp(moment.start)} → {_stamp(moment.end)}  "
            f"score {moment.score:.2f}{size}")
        if moment.transcript:
            out.append(f"      {moment.transcript[:110]}")
    return out
