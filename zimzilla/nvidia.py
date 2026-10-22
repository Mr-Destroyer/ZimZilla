"""NVIDIA NIM — the hosted developer catalog, reached directly with an ``nvapi-`` key.

NVIDIA's API catalog (``https://integrate.api.nvidia.com``) serves an
**OpenAI-compatible** Chat Completions API. That makes it a *direct* source, the
same shape as Token Juice: no LiteLLM proxy, no port to start, no health check
— the upstream speaks the protocol this harness speaks, so the route is
declared in :mod:`zimzilla.sources` (:data:`~zimzilla.sources.NVIDIA_DIRECT`)
and the profile below is the whole install.

Measured on this box, 2026-10-22, against the live endpoint:

* ``GET /v1/models`` answers **without a credential** — 83 entries. So the
  catalog, and therefore ``/nvidia-models`` and the model picker, work before
  ``/nvidia-api-setup`` has ever run.
* Streaming is standard SSE, ``stream_options: {"include_usage": true}`` is
  honoured (the final frame carries ``usage``), and tool calls arrive as
  ``delta.tool_calls[]`` with a ``function.arguments`` string that parses as
  JSON. Verified on five models; ``finish_reason`` is ``tool_calls``.
* Reasoning is forwarded as ``reasoning_content`` — the spelling
  ``openai_compat._delta_reasoning`` already reads, so ``/think`` works here
  with no transport change. **It is billed against ``max_tokens``**, and the
  default model reasons heavily: ``max_tokens: 120`` produced 104 thinking
  frames and not one token of text, while 1024 produced the answer. The
  harness's configured ceiling (32768) has room, and
  ``Agent``'s ``reasoning_exhausted`` retry doubles it when a reply is spent on
  thinking — so this is a property to know, not a defect to work around.
* ``max_tokens`` ceilings are **per model, and the error shape varies**.
  ``nvidia/nemotron-3.5-lightning-30b-a3b`` accepted 200000;
  ``openai/gpt-oss-20b`` answered ``400 ... max_tokens=200000 cannot be greater
  than max_model_len=max_total_tokens=131072``; a wild value elsewhere answers
  ``400 Validation: Max tokens must not exceed 1048576``. Only the last shape
  matches ``openai_compat._output_limit_from``, so the harness cannot learn the
  first. It does not need to at the configured default: 32768 sits inside every
  ceiling sampled, and ``MAX_OUTPUT_CEILING`` (131072) is exactly gpt-oss's
  limit, so the reasoning retry cannot overshoot it.

**The catalog is thin, and that shapes this module.** Each entry carries only
``id``, ``object``, ``created`` and ``owned_by``. There is no price, no context
window, no capability list, no free flag. Three consequences, each handled
explicitly rather than guessed at:

1. **There is no per-model price.** On the developer tier every call is covered
   by the account's trial allowance (community-reported ~40 RPM and a per-account
   credit pool; NVIDIA does not publish a static table). So "free" here is an
   *account* property, not a per-model one: :attr:`Model.free` is true for every
   entry, and the module says that in as many words rather than inventing a
   price the catalog never sent. :func:`price_for` returns ``(0.0, 0.0)`` so the
   harness's cost line reads ``free`` instead of billing a model at
   ``DEFAULT_PRICE``.

2. **There is no context window.** :data:`CONTEXT_LENGTHS` carries the figure
   from NVIDIA's own model page for the ids where it was read, and
   :data:`DEFAULT_CONTEXT` (128K) answers for the rest. The direction of that
   guess is deliberate: a window assumed too *small* makes the harness compact
   early, which is survivable; one assumed too *large* overflows mid-turn,
   which is not. 128K is the modal value across the hosted chat models, and the
   two ids this harness ships as defaults both have a real number here.

3. **There is no capability list.** :attr:`Model.chat` therefore filters on the
   model *family* — embeddings, rerankers, guards, parsers, detectors and image
   generators cannot answer a chat turn. The marker list is deliberately narrow
   and errs toward **keeping** an unknown model: hiding one the operator could
   have used is worse than showing one that 400s, because the error is
   immediate and self-explanatory.

**The catalog also lists models this account cannot call.** Measured
2026-10-22, seven of eighteen sampled ids answered
``404 {"detail": "Function '<uuid>': Not found for account '<id>'"}` — granite,
phi-3.5-moe, mistral-large-2, kimi-k2.6, nemotron-ultra-253b,
nemotron-3-super-120b and nemotron-nano-3-30b among them. A retired model
answers ``410 Gone`` with an end-of-life date (``deepseek-v4-pro``,
``llama-3.1-8b-instruct``), and a saturated one answers
``503 ResourceExhausted``. None of that is visible in ``/v1/models``, so
:func:`explain_error` turns each shape into the sentence that says what
actually happened — the difference between "this model is not on your account"
and "this model no longer exists" is the difference between picking another and
giving up.

**Some models are very slow.** ``z-ai/glm-5.3-flash`` took 150s for a
three-token reply while ``openai/gpt-oss-20b`` took 3s, and ``glm-5.3`` did not
answer inside 90s at all. That is why the declared fallback is the set that was
measured fast *and* tool-capable, and why the module never claims a model is
broken on a timeout alone.

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
#: Where the operator keeps their per-provider shell profiles — the same
#: ``~/claude-source/<name>`` files a plain ``source`` loads for Claude Code.
CLAUDE_SOURCE = Path(os.environ.get("ZIMZILLA_CLAUDE_SOURCE")
                     or (Path.home() / "claude-source"))

#: The upstream API root. No proxy sits in front of it: this is the address the
#: session itself is pointed at, which is why it is a ``DirectRoute`` base URL
#: and not a ``localhost`` port.
API_BASE_URL = "https://integrate.api.nvidia.com/v1"

#: The public model catalog. No key required — that is what lets
#: ``/nvidia-models`` answer before ``/nvidia-api-setup`` has run.
MODELS_URL = f"{API_BASE_URL}/models"

#: The profile ``/nvidia-api-setup`` writes and every lookup reads.
PROFILE = ZIMZILLA_HOME / "nvidia" / "source"

#: NVIDIA developer keys are issued as ``nvapi-<long token>``. A hint for the
#: confirmation line, never a gate: the upstream is the authority on its own
#: keys, and refusing a real key of an unexpected shape would be worse than
#: storing it and letting one 401 say so.
KEY_PREFIX = "nvapi-"

#: The model a switch selects when the profile names none.
#:
#: Chosen on measurement, not on rank: it answered a tool-calling turn in 2.2s
#: — the fastest of the five ids that returned valid JSON arguments — and its
#: model page puts the context at 1M tokens, which is the figure
#: ``Config.context_window`` already assumes. A fast default matters here
#: because the free tier queues.
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"

#: A fetched catalog older than this is refetched rather than trusted. The
#: catalog turns over — two ids sampled on 2026-10-22 were already retired — so
#: a stale list is exactly the wrong list this picker exists to avoid.
CACHE_TTL = 300.0

#: The window to plan against when neither the catalog nor the table knows.
#:
#: 128K, not the harness's 1M fallback: the catalog publishes no window at all,
#: and guessing high is the unrecoverable direction. See the module docstring.
DEFAULT_CONTEXT = 131_072

#: Real windows, read from NVIDIA's own model page for the id. Only ids that
#: were actually looked up belong here — a number invented for this table would
#: be indistinguishable from a measured one at the point it matters.
CONTEXT_LENGTHS: dict[str, int] = {
    # "Context Length | Up to 1M tokens" (docs.api.nvidia.com model page)
    "nvidia/nemotron-3.5-lightning-30b-a3b": 1_048_576,
    # "Input Context Length (ISL): 128000"
    "openai/gpt-oss-20b": 131_072,
}

#: Families in this catalog that cannot answer a chat turn, matched as
#: substrings of the lowercased id.
#:
#: Embedding and reranking models return vectors, guards return a safety label,
#: parsers return structured document data, detectors and image generators
#: return something that is not text — a chat request against any of them is a
#: 400 at best. The list is matched narrowly and never used to *validate* a
#: name the operator typed: it only shapes the picker.
_NON_CHAT_MARKERS: tuple[str, ...] = (
    "embed", "bge", "rerank", "ranking", "reward",
    "clip", "neva", "vila", "kosmos", "deplot", "diffusion", "cosmos",
    "guard", "safety", "jailbreak", "detector", "gliner", "pii",
    "parse", "nemoretriever", "calibration", "safety-guard",
)


@dataclass(frozen=True)
class Model:
    """One entry from NVIDIA's public model catalog.

    Only the four fields the catalog actually sends are stored. Everything
    else — price, window, capability — is derived here or is deliberately
    absent, because the catalog sends none of it and a default in this object
    would be indistinguishable from a fact at the point of use.
    """

    id: str
    owned_by: str = ""
    created: int | None = None

    @property
    def free(self) -> bool:
        """Whether this model costs money to call.

        Always false-cost on the developer tier: NVIDIA's hosted catalog is
        billed against the account's trial allowance, not per token, and the
        catalog publishes no per-model price to contradict that. This is what
        makes ``/model`` and ``/cost`` read ``free`` instead of billing a
        model at ``DEFAULT_PRICE`` — the failure the other gateway modules
        document, arriving here from the opposite direction.
        """
        return True

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn, by family."""
        lowered = self.id.lower()
        return not any(marker in lowered for marker in _NON_CHAT_MARKERS)

    @property
    def context_length(self) -> int:
        """The window to plan against, in tokens.

        The table's figure when NVIDIA's model page gave one, else
        :data:`DEFAULT_CONTEXT`. Never ``None``: unlike the catalogs that
        publish a window, silence here is not "unknown, use the harness
        default" — the harness default is 1M, and a 1M assumption against a
        128K model is an overflow the operator discovers mid-task.
        """
        return CONTEXT_LENGTHS.get(self.id, DEFAULT_CONTEXT)

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits = ["free (dev tier)"]
        window = self.context_length
        bits.append(f"{window // 1000}k ctx")
        if self.owned_by:
            bits.append(self.owned_by)
        return " · ".join(bits)


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------

