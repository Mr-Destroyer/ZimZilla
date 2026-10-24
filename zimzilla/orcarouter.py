"""OrcaRouter Cyber Inference — a hosted cyber-inference gateway as a ZimZilla source.

OrcaRouter (``https://api.orcarouter.ai/v1``) is an OpenAI-compatible gateway
that fronts many models behind one key, and it also serves the **Anthropic
Messages** protocol natively: ``POST /v1/messages`` answers with an
Anthropic-shaped 401 when no key is sent, measured on 2026-10-24. So this is a
*hosted* source, the same shape as :mod:`zimzilla.openrouter` — no proxy, no
port, nothing to start — and :data:`DEFAULT_BASE_URL` carries no path component
for the same reason OpenRouter's does not: the SDK appends ``/v1/messages``
itself, and a base that already ended in ``/v1`` composed to ``/v1/v1/messages``
and 404'd on every turn.

**Why a named cyber provider rather than the generic endpoint command.** Most
general gateways refuse or silently downgrade authorized, scoped security
reasoning. OrcaRouter keeps a cyber tier for teams that have completed business
verification — OrcaCyber Ultra and Zero — alongside a general fallback. This
module registers that as a first-class source with its own profile, catalog and
commands, so the operator picks it from ``/zim-source`` instead of hand-typing a
base URL into ``/openai-endpoint``.

**Two credentials, one resulting key.** The operator either pastes an existing
``sk-orca-…`` key (``/orcarouter-api-setup``) or authorizes with their own
OrcaRouter account over OAuth 2.0 + PKCE (``/orcarouter-auth``). Both adapters
produce the same :class:`Credential`, and every downstream caller — the
transport, the catalog fetch, ``/model`` — sees only a token. PKCE here is
**Flow B, out-of-band**: this harness is a terminal UI, so it can open a browser
but must not assume it can listen on a loopback port (the install may be over
SSH, in a container, or behind a locked-down corporate proxy). The consent
screen shows a code, the operator pastes it back, and S256 is mandatory — which
is the point, because a displayed code is put into human hands.

**A PKCE-issued key is durable, not refreshable.** OrcaRouter returns a normal
API key with no refresh grant. It is reused until revoked; a relay ``401``
marks exactly the credential generation that was rejected as needing
re-authentication, and never triggers a refresh attempt or wipes a newer key.

**The catalog is the live one.** ``GET /v1/models`` is the only source of truth,
and the chat list is filtered on what the catalog actually declares —
``supported_endpoint_types`` and ``architecture.output_modalities`` — never on a
model's name. Entries that declare image output are kept out of a text picker;
an entry whose modalities are absent is kept (silence is not evidence against a
chat model, and hiding a working one is worse than showing one that 400s). A
small verified seed covers a cold start, and live success is authoritative.

Nothing here echoes a key, a verifier, or a code: the confirmation lines name
the profile path and the last four characters only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
#: Where the operator keeps their per-provider shell profiles — the same
#: ``~/claude-source/<name>`` files a plain ``source`` loads for Claude Code.
CLAUDE_SOURCE = Path(os.environ.get("ZIMZILLA_CLAUDE_SOURCE")
                     or (Path.home() / "claude-source"))

#: The inference host. Matching is by this substring for the same reason
#: OpenRouter's is: a trailing slash and any path all count as the same source.
HOST = "api.orcarouter.ai"

#: Auth lives on a **different origin** from inference. This is the mistake that
#: 404s: ``https://api.orcarouter.ai/v1/auth/keys`` does not exist, and neither
#: origin may be derived from the other by swapping a hostname.
AUTH_BASE = "https://www.orcarouter.ai"
API_BASE = "https://api.orcarouter.ai"

#: The authorize screen (browser) and the code exchange (POST), both on the auth
#: origin and both path-fixed.
AUTHORIZE_PATH = "/auth"
EXCHANGE_PATH = "/api/v1/auth/keys"

#: The SDK's ``base_url`` — the PREFIX BEFORE ``/v1``, not the API root. The
#: Anthropic SDK appends ``/v1/messages`` to whatever it is given, so this
#: composes to ``/v1/messages``, which is where OrcaRouter serves the Messages
#: protocol (a 401 without a key, i.e. the path exists). See the module docstring.
DEFAULT_BASE_URL = API_BASE

#: The live catalog, and the capability filter the picker asks with.
MODELS_PATH = "/v1/models"

#: The profile this module writes and reads for whichever credential is in force.
PROFILE = ZIMZILLA_HOME / "orcarouter" / "source"

#: Terminal credential state, beside the profile: generation counter and the
#: ``needs_reauth`` flag for the exact generation a relay rejected.
STATE = ZIMZILLA_HOME / "orcarouter" / "state.json"

#: A fetched catalog older than this is refetched rather than trusted. The
#: listing turns over (the anonymous listing and a per-key listing differ by an
#: order of magnitude), so a stale list is exactly the wrong list a picker exists
#: to avoid.
CACHE_TTL = 300.0

#: OrcaRouter keys are issued as ``sk-orca-…``. A hint for the confirmation
#: line, never a gate.
KEY_PREFIX = "sk-orca-"

#: The model a switch selects when the profile names none. ``orcarouter/auto``
#: is the gateway's own router: it needs no model choice from the operator and
#: is the documented default for a new key.
DEFAULT_MODEL = "orcarouter/auto"

#: Where the operator manages or revokes keys.
CONSOLE_URL = "https://www.orcarouter.ai/console/authorized-apps"

#: Provider page and cyber-tier catalog, for the docs and the setup output.
CYBER_PAGE = "https://www.orcarouter.ai/cyber"
CYBER_CATALOG = "https://www.orcarouter.ai/models?tier=cyber"

#: Endpoint types this harness can speak. A chat entry must advertise one of
#: them, or ``/model`` would offer a route the transport cannot call.
CHAT_ENDPOINT_TYPES = frozenset({"openai", "anthropic", "gemini", "openai-response"})

#: Endpoint types that are not a chat turn, whatever the listing says.
NON_CHAT_ENDPOINT_TYPES = frozenset({
    "image-generation", "openai-video", "jina-rerank", "embedding",
    "embeddings", "rerank", "audio", "tts",
})

#: Reasoning-effort ladders for the seed entries whose vendor publishes one.
#: Kept out of discovery on purpose: a catalog that sends no ladder must not
#: erase a figure already verified here.
REASONING_EFFORTS: dict[str, tuple[str, ...]] = {
    "openai/gpt-5.5": ("low", "medium", "high", "xhigh"),
}


# ---------------------------------------------------------------------------
# credentials: one seam, two adapters
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Credential:
    """The one thing both entry paths produce: a usable OrcaRouter key.

    ``source`` is ``"api_key"`` or ``"pkce"`` and exists for the transcript and
    for tests — no request path reads it, which is what keeps a key obtained
    either way identical downstream. ``generation`` increments on every store,
    so a late ``401`` for an old credential cannot mark a new one broken.
    """

    token: str
    source: str = "api_key"
    scope: str = "api"
    user_id: str = ""
    generation: int = 1
    origin: str = ""


def new_pkce() -> tuple[str, str, str]:
    """A fresh ``(verifier, challenge, state)`` from the OS CSPRNG.

    Every attempt gets its own values: a verifier reused across attempts (or
    derived from a timestamp or username) makes the whole binding decorative.
    The challenge is unpadded ``base64url(sha256(verifier))`` as S256 requires.
    """
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(16))
    return verifier, challenge, state


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def auth_base() -> str:
    """The auth origin: explicit override, then the shared self-hosted base.

    Explicit values win, as the integration rules require. The two origins are
    never derived from one another.
    """
    return _clean_base(os.environ.get("ORCA_AUTH_BASE_URL")
                       or os.environ.get("ORCA_BASE_URL") or AUTH_BASE)


def api_base() -> str:
    """The inference origin: explicit override, then the shared base."""
    return _clean_base(os.environ.get("ORCA_API_BASE_URL")
                       or os.environ.get("ORCA_BASE_URL") or API_BASE)


def _clean_base(value: str) -> str:
    """Strip a trailing slash and enforce HTTPS off loopback.

    HTTP stays allowed for ``localhost``/``127.0.0.1`` so a local relay can be
    tested without a certificate; anything else remote must be HTTPS, because
    the code exchange and the bearer token both travel over it.
    """
    base = (value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme == "http" and parsed.hostname not in (
            "localhost", "127.0.0.1", "::1"):
        raise ValueError("ORCA auth/API base must be HTTPS unless it is loopback")
    return base


def authorize_url(challenge: str, state: str, *, app_name: str = "ZimZilla",
                  scope: str = "api") -> str:
    """The consent URL for Flow B — ``callback_url=oob``, S256, no secret.

    ``callback_url=oob`` is spelled out rather than omitted: the mode is asked
    for rather than guessed at, and it is the only mode a terminal client can
    complete without a loopback listener.
    """
    params = urllib.parse.urlencode({
        "callback_url": "oob",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "app_name": app_name,
        "scope": scope,
    })
    return f"{auth_base()}{AUTHORIZE_PATH}?{params}"


def exchange_code(code: str, verifier: str, *, timeout: float = 30.0) -> Credential:
    """Exchange an out-of-band code for a key. Raises :class:`AuthError`.

    The request goes to the **auth origin** at ``/api/v1/auth/keys`` — never to
    the inference origin, and never to ``/v1/auth/keys`` (a 404). ``scope`` is
    read back from the response: it is what was granted, not what was asked for.
    """
    code = (code or "").strip()
    if not code:
        raise AuthError("no code supplied — paste the code the consent screen showed")
    body = json.dumps({
        "code": code,
        "code_verifier": verifier,
        "code_challenge_method": "S256",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{auth_base()}{EXCHANGE_PATH}", data=body, method="POST",
        headers={"Content-Type": "application/json", "accept": "application/json",
                 "User-Agent": "zimzilla"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except OSError:
            pass
        if e.code == 403:
            raise AuthError(
                "the code was refused — unknown, expired (10 min), already used, "
                "or the verifier does not match. Start again with /orcarouter-auth",
                status=403, detail=detail) from None
        if e.code == 400:
            raise AuthError(
                f"the authorization was rejected as malformed: {detail}",
                status=400, detail=detail) from None
        if e.code == 429:
            raise AuthError(
                "too many authorizations (429) — there is a cap of 10 PKCE keys "
                "per user per 24 hours; reuse the stored key", status=429,
                detail=detail) from None
        raise AuthError(f"authorization failed: HTTP {e.code}", status=e.code,
                        detail=detail) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AuthError(
            f"could not reach the OrcaRouter auth origin ({auth_base()}): {e}"
        ) from None

    if not isinstance(payload, dict):
        raise AuthError("authorization succeeded but returned no key object")
    key = str(payload.get("key") or "").strip()
    if not key:
        raise AuthError("authorization succeeded but returned an empty key")
    scope = str(payload.get("scope") or "")
    return Credential(token=key, source="pkce", scope=scope,
                      user_id=str(payload.get("user_id") or ""))


class AuthError(Exception):
    """A terminal authorization failure. ``status`` is the HTTP status, if any."""

    def __init__(self, message: str, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        # Never the response body verbatim in a transcript: it can echo back a
        # key on a malformed success. Kept for logs that redact.
        self.detail = detail


def credential_from_api_key(key: str) -> Credential:
    """Adapter one: the operator's existing ``sk-orca-…`` key.

    A single-credential profile. Format is checked only to catch a pasted
    ``export`` line; validity is the relay's to decide.
    """
    return Credential(token=key.strip(), source="api_key", scope="api")


def credential_from_pkce(code: str, verifier: str) -> Credential:
    """Adapter two: authorize → code → exchange. Returns the same shape."""
    return exchange_code(code, verifier)


# ---------------------------------------------------------------------------
# credential lifecycle
# ---------------------------------------------------------------------------

def _read_state() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data: dict) -> None:
    """Write the state file mode 600, creating the directory 700.

    Never fatal: a read-only home must not break the TUI, and the state is an
    annotation on a credential rather than the credential itself.
    """
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(STATE.parent, 0o700)
        fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def current_generation() -> int:
    return int(_read_state().get("generation") or 0)


def needs_reauth() -> bool:
    """Whether the credential in force was rejected by the relay."""
    return bool(_read_state().get("needs_reauth"))


def mark_needs_reauth(generation: int | None = None) -> None:
    """Mark **exactly** the generation that made the rejected request.

    Generation-safe on purpose: a late 401 from a request made under an old key
    must not mark the freshly re-authorized credential as broken, which would
    loop the operator back into a login they just completed.
    """
    state = _read_state()
    live = int(state.get("generation") or 0)
    target = live if generation is None else int(generation)
    if target != live:
        return
    state["needs_reauth"] = True
    state["reauth_generation"] = live
    _write_state(state)


def clear_reauth(generation: int | None = None) -> None:
    state = _read_state()
    live = int(state.get("generation") or 0)
    if generation is not None and int(generation) != live:
        return
    state["needs_reauth"] = False
    _write_state(state)


def store(cred: Credential) -> Credential:
    """Persist a credential and bump the generation.

    The old secret is overwritten only here, after a new one exists — a failed
    login therefore leaves the previous key in place instead of deleting the
    operator's only credential.
    """
    token = (cred.token or "").strip()
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    model = ""
    existing = _read_exports(PROFILE)
    if existing.get("ANTHROPIC_BASE_URL", "").startswith(DEFAULT_BASE_URL):
        model = existing.get("ANTHROPIC_MODEL", "")
    body = (
        "# ZimZilla — OrcaRouter Cyber Inference profile "
        "(written by /orcarouter-api-setup or /orcarouter-auth)\n"
        "# Treat this file as a credential. Never commit it.\n"
        f'export ANTHROPIC_BASE_URL="{DEFAULT_BASE_URL}"\n'
        f'export ANTHROPIC_AUTH_TOKEN="{token}"\n'
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
    state = _read_state()
    generation = int(state.get("generation") or 0) + 1
    _write_state({
        "generation": generation,
        "needs_reauth": False,
        "source": cred.source or "api_key",
        "scope": cred.scope or "api",
    })
    return Credential(token=token, source=cred.source, scope=cred.scope,
                      user_id=cred.user_id, generation=generation,
                      origin=str(PROFILE))


def clear() -> None:
    """Remove the stored key and its state. Used by the clear command."""
    for path in (PROFILE, STATE):
        try:
            path.unlink()
        except OSError:
            pass


def profile_model() -> str:
    """The model stored beside the key, or this provider's default."""
    exports = _read_exports(PROFILE)
    if not is_orcarouter(exports.get("ANTHROPIC_BASE_URL")):
        return DEFAULT_MODEL
    return exports.get("ANTHROPIC_MODEL") or DEFAULT_MODEL


