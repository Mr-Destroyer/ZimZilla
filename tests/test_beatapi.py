"""Regression suite for the BeatAPI source.

Run:  python tests/test_beatapi.py   (from an activated venv)

Protects the things that are easy to break and expensive to notice:

* **Credential precedence.** ``source ~/claude-source/<name> && zimzilla`` must
  work: a key the operator already has in the environment wins over every file,
  and the gateway's host is recognised without a port to match on.
* **The key file.** Written mode 600, in the profile format the rest of the
  project reads, and never echoed back.
* **The free list.** BeatAPI's catalog publishes **no pricing block**, so free
  is decided by the ``-free`` id suffix — and, critically, a model with no price
  at all must NOT read as free. That is the one bug this catalog shape invites,
  so it gets its own test.
* **Totality.** A failed fetch returns an error rather than raising, because
  the caller is a TUI command.

No test here touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


# Shaped like the real response: a bare `data` list, `owned_by` per entry, and
# NO pricing block anywhere — which is the whole point. Four free (by suffix),
# three paid.
CATALOG = {
    "object": "list",
    "data": [
        {"id": "claude-haiku-5.5-free", "owned_by": "anthropic",
         "context_length": 200000, "supported_endpoint_types": ["anthropic"]},
        {"id": "deepseek-v4.1-flash-free", "owned_by": "deepseek",
         "context_length": 128000, "supported_endpoint_types": ["anthropic"]},
        {"id": "gpt-oss-120b-free", "owned_by": "openai",
         "context_length": 131072},
        {"id": "gemini-4-flash-free", "owned_by": "google",
         "context_length": 1048576},
        {"id": "claude-opus-5.5", "owned_by": "anthropic",
         "context_length": 200000},
        {"id": "deepseek-v4.1-pro", "owned_by": "deepseek",
         "context_length": 128000},
        {"id": "gpt-5.2", "owned_by": "openai", "context_length": 400000},
    ],
}

PROFILE_TEXT = (
    'export ANTHROPIC_BASE_URL="https://api.beatapi.io"\n'
    'export ANTHROPIC_AUTH_TOKEN="sk-fromprofile000000000000000"\n'
    'export ANTHROPIC_MODEL="deepseek-v4.1-flash-free"\n'
    'export ANTHROPIC_API_KEY=""\n'
)


class _Resp(io.BytesIO):
    """A urlopen() result: a context manager that also reports a status."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(payload: dict | None = None, error: Exception | None = None):
    def _open(req, timeout=None):
        if error is not None:
            raise error
        return _Resp(json.dumps(payload or CATALOG).encode())
    return _open


# ---------------------------------------------------------------------------

def test_free_detection() -> None:
    """Free is the ``-free`` suffix — and an unpriced model is NOT free.

    This is the trap in BeatAPI's shape: it publishes no pricing block, so every
    entry arrives with no price. If ``free`` were ``price_in == 0``, the entire
    catalog would read as free the moment the suffix check missed. The module
    tracks whether a price was actually present, and this asserts it.
    """
    from zimzilla import beatapi as b

    models = b._parse_models(CATALOG)
    by_id = {m.id: m for m in models}
    check("catalog: every entry parsed", len(models) == 7, str(len(models)))
    check("catalog: a -free suffix is free", by_id["claude-haiku-5.5-free"].free)
    check("catalog: another -free suffix is free",
          by_id["gemini-4-flash-free"].free)

    # The trap: no pricing block at all, and these must still be paid.
    check("catalog: an UNPRICED model is not free",
          not by_id["claude-opus-5.5"].free)
    check("catalog: an unpriced non-suffixed model is not free",
          not by_id["gpt-5.2"].free)

    check("catalog: owned_by is read",
          by_id["claude-opus-5.5"].owned_by == "anthropic")
    check("catalog: context is read",
          by_id["gemini-4-flash-free"].context_length == 1_048_576)

    free = sorted(m.id for m in models if m.free)
    check("catalog: exactly the four free ones", free == [
        "claude-haiku-5.5-free", "deepseek-v4.1-flash-free",
        "gemini-4-flash-free", "gpt-oss-120b-free"], str(free))

    # A malformed entry must not take the whole catalog down.
    messy = b._parse_models({"data": [
        {"id": "ok"},
        {"no_id": True},
        "not-a-dict",
        {"id": "  "},
        {"id": "bad-ctx", "context_length": "nonsense"},
    ]})
    check("catalog: bad entries are skipped, not fatal",
          [m.id for m in messy] == ["ok", "bad-ctx"], str([m.id for m in messy]))
    check("catalog: a bad context degrades to None",
          messy[1].context_length is None)


