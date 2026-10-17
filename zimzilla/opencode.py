"""Run the `opencode` CLI behind a rotating, per-process IP.

What this is
------------
``opencode`` ships a set of models it serves itself, and the free ones need no
account, no key and no signup — verified by *deleting the credential and keeping
the config*: copy ``~/.local/share/opencode`` and ``~/.config/opencode`` to a
temp ``HOME``, remove ``auth.json``, and ``opencode run -m
opencode/deepseek-v4.1-flash-free`` still answers. So the whole feature is
reachable with nothing but the binary.

(An empty ``$HOME`` does NOT prove this, though it looks like it should. A fresh
home has no config and no model cache, so opencode fails there for reasons that
have nothing to do with credentials — and a refusal is not a "no credential
needed" signal. Delete the credential, keep the config. A machine that has an
``auth.json`` is also not evidence against keyless operation: the key is simply
unused for these model ids.)

The catch is that opencode's free tier **refuses Tor exit IPs** — a request
through Tor comes back "OpenCode's free tier can only be used from within
OpenCode", while the identical request through a proxy that exits from an
ordinary address succeeds. The block is against Tor specifically, not against
proxying and not against datacenter ranges: Tor publishes its exit list, so
anyone can refuse it in one line. That is why a commercial VPN (Proton, say)
works where Tor does not, and it is what makes free proxies usable here at all.

So rotation is done with a pool of ordinary HTTP proxies rather than Tor.

Why HTTP proxies and not SOCKS
------------------------------
opencode's fetch layer accepts an ``http://`` proxy URL and rejects
``socks5://`` outright ("UnsupportedProxyProtocol"). A SOCKS-only route
therefore needs a bridge process. Free proxy lists are overwhelmingly plain
HTTP already, so no bridge is needed for the pool — the Tor path, which *is*
SOCKS-only, is deliberately not used.

Scope
-----
Nothing global is touched. The proxy is passed to the child through
``HTTPS_PROXY``/``HTTP_PROXY`` in its environment, so only the opencode process
egresses through it. The operator's own IP, the shell, and every other program
on the machine are unaffected. This is the difference from proxychains, which
is also not used: its LD_PRELOAD hook is not the slow part (Tor is), but it
applies to the whole process tree and needs root to configure.

Rotation
--------
Every launch takes a proxy that has completed a turn and marks it used, so
consecutive runs leave from different addresses *and* each of them works.

Those two goals contradict each other, which is the trap here. A pool built by
turn-validation has every entry proven, so "prefer a proven proxy" hands back
the same address on every launch — the first version of this did exactly that,
returning one proxy five times out of five. So the two facts are tracked
separately: ``proven_at`` is whether a proxy *works*, ``used_at`` is whether it
has already been *given out*, and each launch takes a proven proxy that has not
been used yet. Only once those run out does the plain cursor take over, which
makes a small pool degrade into reuse rather than into failure. Both markers are
persisted, so a restart does not reset the rotation.

A refusal retires a proxy, because it is newer evidence than the proof that put
it at the front of the queue — free proxies die without warning, and one that
answered yesterday and died overnight would otherwise be tried first on every
launch and time out for ~40s before the pool moved on. Recovery clears it.

Validation is two stages, and the second is the one that matters. An ipify
fetch through a candidate proves it carries traffic; it says nothing about
whether opencode will accept the exit IP. Measured on this pool, those two are
close to independent: validated on liveness alone a pool ran ~12% usable, while
the same pool re-validated with real turns ran 79%. So the survivors of the
cheap stage get a real one-shot turn, in parallel, and only the ones that
answer are kept — stamped with ``proven_at`` at the moment they pass, since a
pool saved unmarked would carry no memory at all.

The gate has no pattern worth predicting — residential and datacenter addresses
scored identically — so the only check that predicts a working launch is a
launch. That is also why the pool *remembers*: a proxy that answered once is
better evidence than any measurement, and the pool gets cheaper the more it is
used.

Working proxies are not volatile. Re-tested hours later, four of five still
answered, so a validated pool is worth caching to disk; it is refreshed when it
runs dry or ages out, and known-good entries are carried across a rebuild.

Nothing here writes a credential: these proxies take no authentication. The
free model ids need no credential either — see the note at the top about how
that was actually verified.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

#: The binary opencode installs. Also the name the operator types.
BINARY = "opencode"

#: The alias this module installs, so opencode can be launched behind a fresh
#: proxy from a plain shell as well as from inside ZimZilla.
ALIAS = "opencode-zim"

#: Default model. One of opencode's own free models — no key, no account. Kept
#: as a single id rather than a list because the free set rotates: opencode
#: retires and adds these over time, and a stale id fails loudly with "Model is
#: unavailable" rather than silently costing money, which is the failure we
#: want. `/zim-opencode --model <id>` overrides it; `opencode models` lists.
DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"

#: Public lists of plain-HTTP proxies. Only HTTP: see the module docstring.
PROXY_SOURCES: tuple[str, ...] = (
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=yes&anonymity=all",
)

#: How many candidates to probe per validation pass, and how many live proxies
#: a full pool wants. Probing is the slow part, so the pool is built to last
#: several launches rather than exactly one.
PROBE_LIMIT = 600
POOL_TARGET = 24
PROBE_WORKERS = 96

#: A proxy that cannot complete an HTTPS fetch in this many seconds is dropped.
#: Generous, because a slow proxy still works and the pool is chosen from.
PROBE_TIMEOUT = 7.0

#: The second, decisive stage: how many of the ipify-live proxies get a real
#: opencode turn, how many run at once, and how long each is given.
#:
#: This stage is why the pool is worth anything. An ipify fetch proves a proxy
#: carries traffic; it says nothing about whether opencode's free tier will
#: accept the exit IP, and those two are close to independent — measured, a pool
#: validated on ipify alone ran ~12% usable, while the same pool re-validated
#: with real turns ran 79%. The gate refuses *some* addresses and not others
#: with no pattern worth predicting (residential and datacenter scored
#: identically), so the only check that predicts a working launch is a launch.
#:
#: It is affordable because it is parallel and because the yield is high:
#: 24 candidates at 12 workers is ~90s to fill a pool that then works on the
#: first try, against ~25s to build one that mostly fails.
TURN_LIMIT = 48
TURN_WORKERS = 12
TURN_TIMEOUT = 60.0

#: Working proxies are NOT volatile — re-tested hours later, 4 of 5 still
#: answered — so a validated pool is worth keeping, and a proxy that worked once
#: is worth trying first next time. This is how long that knowledge is trusted.
KNOWN_GOOD_AGE = 24 * 3600

#: The pool is re-validated when it is older than this. Free proxies die within
#: hours, so a day-old pool is mostly dead entries.
POOL_MAX_AGE = 6 * 3600

#: A model reply can legitimately take a while; this bounds the whole run.
RUN_TIMEOUT = 600


def _home() -> Path:
    return Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))


def state_dir() -> Path:
    """Directory holding the pool, the cache and the shim."""
    d = _home() / "opencode"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pool_file() -> Path:
    return state_dir() / "pool.json"


def find_binary() -> str | None:
    """Path to the opencode binary, or None when it is not installed.

    ``shutil.which`` covers a normal PATH install. The mise/npm layouts put the
    binary under the user's own data dir and only expose it through a shim, so
    a session that inherited a different PATH — which is exactly what happens
    when ZimZilla is launched from a desktop entry — would otherwise report
    "not found" for a binary that is plainly installed.
    """
    found = shutil.which(BINARY)
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".opencode" / "bin" / BINARY,
        home / ".local" / "bin" / BINARY,
        home / ".local" / "share" / "mise" / "installs" / "opencode" / "latest" / BINARY,
    ]
    # mise keeps one directory per version; newest first is close enough, and a
    # wrong pick still runs — it is simply an older build.
    mise = home / ".local" / "share" / "mise" / "installs" / "opencode"
    if mise.is_dir():
        candidates.extend(sorted(mise.glob(f"*/{BINARY}"), reverse=True))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def is_installed() -> bool:
    return find_binary() is not None


# ---------------------------------------------------------------------------
# The proxy pool
# ---------------------------------------------------------------------------

@dataclass
class Proxy:
    """One candidate HTTP proxy."""

    host: str
    port: int
    #: Measured seconds to complete one HTTPS fetch through it, when known.
    latency: float | None = None
    #: When this proxy last completed a real opencode turn, if it ever has.
    #: This is the only field that predicts anything: `latency` says the proxy
    #: carries traffic, which is nearly unrelated to whether opencode accepts
    #: its exit IP. A proxy with a `proven_at` is worth trying before one
    #: without, whatever their latencies say.
    proven_at: float = 0.0
    #: When this proxy was last handed to a launch. Distinct from `proven_at`,
    #: which records whether it *works*. Rotation needs to know what the
    #: operator has already been given: a pool where every entry is proven would
    #: otherwise hand out its best proxy every single time, which is the one
    #: thing the command must not do.
    used_at: float = 0.0
    #: When this proxy last failed a turn, if it has failed since it last
    #: worked. A failure is *newer evidence* than a proof, so it invalidates
    #: one: free proxies die without warning, and a proxy that is still trusted
    #: on the strength of yesterday's success would be tried first on every
    #: launch and cost a ~40s timeout each time before the pool moved on.
    failed_at: float = 0.0

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    def is_proven(self, now: float | None = None) -> bool:
        """True if this proxy completed a turn recently enough to trust.

        A failure after the proof cancels it. Without that, a proxy that worked
        yesterday and died overnight keeps its place at the head of the queue
        and is retried — and timed out — on every single launch.
        """
        if not self.proven_at:
            return False
        if self.failed_at > self.proven_at:
            return False
        now = time.time() if now is None else now
        return (now - self.proven_at) < KNOWN_GOOD_AGE

    def to_json(self) -> dict:
        return {"host": self.host, "port": self.port, "latency": self.latency,
                "proven_at": self.proven_at, "used_at": self.used_at,
                "failed_at": self.failed_at}

    @classmethod
    def from_json(cls, d: dict) -> "Proxy":
        return cls(host=str(d["host"]), port=int(d["port"]),
                   latency=d.get("latency"), proven_at=float(d.get("proven_at") or 0.0),
                   used_at=float(d.get("used_at") or 0.0),
                   failed_at=float(d.get("failed_at") or 0.0))


@dataclass
class Pool:
    """The validated proxies plus the bookkeeping that makes rotation work."""

    proxies: list[Proxy] = field(default_factory=list)
    #: Index of the next proxy to hand out. Persisted so consecutive launches
    #: differ even across restarts — an in-memory counter would reset and the
    #: operator would see the same IP twice in a row.
    cursor: int = 0
    validated_at: float = 0.0

    def to_json(self) -> dict:
        return {
            "proxies": [p.to_json() for p in self.proxies],
            "cursor": self.cursor,
            "validated_at": self.validated_at,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Pool":
        return cls(
            proxies=[Proxy.from_json(p) for p in d.get("proxies", [])],
            cursor=int(d.get("cursor", 0)),
            validated_at=float(d.get("validated_at", 0.0)),
        )

    def next_proxy(self, mark: bool = False) -> Proxy | None:
        """The next proxy in rotation, advancing the cursor. None if empty.

        ``mark`` stamps ``used_at``, which is what the proven-first path reads
        to decide what has already been handed out. A dry peek at the next
        proxy should leave it False so looking does not consume it.
        """
        if not self.proxies:
            return None
        p = self.proxies[self.cursor % len(self.proxies)]
        self.cursor = (self.cursor + 1) % len(self.proxies)
        if mark:
            p.used_at = time.time()
        return p

    def known_good(self) -> list[Proxy]:
        """Proxies that have completed a turn, most recent first."""
        return sorted((p for p in self.proxies if p.is_proven()),
                      key=lambda p: -p.proven_at)

    def next_unused(self, exclude: set[str] | None = None) -> Proxy | None:
        """A proven proxy the operator has not been handed yet.

        The two requirements pull against each other: the launch must work, so
        it wants a proven proxy, and it must differ from last time, so it wants
        one that has not been used. Proven-and-unused satisfies both. Ordering
        by `used_at` ascending makes this a rotation rather than a fixed
        favourite — without it the same proxy would win every launch, because
        every entry in a fresh pool is proven.

        ``exclude`` is a set of keys to skip, for a caller that is assembling
        several at once and must not be handed the same one twice. It is a
        parameter rather than a side effect so that merely *considering* a proxy
        does not consume it.

        None when every proven proxy has already been handed out; the caller
        then falls back to the plain cursor, which is what keeps a small pool
        cycling instead of failing.
        """
        skip = exclude or set()
        unused = [p for p in self.proxies
                  if p.is_proven() and not p.used_at and p.key not in skip]
        if not unused:
            return None
        return min(unused, key=lambda p: (p.used_at, -p.proven_at))

    def mark_used(self, proxy: Proxy) -> None:
        """Record that *proxy* was handed to a launch, so it rotates out.

        Only this makes consecutive launches differ. `proven_at` cannot do it:
        it answers "does this work", and in a freshly validated pool every
        entry answers yes.
        """
        now = time.time()
        for p in self.proxies:
            if p.key == proxy.key:
                p.used_at = now
                return
        proxy.used_at = now
        self.proxies.append(proxy)

    def mark_proven(self, proxy: Proxy) -> None:
        """Record that *proxy* completed a real turn, and keep it in the pool.

        Called on every success, so the pool improves the more it is used. A
        proxy that answered once is the best evidence available that it will
        answer again — better than any liveness measurement.
        """
        now = time.time()
        for p in self.proxies:
            if p.key == proxy.key:
                p.proven_at = now
                p.failed_at = 0.0  # it just worked, so the last failure is moot
                return
        proxy.proven_at = now
        proxy.failed_at = 0.0
        self.proxies.append(proxy)

    def mark_failed(self, proxy: Proxy) -> None:
        """Record that *proxy* was refused, retiring it from the trusted set.

        Free proxies die without warning, so a failure is newer evidence than
        the proof it contradicts and has to cancel it — otherwise a proxy that
        worked yesterday and died overnight keeps being tried first, costing a
        ~40s timeout on every launch before the pool moves on.
        """
        now = time.time()
        for p in self.proxies:
            if p.key == proxy.key:
                p.failed_at = now
                return
        proxy.failed_at = now
        self.proxies.append(proxy)

    def is_fresh(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.proxies) and (now - self.validated_at) < POOL_MAX_AGE


def load_pool(path: Path | None = None) -> Pool:
    """Read the cached pool. A corrupt or missing file is an empty pool."""
    path = path or pool_file()
    try:
        return Pool.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return Pool()


def save_pool(pool: Pool, path: Path | None = None) -> None:
    """Persist the pool. Never fatal — a read-only home must not break the run."""
    path = path or pool_file()
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(pool.to_json(), indent=1), encoding="utf-8")
    except OSError:
        pass


def parse_candidates(text: str) -> list[Proxy]:
    """Pull ``host:port`` lines out of a proxy-list body.

    Tolerant on purpose: these lists are hand-maintained and carry blank lines,
    comments and the occasional scheme prefix. Anything that is not a clean
    host:port pair is skipped rather than raising, because one malformed line
    should not cost the whole list.
    """
    out: list[Proxy] = []
    seen: set[tuple[str, int]] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split("://")[-1].strip()
        host, sep, port = line.rpartition(":")
        if not sep or not host or not port.isdigit():
            continue
        key = (host, int(port))
        if key in seen:
            continue
        seen.add(key)
        out.append(Proxy(host=host, port=int(port)))
    return out


def fetch_candidates(timeout: float = 30.0) -> list[Proxy]:
    """Download and merge every configured proxy list."""
    out: list[Proxy] = []
    seen: set[tuple[str, int]] = set()
    for url in PROXY_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "zimzilla"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        for p in parse_candidates(body):
            key = (p.host, p.port)
            if key not in seen:
                seen.add(key)
                out.append(p)
    return out


def _probe(proxy: Proxy, timeout: float = PROBE_TIMEOUT) -> Proxy | None:
    """Return the proxy with its latency filled in if it actually carries traffic.

    The check is a real HTTPS fetch *through* the proxy, not a TCP connect: a
    port that accepts a connection and then refuses to forward is the common
    failure in these lists, and a connect-only check would keep every one of
    them.
    """
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy.url, "https": proxy.url})
    )
    started = time.monotonic()
    try:
        with opener.open("https://api.ipify.org", timeout=timeout) as resp:
            ip = resp.read().decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 — any failure means "not usable"
        return None
    if ip.count(".") != 3:
        return None
    proxy.latency = round(time.monotonic() - started, 2)
    return proxy


def validate(candidates: list[Proxy], target: int = POOL_TARGET,
             limit: int = PROBE_LIMIT, workers: int = PROBE_WORKERS,
             progress=None) -> list[Proxy]:
    """Probe candidates concurrently and return the live ones, fastest first.

    Concurrency is what makes this bearable: a sequential pass over hundreds of
    candidates at seven seconds each is an hour, while a pool of workers turns
    it into well under a minute. Threads are the right tool because the work is
    entirely blocking socket I/O.

    ``progress`` is called with ``(done, total)`` after each probe so a caller
    with a UI can show movement.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    sample = candidates[:limit]
    if not sample:
        return []
    live: list[Proxy] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_probe, p): p for p in sample}
        for fut in as_completed(futures):
            done += 1
            if progress is not None:
                progress(done, len(sample))
            try:
                got = fut.result()
            except Exception:  # noqa: BLE001
                got = None
            if got is not None:
                live.append(got)
                if len(live) >= target:
                    # Enough to rotate with. The remaining probes are cancelled
                    # rather than waited on — a launch should not block on
                    # candidates it will never use.
                    for f in futures:
                        f.cancel()
                    break
    live.sort(key=lambda p: (p.latency if p.latency is not None else 9e9))
    return live