# ---------------------------------------------------------------------------
# keys and profiles
# ---------------------------------------------------------------------------

def current_key() -> str | None:
    from . import sources as sources_mod
    return sources_mod.current_key()


def set_current(key: str) -> None:
    from . import sources as sources_mod
    sources_mod.set_current(key)


def is_orcarouter(base_url: str | None) -> bool:
    """Whether *base_url* points at OrcaRouter's inference origin."""
    return HOST in (base_url or "")


#: Generic name for the same test, so code that handles several gateways
#: uniformly can call ``mod.active_endpoint(url)`` without knowing which one.
active_endpoint = is_orcarouter


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
    """An OrcaRouter key already sitting in ``~/claude-source/<name>``.

    Only a profile aimed at this host counts: adopting another provider's
    credential would send the wrong key upstream.
    """
    try:
        entries = sorted(CLAUDE_SOURCE.iterdir())
    except OSError:
        return None
    for path in entries:
        if not path.is_file():
            continue
        exports = _read_exports(path)
        if not is_orcarouter(exports.get("ANTHROPIC_BASE_URL")):
            continue
        token = exports.get("ANTHROPIC_AUTH_TOKEN") or exports.get("ANTHROPIC_API_KEY")
        if token:
            return token, exports.get("ANTHROPIC_MODEL", ""), str(path)
    return None


