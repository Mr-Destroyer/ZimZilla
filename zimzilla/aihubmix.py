"""AIHubMix — a hosted Anthropic-protocol gateway, as a ZimZilla source.

AIHubMix is a remote endpoint (``https://aihubmix.com``) that fronts 857
models behind one key. Measured on this box, not assumed:

* ``POST /v1/messages`` **is served natively**. It answers with a real
  Anthropic-shaped reply — ``content`` blocks of ``thinking`` / ``text`` /
  ``tool_use``, ``stop_reason`` of ``tool_use``, and a proper SSE stream of
  ``message_start`` … ``message_stop`` when ``stream: true``. There is no
  translation layer to write and none is written, which is what makes this a
  genuine source rather than a tool surface like LDXHub.
* ``GET /api/v1/models`` answers **with no key at all** — 857 models when this
  was written. The catalog is public, so the free list can be shown, and a
  model chosen, *before* the operator has set anything.
* The free models actually run. ``coding-glm-5-free`` and ``qwen3.8-flash-free``
  both answered a real request on a key with no credit balance — the paid ones
  returned ``403 "Your account balance is insufficient"``. So the free tier is
  usable on its own, which is the whole reason to add this source.

Two traps, measured rather than guessed:

* **Do not trust the ``tool_call`` field to mean "no tools".**
  ``qwen3.8-flash-free`` carries ``tool_call: null`` and still returns a
  ``tool_use`` block when handed a tool. Filtering the free list on that field
  — the obvious thing, and what OpenRouter's equivalent invites — would hide
  working models. It is recorded for display and never used to exclude.
* **The ``-free`` / ``:free`` suffix is not the free signal.** Six
  ``meta-llama/…:free`` and ``mistralai/…:free`` entries carry a non-zero
  0.02 price; they are *not* free. Meanwhile 50 entries are priced 0/0. The
  price is the truth and the suffix is a hint, so :attr:`Model.free` requires a
  successful price read and treats the suffix as corroboration only.

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

#: The canonical host. Matching is by this substring, so a trailing slash and any
#: subdomain path all count as the same source.
HOST = "aihubmix.com"

#: The SDK's ``base_url`` — and it is the PREFIX BEFORE ``/v1``, not the API root.
#: The Anthropic SDK appends ``/v1/messages`` to whatever it is given, so this
#: composes to ``https://aihubmix.com/v1/messages``, which is exactly where this
#: gateway serves the Messages protocol (measured: a real 403 balance error, not
#: a 404). The catalog is a *sibling* under the same version segment and is
#: spelled out in full at the call site, because it lives at ``/api/v1/models``
#: — a different prefix entirely.
DEFAULT_BASE_URL = "https://aihubmix.com"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = ZIMZILLA_HOME / "aihubmix" / "source"

#: A fetched catalog older than this is refetched rather than trusted.
CACHE_TTL = 300.0

#: Suffixes that *hint* at a free model. A hint only — see the module docstring.
FREE_SUFFIXES = ("-free", ":free")


@dataclass(frozen=True)
class Model:
    """One entry from AIHubMix's catalog."""

    id: str
    label: str = ""
    context_length: int | None = None
    max_output: int | None = None
    price_in: float = 0.0
    price_out: float = 0.0
    #: ``types`` verbatim — ``llm``, ``image_generation``, ``tts``, ``embedding``
    #: and combinations (``llm,search``). Only something carrying ``llm`` can
    #: answer a chat request, so this is what keeps image and audio models out of
    #: ``/model``.
    types: str = ""
    #: Whether the catalog *claims* tool calling. Advisory only: this is ``None``
    #: for models that demonstrably do call tools, so it is shown and never used
    #: to filter.
    tool_call: bool | None = None
    reasoning: bool = False
    #: Whether a price was actually **read**, not merely whether a pricing block
    #: was present. Without it a missing block would read as 0/0 and mark every
    #: unpriced model free — and this catalog has entries carrying a null price.
    priced: bool = False

    @property
    def free(self) -> bool:
        """Whether this model costs nothing.

        Requires a successful price read of 0/0, and *additionally* accepts the
        suffix when a price could not be read at all. The suffix alone is not
        enough — the ``meta-llama/…:free`` family carries a real 0.02 price and
        is not free — but a suffixed entry with no parseable price is better
        treated as free than as a paid model with an unknown cost.
        """
        if self.priced:
            return self.price_in == 0.0 and self.price_out == 0.0
        return self.id.endswith(FREE_SUFFIXES)

    @property
    def chat(self) -> bool:
        """Whether this model can answer a chat request at all."""
        return "llm" in self.types

    @property
    def tools(self) -> bool:
        """Whether the catalog *claims* tool support. Advisory, not a filter."""
        return bool(self.tool_call)

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
        if self.reasoning:
            bits.append("reasoning")
        if self.types and self.types != "llm":
            bits.append(self.types)
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


def is_aihubmix(base_url: str | None) -> bool:
    """Whether *base_url* points at AIHubMix."""
    return HOST in (base_url or "")


