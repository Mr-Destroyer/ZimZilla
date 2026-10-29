"""Regression suite for `/image-gen` and the ``generate_image`` tool.

Run:  ~/.local/share/zimzilla/.venv/bin/python tests/test_imagegen.py

What this pins is the contract the feature was asked for:

1. **A prompt becomes a file.** The request goes to
   ``/v1/images/generations`` with the model, prompt and size; a ``b64_json``
   reply is decoded and written, and a ``url`` reply is downloaded. The saved
   bytes are the image, not a placeholder.
2. **The model can reach it.** ``generate_image`` is advertised when a key
   exists, withheld when none does, and refused by name in plan mode.
3. **The operator can reach it.** ``/image-gen`` parses a quoted prompt,
   ``--model``/``--size``/``--dest``, ``--models`` and ``--help``, and prints
   usage rather than doing anything when given no prompt.
4. **It fails safely.** A bad size, an HTTP error and a path outside the jail
   are ordinary messages; a key is never echoed.

No test touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import imagegen as IG  # noqa: E402
from zimzilla import logfare as LF  # noqa: E402
from zimzilla import tools  # noqa: E402
from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.complete import SLASH_COMMANDS  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 120
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 120
TOKEN = "lfu_test_do_not_echo"


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


class _Resp(io.BytesIO):
    """Enough of an http.client.HTTPResponse for urlopen's context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json(payload: dict) -> _Resp:
    return _Resp(json.dumps(payload).encode())


def _cfg(tmp: Path, **kw) -> Config:
    kw.setdefault("base_url", "https://logfare.ai/v1")
    kw.setdefault("auth_token", TOKEN)
    return Config.from_env(workdir=tmp, **kw)


def _stub_generation(payload: dict | None = None, *, url_body: bytes | None = None,
                     error: Exception | None = None):
    """A urlopen that answers the POST, and optionally the URL download."""
    calls: list[tuple[str, dict, bytes]] = []

    def _open(req, timeout=None):
        calls.append((req.full_url, dict(req.headers), req.data or b""))
        if error is not None:
            raise error
        if req.full_url.endswith("/images/generations"):
            return _json(payload or {
                "model": "flux-1-schnell",
                "data": [{"b64_json": base64.b64encode(PNG).decode()}],
            })
        return _Resp(url_body if url_body is not None else JPEG)

    return _open, calls


# ---------------------------------------------------------------------------
# 1 — argument parsing
# ---------------------------------------------------------------------------

def test_parse() -> None:
    opts, err = IG.parse_args(['"a cat on a mat"'])
    check("parse: a quoted prompt is one string", err == "" and opts.prompt == "a cat on a mat",
          repr(opts.prompt))

    opts, err = IG.parse_args(["--model", "flux-2-dev", "--size", "512x512",
                               "a", "cat"])
    check("parse: flags are read", err == "" and opts.model == "flux-2-dev"
          and opts.size == "512x512", str(opts))
    check("parse: the remaining words are the prompt", opts.prompt == "a cat",
          repr(opts.prompt))

    opts, _ = IG.parse_args(["--model=flux-2-dev", "x"])
    check("parse: --flag=value works", opts.model == "flux-2-dev", str(opts))

    opts, _ = IG.parse_args(["--models"])
    check("parse: --models is a mode", opts.list_models and not opts.prompt, str(opts))

    opts, _ = IG.parse_args(["--help"])
    check("parse: --help is a mode", opts.help, str(opts))

    _, err = IG.parse_args(["--model"])
    check("parse: a flag with no value is refused", "needs a value" in err, err)

    _, err = IG.parse_args(["--wat", "x"])
    check("parse: an unknown option is refused", "unknown option" in err, err)

    opts, err = IG.parse_args(["don't", "stop"])
    check("parse: an apostrophe is not an unbalanced quote",
          err == "" and opts.prompt == "don't stop", repr(opts.prompt))


# ---------------------------------------------------------------------------
# 2 — where it goes and with which key
# ---------------------------------------------------------------------------

