"""Inferbase — a routing gateway with a priced catalog and a free price band.

Inferbase (``api.inferbase.ai``) is a control plane in front of a pool of
models: one OpenAI-compatible endpoint, a routing engine that picks a model per
request, and a catalog that publishes what each model costs. It is a *direct*
source in the same sense Token Juice, NVIDIA, wandb and InferX are — the
upstream speaks Chat Completions itself, so there is no LiteLLM proxy, no port
to start, no health check. The route is declared in :mod:`zimzilla.sources`
(:data:`~zimzilla.sources.INFERBASE_DIRECT`) and the profile below is the whole
install.

Measured on this box, 2026-11-07, against the live endpoint:

* ``POST /v1/chat/completions`` answers a plain completion, streams standard
  SSE, and honours ``stream_options: {"include_usage": true}``. Tool calls
  arrive as ``delta.tool_calls[]`` with a ``function.arguments`` string, and a
  pinned model and ``auto`` both returned valid tool-call JSON.
* The reply carries ``reasoning_content`` — the spelling
  ``openai_compat._delta_reasoning`` already reads — so ``/think`` works here
  with no transport change.
* ``GET /v1/models`` is **public**: it answers 200 with no credential and with
  a wrong one. That is load-bearing for the switch, and it is the opposite of
  InferX's key-gated route: a probe against it would call a rejected key
  healthy, so :func:`verify_key` makes one tiny real chat call instead.
* ``GET /api/v1/inference/catalog`` is **key-gated** (401 without a key) and is
  the catalog worth reading: 96 entries carrying ``model_type``,
  ``context_window``, ``max_output_tokens``, ``input_cost_per_1m`` and
  ``output_cost_per_1m``. ``/v1/models`` lists 134 ids and carries none of
  that, so the two are read together — the catalog for the facts, the Models
  route for the ids it does not cover (37 ``:fp8``/``:turbo``/``:fp4``
  variants, and ``auto``).
* **The free plan is a price band, not a per-model flag.** Models whose output
  price is at or below **$2.00 per 1M tokens** are included; anything above
  answers ``402`` with ``"This model is outside the free plan's price band
  ($2.00 per 1M output tokens at the provider)"``. Measured: ``qwen3-8-max``
  and ``deepseek-v4-pro`` answered 402 while ``zai-glm-4-7-flash`` and
  ``deepseek-v4-flash`` answered 200 on the same key in the same minute.
* The free plan is also **rate limited to 10 requests per minute** on
  Inferbase's own models, answered as ``429`` with a ``retry-after`` and the
  sentence ``"Free plan limit: 10 requests per minute …"``. That is a window,
  not a burst, and ``agent._rate_limit_is_a_window`` already recognises the
  wording — so the harness reports it instead of retrying into it.
* Unknown request fields are **rejected with 422**, never ignored
  (``"bogus_field: Extra inputs are not permitted"``). ``reasoning_effort`` is
  accepted; an Anthropic-style ``thinking`` block is not. So ``/effort`` works
  here and the drop-and-retry path is what covers a model that refuses the
  field.
* Non-chat entries answer ``404`` with a sentence naming what they are
  (``"'qwen3-embedding-8b' is an embedding model; it cannot answer chat
  prompts"``), which is why the picker filters on the catalog's ``model_type``
  rather than on a name list.

**The catalog is priced, and that shapes this module.** Unlike NVIDIA's and
InferX's, this one publishes a real rate per model, so :func:`price_for`
returns it — but a model inside the free band is covered by the plan, so it
answers ``(0.0, 0.0)`` and ``/cost`` reads ``free``. A model outside the band
is priced at its published rate, which is the honest figure for what it would
cost with credit on the account. :attr:`Model.free` is the band test, and it is
the same test the gateway applies.

Nothing here echoes a key. The profile is written mode 600 and read back the
same way every other source's is.
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

#: The API root the session is pointed at. No proxy sits in front of it: this is
#: the address the operator's own curl example uses, and the one the OpenAI SDK
#: is pointed at in Inferbase's docs.
API_BASE_URL = "https://api.inferbase.ai/v1"

#: The model list. **Public** — it answers 200 with no credential and with a
#: wrong one — so it is a catalog read and never a key check.
MODELS_URL = f"{API_BASE_URL}/models"

#: The rich catalog: ``model_type``, ``context_window``, ``max_output_tokens``
#: and both prices per entry. Key-gated, and served from the ``/api/v1`` root
#: rather than ``/v1`` — the docs are explicit that keys, credits, usage and
#: model health live only under ``/api/v1/inference``.
CATALOG_URL = "https://api.inferbase.ai/api/v1/inference/catalog"

#: The profile ``/inferbase-api-setup`` writes and every lookup reads.
PROFILE = ZIMZILLA_HOME / "inferbase" / "source"

#: Inferbase keys are issued as ``inf_<long token>``. A hint for the
#: confirmation line, never a gate: the upstream is the authority on its own
#: keys, and refusing a real key of an unexpected shape would be worse than
#: storing it and letting one 401 say so.
KEY_PREFIX = "inf_"

#: The model a switch selects when the profile names none.
#:
#: ``auto`` is Inferbase's own default and the id the operator's curl example
#: used: the router picks a model from the pool the plan allows, and the
#: response names whichever one answered. It is the right default here because
#: the free plan's band is a *price* rule rather than a model list — pinning a
#: name would pin a model the band may not cover next month, while ``auto``
#: routes within it by construction.
DEFAULT_MODEL = "auto"

#: A fetched catalog older than this is refetched rather than trusted. The
#: lineups and the prices behind the free band both move, so a stale list is
#: exactly the wrong list this picker exists to avoid.
CACHE_TTL = 300.0

#: The free plan's price band, in dollars per 1M **output** tokens. Read from
#: the gateway's own 402 sentence and from its pricing page, which state the
#: same figure: "Managed models up to $2.00 per 1M output tokens".
FREE_BAND = 2.00

#: The window to plan against when the catalog does not know the id.
#:
#: 131,072 — the modal ``context_window`` across the 49 in-band chat models
#: (14 of them), not the harness's 1M fallback and not the largest figure in
#: the catalog. The direction of that guess is deliberate, the same as NVIDIA's
#: and InferX's: a window assumed too *small* makes the harness compact early,
#: which is survivable; one assumed too *large* overflows mid-turn, which is
#: not. ``auto`` is the id this answers for most often, and the model it routed
#: to when measured (``minimax-m2-7``) publishes 204,800 — so 131K errs safe.
DEFAULT_CONTEXT = 131_072

#: ``model_type`` values that can answer a chat turn. The catalog's own
#: vocabulary, read from the live payload: ``chat``, ``code`` and ``vision``
#: are the three that take a prompt and return text. Everything else —
#: ``embedding``, ``rerank``, ``image``, ``audio`` — answers 404 on the chat
#: route with a sentence saying so.
CHAT_TYPES: frozenset[str] = frozenset({"chat", "code", "vision"})

#: Families that cannot answer a chat turn, matched as substrings of the
#: lowercased id. Only consulted for an id the catalog does not cover — the 37
#: ``:fp8``/``:turbo``/``:fp4`` variants resolve through their base id, and
#: ``auto`` is chat by definition. Deliberately narrow, and it errs toward
#: **keeping** an unknown id: hiding one the operator could have used is worse
#: than showing one that 404s, because the error is immediate and
#: self-explanatory.
_NON_CHAT_MARKERS: tuple[str, ...] = (
    "embed", "bge", "rerank", "ranking", "reward",
    "clip", "vila", "kosmos", "deplot", "diffusion", "cosmos",
    "guard", "safety", "jailbreak", "detector", "pii",
    "parse", "whisper", "tts", "audio", "image", "flux", "voxtral",
    "parakeet", "asr",
)


@dataclass(frozen=True)
class Model:
    """One entry from Inferbase's catalog.

    The catalog publishes more than any other source here — a type, a window,
    an output ceiling and both prices — so this object carries them rather than
    guessing. ``priced`` records whether a price was actually **read**: a
    missing price must not read as 0/0 and mark every unpriced entry free,
    which is the trap the other gateway modules document.
    """

    id: str
    name: str = ""
    model_type: str = ""
    #: The catalog's window, or ``None`` when it does not cover this id.
    context: int | None = None
    max_output: int | None = None
    price_in: float = 0.0
    price_out: float = 0.0
    priced: bool = False
    #: Whether the catalog advertises tool calling. Read from
    #: ``supported_parameters`` on the Models route, which is the only place
    #: the gateway states it.
    tools: bool = False
    #: The base id for a ``:fp8``/``:turbo``/``:fp4`` variant, else ``""``.
    variant_of: str = ""

    @property
    def free(self) -> bool:
        """Whether the free plan covers this model.

        The plan's rule is a **price band**, not a per-model flag: managed
        models priced at or below :data:`FREE_BAND` per 1M output tokens are
        included, and anything above answers 402. So the test is the published
        rate, and a model with no published rate is not called free — that is
        what ``priced`` is for.

        ``auto`` is the exception, and it is free by construction: the router
        picks from the pool the plan allows, so whatever it lands on is inside
        the band. It has no catalog entry of its own to price.
        """
        if self.id == "auto":
            return True
        return self.priced and self.price_out <= FREE_BAND

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn.

        The catalog's ``model_type`` is authoritative when it is known, because
        it is the field the gateway itself routes on. An id the catalog does
        not cover falls back to the family markers, which is what keeps a
        ``:fp8`` variant of a chat model in the list.
        """
        if self.model_type:
            return self.model_type in CHAT_TYPES
        lowered = self.id.lower()
        return not any(marker in lowered for marker in _NON_CHAT_MARKERS)

    @property
    def context_length(self) -> int:
        """The window to plan against, in tokens.

        The catalog's figure when it has one, else :data:`DEFAULT_CONTEXT`.
        Never ``None``: unlike the catalogs that publish a window, silence here
        is not "unknown, use the harness default" — the harness default is 1M,
        and a 1M assumption against a 131K model is an overflow the operator
        discovers mid-task.
        """
        return self.context or DEFAULT_CONTEXT

    @property
    def context_known(self) -> bool:
        """Whether :attr:`context_length` is the catalog's figure or a guess."""
        return bool(self.context)

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits: list[str] = []
        if self.free:
            bits.append("free plan")
        elif self.priced and (self.price_in or self.price_out):
            bits.append(f"${self.price_in:g}/${self.price_out:g}")
        else:
            bits.append("unpriced")
        bits.append(f"{self.context_length // 1000}k ctx")
        if self.variant_of:
            bits.append(self.id.split(":", 1)[1])
        if not self.tools and self.model_type:
            bits.append("no tools")
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------