def test_zero_price_still_counts() -> None:
    """If BeatAPI ever grows a pricing block, a 0/0 entry is free.

    The suffix is what decides today, but a zero price is accepted when one
    appears — otherwise the day the catalog adds pricing, a model zeroed out
    without a rename would silently become invisible to /models.
    """
    from zimzilla import beatapi as b

    models = b._parse_models({"data": [
        {"id": "renamed-but-free",
         "pricing": {"input_usd_per_1m": 0, "output_usd_per_1m": 0}},
        {"id": "genuinely-paid",
         "pricing": {"input_usd_per_1m": 3, "output_usd_per_1m": 15}},
    ]})
    by_id = {m.id: m for m in models}
    check("catalog: a zero price with no suffix is free",
          by_id["renamed-but-free"].free)
    check("catalog: a paid model is not free", not by_id["genuinely-paid"].free)
    check("catalog: prices are read",
          by_id["genuinely-paid"].price_in == 3.0
          and by_id["genuinely-paid"].price_out == 15.0)


def test_fetch_is_total() -> None:
    """fetch_models returns errors; it never raises into the TUI."""
    from zimzilla import beatapi as b

    b.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        models, err = b.fetch_models("sk-x", use_cache=False)
    check("fetch: a good response parses", err == "" and models is not None
          and len(models) == 7, err)
    check("fetch: the result is cached", b.cached_ids() is not None
          and len(b.cached_ids() or []) == 7)

    b.clear_cache()
    with mock.patch("urllib.request.urlopen",
                    _stub_urlopen(error=urllib.error.URLError("no route"))):
        models, err = b.fetch_models("sk-x", use_cache=False)
    check("fetch: a network failure returns None and a message",
          models is None and "could not reach" in err, err)

    b.clear_cache()
    http = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    with mock.patch("urllib.request.urlopen", _stub_urlopen(error=http)):
        models, err = b.fetch_models("sk-x", use_cache=False)
    check("fetch: a rejected key says so", models is None and "401" in err, err)
    check("fetch: the rejection names the setup command",
          "beatapi-api-setup" in err, err)


def test_cache_is_keyed_by_token() -> None:
    """A different key must not be served the previous key's catalog."""
    from zimzilla import beatapi as b

    b.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        b.fetch_models("key-a")
    check("cache: key-a is cached", b.cached_ids() is not None)
    calls = []

    def counting(req, timeout=None):
        calls.append(req)
        return _Resp(json.dumps({"data": []}).encode())

    with mock.patch("urllib.request.urlopen", counting):
        models, _ = b.fetch_models("key-b")
    check("cache: a new key fetches rather than reusing another's list",
          len(calls) == 1 and models == [], str(len(calls)))


def test_free_models_only() -> None:
    from zimzilla import beatapi as b

    b.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        free, err = b.free_models("sk-x", use_cache=False)
    check("free_models: only the free ones", len(free) == 4
          and all(m.free for m in free), str([m.id for m in free]))


