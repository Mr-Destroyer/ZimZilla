"""UnoRouter — a hosted Anthropic-protocol gateway, as a ZimZilla source.

UnoRouter is a remote endpoint (``https://api.unorouter.com``) that fronts a few
hundred models behind one key. Measured on this box, not assumed:

* ``POST /v1/messages`` answers with an Anthropic-shaped 401 when no key is sent
  — i.e. the Messages protocol is served natively, so there is no translation
  layer to write and none is written. Note the shape of that URL against
  :data:`DEFAULT_BASE_URL`: the ``/v1`` is the SDK's, appended to the prefix it
  is handed. Passing ``https://api.unorouter.com/v1`` as the base composes to
  ``/v1/v1/messages`` and 404s on every turn while the catalog still works —
  the exact trap ``openrouter.py`` documents, and the reason this constant
  carries no path component.

**The catalog is two endpoints, and the obvious one is the wrong one.**
``GET /v1/models`` is thin: no pricing, no tool metadata, and it lists every
kind of model the gateway fronts — chat, image, embedding, TTS, STT — with the
free ones marked only by a ``:free`` suffix. Taking it at face value yields a
picker where ~97 entries look free and most cannot run a turn at all.

``GET /api/pricing`` is the real catalog, and it is **public** — 200 with no key
at all, so the free list is available before the operator has set anything.
Each entry carries ``is_free``, ``online``, and a ``metadata`` field holding
``supportsTools`` and ``outputModalities``. That is what makes the filter in
:func:`usable_free_models` possible: free **and** tool-calling **and**
text-output.

**The counts move, so they are measured rather than remembered.** Re-measured
against the live catalog on 2026-10-16: **230 entries, 97 free, 161 text-output,
46 free *and* usable here.** The 51 free entries the filter drops split as 36
non-text (image, embedding, speech) and 15 text-but-no-tools. Every one of those
numbers was different an hour earlier — 220/87/154/40, then 232/99/161/48 — and
they moved *again* between two runs minutes apart. That is the point: this
catalog is live, so any figure written into a docstring or a README is a
snapshot with a date on it, not a constant to be trusted. The filter is what
holds; the counts are what it returned on one afternoon.

**Two measured caveats, stated rather than hidden.** Both were found by sending
real tool-using requests to every usable free model:

* ``supportsTools`` is a strong filter but not a guarantee. Of 43 free models
  measured, 26 returned a real ``tool_use`` block and 17 did not — most of those
  17 answering in prose. Two of them (``gemma-4-26b:free``, ``lfm-2.5-2.6b:free``)
  emit their tool call as *text*, in their own non-Anthropic format
  (``<|tool_call>call:get_weather{...}<tool_call|>``), which this harness cannot
  read. A model that fails the first turn is a model to switch off, not a bug here.
  The failure is not confined to obscure entries: ``nemotron-3-super-120b-a12b:free``
  advertises tools, has a 262k context and answered a tool-using request with a
  complete ``end_turn`` in prose — it was briefly ranked second here on the
  strength of one early sample, and removed once it was re-measured.
* **The free tier is rate-limited and variable, and that is the constraint that
  bites first.** Repeating the same request, models that answered cleanly once
  came back ``429 rate_limit_exceeded`` or ``503 get_channel_failed`` on the next
  attempt; two separate sweeps of the same five models disagreed with each other
  on every single one. So a free model failing is more often "wait and retry"
  than "this model is broken", and **no small sample can rank these models** —
  the variance exceeds the differences between them. :data:`PREFERRED_FREE_MODELS`
  is therefore a short list of models *verified to call a tool at all*, ordered
  by a context tiebreak rather than by a reliability claim the data cannot
  support.

``metadata`` is a **JSON-encoded string**, not an object, so it is parsed rather
than read. A malformed one degrades to "unknown", never to an exception: this
runs inside a TUI command.

Two things make it different enough to live in its own module, exactly as
TokenHarbour and OpenRouter do:

* **The credential is set, not discovered.** There is no shipped profile — the
  operator supplies their own key with ``/unorouter-api-setup``. It is kept in a
  ``source``-format file of the same shape as every other profile, mode 600.
* **The catalog is live, public and priced**, so the free list is real time and
  a model can be chosen before a key exists.

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
HOST = "unorouter.com"

#: The SDK's ``base_url`` — and it is the PREFIX BEFORE ``/v1``, not the API root.
#: The Anthropic SDK appends ``/v1/messages`` to whatever it is given, so this
#: composes to ``https://api.unorouter.com/v1/messages``, which is where
#: UnoRouter serves the Messages protocol (a 401 without a key, i.e. the path
#: exists). Measured: ``https://api.unorouter.com/v1`` as the base 404s with
#: ``POST /v1/v1/messages``.
DEFAULT_BASE_URL = "https://api.unorouter.com"

#: The real catalog. Public — no key required — which is why the free list works
#: before ``/unorouter-api-setup`` has run. The ``/v1/models`` endpoint is
#: deliberately not used: it carries no pricing and no tool metadata.
PRICING_URL = f"{DEFAULT_BASE_URL}/api/pricing"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = ZIMZILLA_HOME / "unorouter" / "source"

#: A fetched catalog older than this is refetched rather than trusted. The free
#: tier turns over, so a stale list is exactly the wrong list this picker exists
#: to avoid.
CACHE_TTL = 300.0

#: The suffix UnoRouter puts on a model that costs nothing.
FREE_SUFFIX = ":free"

#: The output modality a model must offer to be usable by this harness.
TEXT_MODALITY = "text"


@dataclass(frozen=True)
class Model:
    """One entry from UnoRouter's pricing catalog."""

    id: str
    label: str = ""
    context_length: int | None = None
    max_output: int | None = None
    #: Whether the catalog says this model can call tools. What makes it usable
    #: by this harness at all — a model that cannot call a tool cannot drive an
    #: agent loop. A strong filter, not a guarantee: see the module docstring.
    tools: bool = False
    #: The catalog's own ``is_free``, cross-checked against the ``:free`` suffix.
    free: bool = False
    reasoning: bool = False
    online: bool = False
    #: Output modalities the catalog reports. ``("text",)`` is what a chat model
    #: offers; an image or embedding model offers something else, and is dropped
    #: by :func:`usable_free_models` however free it is.
    modalities: tuple[str, ...] = ()

    @property
    def chat(self) -> bool:
        """Whether this model produces text — i.e. can answer a Messages turn."""
        return TEXT_MODALITY in self.modalities

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits = []
        if self.free:
            bits.append("free")
        if not self.tools:
            bits.append("no tools")
        if self.reasoning:
            bits.append("reasoning")
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
        if not self.online:
            bits.append("offline")
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


