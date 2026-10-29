"""Text-to-image generation, for `/image-gen` and the ``generate_image`` tool.

The harness talks to Logfare for images. Logfare is the default route, its key
is already the one most sessions carry, and its public status API names which
of its models serve ``images/generations`` — so the catalog is *read*, not
baked in, exactly as ``logfare.py`` does for chat models.

Why a separate module rather than a branch in ``openai_compat.py``: an image
request is a different shape from a chat turn. It has no messages, no tools and
no streaming; it takes a prompt and a size and returns bytes. Threading that
through the chat transport would put a second protocol inside the one module
that exists to translate the first.

The endpoint is always Logfare's direct ``/v1`` root, even when the session is
pointed at a local proxy or a different upstream. A chat source that does not
serve images cannot answer this, and falling back to one that does is more
useful than failing with a 404 from NVIDIA. When the session *is* Logfare, the
session's own credential is used; otherwise the stored Logfare key is.

Nothing here echoes a key. A failure names the status code and the upstream's
own message, with the token redacted if the upstream ever reflects it.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .sandbox import SandboxError, display_path, resolve_in_jail

#: The model used when the operator does not name one. ``flux-1-schnell`` is
#: the fast/cheap member of Logfare's Flux line and is the sensible default for
#: "make me a picture" — ``--model`` reaches the larger ones. The catalog can
#: move, so a 404 says to run ``/image-gen --models`` rather than guessing.
DEFAULT_MODEL = "flux-1-schnell"

#: OpenAI's documented square size, and the one every Flux model on the
#: gateway accepts. A caller may ask for anything matching ``WxH``.
DEFAULT_SIZE = "1024x1024"

#: Where generated images land when the caller gives no ``dest``. Relative to
#: the session's working directory, so the files show up where the operator is
#: already looking instead of in a dotdir they have to go hunting for.
DEFAULT_DIR = "images"

#: Bounds that keep a typo from becoming a multi-minute render. The gateway
#: rejects most of these anyway; refusing locally is faster and clearer.
MIN_DIMENSION = 64
MAX_DIMENSION = 2048

#: A prompt is text, not a file. 4000 characters is far past any real image
#: prompt and short enough that a pasted document is refused by name.
MAX_PROMPT = 4000

#: The largest image this will write. A 2048x2048 PNG is a few megabytes;
#: 40 MB leaves generous room and still bounds a hostile or broken response.
MAX_BYTES = 40 * 1024 * 1024

#: The largest JSON envelope accepted. Base64 inflates the image by about a
#: third, so this is deliberately larger than ``MAX_BYTES``: capping the reply
#: at the image limit would refuse a legitimate image just under the ceiling.
MAX_REPLY = 64 * 1024 * 1024

#: Generation is slow — a large Flux render is routinely 30-60 seconds — so the
#: read timeout is generous. It bounds the gap between bytes, not the whole
#: request, and urllib applies it per read.
DEFAULT_TIMEOUT = 180.0

#: Downloading the returned URL is a plain CDN fetch and should be quick.
DOWNLOAD_TIMEOUT = 60.0

_SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")


class ImageGenError(Exception):
    """A generation failed for a reason worth showing the operator."""


@dataclass(frozen=True)
class Image:
    """One generated image that has been written to disk."""

    path: Path
    model: str
    size: str
    data: bytes
    elapsed: float
    revised_prompt: str = ""

    @property
    def human_size(self) -> str:
        """The file size in the unit a person would use for it."""
        if len(self.data) >= 1024 * 1024:
            return f"{len(self.data) / (1024 * 1024):.1f} MB"
        return f"{len(self.data) / 1024:.0f} KB"

    def summary(self) -> str:
        """A one-line description, without the path."""
        bits = [self.model, self.size, self.human_size,
                f"{self.elapsed:.1f}s"]
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# Where the request goes, and with which key
# ---------------------------------------------------------------------------

def _api_root(base_url: str) -> str:
    """The ``/v1`` root under *base_url*, without doubling the ``/v1``.

    ``Config.base_url`` is ``https://logfare.ai/v1`` by default, but a profile
    written by hand may name the origin alone. Both compose to the same image
    endpoint; ``https://logfare.ai/v1/v1/images/generations`` does not.
    """
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return base
    return base + "/v1"


def credentials(cfg=None) -> tuple[str, str, str] | None:
    """``(api_root, token, origin)`` for image generation, or ``None``.

    The session's credential wins when the session is *on* Logfare, because
    that is the key the operator is already paying with. Otherwise the stored
    Logfare profile is used: image generation is a Logfare capability here, and
    a session on NVIDIA or a local proxy has no image route of its own.

    ``origin`` is a path or an env var name, for saying where the key came
    from without showing it.
    """
    from . import logfare as lf

    base = str(getattr(cfg, "base_url", "") or "")
    if lf.is_logfare(base):
        token = (getattr(cfg, "auth_token", None)
                 or getattr(cfg, "api_key", None) or "").strip()
        if token:
            return _api_root(base), token, "session"
    creds = lf.load_credentials()
    if creds is None:
        return None
    token, _model, origin = creds
    return lf.DIRECT_BASE_URL, token, origin


def available(cfg=None) -> bool:
    """Whether an image route with a key exists. Never touches the network."""
    return credentials(cfg) is not None


def list_models(*, use_cache: bool = True):
    """``(models, error)`` for Logfare's image catalog.

    ``models`` is ``None`` on a failed fetch, ``[]`` for a genuinely empty
    catalog. The entries are ``logfare.Model`` objects, so the caller gets the
    same status and uptime fields the chat picker shows.
    """
    from . import logfare as lf

    return lf.fetch_image_models(use_cache=use_cache)


# ---------------------------------------------------------------------------
# Argument parsing (shared by the command and its tests)
# ---------------------------------------------------------------------------

@dataclass
class Options:
    prompt: str = ""
    model: str | None = None
    size: str | None = None
    dest: str | None = None
    list_models: bool = False
    help: bool = False


def _unquote(text: str) -> str:
    """Strip one matching pair of quotes from *text*, if it has one."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def parse_args(args: list[str]) -> tuple[Options, str]:
    """Parse ``/image-gen`` arguments. Returns ``(options, error)``.

    ``/image-gen "a cat on a mat"`` reaches here as the tokens the TUI already
    split, quotes included. Re-joining and running ``shlex`` restores the
    grouping a shell would have given it — so a quoted prompt stays one
    argument — while a prompt with an apostrophe (``don't``) falls back to the
    plain join rather than failing on an unbalanced quote.
    """
    opts = Options()
    raw = " ".join(args)
    try:
        import shlex
        tokens = shlex.split(raw)
    except ValueError:
        tokens = list(args)

    rest: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            rest.extend(tokens[i + 1:])
            break
        if token in ("-h", "--help"):
            opts.help = True
            i += 1
            continue
        if token == "--models":
            opts.list_models = True
            i += 1
            continue
        name, sep, value = token.partition("=")
        if name in ("-m", "--model", "-s", "--size", "-o", "--dest"):
            if not sep:
                if i + 1 >= len(tokens):
                    return opts, f"{name} needs a value"
                value = tokens[i + 1]
                i += 2
            else:
                i += 1
            value = value.strip()
            if not value:
                return opts, f"{name} needs a value"
            if name in ("-m", "--model"):
                opts.model = value
            elif name in ("-s", "--size"):
                opts.size = value
            else:
                opts.dest = value
            continue
        if token.startswith("--") and len(token) > 2:
            return opts, f"unknown option: {token}"
        rest.append(token)
        i += 1

    opts.prompt = _unquote(" ".join(rest))
    return opts, ""


