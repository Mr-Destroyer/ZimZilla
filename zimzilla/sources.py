"""Upstream "sources" — where ZimZilla can point a session, and how.

ZimZilla speaks the Anthropic Messages protocol to whatever ``ANTHROPIC_BASE_URL``
points at. That is the default, not the only option: an endpoint that serves
Chat Completions can be reached directly instead, with no proxy in between —
see :mod:`zimzilla.openai_compat`, ``/openai-endpoint`` and the ``protocol``
field on :class:`zimzilla.config.Config`. A source is the *route*: port,
profile, service for one that runs a local proxy; a profile alone for one that
is reached directly. A proxy route's direct endpoint is not a second Source but
a property read out of the same config (:func:`direct_route`), and switching
prefers it.

Three local LiteLLM proxies front three different upstreams:

    logfare       :4001  →  logfare.ai         (the default, shipped with the repo)
    iteracompute  :4002  →  api.iteracompute.com (OpenAI-only; free Qwen tier)
    infronai      :4003  →  llm.onerouter.pro  (OpenAI-only; free model tier)

Token Juice has no proxy at all: its upstream serves Chat Completions, the
harness speaks it, so the route is declared here and reached directly —
``api.tokenjuice.ai/v1``, keyed by the same profile ``/zim-tokenjuice`` always
read.

Logfare is a full route, not just the starting point: the shipped profile makes
a fresh clone run with no signup, and ``/zim-logfare`` switches back to it at
any time. Its key is shared by every install, so heavy use can exhaust the
upstream's daily request budget — that arrives as a 429 with a ``Retry-After``,
which the harness reports rather than retrying.

IteraCompute is OpenAI-compatible only, so its proxy is not optional: the
upstream has no Messages endpoint at all. Its free tier is 30M tokens per UTC
calendar month on one model, and ``/iteracompute-api-setup`` stores the key.

InfronAI does serve Messages natively, but its free models are listed
``openai``-endpoint only in the catalog, so its proxy is not optional either.
Its free tier needs a funded balance ($5) and allows 1000 requests per day
across all free models; ``/infronai-api-setup`` stores the key.

Token Juice used to be the fourth proxy. It is now a declared direct route
(see ``TOKENJUICE_DIRECT`` and ``DIRECT_ONLY``): the upstream answers Chat
Completions itself, so there is nothing to translate and nothing to start.

NVIDIA NIM is the same shape and has never had a proxy: its hosted catalog at
``integrate.api.nvidia.com/v1`` speaks Chat Completions, so ``/zim-nvidia``
points the session straight at it. Its key is the operator's own developer key
(``/nvidia-api-setup``), never shipped, and its catalog is public — see
:mod:`zimzilla.nvidia` for what that catalog does and does not say.

The other upstreams (TokenHarbour, OpenRouter, UnoRouter, AIHubMix, BeatAPI,
AntSeed) are remote or operator-started, and live in their own modules; this one
is the local proxy plumbing and the selection record
(``~/.zimzilla/current-source``).

Nothing here echoes a key. Credentials live in each source's profile file, mode
600. Logfare's ``packaging/logfare/source`` is a template, not a credential: it
ships a placeholder so the shape of the file is visible, and the operator's own
key goes in ``~/.zimzilla/logfare/source``.
"""

from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a hard dep, but stay importable
    yaml = None

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
DEFAULT_HOME = Path.home()
DEFAULT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Source:
    """One upstream: where to reach it, how to start it, what to call the model."""

    key: str            # "logfare" | "tokenjuice" | "iteracompute"
    label: str          # human name, for the transcript
    port: int           # 0 for a source with no proxy
    profile: Path       # shell profile to source (holds the key; mode 600)
    model: str          # default model name for this upstream
    root: Path          # checkout the service manager is started from
    service: Path | None = None    # start-litellm.sh-compatible manager
    config: Path | None = None     # the litellm yaml
    models: tuple[str, ...] = ()   # declared fallback list; the live one is fetched
    #: The route, when it is declared here rather than read from a proxy config.
    #: Set for a source that has no proxy at all — see DIRECT_ONLY.
    direct: "DirectRoute | None" = None
    #: Non-None while the route is deliberately not usable — the reason to print.
    #: See UNDER_CONSTRUCTION.
    under_construction: str | None = None

    @property
    def base_url(self) -> str:
        """Where the session is pointed for this source.

        The proxy's address when there is a proxy, and the declared upstream
        when there is not. A direct-only source has no port — it is discovered
        with port 0 — so ``http://localhost:0`` would be the literal answer,
        and a caller that believed it would point the session at nothing. The
        switch path uses ``DirectRoute.base_url`` directly, so this only ever
        mattered as a trap; it is answered here instead.
        """
        if self.direct is not None and self.is_direct_only:
            return self.direct.base_url
        return f"http://localhost:{self.port}"

    @property
    def is_direct_only(self) -> bool:
        """Whether this route is reached directly and *only* directly.

        True for a source whose upstream speaks Chat Completions and which has
        no LiteLLM proxy in front of it: there is no port to start, no health
        check to make, and no `proxy` argument that could do anything.
        """
        return self.service is None and self.direct is not None


