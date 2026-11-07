"""Google Gemini — the AI Studio free tier, reached directly over Chat Completions.

Gemini is a *direct* source in the same shape as NVIDIA NIM and Inferbase: no
LiteLLM proxy, no port to start, no health check. Google serves an
**OpenAI-compatible** surface at ``/v1beta/openai``, so the harness's own
transport speaks to it with no translation layer, and the profile below is the
whole install.

Measured on this box, 2026-11-07, against the live endpoint with a free AI
Studio key:

* ``GET /v1beta/openai/models`` answers **only with a credential** — 61 entries
  with a key, ``404 Requested entity was not found`` without one. So unlike
  NVIDIA's public catalog, this one is key-gated: ``/gemini-models`` needs the
  key before it can list anything, and the switch probe *is* the key check.
* ``POST /v1beta/openai/chat/completions`` is standard OpenAI: SSE streaming
  works, ``tool_calls`` arrive as ``delta.tool_calls[]`` with a
  ``function.arguments`` JSON string, and ``finish_reason`` is ``tool_calls``.
  Verified on ``gemini-flash-lite-latest``.
* **The catalog is not the free tier.** 61 models are listed; **12** answer on a
  free key. The rest fail with ``429`` and ``limit: 0`` — which is not a rate
  limit but a hard "this model is not offered on free tier". Retrying never
  clears it. :func:`explain_error` says so in as many words, because the raw
  message ("please retry in 21h") sends the operator to wait for something that
  will not happen.
* **The thinking models need a real token budget.** ``gemini-3*``,
  ``gemini-3.5*``, ``gemini-3.6*`` and ``gemini-robotics-*`` spend the whole
  ``max_tokens`` on reasoning and return ``content: null``. Measured: 79-127
  reasoning tokens for a two-token answer. At ``max_tokens: 20`` the reply is
  empty; at 300 it is the answer. :data:`MIN_OUTPUT_TOKENS` carries the floor
  and :func:`is_thinking` the test, so the harness can raise the ceiling rather
  than report a working model as broken.
* **The base URL is not ``/v1``.** It is ``/v1beta/openai``, which the generic
  ``chat_completions_url`` would mangle into ``.../v1beta/openai/v1/chat/
  completions``. :func:`zimzilla.openai_compat.chat_completions_url` special-
  cases this host for exactly that reason.

**Free is an account property, not a per-model price.** The catalog publishes no
price at all, and the free tier is a quota on the *account*. So
:attr:`Model.free` is true for every entry, and :func:`price_for` returns
``(0.0, 0.0)`` so the harness's cost line reads ``free`` instead of billing a
model at ``DEFAULT_PRICE``.

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

#: Where the operator keeps their per-provider shell profiles.
CLAUDE_SOURCE = Path(os.environ.get("ZIMZILLA_CLAUDE_SOURCE")
                     or (Path.home() / "claude-source"))

#: The upstream API root. No proxy sits in front of it: this is the address the
#: session itself is pointed at, which is why it is a ``DirectRoute`` base URL
#: and not a ``localhost`` port.
#:
#: The ``/openai`` suffix is load-bearing. Google's native surface is
#: ``/v1beta/models/<id>:generateContent``; the OpenAI-compatible one is
#: ``/v1beta/openai``, and only the latter speaks the protocol this harness
#: sends. See :func:`zimzilla.openai_compat.chat_completions_url`, which
#: special-cases this host so the ``/v1`` rule does not append a second path.
API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

#: The model catalog. **Key-gated** — 404 without a credential — which is what
#: makes the switch probe a real key check rather than a formality.
MODELS_URL = f"{API_BASE_URL}/models"

#: The profile ``/gemini-api-setup`` writes and every lookup reads.
PROFILE = ZIMZILLA_HOME / "gemini" / "source"

#: AI Studio keys are issued as ``AQ.<long token>``. A hint for the confirmation
#: line, never a gate: the upstream is the authority on its own keys, and
#: refusing a real key of an unexpected shape would be worse than storing it.
KEY_PREFIX = "AQ."

#: The model to fall back to when the chosen one is gone from the catalog.
#: ``flash-lite-latest`` is the cheapest thing that answers on a free key and
#: does not spend its budget thinking, which makes it the right default for a
#: harness that sends many small turns.
DEFAULT_MODEL = "gemini-flash-lite-latest"

#: How long a fetched catalog is reused. The lineup turns over on Google's
#: schedule, not ours, and a stale list costs one 404 on a switch — which the
#: switch already handles by re-picking. Five minutes keeps ``/model``
#: responsive without pinning a retired name for a session.
CACHE_TTL = 300.0

#: One HTTP request per call. Short on purpose: a call that hangs is worth
#: abandoning, and the operator can simply run it again.
TIMEOUT = 25.0

#: The output-token floor for a thinking model. Below this the reply is empty
#: because the budget went to reasoning — measured at 79-127 reasoning tokens
#: for a two-token answer. 300 clears every model sampled with room to spare.
MIN_OUTPUT_TOKENS = 300

#: Families that reason before answering, and therefore need the floor above.
#: Matched on the id prefix, because Google's naming is consistent here and a
#: marker list would miss the next one.
_THINKING_PREFIXES: tuple[str, ...] = (
    "gemini-3-", "gemini-3.", "gemini-robotics-",
)

#: Families that cannot answer a text chat turn. The catalog carries image
#: generators, TTS, transcription, live/audio, music (Lyria), embeddings and
#: research agents; a chat request against any of them is a 400 at best.
#:
#: Deliberately narrow and erring toward **keeping** an unknown model: hiding
#: one the operator could have used is worse than showing one that 400s,
#: because the error is immediate and self-explanatory.
_NON_CHAT_MARKERS: tuple[str, ...] = (
    "embedding", "embed",
    "-image", "image-", "nano-banana",
    "-tts", "tts-", "transcribe", "-live", "live-", "native-audio",
    "lyria", "aqa",
    "computer-use", "deep-research", "antigravity",
    "omni",
)


@dataclass(frozen=True)
class Model:
    """One entry from Gemini's OpenAI-compatible model catalog.

    Only the fields the catalog actually sends are stored. Everything else —
    price, window, capability — is derived here or is deliberately absent,
    because the catalog sends none of it and a default in this object would be
    indistinguishable from a fact at the point of use.
    """

    id: str
    owned_by: str = ""
    created: int | None = None
    display_name: str = ""

    @property
    def free(self) -> bool:
        """Whether this model costs money to call.

        Always true on the free tier: the catalog publishes no per-model price,
        and the tier is a quota on the *account* rather than a price on the
        model. This is what makes ``/model`` and ``/cost`` read ``free``
        instead of billing a model at ``DEFAULT_PRICE``.

        It is **not** a claim that every listed model answers — 49 of the 61 do
        not, with ``limit: 0``. That is a quota fact the catalog cannot express,
        and :func:`explain_error` is where it is told.
        """
        return True

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn, by family."""
        lowered = self.id.lower()
        return not any(marker in lowered for marker in _NON_CHAT_MARKERS)

    @property
    def thinking(self) -> bool:
        """Whether this model reasons before answering."""
        return is_thinking(self.id)

    @property
    def context_length(self) -> int:
        """The window to plan against, in tokens.

        Never ``None``: the harness default is 1M, and a 1M assumption against a
        smaller model is an overflow the operator discovers mid-task. The
        direction of the guess is deliberate — too small compacts early, which
        is survivable; too large overflows, which is not.
        """
        return CONTEXT_LENGTHS.get(self.id, DEFAULT_CONTEXT)

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        bits = ["free (AI Studio tier)"]
        if self.thinking:
            bits.append("thinking")
        bits.append(f"{self.context_length // 1000}k ctx")
        if self.owned_by:
            bits.append(self.owned_by)
        return " · ".join(bits)


