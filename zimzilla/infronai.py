"""InfronAI — a free-model upstream, reached directly by default.

Infron (``https://infron.ai``, API at ``https://llm.onerouter.pro``) is a model
marketplace that fronts a few hundred models behind one key. Measured on this
box, 2026-10-20:

* It **does** serve the Anthropic Messages protocol natively. ``POST
  /v1/messages`` answers a 401 without a key while ``POST /v1/nonsense-path``
  answers 404 — i.e. the path exists, so a session *could* point straight at
  ``https://llm.onerouter.pro``.
* **But the free models cannot be reached that way.** The catalog publishes
  ``supported_endpoint_types`` per model, and every one of the six ``:free``
  chat models lists ``["openai"]`` and nothing else. Of 341 LLM entries only 21
  list ``anthropic``, and none of those is free. A turn for a free model sent to
  the Messages endpoint would be routed to a provider that does not speak it.

So this is the IteraCompute arrangement, not the UnoRouter one: a local LiteLLM
proxy on :4003 translates ``/v1/messages`` into ``/v1/chat/completions`` at
``https://llm.onerouter.pro/v1``. 4001 is Logfare and 4002 is IteraCompute;
this one must not collide with either. (Token Juice has no port any more: its
upstream is reached directly.)

That proxy is no longer the route a switch takes. ``/zim-infronai`` reads the
proxy's own config for the upstream URL and the ``:free`` model ids and talks
Chat Completions to ``https://llm.onerouter.pro/v1`` directly, because the
harness speaks that protocol itself now (``zimzilla.openai_compat``). The proxy
remains as ``/zim-infronai proxy`` — it is still the thing that knows how to
translate, and the config here is still what names the ids.

**The proxy pins Chat Completions, and that is load-bearing here too.** LiteLLM
1.x routes a ``/v1/messages`` request for an ``openai/`` deployment through the
Responses API by default. Infron serves ``/v1/chat/completions`` for all 503
catalog entries and ``/v1/responses`` only for a handful, so the default is the
wrong path for the models this source exists to reach.
``litellm_settings.use_chat_completions_url_for_anthropic_messages: true``
opts back out, exactly as ``packaging/iteracompute`` does.

The gateway's own docs state the contract:

* ``https://llm.onerouter.pro/v1`` is the OpenAI-compatible API root;
  ``https://llm.onerouter.pro`` (no ``/v1``) is the Anthropic-compatible one.
  Use the catalog's ``id`` values verbatim — they are ``provider/model`` and
  contain a slash, so no prefix may be added.
* Free variants carry a ``:free`` suffix and are billed at $0.00 per token.
  Free usage **never deducts from the credit balance**.
* **A balance is still required as an anti-abuse measure.** Standard free
  models need **$5 or more** on the account and are capped at **1000 requests
  per day per account, shared across every standard free model**, resetting at
  00:00 UTC. An unfunded account gets ``402`` even for a ``:free`` model. That
  is the one thing about this free tier that is easy to get wrong, so
  ``/infronai-models`` says it rather than letting a 402 arrive unexplained.
* The docs describe a limited-time promotion (``qwen/qwen3.8-max-preview:free``,
  no daily cap, through 2026-07-28). That date has passed and the id is not in
  the live catalog, so it is recorded here as history and **not** hardcoded as
  a preference.
* Errors: ``401`` bad key, ``402`` insufficient credits, ``429`` rate limited,
  ``502`` model down, ``503`` no provider meets the routing requirements.

**Free is the catalog's price, with the suffix as corroboration.** Each entry
carries ``min_prompt_price``/``min_completion_price`` in **USD per million
tokens** (verified against known rates: ``anthropic/claude-sonnet-4.5`` reads
3/15, ``openai/gpt-4o`` 1.875/7.5). A free entry prices 0/0 and carries the
suffix; both are read, and the two agreed on every entry measured. A *missing*
price — 120 of the 503 entries have nulls — is **not** free: the ``priced`` flag
records whether a price was actually read, so an unpriced model falls back to
the harness's labelled estimate instead of being advertised as free.

**Two catalog traps, both filtered rather than named.**

* Entries whose ``output_modalities`` is not ``text`` cannot answer a turn —
  the catalog fronts video, image, speech, embedding and rerank models, and 52
  of the free-priced ones are video generators. Filtering on the name would rot
  as models are added, so the modality is the test.
* Six LLM entries list ``openai-response`` as their only endpoint
  (``openai/gpt-5*-codex``, ``openai/gpt-5-pro``, ``openai/gpt-5.4-pro``).
  The proxy forwards Chat Completions, so those cannot be served through it.
  They are excluded from :func:`model_ids` rather than offered and then failed
  on the first turn. A missing endpoint list is treated as permissive — an
  absent field is not evidence of absence.
* ``is_display_only`` marks a model the marketplace shows but does not serve;
  the one such entry in this catalog is also priced 0/0, so without the check
  it would appear as a free chat model.

The catalog is **public** — ``GET /v1/models`` answers with no key — so the
free list works before ``/infronai-api-setup`` has run.

**One request detail is load-bearing.** The gateway sits behind Cloudflare, and
Cloudflare's bot rule answers urllib's default ``Python-urllib/3.x`` signature
with ``403 Error 1010: Access denied`` — measured 2026-10-20, where the only
variable was that header. A 403 is therefore reported as the bot rule rather
than as a rejected key, because a bad key reads as ``401`` from this gateway and
"check your key" would send the operator to fix something that is not wrong.
:data:`USER_AGENT` is what avoids it. LiteLLM's own client sends a different
signature, so the proxy's upstream traffic is unaffected.

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

#: The port the local LiteLLM translation proxy listens on. 4001 is Logfare,
#: 4000 is Token Juice and 4002 is IteraCompute; this one must not collide.
PROXY_PORT = 4003

#: What the session points at. This is the **local proxy**, not the upstream:
#: the free models are ``openai``-endpoint only, so a turn sent straight to
#: ``llm.onerouter.pro`` would reach a provider that does not speak Messages.
#: The launcher and ``/zim-infronai`` both start the proxy before aiming here.
DEFAULT_BASE_URL = f"http://localhost:{PROXY_PORT}"

#: The upstream OpenAI-compatible API root, used by the proxy config. Kept here
#: so the host is spelled once; ``packaging/infronai/litellm-config.yaml``
#: carries it for LiteLLM, and a test asserts the two agree.
API_BASE_URL = "https://llm.onerouter.pro/v1"

#: The public model catalog. No key required, which is why the free list works
#: before ``/infronai-api-setup`` has run. ``api.infron.ai`` serves the same
#: payload; the documented host is used so there is one URL to reason about.
MODELS_URL = f"{API_BASE_URL}/models"

#: The profile this module writes and reads for the in-TUI key.
PROFILE = ZIMZILLA_HOME / "infronai" / "source"

#: A fetched catalog older than this is refetched rather than trusted. The free
#: lineup changes over time, so a stale list is exactly the wrong list this
#: picker exists to avoid.
CACHE_TTL = 300.0

#: The suffix the gateway puts on a model that costs nothing.
FREE_SUFFIX = ":free"

#: The output modality a model must offer to be usable by this harness.
TEXT_MODALITY = "text"

#: The endpoint a model must support for the proxy to be able to forward it.
#: The proxy speaks Chat Completions, so an entry listing only
#: ``openai-response`` cannot be served through it.
CHAT_ENDPOINT = "openai"

#: Sent on every catalog request. **Load-bearing, not politeness.** The gateway
#: sits behind Cloudflare, which answers urllib's default ``Python-urllib/3.x``
#: signature with ``403 Error 1010: Access denied`` — measured 2026-10-20, where
#: ``curl`` (200), any ordinary token in this header (200) and no header at all
#: (403) differed only in this string. Without it the public catalog reads as a
#: rejected key, which is the most misleading failure this module could report.
USER_AGENT = "zimzilla"


@dataclass(frozen=True)
class Model:
    """One entry from InfronAI's public model catalog."""

    id: str
    label: str = ""
    #: The catalog's ``context_length``, unclamped.
    catalog_context: int | None = None
    max_output: int | None = None
    #: USD per million tokens, as the catalog publishes them.
    price_in: float = 0.0
    price_out: float = 0.0
    #: Whether a price was actually **read**. A missing price must not read as
    #: 0/0 and mark every unpriced entry free — 120 entries carry nulls.
    priced: bool = False
    #: The ``:free`` suffix, which is how the docs say a free variant is marked.
    suffixed_free: bool = False
    #: Output modalities, lowercased. ``("text",)`` is what a chat model offers.
    modalities: tuple[str, ...] = ()
    #: The endpoints the gateway serves this model on.
    endpoints: tuple[str, ...] = ()
    tools: bool = False
    vision: bool = False
    #: ``is_display_only`` — shown in the marketplace, not callable.
    display_only: bool = False
    deprecated: bool = False
    #: How many upstream providers the catalog lists. Redundancy, not health.
    providers: int = 0

    @property
    def free(self) -> bool:
        """Whether this model costs nothing.

        A read 0/0 price, or the documented ``:free`` suffix. The suffix alone
        is accepted only when no price could be read, and a missing price on an
        entry without the suffix is **not** free — the ``priced`` flag is what
        keeps an unpriced model from being advertised as free.
        """
        if self.priced:
            return self.price_in == 0.0 and self.price_out == 0.0
        return self.suffixed_free

    @property
    def chat(self) -> bool:
        """Whether this model can answer a Messages turn through the proxy.

        Text output, not display-only, and — when the catalog names endpoints —
        reachable over Chat Completions. All three tests earn their place; see
        the module docstring for what each one excludes.
        """
        if TEXT_MODALITY not in self.modalities:
            return False
        if self.display_only or self.deprecated:
            return False
        if self.endpoints and CHAT_ENDPOINT not in self.endpoints:
            return False
        return True

    @property
    def context_length(self) -> int | None:
        """The window to plan against, in tokens — the catalog's own figure.

        Unlike IteraCompute there is no account-tier cap to apply: the docs
        describe free variants as the same models, limited by request count
        rather than by window. The published figure is the answer.
        """
        return self.catalog_context

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits = []
        if self.free:
            bits.append("free")
        elif self.priced and (self.price_in or self.price_out):
            bits.append(f"${self.price_in:g}/${self.price_out:g}")
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
        if not self.tools:
            bits.append("no tools")
        if self.vision:
            bits.append("vision")
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------

