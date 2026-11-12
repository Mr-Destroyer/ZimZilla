"""The generated MCP bridge — one small server per connected application.

ZimZilla speaks MCP to this process over stdio. A bridge is deliberately *not*
a pile of GUI macros: for an application with a real file format and a render
engine, the file is the API. Kdenlive is the case that matters here — its
project is MLT XML and ``melt`` renders it — so that adapter creates and edits
timelines directly and uses the GUI only to show the result. A generic desktop
adapter covers applications that have no file API: list/focus windows, type,
press keys, screenshot and OCR.

Security boundaries:

* The server exposes **no shell tool**. Project edits are XML operations;
  rendering runs ``melt`` on a named project; the only launch command is the
  one recorded in the bridge's own config.
* Kdenlive screenshots and OCR read the screen; MCP calls are still gated by
  ZimZilla's MCP permission rule, and this server never bypasses it.
* Every path is expanded and required to exist before it is used.

The module is stdlib-only so the generated ``server.py`` can run under the
project venv without importing Textual or the agent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"

#: How long a render may take. A real project can be long; a bridge call that
#: silently waits forever is worse than one that reports the timeout.
RENDER_TIMEOUT = 3600

#: Where the MCP pane listens for app_type/app_keys when it is hosting the
#: app in an embedded terminal. Set in ``main`` from bridge.json.
_CONTROL_SOCKET = ""

#: ``ffprobe`` is used for duration only. When it is absent, the caller must
#: supply an out frame; guessing a duration is how a clip becomes a still.
def _which(name: str) -> str | None:
    return shutil.which(name)


class BridgeError(Exception):
    """A tool failure worth returning to the model as text."""


# ---------------------------------------------------------------------------
# MLT / Kdenlive project handling
# ---------------------------------------------------------------------------

def _fps(profile: ET.Element) -> float:
    try:
        num = float(profile.get("frame_rate_num") or 30)
        den = float(profile.get("frame_rate_den") or 1)
        return num / den if den else 30.0
    except (TypeError, ValueError):
        return 30.0


def _profile(root: ET.Element) -> ET.Element:
    found = root.find("profile")
    if found is None:
        found = ET.SubElement(root, "profile")
        found.set("width", "1920")
        found.set("height", "1080")
        found.set("frame_rate_num", "30")
        found.set("frame_rate_den", "1")
    return found


def _prop(parent: ET.Element, name: str, value: Any) -> ET.Element:
    el = ET.SubElement(parent, "property", {"name": str(name)})
    el.text = str(value)
    return el


def _producer_id(root: ET.Element) -> str:
    used = {p.get("id") for p in root.findall("producer")}
    i = 0
    while f"producer{i}" in used:
        i += 1
    return f"producer{i}"


def _clip_number(root: ET.Element) -> int:
    highest = 0
    for prop in root.iter("property"):
        if prop.get("name") == "kdenlive:id" and prop.text and prop.text.isdigit():
            highest = max(highest, int(prop.text))
    return highest + 1


def _playlists(root: ET.Element) -> list[ET.Element]:
    out = []
    for pl in root.findall("playlist"):
        if pl.get("id") == "main_bin":
            continue
        out.append(pl)
    return out


def _tractor(root: ET.Element) -> ET.Element:
    tractor = root.find("tractor")
    if tractor is None:
        tractor = ET.SubElement(root, "tractor", {"id": "tractor0", "in": "0",
                                                   "out": "0"})
        _prop(tractor, "kdenlive:projectTractor", "1")
    return tractor


def _track_name(playlist: ET.Element) -> str:
    for prop in playlist.findall("property"):
        if prop.get("name") == "kdenlive:trackName":
            return prop.text or ""
    return playlist.get("id") or "track"


def _entry_frames(entry: ET.Element) -> int:
    try:
        return max(0, int(entry.get("out", "0")) - int(entry.get("in", "0")) + 1)
    except (TypeError, ValueError):
        return 0


def _playlist_length(playlist: ET.Element) -> int:
    total = 0
    for child in list(playlist):
        if child.tag == "entry":
            total += _entry_frames(child)
        elif child.tag == "blank":
            try:
                total += int(child.get("length") or 0)
            except (TypeError, ValueError):
                pass
    return total


def _update_tractor_out(root: ET.Element) -> int:
    tractor = _tractor(root)
    length = max([_playlist_length(p) for p in _playlists(root)] + [0])
    tractor.set("out", str(max(0, length - 1)))
    for track in tractor.findall("track"):
        pl = next((p for p in root.findall("playlist")
                   if p.get("id") == track.get("producer")), None)
        if pl is not None:
            track.set("in", "0")
            track.set("out", str(max(0, _playlist_length(pl) - 1)))
    return length


def _duration_frames(source: Path, fps: float) -> int:
    """Frame count from ffprobe, or a still's default when it cannot be read."""
    ffprobe = _which("ffprobe")
    if ffprobe is None:
        raise BridgeError("ffprobe is required to size a clip; install ffmpeg")
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
            capture_output=True, text=True, timeout=20)
        seconds = float((proc.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        seconds = 0.0
    if seconds <= 0:
        raise BridgeError(f"could not read a duration from {source}")
    return max(1, round(seconds * fps))


def _read_project(path: str | Path) -> tuple[ET.ElementTree, Path]:
    project = Path(path).expanduser().resolve()
    if not project.is_file():
        raise BridgeError(f"project does not exist: {project}")
    try:
        tree = ET.parse(project)
    except (ET.ParseError, OSError) as e:
        raise BridgeError(f"cannot parse {project}: {e}") from e
    root = tree.getroot()
    if root.tag != "mlt":
        raise BridgeError(f"{project} is not an MLT project (root <{root.tag}>)")
    return tree, project


def _write_project(tree: ET.ElementTree, project: Path) -> None:
    try:
        ET.indent(tree, space="  ")
        tree.write(project, encoding="utf-8", xml_declaration=True)
    except OSError as e:
        raise BridgeError(f"cannot write {project}: {e}") from e


def project_create(path: str, *, width: int = 1920, height: int = 1080,
                   fps: float = 30.0, tracks: int = 2,
                   overwrite: bool = False) -> str:
    project = Path(path).expanduser().resolve()
    if project.exists() and not overwrite:
        raise BridgeError(f"{project} already exists (pass overwrite=true)")
    project.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element("mlt", {
        "LC_NUMERIC": "C", "version": "7.42.0",
        "title": project.stem, "producer": "main_bin",
    })
    profile = ET.SubElement(root, "profile", {
        "description": f"ZimZilla {width}x{height} {fps:g} fps",
        "width": str(int(width)), "height": str(int(height)),
        "progressive": "1", "sample_aspect_num": "1",
        "sample_aspect_den": "1", "display_aspect_num": "16",
        "display_aspect_den": "9", "frame_rate_num": str(int(fps)),
        "frame_rate_den": "1", "colorspace": "709",
    })
    _ = profile
    main_bin = ET.SubElement(root, "playlist", {"id": "main_bin"})
    for key, value in (
        ("kdenlive:docproperties.version", "1.1"),
        ("kdenlive:docproperties.kdenliveversion", "26.08.2"),
        ("kdenlive:docproperties.profile",
         f"{int(width)}x{int(height)} {fps:g}"),
        ("kdenlive:docproperties.decimalPoint", "."),
        ("kdenlive:docproperties.activeTrack", "1"),
    ):
        _prop(main_bin, key, value)
    for index in range(max(1, int(tracks))):
        pl = ET.SubElement(root, "playlist", {"id": f"playlist{index}"})
        _prop(pl, "kdenlive:trackName", f"Track {index + 1}")
        _prop(pl, "kdenlive:audio_track", "1" if index == 0 else "0")
    tractor = ET.SubElement(root, "tractor", {"id": "tractor0", "in": "0",
                                               "out": "0"})
    _prop(tractor, "kdenlive:projectTractor", "1")
    for index in range(max(1, int(tracks))):
        ET.SubElement(tractor, "track", {"producer": f"playlist{index}"})
    _write_project(ET.ElementTree(root), project)
    return (f"created {project} ({int(width)}x{int(height)}, {fps:g} fps, "
            f"{max(1, int(tracks))} track(s))")


def project_info(path: str) -> str:
    tree, project = _read_project(path)
    root = tree.getroot()
    profile = _profile(root)
    fps = _fps(profile)
    width = profile.get("width", "?")
    height = profile.get("height", "?")
    producers = {p.get("id"): p for p in root.findall("producer")}
    lines = [
        f"PROJECT: {project}",
        f"FORMAT: {width}x{height}  {fps:g} fps",
    ]
    tracks = _playlists(root)
    total = max([_playlist_length(p) for p in tracks] + [0])
    lines.append(f"DURATION: {total} frames ({total / fps:.2f}s)")
    lines.append(f"TRACKS: {len(tracks)}")
    for index, pl in enumerate(tracks):
        lines.append(f"  [{index}] {_track_name(pl)}  "
                     f"{_playlist_length(pl)} frames")
        position = 0
        for child in list(pl):
            if child.tag == "blank":
                position += int(child.get("length") or 0)
                continue
            if child.tag != "entry":
                continue
            pid = child.get("producer") or ""
            prod = producers.get(pid)
            if prod is None:
                position += _entry_frames(child)
                continue
            resource = ""
            clipname = ""
            for prop in prod.findall("property"):
                if prop.get("name") == "resource":
                    resource = prop.text or ""
                elif prop.get("name") == "kdenlive:clipname":
                    clipname = prop.text or ""
            lines.append(
                f"    {pid}  {clipname or Path(resource).name or '?'}  "
                f"at {position}  in {child.get('in')}  out {child.get('out')}  "
                f"({_entry_frames(child)} frames)")
            if resource:
                lines.append(f"      resource: {resource}")
            position += _entry_frames(child)
    bin_producers = [p for p in producers.values()
                     if p.find("property[@name='kdenlive:clipname']") is not None]
    lines.append(f"BIN: {len(bin_producers)} producer(s)")
    return "\n".join(lines)


def _add_producer(root: ET.Element, source: Path, fps: float, *,
                  service: str, out: int | None = None,
                  duration: float | None = None) -> tuple[str, int, str]:
    if out is None:
        if duration is not None:
            out = max(0, round(float(duration) * fps) - 1)
        else:
            out = _duration_frames(source, fps) - 1
    out = max(0, int(out))
    pid = _producer_id(root)
    producer = ET.Element("producer", {"id": pid, "in": "0", "out": str(out)})
    _prop(producer, "resource", str(source))
    _prop(producer, "mlt_service", service)
    _prop(producer, "kdenlive:clipname", source.name)
    _prop(producer, "kdenlive:folderid", "-1")
    _prop(producer, "kdenlive:id", str(_clip_number(root)))
    _prop(producer, "kdenlive:duration", str(out + 1))
    _prop(producer, "seekable", "1")
    if service == "qimage":
        _prop(producer, "ttl", "1")
    else:
        _prop(producer, "audio_index", "1")
        _prop(producer, "video_index", "0")
    # Producers conventionally precede the bin; Kdenlive's parser is tolerant,
    # but keeping the layout conventional makes the file readable in an editor.
    bin_index = next((i for i, el in enumerate(root)
                      if el.tag == "playlist" and el.get("id") == "main_bin"),
                     len(root))
    root.insert(bin_index, producer)
    return pid, out, source.name


def _entry(producer_id: str, out: int, in_frame: int = 0) -> ET.Element:
    return ET.Element("entry", {"producer": producer_id,
                                "in": str(max(0, int(in_frame))),
                                "out": str(max(0, out))})


def _main_bin(root: ET.Element) -> ET.Element:
    for pl in root.findall("playlist"):
        if pl.get("id") == "main_bin":
            return pl
    raise BridgeError("project has no main_bin playlist")


def _track(root: ET.Element, index: int) -> ET.Element:
    tracks = _playlists(root)
    if not tracks:
        raise BridgeError("project has no timeline tracks")
    if index < 0 or index >= len(tracks):
        raise BridgeError(f"track {index} out of range (0-{len(tracks) - 1})")
    return tracks[index]


def _append_clip(root: ET.Element, track_index: int, pid: str, out: int,
                 position: int | None, in_frame: int = 0) -> int:
    playlist = _track(root, track_index)
    current = _playlist_length(playlist)
    if position is not None and position > current:
        ET.SubElement(playlist, "blank", {"length": str(position - current)})
        current = position
    playlist.append(_entry(pid, out, in_frame))
    return current


def add_clip(project: str, source: str, *, track: int = 0,
             position: int | None = None, in_frame: int = 0,
             out_frame: int | None = None) -> str:
    tree, project_path = _read_project(project)
    root = tree.getroot()
    src = Path(source).expanduser().resolve()
    if not src.is_file():
        raise BridgeError(f"clip does not exist: {src}")
    fps = _fps(_profile(root))
    out = (out_frame if out_frame is not None
           else _duration_frames(src, fps) - 1)
    pid, out, name = _add_producer(root, src, fps, service="avformat-novalidate",
                                   out=out)
    _main_bin(root).append(_entry(pid, out))
    at = _append_clip(root, track, pid, out, position, in_frame)
    _update_tractor_out(root)
    _write_project(tree, project_path)
    return (f"added {name} as {pid} on track {track} at frame {at} "
            f"({out + 1} frames); project saved")


def add_image(project: str, source: str, *, duration: float = 5.0,
              track: int = 0, position: int | None = None) -> str:
    tree, project_path = _read_project(project)
    root = tree.getroot()
    src = Path(source).expanduser().resolve()
    if not src.is_file():
        raise BridgeError(f"image does not exist: {src}")
    fps = _fps(_profile(root))
    out = max(0, round(float(duration) * fps) - 1)
    pid, out, name = _add_producer(root, src, fps, service="qimage", out=out)
    _main_bin(root).append(_entry(pid, out))
    at = _append_clip(root, track, pid, out, position)
    _update_tractor_out(root)
    _write_project(tree, project_path)
    return (f"added image {name} as {pid} on track {track} at frame {at} "
            f"for {duration:g}s; project saved")


def trim_clip(project: str, clip: str, *, in_frame: int | None = None,
              out_frame: int | None = None) -> str:
    """Change the source in/out of every timeline use of a clip.

    This is a trim, not a split: the same producer can appear more than once,
    and each appearance is changed. A split would need a second producer with
    the remaining source range; that is a later operation, not something to
    fake by editing one entry and silently leaving the other.
    """
    tree, project_path = _read_project(project)
    root = tree.getroot()
    target = None
    for producer in root.findall("producer"):
        if producer.get("id") == clip:
            target = producer
            break
        for prop in producer.findall("property"):
            if prop.get("name") == "kdenlive:clipname" and prop.text == clip:
                target = producer
                break
        if target is not None:
            break
    if target is None:
        raise BridgeError(f"no clip named {clip!r} in {project_path.name}")
    pid = target.get("id")
    changed = 0
    for playlist in _playlists(root):
        for entry in playlist.findall("entry"):
            if entry.get("producer") != pid:
                continue
            if in_frame is not None:
                entry.set("in", str(max(0, int(in_frame))))
            if out_frame is not None:
                entry.set("out", str(max(int(entry.get("in", "0")),
                                        int(out_frame))))
            changed += 1
    if not changed:
        raise BridgeError(f"{clip!r} is in the bin but not on a timeline")
    _update_tractor_out(root)
    _write_project(tree, project_path)
    return f"trimmed {changed} timeline use(s) of {pid}; project saved"


def add_effect(project: str, clip: str, service: str,
               params: dict | None = None) -> str:
    tree, project_path = _read_project(project)
    root = tree.getroot()
    target = None
    for producer in root.findall("producer"):
        if producer.get("id") == clip:
            target = producer
            break
        for prop in producer.findall("property"):
            if prop.get("name") == "kdenlive:clipname" and prop.text == clip:
                target = producer
                break
        if target is not None:
            break
    if target is None:
        raise BridgeError(f"no clip named {clip!r} in {project_path.name}")
    filters = target.findall("filter")
    fid = f"filter{len(filters)}"
    flt = ET.SubElement(target, "filter", {
        "id": fid, "in": target.get("in", "0"), "out": target.get("out", "0")})
    _prop(flt, "mlt_service", service)
    for key, value in (params or {}).items():
        _prop(flt, key, value)
    _write_project(tree, project_path)
    return (f"added {service} as {fid} on {target.get('id')} "
            f"({target.get('out')}); project saved")


def add_transition(project: str, *, a_track: int = 0, b_track: int = 1,
                   service: str = "mix", duration: int | None = None) -> str:
    tree, project_path = _read_project(project)
    root = tree.getroot()
    tractor = _tractor(root)
    transitions = tractor.findall("transition")
    tid = f"transition{len(transitions)}"
    out = int(tractor.get("out", "0"))
    tr = ET.SubElement(tractor, "transition", {
        "id": tid, "in": "0", "out": str(max(0, duration if duration is not None else out))})
    _prop(tr, "a_track", str(int(a_track)))
    _prop(tr, "b_track", str(int(b_track)))
    _prop(tr, "mlt_service", service)
    _prop(tr, "always_active", "1")
    _write_project(tree, project_path)
    return f"added {service} transition {tid} between tracks {a_track}/{b_track}"


def render(project: str, output: str, *, crf: int = 20,
           preset: str = "medium") -> str:
    tree, project_path = _read_project(project)
    _ = tree
    out = Path(output).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    melt = _which("melt")
    if melt is None:
        raise BridgeError("melt is not installed (it ships with MLT/Kdenlive)")
    cmd = [
        melt, str(project_path), "-consumer", f"avformat:{out}",
        "vcodec=libx264", f"crf={int(crf)}", f"preset={preset}",
        "acodec=aac", "ab=192k",
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=RENDER_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise BridgeError(f"render timed out after {RENDER_TIMEOUT}s") from e
    except OSError as e:
        raise BridgeError(f"could not run melt: {e}") from e
    if proc.returncode != 0 or not out.is_file():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise BridgeError("render failed: " + (" | ".join(tail) or
                                               f"melt exited {proc.returncode}"))
    seconds = time.monotonic() - started
    size = out.stat().st_size
    return (f"rendered {out} ({size / 1024 / 1024:.1f} MiB) in {seconds:.1f}s "
            f"with melt")


# ---------------------------------------------------------------------------
# desktop adapter — Hyprland first, X11 fallbacks
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _hypr_clients() -> list[dict]:
    hyprctl = _which("hyprctl")
    if hyprctl is None:
        return []
    try:
        proc = _run([hyprctl, "clients", "-j"])
        data = json.loads(proc.stdout or "[]")
        return data if isinstance(data, list) else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def list_windows() -> str:
    clients = _hypr_clients()
    if clients:
        lines = []
        for c in clients:
            if not isinstance(c, dict):
                continue
            lines.append(f"{c.get('address', '?')}  "
                         f"{c.get('class') or '?'}  "
                         f"{str(c.get('title') or '')[:90]}")
        return "\n".join(lines) or "(no windows)"
    wmctrl = _which("wmctrl")
    if wmctrl:
        try:
            proc = _run([wmctrl, "-l"])
            return proc.stdout.strip() or "(no windows)"
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise BridgeError("no window list backend (need Hyprland's hyprctl or wmctrl)")


def _match_window(spec: str) -> dict | None:
    needle = (spec or "").strip().lower()
    if not needle:
        return None
    clients = _hypr_clients()
    for c in clients:
        if not isinstance(c, dict):
            continue
        hay = " ".join(str(c.get(k) or "") for k in
                       ("address", "class", "initialClass", "title"))
        if needle in hay.lower():
            return c
    return None


def focus_window(spec: str, window_class: str = "") -> str:
    """Focus a window by class/title/address fragment.

    Hyprland 0.56 moved dispatchers to its Lua API: ``hl.dsp.focus({window=…})``
    is the supported form. The match is sanitised before it reaches that string
    — a title containing a quote must not become Lua code.
    """
    match = (spec or window_class or "").strip()
    if not match:
        raise BridgeError("give a window class, title or address")
    client = _match_window(match)
    if client is not None:
        match = f"address:{client.get('address')}"
    elif not re.fullmatch(r"[A-Za-z0-9 ._:@/+\-]{1,120}", match):
        raise BridgeError(f"unsafe window match: {match!r}")
    elif ":" not in match:
        match = f"class:{match}"
    hyprctl = _which("hyprctl")
    if hyprctl:
        safe = match.replace('"', "")
        code = (f'hl.dispatch(hl.dsp.focus({{window="{safe}"}}))')
        try:
            if _send_control({"action": "focus"}):
                return "focused the embedded app"
            proc = _run([hyprctl, "eval", code])
            if proc.returncode == 0 and "error:" not in (proc.stdout or "").lower():
                return f"focused {match}"
        except (OSError, subprocess.TimeoutExpired):
            pass
    wmctrl = _which("wmctrl")
    if wmctrl:
        try:
            proc = _run([wmctrl, "-a", match])
            if proc.returncode == 0:
                return f"focused {match}"
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise BridgeError(f"could not focus a window matching {match!r}")


def screenshot(path: str = "", *, region: str = "",
               default_dir: Path | None = None) -> str:
    if path:
        out = Path(path).expanduser()
    else:
        base = Path(default_dir) if default_dir else (
            Path(tempfile.gettempdir()) / "zimzilla-bridge-shots")
        out = base / f"screen-{int(time.time())}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    grim = _which("grim")
    if grim:
        cmd = [grim]
        if region:
            if not re.fullmatch(r"\d+,\d+ \d+x\d+", region):
                raise BridgeError("region must look like 'x,y WxH'")
            cmd += ["-g", region]
        cmd.append(str(out))
        try:
            proc = _run(cmd, timeout=30)
            if proc.returncode == 0 and out.is_file():
                return f"screenshot saved: {out}"
        except (OSError, subprocess.TimeoutExpired):
            pass
    convert = _which("import")
    if convert:
        try:
            proc = _run([convert, "-window", "root", str(out)], timeout=30)
            if proc.returncode == 0 and out.is_file():
                return f"screenshot saved: {out}"
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise BridgeError("no screenshot backend (need grim on Wayland or ImageMagick import)")


def _send_control(message: dict, *, timeout: float = 2.0) -> bool:
    """Send an input command to the MCP pane's embedded terminal, if any."""
    path = _CONTROL_SOCKET
    if not path or not Path(path).exists():
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(json.dumps(message).encode("utf-8"))
        sock.close()
        return True
    except OSError:
        return False


def type_text(text: str) -> str:
    """Type into the focused window with wtype (Wayland) or xdotool (X11)."""
    if not text:
        raise BridgeError("nothing to type")
    if _send_control({"action": "text", "text": text}):
        return f"typed {len(text)} character(s) into the embedded app"
    wtype = _which("wtype")
    if wtype:
        try:
            proc = _run([wtype, "--", text], timeout=30)
            if proc.returncode == 0:
                return f"typed {len(text)} character(s)"
        except (OSError, subprocess.TimeoutExpired):
            pass
    xdotool = _which("xdotool")
    if xdotool:
        try:
            proc = _run([xdotool, "type", "--", text], timeout=30)
            if proc.returncode == 0:
                return f"typed {len(text)} character(s)"
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise BridgeError("no typing backend (need wtype on Wayland or xdotool on X11)")


_KEY_NAMES = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": "space", "backspace": "BackSpace",
    "delete": "Delete", "home": "Home", "end": "End", "pageup": "Page_Up",
    "pagedown": "Page_Down", "up": "Up", "down": "Down", "left": "Left",
    "right": "Right", "plus": "plus", "minus": "minus",
}
_MODS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift",
         "alt": "alt", "super": "logo", "meta": "logo", "logo": "logo"}


def press_keys(keys: str) -> str:
    """Press a chord such as ``ctrl+s`` or ``ctrl+shift+z``.

    Several chords can be space-separated (``ctrl+s Return``). Both wtype and
    xdotool are supported; wtype takes presses and releases explicitly.
    """
    if _send_control({"action": "keys", "keys": keys}):
        return f"sent {keys!r} to the embedded app"
    wtype = _which("wtype")
    xdotool = _which("xdotool")
    if not wtype and not xdotool:
        raise BridgeError("no key backend (need wtype or xdotool)")
    chords = [c for c in (keys or "").split() if c]
    if not chords:
        raise BridgeError("no keys given")
    sent = 0
    for chord in chords:
        parts = [p for p in chord.split("+") if p]
        if not parts:
            continue
        mods = [_MODS[p.lower()] for p in parts[:-1] if p.lower() in _MODS]
        key = _KEY_NAMES.get(parts[-1].lower(), parts[-1])
        if wtype:
            cmd = [wtype]
            for mod in mods:
                cmd += ["-M", mod]
            cmd += ["-k", key]
            for mod in reversed(mods):
                cmd += ["-m", mod]
            try:
                if _run(cmd, timeout=15).returncode != 0:
                    continue
                sent += 1
                continue
            except (OSError, subprocess.TimeoutExpired):
                continue
        if xdotool:
            spec = "+".join(mods + [key])
            try:
                if _run([xdotool, "key", "--", spec], timeout=15).returncode == 0:
                    sent += 1
            except (OSError, subprocess.TimeoutExpired):
                continue
    if not sent:
        raise BridgeError(f"could not send {keys!r}")
    return f"sent {sent} key chord(s): {keys}"


def ocr(path: str = "", *, default_dir: Path | None = None) -> str:
    tesseract = _which("tesseract")
    if tesseract is None:
        raise BridgeError("tesseract is not installed")
    image = Path(path).expanduser() if path else None
    if image is None:
        shot = screenshot(default_dir=default_dir)
        image = Path(shot.split(": ", 1)[-1])
    try:
        proc = _run([tesseract, str(image), "stdout"], timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BridgeError(f"tesseract failed: {e}") from e
    text = (proc.stdout or "").strip()
    return text or "(no text recognised)"


def launch_app(command: list[str]) -> str:
    if _send_control({"action": "focus"}):
        return "the app is already running in the MCP pane"
    if not command:
        raise BridgeError("bridge has no launch command")
    if not isinstance(command, list) or not all(isinstance(c, str) for c in command):
        raise BridgeError("launch command must be a list of strings")
    exe = _which(command[0])
    if exe is None:
        raise BridgeError(f"command not found: {command[0]}")
    try:
        proc = subprocess.Popen([exe, *command[1:]],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as e:
        raise BridgeError(f"could not launch {command[0]}: {e}") from e
    return f"launched {command[0]} (pid {proc.pid})"


def open_in_editor(project: str, command: list[str]) -> str:
    project_path = Path(project).expanduser().resolve()
    if not project_path.is_file():
        raise BridgeError(f"project does not exist: {project_path}")
    if not command:
        raise BridgeError("bridge has no launch command")
    exe = _which(command[0])
    if exe is None:
        raise BridgeError(f"command not found: {command[0]}")
    try:
        proc = subprocess.Popen([exe, *command[1:], str(project_path)],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError as e:
        raise BridgeError(f"could not launch {command[0]}: {e}") from e
    time.sleep(1.0)
    return f"opened {project_path} in {command[0]} (pid {proc.pid})"


# ---------------------------------------------------------------------------
# tool table
# ---------------------------------------------------------------------------

def _schema(name: str, description: str, properties: dict,
            required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            **({"required": required} if required else {}),
        },
    }


def _string(desc: str, default: str | None = None) -> dict:
    out = {"type": "string", "description": desc}
    if default is not None:
        out["default"] = default
    return out


def _integer(desc: str, default: int | None = None) -> dict:
    out = {"type": "integer", "description": desc}
    if default is not None:
        out["default"] = default
    return out


def _tool_table(cfg: dict) -> dict[str, tuple[dict, Any]]:
    command = cfg.get("command") or []
    window_class = str(cfg.get("window_class") or cfg.get("app") or "")
    default_project = str(cfg.get("project") or "")
    # Screenshots land beside the bridge (under ~/.zimzilla), which is the
    # state dir ZimZilla's view_image/show_image can read. /tmp is outside it.
    shot_dir = (Path(cfg["_dir"]) / "shots") if cfg.get("_dir") else None
    tools: dict[str, tuple[dict, Any]] = {}

    def add(schema: dict, fn) -> None:
        tools[schema["name"]] = (schema, fn)

    # ---- generic desktop surface, useful for every adapter
    add(_schema(
        "app_list_windows",
        "List the open desktop windows with address, class and title.",
        {}), lambda a: list_windows())

    add(_schema(
        "app_focus",
        f"Focus a window. With no match, focuses {window_class or 'the bridged app'}.",
        {"match": _string("Class, title or address fragment (optional).")}),
        lambda a: focus_window(str(a.get("match") or ""), window_class))

    add(_schema(
        "app_screenshot",
        "Capture the screen (or a region) to a PNG and return its path. The "
        "path can be passed to ZimZilla's view_image or show_image.",
        {"path": _string("Where to write the PNG (optional)."),
         "region": _string("Optional 'x,y WxH' region.")}),
        lambda a: screenshot(str(a.get("path") or ""),
                             region=str(a.get("region") or ""),
                             default_dir=shot_dir))

    add(_schema(
        "app_type",
        "Type text into the focused window (Wayland wtype / X11 xdotool).",
        {"text": _string("Text to type.")}, ["text"]),
        lambda a: type_text(str(a.get("text") or "")))

    add(_schema(
        "app_keys",
        "Press keyboard shortcut(s), e.g. 'ctrl+s' or 'ctrl+shift+z Return'.",
        {"keys": _string("Chord(s), space separated.")}, ["keys"]),
        lambda a: press_keys(str(a.get("keys") or "")))

    add(_schema(
        "app_ocr",
        "OCR a screenshot or image and return the text.",
        {"path": _string("Image path (optional; captures the screen if omitted).")}),
        lambda a: ocr(str(a.get("path") or ""), default_dir=shot_dir))

    add(_schema(
        "app_launch",
        "Launch the application this bridge was generated for.",
        {}), lambda a: launch_app(command))

    if cfg.get("adapter") == "kdenlive":
        add(_schema(
            "kdenlive_create_project",
            "Create a new Kdenlive/MLT project with tracks.",
            {"path": _string("Project path (.kdenlive)."),
             "width": _integer("Frame width", 1920),
             "height": _integer("Frame height", 1080),
             "fps": {"type": "number", "description": "Frame rate", "default": 30},
             "tracks": _integer("Number of tracks", 2),
             "overwrite": {"type": "boolean", "description": "Replace an existing file"}},
            ["path"]),
            lambda a: project_create(
                str(_project(a, default_project)), width=int(a.get("width") or 1920),
                height=int(a.get("height") or 1080), fps=float(a.get("fps") or 30),
                tracks=int(a.get("tracks") or 2),
                overwrite=bool(a.get("overwrite"))))

        add(_schema(
            "kdenlive_project_info",
            "Read a project's profile, tracks, clips, positions and duration.",
            {"project": _string("Project path (optional if configured).")}),
            lambda a: project_info(str(_project(a, default_project))))

        add(_schema(
            "kdenlive_add_clip",
            "Add a video/audio file to a timeline track (append or at a frame).",
            {"project": _string("Project path (optional)."),
             "source": _string("Clip file to add."),
             "track": _integer("Track index", 0),
             "position": _integer("Frame to start at (optional; appends if omitted)."),
             "out_frame": _integer("Last source frame (optional; probe if omitted).")},
            ["source"]),
            lambda a: add_clip(
                str(_project(a, default_project)), str(a.get("source") or ""),
                track=int(a.get("track") or 0),
                position=(int(a["position"]) if a.get("position") is not None else None),
                out_frame=(int(a["out_frame"]) if a.get("out_frame") is not None else None)))

        add(_schema(
            "kdenlive_add_image",
            "Add a still image to a track for N seconds (MLT qimage producer).",
            {"project": _string("Project path (optional)."),
             "source": _string("Image file."),
             "duration": {"type": "number", "description": "Seconds", "default": 5},
             "track": _integer("Track index", 0),
             "position": _integer("Frame to start at (optional).")},
            ["source"]),
            lambda a: add_image(
                str(_project(a, default_project)), str(a.get("source") or ""),
                duration=float(a.get("duration") or 5),
                track=int(a.get("track") or 0),
                position=(int(a["position"]) if a.get("position") is not None else None)))

        add(_schema(
            "kdenlive_trim_clip",
            "Trim every timeline use of a clip to new source in/out frames.",
            {"project": _string("Project path (optional)."),
             "clip": _string("Producer id or clip name."),
             "in_frame": _integer("New source in frame (optional)."),
             "out_frame": _integer("New source out frame (optional).")},
            ["clip"]),
            lambda a: trim_clip(
                str(_project(a, default_project)), str(a.get("clip") or ""),
                in_frame=(int(a["in_frame"]) if a.get("in_frame") is not None else None),
                out_frame=(int(a["out_frame"]) if a.get("out_frame") is not None else None)))

        add(_schema(
            "kdenlive_add_effect",
            "Attach an MLT filter to a clip, e.g. service='brightness' with "
            "params={'level': 0.5}, or service='volume' with level.",
            {"project": _string("Project path (optional)."),
             "clip": _string("Producer id or clip name."),
             "service": _string("MLT filter service, e.g. brightness, volume, boxblur."),
             "params": {"type": "object", "description": "Filter properties."}},
            ["clip", "service"]),
            lambda a: add_effect(
                str(_project(a, default_project)), str(a.get("clip") or ""),
                str(a.get("service") or ""),
                a.get("params") if isinstance(a.get("params"), dict) else {}))

        add(_schema(
            "kdenlive_add_transition",
            "Add an MLT transition between two tracks (default: mix).",
            {"project": _string("Project path (optional)."),
             "a_track": _integer("A track", 0),
             "b_track": _integer("B track", 1),
             "service": _string("MLT transition service", "mix"),
             "duration": _integer("Frames (optional; whole project if omitted).")}),
            lambda a: add_transition(
                str(_project(a, default_project)), a_track=int(a.get("a_track") or 0),
                b_track=int(a.get("b_track") or 1),
                service=str(a.get("service") or "mix"),
                duration=(int(a["duration"]) if a.get("duration") is not None else None)))

        add(_schema(
            "kdenlive_render",
            "Render the project with melt (libx264/AAC) and return the output path.",
            {"project": _string("Project path (optional)."),
             "output": _string("Output video path (.mp4)."),
             "crf": _integer("x264 quality, lower is better", 20),
             "preset": _string("x264 preset", "medium")},
            ["output"]),
            lambda a: render(
                str(_project(a, default_project)), str(a.get("output") or ""),
                crf=int(a.get("crf") or 20),
                preset=str(a.get("preset") or "medium")))

        add(_schema(
            "kdenlive_open",
            "Open the project in the Kdenlive GUI.",
            {"project": _string("Project path (optional).")}),
            lambda a: open_in_editor(str(_project(a, default_project)), command))
    return tools


def _project(args: dict, default: str) -> str:
    value = str(args.get("project") or default or "").strip()
    if not value:
        raise BridgeError("no project path — pass one or configure the bridge")
    return value


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

def _load_config(path: str | Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def main(adapter: str = "desktop", config_path: str | Path | None = None) -> int:
    """Run the stdio MCP server. Blocking; exits on stdin EOF."""
    cfg = _load_config(config_path)
    cfg["adapter"] = adapter or cfg.get("adapter") or "desktop"
    global _CONTROL_SOCKET
    if config_path:
        cfg["_dir"] = str(Path(config_path).resolve().parent)
        _CONTROL_SOCKET = str(cfg.get("control_socket")
                              or (Path(config_path).resolve().parent
                                  / "control.sock"))
    tools = _tool_table(cfg)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        msg_id = message.get("id")
        method = str(message.get("method") or "")
        if msg_id is None:
            # Notification — tools/list_changed and initialized are the only
            # ones this server receives, and neither needs a reply.
            continue
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"zimzilla-bridge-{cfg['adapter']}",
                               "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": [schema for schema, _fn in tools.values()]}
        elif method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            found = tools.get(name)
            if found is None:
                print(json.dumps(_error(msg_id, -32601, f"no tool {name}")),
                      flush=True)
                continue
            args = params.get("arguments") or {}
            try:
                text = found[1](args if isinstance(args, dict) else {})
                is_error = False
            except BridgeError as e:
                text, is_error = str(e), True
            except Exception as e:  # noqa: BLE001 - a tool must not kill the server
                text, is_error = f"{type(e).__name__}: {e}", True
            result = {"content": [{"type": "text", "text": text}],
                      "isError": is_error}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps(_error(msg_id, -32601, f"unknown method {method}")),
                  flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}),
              flush=True)
    return 0


if __name__ == "__main__":
    # A generated launcher calls ``main(adapter, config)``; this direct path is
    # for inspecting the server by hand.
    _cfg = os.environ.get("ZIMZILLA_BRIDGE_CONFIG")
    raise SystemExit(main(os.environ.get("ZIMZILLA_BRIDGE_ADAPTER", "desktop"), _cfg))
