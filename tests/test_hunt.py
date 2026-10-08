"""`/bug-hunt` regression suite: findings parsing, the wave loop, stop, reports.

Run:  python tests/test_hunt.py   (from an activated venv)

Everything here is offline. The campaign's model calls go through a stubbed
``agent_factory`` — the same seam team.py exposes and tests/test_phase4.py
uses — so the loop, the planner feedback edge and the stop signal are all
exercised without a terminal or a network.

Two invariants this file is built around:

* **The stop is set from inside ``on_event``.** ``run_hunt`` awaits every event
  it emits, so setting the flag there is deterministic — the loop's
  ``while not stop.is_set()`` sees it before it can start another wave. A
  separate watcher task would make "how many waves ran" a scheduling race.
* **Agents never touch the network.** The one tool call a stub agent makes is
  ``list_dir``, which is ungated and local; a ``bash`` stub would really run.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import hunt as hunt_mod  # noqa: E402
from zimzilla import team as team_mod  # noqa: E402
from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.theme import get_palette  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402
from zimzilla.ui.widgets import ChatPane, ZimPane  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


class _Blk:
    def __init__(self, **k):
        self.__dict__.update(k)

    def model_dump(self, exclude_none: bool = False) -> dict:
        """Match the real SDK block's interface — see tests/test_phase4.py."""
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


class _Usage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class _Msg:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage


# ---------------------------------------------------------------------------
# Findings parsing
# ---------------------------------------------------------------------------

def test_parse_findings() -> None:
    text = (
        "I poked at the login form.\n\n"
        "```json\n"
        '{"findings": ['
        '{"title": "IDOR on /api/users/{id}", "severity": "HIGH",'
        ' "asset": "api.example.test", "summary": "any user readable",'
        ' "evidence": "GET /api/users/2 returned user 1",'
        ' "remediation": "check ownership"},'
        '{"title": "missing HSTS", "severity": "low"},'
        '{"title": "info leak", "severity": "informational"}'
        ']}\n'
        "```\n"
    )
    fs = hunt_mod.parse_findings(text, wave=3, agent="access-control")
    check("findings: every entry is read", len(fs) == 3, str(len(fs)))
    check("findings: severity is lowercased and kept",
          fs[0].severity == "high", fs[0].severity)
    check("findings: an alias severity is folded",
          fs[2].severity == "info", fs[2].severity)
    check("findings: the wave and agent are stamped on",
          all(f.wave == 3 and f.agent == "access-control" for f in fs))
    check("findings: only medium+ earn a report",
          [f.saved for f in fs] == [True, False, False],
          str([f.saved for f in fs]))
    check("findings: a brace inside a title does not unbalance the scan",
          fs[0].title == "IDOR on /api/users/{id}", fs[0].title)
    check("findings: evidence survives intact",
          "returned user 1" in fs[0].evidence, fs[0].evidence)

    # A bare object, not wrapped in a "findings" envelope.
    bare = '{"title": "open redirect", "severity": "medium", "asset": "a.test"}'
    one = hunt_mod.parse_findings(bare, wave=1, agent="x")
    check("findings: a bare finding object is accepted", len(one) == 1, str(len(one)))
    check("findings: the bare object keeps its severity",
          bool(one) and one[0].severity == "medium")

    check("findings: an empty reply yields nothing",
          hunt_mod.parse_findings("", wave=1, agent="x") == [])
    check("findings: prose with no JSON yields nothing",
          hunt_mod.parse_findings("nothing found, all clean", wave=1, agent="x") == [])
    check("findings: an entry with no title is dropped",
          hunt_mod.parse_findings('{"findings": [{"severity": "high"}]}',
                                  wave=1, agent="x") == [])
    check("findings: two blocks in one message are both read",
          len(hunt_mod.parse_findings(
              '{"findings":[{"title":"a","severity":"high"}]} and '
              '{"findings":[{"title":"b","severity":"high"}]}',
              wave=1, agent="x")) == 2)
    check("findings: severity aliases normalise",
          hunt_mod.normalise_severity("Moderate") == "medium"
          and hunt_mod.normalise_severity("SEVERE") == "critical"
          and hunt_mod.normalise_severity("nonsense") == "info")


