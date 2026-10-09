"""OpenCode Zen — a hosted Anthropic-protocol gateway, as a ZimZilla source.

OpenCode Zen is not a local LiteLLM proxy like Logfare and Token Juice: it is a
remote endpoint (``https://opencode.ai/zen``) that speaks the same Anthropic
Messages protocol, so ZimZilla can talk to it with no proxy in between. It is
the same shape of thing as :mod:`zimzilla.tokenharbour`, and this module is
deliberately its twin — the two differ only in the details below, and every
comment here that repeats TokenHarbour's says so on purpose, so the next reader
can diff the pair instead of learning a second design.

* **The credential is set, not discovered.** There is no shipped profile — the
  operator supplies their own key with ``/opencode-api-setup``. It is kept in a
  ``source``-format file of the same shape as every other profile, mode 600, so
  it is read by the same parser and never printed.
* **The catalog is live, and public.** ``/zen/v1/models`` answers with no key at
  all, so ``/opencode`` can list the free models before the operator has set one.
  A model is free when its id ends ``-free``; the endpoint carries no pricing, so
  the suffix is the only signal there is (unlike TokenHarbour, which also zeroes
  a price field).
* **Free models are reachable on the Anthropic endpoint.** OpenCode's docs group
  the ``-free`` ids under the OpenAI ``chat/completions`` route, but the gateway
  routes them on ``/v1/messages`` too, which is what lets ZimZilla use them with
  its Anthropic client and no translation layer.

Nothing here echoes a key. A profile that a shell already sourced — the
``source ~/claude-source/<name>`` idiom — is honoured too: if
``ANTHROPIC_BASE_URL`` already points at OpenCode Zen, that key is used and
nothing is written.
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

#: The canonical host. Matching is by this substring, so ``https://opencode.ai``,
#: ``https://opencode.ai/zen`` and a trailing slash all count as the same source.
HOST = "opencode.ai"
#: The base URL the Anthropic client is pointed at. The SDK appends
#: ``/v1/messages``, so the result is ``https://opencode.ai/zen/v1/messages`` —
#: which is exactly the route OpenCode documents for Anthropic-protocol calls.
DEFAULT_BASE_URL = "https://opencode.ai/zen"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = ZIMZILLA_HOME / "opencode" / "source"

#: A fetched catalog older than this is refetched rather than trusted, so the
#: free list stays "real time" without hitting the network on every keystroke.
CACHE_TTL = 300.0

#: The suffix OpenCode Zen puts on a model that costs nothing.
FREE_SUFFIX = "-free"

#: OpenCode Zen sits behind Cloudflare, which rejects urllib's default
#: ``Python-urllib/3.x`` agent outright with "error code: 1010" — a 403, not a
#: 200 — so the catalog is unreachable without a User-Agent of our own. Any
#: explicit agent is accepted; this one names the client honestly.
USER_AGENT = "zimzilla/1.0 (+https://opencode.ai/zen)"


@dataclass(frozen=True)
class Model:
    """One entry from OpenCode Zen's catalog.

    The live endpoint returns only ``id``/``object``/``created``/``owned_by``, so
    there is no price, tier or context to carry — unlike TokenHarbour's richer
    listing. ``label`` is derived, so the picker still has something to show.
    """

    id: str
    label: str = ""
    tier: str = ""
    context_length: int | None = None
    price_in: float = 0.0
    price_out: float = 0.0
    tool_call: bool = True

    @property
    def free(self) -> bool:
        """Whether OpenCode Zen marks this model free.

        The ``-free`` suffix is the only signal OpenCode Zen gives: the listing
        carries no pricing, so unlike TokenHarbour there is no zero price to fall
        back on.

        Marked free is not the same as usable from here. Zen gates its free tier
        to its own client at the gateway — a request for one of these models
        answers ``403 FreeTierError: OpenCode's free tier can only be used from
        within OpenCode``, whatever the credential or User-Agent (verified
        against four agents, including ``opencode/0.0.1``). So this flag is
        honest about the listing and says nothing about whether a turn will run;
        a session on one of these needs a funded account and a paid model.
        """
        return self.id.endswith(FREE_SUFFIX)

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits = []
        if self.free:
            bits.append("free")
        if self.tier:
            bits.append(self.tier)
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


def is_opencode(base_url: str | None) -> bool:
    """Whether *base_url* points at OpenCode Zen."""
    return HOST in (base_url or "")


def _read_exports(path: Path) -> dict[str, str]:
    """Parse ``export VAR="value"`` lines from a profile, without a shell.

    The same shape every other profile uses, so an OpenCode Zen profile written
    by hand and one written by ``/opencode-api-setup`` read identically.
    """
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
    """An OpenCode Zen key already sitting in ``~/claude-source/<name>``.

    This is the ``source ~/claude-source/<name>`` idiom: the operator has already
    written a profile for Claude Code, so ZimZilla should simply use it rather
    than asking for the key again. Returns ``(key, model, path)`` for the first
    profile that points at OpenCode Zen and carries a token.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_opencode(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The OpenCode Zen key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at OpenCode Zen — i.e. the operator
         sourced a profile before launching. This is the case the feature exists
         for: ``source ~/claude-source/<name> && zimzilla`` should just work.
      2. ZimZilla's own profile, written by ``/opencode-api-setup``.
      3. Any OpenCode Zen profile under ``~/claude-source``.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged or
    returned to the transcript — only ``origin`` (a path, or "environment") is.
    """
    if is_opencode(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Write the key to ZimZilla's own OpenCode Zen profile, mode 600.

    The file is the same ``source`` format as every other profile — it can be
    sourced by a shell, and read by ``load_credentials`` — so there is one
    credential format in the project, not two.
    """
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    # The key goes in ANTHROPIC_API_KEY, not ANTHROPIC_AUTH_TOKEN. The Anthropic
    # SDK maps api_key to the ``x-api-key`` header and auth_token to
    # ``Authorization: Bearer``; OpenCode Zen reads only ``x-api-key`` and
    # ignores Bearer entirely, so a token here would be a key the gateway never
    # looks at. AUTH_TOKEN is emptied rather than left alone because the SDK
    # sends BOTH when both are set — a stale one from another provider would put
    # a second, wrong credential on the wire.
    body = (
        f'export ANTHROPIC_BASE_URL="{DEFAULT_BASE_URL}/"\n'
        'export ANTHROPIC_AUTH_TOKEN=""\n'
        f'export ANTHROPIC_MODEL="{model}"\n'
        f'export ANTHROPIC_API_KEY="{token}"\n'
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
    """Point *cfg* at OpenCode Zen. Returns ``(origin, model)`` or ``None``.

    The one place that knows how to aim a session at this gateway, shared by the
    ``/opencode`` command and the launch-time restore in ``__main__`` — so a
    session started by the command and one started from the remembered source end
    up configured identically.

    The key goes in ``api_key`` and ``auth_token`` is cleared, because the SDK
    sends ``x-api-key`` for the former and ``Authorization: Bearer`` for the
    latter — and Zen reads only ``x-api-key``. A token here would be a credential
    the gateway never looks at; leaving a stale one in place would put a second,
    wrong credential on the wire.

    Returns ``None`` when no key can be found anywhere, and leaves *cfg* alone.
    """
    creds = load_credentials()
    if creds is None:
        return None
    token, model, origin = creds
    cfg.base_url = DEFAULT_BASE_URL
    cfg.api_key = token
    cfg.auth_token = ""
    if model:
        cfg.model = model
    return origin, model


def applies_to_default_endpoint(base_url: str | None) -> bool:
    """Whether a remembered OpenCode Zen choice should override *base_url*.

    False for an endpoint the operator pointed somewhere deliberate — the hosted
    gateway itself, or any other non-local host. True for the local proxies and
    the built-in default, which is what the launcher leaves behind when it has no
    OpenCode Zen profile to source. This is what keeps a stale ``current-source``
    from hijacking a session aimed at a different provider.
    """
    url = (base_url or "").strip()
    if is_opencode(url):
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

#: The key used for a fetch made before the operator has set one. The endpoint is
#: public, so an empty token is a legitimate cache key rather than a bug — and
#: keeping it distinct from a real token is what stops a keyed fetch from being
#: served the anonymous list.
ANON = ""


def _parse_models(payload: dict) -> list[Model]:
    out: list[Model] = []
    for entry in payload.get("data") or []:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        out.append(Model(
            id=mid,
            label=str(entry.get("label") or ""),
            tier=str(entry.get("tier") or ""),
            context_length=entry.get("context_length"),
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch OpenCode Zen's catalog. Returns ``(models, error)``.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach OpenCode Zen" rather than take the session
    down. ``models`` is ``None`` on failure, ``[]`` for a genuinely empty
    catalog — the caller can tell the two apart.

    The endpoint is public, so *token* is optional and only ever sent when
    present: listing models works before a key is set, which is what lets
    ``/opencode`` show the free list to an operator who has not set one yet.
    """
    cache_key = token or ANON
    if use_cache:
        hit = _CACHE.get(cache_key)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    url = f"{DEFAULT_BASE_URL}/v1/models"
    headers = {"accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        # Send both, the way every Anthropic-protocol client does: the gateway
        # accepts either, and which one it honours is its business.
        headers["Authorization"] = f"Bearer {token}"
        headers["x-api-key"] = token
        headers["anthropic-version"] = "2023-06-01"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # A 401/403 is only about the key when one was actually sent: the
        # catalog endpoint is public, so a refusal on a tokenless fetch is the
        # gateway's edge turning us away, not a bad credential.
        if token and e.code in (401, 403):
            return None, "key rejected (401/403) — check it with /opencode-api-setup"
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"

    models = _parse_models(payload)
    _CACHE[cache_key] = (models, time.monotonic())
    return models, ""


def free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the models OpenCode Zen is currently giving away."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every model id, catalog order preserved — what ``/model`` should offer."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def cached_models() -> list[Model] | None:
    """The last-fetched catalog as :class:`Model` objects, or ``None``.

    ``/model`` runs on the UI thread and must not block on a fetch, so it reads
    whatever the last ``/opencode`` (or a warm cache) left behind. A cold cache
    returns ``None`` and the caller falls back to its own list.
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
    """The catalog's price for *model*, if the catalog knows it.

    OpenCode Zen's listing carries no pricing, so this is always ``None`` for a
    known model — the caller falls through to the local estimate table. It exists
    so the two hosted gateways present the same interface to ``/model``.
    """
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
    enough to catch a pasted shell line or a bare word: an OpenCode Zen key is a
    long token (``sk-…``), with no whitespace.
    """
    value = value.strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))
