"""Regressions for three bugs found by pointing the harness at BeatAPI live.

Run:  python tests/test_gateway_auth.py   (from an activated venv)

None of these three were caught by the existing suite, and all three were
shipping-blocking in the same way: the operator was told something false about
their own tooling. They are grouped here because they share that shape — each is
a case where the harness had all the information needed to be right and said the
wrong thing anyway.

1. **A bearer token and a placeholder key were sent together.** The client was
   built with ``api_key=cfg.api_key or "placeholder"``, so a session pointed at a
   gateway with an ``auth_token`` sent BOTH ``Authorization: Bearer <token>`` and
   ``x-api-key: placeholder``. BeatAPI validates ``x-api-key`` and rejects the
   request 401 before it ever looks at the bearer — so a perfectly good key
   reported as "authentication failed".

2. **A free model was billed at the paid model's rate.** ``price_for`` tolerates
   prefixes so dated and provider-qualified names resolve. BeatAPI's giveaway is
   ``deepseek-v4.1-flash-free``, which starts with the paid ``deepseek-v4.1-flash``
   row, so the free model was charged $0.28/Mtok — and an *unknown* free model
   fell through to ``DEFAULT_PRICE`` at $1/$3. The operator saw a cost line for a
   model they were told costs nothing.

3. **A zero balance was reported as an auth failure.** BeatAPI answers a paid
   model on an un-topped-up account with 402 ``insufficient_credits``. That is a
   billing state with one obvious fix; it rendered as a generic error string, and
   before fix 1 it was indistinguishable from a bad key.

4. **A gated model and a throttled free tier both read as raw SDK dicts.**
   OpenRouter reserves some ``:free`` models for an allowlist of apps (403
   ``permission_denied``, "only available on agentic harnesses") and throttles
   others per model (429). Neither is an auth failure and neither is fixable from
   here, but both have a true, useful answer — the key is fine, pick another
   model — and both were instead printed as ``{'type': 'error', ...}``.

No network: every case is constructed locally.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
# The SDK pins its HTTP client under the name ``httpx2`` (see
# ``anthropic._base_client``), so this is the same class the SDK will hand back
# in ``APIStatusError.response`` — not a stand-in that merely resembles it.
import httpx2  # noqa: E402

from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import (  # noqa: E402
    DEFAULT_PRICE, PRICING, Config, price_for,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(**kw) -> Config:
    """A Config with ONLY the credentials passed in.

    The environment must already be cleared — see ``main``. Both ``Config`` and
    the SDK itself read ``ANTHROPIC_AUTH_TOKEN`` / ``ANTHROPIC_API_KEY`` from it,
    and on an operator's box those are set for real, so without clearing them
    every case here would be asserting about the shell rather than the code.
    """
    return Config.from_env(workdir=Path("."),
                           base_url="https://api.beatapi.io", **kw)


def _auth_headers(cfg: Config) -> dict[str, str]:
    """The auth headers the SDK will actually send for *cfg*.

    Read off the built client rather than from a request, so the assertion is
    about what the harness configures and not about a mocked transport.
    """
    agent = Agent(cfg)
    return {k.lower(): v for k, v in agent.client.auth_headers.items()}


# --- 1. one credential, not two -------------------------------------------

def test_a_bearer_token_is_sent_alone() -> None:
    """The bug: a bearer token plus ``x-api-key: placeholder`` → 401."""
    hdrs = _auth_headers(_cfg(auth_token="sk-live-token", api_key=""))
    check("auth: the bearer token is sent", hdrs.get("authorization") == "Bearer sk-live-token",
          str(hdrs))
    check("auth: no x-api-key is sent alongside it",
          "x-api-key" not in hdrs, str(hdrs))


def test_a_stale_api_key_is_not_forwarded_with_a_token() -> None:
    """A key left over from another provider is a credential for the wrong service."""
    hdrs = _auth_headers(_cfg(auth_token="sk-live-token", api_key="sk-other-provider"))
    check("auth: the bearer token still wins",
          hdrs.get("authorization") == "Bearer sk-live-token", str(hdrs))
    check("auth: the stale api_key is NOT forwarded",
          "x-api-key" not in hdrs, str(hdrs))


def test_an_api_key_still_works_on_its_own() -> None:
    """The ordinary path must be untouched by any of this."""
    hdrs = _auth_headers(_cfg(api_key="sk-plain-key"))
    check("auth: a plain api_key is still sent",
          hdrs.get("x-api-key") == "sk-plain-key", str(hdrs))
    check("auth: no bearer token is invented",
          "authorization" not in hdrs, str(hdrs))


def test_no_credential_sends_no_auth_header() -> None:
    """Neither credential set: the SDK must still construct, with no auth at all.

    The placeholder existed only to give the SDK a non-empty string. ``None``
    constructs just as well and puts nothing on the wire — which is what lets an
    unauthenticated local proxy work.
    """
    hdrs = _auth_headers(_cfg(api_key="", auth_token=""))
    check("auth: no authorization header", "authorization" not in hdrs, str(hdrs))
    check("auth: no x-api-key header", "x-api-key" not in hdrs, str(hdrs))


# --- 2. a free model costs nothing -----------------------------------------

def test_a_free_suffix_is_priced_at_zero() -> None:
    """The bug: prefix tolerance billed ``…-free`` at the paid row's rate."""
    check("price: BeatAPI's -free suffix is free", price_for("deepseek-v4.1-flash-free") == (0.0, 0.0),
          str(price_for("deepseek-v4.1-flash-free")))
    check("price: OpenRouter's :free suffix is free",
          price_for("dots-studio/dots-3-note-preview:free") == (0.0, 0.0),
          str(price_for("dots-studio/dots-3-note-preview:free")))
    check("price: a bare -free model is free",
          price_for("gemini-3.8-flash-free") == (0.0, 0.0),
          str(price_for("gemini-3.8-flash-free")))


