"""Numes — a hosted inference marketplace, as a free-only ZimZilla source.

Numes serves the OpenAI Chat Completions API at ``/api/v1``. Its catalogue is
key-gated (``GET /api/v1/models`` answers 401 without a key) and prices each
model per token in USD. A model is offered here only when **both** prompt and
completion prices are zero; a missing price is not free. The source therefore
cannot silently land on a paid model.

Measured 2026-11-14 with an account key: 44 models, 9 at $0/$0, including
``deepseek/deepseek-v4.1-flash`` and ``z-ai/glm-5.3-flash``. A free model
answered a Chat Completions turn and a tool call with ``usage.cost: 0``.
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

HOST = "numes.ai"
#: Chat Completions base. The OpenAI SDK appends /chat/completions itself.
DEFAULT_BASE_URL = "https://api.numes.ai/api/v1"
CATALOG_URL = "https://api.numes.ai/api/v1/models"

PROFILE = ZIMZILLA_HOME / "numes" / "source"
KEY_RE = re.compile(r"^sk-[A-Za-z0-9._\-]{20,200}$")

#: The free model to land on when the catalogue has not been read. It is a real
#: free id measured on 2026-11-14, with a 1M window and tools.
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

#: Free chat ids measured at the same time. Used by /model on a cold cache; the
#: live catalogue replaces them as soon as it is fetched.
FREE_FALLBACK: tuple[str, ...] = (
    "deepseek/deepseek-v4.1-flash",
    "z-ai/glm-5.3-flash",
    "z-ai/glm-5.3",
    "moonshotai/kimi-k3.1-pro",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "uniboshi/kimi-k3-abliterated-v1",
    "dealignai/deepseek-v4.1-flash-uncensored-fp8",
    "dealignai/glm-5.3-flash-uncensored-fp8",
    "dealignai/glm-5.3-uncensored-fp8",
)

CACHE_TTL = 300.0
TIMEOUT = 30.0


class NumesError(Exception):
    """A catalogue or credential failure worth showing."""


@dataclass(frozen=True)
class Model:
    id: str
    name: str = ""
    context_length: int | None = None
    max_output: int | None = None
    price_in: float | None = None      # USD per million tokens
    price_out: float | None = None
    modalities: tuple[str, ...] = ()
    supported: tuple[str, ...] = ()

    @property
    def free(self) -> bool:
        return self.price_in == 0.0 and self.price_out == 0.0

    @property
    def chat(self) -> bool:
        mods = set(self.modalities)
        return "text" in mods if mods else True

    @property
    def vision(self) -> bool:
        return "image" in set(self.modalities)

    @property
    def blurb(self) -> str:
        bits = ["free"]
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
        if self.vision:
            bits.append("vision")
        bits.append(self.id.split("/", 1)[0])
        return " · ".join(bits)


_CACHE: dict[str, tuple[list[Model], float]] = {}


def _host(base_url: str | None) -> str:
    try:
        return (urllib.parse.urlsplit(base_url or "").hostname or "").lower()
    except ValueError:
        return ""


def is_numes(base_url: str | None) -> bool:
    host = _host(base_url)
    return host == HOST or host.endswith("." + HOST)


active_endpoint = is_numes


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
    return (exports.get("NUMES_API_KEY")
            or exports.get("ANTHROPIC_AUTH_TOKEN")
            or exports.get("ANTHROPIC_API_KEY") or "").strip()


def load_credentials() -> tuple[str, str, str] | None:
    env = os.environ.get("NUMES_API_KEY", "").strip()
    if env:
        return env, os.environ.get("ANTHROPIC_MODEL", ""), "environment"
    if is_numes(os.environ.get("ANTHROPIC_BASE_URL")):
        token = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                 or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if token:
            return token, os.environ.get("ANTHROPIC_MODEL", ""), "environment"
    token = _token_from(_read_exports(PROFILE))
    if token:
        return token, _read_exports(PROFILE).get("ANTHROPIC_MODEL", ""), str(PROFILE)
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_numes(exports.get("ANTHROPIC_BASE_URL")):
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
        "# ZimZilla — Numes profile (written by /numes-api-setup)\n"
        "# Treat this file as a credential. Never commit it.\n"
        f'export NUMES_API_KEY="{token}"\n'
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

def _num(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse(payload: dict) -> list[Model]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    out: list[Model] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        pricing = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}
        arch = entry.get("architecture") if isinstance(entry.get("architecture"), dict) else {}
        top = entry.get("top_provider") if isinstance(entry.get("top_provider"), dict) else {}
        mods = arch.get("input_modalities")
        modalities = tuple(str(m) for m in mods) if isinstance(mods, list) else ()
        supported = entry.get("supported_parameters")
        supported_t = tuple(str(x) for x in supported) if isinstance(supported, list) else ()
        pin, pout = _num(pricing.get("prompt")), _num(pricing.get("completion"))
        # Catalogue prices are per token; the harness displays per Mtok.
        p_in = pin * 1_000_000 if pin is not None else None
        p_out = pout * 1_000_000 if pout is not None else None
        out.append(Model(
            id=mid,
            name=str(entry.get("name") or mid),
            context_length=_int(entry.get("context_length")),
            max_output=(_int(entry.get("maximum_output_length"))
                        or _int(top.get("max_completion_tokens"))),
            price_in=p_in,
            price_out=p_out,
            modalities=modalities,
            supported=supported_t,
        ))
    return out


def fetch_models(token: str | None = None, *, timeout: float = TIMEOUT,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    if use_cache:
        hit = _CACHE.get("catalog")
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""
    if not token:
        creds = load_credentials()
        token = creds[0] if creds else ""
    if not token:
        return None, "no Numes key — run /numes-api-setup <key>"
    req = urllib.request.Request(CATALOG_URL, headers={
        "Authorization": f"Bearer {token}",
        "accept": "application/json", "User-Agent": "zimzilla",
    })
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


def free_models(token: str | None = None, *, timeout: float = TIMEOUT
                ) -> tuple[list[Model] | None, str]:
    models, err = fetch_models(token, timeout=timeout)
    if models is None:
        return None, err
    free = [m for m in models if m.free and m.chat]
    free.sort(key=_rank)
    return free, ""


def usable_free_models(token: str | None = None, *, timeout: float = TIMEOUT
                       ) -> tuple[list[Model] | None, str]:
    return free_models(token, timeout=timeout)


def _rank(m: Model) -> tuple[int, int]:
    try:
        return (FREE_FALLBACK.index(m.id), -(m.context_length or 0))
    except ValueError:
        return (len(FREE_FALLBACK), -(m.context_length or 0))


def cached_models() -> list[Model] | None:
    hit = _CACHE.get("catalog")
    if not hit or (time.monotonic() - hit[1]) >= CACHE_TTL:
        return None
    return list(hit[0])


def cached_free_models() -> list[Model] | None:
    models = cached_models()
    if models is None:
        return None
    return sorted([m for m in models if m.free and m.chat], key=_rank)


def cached_ids() -> list[str] | None:
    free = cached_free_models()
    return [m.id for m in free] if free is not None else None


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
    if found is None or not found.free:
        return None
    return (0.0, 0.0)


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
    if is_numes(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------------------
# errors and key probe
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
        return "key rejected — check it with /numes-api-setup"
    if status == 402 or "credit" in lowered or "balance" in lowered:
        return ("out of credits — Numes holds an upper bound per request; top up "
                "at https://numes.ai, or use a $0 model")
    if status == 429:
        return "rate limited (429) — retry shortly"
    if status >= 500:
        return f"the gateway is unavailable ({status}) — retry shortly"
    return f"HTTP {status}" + (f": {detail[:200]}" if detail else "")


def verify_key(token: str, *, timeout: float = TIMEOUT) -> tuple[bool, str]:
    token = (token or "").strip()
    if not token:
        return False, "no key"
    req = urllib.request.Request(CATALOG_URL, headers={
        "Authorization": f"Bearer {token}", "accept": "application/json",
        "User-Agent": "zimzilla"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True, "key accepted"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, explain_error(e.code, _body(e))
        return True, explain_error(e.code, _body(e))
    except (urllib.error.URLError, OSError) as e:
        return False, f"could not reach {HOST}: {e}"
