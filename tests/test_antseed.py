"""Regression suite for the AntSeed source.

Run:  python tests/test_antseed.py   (from an activated venv)

Protects what is particular about this provider. Every one of these is a
measured behaviour of the live network, not a guess:

* **The pin is the feature.** AntSeed's router ranks by price *and* trust, and
  its ``minTrustScore`` defaults to 60 while free peers sit below it. So a free
  model requested by its bare name answers
  ``502 model_not_found: No policy-allowed peer currently serves …`` even though
  the peer is online. The network's own ``<peerId>@<model>`` syntax bypasses the
  ranking — measured 200 where the bare name gave 502. ``free_model_ids``
  therefore hands back *pinned* ids, and this suite asserts that, because
  returning bare names would produce a list that looks right and fails on use.
* **Free is a property of a peer, not a model.** There is no ``:free`` suffix and
  no free tag — the only signal AntSeed publishes is the price on each seller.
  A model is free when at least one peer charges 0 in *and* 0 out, and a peer
  free in but paid out is not free at all.
* **The catalog is OpenAI-shaped but must not be parsed as OpenAI's.** It has
  ``{"object": "list", "data": [...]}``, yet all the pricing lives in a nested
  ``peers`` array. A shared parser would appear to work and then report every
  model as free, because an entry with no peers parses to 0/0.
* **The endpoint is identified by port, not by name.** The buyer hands out a
  plain ``http://localhost:8377`` with no "antseed" in it, and the two LiteLLM
  proxies are on 4000/4001 — so the port is the only thing that distinguishes
  them, and a regression there would silently mislabel the active source.
* **There is no key.** ``looks_like_key`` is False for everything, and a fetch
  sends no credential. That is the honest answer, not a stub.

No test here touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


# Shaped like the real response: `object`/`data`, every entry carrying a nested
# `peers` array whose members hold the pricing. The cases that matter:
#   - one free peer, high trust            → free and pinnable
#   - two free peers, one higher-trust     → the better one is pinned
#   - a free peer that is free-in/paid-out → NOT free
#   - no free peer at all                  → not free, not pinnable
#   - an entry with no peers               → must NOT read as free
CATALOG = {
    "object": "list",
    "data": [
        {
            "id": "glm-4.5-flash", "name": "GLM 4.5 Flash", "type": "text",
            "aliases": ["glm-4.5-flash", "glm45flash"],
            "supported_protocols": ["openai-chat-completions"],
            "capabilities": {"contextWindow": 128000, "maxOutputTokens": 16384,
                             "toolUse": True},
            "peers": [
                {"peerId": "aaaa1111", "displayName": "refound",
                 "provider": "openai", "protocol": "openai-chat-completions",
                 "inputUsdPerMillion": 0, "outputUsdPerMillion": 0,
                 "reputationScore": None, "effectiveReputationScore": None},
                {"peerId": "bbbb2222", "displayName": "Crimson",
                 "provider": "openai", "protocol": "openai-chat-completions",
                 "inputUsdPerMillion": 0, "outputUsdPerMillion": 0,
                 "effectiveReputationScore": 44.58},
            ],
        },
        {
            "id": "ling-3.1-flash", "name": "Ling 3.1 Flash", "type": "text",
            "supported_protocols": ["openai-chat-completions"],
            "capabilities": {"contextWindow": 262144, "toolUse": True,
                             "reasoning": True},
            "peers": [
                {"peerId": "cccc3333", "displayName": "Relay Works",
                 "protocol": "anthropic-messages",
                 "inputUsdPerMillion": 0, "outputUsdPerMillion": 0,
                 "effectiveReputationScore": 65.22},
            ],
        },
        {
            "id": "half-free", "name": "Half Free", "type": "text",
            "supported_protocols": ["openai-chat-completions"],
            "peers": [
                {"peerId": "dddd4444", "displayName": "Sneaky",
                 "protocol": "openai-chat-completions",
                 "inputUsdPerMillion": 0, "outputUsdPerMillion": 3.5,
                 "effectiveReputationScore": 70.0},
            ],
        },
        {
            # Two free peers, and the lower-trust one is the Anthropic one: the
            # only entry that can show protocol support outranking trust.
            # The live network has no such peer (every seller advertises OpenAI
            # only), so without this fixture that ordering rule is untested.
            "id": "dual-free", "name": "Dual Free", "type": "text",
            "supported_protocols": ["openai-chat-completions",
                                    "anthropic-messages"],
            "peers": [
                {"peerId": "ffff6666", "displayName": "OpenAI Only",
                 "protocol": "openai-chat-completions",
                 "inputUsdPerMillion": 0, "outputUsdPerMillion": 0,
                 "effectiveReputationScore": 80.0},
                {"peerId": "gggg7777", "displayName": "Speaks Anthropic",
                 "protocol": "anthropic-messages",
                 "inputUsdPerMillion": 0, "outputUsdPerMillion": 0,
                 "effectiveReputationScore": 20.0},
                # A paid seller alongside the free ones, so the blurb has a
                # genuine "N of M" to render — all-free is the easy case.
                {"peerId": "hhhh8888", "displayName": "Costs Money",
                 "protocol": "openai-chat-completions",
                 "inputUsdPerMillion": 1.5, "outputUsdPerMillion": 6.0,
                 "effectiveReputationScore": 90.0},
            ],
        },
        {
            "id": "claude-fable-5", "name": "Claude Fable 5", "type": "text",
            "supported_protocols": ["openai-chat-completions",
                                    "anthropic-messages"],
            "capabilities": {"contextWindow": 200000, "toolUse": True},
            "peers": [
                {"peerId": "eeee5555", "displayName": "Best",
                 "protocol": "openai-chat-completions",
                 "inputUsdPerMillion": 0.3449, "outputUsdPerMillion": 1.1199,
                 "effectiveReputationScore": 63.77},
            ],
        },
        {
            "id": "no-peers", "name": "Nobody Serves This", "type": "text",
            "supported_protocols": [],
            "peers": [],
        },
    ],
}


class _Resp:
    """A urlopen result good enough for the code under test."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_urlopen(payload=None, error: Exception | None = None):
    """A urlopen replacement serving *payload* (or raising *error*)."""
    def _open(req, timeout=None):
        if error is not None:
            raise error
        body = json.dumps(payload if payload is not None else CATALOG).encode()
        return _Resp(body)
    return _open


