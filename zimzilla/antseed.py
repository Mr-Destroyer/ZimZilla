"""AntSeed — the P2P marketplace, reached through its local buyer proxy.

AntSeed is not a hosted gateway like TokenHarbour or OpenRouter. It is a peer
to peer network: independent sellers advertise models, and a **buyer** process
you run locally routes to them. That buyer exposes a normal HTTP API on
``localhost:8377`` and speaks both OpenAI and Anthropic, so ZimZilla talks to
it exactly as it talks to the LiteLLM proxies — with one difference that shapes
this whole module.

What was measured on the live network (antseed 0.1.169, 2026-10-14):

* **It is an Anthropic endpoint.** ``antseed buyer start`` prints
  ``export ANTHROPIC_BASE_URL=http://localhost:8377``, and ``POST
  /v1/messages`` answers with a real Anthropic body —
  ``{"type":"message","content":[{"type":"text",...}],"stop_reason":"end_turn"}``.
  So ``DEFAULT_BASE_URL`` is the prefix *before* ``/v1``, like every other
  source here. Tool use survives the hop: ``stop_reason:"tool_use"`` with a
  proper ``tool_use`` block came back from a free peer.
* **No credential is required.** The proxy authenticated nothing: a request with
  no ``Authorization`` header at all returned 200. A placeholder bearer is also
  accepted, which is why :func:`apply_to` sets one — the harness refuses to
  start without ``api_key_present``, and an empty string would be a lie about a
  key that does not exist.
* **The catalog is OpenAI-shaped.** ``GET /v1/models`` returns
  ``{"object":"list","data":[...]}`` — 323 models when measured.
* **A model is not one offer, it is many.** Each entry carries a ``peers`` list:
  one record per seller serving that model, each with its own price and
  reputation. ``inputUsdPerMillion``/``outputUsdPerMillion`` are already
  per-million floats, so no scaling is needed on the way in.
* **Free means a free *peer*.** A model is free when at least one of its peers
  charges 0 in and 0 out. Eight free peers covered eight models when measured.
  The catalog *also* carries a ``categories`` list that sometimes contains
  ``"free"``, and it is not a substitute: 6 free peers were tagged and 7 more
  were priced 0/0 with no tag at all. Price is the reliable signal, so that is
  what this module reads; the tag is never consulted.
* **Every seller on the network advertises OpenAI, and none advertises
  Anthropic.** ``supported_protocols`` was ``["openai-chat-completions"]`` for
  all 323 models and ``protocols`` on every peer. The proxy translates anyway
  for most of them — the Anthropic request that worked above was served by a
  peer advertising OpenAI only — so an Anthropic client works here, but it is
  working through a translation, not a native protocol.
* **A short ``max_tokens`` is not a small answer.** The reasoning models spend
  the budget before emitting any text: at ``max_tokens: 32`` a 200 came back
  with ``content: []`` and ``stop_reason: "max_tokens"``, and the same request
  at 256 produced prose. The same happened on the peer's native
  ``/v1/chat/completions``, so it is the model's behaviour and not a
  translation loss — but it means a tiny budget looks like an empty reply.
  ZimZilla's default ``max_tokens`` is well clear of this.

The trap this module exists to solve
------------------------------------

The router will not send you to a free peer by default. It ranks by price *and*
trust, and its ``minTrustScore`` defaults to 60 while free peers cluster below
it — the free peers measured 18, 29, 44, 54, 54, 65, 72, and one with no score
at all. So ``glm-4.5-flash``, which is served free by three peers, answers

    502 {"code":"model_not_found",
         "message":"No policy-allowed peer currently serves model \"glm-4.5-flash\"."}

The fix is the network's own pinning syntax, ``<peerId>@<model>``, which
"explicit model pin" in the buyer's own startup banner documents as bypassing
the ranking. Measured: a composed pin against ``/v1/messages`` returned 200
where the bare name returned 502. The equivalent ``x-antseed-pin-peer`` header
also works, but the composed id needs no client change — ``base_url``, headers
and auth all stay exactly as they are — so that is what :func:`free_model_ids`
hands back.

A pin bypasses the ranking, not the seller. It gets the request to *that peer*,
and what that peer then does is its own business: measured across the eight
free pins, one answered 200, one 429, one ``502 "pinned peer could not
complete"``, and one ``400 unsupported_protocol``. So a pin is necessary and
not sufficient, and the picker orders peers by protocol support and trust
rather than promising a particular pin will work.

Nothing here echoes a key, and nothing here can spend money: a request that
would need credits comes back ``402 antseed_buyer_fault`` from the proxy before
this code sees a token.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ZIMZILLA_HOME = Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))

#: The buyer's own identity directory. It holds ``config.json`` (which names the
#: proxy port) and the node's wallet — read for the port, never for a secret.
ANTSEED_HOME = Path(os.environ.get("ANTSEED_DATA_DIR") or (Path.home() / ".antseed"))

#: Matched as a substring, so ``http://localhost:8377``, ``http://127.0.0.1:8377``
#: and a tunnel host all count. The port is what actually identifies it; see
#: :func:`is_antseed`.
HOST = "antseed"

#: The prefix *before* ``/v1`` — the SDK appends ``/v1/messages`` to this. The
#: buyer prints this exact value as ``ANTHROPIC_BASE_URL``.
DEFAULT_BASE_URL = "http://localhost:8377"

#: Where a remembered preference for this source is written. AntSeed needs no
#: key, so unlike the other gateways this profile holds only the chosen peer
#: pin and model — see :func:`save_pin`.
PROFILE = ZIMZILLA_HOME / "antseed" / "source"

#: A fetched catalog older than this is refetched rather than trusted. The peer
#: list churns as sellers come and go, so this is deliberately short.
CACHE_TTL = 120.0

#: The buyer's default proxy port, used when ``~/.antseed/config.json`` cannot be
#: read. Overridable at launch with ``antseed buyer start --port``.
DEFAULT_PORT = 8377

#: The protocol name AntSeed uses for Anthropic Messages. A seller that does not
#: advertise it rejects an Anthropic request with ``400 unsupported_protocol``.
ANTHROPIC_PROTOCOL = "anthropic-messages"

#: The protocol a *peer* advertises when it can serve one. Note the asymmetry:
#: a model's ``supported_protocols`` is what the network claims, a peer's
#: ``protocol`` is what that one seller actually speaks — and they disagree.
OPENAI_PROTOCOL = "openai-chat-completions"

#: What is sent as the bearer token. The buyer authenticates nothing, so this is
#: a marker that the harness has a credential-shaped value rather than a secret.
#: Deliberately not a key-shaped string, so any log scrubber treats it as the
#: non-secret it is.
NO_KEY_PLACEHOLDER = "antseed-local-no-key"


# ---------------------------------------------------------------------------
# the local buyer
# ---------------------------------------------------------------------------

def buyer_config() -> dict:
    """``~/.antseed/config.json`` as a dict, or ``{}`` when unreadable."""
    try:
        return json.loads((ANTSEED_HOME / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def proxy_port() -> int:
    """The port the local buyer is configured to listen on.

    Read from the buyer's own config rather than hardcoded, because
    ``antseed buyer start --port`` changes it and the port is the only thing
    that identifies this source. Falls back to :data:`DEFAULT_PORT` when the
    file is missing or malformed — a machine that has never run AntSeed still
    gets a sensible guess rather than an exception.
    """
    port = (buyer_config().get("buyer") or {}).get("proxyPort")
    if isinstance(port, int) and 0 < port < 65536:
        return port
    return DEFAULT_PORT


def is_antseed(base_url: str | None) -> bool:
    """Whether *base_url* points at a local AntSeed buyer proxy.

    Identified by the buyer's port, not by the word "antseed" — the URL the
    buyer hands out is plain ``http://localhost:8377`` and contains no such
    string. A ``*.antseed.com`` tunnel URL is matched too, for the case where
    the buyer has been exposed with ``antseed tunnel``.
    """
    url = (base_url or "").strip()
    if not url:
        return False
    if "antseed.com" in url:
        return True
    port = proxy_port()
    return any(f"{host}:{port}" in url for host in ("localhost", "127.0.0.1", "[::1]"))


#: Generic name for the same test, so code handling several gateways uniformly
#: can call ``mod.active_endpoint(url)`` without knowing which one it holds.
active_endpoint = is_antseed


def reachable(timeout: float = 1.5) -> bool:
    """Whether a buyer proxy is actually answering on the configured port.

    A cheap check the UI can make before offering AntSeed, since the proxy only
    exists while ``antseed buyer start`` is running.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{proxy_port()}/v1/models", timeout=timeout
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# the catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Peer:
    """One seller's offer of one model."""

    peer_id: str
    display_name: str = ""
    provider: str = ""
    protocol: str = ""
    price_in: float = 0.0
    price_out: float = 0.0
    reputation: float | None = None

    @property
    def free(self) -> bool:
        """Whether this seller charges nothing for the model.

        Both directions must be zero. A peer that is free in and paid out is a
        paid peer — it would still drain credits on a long answer.
        """
        return self.price_in == 0.0 and self.price_out == 0.0

    @property
    def serves_anthropic(self) -> bool:
        """Whether this seller advertises the Anthropic protocol.

        Sellers overwhelmingly advertise only ``openai-chat-completions``, and
        the proxy does translate for most of them — but not reliably: pinning
        ``qwen2.5-coder-7b`` returned ``400 unsupported_protocol`` on one
        measurement and 200 on another. So this is advisory, used to order the
        picker, never to hide a model.
        """
        return self.protocol == ANTHROPIC_PROTOCOL

    @property
    def blurb(self) -> str:
        bits = []
        if self.free:
            bits.append("free")
        else:
            bits.append(f"${self.price_in:g}/${self.price_out:g} per Mtok")
        if self.reputation is not None:
            bits.append(f"trust {self.reputation:.0f}")
        if self.display_name:
            bits.append(self.display_name)
        return " · ".join(bits)


