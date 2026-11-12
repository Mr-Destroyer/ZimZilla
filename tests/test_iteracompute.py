"""Regression suite for the IteraCompute source.

Run:  python tests/test_iteracompute.py   (from an activated venv)

Protects what is particular about this provider:

* **It is a local proxy, not a hosted gateway.** IteraCompute serves the OpenAI
  protocol only — measured 2026-10-19, ``POST /v1/messages`` answers
  ``404 edge_route_not_allowed`` — so the session talks to ``localhost:4002``
  and the proxy config is what makes it usable. ``is_iteracompute`` must match
  the proxy and must NOT match ``api.iteracompute.com``: a session aimed at the
  upstream is broken, and calling it a working source would hide that.
* **Free is the catalog's ``is_free`` flag, corroborated by a read 0/0 price.**
  A *missing* price is not free — the same trap the other gateway modules
  document.
* **Image entries are not chat models.** ``gpt-image-*`` and
  ``gemini-*-argon`` publish no ``context_window``; they are filtered on that,
  not on their names, so a new image model is excluded without an edit here.
* **The catalog's limits are used.** ``max_output_tokens`` is 8192 on the free
  model while ``Config.max_tokens`` is 32768, so the agent's ceiling must come
  from the catalog — a request above the limit is one the upstream rejects.
* **The free tier's 128K cap beats the model's larger window.** The catalog
  advertises 262144; the docs put a Free account at 128K. An API key does not
  say which tier the account is on, so the conservative figure is reported.
* **The catalog is public**, so the free list works before the key is set.

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


#: Shaped like the live catalog: ``{"object": "list", "data": [...]}``, pricing
#: as string numbers, ``is_free`` on every entry.
CATALOG = {
    "object": "list",
    "data": [
        {
            "id": "qwen/qwen3.8-27b", "object": "model", "created": 0,
            "owned_by": "iteracompute",
            "reasoning": {"supported": True, "mandatory": False,
                          "supports_enable_disable": True,
                          "effort_support": "supported",
                          "supported_efforts": ["low", "medium", "xhigh"],
                          "default_effort": "xhigh"},
            "context_window": 262144, "max_output_tokens": 8192,
            "is_free": True,
            "pricing": {"currency": "USD", "unit": "per_1m_tokens",
                        "input": "0", "cached_input": "0",
                        "cache_write": None, "output": "0"},
        },
        {
            "id": "minimax/minimax-m2.5",
            "context_window": 204800, "max_output_tokens": 16384,
            "is_free": False,
            "pricing": {"currency": "USD", "unit": "per_1m_tokens",
                        "input": "0.27", "cached_input": "0.03",
                        "cache_write": None, "output": "1.03"},
        },
        {
            # Free but NOT chat: no context window, no output limit.
            "id": "gpt-image-2.5-sunburst",
            "context_window": None, "max_output_tokens": None,
            "is_free": True,
            "pricing": {"currency": "USD", "unit": "per_1m_tokens",
                        "input": "4", "cached_input": "1",
                        "cache_write": None, "output": "0"},
        },
        {
            # No pricing at all and no free marker: NOT free. This is the row
            # a missing-price-reads-as-zero bug would report as free.
            "id": "mystery/thing",
            "context_window": 32000, "max_output_tokens": 4096,
        },
        {
            # A genuine 0/0 read, not marked free: free by price.
            "id": "zero/thing",
            "context_window": 64000, "max_output_tokens": 2048,
            "is_free": False,
            "pricing": {"input": "0", "output": "0"},
        },
        "not-a-row",
        {"context_window": 1000},
        {"id": "   ", "is_free": True},
    ],
}

#: The chat-capable ids, in catalog order.
CHAT = ["qwen/qwen3.8-27b", "minimax/minimax-m2.5", "mystery/thing",
        "zero/thing"]


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


def _capture(payload=None):
    """A stub that records the request, so the URL and headers can be asserted."""
    seen: dict = {}

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())

    return seen, _open


def _clean_env():
    """Context manager that hides any IteraCompute credential in the environment."""
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

def test_free_is_the_flag_or_a_read_price() -> None:
    from zimzilla import iteracompute as ic

    models = ic._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("catalog: every well-formed row parsed", len(models) == 5, str(len(models)))
    check("free: is_free marks the free model", by_id["qwen/qwen3.8-27b"].free)
    check("free: a paid model is not free",
          not by_id["minimax/minimax-m2.5"].free)
    check("free: an unpriced model with no marker is NOT free",
          not by_id["mystery/thing"].free)
    check("free: a read 0/0 price is free", by_id["zero/thing"].free)

    got = [m.id for m in models if m.free]
    check("free: exactly the free set, in catalog order",
          got == ["qwen/qwen3.8-27b", "gpt-image-2.5-sunburst", "zero/thing"],
          str(got))


def test_priced_records_a_read() -> None:
    from zimzilla import iteracompute as ic

    by_id = {m.id: m for m in ic._parse_models(CATALOG)}
    check("priced: a real pair counts as priced",
          by_id["minimax/minimax-m2.5"].priced)
    check("priced: a free entry's 0/0 is a read", by_id["qwen/qwen3.8-27b"].priced)
    check("priced: an entry with no pricing is not priced",
          not by_id["mystery/thing"].priced)
    check("priced: pricing as string numbers parses",
          by_id["minimax/minimax-m2.5"].price_in == 0.27,
          str(by_id["minimax/minimax-m2.5"].price_in))


def test_image_entries_are_not_chat() -> None:
    """The window, not the name, is what decides chat capability."""
    from zimzilla import iteracompute as ic

    by_id = {m.id: m for m in ic._parse_models(CATALOG)}
    check("chat: a text model is chat", by_id["qwen/qwen3.8-27b"].chat)
    check("chat: an image entry with no window is not chat",
          not by_id["gpt-image-2.5-sunburst"].chat)

    with mock.patch("urllib.request.urlopen", _stub()):
        ic.clear_cache()
        ids, err = ic.model_ids()
    check("chat: model_ids excludes the image entry",
          ids is not None and "gpt-image-2.5-sunburst" not in ids, str(ids))
    check("chat: model_ids keeps every chat model",
          ids is not None and set(CHAT) <= set(ids), str(ids))


def test_free_tier_context_is_conservative() -> None:
    """The catalog says 262144; the free account tier is 128K. Use 128K."""
    from zimzilla import iteracompute as ic

    by_id = {m.id: m for m in ic._parse_models(CATALOG)}
    free = by_id["qwen/qwen3.8-27b"]
    check("context: the catalog value is kept unclamped",
          free.catalog_context == 262144, str(free.catalog_context))
    check("context: a free model reports the free-tier cap",
          free.context_length == ic.FREE_CONTEXT_CAP, str(free.context_length))
    check("context: a paid model keeps the catalog figure",
          by_id["minimax/minimax-m2.5"].context_length == 204800,
          str(by_id["minimax/minimax-m2.5"].context_length))
    check("context: the free tier cap is 128K", ic.FREE_CONTEXT_CAP == 131072,
          str(ic.FREE_CONTEXT_CAP))


def test_output_ceiling_comes_from_the_catalog() -> None:
    """The free model allows 8192 output; Config.max_tokens is 32768."""
    from zimzilla import iteracompute as ic
    from zimzilla import sources as s
    from zimzilla.config import Config
    from zimzilla.agent import Agent

    with mock.patch("urllib.request.urlopen", _stub()):
        ic.clear_cache()
        ic.fetch_models()
        got = s.max_output_for(ic.DEFAULT_BASE_URL, "qwen/qwen3.8-27b")
    check("ceiling: the catalog's max_output_tokens is read", got == 8192, str(got))
    check("ceiling: the harness default is larger — the reason this matters",
          Config().max_tokens > 8192, str(Config().max_tokens))
    check("ceiling: an unknown model has none",
          s.max_output_for(ic.DEFAULT_BASE_URL, "nope") is None)
    check("ceiling: a cold catalog has none",
          s.max_output_for("https://api.example.com", "qwen/qwen3.8-27b") is None)

    # The reasoning-retry path distinguishes the two: `output_ceiling()` is the
    # size to request (falling back to the config), `published_output_ceiling()`
    # is the hard cap. Conflating them capped a retry at the number it was
    # trying to escape, so a model with no published limit could never retry —
    # measured as 8 failures in tests/test_deliverable.py.
    from zimzilla.config import Config as Cfg

    def _agent(model, base):
        cfg = Cfg()
        cfg.base_url, cfg.model = base, model
        return Agent(cfg=cfg)

    with mock.patch("urllib.request.urlopen", _stub()):
        ic.clear_cache()
        ic.fetch_models()
        a = _agent("qwen/qwen3.8-27b", ic.DEFAULT_BASE_URL)
        check("ceiling: the agent sizes a turn from the catalog",
              a.output_ceiling() == 8192, str(a.output_ceiling()))
        check("ceiling: and knows that limit is published",
              a.published_output_ceiling() == 8192, str(a.published_output_ceiling()))

    b = _agent("deepseek-v4.1-flash", "http://localhost:4001")
    check("ceiling: an unpublished model sizes from the config",
          b.output_ceiling() == b.cfg.max_tokens, str(b.output_ceiling()))
    check("ceiling: but reports no published cap, so a retry may raise it",
          b.published_output_ceiling() is None,
          str(b.published_output_ceiling()))


def test_catalog_price_beats_the_estimate_table() -> None:
    """``qwen/qwen3.8-27b`` is not a key in PRICING, so the table would bill it."""
    from zimzilla import iteracompute as ic
    from zimzilla import sources as s
    from zimzilla.config import DEFAULT_PRICE, price_for

    check("price: the estimate table does not know the gateway's id",
          price_for("qwen/qwen3.8-27b") == DEFAULT_PRICE,
          str(price_for("qwen/qwen3.8-27b")))

    with mock.patch("urllib.request.urlopen", _stub()):
        ic.clear_cache()
        ic.fetch_models()
        live = s.catalog_price_for(ic.DEFAULT_BASE_URL, "qwen/qwen3.8-27b")
        paid = s.catalog_price_for(ic.DEFAULT_BASE_URL, "minimax/minimax-m2.5")
    check("price: the free model is free from the catalog", live == (0.0, 0.0),
          str(live))
    check("price: a paid model gets the gateway's own rate",
          paid == (0.27, 1.03), str(paid))


def test_catalog_is_public_and_the_url_is_right() -> None:
    seen, opener = _capture()
    from zimzilla import iteracompute as ic

    with mock.patch("urllib.request.urlopen", opener):
        ic.clear_cache()
        models, err = ic.fetch_models()

    check("catalog: it fetches without a key", models is not None and err == "",
          err)
    check("catalog: the URL is the public Models endpoint",
          seen.get("url") == "https://api.iteracompute.com/v1/models",
          str(seen.get("url")))
    check("catalog: it is a GET", seen.get("method") == "GET")
    check("catalog: no Authorization header without a key",
          "authorization" not in seen.get("headers", {}),
          str(seen.get("headers")))

    seen2, opener2 = _capture()
    with mock.patch("urllib.request.urlopen", opener2):
        ic.clear_cache()
        ic.fetch_models("test-token-abcdef")
    check("catalog: a supplied key is sent as a bearer token",
          seen2.get("headers", {}).get("authorization") == "Bearer test-token-abcdef",
          str(seen2.get("headers")))


def test_fetch_is_total() -> None:
    """A network failure is returned, never raised."""
    from zimzilla import iteracompute as ic

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        ic.clear_cache()
        models, err = ic.fetch_models()
    check("fetch: a connection failure returns None and an error",
          models is None and "iteracompute" in err, repr(err))

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.HTTPError(
                        "u", 401, "no", {}, None))):
        ic.clear_cache()
        models, err = ic.fetch_models()
    check("fetch: a 401 names the setup command",
          models is None and "/iteracompute-api-setup" in err, repr(err))

    with mock.patch("urllib.request.urlopen", _stub(payload={"data": "junk"})):
        ic.clear_cache()
        models, err = ic.fetch_models()
    check("fetch: a malformed payload is an empty catalog, not a crash",
          models == [] and err == "", repr(models))


def test_cache_is_keyed_and_expires() -> None:
    from zimzilla import iteracompute as ic

    with mock.patch("urllib.request.urlopen", _stub()):
        ic.clear_cache()
        check("cache: cold is None", ic.cached_models() is None)
        ic.fetch_models()
        check("cache: warm has models", ic.cached_models() is not None)
        check("cache: cached_ids filters image entries",
              "gpt-image-2.5-sunburst" not in (ic.cached_ids() or []))

        # Age the entry past the TTL; it must read as None, not as stale-live.
        ic._CACHE[""] = (ic._CACHE[""][0], ic._CACHE[""][1] - ic.CACHE_TTL - 1)
        check("cache: an expired entry reads as None", ic.cached_models() is None)
        check("cache: but the age is still reported",
              (ic.catalog_age() or 0) > ic.CACHE_TTL, str(ic.catalog_age()))
        ic.clear_cache()


def test_is_iteracompute_matches_only_the_proxy() -> None:
    """The upstream has no Messages endpoint; matching it would be a lie."""
    from zimzilla import iteracompute as ic

    check("endpoint: the local proxy matches",
          ic.is_iteracompute("http://localhost:4002"))
    check("endpoint: 127.0.0.1 matches too",
          ic.is_iteracompute("http://127.0.0.1:4002"))
    check("endpoint: the upstream does NOT match",
          not ic.is_iteracompute("https://api.iteracompute.com/v1"))
    check("endpoint: another local port does not match",
          not ic.is_iteracompute("http://localhost:4001"))
    check("endpoint: empty is not a match", not ic.is_iteracompute(""))

    from zimzilla import sources as s
    check("endpoint: sources identifies it by port",
          s.active_key("http://localhost:4002") == "iteracompute",
          str(s.active_key("http://localhost:4002")))
    check("endpoint: sources does not identify the upstream",
          s.active_key("https://api.iteracompute.com/v1") is None)


def test_profile_roundtrip(tmp: Path) -> None:
    from zimzilla import iteracompute as ic

    with _clean_env():
        path = ic.save_key("ic_test_key_1234567890", "qwen/qwen3.8-27b")
        check("profile: written under ZIMZILLA_HOME",
              path == tmp / ".zimzilla" / "iteracompute" / "source", str(path))
        check("profile: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
              oct(path.stat().st_mode & 0o777))
        check("profile: parent is 700",
              oct(path.parent.stat().st_mode & 0o777) == "0o700",
              oct(path.parent.stat().st_mode & 0o777))
        text = path.read_text()
        check("profile: points at the local proxy, not the upstream",
              'ANTHROPIC_BASE_URL="http://localhost:4002"' in text)
        check("profile: it does not contain the upstream URL",
              "api.iteracompute.com" not in text)

        creds = ic.load_credentials()
        check("profile: the key loads back",
              creds is not None and creds[0] == "ic_test_key_1234567890",
              repr(creds and creds[0][:0]))
        check("profile: the model loads back",
              creds is not None and creds[1] == "qwen/qwen3.8-27b")
        check("profile: has_key agrees", ic.has_key())

        path.unlink()


def test_credential_precedence(tmp: Path) -> None:
    from zimzilla import iteracompute as ic

    with _clean_env():
        check("creds: nothing set is None", ic.load_credentials() is None)
        ic.save_key("ic_from_profile_0001")
        check("creds: the own profile is used",
              ic.load_credentials()[2] == str(ic.PROFILE),
              str(ic.load_credentials()[2]))

        os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:4002"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "ic_from_env_0002"
        check("creds: the environment wins when aimed at the proxy",
              ic.load_credentials()[0] == "ic_from_env_0002")
        check("creds: and it is named as the environment",
              ic.load_credentials()[2] == "environment")

        # A token in the environment aimed elsewhere is not this source's.
        os.environ["ANTHROPIC_BASE_URL"] = "https://api.example.com"
        check("creds: a token for another endpoint is ignored",
              ic.load_credentials()[0] == "ic_from_profile_0001")
        os.environ.pop("ANTHROPIC_BASE_URL")
        os.environ.pop("ANTHROPIC_AUTH_TOKEN")

        ic.PROFILE.unlink()


def test_looks_like_key() -> None:
    from zimzilla import iteracompute as ic

    check("key: a plausible token passes", ic.looks_like_key("ic_abc123456789XYZ"))
    check("key: a short string fails", not ic.looks_like_key("short"))
    check("key: whitespace fails", not ic.looks_like_key("two words here now"))
    check("key: a whole export line fails",
          not ic.looks_like_key('export ANTHROPIC_AUTH_TOKEN="abc"'))
    check("key: slashes fail", not ic.looks_like_key("provider/model-name"))


def test_sources_registry_dispatch() -> None:
    from zimzilla import iteracompute as ic
    from zimzilla import sources as s

    with mock.patch("urllib.request.urlopen", _stub()):
        ic.clear_cache()
        check("registry: a cold cache is None",
              s.models_for(ic.DEFAULT_BASE_URL) is None)
        check("registry: and no catalog models either",
              s.catalog_models("iteracompute") is None)

        ic.fetch_models()
        ids = s.models_for(ic.DEFAULT_BASE_URL)
        check("registry: a warm cache gives the ids",
              ids is not None and "qwen/qwen3.8-27b" in ids, str(ids))
        check("registry: the image entry is not offered",
              ids is not None and "gpt-image-2.5-sunburst" not in ids)
        check("registry: catalog_models returns the Model objects",
              len(s.catalog_models("iteracompute") or []) == 5)
        check("registry: an unknown key is still None",
              s.catalog_models("nope") is None)
        win = s.context_window_for(ic.DEFAULT_BASE_URL, "qwen/qwen3.8-27b")
        check("registry: the context window is the free-tier cap",
              win == ic.FREE_CONTEXT_CAP, str(win))


def test_discovery_needs_a_profile() -> None:
    """The service and config ship; without the operator's key there is no route."""
    from zimzilla import sources as s

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "zimzilla"
        home.mkdir()
        (zhome / "iteracompute").mkdir(parents=True)
        for name in ("start-litellm.sh", "litellm-config.yaml"):
            (zhome / "iteracompute" / name).write_text("# test\n")

        with mock.patch.object(s, "DEFAULT_HOME", home), \
             mock.patch.object(s, "ZIMZILLA_HOME", zhome):
            check("discovery: no profile means no source",
                  s.discover().get("iteracompute") is None)
            (zhome / "iteracompute" / "source").write_text(
                'export ANTHROPIC_AUTH_TOKEN="x"\n')
            src = s.discover().get("iteracompute")
            check("discovery: with a profile it appears", src is not None)
            if src is not None:
                check("discovery: it owns port 4002", src.port == 4002,
                      str(src.port))
                check("discovery: its base URL is the local proxy",
                      src.base_url == "http://localhost:4002", src.base_url)
                check("discovery: its fallback names the free model",
                      "qwen/qwen3.8-27b" in src.models, str(src.models))
                check("discovery: it is not parked",
                      src.under_construction is None)


