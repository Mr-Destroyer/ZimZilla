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


def test_find_working_proxy():
    binary = "/usr/bin/opencode"
    pool = oc.Pool(proxies=[oc.Proxy("10.0.0.%d" % i, 80) for i in range(6)])

    # Third time lucky: the loop must keep going rather than give up early.
    calls: list[str] = []

    def flaky(proxy, b, model=None, timeout=None):
        calls.append(proxy.host)
        return "ok" if len(calls) == 3 else "retry"

    def nxt(refresh=False, progress=None):
        return pool.next_proxy()

    with mock.patch.object(oc, "rotate", side_effect=nxt):
        with mock.patch.object(oc, "probe_launch", side_effect=flaky):
            got, note = oc.find_working_proxy(binary, attempts=6)
    check("finds a working proxy after failures", got is not None and note == "", note)
    check("tried until it worked", len(calls) == 3, str(calls))

    # A model error must stop immediately: another proxy cannot fix a bad id,
    # and six attempts would just be six slow identical failures.
    with mock.patch.object(oc, "rotate", side_effect=nxt):
        with mock.patch.object(oc, "probe_launch", return_value="model") as pl:
            got, note = oc.find_working_proxy(binary, attempts=6)
    check("model error stops the loop", pl.call_count == 1, str(pl.call_count))
    check("model error is reported as such", "model" in note, note)

    with mock.patch.object(oc, "rotate", return_value=None):
        got, note = oc.find_working_proxy(binary, attempts=3)
    check("no proxies reported honestly", got is None and "no proxies" in note, note)

    with mock.patch.object(oc, "rotate", side_effect=nxt):
        with mock.patch.object(oc, "probe_launch", return_value="retry"):
            got, note = oc.find_working_proxy(binary, attempts=2)
    check("exhausted attempts reported honestly", got is None and "2 proxies" in note, note)


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
        test_find_working_proxy()
        test_child_env()
        test_build_argv()
        test_run_env_and_status()
        test_alias(tmp)
        test_missing_binary_message()
        test_binary_lookup(tmp)
        test_state_paths(tmp)
        test_ui_wiring()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all opencode checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
