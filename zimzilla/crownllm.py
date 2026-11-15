"""CrowLLM — a hosted OpenAI-compatible gateway, as a free-only source.

CrowLLM serves Chat Completions at ``https://crowllm.com/v1``. Its catalogue is
public (``GET /v1/models`` answers 200 without a key) and marks free models with
the ``:free`` suffix. ZimZilla offers only those, so this source cannot land on
the two paid ids in the catalogue.

Measured 2026-11-15: 76 catalogue entries, 74 ending in ``:free``. Individual
free models are uneven: some return provider 503s, while ``gpt-4o:free`` and
``qwen-3.8-27b:free`` answered. ``gpt-4o:free`` also round-tripped a tool call.
The default is therefore a known-working free id, not simply catalogue order.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
CLAUDE_SOURCE = Path(os.environ.get("ZIMZILLA_CLAUDE_SOURCE")
                     or (Path.home() / "claude-source"))

HOST = "crowllm.com"
DEFAULT_BASE_URL = "https://crowllm.com/v1"
CATALOG_URL = "https://crowllm.com/v1/models"
CHAT_URL = "https://crowllm.com/v1/chat/completions"

PROFILE = ZIMZILLA_HOME / "crownllm" / "source"
KEY_RE = re.compile(r"^[A-Za-z0-9._\-]{20,200}$")

DEFAULT_MODEL = "gpt-4o:free"

#: Free ids that answered on 2026-11-15, best first. The live catalogue is
#: authoritative; this is the cold-cache / offline fallback.
FREE_FALLBACK: tuple[str, ...] = (
    "gpt-4o:free",
    "qwen-3.8-27b:free",
    "gemini-3-flash:free",
    "deepseek-v4.1-flash:free",
    "glm-5.3-flash:free",
    "gemini-3.5-flash:free",
    "llama-3.3-70b-instruct:free",
    "mistral-medium-3.5:free",
    "nemotron-3-ultra-550b-a55b:free",
    "minimax-m2.5:free",
)

CACHE_TTL = 300.0
TIMEOUT = 30.0


class CrownError(Exception):
    """A catalogue or credential failure worth showing."""


@dataclass(frozen=True)
class Model:
    id: str
    owned_by: str = ""
    created: int = 0

    @property
    def free(self) -> bool:
        return self.id.endswith(":free")

    @property
    def chat(self) -> bool:
        return True

    @property
    def blurb(self) -> str:
        bits = ["free"]
        if self.owned_by:
            bits.append(self.owned_by)
        return " · ".join(bits)


_CACHE: dict[str, tuple[list[Model], float]] = {}


def _host(base_url: str | None) -> str:
    try:
        return (urllib.parse.urlsplit(base_url or "").hostname or "").lower()
    except ValueError:
        return ""


def is_crownllm(base_url: str | None) -> bool:
    host = _host(base_url)
    return host == HOST or host.endswith("." + HOST)


active_endpoint = is_crownllm


def current_key() -> str | None:
    from . import sources as sources_mod
    return sources_mod.current_key()


def set_current(key: str) -> None:
    from . import sources as sources_mod
    sources_mod.set_current(key)


def looks_like_key(value: str) -> bool:
    return bool(KEY_RE.fullmatch((value or "").strip()))


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

def _read_exports(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        name, _, value = line[len("export "):].partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name.strip():
            out[name.strip()] = value
    return out


def _token_from(exports: dict) -> str:
    return (exports.get("CROWNLLM_API_KEY")
            or exports.get("ANTHROPIC_AUTH_TOKEN")
            or exports.get("ANTHROPIC_API_KEY") or "").strip()


def load_credentials() -> tuple[str, str, str] | None:
    env = os.environ.get("CROWNLLM_API_KEY", "").strip()
    if env:
        return env, os.environ.get("ANTHROPIC_MODEL", ""), "environment"
    if is_crownllm(os.environ.get("ANTHROPIC_BASE_URL")):
        token = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                 or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if token:
            return token, os.environ.get("ANTHROPIC_MODEL", ""), "environment"
    exports = _read_exports(PROFILE)
    token = _token_from(exports)
    if token:
        return token, exports.get("ANTHROPIC_MODEL", ""), str(PROFILE)
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_crownllm(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = _token_from(exports)
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def save_key(token: str, model: str = "") -> Path:
    token = (token or "").strip()
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        "# ZimZilla — CrowLLM profile (written by /crownllm-api-key)\n"
        "# Treat this file as a credential. Never commit it.\n"
        f'export CROWNLLM_API_KEY="{token}"\n'
        f'export ANTHROPIC_BASE_URL="{DEFAULT_BASE_URL}"\n'
        f'export ANTHROPIC_AUTH_TOKEN="{token}"\n'
        f'export ANTHROPIC_MODEL="{model}"\n'
        'export ANTHROPIC_API_KEY=""\n'
    )
    fd = os.open(PROFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        os.chmod(PROFILE, 0o600)
    except OSError:
        pass
    return PROFILE


def has_key() -> bool:
    return load_credentials() is not None


# ---------------------------------------------------------------------------
# catalogue
# ---------------------------------------------------------------------------

def _parse(payload: dict) -> list[Model]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    out: list[Model] = []
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid or not mid.endswith(":free") or mid in seen:
            continue
        seen.add(mid)
        try:
            created = int(entry.get("created") or 0)
        except (TypeError, ValueError):
            created = 0
        out.append(Model(id=mid, owned_by=str(entry.get("owned_by") or ""),
                         created=created))
    return out


def fetch_models(*, timeout: float = TIMEOUT, use_cache: bool = True
                 ) -> tuple[list[Model] | None, str]:
    if use_cache:
        hit = _CACHE.get("catalog")
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""
    headers = {"accept": "application/json", "User-Agent": "zimzilla"}
    creds = load_credentials()
    if creds:
        headers["Authorization"] = f"Bearer {creds[0]}"
    req = urllib.request.Request(CATALOG_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, explain_error(e.code, _body(e))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"
    models = _parse(payload)
    _CACHE["catalog"] = (models, time.monotonic())
    return models, ""


def free_models(*, timeout: float = TIMEOUT) -> tuple[list[Model] | None, str]:
    models, err = fetch_models(timeout=timeout)
    if models is None:
        return None, err
    return sorted(models, key=_rank), ""


def usable_free_models(*, timeout: float = TIMEOUT):
    return free_models(timeout=timeout)


def _rank(m: Model) -> tuple[int, int]:
    try:
        return (FREE_FALLBACK.index(m.id), 0)
    except ValueError:
        return (len(FREE_FALLBACK), -m.created)


def cached_models() -> list[Model] | None:
    hit = _CACHE.get("catalog")
    if not hit or (time.monotonic() - hit[1]) >= CACHE_TTL:
        return None
    return list(hit[0])


def cached_free_models() -> list[Model] | None:
    models = cached_models()
    return sorted(models, key=_rank) if models is not None else None


def cached_ids() -> list[str] | None:
    models = cached_free_models()
    return [m.id for m in models] if models is not None else None


def catalog_age() -> float | None:
    hit = _CACHE.get("catalog")
    return time.monotonic() - hit[1] if hit else None


def clear_cache() -> None:
    _CACHE.clear()


def model_for(model: str, models: list[Model] | None) -> Model | None:
    for m in models or []:
        if m.id == model:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    found = model_for(model, models)
    return (0.0, 0.0) if found is not None and found.free else None


def default_free_model(*, timeout: float = 8.0) -> str:
    cached = cached_free_models()
    if cached:
        return cached[0].id
    models, _ = free_models(timeout=timeout)
    if models:
        return models[0].id
    return FREE_FALLBACK[0]


def apply_to(cfg) -> tuple[str, str] | None:
    creds = load_credentials()
    if creds is None:
        return None
    token, saved, origin = creds
    cfg.base_url = DEFAULT_BASE_URL
    cfg.auth_token = token
    cfg.api_key = ""
    free = cached_ids() or list(FREE_FALLBACK)
    cfg.model = saved if saved and saved in free else default_free_model()
    return origin, cfg.model


def applies_to_default_endpoint(base_url: str | None) -> bool:
    url = (base_url or "").strip()
    if is_crownllm(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------------------
# errors and probe
# ---------------------------------------------------------------------------

def _body(exc) -> str:
    try:
        return exc.read().decode("utf-8", "replace")
    except OSError:
        return ""


def explain_error(status: int, body: str) -> str:
    text = (body or "").strip()
    detail = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
        if not detail:
            detail = str(payload.get("message") or payload.get("detail") or "")
    lowered = (detail or text).lower()
    if status in (401, 403):
        return "key rejected — check it with /crownllm-api-key"
    if status == 402 or "credit" in lowered or "balance" in lowered:
        return "out of credits — use a :free model or top up at https://crowllm.com"
    if status == 429:
        return "rate limited (429) — retry shortly"
    if status == 503 or "cpu overloaded" in lowered or "provider" in lowered:
        return ("the provider is overloaded or failing for this model — try "
                "another :free model")
    if status >= 500:
        return f"the gateway is unavailable ({status}) — retry shortly"
    return f"HTTP {status}" + (f": {detail[:200]}" if detail else "")


def verify_key(token: str, *, timeout: float = 60.0) -> tuple[bool, str]:
    """Prove the key with a tiny chat call to a free model.

    The catalogue is public, so a 200 there proves nothing about a credential.
    ``gpt-4o:free`` is the measured working default; a 503 means the provider is
    busy, not that the key is wrong, and is reported as accepted-with-detail.
    """
    token = (token or "").strip()
    if not token:
        return False, "no key"
    body = {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 4,
    }
    req = urllib.request.Request(
        CHAT_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "zimzilla"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True, "key accepted"
    except urllib.error.HTTPError as e:
        detail = explain_error(e.code, _body(e))
        if e.code in (401, 403):
            return False, detail
        return True, detail
    except (urllib.error.URLError, OSError) as e:
        return False, f"could not reach {HOST}: {e}"
