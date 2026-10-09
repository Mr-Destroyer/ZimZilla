"""Regression suite for the TokenHarbour source.

Run:  python tests/test_tokenharbour.py   (from an activated venv)

Protects three things that are easy to break and expensive to notice:

* **Credential precedence.** ``source ~/claude-source/<name> && zimzilla`` is
  the whole point of the feature: a key the operator already has in the
  environment must win over everything, and the gateway's host must be
  recognised without a port to match on.
* **The key file.** It is written mode 600, in the profile format the rest of
  the project reads, and never echoed back.
* **The live catalog.** Free models are found from the gateway's own response
  — by ``:free`` suffix *or* a zero price — and a failed fetch returns an error
  rather than raising, because the caller is a TUI command.

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


# A minimal catalog shaped like the real one: two free (one by suffix, one by a
# zero price with no suffix), two paid.
CATALOG = {
    "object": "list",
    "data": [
        {"id": "claude-haiku-5.5:free", "label": "Haiku", "tier": "mid",
         "context_length": 1000000, "tool_call": True,
         "pricing": {"input_usd_per_1m": 0, "output_usd_per_1m": 0}},
        {"id": "zero-priced", "label": "Zero", "tier": "low",
         "context_length": 200000, "tool_call": True,
         "pricing": {"input_usd_per_1m": 0.0, "output_usd_per_1m": 0.0}},
        {"id": "claude-opus-5.5", "label": "Opus", "tier": "high",
         "context_length": 1000000, "tool_call": True,
         "pricing": {"input_usd_per_1m": 4, "output_usd_per_1m": 20}},
        {"id": "grok-4.6", "label": "Grok", "tier": "frontier",
         "context_length": 500000, "tool_call": True,
         "pricing": {"input_usd_per_1m": 2, "output_usd_per_1m": 6}},
    ],
}

PROFILE_TEXT = (
    'export ANTHROPIC_BASE_URL="https://tokenharbor.ai/"\n'
    'export ANTHROPIC_AUTH_TOKEN="thk_live_fromprofile0000000000"\n'
    'export ANTHROPIC_MODEL="mimo-v2.5:free"\n'
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
    """Free is the suffix OR a zero price — either signal alone is enough."""
    from zimzilla import tokenharbour as th

    models = th._parse_models(CATALOG)
    by_id = {m.id: m for m in models}
    check("catalog: every entry parsed", len(models) == 4, str(len(models)))
    check("catalog: a :free suffix is free", by_id["claude-haiku-5.5:free"].free)
    check("catalog: a zero price with no suffix is free",
          by_id["zero-priced"].free)
    check("catalog: a priced model is not free", not by_id["claude-opus-5.5"].free)
    check("catalog: prices are read", by_id["grok-4.6"].price_in == 2.0
          and by_id["grok-4.6"].price_out == 6.0)
    check("catalog: context is read",
          by_id["claude-opus-5.5"].context_length == 1_000_000)

    free = [m.id for m in models if m.free]
    check("catalog: exactly the two free ones", sorted(free) ==
          ["claude-haiku-5.5:free", "zero-priced"], str(free))

    # A malformed entry must not take the whole catalog down.
    messy = th._parse_models({"data": [
        {"id": "ok", "pricing": {"input_usd_per_1m": "nonsense"}},
        {"no_id": True},
        "not-a-dict",
        {"id": "no-pricing"},
    ]})
    check("catalog: a bad price degrades to 0, not a crash",
          [m.id for m in messy] == ["ok", "no-pricing"], str([m.id for m in messy]))


def test_fetch_is_total() -> None:
    """fetch_models returns errors; it never raises into the TUI."""
    from zimzilla import tokenharbour as th

    th.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        models, err = th.fetch_models("k", use_cache=False)
    check("fetch: a good response parses", err == "" and models is not None
          and len(models) == 4, err)
    check("fetch: the result is cached", th.cached_ids() is not None
          and len(th.cached_ids() or []) == 4)

    th.clear_cache()
    with mock.patch("urllib.request.urlopen",
                    _stub_urlopen(error=urllib.error.URLError("no route"))):
        models, err = th.fetch_models("k", use_cache=False)
    check("fetch: a network failure returns None and a message",
          models is None and "could not reach" in err, err)

    th.clear_cache()
    http = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    with mock.patch("urllib.request.urlopen", _stub_urlopen(error=http)):
        models, err = th.fetch_models("k", use_cache=False)
    check("fetch: a rejected key says so", models is None and "401" in err, err)


def test_cache_is_keyed_by_token() -> None:
    """A different key must not be served the previous key's catalog."""
    from zimzilla import tokenharbour as th

    th.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        th.fetch_models("key-a")
    check("cache: key-a is cached", th.cached_ids() is not None)
    # key-b has no cache entry, so it must hit the network.
    calls = []

    def counting(req, timeout=None):
        calls.append(req)
        return _Resp(json.dumps({"data": []}).encode())

    with mock.patch("urllib.request.urlopen", counting):
        models, _ = th.fetch_models("key-b")
    check("cache: a new key fetches rather than reusing another's list",
          len(calls) == 1 and models == [], str(len(calls)))