#: Context windows for the ids where Google's model page gave one. The rest get
#: :data:`DEFAULT_CONTEXT`.
CONTEXT_LENGTHS: dict[str, int] = {
    "gemini-flash-lite-latest": 1_048_576,
    "gemini-flash-latest": 1_048_576,
    "gemini-pro-latest": 1_048_576,
    "gemini-3-flash-preview": 1_048_576,
    "gemini-3.1-flash-lite": 1_048_576,
    "gemini-3.1-flash-lite-preview": 1_048_576,
    "gemini-3.5-flash": 1_048_576,
    "gemini-3.5-flash-lite": 1_048_576,
    "gemini-3.6-flash": 1_048_576,
    "gemini-3.7-flash": 1_048_576,
    "gemini-3.8-flash": 1_048_576,
    "gemma-4-26b-a4b-it": 131_072,
    "gemma-4-31b-it": 131_072,
}

#: The window assumed for an id with no entry above. 1M is the modal value
#: across the Gemini line, and every id this harness ships as a default has a
#: real number in the table.
DEFAULT_CONTEXT = 1_048_576


def is_thinking(model_id: str) -> bool:
    """Whether *model_id* reasons before answering.

    Exposed because the harness needs it at request time, not just for the
    picker: a thinking model sent ``max_tokens: 20`` returns an empty string,
    and the fix is to raise the ceiling rather than to report a failure.
    """
    lowered = (model_id or "").strip().lower()
    return any(lowered.startswith(p) for p in _THINKING_PREFIXES)


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------

