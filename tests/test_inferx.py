"""Regression suite for the InferX source.

Run:  ~/.local/share/zimzilla/.venv/bin/python tests/test_inferx.py

Protects what is particular about this provider:

* **The catalog is key-gated, and that makes the probe the key check.** Unlike
  NVIDIA's public Models route, ``GET /endpoints/v1/models`` answers 401 with
  no credential *and* with a wrong one, so the switch's probe is a real
  credential test and ``/inferx-models`` needs the key before it can list
  anything. A 401 must name ``/inferx-api-setup``, not report a bare status.
* **There is no per-model price.** The public catalog page has price columns
  and leaves them empty, so ``free`` is the key's *tenant allowance*, exactly
  as NVIDIA's is its developer allowance — and ``sources.catalog_price_for``
  must answer (0, 0) *even on a cold cache*. Returning None there would fall
  through to the estimate table and bill a model at ``DEFAULT_PRICE``, the
  discrepancy the function exists to prevent.
* **The context window comes from the provider's own catalog page.** The API
  sends no window at all, so the table carries the figures that were read and
  ``DEFAULT_CONTEXT`` (256K, the modal value) answers for the rest — never
  ``None``, because the harness default is 1M and guessing high is the
  unrecoverable direction.
* **A spent quota is not a rejected key.** Measured: after a first successful
  turn the endpoint answered ``429 service failure: tenant … quota exceeded``
  while the Models route kept answering 200. ``explain_error`` has to keep
  those apart, because one calls for a new key and the other for waiting.
* **The key never ships.** ``~/.zimzilla/inferx/source`` is written by
  ``/inferx-api-setup``, and nothing under ``packaging/`` contains a credential.
* **A remembered selection survives a restart.** Without the launcher branch a
  session last used on InferX falls through to the Logfare profile, so the
  launcher would source the wrong endpoint and the session would come up on
  the wrong provider with no indication.

No test here touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

import io
import json
import os
import re
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


#: Shaped like the live catalog: ``{"object": "list", "data": [...]}`` with the
#: four fields the endpoint actually sends — no price, no window, no capability
#: flags. That sparseness is the point; a fixture richer than the real payload
#: would let a test pass on data the endpoint never sends.
CATALOG = {
    "object": "list",
    "data": [
        {"id": "Agents-A1", "object": "model", "created": 1793878405,
         "owned_by": "inferx"},
        {"id": "deepseek-v4.1-flash-1017", "object": "model",
         "created": 1793878405, "owned_by": "inferx"},
        {"id": "gpt-oss-20b", "object": "model", "created": 1793878405,
         "owned_by": "inferx"},
        {"id": "glm-5.3", "object": "model", "created": 1793878405,
         "owned_by": "inferx"},
        # --- a family that must be filtered if it ever appears ---------------
        {"id": "inferx/bge-m3-embedding", "object": "model",
         "owned_by": "inferx"},
        # --- rows that must be dropped by the parser, not the filter --------
        "not-a-row",
        {"no_id": True},
        {"id": "   "},
    ],
}

#: Every chat-capable id, catalog order preserved.
CHAT = ["Agents-A1", "deepseek-v4.1-flash-1017", "gpt-oss-20b", "glm-5.3"]


class _Resp(io.BytesIO):
    """A urlopen() result: a context manager that also reports a status."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub(payload=None, error=None):
    def _open(req, timeout=None):
        if error is not None:
            raise error
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())
    return _open


def _capture(payload=None, error=None):
    """A stub that records the request, so URL, method and headers can be asserted."""
    seen: dict = {}

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = req.data
        if error is not None:
            raise error
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())

    return seen, _open


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "u", code, "err", {}, io.BytesIO(body.encode()))


def _clean_env():
    """Context manager that hides any InferX credential in the environment."""
    class _C:
        def __enter__(self):
            self.saved = {k: os.environ.pop(k, None) for k in
                          ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                           "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")}
            return self

        def __exit__(self, *exc):
            for k, v in self.saved.items():
                if v is not None:
                    os.environ[k] = v
            return False
    return _C()


# ---------------------------------------------------------------------------