@dataclass(frozen=True)
class Model:
    """One model on the network, with every seller offering it."""

    id: str
    label: str = ""
    aliases: tuple[str, ...] = ()
    context_length: int | None = None
    max_output_tokens: int | None = None
    tool_use: bool = False
    reasoning: bool = False
    model_type: str = "text"
    protocols: tuple[str, ...] = ()
    peers: tuple[Peer, ...] = field(default_factory=tuple)

    @property
    def free_peers(self) -> tuple[Peer, ...]:
        return tuple(p for p in self.peers if p.free)

    @property
    def free(self) -> bool:
        """Whether *any* seller offers this model for nothing.

        This is the whole definition of a free model here. There is no tag and
        no suffix — the price on a peer is the only signal AntSeed publishes.
        """
        return bool(self.free_peers)

    @property
    def serves_anthropic(self) -> bool:
        """Whether the network claims Anthropic support for this model."""
        return ANTHROPIC_PROTOCOL in self.protocols

    @property
    def cheapest(self) -> Peer | None:
        """The peer the router would most plausibly pick, ignoring trust.

        Used for the price shown in the picker: quoting the cheapest seller is
        the honest answer to "what does this model cost", even though the
        router's trust floor may refuse to use it.
        """
        if not self.peers:
            return None
        return min(self.peers, key=lambda p: (p.price_in, p.price_out))

    def pin(self) -> str | None:
        """The model id to send so this model routes to a free seller.

        Composes AntSeed's own ``<peerId>@<model>`` syntax, which the buyer
        documents as bypassing the router's price-and-trust ranking. Free peers
        sit below the default ``minTrustScore`` of 60, so without this a free
        model answers ``502 model_not_found`` even though the peer is online.

        Among the free peers, the highest-trust one that advertises Anthropic
        wins; failing that, the highest-trust free peer of any kind.

        The Anthropic preference is a *preference*, not a filter. In practice
        no free peer advertises ``anthropic-messages`` at all, so the fallback
        is the branch that actually runs — and the proxy translates for most
        OpenAI-only sellers, which is why pinning a free model normally works
        from here. Returns ``None`` when no seller offers the model for free.
        """
        free = self.free_peers
        if not free:
            return None
        ranked = sorted(
            free,
            key=lambda p: (p.serves_anthropic, p.reputation or 0.0),
            reverse=True,
        )
        return f"{ranked[0].peer_id}@{self.id}"

    @property
    def blurb(self) -> str:
        """A one-line description for the picker.

        The seller counts are written as ``N of M sellers`` rather than
        ``N peers · M sellers``: "peer" and "seller" are the same thing in
        AntSeed's vocabulary, so the two numbers side by side read as a
        contradiction ("4 peers · 9 sellers" — which is the free one?) rather
        than as the free count out of the total.
        """
        bits = []
        if self.free:
            free = len(self.free_peers)
            total = len(self.peers)
            if free == total:
                bits.append(f"free · {total} seller" + ("s" if total != 1 else ""))
            else:
                bits.append(f"free · {free} of {total} sellers")
        elif (c := self.cheapest) is not None:
            bits.append(f"${c.price_in:g}/${c.price_out:g} per Mtok")
            total = len(self.peers)
            bits.append(f"{total} seller" + ("s" if total != 1 else ""))
        if self.tool_use:
            bits.append("tools")
        if self.context_length:
            bits.append(f"{self.context_length // 1000}k ctx")
        return " · ".join(bits)


