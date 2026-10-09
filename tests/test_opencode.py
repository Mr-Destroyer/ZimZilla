"""Regression suite for the OpenCode Zen source.

Run:  python tests/test_opencode.py   (from an activated venv)

OpenCode Zen is TokenHarbour's twin — a hosted Anthropic-protocol gateway with a
key set by the operator and a catalog fetched live. This suite protects the same
three things, plus the one place the twins differ:

* **Credential precedence.** ``source ~/claude-source/<name> && zimzilla`` is the
  whole point of the feature: a key the operator already has in the environment
  must win over everything, and the gateway's host must be recognised without a
  port to match on.
* **The key file.** It is written mode 600, in the profile format the rest of the
  project reads, and never echoed back.
* **The live catalog.** Free models are found from the ``-free`` suffix — the
  only signal OpenCode Zen gives, since its listing carries no prices — and a
  failed fetch returns an error rather than raising, because the caller is a TUI
  command.
* **The public endpoint.** ``/zen/v1/models`` answers without a key, so a fetch
  with no token must succeed, and an anonymous list must not be served to a
  keyed fetch (or the reverse).

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


# A minimal catalog shaped like the real one: the endpoint returns only
# id/object/created/owned_by, so free-ness rides entirely on the -free suffix.
CATALOG = {
    "object": "list",
    "data": [
        {"id": "mimo-v2.6-flash-free", "object": "model", "created": 1,
         "owned_by": "opencode"},
        {"id": "nemotron-3-ultra-free", "object": "model", "created": 1,
         "owned_by": "opencode"},
        {"id": "claude-opus-5", "object": "model", "created": 1,
         "owned_by": "opencode"},
        {"id": "gpt-5.5", "object": "model", "created": 1, "owned_by": "opencode"},
    ],
}

PROFILE_TEXT = (
    'export ANTHROPIC_BASE_URL="https://opencode.ai/zen/"\n'
    'export ANTHROPIC_AUTH_TOKEN="sk-fromprofile00000000000000"\n'
    'export ANTHROPIC_MODEL="mimo-v2.6-flash-free"\n'
    'export ANTHROPIC_API_KEY=""\n'
)


class _Resp(io.BytesIO):
    """A urlopen() result: a context manager that also reports a status."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(payload: dict | None = None, error: Exception | None = None,
                  record: list | None = None):
    def _open(req, timeout=None):
        if record is not None:
            record.append(req)
        if error is not None:
            raise error
        return _Resp(json.dumps(payload or CATALOG).encode())
    return _open


# ---------------------------------------------------------------------------

def test_free_detection() -> None:
    """Free is the -free suffix — the only signal OpenCode Zen gives."""
    from zimzilla import opencode as oc

    models = oc._parse_models(CATALOG)
    by_id = {m.id: m for m in models}
    check("catalog: every entry parsed", len(models) == 4, str(len(models)))
    check("catalog: a -free suffix is free", by_id["mimo-v2.6-flash-free"].free)
    check("catalog: a plain id is not free", not by_id["claude-opus-5"].free)
    check("catalog: 'free' mid-id without the suffix is not free",
          not oc.Model(id="free-range-model").free)

    free = [m.id for m in models if m.free]
    check("catalog: exactly the two free ones", sorted(free) ==
          ["mimo-v2.6-flash-free", "nemotron-3-ultra-free"], str(free))

    # A malformed entry must not take the whole catalog down.
    messy = oc._parse_models({"data": [
        {"id": "ok"},
        {"no_id": True},
        "not-a-dict",
        {"id": "  "},
    ]})
    check("catalog: a malformed entry is skipped, not fatal",
          [m.id for m in messy] == ["ok"], str([m.id for m in messy]))