def test_parser_drops_malformed_rows() -> None:
    from zimzilla import inferx as ix

    models = ix._parse_models(CATALOG)
    ids = [m.id for m in models]
    check("parse: every well-formed row kept", len(models) == 5, str(len(models)))
    check("parse: a bare string is dropped", "not-a-row" not in ids)
    check("parse: the well-formed ids survive in catalog order",
          ids == CHAT + ["inferx/bge-m3-embedding"], str(ids))
    check("parse: a whitespace id is dropped", "   " not in ids)
    check("parse: owned_by is carried", models[0].owned_by == "inferx",
          models[0].owned_by)
    check("parse: created is carried", models[0].created == 1793878405,
          str(models[0].created))
    check("parse: a payload with no data is an empty catalog",
          ix._parse_models({"object": "list"}) == [])
    check("parse: a junk data field is an empty catalog, not a crash",
          ix._parse_models({"data": "junk"}) == [])


def test_non_chat_families_are_filtered() -> None:
    """Marker-based, so an embedding model beside the chat ones is not offered."""
    from zimzilla import inferx as ix

    by_id = {m.id: m for m in ix._parse_models(CATALOG)}
    check("chat: a text LLM is chat", by_id["glm-5.3"].chat)
    check("chat: the default is chat", by_id["deepseek-v4.1-flash-1017"].chat)
    check("chat: an embedding id is not chat",
          not by_id["inferx/bge-m3-embedding"].chat)

    with mock.patch("urllib.request.urlopen", _stub()):
        ix.clear_cache()
        ids, err = ix.model_ids("ix_test-key-abcdefghijklmnop")
    check("chat: model_ids keeps exactly the chat set",
          ids is not None and ids == CHAT, str(ids))
    check("chat: model_ids reports no error", err == "", err)
    check("chat: an embedding model is not offered",
          ids is not None and "inferx/bge-m3-embedding" not in ids)


def test_free_is_the_tenant_quota() -> None:
    """The catalog publishes no price, so the allowance is the only true answer."""
    from zimzilla import inferx as ix
    from zimzilla import sources as s

    models = ix._parse_models(CATALOG)
    check("free: every entry is free on the tenant allowance",
          all(m.free for m in models))
    check("price: price_for says (0, 0), not None",
          ix.price_for("gpt-oss-20b", models) == (0.0, 0.0),
          str(ix.price_for("gpt-oss-20b", models)))
    check("price: an unknown id is still None",
          ix.price_for("nope", models) is None)

    with mock.patch("urllib.request.urlopen", _stub()):
        ix.clear_cache()
        ix.fetch_models("ix_test-key-abcdefghijklmnop")
        warm = s.catalog_price_for(ix.API_BASE_URL, "gpt-oss-20b")
        ix.clear_cache()
        cold = s.catalog_price_for(ix.API_BASE_URL, "gpt-oss-20b")
    check("price: the registry reports a quota model as free", warm == (0.0, 0.0),
          str(warm))
    check("price: and it does so on a COLD cache too", cold == (0.0, 0.0),
          str(cold))
    check("price: the estimate table would have said otherwise",
          __import__("zimzilla.config", fromlist=["x"]).price_for(
              "gpt-oss-20b") != (0.0, 0.0))


def test_context_is_never_none() -> None:
    """Measured windows from the provider's page; 256K for anything it has not listed."""
    from zimzilla import inferx as ix

    by_id = {m.id: m for m in ix._parse_models(CATALOG)}
    check("context: the default model reports its published 1M window",
          by_id["deepseek-v4.1-flash-1017"].context_length == 1_048_576,
          str(by_id["deepseek-v4.1-flash-1017"].context_length))
    check("context: gpt-oss-20b reports its published 20k window",
          by_id["gpt-oss-20b"].context_length == 20_000,
          str(by_id["gpt-oss-20b"].context_length))
    check("context: an unlisted id gets DEFAULT_CONTEXT",
          ix.Model("brand-new-model-the-catalog-has-never-seen").context_length
          == ix.DEFAULT_CONTEXT,
          str(ix.DEFAULT_CONTEXT))
    check("context: DEFAULT_CONTEXT is 256K, not the harness's 1M",
          ix.DEFAULT_CONTEXT == 262_144, str(ix.DEFAULT_CONTEXT))
    check("context: it is never None",
          all(isinstance(m.context_length, int) for m in by_id.values()))
    check("context: every id in the declared fallback has a real figure",
          all(mid in ix.CONTEXT_LENGTHS for mid in ix.CONTEXT_LENGTHS))
    check("blurb: it names the allowance and the window",
          "tenant quota" in by_id["glm-5.3"].blurb
          and "1048k ctx" in by_id["glm-5.3"].blurb,
          by_id["glm-5.3"].blurb)