def _url_of(req) -> str:
    """The URL out of whatever urlopen was handed.

    ``fetch_models`` passes a bare URL *string* rather than a ``Request``
    object — there is no header to attach, because there is no credential.
    Accepting both shapes keeps these stubs working either way.
    """
    return req.full_url if hasattr(req, "full_url") else str(req)


def _headers_of(req) -> dict:
    """The headers urlopen was handed, lowercased — empty for a bare URL."""
    if not hasattr(req, "headers"):
        return {}
    return {k.lower(): v for k, v in req.headers.items()}


# ---------------------------------------------------------------------------
# free detection
# ---------------------------------------------------------------------------

def test_free_is_a_peer_property() -> None:
    """Free is decided by the price on a seller, not by a tag or a suffix."""
    from zimzilla import antseed as a

    models = a._parse_models(CATALOG)
    by_id = {m.id: m for m in models}

    check("free: a model with a zero-priced peer is free",
          by_id["glm-4.5-flash"].free)
    check("free: the free peers are the zero-priced ones",
          len(by_id["glm-4.5-flash"].free_peers) == 2)
    check("free: a paid model is not free",
          not by_id["claude-fable-5"].free)
    check("free: free-in but paid-out is NOT free",
          not by_id["half-free"].free,
          "0 in / 3.5 out must not read as free")
    check("free: a model nobody serves is not free",
          not by_id["no-peers"].free)
    check("free: an entry with no peers does not read as free",
          by_id["no-peers"].peers == ())
    # The distinction that a shared OpenAI parser would get wrong: an entry with
    # no pricing of its own is not thereby free.
    check("free: pricing is read from peers, not from the entry",
          a._parse_models({"data": [{"id": "x"}]})[0].free is False)


def test_prices_are_per_million_floats() -> None:
    """AntSeed quotes per-million numbers; nothing is scaled on the way in."""
    from zimzilla import antseed as a

    by_id = {m.id: m for m in a._parse_models(CATALOG)}
    c = by_id["claude-fable-5"].cheapest
    check("price: kept as per-million, unscaled",
          c is not None and c.price_in == 0.3449 and c.price_out == 1.1199,
          str((c.price_in, c.price_out) if c else None))
    check("price: the cheapest peer is the one quoted",
          c is not None and c.peer_id == "eeee5555")
    check("price: a free model prices at zero",
          a.price_for("glm-4.5-flash", list(by_id.values())) == (0.0, 0.0))
    # A malformed price must not take the catalog down, and must not invent a
    # free model: both directions have to be zero.
    odd = a._parse_models({"data": [{"id": "odd", "peers": [
        {"peerId": "z", "inputUsdPerMillion": "not-a-number",
         "outputUsdPerMillion": 5}]}]})[0]
    check("price: a malformed price degrades to 0, not to an exception",
          odd.peers[0].price_in == 0.0)
    check("price: a malformed price does not make a model free",
          not odd.free, "0 in / 5 out is paid")