def test_free_models_only() -> None:
    from zimzilla import tokenharbour as th

    th.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        free, err = th.free_models("k", use_cache=False)
    check("free_models: only the free ones",
          [m.id for m in free] == ["claude-haiku-5.5:free", "zero-priced"],
          str([m.id for m in free]))


def test_profile_roundtrip() -> None:
    """save_key writes mode 600, in a format the parser reads back."""
    from zimzilla import tokenharbour as th

    token = "thk_live_saved0000000000000000"
    path = th.save_key(token, "mimo-v2.5:free")
    check("key: written where load_credentials looks", path == th.PROFILE)
    mode = path.stat().st_mode & 0o777
    check("key: the file is mode 600", mode == 0o600, oct(mode))
    parent_mode = path.parent.stat().st_mode & 0o777
    check("key: the directory is mode 700", parent_mode == 0o700, oct(parent_mode))

    exports = th._read_exports(path)
    check("key: the token round-trips", exports.get("ANTHROPIC_AUTH_TOKEN") == token)
    check("key: the model round-trips",
          exports.get("ANTHROPIC_MODEL") == "mimo-v2.5:free")
    check("key: the endpoint is the gateway",
          th.is_tokenharbour(exports.get("ANTHROPIC_BASE_URL")))
    # The file must be sourceable by a shell, like every other profile.
    check("key: the file is source-format", path.read_text().count("export ") == 4)


def test_looks_like_key() -> None:
    from zimzilla import tokenharbour as th

    check("key: a real-looking key passes",
          th.looks_like_key("thk_live_rcYcM3enKGzKrtuhxdJBzN3QWNW10KlFFvxs5rpz"))
    check("key: a short string is rejected", not th.looks_like_key("abc"))
    check("key: a pasted export line is rejected",
          not th.looks_like_key('export ANTHROPIC_AUTH_TOKEN="thk_live_xxx"'))
    check("key: whitespace is rejected",
          not th.looks_like_key("thk_live_aaaaaaaaaaaaaaaaaaaa bbbb"))


def test_active_key_recognises_host() -> None:
    """A hosted URL has no port, so it must be matched by host."""
    from zimzilla import sources as s

    check("sources: the gateway host is the tokenharbour source",
          s.active_key("https://tokenharbor.ai/") == "tokenharbour")
    check("sources: with no trailing slash too",
          s.active_key("https://tokenharbor.ai") == "tokenharbour")
    check("sources: a local proxy is still itself",
          s.active_key("http://localhost:4001") == "logfare")
    check("sources: an unrelated host is unknown",
          s.active_key("https://api.example.com") is None)


