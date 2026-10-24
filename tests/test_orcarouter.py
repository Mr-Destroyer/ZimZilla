"""Regression suite for the OrcaRouter Cyber Inference source.

Run:  python tests/test_orcarouter.py   (from an activated venv)

Protects what is particular about this provider, in the order it can go wrong:

* **Two origins, never one.** Consent and code exchange live on
  ``www.orcarouter.ai`` (``/auth``, ``POST /api/v1/auth/keys``); inference and
  the catalog live on ``api.orcarouter.ai``. Deriving either from the other —
  or posting to ``/v1/auth/keys`` — is a 404, so both are asserted by capture.
* **One credential seam, two adapters.** A pasted ``sk-orca-…`` and a PKCE
  exchange must produce the same ``Credential`` shape; nothing downstream reads
  ``source``, which is what keeps the two paths equivalent.
* **PKCE binding is real.** Fresh verifier/challenge/state per attempt,
  ``S256``, unpadded base64url, the verifier never in the URL, and an
  out-of-band callback rather than a loopback redirect a terminal cannot serve.
* **A rejected credential is annotated, not refreshed.** There is no refresh
  grant: a 401 marks *exactly* the generation that made the request, so a late
  failure cannot mark a freshly authorized key broken.
* **The catalog filters on declared capability.** Chat entries must advertise a
  transport this harness speaks, must not be an image/video/rerank/embedding
  route, and image input is fail-closed on declared modalities. Statuses are
  also kept apart: 403 ``model_access_denied`` is a scope answer about a good
  key, not an auth failure.

No test here touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


# Shaped like the real listing: thin ``orcarouter/*`` entries with no
# architecture block, three chat entries that output images, and one entry per
# non-chat endpoint type. Every one of those must be excluded from the text
# picker — and for a different reason, so a single over-broad filter would show
# up as a specific failure.
CATALOG = {
    "data": [
        {"id": "openai/gpt-5.5", "name": "OpenAI: GPT-5.5",
         "architecture": {"input_modalities": ["file", "image", "text"],
                          "output_modalities": ["text"]},
         "supported_endpoint_types": ["openai", "openai-response"],
         "supported_parameters": ["tools", "reasoning"],
         "pricing": {"prompt_per_million": 1.25,
                     "completion_per_million": 10.0}},
        {"id": "anthropic/claude-opus-4.8", "name": "Anthropic: Claude Opus 4.8",
         "context_length": 1000000, "max_completion_tokens": 128000,
         "architecture": {"input_modalities": ["text", "image", "file"],
                          "output_modalities": ["text"]},
         "supported_endpoint_types": ["openai", "anthropic"],
         "supported_parameters": ["tools", "reasoning"],
         "pricing": {"prompt": 0.000005, "completion": 0.000025}},
        {"id": "deepseek/deepseek-v4-pro", "name": "DeepSeek: V4 Pro",
         "context_length": 1048576,
         "architecture": {"input_modalities": ["text"],
                          "output_modalities": ["text"]},
         "supported_endpoint_types": ["openai"],
         "supported_parameters": ["tools"]},
        # Thin gateway entries: no architecture, no pricing. Kept as chat
        # (silence about output modalities must not hide a working model).
        {"id": "orcarouter/auto", "name": "OrcaRouter: Auto",
         "supported_endpoint_types": ["openai", "anthropic", "gemini"]},
        {"id": "orcarouter/fusion", "name": "OrcaRouter: Fusion",
         "output_modalities": None, "supported_endpoint_types": ["openai"]},
        {"id": "orca/orcacyber-ultra-1.0", "name": "OrcaCyber Ultra 1.0",
         "context_length": 1048576,
         "architecture": {"input_modalities": ["text"],
                          "output_modalities": ["text"]},
         "supported_endpoint_types": ["openai"],
         "supported_parameters": ["tools", "reasoning"]},
        # A chat route whose OUTPUT is an image: must never reach a text turn.
        {"id": "google/gemini-3-pro-image", "name": "Gemini 3 Pro Image",
         "architecture": {"input_modalities": ["text", "image"],
                          "output_modalities": ["image", "text"]},
         "supported_endpoint_types": ["openai", "gemini"]},
        # An image generator: declared endpoint type is not a chat transport.
        {"id": "openai/gpt-image-2", "name": "GPT Image 2",
         "architecture": {"input_modalities": ["text"],
                          "output_modalities": ["image"]},
         "supported_endpoint_types": ["image-generation"]},
        {"id": "openai/sora-3", "name": "Sora 3",
         "architecture": {"input_modalities": ["text"],
                          "output_modalities": ["video"]},
         "supported_endpoint_types": ["openai-video"]},
        {"id": "jina/jina-rerank-3", "name": "Jina Rerank 3",
         "supported_endpoint_types": ["jina-rerank"]},
        {"id": "openai/text-embedding-4", "name": "Text Embedding 4",
         "architecture": {"input_modalities": ["text"],
                          "output_modalities": ["embedding"]},
         "supported_endpoint_types": ["embedding"]},
        # No endpoint types at all and no architecture: the catalog cannot say
        # it is chat, but it also does not contradict it.
        {"id": "vendor/mystery-model", "name": "Mystery"},
        # Garbage that must be skipped rather than crash the picker.
        {"name": "no id here"},
        "not a dict",
    ],
}

EXPORTS = (
    'export ANTHROPIC_BASE_URL="https://api.orcarouter.ai"\n'
    'export ANTHROPIC_AUTH_TOKEN="sk-orca-profile0000000000000000"\n'
    'export ANTHROPIC_MODEL="orcarouter/auto"\n'
    'export ANTHROPIC_API_KEY=""\n'
)


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub(payload: dict | None = None, error: Exception | None = None,
          capture: dict | None = None):
    def _open(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["body"] = req.data
            capture["headers"] = {k.lower(): v for k, v in req.headers.items()}
            capture["method"] = req.get_method()
        if error is not None:
            raise error
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())
    return _open


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def test_pkce_is_fresh_and_s256() -> None:
    from zimzilla import orcarouter as o

    verifier, challenge, state = o.new_pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    check("pkce: the challenge is S256 of the verifier", challenge == expected)
    check("pkce: the challenge is unpadded base64url", "=" not in challenge
          and "+" not in challenge and "/" not in challenge)
    check("pkce: the verifier is long enough to be worth binding",
          len(verifier) >= 43, str(len(verifier)))

    seen = {o.new_pkce() for _ in range(24)}
    check("pkce: every attempt gets a distinct verifier/challenge/state",
          len(seen) == 24, str(len(seen)))
    check("pkce: state is a separate value from the verifier",
          o.new_pkce()[2] != verifier)


def test_authorize_url_shape() -> None:
    from zimzilla import orcarouter as o

    verifier, challenge, state = o.new_pkce()
    url = o.authorize_url(challenge, state)
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)

    check("auth url: consent lives on the auth origin",
          parsed.netloc == "www.orcarouter.ai", parsed.netloc)
    check("auth url: consent path is /auth", parsed.path == "/auth", parsed.path)
    check("auth url: out-of-band callback is asked for explicitly",
          params.get("callback_url") == ["oob"], str(params.get("callback_url")))
    check("auth url: S256 is mandatory, not preferred",
          params.get("code_challenge_method") == ["S256"])
    check("auth url: the challenge travels, not the verifier",
          params.get("code_challenge") == [challenge]
          and verifier not in url)
    check("auth url: the state travels", params.get("state") == [state])
    check("auth url: the app is named", params.get("app_name") == ["ZimZilla"])
    check("auth url: no client secret anywhere in it",
          "client_secret" not in url)
    check("auth url: no inference origin is used for consent",
          "api.orcarouter.ai" not in url)


def test_exchange_hits_auth_origin_with_right_body() -> None:
    from zimzilla import orcarouter as o

    seen: dict = {}
    payload = {"key": "sk-orca-issued000000000000000", "scope": "api",
               "user_id": "u_123"}
    with mock.patch("urllib.request.urlopen", _stub(payload, capture=seen)):
        cred = o.exchange_code("the-code", "the-verifier")

    check("exchange: POSTs the auth origin, not the API origin",
          seen["url"] == "https://www.orcarouter.ai/api/v1/auth/keys", seen["url"])
    check("exchange: it is a POST", seen["method"] == "POST", seen["method"])
    body = json.loads((seen["body"] or b"{}").decode())
    check("exchange: the code is sent", body.get("code") == "the-code")
    check("exchange: the verifier is sent to the exchange, not the browser",
          body.get("code_verifier") == "the-verifier")
    check("exchange: the method is stated again as S256",
          body.get("code_challenge_method") == "S256")
    check("exchange: the response yields a credential",
          cred.token == "sk-orca-issued000000000000000" and cred.source == "pkce")
    check("exchange: the granted scope is read back", cred.scope == "api")


def test_exchange_failures_are_classified() -> None:
    from zimzilla import orcarouter as o

    def raises(code: int):
        return _stub(error=urllib.error.HTTPError(
            "u", code, "x", {}, io.BytesIO(b'{"error":"nope"}')))

    for status, needle in ((403, "refused"), (400, "malformed"),
                           (429, "10 PKCE keys")):
        with mock.patch("urllib.request.urlopen", raises(status)):
            try:
                o.exchange_code("c", "v")
                check(f"exchange {status}: raises", False)
            except o.AuthError as e:
                check(f"exchange {status}: raises AuthError", True)
                check(f"exchange {status}: carries the status", e.status == status)
                check(f"exchange {status}: explains the cause", needle in str(e),
                      str(e)[:80])
                check(f"exchange {status}: never echoes the verifier",
                      "v" != str(e).strip() and "code_verifier" not in str(e))

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("no route"))):
        try:
            o.exchange_code("c", "v")
            check("exchange network: raises", False)
        except o.AuthError as e:
            check("exchange network: says the auth origin was unreachable",
                  "could not reach" in str(e), str(e)[:80])

    for payload, needle in (({}, "empty key"), ({"key": ""}, "empty key")):
        with mock.patch("urllib.request.urlopen", _stub(payload)):
            try:
                o.exchange_code("c", "v")
                check(f"exchange malformed {payload}: raises", False)
            except o.AuthError as e:
                check(f"exchange malformed {payload}: refuses an empty key",
                      needle in str(e), str(e)[:80])


def test_two_adapters_one_shape() -> None:
    from zimzilla import orcarouter as o

    key = o.credential_from_api_key("  sk-orca-pasted0000000000000000  ")
    check("adapters: the api-key adapter trims and tags its source",
          key.token == "sk-orca-pasted0000000000000000" and key.source == "api_key")

    with mock.patch("urllib.request.urlopen",
                    _stub({"key": "sk-orca-issued000000000000000",
                           "scope": "api", "user_id": "u_1"})):
        pkce = o.credential_from_pkce("c", "v")
    check("adapters: the PKCE adapter produces the same type",
          isinstance(pkce, type(key)))
    check("adapters: both carry a token and a scope",
          bool(key.token) and bool(pkce.token) and bool(pkce.scope))
    check("adapters: only the provenance differs",
          {f for f in key.__dataclass_fields__ if getattr(key, f) != getattr(pkce, f)}
          == {"token", "source", "user_id"} | (
              {"scope"} if key.scope != pkce.scope else set()),
          str([f for f in key.__dataclass_fields__
               if getattr(key, f) != getattr(pkce, f)]))


def test_finish_authorization_is_terminal() -> None:
    from zimzilla import orcarouter as o

    pending = o.start_authorization()
    check("flow b: pending carries no verifier in the URL",
          pending.verifier not in pending.url)
    check("flow b: state matches itself", o.state_matches(pending, pending.state))
    check("flow b: a different state does not match",
          not o.state_matches(pending, "other"))
    check("flow b: an empty state does not match",
          not o.state_matches(pending, ""))

    pending.started -= 601
    try:
        o.finish_authorization(pending, "code")
        check("flow b: an expired attempt is refused", False)
    except o.AuthError as e:
        check("flow b: an expired attempt is refused before any request",
              "expired" in str(e), str(e)[:80])

    fresh = o.start_authorization()
    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.HTTPError(
                        "u", 403, "x", {}, io.BytesIO(b"{}")))):
        try:
            o.finish_authorization(fresh, "code")
            check("flow b: a refused code raises", False)
        except o.AuthError:
            check("flow b: a refused code raises", True)


def test_failed_login_keeps_the_old_key(tmp: Path) -> None:
    """A denial must not delete the operator's only working credential."""
    from zimzilla import orcarouter as o

    o.store(o.credential_from_api_key("sk-orca-good00000000000000000"))
    before = o.PROFILE.read_text()

    pending = o.start_authorization()
    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.HTTPError(
                        "u", 403, "x", {}, io.BytesIO(b"{}")))):
        try:
            o.finish_authorization(pending, "code")
        except o.AuthError:
            pass
    check("lifecycle: a failed login leaves the previous profile intact",
          o.PROFILE.read_text() == before)
    check("lifecycle: the old key still loads",
          (o.load_credentials() or ("",))[0] == "sk-orca-good00000000000000000")