def is_inferbase(base_url: str | None) -> bool:
    """Whether *base_url* points at Inferbase.

    Matched on host, because this route has no port to match on. Both the
    ``/v1`` and ``/api/v1/inference`` roots are the same host, so only the host
    is matched.
    """
    return "api.inferbase.ai" in (base_url or "").strip().lower()


#: Generic name for the same test, so code that handles several sources
#: uniformly can call ``mod.active_endpoint(url)``.
active_endpoint = is_inferbase


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


def load_credentials() -> tuple[str, str, str] | None:
    """The Inferbase key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` in the environment,
         when ``ANTHROPIC_BASE_URL`` points at an Inferbase host — i.e. the
         operator sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/inferbase-api-setup``.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged
    or returned to the transcript — only ``origin`` (a path, or "environment").
    """
    if is_inferbase(os.environ.get("ANTHROPIC_BASE_URL")):
        token = (os.environ.get("ANTHROPIC_AUTH_TOKEN")
                 or os.environ.get("ANTHROPIC_API_KEY") or "")
        if token:
            return token, os.environ.get("ANTHROPIC_MODEL", ""), "environment"

    exports = _read_exports(PROFILE)
    token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
    if token:
        return token, exports.get("ANTHROPIC_MODEL", ""), str(PROFILE)

    return None