def test_the_request_carries_the_key() -> None:
    """The catalog is gated, so the fetch must actually send the credential."""
    from zimzilla import beatapi as b

    b.clear_cache()
    seen = {}

    def capture(req, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
        seen["url"] = req.full_url
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", capture):
        b.fetch_models("sk-secret000000000000000000", use_cache=False)
    check("fetch: hits the gateway's models endpoint",
          seen["url"] == "https://api.beatapi.io/v1/models", seen.get("url"))
    check("fetch: sends the key as a bearer token",
          seen["headers"].get("authorization")
          == "Bearer sk-secret000000000000000000")
    check("fetch: sends x-api-key too",
          seen["headers"].get("x-api-key") == "sk-secret000000000000000000")
    # Load-bearing, not decoration: Cloudflare bans urllib's default signature
    # with error 1010 before the key is examined. Measured — no UA is a 403,
    # any explicit UA is a 200.
    check("fetch: sends an explicit User-Agent",
          seen["headers"].get("user-agent") == b.USER_AGENT,
          str(seen["headers"].get("user-agent")))


def test_a_cloudflare_block_is_not_blamed_on_the_key() -> None:
    """A 403 from the edge must not read as a rejected key.

    BeatAPI is behind Cloudflare. When it refuses the client at the edge the
    response is a 403 carrying error 1010 — raised before the API sees the
    request, so the key is never examined. Reporting that as "key rejected"
    sends the operator to re-check a credential that was never looked at, which
    is worse than saying nothing.
    """
    from zimzilla import beatapi as b

    cf_body = json.dumps({
        "type": "https://developers.cloudflare.com/support/troubleshooting/"
                "http-status-codes/cloudflare-1xxx-errors/error-1010/",
        "title": "Error 1010: Access denied",
        "status": 403,
        "error_code": 1010,
        "error_name": "browser_signature_banned",
    }).encode()

    b.clear_cache()
    edge = urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(cf_body))
    with mock.patch("urllib.request.urlopen", _stub_urlopen(error=edge)):
        models, err = b.fetch_models("sk-x", use_cache=False)
    check("cloudflare: a 1010 block is reported as an edge refusal",
          models is None and "1010" in err, err)
    check("cloudflare: it does NOT claim the key was rejected",
          "key rejected" not in err, err)
    check("cloudflare: it does not send the operator to re-check the key",
          "api-setup" not in err, err)

    # A genuine 403 from the API itself must still be reported as a bad key.
    b.clear_cache()
    plain = urllib.error.HTTPError(
        "u", 403, "forbidden", {},
        io.BytesIO(b'{"error":{"message":"invalid api key"}}'))
    with mock.patch("urllib.request.urlopen", _stub_urlopen(error=plain)):
        models, err = b.fetch_models("sk-x", use_cache=False)
    check("cloudflare: a plain 403 is still reported as a key problem",
          models is None and "key rejected" in err, err)


def test_profile_roundtrip() -> None:
    """save_key writes mode 600, in a format the parser reads back."""
    from zimzilla import beatapi as b

    token = "sk-saved000000000000000000000"
    path = b.save_key(token, "deepseek-v4.1-flash-free")
    check("key: written where load_credentials looks", path == b.PROFILE)
    mode = path.stat().st_mode & 0o777
    check("key: the file is mode 600", mode == 0o600, oct(mode))
    parent_mode = path.parent.stat().st_mode & 0o777
    check("key: the directory is mode 700", parent_mode == 0o700, oct(parent_mode))

    exports = b._read_exports(path)
    check("key: the token round-trips", exports.get("ANTHROPIC_AUTH_TOKEN") == token)
    check("key: the model round-trips",
          exports.get("ANTHROPIC_MODEL") == "deepseek-v4.1-flash-free")
    check("key: the endpoint is the gateway",
          b.is_beatapi(exports.get("ANTHROPIC_BASE_URL")))
    check("key: the file is source-format", path.read_text().count("export ") == 4)


def test_looks_like_key() -> None:
    from zimzilla import beatapi as b

    check("key: a real-looking key passes",
          b.looks_like_key("sk-3hlwxTtJcXQfoCgJ2q4OHWfrl7SZxjAAup9FvRGXRuX15B17"))
    check("key: a short string is rejected", not b.looks_like_key("abc"))
    check("key: a pasted export line is rejected",
          not b.looks_like_key('export ANTHROPIC_AUTH_TOKEN="sk-xxxx"'))
    check("key: whitespace is rejected",
          not b.looks_like_key("sk-aaaaaaaaaaaaaaaaaaaaaaaa bbbb"))


def test_active_key_recognises_host() -> None:
    """A hosted URL has no port, so it must be matched by host."""
    from zimzilla import sources as s

    check("sources: the gateway host is the beatapi source",
          s.active_key("https://api.beatapi.io") == "beatapi")
    check("sources: with a trailing slash too",
          s.active_key("https://api.beatapi.io/") == "beatapi")
    check("sources: it is a builtin, so it survives a restart",
          "beatapi" in s.BUILTIN_KEYS)
    check("sources: a local proxy is still itself",
          s.active_key("http://localhost:4001") == "logfare")
    check("sources: an unrelated host is unknown",
          s.active_key("https://api.example.com") is None)