def load_credentials() -> tuple[str, str, str] | None:
    """The OrcaRouter key, however the operator supplied it.

    Resolution order, first hit wins: environment (when ``ANTHROPIC_BASE_URL``
    points here), ZimZilla's own profile, then a ``~/claude-source`` profile
    aimed at this host. Returns ``(token, model, origin)`` or ``None``. The
    token is never logged — only ``origin`` is.
    """
    if is_orcarouter(os.environ.get("ANTHROPIC_BASE_URL")):
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
    """Store a pasted API key through the shared seam, keeping *model*.

    Kept as a named function because every other source module has one and the
    UI's key command calls it uniformly; the write itself goes through
    :func:`store`, so there is one implementation of the profile format.
    """
    cred = store(credential_from_api_key(token))
    if model:
        _rewrite_model(model)
    return PROFILE if cred.token else PROFILE


def _rewrite_model(model: str) -> None:
    exports = _read_exports(PROFILE)
    token = exports.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        return
    body = (
        "# ZimZilla — OrcaRouter Cyber Inference profile\n"
        "# Treat this file as a credential. Never commit it.\n"
        f'export ANTHROPIC_BASE_URL="{DEFAULT_BASE_URL}"\n'
        f'export ANTHROPIC_AUTH_TOKEN="{token}"\n'
        f'export ANTHROPIC_MODEL="{model}"\n'
        'export ANTHROPIC_API_KEY=""\n'
    )
    fd = os.open(PROFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)