def test_credentials(tmp: Path) -> None:
    cfg = _cfg(tmp)
    creds = IG.credentials(cfg)
    check("creds: a Logfare session uses the session key",
          creds is not None and creds[0] == "https://logfare.ai/v1"
          and creds[1] == TOKEN, str(creds and creds[:2]))

    # The origin without /v1 composes to the same endpoint.
    cfg2 = _cfg(tmp, base_url="https://logfare.ai")
    creds2 = IG.credentials(cfg2)
    check("creds: an origin gets /v1 exactly once",
          creds2 is not None and creds2[0] == "https://logfare.ai/v1",
          str(creds2 and creds2[0]))

    # A session on another upstream falls back to the stored Logfare key.
    with mock.patch.object(LF, "load_credentials",
                           return_value=(TOKEN, "m", "/tmp/logfare/source")):
        cfg3 = _cfg(tmp, base_url="https://integrate.api.nvidia.com/v1",
                    auth_token=None, api_key="nvapi-x")
        creds3 = IG.credentials(cfg3)
    check("creds: another upstream falls back to Logfare",
          creds3 is not None and creds3[0] == LF.DIRECT_BASE_URL
          and creds3[1] == TOKEN, str(creds3))

    with mock.patch.object(LF, "load_credentials", return_value=None):
        cfg4 = _cfg(tmp, base_url="https://integrate.api.nvidia.com/v1",
                    auth_token=None, api_key="nvapi-x")
        check("creds: no Logfare key means no image route",
              IG.credentials(cfg4) is None and not IG.available(cfg4))
        check("creds: the tool is withheld without a key",
              "generate_image" not in [t["name"] for t in Agent(cfg4).tool_schemas()])


# ---------------------------------------------------------------------------
# 3 — generation
# ---------------------------------------------------------------------------

def test_generate_b64(tmp: Path) -> None:
    cfg = _cfg(tmp)
    opener, calls = _stub_generation()
    with mock.patch("urllib.request.urlopen", opener):
        image = IG.generate("A green cat", cfg=cfg)

    check("generate: the request goes to /v1/images/generations",
          calls and calls[0][0] == "https://logfare.ai/v1/images/generations",
          str(calls[:1]))
    body = json.loads(calls[0][2])
    check("generate: the prompt and size are in the body",
          body.get("prompt") == "A green cat"
          and body.get("size") == "1024x1024"
          and body.get("n") == 1, str(body))
    check("generate: the model is in the body",
          body.get("model") == IG.DEFAULT_MODEL, str(body.get("model")))
    check("generate: the image is written", image.path.is_file())
    check("generate: the bytes are the decoded image",
          image.path.read_bytes() == PNG)
    check("generate: a png keeps its extension", image.path.suffix == ".png",
          image.path.suffix)
    check("generate: the filename comes from the prompt",
          image.path.name.startswith("a-green-cat-"), image.path.name)
    check("generate: the default folder is images/",
          image.path.parent.name == "images", str(image.path))
    check("generate: the summary names model, size and file",
          "flux-1-schnell" in image.summary() and "1024x1024" in image.summary(),
          image.summary())
    check("generate: describe() shows a relative path",
          "images/" in IG.describe(image, tmp), IG.describe(image, tmp))

    # Request shape and credential, read back from the captured headers/body.
    req = calls[0][1]
    check("generate: the bearer token is sent",
          req.get("Authorization") == f"Bearer {TOKEN}", str(req.get("Authorization")))
    check("generate: the content type is JSON",
          req.get("Content-type") == "application/json", str(req.get("Content-type")))


def test_generate_url(tmp: Path) -> None:
    cfg = _cfg(tmp)
    opener, calls = _stub_generation(
        {"model": "flux-2-dev",
         "data": [{"url": "https://logfare.ai/files/x.jpg"}]},
        url_body=JPEG,
    )
    with mock.patch("urllib.request.urlopen", opener):
        image = IG.generate("a photo", cfg=cfg, model="flux-2-dev")

    check("url: the download is a second request", len(calls) == 2, str(len(calls)))
    check("url: the returned bytes are saved", image.path.read_bytes() == JPEG)
    check("url: a jpeg gets a .jpg extension", image.path.suffix == ".jpg",
          image.path.suffix)
    check("url: the key is sent to the gateway host",
          calls[1][1].get("Authorization") == f"Bearer {TOKEN}",
          str(calls[1][1].get("Authorization")))

    # A CDN on another host must not receive the key.
    opener, calls = _stub_generation(
        {"data": [{"url": "https://cdn.example.test/x.png"}]})
    with mock.patch("urllib.request.urlopen", opener):
        IG.generate("a photo", cfg=cfg)
    check("url: the key is not sent to another host",
          "Authorization" not in calls[1][1], str(calls[1][1]))

    # A gateway that answers with an internal address must not turn into an
    # SSRF primitive.
    opener, calls = _stub_generation(
        {"data": [{"url": "http://127.0.0.1:8080/secret"}]})
    with mock.patch("urllib.request.urlopen", opener):
        try:
            IG.generate("x", cfg=cfg)
            local = ""
        except IG.ImageGenError as e:
            local = str(e)
    check("url: a loopback URL is refused", "local infrastructure" in local, local)
    check("url: the loopback URL is never fetched", len(calls) == 1, str(len(calls)))