# ---------------------------------------------------------------------------
# credential lifecycle
# ---------------------------------------------------------------------------

def test_generation_safe_reauth() -> None:
    """A late 401 must not mark a newer credential broken."""
    from zimzilla import orcarouter as o

    first = o.store(o.credential_from_api_key("sk-orca-one000000000000000000"))
    check("state: store bumps the generation", first.generation >= 1)
    check("state: a fresh store is not marked", not o.needs_reauth())

    o.mark_needs_reauth(first.generation)
    check("state: the live generation is marked", o.needs_reauth())

    second = o.store(o.credential_from_api_key("sk-orca-two000000000000000000"))
    check("state: storing a new credential clears the flag",
          not o.needs_reauth())
    check("state: the generation advanced", second.generation > first.generation)

    o.mark_needs_reauth(first.generation)
    check("state: a late failure for the OLD generation is ignored",
          not o.needs_reauth(), str(o._read_state()))

    o.mark_needs_reauth(second.generation)
    check("state: the current generation can still be marked", o.needs_reauth())
    o.mark_needs_reauth()
    check("state: a call with no generation marks the live one",
          o.needs_reauth())

    o.clear_reauth(second.generation - 1)
    check("state: clearing a stale generation is ignored", o.needs_reauth())
    o.clear_reauth(second.generation)
    check("state: clearing the live generation works", not o.needs_reauth())