# ---------------------------------------------------------------------------
# Fallback roster
# ---------------------------------------------------------------------------

def test_fallback() -> None:
    roster = hunt_mod.fallback_roster(1)
    check("fallback: the matrix has ten vectors",
          len(hunt_mod.HUNT_VECTORS) == 10, str(len(hunt_mod.HUNT_VECTORS)))
    check("fallback: a full wave is produced",
          len(roster.workers) == hunt_mod.HUNT_WAVE_SIZE, str(len(roster.workers)))
    check("fallback: every worker has a brief and an owned vector",
          all(w.brief and w.owns for w in roster.workers))
    check("fallback: names are unique",
          len({w.name for w in roster.workers}) == len(roster.workers))
    check("fallback: a smaller wave is honoured",
          len(hunt_mod.fallback_roster(1, 3).workers) == 3)
    check("fallback: all ten run at once by default",
          hunt_mod.HUNT_CONCURRENCY == hunt_mod.HUNT_WAVE_SIZE,
          f"{hunt_mod.HUNT_CONCURRENCY} vs {hunt_mod.HUNT_WAVE_SIZE}")


def test_planner_fallback_path() -> None:
    """A planner that rambles must not cost the wave."""
    check("planner: garbage parses to None",
          team_mod.parse_roster("I could not decide. Sorry!", max_agents=10) is None)
    check("planner: an empty roster parses to no workers",
          not (team_mod.parse_roster('{"summary": "x", "workers": []}')
               or team_mod.Roster("", [])).workers)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_reports(wd: Path) -> None:
    cfg = _cfg(wd)
    cfg.state_dir = wd / "state"
    run = hunt_mod.HuntRun(target="example.test", wave=2,
                           directory=hunt_mod.case_dir(cfg, "example.test"))

    f = hunt_mod.Finding(wave=2, agent="injection", title="SQLi in ?q",
                         severity="critical", asset="example.test/search",
                         summary="the q parameter is concatenated into SQL",
                         evidence="' OR 1=1-- returned every row",
                         remediation="use a parameterised query")
    run.findings.append(f)

    path = hunt_mod.write_report(run, f)
    check("report: the file lands under findings/",
          path.parent.name == "findings" and path.is_file(), str(path))
    check("report: the filename carries the wave and a slug",
          path.name.startswith("wave-2-"), path.name)

    body = path.read_text()
    check("report: frontmatter carries the fields",
          "severity: critical" in body and "wave: 2" in body
          and "agent: injection" in body and "target: example.test" in body)
    check("report: frontmatter carries one timestamp",
          "timestamp: 20" in body and body.count("timestamp:") == 1)
    check("report: the title and sections are present",
          "# SQLi in ?q" in body and "## Evidence" in body
          and "## Remediation" in body)
    check("report: the evidence is preserved", "' OR 1=1--" in body)

    # A low finding earns no file.
    low = hunt_mod.Finding(wave=2, agent="x", title="nitpick", severity="low")
    check("report: a low finding is not saved", hunt_mod._save(run, low) == "")

    # Round-trip: a campaign can be reloaded from disk.
    loaded = hunt_mod.load_run(run.directory)
    check("reload: a campaign reads back off disk", loaded is not None)
    check("reload: the finding is recovered",
          loaded is not None and len(loaded.findings) == 1,
          str(len(loaded.findings) if loaded else 0))
    check("reload: severity and title survive the round trip",
          loaded is not None and loaded.findings[0].severity == "critical"
          and loaded.findings[0].title == "SQLi in ?q")
    check("reload: the summary section is recovered",
          loaded is not None and "concatenated" in loaded.findings[0].summary)
    check("reload: an empty directory yields None",
          hunt_mod.load_run(wd / "nothing-here") is None)

    # Two agents reporting the same title in one wave must not collide: the
    # slug is derived from the title, so a naive write would drop one report.
    dup = hunt_mod.Finding(wave=2, agent="other", title="SQLi in ?q",
                           severity="high", evidence="second agent's proof")
    second = hunt_mod.write_report(run, dup)
    check("report: a duplicate title gets its own file", second != path,
          f"{path.name} vs {second.name}")
    check("report: the duplicate's evidence survives",
          "second agent's proof" in second.read_text())
    check("report: the first file is untouched by the duplicate",
          "' OR 1=1--" in path.read_text()
          and "second agent" not in path.read_text())

    # The summary archives into the flat reports store, like /osint.
    dest = hunt_mod.write_summary(cfg, run, "the closing report")
    check("summary: it is written beside the evidence",
          (run.directory / "summary.md").is_file())
    check("summary: it is archived into the reports store",
          dest.parent == hunt_mod.reports_dir(cfg), str(dest))
    check("summary: the archive carries the run's shape",
          "waves: 2" in dest.read_text() and "findings: 1" in dest.read_text())


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