# ---------------------------------------------------------------------------
# the pin — the whole point of this module
# ---------------------------------------------------------------------------

def test_pin_composes_peer_at_model() -> None:
    """The pin is `<peerId>@<model>`, which is what makes a free model route."""
    from zimzilla import antseed as a

    by_id = {m.id: m for m in a._parse_models(CATALOG)}
    pin = by_id["glm-4.5-flash"].pin()
    check("pin: composes <peerId>@<model>",
          pin is not None and pin.endswith("@glm-4.5-flash")
          and pin.split("@")[0] in ("aaaa1111", "bbbb2222"), str(pin))
    check("pin: the bare model name is NOT returned",
          pin != "glm-4.5-flash",
          "a bare name 502s against the router's trust floor")
    check("pin: a paid model has no pin",
          by_id["claude-fable-5"].pin() is None)
    check("pin: a model with no free peer has no pin",
          by_id["half-free"].pin() is None)


def test_pin_prefers_trust_then_anthropic() -> None:
    """Among free peers, Anthropic support wins, then reputation."""
    from zimzilla import antseed as a

    by_id = {m.id: m for m in a._parse_models(CATALOG)}
    # glm-4.5-flash has a None-reputation peer and a 44.58 one: take the higher.
    check("pin: the higher-trust free peer is chosen",
          by_id["glm-4.5-flash"].pin() == "bbbb2222@glm-4.5-flash",
          str(by_id["glm-4.5-flash"].pin()))
    # A peer advertising anthropic-messages outranks one that does not, even at
    # a *lower* trust — measured: one free peer rejects Anthropic outright.
    check("pin: an anthropic-capable peer outranks a higher-trust one",
          by_id["dual-free"].pin() == "gggg7777@dual-free",
          f"{by_id['dual-free'].pin()} (80-trust OpenAI peer must lose to "
          f"the 20-trust Anthropic one)")
    # The highest-trust seller of all is a *paid* one, and it must still lose:
    # the pin's entire job is to reach a free seller, so trust only ever ranks
    # peers that are already free. A regression here would silently start
    # spending money on a model the operator picked because it was free.
    check("pin: a higher-trust PAID seller never wins the pin",
          by_id["dual-free"].pin() != "hhhh8888@dual-free"
          and "hhhh8888" not in (by_id["dual-free"].pin() or ""),
          f"90-trust paid seller must not be pinned: "
          f"{by_id['dual-free'].pin()}")
    check("pin: serves_anthropic reads the peer protocol",
          by_id["ling-3.1-flash"].free_peers[0].serves_anthropic)
    check("pin: a model's own protocol list is separate from its peers'",
          by_id["claude-fable-5"].serves_anthropic
          and not by_id["claude-fable-5"].peers[0].serves_anthropic)
    # The rule is a preference, never a filter: with no Anthropic peer at all,
    # the best OpenAI-only free peer is still pinned. Every free peer on the
    # live network is in this branch, so it is the one that actually runs.
    glm = by_id["glm-4.5-flash"]
    check("pin: no free peer advertises Anthropic here",
          not any(p.serves_anthropic for p in glm.free_peers))
    check("pin: an OpenAI-only free peer is still pinned",
          glm.pin() == "bbbb2222@glm-4.5-flash", str(glm.pin()))


