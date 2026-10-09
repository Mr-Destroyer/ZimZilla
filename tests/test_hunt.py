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
from zimzilla.ui.app import ReconOverlay, ZimZillaApp  # noqa: E402
from zimzilla.ui.widgets import ChatPane, ZimPane  # noqa: E402
from rich.text import Text  # noqa: E402

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
    silently contribute the workers' findings. ``script["first_tool"]`` is the
    ``(name, args)`` of the one tool call a worker makes, defaulting to an
    ungated ``list_dir``; ``script["worker_prompts"]`` collects what each worker
    was actually handed.
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
                # First worker turn: one tool call, so the tool-call path is
                # exercised. Defaults to an ungated local listing — a bash stub
                # here would really run — and a test that needs a longer or
                # differently-shaped command overrides it through
                # ``script["first_tool"]``.
                script.setdefault("worker_prompts", []).append(prompt)
                tool, tool_args = script.get("first_tool",
                                             ("list_dir", {"path": "."}))
                yield ({"type": "text_delta", "text": "probing"}, None)
                yield (None, _Msg([
                    _Blk(type="text", text="probing"),
                    _Blk(type="tool_use", id="t1", name=tool,
                         input=dict(tool_args)),
                ], _Usage(100, 20)))
                return

            text = _next_final(script)
            yield ({"type": "text_delta", "text": text}, None)
            yield (None, _Msg([_Blk(type="text", text=text)], _Usage(30, 10)))

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


def _finding_json(marker="auth bypass", *, asset="x.test"):
    return ('{"findings": [{"title": "%s", "severity": "critical",'
            ' "asset": "%s", "summary": "no check", "evidence": "curl"}]}'
            % (marker, asset))


def _next_final(script):
    """The text the next worker reports.

    ``script["final"]`` is a single string for the common case. It may also be a
    list, consumed one entry per worker, so a test with several workers can give
    each a *distinct* finding — which the tracker, now that it deduplicates,
    needs in order to see more than one. The last entry is reused once the list
    runs out.
    """
    final = script["final"]
    if isinstance(final, list):
        i = script.get("worker_finals", 0)
        script["worker_finals"] = i + 1
        return final[min(i, len(final) - 1)]
    return final


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
        # Distinct findings per worker: the tracker deduplicates, so two
        # identical reports would collapse to one and this test's "two findings
        # harvested" would be measuring the dedup, not the harvest.
        "final": [_finding_json("auth bypass"), _finding_json("idor on /api/users")],
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
        # A distinct finding each wave: the tracker deduplicates on title+asset,
        # and this test is about accumulation across waves, not about dedup.
        "final": [_finding_json("unique-marker-bug"),
                  _finding_json("second-wave-bug")],
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


