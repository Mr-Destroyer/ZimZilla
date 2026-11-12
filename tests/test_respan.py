"""Regression suite for Respan — the hosted gateway, free models only.

Run:  python tests/test_respan.py

What this pins:

1. **The free filter.** The public catalogue lists paid models, provider-prefixed
   aliases of free ones, speech models and zero-context placeholders. Only the
   free *chat* models may reach `/model`; a paid id must be refused there.
2. **The profile.** A key is stored mode 600 in the same source format as every
   other provider, and ``apply_to`` always lands on a free model.
3. **The activation distinction.** A fresh Respan account is valid but cannot
   call even $0 models until credits/BYOK are added. That 401 is not a bad key,
   and ``verify_key`` must say which it is.
4. **The wiring.** The source is registered, restored at launch, advertised,
   documented and dispatched.

Everything is offline: the catalogue and the probe are served by a stub.
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

_HOME = Path(tempfile.mkdtemp(prefix="zim-respan-home-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)

from zimzilla import respan as rp  # noqa: E402
from zimzilla import sources  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


CATALOG = {
    "object": "list",
    "data": [
        {"id": "glm-4.7-flash", "owned_by": "zai", "context_length": 200000,
         "pricing": {"input_cost": 0.0, "output_cost": 0.0}},
        {"id": "zai/glm-4.7-flash", "owned_by": "zai", "context_length": 200000,
         "pricing": {"input_cost": 0.0, "output_cost": 0.0}},
        {"id": "gemma-4-31b-it", "owned_by": "google_gemini_ai",
         "context_length": 262144,
         "pricing": {"input_cost": 0.0, "output_cost": 0.0}},
        {"id": "phonic-1", "owned_by": "phonic", "context_length": None,
         "pricing": {"input_cost": 0.0, "output_cost": 0.0}},
        {"id": "span-01-lite", "owned_by": "respan", "context_length": 0,
         "pricing": {"input_cost": 0.0, "output_cost": 0.0}},
        {"id": "paid-model", "owned_by": "openai", "context_length": 128000,
         "pricing": {"input_cost": 1.0, "output_cost": 2.0}},
        {"id": "unpriced", "owned_by": "somebody", "context_length": 128000},
    ],
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
        "https://gateway.respan.ai/v1/messages", code, "error", {},
        io.BytesIO(json.dumps(body).encode()))


def _cfg() -> Config:
    cfg = Config.from_env(workdir=_HOME, base_url="https://logfare.ai/v1",
                          api_key="placeholder")
    cfg.state_dir = _HOME / "state"
    return cfg


# ---------------------------------------------------------------------------
# 1 — the catalogue and the free filter
# ---------------------------------------------------------------------------

def test_catalogue() -> None:
    rp.clear_cache()
    parsed = rp._parse_models(CATALOG)
    ids = [m.id for m in parsed]
    check("catalog: prefixed aliases are folded onto the bare id",
          "glm-4.7-flash" in ids and "zai/glm-4.7-flash" not in ids, str(ids))
    check("catalog: a free model is free", rp.model_for("glm-4.7-flash", parsed).free)
    check("catalog: a paid model is not free",
          not rp.model_for("paid-model", parsed).free)
    check("catalog: a missing price is not free",
          not rp.model_for("unpriced", parsed).free)
    check("catalog: speech is not a chat model",
          not rp.model_for("phonic-1", parsed).chat)
    check("catalog: a zero context is not a chat model",
          not rp.model_for("span-01-lite", parsed).chat)

    with _open(CATALOG):
        free, err = rp.free_models()
    check("free: the catalogue fetch succeeds", err == "" and free is not None, err)
    got = [m.id for m in free or []]
    check("free: only zero-priced chat models are offered",
          got == ["glm-4.7-flash", "gemma-4-31b-it"], str(got))
    check("free: the paid model is excluded", "paid-model" not in got)
    check("free: the aliased duplicate is not listed twice",
          len(got) == len(set(got)))
    check("cache: ids are the free list", rp.cached_ids() == got, str(rp.cached_ids()))
    check("cache: the age is reported", rp.catalog_age() is not None)
    check("default: it is a free model", rp.default_free_model() in got,
          rp.default_free_model())


# ---------------------------------------------------------------------------
# 2 — the profile and apply_to
# ---------------------------------------------------------------------------

def test_credentials() -> None:
    check("key: the provided shape is accepted",
          rp.looks_like_key("MBQD1ZL0.sqZjpyk8MlDezjOlKFcRj4dPY1zQTauq"))
    check("key: a short string is refused", not rp.looks_like_key("short"))
    check("key: whitespace is refused", not rp.looks_like_key("a" * 20 + " b"))

    path = rp.save_key("rp_testkey_1234567890abcdef", "glm-4.7-flash")
    check("profile: it writes where it says", path == rp.PROFILE, str(path))
    check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
          oct(path.stat().st_mode & 0o777))
    text = path.read_text()
    check("profile: source format", "export RESPAN_API_KEY=" in text)
    check("profile: it names the Messages base",
          'ANTHROPIC_BASE_URL="https://gateway.respan.ai"' in text)
    check("profile: it blanks api_key", 'ANTHROPIC_API_KEY=""' in text)
    check("profile: the key reads back",
          rp.load_credentials()[0] == "rp_testkey_1234567890abcdef")

    cfg = _cfg()
    applied = rp.apply_to(cfg)
    check("apply_to: it returns the origin and model", applied is not None, str(applied))
    check("apply_to: base is the gateway", cfg.base_url == rp.DEFAULT_BASE_URL)
    check("apply_to: the token is set", bool(cfg.auth_token))
    check("apply_to: a stale api_key is cleared", cfg.api_key == "", cfg.api_key)
    check("apply_to: the model is free", cfg.model in rp.FREE_FALLBACK, cfg.model)


# ---------------------------------------------------------------------------
# 3 — errors and the key probe
# ---------------------------------------------------------------------------

def test_probe() -> None:
    activation = _http_error(401, {"error": {
        "message": "Model 'glm-4.7-flash' needs Respan credits.",
        "type": "authentication_error", "code": "activation_required"}})
    with mock.patch("urllib.request.urlopen", side_effect=activation):
        ok, detail = rp.verify_key("rp_testkey_1234567890abcdef")
    check("probe: activation_required still means the key is valid", ok is True, detail)
    check("probe: it names the account state",
          "not activated" in detail or "credits" in detail, detail)

    rejected = _http_error(401, {"error": {
        "message": "invalid api key", "code": "authentication_error"}})
    with mock.patch("urllib.request.urlopen", side_effect=rejected):
        ok, detail = rp.verify_key("rp_bad_key_1234567890abcdef")
    check("probe: a rejected key is not valid", ok is False, detail)

    with _open({"ok": True}):
        ok, detail = rp.verify_key("rp_testkey_1234567890abcdef")
    check("probe: a 200 proves the key", ok is True, detail)

    explained = rp.explain_error(401, json.dumps({"error": {
        "message": "needs Respan credits", "code": "activation_required"}}))
    check("error: activation is explained", "not activated" in explained, explained)
    check("error: it points at billing", "billing" in explained, explained)
    check("error: a 429 is transient", "rate limited" in rp.explain_error(429, ""))


# ---------------------------------------------------------------------------
# 4 — registry and TUI wiring
# ---------------------------------------------------------------------------

def test_registry() -> None:
    rp.save_key("rp_testkey_1234567890abcdef")
    rp.fetch_models()
    check("registry: it is a builtin key", "respan" in sources.BUILTIN_KEYS)
    check("registry: active_key recognises the host",
          sources.active_key(rp.DEFAULT_BASE_URL) == "respan")
    check("registry: _gateway_module resolves it",
          sources._gateway_module("respan") is rp)
    check("registry: active_key does not confuse it with Logfare",
          sources.active_key("https://logfare.ai/v1") != "respan")
    check("registry: /model gets only free ids",
          set(sources.models_for(rp.DEFAULT_BASE_URL) or ()) <=
          {"glm-4.7-flash", "gemma-4-31b-it"})
    check("registry: the window comes from the catalogue",
          sources.context_window_for(rp.DEFAULT_BASE_URL, "glm-4.7-flash") == 200000)
    check("registry: the catalogue prices it free",
          sources.catalog_price_for(rp.DEFAULT_BASE_URL, "glm-4.7-flash") == (0.0, 0.0))
    check("registry: a paid id is not in the offered list",
          "paid-model" not in (sources.models_for(rp.DEFAULT_BASE_URL) or ()))


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    agent = (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8")
    for cmd in ("zim-respan", "respan-api-setup", "respan-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app)
        check(f"tui: /{cmd} is busy-ok", f'"{cmd}"' in app)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp)
    check("tui: the gateway table knows it", 'if kind == "respan":' in app)
    check("tui: the status pane lists it", '("respan", rp_mod' in app)
    check("tui: /models accepts it", '"respan"' in
          app.split('known = {"logfare"', 1)[-1].split("}", 1)[0])
    check("tui: /model refetches for it", 'elif active == "respan":' in app)
    check("tui: the switch shows the free list",
          'if kind == "respan":' in app and "_fetch_models(kind)" in app)
    check("launch: the restore list knows it", '("respan", rp_mod)' in main)
    check("prompt: the model is told where the key lives",
          '("respan", "Respan"' in agent)
    check("credential: nothing under packaging/ holds a Respan key",
          not any("RESPAN_API_KEY" in p.read_text(errors="replace")
                  for p in (ROOT / "packaging").rglob("*") if p.is_file()))


def main() -> int:
    test_catalogue()
    test_credentials()
    test_probe()
    test_registry()
    test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