def test_dest(tmp: Path) -> None:
    cfg = _cfg(tmp)
    opener, _ = _stub_generation()
    with mock.patch("urllib.request.urlopen", opener):
        image = IG.generate("x", cfg=cfg, dest="art/picture")
    check("dest: the path is honoured and gets the detected extension",
          image.path == (tmp / "art" / "picture.png").resolve(), str(image.path))
    check("dest: the parent directory is created", image.path.is_file())

    opener, _ = _stub_generation()
    with mock.patch("urllib.request.urlopen", opener):
        image2 = IG.generate("x", cfg=cfg)
        image3 = IG.generate("x", cfg=cfg)
    check("dest: a second image with the same prompt does not overwrite",
          image2.path != image3.path, f"{image2.path} == {image3.path}")

    # The path is checked before the paid request: a refused dest must not
    # reach the network at all.
    opener, calls = _stub_generation()
    with mock.patch("urllib.request.urlopen", opener):
        try:
            IG.generate("x", cfg=cfg, dest="/etc/definitely-not-ours.png")
            refused = ""
        except IG.ImageGenError as e:
            refused = str(e)
    check("dest: a path outside the jail is refused", "escapes the working directory" in refused,
          refused)
    check("dest: a refused path spends no generation", calls == [], str(calls))


def test_errors(tmp: Path) -> None:
    cfg = _cfg(tmp)
    opener, _ = _stub_generation()
    with mock.patch("urllib.request.urlopen", opener):
        try:
            IG.generate("   ", cfg=cfg)
            empty = ""
        except IG.ImageGenError as e:
            empty = str(e)
    check("errors: an empty prompt is refused", "prompt is required" in empty, empty)

    with mock.patch("urllib.request.urlopen", opener):
        try:
            IG.generate("x" * (IG.MAX_PROMPT + 1), cfg=cfg)
            long = ""
        except IG.ImageGenError as e:
            long = str(e)
    check("errors: an oversized prompt is refused", "limit is" in long, long)

    with mock.patch("urllib.request.urlopen", opener):
        try:
            IG.generate("x", cfg=cfg, size="huge")
            bad_size = ""
        except IG.ImageGenError as e:
            bad_size = str(e)
    check("errors: a malformed size is refused", "1024x1024" in bad_size, bad_size)

    with mock.patch("urllib.request.urlopen", opener):
        try:
            IG.generate("x", cfg=cfg, size="4096x4096")
            too_big = ""
        except IG.ImageGenError as e:
            too_big = str(e)
    check("errors: an out-of-range size is refused", "2048" in too_big, too_big)

    body = json.dumps({"error": {"message": "bad key"}}).encode()
    http = urllib.error.HTTPError("u", 401, "Unauthorized", {},
                                  io.BytesIO(body))
    opener, _ = _stub_generation(error=http)
    with mock.patch("urllib.request.urlopen", opener):
        try:
            IG.generate("x", cfg=cfg)
            auth = ""
        except IG.ImageGenError as e:
            auth = str(e)
    check("errors: a 401 says the key was rejected",
          "401" in auth and "logfare-api-setup" in auth, auth)
    check("errors: the upstream message is carried", "bad key" in auth, auth)
    check("errors: the key is never echoed", TOKEN not in auth, auth)

    with mock.patch.object(LF, "load_credentials", return_value=None):
        cfg2 = _cfg(tmp, base_url="https://integrate.api.nvidia.com/v1",
                    auth_token=None)
        try:
            IG.generate("x", cfg=cfg2)
            no_key = ""
        except IG.ImageGenError as e:
            no_key = str(e)
    check("errors: no key is explained, not raised raw",
          "no image route" in no_key, no_key)


# ---------------------------------------------------------------------------
# 4 — the image catalog
# ---------------------------------------------------------------------------

CATALOG = {
    "data": [
        {"model_id": "flux-1-schnell", "status": "operational",
         "uptime_percent": 99.0, "endpoints": ["images/generations"]},
        {"model_id": "flux-2-dev", "status": "operational",
         "uptime_percent": 98.0,
         "endpoints": ["images/generations", "images/edits"]},
        {"model_id": "deepseek-v4.1-flash", "status": "operational",
         "uptime_percent": 93.0, "endpoints": ["chat/completions"]},
        {"model_id": "whisper-large-v3-turbo", "status": "operational",
         "endpoints": ["audio/transcriptions"]},
    ]
}