def test_profile_roundtrip_and_permissions() -> None:
    from zimzilla import orcarouter as o

    cred = o.store(o.credential_from_api_key("sk-orca-saved00000000000000000"))
    check("profile: written where load_credentials looks",
          Path(cred.origin) == o.PROFILE)
    check("profile: mode 600", (o.PROFILE.stat().st_mode & 0o777) == 0o600,
          oct(o.PROFILE.stat().st_mode & 0o777))
    check("profile: directory mode 700",
          (o.PROFILE.parent.stat().st_mode & 0o777) == 0o700,
          oct(o.PROFILE.parent.stat().st_mode & 0o777))
    check("profile: state file mode 600",
          (o.STATE.stat().st_mode & 0o777) == 0o600,
          oct(o.STATE.stat().st_mode & 0o777))

    exports = o._read_exports(o.PROFILE)
    check("profile: the token round-trips",
          exports.get("ANTHROPIC_AUTH_TOKEN") == "sk-orca-saved00000000000000000")
    check("profile: the endpoint is the inference origin, no /v1 suffix",
          exports.get("ANTHROPIC_BASE_URL") == "https://api.orcarouter.ai",
          exports.get("ANTHROPIC_BASE_URL"))
    check("profile: a model is always named", bool(exports.get("ANTHROPIC_MODEL")))
    check("profile: no api_key is written beside the token",
          exports.get("ANTHROPIC_API_KEY") == "")

    creds = o.load_credentials()
    check("profile: load_credentials returns (key, model, origin)",
          creds is not None and creds[0].startswith("sk-orca-"))
    check("profile: the origin is a path, never the key",
          "sk-orca-" not in creds[2], creds[2])

    o.clear()
    check("clear: the profile is gone", not o.PROFILE.exists())
    check("clear: the state is gone", not o.STATE.exists())
    check("clear: a cleared provider still reports its default model",
          o.profile_model() == o.DEFAULT_MODEL)


