"""InferX Endpoints — a tenant-scoped, key-gated OpenAI-compatible endpoint.

InferX (``model.inferx.net``) publishes serverless inference endpoints behind an
OpenAI-compatible API. The address a key is issued for — the tenant path, here
``https://model.inferx.net/endpoints/v1`` — is a *direct* source in the same
sense Token Juice, NVIDIA and wandb are: the upstream speaks Chat Completions
itself, so there is no LiteLLM proxy, no port to start, no health check. The
route is declared in :mod:`zimzilla.sources` (:data:`~zimzilla.sources.INFERX_DIRECT`)
and the profile below is the whole install.

Measured on this box, 2026-11-05, against the live endpoint:

* ``POST /chat/completions`` answers a plain completion, and the reply carries
  ``reasoning_content`` — the spelling ``openai_compat._delta_reasoning`` already
  reads, so ``/think`` works here with no transport change.
* ``GET /models`` is **key-gated**, the opposite of NVIDIA's public catalog: it
  answers 200 with the operator's key and ``401 {"detail": "invalid auth token"}``
  without one (and with a wrong one). That is load-bearing for the switch —
  ``_switch_direct`` probes the Models route with the profile's token, so a bad
  key is refused at switch time rather than met as a 401 on the first turn.
* The catalog lists **15 chat ids** — ``deepseek-v4.1-flash-1017``, ``glm-5.3``,
  ``kimi-k3``, ``qwen3.8-max``, ``gpt-oss-20b``, ``Agents-A1`` … — every one a
  chat model; there is no embedding, reranker or guard family in between.
* Each entry carries only ``id``, ``object``, ``created`` and ``owned_by``: no
  price, no context window, no capability list. The public catalog page
  publishes a **context length** per endpoint (the table below) but leaves its
  Input / Output / Cached per-1M columns empty, so this module claims no
  per-token price in either direction.
* Calls are metered against the **tenant's allowance**, and it is small: after a
  first successful turn, chat calls answered ``429`` with
  ``service failure: tenant <id> quota exceeded`` while the key-gated Models
  route kept answering 200. So ``free`` here means the tenant's included quota,
  exactly as NVIDIA's means its developer allowance — see :attr:`Model.free` —
  and the sentence to expect when it runs out is above, not a bill.

**The catalog is thin, and that shapes this module.** Two consequences, each
handled explicitly rather than guessed at:

1. **There is no per-model price.** The public catalog's price columns are
   present and empty, and the endpoint has no billing field anywhere in its
   responses. ``free`` is therefore an *account* property — the tenant's quota —
   not a per-model one, and :func:`price_for` returns ``(0.0, 0.0)`` so
   ``/model`` and ``/cost`` read ``free`` instead of billing a model at
   ``DEFAULT_PRICE``.

2. **There is no context window.** :data:`CONTEXT_LENGTHS` carries the figure
   from InferX's own public catalog page for every id it listed, and
   :data:`DEFAULT_CONTEXT` (256K, the modal measured value) answers for anything
   newer. The direction of that guess is deliberate, the same as NVIDIA's: a
   window assumed too *small* makes the harness compact early, which is
   survivable; one assumed too *large* overflows mid-turn, which is not.

The id goes on the wire unchanged — there is no alias half to translate, unlike
Token Juice's proxy names — so a switch lands on the live catalog's own spelling
and ``/model`` offers exactly the ids this endpoint serves.

Nothing here echoes a key. The profile is written mode 600 and read back the
same way every other source's is.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))

#: The tenant's API root. No proxy sits in front of it: this is the address the
#: session itself is pointed at, which is why it is a ``DirectRoute`` base URL
#: and not a ``localhost`` port. It is also the string the operator's own curl
#: example uses, which is the form the key is issued against.
API_BASE_URL = "https://model.inferx.net/endpoints/v1"

#: The model catalog. A key is required — see the module docstring — so a fetch
#: with no token answers 401 rather than a list.
MODELS_URL = f"{API_BASE_URL}/models"

#: The profile ``/inferx-api-setup`` writes and every lookup reads.
PROFILE = ZIMZILLA_HOME / "inferx" / "source"

#: InferX keys are issued as ``ix_<long token>``. A hint for the confirmation
#: line, never a gate: the upstream is the authority on its own keys, and
#: refusing a real key of an unexpected shape would be worse than storing it and
#: letting one 401 say so.
KEY_PREFIX = "ix_"

#: The model a switch selects when the profile names none.
#:
#: The id the operator's own curl example used, measured answering a chat turn
#: in a couple of seconds, and the one whose window the public catalog gives as
#: 1M — the figure ``Config.context_window`` already assumes.
DEFAULT_MODEL = "deepseek-v4.1-flash-1017"

#: A fetched catalog older than this is refetched rather than trusted. The
#: lineups here move with the endpoints InferX publishes, so a stale list is
#: exactly the wrong list this picker exists to avoid.
CACHE_TTL = 300.0

#: The window to plan against when the table does not know the id.
#:
#: 256K, the modal measured value across the 15 published endpoints, not the
#: harness's 1M fallback: the catalog sends no window at all, and guessing high
#: is the unrecoverable direction. See the module docstring.
DEFAULT_CONTEXT = 262_144

#: Real windows, read from InferX's public catalog page (``/catalog/endpoints``,
#: its "Context Length" column) on 2026-11-05, for every id it listed. Only ids
#: that were actually looked up belong here — a number invented for this table
#: would be indistinguishable from a measured one at the point it matters.
CONTEXT_LENGTHS: dict[str, int] = {
    "deepseek-v4-flash-0731": 262_144,
    "deepseek-v4.1-flash": 1_048_576,
    "deepseek-v4.1-flash-1017": 1_048_576,
    "deepseek-v4.1-flash-1017-basic": 262_144,
    "glm-5.3": 1_048_576,
    "glm-5.3-flash": 262_144,
    "kimi-k3": 1_048_576,
    "qwen3.8-max": 1_048_576,
    "Agents-A1": 262_000,
    "Devstral-2-123B-Instruct-2512-int4-AutoRound": 128_000,
    "gemma-4-31B-it-fp8": 262_144,
    "gpt-oss-20b": 20_000,
    "Qwen3-Coder-Next-FP8": 256_144,
    "Qwen3.6-35B-A3B-FP8": 262_000,
    "Qwen3.8-27B-FP8": 262_000,
}

#: Families that cannot answer a chat turn, matched as substrings of the
#: lowercased id. Every id in today's catalog is a chat model, so this list
#: filters nothing today; it exists so that the day an embedding or vision
#: endpoint appears beside them the picker does not offer it as a chat model.
#: Deliberately narrow, and it errs toward **keeping** an unknown id: hiding one
#: the operator could have used is worse than showing one that 400s, because the
#: error is immediate and self-explanatory.
_NON_CHAT_MARKERS: tuple[str, ...] = (
    "embed", "bge", "rerank", "ranking", "reward",
    "clip", "vila", "kosmos", "deplot", "diffusion", "cosmos",
    "guard", "safety", "jailbreak", "detector", "pii",
    "parse", "whisper", "tts", "audio", "image",
)


@dataclass(frozen=True)
class Model:
    """One entry from InferX's model catalog.

    Only the fields the catalog actually sends are stored. The window is
    derived from :data:`CONTEXT_LENGTHS` or the default, and price is
    deliberately absent: the catalog publishes none, and a default in this
    object would be indistinguishable from a fact at the point of use.
    """

    id: str
    owned_by: str = ""
    created: int | None = None

    @property
    def free(self) -> bool:
        """Whether this model costs money to call.

        Always true in the sense this harness means: calls are covered by the
        key's tenant quota, not billed per token, and the catalog publishes no
        price to contradict that. When the quota is spent the endpoint answers
        429 ``tenant … quota exceeded`` — a rate condition to report, not an
        invoice. This is what makes ``/model`` and ``/cost`` read ``free``
        instead of billing a model at ``DEFAULT_PRICE``.
        """
        return True

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn, by family."""
        lowered = self.id.lower()
        return not any(marker in lowered for marker in _NON_CHAT_MARKERS)

    @property
    def context_length(self) -> int:
        """The window to plan against, in tokens.

        The table's figure when the public catalog gave one, else
        :data:`DEFAULT_CONTEXT`. Never ``None``: unlike the catalogs that
        publish a window, silence here is not "unknown, use the harness
        default" — the harness default is 1M, and a 1M assumption against a
        256K model is an overflow the operator discovers mid-task.
        """
        return CONTEXT_LENGTHS.get(self.id, DEFAULT_CONTEXT)

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        return f"free (tenant quota) · {self.context_length // 1000}k ctx"


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------