def test_catalog_is_key_gated_and_the_url_is_right() -> None:
    seen, opener = _capture()
    from zimzilla import inferx as ix

    with mock.patch("urllib.request.urlopen", opener):
        ix.clear_cache()
        models, err = ix.fetch_models("ix_test-key-abcdefghijklmnop")

    check("catalog: a keyed fetch succeeds",
          models is not None and err == "", err)
    check("catalog: the URL is the tenant Models endpoint",
          seen.get("url") == "https://model.inferx.net/endpoints/v1/models",
          str(seen.get("url")))
    check("catalog: it is a GET", seen.get("method") == "GET")
    check("catalog: the key is sent as a bearer token",
          seen.get("headers", {}).get("authorization")
          == "Bearer ix_test-key-abcdefghijklmnop", str(seen.get("headers")))
    check("catalog: a User-Agent is sent",
          seen.get("headers", {}).get("user-agent") == "zimzilla",
          str(seen.get("headers", {}).get("user-agent")))

    seen2, opener2 = _capture(error=_http_error(401, '{"detail": "invalid auth token"}'))
    with mock.patch("urllib.request.urlopen", opener2):
        ix.clear_cache()
        models2, err2 = ix.fetch_models("")
    check("catalog: the keyless fetch is refused, as the endpoint does",
          models2 is None and "401" in err2, repr(err2))
    check("catalog: and the refusal names the setup command",
          "/inferx-api-setup" in err2, repr(err2))
    # `_switch_direct` refuses a switch on "HTTP 401" in the detail string, so
    # this wording is what stops a bad key from landing on a route that would
    # 401 on the first turn.
    check("catalog: the refusal carries the HTTP status the switch keys on",
          "HTTP 401" in err2, repr(err2))


def test_fetch_is_total() -> None:
    """A network failure is returned, never raised."""
    from zimzilla import inferx as ix

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        ix.clear_cache()
        models, err = ix.fetch_models("ix_test-key-abcdefghijklmnop")
    check("fetch: a connection failure returns None and an error",
          models is None and "model.inferx.net" in err, repr(err))

    with mock.patch("urllib.request.urlopen", _stub(error=_http_error(500, "boom"))):
        ix.clear_cache()
        models, err = ix.fetch_models("ix_test-key-abcdefghijklmnop")
    check("fetch: a 500 is reported with its status",
          models is None and "500" in err, repr(err))

    with mock.patch("urllib.request.urlopen", _stub(payload={"data": "junk"})):
        ix.clear_cache()
        models, err = ix.fetch_models("ix_test-key-abcdefghijklmnop")
    check("fetch: a malformed payload is an empty catalog, not a crash",
          models == [] and err == "", repr(models))


def test_cache_is_keyed_and_expires() -> None:
    from zimzilla import inferx as ix

    with mock.patch("urllib.request.urlopen", _stub()):
        ix.clear_cache()
        check("cache: cold is None", ix.cached_models() is None)
        check("cache: cold has no age", ix.catalog_age() is None)
        ix.fetch_models("ix_test-key-abcdefghijklmnop")
        check("cache: warm has models", ix.cached_models() is not None)
        check("cache: cached_ids filters non-chat entries",
              "inferx/bge-m3-embedding" not in (ix.cached_ids() or []))
        check("cache: cached_ids keeps chat entries",
              ix.cached_ids() == CHAT, str(ix.cached_ids()))

        # Keyed by the token the fetch used: a second key is a second catalog.
        ix.fetch_models("ix_other-key-abcdefghijklmnop")
        check("cache: a keyed fetch is its own entry", len(ix._CACHE) == 2,
              str(len(ix._CACHE)))

        for k, (models, when) in list(ix._CACHE.items()):
            ix._CACHE[k] = (models, when - ix.CACHE_TTL - 1)
        check("cache: an expired entry reads as None", ix.cached_models() is None)
        check("cache: but the age is still reported",
              (ix.catalog_age() or 0) > ix.CACHE_TTL, str(ix.catalog_age()))
        ix.clear_cache()


