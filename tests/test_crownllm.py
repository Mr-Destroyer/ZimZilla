"""Regression suite for CrowLLM — free-only hosted source.

Run:  python tests/test_crownllm.py

Offline: the catalogue is a fixture. The important property is that only ids
ending in :free are ever offered — the catalogue contains two paid ids.
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

_HOME = Path(tempfile.mkdtemp(prefix="zim-crown-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)

from zimzilla import crownllm as cl  # noqa: E402
from zimzilla import sources  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


CATALOG = {"data": [
    {"id": "gpt-4o:free", "owned_by": "OpenAI", "created": 1700000000},
    {"id": "qwen-3.8-27b:free", "owned_by": "Alibaba", "created": 1700000001},
    {"id": "gemini-3-flash:free", "owned_by": "Google", "created": 1700000002},
    {"id": "runpod/kimi-k3", "owned_by": "Moonshot", "created": 1700000003},
    {"id": "typhoon-v2.5-30b-a3b-instruct", "owned_by": "OpenAI", "created": 1700000004},
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


def _open(payload: dict, url: str | None = None):
    return mock.patch("urllib.request.urlopen",
                      return_value=_Resp(json.dumps(payload).encode()))


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(cl.CATALOG_URL, code, "error", {},
                                  io.BytesIO(json.dumps(body).encode()))


def _cfg() -> Config:
    cfg = Config.from_env(workdir=_HOME, base_url="https://logfare.ai/v1",
                          api_key="placeholder")
    cfg.state_dir = _HOME / "state"
    return cfg


def test_catalogue() -> None:
    cl.clear_cache()
    with _open(CATALOG):
        models, err = cl.fetch_models()
    check("catalog: it fetches without a key", err == "" and models is not None, err)
    got = [m.id for m in models or []]
    check("catalog: free ids are kept",
          got == ["gpt-4o:free", "qwen-3.8-27b:free", "gemini-3-flash:free"], str(got))
    check("catalog: the paid runpod id is excluded", "runpod/kimi-k3" not in got)
    check("catalog: the paid typhoon id is excluded",
          "typhoon-v2.5-30b-a3b-instruct" not in got)
    free, ferr = cl.free_models()
    check("free: the free list is the free suffix",
          all(m.id.endswith(":free") for m in free or []), str(free))
    check("cache: ids are the free list", cl.cached_ids() == got)
    check("cache: the age is reported", cl.catalog_age() is not None)
    check("default: it is a working free id", cl.default_free_model() in got,
          cl.default_free_model())


def test_credentials() -> None:
    check("key: the provided shape is accepted",
          cl.looks_like_key("WeKjmVGZNNuAwFATxdu6ECRwzc54tXaiT7DTsnsnKgERZp7d"))
    check("key: a short string is refused", not cl.looks_like_key("short"))
    path = cl.save_key("testkey1234567890abcdef", "gpt-4o:free")
    check("profile: it writes where it says", path == cl.PROFILE)
    check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
          oct(path.stat().st_mode & 0o777))
    text = path.read_text()
    check("profile: source format", "export CROWNLLM_API_KEY=" in text)
    check("profile: it names the Chat Completions base",
          'ANTHROPIC_BASE_URL="https://crowllm.com/v1"' in text)
    cfg = _cfg()
    with _open(CATALOG):
        cl.fetch_models()
    applied = cl.apply_to(cfg)
    check("apply_to: it returns origin and model", applied is not None, str(applied))
    check("apply_to: base is the v1 root", cfg.base_url == cl.DEFAULT_BASE_URL)
    check("apply_to: the key is set", bool(cfg.auth_token))
    check("apply_to: stale api_key is cleared", cfg.api_key == "")
    check("apply_to: the model is free", cfg.model.endswith(":free"), cfg.model)


def test_errors() -> None:
    check("error: a 401 points at the key command",
          "crownllm-api-key" in cl.explain_error(401, ""))
    check("error: a 503 is a provider health problem",
          "provider" in cl.explain_error(503, "").lower())
    check("error: a 429 is transient", "rate limited" in cl.explain_error(429, ""))
    rejected = _http_error(401, {"error": {"message": "invalid key"}})
    with mock.patch("urllib.request.urlopen", side_effect=rejected):
        ok, detail = cl.verify_key("testkey1234567890abcdef")
    check("probe: a rejected key is refused", ok is False, detail)
    with _open({"choices": []}):
        ok, detail = cl.verify_key("testkey1234567890abcdef")
    check("probe: a 200 proves the key", ok is True, detail)


def test_registry() -> None:
    cl.save_key("testkey1234567890abcdef")
    with _open(CATALOG):
        cl.fetch_models()
    check("registry: it is a builtin key", "crownllm" in sources.BUILTIN_KEYS)
    check("registry: active_key recognises the host",
          sources.active_key(cl.DEFAULT_BASE_URL) == "crownllm")
    check("registry: _gateway_module resolves it",
          sources._gateway_module("crownllm") is cl)
    check("registry: /model gets only free ids",
          set(sources.models_for(cl.DEFAULT_BASE_URL) or ()) <=
          set(cl.cached_ids() or ()))
    check("registry: a free id prices at zero",
          sources.catalog_price_for(cl.DEFAULT_BASE_URL, "gpt-4o:free") == (0.0, 0.0))
    check("registry: the paid id is not offered",
          "runpod/kimi-k3" not in (sources.models_for(cl.DEFAULT_BASE_URL) or ()))


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    agent = (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8")
    for cmd in ("zim-crownllm", "crownllm-api-key", "crownllm-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp)
    check("tui: the gateway table knows it", 'if kind == "crownllm":' in app)
    check("tui: the status pane lists it", '("crownllm", cl_mod' in app)
    check("tui: /models accepts it", '"crownllm"' in
          app.split('known = {"logfare"', 1)[-1].split("}", 1)[0])
    check("tui: /model refetches for it", 'elif active == "crownllm":' in app)
    check("launch: the restore list knows it", '("crownllm", cl_mod)' in main)
    check("prompt: the model is told where the key lives",
          '("crownllm", "CrowLLM"' in agent)
    check("credential: no CrowLLM key ships",
          not any("CROWNLLM_API_KEY" in p.read_text(errors="replace") or
                  "WeKjmVGZ" in p.read_text(errors="replace")
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
