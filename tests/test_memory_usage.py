"""Regression suite for operator memory and the persistent /usage ledger.

Run:  python tests/test_memory_usage.py

Offline. Covers what gets remembered, what deliberately does not, persistence
and permissions, usage aggregation/ranking/days, and the TUI wiring.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_HOME = Path(tempfile.mkdtemp(prefix="zim-memhome-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)

from zimzilla import memory as mem  # noqa: E402
from zimzilla import usage as usg  # noqa: E402
from zimzilla.config import Config  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def test_memory(tmp: Path) -> None:
    path = tmp / "memory.json"
    m = mem.Memory(path)
    text = ("My name is Zim and I like dark terminals, fast cars. "
            "I hate light mode. I always use short answers. "
            "My favourite editor is Neovim. Remember that this project is ZimZilla.")
    learned = m.learn(text)
    kinds = [(f.kind, f.text) for f in learned]
    check("memory: the name is learned", ("identity", "Zim") in kinds, str(kinds))
    check("memory: likes are learned", any(k == "like" for k, _ in kinds), str(kinds))
    check("memory: dislikes are learned",
          any(k == "dislike" for k, _ in kinds), str(kinds))
    check("memory: preferences are learned",
          any(k == "preference" for k, _ in kinds), str(kinds))
    check("memory: explicit notes are learned",
          any(k == "note" and "ZimZilla" in v for k, v in kinds), str(kinds))
    check("memory: it does not treat 'I am tired' as identity",
          not any(v == "tired" for k, v in kinds))
    note = m.prompt_note()
    check("memory: the prompt carries the name", "Zim" in note, note[:200])
    check("memory: the prompt labels itself",
          "OPERATOR MEMORY" in note and "current request" in note.lower(), note[:200])
    check("memory: facts deduplicate", len(m.learn("my name is Zim")) == 1 and
          len([f for f in m.facts if f.text == "Zim"]) == 1)

    reloaded = mem.Memory(path)
    check("memory: it persists", any(f.text == "Zim" for f in reloaded.facts))
    check("memory: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
          oct(path.stat().st_mode & 0o777))
    check("memory: forget removes a match", m.forget("light mode") == 1)
    check("memory: forget is gone", "light mode" not in m.prompt_note())
    m.clear()
    check("memory: clear empties it", not m.facts and not m.prompt_note())

    secret = m.learn("remember that my key is sk-abcdefghijklmnopqrstuvwxyz")
    check("memory: credentials are redacted",
          all("sk-abcdef" not in f.text for f in secret), str(secret))


def test_usage(tmp: Path) -> None:
    path = tmp / "usage.json"
    u = usg.UsageStats(path)
    now = time.time()
    u.record("gpt-4o:free", "crowllm", 1200, 300, 0.0, when=now - 86400 * 2)
    u.record("gpt-4o:free", "crowllm", 1000, 200, 0.0, when=now - 86400)
    u.record("deepseek:free", "numes", 800, 100, 0.001, when=now)
    totals = u.data["totals"]
    check("usage: turns aggregate", totals["turns"] == 3, str(totals))
    check("usage: input aggregates", totals["input"] == 3000, str(totals))
    check("usage: output aggregates", totals["output"] == 600, str(totals))
    check("usage: cost aggregates", abs(totals["cost"] - 0.001) < 1e-9, str(totals))
    check("usage: top model is by turns", u.top("models")[0] == "gpt-4o:free",
          str(u.top("models")))
    check("usage: top source is by turns", u.top("sources")[0] == "crowllm",
          str(u.top("sources")))
    check("usage: model ranking", [m for m, _ in u.ranked("models")] ==
          ["gpt-4o:free", "deepseek:free"])
    check("usage: token formatting", usg.format_tokens(1_250_000) == "1.25M",
          usg.format_tokens(1_250_000))
    days = dict(u.recent_days(14))
    check("usage: the daily graph has 14 entries", len(days) == 14, str(len(days)))
    check("usage: today has a turn", list(days.values())[-1] == 1, str(days))
    check("usage: it persists",
          usg.UsageStats(path).data["totals"]["turns"] == 3)
    check("usage: mode 600", oct(path.stat().st_mode & 0o777) == "0o600",
          oct(path.stat().st_mode & 0o777))
    u.reset()
    check("usage: reset clears totals", u.data["totals"]["turns"] == 0)


def test_agent_prompt() -> None:
    cfg = Config.from_env(workdir=_HOME, base_url="https://logfare.ai/v1",
                          api_key="test")
    cfg.state_dir = _HOME / "state"
    from zimzilla.agent import Agent
    a = Agent(cfg)
    a.memory_note = "[OPERATOR MEMORY]\nIdentity: Zim"
    check("prompt: memory is appended", "Identity: Zim" in a.system_prompt())
    a.memory_note = ""
    check("prompt: no memory adds no section", "OPERATOR MEMORY" not in a.system_prompt())


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")
    for cmd in ("usage", "memory"):
        check(f"tui: /{cmd} is dispatched", f'"{cmd}": lambda a:' in app)
        check(f"tui: /{cmd} is completed", f'"/{cmd}"' in comp)
    check("tui: /usage has the graph paths", "LAST 14 DAYS" in app and
          "_usage_bar" in app)
    check("tui: memory is learned on a turn", "self.memory.learn(text" in app)
    check("tui: usage is recorded on a turn", "self._record_usage()" in app)
    check("tui: /memory edits the prompt", "agent.memory_note = self.memory.prompt_note()" in app)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zim-mem-") as td:
        base = Path(td)
        test_memory(base / "memory")
        test_usage(base / "usage")
    test_agent_prompt()
    test_wiring()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
