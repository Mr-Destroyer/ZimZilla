"""Flex.ai's playground route — an unauthenticated Chat Completions dialect.

``https://eu.api.flex.ai/api/playground/chat`` is the endpoint behind Flex's
public model playground. It speaks Chat Completions and needs **no credential
at all**: measured on 2026-11-07, it answers 200 with no ``Authorization``
header, with an expired one, and with a bogus one. That is what makes it a
*direct* source in the same sense Token Juice, NVIDIA, wandb, InferX and
Inferbase are — no proxy, no port, nothing to start — with one difference that
shapes this module: there is no key to store, so the profile is a **route
record** rather than a credential, and ``/zim-flex`` needs no setup command.

What it is not
--------------
It is not a general-purpose inference API, and the difference is load-bearing
rather than pedantic. Three limits were measured against the live route, and
each is enforced by the endpoint rather than by this harness:

* **At most 10 messages.** An 11th answers ``400 {"detail": "Maximum 10
  messages allowed"}``. The system message counts toward the ten: a system plus
  nine answered 200, a system plus ten answered 400.
* **At most 2000 characters in the last message.** ``400 {"detail": "Message
  content too long (max 2000 characters)"}``. Only the *last* message is
  checked — 5000-character messages earlier in the history passed, and so did a
  60000-character system prompt — which is why the adapter trims the tail
  rather than the whole history.
* **No ``tool_calls``, ever.** A ``tools`` array is accepted and the model does
  emit a call-shaped reply, but it arrives as ordinary ``content`` text
  (``{"name": "get_weather", "arguments": "{\\"city\\": \\"Paris\\"}"}`` from
  gpt-oss-20b, ``get_weather(city="Paris")`` from the Qwen coders). Nothing in
  the stream is a ``delta.tool_calls[]``, so the harness's tool loop cannot fire
  on this route. It is a **chat** route, and this module says so rather than
  advertising tools that will never be called.

Two more behaviours the transport has to know about:

* **Errors arrive as 200.** A busy playground, an unknown model and an empty
  message list all answer ``200`` with an SSE frame
  ``{"error": {"code": "rate_limit" | "generic", "message": …}}``. A status-code
  check sees success; the frame is the failure. ``openai_compat`` maps the
  ``rate_limit`` code onto a 429 so the existing backoff retries it, which is
  what it needs — measured, the busy condition clears in seconds and the same
  request succeeds on the next attempt.
* **The reply is always SSE**, even for ``stream: false``. The adapter forces
  streaming for this host, because the non-streaming path would try to parse an
  SSE body as one JSON object and read an empty reply.

The catalog is static
---------------------
There is no Models route: ``/api/playground/models`` and ``/api/models`` both
answer 404, and ``/v1/models`` is the *authenticated* API's route (401 without a
key). The twelve ids below were read off the playground's own model picker and
each one was confirmed to answer a chat turn on 2026-11-07. They are declared
here rather than fetched, and :func:`fetch_models` returns them unchanged — a
"catalog" that made a network call to learn a constant would be a lie about
where the list comes from.

The window is deliberately small
--------------------------------
The playground publishes no context window, and the harness default is 1M.
Guessing high is the unrecoverable direction, so :data:`DEFAULT_CONTEXT` is
32K — and on this route the guess barely matters: ten messages of at most 2000
characters is a prompt bounded at roughly 5K tokens before the system message,
so a 32K window is generous rather than tight. A small window makes the harness
compact early, which is survivable; a large one overflows mid-turn, which is
not.

Nothing here echoes a credential, because there is none to echo.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
DEFAULT_ROOT = Path(__file__).resolve().parent.parent

#: The playground's Chat Completions route. This *is* the endpoint — there is no
#: ``/v1`` prefix to append and no base URL to derive it from, which is why
#: ``openai_compat.chat_completions_url`` returns it untouched for this host.
API_BASE_URL = "https://eu.api.flex.ai/api/playground/chat"

#: The playground's own limits, served publicly and used as the health check.
#: ``{"max_messages": 10, "max_message_chars": 2000}`` — the two numbers this
#: module enforces, read from the endpoint rather than hardcoded from a guess.
CONFIG_URL = "https://eu.api.flex.ai/api/playground/config"

#: The profile ``/zim-flex`` reads. It carries the route and the model, not a
#: credential: this endpoint authenticates nothing.
PROFILE = ZIMZILLA_HOME / "flex" / "source"

#: The model a switch selects when the profile names none. DeepSeek is the one
#: the operator asked for by name, and it is the strongest of the twelve.
DEFAULT_MODEL = "DeepSeek-V4-Flash-0731"

#: The playground's hard limits, measured 2026-11-07 and published by
#: :data:`CONFIG_URL`. Enforced by ``openai_compat._flex_payload``.
MAX_MESSAGES = 10
MAX_MESSAGE_CHARS = 2000

#: Appended to a message the adapter had to trim, so a model reading a cut-off
#: tool result knows it is reading one rather than a complete answer.
TRIM_MARK = "\n…[trimmed: the Flex playground caps the last message at 2000 chars]"

#: The window to plan against. See the module docstring: the playground
#: publishes none, and the harness default (1M) is the unrecoverable direction.
DEFAULT_CONTEXT = 32_768

#: Families that cannot answer a chat turn, matched as substrings of the
#: lowercased id. ``PaddleOCR-VL`` is the one entry in today's catalog that this
#: catches, and it is genuinely not a chat model — it is an OCR/VL model the
#: playground lists beside the chat ones. Deliberately narrow, and it errs
#: toward **keeping** an unknown id: hiding one the operator could have used is
#: worse than showing one that refuses, because the refusal is immediate.
_NON_CHAT_MARKERS: tuple[str, ...] = (
    "ocr", "embed", "bge", "rerank", "ranking", "reward",
    "clip", "whisper", "tts", "audio", "diffusion",
)

#: The twelve ids the playground's picker offered on 2026-11-07, each confirmed
#: to answer a chat turn. Order is the picker's own: the DeepSeek and gpt-oss
#: entries first, then the Qwen family, then the Gemma/Llama/Mistral tail.
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


@dataclass(frozen=True)
class Model:
    """One entry from the playground's model picker.

    Only the id is a fact the endpoint gave; the window is
    :data:`DEFAULT_CONTEXT` and the price is nothing, because the playground
    publishes neither. A default in this object would be indistinguishable from
    a measurement at the point of use, so both are properties with a stated
    reason rather than fields.
    """

    id: str

    @property
    def free(self) -> bool:
        """Whether this model costs money to call.

        Always true, and not as a guess: the playground is unauthenticated, so
        there is no account to bill and no key to meter. ``/model`` and
        ``/cost`` read ``free`` instead of billing a model at
        ``DEFAULT_PRICE``.
        """
        return True

    @property
    def chat(self) -> bool:
        """Whether this model can answer a text turn, by family."""
        lowered = self.id.lower()
        return not any(marker in lowered for marker in _NON_CHAT_MARKERS)

    @property
    def context_length(self) -> int:
        """The window to plan against, in tokens. Never ``None``."""
        return DEFAULT_CONTEXT

    @property
    def blurb(self) -> str:
        """A one-line description for the picker."""
        return f"free · no key · {self.context_length // 1000}k ctx"


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------


def is_flex(base_url: str | None) -> bool:
    """Whether *base_url* points at the Flex playground.

    Matched on host, because this route has no port to match on and the path is
    the endpoint itself rather than a base URL.
    """
    return "eu.api.flex.ai" in (base_url or "").strip().lower()


#: Generic name for the same test, so code that handles several sources
#: uniformly can call ``mod.active_endpoint(url)``.
active_endpoint = is_flex


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
    """The route and model, in the shape every other source's loader returns.

    ``(token, model, origin)`` with an **empty token**, because this endpoint
    authenticates nothing. The tuple shape is kept rather than special-cased so
    ``/zim-source``, ``switchable`` and ``/models`` can treat flex like any
    other source without a branch each — and so a caller that checks
    ``creds is None`` still means "this route is not set up", which is the
    question it is asking.

    Resolved in order, first hit wins:

      1. ``ANTHROPIC_BASE_URL`` in the environment pointing at the playground —
         i.e. the operator sourced the profile before launching.
      2. ZimZilla's own profile, shipped in the tree and rewritten by
         :func:`write_profile`.
    """
    if is_flex(os.environ.get("ANTHROPIC_BASE_URL")):
        return ("", os.environ.get("ANTHROPIC_MODEL", ""), "environment")

    exports = _read_exports(PROFILE)
    if exports:
        return ("", exports.get("ANTHROPIC_MODEL", ""), str(PROFILE))

    shipped = DEFAULT_ROOT / "packaging" / "flex" / "source"
    exports = _read_exports(shipped)
    if exports:
        return ("", exports.get("ANTHROPIC_MODEL", ""), str(shipped))

    return None


def write_profile(model: str = "") -> Path:
    """Write the route record to ZimZilla's own flex profile, mode 600.

    Same ``source`` format as every other profile so it can be sourced by a
    shell as well as read by the harness — but the token line is **empty on
    purpose**. This endpoint takes no credential, and writing a placeholder
    would turn "there is nothing to authenticate" into a 401 that looks like a
    broken account.
    """
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        "# ZimZilla — Flex playground route (written by /zim-flex)\n"
        "# No credential: this endpoint authenticates nothing.\n"
        f'export ANTHROPIC_BASE_URL="{API_BASE_URL}"\n'
        'export ANTHROPIC_AUTH_TOKEN=""\n'
        f'export ANTHROPIC_MODEL="{model or DEFAULT_MODEL}"\n'
        'export ANTHROPIC_API_KEY=""\n'
    )
    fd = os.open(PROFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        os.chmod(PROFILE, 0o600)
    except OSError:
        pass
    return PROFILE


def has_key() -> bool:
    """Whether the route is set up. There is no key, so this is a route check.

    Named ``has_key`` because that is the call every other source module
    answers and the callers are shared; the docstring is the place to say that
    here it means "the profile exists", not "a credential is present".
    """
    return load_credentials() is not None


def looks_like_key(value: str) -> bool:
    """Always False: this route has no key to set.

    Present so a shared caller that offers a key box can ask first. Returning
    False is the honest answer — there is no string that would be a Flex
    playground credential.
    """
    return False


# ---------------------------------------------------------------------------
# the catalog
# ---------------------------------------------------------------------------


def _parse_models(payload: dict) -> list[Model]:
    """Every row in a catalog payload, in catalog order.

    Kept for shape-compatibility with the other source modules; the playground
    has no Models route, so :func:`fetch_models` does not call it.
    """
    out: list[Model] = []
    data = payload.get("data")
    if not isinstance(data, list):
        return out
    for row in data:
        if not isinstance(row, dict):
            continue
        ident = row.get("id")
        if isinstance(ident, str) and ident:
            out.append(Model(id=ident))
    return out


def fetch_models(token: str = "", *, timeout: float = 20.0,
                 force: bool = False) -> tuple[list[Model] | None, str]:
    """The playground's catalog. Declared, not fetched — see the docstring.

    ``token``, ``timeout`` and ``force`` are accepted and ignored so this has
    the same signature as every other source's ``fetch_models``; a caller that
    passes a key is not making a mistake, there is simply nothing to send.
    """
    return [Model(id=ident) for ident in FLEX_MODELS], ""


def chat_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The chat-capable entries — the picker's list."""
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.chat], ""