def test_blurb_says_which_count_is_the_free_one() -> None:
    """`free · 4 of 9 sellers` — the counts must not read as a contradiction.

    "peer" and "seller" are the same thing in AntSeed's vocabulary, so a blurb
    reading "4 peers · 9 sellers" leaves the operator unable to tell which
    number is the free one. Every free model here is pinnable, so the free
    count is the one that matters.
    """
    from zimzilla import antseed as a

    by_id = {m.id: m for m in a._parse_models(CATALOG)}
    # All sellers free → a single count, no ratio.
    check("blurb: all-free reads as one count",
          by_id["glm-4.5-flash"].blurb.startswith("free · 2 sellers"),
          by_id["glm-4.5-flash"].blurb)
    # Some free → the ratio, free first. This is the case that used to render
    # as "2 peers · 2 sellers" — two numbers, no way to tell them apart.
    check("blurb: part-free reads as 'N of M sellers'",
          by_id["dual-free"].blurb.startswith("free · 2 of 3 sellers"),
          by_id["dual-free"].blurb)
    # A paid model quotes a price and never says "free".
    paid = by_id["claude-fable-5"].blurb
    check("blurb: a paid model quotes a price, not 'free'",
          "$0.3449/$1.1199 per Mtok" in paid and "free" not in paid, paid)
    check("blurb: a paid model still says how many sellers",
          "1 seller" in paid, paid)
    # Singular/plural must not read "1 sellers".
    one = by_id["ling-3.1-flash"].blurb
    check("blurb: one seller is singular",
          "1 seller" in one and "1 sellers" not in one, one)
    check("blurb: the word 'peers' is gone from the free blurb",
          "peer" not in by_id["glm-4.5-flash"].blurb,
          "two words for one thing is what made it ambiguous")


def test_a_pin_bypasses_ranking_not_the_seller() -> None:
    """A pin gets you to the peer; it does not make that peer answer.

    Measured across the eight free pins on the live network: one 200, one 429,
    one ``502 "pinned peer could not complete"``, one ``400
    unsupported_protocol``. So this module must hand back a *set* of free pins
    and order them sensibly, never present one as guaranteed — and must not
    quietly drop the peers that are merely untried. The set is what the picker
    shows; a single "the free model" would be a promise the network cannot keep.
    """
    from zimzilla import antseed as a

    a.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        ids, _ = a.free_model_ids(use_cache=False)
    check("pin: every free model is offered, not just the best one",
          ids is not None and len(ids) == 3,
          f"{len(ids or [])} pins — a pin can 429 or 502, so all are offered")
    check("pin: no duplicate pins",
          ids is not None and len(set(ids)) == len(ids))
    # The pins carry the peer that was chosen, so a failure names a peer the
    # operator can see and re-pick rather than an opaque model id.
    check("pin: each pin names both the peer and the model",
          all(i.count("@") == 1 and i.split("@")[0] and i.split("@")[1]
              for i in (ids or [])))
    check("pin: a pin is not the bare model id",
          all(i.split("@")[1] not in i or i != i.split("@")[1]
              for i in (ids or [])),
          "a bare id 502s against the router's trust floor")


def test_free_model_ids_are_pinned() -> None:
    """`free_model_ids` must return pinned ids — bare ones would 502."""
    from zimzilla import antseed as a

    a.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        ids, err = a.free_model_ids(use_cache=False)
    check("free ids: the fetch succeeds", err == "" and ids is not None, err)
    check("free ids: every id is pinned",
          bool(ids) and all("@" in i for i in ids), str(ids))
    check("free ids: the paid models are excluded",
          not any("claude-fable-5" in i for i in (ids or [])))
    check("free ids: the half-free model is excluded",
          not any("half-free" in i for i in (ids or [])))
    check("free ids: sorted set matches the free models",
          sorted(i.split("@")[1] for i in ids) == ["dual-free",
                                                   "glm-4.5-flash",
                                                   "ling-3.1-flash"],
          str(ids))


def test_usable_free_models_is_the_pinnable_set() -> None:
    """`/models antseed` reads this, so a free-but-unpinnable model is out."""
    from zimzilla import antseed as a

    a.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        free, _ = a.free_models(use_cache=False)
    a.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen()):
        usable, err = a.usable_free_models(use_cache=False)
    check("usable: every usable model is pinnable",
          usable is not None and all(m.pin() for m in usable), err)
    check("usable: it is a subset of the free list",
          {m.id for m in (usable or [])} <= {m.id for m in (free or [])})
    check("usable: the count matches the pinnable free models",
          len(usable or []) == 3, str([m.id for m in (usable or [])]))


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def test_catalog_needs_no_key() -> None:
    """The buyer authenticates nothing; a fetch must send no credential."""
    from zimzilla import antseed as a

    a.clear_cache()
    seen = {}

    def capture(req, timeout=None):
        seen["headers"] = _headers_of(req)
        seen["url"] = _url_of(req)
        seen["bare"] = not hasattr(req, "headers")
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", capture):
        models, err = a.fetch_models(use_cache=False)
    check("keyless: a fetch with no key succeeds",
          err == "" and models is not None and len(models) == 6, err)
    check("keyless: hits /v1/models on the buyer",
          seen.get("url", "").endswith("/v1/models"), seen.get("url"))
    check("keyless: sends NO credential",
          "authorization" not in seen.get("headers", {})
          and "x-api-key" not in seen.get("headers", {}),
          str(seen.get("headers")))
    check("keyless: the request is a bare URL, so there is nowhere to put one",
          seen.get("bare") is True, "no Request object means no headers")
    check("keyless: looks_like_key is False for everything",
          not a.looks_like_key("antseed_abc") and not a.looks_like_key(""))


