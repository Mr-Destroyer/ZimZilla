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

Two local LiteLLM proxies front two different upstreams:

    iteracompute  :4002  →  api.iteracompute.com (OpenAI-only; free Qwen tier)
    infronai      :4003  →  llm.onerouter.pro  (OpenAI-only; free model tier)

Logfare used to be the third. Its upstream serves Chat Completions itself, so
the proxy is gone: ``https://logfare.ai/v1`` is the default route, reached
directly with the same profile key (see ``LOGFARE_DIRECT``). ``:4001`` is still
recognised so a profile written by an older install is migrated at launch
rather than pointed at an address nothing answers.

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

Zro (MoonMath.ai, ``zro.moonmath.ai``) is a hosted gateway like TokenHarbour,
but it is the one that makes the split explicit: it serves Messages natively at
the host root *and* Chat Completions under ``/v1``. ZimZilla uses the native
Messages surface, so there is no translation and no proxy — ``/zim-moonmath``
points the session at ``https://zro.moonmath.ai`` and nothing else has to
exist. Its catalog is public and priced, which is what lets
``/moonmath-models`` show the Free plan's models before a key is set; see
:mod:`zimzilla.moonmath`.

InferX (``model.inferx.net``) is the same shape again — Chat Completions served
by the upstream, no proxy, the profile is the whole install — with one
difference that matters for the switch: its catalog is **key-gated**. Without a
credential the Models route answers 401 rather than a list, so the probe
``/zim-inferx`` makes is a real key check, and ``/inferx-models`` needs the key
that ``/inferx-api-setup`` stores. Calls draw on the key's tenant quota rather
than per-token billing; see :mod:`zimzilla.inferx`.

Inferbase (``api.inferbase.ai``) is a routing gateway reached directly, and it
is the one direct source whose catalog is **priced**: a key-gated
``/api/v1/inference/catalog`` publishes a window, an output ceiling and both
rates per model, while the public ``/v1/models`` lists the served ids. The two
are read together, because neither is complete on its own. Its free plan is a
**price band** — models at or below $2.00 per 1M output tokens are included and
anything above answers 402 — so ``free`` here is a published rate rather than an
allowance; see :mod:`zimzilla.inferbase`.

Flex (``eu.api.flex.ai``) is the odd one out, and the difference is worth
stating plainly: its playground route is **unauthenticated**. There is no key to
store, so its profile is a route record rather than a credential, and
``/zim-flex`` needs no setup command. It is also a *playground* rather than an
API — 10 messages, 2000 characters in the last one, no ``tool_calls``, and
failures delivered as a 200 with an SSE error frame — so it is a chat route and
not a route this harness's tool loop can drive. See :mod:`zimzilla.flex` and
``openai_compat.PLAYGROUND_HOSTS``.

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


#: wandb's route, declared rather than read from a proxy config.
#:
#: ``api.inference.wandb.ai/v1`` speaks Chat Completions itself, so — like
#: Token Juice and NVIDIA — there is no LiteLLM hop and nothing to start. The
#: alias map is empty on purpose: the catalog's ids are already the ids that go
#: on the wire. See :mod:`zimzilla.wandb`.
WANDB_DIRECT = DirectRoute(base_url="https://api.inference.wandb.ai/v1")

#: InferX's route, declared rather than read from a proxy config.
#:
#: ``model.inferx.net/endpoints/v1`` speaks Chat Completions itself, so — like
#: Token Juice, NVIDIA and wandb — there is no LiteLLM hop and nothing to
#: start. The alias map is empty on purpose: the catalog's ids are already the
#: ids that go on the wire (``deepseek-v4.1-flash-1017``, ``glm-5.3``, …), and
#: there is no proxy name to translate. See :mod:`zimzilla.inferx`.
INFERX_DIRECT = DirectRoute(base_url="https://model.inferx.net/endpoints/v1")

#: Inferbase's route, declared rather than read from a proxy config.
#:
#: ``api.inferbase.ai/v1`` speaks Chat Completions itself, so — like Token
#: Juice, NVIDIA, wandb and InferX — there is no LiteLLM hop and nothing to
#: start. The alias map is empty on purpose: the catalog's ids are already the
#: ids that go on the wire (``deepseek-v4-flash``, ``zai-glm-4-7-flash``,
#: ``auto``, …), and a provider's own spelling is accepted as an alias by the
#: gateway rather than needing translation here. See
#: :mod:`zimzilla.inferbase`.
INFERBASE_DIRECT = DirectRoute(base_url="https://api.inferbase.ai/v1")