def test_an_unknown_free_model_is_not_billed_the_default() -> None:
    """A free model with no PRICING row must not fall through to $1/$3."""
    got = price_for("some-brand-new-model-free")
    check("price: an unknown free model is free, not DEFAULT_PRICE",
          got == (0.0, 0.0) and got != DEFAULT_PRICE, str(got))


def test_a_paid_model_keeps_its_price() -> None:
    """The free check must not swallow the models it is not about."""
    check("price: the paid row is unchanged",
          price_for("deepseek-v4.1-flash") == PRICING["deepseek-v4.1-flash"],
          str(price_for("deepseek-v4.1-flash")))
    check("price: another paid row is unchanged",
          price_for("qwen-3.8-27b") == PRICING["qwen-3.8-27b"],
          str(price_for("qwen-3.8-27b")))
    check("price: an unknown paid model still gets DEFAULT_PRICE",
          price_for("brand-new-paid-model") == DEFAULT_PRICE,
          str(price_for("brand-new-paid-model")))


def test_freebie_in_the_middle_is_not_free() -> None:
    """The suffix has to be exact — a substring match would mis-price a real charge."""
    got = price_for("some-paid:freebie")
    check("price: 'freebie' is not mistaken for 'free'",
          got != (0.0, 0.0), str(got))
    got2 = price_for("free-tier-plus")
    check("price: a leading 'free' is not a free suffix",
          got2 != (0.0, 0.0), str(got2))


# --- 3. a zero balance is not an auth failure ------------------------------

def _status_error(code: int, body: dict) -> anthropic.APIStatusError:
    """The SDK error the gateway's response would actually produce.

    The class matters, not just the status: ``_friendly_error`` dispatches on
    ``isinstance``, and the SDK raises ``AuthenticationError`` for a 401 — a bare
    ``APIStatusError`` would fall through to the generic branch and the test would
    be asserting about a shape the harness never sees. 402 has no dedicated
    subclass, so the base is correct there and ``_friendly_error`` reads its
    ``status_code``.
    """
    cls = {401: anthropic.AuthenticationError,
           403: anthropic.PermissionDeniedError,
           429: anthropic.RateLimitError}.get(code, anthropic.APIStatusError)
    resp = httpx2.Response(
        code, json=body,
        request=httpx2.Request("POST", "https://api.beatapi.io/v1/messages"))
    return cls("boom", response=resp, body=body)