USAGE = (
    "usage: /image-gen [--model M] [--size WxH] [--dest PATH] <prompt>\n"
    "       /image-gen --models   list the image models Logfare serves\n"
    "       /image-gen --help     this text"
)


def usage_lines() -> list[str]:
    return USAGE.splitlines()


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _validate_size(size: str | None) -> str:
    value = (size or DEFAULT_SIZE).strip().lower()
    match = _SIZE_RE.match(value)
    if not match:
        raise ImageGenError(
            f"size must look like 1024x1024, not {value!r}")
    width, height = int(match.group(1)), int(match.group(2))
    for dim in (width, height):
        if not MIN_DIMENSION <= dim <= MAX_DIMENSION:
            raise ImageGenError(
                f"each dimension must be {MIN_DIMENSION}-{MAX_DIMENSION}, "
                f"not {dim}")
    return f"{width}x{height}"


def _error_from_body(body: bytes, code: int, token: str) -> str:
    """A readable failure, with the upstream's own message where it gave one."""
    text = (body or b"").decode("utf-8", "replace").strip()
    message = ""
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("detail") or "")
        elif isinstance(err, str):
            message = err
        if not message:
            message = str(payload.get("message") or payload.get("detail") or "")
    if not message:
        message = text[:300]
    if token and token in message:
        message = message.replace(token, "[REDACTED_TOKEN]")

    if code in (401, 403):
        hint = "the image route rejected the key — check /logfare-api-setup"
    elif code == 404:
        hint = "the model or endpoint was not found — /image-gen --models lists what is up"
    elif code == 429:
        hint = "rate limited — retry in a little while"
    else:
        hint = "the image route refused the request"
    detail = f": {message}" if message else ""
    return f"{hint} (HTTP {code}){detail}"