def test_store_preserves_the_chosen_model() -> None:
    from zimzilla import orcarouter as o

    o.store(o.credential_from_api_key("sk-orca-one000000000000000000"))
    o._rewrite_model("orca/orcacyber-ultra-1.0")
    check("profile: the model is rewritten in place",
          o.profile_model() == "orca/orcacyber-ultra-1.0", o.profile_model())
    check("profile: rewriting the model keeps the key",
          (o.load_credentials() or ("",))[0] == "sk-orca-one000000000000000000")

    cred = o.store(o.credential_from_api_key("sk-orca-two000000000000000000"))
    check("profile: a new key keeps the previously chosen model",
          o.profile_model() == "orca/orcacyber-ultra-1.0", o.profile_model())
    check("profile: the new key replaced the old one",
          (o.load_credentials() or ("",))[0] == cred.token)


def test_credential_precedence(tmp: Path) -> None:
    """Environment beats the profile; the profile beats ~/claude-source."""
    from zimzilla import orcarouter as o

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        o.clear()
        claude_source = tmp / "claude-source"
        claude_source.mkdir(parents=True, exist_ok=True)
        (claude_source / "orcarouter").write_text(EXPORTS)
        with mock.patch.object(o, "CLAUDE_SOURCE", claude_source):
            creds = o.load_credentials()
            check("creds: falls back to ~/claude-source",
                  creds is not None and creds[2].endswith("orcarouter"),
                  str(creds and creds[2]))
            check("creds: the model comes from that profile",
                  creds is not None and creds[1] == "orcarouter/auto")

            o.store(o.credential_from_api_key("sk-orca-own0000000000000000000"))
            creds = o.load_credentials()
            check("creds: ZimZilla's own profile beats ~/claude-source",
                  creds is not None and creds[2] == str(o.PROFILE),
                  str(creds and creds[2]))

            os.environ["ANTHROPIC_BASE_URL"] = o.DEFAULT_BASE_URL
            os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-orca-env0000000000000000000"
            os.environ["ANTHROPIC_MODEL"] = "orca/orcacyber-zero-1.5"
            creds = o.load_credentials()
            check("creds: the environment beats every file",
                  creds is not None and creds[2] == "environment"
                  and creds[0] == "sk-orca-env0000000000000000000",
                  str(creds and creds[2]))

            os.environ["ANTHROPIC_BASE_URL"] = "https://openrouter.ai/api"
            creds = o.load_credentials()
            check("creds: an env key for another gateway is ignored",
                  creds is not None and creds[2] == str(o.PROFILE),
                  str(creds and creds[2]))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_looks_like_key() -> None:
    from zimzilla import orcarouter as o

    check("key: a real-looking sk-orca key passes",
          o.looks_like_key("sk-orca-0000000000000000000000"))
    check("key: a short string is rejected", not o.looks_like_key("abc"))
    check("key: a pasted export line is rejected",
          not o.looks_like_key('export ANTHROPIC_AUTH_TOKEN="sk-orca-xxx"'))
    check("key: whitespace inside is rejected",
          not o.looks_like_key("sk-orca-aaaaaaaaaaaaaaaa bbbb"))