def is_gemini(base_url: str | None) -> bool:
    """Whether *base_url* points at Gemini's OpenAI-compatible surface.

    Matched on host, because this route has no port to match on. The native
    ``/v1beta`` surface is accepted too: an operator who pasted the URL from
    Google's docs lands on the same service, and the switch rewrites it to the
    ``/openai`` path that actually speaks this protocol.
    """
    url = (base_url or "").strip().lower()
    return "generativelanguage.googleapis.com" in url


#: Generic name for the same test, so code that handles several sources
#: uniformly can call ``mod.active_endpoint(url)``.
active_endpoint = is_gemini


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
    """A Gemini key already sitting in ``~/claude-source/<name>``.

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
        if not is_gemini(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The Gemini key, however the operator supplied it.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_AUTH_TOKEN``/``ANTHROPIC_API_KEY`` **in the environment**,
         when ``ANTHROPIC_BASE_URL`` points at Gemini — i.e. the operator
         sourced a profile before launching.
      2. ZimZilla's own profile, written by ``/gemini-api-setup``.
      3. Any profile under ``~/claude-source`` aimed at this host.

    Returns ``(token, model, origin)`` or ``None``. The token is never logged
    or returned to the transcript — only ``origin`` (a path, or "environment").
    """
    if is_gemini(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Write the key to ZimZilla's own Gemini profile, mode 600.

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
        "# ZimZilla — Google Gemini profile (written by /gemini-api-setup)\n"
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
    strict enough to catch a pasted shell line or a bare word. The ``AQ.``
    prefix is *not* required: a real key of an unexpected shape is worth more
    stored than rejected, and a wrong one only costs a single 400.
    """
    value = (value or "").strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

#: Fetched catalogs, keyed by the token they were fetched with. Unlike NVIDIA's
#: this catalog is key-gated, so there is no keyless entry to cache under "".
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


def _strip_prefix(mid: str) -> str:
    """Drop the ``models/`` prefix the OpenAI-compat catalog puts on every id.

    The chat endpoint accepts either spelling — verified 2026-11-07 — but the
    bare id is what the operator retypes into ``/model`` and what the native
    API documents, so it is the one this module stores and shows.
    """
    mid = (mid or "").strip()
    return mid[len("models/"):] if mid.startswith("models/") else mid


def _parse_models(payload: dict) -> list[Model]:
    """Every entry in the catalog payload, in catalog order."""
    out: list[Model] = []
    for entry in _entries(payload):
        mid = _strip_prefix(str(entry.get("id") or ""))
        if not mid:
            continue
        created = entry.get("created")
        out.append(Model(
            id=mid,
            owned_by=str(entry.get("owned_by") or ""),
            created=created if isinstance(created, int) else None,
            display_name=str(entry.get("display_name") or ""),
        ))
    return out


def fetch_models(token: str = "", *, timeout: float = TIMEOUT,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch Gemini's model catalog. Returns ``(models, error)``.

    **A key is required** — the Models route answers ``404`` without one — so
    this is also the key check. ``models`` is ``None`` on failure, ``[]`` for a
    genuinely empty catalog: the caller can tell the two apart.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach Gemini" rather than take the session down.
    """
    token = (token or "").strip()
    if not token:
        return None, "no key — set one with /gemini-api-setup <key>"
    if use_cache:
        hit = _CACHE.get(token)
        if hit and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    req = urllib.request.Request(MODELS_URL, headers={
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
        "User-Agent": "zimzilla",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except OSError:
            pass
        if e.code in (400, 401, 403):
            return None, explain_error(e.code, body)
        return None, f"HTTP {e.code} from generativelanguage.googleapis.com"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach generativelanguage.googleapis.com: {e}"

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
    is callable on the free tier, and the tier charges the account's quota
    rather than the model. See :attr:`Model.free`.
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
    """Seconds since the cached catalog was fetched, or None when cold."""
    if not _CACHE:
        return None
    newest = max(ts for _, ts in _CACHE.values())
    return time.monotonic() - newest


def cached_models() -> list[Model] | None:
    """The cached catalog, or None when cold. Never fetches."""
    if not _CACHE:
        return None
    newest = max(_CACHE.values(), key=lambda v: v[1])
    return newest[0]


def cached_ids() -> list[str] | None:
    """The cached chat ids, or None when cold. Never fetches."""
    models = cached_models()
    if models is None:
        return None
    return [m.id for m in models if m.chat]


def clear_cache() -> None:
    """Drop the cached catalog — a new key invalidates the old one."""
    _CACHE.clear()


def model_for(model: str, models: list[Model] | None) -> Model | None:
    """The catalog entry for *model*, or None when it is not listed."""
    for m in models or []:
        if m.id == model:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(input, output)`` per 1M tokens — always ``(0.0, 0.0)`` here.

    The catalog publishes no price, and the free tier is a quota on the account
    rather than a price on the model. Returning ``None`` would fall through to
    the harness's estimate table and bill a free model at ``DEFAULT_PRICE``,
    which is the exact discrepancy this function exists to prevent.
    """
    return (0.0, 0.0)


def default_free_model() -> str:
    """The model to fall back to when the chosen one is gone from the catalog."""
    return DEFAULT_MODEL


# ---------------------------------------------------------------------------
# error shapes
# ---------------------------------------------------------------------------

def explain_error(status: int, body: str) -> str:
    """A sentence for a Gemini failure, from the shape it came back in.

    The shapes this endpoint actually returns are not interchangeable, and the
    catalog cannot predict any of them:

    * ``429`` with ``limit: 0`` — the model is **not offered on the free tier**.
      This is the one that matters: the raw message says "please retry in
      21h5m17s", which is a lie for this case. Retrying never clears it; the
      model has to be changed.
    * ``429`` without ``limit: 0`` — a real rate limit. Retry later.
    * ``400 Invalid Auth key`` — the credential is wrong. Note the status: a
      rejected Gemini key is a **400**, not a 401, so a caller that only checks
      401 would call a dead key healthy.
    * ``404 Requested entity was not found`` — the Models route without a key.
    * ``503`` high demand — transient; retry.

    ``body`` is the decoded response text, or ``""`` when there was none.
    """
    text = (body or "").strip()
    detail = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        detail = str(payload.get("message") or payload.get("detail") or "")
    elif isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            detail = str((first.get("error") or {}).get("message") or "")
    lowered = (detail or text).lower()

    if "invalid auth key" in lowered or "api key not valid" in lowered:
        return ("Gemini rejected the key — check it with /gemini-api-setup "
                "(a bad key answers 400, not 401)")
    if status in (401, 403):
        return "key rejected — check it with /gemini-api-setup"
    if status == 404 and "entity was not found" in lowered:
        return ("the Models route needs a key — set one with "
                "/gemini-api-setup <key>")
    if status == 429 and "limit: 0" in lowered:
        return ("this model is not offered on the free tier (quota limit: 0) — "
                "retrying will not help, pick another model")
    if status == 429:
        return "rate limited (429) — the free tier is per-model and per-day"
    if status == 503 or "high demand" in lowered:
        return "Gemini is at capacity for this model (503) — retry shortly"
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    return f"HTTP {status}"


def verify_key(token: str, model: str = "", *, timeout: float = 90.0) -> tuple[bool, str]:
    """Prove *token* works by making one real, tiny chat call.

    The Models route *would* answer this — it is key-gated, unlike NVIDIA's —
    but a chat call is still the better probe: it proves the credential **and**
    that the model the session is about to use is callable, in one round-trip.
    A key that lists a catalog but cannot call anything is not a working route.

    ``ok`` is False **only** for a rejected key or an unreachable endpoint. A
    model that is not on the free tier, or temporarily at capacity, says nothing
    about the credential, so those come back ``ok=True`` with the reason in
    ``detail`` — failing the switch on them would refuse a working key over a
    model choice.
    """
    token = (token or "").strip()
    if not token:
        return False, "no key"
    chosen = model or DEFAULT_MODEL
    payload = {
        "model": chosen,
        "messages": [{"role": "user", "content": "ok"}],
        # The floor, not 8: a thinking model sent 8 tokens returns an empty
        # string, and an empty reply is not evidence of a bad key.
        "max_tokens": MIN_OUTPUT_TOKENS,
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
        # A rejected Gemini key is a 400 with "Invalid Auth key", not a 401.
        if e.code in (401, 403) or "invalid auth key" in body.lower():
            return False, f"HTTP {e.code} — {explain_error(e.code, body)}"
        return True, explain_error(e.code, body)
    except (urllib.error.URLError, OSError) as e:
        return False, f"could not reach generativelanguage.googleapis.com: {e}"