def is_unorouter(base_url: str | None) -> bool:
    """Whether *base_url* points at UnoRouter."""
    return HOST in (base_url or "")


#: Generic name for the same test, so code that handles several gateways
#: uniformly can call ``mod.active_endpoint(url)`` without knowing which one it
#: holds. See ``_cmd_gateway_key`` in ui/app.py.
active_endpoint = is_unorouter


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
    """A UnoRouter key already sitting in ``~/claude-source/<name>``.

    This is the ``source ~/claude-source/<name>`` idiom: the operator has
    already written a profile for Claude Code, so ZimZilla should use it rather
    than asking for the key again. Returns ``(key, model, path)`` for the first
    profile that points at UnoRouter and carries a token.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_unorouter(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The UnoRouter key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at UnoRouter — i.e. the operator
         sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/unorouter-api-setup``.
      3. Any UnoRouter profile under ``~/claude-source``.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged or
    returned to the transcript — only ``origin`` (a path, or "environment") is.
    """
    if is_unorouter(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Write the key to ZimZilla's own UnoRouter profile, mode 600.

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
    """Point *cfg* at UnoRouter. Returns ``(origin, model)`` or ``None``.

    The one place that knows how to aim a session at this gateway, shared by
    ``/zim-unorouter`` and the launch-time restore in ``__main__`` — so a
    session started by the command and one started from the remembered source
    end up configured identically.

    ``api_key`` is cleared rather than left as-is: the client sends
    ``auth_token`` when it is set, and a stale ``api_key`` from another provider
    would be a credential for the wrong service sitting in memory.

    With no model named by the profile, the session is aimed at a **free** model
    rather than left on whatever the previous source called its model — see
    :func:`default_free_model`. Returns ``None`` when no key can be found
    anywhere, and leaves *cfg* alone.
    """
    creds = load_credentials()
    if creds is None:
        return None
    token, model, origin = creds
    cfg.base_url = DEFAULT_BASE_URL
    cfg.auth_token = token
    cfg.api_key = ""
    if not model:
        model = default_free_model() or ""
    if model:
        cfg.model = model
    return origin, model


def applies_to_default_endpoint(base_url: str | None) -> bool:
    """Whether a remembered UnoRouter choice should override *base_url*.

    False for an endpoint the operator pointed somewhere deliberate — this
    gateway itself, or any other non-local host. True for the local proxies and
    the built-in default, which is what the launcher leaves behind when it has
    no UnoRouter profile to source. This is what keeps a stale ``current-source``
    from hijacking a session aimed at a different provider.
    """
    url = (base_url or "").strip()
    if is_unorouter(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

#: Fetched catalogs, keyed by the token they were fetched with. UnoRouter's
#: catalog is public and identical for every caller, so the keyless fetch is
#: cached under "" and a keyed one under its token — a key change refetches
#: rather than serving a list fetched for a different account.
_CACHE: dict[str, tuple[list[Model], float]] = {}


def _metadata(entry: dict) -> dict:
    """The entry's ``metadata``, however it arrived.

    It is a **JSON-encoded string** in the payload, so it is parsed. A dict
    (already decoded by some future revision), ``None``, or junk all degrade to
    an empty mapping: this runs inside a TUI command, and a metadata blob that
    cannot be read is a model with unknown capabilities, not a crashed catalog.
    """
    raw = entry.get("metadata")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_models(payload: dict) -> list[Model]:
    """Every entry in the pricing payload, in the order the API returned them.

    ``data`` is a mapping keyed by model name, so it is walked by value. A
    malformed entry is skipped rather than allowed to take the catalog down.
    """
    data = payload.get("data")
    entries = data.values() if isinstance(data, dict) else (data or [])
    out: list[Model] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("model_name") or entry.get("id") or "").strip()
        if not mid:
            continue
        meta = _metadata(entry)
        mods = meta.get("outputModalities")
        modalities = (tuple(str(m) for m in mods if isinstance(m, str))
                      if isinstance(mods, list) else ())
        # `is_free` is the catalog's own answer and was measured to agree with
        # the `:free` suffix exactly; both are checked so a model zeroed out
        # without renaming is still caught.
        out.append(Model(
            id=mid,
            label=str(entry.get("description") or "")[:120],
            context_length=_as_int(meta.get("contextWindow")
                                   or meta.get("maxInputTokens")),
            max_output=_as_int(meta.get("maxOutputTokens")),
            tools=bool(meta.get("supportsTools")),
            free=bool(entry.get("is_free")) or mid.endswith(FREE_SUFFIX),
            reasoning=bool(meta.get("isReasoning")),
            online=bool(entry.get("online", True)),
            modalities=modalities,
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch UnoRouter's pricing catalog. Returns ``(models, error)``.

    **No key required** — the catalog is public, so ``token`` defaults to empty
    and the free list is available before ``/unorouter-api-setup`` has run. A
    key is sent when one is supplied, so the listing matches what this session
    would actually be charged.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach UnoRouter" rather than take the session down.
    ``models`` is ``None`` on failure, ``[]`` for a genuinely empty catalog —
    the caller can tell the two apart.
    """
    token = (token or "").strip()
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    # The catalog lives at ``/api/pricing`` — a sibling of the SDK's
    # ``/v1/messages``, not under the ``/v1`` prefix, so it is spelled out in
    # PRICING_URL rather than composed from DEFAULT_BASE_URL. The two are
    # different roots on purpose.
    headers = {"accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["x-api-key"] = token
    req = urllib.request.Request(PRICING_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "key rejected (401/403) — check it with /unorouter-api-setup"
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the models UnoRouter is currently giving away."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The free models this harness can actually drive.

    Free **and** tool-calling **and** text-output. Both extra tests earn their
    place:

    * Without the tool test, a model that cannot call a tool is offered and then
      fails on the first turn — an agent loop with no way to act.
    * Without the modality test the list fills with what the gateway also fronts
      for free: SDXL image generators, embedding models, Whisper transcription
      and TTS voices. All are genuinely free, none can answer a Messages turn.
      Measured 2026-10-16: 97 free entries, 46 usable here — the filter drops 36
      for modality and 15 for missing tool support. Those counts shift between
      runs; see the module docstring.
    """
    models, err = free_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.tools and m.chat], ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every text model id, catalog order preserved — what ``/model`` should offer.

    Text-output models, not only the free ones: an operator who names a paid
    model deliberately should not have it refused as "not served by unorouter"
    against a list that was filtered to the free tier. The image, embedding and
    speech entries are still dropped — they cannot answer a turn, so offering
    them would be offering something that fails.
    """
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m.id for m in models if m.chat], ""


#: Free models **verified to call a tool at all**, in the order a fresh switch
#: should try them. Every name here was sent a real tool-using request and
#: returned an actual ``tool_use`` block at least once — that is the bar, and it
#: is a real one, because most of the catalog's free models never clear it.
#:
#: **The order is not a reliability ranking, and the code should not pretend it
#: is.** Two sweeps were run on 2026-10-16, three spaced attempts per model each
#: time. They did not agree:
#:
#:              first sweep      confirmation
#:   gemini-3.6-flash:free      3/3      2/3
#:   qwen3.8-27b:free           3/3      2/3
#:   glm-5.3-flash:free         3/3      1/3
#:   deepseek-v4.1-flash:free   1/3      2/3
#:   gpt-oss-20b:free           1/3      2/3
#:
#: Nothing reproduced, and the failures are a mix of the gateway erroring out and
#: the model answering in prose. Three attempts cannot separate these models: the
#: run-to-run variance is larger than the difference between them. So the honest
#: reading of that table is not "gemini is the best" but **"all five can call a
#: tool, and none does so dependably"** — which is a property of the free tier,
#: not of any one model, and is the constraint the module docstring describes.
#:
#: What the order *does* encode is a tiebreak on **context** among models that
#: showed no measurable difference in willingness: ``gemini-3.6-flash:free``
#: leads on its 1M window over ``qwen3.8-27b:free``'s 64k, which is tight for the
#: tool loop this harness runs. Below those two the order is a preference, not a
#: finding.
#:
#: A name that has since gone away falls through to the next, then to the
#: catalog's own order — load-bearing rather than theoretical, since
#: ``glm-5.3-flash:free`` was gone from the catalog within the hour it was
#: measured.
PREFERRED_FREE_MODELS = (
    "gemini-3.6-flash:free",
    "glm-5.3-flash:free",
    "qwen3.8-27b:free",
    "deepseek-v4.1-flash:free",
    "gpt-oss-20b:free",
)


def default_free_model(token: str = "", *, timeout: float = 8.0) -> str | None:
    """A free model to use when the operator has chosen none.

    Exists because the alternative was measured to fail on the other gateways:
    with no saved preference, :func:`apply_to` left ``cfg.model`` on whatever the
    previous source had named, and switching here then sent *that* provider's
    model name to UnoRouter — a name it does not serve. Naming a free model
    instead means a fresh switch spends nothing and works, which is the whole
    reason a free tier is worth surfacing.

    Cache first: this runs inside :func:`apply_to`, which runs at startup and on
    every source switch, so the common case must not touch the network. Only
    with a cold cache is a fetch attempted, and then bounded by *timeout* so a
    slow gateway delays a launch rather than hanging it.

    Returns ``None`` when nothing free is on offer, leaving the caller's model
    untouched — an empty catalog is not a reason to invent a name.
    """
    models = cached_models()
    if models is None:
        models, _ = fetch_models(token, timeout=timeout)
    usable = [m for m in (models or []) if m.free and m.tools and m.chat]
    if not usable:
        return None
    present = {m.id for m in usable}
    for preferred in PREFERRED_FREE_MODELS:
        if preferred in present:
            return preferred
    return usable[0].id


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

    **The ``CACHE_TTL`` check is load-bearing**, exactly as it is in the other
    gateway modules. ``fetch_models`` has always refused to *reuse* an entry past
    its TTL, but reading it back regardless reported a catalog fetched once as
    live for the rest of the session. A free tier turns over, so a stale list is
    the wrong list this picker exists to avoid. An expired entry reads as
    ``None``, which callers already handle as "no catalog".
    """
    newest: tuple[list[Model], float] | None = None
    for entry in _CACHE.values():
        if newest is None or entry[1] > newest[1]:
            newest = entry
    if newest is None or (time.monotonic() - newest[1]) >= CACHE_TTL:
        return None
    return list(newest[0])


def cached_ids() -> list[str] | None:
    """The last-fetched text model ids, without touching the network."""
    models = cached_models()
    return [m.id for m in models if m.chat] if models is not None else None


def clear_cache() -> None:
    """Drop every cached catalog. Called when the key changes."""
    _CACHE.clear()


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok for *model*, when the catalog can answer.

    Only the **free** tier can be priced from this catalog. UnoRouter publishes
    ``model_ratio``/``completion_ratio`` — dimensionless multipliers against the
    gateway's own credit unit — not dollars per million tokens, so turning them
    into a rate would be inventing a number and printing it as if it were the
    gateway's own. A paid model therefore returns ``None`` and the caller falls
    back to this harness's local estimate table, which is labelled as an
    estimate. A free model returns ``(0.0, 0.0)``, which is exact, and is what
    makes the note read ``free`` rather than ``$0.00/$0.00``.
    """
    found = model_for(model, models)
    if found is None or not found.free:
        return None
    return (0.0, 0.0)


def model_for(model: str, models: list[Model] | None) -> Model | None:
    """The catalog's entry for *model*, if the catalog knows it."""
    if not models:
        return None
    base = model.strip()
    for m in models:
        if m.id == base:
            return m
    return None


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    Deliberately loose — the gateway decides what is valid, not us — but strict
    enough to catch a pasted shell line or a bare word: a UnoRouter key is a
    long ``sk-`` token.
    """
    value = value.strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))