def save_key(token: str, model: str = "") -> Path:
    """Write the key to ZimZilla's own Inferbase profile, mode 600.

    Same ``source`` format as every other profile, so it can be sourced by a
    shell as well as read by the harness. ``ANTHROPIC_BASE_URL`` names the
    gateway itself — there is no proxy to name — and ``ANTHROPIC_MODEL``
    carries the model the operator was on, so setting a key does not silently
    reset it.
    """
    token = (token or "").strip()
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        "# ZimZilla — Inferbase profile (written by /inferbase-api-setup)\n"
        "# Treat this file as a credential. Never commit it.\n"
        f'export ANTHROPIC_BASE_URL="{API_BASE_URL}"\n'
        f'export ANTHROPIC_AUTH_TOKEN="{token}"\n'
        f'export ANTHROPIC_MODEL="{model or DEFAULT_MODEL}"\n'
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

    Deliberately loose — the upstream decides what is valid, not us — but
    strict enough to catch a pasted shell line or a bare word. The ``inf_``
    prefix is *not* required: a real key of an unexpected shape is worth more
    stored than rejected, and a wrong one only costs a single 401.
    """
    value = (value or "").strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

#: Fetched catalogs, keyed by the token they were fetched with. The value also
#: records whether the *rich* catalog was read, because the public Models route
#: answers with ids only and a caller that needs a window has to know which of
#: the two it is holding.
_CACHE: dict[str, tuple[list[Model], float, bool]] = {}


def _base_id(mid: str) -> str:
    """The catalog id a variant belongs to: ``deepseek-v4-flash:fp8`` → base."""
    return mid.split(":", 1)[0]


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    try:
        got = int(value)
    except (TypeError, ValueError):
        return None
    return got if got > 0 else None


def _parse_catalog(payload: dict) -> list[Model]:
    """Every row of the rich catalog, in catalog order.

    A malformed payload yields an empty catalog rather than an exception: this
    runs inside a TUI command.
    """
    out: list[Model] = []
    items = payload.get("items")
    if not isinstance(items, list):
        return out
    for entry in items:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("inference_model_id") or "").strip()
        if not mid:
            continue
        price_in = _float(entry.get("input_cost_per_1m"))
        price_out = _float(entry.get("output_cost_per_1m"))
        out.append(Model(
            id=mid,
            name=str(entry.get("name") or ""),
            model_type=str(entry.get("model_type") or ""),
            context=_int(entry.get("context_window")),
            max_output=_int(entry.get("max_output_tokens")),
            price_in=price_in or 0.0,
            price_out=price_out or 0.0,
            # Both prices read is what makes the entry priceable. One of them
            # missing is not a zero price, and calling it free would be the
            # exact mistake `priced` exists to prevent.
            priced=price_in is not None and price_out is not None,
        ))
    return out


def _parse_models(payload: dict) -> list[Model]:
    """Every row of the public Models route, in catalog order.

    Carries ids and ``supported_parameters`` only — no window, no price — so
    every entry is unpriced and its window falls back to
    :data:`DEFAULT_CONTEXT`. Used when the key-gated catalog cannot be read.
    """
    out: list[Model] = []
    data = payload.get("data")
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        params = entry.get("supported_parameters")
        tools = isinstance(params, list) and "tools" in params
        out.append(Model(
            id=mid,
            name=str(entry.get("name") or ""),
            tools=tools,
            variant_of=_base_id(mid) if ":" in mid else "",
        ))
    return out


def _merge(rich: list[Model], served: list[Model]) -> list[Model]:
    """The served ids, each carrying the catalog's facts where it has them.

    The two routes answer different questions and neither is complete on its
    own: the catalog knows what a model *is* (type, window, price) but not
    which variants are served, and the Models route knows every id that will
    answer but nothing about it. So the served list decides membership and the
    catalog decides the facts — including for a ``:fp8`` variant, which
    inherits its base id's entry because the gateway serves it as the same
    model at a different precision.

    A served id the catalog does not cover at all keeps its own (empty) facts
    rather than borrowing a neighbour's.
    """
    by_id = {m.id: m for m in rich}
    out: list[Model] = []
    for entry in served:
        facts = by_id.get(entry.id) or by_id.get(_base_id(entry.id))
        if facts is None:
            out.append(entry)
            continue
        out.append(Model(
            id=entry.id,
            name=entry.name or facts.name,
            model_type=facts.model_type,
            context=facts.context,
            max_output=facts.max_output,
            price_in=facts.price_in,
            price_out=facts.price_out,
            priced=facts.priced,
            # The Models route is the authority on tool support: it is the
            # field the gateway publishes per id, and the catalog has none.
            tools=entry.tools,
            variant_of=_base_id(entry.id) if ":" in entry.id else "",
        ))
    return out


def _get(url: str, token: str, timeout: float) -> tuple[dict | None, str]:
    """One JSON GET. Returns ``(payload, error)``; never raises."""
    headers = {"Accept": "application/json", "User-Agent": "zimzilla"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach api.inferbase.ai: {e}"


def _fetch_catalog(token: str, timeout: float) -> tuple[list[Model] | None, str]:
    """The key-gated rich catalog, every page of it.

    Paginated: the live payload answers 50 rows a page with ``total_pages``
    alongside, so a single read would silently truncate the list to half the
    catalog — which is exactly the "not all the models" failure this module
    exists to avoid.
    """
    rows: list[Model] = []
    page = 1
    while page <= 20:                      # a runaway guard, not a real limit
        payload, err = _get(f"{CATALOG_URL}?page={page}&page_size=100",
                            token, timeout)
        if payload is None:
            if page == 1:
                return None, err
            break                          # a later page failing keeps page 1
        rows.extend(_parse_catalog(payload))
        try:
            total = int(payload.get("total_pages") or 1)
        except (TypeError, ValueError):
            total = 1
        if page >= total:
            break
        page += 1
    return rows, ""


def fetch_models(token: str = "", *, timeout: float = 25.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch Inferbase's model catalog. Returns ``(models, error)``.

    Two routes, read together, because neither is complete:

    * ``/api/v1/inference/catalog`` — key-gated, and the only place a window,
      an output ceiling or a price is published.
    * ``/v1/models`` — public, and the only place the served ids are listed,
      including the 37 ``:fp8``/``:turbo``/``:fp4`` variants and ``auto``.

    The rich catalog is tried first when there is a key. Without one — or when
    it refuses — the public list still answers, so ``/inferbase-models`` works
    before a key exists and the picker is never empty. ``models`` is ``None``
    only when both routes fail; ``[]`` means a genuinely empty catalog.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach the endpoint" rather than take the session
    down.
    """
    token = (token or "").strip()
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    rich: list[Model] | None = None
    rich_err = ""
    if token:
        rich, rich_err = _fetch_catalog(token, timeout)

    served_payload, served_err = _get(MODELS_URL, token, timeout)
    served = _parse_models(served_payload) if served_payload is not None else None

    if rich and served:
        models = _merge(rich, served)
    elif rich:
        models = rich
    elif served is not None:
        models = served
    else:
        # Neither route answered. The catalog's error is the more useful one
        # when there was a key, because a 401 there names the credential.
        return None, rich_err or served_err or "no catalog"

    _CACHE[token] = (models, time.monotonic(), bool(rich))
    return models, ""