def test_fetch_is_total() -> None:
    """fetch_models returns errors; it never raises into the TUI."""
    from zimzilla import opencode as oc

    oc.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        models, err = oc.fetch_models("k", use_cache=False)
    check("fetch: a good response parses", err == "" and models is not None
          and len(models) == 4, err)
    check("fetch: the result is cached", oc.cached_ids() is not None
          and len(oc.cached_ids() or []) == 4)

    oc.clear_cache()
    with mock.patch("urllib.request.urlopen",
                    _stub_urlopen(error=urllib.error.URLError("no route"))):
        models, err = oc.fetch_models("k", use_cache=False)
    check("fetch: a network failure returns None and a message",
          models is None and "could not reach" in err, err)

    oc.clear_cache()
    http = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    with mock.patch("urllib.request.urlopen", _stub_urlopen(error=http)):
        models, err = oc.fetch_models("k", use_cache=False)
    check("fetch: a rejected key says so", models is None and "401" in err, err)

    # A 403 on a tokenless fetch is the edge refusing us, NOT a bad key — the
    # catalog is public, so blaming the key there would send the operator off to
    # check a credential that was never sent.
    oc.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen(error=http)):
        models, err = oc.fetch_models("", use_cache=False)
    check("fetch: an anonymous 401 is not blamed on the key",
          models is None and "key rejected" not in err, err)


def test_fetch_without_a_key() -> None:
    """The catalog endpoint is public: a tokenless fetch must work and send no
    credential, and it must not be served the list a keyed fetch left behind."""
    from zimzilla import opencode as oc

    oc.clear_cache()
    seen: list = []
    with mock.patch("urllib.request.urlopen", _stub_urlopen(record=seen)):
        models, err = oc.fetch_models("")
    check("anon: a fetch with no token succeeds", err == "" and models is not None)
    check("anon: no Authorization header is sent",
          "Authorization" not in seen[0].headers, str(dict(seen[0].headers)))
    # Cloudflare rejects urllib's default agent with a 403, so a User-Agent of
    # our own is not decoration — without it the catalog is unreachable.
    check("anon: a User-Agent is always sent",
          bool(seen[0].get_header("User-agent")), str(dict(seen[0].headers)))

    # A keyed fetch must not reuse the anonymous cache entry.
    oc.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        oc.fetch_models("")
    calls: list = []
    with mock.patch("urllib.request.urlopen", _stub_urlopen(record=calls)):
        oc.fetch_models("sk-keyed")
    check("anon: a keyed fetch does not reuse the anonymous list",
          len(calls) == 1, str(len(calls)))


def test_cache_is_keyed_by_token() -> None:
    """A different key must not be served the previous key's catalog."""
    from zimzilla import opencode as oc

    oc.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        oc.fetch_models("key-a")
    check("cache: key-a is cached", oc.cached_ids() is not None)
    calls = []
    with mock.patch("urllib.request.urlopen", _stub_urlopen(record=calls)):
        oc.fetch_models("key-b")
    check("cache: a new key fetches rather than reusing another's list",
          len(calls) == 1, str(len(calls)))


def test_free_models_only() -> None:
    from zimzilla import opencode as oc

    oc.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        free, err = oc.free_models("k", use_cache=False)
    check("free_models: only the free ones",
          [m.id for m in free] == ["mimo-v2.6-flash-free", "nemotron-3-ultra-free"],
          str([m.id for m in free]))


def test_profile_roundtrip() -> None:
    """save_key writes mode 600, in a format the parser reads back."""
    from zimzilla import opencode as oc

    token = "sk-saved00000000000000000000"
    path = oc.save_key(token, "mimo-v2.6-flash-free")
    check("key: written where load_credentials looks", path == oc.PROFILE)
    mode = path.stat().st_mode & 0o777
    check("key: the file is mode 600", mode == 0o600, oct(mode))
    parent_mode = path.parent.stat().st_mode & 0o777
    check("key: the directory is mode 700", parent_mode == 0o700, oct(parent_mode))

    exports = oc._read_exports(path)
    # The key lives in ANTHROPIC_API_KEY, not ANTHROPIC_AUTH_TOKEN: the SDK sends
    # x-api-key for the former and Authorization: Bearer for the latter, and Zen
    # reads only x-api-key.
    check("key: the key round-trips through ANTHROPIC_API_KEY",
          exports.get("ANTHROPIC_API_KEY") == token)
    check("key: ANTHROPIC_AUTH_TOKEN is empty (no stray Bearer)",
          exports.get("ANTHROPIC_AUTH_TOKEN") == "")
    check("key: the model round-trips",
          exports.get("ANTHROPIC_MODEL") == "mimo-v2.6-flash-free")
    check("key: the endpoint is the gateway",
          oc.is_opencode(exports.get("ANTHROPIC_BASE_URL")))
    check("key: the file is source-format", path.read_text().count("export ") == 4)


