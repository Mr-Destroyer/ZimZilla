"""Zro (MoonMath.ai) — a hosted Anthropic-protocol gateway, as a ZimZilla source.

Zro is MoonMath.ai's inference service at ``https://zro.moonmath.ai``. It is the
same shape of thing as TokenHarbour and OpenRouter: a remote endpoint that
speaks the **Anthropic Messages** protocol natively, so there is no proxy to
start, no LiteLLM config to translate through and nothing that has to be
installed. Measured against the live host on 2026-11-01:

* ``POST /v1/messages`` answers a JSON 401 ``{"error": {"message":
  "Authentication Error, No api key passed in.", ...}}`` with no key, while
  ``POST /v1/nonsense`` answers a plain-text 404. So the route exists and the
  provider checks the key before it looks at the body — the Messages surface is
  served, and it is the one this harness talks.
* ``POST /v1/chat/completions`` and ``POST /v1/responses`` answer the same 401.
  The provider serves all three surfaces from one host, which is why the
  ``zro`` CLI can point Codex (Responses), Claude Code (Messages) and any
  OpenAI SDK (Chat Completions) at the same key. ZimZilla uses Messages, the
  protocol it already speaks — the other two are mentioned only so a future
  transport change is a documented option rather than a guess.
* ``GET /v1/models`` answers **401 without a key**, so unlike NVIDIA's public
  Models route this one is a genuine credential probe: one GET proves a key.
* ``GET /models`` answers **200 with no key at all** and carries the full
  catalog — price, context window, max output, modalities, features. That is
  what makes ``/moonmath-models`` and the model picker useful *before* a key
  has been set, and it is the catalog this module reads.

The gateway's docs (``/docs/api``) state the contract, and it is the contract
this module implements:

* Messages base URL is the **root**, ``https://zro.moonmath.ai`` — the Anthropic
  SDK appends ``/v1/messages`` itself. Chat Completions and Responses live under
  ``https://zro.moonmath.ai/v1``.
* Keys are bearer tokens shaped ``sk-...`` and are scoped to an account, with
  optional spend limits, expiry and a region allow-list.
* Both the bare id (``deepseek-v4.1-flash``) and its ``zro/`` alias are
  accepted on every endpoint; the catalog publishes the bare id.
* Errors are the standard ``{"error": {"message", "type"}}`` shape, with
  distinct meanings per status: 401 key, 402 budget, 403 key/model, 404 unknown
  model, 429 rate limit.

**Free is the Free plan's daily allowance, not a zero price.** The catalog
publishes a real list price for every model — and three of them are what the
Free plan includes daily usage on: ``deepseek-v4.1-flash`` and
``glm-5.3-flash`` (plus ``auto``, which bills at whichever tier answers, so it
is not marked free). :data:`FREE_MODEL_IDS` is that set, read from the pricing
page rather than from the catalog, because the catalog does not carry a free
flag. :attr:`Model.free` therefore means "the Free plan covers this", and
:func:`price_for` answers ``(0, 0)`` for those ids so ``/cost`` reads
``free``. On a paid plan the same models bill at the published rate — the
catalog cannot see which plan a key is on, and the module says so rather than
pretending the price is always zero.

Nothing here echoes a key. The profile is the same ``export VAR="value"`` file
every other source uses, written mode 600.
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

#: The canonical host. Matching is by this substring, so a trailing slash and
#: the API's own paths all count as the same source.
HOST = "zro.moonmath.ai"

#: What the session points at. The **Messages base URL is the root**: the
#: Anthropic SDK appends ``/v1/messages`` to what it is handed, so spelling
#: ``/v1`` here would produce ``/v1/v1/messages`` and a 404 on every turn.
#: This is the one detail a new gateway gets wrong, and OpenRouter's module
#: documents the same trap from the other side.
DEFAULT_BASE_URL = f"https://{HOST}"

#: The Chat Completions / Responses root. Not the route this harness takes —
#: it is here so the two surfaces are spelled once, and so a future transport
#: does not have to rediscover it.
API_BASE_URL = f"https://{HOST}/v1"

#: The public catalog: every model, with price, window, max output, modalities
#: and features. No credential required — see the module docstring.
CATALOG_URL = f"https://{HOST}/models"

#: The keyed Models route. Thin (OpenAI shape) but authenticated, which is what
#: makes it the credential probe. See :func:`verify_key`.
MODELS_URL = f"{API_BASE_URL}/models"

#: The profile ``/moonmath-api-setup`` writes and every lookup reads.
PROFILE = ZIMZILLA_HOME / "moonmath" / "source"

#: Zro keys are issued as ``sk-<long token>``. A hint for the confirmation
#: line, never a gate: the upstream is the authority on its own keys, and
#: refusing a real key of an unexpected shape would be worse than storing it
#: and letting one 401 say so.
KEY_PREFIX = "sk-"

#: The model a switch selects when the profile names none. It is the
#: gateway's own recommended default for coding agents, it is on the Free
#: plan's daily allowance, and its catalog entry carries a 1M window, tool
#: use and reasoning — three things this harness can actually drive.
DEFAULT_MODEL = "deepseek-v4.1-flash"

#: A fetched catalog older than this is refetched rather than trusted. The
#: lineup turns over (the docs keep a "retired models" page), so a stale list
#: is exactly the wrong list this picker exists to avoid.
CACHE_TTL = 300.0

#: The models the **Free plan** includes daily usage on, per
#: https://zro.moonmath.ai/pricing as read on 2026-11-01. Kept here, not
#: derived from the catalog, because the catalog prices every model and does
#: not say which plan covers it. An id that is later withdrawn is harmless:
#: :func:`usable_free_models` only ever marks a model that the catalog still
#: serves, and the marker is a *quota* fact rather than a wire fact.
FREE_MODEL_IDS: frozenset[str] = frozenset({
    "deepseek-v4.1-flash",
    "glm-5.3-flash",
})

#: Every id is also served under this prefix, for harnesses that reserve
#: well-known model names. Normalised away here so ``zro/glm-5.3`` and
#: ``glm-5.3`` are the same model for pricing and the free check.
ALIAS_PREFIX = "zro/"

#: The window to plan against when the catalog publishes none for an id.
#: 128K rather than the harness's 1M fallback: the direction of a wrong guess
#: matters, and compacting early is survivable while overflowing is not.
DEFAULT_CONTEXT = 131_072

#: The catalog prices in **USD per token**, as strings. Everything in this
#: project is per million tokens, which is the unit ``/cost`` and the estimate
#: table use; the conversion happens once, in :func:`_per_mtok`.
PRICE_SCALE = 1_000_000.0


def _normalise(model: str) -> str:
    """The bare id for *model*: ``zro/glm-5.3`` -> ``glm-5.3``."""
    value = (model or "").strip()
    if value.lower().startswith(ALIAS_PREFIX):
        return value[len(ALIAS_PREFIX):]
    return value


@dataclass(frozen=True)
class Model:
    """One entry from Zro's public model catalog."""

    id: str
    name: str = ""
    owner: str = ""
    context_length: int = DEFAULT_CONTEXT
    max_output: int | None = None
    price_in: float = 0.0
    price_out: float = 0.0
    #: Whether a price was actually **read**. A missing price must not read as
    #: 0/0 and mark every unpriced entry free — the trap the other gateway
    #: modules document.
    priced: bool = False
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    description: str = ""

    @property
    def free(self) -> bool:
        """Whether the Free plan covers this model.

        The plan's included ids are authoritative — the catalog does not carry
        a free flag, and it prices every entry. A read 0/0 price corroborates
        it; a *missing* price does not count, which is what ``priced`` records.
        """
        if _normalise(self.id) in FREE_MODEL_IDS:
            return True
        return self.priced and self.price_in == 0.0 and self.price_out == 0.0

    @property
    def aliases(self) -> tuple[str, ...]:
        """Other spellings the gateway accepts on the wire.

        Zro serves every id both bare (``glm-5.3``) and ``zro/``-prefixed
        (``zro/glm-5.3``), which lets a harness that reserves well-known names
        avoid a collision. The catalog publishes the bare id, so that is
        :attr:`id`; the prefixed form is declared here so ``sources`` prices and
        sizes a turn sent either way identically.
        """
        return (f"{ALIAS_PREFIX}{_normalise(self.id)}",)

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn.

        Every catalog entry today is text-output; the test is on the field the
        catalog actually sends rather than on a name list, so an image or
        embedding model added later is filtered by what it is.
        """
        return "text" in self.output_modalities or not self.output_modalities

    @property
    def tools(self) -> bool:
        """Whether the catalog advertises tool calling for this model."""
        return "tools" in self.features

    @property
    def reasoning(self) -> bool:
        """Whether the catalog advertises a reasoning control."""
        return "reasoning" in self.features

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits: list[str] = []
        if self.free:
            bits.append("free daily")
        elif self.priced and (self.price_in or self.price_out):
            bits.append(f"${self.price_in:g}/${self.price_out:g}")
        bits.append(f"{self.context_length // 1000}k ctx")
        if self.reasoning:
            bits.append("reasoning")
        if not self.tools:
            bits.append("no tools")
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------

def current_key() -> str | None:
    """The source key the operator last selected, if any.

    Delegates to :mod:`zimzilla.sources`, which owns ``current-source``. Two
    writers of one selection file is how a switch made here and a switch made
    with ``/zim-logfare`` end up disagreeing about what is live.
    """
    from . import sources as sources_mod
    return sources_mod.current_key()


def set_current(key: str) -> None:
    """Record the selection. Never fatal — a read-only home must not break the TUI."""
    from . import sources as sources_mod
    sources_mod.set_current(key)


def is_moonmath(base_url: str | None) -> bool:
    """Whether *base_url* points at Zro."""
    return HOST in (base_url or "").lower()


#: Generic name for the same test, so code that handles several gateways
#: uniformly can call ``mod.active_endpoint(url)`` without knowing which one it
#: holds.
active_endpoint = is_moonmath


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
    """A Zro key already sitting in ``~/claude-source/<name>``.

    A profile there is the ``source ~/claude-source/<name>`` idiom, and the
    gateway's own docs publish exactly that setup for Claude Code. Only one
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
        if not is_moonmath(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The Zro key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at Zro — i.e. the operator sourced
         a profile before launching.
      2. ZimZilla's own profile, written by ``/moonmath-api-setup``.
      3. Any Zro profile under ``~/claude-source``.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged
    or returned to the transcript — only ``origin`` (a path, or "environment").
    """
    if is_moonmath(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Write the key to ZimZilla's own Zro profile, mode 600.

    The file is the same ``source`` format as every other profile — it can be
    sourced by a shell and read by ``load_credentials`` — so there is one
    credential format in the project, not two. ``ANTHROPIC_BASE_URL`` names the
    Messages root, not ``/v1``: see :data:`DEFAULT_BASE_URL`.
    """
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        "# ZimZilla — Zro (MoonMath.ai) profile (written by /moonmath-api-setup)\n"
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
    """Point *cfg* at Zro. Returns ``(origin, model)`` or ``None``.

    The one place that knows how to aim a session at this gateway, shared by
    ``/zim-moonmath`` and the launch-time restore in ``__main__`` — so a
    session started by the command and one started from the remembered source
    end up configured identically.

    ``api_key`` is cleared rather than left as-is: the client sends
    ``auth_token`` when it is set, and a stale ``api_key`` from another
    provider would be a credential for the wrong service sitting in memory.

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
    """Whether a remembered Zro choice should override *base_url*.

    False for an endpoint the operator pointed somewhere deliberate — this
    gateway itself, or any other non-local host. True for the local proxies and
    the built-in default, which is what the launcher leaves behind when it has
    no Zro profile to source. This is what keeps a stale ``current-source``
    from hijacking a session aimed at a different provider.
    """
    url = (base_url or "").strip()
    if is_moonmath(url):
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


def _per_mtok(value) -> float | None:
    """A per-token USD string from the catalog, as USD per million tokens."""
    raw = _as_float(value)
    if raw is None:
        return None
    return round(raw * PRICE_SCALE, 6)


def _entries(payload: dict) -> list[dict]:
    """The catalog rows from ``{"data": [...]}``.

    A malformed payload yields an empty catalog rather than an exception: this
    runs inside a TUI command.
    """
    data = payload.get("data")
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    return []


def _str_tuple(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v)


def _parse_models(payload: dict) -> list[Model]:
    """Every entry in the catalog payload, in catalog order."""
    out: list[Model] = []
    for entry in _entries(payload):
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        pricing = entry.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        price_in = _per_mtok(pricing.get("prompt"))
        price_out = _per_mtok(pricing.get("completion"))
        priced = price_in is not None and price_out is not None
        if not priced:
            price_in = price_out = 0.0

        # The catalog names the upstream weights, and its owner is the useful
        # half of that string. ``owned_by`` is the fallback for an entry that
        # does not carry one.
        hf = str(entry.get("hugging_face_id") or "")
        owner = hf.split("/", 1)[0] if "/" in hf else str(entry.get("owned_by") or "")

        out.append(Model(
            id=mid,
            name=str(entry.get("name") or ""),
            owner=owner,
            context_length=_as_int(entry.get("context_length")) or DEFAULT_CONTEXT,
            max_output=_as_int(entry.get("max_output_length")),
            price_in=price_in or 0.0,
            price_out=price_out or 0.0,
            priced=priced,
            input_modalities=_str_tuple(entry.get("input_modalities")),
            output_modalities=_str_tuple(entry.get("output_modalities")),
            features=_str_tuple(entry.get("supported_features")),
            description=str(entry.get("description") or ""),
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch Zro's public model catalog. Returns ``(models, error)``.

    **No key required** — ``/models`` answers 200 with no credential — so
    ``/moonmath-models`` and the picker work before ``/moonmath-api-setup`` has
    run. No credential is sent either: this is the same public list for every
    caller, and transmitting a key to a surface that does not use one is a
    habit worth not forming. *token* only keys the cache, for the day a
    per-account listing is wanted.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach Zro" rather than take the session down.
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
    """Only the models the Free plan covers."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The free models this harness can drive — text output only.

    Free **and** able to answer a chat turn. Today every catalog entry is both,
    so this equals :func:`free_models`; the filter stays because it is the
    question the caller is really asking, and a speech or image model added to
    the free allowance later must not be offered as a chat model.
    """
    models, err = free_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.chat], ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every chat model id, catalog order preserved — what ``/model`` offers.

    All of them, not only the free ones: an operator who names a paid model
    deliberately should not have it refused as "not served by moonmath" against
    a list filtered to the free allowance.
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
    """The catalog's entry for *model*, if the catalog knows it.

    Both spellings resolve: the catalog's bare id and the ``zro/`` alias the
    gateway accepts on the wire.
    """
    if not models:
        return None
    base = _normalise(model)
    for m in models:
        if _normalise(m.id) == base:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok for *model*, when the catalog can answer.

    A Free-plan model returns ``(0.0, 0.0)``, which is exact for that plan and
    makes ``/cost`` read ``free``. An id the catalog does not know returns
    ``None``, so the caller falls back to this harness's labelled estimate.

    An entry the catalog serves **without a price** returns ``None`` too, and
    that is deliberate: ``auto`` is a virtual router billed at whichever model
    answers, so a fabricated ``(0, 0)`` would call it free and the estimate
    table's default is at least labelled as an estimate. A missing price is not
    a zero price — see :attr:`Model.priced`.
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
    return DEFAULT_MODEL if _normalise(DEFAULT_MODEL) in FREE_MODEL_IDS else next(
        iter(sorted(FREE_MODEL_IDS)), DEFAULT_MODEL)


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    Deliberately loose — the gateway decides what is valid, not us — but strict
    enough to catch a pasted shell line or a bare word. The ``sk-`` prefix is
    *not* required: a real key of an unexpected shape is worth more stored than
    rejected, and a wrong one only costs a single 401.
    """
    value = (value or "").strip()
    if len(value) < 12 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))


