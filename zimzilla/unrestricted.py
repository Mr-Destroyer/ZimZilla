"""unrestricted.sh — an uncensored hosted gateway, as a ZimZilla source.

A remote endpoint (``https://api.unrestricted.sh``) that fronts a small, priced
catalogue of uncensored models behind one key. Measured on this box,
2026-11-08, against the live endpoint with a real key:

* ``POST /v1/messages`` answers with an **Anthropic-shaped 401** when no key is
  sent — ``{"type":"error","error":{"type":"authentication_error",...}}`` — so
  the Messages protocol is served natively and there is no translation layer to
  write. Note the shape of that URL against :data:`DEFAULT_BASE_URL`: the
  ``/v1`` is the SDK's, appended to the prefix it is handed. Passing
  ``https://api.unrestricted.sh/v1`` as the base composes to
  ``/v1/v1/messages`` and 404s on every turn while the catalogue still works —
  the exact trap ``unorouter.py`` and ``openrouter.py`` both document, and the
  reason this constant carries no path component.

* **The catalogue is public and priced in dollars.** ``GET /v1/models`` answers
  **200 with no key at all**, and the payload is byte-identical with and without
  one (diffed, not assumed), so the free list is available before
  ``/unrestricted-api-setup`` has run. Each entry carries ``pricing`` with
  ``input``/``cached_input``/``output`` **per million tokens** — real rates, not
  the dimensionless credit ratios UnoRouter publishes — plus ``context_length``,
  ``max_output_tokens``, ``zero_data_retention`` and ``partner_hosted``. Four
  models were listed when this was written.

* **A zero price does not mean callable, and that is the finding this module is
  built around.** ``deepseek-v4.1-flash-uncensored`` was priced ``0/0`` in the
  catalogue and answered ``400`` on a fresh account:

      DeepSeek V4.1 Flash 1017 Uncensored is for paid accounts. Add credit to
      use it.

  So ``free`` here is a *price*, and the account's balance decides whether a
  model answers at all. Measured on a new account with the $0.50 welcome
  credit: of the four listed models, **one** answered — ``qwen3.8-27b-uncensored``
  — and the other three refused with that 400. A picker that trusted the price
  column would have offered three models that cannot run a turn, which is why
  :func:`first_callable` exists and why the switch uses it rather than the
  catalogue's own order.

* **The price column moves, so it is read rather than remembered.** An hour
  after the reading above, the same catalogue re-priced
  ``deepseek-v4.1-flash-uncensored`` at ``$0.15/$0.60`` — so the free list went
  from one entry to **none** while this module was being written, and the model
  still refused with the same 400. Two consequences are load-bearing here:
  :func:`default_free_model` falls back to the *cheapest* model rather than
  returning ``None`` (a ``None`` would leave the previous provider's model name
  in place, and this gateway silently reroutes an unknown id, so the session
  would look fine and run a model nobody chose), and an empty free list is
  reported as "nothing is priced at zero right now" rather than as a broken
  source. Any figure in this docstring is a snapshot with a time on it; the
  filter and the probe are what hold.

* **An unknown model id does not 404 — it silently answers as a different
  model.** ``{"model": "gpt-4o"}`` returned ``200`` with
  ``"model": "qwen3.8-27b-uncensored"`` in the reply. The gateway routes an
  unrecognised name to its default rather than refusing it, so a typo in
  ``/model`` would look like a working switch and quietly run the wrong model.
  This harness therefore polices the name against the catalogue itself; the
  gateway will not.

* **``GET /v1/account`` is key-gated and is the real "what can I call" answer.**
  It returns ``{"balance_usd": 0.5, "plan": null, "month_spend_usd": 0,
  "api_locked": false}``. That is what :func:`account` reads, and what the setup
  command prints, because the catalogue cannot say whether a model is reachable
  on this account and this route can.

* **Streaming and tool calls work as they do on Anthropic.** Verified with a
  real tool-using request: ``content_block_start`` with a ``tool_use`` block,
  ``input_json_delta`` fragments, ``stop_reason: "tool_use"``, and a
  ``tool_result`` round-trip that came back as a normal ``end_turn``. Nothing
  here needs the Chat Completions transport.

* **The first request after idle can hang.** One call in this session timed out
  at 30s with zero bytes and then answered normally on the next attempt; the
  provider's own pricing page explains it — a sleeping GPU is woken for a
  ``$0.01`` wake fee, and "at busy moments, extra requests go to on-demand GPUs,
  which can take 30 seconds or more to start". :data:`TIMEOUT` is therefore
  generous, and a timeout is reported as "retry" rather than as a broken key.

Two things make it different enough to live in its own module, exactly as
UnoRouter and TokenHarbour do:

* **The credential is set, not discovered.** There is no shipped profile — the
  operator supplies their own key with ``/unrestricted-api-setup``. It is kept
  in a ``source``-format file of the same shape as every other profile, mode 600.
* **The catalogue is public and priced in dollars**, so a model can be chosen
  and priced before a key exists, and the cost line is the gateway's own rate
  rather than this harness's estimate.

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
HOST = "unrestricted.sh"

#: The SDK's ``base_url`` — and it is the PREFIX BEFORE ``/v1``, not the API root.
#: The Anthropic SDK appends ``/v1/messages`` to whatever it is given, so this
#: composes to ``https://api.unrestricted.sh/v1/messages``, which is where the
#: Messages protocol is served (a 401 without a key, i.e. the path exists).
#: Measured: ``https://api.unrestricted.sh/v1`` as the base 404s with
#: ``POST /v1/v1/messages``.
DEFAULT_BASE_URL = "https://api.unrestricted.sh"

#: The catalogue. **Public** — 200 with no key — which is why the free list works
#: before ``/unrestricted-api-setup`` has run. It carries real dollar rates per
#: million tokens, so unlike UnoRouter's this one can price a paid model exactly.
MODELS_URL = f"{DEFAULT_BASE_URL}/v1/models"

#: The account route. **Key-gated** — 401 without a credential — and the only
#: place that says whether a model is reachable on this account: the catalogue
#: prices a model, it does not say the balance covers it.
ACCOUNT_URL = f"{DEFAULT_BASE_URL}/v1/account"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = ZIMZILLA_HOME / "unrestricted" / "source"

#: Keys are issued as ``unr_sk_<token>``. A hint for the confirmation line, never
#: a gate: the gateway is the authority on its own keys, and refusing a real key
#: of an unexpected shape would be worse than storing it.
KEY_PREFIX = "unr_sk_"

#: The model to fall back to when the catalogue cannot be read at all. It is the
#: one model measured to answer on a fresh account's welcome credit, so a cold
#: cache still lands on something that works rather than on a name invented here.
DEFAULT_MODEL = "qwen3.8-27b-uncensored"

#: A fetched catalogue older than this is refetched rather than trusted. The
#: lineup is small and the prices are the gateway's own, so a stale list is a
#: stale price — and this picker exists to avoid exactly that.
CACHE_TTL = 300.0

#: One HTTP request per call, and deliberately generous. Measured: a request
#: after idle can take 30s+ while a sleeping GPU is woken (the provider charges a
#: $0.01 wake fee for it), so a short timeout would report a working route as
#: broken. See the module docstring.
TIMEOUT = 90.0

#: How many candidates :func:`first_callable` will probe before giving up. Each
#: probe is a real (tiny) chat call, so this is a cost bound as much as a time
#: one: on a fresh account the first candidate answers and the loop stops after
#: one call, and only a catalogue where nothing is reachable spends the rest.
PROBE_LIMIT = 4

#: The output modality a model must offer to be usable by this harness.
TEXT_MODALITY = "text"


@dataclass(frozen=True)
class Model:
    """One entry from the gateway's model catalogue."""

    id: str
    label: str = ""
    context_length: int | None = None
    max_output: int | None = None
    #: Dollars per million tokens, as the catalogue publishes them. ``None`` when
    #: the entry carried no ``pricing`` object at all — which is not the same as
    #: zero, and must not be read as free.
    price_in: float | None = None
    price_out: float | None = None
    price_cached: float | None = None
    #: The catalogue's own claim that prompts and replies are not retained.
    zero_retention: bool = False
    #: Whether the model runs on a partner's infrastructure rather than the
    #: provider's own GPUs. Worth showing: the provider's own page says partner
    #: models run with zero data retention in the EU and the US, while its own
    #: Qwen runs in Czechia.
    partner_hosted: bool = False
    modalities: tuple[str, ...] = ()

    @property
    def chat(self) -> bool:
        """Whether this model produces text — i.e. can answer a Messages turn."""
        return TEXT_MODALITY in self.modalities

    @property
    def free(self) -> bool:
        """Whether the catalogue prices this model at zero in **and** out.

        Both halves are required. A model with a zero input rate and a non-zero
        output rate is not free — it is free to *ask* and charged to answer,
        which is the shape that produces a surprise bill rather than a surprise
        refusal.

        **This is a price, not a permission, and the price itself moves.**
        Measured twice on 2026-11-08, an hour apart: the catalogue first priced
        ``deepseek-v4.1-flash-uncensored`` at $0.00/$0.00 and it answered ``400
        "for paid accounts. Add credit to use it."`` on a fresh account; an hour
        later the same entry was re-priced at $0.15/$0.60 and still refused. So
        ``free`` says what the model costs *at this reading*, and
        :func:`first_callable` is what says whether it can be called. Nothing in
        this module treats ``free`` as "will answer", and nothing treats an
        empty free list as "the source is broken".
        """
        return (self.price_in == 0.0 and self.price_out == 0.0)

    @property
    def priced(self) -> bool:
        """Whether the catalogue actually published a rate for this model."""
        return self.price_in is not None and self.price_out is not None

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits = []
        if self.free:
            bits.append("free")
        elif self.priced:
            bits.append(f"${self.price_in:g}/${self.price_out:g} per Mtok")
        else:
            bits.append("unpriced")
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
        if self.zero_retention:
            bits.append("no logs")
        if self.partner_hosted:
            bits.append("partner-hosted")
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------

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