#: Flex's playground route, declared rather than read from a proxy config.
#:
#: ``eu.api.flex.ai/api/playground/chat`` speaks Chat Completions itself, so —
#: like Token Juice, NVIDIA, wandb, InferX and Inferbase — there is no LiteLLM
#: hop and nothing to start. Two things make it different from every other
#: route in this table:
#:
#: * **It needs no credential.** The route is unauthenticated, so the profile
#:   below is a route record and ``/zim-flex`` has no setup command to name.
#: * **The URL is the endpoint, not a base.** There is no ``/v1`` to append to,
#:   which is why ``openai_compat.chat_completions_url`` returns it untouched
#:   for this host rather than building ``…/chat/v1/chat/completions``.
#:
#: The alias map is empty: the playground's ids are already the ids that go on
#: the wire (``DeepSeek-V4-Flash-0731``, ``gpt-oss-20b``, …). See
#: :mod:`zimzilla.flex`.
FLEX_DIRECT = DirectRoute(base_url="https://eu.api.flex.ai/api/playground/chat")

#: Logfare's route, declared rather than read from a proxy config.
#:
#: ``https://logfare.ai/v1`` serves Chat Completions itself, so the bundled
#: LiteLLM proxy is no longer part of the route: the harness sends the same
#: bearer token straight upstream. The alias map keeps the haiku names Claude
#: Code probes with pointed at a model Logfare actually serves, because the
#: upstream does not know those names. Catalog ids (``deepseek-v4.1-flash``,
#: ``qwen-3.8-27b``, …) pass through unchanged.
LOGFARE_DIRECT = DirectRoute(
    base_url="https://logfare.ai/v1",
    models={
        "claude-3-5-haiku-latest": "deepseek-v4.1-flash",
        "claude-3-5-haiku": "deepseek-v4.1-flash",
        "claude-haiku-4-5-20251001": "deepseek-v4.1-flash",
    },
)

#: Gemini's route, declared rather than read from a proxy config.
#:
#: ``generativelanguage.googleapis.com/v1beta/openai`` speaks Chat Completions
#: itself, so — like Token Juice, NVIDIA, wandb, InferX, Inferbase and Flex —
#: there is no LiteLLM hop and nothing to start. Two things make it different
#: from every other route in this table:
#:
#: * **The base is not ``/v1``.** It is ``/v1beta/openai``, which is why
#:   :func:`zimzilla.openai_compat.chat_completions_url` special-cases this host
#:   rather than appending ``/v1/chat/completions`` to it.
#: * **The catalog is key-gated.** ``/models`` answers 404 without a credential,
#:   so the switch probe is a real key check and ``/gemini-models`` needs the
#:   key before it can list anything.
#:
#: The alias map is empty: the catalog's ids are already the ids that go on the
#: wire (``gemini-flash-lite-latest``, ``gemma-4-31b-it``, ...). See
#: :mod:`zimzilla.gemini`.
GEMINI_DIRECT = DirectRoute(
    base_url="https://generativelanguage.googleapis.com/v1beta/openai")

#: Sources with no proxy at all. Their credential profile is the only file that
#: has to exist; service and config are absent by design, and ``direct`` says
#: where they go.
DIRECT_ONLY: frozenset[str] = frozenset({"logfare", "tokenjuice", "nvidia", "wandb",
                                         "inferx", "inferbase", "flex", "gemini"})

