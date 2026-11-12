"""Image attachments — read a picture, and put it in front of the model.

ZimZilla grew up text-only: a tool result was a string, and the transports
translated strings. Seeing a web page needs one more kind of content, so this
module owns the small amount of plumbing that makes an image usable on either
wire protocol:

* **Read it once.** A block stored in the conversation is a *placeholder* —
  a path plus its media type — not a megabyte of base64. The data is read when
  the request is assembled, so sessions stay small and a missing file is
  reported rather than silently re-sent as a stale copy.
* **Fit it.** Providers cap an image by bytes and by pixels; a full-page
  screenshot can exceed both. Anything too large is downscaled with ffmpeg
  (already a dependency of the video path), and the result is used from then
  on.
* **Know both shapes.** Anthropic Messages wants
  ``{"type":"image","source":{"type":"base64",...}}``; Chat Completions wants
  a ``{"type":"image_url","image_url":{"url":"data:..."}}`` part. This module
  produces the neutral placeholder and :func:`materialise` turns it into the
  Anthropic shape; ``openai_compat.translate_messages`` turns it into the
  OpenAI shape.
* **Recover from a blind model.** A text-only route answers 400 when it meets
  an image. :func:`image_error` recognises that, and :func:`strip_images`
  removes every image from the history so the turn can be retried with the
  text the tool also returned. A model that cannot see should degrade to
  reading, not fail the turn.

Nothing here imports the agent or a transport, so it is testable on its own.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: The providers' usual ceilings. Anthropic rejects an image over 5 MB, and a
#: 1280×800 screenshot is nowhere near that — but a full-page capture of a long
#: article can be, and a downloaded 4K thumbnail always is. 4 MB leaves room for
#: base64 expansion and the rest of the request.
MAX_BYTES = 4_000_000

#: Longest edge kept for an ordinary image. Beyond this the token cost rises
#: faster than the detail it adds.
MAX_DIM = 1600

#: A page screenshot is tall, not wide: fitting its *longest* edge to 1600
#: would shrink a 1280×4200 capture to 488px wide and make its text unreadable.
#: Tall images are therefore fitted by width, with a separate height ceiling.
MAX_HEIGHT = 6000

#: How many images one request may carry. A browse call that attaches a viewport
#: and a full-page shot, plus a video thumbnail and four frames, is already at
#: the useful limit — more would crowd out the conversation they illustrate.
MAX_PER_REQUEST = 8

#: A tmp directory for downscaled copies. Kept for the process lifetime so a
#: later request can re-read the same file; the OS clears it eventually.
_SCRATCH: Path | None = None


class ImageError(Exception):
    """An image that cannot be read or handed to a provider."""


@dataclass(frozen=True)
class Prepared:
    """An image that is ready to send."""

    path: Path
    media_type: str
    width: int
    height: int
    size: int

    @property
    def data(self) -> bytes:
        return self.path.read_bytes()


def _scratch() -> Path:
    global _SCRATCH
    if _SCRATCH is None:
        _SCRATCH = Path(tempfile.mkdtemp(prefix="zimzilla-images-"))
    else:
        _SCRATCH.mkdir(parents=True, exist_ok=True)
    return _SCRATCH


def sniff(data: bytes) -> str | None:
    """The media type of *data* from its magic bytes, or ``None``.

    The extension is not trusted: a downloaded thumbnail is routinely a WebP
    named ``.jpg``, and base64 is sent with the real type or not at all.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"BM":
        return "image/bmp"
    return None