@dataclass(frozen=True)
class DirectRoute:
    """A source's upstream, reached with no proxy in front of it.

    Every ``Source`` in this tree is a local LiteLLM proxy in front of an
    OpenAI-compatible upstream. The proxy exists to turn Anthropic Messages into
    Chat Completions — which the harness now speaks itself — so the same
    upstream is reachable directly: same key, same catalog, one fewer hop, and
    tool calls that arrive as the model emitted them instead of reassembled by a
    bridge.

    ``base_url`` and the alias map are read out of the very config the proxy is
    started with, so there is one copy of the truth. A model added there is
    reachable directly with no second edit, and an alias that points somewhere
    unexpected is forwarded as written rather than guessed at.

    A source with no proxy has no config to read, so its route is declared in
    this module instead (:data:`TOKENJUICE_DIRECT`) — same shape, same rules.
    """

    base_url: str
    #: Proxy alias -> the id the upstream actually serves.
    models: dict[str, str] = field(default_factory=dict)

    def upstream_model(self, alias: str) -> str:
        """The id to send upstream for *alias* — itself when it is unmapped.

        Token Juice is why this exists: the proxy served it as
        ``deepseek-v4.1-flash`` while the upstream only answers to
        ``deepseek-ai/DeepSeek-V4.1-Flash-1017``.
        """
        return self.models.get(alias, alias)


#: Token Juice's route, declared rather than read from a proxy config.
#:
#: The upstream serves Chat Completions itself, so there is no LiteLLM hop to
#: take the base URL from. The alias is kept because the profile written by an
#: older install still names it, and because it is the stable half of the id:
#: the upstream rotates the ``-1017`` snapshot suffix, and a switch re-reads the
#: live catalog and replaces a retired name rather than 404ing on it.
TOKENJUICE_DIRECT = DirectRoute(
    base_url="https://api.tokenjuice.ai/v1",
    models={"deepseek-v4.1-flash": "deepseek-ai/DeepSeek-V4.1-Flash-1017"},
)

#: NVIDIA NIM's route, declared rather than read from a proxy config.
#:
#: ``integrate.api.nvidia.com/v1`` speaks Chat Completions itself, so — like
#: Token Juice — there is no LiteLLM hop and nothing to start. The alias map is
#: empty on purpose: the catalog's ids are already the ids that go on the wire
#: (``openai/gpt-oss-20b``, ``nvidia/nemotron-3.5-lightning-30b-a3b``), so
#: there is no proxy name to translate. See :mod:`zimzilla.nvidia`.
NVIDIA_DIRECT = DirectRoute(base_url="https://integrate.api.nvidia.com/v1")

#: Sources with no proxy at all. Their credential profile is the only file that
#: has to exist; service and config are absent by design, and ``direct`` says
#: where they go.
DIRECT_ONLY: frozenset[str] = frozenset({"tokenjuice", "nvidia"})

#: Declared routes for the direct-only sources, keyed like ``MODELS_BY_KEY``.
DECLARED_ROUTES: dict[str, DirectRoute] = {
    "tokenjuice": TOKENJUICE_DIRECT,
    "nvidia": NVIDIA_DIRECT,
}


#: Parsed routes, keyed by config path and mtime so an edited config is re-read.
_DIRECT_CACHE: dict[tuple[str, float], DirectRoute | None] = {}


