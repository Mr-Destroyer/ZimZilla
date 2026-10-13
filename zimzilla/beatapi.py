"""BeatAPI — a hosted Anthropic-protocol gateway, as a ZimZilla source.

BeatAPI is not a local LiteLLM proxy like Logfare and Token Juice: it is a
remote endpoint (``https://api.beatapi.io``) that serves the Anthropic Messages
protocol directly. Measured, not assumed — ``POST /v1/messages`` answers 200
against this box's key, accepting ``claude-*``, ``deepseek-*`` and ``gpt-*``
ids alike, so there is no translation layer to write and none is written.

Two things make it different enough to live in its own module, exactly as
TokenHarbour does:

* **The credential is set, not discovered.** There is no shipped profile — the
  operator supplies their own key with ``/beatapi-api-setup``. It is kept in a
  ``source``-format file of the same shape as every other profile, mode 600, so
  it is read by the same parser and never printed.
* **The catalog is live and gated.** ``GET /v1/models`` needs the key (401
  without it) and answers 43 models on this box, 8 of them free.

The free tier is rate-limited to **one request per minute per account** — that
number comes from the 429 body, not from documentation — so a session pointed
here is for occasional work, not a long agentic run. The paid models need a
topped-up balance: this key holds 0 credit, so a request against one returns
402 ``insufficient_credits``.

Nothing here echoes a key. A profile a shell already sourced — the
``source ~/claude-source/<name>`` idiom — is honoured too: if
``ANTHROPIC_BASE_URL`` already points at BeatAPI, that key is used and nothing
is written.
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

#: The canonical host. Matching is by this substring, so ``https://api.beatapi.io``,
#: a trailing slash and any subdomain path all count as the same source.
HOST = "api.beatapi.io"
DEFAULT_BASE_URL = "https://api.beatapi.io"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = ZIMZILLA_HOME / "beatapi" / "source"

#: A fetched catalog older than this is refetched rather than trusted, so the
#: free list stays current without hitting the network on every keystroke.
CACHE_TTL = 300.0

#: The suffix BeatAPI puts on a model that costs nothing.
FREE_SUFFIX = "-free"

#: The free tier's published limit, from the 429 body. Shown with the free list
#: so an operator does not point a long run at it and wonder why it stalls.
FREE_RATE_NOTE = "1 request/minute per account"

#: Sent on every catalog fetch. See the comment in ``fetch_models``: Cloudflare
#: bans urllib's default signature outright, so this is load-bearing, not
#: decoration.
USER_AGENT = "zimzilla/1.0 (+https://github.com/zimzilla)"


@dataclass(frozen=True)
class Model:
    """One entry from BeatAPI's catalog."""

    id: str
    owned_by: str = ""
    context_length: int | None = None
    price_in: float = 0.0
    price_out: float = 0.0
    #: Whether a price was actually **read** for this entry, not merely whether
    #: a pricing block was present. BeatAPI publishes no pricing block today,
    #: and without this every entry would compare 0.0 == 0.0 and read as free
    #: the moment the suffix check missed. Tracking a successful parse rather
    #: than the block's existence closes the same hole for a block that arrives
    #: carrying values that cannot be read.
    priced: bool = False

    @property
    def free(self) -> bool:
        """Whether this model is free.

        The ``-free`` id suffix is the signal that exists: BeatAPI's catalog
        publishes **no pricing block at all**, so unlike TokenHarbour and
        OpenRouter there is no price to read. A zero price is still accepted
        when one is present — it costs nothing and covers the day the catalog
        grows a pricing field — but the suffix is what actually decides today,
        and claiming otherwise in a comment would be a claim the API does not
        support.
        """
        if self.id.endswith(FREE_SUFFIX):
            return True
        return self.priced and self.price_in == 0.0 and self.price_out == 0.0

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits = []
        if self.free:
            bits.append("free")
        if self.owned_by:
            bits.append(self.owned_by)
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
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


def is_beatapi(base_url: str | None) -> bool:
    """Whether *base_url* points at BeatAPI."""
    return HOST in (base_url or "")