def test_base_url_is_the_prefix_before_v1() -> None:
    """The SDK appends /v1/messages — a base carrying /v1 would 404."""
    from zimzilla import antseed as a

    check("base: DEFAULT_BASE_URL has no /v1",
          "/v1" not in a.DEFAULT_BASE_URL, a.DEFAULT_BASE_URL)
    check("base: it is the buyer's documented ANTHROPIC_BASE_URL",
          a.DEFAULT_BASE_URL == "http://localhost:8377")
    a.clear_cache()
    seen = {}

    def capture(req, timeout=None):
        seen["url"] = _url_of(req)
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", capture):
        a.fetch_models(use_cache=False)
    check("base: the catalog URL is base + /v1/models",
          seen.get("url") == "http://localhost:8377/v1/models", seen.get("url"))


def test_fetch_is_total() -> None:
    """Every failure is a message, never an exception."""
    from zimzilla import antseed as a
    import urllib.error

    for label, err in [
        ("unreachable", urllib.error.URLError("connection refused")),
        ("404", urllib.error.HTTPError("u", 404, "nf", {}, None)),
        ("500", urllib.error.HTTPError("u", 500, "err", {}, None)),
        ("garbage", None),
    ]:
        a.clear_cache()
        if label == "garbage":
            def bad(req, timeout=None):
                return _Resp(b"not json at all")
            ctx = mock.patch("urllib.request.urlopen", bad)
        else:
            ctx = mock.patch("urllib.request.urlopen", _stub_urlopen(error=err))
        with ctx:
            models, message = a.fetch_models(use_cache=False)
        check(f"total: {label} returns None and a reason",
              models is None and bool(message), message[:60])

    a.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub_urlopen({"object": "list",
                                                             "data": []})):
        models, err = a.fetch_models(use_cache=False)
    check("total: an empty catalog is [] and not an error",
          models == [] and err == "", repr(err))
    check("total: a non-dict payload yields [] rather than raising",
          a._parse_models([]) == [] and a._parse_models({}) == [])


def test_cache_is_keyed_by_endpoint() -> None:
    """Two endpoints must not share one cache slot."""
    from zimzilla import antseed as a

    a.clear_cache()
    calls = {"n": 0}

    def counting(req, timeout=None):
        calls["n"] += 1
        return _Resp(json.dumps(CATALOG).encode())

    with mock.patch("urllib.request.urlopen", counting):
        a.fetch_models(use_cache=True)
        a.fetch_models(use_cache=True)
    check("cache: a second fetch is served from the cache", calls["n"] == 1,
          f"calls={calls['n']}")

    with mock.patch("urllib.request.urlopen", counting):
        a.fetch_models("http://localhost:9999", use_cache=True)
    check("cache: a different endpoint is fetched separately", calls["n"] == 2,
          f"calls={calls['n']}")
    a.clear_cache()
    check("cache: clearing empties it", a.cached_models() is None)


# ---------------------------------------------------------------------------
# endpoint identification
# ---------------------------------------------------------------------------

def test_is_antseed_by_port_not_name() -> None:
    """The buyer's URL contains no 'antseed' — the port is the identifier."""
    from zimzilla import antseed as a

    check("ident: the plain buyer URL is recognised",
          a.is_antseed("http://localhost:8377"))
    check("ident: 127.0.0.1 is recognised too",
          a.is_antseed("http://127.0.0.1:8377"))
    check("ident: a tunnel host is recognised",
          a.is_antseed("https://abc.antseed.com"))
    check("ident: the LiteLLM proxies are NOT antseed",
          not a.is_antseed("http://localhost:4001")
          and not a.is_antseed("http://localhost:4000"),
          "port 4000/4001 belong to Logfare and Token Juice")
    check("ident: an unrelated host is not antseed",
          not a.is_antseed("https://tokenharbor.ai"))
    check("ident: empty and None are not antseed",
          not a.is_antseed("") and not a.is_antseed(None))
    check("ident: active_endpoint is the same test",
          a.active_endpoint is a.is_antseed)