#: Declared routes for the direct-only sources, keyed like ``MODELS_BY_KEY``.
DECLARED_ROUTES: dict[str, DirectRoute] = {
    "logfare": LOGFARE_DIRECT,
    "tokenjuice": TOKENJUICE_DIRECT,
    "nvidia": NVIDIA_DIRECT,
    "wandb": WANDB_DIRECT,
    "inferx": INFERX_DIRECT,
    "inferbase": INFERBASE_DIRECT,
    "flex": FLEX_DIRECT,
    "gemini": GEMINI_DIRECT,
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

#: wandb's declared fallback. Its real catalog is fetched from the public
#: Models endpoint (see ``wandb.py``); this is what ``/model`` shows before
#: that fetch. The single entry is the model measured to answer a chat turn
#: in a few seconds — the one the operator asked for by name.
WANDB_MODELS: tuple[str, ...] = (
    "deepseek-ai/DeepSeek-V4.1-Flash",
)

#: InferX's declared fallback — the whole catalog as measured on 2026-11-05,
#: catalog order preserved. Unlike the other direct sources this one is *not* a
#: curated subset: every id the key-gated ``/models`` route listed is a chat
#: model, so the fallback is the list itself rather than a selection from it.
#: The live fetch (see ``inferx.py``) replaces it as soon as the switch or a
#: warm-up runs.
INFERX_MODELS: tuple[str, ...] = (
    "Agents-A1",
    "Devstral-2-123B-Instruct-2512-int4-AutoRound",
    "Qwen3-Coder-Next-FP8",
    "Qwen3.6-35B-A3B-FP8",
    "Qwen3.8-27B-FP8",
    "gemma-4-31B-it-fp8",
    "gpt-oss-20b",
    "deepseek-v4-flash-0731",
    "deepseek-v4.1-flash",
    "deepseek-v4.1-flash-1017",
    "deepseek-v4.1-flash-1017-basic",
    "glm-5.3",
    "glm-5.3-flash",
    "kimi-k3",
    "qwen3.8-max",
)

#: Inferbase's declared fallback — the chat models the free plan's price band
#: covers, cheapest output first, as measured on 2026-11-07. Unlike the other
#: direct sources this one is a *rule* rather than a snapshot: the band is
#: "at or below $2.00 per 1M output tokens", so the live catalog decides
#: membership and this list is what ``/model`` shows before that fetch. The
#: order is the point — the cheapest models have the most headroom under the
#: plan's daily usage allowance, so they lead.
#:
#: ``auto`` is deliberately absent: it is the default the switch lands on, and
#: it is not a catalog entry (the router picks a model per request), so listing
#: it here would put a name in the picker that no catalog row explains.
INFERBASE_MODELS: tuple[str, ...] = (
    "deepseek-ocr-2",
    "mistral-nemo-instruct-2407",
    "meta-llama-3-1-8b-instruct",
    "mistral-small-24b-instruct-2501",
    "google-gemma-3-4b-it",
    "microsoft-phi-4",
    "openai-gpt-oss-20b",
    "google-gemma-3-12b-it",
    "qwen3-5-9b",
    "google-gemma-3-27b-it",
    "openai-gpt-oss-120b",
    "deepseek-v4-flash",
    "deepseek-v4-flash-0731",
    "meta-llama-guard-4-12b",
    "mistral-small-3-2-24b-instruct-2506",
    "nvidia-nemotron-3-nano-30b-a3b",
    "qwen3-14b",
    "qwen3-32b",
    "meta-llama-4-scout-17b-16e-instruct",
    "meta-llama-3-3-70b-instruct",
    "google-gemma-4-26b-a4b-it",
    "google-gemma-4-31b-it",
    "deepseek-v3-2",
    "qwen3-8-flash",
    "bytedance-seed-2-0-mini",
    "nvidia-nemotron-3-super-120b-a12b",
    "qwen2-5-72b-instruct",
    "zai-glm-4-7-flash",
    "nvidia-nemotron-3-5-super-vl-120b-a12b",
    "qwen3-30b-a3b",
    "qwen3-vl-30b-a3b-instruct",
    "meta-llama-4-maverick-17b-128e-instruct",
    "zai-glm-4-5-air",
    "qwen3-vl-235b-a22b-instruct",
    "deepseek-v3",
    "zai-glm-4-6v",
    "deepseek-v3-1",
    "qwen3-6-35b-a3b",
    "qwen3-5-35b-a3b",
    "qwen3-coder-480b-a35b-instruct",
    "minimax-m3",
    "qwen3-next-80b-a3b-instruct",
    "minimax-m2-5",
    "minimax-m2-7",
    "reflection-beam-1-0-501b-a23b",
    "zai-glm-4-7",
    "zai-glm-4-5v",
    "bytedance-seed-1-8",
    "zai-glm-4-6",
)

#: Flex's declared catalog — the twelve ids its playground picker offered on
#: 2026-11-07, each confirmed to answer a chat turn. Unlike every other list in
#: this table this one is **not a fallback**: the playground has no Models route
#: (``/api/playground/models`` answers 404, and ``/v1/models`` is the
#: authenticated API's), so there is nothing to fetch and this is the catalog.
#: ``PaddleOCR-VL`` is listed because the picker lists it; ``flex.Model.chat``
#: filters it out of the picker, since it is an OCR model and not a chat one.
FLEX_MODELS: tuple[str, ...] = (
    "DeepSeek-V4-Flash-0731",
    "gpt-oss-20b",
    "Qwen3.5-9B",
    "GLM-4.5-Air-FP8",
    "Qwen3-Coder-30B-A3B-Instruct-FP8",
    "Qwen3.6-35B-A3B-FP8",
    "Qwen3-30B-A3B-Thinking-2507-FP8",
    "gemma-4-26B-A4B-it",
    "gemma-4-31b-it",
    "Meta-Llama-3.1-8B-Instruct-FP8",
    "Mistral-Nemo-Instruct-2407-FP8",
    "PaddleOCR-VL",
)

#: Zro's declared fallback. Its real catalog is fetched from the public
#: ``/models`` route (see ``moonmath.py``); this is what ``/model`` shows before
#: that fetch. The catalog carried three chat models when this was written, and
#: the docs also list two security variants and an ``auto`` router, so the
#: declared list is the documented surface rather than a snapshot of one
#: fetch — a starting point, not the truth. Anything retired answers 404, and a
#: switch re-reads the live catalog before it lands.
MOONMATH_MODELS: tuple[str, ...] = (
    "deepseek-v4.1-flash",
    "glm-5.3",
    "glm-5.3-flash",
    "dolly1-security",
    "dolly2-security",
    "auto",
)

MODELS_BY_KEY: dict[str, tuple[str, ...]] = {
    "logfare": LOGFARE_MODELS,
    "tokenjuice": TOKENJUICE_MODELS,
    "iteracompute": ITERACOMPUTE_MODELS,
    "infronai": INFRONAI_MODELS,
    "nvidia": NVIDIA_MODELS,
    "moonmath": MOONMATH_MODELS,
    "wandb": WANDB_MODELS,
    "inferx": INFERX_MODELS,
    "inferbase": INFERBASE_MODELS,
    "flex": FLEX_MODELS,
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
    the per-source *-models commands exist to warm it. Zro is the exception: its
    declared list is a documented fallback, so a cold cache answers that instead
    of None and ``/model`` offers this provider's surface rather than whatever
    Logfare happens to serve.
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
    if key == "logfare":
        # Logfare has its own catalog module, and that is the list /model must
        # offer: it is filtered to chat-capable models and carries health. The
        # generic direct-only branch below would read the Models route cache
        # instead, which is cold until a switch probes it.
        from . import logfare as lf_mod
        cached = lf_mod.cached_ids()
        return tuple(cached) if cached else None
    if key == "moonmath":
        # Fetched from the gateway's public catalog. The declared fallback is
        # better than the default registry's list here — it is the documented
        # surface of *this* provider — so it answers a cold cache.
        from . import moonmath as mm_mod
        cached = mm_mod.cached_ids()
        return tuple(cached) if cached else (MODELS_BY_KEY.get(key) or None)
    if key == "inferx":
        # Checked before the generic DIRECT_ONLY branch, like NVIDIA's: this
        # source has its own module cache, and that is the list `/model` should
        # offer — it is what the warm-up and `/inferx-models` fill, both of
        # which read the catalog *with* the key. The generic branch below reads
        # the probe cache instead, which is empty until a switch happens, and
        # the catalog is key-gated so an empty cache is the normal state on a
        # fresh launch.
        from . import inferx as ix_mod
        cached = ix_mod.cached_ids()
        return tuple(cached) if cached else (MODELS_BY_KEY.get(key) or None)
    if key == "wandb":
        # Same reasoning as InferX one branch up, and the same placement fix:
        # wandb has its own module cache too, and this branch sat *below* the
        # generic DIRECT_ONLY branch, which returns before reaching it — so
        # `/wandb-models` warmed a cache `/model` never read.
        from . import wandb as wb_mod
        cached = wb_mod.cached_ids()
        return tuple(cached) if cached else (MODELS_BY_KEY.get(key) or None)
    if key == "inferbase":
        # Same placement rule again, and it matters more here than anywhere
        # else: this source's catalog is the only one that carries a window and
        # a price per model, and the generic DIRECT_ONLY branch below would read
        # the *public* Models route's id list instead — which has neither, and
        # which is not what `/inferbase-models` warms.
        from . import inferbase as ib_mod
        cached = ib_mod.cached_ids()
        return tuple(cached) if cached else (MODELS_BY_KEY.get(key) or None)
    if key == "gemini":
        # Checked before the generic DIRECT_ONLY branch for the same reason as
        # InferX: the catalog is key-gated, so the generic branch would read a
        # probe cache that is cold until a switch fills it. Reading the module
        # directly means `/model` offers the chat-capable ids the switch just
        # fetched, not the raw 61 the Models route lists.
        from . import gemini as gm_mod
        cached = gm_mod.cached_ids()
        return tuple(cached) if cached else (MODELS_BY_KEY.get(key) or None)
    if key == "flex":
        # Checked before the generic DIRECT_ONLY branch, and for a reason that
        # is the opposite of Inferbase's: this source has no Models route to
        # probe at all, so the generic branch would read a probe cache that can
        # never be filled and fall back to the declared list anyway. Reading the
        # module directly says where the list comes from — a constant, not a
        # fetch — and keeps `PaddleOCR-VL` out of the picker, which the raw
        # declared tuple would put there.
        from . import flex as flex_mod
        cached = flex_mod.cached_ids()
        return tuple(cached) if cached else (MODELS_BY_KEY.get(key) or None)
    if key in DIRECT_ONLY:
        # Declared, not fetched — but a switch probes the Models route, and what
        # the upstream actually answered is better than the declared fallback.
        from . import openai_compat as oa_mod
        probed = oa_mod.cached_ids(DECLARED_ROUTES[key].base_url)
        return tuple(probed) if probed else (MODELS_BY_KEY.get(key) or None)
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
    if key == "wandb":
        from . import wandb as wb_mod
        return wb_mod.cached_models()
    if key == "inferx":
        from . import inferx as ix_mod
        return ix_mod.cached_models()
    if key == "inferbase":
        from . import inferbase as ib_mod
        return ib_mod.cached_models()
    if key == "flex":
        from . import flex as flex_mod
        return flex_mod.cached_models()
    if key == "gemini":
        from . import gemini as gm_mod
        return gm_mod.cached_models()
    if key == "moonmath":
        from . import moonmath as mm_mod
        return mm_mod.cached_models()
    return None


def _matches(entry, model: str) -> bool:
    """Whether a catalog *entry* is the model *model* names.

    Exact id first, then any alias the source's own module exposes. Zro
    publishes bare ids (``glm-5.3``) but accepts a ``zro/`` prefix on the wire,
    and a turn sent as ``zro/glm-5.3`` has to price and size exactly like the
    bare id — otherwise the alias would silently fall back to this harness's
    estimate table and a free model would be billed.
    """
    if getattr(entry, "id", None) == model:
        return True
    return model in (getattr(entry, "aliases", ()) or ())


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
        if not _matches(entry, model):
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
    if key == "inferx" and not catalog_models(key):
        # Same reasoning as NVIDIA, one route over: InferX bills the key's
        # tenant allowance rather than the model, and its catalog publishes no
        # rate at all (the public page's price columns are empty). A cold cache
        # answering None would print a dollar figure for a model nothing here
        # can price.
        return (0.0, 0.0)
    if key == "inferbase" and not catalog_models(key):
        # Inferbase is the one direct source that DOES publish a rate, so this
        # is not the NVIDIA case — but a cold cache still must not fall through
        # to the estimate table, because the default model is ``auto`` and the
        # estimate table has never heard of it. ``auto`` routes within the free
        # plan's price band by construction, so (0, 0) is the true answer for
        # it warm or cold; a pinned model gets its published rate as soon as
        # the catalog is read.
        return (0.0, 0.0)
    if key == "flex":
        # The playground is unauthenticated, so there is no account to bill and
        # no key to meter. (0, 0) is the true answer warm or cold — and unlike
        # the three branches above, this one is not about a cold cache: the
        # catalog is a constant, so it is never cold.
        return (0.0, 0.0)
    if key == "gemini":
        # NVIDIA's case exactly: the catalog publishes no per-model price, and
        # the free tier is a quota on the *account* rather than a rate on the
        # model. A cold cache answering None would fall through to the estimate
        # table and bill a free model at DEFAULT_PRICE.
        return (0.0, 0.0)
    cached = catalog_models(key)
    if not cached:
        return None
    for entry in cached:
        if not _matches(entry, model):
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


def message_cap_for(base_url: str) -> int | None:
    """The most messages the active route accepts, or ``None`` when uncapped.

    Only a playground caps this. Flex answers ``400 Maximum 10 messages
    allowed`` on the eleventh, system included, and the transport trims the
    history to fit — which means the oldest turns are dropped at the wire with
    no compaction event and no transcript line saying so. The agent loop reads
    this to compact *before* that happens, so the loss is a summarised history
    rather than a silent truncation.

    ``None`` is the honest answer for every other route: an API does not cap
    message count, and a caller that treated "no answer" as a number would
    compact a healthy session for no reason.
    """
    key = active_key(base_url)
    if key == "flex":
        from . import flex as flex_mod
        return flex_mod.MAX_MESSAGES
    return None


def message_chars_for(base_url: str) -> int | None:
    """The most characters the active route accepts in one message, or ``None``.

    The companion to :func:`message_cap_for`, and it exists for the same
    failure: Flex caps the *last* message at 2000 characters and the transport
    trims it, keeping the head. That is right for a tool result and wrong for a
    summariser prompt, whose tail is the recent turns — so ``compact()`` reads
    this to build a prompt that already fits instead of letting the transport
    cut the part worth summarising off the end.

    ``None`` for every other route: an API does not cap message length, and a
    caller that treated "no answer" as a number would truncate a healthy
    prompt.
    """
    key = active_key(base_url)
    if key == "flex":
        from . import flex as flex_mod
        return flex_mod.MAX_MESSAGE_CHARS
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
        if not _matches(entry, model):
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
        # Retired: Logfare is direct-only now (see DECLARED_ROUTES). The paths
        # are still listed so an older install's files can be found and ignored
        # deliberately, rather than being mistaken for a route that still works.
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
        # Same as _service_candidates: kept for recognition of an older
        # install, ignored by _build because Logfare is direct-only.
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
    if key == "wandb":
        # No credential ships either, and none could: a wandb key is the
        # operator's own inference key. Until /wandb-api-setup writes this
        # file the source is not discovered, which is what makes /zim-wandb
        # name the setup command instead of failing at request time.
        return [
            ZIMZILLA_HOME / "wandb" / "source",
        ]
    if key == "inferx":
        # Same shape as NVIDIA and wandb: the key is the operator's own, issued
        # against their tenant endpoint. Until /inferx-api-setup writes this
        # file the source is not discovered, which is what makes /zim-inferx
        # name the setup command instead of failing at request time.
        return [
            ZIMZILLA_HOME / "inferx" / "source",
        ]
    if key == "inferbase":
        # And again: the key is the operator's own, and nothing ships. Until
        # /inferbase-api-setup writes this file the source is not discovered,
        # which is what makes /zim-inferbase name the setup command instead of
        # failing at request time.
        return [
            ZIMZILLA_HOME / "inferbase" / "source",
        ]
    if key == "gemini":
        # No credential ships, and none could: an AI Studio key is the
        # operator's own. Until /gemini-api-setup writes this file the source is
        # not discovered, which is what makes /zim-gemini name the setup command
        # instead of failing at request time.
        return [
            ZIMZILLA_HOME / "gemini" / "source",
        ]
    if key == "flex":
        # The one source whose profile is NOT a credential: the playground
        # authenticates nothing, so this file records the route and the model.
        # It ships in the tree, which is what makes /zim-flex work on a fresh
        # clone with no setup step — and the operator's own copy wins, so a
        # model pinned with /model survives a reinstall.
        return [
            ZIMZILLA_HOME / "flex" / "source",
            DEFAULT_ROOT / "packaging" / "flex" / "source",
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
    # A direct-only source needs no proxy files, and a proxy left behind by an
    # older install is deliberately ignored: the route declared in
    # DECLARED_ROUTES is the only one that exists now. Keeping the old files
    # would make ``is_direct_only`` false and send the session back through a
    # hop that is no longer installed.
    if key in DIRECT_ONLY:
        service = None
        config = None
    elif service is None or config is None:
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

    A direct-only source (Logfare, Token Juice, NVIDIA) is present when its
    profile is. A proxy route is present only when its profile, service and
    config are all there — none of the bundled proxies is the default any more,
    so this is the bring-your-own-proxy case.
    """
    found: dict[str, Source] = {}
    # Direct now: the upstream serves Chat Completions itself, so the profile is
    # the whole install and the retired :4001 proxy files are ignored.
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
    # Direct-only, like Token Juice and NVIDIA: no port and no proxy files, so
    # the profile is the whole install. Absent until /wandb-api-setup has run.
    wandb = _build("wandb", "wandb.ai inference", 0,
                   "deepseek-ai/DeepSeek-V4.1-Flash")
    if wandb:
        found["wandb"] = wandb
    # And again for InferX: direct-only, and — like wandb and unlike NVIDIA —
    # its catalog needs the key, so without a profile there is nothing to list
    # and the route is absent until /inferx-api-setup has run.
    inferx = _build("inferx", "InferX", 0,
                    "deepseek-v4.1-flash-1017")
    if inferx:
        found["inferx"] = inferx
    # And Inferbase: direct-only, and its catalog is the priced one, so a cold
    # cache offers the declared free-band list until the warm-up fills it.
    inferbase = _build("inferbase", "Inferbase", 0, "auto")
    if inferbase:
        found["inferbase"] = inferbase
    # And Flex, which is the one source discovered from a profile that carries
    # no credential. Its profile ships in the tree, so this is present on every
    # install — which is the point: the route needs no key, so there is nothing
    # for the operator to set up before it works.
    flex = _build("flex", "Flex playground", 0, "DeepSeek-V4-Flash-0731")
    if flex:
        found["flex"] = flex
    # And Gemini: direct-only, and — like InferX and unlike NVIDIA — its catalog
    # is key-gated, so without a profile there is nothing to list and the route
    # is absent until /gemini-api-setup has run.
    gemini = _build("gemini", "Google Gemini", 0, "gemini-flash-lite-latest")
    if gemini:
        found["gemini"] = gemini
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
                "tokenharbour", "moonmath", "infronai", "iteracompute",
                "antseed", "nvidia", "tokenjuice", "logfare", "wandb",
                "inferx", "inferbase", "flex"):
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
    from . import flex as flex_mod
    from . import gemini as gm_mod
    from . import inferbase as ib_mod
    from . import inferx as ix_mod
    from . import moonmath as mm_mod
    from . import openrouter as or_mod
    from . import tokenharbour as th_mod
    from . import unorouter as uno_mod
    from . import wandb as wb_mod
    return {
        "openrouter": or_mod, "unorouter": uno_mod, "aihubmix": ah_mod,
        "beatapi": bp_mod, "tokenharbour": th_mod, "moonmath": mm_mod,
        "wandb": wb_mod, "inferx": ix_mod, "inferbase": ib_mod,
        "flex": flex_mod, "gemini": gm_mod,
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
#: `discover()`, which only knows the sources with profiles in this tree — its
#: buyer is started and stopped by the operator, so requiring it to be up at
#: selection time would lose the choice every time the proxy was down.
BUILTIN_KEYS = frozenset({"tokenharbour", "openrouter", "unorouter", "antseed",
                          "aihubmix", "beatapi", "moonmath", "wandb", "inferx",
                          "inferbase", "flex"})


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
    from . import flex as flex_mod
    from . import gemini as gm_mod
    from . import inferbase as ib_mod
    from . import inferx as ix_mod
    from . import infronai as inf_mod
    from . import iteracompute as ic_mod
    from . import moonmath as mm_mod
    from . import nvidia as nv_mod
    from . import openrouter as or_mod
    from . import tokenharbour as th_mod
    from . import unorouter as uno_mod
    from . import wandb as wb_mod
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
    # Zro is hosted and Anthropic-native, identified by host like the other
    # gateways — the Messages base URL is the bare host, so there is no port or
    # path to match on.
    if mm_mod.is_moonmath(base_url):
        return "moonmath"
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
    if wb_mod.is_wandb(base_url):
        return "wandb"
    # Same reasoning as NVIDIA and wandb: reached directly, identified by host
    # rather than by port, and checked before the generic host loop below so a
    # session launched from an environment profile is still recognised.
    if ix_mod.is_inferx(base_url):
        return "inferx"
    # Same reasoning once more: reached directly, identified by host rather than
    # by port, and checked before the generic host loop below so a session
    # launched from an environment profile is still recognised.
    if ib_mod.is_inferbase(base_url):
        return "inferbase"
    # And Flex, which is the one source here with no credential to identify it
    # by: the playground authenticates nothing, so the host is the whole test.
    if flex_mod.is_flex(base_url):
        return "flex"
    # And Gemini, reached directly and identified by host for the same reason:
    # there is no port to match on, and a session launched from an environment
    # profile has no file for discover() to find.
    if gm_mod.is_gemini(base_url):
        return "gemini"
    # Checked before the port match, and by port rather than by name: the buyer
    # hands out a plain http://localhost:8377 with no "antseed" in it, and the
    # regex below would otherwise find port 8377, miss it in source_for_port()
    # (which only knows the ports declared by discover()) and return None.
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

def _parse_exports(text: str) -> dict[str, str]:
    """Parse ``export VAR="value"`` lines into a dict.

    The single reader for every profile in the project: the in-TUI switch
    (``load_profile``), and the Token Juice key report that has to say which
    file is in force. Extracted rather than copied so the two can never
    disagree about what a profile says.
    """
    out: dict[str, str] = {}
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
    return out


def load_profile(src: Source) -> dict[str, str]:
    """Read the profile's exports without a shell, so nothing is echoed.

    Parses the ``export VAR="value"`` lines the profiles are written in. A
    profile that uses anything more exotic than that is read by the launcher
    anyway; this is only for the in-TUI switch.
    """
    try:
        text = src.profile.read_text(encoding="utf-8")
    except OSError:
        return {}
    out = _parse_exports(text)
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


@dataclass(frozen=True)
class TokenJuiceKey:
    """Which Token Juice credential is in force, and where it came from.

    Three origins, because the profile chain has three rungs and "a key is
    set" is not the same answer as "you are spending the shared one":

    * ``personal`` — ``$ZIMZILLA_HOME/tokenjuice/source``, written by
      ``/tokenjuice-api-setup`` or ``/zim-tokenjuice <key>``.
    * ``legacy``   — ``$HOME/claude-source/deepseek-claude``, the older layout,
      still read so an existing install keeps working.
    * ``shipped``  — ``packaging/tokenjuice/source`` in the tree, the shared key
      every fresh clone works on.

    ``tail`` is the last four characters of the token in force, which is enough
    to tell two keys apart and not enough to be one.
    """

    origin: str
    path: Path | None
    tail: str
    base_url: str
    model: str

    @property
    def is_personal(self) -> bool:
        return self.origin in ("personal", "legacy")


def tokenjuice_status() -> TokenJuiceKey:
    """Report the Token Juice credential in force, in profile order.

    Read-only: it never writes, and it never reports the key itself — only the
    last four characters. An install with nothing set at all comes back with
    ``origin == ""``, which is what lets the UI say "no key" instead of showing
    an empty panel.
    """
    for path in _profile_candidates("tokenjuice"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        env = _parse_exports(text)
        token = env.get("ANTHROPIC_AUTH_TOKEN", "")
        if path == tokenjuice_profile():
            origin = "personal"
        elif str(path).startswith(str(DEFAULT_ROOT)):
            origin = "shipped"
        else:
            origin = "legacy"
        return TokenJuiceKey(
            origin=origin,
            path=path,
            tail=token[-4:] if len(token) >= 4 else "",
            base_url=env.get("ANTHROPIC_BASE_URL", ""),
            model=env.get("ANTHROPIC_MODEL", ""),
        )
    return TokenJuiceKey(origin="", path=None, tail="", base_url="", model="")


def clear_token() -> Path | None:
    """Remove the operator's own Token Juice profile, falling back to shared.

    Only the personal file is ever unlinked. The shipped key in the tree is
    what makes a fresh clone work without a signup, so deleting it would be the
    one change this command must never make — a test pins that.
    """
    profile = tokenjuice_profile()
    try:
        profile.unlink()
    except FileNotFoundError:
        return None
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