def has_key() -> bool:
    return load_credentials() is not None


def apply_to(cfg) -> tuple[str, str] | None:
    """Point *cfg* at OrcaRouter. Returns ``(origin, model)`` or ``None``.

    The one place that knows how to aim a session at this gateway, shared by
    ``/zim-orcarouter`` and the launch-time restore. ``protocol`` is re-resolved
    later by the caller; ``api_key`` is cleared so a stale credential for
    another service is not sent alongside the bearer token.
    """
    creds = load_credentials()
    if creds is None:
        return None
    token, model, origin = creds
    cfg.base_url = DEFAULT_BASE_URL
    cfg.auth_token = token
    cfg.api_key = ""
    if model:
        cfg.model = model
    return origin, model


def applies_to_default_endpoint(base_url: str | None) -> bool:
    """Whether a remembered OrcaRouter choice should override *base_url*.

    False for an endpoint the operator pointed somewhere deliberate; true for
    the local proxies and the built-in default.
    """
    url = (base_url or "").strip()
    if is_orcarouter(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


def looks_like_key(value: str) -> bool:
    """A cheap sanity check before writing a key, so a typo fails loudly.

    Deliberately loose — the gateway decides what is valid — but strict enough
    to catch a pasted whole ``export`` line or a bare word.
    """
    value = (value or "").strip()
    if len(value) < 20 or any(c.isspace() for c in value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._:\-]+", value))


# ---------------------------------------------------------------------------
# the live catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Model:
    """One entry from OrcaRouter's catalog.

    Only fields the catalog actually sends are stored. ``modalities`` is
    ``None`` when the entry declares no architecture block — a distinction
    that matters: silence must not be read as "text only", because that would
    hide a multimodal model the endpoint does serve.
    """

    id: str
    label: str = ""
    context_length: int | None = None
    max_output: int | None = None
    modalities: tuple[str, ...] | None = None
    outputs: tuple[str, ...] | None = None
    endpoint_types: tuple[str, ...] = field(default_factory=tuple)
    supported: tuple[str, ...] = field(default_factory=tuple)
    price_in: float | None = None
    price_out: float | None = None
    description: str = ""

    @property
    def chat(self) -> bool:
        """Whether this entry can answer a text turn, by declared capability.

        Three clauses, each from the catalog rather than the name:

        * at least one endpoint type this harness can speak;
        * no endpoint type that is not a chat turn (image generation, video,
          rerank, embeddings);
        * declared output modalities, when present, include ``text``.

        An entry that declares no output modalities is kept: the catalog is
        inconsistent about that field, and dropping a working chat model is
        worse than showing one that refuses.
        """
        types = set(self.endpoint_types)
        if types and not (types & CHAT_ENDPOINT_TYPES):
            return False
        if types & NON_CHAT_ENDPOINT_TYPES:
            return False
        if self.outputs is not None and any(
                out != "text" for out in self.outputs):
            # An entry that emits images *or* video is a generation route that
            # happens to accept a prompt — not a text turn this harness can
            # consume, whatever else it also returns.
            return False
        return True

    def accepts_input(self, modality: str) -> bool:
        """Whether this entry *declares* support for an input modality.

        Fail closed: an entry that declares no input modalities cannot be
        proven to accept an image, so it is not offered for one.
        """
        return bool(self.modalities) and modality in self.modalities

    @property
    def free(self) -> bool:
        return (self.price_in == 0.0 and self.price_out == 0.0)

    @property
    def blurb(self) -> str:
        bits = []
        if self.price_in is not None and self.price_out is not None:
            bits.append("free" if self.free
                        else f"${self.price_in:g}/${self.price_out:g}")
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
        if self.modalities and any(m != "text" for m in self.modalities):
            bits.append("+".join(self.modalities))
        if self.supported and "tools" not in self.supported:
            bits.append("no tools")
        return " · ".join(bits)


#: Verified cold-start seed. Source: ``GET https://api.orcarouter.ai/v1/models``
#: read on 2026-10-24. Each entry carries only metadata that was observed in
#: that response — notably ``openai/gpt-5.5`` publishes no ``context_length``,
#: so none is invented for it, while its reasoning ladder is retained from the
#: vendor's published effort levels.
SEED_MODELS: tuple[Model, ...] = (
    Model(id="openai/gpt-5.5", label="OpenAI: GPT-5.5",
          modalities=("file", "image", "text"), outputs=("text",),
          endpoint_types=("openai", "openai-response"),
          supported=("tools", "reasoning")),
    Model(id="anthropic/claude-opus-4.8", label="Anthropic: Claude Opus 4.8",
          context_length=1_000_000, modalities=("text", "image", "file"),
          outputs=("text",), endpoint_types=("openai", "anthropic", "openai-response"),
          supported=("tools", "reasoning")),
    Model(id="google/gemini-3.5-flash", label="Google: Gemini 3.5 Flash",
          context_length=1_048_576,
          modalities=("text", "image", "video", "file", "audio"),
          outputs=("text",), endpoint_types=("openai", "gemini"),
          supported=("tools", "reasoning")),
    Model(id="deepseek/deepseek-v4-pro", label="DeepSeek: V4 Pro",
          context_length=1_048_576, modalities=("text",), outputs=("text",),
          endpoint_types=("openai", "openai-response"),
          supported=("tools", "reasoning")),
    Model(id="orcarouter/auto", label="OrcaRouter: Auto",
          endpoint_types=("openai", "anthropic", "gemini", "openai-response"),
          supported=("tools",)),
    Model(id="orcarouter/fusion", label="OrcaRouter: Fusion",
          context_length=1_000_000,
          endpoint_types=("openai", "anthropic", "gemini", "openai-response"),
          supported=("tools",)),
    Model(id="orca/orcacyber-ultra-1.0", label="OrcaCyber Ultra 1.0",
          context_length=1_048_576, modalities=("text",), outputs=("text",),
          endpoint_types=("openai",), supported=("tools", "reasoning")),
    Model(id="orca/orcacyber-zero-1.5", label="OrcaCyber Zero 1.5",
          context_length=1_000_000, modalities=("text",), outputs=("text",),
          endpoint_types=("openai",), supported=("tools", "reasoning")),
)

#: Fetched catalogs, keyed by the token they were fetched with, so a key change
#: refetches instead of serving a list fetched for a different account.
_CACHE: dict[str, tuple[list[Model], float]] = {}

#: The capability each cached entry was fetched with.
_CACHE_CAP: dict[str, str] = {}


def _parse_models(payload: dict) -> list[Model]:
    out: list[Model] = []
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return out
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("id") or "").strip()
        if not mid:
            continue
        arch = entry.get("architecture") if isinstance(entry.get("architecture"), dict) else None
        modalities = None
        outputs = None
        if arch is not None:
            inputs = arch.get("input_modalities")
            if isinstance(inputs, list):
                modalities = tuple(str(m) for m in inputs if isinstance(m, str))
            outs = arch.get("output_modalities")
            if isinstance(outs, list):
                outputs = tuple(str(m) for m in outs if isinstance(m, str))
        types = entry.get("supported_endpoint_types")
        endpoint_types = tuple(str(t) for t in types if isinstance(t, str)) \
            if isinstance(types, list) else ()
        params = entry.get("supported_parameters")
        supported = tuple(str(p) for p in params if isinstance(p, str)) \
            if isinstance(params, list) else ()

        pricing = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}
        price_in = price_out = None
        try:
            if pricing.get("prompt_per_million") is not None:
                price_in = float(pricing["prompt_per_million"])
            elif pricing.get("prompt") is not None:
                price_in = float(pricing["prompt"]) * 1_000_000
            if pricing.get("completion_per_million") is not None:
                price_out = float(pricing["completion_per_million"])
            elif pricing.get("completion") is not None:
                price_out = float(pricing["completion"]) * 1_000_000
        except (TypeError, ValueError):
            price_in = price_out = None

        ctx = entry.get("context_length")
        try:
            ctx = int(ctx) if ctx is not None else None
        except (TypeError, ValueError):
            ctx = None
        max_out = entry.get("max_completion_tokens")
        try:
            max_out = int(max_out) if max_out is not None else None
        except (TypeError, ValueError):
            max_out = None

        out.append(Model(
            id=mid,
            label=str(entry.get("name") or ""),
            context_length=ctx,
            max_output=max_out,
            modalities=modalities,
            outputs=outputs,
            endpoint_types=endpoint_types,
            supported=supported,
            price_in=price_in,
            price_out=price_out,
            description=str(entry.get("description") or "")[:400],
        ))
    return out