def test_explain_error_shapes() -> None:
    """A spent quota and a rejected key call for different actions."""
    from zimzilla import inferx as ix

    check("explain: 401 blames the key",
          "/inferx-api-setup" in ix.explain_error(401, ""))
    check("explain: 403 blames the key too",
          "/inferx-api-setup" in ix.explain_error(403, ""))
    quota = ix.explain_error(
        429, "service failure: tenant tn-ngmkg9lao5 quota exceeded")
    check("explain: a 429 with the quota wording says the key is fine",
          "key itself is fine" in quota, quota)
    check("explain: and it points at the command that still answers",
          "/inferx-models" in quota, quota)
    plain = ix.explain_error(429, "slow down")
    check("explain: a plain 429 is a rate limit, not a quota",
          "rate limited" in plain and "key itself is fine" not in plain, plain)
    check("explain: 404 names the model, not the key",
          "not served" in ix.explain_error(404, "")
          and "key" not in ix.explain_error(404, ""))
    check("explain: a JSON detail is carried",
          "boom" in ix.explain_error(500, '{"detail": "boom"}'))
    check("explain: a plain-text body is carried",
          "service failure" in ix.explain_error(500,
                                                "service failure: whatever"))
    check("explain: an empty body still answers",
          ix.explain_error(500, "") == "HTTP 500", ix.explain_error(500, ""))


def test_looks_like_key() -> None:
    from zimzilla import inferx as ix

    check("key: an ix_ token passes",
          ix.looks_like_key("ix_" + "a" * 60))
    check("key: the prefix is NOT required", ix.looks_like_key("a" * 40))
    check("key: a short string fails", not ix.looks_like_key("short"))
    check("key: whitespace fails", not ix.looks_like_key("two words here now"))
    check("key: a whole export line fails",
          not ix.looks_like_key('export ANTHROPIC_AUTH_TOKEN="abc"'))
    check("key: a provider/model id fails",
          not ix.looks_like_key("deepseek-ai/DeepSeek-V4.1-Flash"))


def test_is_inferx_matches_by_host() -> None:
    """No port to match on, so this is a host test."""
    from zimzilla import inferx as ix
    from zimzilla import sources as s

    check("endpoint: the tenant endpoint matches",
          ix.is_inferx("https://model.inferx.net/endpoints/v1"))
    check("endpoint: a bare trailing slash matches",
          ix.is_inferx("https://model.inferx.net/endpoints/v1/"))
    check("endpoint: a tenant-specific deployment path matches too",
          ix.is_inferx("https://model.inferx.net/tenants/abc/v1"))
    check("endpoint: another provider does not match",
          not ix.is_inferx("https://api.inference.wandb.ai/v1"))
    check("endpoint: a local proxy does not match",
          not ix.is_inferx("http://localhost:4001"))
    check("endpoint: empty is not a match", not ix.is_inferx(""))
    check("endpoint: None is not a match", not ix.is_inferx(None))
    check("endpoint: the generic alias is the same test",
          ix.active_endpoint is ix.is_inferx)

    check("endpoint: sources identifies it by host",
          s.active_key("https://model.inferx.net/endpoints/v1") == "inferx",
          str(s.active_key("https://model.inferx.net/endpoints/v1")))
    check("endpoint: sources does not confuse it with wandb",
          s.active_key("https://api.inference.wandb.ai/v1") == "wandb")
    check("endpoint: the protocol heuristic calls it OpenAI-compatible",
          __import__("zimzilla.openai_compat", fromlist=["x"]).resolve_protocol(
              "https://model.inferx.net/endpoints/v1") == "openai")