def is_inferx(base_url: str | None) -> bool:
    """Whether *base_url* points at an InferX endpoint.

    Matched on host, because this route has no port to match on. The tenant
    path differs per key — ``/endpoints/v1`` here, and a tenant-scoped path for
    a deployment of one's own — so only the host is matched.
    """
    return "model.inferx.net" in (base_url or "").strip().lower()


#: Generic name for the same test, so code that handles several sources
#: uniformly can call ``mod.active_endpoint(url)``.
active_endpoint = is_inferx


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


def load_credentials() -> tuple[str, str, str] | None:
    """The InferX key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` in the environment,
         when ``ANTHROPIC_BASE_URL`` points at an InferX host — i.e. the
         operator sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/inferx-api-setup``.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged
    or returned to the transcript — only ``origin`` (a path, or "environment").
    """
    if is_inferx(os.environ.get("ANTHROPIC_BASE_URL")):
        token = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                 or os.environ.get("ANTHROPIC_API_KEY") or "")
        if token:
            return token, os.environ.get("ANTHROPIC_MODEL", ""), "environment"

    exports = _read_exports(PROFILE)
    token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
    if token:
        return token, exports.get("ANTHROPIC_MODEL", ""), str(PROFILE)

    return None


def save_key(token: str, model: str = "") -> Path:
    """Write the key to ZimZilla's own InferX profile, mode 600.

    Same ``source`` format as every other profile, so it can be sourced by a
    shell as well as read by the harness. ``ANTHROPIC_BASE_URL`` names the
    tenant endpoint itself — there is no proxy to name — and
    ``ANTHROPIC_MODEL`` carries the model the operator was on, so setting a key
    does not silently reset it.
    """
    token = (token or "").strip()
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        "# ZimZilla — InferX profile (written by /inferx-api-setup)\n"
        "# Treat this file as a credential. Never commit it.\n"
        f'export ANTHROPIC_BASE_URL="{API_BASE_URL}"\n'
        f'export ANTHROPIC_AUTH_TOKEN="{token}"\n'
        f'export ANTHROPIC_MODEL="{model or DEFAULT_MODEL}"\n'
        'export ANTHROPIC_API_KEY=""\n'
    )
    # Create with 600 from the outset, then rewrite in place: never a window in
    # which the key sits in a world-readable file.
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


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    Deliberately loose — the upstream decides what is valid, not us — but
    strict enough to catch a pasted shell line or a bare word. The ``ix_``
    prefix is *not* required: a real key of an unexpected shape is worth more
    stored than rejected, and a wrong one only costs a single 401.
    """
    value = (value or "").strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

#: Fetched catalogs, keyed by the token they were fetched with. Unlike NVIDIA's
#: the catalog is **not** public — a keyless fetch answers 401 — so the keyless
#: entry is the empty catalog's error rather than a shared list.
_CACHE: dict[str, tuple[list[Model], float]] = {}


def _parse_models(payload: dict) -> list[Model]:
    """Every row in the catalog payload, in catalog order.

    A malformed payload yields an empty catalog rather than an exception: this
    runs inside a TUI command.
    """
    out: list[Model] = []
    data = payload.get("data")
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        created = entry.get("created")
        out.append(Model(
            id=mid,
            owned_by=str(entry.get("owned_by") or ""),
            created=created if isinstance(created, int) else None,
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch InferX's model catalog. Returns ``(models, error)``.

    A key is required — the route answers 401 without one — so the error for a
    missing or rejected key names the command that fixes it rather than
    reporting a bare status. ``models`` is ``None`` on failure and ``[]`` for a
    genuinely empty catalog; the caller can tell the two apart.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach the endpoint" rather than take the session
    down.
    """
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
            # The "HTTP 401" prefix is load-bearing: `_switch_direct` keys its
            # refusal on it, so this wording is what makes a bad key stop the
            # switch instead of landing on a route that 401s on the first turn.
            return None, (f"HTTP {e.code} — key rejected; check it with "
                          "/inferx-api-setup")
        return None, f"HTTP {e.code} from model.inferx.net"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach model.inferx.net: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def chat_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the entries that can answer a chat turn."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.chat], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The models this harness can drive, which are also the free ones.

    Kept as its own name because every other source's ``/models`` path calls it,
    and the two filters coincide here by construction: a chat-capable entry is
    callable on the tenant's quota, and the quota is what covers it. See
    :attr:`Model.free`.
    """
    return chat_models(token, **kw)


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every chat model id, catalog order preserved — what ``/model`` offers."""
    models, err = chat_models(token, **kw)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def catalog_age() -> float | None:
    """Seconds since the newest cache entry was filled, or ``None`` if none was."""
    if not _CACHE:
        return None
    newest = max(entry[1] for entry in _CACHE.values())
    return time.monotonic() - newest


