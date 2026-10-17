"""`/zim-opencode`: the proxy pool, the rotation, and the launch decision.

Run:  python tests/test_opencode.py   (from an activated venv)

Protects what is particular about this feature, none of which is visible from
the command's happy path:

* **Rotation must survive a restart.** ``cursor`` is persisted, not held in
  memory. An in-memory counter would reset on every launch and hand the operator
  the same exit IP twice in a row — which is the one thing the command exists to
  prevent. Both the wraparound and the persistence are asserted.
* **A pool entry is only worth keeping if it carries traffic.** The probe is a
  real HTTPS fetch through the proxy, not a TCP connect: the common failure in
  these lists is a port that accepts a connection and then refuses to forward.
  A connect-only check would keep every one of them.
* **``classify`` is the whole retry policy, and it is pure.** Three verdicts,
  and the order matters — a retired model id can arrive wrapped in text that
  also mentions a connection, so "model" has to win. Getting this backwards
  turns one fast, actionable error into six slow, misleading ones.
* **``child_env`` sets both spellings and always bypasses localhost.** opencode's
  TUI runs its own local HTTP server; with every address proxied that localhost
  connection goes to the proxy too and the TUI breaks. Its own docs call the
  bypass required. And the lowercase spellings are read by different layers than
  the uppercase ones, so setting only one leaves a silent direct egress.
* **The missing-binary message is exact.** It was specified word for word.
* **The alias is idempotent and re-points itself.** A moved checkout must
  rewrite the shim rather than leave it pointing at a dead interpreter.

No test here touches the network: every fetch is served by a stub urlopen, and
every subprocess is mocked.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import opencode as oc  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILS.append(name)


class _Resp:
    """Minimal urlopen context manager returning a fixed body."""

    def __init__(self, body: str) -> None:
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------------------
# Parsing the lists
# ---------------------------------------------------------------------------

def test_parse_candidates():
    body = "\n".join([
        "# a comment",
        "",
        "1.2.3.4:8080",
        "1.2.3.4:8080",          # duplicate
        "http://5.6.7.8:3128",   # scheme prefix
        "9.9.9.9",               # no port
        "10.0.0.1:abc",          # port not numeric
        " 11.1.1.1:80  ",        # padded
        ":::",                   # junk
    ])
    got = oc.parse_candidates(body)
    pairs = [(p.host, p.port) for p in got]
    check("blank lines and comments skipped", pairs == [
        ("1.2.3.4", 8080), ("5.6.7.8", 3128), ("11.1.1.1", 80)], str(pairs))
    check("duplicates collapsed", len(got) == 3, str(pairs))
    check("scheme prefix stripped", got[1].host == "5.6.7.8", got[1].host)
    check("one malformed line does not cost the list", len(got) == 3, str(pairs))

    check("Proxy.url is a plain http URL",
          oc.Proxy("1.2.3.4", 8080).url == "http://1.2.3.4:8080")
    # SOCKS is rejected by opencode's fetch layer, so the pool must never
    # hand one out even if a list carries the scheme.
    check("no socks scheme survives parsing",
          not any("socks" in p.url for p in oc.parse_candidates("socks5://1.2.3.4:1080")))


# ---------------------------------------------------------------------------
# The pool: rotation, freshness, persistence
# ---------------------------------------------------------------------------

def test_pool_rotation():
    ps = [oc.Proxy("10.0.0.%d" % i, 8000 + i) for i in range(3)]
    pool = oc.Pool(proxies=ps)

    got = [pool.next_proxy().host for _ in range(4)]
    check("rotation walks the pool in order", got[:3] == ["10.0.0.0", "10.0.0.1", "10.0.0.2"], str(got))
    check("rotation wraps around", got[3] == "10.0.0.0", str(got))
    check("cursor advanced and wrapped", pool.cursor == 1, str(pool.cursor))

    check("empty pool yields None", oc.Pool().next_proxy() is None)


def test_pool_freshness():
    p = oc.Pool(proxies=[oc.Proxy("1.1.1.1", 80)], validated_at=1000.0)
    check("fresh just after validation", p.is_fresh(now=1000.0 + 60))
    check("stale past POOL_MAX_AGE", not p.is_fresh(now=1000.0 + oc.POOL_MAX_AGE + 1))
    # An empty pool is never fresh, or get_pool would skip the rebuild that
    # could fill it.
    check("empty pool is never fresh",
          not oc.Pool(validated_at=9e12).is_fresh(now=9e12))


def test_pool_persistence(tmp: Path):
    path = tmp / "pool.json"
    ps = [oc.Proxy("1.2.3.4", 8080, latency=0.4), oc.Proxy("5.6.7.8", 3128)]
    oc.save_pool(oc.Pool(proxies=ps, cursor=1, validated_at=123.0), path)
    back = oc.load_pool(path)
    check("round-trips the proxies",
          [(p.host, p.port, p.latency) for p in back.proxies]
          == [("1.2.3.4", 8080, 0.4), ("5.6.7.8", 3128, None)],
          str(back.proxies))
    check("round-trips the cursor", back.cursor == 1, str(back.cursor))
    check("round-trips validated_at", back.validated_at == 123.0)

    # The cursor is the whole point of persisting: a reloaded pool must resume
    # where it left off, not restart at the top.
    check("reloaded pool resumes rotation",
          oc.load_pool(path).next_proxy().host == "5.6.7.8")

    path.write_text("{not json", encoding="utf-8")
    check("corrupt pool degrades to empty", oc.load_pool(path).proxies == [])
    check("missing pool degrades to empty",
          oc.load_pool(tmp / "nope.json").proxies == [])

    # A read-only home must not break the launch.
    with mock.patch.object(Path, "write_text", side_effect=OSError("read-only")):
        oc.save_pool(oc.Pool(proxies=ps), tmp / "x.json")
    check("save failure is never fatal", True)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def test_probe_requires_real_traffic():
    p = oc.Proxy("1.2.3.4", 8080)
    with mock.patch("urllib.request.build_opener") as bo:
        bo.return_value.open.return_value = _Resp("203.0.113.7\n")
        got = oc._probe(p)
    check("live proxy returns with latency filled", got is not None and got.latency is not None,
          str(got))

    # A port that answers and then refuses to forward is the common failure;
    # it must be dropped, not kept.
    with mock.patch("urllib.request.build_opener") as bo:
        bo.return_value.open.side_effect = OSError("connection refused")
        check("dead proxy dropped", oc._probe(oc.Proxy("1.2.3.4", 8080)) is None)

    # Something that answers but is not an exit IP (a captive portal, a proxy
    # error page) must not be kept either.
    with mock.patch("urllib.request.build_opener") as bo:
        bo.return_value.open.return_value = _Resp("<html>blocked</html>")
        check("non-IP body rejected", oc._probe(oc.Proxy("1.2.3.4", 8080)) is None)


def test_validate_sorts_and_caps():
    cands = [oc.Proxy("10.0.0.%d" % i, 80) for i in range(10)]

    def fake(proxy, timeout=None):
        proxy.latency = float(int(proxy.host.rsplit(".", 1)[1]))
        return proxy

    with mock.patch.object(oc, "_probe", side_effect=fake):
        live = oc.validate(cands, target=4, workers=4)
    check("stops at the target", len(live) == 4, str(len(live)))
    check("fastest first", [p.latency for p in live] == sorted(p.latency for p in live),
          str([p.latency for p in live]))

    with mock.patch.object(oc, "_probe", return_value=None):
        check("all-dead sample yields nothing", oc.validate(cands, target=4) == [])
    check("empty sample is not an error", oc.validate([]) == [])


def test_progress_callback():
    seen: list[tuple[int, int]] = []
    with mock.patch.object(oc, "_probe", return_value=None):
        oc.validate([oc.Proxy("10.0.0.%d" % i, 80) for i in range(5)],
                    progress=lambda d, t: seen.append((d, t)))
    check("progress reports every probe", seen and seen[-1] == (5, 5), str(seen))


# ---------------------------------------------------------------------------
# The retry policy
# ---------------------------------------------------------------------------

def test_classify():
    # The refusal opencode returns for a Tor exit — the failure that started
    # this whole design.
    check("free-tier refusal is retryable",
          oc.classify("OpenCode's free tier can only be used from within OpenCode") == "retry")
    check("MITM certificate is retryable",
          oc.classify("self signed certificate in certificate chain") == "retry")
    check("dead proxy is retryable",
          oc.classify("Unable to connect. Is the computer able to access the url?") == "retry")
    check("retired model is a model error",
          oc.classify("Model is unavailable") == "model")
    check("a clean transcript is ok", oc.classify("> build · deepseek ·\nok") == "ok")
    check("empty transcript is ok", oc.classify("") == "ok")
    check("None transcript is ok", oc.classify(None) == "ok")
    # Order matters: this text mentions a connection AND a model error, and the
    # model error is the one that another proxy cannot fix.
    check("model error wins over connection noise",
          oc.classify("model not found — connection closed") == "model")


def test_probe_launch_verdicts():
    binary = "/usr/bin/opencode"
    proxy = oc.Proxy("1.2.3.4", 8080)

    with mock.patch("subprocess.run") as sr:
        sr.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
        check("rc=0 and clean output is ok", oc.probe_launch(proxy, binary) == "ok")

    with mock.patch("subprocess.run") as sr:
        sr.return_value = mock.Mock(returncode=1, stdout="", stderr="Model is unavailable")
        check("retired model reported as model", oc.probe_launch(proxy, binary) == "model")

    with mock.patch("subprocess.run") as sr:
        sr.return_value = mock.Mock(returncode=1, stdout="",
                                    stderr="self signed certificate in certificate chain")
        check("MITM reported as retry", oc.probe_launch(proxy, binary) == "retry")

    # A hung proxy must not hang the launch.
    with mock.patch("subprocess.run", side_effect=oc.subprocess.TimeoutExpired("x", 1)):
        check("timeout reported as retry", oc.probe_launch(proxy, binary) == "retry")

    # rc=0 with a refusal in the output is still a refusal — exit status alone
    # is not the signal.
    with mock.patch("subprocess.run") as sr:
        sr.return_value = mock.Mock(returncode=0, stdout="",
                                    stderr="can only be used from within opencode")
        check("refusal on rc=0 still retries", oc.probe_launch(proxy, binary) == "retry")


def test_find_working_proxy(tmp: Path):
    binary = "/usr/bin/opencode"
    pool = oc.Pool(proxies=[oc.Proxy("10.0.0.%d" % i, 80) for i in range(6)])

    # Third time lucky: the loop must keep going rather than give up early.
    calls: list[str] = []

    def flaky(proxy, b, model=None, timeout=None):
        calls.append(proxy.host)
        return "ok" if len(calls) == 3 else "retry"

    with mock.patch.object(oc, "save_pool"):
        with mock.patch.object(oc, "probe_launch", side_effect=flaky):
            got, note = oc.find_working_proxy(binary, attempts=6, pool=pool)
    check("finds a working proxy after failures", got is not None and note == "", note)
    check("tried until it worked", len(calls) == 3, str(calls))

    # A model error must stop immediately: another proxy cannot fix a bad id,
    # and six attempts would just be six slow identical failures.
    p2 = oc.Pool(proxies=[oc.Proxy("10.1.0.%d" % i, 80) for i in range(6)])
    with mock.patch.object(oc, "probe_launch", return_value="model") as pl:
        got, note = oc.find_working_proxy(binary, attempts=6, pool=p2)
    check("model error stops the loop", pl.call_count == 1, str(pl.call_count))
    check("model error is reported as such", "model" in note, note)

    with mock.patch.object(oc, "probe_launch", return_value="retry"):
        got, note = oc.find_working_proxy(binary, attempts=3, pool=oc.Pool())
    check("no proxies reported honestly", got is None and "no proxies" in note, note)

    p3 = oc.Pool(proxies=[oc.Proxy("10.2.0.%d" % i, 80) for i in range(6)])
    with mock.patch.object(oc, "probe_launch", return_value="retry"):
        got, note = oc.find_working_proxy(binary, attempts=2, pool=p3)
    check("exhausted attempts reported honestly", got is None and "2 proxies" in note, note)


def test_every_launch_is_a_different_proxy():
    """The requirement the command exists for: a second launch must leave from
    a different address than the first.

    This is not free. A pool built by validate_turns has *every* entry proven,
    so "prefer the proven proxy" and "be different each time" contradict each
    other, and the naive implementation of the first silently defeats the
    second. This is the regression test for that.
    """
    now = time.time()
    pool = oc.Pool(proxies=[oc.Proxy("10.0.0.%d" % i, 80, proven_at=now)
                            for i in range(5)])

    with mock.patch.object(oc, "save_pool"):
        with mock.patch.object(oc, "probe_launch", return_value="ok"):
            seen = []
            for _ in range(5):
                got, _ = oc.find_working_proxy("/usr/bin/opencode", attempts=4, pool=pool)
                seen.append(got.key)

    check("five launches gave five distinct proxies",
          len(set(seen)) == 5, str(seen))
    check("no proxy repeated before the pool was exhausted",
          len(seen) == len(set(seen)), str(seen))

    # Once every proven proxy has been handed out, the cursor must take over
    # rather than the command failing — a small pool has to cycle.
    with mock.patch.object(oc, "save_pool"):
        with mock.patch.object(oc, "probe_launch", return_value="ok"):
            more = [oc.find_working_proxy("/usr/bin/opencode", attempts=4,
                                          pool=pool)[0].key for _ in range(5)]
    check("an exhausted pool keeps cycling", all(more), str(more))
    check("cycling does not stall on one proxy", len(set(more)) > 1, str(more))


def test_a_dead_proxy_stops_being_trusted():
    """A failure is newer evidence than a proof, so it cancels it.

    Without this, a proxy that worked yesterday and died overnight keeps its
    place at the head of the queue and is retried — and timed out, ~40s — on
    every launch before the pool moves on.
    """
    now = time.time()
    dead = oc.Proxy("10.0.0.1", 80, proven_at=now - 300)
    alive = oc.Proxy("10.0.0.2", 80, proven_at=now - 300)
    pool = oc.Pool(proxies=[dead, alive])

    check("both start out trusted", len(pool.known_good()) == 2,
          str(len(pool.known_good())))
    pool.mark_failed(dead)
    check("a failed proxy is no longer proven", not dead.is_proven())
    check("the other one is unaffected", alive.is_proven())
    check("known_good drops the failed one",
          [p.key for p in pool.known_good()] == [alive.key],
          str([p.key for p in pool.known_good()]))

    # Ordering matters: a failure only cancels a proof it is newer than.
    old_fail = oc.Proxy("10.0.0.3", 80, proven_at=now, failed_at=now - 500)
    check("a failure older than the proof does not cancel it", old_fail.is_proven())

    # Recovering must clear the failure, or a proxy that comes back is never
    # trusted again.
    pool.mark_proven(dead)
    check("a proxy that works again is trusted again", dead.is_proven())
    check("recovery clears failed_at", dead.failed_at == 0.0, str(dead.failed_at))

    # And a refusal during a hunt must be persisted, not just held in memory.
    failing = oc.Pool(proxies=[oc.Proxy("10.0.0.9", 80, proven_at=now)])
    with mock.patch.object(oc, "save_pool") as sp:
        with mock.patch.object(oc, "probe_launch", return_value="retry"):
            got, note = oc.find_working_proxy("/usr/bin/opencode", attempts=1,
                                              pool=failing)
    check("a refused hunt returns nothing", got is None, str(got))
    check("the refusal is recorded", failing.proxies[0].failed_at > 0)
    check("a refused pool is still saved", sp.called)


def test_failed_at_survives_a_save():
    p = oc.Proxy("1.2.3.4", 8080, proven_at=100.0, failed_at=150.0)
    check("a later failure cancels the proof", not p.is_proven(now=200.0))
    path = oc.pool_file()
    oc.save_pool(oc.Pool(proxies=[p], validated_at=1.0), path)
    back = oc.load_pool(path).proxies[0]
    check("failed_at survives a save/load", back.failed_at == 150.0, str(back.failed_at))
    check("so the dead proxy stays retired after a restart", not back.is_proven(now=200.0))
    path.write_text('{"proxies":[{"host":"9.9.9.9","port":80,"proven_at":5.0}],'
                    '"cursor":0,"validated_at":1.0}', encoding="utf-8")
    check("a pre-failed_at pool file loads", oc.load_pool(path).proxies[0].failed_at == 0.0)


def test_used_at_survives_a_save():
    """used_at is what makes rotation survive a restart. An in-memory-only
    marker would reset on the next process and hand back the same address."""
    path = oc.pool_file()
    p = oc.Proxy("1.2.3.4", 8080, proven_at=100.0, used_at=200.0)
    oc.save_pool(oc.Pool(proxies=[p], validated_at=1.0), path)
    back = oc.load_pool(path).proxies[0]
    check("used_at survives a save/load", back.used_at == 200.0, str(back.used_at))
    # A pool file written before used_at existed must still load.
    path.write_text('{"proxies":[{"host":"9.9.9.9","port":80,"proven_at":5.0}],'
                    '"cursor":0,"validated_at":1.0}', encoding="utf-8")
    old = oc.load_pool(path).proxies[0]
    check("a pre-used_at pool file loads", old.used_at == 0.0, str(old.used_at))
    check("and its proof is still honoured", old.is_proven(now=6.0))


def test_known_good_is_tried_first(tmp: Path):
    """The pool's memory is the point: a proxy that worked once is better
    evidence than any liveness measurement, so it must be tried before the
    rest — and remembered after."""
    stale = [oc.Proxy("10.0.0.%d" % i, 80) for i in range(4)]
    proven = oc.Proxy("10.9.9.9", 8080, proven_at=time.time() - 60)
    pool = oc.Pool(proxies=stale + [proven])

    check("known_good finds the proven one",
          [p.key for p in pool.known_good()] == [proven.key],
          str([p.key for p in pool.known_good()]))

    # A proven proxy is tried first, whatever its position in the list.
    order: list[str] = []
    with mock.patch.object(oc, "save_pool"):
        with mock.patch.object(oc, "probe_launch",
                               side_effect=lambda p, b, model=None, timeout=None:
                               (order.append(p.key), "ok")[1]):
            got, _ = oc.find_working_proxy("/usr/bin/opencode", attempts=6, pool=pool)
    check("proven proxy tried first", order and order[0] == proven.key, str(order))
    check("proven proxy returned", got is not None and got.key == proven.key)

    # Success is recorded, which is what makes the next launch cheaper.
    fresh = oc.Pool(proxies=[oc.Proxy("10.5.5.5", 80)])
    with mock.patch.object(oc, "save_pool") as sp:
        with mock.patch.object(oc, "probe_launch", return_value="ok"):
            got, _ = oc.find_working_proxy("/usr/bin/opencode", attempts=3, pool=fresh)
    check("success is marked proven", got.proven_at > 0, str(got.proven_at))
    check("pool was saved after success", sp.called)
    check("marked proxy is now known_good", fresh.known_good() != [])

    # An aged-out success must not be trusted forever.
    old = oc.Proxy("10.7.7.7", 80, proven_at=time.time() - oc.KNOWN_GOOD_AGE - 1)
    check("stale proof is not trusted", not old.is_proven())
    check("fresh proof is trusted", oc.Proxy("1.1.1.1", 80, proven_at=time.time()).is_proven())
    check("never-proven is not trusted", not oc.Proxy("1.1.1.1", 80).is_proven())


def test_proven_at_round_trips(tmp: Path):
    path = tmp / "pool2.json"
    p = oc.Proxy("1.2.3.4", 8080, latency=0.5, proven_at=1234.0)
    oc.save_pool(oc.Pool(proxies=[p], validated_at=1.0), path)
    back = oc.load_pool(path).proxies[0]
    check("proven_at survives a save/load", back.proven_at == 1234.0, str(back.proven_at))
    check("latency still survives", back.latency == 0.5)
    # An old pool file written before this field existed must still load.
    path.write_text(json.dumps({"proxies": [{"host": "1.1.1.1", "port": 80}],
                                "cursor": 0, "validated_at": 1.0}), encoding="utf-8")
    check("a pre-proven_at pool file still loads",
          oc.load_pool(path).proxies[0].proven_at == 0.0)


def test_build_pool_two_stages():
    """The decisive stage: liveness alone is not enough, so the survivors of
    the cheap probe must get a real turn before they are kept."""
    cands = [oc.Proxy("10.0.0.%d" % i, 80) for i in range(10)]

    with mock.patch.object(oc, "fetch_candidates", return_value=cands):
        with mock.patch.object(oc, "_probe",
                               side_effect=lambda p, timeout=None: p):
            # No binary: degraded path, keeps the liveness result so the caller
            # (which has already reported opencode missing) still gets a pool.
            with mock.patch.object(oc, "save_pool"):
                p = oc.build_pool(target=4, binary=None)
            check("no binary falls back to liveness", len(p.proxies) == 4, str(len(p.proxies)))

        with mock.patch.object(oc, "_probe",
                               side_effect=lambda p, timeout=None: p):
            seen: list[str] = []

            def fake_turn(proxy, binary, model=None, timeout=None):
                seen.append(proxy.key)
                return "ok" if len(seen) <= 2 else "retry"

            with mock.patch.object(oc, "save_pool"):
                with mock.patch.object(oc, "probe_launch", side_effect=fake_turn):
                    p = oc.build_pool(target=2, binary="/usr/bin/opencode")
            check("only turn-proven proxies are kept", len(p.proxies) == 2, str(len(p.proxies)))
            check("the decisive stage actually ran", len(seen) > 0, str(len(seen)))
            # A pool that arrives unstamped hands the next launch a set of
            # proxies it cannot prefer, so the memory is built and discarded.
            # Passing a turn must be recorded at the moment it passes.
            check("survivors are stamped proven_at",
                  all(x.proven_at > 0 for x in p.proxies),
                  str([x.proven_at for x in p.proxies]))
            check("a freshly built pool knows its good proxies",
                  len(p.known_good()) == 2, str(len(p.known_good())))

        # A turn-stage that finds nothing must not persist an empty pool.
        with mock.patch.object(oc, "_probe",
                               side_effect=lambda p, timeout=None: p):
            with mock.patch.object(oc, "probe_launch", return_value="retry"):
                with mock.patch.object(oc, "save_pool") as sp:
                    p = oc.build_pool(target=2, binary="/usr/bin/opencode")
            check("all-refused build yields an empty pool", p.proxies == [])
            check("empty pool is not saved", not sp.called)


# ---------------------------------------------------------------------------
# The child environment
# ---------------------------------------------------------------------------

def test_child_env():
    proxy = oc.Proxy("1.2.3.4", 8080)
    env = oc.child_env(proxy, base={"PATH": "/usr/bin"})

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                 "ALL_PROXY", "all_proxy"):
        check(f"{name} set", env.get(name) == "http://1.2.3.4:8080", str(env.get(name)))
    # Without the bypass opencode's own local server is proxied and the TUI
    # breaks — its docs call this required.
    for name in ("NO_PROXY", "no_proxy"):
        check(f"{name} bypasses localhost",
              env.get(name) == "localhost,127.0.0.1,::1", str(env.get(name)))
    check("base environment preserved", env.get("PATH") == "/usr/bin")

    # Nothing global: building a child env must not touch this process's own
    # environment, which is what keeps the proxy inside ZimZilla.
    before = dict(os.environ)
    oc.child_env(proxy)
    check("os.environ not polluted", dict(os.environ) == before)


def test_build_argv():
    b = "/usr/bin/opencode"
    check("TUI with no model is a bare argv", oc.build_argv(b) == [b])
    check("TUI with a model passes -m",
          oc.build_argv(b, model="opencode/x") == [b, "-m", "opencode/x"])
    check("passthrough args reach the TUI",
          oc.build_argv(b, args=["--auto"], model="m") == [b, "-m", "m", "--auto"])
    # One-shot form: `run` and the prompt, with the default model when none
    # was named — a `run` with no -m would not be the free model.
    check("one-shot form uses run -m <model> <prompt>",
          oc.build_argv(b, prompt="hi") == [b, "run", "-m", oc.DEFAULT_MODEL, "hi"],
          str(oc.build_argv(b, prompt="hi")))


def test_run_env_and_status():
    proxy = oc.Proxy("1.2.3.4", 8080)
    with mock.patch("subprocess.call", return_value=0) as sc:
        rc = oc.run(proxy, "/usr/bin/opencode", args=["--auto"], cwd="/tmp")
    check("exit status propagated", rc == 0, str(rc))
    argv, kw = sc.call_args[0][0], sc.call_args[1]
    check("child got the proxied env",
          kw["env"]["HTTPS_PROXY"] == "http://1.2.3.4:8080")
    check("child ran in the given cwd", kw["cwd"] == "/tmp")
    check("argv is the TUI form", argv == ["/usr/bin/opencode", "--auto"], str(argv))

    # No proxy means inherit the environment untouched — the honest fallback,
    # which the caller is required to have announced first.
    with mock.patch("subprocess.call", return_value=0) as sc:
        oc.run(None, "/usr/bin/opencode")
    check("no proxy inherits the environment", sc.call_args[1]["env"] is None)

    with mock.patch("subprocess.call", side_effect=oc.subprocess.TimeoutExpired("x", 1)):
        check("timeout becomes rc 124", oc.run(proxy, "/usr/bin/opencode") == 124)


# ---------------------------------------------------------------------------
# The alias
# ---------------------------------------------------------------------------

def test_alias(tmp: Path):
    bin_dir = tmp / "bin"
    with mock.patch.dict(os.environ, {"ZIMZILLA_BIN_DIR": str(bin_dir)}):
        check("alias path honours the override",
              oc.alias_path() == bin_dir / oc.ALIAS, str(oc.alias_path()))
        check("alias reports not installed before install", not oc.alias_installed())

        ok, detail = oc.install_alias()
        check("install reports success", ok, detail)
        check("alias exists and is executable",
              oc.alias_installed(), detail)
        mode = oc.alias_path().stat().st_mode
        check("alias is mode 755", stat.S_IMODE(mode) == 0o755, oct(stat.S_IMODE(mode)))

        body = oc.alias_path().read_text(encoding="utf-8")
        check("alias is a bash shim", body.startswith("#!/usr/bin/env bash"), body[:40])
        check("alias does not reimplement rotation", "exec " in body and "zimzilla.opencode" in body)
        # The shim must name the interpreter that has zimzilla importable, not
        # a bare `python3` that may be a different one.
        check("alias pins the interpreter", sys.executable in body, body)

        # Re-running must re-point a moved checkout rather than leave a stale
        # interpreter behind.
        with mock.patch("sys.executable", "/other/python"):
            ok2, _ = oc.install_alias()
        check("reinstall rewrites the interpreter",
              "/other/python" in oc.alias_path().read_text(encoding="utf-8"))
        check("reinstall reports success", ok2)

    os.environ.pop("ZIMZILLA_BIN_DIR", None)
    check("alias defaults to ~/.local/bin",
          oc.alias_path() == Path.home() / ".local" / "bin" / oc.ALIAS,
          str(oc.alias_path()))


def test_missing_binary_message():
    """The wording was specified exactly, and the exit status has to be a
    not-found code so a script around the shim can tell it apart."""
    import contextlib
    import io

    with mock.patch.object(oc, "find_binary", return_value=None):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = oc.main([])
    check("missing binary message is exact",
          buf.getvalue().strip() == "opencode not found - install it please !",
          repr(buf.getvalue()))
    check("missing binary exits 127", rc == 127, str(rc))


def test_binary_lookup(tmp: Path):
    fake = tmp / "opencode"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)

    with mock.patch("shutil.which", return_value=str(fake)):
        check("PATH hit wins", oc.find_binary() == str(fake))
    with mock.patch("shutil.which", return_value=None):
        with mock.patch.object(Path, "home", return_value=tmp):
            (tmp / ".local" / "bin").mkdir(parents=True, exist_ok=True)
            shim = tmp / ".local" / "bin" / "opencode"
            shim.write_text("#!/bin/sh\n", encoding="utf-8")
            shim.chmod(0o755)
            # A session launched from a desktop entry inherits a PATH without
            # ~/.local/bin, so the fallback is what makes "installed" true.
            check("falls back to ~/.local/bin", oc.find_binary() == str(shim))
    with mock.patch("shutil.which", return_value=None):
        with mock.patch.object(Path, "home", return_value=tmp / "empty"):
            check("reports None when truly absent", oc.find_binary() is None)
            check("is_installed agrees", not oc.is_installed())


def test_state_paths(tmp: Path):
    with mock.patch.dict(os.environ, {"ZIMZILLA_HOME": str(tmp / "state")}):
        check("state dir honours ZIMZILLA_HOME",
              oc.state_dir() == tmp / "state" / "opencode", str(oc.state_dir()))
        check("pool file lives in the state dir",
              oc.pool_file() == tmp / "state" / "opencode" / "pool.json",
              str(oc.pool_file()))
        check("state dir is created", oc.state_dir().is_dir())


# ---------------------------------------------------------------------------
# The UI wiring
# ---------------------------------------------------------------------------

def test_ui_wiring():
    from zimzilla.ui import app as app_mod
    from zimzilla.ui import complete as complete_mod

    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    check("dispatch has the command", '"zim-opencode":' in src)
    check("handler exists", "def _cmd_zim_opencode" in src)
    check("launch suspends the TUI", "with self.suspend():" in src)
    check("busy sessions still allow it", '"zim-opencode",' in src)
    # The terminal hand-off must not happen on the worker thread.
    check("launch runs on the UI thread",
          "call_from_thread(self._zim_opencode_launch" in src)

    check("completion popup lists it",
          any(c == "/zim-opencode" for c, _ in complete_mod.SLASH_COMMANDS))

    # The `--refresh` flag has to reach the pool loader, or it is a no-op that
    # still prints "refreshing".
    check("--refresh reaches the worker",
          "refresh: bool = False" in src and "get_pool(refresh=refresh" in src)

    # These proxies are flaky per-request, so the launch can succeed and the
    # courtesy exit-IP lookup still fail. That must not render "(exit None)" —
    # it reads as a failure at the exact moment the route proved itself. The
    # parenthetical is built only when the IP is known.
    check("exit IP is guarded, never '(exit None)'",
          "elif ip:" in src and 'where = f"{proxy.url} (exit {ip})"' in src)


def test_hunt_spinner():
    """The waiting screen: the exact wording asked for, no counts, and an
    animation driven by the UI thread rather than by the worker."""
    from zimzilla.ui import app as app_mod
    from zimzilla.ui.widgets import ChatPane

    check("the hunting message is the one asked for",
          app_mod._HUNTING == "ZIM IS MAKING IT UNLIMITED - FINDING THE BEST PROXIES ...",
          app_mod._HUNTING)
    check("it does not announce a count",
          not any(c.isdigit() for c in app_mod._HUNTING), app_mod._HUNTING)
    check("spinner has multiple frames", len(app_mod._HUNT_FRAMES) > 3,
          str(len(app_mod._HUNT_FRAMES)))
    check("spinner frames are single-width",
          all(len(f) == 1 for f in app_mod._HUNT_FRAMES), app_mod._HUNT_FRAMES)

    # The animation must be on a UI timer: the worker blocks in single socket
    # calls for tens of seconds, so a callback-driven spinner would freeze for
    # exactly the stretch being waited through.
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    check("spinner runs on a UI interval",
          "set_interval(0.12, self._pulse_hunt_spinner)" in src)
    check("spinner is stopped after the hunt",
          "self._hunt_timer.stop()" in src)
    check("spinner state is initialised in __init__",
          "self._hunt_timer = None" in src and "self._hunt_frame = 0" in src)
    # A timer can fire during teardown; the pane lookup must tolerate it.
    check("pane lookup tolerates teardown", "def _chat_pane" in src)

    check("ChatPane has a spinner slot", "id=\"spinner\"" in ChatPane.DEFAULT_CSS
          or hasattr(ChatPane, "set_spinner"))
    check("ChatPane can set and clear it",
          hasattr(ChatPane, "set_spinner") and hasattr(ChatPane, "clear_spinner"))

    # No counts and no unproven addresses on screen while it works.
    check("no proxy count is printed while hunting",
          "proxies live" not in src)
    check("no per-attempt address is printed", "attempt {i}/{n}" not in src)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zz-opencode-"))
    try:
        test_parse_candidates()
        test_pool_rotation()
        test_pool_freshness()
        test_pool_persistence(tmp)
        test_probe_requires_real_traffic()
        test_validate_sorts_and_caps()
        test_progress_callback()
        test_classify()
        test_probe_launch_verdicts()
        test_find_working_proxy(tmp)
        test_every_launch_is_a_different_proxy()
        test_a_dead_proxy_stops_being_trusted()
        test_failed_at_survives_a_save()
        test_used_at_survives_a_save()
        test_known_good_is_tried_first(tmp)
        test_proven_at_round_trips(tmp)
        test_build_pool_two_stages()
        test_child_env()
        test_build_argv()
        test_run_env_and_status()
        test_alias(tmp)
        test_missing_binary_message()
        test_binary_lookup(tmp)
        test_state_paths(tmp)
        test_ui_wiring()
        test_hunt_spinner()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all opencode checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