def test_profile_roundtrip(tmp: Path) -> None:
    from zimzilla import inferx as ix

    with _clean_env():
        path = ix.save_key("ix_test-key-1234567890", "glm-5.3")
        check("profile: written under ZIMZILLA_HOME",
              path == tmp / ".zimzilla" / "inferx" / "source", str(path))
        check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
              oct(path.stat().st_mode & 0o777))
        check("profile: parent is 700",
              oct(path.parent.stat().st_mode & 0o777) == "0o700",
              oct(path.parent.stat().st_mode & 0o777))
        text = path.read_text()
        check("profile: it points at the tenant endpoint, with no proxy",
              'ANTHROPIC_BASE_URL="https://model.inferx.net/endpoints/v1"' in text,
              text.splitlines()[2] if len(text.splitlines()) > 2 else "")
        check("profile: the model is preserved",
              'ANTHROPIC_MODEL="glm-5.3"' in text)
        check("profile: ANTHROPIC_API_KEY is blanked so the token wins",
              'export ANTHROPIC_API_KEY=""' in text)

        creds = ix.load_credentials()
        check("profile: the key loads back",
              creds is not None and creds[0] == "ix_test-key-1234567890")
        check("profile: the model loads back",
              creds is not None and creds[1] == "glm-5.3")
        check("profile: the origin is the path",
              creds is not None and creds[2] == str(ix.PROFILE))
        check("profile: has_key agrees", ix.has_key())

        # A second save with no model must not blank the stored one.
        ix.save_key("ix_test-key-1234567890")
        check("profile: no model argument falls back to DEFAULT_MODEL",
              'ANTHROPIC_MODEL="%s"' % ix.DEFAULT_MODEL in path.read_text())

        path.unlink()


def test_credential_precedence(tmp: Path) -> None:
    from zimzilla import inferx as ix

    with _clean_env():
        check("creds: nothing set is None", ix.load_credentials() is None)
        ix.save_key("ix_from_profile_0001")
        check("creds: the own profile is used",
              ix.load_credentials()[2] == str(ix.PROFILE),
              str(ix.load_credentials()[2]))

        os.environ["ANTHROPIC_BASE_URL"] = "https://model.inferx.net/endpoints/v1"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "ix_from_env_0002"
        check("creds: the environment wins when aimed at InferX",
              ix.load_credentials()[0] == "ix_from_env_0002")
        check("creds: and it is named as the environment",
              ix.load_credentials()[2] == "environment")

        # A token in the environment aimed elsewhere is not this source's.
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.example.com"
        check("creds: a token for another endpoint is ignored",
              ix.load_credentials()[0] == "ix_from_profile_0001")
        os.environ.pop("ANTHROPIC_BASE_URL")
        os.environ.pop("ANTHROPIC_AUTH_TOKEN")

        ix.PROFILE.unlink()


def test_sources_registry_dispatch() -> None:
    from zimzilla import inferx as ix
    from zimzilla import sources as s

    check("registry: it is direct-only",
          "inferx" in s.DIRECT_ONLY, str(sorted(s.DIRECT_ONLY)))
    check("registry: it declares the tenant route",
          s.DECLARED_ROUTES["inferx"].base_url
          == "https://model.inferx.net/endpoints/v1",
          s.DECLARED_ROUTES["inferx"].base_url)
    check("registry: the id goes on the wire unchanged",
          s.DECLARED_ROUTES["inferx"].upstream_model("deepseek-v4.1-flash-1017")
          == "deepseek-v4.1-flash-1017")
    check("registry: the declared fallback is the measured catalog",
          ix.DEFAULT_MODEL in s.INFERX_MODELS
          and len(s.INFERX_MODELS) == len(ix.CONTEXT_LENGTHS),
          f"{len(s.INFERX_MODELS)} ids")
    check("registry: it is a built-in key, so a selection survives a restart",
          "inferx" in s.BUILTIN_KEYS)

    # Placement is load-bearing: a branch written *after* the generic
    # DIRECT_ONLY block is dead code, because that block returns first. Both
    # this one and wandb's were written there once, and their own module caches
    # were never read — so `/inferx-models` warmed a list `/model` could not see.
    import inspect
    body = inspect.getsource(s.models_for)
    check("registry: the InferX branch precedes the generic direct-only one",
          'if key == "inferx"' in body
          and body.index('if key == "inferx"') < body.index("if key in DIRECT_ONLY"))
    check("registry: wandb's branch is reachable too",
          body.index('if key == "wandb"') < body.index("if key in DIRECT_ONLY"))

    with mock.patch("urllib.request.urlopen", _stub()):
        ix.clear_cache()
        cold = s.models_for(ix.API_BASE_URL)
        check("registry: a cold cache falls back to the declared list",
              cold is not None and tuple(cold) == tuple(s.INFERX_MODELS),
              str(len(cold or ())))
        ix.fetch_models("ix_test-key-abcdefghijklmnop")
        warm = s.models_for(ix.API_BASE_URL)
        check("registry: a warm cache gives the chat ids",
              warm is not None and tuple(warm) == tuple(CHAT), str(warm))
        check("registry: the non-chat entries are not offered",
              warm is not None and "inferx/bge-m3-embedding" not in warm)
        check("registry: catalog_models returns Model objects",
              len(s.catalog_models("inferx") or []) == 5)
        check("registry: an unknown key is still None",
              s.catalog_models("nope") is None)