def validate_turns(candidates: list[Proxy], binary: str, model: str | None = None,
                   target: int = POOL_TARGET, limit: int = TURN_LIMIT,
                   workers: int = TURN_WORKERS, progress=None) -> list[Proxy]:
    """Keep the proxies that complete a real opencode turn.

    The decisive stage. An ipify fetch proves a proxy carries traffic; it says
    nothing about whether opencode's free tier will accept the exit IP, and
    measured, that distinction is the difference between a pool that runs ~12%
    usable and one that runs 79%.

    Parallel because it is slow — a turn is tens of seconds — and the yield is
    high enough that a small sample fills a pool. A "model" verdict is not this
    proxy's fault and is reported by the caller instead of being retried here.

    Every survivor is stamped with ``proven_at`` here rather than by the caller.
    Passing a turn is the strongest evidence available and the whole reason
    :meth:`Pool.known_good` exists, so a pool that arrives unstamped would hand
    the next launch a set of proxies it cannot prefer — the memory would be
    built and then immediately thrown away.

    ``progress`` is called with ``(done, total, verdict)`` after each attempt.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    sample = candidates[:limit]
    if not sample:
        return []
    proven: list[Proxy] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(probe_launch, p, binary, model, TURN_TIMEOUT): p
                   for p in sample}
        for fut in as_completed(futures):
            done += 1
            try:
                verdict = fut.result()
            except Exception:  # noqa: BLE001
                verdict = "retry"
            if progress is not None:
                progress(done, len(sample), verdict)
            if verdict == "ok":
                proxy = futures[fut]
                proxy.proven_at = time.time()
                # It just answered, so any earlier refusal is superseded. These
                # are freshly fetched candidates and normally carry no history,
                # but a proxy handed in from the old pool does.
                proxy.failed_at = 0.0
                proven.append(proxy)
                if len(proven) >= target:
                    for f in futures:
                        f.cancel()
                    break
    return proven


def build_pool(target: int = POOL_TARGET, progress=None,
               binary: str | None = None, model: str | None = None,
               turn_progress=None) -> Pool:
    """Fetch, validate and persist a fresh pool.

    Two stages, and the second is the one that matters: ipify-liveness to cut
    thousands of candidates down cheaply, then a real opencode turn on the
    survivors. Skipping the second stage is what made an earlier version of this
    build pools that were mostly refused — see :data:`TURN_LIMIT`.
    """
    candidates = fetch_candidates()
    random.shuffle(candidates)
    live = validate(candidates, target=max(target * 2, target), progress=progress)
    if not live:
        return Pool(validated_at=time.time())

    # Without a binary there is no way to run the decisive check, so the
    # liveness result is all there is. The caller has already reported that
    # opencode is missing, so this is a degraded path, not a silent one.
    if not binary:
        return Pool(proxies=live[:target], cursor=0, validated_at=time.time())

    proven = validate_turns(live, binary, model=model, target=target,
                            progress=turn_progress)
    pool = Pool(proxies=proven, cursor=0, validated_at=time.time())
    if proven:
        save_pool(pool)
    return pool


def get_pool(refresh: bool = False, progress=None, binary: str | None = None,
             model: str | None = None, turn_progress=None) -> Pool:
    """The pool to rotate from: the cache when it is fresh, else a new build."""
    pool = load_pool()
    if not refresh and pool.is_fresh():
        return pool
    fresh = build_pool(progress=progress, binary=binary, model=model,
                       turn_progress=turn_progress)
    # A rebuild that found nothing must not throw away a pool that still has
    # entries — free lists have bad days, and a stale-but-live proxy beats none.
    if not fresh.proxies and pool.proxies:
        return pool
    # Carry forward what the old pool learned. A proxy that completed a turn
    # yesterday is better evidence than anything the rebuild just measured, and
    # discarding it would make every refresh start from zero knowledge.
    if fresh.proxies and pool.proxies:
        seen = {p.key for p in fresh.proxies}
        for p in pool.known_good():
            if p.key not in seen:
                fresh.proxies.append(p)
    return fresh


def rotate(refresh: bool = False, progress=None, prefer_proven: bool = True,
           pool: Pool | None = None) -> Proxy | None:
    """Take the next proxy and advance the pool's cursor.

    Returns None when no proxy could be found, which the caller must treat as
    "go direct" and say so — silently falling back to the operator's real IP
    would defeat the entire point of the command.

    ``prefer_proven`` hands out a proxy that has completed a turn before one
    that has only been shown to carry traffic, because the second says almost
    nothing about whether opencode will accept it — but only while there is a
    proven proxy left that has not already been handed out. Picking from
    ``known_good`` alone would return the same entry on every call once the
    whole pool is proven, which is precisely what rotation exists to prevent.
    """
    pool = pool or get_pool(refresh=refresh, progress=progress)
    proxy = None
    if prefer_proven:
        proxy = pool.next_unused()
    if proxy is None:
        proxy = pool.next_proxy(mark=True)
    else:
        pool.mark_used(proxy)
    if proxy is not None:
        save_pool(pool)  # persist the advanced cursor
    return proxy


def child_env(proxy: Proxy, base: dict | None = None) -> dict:
    """The environment for the opencode child: proxied, with localhost bypassed.

    ``NO_PROXY`` is not optional. opencode's TUI talks to a local HTTP server of
    its own, and with every address routed through the proxy that localhost
    connection is sent to the proxy too, which breaks the TUI. Its own docs call
    the bypass required.

    Both cases of each variable are set because the two spellings are read by
    different layers — Bun's fetch reads the uppercase form, but plenty of
    tooling under it (and npm, when opencode shells out) reads the lowercase.
    """
    env = dict(os.environ if base is None else base)
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                 "ALL_PROXY", "all_proxy"):
        env[name] = proxy.url
    for name in ("NO_PROXY", "no_proxy"):
        env[name] = "localhost,127.0.0.1,::1"
    # opencode is being driven from inside another TUI; a nested pager or a
    # colour probe would fight for the terminal it has already been given.
    env.setdefault("TERM", os.environ.get("TERM", "xterm-256color"))
    return env


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------

def build_argv(binary: str, args: list[str] | None = None,
               model: str | None = None, prompt: str | None = None) -> list[str]:
    """The argv that runs opencode behind the proxy.

    ``opencode run <prompt>`` is the one-shot form; with no prompt it is the
    TUI, which is what the slash command opens so the operator gets the full
    interface rather than a single answer.
    """
    argv = [binary]
    if prompt:
        argv += ["run", "-m", model or DEFAULT_MODEL, prompt]
    else:
        if model:
            argv += ["-m", model]
        argv += list(args or [])
    return argv


def run(proxy: Proxy | None, binary: str, args: list[str] | None = None,
        model: str | None = None, prompt: str | None = None,
        timeout: int | None = None, cwd: str | None = None) -> int:
    """Run opencode, returning its exit status.

    With no proxy the child inherits the environment untouched — that is the
    honest fallback when rotation could not find one, and the caller is
    expected to have said so first.
    """
    argv = build_argv(binary, args=args, model=model, prompt=prompt)
    env = child_env(proxy) if proxy is not None else None
    try:
        return subprocess.call(argv, env=env, cwd=cwd,
                               timeout=timeout if timeout else None)
    except subprocess.TimeoutExpired:
        return 124


def probe_exit_ip(proxy: Proxy, timeout: float = 10.0) -> str | None:
    """The public IP this proxy egresses from, for display. None on failure."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy.url, "https": proxy.url})
    )
    try:
        with opener.open("https://api.ipify.org", timeout=timeout) as resp:
            ip = resp.read().decode("utf-8", errors="replace").strip()
        return ip if ip.count(".") == 3 else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Judging whether a launch actually worked