def is_infronai(base_url: str | None) -> bool:
    """Whether *base_url* points at this source's local proxy.

    Matched by port on loopback only. ``llm.onerouter.pro`` deliberately does
    **not** match: this predicate answers "is the session on the proxy route",
    and the upstream is not that route. The upstream is reachable — over Chat
    Completions, with ``/openai-endpoint`` or ``--protocol openai`` — but it is
    a direct endpoint, not an InfronAI *source* with a proxy to manage.
    """
    url = (base_url or "").strip()
    return (f"localhost:{PROXY_PORT}" in url
            or f"127.0.0.1:{PROXY_PORT}" in url)


#: Generic name for the same test, so code that handles several sources
#: uniformly can call ``mod.active_endpoint(url)``.
active_endpoint = is_infronai


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
    """An InfronAI key already sitting in ``~/claude-source/<name>``.

    A profile there is the ``source ~/claude-source/<name>`` idiom. Only one
    that points at this proxy's port counts — a profile aimed at the upstream
    directly is not a working setup for the free tier, so adopting its key
    would be a lie.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_infronai(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The InfronAI key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at this proxy — i.e. the operator
         sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/infronai-api-setup``.
      3. Any profile under ``~/claude-source`` aimed at this proxy.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged
    or returned to the transcript — only ``origin`` (a path, or "environment").
    """
    if is_infronai(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Write the key to ZimZilla's own InfronAI profile, mode 600.

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
    """The catalog rows from ``{"data": [...]}``.

    A malformed payload yields an empty catalog rather than an exception: this
    runs inside a TUI command.
    """
    data = payload.get("data")
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    return []


def _strings(value) -> tuple[str, ...]:
    """A list of strings, lowercased; anything else yields ``()``."""
    if not isinstance(value, list):
        return ()
    return tuple(str(v).strip().lower() for v in value if isinstance(v, str))


def _parse_models(payload: dict) -> list[Model]:
    """Every entry in the catalog payload, in catalog order."""
    out: list[Model] = []
    for entry in _entries(payload):
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        price_in = _as_float(entry.get("min_prompt_price"))
        price_out = _as_float(entry.get("min_completion_price"))
        priced = price_in is not None and price_out is not None
        if not priced:
            price_in = price_out = 0.0

        providers = entry.get("providers")
        out.append(Model(
            id=mid,
            label=str(entry.get("display_name") or "")[:120],
            catalog_context=_as_int(entry.get("context_length")),
            max_output=_as_int(entry.get("max_output_tokens")),
            price_in=price_in or 0.0,
            price_out=price_out or 0.0,
            priced=priced,
            suffixed_free=mid.endswith(FREE_SUFFIX),
            modalities=_strings(entry.get("output_modalities")),
            endpoints=_strings(entry.get("supported_endpoint_types")),
            tools=bool(entry.get("supports_function_calling")),
            vision=bool(entry.get("supports_vision")),
            display_only=bool(entry.get("is_display_only")),
            deprecated=bool(entry.get("deprecated")),
            providers=len(providers) if isinstance(providers, list) else 0,
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch InfronAI's model catalog. Returns ``(models, error)``.

    **No key required** — the catalog is public, so ``token`` defaults to empty
    and the free list is available before ``/infronai-api-setup`` has run. A
    key is sent when one is supplied, so the listing matches this account.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach InfronAI" rather than take the session down.
    ``models`` is ``None`` on failure, ``[]`` for a genuinely empty catalog —
    the caller can tell the two apart.
    """
    token = (token or "").strip()
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    headers = {"accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(MODELS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            # Two very different causes share this status, and confusing them
            # sends the operator to check a key that is not the problem: a bad
            # key is rejected as 401 by this gateway, while Cloudflare's bot
            # rule answers 1010 — which a missing User-Agent triggers.
            return None, ("403 from llm.onerouter.pro — a rejected key reads as "
                          "401 here, so this is Cloudflare's bot rule (1010); "
                          "check the User-Agent this build sends")
        if e.code == 401:
            return None, "key rejected (401) — check it with /infronai-api-setup"
        if e.code == 402:
            return None, ("account has insufficient credits (402) — free models "
                          "still require a funded balance")
        return None, f"HTTP {e.code} from llm.onerouter.pro"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach llm.onerouter.pro: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the models InfronAI is currently giving away."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The free models this harness can actually drive, most context first.

    Free **and** text-output **and** reachable through the proxy. Without the
    modality test the list fills with the free-priced video generators the
    gateway fronts (52 of them at last count); without the endpoint test it
    offers models the Chat Completions proxy cannot forward.

    Sorted by context window descending — the models here are otherwise alike
    (all free, all tool-capable), so the window is the one published difference
    worth ranking on. Catalog order is the stable-sort tiebreak.
    """
    models, err = free_models(token, **kw)
    if models is None:
        return None, err
    usable = [m for m in models if m.chat]
    usable.sort(key=lambda m: m.catalog_context or 0, reverse=True)
    return usable, ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every servable chat model id, catalog order preserved.

    All of them, not only the free ones: an operator who names a paid model
    deliberately should not have it refused as "not served by infronai" against
    a list filtered to the free tier. Non-text, display-only and
    Responses-only entries are still dropped — they cannot answer a turn
    through this proxy, so offering them would be offering something that
    fails.
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
    ``free``. A paid model returns the catalog's own USD-per-Mtok rates. An id
    the catalog does not know — or one whose price could not be read — returns
    ``None``, so the caller falls back to this harness's labelled estimate.
    """
    found = model_for(model, models)
    if found is None:
        return None
    if found.free:
        return (0.0, 0.0)
    if not found.priced:
        return None
    return (found.price_in, found.price_out)


#: Free models to try first, in order. Every name here was measured present,
#: free, text-output and tool-capable on 2026-10-20. The order is a context
#: tiebreak, not a reliability claim: nothing has been measured about how these
#: behave under load, so ranking them on anything else would be inventing a
#: finding. A name that has since gone away falls through to the next, then to
#: the catalog's own order — the lineup is documented as changing over time.
PREFERRED_FREE_MODELS = (
    "deepseek/deepseek-v4.1-flash:free",
    "qwen/qwen3.8-27b:free",
    "apodex/apodex-1.1-mini:free",
    "inclusionai/ling-3.1-flash:free",
    "internlm/atria-dawn-preview:free",
)


def default_free_model(token: str = "", *, timeout: float = 8.0) -> str | None:
    """A free model to use when the operator has chosen none.

    Exists because the alternative was measured to fail on the other gateways:
    with no saved preference, a switch left ``cfg.model`` on whatever the
    previous source had named, and that provider's model name then went to
    InfronAI — a name it does not serve. Naming a free model instead means a
    fresh switch spends nothing and works.

    Cache first: this runs on every source switch, so the common case must not
    touch the network. Only with a cold cache is a fetch attempted, bounded by
    *timeout* so a slow gateway delays a launch rather than hanging it.

    Returns ``None`` when nothing free is on offer, leaving the caller's model
    untouched — an empty catalog is not a reason to invent a name.
    """
    models = cached_models()
    if models is None:
        models, _ = fetch_models(token, timeout=timeout)
    usable = [m for m in (models or []) if m.free and m.chat]
    if not usable:
        return None
    present = {m.id for m in usable}
    for preferred in PREFERRED_FREE_MODELS:
        if preferred in present:
            return preferred
    return usable[0].id


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