def test_discovery_needs_a_profile() -> None:
    """Without the operator's key there is no route — that is what makes the
    switch name /inferx-api-setup instead of failing at request time."""
    from zimzilla import sources as s

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "zimzilla"
        home.mkdir()
        zhome.mkdir()

        with mock.patch.object(s, "DEFAULT_HOME", home), \
             mock.patch.object(s, "ZIMZILLA_HOME", zhome):
            check("discovery: no profile means no source",
                  s.discover().get("inferx") is None)
            check("discovery: and it is not offered as a switch target",
                  "inferx" not in s.switchable())
            (zhome / "inferx").mkdir()
            (zhome / "inferx" / "source").write_text(
                'export ANTHROPIC_AUTH_TOKEN="ix_x"\n')
            src = s.discover().get("inferx")
            check("discovery: with a profile it appears", src is not None)
            if src is not None:
                check("discovery: it owns no port", src.port == 0, str(src.port))
                check("discovery: its base URL is the tenant endpoint",
                      src.base_url == "https://model.inferx.net/endpoints/v1",
                      src.base_url)
                check("discovery: it is direct-only", src.is_direct_only)
                check("discovery: it is not parked",
                      src.under_construction is None)
                check("discovery: its fallback is the measured catalog",
                      "deepseek-v4.1-flash-1017" in src.models, str(src.models[:2]))
            check("discovery: and now it IS offered as a switch target",
                  "inferx" in s.switchable())