def is_unrestricted(base_url: str | None) -> bool:
    """Whether *base_url* points at unrestricted.sh."""
    return HOST in (base_url or "")


#: Generic name for the same test, so code that handles several gateways
#: uniformly can call ``mod.active_endpoint(url)`` without knowing which one it
#: holds. See ``_cmd_gateway_key`` in ui/app.py.
active_endpoint = is_unrestricted


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
    """A key already sitting in ``~/claude-source/<name>``.

    This is the ``source ~/claude-source/<name>`` idiom: the operator has
    already written a profile for Claude Code, so ZimZilla should use it rather
    than asking for the key again. Returns ``(key, model, path)`` for the first
    profile that points at this host and carries a token.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_unrestricted(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at this host — i.e. the operator
         sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/unrestricted-api-setup``.
      3. Any profile under ``~/claude-source`` aimed at this host.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged or
    returned to the transcript — only ``origin`` (a path, or "environment") is.
    """
    if is_unrestricted(os.environ.get("ANTHROPIC_BASE_URL")):
        token = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                 or os.environ.get("ANTHROPIC_API_KEY") or "")
        if token:
            return token, os.environ.get("ANTHROPIC_MODEL", ""), "environment"

    exports = _read_exports(PROFILE)
    token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
    if token:
        return token, exports.get("ANTHROPIC_MODEL", ""), str(PROFILE)

    return _key_from_claude_source()