def catalog_is_rich() -> bool:
    """Whether the last fetch read the priced catalog rather than the id list.

    ``/inferbase-models`` says which of the two it is showing: a list with no
    windows and no prices is still a working list, but it is not the catalog,
    and calling it one would make the missing columns look like a bug.
    """
    newest: tuple[list[Model], float, bool] | None = None
    for entry in _CACHE.values():
        if newest is None or entry[1] > newest[1]:
            newest = entry
    return bool(newest and newest[2])


def chat_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the entries that can answer a chat turn."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.chat], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The chat models the free plan covers, cheapest output first.

    Kept as its own name because every other source's ``/models`` path calls
    it. The order is the point: the band is a price rule, so the cheapest
    models are the ones with the most headroom under the plan's daily usage
    allowance, and they lead.
    """
    models, err = chat_models(token, **kw)
    if models is None:
        return None, err
    free = [m for m in models if m.free]
    free.sort(key=lambda m: (m.price_out, m.price_in, m.id))
    return free, ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every chat model id, catalog order preserved — what ``/model`` offers."""
    models, err = chat_models(token, **kw)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def catalog_age() -> float | None:
    """Seconds since the newest cache entry was filled, or ``None`` if none was."""
    if not _CACHE:
        return None
    newest = max(entry[1] for entry in _CACHE.values())
    return time.monotonic() - newest