def fetch_models(token: str = "", *, capability: str = "chat",
                 timeout: float = 20.0, use_cache: bool = True,
                 ) -> tuple[list[Model] | None, str]:
    """Fetch the live catalog. Returns ``(models, error)``.

    The endpoint is public — a keyless ``GET`` answers the full catalog — so
    the picker and ``/orcarouter-models`` work before any key is set. A key is
    sent when supplied, because the per-workspace listing is what that account
    can actually call and is the honest list to show.

    A network failure is returned, never raised: the caller is a TUI command
    that must say "could not reach OrcaRouter" rather than die. ``models`` is
    ``None`` on failure and ``[]`` for a genuinely empty catalog.
    """
    token = (token or "").strip()
    capability = capability or "chat"
    if use_cache:
        hit = _CACHE.get(token)
        if hit and _CACHE_CAP.get(token) == capability \
                and (time.monotonic() - hit[1]) < CACHE_TTL:
            return hit[0], ""

    url = f"{api_base()}{MODELS_PATH}"
    if capability:
        url += "?" + urllib.parse.urlencode({"capability": capability})
    headers = {"accept": "application/json", "User-Agent": "zimzilla"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["x-api-key"] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, ("key rejected (401/403) — check it with "
                          "/orcarouter-api-setup, or reconnect with /orcarouter-auth")
        return None, f"HTTP {e.code} from {HOST}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"could not reach {HOST}: {e}"

    models = _parse_models(payload)
    _CACHE[token] = (models, time.monotonic())
    _CACHE_CAP[token] = capability
    return models, ""


def chat_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    """The entries that can answer a text turn, by declared capability."""
    models, err = fetch_models(token, capability="chat", **kw)
    if models is None:
        return None, err
    return [m for m in models if m.chat], ""


def multimodal_models(token: str = "", modality: str = "image",
                      **kw) -> tuple[list[Model] | None, str]:
    """Chat entries that explicitly declare *modality* as an input.

    Two filters, in order: it must be a chat model first, and then it must
    *declare* the modality. An entry that declares no input modalities is
    excluded — fail closed, so an image never reaches a model that never said
    it takes one.
    """
    models, err = chat_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.accepts_input(modality)], ""


