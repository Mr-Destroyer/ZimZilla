"""Coarena — a hosted Anthropic-protocol gateway, as a ZimZilla source.

Coarena (``https://coarena.ai``, by Coasty Systems) is a computer-use model
arena that also runs a **free Model API** in front of the frontier lineup. This
module wires up the text half: one endpoint that speaks the Anthropic Messages
protocol natively, so there is no translation layer to write and none is
written.

Measured against the live host on 2026-11-12, not assumed:

* ``POST https://coarena.ai/api/v1/messages`` answers a real Anthropic-shaped
  message — ``{"type": "message", "content": [{"type": "text", ...}]}`` — with
  ``x-api-key: coa_…`` or ``Authorization: Bearer coa_…``. A tool-using turn
  round-trips: the reply came back as ``{"type": "tool_use", "name":
  "get_weather", "input": {"city": "Paris"}}`` with ``stop_reason: "tool_use"``,
  which is what makes this a route the harness's agent loop can actually drive.
* ``stream: true`` answers a proper SSE event stream (``message_start``,
  ``content_block_delta``, ``content_block_stop``, …), so long answers are not
  cut off by the non-streaming 20-second rule the docs describe.
* ``GET https://coarena.ai/api/v1/models`` answers **200 with no key at all**
  and lists the models that are currently *on* — 18 of them when this was
  written. The catalog is public, so the free list can be shown, and a model
  chosen, *before* the operator has set anything.
* ``GET https://coarena.ai/api/v1/limits`` answers **401 without a key** and
  **200 with one**, so unlike the public Models route this is a genuine
  credential probe: one GET proves a key without spending a token.
* ``POST /api/v1/messages`` with no key answers a 401 whose body names both
  accepted header forms and the page that issues keys — so a missing key is
  reported as a missing key rather than as a broken account.

**The base URL is ``https://coarena.ai/api``, not ``/api/v1``.** The Anthropic
SDK appends ``/v1/messages`` to whatever base it is handed, so
:data:`DEFAULT_BASE_URL` is the origin plus ``/api`` and composes to
``/api/v1/messages``. Passing ``https://coarena.ai/api/v1`` would compose to
``/api/v1/v1/messages`` and 404 on every turn while the catalog still worked —
the same trap ``openrouter.py``, ``unorouter.py`` and ``moonmath.py`` document.
The provider's own docs state it plainly: "Anthropic SDK Base URL
``https://coarena.ai/api``: the SDK adds ``/v1/messages`` itself."

**Free is a daily allowance, not a zero price.** The catalog publishes
``{"prompt": "0", "completion": "0"}`` for every model, and the account is
nevertheless metered in real dollars: ``/api/v1/limits`` reported
``used_usd: 0.000083`` after five small calls, against a ``usd_per_day`` of
``$5.00``. So :attr:`Model.free` means "no per-token charge to the operator",
and the allowance is the real ceiling — which is why :func:`limits` exists and
why ``/coarena-models`` prints the day's remaining budget beside the list. A
model that is *listed* is not necessarily *on*: the docs name 22 models while
``/models`` listed 18, and the four missing ones (the ``gpt-6-*`` line)
answered ``400 model_off`` — "turned off on the Coarena Model API right now".
The catalog is therefore the authority for what is callable, and it is fetched
live rather than baked in.

**The input cap is 128,000 tokens and it is hard.** The docs are explicit that
a request past it is a ``400`` until the session is shorter, and that this is
*less* than Claude Code fills before it compacts on its own. :data:`DEFAULT_CONTEXT`
carries the real number so the harness's own compaction triggers against it
rather than against a 1M default it would never reach.

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

#: The canonical host. Matching is by this substring, so the Messages root
#: (``/api``) and the OpenAI surface (``/api/v1``) count as the same source.
HOST = "coarena.ai"

#: The SDK's ``base_url`` — the origin plus ``/api``, BEFORE ``/v1``. The
#: Anthropic SDK appends ``/v1/messages``, composing to
#: ``https://coarena.ai/api/v1/messages``, which is where Coarena serves the
#: Messages protocol (measured: a real tool-using turn, not a 404). The
#: provider's docs name this exact base for the Anthropic SDK.
DEFAULT_BASE_URL = f"https://{HOST}/api"

#: The OpenAI-compatible root. Not the route this harness takes — it is here so
#: the two surfaces are spelled once, and so a future transport does not have to
#: rediscover it. ``/api/v1`` is what the docs hand the OpenAI SDK, Codex and
#: the Vercel AI SDK.
API_BASE_URL = f"https://{HOST}/api/v1"

#: The public model catalog. No key required — which is why the free list works
#: before ``/coarena-api-setup`` has run. It lists the models that are *on*,
#: which is the only list worth offering: the docs name four more that answer
#: ``400 model_off``.
CATALOG_URL = f"{API_BASE_URL}/models"

#: The keyed limits route. Answers 401 without a credential, so it is the
#: credential probe — and it is also the only place the day's remaining budget
#: is published, which is what makes "free" honest rather than nominal.
LIMITS_URL = f"{API_BASE_URL}/limits"

#: The profile ``/coarena-api-setup`` writes and every lookup reads.
PROFILE = ZIMZILLA_HOME / "coarena" / "source"

#: Coarena keys are issued as ``coa_<32 hex>``. A hint for the confirmation
#: line, never a gate: the upstream is the authority on its own keys, and
#: refusing a real key of an unexpected shape would be worse than storing it
#: and letting one 401 say so.
KEY_PREFIX = "coa_"

#: The model a switch selects when the profile names none. It is a frontier
#: Claude with tool calling and a 128K window, it is on the free allowance like
#: every other listed model, and it is the one the provider's own Claude Code
#: instructions name for ``/model sonnet``.
DEFAULT_MODEL = "claude-sonnet-5-5"

#: A fetched catalog older than this is refetched rather than trusted. The
#: lineup turns over — models are switched off and on at the provider's
#: discretion, and the docs' 22 and the catalog's 18 already disagree — so a
#: stale list is exactly the wrong list this picker exists to avoid.
CACHE_TTL = 300.0

#: The window to plan against when the catalog publishes none for an id.
#: 128K is the provider's own hard input cap, not a guess: the docs state that
#: a request past it is a 400 until the session is shorter.
DEFAULT_CONTEXT = 128_000

#: The output ceiling the provider enforces, and the default it applies when a
#: request asks for none. Both are published on the docs' limits table.
MAX_OUTPUT = 16_384
DEFAULT_MAX_OUTPUT = 8_192

#: The daily allowance a fresh account gets, in USD. Read from
#: ``/api/v1/limits`` on 2026-11-12 (``usd_per_day: 5``, ``source: "default"``).
#: Kept as a constant only so a cold cache can still say what the plan is; the
#: live figure always wins when :func:`limits` has been called.
DEFAULT_DAILY_USD = 5.0

#: The per-minute request ceiling, per key and per account. Published on the
#: same limits table; the live value is read from :func:`limits`.
DEFAULT_RPM = 30


@dataclass(frozen=True)
class Model:
    """One entry from Coarena's public model catalog."""

    id: str
    name: str = ""
    owner: str = ""
    context_length: int = DEFAULT_CONTEXT
    max_output: int | None = None
    price_in: float = 0.0
    price_out: float = 0.0
    #: Whether a price was actually **read**. The catalog publishes ``"0"`` for
    #: every model today, so this is True across the board — but a gateway that
    #: stops publishing a price must not have that read as 0/0 and marked free.
    #: The flag is what keeps "no price" from becoming "free".
    priced: bool = False
    #: The catalog's own capability flags. Unlike BeatAPI's free tier, Coarena
    #: publishes these for every entry, so they are a measurement rather than a
    #: cross-reference borrowed from a paid sibling.
    tools: bool = False
    vision: bool = False
    reasoning: bool = False
    json_schema: bool = False
    #: The reasoning efforts the model accepts (``low``, ``medium``, ``high``,
    #: ``xhigh``, ``max``, …). Display only: the harness has its own
    #: ``/effort`` control and does not map one onto the other.
    efforts: tuple[str, ...] = ()
    #: Other spellings the gateway accepts on the wire, straight from the
    #: catalog (``fable-5``, ``anthropic/claude-fable-5``). Carried so a turn
    #: sent under an alias prices and sizes exactly like the bare id.
    aliases: tuple[str, ...] = ()
    description: str = ""

    @property
    def free(self) -> bool:
        """Whether this model costs the operator nothing per token.

        The catalog prices every listed model at ``0`` in and ``0`` out, and
        the account is metered in real dollars against a daily allowance — so
        "free" here means *no per-token charge*, and the allowance is the real
        ceiling. A **missing** price does not count, which is what ``priced``
        records: without it an unpriced entry would read as 0/0 and be called
        free, the trap the other gateway modules document.
        """
        return self.priced and self.price_in == 0.0 and self.price_out == 0.0

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn.

        Always true: ``/api/v1/models`` is the Model API's text catalog, and
        every entry carries ``type: "model"`` with a text description. The
        modality filter the other gateway modules carry has nothing to drop
        here, and the property stays so callers can ask uniformly.
        """
        return True

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits: list[str] = []
        if self.free:
            bits.append("free")
        elif self.priced and (self.price_in or self.price_out):
            bits.append(f"${self.price_in:g}/${self.price_out:g}")
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
        if self.reasoning:
            bits.append("reasoning")
        if self.vision:
            bits.append("vision")
        if not self.tools:
            bits.append("no tools")
        if self.owner:
            bits.append(self.owner)
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------

def current_key() -> str | None:
    """The source key the operator last selected, if any.

    Delegates to :mod:`zimzilla.sources`, which owns ``current-source``. Two
    writers of one selection file is how a switch made here and a switch made
    with another gateway's command end up disagreeing about what is live.
    """
    from . import sources as sources_mod
    return sources_mod.current_key()


def set_current(key: str) -> None:
    """Record the selection. Never fatal — a read-only home must not break the TUI."""
    from . import sources as sources_mod
    sources_mod.set_current(key)


def is_coarena(base_url: str | None) -> bool:
    """Whether *base_url* points at Coarena (either surface)."""
    return HOST in (base_url or "").lower()


#: Generic name for the same test, so code that handles several gateways
#: uniformly can call ``mod.active_endpoint(url)`` without knowing which one it
#: holds. See ``_cmd_gateway_key`` in ui/app.py.
active_endpoint = is_coarena


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
    """A Coarena key already sitting in ``~/claude-source/<name>``.

    A profile there is the ``source ~/claude-source/<name>`` idiom, and the
    provider's own docs publish exactly that setup for Claude Code. Only one
    aimed at this host counts — a profile pointing somewhere else is a
    different provider's credential and adopting it would send the wrong key
    upstream.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_coarena(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The Coarena key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at Coarena — i.e. the operator
         sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/coarena-api-setup``.
      3. Any Coarena profile under ``~/claude-source``.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged
    or returned to the transcript — only ``origin`` (a path, or "environment").
    """
    if is_coarena(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Write the key to ZimZilla's own Coarena profile, mode 600.

    The file is the same ``source`` format as every other profile — it can be
    sourced by a shell and read by ``load_credentials`` — so there is one
    credential format in the project, not two. ``ANTHROPIC_BASE_URL`` names the
    Messages root (``/api``), not ``/api/v1``: see :data:`DEFAULT_BASE_URL`.
    """
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        "# ZimZilla — Coarena profile (written by /coarena-api-setup)\n"
        "# Treat this file as a credential. Never commit it.\n"
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


def apply_to(cfg) -> tuple[str, str] | None:
    """Point *cfg* at Coarena. Returns ``(origin, model)`` or ``None``.

    The one place that knows how to aim a session at this gateway, shared by
    ``/zim-coarena`` and the launch-time restore in ``__main__`` — so a session
    started by the command and one started from the remembered source end up
    configured identically.

    ``api_key`` is cleared rather than left as-is: the client sends
    ``auth_token`` when it is set, and a stale ``api_key`` from another
    provider would be a credential for the wrong service sitting in memory.
    The provider's docs warn about the same thing from the other side — "an
    Anthropic key left in ``ANTHROPIC_API_KEY`` is ignored here, but it still
    leaves your machine; a second Coarena key there is a 401
    ``conflicting_credentials``".

    The model is the profile's when it names one; when it does not, ``cfg`` is
    left as it is. That is deliberate: ``apply_to`` also runs on the
    launch-time restore, where the model may be an explicit ``--model`` the
    operator typed, and overriding it here would silently discard the choice.
    A *switch* that has no model to apply lands on :func:`default_free_model`
    in the caller instead, where that intent is unambiguous.

    Returns ``None`` when no key can be found anywhere, and leaves *cfg* alone.
    """
    creds = load_credentials()
    if creds is None:
        return None
    token, model, origin = creds
    cfg.base_url = DEFAULT_BASE_URL
    cfg.auth_token = token
    cfg.api_key = ""
    if model:
        cfg.model = model
    return origin, model


def applies_to_default_endpoint(base_url: str | None) -> bool:
    """Whether a remembered Coarena choice should override *base_url*.

    False for an endpoint the operator pointed somewhere deliberate — this
    gateway itself, or any other non-local host. True for the local proxies and
    the built-in default, which is what the launcher leaves behind when it has
    no Coarena profile to source. This is what keeps a stale ``current-source``
    from hijacking a session aimed at a different provider.
    """
    url = (base_url or "").strip()
    if is_coarena(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

#: Fetched catalogs, keyed by the token they were fetched with. The catalog is
#: public and identical for every caller, so every fetch is cached under "" in
#: practice; the key is kept in the cache key so a future keyed fetch (a
#: per-account model allow-list) can be added without changing callers.
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


def _str_tuple(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v)


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
    """Every entry in the catalog payload, in catalog order.

    The catalog's shape, measured: ``id``, ``display_name``, ``owned_by``,
    ``context_length``, ``max_completion_tokens``, ``pricing: {prompt,
    completion}`` as **strings of dollars per token**, ``capabilities: {vision,
    tools, json_schema, reasoning_text, efforts: [...]}``, ``aliases: [...]``
    and a ``description``. Prices are converted to per-million once, here, so
    every consumer in the project sees the same unit.
    """
    out: list[Model] = []
    for entry in _entries(payload):
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        pricing = entry.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        price_in = _as_float(pricing.get("prompt"))
        price_out = _as_float(pricing.get("completion"))
        priced = price_in is not None and price_out is not None
        if not priced:
            price_in = price_out = 0.0
        else:
            # The catalog prices per *token*; everything in this project is per
            # million. Converted once, here, rather than in every consumer.
            price_in = round(price_in * 1_000_000.0, 6)
            price_out = round(price_out * 1_000_000.0, 6)

        caps = entry.get("capabilities")
        caps = caps if isinstance(caps, dict) else {}

        out.append(Model(
            id=mid,
            name=str(entry.get("display_name") or ""),
            owner=str(entry.get("owned_by") or ""),
            context_length=_as_int(entry.get("context_length")) or DEFAULT_CONTEXT,
            max_output=_as_int(entry.get("max_completion_tokens")),
            price_in=price_in or 0.0,
            price_out=price_out or 0.0,
            priced=priced,
            tools=bool(caps.get("tools")),
            vision=bool(caps.get("vision")),
            reasoning=bool(caps.get("reasoning_text")),
            json_schema=bool(caps.get("json_schema")),
            efforts=_str_tuple(caps.get("efforts")),
            aliases=_str_tuple(entry.get("aliases")),
            description=str(entry.get("description") or ""),
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch Coarena's public model catalog. Returns ``(models, error)``.

    **No key required** — ``/api/v1/models`` answers 200 with no credential, and
    with a *bad* one too (measured), so this is a public list rather than a
    credential test. That is what lets ``/coarena-models`` and the picker work
    before ``/coarena-api-setup`` has run. No credential is sent either: this is
    the same public list for every caller, and transmitting a key to a surface
    that does not use one is a habit worth not forming. *token* only keys the
    cache, for the day a per-account listing is wanted.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach Coarena" rather than take the session down.
    ``models`` is ``None`` on failure, ``[]`` for a genuinely empty catalog —
    the caller can tell the two apart.
    """
    token = (token or "").strip()
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    req = urllib.request.Request(
        CATALOG_URL,
        headers={"accept": "application/json", "User-Agent": "zimzilla"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, "rate limited (429) — retry shortly"
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"

    models = _parse_models(payload if isinstance(payload, dict) else {})
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the models Coarena charges nothing per token for.

    Which, today, is every listed model — the catalog prices them all at 0/0
    and the account is metered against a daily allowance instead. The filter
    stays because it is the question the caller is really asking, and a model
    added later with a real per-token rate must not be offered as free.
    """
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The free models this harness can drive, tool-capable line first.

    Free **and** able to answer a chat turn — which, since the catalog is the
    Model API's text one, means every free entry. The sort puts a model whose
    catalog entry advertises tool calling first, because a model that cannot
    call a tool cannot drive an agent loop; the sort is stable, so within a tier
    catalog order is preserved. Unlike BeatAPI's free tier, Coarena publishes
    ``capabilities`` for every entry, so this is a measurement rather than a
    cross-reference borrowed from a paid sibling.
    """
    models, err = free_models(token, **kw)
    if models is None:
        return None, err
    return sorted(models, key=lambda m: not m.tools), ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every model id, catalog order preserved — what ``/model`` offers.

    All of them, not only the free ones: an operator who names a paid model
    deliberately should not have it refused as "not served by coarena" against
    a list filtered to the free tier.
    """
    models, err = fetch_models(token, **kw)
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
    whatever the last fetch left behind.

    **The ``CACHE_TTL`` check is load-bearing**, exactly as in the other
    gateway modules. Without it a catalog fetched once reads as live for the
    rest of the session, and models are switched off and on at the provider's
    discretion, so a stale list is the wrong list this picker exists to avoid.
    An expired entry reads as ``None``, which callers already handle as "no
    catalog".
    """
    newest: tuple[list[Model], float] | None = None
    for entry in _CACHE.values():
        if newest is None or entry[1] > newest[1]:
            newest = entry
    if newest is None or (time.monotonic() - newest[1]) >= CACHE_TTL:
        return None
    return list(newest[0])


def cached_ids() -> list[str] | None:
    """The last-fetched model ids, without touching the network."""
    models = cached_models()
    return [m.id for m in models] if models is not None else None


def clear_cache() -> None:
    """Drop every cached catalog. Called when the key changes."""
    _CACHE.clear()


def model_for(model: str, models: list[Model] | None) -> Model | None:
    """The catalog's entry for *model*, if the catalog knows it.

    Both spellings resolve: the catalog's bare id and any alias it publishes
    (``fable-5``, ``anthropic/claude-fable-5``), so a turn sent under an alias
    prices and sizes exactly like the bare id.
    """
    if not models:
        return None
    base = (model or "").strip()
    for m in models:
        if m.id == base:
            return m
    for m in models:
        if base in m.aliases:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok for *model*, when the catalog can answer.

    A free model returns ``(0.0, 0.0)``, which is exact for the per-token
    charge and makes ``/cost`` read ``free``. An id the catalog does not know
    returns ``None``, so the caller falls back to this harness's labelled
    estimate.

    An entry the catalog serves **without a price** returns ``None`` too, and
    that is deliberate: a missing price is not a zero price — see
    :attr:`Model.priced`.
    """
    found = model_for(model, models)
    if found is None:
        return None
    if found.free:
        return (0.0, 0.0)
    if not found.priced:
        return None
    return (found.price_in, found.price_out)


def default_free_model() -> str:
    """The model to land on when nothing is chosen — the free default."""
    return DEFAULT_MODEL


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    Deliberately loose — the gateway decides what is valid, not us — but strict
    enough to catch a pasted shell line or a bare word. The ``coa_`` prefix is
    *not* required: a real key of an unexpected shape is worth more stored than
    rejected, and a wrong one only costs a single 401.
    """
    value = (value or "").strip()
    if len(value) < 12 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))


# ---------------------------------------------------------------------------
# the account's allowance
# ---------------------------------------------------------------------------

def limits(token: str, *, timeout: float = 20.0) -> tuple[dict | None, str]:
    """The account's live allowance. Returns ``(payload, error)``.

    ``GET /api/v1/limits`` is the one route that answers **401 without a key**
    and 200 with one, so this doubles as the credential probe — see
    :func:`verify_key`. It is also the only place the day's remaining budget is
    published, which is what makes "free" honest: the catalog prices every
    model at zero, and the account is nevertheless metered in real dollars
    against ``usd_per_day``.

    The returned dict is the ``data`` object as the gateway sent it, so a
    caller can read ``usd_per_day``, ``today.left_usd``, ``today.requests``,
    ``requests_per_minute_key`` and ``concurrency`` without this module
    inventing a second shape for them. A failure is returned, never raised.
    """
    token = (token or "").strip()
    if not token:
        return None, "no key"
    req = urllib.request.Request(
        LIMITS_URL,
        headers={"Authorization": f"Bearer {token}",
                 "accept": "application/json",
                 "User-Agent": "zimzilla"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except OSError:
            pass
        return None, explain_error(e.code, body)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"
    data = payload.get("data") if isinstance(payload, dict) else None
    return (data if isinstance(data, dict) else {}), ""


def allowance_line(payload: dict | None) -> str:
    """A one-line summary of an allowance payload, for the transcript.

    Empty when there is nothing to say, so a caller can append it
    unconditionally. The figures are the gateway's own — this only formats
    them, and it never invents a number the payload did not carry.
    """
    if not isinstance(payload, dict) or not payload:
        return ""
    today = payload.get("today")
    today = today if isinstance(today, dict) else {}
    bits: list[str] = []
    per_day = _as_float(payload.get("usd_per_day"))
    if per_day is not None:
        bits.append(f"${per_day:g}/day")
    left = _as_float(today.get("left_usd"))
    if left is not None:
        bits.append(f"${left:.2f} left today")
    requests = _as_int(today.get("requests"))
    if requests is not None:
        bits.append(f"{requests} requests")
    rpm = _as_int(payload.get("requests_per_minute_key"))
    if rpm is not None:
        bits.append(f"{rpm}/min")
    conc = _as_int(payload.get("concurrency"))
    if conc is not None:
        bits.append(f"{conc} concurrent")
    return " · ".join(bits)


# ---------------------------------------------------------------------------
# error shapes
# ---------------------------------------------------------------------------

#: The provider's own error codes, mapped to the action each one calls for.
#: Read from the docs' 41-code table on 2026-11-12. The distinction matters:
#: "your key is wrong" and "this model is switched off" are both 400/401 and
#: call for completely different things.
_ERROR_HINTS: dict[str, str] = {
    "invalid_api_key": "the key is not valid — check it with /coarena-api-setup",
    "api_key_revoked": "the key was revoked — make a new one at "
                       "https://coarena.ai/account/api/keys",
    "api_key_expired": "the key expired — make a new one at "
                       "https://coarena.ai/account/api/keys",
    "api_keys_disabled": "keys are disabled on this account",
    "conflicting_credentials": "two credentials were sent — clear "
                               "ANTHROPIC_API_KEY and keep only the token",
    "insufficient_scope": "the key lacks the inference:write scope — make a new "
                          "one with it",
    "model_api_terms_required": "accept the Model API terms once at "
                                "https://coarena.ai/account/api",
    "account_blocked": "the account is blocked",
    "account_withdrawn": "the account has withdrawn from the Model API",
    "limit_request_no_calls": "a limit increase needs one successful call first",
    "limit_request_pending": "a limit request is already open — "
                             "GET /api/v1/limits shows where it stands",
    "limit_at_max": "the limit is already at its maximum",
    "model_api_off": "the Model API is switched off on this deployment",
    "model_off": "that model is switched off right now — /coarena-models lists "
                 "the ones that are on",
    "provider_restricted": "the model's provider is restricted on this account",
    "model_not_found": "unknown model id — see /model for what is served",
    "request_too_large": "the request is too large — the input cap is 128,000 "
                         "tokens",
    "context_length_exceeded": "past the 128,000-token input cap — /compact and "
                               "retry",
    "assistant_prefill_unsupported": "this model does not accept an assistant "
                                     "prefill",
    "content_filtered": "the provider filtered the content",
    "rate_limit_exceeded": "rate limited (30/min per key) — retry with backoff",
    "concurrency_limit_exceeded": "too many calls at once (2 per account) — "
                                  "retry with backoff",
    "insufficient_quota": "the $5/day allowance is spent — it resets on the UTC "
                          "day, or ask for more with POST /api/v1/limits/requests",
    "upstream_error": "the provider behind the model errored — retry",
    "model_api_disabled": "the Model API is disabled — retry later",
}


def explain_error(status: int, body: str) -> str:
    """A sentence for a Coarena failure, from the shape it came back in.

    The gateway answers in the Anthropic error shape —
    ``{"type": "error", "error": {"type", "message", "code"}}`` — and its
    ``code`` is the specific half: ``model_off`` and ``invalid_api_key`` are
    both a 400/401 and call for completely different actions. The code is
    preferred when it is one this module knows, and the message is the fallback
    so an unmapped code still says something useful.

    ``body`` is the decoded response text, or ``""`` when there was none.
    """
    text = (body or "").strip()
    detail = ""
    code = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        inner = payload.get("error")
        if isinstance(inner, dict):
            detail = str(inner.get("message") or "")
            code = str(inner.get("code") or "")
        if not detail:
            detail = str(payload.get("message") or "")
        if not code:
            code = str(payload.get("code") or "")

    hint = _ERROR_HINTS.get(code)
    if hint:
        return hint
    if status == 401:
        return ("key rejected (401) — check it with /coarena-api-setup")
    if status == 403:
        return ("the key is not permitted to do that (403) — the key itself is "
                "fine; check its scopes or the account's standing")
    if status == 404:
        return "unknown model id (404) — see /model for what is served"
    if status == 409:
        return "a limit request is already open (409) — GET /api/v1/limits"
    if status == 413:
        return "the request is too large (413) — the input cap is 128,000 tokens"
    if status == 429:
        return ("rate limited or out of allowance (429) — retry with backoff; a "
                "daily limit resets on the UTC day")
    if 500 <= status < 600:
        base = f"upstream error ({status}) — retry; if it persists, the model is the problem"
        return f"{base}: {detail[:200]}" if detail else base
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    return f"HTTP {status}"


def verify_key(token: str, *, timeout: float = 20.0) -> tuple[bool, str]:
    """Prove *token* works with one authenticated, read-only request.

    ``GET /api/v1/limits`` requires a key — it answers 401 without one, and 401
    with a bad one (both measured) — so unlike the public Models route this
    probe really does test the credential, and it sends no tokens to do it. A
    ``200`` means the key is accepted, and the same reply carries the day's
    allowance, which is worth showing.

    ``ok`` is False only for a rejected key (401/403) or an unreachable
    endpoint. A 429 or 5xx says something about the account's state or the
    provider's capacity, not about the credential, so those come back
    ``ok=True`` with the reason in ``detail`` — failing the setup on them would
    refuse a working key over a condition that clears on its own.
    """
    token = (token or "").strip()
    if not token:
        return False, "no key"
    req = urllib.request.Request(
        LIMITS_URL,
        headers={"Authorization": f"Bearer {token}",
                 "accept": "application/json",
                 "User-Agent": "zimzilla"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except OSError:
            pass
        if e.code in (401, 403):
            return False, explain_error(e.code, body)
        return True, explain_error(e.code, body)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"could not reach {HOST}: {e}"

    # The probe already paid for the allowance payload, so report it rather
    # than making the operator run a second command to see the same numbers.
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        payload = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    line = allowance_line(data if isinstance(data, dict) else None)
    return True, f"key accepted — {line}" if line else "key accepted"