_RECON_MARK = "opening a security engagement"


def _hunt_factory(script):
    """A factory whose planner answers from *script* and whose agents report.

    ``script["rosters"]`` is consumed one per planner turn, so a test can give
    wave 1 and wave 2 different briefs and prove the loop re-plans.
    ``script["prompts"]`` collects every prompt the planner was handed, which is
    how the feedback edge is checked. ``script["recon_final"]`` is the recon
    agent's own report, kept separate from ``script["final"]`` so recon does not
    silently contribute the workers' findings.
    """
    built: list[Agent] = []

    def factory(cfg, **kw):
        agent = Agent(cfg, permission_handler=kw.get("permission_handler"),
                      tool_hook=kw.get("tool_hook"))
        state = {"n": 0}

        async def fake_stream():
            state["n"] += 1
            prompt = agent.messages[-1]["content"] if agent.messages else ""
            prompt = prompt if isinstance(prompt, str) else ""

            if cfg.mode == "plan":
                script["prompts"].append(prompt)
                idx = script["planner_calls"]
                script["planner_calls"] += 1
                rosters = script["rosters"]
                text = rosters[min(idx, len(rosters) - 1)]
                yield ({"type": "text_delta", "text": text}, None)
                yield (None, _Msg([_Blk(type="text", text=text)], _Usage(10, 5)))
                return

            if _RECON_MARK in prompt:
                # Recon reports in one turn, text only.
                text = script.get("recon_final", "nothing reachable")
                yield ({"type": "text_delta", "text": text}, None)
                yield (None, _Msg([_Blk(type="text", text=text)], _Usage(20, 8)))
                return

            if state["n"] == 1:
                # First worker turn: a harmless local tool call, so the
                # tool-call path is exercised without touching the network.
                yield ({"type": "text_delta", "text": "probing"}, None)
                yield (None, _Msg([
                    _Blk(type="text", text="probing"),
                    _Blk(type="tool_use", id="t1", name="list_dir",
                         input={"path": "."}),
                ], _Usage(100, 20)))
                return

            yield ({"type": "text_delta", "text": script["final"]}, None)
            yield (None, _Msg([_Blk(type="text", text=script["final"])],
                              _Usage(30, 10)))

        agent._stream_once = fake_stream
        built.append(agent)
        return agent

    factory.built = built
    return factory


def _recorder(events, stop, *, after=1):
    """An on_event sink that stops the campaign after *after* waves.

    Setting the flag here rather than from a watcher task is what makes "how
    many waves ran" deterministic: run_hunt awaits this, so the loop's stop
    check sees the flag before it can plan another wave.
    """
    async def on_event(ev):
        events.append(ev)
        if ev["type"] == "hunt_wave_end" and ev["wave"] >= after:
            stop.set()
    return on_event


def _finding_json(marker="auth bypass"):
    return ('{"findings": [{"title": "%s", "severity": "critical",'
            ' "asset": "x.test", "summary": "no check", "evidence": "curl"}]}'
            % marker)


