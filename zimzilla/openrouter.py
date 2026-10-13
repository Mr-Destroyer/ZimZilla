"""OpenRouter — a hosted Anthropic-protocol gateway, as a ZimZilla source.

OpenRouter is a remote endpoint (``https://openrouter.ai/api``) that fronts
hundreds of models behind one key. Measured on this box, not assumed:

* ``POST /api/v1/messages`` answers with an Anthropic-shaped 401 when no key is
  sent — i.e. the Messages protocol is served natively, so there is no
  translation layer to write and none is written. Note the shape of that URL
  against :data:`DEFAULT_BASE_URL`: the ``/v1`` is the SDK's, appended to the
  prefix it is handed. Getting that wrong is a 404 on every turn while the
  catalog still works, which is exactly what happened here once.
* ``GET /api/v1/models`` answers **with no key at all** — 463 models when this
  was written. The catalog is public, which is the fact that shapes this module:
  the free list can be shown, and a model chosen, *before* the operator has set
  anything. That is what makes the keyless path of ``/openrouter-api-setup``
  genuinely useful rather than a placeholder.

One trap, measured rather than guessed: **do not send ``anthropic-version`` to
``/models``.** Doing so flips the endpoint into the Anthropic Models API shape —
paginated at 20 entries with ``has_more``/``first_id``/``last_id`` and every id
prefixed ``anthropic/``. 20 mangled ids with the header, 463 clean ones without.
The catalog is the OpenAI-shaped listing; that header belongs on
``/v1/messages``.

Two things make it different enough to live in its own module, exactly as
TokenHarbour does:

* **The credential is set, not discovered.** There is no shipped profile — the
  operator supplies their own key with ``/openrouter-api-setup``. It is kept in
  a ``source``-format file of the same shape as every other profile, mode 600.
* **The catalog is live, public and priced.** Some models carry the ``:free``
  suffix and a 0/0 price, and most — but not all — of those advertise ``tools``
  support. The ones that do not (a content-safety classifier, two music models)
  would be useless to an agentic harness, so :func:`usable_free_models` filters
  on tool support and not merely on price.

Nothing here echoes a key.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
#: Where the operator keeps their per-provider shell profiles — the same
#: ``~/claude-source/<name>`` files a plain ``source`` loads for Claude Code.
CLAUDE_SOURCE = Path(os.environ.get("ZIMZILLA_CLAUDE_SOURCE")
                     or (Path.home() / "claude-source"))

#: The canonical host. Matching is by this substring, so a trailing slash and any
#: subdomain path all count as the same source.
HOST = "openrouter.ai"

#: The SDK's ``base_url`` — and it is the PREFIX BEFORE ``/v1``, not the API root.
#: The Anthropic SDK appends ``/v1/messages`` to whatever it is given (see
#: ``_build_request``), so this composes to ``/api/v1/messages``, which is where
#: OpenRouter actually serves the Messages protocol (a 401 without a key, i.e. the
#: path exists).
#:
#: It used to be ``https://openrouter.ai/api/v1``, which composed to
#: ``/api/v1/v1/messages`` and 404'd on *every* turn — measured live, after a model
#: switch that reported success. Nothing caught it because the catalog is built
#: separately and kept working, so ``/models`` looked fine while no message could
#: be sent. TokenHarbour never hit this: its base has no path component, so the
#: doubled ``/v1`` had nothing to collide with. That shape — origin in the
#: constant, ``/v1`` added at each call site — is the one to copy.
DEFAULT_BASE_URL = "https://openrouter.ai/api"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = ZIMZILLA_HOME / "openrouter" / "source"

#: A fetched catalog older than this is refetched rather than trusted.
CACHE_TTL = 300.0

#: The suffix OpenRouter puts on a model that costs nothing.
FREE_SUFFIX = ":free"


@dataclass(frozen=True)
class Model:
    """One entry from OpenRouter's catalog."""

    id: str
    label: str = ""
    context_length: int | None = None
    price_in: float = 0.0
    price_out: float = 0.0
    #: Parameter names the model accepts. ``tools`` here is what makes it usable
    #: by this harness at all — a model that cannot call a tool cannot drive an
    #: agent loop.
    supported: tuple[str, ...] = field(default_factory=tuple)
    #: Whether a price was actually **read** for this entry, not merely whether
    #: a pricing block was present. Without it a missing pricing block would
    #: read as 0/0 and mark every unpriced model free; and a block whose values
    #: cannot be parsed would do the same, which is why this tracks a successful
    #: parse rather than the block's existence.
    priced: bool = False

    @property
    def free(self) -> bool:
        """Whether this model is free.

        Two signals, either sufficient — the same pair TokenHarbour uses, for
        the same reason. The ``:free`` suffix alone would miss a model the
        gateway zeroes out without renaming; the price alone would miss one that
        is free but not yet priced in the listing. The catalog sends prices as
        **strings** ("0"), so they are parsed in ``_parse_models`` rather than
        compared here.
        """
        if self.id.endswith(FREE_SUFFIX):
            return True
        return self.priced and self.price_in == 0.0 and self.price_out == 0.0

    @property
    def tools(self) -> bool:
        """Whether the model advertises tool calling."""
        return "tools" in self.supported

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits = []
        if self.free:
            bits.append("free")
        elif self.price_in or self.price_out:
            bits.append(f"${self.price_in:g}/${self.price_out:g}")
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
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