def test_credential_precedence(tmp: Path) -> None:
    """Environment beats the profile; the profile beats ~/claude-source."""
    from zimzilla import tokenharbour as th

    claude_source = tmp / "claude-source"
    claude_source.mkdir(parents=True, exist_ok=True)
    (claude_source / "haiku-5.5").write_text(PROFILE_TEXT)

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        with mock.patch.object(th, "CLAUDE_SOURCE", claude_source), \
             mock.patch.object(th, "PROFILE", tmp / "nonexistent" / "source"):
            creds = th.load_credentials()
            check("creds: falls back to ~/claude-source",
                  creds is not None and creds[2].endswith("haiku-5.5"),
                  str(creds and creds[2]))
            check("creds: the model comes from that profile",
                  creds is not None and creds[1] == "mimo-v2.5:free")

            # Now give it a profile of its own: that must win.
            own = tmp / "own" / "source"
            own.parent.mkdir(parents=True, exist_ok=True)
            own.write_text('export ANTHROPIC_AUTH_TOKEN="thk_live_ownprofile000000000"\n')
            with mock.patch.object(th, "PROFILE", own):
                creds = th.load_credentials()
                check("creds: ZimZilla's own profile beats ~/claude-source",
                      creds is not None and creds[2] == str(own),
                      str(creds and creds[2]))

                # And the environment beats both.
                os.environ["ANTHROPIC_BASE_URL"] = "https://tokenharbor.ai/"
                os.environ["ANTHROPIC_AUTH_TOKEN"] = "thk_live_fromenv00000000000000"
                os.environ["ANTHROPIC_MODEL"] = "claude-haiku-5.5:free"
                creds = th.load_credentials()
                check("creds: the environment beats every file",
                      creds is not None and creds[2] == "environment"
                      and creds[0] == "thk_live_fromenv00000000000000",
                      str(creds and creds[2]))
                check("creds: the environment's model is used",
                      creds is not None and creds[1] == "claude-haiku-5.5:free")

                # An env key pointed at ANOTHER host must not be mistaken for ours.
                os.environ["ANTHROPIC_BASE_URL"] = "https://elsewhere.example.com"
                creds = th.load_credentials()
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
    from zimzilla import tokenharbour as th

    class _Cfg:
        base_url = "http://localhost:4001"
        auth_token = "leftover-from-logfare"
        api_key = "sk-leftover"
        model = "deepseek-v4.1-flash"

    with mock.patch.object(th, "load_credentials",
                           lambda: ("thk_live_x000000000000000000", "mimo-v2.5:free",
                                    "test")):
        cfg = _Cfg()
        applied = th.apply_to(cfg)
        check("apply: returns the origin and model",
              applied == ("test", "mimo-v2.5:free"), str(applied))
        check("apply: the endpoint is the gateway",
              cfg.base_url == th.DEFAULT_BASE_URL)
        check("apply: the token is set", cfg.auth_token.startswith("thk_live_"))
        check("apply: the stale api_key is cleared", cfg.api_key == "")
        check("apply: the model is applied", cfg.model == "mimo-v2.5:free")

    with mock.patch.object(th, "load_credentials", lambda: None):
        cfg = _Cfg()
        check("apply: no key leaves the config alone",
              th.apply_to(cfg) is None and cfg.base_url == "http://localhost:4001")

    # The guard: which endpoints a remembered choice may override.
    check("guard: the default local proxy is overridable",
          th.applies_to_default_endpoint("http://localhost:4001"))
    check("guard: an empty endpoint is overridable",
          th.applies_to_default_endpoint(""))
    check("guard: the gateway itself is NOT overridden",
          not th.applies_to_default_endpoint("https://tokenharbor.ai/"))
    check("guard: another host is NOT overridden",
          not th.applies_to_default_endpoint("https://api.example.com"))


def test_selection_file_is_shared() -> None:
    """The selection file is the same one sources.py reads."""
    from zimzilla import sources as s, tokenharbour as th

    th.set_current("tokenharbour")
    check("selection: sources.py sees the tokenharbour choice",
          s.current_key() == "tokenharbour", str(s.current_key()))
    check("selection: tokenharbour sees it too",
          th.current_key() == "tokenharbour")


def test_no_key_is_echoed(tmp: Path) -> None:
    """The display value is a path, never the key itself."""
    from zimzilla import tokenharbour as th

    own = tmp / "own2" / "source"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text('export ANTHROPIC_AUTH_TOKEN="thk_live_secret00000000000000"\n')
    with mock.patch.object(th, "PROFILE", own):
        creds = th.load_credentials()
    origin = creds[2]
    check("display: the origin is a path, not a key",
          "thk_" not in origin, origin)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-th-test-"))
    # Pin the module's paths to the temp tree before anything imports it.
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    test_free_detection()
    test_fetch_is_total()
    test_cache_is_keyed_by_token()
    test_free_models_only()
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