def test_active_key_does_not_confuse_sources() -> None:
    """sources.active_key must route each port to the right provider."""
    from zimzilla import sources as s

    check("sources: 8377 is antseed",
          s.active_key("http://localhost:8377") == "antseed")
    check("sources: 4001 is logfare",
          s.active_key("http://localhost:4001") == "logfare")
    check("sources: 4000 is tokenjuice",
          s.active_key("http://localhost:4000") == "tokenjuice")
    check("sources: antseed is a builtin key, so a selection survives",
          "antseed" in s.BUILTIN_KEYS)
    check("sources: models_for returns None on a cold cache",
          s.models_for("http://localhost:8377") is None)


def test_proxy_port_comes_from_the_buyer_config(tmp) -> None:
    """The port is read from AntSeed's own config, with a sane fallback."""
    from zimzilla import antseed as a

    fake_home = tmp / "antseed-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(a, "ANTSEED_HOME", fake_home):
        check("port: falls back when there is no config",
              a.proxy_port() == a.DEFAULT_PORT, str(a.proxy_port()))
        (fake_home / "config.json").write_text(
            json.dumps({"buyer": {"proxyPort": 9999}}), encoding="utf-8")
        check("port: reads buyer.proxyPort from the config",
              a.proxy_port() == 9999, str(a.proxy_port()))
        (fake_home / "config.json").write_text("{ not json", encoding="utf-8")
        check("port: a malformed config falls back rather than raising",
              a.proxy_port() == a.DEFAULT_PORT)
        (fake_home / "config.json").write_text(
            json.dumps({"buyer": {"proxyPort": 999999}}), encoding="utf-8")
        check("port: an out-of-range port is rejected",
              a.proxy_port() == a.DEFAULT_PORT)


# ---------------------------------------------------------------------------
# the remembered pin
# ---------------------------------------------------------------------------

def test_profile_roundtrip(tmp) -> None:
    """The preference file round-trips, is mode 600, and holds no key."""
    from zimzilla import antseed as a

    own = tmp / "profile" / "source"
    with mock.patch.object(a, "PROFILE", own):
        check("profile: nothing remembered initially", a.load_pin() is None)
        path = a.save_pin("glm-4.5-flash", "bbbb2222")
        check("profile: saved to the expected path", path == own)
        check("profile: mode is 600", oct(own.stat().st_mode)[-3:] == "600",
              oct(own.stat().st_mode)[-3:])
        check("profile: parent dir is 700",
              oct(own.parent.stat().st_mode)[-3:] == "700",
              oct(own.parent.stat().st_mode)[-3:])
        check("profile: round-trips the model and peer",
              a.load_pin() == ("glm-4.5-flash", "bbbb2222"), str(a.load_pin()))
        text = own.read_text(encoding="utf-8")
        check("profile: the file says it is not a credential",
              "not a credential" in text.lower())
        check("profile: it records the base url for a shell to source",
              'export ANTHROPIC_BASE_URL="http://localhost:8377"' in text)
        # A model with no peer must still read back.
        a.save_pin("solo-model", "")
        check("profile: a model with no peer round-trips",
              a.load_pin() == ("solo-model", ""))


