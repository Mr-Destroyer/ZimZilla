"""Respan — a hosted AI gateway, as a ZimZilla source.

Respan fronts a large model catalogue behind one key and serves every chat
format natively. ZimZilla uses its Anthropic Messages surface:

    https://gateway.respan.ai        POST /v1/messages

which is the base an Anthropic SDK expects (it appends ``/v1/messages``
itself). The same host also serves OpenAI Chat Completions at
``/v1/chat/completions``; this module does not use it because the native
Messages route keeps tool calls intact and needs no translation.

**Only free models are offered, by request.** The public catalogue at
``https://api.respan.ai/api/v1/models`` publishes a price per model, in
tokens, and a model whose input and output prices are both zero is free. That
set is filtered, deduplicated to bare gateway ids (the catalogue also lists
provider-prefixed aliases) and ordered with the strongest chat models first.
``/model`` on this source therefore cannot land on a paid model, and a manual
name is checked against the same list.

**A free price is not an activated account.** Measured 2026-11-12 with a fresh
key: every free model — ``glm-4.7-flash``, ``gemma-4-31b-it``, ``span-01-free``
included — answered

    401 activation_required
    "Model 'glm-4.7-flash' needs Respan credits. Add credits at …/billing."

The catalogue price is zero, but the gateway still holds a credit check before
it will serve a model on Respan's keys. :func:`verify_key` distinguishes that
state from a rejected credential: a 401 ``activation_required`` means the key
is good and the account needs credits (or a BYOK provider key), while a 401
``authentication_error`` is a bad key. The switch reports the first honestly
instead of pretending the route is broken.

Nothing here echoes a key. The profile is a mode-600 ``source`` file in the
same shape as every other credential in the tree.
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

#: The host every Respan surface lives under. Used for recognition, not for a
#: substring test that would match an unrelated ``notrespan.ai``.
HOST = "respan.ai"

#: The Messages base. The SDK appends ``/v1/messages``; adding ``/v1`` here
#: would compose ``/v1/v1/messages`` and 404 on every turn.
DEFAULT_BASE_URL = "https://gateway.respan.ai"

#: The public, keyless catalogue. It carries a price per model, which is what
#: makes "free" a fact rather than a guess. The gateway's own ``/v1/models``
#: answers only with an authenticated plan's ids and no prices.
CATALOG_URL = "https://api.respan.ai/api/v1/models"

#: The profile this module writes and reads.
PROFILE = ZIMZILLA_HOME / "respan" / "source"

#: Keys look like ``MBQD1ZL0.sq…``. There is no published fixed prefix, so this
#: is only a shape check: long enough, no whitespace, URL-safe punctuation.
KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{20,200}$")

#: The free model to fall back to when the catalogue cannot be read. It is a
#: real gateway id with a 200k window that was listed at $0 in the catalogue.
DEFAULT_MODEL = "glm-4.7-flash"

#: The free chat models measured present on 2026-11-12, best first. Used when
#: the catalogue has not been fetched, so `/model` can never fall back to the
#: harness's default registry — which contains paid models from other sources.
FREE_FALLBACK: tuple[str, ...] = (
    "glm-4.7-flash",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
    "glm-4.6v-flash",
    "glm-4.5-flash",
    "apodex-1.1-mini",
    "leanstral-1-5",
    "allam-2-7b",
)

#: Free catalogue entries that are not chat models. ``phonic-1`` is speech and
#: ``span-01-lite`` publishes a zero context window, so neither can answer a
#: Messages turn. Kept by id rather than inferred because the catalogue
#: publishes no modality field for the free entries.
NON_CHAT = frozenset({"phonic-1", "phonic/phonic-1", "span-01-lite",
                      "openrouter/respan/span-01-lite",
                      "openrouter/respan/span-01-lite:free"})

#: A fetched catalogue older than this is refetched rather than trusted. The
#: free list moves with promotions, and a stale "free" claim is the one thing
#: this source must not make.
CACHE_TTL = 300.0

TIMEOUT = 30.0


class RespanError(Exception):
    """A catalogue or credential failure with a message worth showing."""


@dataclass(frozen=True)
class Model:
    """One catalogue entry."""

    id: str
    owned_by: str = ""
    context_length: int | None = None
    price_in: float | None = None
    price_out: float | None = None
    created: int = 0

    @property
    def free(self) -> bool:
        """Priced zero in *and* out. A missing price is not zero."""
        return self.price_in == 0.0 and self.price_out == 0.0

    @property
    def chat(self) -> bool:
        """Whether this can answer a chat turn.

        The free catalogue has no modality field, so this is the two facts that
        can be checked: a positive context window and not a known non-chat id.
        """
        if self.id in NON_CHAT:
            return False
        return bool(self.context_length and self.context_length > 0)

    @property
    def blurb(self) -> str:
        bits = ["free"]
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
        if self.owned_by:
            bits.append(self.owned_by)
        return " · ".join(bits)


#: Fetched catalogues, keyed by the URL they came from. The catalogue is
#: public, so there is one entry and no key dimension.
_CACHE: dict[str, tuple[list[Model], float]] = {}


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _host(base_url: str | None) -> str:
    try:
        return (urllib.parse.urlsplit(base_url or "").hostname or "").lower()
    except ValueError:
        return ""


def is_respan(base_url: str | None) -> bool:
    host = _host(base_url)
    return host == HOST or host.endswith("." + HOST)


#: Generic name for the same test, so the shared key handler can call
#: ``mod.active_endpoint(url)`` without knowing which gateway it holds.
active_endpoint = is_respan


def current_key() -> str | None:
    """The source key the operator last selected, if any.

    Delegates to :mod:`zimzilla.sources`, which owns ``current-source``. Two
    writers of one selection file is how a switch made here and a switch made
    with ``/zim-unorouter`` end up disagreeing about what is live.
    """
    from . import sources as sources_mod
    return sources_mod.current_key()


def set_current(key: str) -> None:
    """Record the selection. Never fatal — a read-only home must not break the TUI."""
    from . import sources as sources_mod
    sources_mod.set_current(key)


def looks_like_key(value: str) -> bool:
    """A cheap shape check before writing a key. The gateway is the authority."""
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
        body = line[len("export "):]
        name, _, value = body.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    return out


def _key_from_claude_source() -> tuple[str, str, str] | None:
    """A key already in ``~/claude-source/<name>`` aimed at Respan."""
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_respan(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = (exports.get("RESPAN_API_KEY")
                 or exports.get("ANTHROPIC_AUTH_TOKEN")
                 or exports.get("ANTHROPIC_API_KEY"))
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The key, its model preference, and where it came from.

    Order, first hit wins:

      1. ``RESPAN_API_KEY`` in the environment — the name Respan's own docs
         use, so a shell that already has one needs no setup.
      2. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` when the base URL points
         at Respan — the sourced-profile case.
      3. ZimZilla's own profile, written by ``/respan-api-setup``.
      4. A ``~/claude-source`` profile aimed at the gateway.
    """
    env_token = os.environ.get("RESPAN_API_KEY", "").strip()
    if env_token:
        return env_token, os.environ.get("ANTHROPIC_MODEL", ""), "environment"
    if is_respan(os.environ.get("ANTHROPIC_BASE_URL")):
        token = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                 or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if token:
            return token, os.environ.get("ANTHROPIC_MODEL", ""), "environment"

    exports = _read_exports(PROFILE)
    token = (exports.get("RESPAN_API_KEY")
             or exports.get("ANTHROPIC_AUTH_TOKEN")
             or exports.get("ANTHROPIC_API_KEY") or "").strip()
    if token:
        return token, exports.get("ANTHROPIC_MODEL", ""), str(PROFILE)
    return _key_from_claude_source()


