"""IteraCompute — a free-model upstream, reached directly by default.

IteraCompute (``https://api.iteracompute.com``) is **OpenAI-compatible only**.
Measured on this box, 2026-10-19: ``POST /v1/messages`` answers ``404
edge_route_not_allowed``; several guessed Anthropic-compat paths and
subdomains do the same or do not resolve. So it cannot be a hosted gateway the
way TokenHarbour or BeatAPI is — nothing here speaks the Anthropic Messages
protocol back.

What makes it a source is the same arrangement Logfare uses: a local LiteLLM
proxy on :4002 translates ``/v1/messages`` into ``/v1/chat/
completions`` at ``https://api.iteracompute.com/v1``. (Token Juice used to be
the third one; it is direct-only now — no proxy at all.) Since the harness grew
a
native Chat Completions transport (``zimzilla.openai_compat``), the proxy is no
longer the route a switch takes: ``/zim-iteracompute`` reads this source's proxy
config for the upstream URL and the model ids and goes straight there, with the
proxy left as ``/zim-iteracompute proxy``. This module is still the catalog and
the credential, and the proxy still works if the operator asks for it.

The gateway's own docs state the contract:

* ``https://api.iteracompute.com/v1`` is the API root; Chat Completions and
  Responses authenticate with the key in the ``Authorization`` header.
* The Models endpoint is public. Use its ``id`` values exactly as returned —
  no ``iteracompute/`` prefix — which matters here because the ids are
  ``provider/model`` and contain a slash.
* Free / Starter / Pro provide 30M / 100M / 300M tokens per UTC calendar
  month, 160K / 300K / 600K TPM and 128K / 128K / 256K context. A net
  completed recharge of $10 or $50 unlocks Starter or Pro.
* Free accounts may queue when GPU capacity is busy; exhausted allowances,
  full queues and queue timeouts return ``429``. Honor ``Retry-After``.
* Qwen supports Chat Completions, streaming and tool calling. The guide does
  not establish Qwen Responses API support, so the proxy is configured for
  Chat Completions only — explicitly, because LiteLLM's default for an
  ``openai/`` deployment is to route ``/v1/messages`` through the Responses
  API. Measured with litellm 1.105.1: without the opt-out the upstream received
  ``POST /v1/responses``; with it, ``POST /v1/chat/completions``, for both
  streaming and non-streaming requests, and tool calls round-trip into
  Anthropic ``tool_use`` blocks.

**Free is the catalog's own ``is_free`` flag.** The catalog publishes a
``pricing`` object with string numbers; a free entry prices input, cached
input and output at ``"0"``. The flag and the zero price agree, and both are
read — a missing price is *not* treated as free, the same trap the other
gateway modules document.

**The catalog is also the only place the model's limits are published**, and
two of them are load-bearing here:

* ``max_output_tokens`` is 8192 on the free model. The harness's configured
  ``max_tokens`` is larger, and asking for more than the model allows is a
  request the upstream rejects. ``sources.max_output_for`` reads this value
  and the agent clamps to it, so the turn is sized for the model rather than
  for the harness default.
* ``context_window`` is 262144 on the free model, but the docs put the
  **free account tier** at 128K context. The account's tier is not visible
  from an API key, so this module reports the conservative figure — the
  smaller of the catalog value and :data:`FREE_CONTEXT_CAP` — because
  compacting a little early is survivable and overflowing is not.

Image entries in the catalog (``gpt-image-*``, ``gemini-*-argon``) publish no
``context_window`` and no ``max_output_tokens``. They cannot answer a chat
turn, so :func:`usable_free_models` filters on the presence of a window
rather than on the name.

Nothing here echoes a key.
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
#: Where the operator keeps their per-provider shell profiles — the same
#: ``~/claude-source/<name>`` files a plain ``source`` loads for Claude Code.
CLAUDE_SOURCE = Path(os.environ.get("ZIMZILLA_CLAUDE_SOURCE")
                     or (Path.home() / "claude-source"))

#: The port the local LiteLLM translation proxy listens on. 4001 is Logfare;
#: this one must not collide with it. (Token Juice has no port: it is reached
#: directly, with no proxy.)
PROXY_PORT = 4002

#: What the session points at. This is the **local proxy**, not the upstream:
#: IteraCompute has no Messages endpoint, so a turn sent straight to
#: ``api.iteracompute.com`` would 404. The launcher and ``/zim-iteracompute``
#: both start the proxy before the session is aimed here.
DEFAULT_BASE_URL = f"http://localhost:{PROXY_PORT}"

#: The upstream API root, used by the proxy config. Kept here so the host is
#: spelled once; ``packaging/iteracompute/litellm-config.yaml`` carries it for
#: LiteLLM, and a test asserts the two agree.
API_BASE_URL = "https://api.iteracompute.com/v1"

#: The public model catalog. No key required, which is why the free list works
#: before ``/iteracompute-api-setup`` has run.
MODELS_URL = f"{API_BASE_URL}/models"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = ZIMZILLA_HOME / "iteracompute" / "source"

#: A fetched catalog older than this is refetched rather than trusted. The
#: free tier turns over, so a stale list is exactly the wrong list this picker
#: exists to avoid.
CACHE_TTL = 300.0

#: The free account tier's context, per the gateway's own docs (128K). The
#: catalog advertises the model's larger capability, but an API key does not
#: say which tier the account is on, so the conservative figure is reported.
FREE_CONTEXT_CAP = 131_072


@dataclass(frozen=True)
class Model:
    """One entry from IteraCompute's public model catalog."""

    id: str
    label: str = ""
    #: The catalog's ``context_window``, unclamped. :attr:`context_length` is
    #: the figure to act on — see the module docstring.
    catalog_context: int | None = None
    max_output: int | None = None
    price_in: float = 0.0
    price_out: float = 0.0
    #: The catalog's own ``is_free`` flag.
    is_free: bool = False
    #: Whether a price was actually **read**. A missing price must not read as
    #: 0/0 and mark every unpriced entry free.
    priced: bool = False
    reasoning: bool = False
    reasoning_effort: str = ""
    owned_by: str = ""

    @property
    def free(self) -> bool:
        """Whether this model costs nothing.

        The catalog's ``is_free`` is authoritative — it is what the gateway
        documents. A read 0/0 price corroborates it; a *missing* price does
        not count, which is what ``priced`` records.
        """
        if self.is_free:
            return True
        return self.priced and self.price_in == 0.0 and self.price_out == 0.0

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn.

        The catalog's image entries publish no context window; a chat model
        always does. Filtering on the name would rot as new image models are
        added, so the window is the test.
        """
        return bool(self.catalog_context)

    @property
    def context_length(self) -> int | None:
        """The window to plan against, in tokens.

        For a free model this is the smaller of the catalog value and
        :data:`FREE_CONTEXT_CAP`, because the docs cap the free *account tier*
        at 128K regardless of what the model supports. A paid model keeps the
        catalog figure: its tier is Starter/Pro, whose limits are the catalog's
        own.
        """
        if not self.catalog_context:
            return None
        if self.free:
            return min(self.catalog_context, FREE_CONTEXT_CAP)
        return self.catalog_context

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits = []
        if self.free:
            bits.append("free")
        elif self.priced and (self.price_in or self.price_out):
            bits.append(f"${self.price_in:g}/${self.price_out:g}")
        window = self.context_length
        if window:
            bits.append(f"{window // 1000}k ctx")
        if self.reasoning:
            bits.append("reasoning")
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------

def is_iteracompute(base_url: str | None) -> bool:
    """Whether *base_url* points at this source's local proxy.

    Matched by port on loopback only. ``api.iteracompute.com`` deliberately does
    **not** match: this predicate answers "is the session on the proxy route",
    and the upstream is not that route. The upstream is reachable — over Chat
    Completions, with ``/openai-endpoint`` or ``--protocol openai`` — but it is
    a direct endpoint, not an IteraCompute *source* with a proxy to manage.
    """
    url = (base_url or "").strip()
    return (f"localhost:{PROXY_PORT}" in url
            or f"127.0.0.1:{PROXY_PORT}" in url)


#: Generic name for the same test, so code that handles several sources
#: uniformly can call ``mod.active_endpoint(url)``.
active_endpoint = is_iteracompute


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


def _key_from_claude_source() -> tuple[str, str, str] | None:
    """An IteraCompute key already sitting in ``~/claude-source/<name>``.

    A profile there is the ``source ~/claude-source/<name>`` idiom. Only one
    that points at this proxy's port counts — a profile aimed at the upstream
    directly is not a working setup, so adopting its key would be a lie.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_iteracompute(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The IteraCompute key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at this proxy — i.e. the operator
         sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/iteracompute-api-setup``.
      3. Any profile under ``~/claude-source`` aimed at this proxy.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged
    or returned to the transcript — only ``origin`` (a path, or "environment").
    """
    if is_iteracompute(os.environ.get("ANTHROPIC_BASE_URL")):
        token = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                 or os.environ.get("ANTHROPIC_API_KEY") or "")
        if token:
            return token, os.environ.get("ANTHROPIC_MODEL", ""), "environment"

    exports = _read_exports(PROFILE)
    token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
    if token:
        return token, exports.get("ANTHROPIC_MODEL", ""), str(PROFILE)

    found = _key_from_claude_source()
    if found:
        return found
    return None


def save_key(token: str, model: str = "") -> Path:
    """Write the key to ZimZilla's own IteraCompute profile, mode 600.

    Same ``source`` format as every other profile, so it can be sourced by a
    shell as well as read by the harness. ``ANTHROPIC_BASE_URL`` names the
    local proxy, not the upstream — see :data:`DEFAULT_BASE_URL`.
    """
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        f'export ANTHROPIC_BASE_URL="{DEFAULT_BASE_URL}"\n'
        f'export ANTHROPIC_AUTH_TOKEN="{token}"\n'
        f'export ANTHROPIC_MODEL="{model}"\n'
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


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

#: Fetched catalogs, keyed by the token they were fetched with. The catalog is
#: public and identical for every caller, so the keyless fetch is cached under
#: "" and a keyed one under its token.
_CACHE: dict[str, tuple[list[Model], float]] = {}


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _entries(payload: dict) -> list[dict]:
    """The catalog rows from ``{"object": "list", "data": [...]}``.

    A malformed payload yields an empty catalog rather than an exception: this
    runs inside a TUI command.
    """
    data = payload.get("data")
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    return []


def _parse_models(payload: dict) -> list[Model]:
    """Every entry in the catalog payload, in catalog order."""
    out: list[Model] = []
    for entry in _entries(payload):
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        pricing = entry.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        price_in = _as_float(pricing.get("input"))
        price_out = _as_float(pricing.get("output"))
        priced = price_in is not None and price_out is not None
        if not priced:
            price_in = price_out = 0.0

        reasoning = entry.get("reasoning")
        reasoning = reasoning if isinstance(reasoning, dict) else {}

        out.append(Model(
            id=mid,
            label=str(entry.get("owned_by") or ""),
            catalog_context=_as_int(entry.get("context_window")),
            max_output=_as_int(entry.get("max_output_tokens")),
            price_in=price_in or 0.0,
            price_out=price_out or 0.0,
            is_free=bool(entry.get("is_free")),
            priced=priced,
            reasoning=bool(reasoning.get("supported")),
            reasoning_effort=str(reasoning.get("default_effort") or ""),
            owned_by=str(entry.get("owned_by") or ""),
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch IteraCompute's model catalog. Returns ``(models, error)``.

    **No key required** — the catalog is public, so ``token`` defaults to empty
    and the free list is available before ``/iteracompute-api-setup`` has run.
    A key is sent when one is supplied, so the listing matches this account.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach IteraCompute" rather than take the session
    down. ``models`` is ``None`` on failure, ``[]`` for a genuinely empty
    catalog — the caller can tell the two apart.
    """
    token = (token or "").strip()
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    headers = {"accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(MODELS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "key rejected (401/403) — check it with /iteracompute-api-setup"
        return None, f"HTTP {e.code} from api.iteracompute.com"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach api.iteracompute.com: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the models IteraCompute is currently giving away."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The free models this harness can drive — chat-capable only.

    Free **and** carrying a context window. The catalog's image entries are
    free of a chat limit because they are not chat models at all; they publish
    no window, which is the filter. No capability filter beyond that: the
    catalog does not publish tool support per model, and hiding a model on a
    field it never sent would be a guess.
    """
    models, err = free_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.chat], ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every chat model id, catalog order preserved — what ``/model`` offers.

    All of them, not only the free ones: an operator who names a paid model
    deliberately should not have it refused as "not served by iteracompute"
    against a list filtered to the free tier. Image entries are excluded
    because they cannot answer a turn.
    """
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m.id for m in models if m.chat], ""


def catalog_age() -> float | None:
    """Seconds since the newest cache entry was filled, or ``None`` if none was."""
    if not _CACHE:
        return None
    newest = max(entry[1] for entry in _CACHE.values())
    return time.monotonic() - newest


def cached_models() -> list[Model] | None:
    """The last-fetched catalog as :class:`Model` objects, or ``None``.

    ``/model`` runs on the UI thread and must not block on a fetch, so it reads
    whatever the last fetch left behind.

    **The ``CACHE_TTL`` check is load-bearing**, exactly as in the other
    gateway modules: an expired entry reads as ``None``, which callers already
    handle as "no catalog", rather than a stale list being called live.
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
    base = model.strip()
    for m in models:
        if m.id == base:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok for *model*, when the catalog can answer.

    A free model returns ``(0.0, 0.0)``, which is exact and makes the note read
    ``free``. An id the catalog does not know returns ``None``, so the caller
    falls back to this harness's labelled estimate.
    """
    found = model_for(model, models)
    if found is None:
        return None
    if found.free:
        return (0.0, 0.0)
    if not found.priced:
        return None
    return (found.price_in, found.price_out)


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    Deliberately loose — the gateway decides what is valid, not us — but strict
    enough to catch a pasted shell line or a bare word. No prefix is required;
    only length, no whitespace, and the character set a token is drawn from.
    """
    value = value.strip()
    if len(value) < 12 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))