def looks_like_image(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return sniff(fh.read(32)) is not None
    except OSError:
        return False


def dimensions(data: bytes) -> tuple[int, int]:
    """``(width, height)`` from the file header, or ``(0, 0)`` if unknown.

    Deliberately header-only: this runs on the request path, and shelling out to
    ffprobe for every screenshot would cost more than the resize it informs.
    JPEG needs a marker walk; the other formats store the size in a fixed field.
    """
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return (int.from_bytes(data[6:8], "little"),
                    int.from_bytes(data[8:10], "little"))
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            kind = data[12:16]
            if kind == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return w, h
            if kind == b"VP8 " and len(data) >= 30:
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return w, h
        if data.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = int.from_bytes(data[i + 2:i + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                              0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return w, h
                i += 2 + length
    except (IndexError, ValueError):
        pass
    return (0, 0)


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _fit(path: Path, media_type: str, *, width: int, height: int,
         jpeg: bool = False) -> Path:
    """A copy of *path* fitted inside *width*×*height*, preserving aspect.

    ``force_original_aspect_ratio=decrease`` is what keeps a tall screenshot
    tall: a 1280×4200 page inside a 1600×6000 box is left at its own size,
    where a longest-edge rule would have cut it to 488px wide and destroyed the
    text the picture exists to show. JPEG is used only when the PNG is still
    too many bytes — text stays legible at q3 and the payload shrinks by an
    order of magnitude.
    """
    ff = _ffmpeg()
    if ff is None:
        return path
    if jpeg:
        suffix = ".jpg"
    else:
        suffix = {"image/png": ".png", "image/jpeg": ".jpg",
                  "image/webp": ".png", "image/gif": ".gif",
                  "image/bmp": ".png"}.get(media_type, ".png")
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:12]
    out = _scratch() / f"{path.stem[:40]}-{digest}-{width}x{height}{suffix}"
    if out.exists() and out.stat().st_size > 0:
        return out
    vf = (f"scale={width}:{height}:"
          "force_original_aspect_ratio=decrease")
    cmd = [ff, "-y", "-loglevel", "error", "-i", str(path),
           "-vf", vf, "-frames:v", "1"]
    if jpeg:
        cmd += ["-q:v", "3"]
    cmd.append(str(out))
    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return path
    return out if out.exists() and out.stat().st_size > 0 else path


def prepare(path: str | Path, *, max_dim: int = MAX_DIM,
            max_bytes: int = MAX_BYTES) -> Prepared | None:
    """Read *path* and fit it for a provider. ``None`` if it is not an image.

    The returned path is the original whenever it already fits, so a normal
    screenshot is never re-encoded and the transcript's path stays the one the
    operator can open.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError:
        return None
    media_type = sniff(data)
    if media_type is None:
        return None
    width, height = dimensions(data)
    # Tall captures are fitted by width; ordinary pictures by their longest
    # edge. The two cases are different because a screenshot's information is
    # spread vertically and shrinking it to a square box loses the text.
    tall = bool(width and height and height > width * 1.8)
    needs_resize = (
        media_type == "image/bmp"      # no provider accepts BMP
        or len(data) > max_bytes
        or (width and width > max_dim)
        or (height and height > (MAX_HEIGHT if tall else max_dim))
    )
    if needs_resize:
        resized = _fit(p, media_type, width=max_dim,
                       height=MAX_HEIGHT if tall else max_dim)
        if resized != p:
            try:
                data = resized.read_bytes()
            except OSError:
                return None
            p = resized
            media_type = sniff(data) or media_type
            width, height = dimensions(data)
    # A still-too-large image is re-encoded harder rather than rejected: a
    # screenshot the model cannot receive is a worse answer than a smaller one.
    # JPEG is the escape hatch — a PNG of a text-heavy page can be megabytes,
    # and the same page at q3 is hundreds of kilobytes and still readable.
    for width_limit in (1000, 800, 600):
        if len(data) <= max_bytes:
            break
        resized = _fit(p, media_type, width=width_limit, height=MAX_HEIGHT,
                       jpeg=True)
        if resized == p:
            break
        try:
            data = resized.read_bytes()
        except OSError:
            break
        p, media_type = resized, (sniff(data) or media_type)
        width, height = dimensions(data)
    if len(data) > max_bytes * 2:
        return None
    return Prepared(path=p, media_type=media_type, width=width,
                    height=height, size=len(data))


def placeholder(path: str | Path) -> dict | None:
    """The internal conversation block for an image, or ``None``.

    The shape carries a path, not the bytes. ``_path`` is deliberately private:
    it is a harness implementation detail, and both transports know to turn it
    into their own representation at request time.
    """
    got = prepare(path)
    if got is None:
        return None
    return {
        "type": "image",
        "_path": str(got.path),
        "media_type": got.media_type,
        "width": got.width,
        "height": got.height,
    }


def placeholders(paths, *, limit: int = MAX_PER_REQUEST) -> list[dict]:
    """One placeholder per readable image in *paths*, capped at *limit*."""
    out: list[dict] = []
    for path in paths or []:
        if len(out) >= limit:
            break
        block = placeholder(path)
        if block is not None:
            out.append(block)
    return out


def tool_content(output: str, paths) -> str | list:
    """A ``tool_result`` content value carrying text and any images.

    The string form is kept when there is no image, so every existing tool's
    result is byte-identical on the wire. With an image, Anthropic accepts a
    content-block list and that is what the loop stores; Chat Completions has
    no image in a tool message, so its translator pulls these out into a
    following user message.
    """
    blocks = placeholders(paths)
    if not blocks:
        return output
    return [{"type": "text", "text": output or "(no output)"}, *blocks]


def _is_image(block) -> bool:
    return isinstance(block, dict) and block.get("type") == "image"


def _anthropic_image(block: dict) -> dict | None:
    """The Messages shape for one internal image block."""
    if isinstance(block.get("source"), dict) and block["source"].get("data"):
        return {"type": "image", "source": block["source"]}
    raw = block.get("_path")
    if not raw:
        return None
    got = prepare(raw)
    if got is None:
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": got.media_type,
            "data": base64.b64encode(got.data).decode("ascii"),
        },
    }


def _materialise_block(block: dict, budget: list[int]) -> dict | None:
    """One block for the Anthropic request, or ``None`` to drop it."""
    if _is_image(block):
        if budget[0] <= 0:
            return {"type": "text", "text": "[image omitted — request image limit]"}
        out = _anthropic_image(block)
        if out is None:
            return {"type": "text", "text": "[image unavailable — file missing or unreadable]"}
        budget[0] -= 1
        return out
    if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
        content = []
        for inner in block["content"]:
            if _is_image(inner):
                if budget[0] <= 0:
                    content.append({"type": "text",
                                    "text": "[image omitted — request image limit]"})
                    continue
                got = _anthropic_image(inner)
                if got is None:
                    content.append({"type": "text",
                                    "text": "[image unavailable — file missing]"})
                else:
                    budget[0] -= 1
                    content.append(got)
            else:
                content.append(inner)
        return {**block, "content": content}
    return block


def materialise(messages: list[dict]) -> list[dict]:
    """A copy of *messages* with every image placeholder made sendable.

    Anthropic Messages is the only caller: it hands the SDK the conversation
    directly, so the base64 has to be in the block. Chat Completions translates
    at the boundary instead (see ``openai_compat``). Messages without images are
    returned by reference, so an ordinary session pays nothing for this.
    """
    budget = [MAX_PER_REQUEST]
    out: list[dict] = []
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or not any(
            _is_image(b) or (isinstance(b, dict) and b.get("type") == "tool_result"
                             and isinstance(b.get("content"), list)
                             and any(_is_image(i) for i in b["content"]))
            for b in content
        ):
            out.append(message)
            continue
        rebuilt = []
        for block in content:
            got = _materialise_block(block, budget) if isinstance(block, dict) else None
            if got is not None:
                rebuilt.append(got)
        out.append({**message, "content": rebuilt})
    return out


def strip_images(messages: list[dict]) -> tuple[list[dict], int]:
    """Remove every image from *messages*, for a route that cannot see.

    Returns ``(messages, removed)``. A tool result that was nothing but an
    image keeps a text note saying one was there, so the model knows the tool
    did something rather than receiving an empty result.
    """
    removed = 0
    out = []
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            out.append(message)
            continue
        rebuilt = []
        for block in content:
            if _is_image(block):
                removed += 1
                continue
            if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                inner = []
                for item in block["content"]:
                    if _is_image(item):
                        removed += 1
                        continue
                    inner.append(item)
                if not inner:
                    inner = [{"type": "text",
                              "text": "[image omitted — this model cannot see images]"}]
                rebuilt.append({**block, "content": inner})
            else:
                rebuilt.append(block)
        if not rebuilt:
            rebuilt = [{"type": "text",
                        "text": "[image omitted — this model cannot see images]"}]
        out.append({**message, "content": rebuilt})
    return out, removed


def has_images(messages: list[dict]) -> bool:
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if _is_image(block):
                return True
            if (block.get("type") == "tool_result"
                    and isinstance(block.get("content"), list)
                    and any(_is_image(i) for i in block["content"])):
                return True
    return False


_OLD_IMAGE_NOTE = ("[earlier image omitted — only the most recent "
                   "screenshots are kept in the conversation]")


def trim_images(messages: list[dict], keep: int = MAX_PER_REQUEST
                ) -> tuple[list[dict], int]:
    """Keep only the newest *keep* images, so history cannot grow unbounded.

    Every image in the conversation is re-sent on every request, so an
    afternoon of browsing would otherwise mean a dozen screenshots on each
    turn — tokens spent looking at pages the conversation has moved past. The
    newest images are the ones being discussed; older ones become a one-line
    note, which is enough to explain the gap without pretending the picture is
    still there.
    """
    keep = max(0, int(keep))
    seen = 0
    removed = 0

    def _one(block: dict) -> dict:
        nonlocal seen, removed
        if _is_image(block):
            seen += 1
            if seen > keep:
                removed += 1
                return {"type": "text", "text": _OLD_IMAGE_NOTE}
        return block

    out_rev: list[dict] = []
    for message in reversed(messages or []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            out_rev.append(message)
            continue
        blocks_rev = []
        for block in reversed(content):
            if not isinstance(block, dict):
                blocks_rev.append(block)
                continue
            if block.get("type") == "tool_result" and isinstance(
                    block.get("content"), list):
                inner_rev = [_one(item) if isinstance(item, dict) else item
                             for item in reversed(block["content"])]
                blocks_rev.append({**block, "content": list(reversed(inner_rev))})
            else:
                blocks_rev.append(_one(block))
        out_rev.append({**message, "content": list(reversed(blocks_rev))})
    return list(reversed(out_rev)), removed


_IMAGE_WORDS = ("image", "vision", "multimodal", "media type", "pixel",
                "base64", "image_url", "content block")


def image_error(exc: Exception) -> bool:
    """Whether *exc* is a provider saying it cannot accept an image.

    Narrow on purpose. A context overflow can mention "image" only if the
    conversation did, and a bad model name never will; requiring both an image
    word and a rejection word keeps those diagnoses intact.
    """
    msg = str(exc).lower()
    if not any(word in msg for word in _IMAGE_WORDS):
        return False
    return any(word in msg for word in (
        "not supported", "unsupported", "invalid", "too large", "exceed",
        "only text", "text-only", "does not support", "cannot process",
        "unable to process", "not allowed", "unexpected", "must be",
    ))


def describe(prepared: Prepared | None) -> str:
    """A one-line label for a transcript or a tool result."""
    if prepared is None:
        return "image unavailable"
    size = prepared.size / 1024
    dims = f"{prepared.width}×{prepared.height}" if prepared.width else "unknown size"
    return f"{prepared.path} ({prepared.media_type}, {dims}, {size:.0f} KiB)"


def gif_frames(path: str | Path, *, count: int = 4) -> list[Path]:
    """Still frames spread through an animated GIF.

    A model given an animated GIF sees one frame. For "what happens in this
    GIF" that is often the wrong frame, so the caller can ask for several; this
    extracts them with ffmpeg at evenly spaced timestamps. A still image — or a
    host without ffmpeg — returns the original alone.
    """
    p = Path(path)
    count = max(1, min(int(count), 6))
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if count <= 1 or ffmpeg is None or not p.exists():
        return [p]
    try:
        with open(p, "rb") as fh:
            if not fh.read(6) in (b"GIF87a", b"GIF89a"):
                return [p]
    except OSError:
        return [p]
    duration = 0.0
    if ffprobe:
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
                capture_output=True, text=True, timeout=15)
            duration = float((proc.stdout or "0").strip() or 0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            duration = 0.0
    outdir = _scratch() / f"gif-{hashlib.sha1(str(p).encode()).hexdigest()[:10]}"
    outdir.mkdir(parents=True, exist_ok=True)
    points = ([duration * (i + 0.5) / count for i in range(count)]
              if duration > 0 else [0.0])
    shots: list[Path] = []
    for i, at in enumerate(points, 1):
        dest = outdir / f"frame-{i}.png"
        if dest.exists() and dest.stat().st_size > 0:
            shots.append(dest)
            continue
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{at:.3f}",
               "-i", str(p), "-frames:v", "1", str(dest)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if dest.exists() and dest.stat().st_size > 0:
            shots.append(dest)
    if shots:
        return shots
    # No duration (or no seekable stream): take the first frames in order.
    dest = outdir / "frame-%02d.png"
    try:
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(p),
                        "-frames:v", str(count), str(dest)],
                       capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return [p]
    return sorted(outdir.glob("frame-*.png")) or [p]
