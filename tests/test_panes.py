"""Pane messaging: mailbox semantics, tool gating, and the generated tmux config.

Run:  python tests/test_panes.py   (from an activated venv)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import panes, tools  # noqa: E402
from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import Config  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILS.append(name)


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

    # --- tmux config ------------------------------------------------------
    conf = panes.tmux_conf("/usr/bin/zimzilla --workdir '/tmp/x y'")
    check("prefix is ctrl+b", "set -g prefix C-b" in conf)
    check("bind h is horizontal split", "bind h split-window -h" in conf)
    check("bind v is vertical split", "bind v split-window -v" in conf)

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
            check("prefix h bound to split-window -h",
                  any(" h " in l and "split-window -h" in l for l in keys.splitlines()), keys)
            check("prefix v bound to split-window -v",
                  any(" v " in l and "split-window -v" in l for l in keys.splitlines()), keys)
            ss = subprocess.run(["tmux", "-L", sock, "show-environment", "-g", panes.ENV_SESSION],
                                env=env, capture_output=True, text=True).stdout
            check("global session env set", "ZIMZILLA_PANE_SESSION=zimzilla" in ss, ss)
        finally:
            subprocess.run(["tmux", "-L", sock, "kill-server"], env=env, capture_output=True)
    else:
        print("SKIP tmux not installed")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all panes checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
