"""Regression suite for Numes — free-only hosted source.

Run:  python tests/test_numes.py

Everything is offline: the key-gated catalogue and probe are served by a stub.
What this pins: only prompt=0 AND completion=0 chat models are offered, the
profile is mode 600, `apply_to` lands on a free id, and the TUI surfaces are
wired.
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

_HOME = Path(tempfile.mkdtemp(prefix="zim-numes-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)

from zimzilla import numes as nm  # noqa: E402
from zimzilla import sources  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


CATALOG = {
    "data": [
        {"id": "deepseek/deepseek-v4.1-flash", "name": "DeepSeek",
         "context_length": 1048576, "maximum_output_length": 65536,
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["text"]}},
        {"id": "vision/free-model", "name": "Vision Free",
         "context_length": 200000, "maximum_output_length": 32768,
         "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "paid/model", "name": "Paid",
         "context_length": 128000,
         "pricing": {"prompt": "0.00001", "completion": "0.00002"},
         "architecture": {"input_modalities": ["text"]}},
        {"id": "unpriced/model", "name": "Unpriced",
         "context_length": 128000,
         "architecture": {"input_modalities": ["text"]}},
    ]
}


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _open(payload: dict):
    return mock.patch("urllib.request.urlopen",
                      return_value=_Resp(json.dumps(payload).encode()))


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        nm.CATALOG_URL, code, "error", {},
        io.BytesIO(json.dumps(body).encode()))


def _cfg() -> Config:
    cfg = Config.from_env(workdir=_HOME, base_url="https://logfare.ai/v1",
                          api_key="placeholder")
    cfg.state_dir = _HOME / "state"
    return cfg


def test_catalogue() -> None:
    nm.clear_cache()
    with _open(CATALOG):
        models, err = nm.fetch_models("sk-numes-v1-testkey1234567890")
    check("catalog: it fetches with a key", err == "" and models is not None, err)
    free, ferr = nm.free_models("sk-numes-v1-testkey1234567890")
    got = [m.id for m in free or []]
    check("free: only prompt=0 AND completion=0 are offered",
          got == ["deepseek/deepseek-v4.1-flash", "vision/free-model"], str(got))
    check("free: the paid model is excluded", "paid/model" not in got)
    check("free: the unpriced model is excluded", "unpriced/model" not in got)
    check("free: vision modality is recorded",
          any(m.vision for m in free or []))
    check("free: prices are per million tokens",
          all(m.price_in == 0 and m.price_out == 0 for m in free or []))
    check("cache: ids are the free list", nm.cached_ids() == got, str(nm.cached_ids()))
    check("cache: the age is reported", nm.catalog_age() is not None)
    check("default: it is a free id", nm.default_free_model() in got,
          nm.default_free_model())


def test_credentials() -> None:
    check("key: the shape is accepted",
          nm.looks_like_key("sk-numes-v1-2e94ef51150954415ce9fbe47e7813f1f5a385f7b60fed9f1b420cfb0b1590ec"))
    check("key: a short string is refused", not nm.looks_like_key("short"))
    path = nm.save_key("sk-numes-v1-testkey1234567890", "deepseek/deepseek-v4.1-flash")
    check("profile: it writes where it says", path == nm.PROFILE)
    check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
          oct(path.stat().st_mode & 0o777))
    text = path.read_text()
    check("profile: source format", "export NUMES_API_KEY=" in text)
    check("profile: it names the Chat Completions base",
          'ANTHROPIC_BASE_URL="https://api.numes.ai/api/v1"' in text)
    cfg = _cfg()
    applied = nm.apply_to(cfg)
    check("apply_to: it returns origin and model", applied is not None, str(applied))
    check("apply_to: base is the API root", cfg.base_url == nm.DEFAULT_BASE_URL)
    check("apply_to: the key is set", bool(cfg.auth_token))
    check("apply_to: stale api_key is cleared", cfg.api_key == "")
    check("apply_to: the model is free", cfg.model in nm.FREE_FALLBACK, cfg.model)


def test_errors() -> None:
    rejected = _http_error(401, {"error": {"message": "invalid key"}})
    with mock.patch("urllib.request.urlopen", side_effect=rejected):
        ok, detail = nm.verify_key("sk-numes-v1-badkey1234567890")
    check("probe: a rejected key is refused", ok is False, detail)
    with _open(CATALOG):
        ok, detail = nm.verify_key("sk-numes-v1-testkey1234567890")
    check("probe: a 200 proves the key", ok is True, detail)
    check("error: a 402 points at credit/balance",
          "credit" in nm.explain_error(402, "").lower() or
          "balance" in nm.explain_error(402, "").lower())
    check("error: a 429 is transient", "rate limited" in nm.explain_error(429, ""))


def test_registry() -> None:
    nm.save_key("sk-numes-v1-testkey1234567890")
    with _open(CATALOG):
        nm.fetch_models()
    check("registry: it is a builtin key", "numes" in sources.BUILTIN_KEYS)
    check("registry: active_key recognises the host",
          sources.active_key(nm.DEFAULT_BASE_URL) == "numes")
    check("registry: _gateway_module resolves it",
          sources._gateway_module("numes") is nm)
    check("registry: /model gets only free ids",
          set(sources.models_for(nm.DEFAULT_BASE_URL) or ()) <=
          set(nm.cached_ids() or ()))
    check("registry: the window comes from the catalogue",
          sources.context_window_for(nm.DEFAULT_BASE_URL,
                                     "deepseek/deepseek-v4.1-flash") == 1048576)
    check("registry: a free model prices at zero",
          sources.catalog_price_for(nm.DEFAULT_BASE_URL,
                                    "deepseek/deepseek-v4.1-flash") == (0.0, 0.0))
    check("registry: the paid id is not offered",
          "paid/model" not in (sources.models_for(nm.DEFAULT_BASE_URL) or ()))


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    agent = (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8")
    for cmd in ("zim-numes", "numes-api-setup", "numes-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp)
    check("tui: the gateway table knows it", 'if kind == "numes":' in app)
    check("tui: the status pane lists it", '("numes", nm_mod' in app)
    check("tui: /models accepts it", '"numes"' in
          app.split('known = {"logfare"', 1)[-1].split("}", 1)[0])
    check("tui: /model refetches for it", 'elif active == "numes":' in app)
    check("launch: the restore list knows it", '("numes", nm_mod)' in main)
    check("prompt: the model is told where the key lives",
          '("numes", "Numes"' in agent)
    check("credential: no Numes key ships",
          not any("sk-numes-" in p.read_text(errors="replace")
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