def save_key(token: str, model: str = "") -> Path:
    """Write the key to ZimZilla's own profile, mode 600.

    The file is the same ``source`` format as every other profile — it can be
    sourced by a shell and read by ``load_credentials`` — so there is one
    credential format in the project, not two.
    """
    token = (token or "").strip()
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        "# ZimZilla — unrestricted.sh profile (written by "
        "/unrestricted-api-setup)\n"
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


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    Deliberately loose — the gateway decides what is valid, not us — but strict
    enough to catch a pasted shell line or a bare word. The ``unr_sk_`` prefix is
    *not* required: a real key of an unexpected shape is worth more stored than
    rejected, and a wrong one only costs a single 401.
    """
    value = (value or "").strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))


def apply_to(cfg) -> tuple[str, str] | None:
    """Point *cfg* at unrestricted.sh. Returns ``(origin, model)`` or ``None``.

    The one place that knows how to aim a session at this gateway, shared by
    ``/zim-unrestricted`` and the launch-time restore in ``__main__`` — so a
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
    """Whether a remembered choice should override *base_url*.

    False for an endpoint the operator pointed somewhere deliberate — this
    gateway itself, or any other non-local host. True for the local proxies and
    the built-in default, which is what the launcher leaves behind when it has
    no profile to source. This is what keeps a stale ``current-source`` from
    hijacking a session aimed at a different provider.
    """
    url = (base_url or "").strip()
    if is_unrestricted(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


# ---------------------------------------------------------------------------
# the live catalogue
# ---------------------------------------------------------------------------

#: Fetched catalogues, keyed by the token they were fetched with. The catalogue
#: is public and identical for every caller — diffed with and without a key — so
#: the keyless fetch is cached under "" and a keyed one under its token, which
#: means a key change refetches rather than serving a list fetched for a
#: different account.
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


def _entries(payload: dict) -> list[dict]:
    """The catalogue rows from ``{"object": "list", "data": [...]}``.

    A malformed payload yields an empty catalogue rather than an exception: this
    runs inside a TUI command.
    """
    data = payload.get("data")
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    return []


def _parse_models(payload: dict) -> list[Model]:
    """Every entry in the catalogue payload, in the order the API returned them.

    A malformed entry is skipped rather than allowed to take the catalogue down.
    """
    out: list[Model] = []
    for entry in _entries(payload):
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        pricing = entry.get("pricing")
        pricing = pricing if isinstance(pricing, dict) else {}
        mods = entry.get("input_modalities")
        modalities = (tuple(str(m) for m in mods if isinstance(m, str))
                      if isinstance(mods, list) else ())
        out.append(Model(
            id=mid,
            label=str(entry.get("display_name") or entry.get("name") or "")[:120],
            context_length=_as_int(entry.get("context_length")),
            max_output=_as_int(entry.get("max_output_tokens")),
            price_in=_as_float(pricing.get("input")),
            price_out=_as_float(pricing.get("output")),
            price_cached=_as_float(pricing.get("cached_input")),
            zero_retention=bool(entry.get("zero_data_retention")),
            partner_hosted=bool(entry.get("partner_hosted")),
            modalities=modalities,
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = TIMEOUT,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch the model catalogue. Returns ``(models, error)``.

    **No key required** — the catalogue is public, so ``token`` defaults to empty
    and the free list is available before ``/unrestricted-api-setup`` has run. A
    key is sent when one is supplied, so the listing matches what this session
    would actually be charged.

    A network failure is returned, never raised: the caller is a TUI command that
    must say "could not reach unrestricted.sh" rather than take the session down.
    ``models`` is ``None`` on failure, ``[]`` for a genuinely empty catalogue —
    the caller can tell the two apart.
    """
    token = (token or "").strip()
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    headers = {"accept": "application/json", "User-Agent": "zimzilla"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["x-api-key"] = token
    req = urllib.request.Request(MODELS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, ("key rejected (401/403) — check it with "
                          "/unrestricted-api-setup")
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the models the catalogue prices at zero in and out."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The zero-priced models this harness can actually drive.

    Free **and** text-output. The modality test earns its place even on a
    four-model catalogue: the gateway's own page advertises image and chat
    surfaces, and a future entry that generates images would be genuinely free
    and unable to answer a Messages turn.

    **Sorted cheapest-first, then by context descending.** On this catalogue
    that puts the zero-priced model first and the rest in price order, which is
    the order an operator with a small balance wants. It is *not* a claim that
    the first entry answers — see :func:`first_callable` for that, and the
    module docstring for why the two are different questions.
    """
    models, err = free_models(token, **kw)
    if models is None:
        return None, err
    usable = [m for m in models if m.chat]
    usable.sort(key=lambda m: (m.price_out if m.price_out is not None else 0.0,
                               -(m.context_length or 0)))
    return usable, ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every text model id, catalogue order preserved — what ``/model`` offers.

    Text-output models, not only the free ones: an operator who names a paid
    model deliberately should not have it refused as "not served" against a list
    filtered to the free tier. This list is also the *only* thing that polices a
    model name here, because the gateway does not: an unknown id is silently
    routed to its default and answers 200 as a different model (measured — see
    the module docstring).
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
    """The last-fetched catalogue as :class:`Model` objects, or ``None``.

    ``/model`` runs on the UI thread and must not block on a fetch, so it reads
    whatever the last fetch left behind. The ``CACHE_TTL`` check is load-bearing
    for the same reason it is in the other gateway modules: an expired entry
    reads as ``None``, which callers already handle as "no catalogue", rather
    than reporting a list fetched once as live for the rest of the session.
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
    """Drop every cached catalogue. Called when the key changes."""
    _CACHE.clear()


def model_for(model: str, models: list[Model] | None) -> Model | None:
    """The catalogue's entry for *model*, if the catalogue knows it."""
    base = (model or "").strip()
    for m in models or []:
        if m.id == base:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok for *model*, when the catalogue can answer.

    Unlike UnoRouter's, this catalogue publishes **dollars per million tokens**,
    so a paid model is priced exactly rather than falling through to this
    harness's estimate table. ``None`` means the catalogue does not know the
    model, or the entry carried no ``pricing`` object — which is not zero and
    must not be reported as free.
    """
    found = model_for(model, models)
    if found is None or not found.priced:
        return None
    return (float(found.price_in or 0.0), float(found.price_out or 0.0))


def default_free_model(token: str = "", *, timeout: float = 8.0) -> str | None:
    """The cheapest model to use when the operator has chosen none.

    Exists because the alternative was measured to fail on the other gateways:
    with no saved preference, :func:`apply_to` left ``cfg.model`` on whatever the
    previous source had named, and switching here then sent *that* provider's
    model name to this one — a name it does not serve, which this gateway
    silently routes to its own default rather than refusing.

    **It falls back to the cheapest model when nothing is free, and that is not
    a nicety.** Measured twice on 2026-11-08, an hour apart: the catalogue first
    priced ``deepseek-v4.1-flash-uncensored`` at $0.00/$0.00, then re-priced it
    at $0.15/$0.60 — so "the free model" went from one entry to none while this
    module was being written. A function that returned ``None`` in that state
    would leave ``cfg.model`` on the *previous* provider's name, and because
    this gateway silently reroutes an unknown id to its own default, the session
    would look fine and quietly run a model nobody chose. Naming the cheapest
    entry instead is always a real id from this catalogue.

    Cache first: this runs inside :func:`apply_to`, which runs at startup and on
    every source switch, so the common case must not touch the network. Only with
    a cold cache is a fetch attempted, and then bounded by *timeout* so a slow
    gateway delays a launch rather than hanging it.

    Returns ``None`` only when the catalogue cannot be read at all, leaving the
    caller's model untouched — an unreachable catalogue is not a reason to
    invent a name.
    """
    models = cached_models()
    if models is None:
        models, _ = fetch_models(token, timeout=timeout)
    usable = [m for m in (models or []) if m.chat]
    if not usable:
        return None
    # Free first (cheapest possible), then by output rate ascending, with the
    # context window as the tiebreak — the same order `candidates` probes in, so
    # a launch-time restore and an explicit switch agree.
    usable.sort(key=lambda m: (m.price_out if m.price_out is not None else 0.0,
                               -(m.context_length or 0)))
    return usable[0].id


def candidates(token: str = "", *, timeout: float = TIMEOUT) -> list[str]:
    """Model ids in the order a fresh switch should try them, cheapest first.

    Zero-priced models lead, then the rest by output rate ascending, with the
    context window as the tiebreak. That is the order an operator with a small
    balance wants, and it is the order :func:`first_callable` probes in.

    Falls back to :data:`DEFAULT_MODEL` when the catalogue cannot be read at all,
    so a switch on an offline box still names a model that exists rather than
    leaving the previous provider's name in place.
    """
    models, _ = fetch_models(token, timeout=timeout)
    usable = [m for m in (models or []) if m.chat]
    if not usable:
        return [DEFAULT_MODEL]
    usable.sort(key=lambda m: (m.price_out if m.price_out is not None else 0.0,
                               -(m.context_length or 0)))
    return [m.id for m in usable]


def probe_model(token: str, model: str, *, timeout: float = TIMEOUT) -> tuple[bool, str]:
    """Whether *model* actually answers on this account. ``(answered, detail)``.

    Deliberately **not** :func:`verify_key`, and the difference is the whole
    point of this function. ``verify_key`` answers "is the credential good", so
    it returns True for a model the balance does not cover — a model problem must
    not fail a working key. This one answers "did this model reply", so only a
    200 counts. :func:`first_callable` needs the second question, and using the
    first for it would report the very first candidate as working on an account
    where it 400s.

    ``detail`` is the reason on failure, already run through
    :func:`explain_error`, so the caller can print it without re-parsing.
    """
    token = (token or "").strip()
    if not token:
        return False, "no key"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{DEFAULT_BASE_URL}/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"x-api-key": token,
                 "Authorization": f"Bearer {token}",
                 "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json",
                 "User-Agent": "zimzilla"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True, "answered"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except OSError:
            pass
        return False, explain_error(e.code, body)
    except (urllib.error.URLError, OSError) as e:
        return False, f"could not reach {HOST}: {e}"


def first_callable(token: str, *, limit: int = PROBE_LIMIT,
                   timeout: float = TIMEOUT) -> tuple[str | None, str]:
    """The first model that actually answers on this account. ``(model, detail)``.

    **This is the function the "use the free model" behaviour needs, and the
    catalogue cannot replace it.** Measured 2026-11-08 on a fresh account: the
    one zero-priced model in the catalogue answered ``400 "for paid accounts.
    Add credit to use it."``, while a *paid* model answered normally against the
    $0.50 welcome credit. So the price column and the reachable set are
    different sets, and only a real call tells them apart.

    Probes in :func:`candidates` order — cheapest first — and stops at the first
    model that answers. Each probe is one tiny chat call, so *limit* bounds both
    the cost and the wait; on the measured account the first candidate answers
    and the loop stops after one call.

    A model that refuses for a *model* reason (not on this plan, at capacity) is
    skipped and the next is tried. A **rejected key** stops the walk
    immediately: every remaining candidate would fail the same way, and
    reporting "no model works" for a bad credential would send the operator
    looking in the wrong place.

    Returns ``(None, reason)`` when nothing answered, so the caller can say why
    rather than leaving the session on a model that will 400.
    """
    token = (token or "").strip()
    if not token:
        return None, "no key — set one with /unrestricted-api-setup <key>"
    tried: list[str] = []
    last = ""
    for mid in candidates(token, timeout=timeout)[:max(1, limit)]:
        tried.append(mid)
        answered, detail = probe_model(token, mid, timeout=timeout)
        if answered:
            return mid, detail
        last = detail
        if "key rejected" in detail.lower():
            return None, detail
    if not tried:
        return None, last or "the catalogue listed no chat models"
    return None, (f"none of {len(tried)} model(s) answered on this account — "
                  f"{last}")


# ---------------------------------------------------------------------------
# the account
# ---------------------------------------------------------------------------

def account(token: str, *, timeout: float = 20.0) -> tuple[dict | None, str]:
    """``GET /v1/account`` — balance, plan and spend. ``(payload, error)``.

    **Key-gated** (401 without a credential), and the only route that answers the
    question the catalogue cannot: whether this account can call anything. The
    catalogue prices a model; this says whether the balance covers it.

    Returns the decoded payload — ``balance_usd``, ``plan``, ``month_spend_usd``,
    ``api_locked`` — or ``(None, reason)``. Never raises: the caller is a TUI
    command.
    """
    token = (token or "").strip()
    if not token:
        return None, "no key"
    req = urllib.request.Request(ACCOUNT_URL, headers={
        "Authorization": f"Bearer {token}",
        "x-api-key": token,
        "accept": "application/json",
        "User-Agent": "zimzilla",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, ("key rejected (401/403) — check it with "
                          "/unrestricted-api-setup")
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"
    return (payload if isinstance(payload, dict) else {}), ""


# ---------------------------------------------------------------------------
# error shapes
# ---------------------------------------------------------------------------

def explain_error(status: int, body: str) -> str:
    """A sentence for a failure, from the shape it came back in.

    The shapes this endpoint actually returns are not interchangeable, and the
    catalogue cannot predict any of them:

    * ``400`` with ``"is for paid accounts. Add credit to use it."`` — the model
      is priced but this account's balance does not cover it. **This is the one
      that matters**: it is a 400, not a 402, so a caller that only checks 402
      would report it as a malformed request. Retrying never clears it; the
      model has to be changed or credit added.
    * ``401`` with ``authentication_error`` — the credential is wrong or missing.
    * ``429`` — rate limited. The provider's own page says 3 requests at once per
      account (5 on Pro, 8 on Max), so this is a concurrency limit as much as a
      rate one.
    * A timeout with no body — a sleeping GPU being woken. The provider charges a
      ``$0.01`` wake fee and says on-demand GPUs "can take 30 seconds or more to
      start", so this is "retry", not "broken".

    ``body`` is the decoded response text, or ``""`` when there was none.
    """
    text = (body or "").strip()
    detail = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
        if not detail:
            detail = str(payload.get("message") or payload.get("detail") or "")
    lowered = (detail or text).lower()

    if "for paid accounts" in lowered or "add credit" in lowered:
        return ("this model is for paid accounts — the balance does not cover "
                "it. Pick another model (/model) or add credit; retrying will "
                "not help")
    if status in (401, 403):
        return "key rejected — check it with /unrestricted-api-setup"
    if status == 402:
        return ("out of credit (402) — top up at https://unrestricted.sh, or "
                "switch to a free model with /model")
    if status == 429:
        return ("rate limited (429) — 3 requests at once per account; retry "
                "shortly")
    if status == 503 or "capacity" in lowered or "overloaded" in lowered:
        return "the gateway is at capacity (503) — retry shortly"
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    return f"HTTP {status}"


def verify_key(token: str, model: str = "", *, timeout: float = TIMEOUT) -> tuple[bool, str]:
    """Prove *token* works by making one real, tiny chat call.

    The catalogue *would* answer this — it is public, so it answers with a wrong
    key too — which is exactly why a chat call is the better probe: it proves the
    credential **and** that the model the session is about to use is callable, in
    one round-trip. A key that lists a catalogue but cannot call anything is not
    a working route, and on this gateway that is the normal state of a fresh
    account for three of its four models.

    ``ok`` is False **only** for a rejected key or an unreachable endpoint. A
    model that is not covered by the balance, or a rate limit, says nothing about
    the credential, so those come back ``ok=True`` with the reason in ``detail``
    — failing the switch on them would refuse a working key over a model choice.
    :func:`first_callable` is what walks past them.
    """
    token = (token or "").strip()
    if not token:
        return False, "no key"
    chosen = model or DEFAULT_MODEL
    payload = {
        "model": chosen,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{DEFAULT_BASE_URL}/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"x-api-key": token,
                 "Authorization": f"Bearer {token}",
                 "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json",
                 "User-Agent": "zimzilla"},
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
        if e.code in (401, 403):
            return False, f"HTTP {e.code} — {explain_error(e.code, body)}"
        return True, explain_error(e.code, body)
    except (urllib.error.URLError, OSError) as e:
        return False, f"could not reach {HOST}: {e}"