def usable_free_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Every entry is free here, so this is the chat list."""
    return chat_models(token, **kw)


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    models, err = chat_models(token, **kw)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def catalog_age() -> float | None:
    """Always 0.0: the catalog is a constant, so it is never stale."""
    return 0.0


def cached_models() -> list[Model] | None:
    """The catalog, warm or cold — it is declared, so it is always available."""
    return [Model(id=ident) for ident in FLEX_MODELS]


def cached_ids() -> list[str] | None:
    models = cached_models()
    return [m.id for m in models if m.chat] if models is not None else None


def clear_cache() -> None:
    """Nothing to clear. Present so a shared caller can call it blindly."""


def model_for(model: str, models: list[Model] | None) -> Model | None:
    if not models:
        return None
    base = (model or "").strip()
    for m in models:
        if m.id == base:
            return m
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(0.0, 0.0)`` for any entry — the playground bills nothing."""
    found = model_for(model, models)
    if found is None:
        return None
    return (0.0, 0.0)


def default_free_model() -> str:
    return DEFAULT_MODEL


# ---------------------------------------------------------------------------
# errors and the route check
# ---------------------------------------------------------------------------


def explain_error(status: int, body: str) -> str:
    """A sentence for a Flex failure, from the shape it came back in.

    The playground's failures are not all status codes: a busy playground, an
    unknown model and an empty message list answer **200** with an SSE error
    frame, so ``status`` is often 200 and the frame is the whole story. The
    three shapes that matter:

    * ``rate_limit`` — the playground is busy. Transient; retry.
    * ``generic`` — the model id was not one it serves, or the request was
      malformed in a way it does not name. Pick another model.
    * ``400`` with a sentence naming a limit — the 10-message and 2000-character
      caps, which the adapter applies but a hand-built request can still hit.

    ``body`` is the decoded response text, or ``""`` when there was none.
    """
    text = (body or "").strip()
    detail = ""
    code = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            detail = str(error.get("message") or "")
        if not detail:
            detail = str(payload.get("detail") or payload.get("message") or "")
    lowered = (detail or text).lower()

    if code == "rate_limit" or "playground is busy" in lowered:
        return ("the playground is busy — it answers 200 with a rate_limit frame "
                "rather than a 429, and it clears in seconds; retry")
    if "maximum" in lowered and "messages" in lowered:
        return (f"the playground accepts at most {MAX_MESSAGES} messages, system "
                "included — the adapter trims history to fit, so this means the "
                "request was built by hand")
    if "too long" in lowered and "characters" in lowered:
        return (f"the playground caps the last message at {MAX_MESSAGE_CHARS} "
                "characters — the adapter trims it, so this means the request "
                "was built by hand")
    if status in (401, 403):
        return ("the playground refused the request — it needs no key, so this "
                "is a route or WAF change, not a credential")
    if status == 404:
        return ("no playground route at that address — the endpoint is "
                f"{API_BASE_URL}")
    if status == 422:
        return "the playground could not parse the request body (422)"
    if code == "generic" or "something went wrong" in lowered:
        return ("the playground could not generate a reply — usually an unknown "
                "model id; /flex-models lists the twelve it serves")
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    return f"HTTP {status}"