async def test_hunt_loop(wd: Path) -> None:
    """Recon, one wave, findings harvested, then stopped."""
    roster = ('{"summary": "aiming at auth", "workers": ['
              '{"name": "auth", "brief": "test auth", "owns": ["auth"]},'
              '{"name": "idor", "brief": "test idor", "owns": ["idor"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "the target answers on 443, nginx, a login form at /login",
        "final": _finding_json(),
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = wd / "state"

    run = await hunt_mod.run_hunt(
        cfg, "x.test", on_event=_recorder(events, stop),
        agent_factory=factory, stop=stop, wave_size=2, concurrency=2,
    )

    kinds = [e["type"] for e in events]
    check("hunt: recon runs before any wave",
          kinds.index("hunt_recon_start") < kinds.index("hunt_wave_start"))
    check("hunt: recon reports back", "hunt_recon_done" in kinds)
    check("hunt: a plan event is emitted", "hunt_plan" in kinds)
    check("hunt: every agent starts and finishes",
          kinds.count("hunt_agent_start") == 2
          and kinds.count("hunt_agent_done") == 2)
    check("hunt: the wave closes with hunt_wave_end", "hunt_wave_end" in kinds)
    check("hunt: the run closes with hunt_end", kinds[-1] == "hunt_end", kinds[-1])
    check("hunt: exactly one wave ran", run.wave == 1, str(run.wave))
    check("hunt: the recon text is kept on the run",
          "nginx" in run.recon, run.recon[:60])

    check("hunt: findings are harvested off the agents",
          len(run.findings) == 2, str(len(run.findings)))
    check("hunt: a critical finding is kept",
          any(f.severity == "critical" for f in run.findings))
    check("hunt: a finding event is emitted per finding",
          kinds.count("hunt_finding") == 2, str(kinds.count("hunt_finding")))
    check("hunt: saved findings report their path",
          any(e.get("saved") for e in events if e["type"] == "hunt_finding"))
    check("hunt: the report file exists on disk",
          len(list((run.directory / "findings").glob("*.md"))) == 2,
          str(len(list((run.directory / "findings").glob("*.md")))))
    check("hunt: findings are attributed to the agent that found them",
          {f.agent for f in run.findings} == {"auth", "idor"},
          str({f.agent for f in run.findings}))

    # The planner is read-only, so it cannot do the work itself.
    planners = [a for a in factory.built if a.cfg.mode == "plan"]
    agents = [a for a in factory.built if a.cfg.mode == "zim"]
    check("hunt: the planner runs in plan mode", len(planners) == 1)
    check("hunt: agents inherit the session mode (zim)",
          len(agents) == 3, str(len(agents)))  # recon + 2 workers
    check("hunt: each agent gets its own Config",
          len({id(a.cfg) for a in agents}) == 3)


async def test_hunt_replans_from_findings(wd: Path) -> None:
    """Wave 2's planner must be handed wave 1's findings."""
    roster = ('{"summary": "s", "workers": ['
              '{"name": "w1", "brief": "first", "owns": ["a"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "recon: a single host, nothing exotic",
        "final": _finding_json("unique-marker-bug"),
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = wd / "state"

    run = await hunt_mod.run_hunt(
        cfg, "x.test", on_event=_recorder(events, stop, after=2),
        agent_factory=factory, stop=stop, wave_size=1, concurrency=1,
    )

    check("replan: two waves ran", run.wave == 2, str(run.wave))
    check("replan: the planner was consulted twice",
          len(script["prompts"]) == 2, str(len(script["prompts"])))
    check("replan: wave 1's prompt carries no prior findings",
          "nothing confirmed yet" in script["prompts"][0])
    check("replan: wave 2's prompt carries wave 1's finding",
          "unique-marker-bug" in script["prompts"][1],
          script["prompts"][1][:200])
    check("replan: wave 2's prompt carries the wave history",
          "wave 1:" in script["prompts"][1])
    check("replan: findings accumulate across waves",
          len(run.findings) == 2, str(len(run.findings)))
    check("replan: the run records both waves",
          len(run.waves) == 2, str(len(run.waves)))


async def test_hunt_fallback_wave(wd: Path) -> None:
    """An unreadable planner reply falls back to the vector matrix."""
    script = {
        "rosters": ["I really cannot decide what to test next."],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "recon: nothing notable",
        "final": "no findings",
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = wd / "state"

    run = await hunt_mod.run_hunt(
        cfg, "x.test", on_event=_recorder(events, stop),
        agent_factory=factory, stop=stop, wave_size=3, concurrency=3,
    )

    plan = next(e for e in events if e["type"] == "hunt_plan")
    check("fallback: the plan event is flagged", plan.get("fell_back") is True)
    check("fallback: the fixed matrix supplies the briefs",
          len(plan["workers"]) == 3, str(len(plan["workers"])))
    check("fallback: the vector names are used",
          plan["workers"][0]["name"] == "access-control",
          plan["workers"][0]["name"])
    check("fallback: the wave still ran",
          sum(1 for e in events if e["type"] == "hunt_agent_start") == 3)
    check("fallback: the wave history records the fallback",
          any("fallback" in w for w in run.waves), str(run.waves))
    check("fallback: the campaign still ends cleanly",
          events[-1]["type"] == "hunt_end")


async def test_hunt_stops_midwave(wd: Path) -> None:
    """A stop set mid-wave cancels the in-flight agents promptly."""
    roster = ('{"summary": "s", "workers": ['
              + ",".join(f'{{"name": "w{i}", "brief": "b", "owns": ["o{i}"]}}'
                         for i in range(6)) + "]}")

    started: list[str] = []

    def factory(cfg, **kw):
        agent = Agent(cfg, permission_handler=kw.get("permission_handler"),
                      tool_hook=kw.get("tool_hook"))

        async def fake_stream():
            prompt = agent.messages[-1]["content"] if agent.messages else ""
            prompt = prompt if isinstance(prompt, str) else ""

            if cfg.mode == "plan":
                yield ({"type": "text_delta", "text": roster}, None)
                yield (None, _Msg([_Blk(type="text", text=roster)]))
                return
            if _RECON_MARK in prompt:
                yield ({"type": "text_delta", "text": "recon done"}, None)
                yield (None, _Msg([_Blk(type="text", text="recon done")]))
                return

            started.append("x")
            # An agent that never finishes on its own: only cancellation ends it.
            yield ({"type": "text_delta", "text": "working"}, None)
            await asyncio.sleep(30)
            yield (None, _Msg([_Blk(type="text", text="done")]))

        agent._stream_once = fake_stream
        return agent

    events: list[dict] = []

    async def on_event(ev):
        events.append(ev)

    stop = asyncio.Event()

    async def stopper():
        while len(started) < 2:
            await asyncio.sleep(0.01)
        stop.set()

    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = wd / "state"
    task = asyncio.create_task(stopper())

    import time as _time
    t0 = _time.monotonic()
    run = await asyncio.wait_for(
        hunt_mod.run_hunt(cfg, "x.test", on_event=on_event, agent_factory=factory,
                          stop=stop, wave_size=6, concurrency=6),
        timeout=8.0,
    )
    elapsed = _time.monotonic() - t0
    await task

    check("stop: the run returns instead of hanging", run is not None)
    check("stop: it lands well inside the agents' 30s sleep",
          elapsed < 8.0, f"{elapsed:.2f}s")
    check("stop: no second wave is started", run.wave == 1, str(run.wave))
    check("stop: hunt_end is still emitted",
          bool(events) and events[-1]["type"] == "hunt_end",
          events[-1]["type"] if events else "")


async def test_hunt_summary_flag(wd: Path) -> None:
    """A queued summary runs at the wave boundary, not concurrently."""
    roster = ('{"summary": "s", "workers": ['
              '{"name": "w1", "brief": "b", "owns": ["o1"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "recon: one host",
        "final": "nothing",
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    flag = {"queued": True}  # asked for before the run even starts

    async def on_event(ev):
        events.append(ev)
        if ev["type"] == "hunt_summary_done":
            stop.set()

    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = wd / "state"
    run = await hunt_mod.run_hunt(
        cfg, "x.test", on_event=on_event, agent_factory=factory,
        stop=stop, wave_size=1, concurrency=1,
        summary_flag=lambda: flag["queued"],
    )

    kinds = [e["type"] for e in events]
    done = [e for e in events if e["type"] == "hunt_summary_done"]
    check("summary: the queued summary runs", len(done) == 1, str(len(done)))
    check("summary: it runs after the wave, not during it",
          kinds.index("hunt_summary_done") > kinds.index("hunt_wave_end"))
    check("summary: it lands at the wave boundary",
          bool(done) and done[0]["wave"] == 1,
          str(done[0]["wave"] if done else None))
    check("summary: the report file is written",
          bool(done) and Path(done[0]["path"]).is_file(),
          done[0]["path"] if done else "")
    check("summary: the campaign then stops",
          run.wave == 1, str(run.wave))


# ---------------------------------------------------------------------------
# The pane
# ---------------------------------------------------------------------------

def test_pane() -> None:
    p = get_palette("green", None)
    pane = ZimPane(p)
    pane.update = lambda *a, **k: None  # no live Textual app in a test

    pane.show_hunt("example.test", 10)
    check("pane: the hunt view is selected", pane.view == "hunt", pane.view)
    check("pane: it is made visible", pane.has_class("visible"))

    pane.note_hunt({"type": "hunt_wave_start", "wave": 1, "size": 10})
    check("pane: the wave is recorded", pane.hunt_wave == 1, str(pane.hunt_wave))
    check("pane: the wave size is recorded", pane.hunt_size == 10)

    for i in range(10):
        pane.note_hunt({"type": "hunt_agent_start", "name": f"a{i}", "index": i})
    check("pane: all ten agents are tracked",
          len(pane.hunt_agents) == 10, str(len(pane.hunt_agents)))

    pane.note_hunt({"type": "hunt_agent_tool", "name": "a3", "tool": "bash",
                    "args": {"command": "curl -s http://example.test/login"}})
    check("pane: the tool call becomes the activity line",
          "bash" in pane.hunt_agents["a3"]["activity"],
          pane.hunt_agents["a3"]["activity"])
    check("pane: the active agent moves to the end of the order",
          list(pane.hunt_agents)[-1] == "a3", str(list(pane.hunt_agents)[-1]))

    pane.note_hunt({"type": "hunt_agent_done", "name": "a0", "ok": True})
    pane.note_hunt({"type": "hunt_agent_done", "name": "a1", "ok": False})
    check("pane: a done agent is marked done",
          pane.hunt_agents["a0"]["status"] == "done")
    check("pane: a failed agent is marked failed",
          pane.hunt_agents["a1"]["status"] == "failed")

    pane.note_hunt({"type": "hunt_finding",
                    "finding": {"severity": "critical", "title": "x"}})
    pane.note_hunt({"type": "hunt_finding",
                    "finding": {"severity": "low", "title": "y"}})
    check("pane: findings are counted by severity",
          pane.hunt_severity == {"critical": 1, "low": 1}, str(pane.hunt_severity))

    pane.note_hunt({"type": "hunt_wave_start", "wave": 2, "size": 10})
    check("pane: a new wave resets the running agents to queued",
          pane.hunt_agents["a3"]["status"] == "queued",
          pane.hunt_agents["a3"]["status"])
    check("pane: completed agents keep their terminal state",
          pane.hunt_agents["a0"]["status"] == "done")

    pane.note_hunt({"type": "hunt_end", "wave": 2})
    check("pane: the hunt is marked stopped", pane.hunt_running is False)

    # The phish view must be untouched by all of this.
    pane2 = ZimPane(p)
    pane2.update = lambda *a, **k: None
    pane2.show_campaign("phish.test")
    pane2.note({"kind": "hit", "method": "GET", "path": "/x"})
    check("pane: a phish campaign still uses the phish view",
          pane2.view == "phish" and len(pane2.hits) == 1, pane2.view)
    check("pane: switching views clears nothing on the phish side",
          pane2.host == "phish.test")


# ---------------------------------------------------------------------------
# The busy gate
# ---------------------------------------------------------------------------

def _transcript_text(app) -> str:
    """Everything written to the transcript so far, as plain text.

    Read from the RainRichLog's own record of the renderables it was handed
    rather than from its rendered strips, which depend on a laid-out width.
    """
    log = app.query_one(ChatPane).query_one("#transcript")
    parts: list[str] = []
    for item in getattr(log, "_blocks", []):
        try:
            parts.append(str(item[0]))
        except Exception:
            pass
    return "\n".join(parts)


async def test_busy_gate(wd: Path) -> None:
    """Some commands must answer mid-run; the launchers must not."""
    app = ZimZillaApp(_cfg(wd, boot_rain=False))

    # BUSY_OK is the contract: it must contain the hunt controls and must not
    # contain any command that drives a turn on the shared agent.
    check("gate: stop-hunt works while busy", "stop-hunt" in app.BUSY_OK)
    check("gate: summary-hunt works while busy", "summary-hunt" in app.BUSY_OK)
    check("gate: help/cost/scope/mode/model work while busy",
          {"help", "cost", "scope", "mode", "model"} <= app.BUSY_OK)
    check("gate: launchers are NOT busy-permitted",
          not ({"team", "osint", "phish", "bug-hunt", "compact"} & app.BUSY_OK),
          str(sorted({"team", "osint", "phish", "bug-hunt", "compact"}
                     & app.BUSY_OK)))
    check("gate: /clear is NOT busy-permitted (it wipes live history)",
          "clear" not in app.BUSY_OK)

    # Every BUSY_OK name must actually dispatch to something, or the set is a
    # lie the operator discovers at the worst moment.
    dispatch_names = {
        "help", "clear", "mode", "model", "cost", "scope", "rain", "theme",
        "save", "load", "compact", "team", "osint", "phish", "bug-hunt",
        "stop-hunt", "summary-hunt", "zim-logfare", "zim-tokenjuice",
        "zim-source", "exit", "quit",
    }
    check("gate: every busy-permitted command is dispatchable",
          app.BUSY_OK <= dispatch_names,
          str(sorted(app.BUSY_OK - dispatch_names)))

    from textual.widgets import Input

    async with app.run_test(size=(100, 40)) as pilot:
        app.pop_screen()  # the boot splash
        await pilot.pause()
        inp = app.query_one("#input", Input)
        inp.focus()
        await pilot.pause()

        app.busy = True

        inp.value = "/stop-hunt"
        await pilot.press("enter")
        await pilot.pause()
        text = _transcript_text(app)
        # No hunt is running, so the honest answer is "no hunt is running" —
        # and crucially NOT "harness is busy".
        check("gate: a busy-permitted command is not refused when busy",
              "harness is busy" not in text and "no hunt is running" in text,
              text[-160:])

        inp.value = "/team do a thing"
        await pilot.press("enter")
        await pilot.pause()
        text = _transcript_text(app)
        check("gate: a launcher is still refused when busy",
              "harness is busy" in text, text[-160:])

        inp.value = "/bug-hunt example.test"
        await pilot.press("enter")
        await pilot.pause()
        check("gate: /bug-hunt is refused while busy",
              _transcript_text(app).count("harness is busy") == 2,
              str(_transcript_text(app).count("harness is busy")))

        app.busy = False


# ---------------------------------------------------------------------------

async def main() -> int:
    test_parse_findings()
    test_fallback()
    test_planner_fallback_path()
    test_pane()

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        test_reports(wd)
        await test_hunt_loop(wd)
        await test_hunt_replans_from_findings(wd)
        await test_hunt_fallback_wave(wd)
        await test_hunt_stops_midwave(wd)
        await test_hunt_summary_flag(wd)
        await test_busy_gate(wd)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