def test_apply_and_guard() -> None:
    from zimzilla import orcarouter as o

    class _Cfg:
        base_url = "http://localhost:4001"
        auth_token = "leftover"
        api_key = "sk-leftover"
        model = "deepseek-v4.1-flash"
        protocol = "anthropic"

    with mock.patch.object(o, "load_credentials",
                           lambda: ("sk-orca-x000000000000000000000",
                                    "orca/orcacyber-ultra-1.0", "test")):
        cfg = _Cfg()
        applied = o.apply_to(cfg)
        check("apply: returns the origin and model",
              applied == ("test", "orca/orcacyber-ultra-1.0"), str(applied))
        check("apply: the endpoint is OrcaRouter", cfg.base_url == o.DEFAULT_BASE_URL)
        check("apply: the token is set", cfg.auth_token.startswith("sk-orca-"))
        check("apply: the stale api_key is cleared", cfg.api_key == "")
        check("apply: the model is applied", cfg.model == "orca/orcacyber-ultra-1.0")
        check("apply: the protocol is left for the caller to re-resolve",
              cfg.protocol == "anthropic",
              "apply_to only touches endpoint/token/model; the UI resets "
              "protocol to auto")

    with mock.patch.object(o, "load_credentials", lambda: None):
        cfg = _Cfg()
        check("apply: no key leaves the config alone",
              o.apply_to(cfg) is None and cfg.base_url == "http://localhost:4001")

    check("guard: the default local proxy is overridable",
          o.applies_to_default_endpoint("http://localhost:4001"))
    check("guard: OrcaRouter itself is NOT overridden",
          not o.applies_to_default_endpoint(o.DEFAULT_BASE_URL))
    check("guard: another host is NOT overridden",
          not o.applies_to_default_endpoint("https://api.example.com"))


# ---------------------------------------------------------------------------
# provider registration and protocol
# ---------------------------------------------------------------------------

def test_source_registration() -> None:
    from zimzilla import sources as s

    check("sources: orcarouter is a builtin (survives a restart)",
          "orcarouter" in s.BUILTIN_KEYS)
    check("sources: it is offered by switchable() once a key exists",
          "orcarouter" in s.switchable())
    check("sources: the gateway module behind it is its own",
          s._gateway_module("orcarouter") is __import__(
              "zimzilla.orcarouter", fromlist=["orcarouter"]))
    check("sources: the inference host resolves to this source",
          s.active_key("https://api.orcarouter.ai") == "orcarouter")
    check("sources: a /v1 suffix resolves to the same source",
          s.active_key("https://api.orcarouter.ai/v1") == "orcarouter")
    check("sources: with a trailing slash too",
          s.active_key("https://api.orcarouter.ai/") == "orcarouter")
    check("sources: openrouter is not orcarouter",
          s.active_key("https://openrouter.ai/api") == "openrouter")
    check("sources: orcarouter is not openrouter",
          s.active_key("https://api.orcarouter.ai") != "openrouter")
    check("sources: a cold catalog still offers the verified seed",
          tuple(s.models_for("https://api.orcarouter.ai") or ())[:1]
          == ("openai/gpt-5.5",),
          str(tuple(s.models_for("https://api.orcarouter.ai") or ())[:1]))
    check("sources: the seed names the gateway router as the default",
          "orcarouter/auto" in (s.models_for("https://api.orcarouter.ai") or ()),
          str(s.models_for("https://api.orcarouter.ai")))
    check("sources: the seed carries the cyber tier",
          {"orca/orcacyber-ultra-1.0", "orca/orcacyber-zero-1.5"}
          <= set(s.models_for("https://api.orcarouter.ai") or ()))


def test_protocol_resolution() -> None:
    from zimzilla import openai_compat as oc

    check("protocol: the base URL resolves to the Messages protocol",
          oc.resolve_protocol("https://api.orcarouter.ai") == "anthropic",
          oc.resolve_protocol("https://api.orcarouter.ai"))
    # Only the bare base is used as a profile endpoint, and that is the case
    # that must not be guessed wrong. A ``/v1`` URL is the universal OpenAI
    # convention and resolves to Chat Completions — which OrcaRouter also
    # serves, so pinning it by hand is a valid choice rather than a trap.
    check("protocol: an explicit /v1 base resolves to Chat Completions",
          oc.resolve_protocol("https://api.orcarouter.ai/v1") == "openai",
          oc.resolve_protocol("https://api.orcarouter.ai/v1"))
    check("protocol: an explicit override can pin either protocol",
          oc.resolve_protocol("https://api.orcarouter.ai", "openai") == "openai"
          and oc.resolve_protocol("https://api.orcarouter.ai/v1",
                                  "anthropic") == "anthropic")

    import anthropic
    from anthropic._models import FinalRequestOptions

    from zimzilla.agent import Agent
    from zimzilla.config import Config
    from zimzilla import orcarouter as o

    cfg = Config(base_url=o.DEFAULT_BASE_URL, auth_token="sk-orca-x0000000000000000")
    agent = Agent(cfg)
    opts = FinalRequestOptions(method="post", url="/v1/messages",
                               json_data={}, headers={})
    composed = str(agent.client._build_request(opts).url)
    check("protocol: the message request lands on /v1/messages",
          composed == "https://api.orcarouter.ai/v1/messages", composed)
    check("protocol: there is no doubled /v1", "/v1/v1/" not in composed, composed)

    seen: dict = {}
    o.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub(capture=seen)):
        o.fetch_models(use_cache=False)
    check("protocol: the catalog lands on /v1/models",
          seen.get("url", "").startswith("https://api.orcarouter.ai/v1/models"),
          seen.get("url"))
    check("protocol: the catalog asks for the chat capability",
          "capability=chat" in seen.get("url", ""), seen.get("url"))
    check("protocol: the catalog is public, so no credential is sent",
          "authorization" not in seen.get("headers", {})
          and "x-api-key" not in seen.get("headers", {}),
          str(sorted(seen.get("headers", {}))))