def _num(value) -> float:
    """A price as a float, tolerating a string or a missing field.

    AntSeed sends JSON numbers, but a seller's own metadata is not always
    well-typed and a malformed price must not take the whole catalog down. A
    zero here reads as "free", which is only safe because :meth:`Peer.free`
    also requires the *other* direction to be zero.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_peers(raw) -> tuple[Peer, ...]:
    out: list[Peer] = []
    for entry in raw or ():
        if not isinstance(entry, dict):
            continue
        peer_id = str(entry.get("peerId") or "").strip()
        if not peer_id:
            continue
        rep = entry.get("effectiveReputationScore")
        if rep is None:
            rep = entry.get("reputationScore")
        out.append(Peer(
            peer_id=peer_id,
            display_name=str(entry.get("displayName") or ""),
            provider=str(entry.get("provider") or ""),
            protocol=str(entry.get("protocol") or ""),
            price_in=_num(entry.get("inputUsdPerMillion")),
            price_out=_num(entry.get("outputUsdPerMillion")),
            reputation=None if rep is None else _num(rep),
        ))
    return tuple(out)


def _parse_models(payload: dict) -> list[Model]:
    """Turn the ``/v1/models`` payload into :class:`Model` objects.

    The payload is OpenAI-shaped — ``{"object": "list", "data": [...]}`` — so
    the shared OpenAI parser would appear to work. It would then silently drop
    every peer, because the pricing that matters lives in a nested ``peers``
    array rather than in the entry, and a model with no peers would look free.
    Hence a parser of its own.

    An entry with no id is skipped rather than kept: it cannot be requested, and
    a nameless row in the picker is worse than no row.
    """
    out: list[Model] = []
    for entry in (payload or {}).get("data") or ():
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            continue
        caps = entry.get("capabilities") or {}
        if not isinstance(caps, dict):
            caps = {}
        ctx = entry.get("context_length") or caps.get("contextWindow")
        max_out = entry.get("max_output_tokens") or caps.get("maxOutputTokens")
        aliases = entry.get("aliases") or ()
        out.append(Model(
            id=model_id,
            label=str(entry.get("name") or ""),
            aliases=tuple(str(a) for a in aliases if isinstance(a, (str, int))),
            context_length=int(ctx) if isinstance(ctx, (int, float)) and ctx else None,
            max_output_tokens=(int(max_out)
                               if isinstance(max_out, (int, float)) and max_out else None),
            tool_use=bool(caps.get("toolUse")),
            reasoning=bool(caps.get("reasoning")),
            model_type=str(entry.get("type") or "text"),
            protocols=tuple(str(p) for p in entry.get("supported_protocols") or ()),
            peers=_parse_peers(entry.get("peers")),
        ))
    return out


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, list[Model]]] = {}


def _cache_key(base_url: str) -> str:
    """Cache per endpoint, not per module.

    The port can be changed with ``--port`` and the network's contents differ
    between a local buyer and a tunnel, so one process-wide slot would serve a
    catalog fetched from one endpoint to a session pointed at another.
    """
    return (base_url or DEFAULT_BASE_URL).rstrip("/")


def fetch_models(base_url: str = "", *, timeout: float = 20.0,
                 use_cache: bool = True) -> tuple[list[Model] | None, str]:
    """Fetch the network's catalog. Returns ``(models, "")`` or ``(None, why)``.

    Needs no credential — the buyer serves its catalog openly to anything that
    can reach the port, and a buyer that is not running is the expected failure
    rather than an error worth alarming about.
    """
    import time

    url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}/v1/models"
    key = _cache_key(base_url)
    if use_cache:
        hit = _CACHE.get(key)
        if hit and (time.monotonic() - hit[0]) < CACHE_TTL:
            return hit[1], ""

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} from the AntSeed buyer"
    except urllib.error.URLError as e:
        return None, (f"could not reach the AntSeed buyer at {url} — "
                      f"is `antseed buyer start` running? ({e.reason})")
    except (OSError, ValueError) as e:
        return None, f"could not read the AntSeed catalog: {e}"

    models = _parse_models(payload if isinstance(payload, dict) else {})
    # An empty list is cached as a real answer: the network genuinely can have
    # nothing to sell, and re-fetching on every keystroke to rediscover that is
    # exactly the hammering the cache exists to prevent.
    _CACHE[key] = (time.monotonic(), models)
    return models, ""


def catalog_age() -> float | None:
    """Seconds since the cache was filled, or ``None`` if it never was.

    Not clamped to ``CACHE_TTL``: an expired catalog is the case worth
    reporting, and the caller needs the real number to say how stale it is.
    """
    import time

    hit = _CACHE.get(_cache_key(""))
    return (time.monotonic() - hit[0]) if hit else None


def cached_models() -> list[Model] | None:
    """The last fetched catalog, if it is still fresh — else ``None``.

    Never fetches. **The ``CACHE_TTL`` check is load-bearing:** ``fetch_models``
    has always refused to *reuse* an entry past its TTL, but this read it back
    regardless, so a catalog fetched once was reported as live for the rest of
    the session. AntSeed's seller set is the most volatile of any source here —
    peers join and leave constantly — so a stale list is the worst case. An
    expired entry now reads as ``None``, which callers already handle as "no
    catalog".
    """
    import time

    hit = _CACHE.get(_cache_key(""))
    if not hit:
        return None
    if (time.monotonic() - hit[0]) >= CACHE_TTL:
        return None
    return hit[1]


def cached_ids() -> list[str] | None:
    models = cached_models()
    if models is None:
        return None
    return [m.id for m in models]


def clear_cache() -> None:
    _CACHE.clear()


def free_models(base_url: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Every model at least one seller offers for nothing."""
    models, err = fetch_models(base_url, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.free], ""