#: Generic name for the same test, so code that handles several gateways
#: uniformly can call ``mod.active_endpoint(url)`` without knowing which one it
#: holds. See ``_cmd_gateway_key`` in ui/app.py.
active_endpoint = is_beatapi


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
    """A BeatAPI key already sitting in ``~/claude-source/<name>``.

    This is the ``source ~/claude-source/<name>`` idiom: the operator has
    already written a profile for Claude Code, so ZimZilla should use it rather
    than asking for the key again. Returns ``(key, model, path)`` for the first
    profile that points at BeatAPI and carries a token.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_beatapi(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The BeatAPI key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at BeatAPI — i.e. the operator
         sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/beatapi-api-setup``.
      3. Any BeatAPI profile under ``~/claude-source``.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged or
    returned to the transcript — only ``origin`` (a path, or "environment") is.
    """
    if is_beatapi(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Write the key to ZimZilla's own BeatAPI profile, mode 600.

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
    """Point *cfg* at BeatAPI. Returns ``(origin, model)`` or ``None``.

    The one place that knows how to aim a session at this gateway, shared by
    ``/zim-beatapi`` and the launch-time restore in ``__main__`` — so a session
    started by the command and one started from the remembered source end up
    configured identically.

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
    """Whether a remembered BeatAPI choice should override *base_url*.

    False for an endpoint the operator pointed somewhere deliberate — this
    gateway itself, or any other non-local host. True for the local proxies and
    the built-in default, which is what the launcher leaves behind when it has
    no BeatAPI profile to source. This is what keeps a stale ``current-source``
    from hijacking a session aimed at a different provider.
    """
    url = (base_url or "").strip()
    if is_beatapi(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

#: Fetched catalogs, keyed by the token they were fetched with, so a key change
#: does not serve the previous key's list. (models, monotonic timestamp).
_CACHE: dict[str, tuple[list[Model], float]] = {}


def _is_cloudflare_block(e: urllib.error.HTTPError) -> bool:
    """Whether an HTTP error is Cloudflare refusing the client at the edge.

    BeatAPI is fronted by Cloudflare, which answers 403 with error 1010
    (``browser_signature_banned``) for a client whose signature it dislikes —
    before the API sees the request, so the key is never examined. Reported as a
    key rejection it would be actively misleading; this tells the two apart.
    """
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — a diagnostic must not itself raise
        return False
    return "1010" in body or "browser_signature_banned" in body


def _parse_models(payload: dict) -> list[Model]:
    out: list[Model] = []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        # BeatAPI's catalog has no pricing block today; read one if it appears,
        # so `free` degrades to the suffix check rather than crashing.
        pricing = entry.get("pricing") or {}
        # `priced` records a successful *read*, not the block's presence — see
        # the field's comment.
        priced = False
        pin = pout = 0.0
        if pricing:
            try:
                pin = float(pricing.get("input_usd_per_1m"))
                pout = float(pricing.get("output_usd_per_1m"))
                priced = True
            except (TypeError, ValueError):
                priced = False
        ctx = entry.get("context_length")
        try:
            ctx = int(ctx) if ctx is not None else None
        except (TypeError, ValueError):
            ctx = None
        out.append(Model(
            id=mid,
            owned_by=str(entry.get("owned_by") or ""),
            context_length=ctx,
            price_in=pin,
            price_out=pout,
            priced=priced,
        ))
    return out


def fetch_models(token: str, *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch BeatAPI's catalog. Returns ``(models, error)``.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach BeatAPI" rather than take the session down.
    ``models`` is ``None`` on failure, ``[]`` for a genuinely empty catalog —
    the caller can tell the two apart.
    """
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    url = f"{DEFAULT_BASE_URL}/v1/models"
    req = urllib.request.Request(url, headers={
        # Send both, the way every Anthropic-protocol client does: the gateway
        # accepts either, and which one it honours is its business.
        "Authorization": f"Bearer {token}",
        "x-api-key": token,
        "anthropic-version": "2023-06-01",
        "accept": "application/json",
        # NOT optional. BeatAPI sits behind Cloudflare, which bans the default
        # urllib signature with error 1010 ("browser_signature_banned") — a 403
        # raised before the key is ever examined. Measured on this box: no UA
        # gives 403, any explicit UA (curl, a browser string, this one) gives
        # 200 and the catalog. Without this every fetch fails and the failure
        # reads like a bad key.
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # A Cloudflare block is not a credential problem, and saying it is would
        # send the operator to re-check a key that was never looked at. Detect
        # it by its own error code so the two 403s stay distinguishable.
        if e.code == 403 and _is_cloudflare_block(e):
            return None, (f"{HOST} refused the request at its edge (Cloudflare "
                          "1010) — not a key problem; the catalog is unreachable "
                          "from this client")
        if e.code in (401, 403):
            return None, "key rejected (401/403) — check it with /beatapi-api-setup"
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def free_models(token: str, **kw) -> tuple[list[Model] | None, str]:
    """Only the models BeatAPI is currently giving away."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def model_ids(token: str, **kw) -> tuple[list[str] | None, str]:
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
    enough to catch a pasted shell line or a bare word: a BeatAPI key is an
    ``sk-`` token of some length.
    """
    value = value.strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))