# ---------------------------------------------------------------------------
# catalog filtering
# ---------------------------------------------------------------------------

def test_chat_filter_is_capability_based() -> None:
    from zimzilla import orcarouter as o

    models = o._parse_models(CATALOG)
    by_id = {m.id: m for m in models}
    check("catalog: malformed rows are skipped, not fatal",
          len(models) == 12, str(len(models)))
    check("catalog: the thin gateway entry is still parseable",
          by_id["orcarouter/auto"].modalities is None)
    check("catalog: a null output-modality list is kept as None",
          by_id["orcarouter/fusion"].outputs is None
          or by_id["orcarouter/fusion"].outputs == (),
          str(by_id["orcarouter/fusion"].outputs))

    check("chat: a normal text model is chat", by_id["openai/gpt-5.5"].chat)
    check("chat: an anthropic-endpoint model is chat",
          by_id["anthropic/claude-opus-4.8"].chat)
    check("chat: a thin gateway entry with no architecture is chat",
          by_id["orcarouter/auto"].chat)
    check("chat: the mystery entry with no declared types is kept",
          by_id["vendor/mystery-model"].chat)
    check("chat: an image-generating model is excluded",
          not by_id["openai/gpt-image-2"].chat)
    check("chat: a video model is excluded", not by_id["openai/sora-3"].chat)
    check("chat: a rerank model is excluded",
          not by_id["jina/jina-rerank-3"].chat)
    check("chat: an embedding model is excluded",
          not by_id["openai/text-embedding-4"].chat)
    check("chat: an image-OUTPUT chat route is excluded from a text turn",
          not by_id["google/gemini-3-pro-image"].chat,
          str(by_id["google/gemini-3-pro-image"].outputs))

    check("catalog: prices arrive per-million, directly",
          by_id["openai/gpt-5.5"].price_in == 1.25,
          str(by_id["openai/gpt-5.5"].price_in))
    check("catalog: a per-token price is scaled to per-million",
          round(by_id["anthropic/claude-opus-4.8"].price_in or 0, 6) == 5.0,
          str(by_id["anthropic/claude-opus-4.8"].price_in))
    check("catalog: an unpriced entry stays unpriced (not free)",
          by_id["orcarouter/auto"].price_in is None
          and not by_id["orcarouter/auto"].free)
    check("catalog: max output is read",
          by_id["anthropic/claude-opus-4.8"].max_output == 128000)


def test_capability_helpers() -> None:
    from zimzilla import orcarouter as o

    o.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub()):
        ids, err = o.model_ids(use_cache=False)
    check("picker: chat ids exclude every non-chat route",
          ids is not None
          and ids == ["openai/gpt-5.5", "anthropic/claude-opus-4.8",
                      "deepseek/deepseek-v4-pro", "orcarouter/auto",
                      "orcarouter/fusion", "orca/orcacyber-ultra-1.0",
                      "vendor/mystery-model"],
          str(ids))
    check("picker: no error on a good catalog", err == "")

    o.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub()):
        multi, _ = o.multimodal_models(modality="image", use_cache=False)
    check("multimodal: only chat entries that DECLARE image input",
          multi is not None
          and [m.id for m in multi] == ["openai/gpt-5.5",
                                        "anthropic/claude-opus-4.8"],
          str([m.id for m in (multi or [])]))
    check("multimodal: the image-output model is not offered for image input",
          all(m.id != "google/gemini-3-pro-image" for m in (multi or [])))
    check("multimodal: an entry with no declared modalities fails closed",
          all(m.modalities for m in (multi or [])))

    o.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub()):
        text, _ = o.multimodal_models(modality="text", use_cache=False)
    check("multimodal: declaring text input is not the same as declaring image",
          text is not None and "orcarouter/auto" not in [m.id for m in text],
          str([m.id for m in (text or [])])[:80])

    for fn, expected in ((o.embedding_models, ["openai/text-embedding-4"]),
                         (o.image_models, ["openai/gpt-image-2"]),
                         (o.video_models, ["openai/sora-3"]),
                         (o.rerank_models, ["jina/jina-rerank-3"])):
        o.clear_cache()
        with mock.patch("urllib.request.urlopen", _stub()):
            got, _ = fn(use_cache=False)
        check(f"endpoints: {fn.__name__} matches only its own endpoint type",
              [m.id for m in (got or [])] == expected, str([m.id for m in (got or [])]))