def test_looks_like_key() -> None:
    from zimzilla import opencode as oc

    check("key: a real-looking key passes",
          oc.looks_like_key("sk-rcYcM3enKGzKrtuhxdJBzN3QWNW10KlFFvxs5rpz"))
    check("key: a short string is rejected", not oc.looks_like_key("abc"))
    check("key: a pasted export line is rejected",
          not oc.looks_like_key('export ANTHROPIC_AUTH_TOKEN="sk-xxx"'))
    check("key: whitespace is rejected",
          not oc.looks_like_key("sk-aaaaaaaaaaaaaaaaaaaa bbbb"))


def test_active_key_recognises_host() -> None:
    """A hosted URL has no port, so it must be matched by host."""
    from zimzilla import sources as s

    check("sources: the zen path is the opencode source",
          s.active_key("https://opencode.ai/zen") == "opencode")
    check("sources: with a trailing slash too",
          s.active_key("https://opencode.ai/zen/") == "opencode")
    check("sources: the bare host too",
          s.active_key("https://opencode.ai") == "opencode")
    check("sources: tokenharbour is still itself",
          s.active_key("https://tokenharbor.ai/") == "tokenharbour")
    check("sources: a local proxy is still itself",
          s.active_key("http://localhost:4001") == "logfare")
    check("sources: an unrelated host is unknown",
          s.active_key("https://api.example.com") is None)


def test_credential_precedence(tmp: Path) -> None:
    """Environment beats the profile; the profile beats ~/claude-source."""
    from zimzilla import opencode as oc

    claude_source = tmp / "claude-source"
    claude_source.mkdir(parents=True, exist_ok=True)
    (claude_source / "zen").write_text(PROFILE_TEXT)

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        with mock.patch.object(oc, "CLAUDE_SOURCE", claude_source), \
             mock.patch.object(oc, "PROFILE", tmp / "nonexistent" / "source"):
            creds = oc.load_credentials()
            check("creds: falls back to ~/claude-source",
                  creds is not None and creds[2].endswith("zen"),
                  str(creds and creds[2]))
            check("creds: the model comes from that profile",
                  creds is not None and creds[1] == "mimo-v2.6-flash-free")

            own = tmp / "own" / "source"
            own.parent.mkdir(parents=True, exist_ok=True)
            own.write_text('export ANTHROPIC_AUTH_TOKEN="sk-ownprofile000000000000"\n')
            with mock.patch.object(oc, "PROFILE", own):
                creds = oc.load_credentials()
                check("creds: ZimZilla's own profile beats ~/claude-source",
                      creds is not None and creds[2] == str(own),
                      str(creds and creds[2]))

                os.environ["ANTHROPIC_BASE_URL"] = "https://opencode.ai/zen/"
                os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-fromenv0000000000000000"
                os.environ["ANTHROPIC_MODEL"] = "nemotron-3-ultra-free"
                creds = oc.load_credentials()
                check("creds: the environment beats every file",
                      creds is not None and creds[2] == "environment"
                      and creds[0] == "sk-fromenv0000000000000000",
                      str(creds and creds[2]))
                check("creds: the environment's model is used",
                      creds is not None and creds[1] == "nemotron-3-ultra-free")

                # An env key pointed at ANOTHER host must not be mistaken for ours.
                os.environ["ANTHROPIC_BASE_URL"] = "https://elsewhere.example.com"
                creds = oc.load_credentials()
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
    from zimzilla import opencode as oc

    class _Cfg:
        base_url = "http://localhost:4001"
        auth_token = "leftover-from-logfare"
        api_key = "sk-leftover"
        model = "deepseek-v4.1-flash"

    with mock.patch.object(oc, "load_credentials",
                           lambda: ("sk-x0000000000000000000000",
                                    "mimo-v2.6-flash-free", "test")):
        cfg = _Cfg()
        applied = oc.apply_to(cfg)
        check("apply: returns the origin and model",
              applied == ("test", "mimo-v2.6-flash-free"), str(applied))
        check("apply: the endpoint is the gateway",
              cfg.base_url == oc.DEFAULT_BASE_URL)
        check("apply: the endpoint ends at /zen (the SDK adds /v1/messages)",
              cfg.base_url.endswith("/zen"), cfg.base_url)
        check("apply: the key is set as api_key", cfg.api_key.startswith("sk-"))
        check("apply: the stale auth_token is cleared", cfg.auth_token == "")
        check("apply: the model is applied", cfg.model == "mimo-v2.6-flash-free")

    with mock.patch.object(oc, "load_credentials", lambda: None):
        cfg = _Cfg()
        check("apply: no key leaves the config alone",
              oc.apply_to(cfg) is None and cfg.base_url == "http://localhost:4001")

    check("guard: the default local proxy is overridable",
          oc.applies_to_default_endpoint("http://localhost:4001"))
    check("guard: an empty endpoint is overridable",
          oc.applies_to_default_endpoint(""))
    check("guard: the gateway itself is NOT overridden",
          not oc.applies_to_default_endpoint("https://opencode.ai/zen/"))
    check("guard: another host is NOT overridden",
          not oc.applies_to_default_endpoint("https://api.example.com"))


