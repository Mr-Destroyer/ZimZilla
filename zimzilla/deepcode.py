"""DeepAI Code — the free/unmetered models inside the AppImage, reachable from ZimZilla.

Reverse-engineered from ``DeepAI-Code-0.3.42-x86_64.AppImage`` (see
``/home/zim/Documents/deepcode-ai-unlimited/FINDINGS.md`` for the full report).

How the app invokes its models:

1. The user signs in once through the app's device-code flow at
   ``https://api.deepai.org/deepai_code/desktop_auth/``. The app saves the
   resulting ``api_key`` to
   ``~/.deepai-code/userdata/secrets/deepai-account.bin``.
2. The app starts a loopback model transport on ``127.0.0.1:<random port>`` that
   forwards ``POST /chat/completions`` to
   ``https://api.deepai.org/deepai_code/chat/completions``, stripping the
   ``deepai/`` prefix from the model name and sending these headers:

   * ``X-DeepAI-Code-Theos: "7"`` — lane / build version
   * ``X-DeepAI-Code-Instance: <16 random bytes hex>``
   * ``X-DeepAI-Code-Machine: sha256(system machine-id)`` — from ``/etc/machine-id``
   * ``api-key: <saved api_key>``
   * ``Content-Type: application/json``

3. Models with ``theosPerRequest == "0"`` are free and unmetered. The app writes
   the full catalog to
   ``~/.deepai-code/userdata/deepai/pi/deepai/models.json``.

This module reproduces steps 2-3 directly: it reads the saved api_key, computes
the machine-id header, and calls the upstream endpoint itself — no Electron, no
AppImage, no local transport needed. It is used by the ``/zim-zimthegoat``
command.

Free models (11, theosPerRequest == "0"):

    deepai/glm-5.3-flash        GLM 5.3 Flash
    deepai/deepseek-v4.1-flash  DeepSeek V4.1 Flash 1017
    deepai/mimo-2.6-flash       MiMo 2.6 Flash
    deepai/qwen-3.8-flash       Qwen 3.8 Flash
    deepai/minimax-m3.1-flash   MiniMax M3.1 Flash
    deepai/muse-ember           Muse Ember
    deepai/solar-mini-4         Solar Mini 4
    deepai/gpt-6-luna           GPT-6 Luna
    deepai/mimo-2.6-pro         MiMo 2.6 Pro
    deepai/glyph-cluster        Glyph Cluster
    deepai/deepai-cyber         DeepAI Cyber

Paid models (theosPerRequest > 0) are also listed — they answer 200 on this
endpoint but spend Theos per call. ``gpt-6-astra`` specifically 404s on this
build.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_API_ORIGIN = "https://api.deepai.org"
CHAT_PATH = "/deepai_code/chat/completions"
MODELS_PATH = "/deepai_code/models"

#: The upstream API root. No proxy sits in front of it: this is the address the
#: session itself is pointed at.
API_BASE_URL = DEFAULT_API_ORIGIN

#: A fetched catalog older than this is refetched rather than trusted.
CACHE_TTL = 300.0

#: The window to plan against when neither the catalog nor the table knows.
DEFAULT_CONTEXT = 320_000

SECRETS_PATH = Path(
    os.path.expanduser("~/.deepai-code/userdata/secrets/deepai-account.bin")
)
MODELS_PATH = Path(
    os.path.expanduser("~/.deepai-code/userdata/deepai/pi/deepai/models.json")
)
MACHINE_ID_PATHS = ["/etc/machine-id", "/var/lib/dbus/machine-id"]

#: Slug -> human name, for the 11 free models verified working.
FREE_MODELS: dict[str, str] = {
    "glm-5.3-flash": "GLM 5.3 Flash",
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash 1017",
    "mimo-2.6-flash": "MiMo 2.6 Flash",
    "qwen-3.8-flash": "Qwen 3.8 Flash",
    "minimax-m3.1-flash": "MiniMax M3.1 Flash",
    "muse-ember": "Muse Ember",
    "solar-mini-4": "Solar Mini 4",
    "gpt-6-luna": "GPT-6 Luna",
    "mimo-2.6-pro": "MiMo 2.6 Pro",
    "glyph-cluster": "Glyph Cluster",
    "deepai-cyber": "DeepAI Cyber",
}

#: Paid models observed in the catalog (theosPerRequest > 0). They answer 200
#: but spend Theos per call. gpt-6-astra 404s on this build.
PAID_MODELS: dict[str, str] = {
    "minimax-m3": "MiniMax M3",
    "gpt-6.1-sol": "GPT-6.1 Sol",
    "gemini-4-argon": "Gemini 4 Argon",
    "glm-5.3": "GLM 5.3",
    "kimi-k3": "Kimi K3",
    "kimi-k3.1-pro": "Kimi K3.1 Pro",
    "gpt-6.1-terra": "GPT-6.1 Terra",
    "muse-spark-1.3": "Muse Spark 1.3",
    "gemini-4-hydrogen": "Gemini 4 Hydrogen",
    "gemini-4-carbon": "Gemini 4 Carbon",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-opus-5-5": "Claude Opus 5.5",
    "muse-wonder": "Muse Wonder",
    "qwen-3.8-max-prime": "Qwen 3.8 Max Prime",
    "claude-fable-5-2": "Claude Fable 5.2",
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-6.1-terra": "GPT-6.1 Terra",
}


@dataclass
class DeepAIModel:
    slug: str
    name: str
    free: bool
    context_window: int
    theos_per_request: Optional[str]


def load_api_key() -> Optional[str]:
    """Read the api_key the DeepAI Code app saved after sign-in."""
    env_key = os.environ.get("DEEPAI_API_KEY")
    if env_key:
        return env_key
    try:
        with open(SECRETS_PATH, "r") as f:
            data = json.load(f)
        return data.get("apiKey")
    except Exception:
        return None


def load_machine_id() -> Optional[str]:
    """sha256 of the system machine-id — same header the app sends."""
    env_mid = os.environ.get("DEEPAI_MACHINE_ID")
    if env_mid:
        return env_mid
    for path in MACHINE_ID_PATHS:
        try:
            with open(path, "r") as f:
                raw = f.read().strip().lower()
            if raw and not set(raw) <= {"0"}:
                return hashlib.sha256(raw.encode()).hexdigest()
        except Exception:
            continue
    return None


def load_cached_models() -> list[dict]:
    """Return the model list the app wrote to models.json (if present)."""
    try:
        with open(MODELS_PATH, "r") as f:
            data = json.load(f)
        return data.get("providers", {}).get("deepai", {}).get("models", [])
    except Exception:
        return []


def all_models() -> list[DeepAIModel]:
    """Every DeepAI Code model known to this machine, free and paid.

    The app's cached catalog is the source of truth; models not in it are
    filled in from the reverse-engineered lists so the display is complete.
    """
    cached = load_cached_models()
    seen: dict[str, DeepAIModel] = {}

    for m in cached:
        mid = m.get("id", "")
        slug = f"deepai/{mid}"
        seen[slug] = DeepAIModel(
            slug=slug,
            name=m.get("name", mid),
            free=m.get("theosPerRequest") == "0",
            context_window=m.get("contextWindow", 0),
            theos_per_request=m.get("theosPerRequest"),
        )

    for mid, name in FREE_MODELS.items():
        slug = f"deepai/{mid}"
        if slug not in seen:
            seen[slug] = DeepAIModel(
                slug=slug, name=name, free=True, context_window=0,
                theos_per_request="0",
            )
    for mid, name in PAID_MODELS.items():
        slug = f"deepai/{mid}"
        if slug not in seen:
            seen[slug] = DeepAIModel(
                slug=slug, name=name, free=False, context_window=0,
                theos_per_request=None,
            )

    return sorted(seen.values(), key=lambda m: (not m.free, m.slug))


def free_models() -> list[DeepAIModel]:
    """Only the free (theosPerRequest == "0") models."""
    return [m for m in all_models() if m.free]


@dataclass(frozen=True)
class Model:
    """One entry from DeepAI Code's model catalog."""

    id: str
    name: str
    free: bool
    context_window: int

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn."""
        low = self.id.lower()
        return not any(
            marker in low
            for marker in (
                "embed", "bge", "rerank", "ranking", "reward",
                "clip", "neva", "vila", "kosmos", "deplot", "diffusion",
                "guard", "safety", "jailbreak", "detector", "gliner", "pii",
                "parse", "nemoretriever", "calibration", "safety-guard",
            )
        )

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits: list[str] = ["free"] if self.free else ["paid"]
        if self.context_window:
            bits.append(f"{self.context_window // 1000}k ctx")
        return " · ".join(bits)


def is_deepai(base_url: str | None) -> bool:
    """Whether *base_url* points at DeepAI Code."""
    return "api.deepai.org" in (base_url or "")


active_endpoint = is_deepai


def load_credentials() -> tuple[str, str, str] | None:
    """The DeepAI api_key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ZimZilla's own profile, written by ``/deepai-api-setup``.
      2. ``DEEPAI_API_KEY`` in the environment.
      3. The DeepAI Code app's saved secret file.

    Returns ``(api_key, model, origin)`` or ``None``.
    """
    from . import sources as sources_mod
    profile = sources_mod.ZIMZILLA_HOME / "deepai" / "source"
    try:
        text = profile.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        body = line[len("export "):]
        if "=" not in body:
            continue
        name, _, value = body.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name == "DEEPAI_API_KEY" and value:
            return value, "", str(profile)

    env_key = os.environ.get("DEEPAI_API_KEY")
    if env_key:
        return env_key, "", "environment"

    app_key = load_api_key()
    if app_key:
        return app_key, "", "DeepAI Code app"

    return None


def has_key() -> bool:
    return load_credentials() is not None


def save_key(api_key: str, model: str = "") -> Path:
    """Write the DeepAI api_key to ZimZilla's own deepai profile, mode 600."""
    from . import sources as sources_mod
    profile = sources_mod.ZIMZILLA_HOME / "deepai" / "source"
    profile.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(profile.parent, 0o700)
    except OSError:
        pass
    body = (
        f'export DEEPAI_API_KEY="{api_key}"\n'
        f'export DEEPAI_API_ORIGIN="{API_BASE_URL}"\n'
        f'export DEEPAI_MODEL="{model or DEFAULT_MODEL}"\n'
    )
    fd = os.open(profile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        os.chmod(profile, 0o600)
    except OSError:
        pass
    return profile


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

_CACHE: dict[str, tuple[list[Model], float]] = {}


def _parse_models(payload: dict) -> list[Model]:
    out: list[Model] = []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        name = str(entry.get("name") or mid)
        free = entry.get("theosPerRequest") == "0"
        ctx = entry.get("contextWindow")
        try:
            ctx = int(ctx) if ctx is not None else 0
        except (TypeError, ValueError):
            ctx = 0
        out.append(Model(
            id=f"deepai/{mid}",
            name=name,
            free=free,
            context_window=ctx,
        ))
    return out


def fetch_models(api_key: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch DeepAI Code's model catalog. Returns ``(models, error)``."""
    api_key = (api_key or "").strip()
    if use_cache:
        hit = _CACHE.get(api_key)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    headers = {"Accept": "application/json", "User-Agent": "zimzilla"}
    if api_key:
        headers["api-key"] = api_key
    machine_id = load_machine_id()
    if machine_id:
        headers["X-DeepAI-Code-Machine"] = machine_id
    headers["X-DeepAI-Code-Theos"] = "7"
    headers["X-DeepAI-Code-Instance"] = "00" * 16

    url = f"{API_BASE_URL}{MODELS_PATH}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "key rejected (401/403) — check it with /deepai-api-setup"
        return None, f"HTTP {e.code} from api.deepai.org"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach api.deepai.org: {e}"

    models = _parse_models(payload)
    _CACHE[api_key] = (models, time.monotonic())
    return models, ""


def chat_models(api_key: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the entries that can answer a chat turn."""
    models, err = fetch_models(api_key, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.chat], ""


def model_ids(api_key: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every chat model id, catalog order preserved."""
    models, err = chat_models(api_key, **kw)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def catalog_age() -> float | None:
    if not _CACHE:
        return None
    newest = max(entry[1] for entry in _CACHE.values())
    return time.monotonic() - newest


def cached_models() -> list[Model] | None:
    newest: tuple[list[Model], float] | None = None
    for entry in _CACHE.values():
        if newest is None or entry[1] > newest[1]:
            newest = entry
    if newest is None or (time.monotonic() - newest[1]) >= CACHE_TTL:
        return None
    return list(newest[0])


def cached_ids() -> list[str] | None:
    models = cached_models()
    return [m.id for m in models if m.chat] if models is not None else None


def clear_cache() -> None:
    _CACHE.clear()


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(0.0, 0.0)`` for free models, ``None`` for unknown ones."""
    found = model_for(model, models)
    if found is None:
        return None
    return (0.0, 0.0) if found.free else None


def model_for(model: str, models: list[Model] | None) -> Model | None:
    if not models:
        return None
    base = (model or "").strip()
    for m in models:
        if m.id == base:
            return m
    return None


def default_free_model() -> str:
    return "deepai/glm-5.3-flash"


def explain_error(status: int, body: str) -> str:
    """A sentence for a DeepAI failure, from the shape it came back in."""
    text = (body or "").strip()
    detail = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        detail = str(payload.get("detail") or payload.get("message") or "")
    lowered = (detail or text).lower()
    if status in (401, 403):
        return "key rejected — check it with /deepai-api-setup"
    if status == 404:
        return "this model is not served by DeepAI Code — pick another model"
    if status == 429:
        return "rate limited (429) — retry shortly"
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    return f"HTTP {status}"


def chat_completion(model: str, prompt: str, origin: Optional[str] = None,
                    api_key: Optional[str] = None, stream: bool = False) -> tuple[int, str]:
    """Send one chat completion to DeepAI Code's endpoint.

    Returns ``(status_code, body_text)``. On HTTP error the status is the
    error code and the body is the error JSON.
    """
    if api_key is None:
        api_key = load_api_key()
    if not api_key:
        return 0, ("No DeepAI api_key found. Sign in to DeepAI Code once, or set "
                   "DEEPAI_API_KEY.")
    if origin is None:
        origin = os.environ.get("DEEPAI_API_ORIGIN", DEFAULT_API_ORIGIN)

    machine_id = load_machine_id()
    headers = {
        "Content-Type": "application/json",
        "X-DeepAI-Code-Theos": "7",
        "X-DeepAI-Code-Instance": "00" * 16,
        "api-key": api_key,
    }
    if machine_id:
        headers["X-DeepAI-Code-Machine"] = machine_id

    upstream_model = model.replace("deepai/", "", 1)
    body = {
        "model": upstream_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
    }

    url = f"{origin.rstrip('/')}{CHAT_PATH}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"