def test_tui_surfaces() -> None:
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    agent_src = (ROOT / "zimzilla" / "agent.py").read_text(encoding="utf-8")

    for cmd in ("zim-inferx", "inferx-api-setup", "inferx-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app_src)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp_src)
        check(f"tui: /{cmd} is documented in /help", f"/{cmd}" in app_src)
        check(f"tui: /{cmd} is in the command palette", f'"{cmd}"' in app_src)

    check("tui: the gateway table knows it",
          'return ix_mod, "InferX"' in app_src)
    check("tui: the status pane lists it",
          '("inferx", ix_mod, "InferX", "/inferx-api-setup")' in app_src)
    check("tui: the status pane prints direct, not a port",
          '_kind in ("nvidia", "inferx")' in app_src)
    check("tui: the model picker reads its catalog",
          'if active == "inferx" else None' in app_src)
    check("tui: /model refetches for it", 'elif active == "inferx":' in app_src)
    check("tui: /model prices it from the catalog",
          "ix_mod.price_for(name, ix_catalog)" in app_src)
    check("tui: /model names the allowance when the catalog is cold",
          'return "free (tenant quota)"' in app_src)
    check("tui: /models accepts it", '"inferx"' in app_src)
    check("tui: /models dispatches to its chat list",
          'return ix_mod.chat_models(creds[0] if creds else "")' in app_src)
    check("tui: /models labels the listing", '"inferx": "INFERX"' in app_src)
    check("tui: the startup warm-up runs for it",
          "self._warm_inferx_catalog()" in app_src)
    check("tui: the switch reads the catalogue through its own module",
          'models, err = ix_mod.fetch_models(token or "")' in app_src)
    check("tui: the module is imported by the app",
          "from .. import inferx as ix_mod" in app_src)
    check("tui: /models completion offers it",
          '"wandb", "inferx"' in comp_src)
    check("tui: a 401 hint names the InferX command",
          "/inferx-api-setup <ix_ key>" in app_src)
    check("tui: the inside-session switch refuses with the setup command",
          "/inferx-api-setup <ix_ key> (a tenant key from" in app_src)
    check("agent: the environment note lists the profile",
          '("inferx", "InferX", "ANTHROPIC_AUTH_TOKEN")' in agent_src)
    check("sources: the host is a known Chat Completions host",
          '"model.inferx.net"' in
          (ROOT / "zimzilla" / "openai_compat.py").read_text(encoding="utf-8"))

    # InferX is direct-only, so the launch-time restore loop — which applies
    # hosted gateways — must not list it: the module has no ``apply_to``.
    main_src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    check("main: the restore loop is not used for it",
          '("inferx", ix_mod)' not in main_src)


def test_launcher_restores_the_route() -> None:
    """A remembered InferX selection must survive a restart.

    Without its own branch the launcher takes the Logfare path: it sources the
    Logfare profile, so a session last used on InferX would come up on Logfare
    with no indication — the same failure the NVIDIA branch documents.
    """
    launcher = (ROOT / "packaging" / "zimzilla").read_text(encoding="utf-8")

    check("launcher: it has its own branch",
          'if [[ "$_current_source" == "inferx" ]]' in launcher)
    check("launcher: it sources the operator's profile",
          '_ix_profile="$ZIMZILLA_HOME/inferx/source"' in launcher)
    check("launcher: it turns the proxy off",
          launcher.index('_ix_profile="$ZIMZILLA_HOME/inferx/source"')
          < launcher.index("ZIMZILLA_NO_PROXY=1",
                           launcher.index('_ix_profile="$ZIMZILLA_HOME/inferx/source"')),
          "the branch must set ZIMZILLA_NO_PROXY=1")
    check("launcher: it has no shipped profile to fall back to",
          "packaging/inferx/source" not in launcher)
    # Guarded on the profile being readable, so a missing key falls through to
    # Logfare rather than dying fail-closed on an unfinished route.
    start = launcher.index('if [[ "$_current_source" == "inferx" ]]')
    end = launcher.index("LITELLM_ENV_FILE=", start)
    branch = launcher[start:end]
    check("launcher: the branch is guarded on the profile existing",
          'if [[ -r "$_ix_profile" ]]' in branch, branch.strip()[:200])


def test_nothing_ships_a_credential() -> None:
    pkg = ROOT / "packaging"
    leaked = [str(p.relative_to(ROOT)) for p in pkg.rglob("*")
              if p.is_file() and re.search(r"ix_[A-Za-z0-9]{20,}", _safe_text(p))]
    check("packaging: no InferX key ships in the tree", not leaked, str(leaked))
    check("packaging: no inferx profile ships",
          not (pkg / "inferx" / "source").exists())

    # And the module must not embed one either: the prefix appears in the
    # docstrings and comments on purpose, but never as a *key*.
    mod_src = (ROOT / "zimzilla" / "inferx.py").read_text(encoding="utf-8")
    pasted = re.findall(r"ix_[A-Za-z0-9]{20,}", mod_src)
    check("module: no literal ix_ key in the source", not pasted,
          f"a key is pasted into zimzilla/inferx.py: {len(pasted)} match(es)")


def _safe_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-ix-test-"))
    # Pin the module's paths to the temp tree before anything imports it.
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "ANTHROPIC_MODEL")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        test_parser_drops_malformed_rows()
        test_non_chat_families_are_filtered()
        test_free_is_the_tenant_quota()
        test_context_is_never_none()
        test_catalog_is_key_gated_and_the_url_is_right()
        test_fetch_is_total()
        test_cache_is_keyed_and_expires()
        test_explain_error_shapes()
        test_looks_like_key()
        test_is_inferx_matches_by_host()
        test_profile_roundtrip(tmp)
        test_credential_precedence(tmp)
        test_sources_registry_dispatch()
        test_discovery_needs_a_profile()
        test_tui_surfaces()
        test_launcher_restores_the_route()
        test_nothing_ships_a_credential()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

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