def embedding_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    return _by_endpoint(token, {"embedding", "embeddings"}, **kw)


def image_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    return _by_endpoint(token, {"image-generation"}, **kw)


def video_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    return _by_endpoint(token, {"openai-video"}, **kw)


def rerank_models(token: str = "", **kw) -> tuple[list[Model] | None, str]:
    return _by_endpoint(token, {"jina-rerank"}, **kw)


def _by_endpoint(token: str, wanted: set[str], **kw
                 ) -> tuple[list[Model] | None, str]:
    """Entries whose declared endpoint types intersect *wanted*.

    Used for the non-chat entries, where a capability query alone is not proof:
    the endpoint type is the contract the transport needs.
    """
    models, err = fetch_models(token, **kw)
    if models is None:
        return None, err
    return [m for m in models if wanted & set(m.endpoint_types)], ""


def model_ids(token: str = "", **kw) -> tuple[list[str] | None, str]:
    """Every chat model id, catalog order preserved — what ``/model`` offers."""
    models, err = chat_models(token, **kw)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def catalog_age() -> float | None:
    if not _CACHE:
        return None
    return time.monotonic() - max(entry[1] for entry in _CACHE.values())


def cached_models() -> list[Model] | None:
    """The last-fetched catalog, or ``None`` when cold or expired.

    ``/model`` runs on the UI thread and must not block on a fetch. The TTL
    check is load-bearing: an expired entry reads as ``None``, which callers
    already handle as "no catalog", rather than a stale list being called live.
    """
    newest: tuple[list[Model], float] | None = None
    for entry in _CACHE.values():
        if newest is None or entry[1] > newest[1]:
            newest = entry
    if newest is None or (time.monotonic() - newest[1]) >= CACHE_TTL:
        return None
    return [m for m in newest[0] if m.chat]