def _post(url: str, token: str, body: dict, timeout: float) -> dict:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "authorization": f"Bearer {token}",
            "user-agent": "zimzilla/image-gen",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_REPLY + 1)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(64 * 1024)
        except Exception:  # noqa: BLE001 - the status is the useful part
            raw = b""
        raise ImageGenError(_error_from_body(raw, e.code, token)) from None
    except (urllib.error.URLError, OSError) as e:
        raise ImageGenError(f"could not reach the image route: {e}") from None
    if len(raw) > MAX_REPLY:
        raise ImageGenError("the image route returned an implausibly large reply")
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ImageGenError("the image route returned something that is not JSON") from None
    if not isinstance(data, dict):
        raise ImageGenError("the image route returned an unexpected JSON shape")
    return data


def _first_entry(payload: dict) -> dict:
    data = payload.get("data")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        # Some gateways put the single result at the top level.
        if any(k in payload for k in ("b64_json", "url", "image")):
            return payload
        raise ImageGenError("the image route returned no image")
    return data[0]


def _decode_b64(value: str) -> bytes:
    text = value.strip()
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    try:
        return base64.b64decode(text, validate=False)
    except Exception as e:  # noqa: BLE001 - any decode failure is one message
        raise ImageGenError(f"the image route returned undecodable base64: {e}") from None


def _same_host(url: str, api_root: str) -> bool:
    a = urllib.parse.urlsplit(url)
    b = urllib.parse.urlsplit(api_root)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


def _blocked_host(host: str) -> bool:
    """Whether a URL host is local infrastructure rather than a public CDN.

    The URL comes from the image gateway, not from the model, so this is a
    bound on a broken or hostile response rather than a full SSRF defence: an
    IP literal pointing at the loopback, a private range or the metadata
    service is refused. Hostnames are not resolved here — a CDN name is the
    ordinary case, and a resolution check on every download would cost more
    than it buys.
    """
    name = (host or "").strip().lower()
    if name in {"localhost", "localhost.localdomain"}:
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return not ip.is_global


def _download(url: str, api_root: str, token: str, timeout: float) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ImageGenError(f"the image route returned an unsupported URL: {url[:80]}")
    if _blocked_host(parsed.hostname or ""):
        raise ImageGenError(
            "the image route returned a URL pointing at local infrastructure")
    if not parsed.netloc:
        # A relative path from the gateway, resolved against its own origin.
        url = urllib.parse.urljoin(api_root + "/", url)
    headers = {"user-agent": "zimzilla/image-gen", "accept": "image/*"}
    # Only hand the key to the host that issued the URL. An image CDN has no
    # business seeing the operator's credential.
    if _same_host(url, api_root):
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ImageGenError(f"could not download the generated image (HTTP {e.code})") from None
    except (urllib.error.URLError, OSError) as e:
        raise ImageGenError(f"could not download the generated image: {e}") from None
    if len(raw) > MAX_BYTES:
        raise ImageGenError("the generated image is larger than 40 MB")
    return raw