def usable_free_models(base_url: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Free models that can actually be pinned to a free seller.

    Identical to :func:`free_models` in practice — :meth:`Model.pin` returns a
    value exactly when a free peer exists — but stated separately so a caller
    cannot forget that a free model is useless here without a pinnable peer.
    """
    models, err = free_models(base_url, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.pin()], ""


def free_model_ids(base_url: str = "", **kw) -> tuple[list[str] | None, str]:
    """The **pinned** ids to send so free models route to a free seller.

    These are ``<peerId>@<model>`` strings, not bare model names. Handing back
    the bare name would produce a list that looks right and 502s on use, which
    is the exact failure this module was written to avoid.
    """
    models, err = usable_free_models(base_url, **kw)
    if models is None:
        return None, err
    out: list[str] = []
    for m in models:
        pin = m.pin()
        if pin:
            out.append(pin)
    return out, ""


def model_ids(base_url: str = "", **kw) -> tuple[list[str] | None, str]:
    models, err = fetch_models(base_url, **kw)
    if models is None:
        return None, err
    return [m.id for m in models], ""


def agentic_models(base_url: str = "", **kw) -> tuple[list[Model] | None, str]:
    """Text models that report tool support — what an agentic harness needs."""
    models, err = fetch_models(base_url, **kw)
    if models is None:
        return None, err
    return [m for m in models if m.tool_use and m.model_type == "text"], ""


def price_for(model: str, models: list[Model] | None) -> tuple[float, float] | None:
    """``(in, out)`` per Mtok for *model*, or ``None`` when it cannot be priced.

    A pinned id (``<peerId>@<model>``) is unwrapped to the model before the
    lookup, so a session running on a pinned free model still shows its real
    price — which is zero — rather than failing to find itself in the catalog.
    """
    if not models:
        return None
    wanted = model.split("@", 1)[1] if "@" in model else model
    for m in models:
        if m.id == wanted:
            if m.free:
                return (0.0, 0.0)
            c = m.cheapest
            return (c.price_in, c.price_out) if c else None
    return None


# ---------------------------------------------------------------------------
# the remembered pin
# ---------------------------------------------------------------------------

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


def load_pin() -> tuple[str, str] | None:
    """The remembered ``(model, peer_id)`` preference, or ``None``.

    AntSeed issues no key, so this profile is not a credential — it is the
    operator's answer to "which free peer should my sessions use", so that a
    restart lands back on the same seller instead of re-picking one.
    """
    exports = _read_exports(PROFILE)
    model = exports.get("ANTSEED_MODEL", "").strip()
    peer = exports.get("ANTSEED_PEER", "").strip()
    if not model and not peer:
        return None
    return model, peer


def save_pin(model: str, peer: str = "") -> Path:
    """Remember a model and the peer to pin it to. Returns the profile path.

    Written mode 600 through ``os.open`` for the same reason every other profile
    is: it lives in a home directory that may be shared, and a later version of
    this file could carry a credential. Today it carries none, which the header
    says out loud so nobody assumes otherwise.
    """
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PROFILE.parent, 0o700)
    except OSError:
        pass
    body = (
        "# ZimZilla — AntSeed source preference. Not a credential:\n"
        "# the local buyer proxy authenticates nothing.\n"
        f'export ANTSEED_MODEL="{model}"\n'
        f'export ANTSEED_PEER="{peer}"\n'
        f'export ANTHROPIC_BASE_URL="{DEFAULT_BASE_URL}"\n'
    )
    fd = os.open(PROFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    return PROFILE


def load_credentials() -> tuple[str, str, str] | None:
    """``(token, model, origin)`` in the shape the other gateways use.

    AntSeed has no key, so the token is :data:`NO_KEY_PLACEHOLDER` — the same
    marker :func:`apply_to` sets. It exists so ``/zim-antseed`` can share one
    code path with ``/zim-openrouter`` rather than branching around a credential
    lookup that does not apply here.

    ``None`` when no buyer is reachable: that is the honest analogue of "no key
    found", because a switch to a proxy that is not running would report success
    and then fail on the first turn.

    ``origin`` is ``"local buyer"`` so the confirmation line reads
    ``key from local buyer``, which is what is actually true — there is no
    profile and no environment variable behind it.
    """
    if not reachable():
        return None
    pin = load_pin()
    model = ""
    if pin:
        chosen, peer = pin
        if chosen and peer:
            model = f"{peer}@{chosen}"
        elif chosen:
            model = chosen
    return NO_KEY_PLACEHOLDER, model, "local buyer"


def apply_to(cfg) -> tuple[str, str] | None:
    """Point *cfg* at the local AntSeed buyer. Returns ``(origin, model)``.

    ``auth_token`` is set to a placeholder because the harness refuses to start
    without one (``api_key_present`` is False for an empty config) while the
    proxy ignores it entirely — measured: a request with no auth header, and one
    with a junk bearer, both returned 200. The placeholder is a marker, not a
    secret, and is named so in the transcript.

    A remembered pin is composed into the model, so the session routes to the
    seller the operator chose rather than to whatever the router ranks first.
    Returns ``None`` when no buyer is reachable, leaving *cfg* alone — a switch
    to a proxy that is not running would look like success and then fail on the
    first turn.
    """
    if not reachable():
        return None
    cfg.base_url = DEFAULT_BASE_URL
    cfg.auth_token = NO_KEY_PLACEHOLDER
    cfg.api_key = ""
    pin = load_pin()
    if pin:
        model, peer = pin
        if model and peer:
            cfg.model = f"{peer}@{model}"
        elif model:
            cfg.model = model
    return ("antseed", cfg.model)


def applies_to_default_endpoint(base_url: str | None) -> bool:
    """Whether a remembered AntSeed choice should override *base_url*.

    True only for the built-in default and the local LiteLLM proxies — the
    endpoints the launcher leaves behind when it has no profile to source. An
    endpoint the operator pointed somewhere deliberate is left exactly as they
    set it, so a stale selection cannot hijack a session aimed elsewhere.
    """
    url = (base_url or "").strip()
    if is_antseed(url):
        return False
    if not url:
        return True
    return "localhost" in url or "127.0.0.1" in url


def looks_like_key(value: str) -> bool:
    """Whether *value* looks like an AntSeed credential.

    Always False, and that is the honest answer: the buyer proxy accepts any
    bearer, so no string is more or less a key than another. Present so this
    module satisfies the same interface as the gateways that do have keys, and
    so a caller that offers to store one gets told there is nothing to store.
    """
    return False


def current_key() -> str | None:
    """The source key the operator last selected, if any.

    Delegates to :mod:`zimzilla.sources`, which owns ``current-source``. Two
    writers of one selection file is how a switch made here and a switch made
    with ``/zim-source`` end up disagreeing about what is live.
    """
    from . import sources as sources_mod
    return sources_mod.current_key()


def set_current(key: str) -> None:
    """Record the selection. Never fatal — a read-only home must not break the TUI.

    The UI calls this on whatever ``_gateway()`` hands back, and AntSeed is in
    that table, so this has to exist even though the module has no key to store.
    """
    from . import sources as sources_mod
    sources_mod.set_current(key)