def test_apply_and_guard(tmp) -> None:
    """apply_to points the config here; the guard leaves other hosts alone."""
    from zimzilla import antseed as a
    from zimzilla.config import Config

    cfg = Config(base_url="http://localhost:4001", model="deepseek-v4.1-flash")
    with mock.patch.object(a, "reachable", lambda *x, **k: False):
        check("apply: no buyer means no switch",
              a.apply_to(cfg) is None
              and cfg.base_url == "http://localhost:4001",
              "a switch to a dead proxy must not report success")
        check("creds: no buyer means no credentials",
              a.load_credentials() is None)

    own = tmp / "apply" / "source"
    with mock.patch.object(a, "PROFILE", own), \
         mock.patch.object(a, "reachable", lambda *x, **k: True):
        applied = a.apply_to(cfg)
        check("apply: points at the buyer", cfg.base_url == "http://localhost:8377")
        check("apply: sets a non-empty token so the harness will start",
              bool(cfg.auth_token), "api_key_present must be True")
        check("apply: the token is the documented placeholder",
              cfg.auth_token == a.NO_KEY_PLACEHOLDER, cfg.auth_token)
        check("apply: api_key is cleared, not left as another provider's",
              cfg.api_key == "")
        check("apply: returns the origin", applied is not None
              and applied[0] == "antseed", str(applied))
        creds = a.load_credentials()
        check("creds: returns the same three-tuple shape as the gateways",
              creds is not None and len(creds) == 3, str(creds))
        check("creds: origin says where it came from",
              creds is not None and creds[2] == "local buyer", str(creds))

        # A remembered pin is composed into the model.
        a.save_pin("glm-4.5-flash", "bbbb2222")
        a.apply_to(cfg)
        check("apply: a remembered pin is composed into the model",
              cfg.model == "bbbb2222@glm-4.5-flash", cfg.model)

    check("guard: the buyer's own endpoint is not overridden",
          not a.applies_to_default_endpoint("http://localhost:8377"))
    check("guard: the default and local proxies may be overridden",
          a.applies_to_default_endpoint("") is True
          and a.applies_to_default_endpoint("http://localhost:4001") is True)
    check("guard: a deliberate remote endpoint is left alone",
          not a.applies_to_default_endpoint("https://tokenharbor.ai"))


def test_selection_file_is_shared() -> None:
    """One selection file, so /zim-antseed and /zim-source agree."""
    from zimzilla import antseed as a, sources as s

    original = s.current_key()
    try:
        a.set_current("antseed")
        check("selection: set_current writes the shared file",
              s.current_key() == "antseed", str(s.current_key()))
        check("selection: current_key reads it back through the module",
              a.current_key() == "antseed")
    finally:
        if original:
            s.set_current(original)
        else:
            try:
                s._state_file().unlink()
            except OSError:
                pass


def test_price_for_unwraps_a_pin() -> None:
    """A pinned model still prices, so /cost is not blank on a free run."""
    from zimzilla import antseed as a

    models = a._parse_models(CATALOG)
    check("price_for: a bare free model prices at zero",
          a.price_for("glm-4.5-flash", models) == (0.0, 0.0))
    check("price_for: a pinned free model prices at zero",
          a.price_for("bbbb2222@glm-4.5-flash", models) == (0.0, 0.0))
    check("price_for: a pinned paid model prices at the cheapest peer",
          a.price_for("eeee5555@claude-fable-5", models) == (0.3449, 1.1199))
    check("price_for: an unknown model prices as None",
          a.price_for("nope", models) is None)
    check("price_for: no catalog prices as None",
          a.price_for("glm-4.5-flash", None) is None)


def test_no_key_is_echoed(tmp) -> None:
    """Nothing this module writes or returns looks like a secret."""
    from zimzilla import antseed as a

    own = tmp / "nokey" / "source"
    with mock.patch.object(a, "PROFILE", own):
        a.save_pin("glm-4.5-flash", "bbbb2222")
        text = own.read_text(encoding="utf-8")
    for marker in ("antseed_", "sk-", "zk_", "Bearer "):
        check(f"nokey: the profile contains no {marker!r}", marker not in text)
    check("nokey: the placeholder is not key-shaped",
          not a.NO_KEY_PLACEHOLDER.startswith(("sk-", "antseed_")),
          a.NO_KEY_PLACEHOLDER)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-antseed-test-"))
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    test_free_is_a_peer_property()
    test_prices_are_per_million_floats()
    test_pin_composes_peer_at_model()
    test_pin_prefers_trust_then_anthropic()
    test_blurb_says_which_count_is_the_free_one()
    test_a_pin_bypasses_ranking_not_the_seller()
    test_free_model_ids_are_pinned()
    test_usable_free_models_is_the_pinnable_set()
    test_catalog_needs_no_key()
    test_base_url_is_the_prefix_before_v1()
    test_fetch_is_total()
    test_cache_is_keyed_by_endpoint()
    test_is_antseed_by_port_not_name()
    test_active_key_does_not_confuse_sources()
    test_proxy_port_comes_from_the_buyer_config(tmp)
    test_profile_roundtrip(tmp)
    test_apply_and_guard(tmp)
    test_selection_file_is_shared()
    test_price_for_unwraps_a_pin()
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