def _ext_for(data: bytes, default: str = ".png") -> str:
    """The extension the bytes actually are, not the one we hoped for."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return default


def slugify(prompt: str, limit: int = 48) -> str:
    """A filesystem-safe stem from a prompt, bounded and never empty."""
    text = re.sub(r"[^a-z0-9]+", "-", (prompt or "").lower()).strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text[:limit].rstrip("-") or "image"


def _resolve_target(dest: str | None, workdir, unsafe: bool) -> tuple[Path | None, Path]:
    """Validate where the image will go *before* spending a generation.

    Returns ``(explicit_target, folder)``. A ``dest`` outside the jail is
    refused here rather than after a paid request has already returned bytes.
    """
    if dest:
        target = resolve_in_jail(workdir, dest, unsafe)
        return target, target.parent
    return None, resolve_in_jail(workdir, DEFAULT_DIR, unsafe)


def _write(data: bytes, *, prompt: str, target: Path | None, folder: Path) -> Path:
    """Write *data* to ``target`` or a generated name under ``folder``."""
    ext = _ext_for(data)
    if target is not None:
        if not target.suffix:
            target = target.with_name(target.name + ext)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    folder.mkdir(parents=True, exist_ok=True)
    stem = slugify(prompt)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = folder / f"{stem}-{stamp}{ext}"
    n = 2
    while target.exists():
        target = folder / f"{stem}-{stamp}-{n}{ext}"
        n += 1
    target.write_bytes(data)
    return target


def generate(
    prompt: str,
    *,
    model: str | None = None,
    size: str | None = None,
    dest: str | None = None,
    cfg=None,
    workdir=None,
    unsafe: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    download_timeout: float = DOWNLOAD_TIMEOUT,
) -> Image:
    """Generate one image from *prompt* and write it to disk.

    Raises :class:`ImageGenError` for every ordinary failure — no key, a bad
    size, an HTTP error, an undecodable reply, a refused path. The caller is a
    tool or a TUI command, neither of which wants a traceback.
    """
    text = (prompt or "").strip()
    if not text:
        raise ImageGenError("a prompt is required")
    if len(text) > MAX_PROMPT:
        raise ImageGenError(
            f"prompt is {len(text)} characters; the limit is {MAX_PROMPT}")

    creds = credentials(cfg)
    if creds is None:
        raise ImageGenError(
            "no image route — store a Logfare key with "
            "/logfare-api-setup <key>")

    api_root, token, _origin = creds
    chosen_model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    chosen_size = _validate_size(size)

    if workdir is None:
        workdir = getattr(cfg, "workdir", None) or Path.cwd()
    try:
        target, folder = _resolve_target(dest, workdir, unsafe)
    except SandboxError as e:
        raise ImageGenError(str(e)) from None

    body = {
        "model": chosen_model,
        "prompt": text,
        "n": 1,
        "size": chosen_size,
    }
    started = time.monotonic()
    payload = _post(f"{api_root}/images/generations", token, body, timeout)
    entry = _first_entry(payload)

    revised = str(entry.get("revised_prompt") or "")
    data: bytes | None = None
    for key in ("b64_json", "image", "b64"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            data = _decode_b64(value)
            break
    if data is None:
        url = entry.get("url")
        if isinstance(url, str) and url.strip():
            value = url.strip()
            # Some gateways inline the image in the url field as a data URI.
            if value.startswith("data:"):
                data = _decode_b64(value)
            else:
                data = _download(value, api_root, token, download_timeout)
    if data is None:
        raise ImageGenError("the image route returned no image data")
    if not data:
        raise ImageGenError("the image route returned an empty image")
    if len(data) > MAX_BYTES:
        raise ImageGenError("the generated image is larger than 40 MB")

    path = _write(data, prompt=text, target=target, folder=folder)

    return Image(
        path=path,
        model=str(payload.get("model") or chosen_model),
        size=chosen_size,
        data=data,
        elapsed=time.monotonic() - started,
        revised_prompt=revised,
    )


def describe(image: Image, workdir=None) -> str:
    """The text a tool returns and the TUI prints, without the raw bytes."""
    shown = display_path(workdir, image.path) if workdir is not None else str(image.path)
    lines = [f"image saved: {shown}", image.summary()]
    if image.revised_prompt:
        lines.append(f"revised prompt: {image.revised_prompt}")
    return "\n".join(lines)
