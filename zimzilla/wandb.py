"""wandb.ai inference — a hosted OpenAI-compatible endpoint reached directly.

wandb serves Chat Completions at ``https://api.inference.wandb.ai/v1`` with no
LiteLLM proxy in front of it, so it is a *direct* source, the same shape as
Token Juice and NVIDIA NIM: no port to start, no health check, nothing to
install. The profile below is the whole install, and the key is the operator's
own ``wandb_v1_…`` token.

Measured on this box, 2026-11-04, against the live endpoint:

* ``GET /v1/models`` answers with a key and lists the served models.
* ``POST /v1/chat/completions`` streams standard SSE, tool calls arrive as
  ``delta.tool_calls[]`` with a ``function.arguments`` string, and
  ``stream_options: {"include_usage": true}`` is honoured.
* The model ``deepseek-ai/DeepSeek-V4.1-Flash`` answers a chat turn in a few
  seconds — fast enough that the slowness the operator complained about on
  Token Juice does not show up here.

Nothing here echoes a key. The profile is written mode 600 and read back the
same way every other source's is.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
import re

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))

#: The upstream API root. No proxy sits in front of it: this is the address the
#: session itself is pointed at.
API_BASE_URL = "https://api.inference.wandb.ai/v1"

#: The public model catalog. A key is required to list models.
MODELS_URL = f"{API_BASE_URL}/models"

#: The profile ``/wandb-api-setup`` writes and every lookup reads.
PROFILE = ZIMZILLA_HOME / "wandb" / "source"

#: wandb keys are issued as ``wandb_v1_<long token>``. A hint for the
#: confirmation line, never a gate: the upstream is the authority on its own
#: keys.
KEY_PREFIX = "wandb_v1_"

#: The model a switch selects when the profile names none.
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash"

#: A fetched catalog older than this is refetched rather than trusted.
CACHE_TTL = 300.0

#: The window to plan against when neither the catalog nor the table knows.
DEFAULT_CONTEXT = 1_048_576


@dataclass(frozen=True)
class Model:
    """One entry from wandb's model catalog."""

    id: str
    owned_by: str = ""
    context_length: int | None = None

    @property
    def free(self) -> bool:
        """Whether this model costs money to call.

        wandb's inference preview is free to use with a valid key, so every
        catalog entry reads as free rather than billing at DEFAULT_PRICE.
        """
        return True

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn.

        Filtered on family so embeddings, rerankers and guards never appear in
        the picker as chat options.
        """
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

    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits: list[str] = ["free"]
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------


def _read_exports(path: Path) -> dict[str, str]:
    """Parse ``export VAR="value"`` lines from a profile, without a shell."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
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
        if name:
            out[name] = value
    return out


def current_key() -> str | None:
    """The wandb key the operator last selected, if any."""
    from . import sources as sources_mod
    return sources_mod.current_key()


def set_current(key: str) -> None:
    """Record the selection. Never fatal — a read-only home must not break the TUI."""
    from . import sources as sources_mod
    sources_mod.set_current(key)


def is_wandb(base_url: str | None) -> bool:
    """Whether *base_url* points at wandb inference."""
    return "inference.wandb.ai" in (base_url or "")


active_endpoint = is_wandb


def load_credentials() -> tuple[str, str, str] | None:
    """The wandb key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ZimZilla's own profile, written by ``/wandb-api-setup``.
      2. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` in the environment,
         when ``ANTHROPIC_BASE_URL`` points at wandb.

    Returns ``(token, model, origin)`` or ``None``.
    """
    exports = _read_exports(PROFILE)
    token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
    if token:
        return token, exports.get("ANTHROPIC_MODEL", ""), str(PROFILE)

    if is_wandb(os.environ.get("ANTHROPIC_BASE_URL")):
        token = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                 or os.environ.get("ANTHROPIC_API_KEY") or "")
        if token:
            return token, os.environ.get("ANTHROPIC_MODEL", ""), "environment"

    return None


def save_key(token: str, model: str = "") -> Path:
    """Write the key to ZimZilla's own wandb profile, mode 600."""
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        f'export ANTHROPIC_BASE_URL="{API_BASE_URL}/"\n'
        f'export ANTHROPIC_AUTH_TOKEN="{token}"\n'
        f'export ANTHROPIC_MODEL="{model or DEFAULT_MODEL}"\n'
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


def apply_to(cfg) -> tuple[str, str] | None:
    """Point *cfg* at wandb. Returns ``(origin, model)`` or ``None``.

    Shared by the ``/zim-wandb`` command and the launch-time restore.
    ``api_key`` is cleared so a stale key from another provider cannot take
    precedence over the wandb token.
    """
    creds = load_credentials()
    if creds is None:
        return None
    token, model, origin = creds
    cfg.base_url = API_BASE_URL
    cfg.auth_token = token
    cfg.api_key = ""
    if model:
        cfg.model = model
    return origin, model


def applies_to_default_endpoint(base_url: str | None) -> bool:
    """Whether a remembered wandb choice should override *base_url*."""
    url = (base_url or "").strip()
    if is_wandb(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


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
        ctx = entry.get("context_length")
        try:
            ctx = int(ctx) if ctx is not None else None
        except (TypeError, ValueError):
            ctx = None
        out.append(Model(
            id=mid,
            owned_by=str(entry.get("owned_by") or ""),
            context_length=ctx,
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch wandb's model catalog. Returns ``(models, error)``."""
    token = (token or "").strip()
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    headers = {"Accept": "application/json", "User-Agent": "zimzilla"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(MODELS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "key rejected (401/403) — check it with /wandb-api-setup"
        return None, f"HTTP {e.code} from inference.wandb.ai"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach inference.wandb.ai: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def chat_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the entries that can answer a chat turn."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.chat], ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every chat model id, catalog order preserved."""
    models, err = chat_models(token, **kw)
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


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    Deliberately loose -- the upstream decides what is valid, not us -- but
    strict enough to catch a pasted shell line or a bare word. The
    ``wandb_v1_`` prefix is *not* required: a real key of an unexpected shape
    is worth more stored than rejected, and a wrong one only costs a single 401.
    """
    value = (value or "").strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))


def model_for(model: str, models: list[Model] | None) -> Model | None:
    if not models:
        return None
    base = (model or "").strip()
    for m in models:
        if m.id == base:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(0.0, 0.0)`` for any catalog entry — wandb's preview is free."""
    found = model_for(model, models)
    if found is None:
        return None
    return (0.0, 0.0)


def default_free_model() -> str:
    return DEFAULT_MODEL


def explain_error(status: int, body: str) -> str:
    """A sentence for a wandb failure, from the shape it came back in."""
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
        return "key rejected — check it with /wandb-api-setup"
    if status == 404:
        return "this model is not served by wandb — pick another model"
    if status == 429:
        return "rate limited (429) — retry shortly"
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    return f"HTTP {status}"


def verify_key(token: str, model: str = "", *, timeout: float = 90.0
               ) -> tuple[bool, str]:
    """Prove *token* works by making one real, tiny chat call."""
    token = (token or "").strip()
    if not token:
        return False, "no key"
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{API_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "zimzilla"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True, "key accepted"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except OSError:
            pass
        if e.code in (401, 403):
            return False, f"HTTP {e.code} — {explain_error(e.code, body)}"
        return True, explain_error(e.code, body)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"could not reach inference.wandb.ai: {e}"
