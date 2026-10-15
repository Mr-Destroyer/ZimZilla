"""Pane messaging: mailbox semantics, tool gating, and the generated tmux config.

Run:  python tests/test_panes.py   (from an activated venv)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import panes, tools  # noqa: E402
from zimzilla import __main__ as main_mod  # noqa: E402
from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import Config  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILS.append(name)


def has_bind(keys: str, key: str, command: str) -> bool:
    """True if ``key`` is bound at top level in the prefix table to ``command``.

    A plain substring test is not enough: tmux's stock prefix table carries one
    enormous ``display-menu`` binding whose *arguments* contain ``h { split-window
    -h }``, so searching for "h" and "split-window -h" matches a server that has
    no ``h`` binding at all — which is precisely the state being tested for.
    Anchoring on ``bind-key -T prefix <key>`` only matches a real binding.
    """
    pat = re.compile(rf"^\s*bind-key\s+-T prefix\s+{re.escape(key)}\s+{re.escape(command)}")
    return any(pat.match(line) for line in keys.splitlines())


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zz-panes-"))
    root = panes.mailbox_root(tmp, "zimzilla")

    # --- mailbox ----------------------------------------------------------
    panes.register(root, "1")
    panes.register(root, "2")
    check("register creates inboxes", set(panes.known_agents(root)) == {"1", "2"})

    panes.send(root, "1", "2", "hello from 1")
    panes.send(root, "1", "2", "second")
    got = panes.drain(root, "2")
    check("drain returns both messages in order",
          [m["body"] for m in got] == ["hello from 1", "second"], str(got))
    check("drain clears the inbox", panes.drain(root, "2") == [])
    check("sender recorded", got and got[0]["from"] == "1")

    try:
        panes.send(root, "1", "9", "x")
        check("unknown recipient rejected", False)
    except ValueError:
        check("unknown recipient rejected", True)

    try:
        panes.send(root, "1", "2", "   ")
        check("empty body rejected", False)
    except ValueError:
        check("empty body rejected", True)

    # path traversal in names is stripped to a safe slug
    check("name sanitised", panes._safe_name("../../etc") == "etc")

    # --- tool gating ------------------------------------------------------
    cfg = Config.from_env(workdir=tmp, state_dir=tmp / "state")
    a_main = Agent(cfg, can_peer=True)
    a_plain = Agent(cfg)
    names_main = {t["name"] for t in a_main.tool_schemas()}
    names_plain = {t["name"] for t in a_plain.tool_schemas()}
    check("pane main agent sees peer tools",
          {"agent_peers", "agent_send", "agent_inbox"} <= names_main)
    check("ordinary session has no peer tools",
          not (names_plain & {"agent_peers", "agent_send", "agent_inbox"}))
    check("denied outside a pane", "only available" in (a_plain._denied("agent_send") or ""))

    # tool execution outside a pane gives a clear error, not a crash
    os.environ.pop(panes.ENV_SESSION, None)
    r = tools.execute("agent_peers", {}, cfg)
    check("peer tool outside pane reports error", r.is_error and "tmux pane" in r.output, r.output)

    # --- inside a simulated pane -----------------------------------------
    os.environ[panes.ENV_SESSION] = "zimzilla"
    os.environ["TMUX_PANE"] = "%7"
    cfg2 = Config.from_env(workdir=tmp, state_dir=tmp / "state")
    panes.register(panes.mailbox_root(cfg2.state_dir, "zimzilla"), "2")
    r = tools.execute("agent_send", {"to": "2", "body": "ping"}, cfg2)
    check("agent_send succeeds in pane", not r.is_error, r.output)
    r = tools.execute("agent_peers", {}, cfg2)
    check("agent_peers lists peers", "2" in r.output and "7" in r.output, r.output)

    # the receiving pane gets it through the agent's peer_inbox hook
    os.environ["TMUX_PANE"] = "%2"
    cfg3 = Config.from_env(workdir=tmp, state_dir=tmp / "state")
    text = panes.format_messages(panes.drain(panes.mailbox_root(cfg3.state_dir, "zimzilla"), "2"))
    check("receiver sees message", "ping" in text and "from 7" in text, text)

    # --- /panes: the relaunch request -------------------------------------
    # `transcript_of` is what makes "your session continues there" true: it
    # flattens the wire-format history into the (role, text) pairs the boot
    # replay draws. Tool traffic is dropped — the panels are live progress, and
    # a wall of them for finished work would bury the conversation.
    check("Relaunch carries the resume path",
          panes.Relaunch(resume="/x/y.json").resume == "/x/y.json")
    check("Relaunch defaults to no resume", panes.Relaunch().resume is None)

    msgs = [
        {"role": "user", "content": "what port"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "4001"},
            {"type": "tool_use", "name": "bash", "id": "t1", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "secret output"},
        ]},
        {"role": "assistant", "content": "done"},
    ]
    pairs = panes.transcript_of(msgs)
    check("transcript_of keeps prose in order",
          pairs == [("user", "what port"), ("assistant", "4001"), ("assistant", "done")],
          str(pairs))
    check("transcript_of drops tool traffic",
          not any("secret output" in t for _, t in pairs), str(pairs))
    check("transcript_of on empty history", panes.transcript_of([]) == [])
    check("transcript_of skips blank turns",
          panes.transcript_of([{"role": "user", "content": "   "}]) == [])
    check("transcript_of tolerates junk blocks",
          panes.transcript_of([{"role": "assistant", "content": [None, 7, {}]}]) == [])

    # --- /panes: the pane command's argv ----------------------------------
    # `--panes` would recurse and `--resume` belongs to the first pane only, so
    # both are stripped from what the ctrl+b h/v bindings run. Everything else
    # the operator typed must survive, or `--panes --theme amber` would open
    # green panes.
    check("argv: --panes stripped",
          main_mod._pane_inner_argv(["--panes"]) == [])
    check("argv: other flags survive",
          main_mod._pane_inner_argv(["--panes", "--theme", "amber"]) == ["--theme", "amber"])
    check("argv: --resume VALUE stripped",
          main_mod._pane_inner_argv(["--panes", "--resume", "/s.json"]) == [])
    check("argv: --resume=VALUE stripped",
          main_mod._pane_inner_argv(["--panes", "--resume=/s.json", "-m", "x"]) == ["-m", "x"])
    check("argv: unrelated value kept",
          main_mod._pane_inner_argv(["--panes", "-m", "x"]) == ["-m", "x"])

    # --- /panes: the command itself ---------------------------------------
    # The app cannot start tmux while it holds the terminal, so the command
    # exits with a Relaunch request and __main__ performs it. These drive the
    # command with the app unmounted, stubbing the two things that need a
    # mounted DOM.
    from zimzilla.ui.app import ZimZillaApp  # noqa: E402
    from zimzilla import session as session_mod  # noqa: E402

    acfg = Config.from_env(workdir=tmp / "appwd", state_dir=tmp / "appstate")
    app = ZimZillaApp(acfg)
    said: list[str] = []
    app._sys_line = lambda msg, **k: said.append(msg)
    exited: dict = {}
    app.exit = lambda result=None, **k: exited.update(result=result)

    # Inside a pane already: nothing to do, and it must not exit.
    os.environ[panes.ENV_SESSION] = "zimzilla"
    os.environ["TMUX_PANE"] = "%1"
    app._cmd_panes()
    check("/panes inside a pane does not exit", "result" not in exited, str(exited))
    check("/panes inside a pane says the keys",
          any("ctrl+b h" in s for s in said), str(said))
    os.environ.pop(panes.ENV_SESSION, None)
    os.environ.pop("TMUX_PANE", None)

    # Outside a pane with tmux present: save, then ask to relaunch.
    said.clear(); exited.clear()
    app._cmd_panes()
    r = exited.get("result")
    check("/panes outside a pane returns a Relaunch", isinstance(r, panes.Relaunch), str(exited))
    check("/panes saves the conversation to resume from",
          bool(r and r.resume and Path(r.resume).exists()), str(r))
    check("/panes announces the relaunch",
          any("relaunching under tmux" in s for s in said), str(said))

    # No tmux: refuse, and do not exit — exiting would leave no session at all.
    said.clear(); exited.clear()
    with mock.patch.object(panes, "tmux_available", lambda: False):
        app._cmd_panes()
    check("/panes without tmux does not exit", "result" not in exited, str(exited))
    check("/panes without tmux says how to fix it",
          any("tmux is not installed" in s for s in said), str(said))

    # A failed save must not cost the operator the relaunch — panes still work
    # without history, so the command carries on with resume=None.
    said.clear(); exited.clear()
    with mock.patch.object(session_mod, "save", side_effect=OSError("read-only")):
        app._cmd_panes()
    r = exited.get("result")
    check("/panes survives a failed save", isinstance(r, panes.Relaunch), str(exited))
    check("/panes reports the failed save and drops resume",
          r is not None and r.resume is None
          and any("could not save" in s for s in said), f"{r} {said}")

    # --- tmux config ------------------------------------------------------
    conf = panes.tmux_conf("/usr/bin/zimzilla --workdir '/tmp/x y'")
    check("prefix is ctrl+b", "set -g prefix C-b" in conf)
    check("bind h is horizontal split", "bind h split-window -h" in conf)
    check("bind v is vertical split", "bind v split-window -v" in conf)
    check("standalone hint needs one prefix", "ctrl+b ctrl+b" not in conf)

    # Nested inside another tmux the outer server eats the prefix, so the
    # binding needs it pressed twice — and the hint has to say so, or the
    # feature looks broken. `send-prefix` is what makes the second one arrive.
    nconf = panes.tmux_conf("/usr/bin/zimzilla", nested=True)
    check("nested hint says ctrl+b twice", "ctrl+b ctrl+b h" in nconf, nconf)
    check("send-prefix bound for forwarding", "bind C-b send-prefix" in nconf)
    check("bindings still present when nested",
          "bind h split-window -h" in nconf and "bind v split-window -v" in nconf)

    old_tmux = os.environ.get("TMUX")
    os.environ["TMUX"] = "/tmp/tmux-1000/outer,123,0"
    check("nested_in_tmux sees an outer tmux", panes.nested_in_tmux())
    os.environ.pop("TMUX", None)
    check("nested_in_tmux false with no outer tmux", not panes.nested_in_tmux())
    if old_tmux is not None:
        os.environ["TMUX"] = old_tmux

    # write_tmux_conf derives `nested` from the environment unless told otherwise
    wconf = panes.write_tmux_conf(tmp / "nested-state", "/usr/bin/zimzilla", nested=True)
    check("write_tmux_conf honours nested=True", "ctrl+b ctrl+b h" in wconf.read_text())
    wconf2 = panes.write_tmux_conf(tmp / "plain-state", "/usr/bin/zimzilla", nested=False)
    check("write_tmux_conf honours nested=False", "ctrl+b ctrl+b" not in wconf2.read_text())

    if shutil.which("tmux"):
        conf_path = tmp / "tmux.conf"
        conf_path.write_text(conf)
        sock = f"zz-test-{os.getpid()}"
        env = dict(os.environ)
        env.pop("TMUX", None)
        base = ["tmux", "-L", sock, "-f", str(conf_path)]
        subprocess.run([*base, "new-session", "-d", "-s", "t", "-x", "200", "-y", "50", "sleep 60"],
                       env=env, check=True)
        try:
            ok = subprocess.run(["tmux", "-L", sock, "source-file", str(conf_path)],
                                env=env, capture_output=True, text=True)
            check("tmux accepts the generated config", ok.returncode == 0, ok.stderr)
            keys = subprocess.run(["tmux", "-L", sock, "list-keys", "-T", "prefix"],
                                  env=env, capture_output=True, text=True).stdout
            check("prefix h bound to split-window -h", has_bind(keys, "h", "split-window -h"), keys)
            check("prefix v bound to split-window -v", has_bind(keys, "v", "split-window -v"), keys)
            ss = subprocess.run(["tmux", "-L", sock, "show-environment", "-g", panes.ENV_SESSION],
                                env=env, capture_output=True, text=True).stdout
            check("global session env set", "ZIMZILLA_PANE_SESSION=zimzilla" in ss, ss)
        finally:
            subprocess.run(["tmux", "-L", sock, "kill-server"], env=env, capture_output=True)

        # --- a pre-existing server must still get the bindings -------------
        # `tmux -f` is read only when a server starts. If one is already up on
        # our socket, `new-session -A` attaches and the config is never read —
        # leaving tmux's stock prefix table, in which `ctrl+b h` does nothing
        # at all. This is the exact bug that made the prefix keys "not work".
        sock2 = f"zz-test-stale-{os.getpid()}"
        env2 = dict(os.environ)
        env2.pop("TMUX", None)
        stale = ["tmux", "-L", sock2, "-f", "/dev/null"]
        subprocess.run([*stale, "new-session", "-d", "-s", "t", "-x", "200", "-y", "50", "sleep 60"],
                       env=env2, check=True)
        try:
            before = subprocess.run(["tmux", "-L", sock2, "list-keys", "-T", "prefix"],
                                    env=env2, capture_output=True, text=True).stdout
            check("stock server really lacks the bindings",
                  not has_bind(before, "h", "split-window -h"), before)
            # apply_config is what launch() calls before attaching.
            panes.apply_config(conf_path, socket=sock2)
            after = subprocess.run(["tmux", "-L", sock2, "list-keys", "-T", "prefix"],
                                   env=env2, capture_output=True, text=True).stdout
            check("apply_config binds h on a pre-existing server",
                  has_bind(after, "h", "split-window -h"), after)
            check("apply_config binds v on a pre-existing server",
                  has_bind(after, "v", "split-window -v"), after)
        finally:
            subprocess.run(["tmux", "-L", sock2, "kill-server"], env=env2, capture_output=True)
    else:
        print("SKIP tmux not installed")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all panes checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