def test_credential_precedence(tmp: Path) -> None:
    """Environment beats the profile; the profile beats ~/claude-source."""
    from zimzilla import beatapi as b

    claude_source = tmp / "claude-source"
    claude_source.mkdir(parents=True, exist_ok=True)
    (claude_source / "beatapi").write_text(PROFILE_TEXT)

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        with mock.patch.object(b, "CLAUDE_SOURCE", claude_source), \
             mock.patch.object(b, "PROFILE", tmp / "nonexistent" / "source"):
            creds = b.load_credentials()
            check("creds: falls back to ~/claude-source",
                  creds is not None and creds[2].endswith("beatapi"),
                  str(creds and creds[2]))
            check("creds: the model comes from that profile",
                  creds is not None and creds[1] == "deepseek-v4.1-flash-free")

            own = tmp / "own" / "source"
            own.parent.mkdir(parents=True, exist_ok=True)
            own.write_text('export ANTHROPIC_AUTH_TOKEN="sk-ownprofile000000000000"\n')
            with mock.patch.object(b, "PROFILE", own):
                creds = b.load_credentials()
                check("creds: ZimZilla's own profile beats ~/claude-source",
                      creds is not None and creds[2] == str(own),
                      str(creds and creds[2]))

                os.environ["ANTHROPIC_BASE_URL"] = "https://api.beatapi.io"
                os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-fromenv0000000000000000"
                os.environ["ANTHROPIC_MODEL"] = "gemini-4-flash-free"
                creds = b.load_credentials()
                check("creds: the environment beats every file",
                      creds is not None and creds[2] == "environment"
                      and creds[0] == "sk-fromenv0000000000000000",
                      str(creds and creds[2]))

                # An env key pointed at ANOTHER host must not be mistaken for ours.
                os.environ["ANTHROPIC_BASE_URL"] = "https://elsewhere.example.com"
                creds = b.load_credentials()
                check("creds: an env key for another host is ignored",
                      creds is not None and creds[2] == str(own),
                      str(creds and creds[2]))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_apply_and_guard() -> None:
    """apply_to aims a config; the guard keeps a stale choice from hijacking."""
    from zimzilla import beatapi as b

    class _Cfg:
        base_url = "http://localhost:4001"
        auth_token = "leftover-from-logfare"
        api_key = "sk-leftover"
        model = "deepseek-v4.1-flash"

    with mock.patch.object(b, "load_credentials",
                           lambda: ("sk-x000000000000000000000", "gpt-oss-120b-free",
                                    "test")):
        cfg = _Cfg()
        applied = b.apply_to(cfg)
        check("apply: returns the origin and model",
              applied == ("test", "gpt-oss-120b-free"), str(applied))
        check("apply: the endpoint is the gateway",
              cfg.base_url == b.DEFAULT_BASE_URL)
        check("apply: the token is set", cfg.auth_token.startswith("sk-"))
        check("apply: the stale api_key is cleared", cfg.api_key == "")
        check("apply: the model is applied", cfg.model == "gpt-oss-120b-free")

    with mock.patch.object(b, "load_credentials", lambda: None):
        cfg = _Cfg()
        check("apply: no key leaves the config alone",
              b.apply_to(cfg) is None and cfg.base_url == "http://localhost:4001")

    check("guard: the default local proxy is overridable",
          b.applies_to_default_endpoint("http://localhost:4001"))
    check("guard: an empty endpoint is overridable",
          b.applies_to_default_endpoint(""))
    check("guard: the gateway itself is NOT overridden",
          not b.applies_to_default_endpoint("https://api.beatapi.io"))
    check("guard: another host is NOT overridden",
          not b.applies_to_default_endpoint("https://api.example.com"))


def test_selection_file_is_shared() -> None:
    """The selection file is the same one sources.py reads."""
    from zimzilla import sources as s, beatapi as b

    b.set_current("beatapi")
    check("selection: sources.py sees the beatapi choice",
          s.current_key() == "beatapi", str(s.current_key()))
    check("selection: beatapi sees it too", b.current_key() == "beatapi")


def test_no_key_is_echoed(tmp: Path) -> None:
    """The display value is a path, never the key itself."""
    from zimzilla import beatapi as b

    own = tmp / "own2" / "source"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text('export ANTHROPIC_AUTH_TOKEN="sk-secret00000000000000000"\n')
    with mock.patch.object(b, "PROFILE", own):
        creds = b.load_credentials()
    origin = creds[2]
    check("display: the origin is a path, not a key", "sk-" not in origin, origin)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-beat-test-"))
    # Pin the module's paths to the temp tree before anything imports it.
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    test_free_detection()
    test_zero_price_still_counts()
    test_fetch_is_total()
    test_cache_is_keyed_by_token()
    test_free_models_only()
    test_the_request_carries_the_key()
    test_a_cloudflare_block_is_not_blamed_on_the_key()
    test_profile_roundtrip()
    test_looks_like_key()
    test_active_key_recognises_host()
    test_credential_precedence(tmp)
    test_apply_and_guard()
    test_selection_file_is_shared()
    test_no_key_is_echoed(tmp)

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