def is_nvidia(base_url: str | None) -> bool:
    """Whether *base_url* points at NVIDIA's hosted catalog.

    Matched on host, because this route has no port to match on. Both the
    documented host and the bare ``api.nvidia.com`` are accepted: the latter is
    the form an operator is most likely to paste, and it is the same service.
    """
    url = (base_url or "").strip().lower()
    return "integrate.api.nvidia.com" in url or "api.nvidia.com" in url


#: Generic name for the same test, so code that handles several sources
#: uniformly can call ``mod.active_endpoint(url)``.
active_endpoint = is_nvidia


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
    """An NVIDIA key already sitting in ``~/claude-source/<name>``.

    A profile there is the ``source ~/claude-source/<name>`` idiom. Only one
    aimed at this host counts — a profile pointing somewhere else is a
    different provider's credential and adopting it would send the wrong key
    upstream.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_nvidia(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The NVIDIA key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at NVIDIA — i.e. the operator
         sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/nvidia-api-setup``.
      3. Any profile under ``~/claude-source`` aimed at this host.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged
    or returned to the transcript — only ``origin`` (a path, or "environment").
    """
    if is_nvidia(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Write the key to ZimZilla's own NVIDIA profile, mode 600.

    Same ``source`` format as every other profile, so it can be sourced by a
    shell as well as read by the harness. ``ANTHROPIC_BASE_URL`` names the
    upstream itself — there is no proxy to name — and ``ANTHROPIC_MODEL``
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
        "# ZimZilla — NVIDIA NIM profile (written by /nvidia-api-setup)\n"
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
    strict enough to catch a pasted shell line or a bare word. The ``nvapi-``
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

#: Fetched catalogs, keyed by the token they were fetched with. The catalog is
#: public and identical for every caller, so the keyless fetch is cached under
#: "" and a keyed one under its token.
_CACHE: dict[str, tuple[list[Model], float]] = {}


def _entries(payload: dict) -> list[dict]:
    """The catalog rows from ``{"object": "list", "data": [...]}``.

    A malformed payload yields an empty catalog rather than an exception: this
    runs inside a TUI command.
    """
    data = payload.get("data")
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    return []


def _parse_models(payload: dict) -> list[Model]:
    """Every entry in the catalog payload, in catalog order."""
    out: list[Model] = []
    for entry in _entries(payload):
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        created = entry.get("created")
        out.append(Model(
            id=mid,
            owned_by=str(entry.get("owned_by") or ""),
            created=created if isinstance(created, int) else None,
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch NVIDIA's model catalog. Returns ``(models, error)``.

    **No key required** — ``/v1/models`` answers without one — so ``token``
    defaults to empty and the list is available before ``/nvidia-api-setup``
    has run. A key is sent when one is supplied, so the listing is made with
    the same credential the turns will use.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach NVIDIA" rather than take the session down.
    ``models`` is ``None`` on failure, ``[]`` for a genuinely empty catalog —
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
    req = urllib.request.Request(MODELS_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, "key rejected (401/403) — check it with /nvidia-api-setup"
        return None, f"HTTP {e.code} from integrate.api.nvidia.com"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach integrate.api.nvidia.com: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    return models, ""


def chat_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Only the entries that can answer a chat turn."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.chat], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The models this harness can drive, which are also the free ones.

    Kept as its own name because every other source's ``/models`` path calls
    it, and the two filters coincide here by construction: a chat-capable entry
    is callable on the developer tier, and the tier charges the account's trial
    allowance rather than the model. See :attr:`Model.free`.
    """
    return chat_models(token, **kw)


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every chat model id, catalog order preserved — what ``/model`` offers.

    Chat entries only. An operator who names a non-chat id deliberately still
    gets it accepted by ``/model`` (the harness only refuses names absent from
    the list); this list is the picker, not a gate.
    """
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
    base = (model or "").strip()
    for m in models:
        if m.id == base:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok for *model*, when the catalog knows it.

    ``(0.0, 0.0)`` for any catalog entry, which is what makes ``/model`` and
    ``/cost`` read ``free``. ``None`` for an id the catalog does not know, so
    the caller falls back to this harness's labelled estimate — the honest
    answer for a name nothing here can price.
    """
    found = model_for(model, models)
    if found is None:
        return None
    return (0.0, 0.0)


def default_free_model() -> str:
    """The model to fall back to when the chosen one is gone from the catalog."""
    return DEFAULT_MODEL


# ---------------------------------------------------------------------------
# error shapes
# ---------------------------------------------------------------------------

def explain_error(status: int, body: str) -> str:
    """A sentence for an NVIDIA failure, from the shape it came back in.

    The three shapes this endpoint actually returns are not interchangeable,
    and the catalog cannot predict any of them:

    * ``404`` with ``"Function '<uuid>': Not found for account"`` — the model
      exists but is **not enabled for this account**. Pick another model; the
      key is fine.
    * ``410`` with an end-of-life date — the model was **retired**. Pick
      another model; nothing is wrong with the account.
    * ``503`` ``ResourceExhausted`` — the worker is **busy**. Retry later; the
      model and the account are both fine.

    ``body`` is the decoded response text, or ``""`` when there was none.
    """
    text = (body or "").strip()
    detail = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        detail = str(payload.get("detail") or payload.get("message") or "")
    lowered = (detail or text).lower()

    if status == 401 or status == 403:
        return "key rejected — check it with /nvidia-api-setup"
    if status == 404 and ("not found for account" in lowered or "function" in lowered):
        return ("this model is not enabled on your NVIDIA account — the key is "
                "fine, pick another model")
    if status == 410 or "end of life" in lowered:
        return "this model has been retired by NVIDIA — pick another model"
    if status == 429:
        return "rate limited (429) — the developer tier is ~40 requests/minute"
    if status == 503 or "resourceexhausted" in lowered:
        return "NVIDIA is at capacity for this model (503) — retry shortly"
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    return f"HTTP {status}"


def verify_key(token: str, model: str = "", *, timeout: float = 90.0) -> tuple[bool, str]:
    """Prove *token* works by making one real, tiny chat call.

    **The Models route cannot answer this.** ``/v1/models`` is public — it
    answers 200 with no credential and with a wrong one — so a probe against it
    would call a rejected key healthy and let the switch succeed, leaving the
    operator to discover the 401 on the first turn. This sends the smallest
    request that proves authentication, which is the only thing that does.

    ``ok`` is False **only** for a rejected key or an unreachable endpoint. A
    model that is retired, not enabled for the account, or temporarily at
    capacity says nothing about the credential, so those come back ``ok=True``
    with the reason in ``detail`` — failing the switch on them would refuse a
    working key over a model choice.
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
        return False, f"could not reach integrate.api.nvidia.com: {e}"