def is_openrouter(base_url: str | None) -> bool:
    """Whether *base_url* points at OpenRouter."""
    return HOST in (base_url or "")


#: Generic name for the same test, so code that handles several gateways
#: uniformly can call ``mod.active_endpoint(url)`` without knowing which one it
#: holds. See ``_cmd_gateway_key`` in ui/app.py.
active_endpoint = is_openrouter


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
    """An OpenRouter key already sitting in ``~/claude-source/<name>``.

    This is the ``source ~/claude-source/<name>`` idiom: the operator has
    already written a profile for Claude Code, so ZimZilla should use it rather
    than asking for the key again. Returns ``(key, model, path)`` for the first
    profile that points at OpenRouter and carries a token.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_openrouter(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The OpenRouter key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at OpenRouter — i.e. the operator
         sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/openrouter-api-setup``.
      3. Any OpenRouter profile under ``~/claude-source``.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged or
    returned to the transcript — only ``origin`` (a path, or "environment") is.
    """
    if is_openrouter(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Write the key to ZimZilla's own OpenRouter profile, mode 600.

    The file is the same ``source`` format as every other profile — it can be
    sourced by a shell and read by ``load_credentials`` — so there is one
    credential format in the project, not two.
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


def apply_to(cfg) -> tuple[str, str] | None:
    """Point *cfg* at OpenRouter. Returns ``(origin, model)`` or ``None``.

    The one place that knows how to aim a session at this gateway, shared by
    ``/zim-openrouter`` and the launch-time restore in ``__main__`` — so a
    session started by the command and one started from the remembered source
    end up configured identically.

    ``api_key`` is cleared rather than left as-is: the client sends
    ``auth_token`` when it is set, and a stale ``api_key`` from another provider
    would be a credential for the wrong service sitting in memory.

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
    """Whether a remembered OpenRouter choice should override *base_url*.

    False for an endpoint the operator pointed somewhere deliberate — this
    gateway itself, or any other non-local host. True for the local proxies and
    the built-in default, which is what the launcher leaves behind when it has
    no OpenRouter profile to source. This is what keeps a stale
    ``current-source`` from hijacking a session aimed at a different provider.
    """
    url = (base_url or "").strip()
    if is_openrouter(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

#: Fetched catalogs, keyed by the token they were fetched with. OpenRouter's
#: catalog is public and identical for every caller, so the keyless fetch is
#: cached under "" and a keyed one under its token — a key change refetches
#: rather than serving a list fetched for a different account.
_CACHE: dict[str, tuple[list[Model], float]] = {}


def _parse_models(payload: dict) -> list[Model]:
    out: list[Model] = []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        pricing = entry.get("pricing") or {}
        # OpenRouter sends prices as STRINGS ("0", "0.0000015"). Parsed here so
        # `free` compares floats; a `== 0` against the raw value would be False
        # for every model including the free ones.
        #
        # `priced` records a successful *read*, not the block's presence: a
        # block carrying junk ("free!", null) would otherwise leave both floats
        # at 0.0 and mark a paid model free — the exact failure the flag exists
        # to prevent, arriving through a different door.
        priced = False
        pin = pout = 0.0
        if pricing:
            try:
                pin = float(pricing.get("prompt"))
                pout = float(pricing.get("completion"))
                priced = True
            except (TypeError, ValueError):
                priced = False
        ctx = entry.get("context_length")
        try:
            ctx = int(ctx) if ctx is not None else None
        except (TypeError, ValueError):
            ctx = None
        params = entry.get("supported_parameters") or []
        supported = tuple(str(p) for p in params if isinstance(p, str))
        out.append(Model(
            id=mid,
            label=str(entry.get("name") or ""),
            context_length=ctx,
            price_in=pin,
            price_out=pout,
            supported=supported,
            priced=priced,
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch OpenRouter's catalog. Returns ``(models, error)``.

    **No key required** — the catalog is public, so ``token`` defaults to empty
    and the free list is available before ``/openrouter-api-setup`` has run.
    A key is sent when one is supplied, because OpenRouter prices some entries
    differently for an authenticated caller and the listing should match what
    this session would actually be charged.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach OpenRouter" rather than take the session
    down. ``models`` is ``None`` on failure, ``[]`` for a genuinely empty
    catalog — the caller can tell the two apart.
    """
    token = (token or "").strip()
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    # The catalog lives at ``/api/v1/models`` — the ``/v1`` is spelled out here
    # rather than inherited from DEFAULT_BASE_URL, because that constant is the
    # SDK's *prefix* (``/api``) and does not carry it. The two paths are siblings
    # under the same version segment, not the same prefix: messages are
    # ``{base}/v1/messages`` by the SDK's own appending, models are
    # ``{base}/v1/models`` by hand. Spelling both out is what stops one being
    # "fixed" into breaking the other, which is precisely how this shipped.
    url = f"{DEFAULT_BASE_URL}/v1/models"
    # NO `anthropic-version` here, and this is not an oversight. Sending it
    # flips this endpoint into the *Anthropic* Models API shape: paginated at 20
    # entries with `has_more`/`first_id`/`last_id` and every id prefixed
    # `anthropic/`. Measured on this box — 20 mangled ids with the header, 463
    # clean ones without. The catalog is the OpenAI-shaped listing; the
    # Anthropic header belongs on /v1/messages, not here.
    headers = {"accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["x-api-key"] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "key rejected (401/403) — check it with /openrouter-api-setup"
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the models OpenRouter is currently giving away."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The free models this harness can actually drive.

    Free *and* advertising tool calling. The filter is not decoration: of the 15
    free models on this box, one (``nvidia/nemotron-3.5-content-safety:free``)
    does not support tools, and a model that cannot call a tool cannot run an
    agent loop — offering it in ``/models`` would be offering something that
    fails on the first turn.
    """
    models, err = free_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.tools], ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every model id, catalog order preserved — what ``/model`` should offer."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def cached_models() -> list[Model] | None:
    """The last-fetched catalog as :class:`Model` objects, or ``None``.

    ``/model`` runs on the UI thread and must not block on a fetch, so it reads
    whatever the last fetch left behind. A cold cache returns ``None`` and the
    caller falls back to its own list.
    """
    newest: tuple[list[Model], float] | None = None
    for entry in _CACHE.values():
        if newest is None or entry[1] > newest[1]:
            newest = entry
    return list(newest[0]) if newest else None


def cached_ids() -> list[str] | None:
    """The last-fetched ids, without touching the network."""
    models = cached_models()
    return [m.id for m in models] if models is not None else None


def clear_cache() -> None:
    """Drop every cached catalog. Called when the key changes."""
    _CACHE.clear()


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """The catalog's price for *model*, if the catalog knows it."""
    if not models:
        return None
    base = model.strip()
    for m in models:
        if m.id == base:
            return (m.price_in, m.price_out)
    return None


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    Deliberately loose — the gateway decides what is valid, not us — but strict
    enough to catch a pasted shell line or a bare word: an OpenRouter key is an
    ``sk-or-`` token of some length.
    """
    value = value.strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))