def cached_models() -> list[Model] | None:
    """The last-fetched catalog as :class:`Model` objects, or ``None``.

    ``/model`` runs on the UI thread and must not block on a fetch, so it reads
    whatever the last fetch left behind — and an entry past :data:`CACHE_TTL`
    reads as ``None``, which callers already handle as "no catalog", rather
    than a stale list being called live.
    """
    newest: tuple[list[Model], float] | None = None
    for entry in _CACHE.values():
        if newest is None or entry[1] > newest[1]:
            newest = entry
    if newest is None or (time.monotonic() - newest[1]) >= CACHE_TTL:
        return None
    return list(newest[0])


def cached_ids() -> list[str] | None:
    """The last-fetched chat model ids, without touching the network."""
    models = cached_models()
    return [m.id for m in models if m.chat] if models is not None else None


def clear_cache() -> None:
    """Drop every cached catalog. Called when the key changes."""
    _CACHE.clear()


def model_for(model: str, models: list[Model] | None) -> Model | None:
    """The catalog's entry for *model*, if the catalog knows it."""
    if not models:
        return None
    base = (model or "").strip()
    for m in models:
        if m.id == base:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok for *model*, when the catalog knows it.

    ``(0.0, 0.0)`` for any catalog entry — the tenant's quota covers it and the
    catalog publishes no per-model price — which is what makes ``/model`` and
    ``/cost`` read ``free``. ``None`` for an id the catalog does not know, so
    the caller falls back to this harness's labelled estimate: the honest answer
    for a name nothing here can price.
    """
    found = model_for(model, models)
    if found is None:
        return None
    return (0.0, 0.0)


def default_free_model() -> str:
    """The model to fall back to when the chosen one is gone from the catalog."""
    return DEFAULT_MODEL


# ---------------------------------------------------------------------------
# error shapes
# ---------------------------------------------------------------------------


def explain_error(status: int, body: str) -> str:
    """A sentence for an InferX failure, from the shape it came back in.

    Two shapes were measured, and they are not interchangeable:

    * ``401`` with ``invalid auth token`` — the key is wrong, missing, or not
      the one this endpoint was issued for. The fix is
      ``/inferx-api-setup``, not a retry.
    * ``429`` with ``service failure: tenant <id> quota exceeded`` — the key is
      **fine** and the tenant's allowance is spent. The key-gated Models route
      keeps answering 200 in this state, which is what says the credential is
      not the problem; a plain rate limit (no quota wording) is reported as
      exactly that instead, because they call for different waiting.
    """
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
        return "key rejected — check it with /inferx-api-setup"
    if status == 429 and "quota" in lowered:
        return ("this key's tenant allowance is spent (429 quota exceeded) — "
                "the key itself is fine; /inferx-models still lists the "
                "catalog")
    if status == 429:
        return "rate limited (429) — retry shortly"
    if status == 404:
        return ("this model is not served by this endpoint — pick another with "
                "/inferx-models")
    if status == 503:
        return "InferX is at capacity for this model (503) — retry shortly"
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    if text and not payload:
        return f"HTTP {status}: {text[:200]}"
    return f"HTTP {status}"