def test_proxy_config_agrees_with_the_module() -> None:
    """The yaml and the module must not drift apart on the upstream or the id."""
    config = (ROOT / "packaging" / "iteracompute" / "litellm-config.yaml").read_text()
    from zimzilla import iteracompute as ic

    check("proxy: the config points at the same upstream",
          ic.API_BASE_URL in config, ic.API_BASE_URL)
    check("proxy: the free model is registered",
          "model_name: qwen/qwen3.8-27b" in config)
    check("proxy: the id is forwarded verbatim, with no added prefix",
          "model: openai/qwen/qwen3.8-27b" in config)
    check("proxy: a wildcard route exists so new models need no release",
          'model_name: "*"' in config)
    check("proxy: the wildcard forwards the requested name",
          "model: openai/*" in config)
    check("proxy: the master key is the profile's token",
          "master_key: os.environ/ANTHROPIC_AUTH_TOKEN" in config)
    # Load-bearing, and measured: litellm 1.105.1 sends an `openai/`
    # deployment's /v1/messages to /v1/responses unless this is set. The
    # gateway documents Chat Completions only, so the default is the one path
    # least likely to work — a turn would fail with the catalog still healthy.
    check("proxy: Messages are routed to Chat Completions, not Responses",
          "use_chat_completions_url_for_anthropic_messages: true" in config,
          "litellm's default would send /v1/responses")

    service = (ROOT / "packaging" / "iteracompute" / "start-litellm.sh").read_text()
    check("proxy: the manager defaults to :4002", 'PORT="${LITELLM_PORT:-4002}"' in service,
          "")
    check("proxy: it has its own pidfile",
          "/tmp/litellm-itera.pid" in service)
    check("proxy: it has its own log", "/tmp/litellm-itera.log" in service)