# ---------------------------------------------------------------------------
# A proxy that carries an ordinary HTTPS fetch is still not necessarily usable
# here, and the three ways it fails are worth telling apart because only two of
# them are worth retrying:
#
#   * opencode refuses the exit IP. Measured repeatedly: the free tier accepts
#     some addresses and rejects others, and because these free proxies rotate
#     their own exits per request, the *same* proxy can succeed and then fail
#     minutes later. Retry — the next proxy, or the next request, may pass.
#   * the proxy MITMs TLS. A free proxy presenting its own certificate breaks
#     the connection outright ("self signed certificate in certificate chain").
#     Retry.
#   * the proxy is simply dead mid-request. Retry.
#
# A model error is NOT retried: "Model is unavailable" means the id is wrong or
# retired, and trying ten more proxies would just fail ten more times slowly.
#
# This is why a plain "does it fetch a page" probe is not enough to pick a
# proxy. The only check that predicts a working launch is a launch.

#: Substrings marking a failure that a different exit IP could fix.
_RETRYABLE = (
    "can only be used from within opencode",
    "self signed certificate",
    "certificate chain",
    "unable to connect",
    "cannot connect to api",
    "socket connection was closed",
    "connection closed",
    "econnrefused",
    "etimedout",
    "tls",
    "ssl",
)