async def test_brief_context(wd: Path) -> None:
    """Every worker must be handed the recon digest, or the wave re-derives it.

    Before this, a worker saw only its own brief, so all ten opened with the
    same `ls` and the same re-read of every recon file.
    """
    roster = ('{"summary": "s", "workers": ['
              '{"name": "a", "brief": "test a", "owns": ["a"]},'
              '{"name": "b", "brief": "test b", "owns": ["b"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "UNIQUE-RECON-MARKER nginx on 443, login at /login",
        "final": "nothing found",
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

    # The stub records what each worker was handed the first time it is turned.
    worker_prompts = script.get("worker_prompts", [])
    check("context: the workers were prompted", len(worker_prompts) >= 2,
          str(len(worker_prompts)))
    check("context: the recon digest reaches every worker",
          all("UNIQUE-RECON-MARKER" in p for p in worker_prompts),
          str([p[:60] for p in worker_prompts]))
    check("context: the brief is still carried alongside it",
          all("test a" in p or "test b" in p for p in worker_prompts))
    check("context: it says plainly not to re-derive",
          all("do not spend a turn re-deriving" in p for p in worker_prompts))
    check("context: run.recon is left whole for the planner and summary",
          "UNIQUE-RECON-MARKER" in run.recon, run.recon[:60])

    # A run with no recon at all must not prepend an empty header.
    empty = hunt_mod.HuntRun(target="y.test", directory=wd / "y")
    check("context: no recon means no context block",
          hunt_mod.brief_context(empty) == "")


def test_brief_context_truncation() -> None:
    """A huge recon report is trimmed, and says so."""
    from pathlib import Path as _P
    run = hunt_mod.HuntRun(target="x.test", directory=_P("/tmp/x"))
    run.recon = "A" * 20000
    ctx = hunt_mod.brief_context(run, limit=100)
    check("context: an oversized recon report is truncated",
          len(ctx) < 1000, str(len(ctx)))
    check("context: the truncation is admitted",
          "truncated" in ctx, ctx[-80:])


async def test_planner_retry(wd: Path) -> None:
    """A garbage planner reply gets one retry before the matrix is used."""
    roster = ('{"summary": "retried ok", "workers": ['
              '{"name": "auth", "brief": "test auth", "owns": ["auth"]}]}')
    # First reply is unparseable, the retry is a real roster.
    script = {
        "rosters": ["I am not sure what to test. Let me think about it.",
                    roster],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "recon: a login form at /login",
        "final": "nothing found",
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = wd / "state"

    await hunt_mod.run_hunt(
        cfg, "x.test", on_event=_recorder(events, stop),
        agent_factory=factory, stop=stop, wave_size=2, concurrency=2,
    )

    plan = next(e for e in events if e["type"] == "hunt_plan")
    check("retry: the planner was asked twice", script["planner_calls"] == 2,
          str(script["planner_calls"]))
    check("retry: the second reply is used", plan.get("fell_back") is False)
    check("retry: the retried roster supplies the briefs",
          [w["name"] for w in plan["workers"]] == ["auth"],
          str([w["name"] for w in plan["workers"]]))
    check("retry: the retry prompt says what was wrong",
          any("could not be read as JSON" in p for p in script["prompts"]),
          str([p[-120:] for p in script["prompts"]]))


async def test_planner_fallback_shows_raw(wd: Path) -> None:
    """When both attempts fail, the operator sees what the planner said."""
    script = {
        "rosters": ["nope", "still nope"],
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

    await hunt_mod.run_hunt(
        cfg, "x.test", on_event=_recorder(events, stop),
        agent_factory=factory, stop=stop, wave_size=2, concurrency=2,
    )

    plan = next(e for e in events if e["type"] == "hunt_plan")
    check("fallback: it is still flagged", plan.get("fell_back") is True)
    check("fallback: the raw reply is carried for the operator",
          plan.get("raw", "").strip() == "still nope", repr(plan.get("raw")))
    check("fallback: the fixed matrix still fills the wave",
          len(plan["workers"]) == 2, str(len(plan["workers"])))


async def test_hunt_result_event_carries_args(wd: Path) -> None:
    """The result event must carry the command and meta, or the transcript
    cannot render a full panel — it can only print a truncated label."""
    roster = ('{"summary": "s", "workers": ['
              '{"name": "a", "brief": "test a", "owns": ["a"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "recon: x",
        "final": "nothing found",
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = wd / "state"

    await hunt_mod.run_hunt(
        cfg, "x.test", on_event=_recorder(events, stop),
        agent_factory=factory, stop=stop, wave_size=1, concurrency=1,
    )

    results = [e for e in events if e["type"] == "hunt_agent_result"]
    check("result: the tool result is reported", len(results) >= 1, str(len(results)))
    if results:
        r = results[0]
        check("result: the command args ride along",
              isinstance(r.get("args"), dict) and r["args"].get("path") == ".",
              repr(r.get("args")))
        check("result: the meta rides along", "meta" in r, repr(r.keys()))
        check("result: the output is present", "output" in r)


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
# The recon window
# ---------------------------------------------------------------------------

async def test_recon_overlay(wd: Path) -> None:
    """The window opens on recon, narrates live, and closes on its own."""
    app = ZimZillaApp(_cfg(wd, boot_rain=False))

    from textual.widgets import Input as _Input

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()  # the boot splash
        await pilot.pause()

        overlay = ReconOverlay("example.test", app.palette, app.cfg)
        app.push_screen(overlay)
        await pilot.pause()

        check("overlay: it is the active screen", app.screen is overlay)
        check("overlay: it does not steal the prompt",
              overlay.can_focus is False)

        overlay.note({"type": "hunt_recon_tool", "tool": "bash",
                      "args": {"command": "nmap -sV example.test"}})
        overlay.note({"type": "hunt_recon_tool", "tool": "web_fetch",
                      "args": {"url": "https://example.test/robots.txt"}})
        overlay.note({"type": "hunt_recon_text",
                      "text": "nginx 1.24 on 443, a login form at /login"})
        await pilot.pause()

        check("overlay: tool calls are counted",
              overlay.calls == 2, str(overlay.calls))
        body = str(overlay.query_one("#recon-lines").render())
        check("overlay: the command is shown",
              "nmap -sV example.test" in body, body[:200])
        check("overlay: the url is shown",
              "robots.txt" in body, body[:200])
        check("overlay: the model's prose is shown",
              "login form at /login" in body, body[:200])
        status = str(overlay.query_one("#recon-status").render())
        check("overlay: the status line carries the call count",
              "2 calls" in status, status)
        check("overlay: the status line carries a clock",
              ":" in status and "working" in status, status)

        # A blocked call is marked, not dropped.
        overlay.note({"type": "hunt_recon_tool", "tool": "bash",
                      "args": {"command": "rm -rf /"}, "blocked": True})
        await pilot.pause()
        check("overlay: a blocked call still counts", overlay.calls == 3)
        check("overlay: a blocked call is marked",
              "✗" in str(overlay.query_one("#recon-lines").render()))

        # The operator can still type while it is up — that is the whole reason
        # can_focus is False.
        inp = app.query_one("#input", _Input)
        inp.focus()
        await pilot.pause()
        inp.value = "/stop-hunt"
        await pilot.press("enter")
        await pilot.pause()
        check("overlay: a command still dispatches while recon is up",
              app.screen is overlay,
              "the overlay was dismissed by typing")

        # And it comes down by itself, exactly once, without a pop_screen()
        # aimed at whatever happens to be on top.
        held = {"screen": overlay}
        app._close_recon(held)
        await pilot.pause()
        check("overlay: closing returns to the shell", app.screen is not overlay)
        check("overlay: closing is idempotent", held["screen"] is None)
        app._close_recon(held)  # must not pop the shell or raise
        await pilot.pause()
        check("overlay: a second close does not pop anything else",
              app.screen is not None)


async def test_recon_reports_live(wd: Path) -> None:
    """run_hunt must emit recon tool calls as they happen, not just at the end."""
    seen: list[dict] = []

    def factory(cfg, **kw):
        agent = Agent(cfg, permission_handler=kw.get("permission_handler"),
                      tool_hook=kw.get("tool_hook"))

        async def fake_stream():
            prompt = agent.messages[-1]["content"] if agent.messages else ""
            prompt = prompt if isinstance(prompt, str) else ""
            if _RECON_MARK in prompt:
                yield ({"type": "text_delta", "text": "probing"}, None)
                yield (None, _Msg([
                    _Blk(type="text", text="probing"),
                    _Blk(type="tool_use", id="r1", name="list_dir",
                         input={"path": "."}),
                ], _Usage(10, 5)))
                return
            if cfg.mode == "plan":
                yield ({"type": "text_delta", "text": "no workers here"}, None)
                yield (None, _Msg([_Blk(type="text", text="no workers here")]))
                return
            yield ({"type": "text_delta", "text": "nothing"}, None)
            yield (None, _Msg([_Blk(type="text", text="nothing")]))

        agent._stream_once = fake_stream
        return agent

    async def on_event(ev):
        seen.append(ev)
        if ev["type"] == "hunt_wave_end":
            stop.set()

    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = wd / "state"

    await hunt_mod.run_hunt(cfg, "x.test", on_event=on_event,
                            agent_factory=factory, stop=stop,
                            wave_size=1, concurrency=1)

    kinds = [e["type"] for e in seen]
    check("recon live: a tool call is reported while recon runs",
          "hunt_recon_tool" in kinds, str(kinds[:8]))
    check("recon live: the call lands before recon is done",
          kinds.index("hunt_recon_tool") < kinds.index("hunt_recon_done"))
    check("recon live: the tool name is carried",
          any(e.get("tool") == "list_dir" for e in seen
              if e["type"] == "hunt_recon_tool"))
    check("recon live: prose is reported in blocks",
          "hunt_recon_text" in kinds, str(kinds[:8]))
    check("recon live: the final recon text is still returned whole",
          any(e["type"] == "hunt_recon_done" and "probing" in (e.get("text") or "")
              for e in seen))


# ---------------------------------------------------------------------------
# The busy gate
# ---------------------------------------------------------------------------

def _transcript_text(app) -> str:
    """Everything written to the transcript so far, as plain text.

    Read from the RainRichLog's own record of the renderables it was handed
    rather than from its rendered strips, which depend on a laid-out width.

    A ``Text`` stringifies to itself, but a ``Panel`` or a ``Syntax`` does not —
    ``str()`` on either is an object repr. Those are rendered through a
    throwaway ``Console`` at a width wide enough that nothing wraps, so the
    assertion is against what the operator would actually read. That matters
    here: the tool result is a panel, and a helper that could not see inside it
    would report every one of them as empty.
    """
    import io

    from rich.console import Console

    log = app.query_one(ChatPane).query_one("#transcript")
    parts: list[str] = []
    for item in getattr(log, "_blocks", []):
        content = item[0]
        try:
            if isinstance(content, str) or isinstance(content, Text):
                parts.append(str(content))
                continue
            buf = io.StringIO()
            Console(file=buf, width=400, color_system=None,
                    legacy_windows=False).print(content)
            parts.append(buf.getvalue())
        except Exception:
            pass
    return "\n".join(parts)


async def test_stop_hunt_reaches_the_loop(wd: Path) -> None:
    """`/stop-hunt` must work *while* the hunt is running.

    It could not: the app published ``self._hunt_run`` only after ``run_hunt``
    returned, so for the entire campaign the command read ``None`` and quietly
    refused — "no hunt is running" — while a hunt was very much running. The
    only way out was Ctrl+C. The run is now published before the first wave.
    """
    roster = ('{"summary": "s", "workers": ['
              '{"name": "p", "brief": "b", "owns": ["p"]}]}')
    app = ZimZillaApp(_cfg(wd, boot_rain=False))
    app._team_agent_factory = _hunt_factory(
        {"rosters": [roster], "planner_calls": 0, "prompts": [],
         "recon_final": "r", "final": "none"})

    async with app.run_test(size=(120, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        app._run_hunt("x.test")
        await pilot.pause()
        await pilot.pause()

        # Mid-hunt: the run is live and the command can see it.
        check("stop: the live run is published while the hunt runs",
              app._hunt_run is not None)
        check("stop: the run shares the app's stop flag",
              app._hunt_run is not None
              and app._hunt_run.stop is app._hunt_stop)
        check("stop: the campaign is genuinely in flight", app.busy is True)

        app._handle_command("/stop-hunt")
        await pilot.pause()
        check("stop: the command sets the flag",
              app._hunt_stop.is_set() is True)

        for _ in range(60):
            await pilot.pause()
            if not app.busy:
                break
        await pilot.pause()
        check("stop: the loop noticed and ended", app.busy is False)
        check("stop: the run is unpublished when it is over",
              app._hunt_run is None)


async def test_recon_overlay_fast_target(wd: Path) -> None:
    """A recon that finishes in the same tick it was pushed must not crash.

    The window is pushed on ``hunt_recon_start`` and popped on
    ``hunt_recon_done``. Against a target that answers instantly both land in
    one tick, so the pop tore the widget tree down before the queued Mount
    message arrived and ``on_mount`` raised out of a message handler — which
    Textual treats as an app crash. This drives that exact sequence.
    """
    app = ZimZillaApp(_cfg(wd, boot_rain=False))

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()  # the boot splash
        await pilot.pause()

        from zimzilla.ui.app import ReconOverlay as _RO

        overlay = _RO("x.test", app.palette, app.cfg)
        app.push_screen(overlay)
        # No pause: this is the fast target — recon is already done.
        app._close_recon({"screen": overlay})
        for _ in range(10):
            await pilot.pause()

        check("overlay fast: it did not strand itself on screen",
              app.screen is not overlay, str(app.screen))
        check("overlay fast: the shell is still alive", app.screen is not None)
        check("overlay fast: closing twice is still safe",
              app._close_recon({"screen": None}) is None)


def test_hunt_tool_panel_renders_bash() -> None:
    """The bash panel: whole command, whole response, attributed.

    Rendered straight, without an app and without running anything — the point
    is what the panel does with a real-shaped command, and a command long
    enough that the old 70-character label would have cut the payload off the
    end, which is where a real probe keeps it.
    """
    import io

    from rich.console import Console

    from zimzilla.ui import renderers as R

    p = get_palette("zim")
    command = ("for p in /admin /login /api/v1/users /debug /backup; do "
               "curl -sk -o /dev/null -w \"%{http_code} $p\\n\" "
               "\"https://x.test$p\"; done")
    output = ("403 /admin\n200 /login\n401 /api/v1/users\n"
              "500 /debug\n404 /backup")

    buf = io.StringIO()
    Console(file=buf, width=200, color_system=None,
            legacy_windows=False).print(
        R.hunt_tool_panel("bash", {"command": command}, output, p,
                          agent="paths", color=p.accent, meta={"exit": 0}))
    text = buf.getvalue()

    check("panel: the whole command is rendered", command in text, text[:300])
    check("panel: the tail of the command survives",
          'https://x.test$p"; done' in text)
    check("panel: nothing is ellipsised", "…" not in text)
    check("panel: the response is rendered", "401 /api/v1/users" in text)
    check("panel: the exit status is rendered", "EXIT 0" in text)
    check("panel: the agent is named", "paths" in text, text[:200])
    # The command must lead the body, above the output, so the two are never
    # mistaken for each other.
    check("panel: the command sits above its output",
          text.index("for p in /admin") < text.index("403 /admin"))

    # A failed call is bordered in red and badged with the code, not hidden.
    buf = io.StringIO()
    Console(file=buf, width=200, color_system=None,
            legacy_windows=False).print(
        R.hunt_tool_panel("bash", {"command": "nmap -sV x.test"},
                          "no route to host", p, is_error=True,
                          meta={"exit": 2}))
    failed = buf.getvalue()
    check("panel: a failure is badged with its exit code", "EXIT 2" in failed)
    check("panel: a failure still shows the command", "nmap -sV x.test" in failed)


async def test_transcript_shows_full_command(wd: Path) -> None:
    """The whole point: the operator sees the real call and the real response.

    Before this, a hunt call rendered as one line truncated at 70 characters and
    the tool's response was dropped on the floor entirely — ``hunt_agent_result``
    had no consumer at all. This drives a real campaign through the app and
    reads back the renderables it handed the transcript.

    The stub's one tool call is a ``read_file`` of a file this test writes: the
    suite's standing rule is that a stub agent never runs ``bash``, and the
    panel shape a bash call produces is covered directly by
    ``test_hunt_tool_panel_renders_bash``.
    """
    (wd / "surface.txt").write_text(
        "alpha /admin 403\nbeta /login 200\ngamma /api/v1/users 401\n")

    roster = ('{"summary": "s", "workers": ['
              '{"name": "paths", "brief": "test paths", "owns": ["paths"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "recon: a login form, nginx, 443 open",
        "first_tool": ("read_file", {"path": "surface.txt"}),
        "final": "nothing confirmed",
    }
    factory = _hunt_factory(script)

    app = ZimZillaApp(_cfg(wd, boot_rain=False))
    app._team_agent_factory = factory

    async with app.run_test(size=(140, 40)) as pilot:
        app.pop_screen()  # the boot splash
        await pilot.pause()

        # Stop the campaign from inside the event stream, exactly the way the
        # operator does — `/stop-hunt` sets the run's stop flag and the loop
        # notices it at the next wave boundary. Driving it from the test loop
        # instead would race: the stub agent completes a whole wave per
        # scheduler slice, so dozens can pass between two `pilot.pause()`es and
        # the command lands after the campaign has already ended on its own.
        #
        # The stop is issued at the start of the *second* turn, which is after
        # the tool call and its result have been through the transcript — that
        # ordering is the whole point of this test.
        real_factory = app._team_agent_factory

        def stopping_factory(cfg, **kw):
            agent = real_factory(cfg, **kw)
            stream = agent._stream_once
            calls = {"n": 0}

            async def stopping_stream():
                calls["n"] += 1
                if calls["n"] == 2 and app._hunt_run is not None:
                    app._handle_command("/stop-hunt")
                async for ev in stream():
                    yield ev

            agent._stream_once = stopping_stream
            return agent

        app._team_agent_factory = stopping_factory
        app._run_hunt("x.test")
        for _ in range(80):
            await pilot.pause()
            if not app.busy:
                break
        await pilot.pause()

        check("transcript: the hunt stopped on command", app.busy is False)
        check("transcript: /stop-hunt actually reached the loop",
              app._hunt_run is None, str(app._hunt_run))

        text = _transcript_text(app)
        # The response, which used to be discarded outright.
        check("transcript: the tool's output is rendered",
              "gamma /api/v1/users 401" in text, text[-600:])
        # The call, and the file it named.
        check("transcript: the call is rendered", "surface.txt" in text)
        # And it is attributed, so ten interleaved agents stay followable.
        check("transcript: the panel names the agent", "paths" in text)

        # The result must be a panel, not a bare line of text.
        log = app.query_one(ChatPane).query_one("#transcript")
        panels = [b for b in getattr(log, "_blocks", [])
                  if type(b[0]).__name__ == "Panel"]
        check("transcript: the result is a panel", len(panels) >= 1,
              str(len(panels)))


async def test_loop_rail_shows_hack_during_hunt(wd: Path) -> None:
    """The rail must read HACK — not IDLE — for the length of a campaign.

    A hunt is not a turn, so nothing in the turn runner sets a stage for it:
    the rail sat on IDLE from recon to the closing report, which reads as an
    idle harness while ten agents are attacking the target.

    The samples are taken from inside ``on_event``. ``run_hunt`` awaits every
    event it emits, so a reading taken there is deterministic — driving it from
    the test loop instead would race, because the stub agent finishes a whole
    wave per scheduler slice and the campaign can end between two pauses.
    """
    from zimzilla.ui.rails import LoopRail

    roster = ('{"summary": "s", "workers": ['
              '{"name": "paths", "brief": "test paths", "owns": ["paths"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "recon: nginx, 443 open",
        "final": "nothing confirmed",
    }
    factory = _hunt_factory(script)

    app = ZimZillaApp(_cfg(wd, boot_rain=False))
    app._team_agent_factory = factory

    #: (event, stage, hunt-counters) captured while the campaign runs.
    seen: list[tuple[str, str, dict | None]] = []
    #: The rail once the campaign is over. Read after ``_run_hunt`` returns,
    #: not from the event stream: the reset lives in that worker's `finally`,
    #: which has not run yet while the last event is being delivered.
    after: dict = {}

    async with app.run_test(size=(140, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()
        check("rail: starts idle", app.query_one(LoopRail).stage == "idle")

        real_factory = app._team_agent_factory
        real_run_hunt = hunt_mod.run_hunt

        def observing_factory(cfg, **kw):
            agent = real_factory(cfg, **kw)
            stream = agent._stream_once
            calls = {"n": 0}

            async def stopping_stream():
                calls["n"] += 1
                if calls["n"] == 2 and app._hunt_run is not None:
                    app._handle_command("/stop-hunt")
                async for ev in stream():
                    yield ev

            agent._stream_once = stopping_stream
            return agent

        async def observing_run_hunt(cfg, target, **kw):
            inner = kw.pop("on_event")

            async def wrapped(ev):
                await inner(ev)
                loop = app.query_one(LoopRail)
                seen.append((ev.get("type", ""), loop.stage,
                             dict(loop.hunt) if loop.hunt else None))

            return await real_run_hunt(cfg, target, on_event=wrapped, **kw)

        app._team_agent_factory = observing_factory
        hunt_mod.run_hunt = observing_run_hunt
        try:
            app._run_hunt("x.test")
            for _ in range(80):
                await pilot.pause()
                if not app.busy:
                    break
            await pilot.pause()
        finally:
            hunt_mod.run_hunt = real_run_hunt
        rail = app.query_one(LoopRail)
        after["stage"] = rail.stage
        after["hunt"] = dict(rail.hunt) if rail.hunt else None

        check("rail: the campaign ran", bool(seen), str(len(seen)))
        stages = {stage for _, stage, _ in seen}
        check("rail: HACK is lit while the campaign runs", "hacking" in stages,
              str(sorted(stages)))
        check("rail: never IDLE mid-campaign",
              not [e for e, s, _ in seen if s == "idle" and e != "hunt_end"],
              str([e for e, s, _ in seen if s == "idle"]))
        # It must be lit *before* the first agent starts, not only once one is
        # running — recon is part of the campaign.
        check("rail: HACK is lit from the first event",
              seen[0][1] == "hacking", f"{seen[0][0]} -> {seen[0][1]}")

        # The counters must describe the campaign, not the main agent's turn.
        during = [h for _, s, h in seen if s == "hacking" and h]
        check("rail: campaign counters replace turn/iter", bool(during),
              str(during[:3]))
        check("rail: counters name the wave",
              all("wave" in h for h in during) and any(h["wave"] >= 1
                                                       for h in during),
              str(during[:3]))
        # The roster has one worker, and the rail must say one — not the ten
        # the planner was *asked* for, which is what `hunt_wave_start`'s `size`
        # carries and what the rail showed before this. And `done` must reach
        # exactly that, so a double-count shows up as 2/1.
        check("rail: agents counted from the roster, not the cap",
              all(h["agents"] <= 1 for h in during),
              str([(h["done"], h["agents"]) for h in during]))
        check("rail: agents counted once each",
              any(h["done"] == h["agents"] == 1 for h in during),
              str([(h["done"], h["agents"]) for h in during]))

        # And it hands the rail back when the campaign is over.
        check("rail: reset to idle after the campaign",
              after.get("stage") == "idle", str(after))
        check("rail: campaign counters cleared after the campaign",
              after.get("hunt") is None, str(after))
        check("rail: still idle once the app settles",
              app.query_one(LoopRail).stage == "idle")


async def test_loop_rail_hunt_render() -> None:
    """The rail's own render: the HACK node lights, and only it.

    Pure — no app — because this is about the glyph and the label, which is
    where an added stage goes wrong: ``render_rail`` lights the node whose key
    equals ``self.stage``, so a mistyped key silently lights nothing.
    """
    import io

    from rich.console import Console

    from zimzilla.ui.rails import STAGES, STAGE_LABEL, LoopRail

    def draw(rail: LoopRail) -> str:
        buf = io.StringIO()
        Console(file=buf, width=60, color_system=None,
                legacy_windows=False).print(rail.rail_text())
        return buf.getvalue()

    check("rail: HACK is a stage", "hacking" in STAGES, str(STAGES))
    check("rail: HACK is labelled", STAGE_LABEL.get("hacking") == "HACK",
          str(STAGE_LABEL.get("hacking")))
    check("rail: idle is still last", STAGES[-1] == "idle", str(STAGES))

    # Set the fields directly rather than through the setters: those call
    # ``update()``, which needs a running app. The app-level test above drives
    # the real setters, so what is under test here is only the layout.
    rail = LoopRail(get_palette("zim"))
    rail.stage = "hacking"
    rail.hunt = {"wave": 2, "agents": 10, "done": 4}
    body = draw(rail)
    check("rail: HACK renders", "HACK" in body, body)
    check("rail: wave counter renders", "wave" in body and "2" in body, body)
    check("rail: agent counter renders", "4/10" in body, body)
    check("rail: turn/iter hidden during a hunt",
          "turn" not in body and "iter" not in body, body)

    rail.hunt = None
    rail.stage = "idle"
    body = draw(rail)
    check("rail: turn/iter restored after a hunt",
          "turn" in body and "iter" in body, body)
    check("rail: wave counter gone after a hunt", "wave" not in body, body)


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
        "stop-hunt", "summary-hunt", "findings", "zim-logfare",
        "zim-tokenjuice", "zim-source", "exit", "quit",
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
# The finding tracker
# ---------------------------------------------------------------------------

def test_contract_reaches_the_worker() -> None:
    """The regression that made the tracker necessary.

    ``FINDING_CONTRACT`` used to be formatted into the recon and planner prompts
    only. The wave agents — the only ones that run a test and therefore the only
    ones that can report what it found — never saw it, so they narrated a
    methodology, ``parse_findings`` read no JSON, and every campaign reported
    nothing no matter what it discovered. This pins the contract to the prompt
    the worker is actually handed.
    """
    contract = hunt_mod.FINDING_CONTRACT
    # A phrase that appears only in the contract, so a hit cannot be an accident
    # of the brief's own wording.
    marker = "One entry per distinct bug"
    check("contract: the marker really is in the contract", marker in contract)

    worker_prompt = "recon digest\n\nprobe the login form\n\n" + contract
    check("contract: a worker prompt carrying it is detectable",
          marker in worker_prompt)

    # And the assembly itself, which is what regressed: context, then brief,
    # then contract last, so the reporting block is what the agent reads as it
    # writes its final message.
    spec = team_mod.WorkerSpec(name="auth", brief="probe the login form",
                               owns=["auth"])
    prompt = f"the digest\n\n{spec.brief}"
    if contract:
        prompt = f"{prompt}\n\n{contract}"
    check("contract: the contract is the last thing in the prompt",
          prompt.rstrip().endswith(contract.rstrip()))
    check("contract: the brief is still present", spec.brief in prompt)


async def test_contract_in_worker_prompts(wd: Path) -> None:
    """End to end: the prompt a wave agent is really handed carries it."""
    roster = ('{"summary": "s", "workers": ['
              '{"name": "auth", "brief": "probe auth", "owns": ["auth"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "worker_prompts": [],
        "recon_final": "recon: one host, a login form",
        "final": _finding_json(),
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = wd / "state"

    await hunt_mod.run_hunt(
        cfg, "x.test", on_event=_recorder(events, stop),
        agent_factory=factory, stop=stop, wave_size=1, concurrency=1,
    )

    prompts = script["worker_prompts"]
    check("contract: the worker was handed a prompt", len(prompts) == 1,
          str(len(prompts)))
    check("contract: the worker prompt carries the finding contract",
          "One entry per distinct bug" in prompts[0], prompts[0][-200:])
    check("contract: the contract comes after the brief",
          prompts[0].index("probe auth") < prompts[0].index("One entry per"))


def test_tracker_model() -> None:
    """`add_finding` deduplicates; `ranked` orders worst first."""
    run = hunt_mod.HuntRun(target="x.test", directory=Path("/tmp/x"))

    a = hunt_mod.Finding(wave=1, agent="a", title="SQLi in ?id",
                         severity="critical", asset="api.x.test")
    b = hunt_mod.Finding(wave=1, agent="b", title="IDOR /api/users",
                         severity="high", asset="api.x.test")
    # The same bug, rediscovered by a third agent: same title slug, same asset.
    dup = hunt_mod.Finding(wave=2, agent="c", title="SQLi in ?id",
                           severity="critical", asset="api.x.test")
    # Same title, different asset: a genuinely different finding.
    other = hunt_mod.Finding(wave=2, agent="d", title="SQLi in ?id",
                             severity="medium", asset="www.x.test")

    check("tracker: a new finding is accepted", run.add_finding(a) is True)
    check("tracker: a second distinct finding is accepted",
          run.add_finding(b) is True)
    check("tracker: an exact repeat is dropped", run.add_finding(dup) is False)
    check("tracker: the repeat did not inflate the list",
          len(run.findings) == 2, str(len(run.findings)))
    check("tracker: the first report of a bug is the one kept",
          run.findings[0].agent == "a")
    check("tracker: the same title on another asset is a different finding",
          run.add_finding(other) is True)

    # Worst first, regardless of the order they arrived in.
    run2 = hunt_mod.HuntRun(target="x.test", directory=Path("/tmp/x"))
    for sev in ("info", "low", "medium", "high", "critical"):
        run2.add_finding(hunt_mod.Finding(wave=1, agent="a",
                                          title=f"{sev} bug", severity=sev))
    order = [f.severity for f in run2.ranked()]
    check("tracker: ranked is worst-first",
          order == ["critical", "high", "medium", "low", "info"], str(order))

    # The planner's digest reads in the same order.
    check("tracker: the digest is worst-first too",
          run2.digest().splitlines()[0].startswith("- [critical]"),
          run2.digest().splitlines()[0])


def test_findings_panel() -> None:
    """The panel renders worst-first, and never steals the prompt."""
    from zimzilla.ui.app import FindingsPanel

    p = get_palette("green", None)
    panel = FindingsPanel(p, target="x.test", live=True)
    panel.query_one = lambda *a, **k: _Stub()
    panel.update = lambda *a, **k: None

    check("panel: it is not focusable, so /stop-hunt stays typeable",
          panel.can_focus is False)

    check("panel: an empty tracker says so plainly",
          "nothing confirmed" in str(panel._body()))

    panel.set_findings([
        {"severity": "medium", "title": "missing HSTS", "asset": "x.test",
         "wave": 2, "evidence": "no header", "agent": "misconfig"},
        {"severity": "critical", "title": "SQLi in ?id", "asset": "api.x.test",
         "wave": 1, "evidence": "5 rows returned", "agent": "injection"},
    ])
    body = str(panel._body())
    check("panel: the critical is shown", "SQLi in ?id" in body)
    check("panel: the evidence is shown", "5 rows returned" in body)
    check("panel: the asset is shown", "api.x.test" in body)
    check("panel: the count is in the title", "2" in str(panel._title()))

    # set_findings does not reorder — the caller ranks — so hand it a ranked
    # list and confirm the panel preserves that order top to bottom.
    run = hunt_mod.HuntRun(target="x.test", directory=Path("/tmp/x"))
    run.add_finding(hunt_mod.Finding(wave=1, agent="a", title="low bug",
                                     severity="low"))
    run.add_finding(hunt_mod.Finding(wave=1, agent="b", title="crit bug",
                                     severity="critical"))
    panel.set_findings([hunt_mod._finding_dict(f) for f in run.ranked()])
    body = str(panel._body())
    check("panel: a ranked list renders worst-first",
          body.index("crit bug") < body.index("low bug"), body[:120])


class _Stub:
    """A no-op stand-in for a queried widget, for tests with no live app."""

    def update(self, *a, **k):
        return None


async def test_findings_command(wd: Path) -> None:
    """/findings opens the tracker live, from disk, and refuses when empty."""
    cfg = _cfg(wd, boot_rain=False)
    # Its own state dir: main() shares one workdir across every test, and the
    # earlier hunt tests archive campaigns into <state_dir>/hunts. Sharing it
    # here would mean /findings finds one of those and the "nothing to show"
    # branch could never run.
    cfg.state_dir = wd / "findings-state"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()  # the boot splash
        await pilot.pause()

        # No hunt has run and nothing is archived: it says so rather than
        # opening an empty window.
        app._handle_command("/findings")
        await pilot.pause()
        check("findings: with nothing to show it says so",
              app._findings_panel is None
              and "no findings yet" in _transcript_text(app),
              _transcript_text(app)[-120:])

        # A live run: the panel opens and shows the run's findings, worst first.
        run = hunt_mod.HuntRun(target="x.test", directory=wd / "state" / "h")
        run.add_finding(hunt_mod.Finding(
            wave=1, agent="injection", title="SQLi in ?id", severity="critical",
            asset="api.x.test", evidence="5 rows returned"))
        app._hunt_run = run

        app._handle_command("/findings")
        await pilot.pause()
        panel = app._findings_panel
        check("findings: a live run opens the panel", panel is not None)
        check("findings: the panel is marked live",
              panel is not None and panel.live is True)
        check("findings: the panel carries the finding",
              panel is not None and "SQLi in ?id" in str(panel._body()),
              str(panel._body())[:120] if panel else "")

        # Running it again toggles the panel shut.
        app._handle_command("/findings")
        await pilot.pause()
        check("findings: the command toggles the panel closed",
              app._findings_panel is None)

        app._hunt_run = None

        # A finished campaign on disk: /findings reads it back, like
        # /summary-hunt does.
        finished = hunt_mod.HuntRun(
            target="archived.test",
            directory=hunt_mod.case_dir(cfg, "archived.test"))
        hunt_mod.write_report(finished, hunt_mod.Finding(
            wave=1, agent="idor", title="IDOR /api/users", severity="high",
            asset="archived.test", evidence="user 2 returned"))
        app._handle_command("/findings")
        await pilot.pause()
        panel = app._findings_panel
        check("findings: an archived campaign is read back off disk",
              panel is not None
              and "IDOR /api/users" in str(panel._body()),
              str(panel._body())[:120] if panel else "")
        check("findings: an archived panel is not marked live",
              panel is not None and panel.live is False)


# ---------------------------------------------------------------------------

async def main() -> int:
    test_parse_findings()
    test_fallback()
    test_planner_fallback_path()
    test_brief_context_truncation()
    test_hunt_tool_panel_renders_bash()
    await test_loop_rail_hunt_render()
    test_pane()
    test_contract_reaches_the_worker()
    test_tracker_model()
    test_findings_panel()

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        test_reports(wd)
        await test_hunt_loop(wd)
        await test_contract_in_worker_prompts(wd)
        await test_hunt_replans_from_findings(wd)
        await test_hunt_fallback_wave(wd)
        await test_brief_context(wd)
        await test_planner_retry(wd)
        await test_planner_fallback_shows_raw(wd)
        await test_hunt_result_event_carries_args(wd)
        await test_hunt_stops_midwave(wd)
        await test_hunt_summary_flag(wd)
        await test_recon_reports_live(wd)
        await test_recon_overlay(wd)
        await test_stop_hunt_reaches_the_loop(wd)
        await test_recon_overlay_fast_target(wd)
        await test_transcript_shows_full_command(wd)
        await test_loop_rail_shows_hack_during_hunt(wd)
        await test_busy_gate(wd)
        await test_findings_command(wd)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