def save_key(token: str, model: str = "") -> Path:
    """Write the key to ZimZilla's profile, mode 600, source format."""
    token = (token or "").strip()
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        "# ZimZilla — Respan profile (written by /respan-api-setup)\n"
        "# Treat this file as a credential. Never commit it.\n"
        f'export RESPAN_API_KEY="{token}"\n'
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
# the catalogue
# ---------------------------------------------------------------------------

def _parse_models(payload: dict) -> list[Model]:
    """Every usable entry, deduplicated to the bare gateway id.

    The public catalogue lists both bare ids and provider-prefixed aliases
    (``glm-4.7-flash`` and ``zai/glm-4.7-flash``), plus OpenRouter spellings
    with ``:free``. The gateway takes the bare id, so aliases are folded onto
    it and the duplicate is dropped rather than offered twice.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    seen: set[str] = set()
    out: list[Model] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid or "/" in mid or mid in seen:
            continue
        pricing = entry.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        model = Model(
            id=mid,
            owned_by=str(entry.get("owned_by") or "")[:60],
            context_length=_as_int(entry.get("context_length")),
            price_in=_as_float(pricing.get("input_cost")),
            price_out=_as_float(pricing.get("output_cost")),
            created=_as_int(entry.get("created")) or 0,
        )
        seen.add(mid)
        out.append(model)
    return out


def fetch_models(*, timeout: float = TIMEOUT,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch the public catalogue. Returns ``(models, error)``.

    No credential is sent and none is needed; this is the same public list the
    marketing site's model page is built from. A network failure is returned,
    never raised, because the caller is a TUI command.
    """
    if use_cache:
        hit = _CACHE.get("catalog")
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""
    req = urllib.request.Request(CATALOG_URL, headers={
        "accept": "application/json", "User-Agent": "zimzilla"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"
    models = _parse_models(payload)
    _CACHE["catalog"] = (models, time.monotonic())
    return models, ""


def _rank(model: Model) -> tuple[int, int]:
    """Preference order within the free set: known-good chat first."""
    try:
        index = FREE_FALLBACK.index(model.id)
    except ValueError:
        index = len(FREE_FALLBACK)
    return (index, -(model.context_length or 0))


def free_models(*, timeout: float = TIMEOUT) -> tuple[list[Model] | None, str]:
    """The free, chat-capable models, best first. This is the whole surface."""
    models, err = fetch_models(timeout=timeout)
    if models is None:
        return None, err
    free = [m for m in models if m.free and m.chat]
    free.sort(key=_rank)
    return free, ""


def usable_free_models(*, timeout: float = TIMEOUT) -> tuple[list[Model] | None, str]:
    """Alias used by the shared ``_models_for_kind`` table."""
    return free_models(timeout=timeout)


def model_ids(*, timeout: float = TIMEOUT) -> tuple[list[str] | None, str]:
    """Free chat model ids, in preference order."""
    models, err = free_models(timeout=timeout)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def catalog_age() -> float | None:
    hit = _CACHE.get("catalog")
    return time.monotonic() - hit[1] if hit else None


def cached_models() -> list[Model] | None:
    """The last-fetched catalogue, or ``None`` when cold or expired."""
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
    """Free chat ids from the warm cache, or ``None`` when cold."""
    models = cached_free_models()
    return [m.id for m in models] if models is not None else None


def clear_cache() -> None:
    _CACHE.clear()


def model_for(model: str, models: list[Model] | None) -> Model | None:
    for entry in models or []:
        if entry.id == model:
            return entry
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(0, 0)`` for a free model, else ``None``.

    The estimate table is never consulted for this source: a paid id is not
    offered by ``/model`` and must not be quietly billed at a made-up rate.
    """
    found = model_for(model, models)
    if found is None or not found.free:
        return None
    return (0.0, 0.0)


def default_free_model(*, timeout: float = 8.0) -> str | None:
    """The free model to land on. Never returns a paid id."""
    cached = cached_free_models()
    if cached:
        return cached[0].id
    models, _ = free_models(timeout=timeout)
    if models:
        return models[0].id
    # The catalogue is unreachable. The declared fallback is a real free id, so
    # a switch on an offline box still names something this gateway serves.
    return FREE_FALLBACK[0]


def apply_to(cfg) -> tuple[str, str] | None:
    """Point *cfg* at Respan. Returns ``(origin, model)`` or ``None``.

    The model is always a free one: the profile's saved name is used only if it
    is still in the free set, otherwise the first free model wins. That is what
    keeps a manual edit from turning `/zim-respan` into a paid route.
    """
    creds = load_credentials()
    if creds is None:
        return None
    token, saved, origin = creds
    cfg.base_url = DEFAULT_BASE_URL
    cfg.auth_token = token
    cfg.api_key = ""
    free = cached_ids() or list(FREE_FALLBACK)
    if saved and saved in free:
        cfg.model = saved
    else:
        cfg.model = default_free_model() or DEFAULT_MODEL
    return origin, cfg.model


def applies_to_default_endpoint(base_url: str | None) -> bool:
    """Whether a remembered Respan choice should override *base_url*.

    False for a deliberate non-local endpoint; true for the local proxies and
    the built-in default, the same rule the other hosted gateways use.
    """
    url = (base_url or "").strip()
    if is_respan(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------------------
# errors and the key probe
# ---------------------------------------------------------------------------

def explain_error(status: int, body: str) -> str:
    """A sentence for a failure, from the gateway's own error shape."""
    text = (body or "").strip()
    detail = ""
    code = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
            code = str(err.get("code") or "")
        if not detail:
            detail = str(payload.get("message") or payload.get("detail") or "")
    lowered = (detail or text).lower()
    if "activation_required" in code or "needs respan credits" in lowered:
        return ("the key is valid but this Respan account is not activated — "
                "the gateway refuses even $0 models until credits (or a BYOK "
                "provider key) are added at "
                "https://platform.respan.ai/platform/settings/workspace/billing")
    if "not available on respan credits" in lowered:
        return ("this model is not served on Respan credits; it needs your own "
                "provider key on the Team plan or above")
    if status in (401, 403):
        return "key rejected — check it with /respan-api-setup"
    if status == 402:
        return "out of credits — top up at https://platform.respan.ai, or use a free model"
    if status == 429:
        return "rate limited (429) — retry shortly"
    if status >= 500:
        return f"the gateway is unavailable ({status}) — retry shortly"
    return f"HTTP {status}" + (f": {detail[:200]}" if detail else "")


def verify_key(token: str, model: str = "", *,
               timeout: float = TIMEOUT) -> tuple[bool, str]:
    """Prove *token* with one tiny Messages call.

    ``ok`` is False only for a rejected credential or an unreachable gateway. A
    model that the account cannot use, or a rate limit, says nothing about the
    key, so those come back ``ok=True`` with the reason in ``detail``. That is
    what lets a fresh, unactivated Respan account store its key and be told
    exactly what to do next instead of being told the key is wrong.
    """
    token = (token or "").strip()
    if not token:
        return False, "no key"
    chosen = model or DEFAULT_MODEL
    payload = {
        "model": chosen,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 8,
    }
    req = urllib.request.Request(
        DEFAULT_BASE_URL.rstrip("/") + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": token,
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "zimzilla",
        },
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
        detail = explain_error(e.code, body)
        if e.code in (401, 403):
            # A valid-but-unactivated account is not a bad key. The detail
            # says which of the two it is; the caller prints it.
            try:
                code = (json.loads(body).get("error") or {}).get("code") or ""
            except (ValueError, AttributeError):
                code = ""
            if code == "activation_required" or "needs respan credits" in body.lower():
                return True, detail
            return False, f"HTTP {e.code} — {detail}"
        return True, detail
    except (urllib.error.URLError, OSError) as e:
        return False, f"could not reach {HOST}: {e}"