#: Substrings marking a failure that is about the model, not the route.
_MODEL_ERROR = (
    "model is unavailable",
    "model not found",
    "no such model",
)


def classify(text: str) -> str:
    """Judge an opencode transcript: ``"ok"``, ``"retry"`` or ``"model"``.

    Pure, so the suite can pin the mapping without spawning anything. The order
    matters: a model error is checked first, because a retired model id can
    arrive wrapped in text that also mentions a connection.
    """
    low = (text or "").lower()
    if any(m in low for m in _MODEL_ERROR):
        return "model"
    if any(m in low for m in _RETRYABLE):
        return "retry"
    return "ok"


def probe_launch(proxy: Proxy, binary: str, model: str | None = None,
                 timeout: float = 90.0) -> str:
    """Run one cheap opencode turn through *proxy* and report how it went.

    This is the pre-flight the TUI depends on. Opening the interactive TUI
    through a proxy that is going to be refused wastes the operator's time —
    they see the interface come up and then an error — so the route is proven
    with a throwaway one-shot first and only a proxy that answered gets the TUI.

    Returns ``"ok"``, ``"retry"`` or ``"model"`` as :func:`classify` does.
    """
    argv = build_argv(binary, model=model, prompt="say ok")
    try:
        proc = subprocess.run(
            argv, env=child_env(proxy), capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "retry"
    except OSError:
        return "retry"
    if proc.returncode == 0 and classify(proc.stdout + proc.stderr) == "ok":
        return "ok"
    return classify(proc.stdout + proc.stderr)


def find_working_proxy(binary: str, model: str | None = None,
                       attempts: int = 6, progress=None,
                       pool: Pool | None = None) -> tuple[Proxy | None, str]:
    """Rotate until a proxy actually completes a turn.

    Returns ``(proxy, note)``. ``proxy`` is None when nothing worked, and the
    note says why so the caller can be honest about it instead of opening a TUI
    that will fail.

    Two requirements have to hold at once, and they pull in opposite
    directions: the launch must work, so it wants a proxy that has completed a
    turn, and it must differ from the last launch, so it wants one that has not
    been handed out. In a pool built by :func:`validate_turns` *every* entry is
    proven, so "proven" alone would return the same proxy forever. The queue is
    therefore proven-and-unused first, then proven, then whatever the cursor
    reaches — and every attempt is stamped as used.

    On success the proxy is recorded in the pool and saved, which is what makes
    the next launch cheaper than this one.
    """
    pool = pool or load_pool()
    # The candidates for this launch: proven proxies the operator has not been
    # handed yet, then the plain cursor once those run out. `exclude` keeps the
    # fill from returning the same proxy repeatedly — and, unlike stamping
    # `used_at` as we go, it leaves a proxy that is only *considered* here still
    # available to the next launch.
    queue: list[Proxy] = []
    taken: set[str] = set()
    for _ in range(attempts):
        unused = pool.next_unused(exclude=taken)
        if unused is None:
            break
        queue.append(unused)
        taken.add(unused.key)
    last = ""
    tried = 0
    for i in range(attempts):
        if queue:
            proxy = queue.pop(0)
        else:
            proxy = pool.next_proxy(mark=True)
        if proxy is None:
            return None, "no proxies in the pool"
        tried += 1
        if progress is not None:
            progress(i + 1, attempts, proxy)
        verdict = probe_launch(proxy, binary, model=model)
        if verdict == "ok":
            # Remember it, so the next launch starts from here rather than from
            # a cold scan. This is the only place the pool learns anything.
            pool.mark_proven(proxy)
            pool.mark_used(proxy)
            save_pool(pool)
            return proxy, ""
        if verdict == "model":
            # The route is fine; the model id is not. Another proxy will not
            # help, so stop and let the caller report it. Not a failure of this
            # proxy, so it must not be retired for it.
            return proxy, "model unavailable"
        # A refusal retires the proxy: it is newer evidence than whatever proof
        # put it at the front of the queue, and without recording it a dead
        # proxy would be retried, and timed out, on every future launch.
        pool.mark_failed(proxy)
        pool.mark_used(proxy)
        last = verdict
    if tried:
        save_pool(pool)
    return None, f"{tried} proxies tried, none worked (last: {last})"


# ---------------------------------------------------------------------------
# The `opencode-zim` alias
# ---------------------------------------------------------------------------

#: The shim's body. It does not reimplement rotation — it calls back into this
#: module, so the shell path and the slash command cannot drift apart.
_SHIM = """#!/usr/bin/env bash
# {alias} — opencode behind a fresh rotating proxy (written by ZimZilla).
#
# Installed by /zim-opencode. Each run leaves from a different address; only
# this process is proxied, never the rest of the system. Delete this file to
# remove it.
set -euo pipefail
exec {python} -m zimzilla.opencode "$@"
"""


def alias_path() -> Path:
    """Where the alias goes: the same bin dir the repo installs `zimzilla` into."""
    override = os.environ.get("ZIMZILLA_BIN_DIR")
    if override:
        return Path(override).expanduser() / ALIAS
    return Path.home() / ".local" / "bin" / ALIAS


def install_alias() -> tuple[bool, str]:
    """Write the ``opencode-zim`` shim. Returns ``(ok, message)``.

    Idempotent: rewritten every time so a moved checkout re-points itself, the
    same way install.sh rewrites its own launcher.
    """
    import sys

    path = alias_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_SHIM.format(alias=ALIAS, python=sys.executable), encoding="utf-8")
        os.chmod(path, 0o755)
    except OSError as e:
        return False, f"could not write {path}: {e}"
    on_path = str(path.parent) in os.environ.get("PATH", "").split(os.pathsep)
    if not on_path:
        return True, f"{path} (add {path.parent} to PATH to use it)"
    return True, str(path)


def alias_installed() -> bool:
    p = alias_path()
    return p.is_file() and os.access(p, os.X_OK)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``opencode-zim`` shim.

    Kept deliberately small: rotate, say which address is in use, exec. The
    shim is a convenience for the shell, and the interesting logic stays in the
    functions above where the tests can reach it.
    """
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    binary = find_binary()
    if binary is None:
        print("opencode not found - install it please !", file=sys.stderr)
        return 127

    # A shell launch has no UI to show progress in, but it should not hand back
    # a TUI that is about to be refused either — so it converges on a working
    # route the same way the slash command does, just more quietly.
    proxy, note = find_working_proxy(binary, attempts=6)
    if proxy is None:
        print(f"zimzilla: no working proxy ({note}) — running from your own address",
              file=sys.stderr)
    else:
        ip = probe_exit_ip(proxy)
        print(f"zimzilla: routing through {proxy.url}"
              + (f" (exit {ip})" if ip else ""), file=sys.stderr)
    return run(proxy, binary, args=argv)


if __name__ == "__main__":
    raise SystemExit(main())