def test_tui_surfaces() -> None:
    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp_src = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    main_src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    launcher = (ROOT / "packaging" / "zimzilla").read_text(encoding="utf-8")
    setup = (ROOT / "setup.sh").read_text(encoding="utf-8")

    for cmd in ("zim-iteracompute", "iteracompute-api-setup",
                "iteracompute-models"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}":' in app_src)
        check(f"tui: /{cmd} is in the busy-ok set", f'"{cmd}"' in app_src)
        check(f"tui: /{cmd} is offered by completion", f"/{cmd}" in comp_src)

    check("tui: the gateway table knows it",
          'if kind == "iteracompute":' in app_src
          and 'return ic_mod, "IteraCompute"' in app_src)
    check("tui: the status pane lists it",
          '("iteracompute", ic_mod, "IteraCompute"' in app_src)
    check("tui: the model picker knows it",
          'if active == "iteracompute" else None' in app_src)
    check("tui: /model refetches for it", 'elif active == "iteracompute":' in app_src)
    # The set/list these two live in grows with each new gateway, so assert
    # membership rather than the exact closing spelling — the next source to be
    # added must not fail this suite.
    check("tui: /models accepts it", '"iteracompute"' in app_src)
    check("tui: the module is imported by the app",
          "from .. import iteracompute as ic_mod" in app_src)
    # Membership, not adjacency — see the same note in test_nvidia.py.
    check("tui: /models completion offers it", '"iteracompute"' in comp_src,
          "the /models source list must offer iteracompute")

    check("launcher: it starts the :4002 proxy",
          'LITELLM_PORT="${_port_override:-4002}"' in launcher)
    check("launcher: it sources the operator's profile",
          '"$ZIMZILLA_HOME/iteracompute/source"' in launcher)
    check("setup: it installs the route",
          'packaging/iteracompute/litellm-config.yaml' in setup)
    check("setup: it does not ship a credential",
          not (ROOT / "packaging" / "iteracompute" / "source").exists())

    # The launch-time restore loop applies hosted gateways only. IteraCompute
    # is restored by the launcher sourcing its profile, so it must NOT be in
    # that tuple — the module has no ``apply_to`` and the loop would crash.
    check("main: the restore loop is left to the launcher",
          '("iteracompute", ic_mod)' not in main_src)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-ic-test-"))
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
        test_free_is_the_flag_or_a_read_price()
        test_priced_records_a_read()
        test_image_entries_are_not_chat()
        test_free_tier_context_is_conservative()
        test_output_ceiling_comes_from_the_catalog()
        test_catalog_price_beats_the_estimate_table()
        test_catalog_is_public_and_the_url_is_right()
        test_fetch_is_total()
        test_cache_is_keyed_and_expires()
        test_is_iteracompute_matches_only_the_proxy()
        test_profile_roundtrip(tmp)
        test_credential_precedence(tmp)
        test_looks_like_key()
        test_sources_registry_dispatch()
        test_discovery_needs_a_profile()
        test_proxy_config_agrees_with_the_module()
        test_tui_surfaces()
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