def verify(model: str = "", *, timeout: float = 60.0) -> tuple[bool, str]:
    """Prove the route answers by making one real, tiny chat call.

    There is no key to check and no Models route to probe, so the only thing
    that proves this route works is a chat turn. ``ok`` is False for an
    unreachable endpoint or a playground that answered with an error frame; a
    busy playground comes back ``ok=False`` with the reason, and the switch
    warns rather than refusing — a transient condition is not evidence that the
    route is wrong.
    """
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 8,
    }
    req = urllib.request.Request(
        API_BASE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "zimzilla"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except OSError:
            pass
        return False, f"HTTP {e.code} — {explain_error(e.code, raw)}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"could not reach eu.api.flex.ai: {e}"
    if '"choices"' in body:
        return True, "playground answered"
    if '"error"' in body:
        return False, explain_error(200, body)
    return False, "the playground answered with something that is not a chat frame"


def config(*, timeout: float = 10.0) -> dict | None:
    """The playground's published limits, or ``None`` if it did not answer.

    ``{"max_messages": 10, "max_message_chars": 2000}``. Read rather than
    assumed, so ``/flex-models`` can show the endpoint's own numbers instead of
    this module's copy of them.
    """
    req = urllib.request.Request(
        CONFIG_URL, headers={"Accept": "application/json",
                             "User-Agent": "zimzilla"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None