def direct_route(src: Source | None) -> DirectRoute | None:
    """The direct Chat Completions route for *src*, or None when it has none.

    None is the honest answer for a config that cannot be read, that names no
    ``api_base`` (AntSeed's local buyer has no upstream URL at all), or whose
    deployments are not ``openai/`` providers. In each of those the proxy is the
    only route, and the caller keeps using it.

    A direct-only source has no config: its route is declared, and that is what
    comes back first.
    """
    if src is None:
        return None
    if src.direct is not None:
        return src.direct
    if src.config is None or yaml is None:
        return None
    try:
        mtime = src.config.stat().st_mtime
    except OSError:
        return None
    cache_key = (str(src.config), mtime)
    if cache_key in _DIRECT_CACHE:
        return _DIRECT_CACHE[cache_key]
    route: DirectRoute | None = None
    try:
        data = yaml.safe_load(src.config.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a broken config is a "no direct route"
        data = {}
    entries = data.get("model_list") if isinstance(data, dict) else None
    if isinstance(entries, list):
        route = _route_from_entries(src, entries)
    _DIRECT_CACHE[cache_key] = route
    return route


def _route_from_entries(src: Source, entries: list) -> DirectRoute | None:
    """Build a :class:`DirectRoute` from a proxy's ``model_list``.

    Only ``openai/`` deployments count. That is the whole point of the exercise
    — an ``anthropic/`` or ``responses/`` deployment is a different wire
    protocol, and sending Chat Completions at it would be the mistake this is
    meant to avoid.
    """
    models: dict[str, str] = {}
    alias_bases: dict[str, str] = {}
    base_counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        alias = entry.get("model_name")
        params = entry.get("litellm_params")
        if not isinstance(alias, str) or not isinstance(params, dict):
            continue
        model = params.get("model")
        base = params.get("api_base")
        if not isinstance(model, str) or not model.startswith("openai/"):
            continue
        if not isinstance(base, str) or not base.startswith("http"):
            continue
        base = base.rstrip("/")
        base_counts[base] = base_counts.get(base, 0) + 1
        upstream = model[len("openai/"):]
        if alias == "*" or not upstream or upstream == "*":
            continue
        alias_bases[alias] = base
        models[alias] = upstream
    if not base_counts:
        return None
    # The declared default's own api_base wins: a config may front two upstreams
    # and `src.model` is the one string that says which of them is meant.
    base_url = alias_bases.get(src.model)
    if base_url is None:
        base_url = max(base_counts, key=lambda b: base_counts[b])
    return DirectRoute(base_url=base_url, models=models)


def _host_of(url: str) -> str:
    """The host in *url*, lowercased, without userinfo or port. ``""`` if none."""
    match = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", (url or "").strip().lower())
    if not match:
        return ""
    return match.group(1).split("@")[-1].split(":")[0]


# Sources that are installed, still discovered, and still listed by
# `/zim-source` — but deliberately not switchable right now. Key -> the reason
# to print.
#
# Parked rather than removed so the route stays *visible*: an operator who
# cannot see a source cannot tell "not installed on this machine" from
# "installed and deliberately off", and those call for different actions. It is
# also what makes bringing it back a one-line change in here plus the UI, with
# no second copy of the decision to find.
#
# A parked source is never marked active, never started by a switch, and a
# remembered selection of it is refused by `current_key` — so a stale
# `current-source` cannot drag a session back onto it.
#
# Logfare is deliberately NOT parked. It is the default route and `/zim-logfare`
# has to be somewhere the operator can come back to, even before a key is set —
# the catalog is public, so the route can answer "what is up" with no credential
# at all. Its budget is per account and exhausting it answers 429 with a
# Retry-After; that is a runtime condition to report, not a reason to hide the
# route.
#
# Nothing is parked right now. Token Juice was the only entry, and it is a
# working direct route again; the mechanism is kept because parking is how a
# route is turned off without also hiding that it exists.
UNDER_CONSTRUCTION: dict[str, str] = {}


# The *fallback* list for each upstream — what to show when the live catalog
# has not been fetched (no network, or /model opened before the startup warm-up
# finished). The real list comes from the upstream's own status API; see
# models_for().
#
# This is a snapshot, so it rots: it was fourteen models, ten of which Logfare
# retired upstream, and /model kept offering them long after every request for
# them 404'd. Treat it as a starting point that /logfare-models corrects, not as
# the truth. Anything not in the live catalog returns "Model not found" (404) at
# request time.
#
# It now mirrors the *whole* live chat catalog rather than a safe subset, so a
# cold cache offers everything the proxy can route. The proxy config carries a
# wildcard, so a model added upstream works here even before this tuple learns
# about it — the tuple only decides what the picker shows before the fetch.
#
# Logfare's catalog also lists image/TTS/STT models (flux-*, sdxl-lightning,
# whisper-large-v3-turbo, aura-2-en, nova-3, phoenix-1.0, lucid-origin); they
# reject chat completions, so they are omitted here.
LOGFARE_MODELS: tuple[str, ...] = (
    "deepseek-v4.1-flash",
    "deepseek-v4-pro-0813",
    "gemma-4-26b",
    "glm-5.3",
    "glm-5.3-flash",
    "kimi-k2.6",
    "claude-haiku-5-5",
    "logfare/auto",
    "qwen-3.8-27b",
)

#: Token Juice's whole catalog is one model, and it is reached directly, so the
#: entry is the id the upstream itself serves — snapshot suffix included,
#: because that is what goes on the wire. It publishes no status API; a switch
#: probes the Models route and ``models_for`` prefers what that found.
TOKENJUICE_MODELS: tuple[str, ...] = (
    "deepseek-ai/DeepSeek-V4.1-Flash-1017",
)

#: IteraCompute's declared fallback. Its real catalog is fetched from the public
#: Models endpoint (see ``iteracompute.py``); this is what ``/model`` shows
#: before that fetch. The free model is the only one an account can use without
#: a balance, so it leads and is the one the switch selects.
ITERACOMPUTE_MODELS: tuple[str, ...] = (
    "qwen/qwen3.8-27b",
)

#: InfronAI's declared fallback. Its real catalog is fetched from the public
#: Models endpoint (see ``infronai.py``); this is what ``/model`` shows before
#: that fetch. The free lineup is six chat models and turns over, so these are
#: the ones measured present on 2026-10-20 — a starting point, not the truth.
INFRONAI_MODELS: tuple[str, ...] = (
    "deepseek/deepseek-v4.1-flash:free",
    "qwen/qwen3.8-27b:free",
    "apodex/apodex-1.1-mini:free",
    "inclusionai/ling-3.1-flash:free",
    "internlm/atria-dawn-preview:free",
    "qwen/qwen3.8-27b-abliterated-cyber:free",
)

#: NVIDIA's declared fallback — the ids that were **measured to answer a
#: tool-calling turn**, in the order they were measured fastest first. This is
#: deliberately not the catalog: ``/v1/models`` lists 83 entries and 57 of them
#: are chat-shaped, but seven of the eighteen sampled on 2026-10-22 answered
#: ``404 Not found for account`` and two more were retired. A picker that
#: offered the catalog would hand the operator models this account cannot call.
#: The live catalog still wins once fetched (see ``models_for``); this is what
#: ``/model`` shows on a cold cache, and every entry here is known to work.
NVIDIA_MODELS: tuple[str, ...] = (
    "nvidia/nemotron-3.5-lightning-30b-a3b",   # 2.2s with tools, 1M ctx
    "openai/gpt-oss-20b",                      # 7.0s with tools, 128K ctx
    "stepfun-ai/step-5-preview",               # 1.9s with tools
    "poolside/laguna-xs-2.1",                  # 44s with tools — slow but works
    "meta/muse-glimmer-30b",                   # 14.5s with tools
)

MODELS_BY_KEY: dict[str, tuple[str, ...]] = {
    "logfare": LOGFARE_MODELS,
    "tokenjuice": TOKENJUICE_MODELS,
    "iteracompute": ITERACOMPUTE_MODELS,
    "infronai": INFRONAI_MODELS,
    "nvidia": NVIDIA_MODELS,
}


def catalog_for(base_url: str):
    """The active source's live catalog, or ``None`` if it has none.

    Only Logfare publishes one. Returned as the source module's own ``Model``
    objects so the caller can read health without knowing where it came from.
    Never fetches: this is the cache, and a cold cache is ``None`` — /model runs
    on the UI thread and must not block on the network.
    """
    if active_key(base_url) != "logfare":
        return None
    from . import logfare as lf_mod
    return lf_mod.cached_models()


def models_for(base_url: str) -> tuple[str, ...] | None:
    """The model list for whichever source this endpoint points at.

    None means "not a known source" — the caller should fall back to the
    default registry rather than showing an empty list.

    Logfare's, TokenHarbour's and OpenRouter's lists are fetched, not declared,
    so they come from a catalog cache rather than MODELS_BY_KEY. A cold cache
    yields None, and the caller falls back to the declared list — which is why
    the per-source *-models commands exist to warm it.
    """
    key = active_key(base_url)
    if key is None:
        # A direct OpenAI-compatible endpoint is not a Source — it has no port,
        # profile or service — but if it was probed, its own ids are the right
        # answer for `/model`, and they are a cached read rather than a fetch.
        from . import openai_compat as oa_mod
        probed = oa_mod.cached_ids(base_url)
        return probed or None
    if key == "nvidia":
        # Checked before the generic DIRECT_ONLY branch: NVIDIA has its own
        # module cache (it filters the catalog to chat models and knows about
        # ids the endpoint lists but the account cannot call), and that is the
        # list `/model` should offer — not the raw Models route the generic
        # branch would read.
        from . import nvidia as nv_mod
        cached = nv_mod.cached_ids()
        return tuple(cached) if cached else (MODELS_BY_KEY.get(key) or None)
    if key in DIRECT_ONLY:
        # Declared, not fetched — but a switch probes the Models route, and what
        # the upstream actually answered is better than the declared fallback.
        from . import openai_compat as oa_mod
        probed = oa_mod.cached_ids(DECLARED_ROUTES[key].base_url)
        return tuple(probed) if probed else (MODELS_BY_KEY.get(key) or None)
    if key == "logfare":
        from . import logfare as lf_mod
        cached = lf_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "tokenharbour":
        from . import tokenharbour as th_mod
        cached = th_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "openrouter":
        from . import openrouter as or_mod
        cached = or_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "unorouter":
        from . import unorouter as uno_mod
        cached = uno_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "antseed":
        from . import antseed as as_mod
        cached = as_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "aihubmix":
        from . import aihubmix as ah_mod
        cached = ah_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "beatapi":
        from . import beatapi as bp_mod
        cached = bp_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "iteracompute":
        from . import iteracompute as ic_mod
        cached = ic_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "infronai":
        from . import infronai as inf_mod
        cached = inf_mod.cached_ids()
        return tuple(cached) if cached else None
    return MODELS_BY_KEY.get(key) or None


def catalog_models(key: str) -> list | None:
    """The cached catalog of *key*, as that source module's own ``Model`` list.

    ``None`` for a source with no catalog (Logfare's is health-only and carries
    no window) or a cold cache. Kept as a small dispatch rather than a dict of
    importers so the modules stay lazily imported — ``sources`` is imported by
    their callers, and a module-level import would make them import each other.
    """
    if key == "tokenharbour":
        from . import tokenharbour as th_mod
        return th_mod.cached_models()
    if key == "openrouter":
        from . import openrouter as or_mod
        return or_mod.cached_models()
    if key == "unorouter":
        from . import unorouter as uno_mod
        return uno_mod.cached_models()
    if key == "antseed":
        from . import antseed as as_mod
        return as_mod.cached_models()
    if key == "aihubmix":
        from . import aihubmix as ah_mod
        return ah_mod.cached_models()
    if key == "beatapi":
        from . import beatapi as bp_mod
        return bp_mod.cached_models()
    if key == "iteracompute":
        from . import iteracompute as ic_mod
        return ic_mod.cached_models()
    if key == "infronai":
        from . import infronai as inf_mod
        return inf_mod.cached_models()
    if key == "nvidia":
        from . import nvidia as nv_mod
        return nv_mod.cached_models()
    return None


def context_window_for(base_url: str, model: str) -> int | None:
    """The active model's real context window, from its source's live catalog.

    ``None`` when nothing knows — an unknown source, a cold cache, or a catalog
    that simply does not publish a window (Logfare's status API has no such
    field). The caller falls back to ``Config.context_window``, so a ``None``
    here is not a failure: it means "use the default".

    Read from the cache and never fetched, for the same reason ``catalog_for``
    never fetches: this is called on the turn path, and a blocking network read
    there would stall the very loop it is measuring.

    The field is spelled ``context_length`` in every module that has one, but it
    is read by name off whatever the module's own ``Model`` exposes rather than
    by importing four dataclasses to unify them — that keeps each source's
    parsing where it already lives.
    """
    key = active_key(base_url)
    if key is None:
        return None
    cached = catalog_models(key)
    if not cached:
        return None
    for entry in cached:
        if getattr(entry, "id", None) != model:
            continue
        window = getattr(entry, "context_length", None)
        if isinstance(window, int) and window > 0:
            return window
        return None
    return None


def catalog_price_for(base_url: str, model: str) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok from the active source's live catalog, or ``None``.

    The configured estimate table is keyed by this harness's own model names,
    and a gateway's ids are not always the same string: IteraCompute serves
    ``qwen/qwen3.8-27b`` where Logfare serves ``qwen-3.8-27b``, so the table
    misses it and bills a free model at ``DEFAULT_PRICE``. The catalog knows the
    real answer — including that it is free — so it is consulted first.

    ``None`` means "the catalog cannot say", and the caller falls back to the
    estimate. Read from the cache and never fetched, for the same reason
    ``context_window_for`` does: this is on the turn path.
    """
    key = active_key(base_url)
    if key is None:
        return None
    if key == "nvidia" and not catalog_models(key):
        # A cold cache is not "cannot say" for NVIDIA. Its hosted catalog is
        # billed against the account's developer allowance rather than per
        # token, and it publishes no price at all — so the answer is the same
        # warm or cold. Returning None here would fall through to the estimate
        # table and bill a free model at DEFAULT_PRICE, which is the exact
        # discrepancy this function exists to prevent.
        return (0.0, 0.0)
    cached = catalog_models(key)
    if not cached:
        return None
    for entry in cached:
        if getattr(entry, "id", None) != model:
            continue
        if getattr(entry, "free", False):
            return (0.0, 0.0)
        # A module that records whether a price was actually read must be
        # believed: its default ``price_in``/``price_out`` are 0.0, so an
        # unpriced entry would otherwise be reported as free. Modules with no
        # such field keep the old behaviour, where the pair is the answer.
        if getattr(entry, "priced", True) is False:
            return None
        price_in = getattr(entry, "price_in", None)
        price_out = getattr(entry, "price_out", None)
        if isinstance(price_in, (int, float)) and isinstance(price_out, (int, float)):
            return (float(price_in), float(price_out))
        return None
    return None


def max_output_for(base_url: str, model: str) -> int | None:
    """The active model's real output ceiling, from its source's live catalog.

    ``None`` when nothing knows, and the caller then keeps the configured
    ``max_tokens``. The distinction matters: this is a **cap**, so a catalog
    that does not publish one must not be read as "zero".

    Only some catalogs publish the field — AIHubMix, UnoRouter, AntSeed and
    IteraCompute do, under ``max_output`` or ``max_output_tokens``; the rest do
    not. Read by name off each module's own ``Model`` for the same reason
    ``context_window_for`` does, and from the cache rather than by fetching,
    because this is called on the turn path.
    """
    key = active_key(base_url)
    if key is None:
        return None
    cached = catalog_models(key)
    if not cached:
        return None
    for entry in cached:
        if getattr(entry, "id", None) != model:
            continue
        for attr in ("max_output", "max_output_tokens"):
            value = getattr(entry, attr, None)
            if isinstance(value, int) and value > 0:
                return value
        return None
    return None


def _first(paths: list[Path]) -> Path | None:
    for p in paths:
        if p and p.is_file():
            return p
    return None


def _service_candidates(key: str) -> list[Path]:
    if key == "logfare":
        return [
            ZIMZILLA_HOME / "logfare" / "start-litellm.sh",
            DEFAULT_ROOT / "packaging" / "logfare" / "start-litellm.sh",
        ]
    if key == "iteracompute":
        return [
            ZIMZILLA_HOME / "iteracompute" / "start-litellm.sh",
            DEFAULT_ROOT / "packaging" / "iteracompute" / "start-litellm.sh",
        ]
    if key == "infronai":
        return [
            ZIMZILLA_HOME / "infronai" / "start-litellm.sh",
            DEFAULT_ROOT / "packaging" / "infronai" / "start-litellm.sh",
        ]
    # Token Juice has no proxy, so there is no manager to find. A copy left in
    # ~/.zimzilla by an older install is deliberately ignored rather than
    # started: the direct route is the only one that exists now.
    return []


def _config_candidates(key: str) -> list[Path]:
    if key == "logfare":
        return [
            ZIMZILLA_HOME / "logfare" / "litellm-config.yaml",
            DEFAULT_ROOT / "packaging" / "logfare" / "litellm-config.yaml",
        ]
    if key == "iteracompute":
        return [
            ZIMZILLA_HOME / "iteracompute" / "litellm-config.yaml",
            DEFAULT_ROOT / "packaging" / "iteracompute" / "litellm-config.yaml",
        ]
    if key == "infronai":
        return [
            ZIMZILLA_HOME / "infronai" / "litellm-config.yaml",
            DEFAULT_ROOT / "packaging" / "infronai" / "litellm-config.yaml",
        ]
    # Same reasoning as _service_candidates: Token Juice's route is declared in
    # DECLARED_ROUTES, not read from a proxy config.
    return []


def _profile_candidates(key: str) -> list[Path]:
    if key == "logfare":
        return [
            ZIMZILLA_HOME / "logfare" / "source",
            DEFAULT_ROOT / "packaging" / "logfare" / "source",
        ]
    if key == "iteracompute":
        # No shipped profile: the key is the operator's own. setup.sh writes it
        # when $ANTHROPIC_AUTH_TOKEN is set, and /iteracompute-api-setup writes
        # it otherwise, so there is nothing to fall back to in the tree.
        return [
            ZIMZILLA_HOME / "iteracompute" / "source",
        ]
    if key == "infronai":
        # Same shape as IteraCompute: no credential ships, and none could.
        return [
            ZIMZILLA_HOME / "infronai" / "source",
        ]
    if key == "nvidia":
        # No credential ships either, and none could: an NVIDIA key is the
        # operator's own developer key. Until /nvidia-api-setup writes this
        # file the source is not discovered, which is what makes /zim-nvidia
        # name the setup command instead of failing at request time.
        return [
            ZIMZILLA_HOME / "nvidia" / "source",
        ]
    return [
        ZIMZILLA_HOME / "tokenjuice" / "source",
        DEFAULT_HOME / "claude-source" / "deepseek-claude",
        # The shared key, last. A personal profile or the older home layout
        # always wins, so a shipped key can be replaced locally without a
        # repository change — and every install gets a working route with no
        # signup.
        DEFAULT_ROOT / "packaging" / "tokenjuice" / "source",
    ]


def _build(key: str, label: str, port: int, model: str) -> Source | None:
    profile = _first(_profile_candidates(key))
    service = _first(_service_candidates(key))
    config = _first(_config_candidates(key))
    if profile is None:
        return None
    # A direct-only source needs no proxy files; every other one needs all
    # three, because the proxy is what the route *is*.
    if key not in DIRECT_ONLY and (service is None or config is None):
        return None
    return Source(
        key=key, label=label, port=port, profile=profile,
        service=service, config=config, model=model, root=DEFAULT_ROOT,
        models=MODELS_BY_KEY.get(key, ()),
        direct=DECLARED_ROUTES.get(key),
        under_construction=UNDER_CONSTRUCTION.get(key),
    )


def discover() -> dict[str, Source]:
    """Every source this machine can switch to, keyed by name.

    A proxy route is present when its profile, service and config are all
    there; a direct-only one (Token Juice) when its profile is.
    """
    found: dict[str, Source] = {}
    logfare = _build("logfare", "Logfare", 4001, "deepseek-v4.1-flash")
    if logfare:
        found["logfare"] = logfare
    # No proxy and no port: the profile is the whole install, and its absence
    # means "no key", which is a route that cannot answer rather than one that
    # is broken.
    tokenjuice = _build("tokenjuice", "Token Juice", 0, "deepseek-v4.1-flash")
    if tokenjuice:
        found["tokenjuice"] = tokenjuice
    # The service and config ship in the tree, so this is discovered on every
    # install; only the profile is the operator's, and without it the source is
    # not offered rather than offered and broken at switch time.
    iteracompute = _build("iteracompute", "IteraCompute", 4002, "qwen/qwen3.8-27b")
    if iteracompute:
        found["iteracompute"] = iteracompute
    # Same arrangement, one port over: the proxy files ship in the tree, and
    # only the operator's key can be missing.
    infronai = _build("infronai", "InfronAI", 4003,
                      "deepseek/deepseek-v4.1-flash:free")
    if infronai:
        found["infronai"] = infronai
    # Direct-only, like Token Juice: no port and no proxy files, so the profile
    # is the whole install. Absent until /nvidia-api-setup has run, which is
    # the honest state — a route with no key cannot answer.
    nvidia = _build("nvidia", "NVIDIA NIM", 0,
                    "nvidia/nemotron-3.5-lightning-30b-a3b")
    if nvidia:
        found["nvidia"] = nvidia
    return found


def source_for_port(port: int) -> str | None:
    """Which source key owns a given proxy port, if any."""
    if port <= 0:
        # A direct-only source has no port to match on — it is identified by
        # the host of its declared route instead (see ``active_key``).
        return None
    for key, src in discover().items():
        if src.port == port:
            return key
    return None


def switchable(exclude: str | None = None) -> list[str]:
    """Source keys this machine could switch to right now, best first.

    "Right now" is meant literally, because the caller is a *failure* message:
    telling an operator to run ``/zim-openrouter`` when no OpenRouter key is
    set moves them from one error to another. So a key is only offered when it
    would actually work —

    * a local proxy is offered when ``discover()`` finds its profile, service
      and config (it can be started on demand);
    * a hosted gateway is offered when it has a credential;
    * AntSeed is offered when a buyer is answering, since it needs no key but
      does need the proxy up;
    * anything parked is never offered.

    ``exclude`` drops the source that just refused the request, so a message
    never suggests the thing that just failed. Ordered by preference rather
    than alphabetically: the hosted gateways are already running and answer
    immediately, while a local proxy has to be started first.
    """
    found = discover()
    out: list[str] = []
    for key in ("openrouter", "unorouter", "aihubmix", "beatapi",
                "tokenharbour", "infronai", "iteracompute", "antseed",
                "nvidia", "tokenjuice", "logfare"):
        if key == exclude or key in UNDER_CONSTRUCTION:
            continue
        if key in found:
            out.append(key)
            continue
        try:
            if key == "antseed":
                from . import antseed as as_mod
                if as_mod.reachable():
                    out.append(key)
                continue
            if key == "logfare":
                # Always available: the shipped profile makes it work on a
                # fresh clone, and its own 429 is why `exclude` exists.
                out.append(key)
                continue
            if key in BUILTIN_KEYS:
                mod = _gateway_module(key)
                if mod is not None and mod.load_credentials() is not None:
                    out.append(key)
        except Exception:  # noqa: BLE001
            # A source that cannot even be asked is not one to recommend.
            continue
    return out


def _gateway_module(key: str):
    """The module that owns a hosted gateway's credential, or ``None``.

    Imported locally for the same reason ``active_key`` does it: these modules
    are imported by ``sources``' callers, and a module-level import would make
    them import each other.
    """
    from . import aihubmix as ah_mod
    from . import beatapi as bp_mod
    from . import openrouter as or_mod
    from . import tokenharbour as th_mod
    from . import unorouter as uno_mod
    return {
        "openrouter": or_mod, "unorouter": uno_mod, "aihubmix": ah_mod,
        "beatapi": bp_mod, "tokenharbour": th_mod,
    }.get(key)


def display_model(base_url: str, model: str) -> str:
    """The model name to SHOW for *model* on this endpoint.

    The name sent and the name worth showing are not always the same string.
    Token Juice's upstream serves ``deepseek-ai/DeepSeek-V4.1-Flash-1017`` — a
    snapshot-suffixed id whose suffix rotates — while the stable alias
    ``deepseek-v4.1-flash`` is what a profile written against the old proxy
    still names. Either can be the current model, and neither is the name worth
    reading: the alias hides which snapshot is answering, and the full id buries
    it in a vendor prefix.

    So the display name is the alias with the upstream snapshot suffix appended
    (``deepseek-v4.1-flash-1017``), derived from the route's own alias map
    rather than hardcoded: the suffix rotates, and a literal here would be
    wrong the next time it does.

    Falls back to *model* unchanged when the endpoint is not a known source, the
    name is not part of an alias pair, or the upstream id is not simply the
    alias plus a suffix — a different model entirely is shown as-is rather than
    mangled. Never fetches: this is called on the render path.
    """
    key = active_key(base_url)
    if key is None:
        return model
    route = direct_route(discover().get(key))
    if route is None:
        return model
    upstream = route.models.get(model)
    if upstream is None:
        # `model` may already be the upstream id — that is what a direct switch
        # stores — so look for the alias it belongs to instead.
        upstream = model
        aliases = [a for a, u in route.models.items() if u == model]
    else:
        aliases = [model]
    for alias in aliases:
        # `deepseek-ai/DeepSeek-V4.1-Flash-1017` -> `deepseek-v4.1-flash-1017`
        leaf = upstream.rsplit("/", 1)[-1].lower()
        if leaf.startswith(alias) and leaf != alias:
            return leaf
    return model


# ---------------------------------------------------------------------------
# current selection (persisted so a restart keeps the working source)
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    return ZIMZILLA_HOME / "current-source"


#: Sources that are always selectable, whether or not `discover()` finds them
#: on this machine. TokenHarbour, OpenRouter, UnoRouter and AIHubMix are remote:
#: they have no local profile, service or config to discover, so gating them on
#: discover() would silently discard the selection and drop a session back onto
#: Logfare at the next launch. AntSeed is local but equally undiscoverable by
#: `discover()`, which only knows the two LiteLLM proxies — its buyer is started
#: and stopped by the operator, so requiring it to be up at selection time would
#: lose the choice every time the proxy was down.
BUILTIN_KEYS = frozenset({"tokenharbour", "openrouter", "unorouter", "antseed",
                          "aihubmix", "beatapi"})


def current_key() -> str | None:
    """The source the user last selected, if it is still a valid one.

    A discovered source is valid while its files are present. A built-in one is
    valid always — there is nothing on disk that could go missing. A parked one
    is valid never: see UNDER_CONSTRUCTION, which is why this is checked before
    the others rather than after.
    """
    try:
        key = _state_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if key in UNDER_CONSTRUCTION:
        return None
    if key in BUILTIN_KEYS:
        return key
    return key if key in discover() else None


def set_current(key: str) -> None:
    """Record the selection. Never fatal — a read-only home must not break the TUI.

    A parked source may be written here; it is ``current_key`` that refuses to
    read one back. Keeping the write unconditional means this stays the plain
    "remember the choice" function it reads as, and the parking decision lives
    in exactly one place rather than being half-applied at both ends.
    """
    try:
        ZIMZILLA_HOME.mkdir(parents=True, exist_ok=True)
        _state_file().write_text(key + "\n", encoding="utf-8")
    except OSError:
        pass


def active_key(base_url: str) -> str | None:
    """Best guess at which source the running session is pointed at.

    The local proxies are identified by their port. The hosted gateways are
    remote, so they are identified by host instead — and checked first, because
    a hosted URL has no port to match on and would otherwise fall through to
    ``None``. Each gateway exposes an ``is_<name>`` predicate, so the host
    string lives in exactly one module rather than being spelled again here.
    """
    # Imported locally: sources is imported by these modules' callers, and a
    # module-level import would make them import each other.
    from . import aihubmix as ah_mod
    from . import antseed as as_mod
    from . import beatapi as bp_mod
    from . import infronai as inf_mod
    from . import iteracompute as ic_mod
    from . import nvidia as nv_mod
    from . import openrouter as or_mod
    from . import tokenharbour as th_mod
    from . import unorouter as uno_mod
    if th_mod.is_tokenharbour(base_url):
        return "tokenharbour"
    if or_mod.is_openrouter(base_url):
        return "openrouter"
    if uno_mod.is_unorouter(base_url):
        return "unorouter"
    if ah_mod.is_aihubmix(base_url):
        return "aihubmix"
    if bp_mod.is_beatapi(base_url):
        return "beatapi"
    # Checked by port before the generic regex for the same reason AntSeed is:
    # it is a local proxy, and identifying it by its own predicate keeps the
    # port in one module rather than spelling it here too. It also means the
    # source is recognised even when `discover()` has no profile to find.
    if ic_mod.is_iteracompute(base_url):
        return "iteracompute"
    # Same reasoning as IteraCompute above: a local proxy identified by its own
    # predicate, so the port lives in one module rather than being spelled here.
    if inf_mod.is_infronai(base_url):
        return "infronai"
    # NVIDIA is identified by host, not port: it is reached directly and has no
    # proxy to match on. Checked before the generic host loop below so a
    # session launched from an environment profile — with no file for
    # discover() to find — is still recognised as being on this source.
    if nv_mod.is_nvidia(base_url):
        return "nvidia"
    # Checked before the port match, and by port rather than by name: the buyer
    # hands out a plain http://localhost:8377 with no "antseed" in it, and the
    # regex below would otherwise find port 8377, miss it in source_for_port()
    # (which only knows the two LiteLLM proxies) and return None.
    if as_mod.is_antseed(base_url):
        return "antseed"
    # A source reached directly, with no proxy in the middle: identified by the
    # upstream host its own config names. Without this the session would look
    # source-less — `/zim-source` would not mark the route, `/model` would fall
    # back to the default snapshot, and switching to the source it is already on
    # would redo the work instead of saying so.
    host = _host_of(base_url)
    if host and host not in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        for key, src in discover().items():
            route = direct_route(src)
            if route is not None and _host_of(route.base_url) == host:
                return key
    m = re.search(r":(\d{2,5})(?:/|$)", base_url or "")
    if not m:
        return None
    return source_for_port(int(m.group(1)))


# ---------------------------------------------------------------------------
# profile / proxy
# ---------------------------------------------------------------------------

def load_profile(src: Source) -> dict[str, str]:
    """Read the profile's exports without a shell, so nothing is echoed.

    Parses the ``export VAR="value"`` lines the profiles are written in. A
    profile that uses anything more exotic than that is read by the launcher
    anyway; this is only for the in-TUI switch.
    """
    out: dict[str, str] = {}
    try:
        text = src.profile.read_text(encoding="utf-8")
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
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    # A template is not a credential. The shipped Logfare profile carries
    # `lfu_REPLACE_ME` so the file's shape is visible in the checkout; sending
    # that upstream would turn "you have not set a key yet" into a 401 that
    # looks like the account is broken.
    from . import logfare as lf_mod
    if lf_mod.is_placeholder_token(out.get("ANTHROPIC_AUTH_TOKEN", "")):
        out.pop("ANTHROPIC_AUTH_TOKEN", None)
    return out


#: The profile `/zim-tokenjuice <key>` writes. The shared profile in the tree is
#: a read-only fallback, so a personal key always goes to the operator's own
#: file — never into the checkout.
def tokenjuice_profile() -> Path:
    return ZIMZILLA_HOME / "tokenjuice" / "source"


def is_tokenjuice_key(value: str | None) -> bool:
    """Whether *value* looks like a Token Juice key rather than a command arg.

    A hint, not a gate: the upstream is the authority. It exists so
    ``/zim-tokenjuice tj_…`` can be told apart from ``/zim-tokenjuice proxy``.
    """
    value = (value or "").strip()
    return value.startswith("tj_") and 16 <= len(value) <= 200


def save_token(token: str) -> Path:
    """Write *token* to the operator's Token Juice profile, mode 600.

    Rewritten rather than appended, so setting a key twice cannot leave two
    ``ANTHROPIC_AUTH_TOKEN`` lines for the shell to disagree about. The shipped
    profile is the template when this is the first key: it already carries the
    upstream URL, the model id, and the empty ``ANTHROPIC_API_KEY`` that keeps
    the token authoritative.
    """
    token = (token or "").strip()
    profile = tokenjuice_profile()
    lines: list[str] = []
    for source in (profile, DEFAULT_ROOT / "packaging" / "tokenjuice" / "source"):
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
            break
        except OSError:
            continue
    if not lines:
        lines = [
            "# ZimZilla — Token Juice profile (written by /zim-tokenjuice)",
            "# Treat this file as a credential. Never commit it.",
            "",
            'export ANTHROPIC_BASE_URL="https://api.tokenjuice.ai/v1"',
            'export ANTHROPIC_MODEL="deepseek-ai/DeepSeek-V4.1-Flash-1017"',
            'export ANTHROPIC_API_KEY=""',
        ]

    out: list[str] = []
    seen = False
    for line in lines:
        if line.strip().startswith("export ANTHROPIC_AUTH_TOKEN="):
            out.append(f'export ANTHROPIC_AUTH_TOKEN="{token}"')
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f'export ANTHROPIC_AUTH_TOKEN="{token}"')
    if not any(ln.strip().startswith("export ANTHROPIC_API_KEY=") for ln in out):
        # Load-bearing: an empty string is what stops a real API key inherited
        # from the shell from taking precedence over the token above.
        out.append('export ANTHROPIC_API_KEY=""')

    profile.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(profile.parent, 0o700)
    profile.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(profile, 0o600)
    return profile


def proxy_healthy(port: int, timeout: float = 2.0) -> bool:
    url = f"http://127.0.0.1:{port}/health/liveliness"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _service_env(src: Source) -> dict[str, str]:
    """The environment a source's proxy is started with.

    The profile is the credential channel: LiteLLM reads ``master_key`` and each
    deployment's ``api_key`` from it via ``os.environ``, which is why writing a
    new key to that file does nothing to a proxy that is already running.
    """
    env = dict(os.environ)
    env.setdefault("ZIMZILLA_ROOT", str(src.root))
    env["LITELLM_ENV_FILE"] = str(src.profile)
    env["LITELLM_CONFIG"] = str(src.config)
    env["LITELLM_PORT"] = str(src.port)
    return env


def _run_service(src: Source, action: str, timeout: float) -> tuple[bool, str]:
    """Run the source's service script with *action*; report health, not exit 0."""
    try:
        proc = subprocess.run(
            [str(src.service), action], env=_service_env(src),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s"
    except OSError as e:
        return False, f"could not run {src.service}: {e}"
    if proxy_healthy(src.port):
        return True, action
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, detail[-1] if detail else f"exit {proc.returncode}"


def start_proxy(src: Source, timeout: float = 180.0) -> tuple[bool, str]:
    """Start the source's proxy if it is not already answering. Blocking."""
    if src.is_direct_only or src.service is None:
        # Nothing to start: the upstream is reached directly, and a key saved
        # to the profile is live on the next request.
        return False, f"{src.label} has no local proxy — it is reached directly"
    if proxy_healthy(src.port):
        return True, "already running"
    if not src.service.is_file():
        return False, f"no service manager at {src.service}"

    # Do not wrap this in flock. The launcher already serialises its own
    # cold-start, and wrapping here is how a leaked lock (LiteLLM inheriting
    # the launcher's lock fd) made the in-TUI switch hang until timeout while
    # a manual start of the same script finished in seconds. Two sources
    # live on different ports, so they can come up independently.
    return _run_service(src, "start", timeout)


def restart_proxy(src: Source, timeout: float = 180.0) -> tuple[bool, str]:
    """Restart the source's proxy so it picks up a key written to its profile.

    A key reaches LiteLLM through the environment it was started with, and that
    environment is the profile — so a key saved while the proxy is up is not the
    key the proxy is using, on either side: the master key it demands of this
    client, and the upstream key it sends. Same no-flock reasoning as
    :func:`start_proxy`. Blocking; callers run it off the UI thread.
    """
    if src.is_direct_only or src.service is None:
        return False, f"{src.label} has no local proxy — it is reached directly"
    if not src.service.is_file():
        return False, f"no service manager at {src.service}"
    return _run_service(src, "restart", timeout)