#: Generic name for the same test, so code that handles several gateways
#: uniformly can call ``mod.active_endpoint(url)`` without knowing which one it
#: holds. See ``_cmd_gateway_key`` in ui/app.py.
active_endpoint = is_aihubmix


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
    """An AIHubMix key already sitting in ``~/claude-source/<name>``.

    This is the ``source ~/claude-source/<name>`` idiom: the operator has
    already written a profile for Claude Code, so ZimZilla should use it rather
    than asking for the key again. Returns ``(key, model, path)`` for the first
    profile that points at AIHubMix and carries a token.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_aihubmix(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The AIHubMix key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at AIHubMix — i.e. the operator
         sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/aihubmix-api-setup``.
      3. Any AIHubMix profile under ``~/claude-source``.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged or
    returned to the transcript — only ``origin`` (a path, or "environment") is.
    """
    if is_aihubmix(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Write the key to ZimZilla's own AIHubMix profile, mode 600.

    The file is the same ``source`` format as every other profile — it can be
    sourced by a shell and read by ``load_credentials`` — so there is one
    credential format in the project, not four.
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
    """Point *cfg* at AIHubMix. Returns ``(origin, model)`` or ``None``.

    The one place that knows how to aim a session at this gateway, shared by
    ``/zim-aihubmix`` and the launch-time restore in ``__main__`` — so a
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
    """Whether a remembered AIHubMix choice should override *base_url*.

    False for an endpoint the operator pointed somewhere deliberate — this
    gateway itself, or any other non-local host. True for the local proxies and
    the built-in default, which is what the launcher leaves behind when it has
    no AIHubMix profile to source. This is what keeps a stale
    ``current-source`` from hijacking a session aimed at a different provider.
    """
    url = (base_url or "").strip()
    if is_aihubmix(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

#: Fetched catalogs, keyed by the token they were fetched with. The catalog is
#: public and identical for every caller, so the keyless fetch is cached under
#: "" and a keyed one under its token — a key change refetches rather than
#: serving a list fetched for a different account.
_CACHE: dict[str, tuple[list[Model], float]] = {}


def _parse_models(payload: dict) -> list[Model]:
    out: list[Model] = []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("model_id") or "").strip()
        if not mid:
            continue
        pricing = entry.get("pricing")
        # `priced` records a successful *read*, not the block's presence: some
        # entries carry a null price, and treating that as 0/0 would mark every
        # unpriced model free — the exact failure the flag exists to prevent.
        priced = False
        pin = pout = 0.0
        if isinstance(pricing, dict):
            try:
                pin = float(pricing.get("input"))
                pout = float(pricing.get("output"))
                priced = True
            except (TypeError, ValueError):
                priced = False

        def _int(key):
            val = entry.get(key)
            try:
                return int(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        tool_call = entry.get("tool_call")
        out.append(Model(
            id=mid,
            label=str(entry.get("model_name") or ""),
            context_length=_int("context_length"),
            max_output=_int("max_output"),
            price_in=pin,
            price_out=pout,
            types=str(entry.get("types") or ""),
            tool_call=bool(tool_call) if tool_call is not None else None,
            reasoning=bool(entry.get("reasoning")),
            priced=priced,
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch AIHubMix's catalog. Returns ``(models, error)``.

    **No key required** — the catalog is public, so ``token`` defaults to empty
    and the free list is available before ``/aihubmix-api-setup`` has run. A key
    is sent when one is supplied, because the listing should match what this
    session would actually be charged.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach AIHubMix" rather than take the session down.
    ``models`` is ``None`` on failure, ``[]`` for a genuinely empty catalog —
    the caller can tell the two apart.
    """
    token = (token or "").strip()
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    # The catalog lives at ``/api/v1/models`` — note the ``/api``, which the
    # SDK's prefix (``https://aihubmix.com``) does not carry. Messages are
    # ``{base}/v1/messages`` by the SDK's own appending; models are
    # ``{base}/api/v1/models`` by hand. Spelling both out is what stops one
    # being "fixed" into breaking the other.
    url = f"{DEFAULT_BASE_URL}/api/v1/models"
    headers = {"accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "key rejected (401/403) — check it with /aihubmix-api-setup"
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the models AIHubMix is currently giving away."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The free models this harness can actually drive.

    Free *and* able to answer a chat request — i.e. ``types`` carrying ``llm``.
    The filter is not decoration: of the 50 zero-priced entries, three are not
    chat models at all (an OCR engine, a transcription endpoint, a decision
    model), and offering those in ``/models`` would be offering something that
    fails on the first turn.

    Note what is deliberately **not** filtered: tool support. The catalog's
    ``tool_call`` field is ``null`` for ``qwen3.8-flash-free``, which then
    returns a real ``tool_use`` block when handed a tool — so filtering on it
    would hide working models. It is shown in the blurb instead.
    """
    models, err = free_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.chat], ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every chat-capable model id, catalog order preserved."""
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

    **The ``CACHE_TTL`` check is load-bearing** — the same trap OpenRouter hit.
    Without it a catalog fetched once reads as live for the rest of the session,
    and this gateway's free tiers turn over (they are promotional campaigns with
    expiry dates), so a stale list is exactly the wrong list this picker exists
    to avoid. An expired entry reads as ``None``, which callers already handle
    as "no catalog".
    """
    newest: tuple[list[Model], float] | None = None
    for entry in _CACHE.values():
        if newest is None or entry[1] > newest[1]:
            newest = entry
    if newest is None or (time.monotonic() - newest[1]) >= CACHE_TTL:
        return None
    return list(newest[0])


def cached_ids() -> list[str] | None:
    """The last-fetched chat-capable ids, without touching the network."""
    models = cached_models()
    if models is None:
        return None
    return [m.id for m in models if m.chat]


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
    enough to catch a pasted shell line or a bare word: an AIHubMix key is an
    ``sk-`` token of hex.
    """
    value = value.strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))