def test_selection_file_is_shared() -> None:
    """The selection file is the same one sources.py reads."""
    from zimzilla import sources as s, opencode as oc

    oc.set_current("opencode")
    check("selection: sources.py sees the opencode choice",
          s.current_key() == "opencode", str(s.current_key()))
    check("selection: opencode sees it too",
          oc.current_key() == "opencode")
    # And a built-in key survives even though discover() cannot find it.
    check("selection: opencode is a builtin key",
          "opencode" in s.BUILTIN_KEYS)


def test_no_key_is_echoed(tmp: Path) -> None:
    """The display value is a path, never the key itself."""
    from zimzilla import opencode as oc

    own = tmp / "own2" / "source"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text('export ANTHROPIC_AUTH_TOKEN="sk-secret000000000000000000"\n')
    with mock.patch.object(oc, "PROFILE", own):
        creds = oc.load_credentials()
    origin = creds[2]
    check("display: the origin is a path, not a key", "sk-" not in origin, origin)


def test_client_sends_the_key_where_zen_reads_it() -> None:
    """The regression this suite was missing.

    OpenCode Zen reads ``x-api-key`` and ignores ``Authorization: Bearer``. The
    Anthropic SDK maps ``api_key`` to the former and ``auth_token`` to the
    latter, so a key parked in ANTHROPIC_AUTH_TOKEN — which is what every other
    source in this project uses — reaches the gateway as a header it never looks
    at. Worse, the SDK sends BOTH when both are set, so a leftover api_key lands
    on the wire as the literal string "placeholder" and the gateway answers 401.

    This asserts on the real SDK's ``auth_headers`` for the config ``apply_to``
    produces, because that dict is what actually goes over the wire.
    """
    import anthropic

    from zimzilla import opencode as oc

    class _Cfg:
        base_url = "http://localhost:4001"
        api_key = "sk-leftover-from-another-provider"
        auth_token = "leftover-token"
        model = "deepseek-v4.1-flash"

    with mock.patch.object(oc, "load_credentials",
                           lambda: ("sk-zen000000000000000000000",
                                    "mimo-v2.6-flash-free", "test")):
        cfg = _Cfg()
        oc.apply_to(cfg)
        # Mirror agent._ensure_client: auth_token wins the branch, and a
        # placeholder is only stood in when there is no real key.
        kwargs = {}
        if cfg.auth_token:
            kwargs["auth_token"] = cfg.auth_token
            kwargs["api_key"] = cfg.api_key or "placeholder"
        elif cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        else:
            kwargs["api_key"] = "placeholder"
        client = anthropic.AsyncAnthropic(base_url=cfg.base_url, **kwargs)
        headers = dict(client.auth_headers)

    check("wire: x-api-key carries the real key",
          headers.get("X-Api-Key") == "sk-zen000000000000000000000",
          str(headers))
    check("wire: x-api-key is NOT the placeholder",
          headers.get("X-Api-Key") != "placeholder", str(headers))
    check("wire: no Authorization header is sent",
          "Authorization" not in headers, str(headers))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-oc-test-"))
    # Pin the module's paths to the temp tree before anything imports it.
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    test_free_detection()
    test_fetch_is_total()
    test_fetch_without_a_key()
    test_cache_is_keyed_by_token()
    test_free_models_only()
    test_profile_roundtrip()
    test_looks_like_key()
    test_active_key_recognises_host()
    test_credential_precedence(tmp)
    test_apply_and_guard()
    test_client_sends_the_key_where_zen_reads_it()
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