def test_catalog() -> None:
    LF.clear_cache()

    def _open(req, timeout=None):
        return _json(CATALOG)

    with mock.patch("urllib.request.urlopen", _open):
        models, err = LF.fetch_image_models(use_cache=False)
    check("catalog: only images/generations entries are offered",
          err == "" and [m.id for m in models or []] == ["flux-1-schnell", "flux-2-dev"],
          str([m.id for m in models or []]))
    check("catalog: the chat catalog is unaffected",
          [m.id for m in LF._parse_models(CATALOG)]
          == ["deepseek-v4.1-flash"], str([m.id for m in LF._parse_models(CATALOG)]))

    # The two caches are separate: an image fetch must not answer /model.
    LF.clear_cache()
    with mock.patch("urllib.request.urlopen", _open):
        LF.fetch_image_models()
    check("catalog: an image fetch does not fill the chat cache",
          LF.cached_ids() is None, str(LF.cached_ids()))
    check("catalog: the image fetch fills its own cache", bool(LF._IMAGE_CACHE))
    LF.clear_cache()
    check("catalog: clear_cache drops the image cache too",
          LF._IMAGE_CACHE == [], str(LF._IMAGE_CACHE))


# ---------------------------------------------------------------------------
# 5 — the tool
# ---------------------------------------------------------------------------

def test_tool(tmp: Path) -> None:
    cfg = _cfg(tmp)
    opener, _ = _stub_generation()
    with mock.patch("urllib.request.urlopen", opener):
        result = tools.execute("generate_image", {"prompt": "a green cat"}, cfg)
    check("tool: the call succeeds", not result.is_error, result.output)
    check("tool: the result names the saved path", "image saved:" in result.output,
          result.output)
    check("tool: the result carries the path in meta",
          Path(result.meta.get("path", "")).is_file(), str(result.meta))
    check("tool: the schema is advertised",
          "generate_image" in tools.TOOL_NAMES)

    with mock.patch("urllib.request.urlopen", opener):
        bad = tools.execute("generate_image", {"prompt": "x", "size": "nope"}, cfg)
    check("tool: a bad argument is a tool error, not an exception",
          bad.is_error and "1024x1024" in bad.output, bad.output)

    empty = tools.execute("generate_image", {}, cfg)
    check("tool: an empty prompt is a tool error",
          empty.is_error and "prompt is required" in empty.output, empty.output)


# ---------------------------------------------------------------------------
# 6 — mode policy
# ---------------------------------------------------------------------------

def test_modes(tmp: Path) -> None:
    cfg = _cfg(tmp, mode="plan")
    agent = Agent(cfg)
    names = [t["name"] for t in agent.tool_schemas()]
    check("modes: plan mode withholds generate_image",
          "generate_image" not in names, str(names))
    denied = agent._denied("generate_image") or ""
    check("modes: plan mode refuses it by name", "plan" in denied.lower(), denied)

    cfg2 = _cfg(tmp, mode="auto")
    agent2 = Agent(cfg2)
    check("modes: auto mode advertises it",
          "generate_image" in [t["name"] for t in agent2.tool_schemas()])


# ---------------------------------------------------------------------------
# 7 — wiring: every surface a grep finds
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    check("wiring: dispatch has /image-gen",
          '"image-gen": lambda a: self._cmd_image_gen(a)' in app_src)
    check("wiring: the help table names /image-gen",
          '("/image-gen <prompt>"' in app_src)
    check("wiring: the palette names /image-gen",
          '("/image-gen", "generate an image from a prompt", "image-gen")' in app_src)
    names = [n for n, _ in SLASH_COMMANDS]
    check("wiring: the completion popup lists /image-gen", "/image-gen" in names)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("wiring: the README documents /image-gen", "/image-gen <prompt>" in readme)


# ---------------------------------------------------------------------------
# 8 — driven through the live TUI
# ---------------------------------------------------------------------------

async def test_ui(tmp: Path) -> None:
    from zimzilla.ui.app import ZimZillaApp

    cfg = _cfg(tmp, base_url="http://localhost:4001")
    cfg.state_dir = tmp / "ui-state"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        app._handle_command("/image-gen")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("ui: no prompt prints usage", "usage: /image-gen" in blob, blob[-400:])

        app._handle_command("/image-gen --help")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("ui: --help prints usage", "usage: /image-gen" in blob, blob[-400:])


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_parse()
        test_credentials(tmp)
        test_generate_b64(tmp)
        test_generate_url(tmp)
        test_dest(tmp)
        test_errors(tmp)
        test_catalog()
        test_tool(tmp)
        test_modes(tmp)
        test_wiring()
        asyncio.run(test_ui(tmp))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