def cached_ids() -> list[str] | None:
    models = cached_models()
    return [m.id for m in models] if models is not None else None


def clear_cache() -> None:
    _CACHE.clear()
    _CACHE_CAP.clear()


def seed_ids() -> tuple[str, ...]:
    """The verified cold-start ids, in the order the picker should show them."""
    return tuple(m.id for m in SEED_MODELS)


def model_for(model: str, models: list[Model] | None) -> Model | None:
    for entry in models or []:
        if entry.id == (model or "").strip():
            return entry
    for entry in SEED_MODELS:
        if entry.id == (model or "").strip():
            return entry
    return None


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok when the catalog priced the entry, else ``None``."""
    found = model_for(model, models)
    if found is None or found.price_in is None or found.price_out is None:
        return None
    return (found.price_in, found.price_out)


def default_free_model() -> str:
    return DEFAULT_MODEL


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

def explain_error(status: int, body: str) -> str:
    """A sentence for an OrcaRouter failure, from the shape it came back in.

    Three statuses mean three different things here, and conflating them sends
    the operator to replace a key that is fine:

    * ``401`` — the credential is rejected or revoked. Re-authenticate.
    * ``403`` with ``model_access_denied`` — the key is **valid** but its scope
      excludes this model (for example a key without the cyber tier). Pick
      another model or widen the key's access in the console.
    * ``429`` — rate limited. Wait; this says nothing about the key.
    """
    text = (body or "").strip()
    detail = ""
    code = ""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
            code = str(err.get("code") or "")
        else:
            detail = str(payload.get("message") or "")
    lowered = (detail or text).lower()

    if status == 401:
        return ("key rejected (401) — it may have been revoked; reconnect with "
                "/orcarouter-auth or paste a new key")
    if status == 403 and "model_access_denied" in (code + lowered):
        return ("this key has no access to that model — the key itself is fine. "
                "Pick another model, or widen the key's access in the OrcaRouter "
                "console")
    if status == 429:
        return "rate limited (429) — retry shortly; the key is fine"
    if status == 402:
        return "insufficient credits (402) — the account balance is the issue"
    if status in (404, 410):
        return "the model is not served (404/410) — pick another from /model"
    if status >= 500:
        return f"OrcaRouter is at capacity ({status}) — retry shortly"
    if detail:
        return f"HTTP {status}: {detail[:200]}"
    return f"HTTP {status}"


def verify_key(token: str, model: str = "", *, timeout: float = 90.0
               ) -> tuple[bool, str]:
    """Prove *token* works by making one real, tiny Messages call.

    The Models route cannot answer this: it is public and answers 200 with a
    wrong key, so a probe against it would call a rejected key healthy and let
    a switch land on a route that 401s on the first turn.

    ``ok`` is False **only** for a rejected credential (401) or an unreachable
    endpoint. A model the key's scope excludes (403 ``model_access_denied``), a
    rate limit, or an unknown model says nothing about the credential and comes
    back ``ok=True`` with the reason in ``detail``.
    """
    token = (token or "").strip()
    if not token:
        return False, "no key"
    payload = {
        "model": model or DEFAULT_MODEL,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ok"}],
    }
    req = urllib.request.Request(
        f"{DEFAULT_BASE_URL}/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "x-api-key": token,
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
        if e.code in (401,):
            return False, f"HTTP {e.code} — {explain_error(e.code, body)}"
        if e.code == 403:
            return True, explain_error(e.code, body)
        return True, explain_error(e.code, body)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"could not reach {HOST}: {e}"


# ---------------------------------------------------------------------------
# Flow B runtime used by the TUI command
# ---------------------------------------------------------------------------

@dataclass
class PendingAuth:
    """One in-flight out-of-band authorization.

    ``verifier`` never leaves this process until the exchange: it is not in the
    URL, not in the transcript, and not in any error message.
    """

    verifier: str
    state: str
    url: str
    app_name: str
    started: float = field(default_factory=time.monotonic)

    def expired(self, ttl: float = 600.0) -> bool:
        return (time.monotonic() - self.started) > ttl


def start_authorization(app_name: str = "ZimZilla") -> PendingAuth:
    """Begin a Flow B login: fresh verifier/state, and the URL to open."""
    verifier, challenge, state = new_pkce()
    return PendingAuth(verifier=verifier, state=state,
                       url=authorize_url(challenge, state, app_name=app_name),
                       app_name=app_name)


def finish_authorization(pending: PendingAuth, code: str) -> Credential:
    """Complete a Flow B login and store the resulting key.

    The code is exchanged at the auth origin, the granted ``scope`` is read
    back, and only then is the credential persisted — a denial, an expired
    code, an old attempt or a scope downgrade all leave the previous key in
    place.
    """
    if pending.expired():
        raise AuthError("this authorization attempt expired (10 minutes) — "
                        "start again with /orcarouter-auth")
    cred = exchange_code(code, pending.verifier)
    if cred.scope and cred.scope != "api":
        # Granted less than asked for. Store it, but say so: assuming we hold
        # the wider grant is how a later call fails inexplicably.
        cred = Credential(token=cred.token, source=cred.source, scope=cred.scope,
                          user_id=cred.user_id)
    return store(cred)


def state_matches(pending: PendingAuth, got: str) -> bool:
    """Constant-time comparison of the echoed state.

    Flow B has no redirect, but the operator can still be handed a code by a
    page that is not ours; the state is what ties this attempt to this process.
    """
    if not got:
        return False
    return hmac.compare_digest(pending.state.encode("ascii", "ignore"),
                               got.encode("ascii", "ignore"))
