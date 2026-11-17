"""Regression suite for Kenari — free-only hosted source.

Run:  python tests/test_kenari.py

Offline. The catalogue is a fixture; the property under test is that only
pricing.free models advertising openai-chat are offered.
"""

from __future__ import annotations

import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-kenari-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)

from zimzilla import kenari as kn  # noqa: E402
from zimzilla import sources  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _model(mid: str, *, free: bool, chat: bool = True, context: int = 262144,
           image: bool = False) -> dict:
    return {
        "id": mid, "name": mid, "context_length": context,
        "max_output_tokens": 32768,
        "pricing": {"free": free, "currency": "IDR"},
        "modalities": {"input": ["text"] + (["image"] if image else []),
                       "output": ["text"]},
        "protocols": ["openai-chat", "anthropic-messages"] if chat else
                     ["openai-responses"],
    }


CATALOG = {"data": [
    _model("qwen3-8-27b:free", free=True),
    _model("glm-4-7-flash:free", free=True, image=True),
    _model("beam-1-0:free", free=True, context=262144),
    _model("paid/claude", free=False),
    _model("responses-only:free", free=True, chat=False),
]}


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _open(payload: dict | None = None):
    body = payload if payload is not None else CATALOG
    return mock.patch("urllib.request.urlopen",
                      return_value=_Resp(json.dumps(body).encode()))


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(kn.CATALOG_URL, code, "error", {},
                                  io.BytesIO(json.dumps(body).encode()))


def _cfg() -> Config:
    cfg = Config.from_env(workdir=_HOME, base_url="https://logfare.ai/v1",
                          api_key="placeholder")
    cfg.state_dir = _HOME / "state"
    return cfg


def test_catalogue() -> None:
    kn.clear_cache()
    with _open():
        models, err = kn.fetch_models()
    check("catalog: it fetches without a key", err == "" and models is not None, err)
    free, ferr = kn.free_models()
    got = [m.id for m in free or []]
    check("free: only pricing.free + openai-chat",
          got == ["qwen3-8-27b:free", "glm-4-7-flash:free", "beam-1-0:free"],
          str(got))
    check("free: the paid id is excluded", "paid/claude" not in got)
    check("free: a non-chat id is excluded", "responses-only:free" not in got)
    check("free: vision is recorded", any(m.vision for m in free or []))
    check("free: context is carried",
          all(m.context_length for m in free or []))
    check("cache: the free ids are cached", kn.cached_ids() == got)
    check("cache: the age is reported", kn.catalog_age() is not None)
    check("default: it lands on a free id", kn.default_free_model() in got,
          kn.default_free_model())


def test_credentials() -> None:
    check("key: the provided shape is accepted",
          kn.looks_like_key("kn-b8fec8fb5089fbeb10d5e95904a62e601d389fb4bbd27599"))
    check("key: a short string is refused", not kn.looks_like_key("short"))
    path = kn.save_key("kn-testkey1234567890abcdef", "qwen3-8-27b:free")
    check("profile: written where it says", path == kn.PROFILE)
    check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
          oct(path.stat().st_mode & 0o777))
    text = path.read_text()
    check("profile: source format", "export KENARI_API_KEY=" in text)
    check("profile: it names the Chat Completions base",
          'ANTHROPIC_BASE_URL="https://kenari.id/v1"' in text)
    cfg = _cfg()
    with _open():
        kn.fetch_models()
    applied = kn.apply_to(cfg)
    check("apply_to: it returns origin and model", applied is not None, str(applied))
    check("apply_to: base is the v1 root", cfg.base_url == kn.DEFAULT_BASE_URL)
    check("apply_to: the key is set", bool(cfg.auth_token))
    check("apply_to: stale api_key is cleared", cfg.api_key == "")
    check("apply_to: the model is free", cfg.model.endswith(":free"), cfg.model)


def test_errors() -> None:
    check("error: a 401 points at the setup command",
          "kenari-api-setup" in kn.explain_error(401, ""))
    check("error: a 429 names the free quota",
          "rate" in kn.explain_error(429, "").lower())
    check("error: a 402 points at credit",
          "credit" in kn.explain_error(402, "").lower())
    rejected = _http_error(401, {"error": {"message": "invalid key"}})
    with mock.patch("urllib.request.urlopen", side_effect=rejected):
        ok, detail = kn.verify_key("kn-testkey1234567890abcdef")
    check("probe: a rejected key is refused", ok is False, detail)
    with _open({"choices": []}):
        ok, detail = kn.verify_key("kn-testkey1234567890abcdef")
    check("probe: a 200 proves the key", ok is True, detail)


def test_registry() -> None:
    kn.save_key("kn-testkey1234567890abcdef")
    with _open():
        kn.fetch_models()
    check("registry: it is a builtin key", "kenari" in sources.BUILTIN_KEYS)
    check("registry: active_key recognises the host",
          sources.active_key(kn.DEFAULT_BASE_URL) == "kenari")
    check("registry: _gateway_module resolves it",
          sources._gateway_module("kenari") is kn)
    check("registry: /model gets only free ids",
          set(sources.models_for(kn.DEFAULT_BASE_URL) or ()) <=
          set(kn.cached_ids() or ()))
    check("registry: a free id prices at zero",
          sources.catalog_price_for(kn.DEFAULT_BASE_URL,
                                    "qwen3-8-27b:free") == (0.0, 0.0))
    check("registry: the paid id is not offered",
          "paid/claude" not in (sources.models_for(kn.DEFAULT_BASE_URL) or ()))
    check("registry: context comes from the model",
          sources.context_window_for(kn.DEFAULT_BASE_URL,
                                     "qwen3-8-27b:free") == 262144)


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    agent = (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8")
    for cmd in ("zim-kenari", "kenari-api-setup", "kenari-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp)
    check("tui: the gateway table knows it", 'if kind == "kenari":' in app)
    check("tui: the status pane lists it", '("kenari", kn_mod' in app)
    check("tui: /models accepts it", '"kenari"' in
          app.split('known = {"logfare"', 1)[-1].split("}", 1)[0])
    check("tui: /model refetches for it", 'elif active == "kenari":' in app)
    check("launch: the restore list knows it", '("kenari", kn_mod)' in main)
    check("prompt: the model is told where the key lives",
          '("kenari", "Kenari"' in agent)
    check("credential: no Kenari key ships",
          not any("KENARI_API_KEY" in p.read_text(errors="replace") or
                  "kn-b8fec8" in p.read_text(errors="replace")
                  for p in (ROOT / "packaging").rglob("*") if p.is_file()))


def main() -> int:
    test_catalogue()
    test_credentials()
    test_errors()
    test_registry()
    test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