# ---------------------------------------------------------------------------
# error shapes
# ---------------------------------------------------------------------------

def explain_error(status: int, body: str) -> str:
    """A sentence for a Zro failure, from the shape it came back in.

    The gateway's docs give one meaning per status, and they are not
    interchangeable — "your key is wrong" and "this model is not on your key"
    call for different actions:

    * ``401`` — missing, invalid, revoked or expired key.
    * ``402`` — spend quota or plan budget exhausted (wait, or top up).
    * ``403`` — the key is not permitted to use the requested model.
    * ``404`` — unknown model id.
    * ``429`` — rate limited; retry with backoff.

    ``body`` is the decoded response text, or ``""`` when there was none.
    """
    text = (body or "").strip()
    detail = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        inner = payload.get("error")
        if isinstance(inner, dict):
            detail = str(inner.get("message") or inner.get("detail") or "")
        if not detail:
            detail = str(payload.get("detail") or payload.get("message") or "")
    lowered = (detail or text).lower()

    if status == 401:
        return ("key rejected (401) — check it with /moonmath-api-setup")
    if status == 402 or "quota" in lowered or "budget" in lowered:
        return ("spend quota or plan budget exhausted (402) — it resets on the "
                "plan's own cadence, or add a usage pack; this is a billing "
                "state, not a bad key")
    if status == 403:
        return ("this key is not permitted to use that model (403) — the key "
                "is fine, pick another model or widen the key's limits")
    if status == 404:
        return "unknown model id (404) — see /model for what this key is served"
    if status == 429:
        return "rate limited (429) — retry with backoff"
    if 500 <= status < 600:
        base = (f"upstream error ({status}) — retry; if it persists, the "
                "model is the problem")
        return f"{base}: {detail[:200]}" if detail else base
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    return f"HTTP {status}"


def verify_key(token: str, *, timeout: float = 20.0) -> tuple[bool, str]:
    """Prove *token* works with one authenticated, read-only request.

    ``GET /v1/models`` requires a key — it answers 401 without one — so unlike
    NVIDIA's public Models route this probe really does test the credential,
    and it sends no tokens to do it. A ``200`` means the key is accepted.

    ``ok`` is False only for a rejected key (401/403) or an unreachable
    endpoint. A 402, 429 or 5xx says something about the account's state, not
    about the credential, so those come back ``ok=True`` with the reason in
    ``detail`` — failing the setup on them would refuse a working key over a
    billing or capacity condition.
    """
    token = (token or "").strip()
    if not token:
        return False, "no key"
    req = urllib.request.Request(
        MODELS_URL,
        headers={"Authorization": f"Bearer {token}",
                 "accept": "application/json",
                 "User-Agent": "zimzilla"},
        method="GET",
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
            return False, explain_error(e.code, body)
        return True, explain_error(e.code, body)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"could not reach {HOST}: {e}"