def test_a_zero_balance_is_reported_as_a_billing_state() -> None:
    """402 insufficient_credits must name the fix, not read as a generic error."""
    err = _status_error(402, {"error": {"type": "insufficient_credits",
                                        "message": "Insufficient balance."}})
    msg = Agent._friendly_error(err)
    check("402: the credit state is named", "no credit" in msg, msg)
    check("402: it points at the free models", "/models" in msg, msg)
    check("402: it is not reported as an auth failure",
          "authentication failed" not in msg, msg)
    check("402: it does not blame the key", "check ANTHROPIC" not in msg, msg)


def test_a_real_auth_failure_is_still_an_auth_failure() -> None:
    """The 402 branch must not swallow a genuine 401."""
    err = _status_error(401, {"error": {"type": "authentication_error",
                                        "message": "invalid x-api-key"}})
    msg = Agent._friendly_error(err)
    check("401: still reads as an auth failure", "authentication failed" in msg, msg)
    check("401: it does not claim a billing problem", "no credit" not in msg, msg)


def test_a_gated_model_is_not_an_auth_failure() -> None:
    """403 permission_denied must name the gate, not read as a bad key.

    Measured live: ``thinkingmachines/inkling:free`` on OpenRouter answers 403
    with "only available on agentic harnesses". The key is valid and thirteen
    other free models on it work in the same minute, so "check your key" would
    send the operator to re-check something that is fine — and before this
    branch existed the raw SDK dict was printed at them instead.
    """
    err = _status_error(403, {"error": {
        "type": "error",
        "error": {"type": "permission_error", "error_type": "permission_denied",
                  "message": "only available on agentic harnesses."}}})
    msg = Agent._friendly_error(err)
    check("403: the gate is named", "gated" in msg, msg)
    check("403: it says the key is fine", "key is fine" in msg, msg)
    check("403: it points at another model", "/models" in msg, msg)
    check("403: it is not reported as an auth failure",
          "authentication failed" not in msg, msg)
    check("403: it does not leak a raw python dict",
          "{'type'" not in msg and "permission_error" not in msg, msg)


def test_a_throttled_free_model_suggests_another_model() -> None:
    """429 on a free tier is per-model, so the useful advice is 'pick another'.

    Measured: three free OpenRouter models 429'd while eleven others on the same
    key answered, in the same minute — so "wait and retry" is the wrong advice.
    """
    err = _status_error(429, {"error": {"type": "rate_limit_exceeded",
                                        "message": "Rate limit exceeded"}})
    msg = Agent._friendly_error(err)
    check("429: it names the throttle", "rate limited" in msg, msg)
    check("429: it points at another free model", "/models" in msg, msg)
    check("429: it is not reported as an auth failure",
          "authentication failed" not in msg, msg)


def main() -> int:
    # Cleared for the WHOLE run, not per case: the SDK falls back to these
    # variables itself when a client is built with no credential, so restoring
    # them between cases would leak a live token into the very assertions about
    # sending no credential at all.
    for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        os.environ.pop(k, None)

    for fn in (
        test_a_bearer_token_is_sent_alone,
        test_a_stale_api_key_is_not_forwarded_with_a_token,
        test_an_api_key_still_works_on_its_own,
        test_no_credential_sends_no_auth_header,
        test_a_free_suffix_is_priced_at_zero,
        test_an_unknown_free_model_is_not_billed_the_default,
        test_a_paid_model_keeps_its_price,
        test_freebie_in_the_middle_is_not_free,
        test_a_zero_balance_is_reported_as_a_billing_state,
        test_a_real_auth_failure_is_still_an_auth_failure,
        test_a_gated_model_is_not_an_auth_failure,
        test_a_throttled_free_model_suggests_another_model,
    ):
        fn()

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