def cached_models() -> list[Model] | None:
    """The last-fetched catalog as :class:`Model` objects, or ``None``.

    ``/model`` runs on the UI thread and must not block on a fetch, so it reads
    whatever the last fetch left behind — and an entry past :data:`CACHE_TTL`
    reads as ``None``, which callers already handle as "no catalog", rather
    than a stale list being called live.
    """
    newest: tuple[list[Model], float, bool] | None = None
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
    base = (model or "").strip()
    for m in models:
        if m.id == base:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok for *model*, when the catalog can answer.

    ``(0.0, 0.0)`` for a model inside the free plan's band — the plan covers
    it, which is what makes ``/model`` and ``/cost`` read ``free``. The
    published rate for a model outside the band, which is the honest figure for
    what it would cost with credit on the account. ``None`` for an id the
    catalog does not know, or one it serves without a price, so the caller
    falls back to this harness's labelled estimate: a missing price is not a
    zero price.
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
    """The model to fall back to when the chosen one is gone from the catalog."""
    return DEFAULT_MODEL


# ---------------------------------------------------------------------------
# error shapes
# ---------------------------------------------------------------------------


def explain_error(status: int, body: str) -> str:
    """A sentence for an Inferbase failure, from the shape it came back in.

    Four shapes were measured, and they call for different actions:

    * ``401`` — the key is wrong, missing, or revoked. The fix is
      ``/inferbase-api-setup``, not a retry.
    * ``402`` with the price-band sentence — the key is **fine** and the model
      is outside what the free plan covers. The fix is a different model, and
      ``/inferbase-models`` lists the ones inside the band.
    * ``404`` with "cannot answer chat prompts" — the id is real but is an
      embedding, rerank, image or speech model. The fix is a different model,
      not a different key.
    * ``429`` with the free-plan sentence — a per-minute window on the plan,
      not a burst. Waiting is the only thing that helps, and the harness does
      not retry into it.
    """
    text = (body or "").strip()
    detail = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        raw = payload.get("detail")
        # ``detail`` is a string on most errors and a **list of validation
        # objects** on a 422 (``[{"loc": ["body", "bogus_field"], "msg": …}]``),
        # so it is only used when it is a string. The gateway's own
        # ``error.message`` is the flattened sentence in both cases, which is
        # why it is preferred over a repr of the list.
        if isinstance(raw, str):
            detail = raw
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            detail = str(err["message"])
        elif not detail:
            detail = str(payload.get("message") or "")
    lowered = (detail or text).lower()

    if status in (401, 403):
        return "key rejected — check it with /inferbase-api-setup"
    if status == 402 and "price band" in lowered:
        return (f"this model is outside the free plan's price band "
                f"(${FREE_BAND:.2f} per 1M output tokens) — the key is fine; "
                f"/inferbase-models lists the models the plan covers")
    if status == 402:
        return ("the account has no credit for this model (402) — free-plan "
                "models still work; /inferbase-models lists them")
    if status == 404 and "cannot answer chat" in lowered:
        return ("this id is not a chat model — it is served on a different "
                "endpoint; /inferbase-models lists the chat ones")
    if status == 404:
        return ("this model is not served by Inferbase — pick another with "
                "/inferbase-models")
    if status == 422:
        return (f"the gateway rejected a request field: {detail[:160]}"
                if detail else "the gateway rejected a request field (422)")
    if status == 429 and "per minute" in lowered:
        return ("the free plan's 10-requests-per-minute window is spent — the "
                "key is fine; wait for the reset, or /inferbase-models to see "
                "what else is covered")
    if status == 429:
        return "rate limited (429) — retry shortly"
    if status == 503:
        return "Inferbase is at capacity for this model (503) — retry shortly"
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    if text and not payload:
        return f"HTTP {status}: {text[:200]}"
    return f"HTTP {status}"


def verify_key(token: str, model: str = "", *, timeout: float = 90.0
               ) -> tuple[bool, str]:
    """Prove *token* works by making one real, tiny chat call.

    **The Models route cannot answer this.** ``/v1/models`` is public — it
    answers 200 with no credential and with a wrong one — so a probe against it
    would call a rejected key healthy and let the switch succeed, leaving the
    operator to discover the 401 on the first turn. This sends the smallest
    request that proves authentication, which is the only thing that does.

    ``ok`` is False **only** for a rejected key or an unreachable endpoint. A
    model outside the free band (402), a per-minute window (429) or a model
    that is not a chat model (404) says nothing about the credential, so those
    come back ``ok=True`` with the reason in ``detail`` — failing the switch on
    them would refuse a working key over a model choice.
    """
    token = (token or "").strip()
    if not token:
        return False, "no key"
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 8,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{API_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
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
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"could not reach api.inferbase.ai: {e}"