def test_catalog_failure_is_total_and_seed_is_offered() -> None:
    from zimzilla import orcarouter as o, sources as s

    o.clear_cache()
    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("no route"))):
        models, err = o.fetch_models(use_cache=False)
    check("fetch: a network failure returns None and a message",
          models is None and "could not reach" in err, err)
    check("fetch: the failure names the provider", "orcarouter" in err, err)

    http = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    with mock.patch("urllib.request.urlopen", _stub(error=http)):
        models, err = o.fetch_models("sk-orca-bad000000000000000000",
                                     use_cache=False)
    check("fetch: a rejected key says so", models is None and "401" in err, err)
    check("fetch: the rejection names both setup commands",
          "orcarouter-api-setup" in err and "orcarouter-auth" in err, err)

    o.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub({"data": []})):
        models, err = o.fetch_models(use_cache=False)
    check("fetch: an empty catalog is [], not an error",
          models == [] and err == "", repr((models, err)))

    check("seed: a verified seed covers the cold start",
          len(o.seed_ids()) == 8, str(len(o.seed_ids())))
    check("seed: the seed contains no non-branded default",
          all(i.startswith(("orcarouter/", "orca/")) or "/" in i
              for i in o.seed_ids()))
    check("seed: the gateway router leads as the default model",
          o.DEFAULT_MODEL == "orcarouter/auto")
    check("seed: the cyber tier is present",
          "orca/orcacyber-ultra-1.0" in o.seed_ids()
          and "orca/orcacyber-zero-1.5" in o.seed_ids())
    check("seed: sources falls back to it when discovery failed",
          tuple(s.models_for("https://api.orcarouter.ai") or ())
          == o.seed_ids())


def test_cache_hygiene() -> None:
    import time

    from zimzilla import orcarouter as o

    o.clear_cache()
    check("cache: cold cache has no age", o.catalog_age() is None)
    check("cache: cold cache reads as None", o.cached_models() is None)

    with mock.patch("urllib.request.urlopen", _stub()):
        o.fetch_models(use_cache=False)
    check("cache: a fresh catalog is served", o.cached_ids() is not None)
    check("cache: it is already filtered to chat",
          "openai/gpt-image-2" not in (o.cached_ids() or []))
    check("cache: its age is reported and small",
          (o.catalog_age() or 999) < 5.0, str(o.catalog_age()))

    models, _ = o._CACHE[""]
    o._CACHE[""] = (models, time.monotonic() - (o.CACHE_TTL + 60))
    check("cache: an expired catalog reads as None", o.cached_models() is None)
    check("cache: cached_ids agrees", o.cached_ids() is None)
    check("cache: the real age is still reported",
          (o.catalog_age() or 0) >= o.CACHE_TTL, str(o.catalog_age()))

    o.clear_cache()
    calls = []

    def counting(req, timeout=None):
        calls.append(req)
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", counting):
        o.fetch_models(use_cache=False)
        o.fetch_models("sk-orca-key000000000000000000", use_cache=False)
    check("cache: a keyed fetch does not reuse another account's list",
          len(calls) == 2, str(len(calls)))
    with mock.patch("urllib.request.urlopen", counting):
        o.fetch_models("sk-orca-key000000000000000000")
    check("cache: a repeat fetch is served from cache", len(calls) == 2)
    o.clear_cache()


# ---------------------------------------------------------------------------
# errors and verification
# ---------------------------------------------------------------------------

def test_error_classification() -> None:
    from zimzilla import orcarouter as o

    check("errors: a 401 sends the operator to re-authenticate",
          "orcarouter-auth" in o.explain_error(401, ""))
    scope = o.explain_error(
        403, '{"error":{"code":"model_access_denied","message":"no access"}}')
    check("errors: 403 model_access_denied says the key is fine",
          "key itself is fine" in scope and "model" in scope, scope)
    check("errors: 403 model_access_denied does NOT say re-authenticate",
          "orcarouter-auth" not in scope, scope)
    check("errors: 403 for another reason is still not a re-auth",
          "orcarouter-auth" not in o.explain_error(403, '{"error":"forbidden"}'))
    check("errors: 429 says the key is fine",
          "key is fine" in o.explain_error(429, ""))
    check("errors: 402 names the balance",
          "credits" in o.explain_error(402, ""))
    check("errors: 5xx says retry",
          "retry" in o.explain_error(503, "").lower())
    check("errors: an unknown status degrades to the status",
          o.explain_error(418, "") == "HTTP 418", o.explain_error(418, ""))


def test_verify_key_only_fails_on_the_credential() -> None:
    from zimzilla import orcarouter as o

    seen: dict = {}
    with mock.patch("urllib.request.urlopen", _stub({"ok": True}, capture=seen)):
        ok, detail = o.verify_key("sk-orca-good00000000000000000",
                                  "orca/orcacyber-ultra-1.0")
    check("verify: a live key is accepted", ok and "accepted" in detail, detail)
    check("verify: it probes the Messages route, not the public catalog",
          seen["url"] == "https://api.orcarouter.ai/v1/messages", seen["url"])
    check("verify: it is a POST", seen["method"] == "POST")
    check("verify: it sends a real credential",
          seen["headers"].get("authorization", "").startswith("Bearer sk-orca-"))
    check("verify: the probe is tiny",
          json.loads(seen["body"]).get("max_tokens") == 8)

    def raises(code: int):
        return _stub(error=urllib.error.HTTPError(
            "u", code, "x", {}, io.BytesIO(
                b'{"error":{"code":"model_access_denied","message":"no"}}')))

    with mock.patch("urllib.request.urlopen", raises(403)):
        ok, detail = o.verify_key("sk-orca-good00000000000000000", "orca/x")
    check("verify: 403 is NOT a credential failure", ok, detail)
    check("verify: 403 explains that the key is fine", "fine" in detail, detail)

    with mock.patch("urllib.request.urlopen", raises(401)):
        ok, detail = o.verify_key("sk-orca-revoked000000000000000", "orca/x")
    check("verify: 401 IS a credential failure", not ok, detail)
    check("verify: 401 names a way to reconnect",
          "orcarouter-auth" in detail or "orcarouter-api-setup" in detail, detail)

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("no route"))):
        ok, detail = o.verify_key("sk-orca-x0000000000000000000")
    check("verify: an unreachable endpoint is a failure, not a pass",
          not ok and "could not reach" in detail, detail)
    check("verify: no key is refused without a request",
          o.verify_key("")[0] is False)


def test_auth_origin_overrides_are_separate() -> None:
    from zimzilla import orcarouter as o

    saved = {k: os.environ.get(k) for k in
             ("ORCA_BASE_URL", "ORCA_AUTH_BASE_URL", "ORCA_API_BASE_URL")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        check("origins: the defaults are the two real origins",
              o.auth_base() == "https://www.orcarouter.ai"
              and o.api_base() == "https://api.orcarouter.ai")

        os.environ["ORCA_BASE_URL"] = "https://relay.example.com/"
        check("origins: the shared base feeds both",
              o.auth_base() == "https://relay.example.com"
              and o.api_base() == "https://relay.example.com")
        check("origins: a trailing slash is stripped",
              not o.auth_base().endswith("/"))

        os.environ["ORCA_AUTH_BASE_URL"] = "https://login.example.com"
        os.environ["ORCA_API_BASE_URL"] = "https://infer.example.com"
        check("origins: explicit overrides win and stay separate",
              o.auth_base() == "https://login.example.com"
              and o.api_base() == "https://infer.example.com")

        os.environ["ORCA_AUTH_BASE_URL"] = "http://relay.example.com"
        try:
            o.auth_base()
            check("origins: plain HTTP off loopback is refused", False)
        except ValueError:
            check("origins: plain HTTP off loopback is refused", True)
        os.environ["ORCA_AUTH_BASE_URL"] = "http://localhost:8080"
        check("origins: loopback HTTP is allowed for local testing",
              o.auth_base() == "http://localhost:8080")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_no_secret_leaks_into_messages() -> None:
    """Errors, blurbs and the state file must not carry the key."""
    from zimzilla import orcarouter as o

    secret = "sk-orca-verysecret0000000000000"
    o.store(o.credential_from_api_key(secret))
    body = o.STATE.read_text()
    check("privacy: the state file holds no key", secret not in body, body[:80])
    check("privacy: the state file records provenance, not the secret",
          "pkce" in body or "api_key" in body)

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("no route"))):
        _, err = o.fetch_models(secret, use_cache=False)
    check("privacy: an error message does not echo the key", secret not in err, err)
    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.HTTPError(
                        "u", 403, "x", {}, io.BytesIO(b"{}")))):
        try:
            o.exchange_code("c", "verifier-secret")
        except o.AuthError as e:
            check("privacy: an auth error does not echo the verifier",
                  "verifier-secret" not in str(e), str(e)[:80])
    check("privacy: no module-level constant holds a real key",
          all("sk-orca-" not in v for name, v in vars(o).items()
              if isinstance(v, str) and name not in ("__doc__", "KEY_PREFIX")))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-orca-test-"))
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    # The harness may itself be running against an OrcaRouter endpoint; an
    # ambient ANTHROPIC_BASE_URL would outrank every file these tests write.
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                 "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        os.environ.pop(name, None)
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    test_pkce_is_fresh_and_s256()
    test_authorize_url_shape()
    test_exchange_hits_auth_origin_with_right_body()
    test_exchange_failures_are_classified()
    test_two_adapters_one_shape()
    test_finish_authorization_is_terminal()
    test_failed_login_keeps_the_old_key(tmp)
    test_generation_safe_reauth()
    test_profile_roundtrip_and_permissions()
    test_store_preserves_the_chosen_model()
    test_credential_precedence(tmp)
    test_looks_like_key()
    test_apply_and_guard()
    test_source_registration()
    test_protocol_resolution()
    test_chat_filter_is_capability_based()
    test_capability_helpers()
    test_catalog_failure_is_total_and_seed_is_offered()
    test_cache_hygiene()
    test_error_classification()
    test_verify_key_only_fails_on_the_credential()
    test_auth_origin_overrides_are_separate()
    test_no_secret_leaks_into_messages()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed — FAILED: "
              + ", ".join(failed))
        return 1
    print(f"{len(RESULTS)}/{len(RESULTS)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
