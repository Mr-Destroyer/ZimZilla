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
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import hunt as hunt_mod  # noqa: E402
from zimzilla import team as team_mod  # noqa: E402
from zimzilla import tools as tools_mod  # noqa: E402
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


def _state(wd: Path, name: str) -> Path:
    """A private ``state_dir`` for one test, under the shared workdir.

    Every test used to share ``wd/"state"``, which was harmless while a campaign
    was purely in-memory — but the store is persistent by design now: a repeat
    ``/bug-hunt`` on a scope reuses the recon and findings already on disk. With
    one shared store, the second test to hunt ``x.test`` inherited the first's
    recon and skipped its own, so tests were measuring each other.

    One directory per test restores the isolation the tests were written
    assuming, and keeps it however much state a campaign comes to keep.
    """
    return wd / "state" / name


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
    cfg.state_dir = _state(wd, "test_reports")
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
          len(list(run.directory.glob("summary-*.md"))) == 1,
          str([p.name for p in run.directory.glob("summary-*.md")]))
    check("summary: it is archived into the reports store",
          dest.parent == hunt_mod.reports_dir(cfg), str(dest))
    check("summary: the archive carries the run's shape",
          "waves: 2" in dest.read_text() and "findings: 1" in dest.read_text())

    # A second campaign on the same scope must not overwrite the first's report:
    # the directory is the scope's and is reused, so a fixed name would lose it.
    dest2 = hunt_mod.write_summary(cfg, run, "the second campaign's report")
    check("summary: a second campaign gets its own report file",
          dest2 != dest and dest.is_file() and dest2.is_file(),
          f"{dest.name} vs {dest2.name}")
    check("summary: the first campaign's report survives the second",
          "the closing report" in dest.read_text()
          and "second campaign" in dest2.read_text())


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
        # can_report is forwarded, not dropped: run_hunt arms it on every hunt
        # agent so report_finding is advertised, and a stub that silently lost
        # it would make a live-reporting agent look like one that never calls
        # the tool — hiding the very path these tests exist to cover.
        agent = Agent(cfg, permission_handler=kw.get("permission_handler"),
                      tool_hook=kw.get("tool_hook"),
                      can_report=kw.get("can_report", False))
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
    cfg.state_dir = _state(wd, "test_hunt_loop")

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


def test_scope_naming(wd: Path) -> None:
    """A target files under one scope directory, or is refused.

    The scope is derived from the target rather than used verbatim: a URL and a
    bare host are the same engagement, and a sentence in the target field is an
    operator mistake that must not become a directory name.
    """
    check("scope: a bare host is its own scope",
          hunt_mod.scope_slug("example.com") == "example.com")
    check("scope: a wildcard drops the star",
          hunt_mod.scope_slug("*.lerevecraze.com") == "lerevecraze.com")
    check("scope: a URL files under its host",
          hunt_mod.scope_slug("https://dev.example.com/login") == "dev.example.com")
    check("scope: a port is dropped",
          hunt_mod.scope_slug("example.com:8443") == "example.com")
    check("scope: a CIDR keeps its network",
          hunt_mod.scope_slug("10.0.0.0/24") == "10.0.0.0")

    # A sentence is refused, not truncated. This is the real failure: an
    # operator typed prose into the target field and got a directory named
    # after its first sixty characters.
    for bad, why in [
        ("bdgroup.com the recons are in this directory", "prose"),
        ("find the bugs", "not a host"),
        ("", "empty"),
        ("   ", "whitespace"),
    ]:
        try:
            got = hunt_mod.scope_slug(bad)
        except ValueError:
            check(f"scope: {why} is refused", True)
        else:
            check(f"scope: {why} is refused", False, repr(got))

    # One directory per scope, reused: a second campaign against the same host
    # adds to the first's history rather than starting a sibling.
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = _state(wd, "test_scope_naming")
    first = hunt_mod.case_dir(cfg, "https://mail.lerevecraze.com/login")
    second = hunt_mod.case_dir(cfg, "mail.lerevecraze.com")
    check("scope: the same host reuses one directory", first == second, str(first))
    check("scope: the directory is named for the scope",
          first.name == "mail.lerevecraze.com", first.name)
    check("scope: the campaign subdirectories are made",
          (first / "findings").is_dir() and (first / "plans").is_dir())


async def test_campaign_notes(wd: Path) -> None:
    """Recon and each wave's plan land on disk, so a campaign accumulates notes.

    Recon used to exist only on the run object and each plan only as a UI event,
    so stopping a campaign lost both — the operator's "save the findings" had
    nothing to resolve against, and `/summary-hunt` after a restart said "recon
    produced nothing" about a recon that was the most informative part of the run.
    """
    roster = ('{"summary": "aiming at auth", "workers": ['
              '{"name": "auth", "brief": "test auth", "owns": ["auth", "login"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "the target answers on 443, nginx, a login form at /login",
        "final": _finding_json("auth bypass"),
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = _state(wd, "test_campaign_notes")

    run = await hunt_mod.run_hunt(
        cfg, "notes.test", on_event=_recorder(events, stop),
        agent_factory=factory, stop=stop, wave_size=1, concurrency=1,
    )

    # ---- recon notes
    recon_files = sorted(run.directory.glob("recon-*.md"))
    check("notes: recon is written to the campaign directory",
          len(recon_files) == 1, str([p.name for p in recon_files]))
    body = recon_files[0].read_text()
    check("notes: the recon text is preserved", "nginx" in body)
    check("notes: recon is stamped wave 0", "wave: 0" in body)
    check("notes: recon carries the target", "target: notes.test" in body)
    check("notes: the recon file is named by its timestamp",
          len(recon_files[0].stem) == len("recon-20261010-025510"),
          recon_files[0].name)

    done = [e for e in events if e["type"] == "hunt_recon_done"]
    check("notes: the recon event carries the path it wrote",
          done and done[0].get("saved") == str(recon_files[0]),
          str(done[0].get("saved") if done else None))

    # ---- plans
    plan_files = sorted((run.directory / "plans").glob("wave-*.md"))
    check("notes: the wave's plan is written",
          len(plan_files) == 1, str([p.name for p in plan_files]))
    plan = plan_files[0].read_text()
    check("notes: the plan names its wave", plan_files[0].name.startswith("wave-1-"),
          plan_files[0].name)
    check("notes: the plan keeps each brief", "test auth" in plan)
    check("notes: the plan keeps what each agent owns", "auth, login" in plan)
    check("notes: the plan records the roster size", "agents: 1" in plan)
    check("notes: a read planner is not marked as fallen back",
          "fell_back: false" in plan)
    check("notes: a good plan does not paste the raw reply",
          "## Planner reply" not in plan)

    # ---- recon is also fed to the planner, unchanged by being written first.
    check("notes: recon still reaches the planner", "nginx" in run.recon)

    # ---- and it reads back. A summary asked for after a restart must not say
    # "recon produced nothing" over a recon that mapped the target.
    recovered = hunt_mod.load_run(run.directory)
    check("notes: a campaign reads back off disk", recovered is not None)
    check("notes: the recon is recovered, not lost",
          recovered is not None and "nginx" in recovered.recon,
          (recovered.recon[:80] if recovered else ""))
    check("notes: the recovered recon drops the file's own heading",
          recovered is not None and not recovered.recon.startswith("#"),
          (recovered.recon[:40] if recovered else ""))
    check("notes: the recovered recon drops the frontmatter",
          recovered is not None and "timestamp:" not in recovered.recon,
          (recovered.recon[:60] if recovered else ""))
    check("notes: the recovered wave history names the wave",
          recovered is not None and any(w.startswith("wave 1: 1 agents")
                                        for w in recovered.waves),
          str(recovered.waves if recovered else None))

    # A campaign with recon but no findings is still summarisable — that is the
    # exact case the operator hit: stopped mid-campaign, nothing confirmed, but
    # recon full of information.
    empty = run.directory.parent / "recon-only.test"
    (empty / "findings").mkdir(parents=True)
    hunt_mod.write_recon(hunt_mod.HuntRun(target="recon-only.test",
                                          directory=empty),
                         "an exposed log at /logs, directory listing on /img")
    only = hunt_mod.load_run(empty)
    check("notes: recon alone is enough to summarise a campaign",
          only is not None, str(only))
    check("notes: the recon-only campaign keeps its notes",
          only is not None and "exposed log" in only.recon,
          (only.recon[:80] if only else ""))
    check("notes: a recon-only campaign has no findings",
          only is not None and only.findings == [])


async def test_repeat_hunt_reuses_recon(wd: Path) -> None:
    """A second `/bug-hunt` on a scope plans against the first run's recon.

    Remapping a target that has not moved costs minutes of wall-clock and a
    wave's worth of tokens before the first brief is written. The campaign
    directory is the scope's and is reused, so a repeat run finds the earlier
    notes sitting there — and must skip the recon phase, carry the earlier
    findings into the tracker, and tell the planner its map is second-hand.
    """
    roster = ('{"summary": "s", "workers": ['
              '{"name": "auth", "brief": "test auth", "owns": ["auth"]}]}')
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = _state(wd, "test_repeat_hunt_reuses_recon")

    # ---- run 1: a normal campaign, which leaves recon and a finding on disk.
    first = _hunt_factory({
        "rosters": [roster], "planner_calls": 0, "prompts": [],
        "recon_final": "nginx on 443, a login form at /login",
        "final": _finding_json("username enumeration", asset="dev.x.test"),
    })
    stop = asyncio.Event()
    run1 = await hunt_mod.run_hunt(
        cfg, "repeat.test", on_event=_recorder([], stop),
        agent_factory=first, stop=stop, wave_size=1, concurrency=1,
    )
    check("repeat: the first campaign really mapped the target",
          "nginx" in run1.recon, run1.recon[:60])
    check("repeat: the first campaign left a finding on disk",
          len(run1.findings) == 1, str(len(run1.findings)))

    # ---- run 2: the same scope, no --fresh. Recon must not run.
    events: list[dict] = []
    second_script = {
        "rosters": [roster], "planner_calls": 0, "prompts": [],
        # A distinct recon text, so if recon *did* run this is what the run
        # would carry — the assertion below can then tell reuse from a remap.
        "recon_final": "REMAP HAPPENED",
        "final": _finding_json("xss in search", asset="dev.x.test"),
    }
    second = _hunt_factory(second_script)
    stop2 = asyncio.Event()
    run2 = await hunt_mod.run_hunt(
        cfg, "repeat.test", on_event=_recorder(events, stop2),
        agent_factory=second, stop=stop2, wave_size=1, concurrency=1,
    )

    kinds = [e["type"] for e in events]
    check("repeat: recon is skipped, not re-run",
          "hunt_recon_skipped" in kinds and "hunt_recon_start" not in kinds,
          str(kinds[:4]))
    # The planner is a zim-mode agent too, so the count is of *non-planner*
    # agents: without recon that is the one worker and nothing else.
    check("repeat: no recon agent was built — one worker, no mapper",
          sum(1 for a in second.built if a.cfg.mode != "plan") == 1,
          str([a.cfg.mode for a in second.built]))
    check("repeat: the inherited recon is the earlier one, not a remap",
          "nginx" in run2.recon and "REMAP HAPPENED" not in run2.recon,
          run2.recon[:60])
    check("repeat: the run records where the recon came from",
          run2.reused_recon is True and run2.recon_source is not None
          and run2.recon_source.name.startswith("recon-"),
          str(run2.recon_source))

    # ---- the earlier finding is carried into this campaign's tracker, and
    # marked so it cannot be read as this campaign's haul.
    carried = [f for f in run2.findings if f.carried]
    check("repeat: the earlier finding is carried into the tracker",
          len(carried) == 1 and carried[0].title == "username enumeration",
          str([f.title for f in carried]))
    check("repeat: the carried count is recorded on the run",
          run2.recon_carried == 1, str(run2.recon_carried))
    check("repeat: the skip event carries the carried findings for the rail",
          any(e["type"] == "hunt_recon_skipped" and e.get("carried") == 1
              and any("username enumeration" in f["title"]
                      for f in e.get("findings", []))
              for e in events))
    check("repeat: the skip event names the notes file it reused",
          any(e["type"] == "hunt_recon_skipped"
              and e.get("path", "").endswith(".md") for e in events))

    # ---- the closing count splits carried from own, so the campaign does not
    # claim bugs it never found.
    end = [e for e in events if e["type"] == "hunt_end"]
    check("repeat: the closing count reports only this campaign's findings",
          end and end[0]["findings"] == 1 and end[0]["carried"] == 1,
          str(end[0] if end else None))

    # ---- the planner is told the map is second-hand. `script["prompts"]`
    # collects every planner prompt in order, so the assertion is against what
    # the model was actually handed rather than a prompt rebuilt here.
    plan_prompt = second_script["prompts"][-1]
    check("repeat: the planner is told the recon was not taken now",
          "was NOT taken now" in plan_prompt, plan_prompt[-400:])
    check("repeat: the planner sees the carried finding as carried",
          "carried from an earlier campaign" in plan_prompt,
          plan_prompt[:400])

    # ---- and --fresh forces the mapping phase back on.
    fresh_events: list[dict] = []
    third = _hunt_factory({
        "rosters": [roster], "planner_calls": 0, "prompts": [],
        "recon_final": "REMAP HAPPENED", "final": _finding_json("y"),
    })
    stop3 = asyncio.Event()
    run3 = await hunt_mod.run_hunt(
        cfg, "repeat.test", on_event=_recorder(fresh_events, stop3),
        agent_factory=third, stop=stop3, wave_size=1, concurrency=1,
        fresh=True,
    )
    kinds3 = [e["type"] for e in fresh_events]
    check("repeat: --fresh remaps rather than reusing",
          "hunt_recon_start" in kinds3 and "hunt_recon_skipped" not in kinds3,
          str(kinds3[:4]))
    check("repeat: --fresh builds a recon agent again",
          sum(1 for a in third.built if a.cfg.mode != "plan") == 2,
          str([a.cfg.mode for a in third.built]))
    check("repeat: --fresh takes the new recon",
          "REMAP HAPPENED" in run3.recon, run3.recon[:60])
    # --fresh draws its own map — that is what fresh means — but the bugs
    # earlier campaigns confirmed are still open, so the tracker is seeded with
    # them and they stay marked carried: this campaign found "y", not those.
    check("repeat: --fresh reuses no map",
          run3.recon_source is None and run3.reused_recon is False,
          str(run3.recon_source))
    check("repeat: a fresh remap still seeds the tracker from disk",
          any(f.title == "username enumeration" for f in run3.findings),
          str([f.title for f in run3.findings]))
    check("repeat: the seeded findings stay marked carried",
          run3.recon_carried == 2
          and all(f.carried for f in run3.findings
                  if f.title == "username enumeration"),
          str(run3.recon_carried))


def test_find_recon_reads_the_newest(wd: Path) -> None:
    """``find_recon`` picks the newest notes, and ignores an empty one.

    Newest by filename, not mtime: ``recon-<stamp>.md`` sorts by the timestamp
    it carries. An empty recon is not something to plan against — re-running is
    the honest answer — so it must not be picked up as if it were a map.
    """
    d = wd / "recon-probe"
    d.mkdir(parents=True, exist_ok=True)

    check("find: a directory with no recon yields None",
          hunt_mod.find_recon(d) is None)
    check("find: a directory that does not exist yields None",
          hunt_mod.find_recon(wd / "nope") is None)

    older = d / "recon-20260101-000000.md"
    older.write_text(
        "---\ntarget: x.test\nwave: 0\n---\n\n# Recon\n\nfirst map\n")
    newer = d / "recon-20260202-000000.md"
    newer.write_text(
        "---\ntarget: x.test\nwave: 0\n---\n\n# Recon\n\nsecond map\n")

    note = hunt_mod.find_recon(d)
    check("find: the newest recon is chosen",
          note is not None and "second map" in note.text,
          note.text if note else "")
    check("find: the frontmatter is stripped", note is not None
          and not note.text.startswith("---") and "target:" not in note.text)
    check("find: the heading is stripped", note is not None
          and not note.text.startswith("#"), note.text if note else "")
    check("find: the path is carried for the operator's notice",
          note is not None and note.path == newer)
    check("find: the age is a non-negative number of seconds",
          note is not None and note.age >= 0.0)

    # An empty newest recon falls through to the one with content, rather than
    # being returned as an empty map.
    (d / "recon-20260303-000000.md").write_text(
        "---\ntarget: x.test\nwave: 0\n---\n\n# Recon\n\n")
    note = hunt_mod.find_recon(d)
    check("find: an empty newest recon falls through to the last real one",
          note is not None and "second map" in note.text,
          note.text if note else "")


async def test_plan_records_a_fallback(wd: Path) -> None:
    """A planner whose reply could not be read keeps what it actually said.

    The fixed matrix runs in its place, and the raw reply is the only way to
    tell a malformed object from an empty turn from a prompt that needs
    rewriting — so it has to outlive the event that announced it.
    """
    script = {
        "rosters": ["I'll send ten agents to look at the login page."],
        "planner_calls": 0,
        "prompts": [],
        "recon_final": "nothing reachable",
        "final": _finding_json("x"),
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = _state(wd, "test_plan_records_a_fallback")

    run = await hunt_mod.run_hunt(
        cfg, "fallback.test", on_event=_recorder(events, stop),
        agent_factory=factory, stop=stop, wave_size=2, concurrency=1,
    )

    plan_files = sorted((run.directory / "plans").glob("wave-*.md"))
    check("fallback: the wave still has a plan file", len(plan_files) == 1)
    plan = plan_files[0].read_text()
    check("fallback: the plan is marked as fallen back",
          "fell_back: true" in plan)
    check("fallback: the raw planner reply is kept",
          "I'll send ten agents" in plan, plan[:200])
    check("fallback: the fixed matrix's briefs are recorded",
          "agents: 2" in plan, plan[:300])


def _legacy_campaign(root: Path, name: str, *,
                     target: str = "", finding: str = "f.md") -> Path:
    """A campaign directory in the pre-scope layout: ``<name>/findings/<finding>``."""
    d = root / name / "findings"
    d.mkdir(parents=True, exist_ok=True)
    if finding:
        body = ("---\n" + (f"target: {target}\n" if target else "")
                + "wave: 0\nseverity: high\ntimestamp: 2026-10-09T03:32:50Z\n---\n\n"
                + f"# {name} finding\n")
        (d / finding).write_text(body)
    return root / name


def test_migrate_hunts(wd: Path) -> None:
    """Legacy stamped campaigns fold into one directory per scope, idempotently.

    The old layout made a new ``<slug>-<stamp>`` sibling for every `/bug-hunt`,
    so one engagement scattered across a dozen directories. The scope comes from
    the findings' own ``target`` where there is one — the directory *name* is
    what was wrong, since a prose target became a name.
    """
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = _state(wd, "test_migrate_hunts")
    root = cfg.state_dir / "hunts"
    root.mkdir(parents=True, exist_ok=True)

    # Two legacy campaigns for the same host, one with a wildcard target that
    # must normalise to the same scope as the bare host.
    _legacy_campaign(root, "lerevecraze.com-20261009-084705",
                     target="*.lerevecraze.com", finding="a.md")
    _legacy_campaign(root, "lerevecraze.com-20261010-025510",
                     target="lerevecraze.com", finding="b.md")

    # A prose target whose findings recorded prose too: the name is all there is,
    # and it still has to shed its stamp.
    _legacy_campaign(root, "bdgroup.com-the-recons-are-in-here-20261008-162136",
                     target="bdgroup.com the recons are in here", finding="c.md")

    # A campaign with no findings at all — nothing to derive a scope from.
    _legacy_campaign(root, "empty.test-20261009-000814", finding="")

    # Already scope-shaped: has plans/, so it is left strictly alone.
    done = root / "settled.test"
    (done / "plans").mkdir(parents=True)
    (done / "findings").mkdir()
    (done / "findings" / "keep.md").write_text("---\ntarget: settled.test\n---\n# k\n")

    moved = hunt_mod.migrate_hunts(cfg)
    dests = {str(src.name): dst.name for src, dst in moved}

    check("migrate: both campaigns for one host merge into its scope",
          (root / "lerevecraze.com" / "findings" / "a.md").is_file()
          and (root / "lerevecraze.com" / "findings" / "b.md").is_file(),
          str(sorted(p.name for p in (root / "lerevecraze.com" / "findings").glob("*.md"))))
    check("migrate: a wildcard target files under the bare host",
          dests.get("lerevecraze.com-20261009-084705") == "lerevecraze.com",
          str(dests))
    check("migrate: the legacy directories are gone",
          not (root / "lerevecraze.com-20261009-084705").exists()
          and not (root / "lerevecraze.com-20261010-025510").exists())
    check("migrate: the scope gets the marker, so a hunt can start there",
          (root / "lerevecraze.com" / "plans").is_dir())

    # Prose: the finding recorded prose, so the scope falls back to the name
    # with its stamp stripped — not to the whole sentence.
    check("migrate: a prose-named campaign sheds its stamp",
          dests.get("bdgroup.com-the-recons-are-in-here-20261008-162136")
          == "bdgroup.com-the-recons-are-in-here", str(dests))
    check("migrate: the prose campaign's finding survived",
          (root / "bdgroup.com-the-recons-are-in-here" / "findings" / "c.md").is_file())

    check("migrate: an empty legacy campaign is removed",
          not (root / "empty.test-20261009-000814").exists())
    check("migrate: an empty campaign yields no move",
          "empty.test-20261009-000814" not in dests, str(dests))
    check("migrate: an empty campaign leaves no stray scope directory",
          not (root / "empty.test").exists())

    check("migrate: a scope-shaped directory is left alone",
          (done / "findings" / "keep.md").is_file()
          and "settled.test" not in dests)

    # Idempotent: a second call moves nothing and disturbs nothing.
    again = hunt_mod.migrate_hunts(cfg)
    check("migrate: a second run is a no-op", again == [], str(again))
    check("migrate: the merged findings are still there after a second run",
          len(list((root / "lerevecraze.com" / "findings").glob("*.md"))) == 2,
          str(sorted(p.name for p in (root / "lerevecraze.com" / "findings").glob("*.md"))))

    # A collision keeps both: a genuinely different finding with the same
    # filename must not be overwritten by the migration.
    _legacy_campaign(root, "collide.test-20261009-010101", target="collide.test",
                     finding="dup.md")
    (root / "collide.test" / "findings").mkdir(parents=True, exist_ok=True)
    (root / "collide.test" / "findings" / "dup.md").write_text("the original")
    hunt_mod.migrate_hunts(cfg)
    check("migrate: a filename collision keeps the existing file",
          (root / "collide.test" / "findings" / "dup.md").read_text()
          == "the original")
    check("migrate: nothing to migrate is not an error",
          hunt_mod.migrate_hunts(cfg) == [])


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
    cfg.state_dir = _state(wd, "test_hunt_replans_from_findings")

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
    cfg.state_dir = _state(wd, "test_hunt_fallback_wave")

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
    cfg.state_dir = _state(wd, "test_brief_context")

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
    cfg.state_dir = _state(wd, "test_planner_retry")

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
    cfg.state_dir = _state(wd, "test_planner_fallback_shows_raw")

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
    cfg.state_dir = _state(wd, "test_hunt_result_event_carries_args")

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
    cfg.state_dir = _state(wd, "test_hunt_stops_midwave")
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
    cfg.state_dir = _state(wd, "test_hunt_summary_flag")
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
        # "Does not take focus itself" — not "leaves the prompt usable". The
        # prompt is unreachable behind any of these overlays; see
        # test_wave_overlay_can_still_be_escaped for the pinned behaviour.
        check("overlay: it does not take focus itself",
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


async def test_boot_migrates_hunts(wd: Path) -> None:
    """The app folds legacy campaigns at boot, before any command can read them.

    Migration at boot rather than on first use is what makes `/findings` and
    `/summary-hunt` see the merged layout from the first command — otherwise the
    first lookup after an upgrade reads a half-migrated store.
    """
    cfg = _cfg(wd, boot_rain=False)
    cfg.state_dir = _state(wd, "test_boot_migrates_hunts")
    _legacy_campaign(cfg.state_dir / "hunts", "legacy.test-20261009-010101",
                     target="legacy.test", finding="a.md")

    app = ZimZillaApp(cfg)
    async with app.run_test(size=(110, 40)) as pilot:
        # Dismiss the splash the way the operator does, so `_boot_done` — which
        # is what runs the migration — actually runs. Popping the screen
        # directly would skip it and the test would be checking nothing.
        app._boot_done()
        await pilot.pause()

        check("boot: the legacy campaign is folded into its scope",
              (cfg.state_dir / "hunts" / "legacy.test" / "findings"
               / "a.md").is_file(),
              str(sorted(p.name for p in (cfg.state_dir / "hunts").iterdir())))
        check("boot: the legacy directory is gone",
              not (cfg.state_dir / "hunts" / "legacy.test-20261009-010101").exists())
        check("boot: the main agent is offered the read tool",
              "hunt_findings" in {t["name"] for t in app.agent.tool_schemas()})

        # The merge is said in the transcript, where the operator sees it —
        # not in the agent's context, which is for the agent's own work. Read
        # the transcript's blocks, not the pane's render: the pane renders to
        # Blank when it has no scrollable content yet.
        log = app.query_one(ChatPane).query_one("#transcript")
        body = "\n".join(str(item[0]) for item in getattr(log, "_blocks", []))
        check("boot: the operator is told the merge happened",
              "merged" in body, body[-400:])


async def test_campaign_reaches_the_main_agent(wd: Path) -> None:
    """A finished campaign is put into the main agent's context, with its path.

    A hunt runs on its own Agent instances and the main agent sees none of it,
    so an operator who stopped a wave and said "save the findings" was answered
    "nothing found" over a directory full of evidence. The fix has two halves —
    a read tool, and this notice — and this is the one that does not depend on
    the model choosing to call anything.
    """
    cfg = _cfg(wd, boot_rain=False)
    cfg.state_dir = _state(wd, "test_campaign_reaches_the_main_agent")
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()  # the boot splash
        await pilot.pause()

        before = len(app.agent.messages)
        app._hunt_target = "lerevecraze.com"
        app._register_hunt({
            "type": "hunt_end", "wave": 2, "findings": 3, "saved": 2,
            "directory": str(cfg.state_dir / "hunts" / "lerevecraze.com"),
        })
        await pilot.pause()

        # Buffered, not appended: the history must keep its user/assistant
        # alternation, because the gateway behind ANTHROPIC_BASE_URL may not
        # merge two user turns the way the API does.
        check("notice: the campaign is buffered for the next turn",
              app.agent._drain_notices() != "" and len(app.agent.messages) == before,
              f"{before} -> {len(app.agent.messages)}")

        # It leads the next turn, so the model sees it before the instruction.
        app._register_hunt({
            "type": "hunt_end", "wave": 2, "findings": 3, "saved": 2,
            "directory": str(cfg.state_dir / "hunts" / "lerevecraze.com"),
        })
        seen: list[str] = []

        async def _fake_stream():
            # Records what the turn is about to send, then ends the stream so
            # run_turn unwinds without needing a gateway.
            seen.append(app.agent.messages[-1]["content"])
            return
            yield  # pragma: no cover - marks this an async generator

        app.agent._stream_once = _fake_stream
        async for _ in app.agent.run_turn("save the findings"):
            pass

        check("notice: it leads the next turn's user text",
              seen and seen[0].startswith("[hunt]"), (seen[0][:120] if seen else ""))
        check("notice: the operator's instruction is still in the turn",
              seen and "save the findings" in seen[0], (seen[0][-80:] if seen else ""))
        notice = seen[0] if seen else ""
        check("notice: it names the campaign directory",
              "hunts/lerevecraze.com" in notice, notice[:200])
        check("notice: it names the target", "lerevecraze.com" in notice)
        check("notice: it carries the counts",
              "findings: 3" in notice and "waves: 2" in notice, notice[:300])
        check("notice: it points at the read tool",
              "hunt_findings" in notice, notice[:400])
        check("notice: it warns against a false 'nothing found'",
              "nothing was found" in notice, notice[:400])
        check("notice: a second turn does not repeat it",
              app.agent._drain_notices() == "")

        # A hunt event with no directory must not buffer a dangling note —
        # there would be nothing for the agent to act on.
        app._register_hunt({"type": "hunt_end", "wave": 0, "findings": 0})
        await pilot.pause()
        check("notice: an event with no directory adds nothing",
              app.agent._drain_notices() == "")


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
    cfg.state_dir = _state(wd, "test_recon_reports_live")

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


async def test_overlays_do_not_stack(wd: Path) -> None:
    """The wave web must not leave the recon window stranded underneath it.

    ``_close_recon`` used to defer its pop a tick when the screen had not
    mounted, and the deferred half re-checked ``self.screen is screen`` before
    popping. By the time it ran, ``hunt_wave_start`` had pushed the wave web on
    top, so the check failed and recon stayed on the stack — under the web, for
    the whole campaign, with a fresh web stacked above it every wave. The
    operator saw the first wave's screen and then nothing that repainted.

    The order here is the real one: recon's done event, then the wave start.
    """
    app = ZimZillaApp(_cfg(wd, boot_rain=False))

    from zimzilla.ui.app import ReconOverlay as _RO
    from zimzilla.ui.app import WaveOverlay as _WO

    async with app.run_test(size=(140, 40)) as pilot:
        app.pop_screen()  # the boot splash
        await pilot.pause()

        recon = _RO("x.test", app.palette, app.cfg)
        app.push_screen(recon)
        # Recon finishes in the same tick it went up, and the wave web follows
        # immediately — the exact sequence that stranded recon.
        app._close_recon({"screen": recon})
        web = _WO("x.test", app.palette, wave=1, size=10)
        app.push_screen(web)
        for _ in range(10):
            await pilot.pause()

        names = [type(s).__name__ for s in app.screen_stack]
        check("stack: recon is gone", recon not in app.screen_stack, str(names))
        check("stack: the wave web is the active screen", app.screen is web,
              str(names))

        # A wave ends and the next one starts. Neither web may survive.
        web2 = _WO("x.test", app.palette, wave=2, size=10)
        app._close_wave({"screen": web})
        app.push_screen(web2)
        for _ in range(10):
            await pilot.pause()
        names = [type(s).__name__ for s in app.screen_stack]
        check("stack: the finished wave is gone", web not in app.screen_stack,
              str(names))
        check("stack: only one web is up", names.count("WaveOverlay") == 1,
              str(names))
        check("stack: nothing is stranded above the shell",
              names == ["Screen", "WaveOverlay"], str(names))

        # And the whole thing unwinds cleanly at the end of the campaign.
        app._close_wave({"screen": web2})
        for _ in range(10):
            await pilot.pause()
        check("stack: the shell is back",
              [type(s).__name__ for s in app.screen_stack] == ["Screen"],
              str([type(s).__name__ for s in app.screen_stack]))
        check("stack: the app is still alive", app.is_running)


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
        "zim-tokenjuice", "zim-source", "zim-tokenharbour",
        "tokenharbour-api-setup", "tokenharbour-models", "logfare-models",
        "exit", "quit",
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
    cfg.state_dir = _state(wd, "test_contract_in_worker_prompts")

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


def test_report_finding_tool() -> None:
    """The tool the live tracker is fed by, and the gate around it.

    This is the seam the whole real-time feature hangs off: without a tool that
    returns the finding on ``meta``, the tracker can only ever be filled in
    after a wave ends, which is exactly the behaviour the operator asked to
    change.
    """
    from zimzilla import tools as tools_mod

    schema = next((t for t in tools_mod.TOOL_SCHEMAS
                   if t["name"] == "report_finding"), None)
    check("tool: report_finding is advertised in the schema list",
          schema is not None)
    if schema is not None:
        props = schema["input_schema"]["properties"]
        check("tool: it takes a title, severity and evidence",
              {"title", "severity", "evidence"} <= set(props), str(sorted(props)))
        # The severity enum is a literal in the schema rather than an import of
        # hunt.VALID_SEVERITIES, because hunt imports agent which imports tools
        # — reaching back would close the cycle. A literal that drifts from the
        # real set would let the model emit a severity the tracker cannot place,
        # so the two are pinned together here.
        check("tool: the severity enum matches the tracker's severities",
              tuple(props["severity"]["enum"]) == hunt_mod.VALID_SEVERITIES,
              str(props["severity"].get("enum")))

    # It writes nothing, so it must not need permission — a prompt in the middle
    # of a wave would stall ten agents behind one operator keystroke.
    check("tool: report_finding is not gated",
          tools_mod.needs_permission("report_finding") is False)

    # A valid call returns the finding on meta, normalised.
    res = tools_mod.execute("report_finding", {
        "title": "SQL injection in ?id", "severity": "CRITICAL",
        "asset": "api.x.test", "evidence": "curl -d \"'\" returned 500",
    }, _cfg(Path(tempfile.mkdtemp())))
    check("tool: a valid report succeeds", res.is_error is False, res.output)
    meta = res.meta or {}
    check("tool: the finding rides out on meta", "finding" in meta, str(meta))
    f = meta.get("finding") or {}
    check("tool: the title is carried", f.get("title") == "SQL injection in ?id")
    check("tool: the severity is normalised", f.get("severity") == "critical",
          str(f.get("severity")))
    check("tool: the evidence is carried",
          "500" in str(f.get("evidence", "")))

    # Missing pieces are refused, not silently recorded — a finding with no
    # evidence is worse than no finding.
    bad = tools_mod.execute("report_finding", {"title": "x", "severity": "high"},
                            _cfg(Path(tempfile.mkdtemp())))
    check("tool: a report with no evidence is an error", bad.is_error is True,
          bad.output)
    check("tool: the rejected report still carries no finding",
          "finding" not in (bad.meta or {}))
    empty = tools_mod.execute("report_finding", {"severity": "high",
                                                 "evidence": "x"},
                              _cfg(Path(tempfile.mkdtemp())))
    check("tool: a report with no title is an error", empty.is_error is True)

    # The summariser the transcript uses, so the call reads as a one-liner.
    line = tools_mod.summarise_call(
        "report_finding", {"title": "SQLi", "severity": "critical"},
        _cfg(Path(tempfile.mkdtemp())))
    check("tool: the transcript summarises the call", "SQLi" in line, line)

    # And the gate: only a hunt agent is advertised it.
    plain = Agent(_cfg(Path(tempfile.mkdtemp())))
    armed = Agent(_cfg(Path(tempfile.mkdtemp())), can_report=True)
    plain_tools = {t["name"] for t in plain.tool_schemas()}
    armed_tools = {t["name"] for t in armed.tool_schemas()}
    check("tool: an ordinary session is not offered report_finding",
          "report_finding" not in plain_tools)
    check("tool: a hunt agent is offered report_finding",
          "report_finding" in armed_tools)
    denied = plain._denied("report_finding")
    check("tool: an ordinary session calling it is denied with a reason",
          denied is not None and "hunt" in denied, str(denied))


def test_hunt_findings_tool(wd: Path) -> None:
    """The main agent's read tool: a campaign on disk, answered from disk.

    A hunt runs on its own Agent instances, so the main agent sees none of it.
    When the operator stopped a wave and said "save the findings", it had no
    path to the campaign directory and said nothing was found — over a directory
    that already held a high-severity finding. This tool is that path.
    """
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = _state(wd, "test_hunt_findings_tool")

    # Nothing has run yet: the tool must say so, not invent a campaign. On its
    # own pristine store, since the tests above share this workdir and have
    # already made campaigns in it.
    virgin = _cfg(Path(tempfile.mkdtemp()), mode="zim")
    virgin.state_dir = Path(tempfile.mkdtemp()) / "state"
    empty = tools_mod.execute("hunt_findings", {}, virgin)
    check("hunt_findings: no campaign yet is stated plainly",
          "no /bug-hunt campaign" in empty.output, empty.output)

    # Build two campaigns: one with findings, recon and plans, and a later one
    # that must win the "most recent" default.
    old = cfg.state_dir / "hunts" / "quiet.test"
    (old / "findings").mkdir(parents=True)
    (old / "plans").mkdir()
    hunt_mod.write_recon(hunt_mod.HuntRun(target="quiet.test", directory=old),
                         "quiet.test answers on 80, nothing else")

    run = hunt_mod.HuntRun(target="target.test", directory=(
        cfg.state_dir / "hunts" / "target.test"))
    hunt_mod.case_dir(cfg, "target.test")
    hunt_mod.write_recon(run, "443 open, nginx, a login form at /login")
    hunt_mod.write_plan(run, 1, hunt_mod.team_mod.Roster(
        summary="aiming at auth",
        workers=[hunt_mod.team_mod.WorkerSpec(
            name="auth", brief="test auth", owns=["auth"])]),
        raw="", fell_back=False)
    hunt_mod.write_report(run, hunt_mod.Finding(
        wave=1, agent="auth", title="auth bypass", severity="critical",
        asset="target.test/login", summary="no check", evidence="curl"))

    # ---- findings
    res = tools_mod.execute("hunt_findings", {"scope": "target.test"}, cfg)
    check("hunt_findings: it names the campaign", "target.test" in res.output,
          res.output[:120])
    check("hunt_findings: it reports the finding",
          "auth bypass" in res.output and "CRITICAL" in res.output, res.output)
    check("hunt_findings: it gives the file path to read in full",
          "wave-1-auth-bypass.md" in res.output, res.output)
    check("hunt_findings: it does not error", res.is_error is False)

    # ---- the default is the most recently written campaign
    recent = tools_mod.execute("hunt_findings", {}, cfg)
    check("hunt_findings: with no scope it reads the most recent campaign",
          "target.test" in recent.output, recent.output[:120])

    # ---- "most recent" follows the artefacts, not the directory's mtime.
    # migrate_hunts moves files *into* a scope directory and bumps its mtime to
    # now, so a week-old campaign merged at boot would otherwise sort ahead of
    # one that finished an hour ago. The artefacts' mtimes survive the move.
    stale = cfg.state_dir / "hunts" / "stale.test"
    hunt_mod.write_recon(hunt_mod.HuntRun(target="stale.test", directory=stale),
                         "an old campaign, merged at boot")
    old_time = time.time() - 86400 * 7
    for path in stale.rglob("*"):
        os.utime(path, (old_time, old_time))
    os.utime(stale, (time.time(), time.time()))   # the move bumped the dir
    still = tools_mod.execute("hunt_findings", {}, cfg)
    check("hunt_findings: a freshly-touched old directory does not win",
          "target.test" in still.output, still.output[:120])

    # ---- the scope is reduced the same way the directory name is, so what the
    # operator types finds the campaign however they think of the engagement.
    for typed in ("https://target.test/login", "target.test:443", "TARGET.TEST"):
        hit = tools_mod.execute("hunt_findings", {"scope": typed}, cfg)
        check(f"hunt_findings: scope {typed!r} finds the campaign",
              "auth bypass" in hit.output, hit.output[:120])

    # ---- recon
    recon = tools_mod.execute("hunt_findings", {"scope": "target.test",
                                                "include": "recon"}, cfg)
    check("hunt_findings: recon is read back",
          "nginx" in recon.output and "/login" in recon.output, recon.output[:200])
    check("hunt_findings: recon is not the frontmatter",
          "timestamp:" not in recon.output, recon.output[:200])

    # ---- plans
    plans = tools_mod.execute("hunt_findings", {"scope": "target.test",
                                                "include": "plans"}, cfg)
    check("hunt_findings: the wave plan is listed",
          "wave 1" in plans.output and "1 agents" in plans.output, plans.output)

    # ---- all
    everything = tools_mod.execute("hunt_findings", {"scope": "target.test",
                                                     "include": "all"}, cfg)
    check("hunt_findings: 'all' carries findings, recon and plans",
          "auth bypass" in everything.output and "nginx" in everything.output
          and "wave 1" in everything.output)

    # ---- a campaign matched by the target recorded in its findings, not just
    # by the directory name.
    by_target = tools_mod.execute("hunt_findings", {"scope": "quiet.test"}, cfg)
    check("hunt_findings: a scope with no findings says so",
          "none were confirmed" in by_target.output, by_target.output)

    # ---- bad input is refused with a reason, not a traceback
    bad = tools_mod.execute("hunt_findings", {"include": "everything"}, cfg)
    check("hunt_findings: an unknown include is an error",
          bad.is_error is True and "findings" in bad.output, bad.output)
    missing = tools_mod.execute("hunt_findings", {"scope": "nope.test"}, cfg)
    check("hunt_findings: an unknown scope names the known ones",
          missing.is_error is False and "Known scopes" in missing.output,
          missing.output)

    # ---- the gate. The main agent gets it; a hunt agent must not.
    main = Agent(_cfg(Path(tempfile.mkdtemp())))
    hunter = Agent(_cfg(Path(tempfile.mkdtemp())), can_report=True,
                   can_hunt_read=False)
    main_tools = {t["name"] for t in main.tool_schemas()}
    hunter_tools = {t["name"] for t in hunter.tool_schemas()}
    check("hunt_findings: an ordinary session is offered it",
          "hunt_findings" in main_tools)
    check("hunt_findings: a hunt agent is NOT offered it",
          "hunt_findings" not in hunter_tools, str(sorted(hunter_tools)))
    denied = hunter._denied("hunt_findings")
    check("hunt_findings: a hunt agent calling it is denied with a reason",
          denied is not None and "independent" in denied, str(denied))
    check("hunt_findings: report_finding is still hunt-only",
          "report_finding" in hunter_tools and "report_finding" not in main_tools)

    # The tool name and the store path are pinned here because tools.py cannot
    # import hunt (hunt imports agent imports tools) — see tools._HUNTS_DIR.
    check("hunt_findings: the store path matches hunt.case_dir",
          hunt_mod.case_dir(cfg, "target.test").parent.name == tools_mod._HUNTS_DIR,
          tools_mod._HUNTS_DIR)


def test_finding_from_tool() -> None:
    """A tool's meta dict becomes a Finding, normalised, or nothing at all."""
    make = hunt_mod._finding_from_tool
    f = make({"finding": {"title": "  IDOR /api/users ", "severity": "SEVERE",
                          "asset": " api.x.test ", "evidence": "e"}},
             wave=3, agent="agent-7")
    check("live: a well-formed meta becomes a finding", f is not None)
    if f is None:
        return
    check("live: the title is trimmed", f.title == "IDOR /api/users", f.title)
    check("live: the severity is normalised", f.severity == "critical", f.severity)
    check("live: the asset is trimmed", f.asset == "api.x.test", f.asset)
    check("live: the wave is the one it was reported in", f.wave == 3)
    check("live: the agent is the one that reported it", f.agent == "agent-7")

    # Anything that is not a finding must yield None rather than a blank
    # Finding, which would land in the tracker as an untitled entry.
    check("live: a missing finding key yields nothing",
          make({}, wave=1, agent="a") is None)
    check("live: a non-dict yields nothing",
          make({"finding": "oops"}, wave=1, agent="a") is None)
    check("live: a finding with no title yields nothing",
          make({"finding": {"severity": "high", "evidence": "e"}},
               wave=1, agent="a") is None)


async def test_live_reporting_mid_wave(wd: Path) -> None:
    """A report_finding call publishes before the wave ends.

    This is the operator's actual request: agent-3 finds something and it shows
    in the tracker *then*, not when the hunt finishes. The assertion that makes
    it real is ordering — a ``hunt_finding`` must appear in the event stream
    before the ``hunt_wave_end`` for the wave it was found in.
    """
    roster = ('{"summary": "s", "workers": ['
              '{"name": "auth", "brief": "probe auth", "owns": ["auth"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "worker_prompts": [],
        "recon_final": "recon: one host",
        # The worker's one tool call is the live report.
        "first_tool": ("report_finding", {
            "title": "SQL injection in ?id", "severity": "critical",
            "asset": "api.x.test", "evidence": "curl returned a stack trace",
        }),
        # And its final message carries the same bug, as the contract asks. The
        # tracker must not count it twice.
        "final": _finding_json("SQL injection in ?id", asset="api.x.test"),
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = _state(wd, "test_live_reporting_mid_wave")

    run = await hunt_mod.run_hunt(
        cfg, "x.test", on_event=_recorder(events, stop),
        agent_factory=factory, stop=stop, wave_size=1, concurrency=1,
    )

    kinds = [e["type"] for e in events]
    check("live: a finding is published", "hunt_finding" in kinds, str(kinds))
    if "hunt_finding" in kinds and "hunt_wave_end" in kinds:
        first_finding = kinds.index("hunt_finding")
        wave_end = kinds.index("hunt_wave_end")
        check("live: the finding is published BEFORE the wave ends",
              first_finding < wave_end,
              f"finding@{first_finding} wave_end@{wave_end}")

        # And specifically after the agent started, so it really is mid-turn.
        check("live: the finding lands after its agent started",
              kinds.index("hunt_agent_start") < first_finding)

        # The event carries the finding, normalised, attributed to the agent.
        ev = next(e for e in events if e["type"] == "hunt_finding")
        got = ev["finding"]
        check("live: the published finding is attributed to the worker",
              got["agent"] == "auth", str(got.get("agent")))
        check("live: the published finding keeps its severity",
              got["severity"] == "critical", str(got.get("severity")))

        # Exactly one, even though the agent both called the tool and repeated
        # it in its final JSON: the tracker deduplicates the two paths.
        n = sum(1 for e in events if e["type"] == "hunt_finding")
        check("live: the tool report and the final JSON are not double-counted",
              n == 1, str(n))
        check("live: the tracker holds exactly one finding",
              len(run.findings) == 1, str(len(run.findings)))
        # A critical earns a report file, even though it arrived live.
        check("live: a live critical still gets a report written",
              run.findings[0].saved, str(run.findings[0].severity))


async def test_live_reporting_is_per_agent(wd: Path) -> None:
    """Two agents reporting the same bug publish it once, from the first.

    The tracker is shared across a wave, so the second agent to rediscover a bug
    must not publish it again — otherwise the operator's feed shows the same
    finding twice and the wave's count is wrong.
    """
    roster = ('{"summary": "s", "workers": ['
              '{"name": "auth", "brief": "probe auth", "owns": ["auth"]},'
              '{"name": "api", "brief": "probe api", "owns": ["api"]}]}')
    script = {
        "rosters": [roster],
        "planner_calls": 0,
        "prompts": [],
        "worker_prompts": [],
        "recon_final": "recon: one host",
        "first_tool": ("report_finding", {
            "title": "SQL injection in ?id", "severity": "high",
            "asset": "api.x.test", "evidence": "same bug, found twice",
        }),
        # Neither agent repeats it in its final message, so every finding in the
        # stream came from a live report.
        "final": '{"findings": []}',
    }
    factory = _hunt_factory(script)

    events: list[dict] = []
    stop = asyncio.Event()
    cfg = _cfg(wd, mode="zim")
    cfg.state_dir = _state(wd, "test_live_reporting_is_per_agent")

    run = await hunt_mod.run_hunt(
        cfg, "x.test", on_event=_recorder(events, stop),
        agent_factory=factory, stop=stop, wave_size=2, concurrency=2,
    )

    published = [e["finding"] for e in events if e["type"] == "hunt_finding"]
    check("live: the shared bug is published once for the wave",
          len(published) == 1, str(len(published)))
    check("live: the tracker holds one finding", len(run.findings) == 1,
          str(len(run.findings)))
    if published:
        # Which agent wins is a scheduling race — both report it — so the
        # assertion is that the winner is one of the two, not which one.
        check("live: the published finding names the agent that reported it",
              published[0]["agent"] in ("auth", "api"), published[0]["agent"])
        # The wave's own count has to include what was published live, or the
        # transcript reads "0 finding(s)" for a wave that announced a bug.
        check("live: the wave-end count includes the live finding",
              any(e.get("found") == 1 for e in events
                  if e["type"] == "hunt_wave_end"),
              str([e.get("found") for e in events if e["type"] == "hunt_wave_end"]))
        check("live: the run's own wave log agrees",
              any("1 finding(s)" in line for line in run.waves), str(run.waves))


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

    # Not focusable, so it never takes the keyboard — but that is not the same
    # as /stop-hunt staying typeable, which it does not while this panel is up.
    check("panel: it is not focusable", panel.can_focus is False)

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


async def test_bug_hunt_reuses_recon_from_the_ui(wd: Path) -> None:
    """`/bug-hunt` on a mapped scope says so up front and skips the remap.

    The operator should learn that the campaign is reusing notes — and how old
    they are — *before* the first brief is written, not by noticing the recon
    window never appeared. `--fresh` is the escape hatch and must be readable
    from either side of the target.
    """
    cfg = _cfg(wd, boot_rain=False)
    cfg.state_dir = wd / "bug-hunt-reuse-state"

    # Recon already on disk for this scope, as a prior campaign would leave it.
    directory = hunt_mod.case_dir(cfg, "mapped.test")
    (directory / "findings").mkdir(parents=True, exist_ok=True)
    hunt_mod.write_recon(
        hunt_mod.HuntRun(target="mapped.test", directory=directory),
        "nginx on 443, a login form at /login")

    roster = ('{"summary": "s", "workers": ['
              '{"name": "p", "brief": "b", "owns": ["p"]}]}')

    def build_app() -> ZimZillaApp:
        """An app whose campaign stops itself after its first wave.

        A campaign is a loop that runs until stopped, so a test that just
        starts one leaves it running — and a free-running hunt writes findings
        and plans into the shared scope directory, which the *next* app in this
        test then reads as prior state. Stopping from inside the event stream is
        the same idiom the transcript tests use: `/stop-hunt` sets the flag and
        the loop notices at the next wave boundary.
        """
        a = ZimZillaApp(_cfg(wd, boot_rain=False))
        a.cfg.state_dir = cfg.state_dir
        real = _hunt_factory(
            {"rosters": [roster], "planner_calls": 0, "prompts": [],
             "recon_final": "REMAP HAPPENED", "final": "none"})

        def factory(c, **kw):
            agent = real(c, **kw)
            stream = agent._stream_once
            calls = {"n": 0}

            async def stopping_stream():
                calls["n"] += 1
                if calls["n"] == 2 and a._hunt_run is not None:
                    a._handle_command("/stop-hunt")
                async for ev in stream():
                    yield ev

            agent._stream_once = stopping_stream
            return agent

        factory.built = real.built
        a._team_agent_factory = factory
        return a

    app = build_app()
    async with app.run_test(size=(120, 40)) as pilot:
        app.pop_screen()  # the boot splash
        await pilot.pause()

        # Typed, not called directly: the flag has to survive argument parsing,
        # and `--fresh` may sit either side of the target.
        app._handle_command("/bug-hunt mapped.test")
        await pilot.pause()
        await pilot.pause()

        text = _transcript_text(app)
        check("ui reuse: the notice says the notes are being reused",
              "reusing notes" in text, text[-260:])
        check("ui reuse: the notice is shown before the campaign's recon runs",
              "mapping the surface" not in text, text[-260:])
        check("ui reuse: no recon window was pushed",
              not isinstance(app.screen, ReconOverlay), str(app.screen))

        for _ in range(80):
            await pilot.pause()
            if not app.busy:
                break
        await pilot.pause()
        check("ui reuse: the campaign stopped cleanly", app.busy is False)

    # ---- --fresh, read from the *left* of the target.
    app2 = build_app()
    async with app2.run_test(size=(120, 40)) as pilot:
        app2.pop_screen()
        await pilot.pause()
        app2._handle_command("/bug-hunt --fresh mapped.test")
        await pilot.pause()
        text = _transcript_text(app2)
        check("ui reuse: --fresh before the target forces a remap",
              "remapping the target" in text and "reusing notes" not in text,
              text[-260:])
        for _ in range(80):
            await pilot.pause()
            if not app2.busy:
                break
        await pilot.pause()

    # ---- and from the right, because both read naturally.
    app3 = build_app()
    async with app3.run_test(size=(120, 40)) as pilot:
        app3.pop_screen()
        await pilot.pause()
        app3._handle_command("/bug-hunt mapped.test --fresh")
        await pilot.pause()
        text = _transcript_text(app3)
        check("ui reuse: --fresh after the target forces a remap too",
              "remapping the target" in text and "reusing notes" not in text,
              text[-260:])
        check("ui reuse: the flag does not leak into the scope directory",
              "fresh" not in directory.name, directory.name)
        for _ in range(80):
            await pilot.pause()
            if not app3.busy:
                break
        await pilot.pause()


# ---------------------------------------------------------------------------

async def test_stream_turn_watchdog() -> None:
    """The planner's prose reaches the UI while it is still thinking.

    A turn's prose arrives as ``text_delta`` events, and the consumers only
    forward it on a tool call or at the end of the turn. The wave planner
    reasons for tens of seconds over the whole recon before it writes a word of
    roster, so without a bound on the wait the wave view has nothing to draw for
    that entire time — the empty screen the planning feed exists to fill.

    What is asserted is only *when* prose is handed on: nothing is invented,
    reordered, or dropped, and the events still arrive in order.
    """
    class _Slow:
        """An agent whose stream emits a delta, stalls, then finishes.

        ``closed`` is appended to from the generator's own ``finally``, which is
        what ``aclose`` on the async generator triggers. It is deliberately not
        an ``aclose`` on the agent: ``_stream_turn`` holds the agent's
        ``run_turn`` *generator*, and it is that generator which has to be
        finalised so the HTTP response behind it is released. Asserting on an
        agent-level method would pass without ever proving the generator closed.
        """

        def __init__(self, events, *, gap, closed):
            self._events = events
            self._gap = gap
            self._closed = closed

        async def run_turn(self, prompt):
            try:
                for ev in self._events:
                    await asyncio.sleep(self._gap)
                    yield ev
            finally:
                self._closed.append(True)

    # A stream slower than the idle bound: on_idle must fire between events.
    closed: list[bool] = []
    agent = _Slow([{"type": "text_delta", "text": "looking at wp-json"},
                   {"type": "text_delta", "text": "then auth"}],
                  gap=0.05, closed=closed)
    flushes: list[int] = []

    async def on_idle():
        flushes.append(len(flushes))

    seen = [ev async for ev in hunt_mod._stream_turn(agent, "plan", on_idle,
                                                     idle=0.01)]
    check("watchdog: a slow stream flushes while it waits", len(flushes) >= 2,
          f"flushes={len(flushes)}")
    check("watchdog: every event still arrives", len(seen) == 2, str(len(seen)))
    check("watchdog: the events are unchanged and in order",
          [e["text"] for e in seen] == ["looking at wp-json", "then auth"],
          str([e["text"] for e in seen]))
    check("watchdog: the agent's stream is closed", closed == [True], str(closed))

    # A stream that never goes quiet must not flush spuriously: flushing
    # mid-sentence would put a partial clause on screen as if it were the
    # planner's whole thought.
    closed2: list[bool] = []
    fast = _Slow([{"type": "text_delta", "text": "a"},
                  {"type": "text_delta", "text": "b"}],
                 gap=0.0, closed=closed2)
    flushes2: list[int] = []
    seen2 = [ev async for ev in hunt_mod._stream_turn(
        fast, "plan", lambda: flushes2.append(1), idle=5.0)]
    check("watchdog: a stream that keeps up does not flush spuriously",
          flushes2 == [], f"flushes={len(flushes2)}")
    check("watchdog: the fast stream still yields both events",
          len(seen2) == 2, str(len(seen2)))

    # A consumer that walks away — the hunt stopped mid-turn — must still close
    # the underlying stream, or the agent's HTTP response is left open.
    #
    # The close is explicit because ``break`` does not finalise an async
    # generator: it stays suspended until the collector reaches it, which for a
    # turn holding an HTTP response is too late to rely on. So the caller closes
    # the generator, and this asserts that doing so reaches the agent's own
    # ``finally`` — that is the whole point of ``_stream_turn`` wrapping it.
    closed3: list[bool] = []
    abandoned = _Slow([{"type": "text_delta", "text": "x"}] * 50,
                      gap=0.0, closed=closed3)
    stream = hunt_mod._stream_turn(abandoned, "plan", _noop_idle, idle=5.0)
    async for _ in stream:
        break
    await stream.aclose()
    check("watchdog: an abandoned turn closes its stream",
          closed3 == [True], str(closed3))


async def _noop_idle() -> None:
    return None


async def test_planner_feed_reaches_the_wave_view() -> None:
    """The planner's live reasoning is routed to the wave view, not dropped.

    ``_collect_text_live`` emits ``hunt_plan_text`` and ``hunt_plan_tool`` while
    the planner works. Those events have to reach ``WaveWeb`` or the planning
    state draws an empty box — which is the whole complaint: the operator
    watches a blank screen for the length of a model call with no way to tell
    planning from a hang.
    """
    from zimzilla.theme import get_palette
    from zimzilla.ui.web import WaveWeb

    web = WaveWeb("t.test", get_palette("green", None), wave=1, size=10)
    check("plan feed: a fresh wave is planning", web.planning is True)

    events: list[dict] = []

    async def on_event(ev):
        events.append(ev)
        # This is the same routing the overlay's ``note`` does.
        if ev["type"] == "hunt_plan_text":
            web.planner_text(ev.get("text", ""))
        elif ev["type"] == "hunt_plan_tool":
            web.planner_tool(ev.get("tool", ""), ev.get("args") or {},
                             _cfg(Path(".")))

    class _Planner:
        """A planner that thinks out loud, then calls one tool."""

        async def run_turn(self, prompt):
            yield {"type": "text_delta",
                   "text": "the auth bypass is closed so wave 2 pivots"}

        async def aclose(self):
            return None

    class _PlannerWithTool:
        async def run_turn(self, prompt):
            yield {"type": "text_delta", "text": "checking the notes"}
            yield {"type": "tool_call", "name": "grep",
                   "args": {"pattern": "wp-json"}}

        async def aclose(self):
            return None

    await hunt_mod._collect_text_live(_Planner(), "plan", on_event,
                                      prefix="plan")
    await hunt_mod._collect_text_live(_PlannerWithTool(), "plan", on_event,
                                      prefix="plan")

    kinds = [e["type"] for e in events]
    check("plan feed: prose is emitted as plan text",
          "hunt_plan_text" in kinds, str(kinds))
    check("plan feed: the tool call is emitted",
          "hunt_plan_tool" in kinds, str(kinds))
    text = web.render(118, 36).plain
    check("plan feed: the reasoning is on screen",
          "pivots" in text, text.replace("\n", "|")[:300])
    # The tool call is summarised with its arguments, not recorded as a bare
    # name: "grep" alone says nothing about where the wave is being aimed.
    check("plan feed: the tool call is on screen with its arguments",
          "wp-json" in text, text.replace("\n", "|")[:300])
    check("plan feed: the tool call is drawn as a command",
          "$ " in text, text.replace("\n", "|")[:300])
    # And the wave view is still in its planning state: nothing has claimed a
    # roster exists yet.
    check("plan feed: the view still says planning", web.planning is True)


async def test_wave_overlay_live(wd: Path) -> None:
    """The wave view's controls work in a real app: clicks, keys and the wheel.

    ``WaveWeb`` is deliberately free of Textual, so its own suite can only prove
    the geometry and the hit map. What that cannot prove is that the widget
    routes an event to the right place — that a click's coordinates arrive as
    grid coordinates, that ``escape`` closes the drawer, that the wheel scrolls
    it. Those only exist once the overlay is mounted in a running app, which is
    what this drives.
    """
    from textual.events import Click, MouseMove
    from zimzilla.ui.app import WaveOverlay

    app = ZimZillaApp(_cfg(wd, boot_rain=False))
    async with app.run_test(size=(118, 36)) as pilot:
        app.pop_screen()  # the boot splash
        await pilot.pause()

        overlay = WaveOverlay("target.test", app.palette, wave=1, size=10)
        app.push_screen(overlay)
        await pilot.pause()
        check("wave overlay: it is the active screen", app.screen is overlay)
        # ``can_focus = False`` means the screen never wants the keyboard for
        # itself. It does *not* mean the prompt underneath is usable — pushing a
        # screen moves focus off the prompt, and this one paints over it — so
        # ``ctrl+c`` is asserted below as the way out rather than ``/stop-hunt``.
        check("wave overlay: it does not take focus itself",
              overlay.can_focus is False)

        # A planning wave: the feed has to be on screen before any roster.
        # ``_paint`` is called directly rather than waiting for the repaint
        # timer: the timer is what keeps the spinner and clock moving, and a
        # test that slept for it would be timing-dependent for no gain.
        overlay.note({"type": "hunt_plan_text",
                      "text": "pivoting to the wp-json surface"})
        overlay.note({"type": "hunt_plan_tool", "tool": "grep",
                      "args": {"pattern": "wp-json"}})
        overlay._paint()
        await pilot.pause()
        canvas = str(overlay.query_one("#wave-canvas").render())
        check("wave overlay: the planner's reasoning is on screen",
              "pivoting to the wp-json surface" in canvas, canvas[:200])
        check("wave overlay: the planner's tool call is on screen",
              "$ " in canvas and "wp-json" in canvas, canvas[:200])
        check("wave overlay: it says PLANNING, not 0 agents",
              "PLANNING" in canvas and "0 agents" not in canvas, canvas[:120])

        # The roster lands, the agents run, one reports.
        overlay.note({"type": "hunt_plan", "wave": 1, "workers": [
            {"name": f"agent-{i + 1}", "brief": f"vector {i + 1}"}
            for i in range(10)]})
        overlay.note({"type": "hunt_agent_start", "name": "agent-1",
                      "brief": "vector 1"})
        overlay.note({"type": "hunt_agent_tool", "name": "agent-1",
                      "tool": "bash", "args": {"command": "curl -s -i $U/x"}})
        overlay.note({"type": "hunt_agent_result", "name": "agent-1",
                      "tool": "bash", "args": {"command": "curl -s -i $U/x"},
                      "output": "HTTP/1.1 200 OK\nbody"})
        overlay.note({"type": "hunt_finding", "finding": {
            "wave": 1, "agent": "agent-2", "severity": "medium",
            "title": "Unauthenticated order status read",
            "asset": "http://dev.target.test/orders/1042",
            "summary": "The v3 route answers without a nonce.",
            "evidence": "GET -> 200 with billing email"}})
        overlay._paint()
        await pilot.pause()

        canvas = str(overlay.query_one("#wave-canvas").render())
        check("wave overlay: the command reaches the pane",
              "curl -s -i" in canvas, canvas[:200])
        check("wave overlay: the output reaches the pane",
              "HTTP/1.1 200 OK" in canvas, canvas[:300])
        check("wave overlay: the finding reaches the tracker",
              "MED" in canvas, canvas[:200])

        # A click on the MED lane, delivered as a real Click event. The canvas
        # fills the screen at its origin, so the event's coordinates are the
        # grid's — which is the assumption this proves.
        overlay.web.render(*overlay.size)
        lane = next(pos for pos, (kind, key) in overlay.web._hits.items()
                    if kind == "lane" and key == "medium")
        overlay.on_click(Click(None, lane[0], lane[1], 0, 0, 1, False, False,
                               False, screen_x=lane[0], screen_y=lane[1]))
        overlay._paint()
        await pilot.pause()
        check("wave overlay: clicking the lane opens the drawer",
              overlay.web.detail.kind == "findings"
              and overlay.web.detail.key == "medium",
              f"{overlay.web.detail.kind}/{overlay.web.detail.key}")
        canvas = str(overlay.query_one("#wave-canvas").render())
        check("wave overlay: the drawer's finding is on screen",
              "Unauthenticated" in canvas, canvas[:400])

        # Escape closes it. The binding is on the overlay and the overlay is the
        # active screen, so this fires without the overlay holding focus.
        await pilot.press("escape")
        await pilot.pause()
        check("wave overlay: escape closes the drawer",
              overlay.web.detail.kind == "", str(overlay.web.detail.kind))

        # A click on a pane opens that agent's run.
        overlay.web.render(*overlay.size)
        pane = next(pos for pos, (kind, _) in overlay.web._hits.items()
                    if kind == "pane" and _ == "agent-1")
        overlay.on_click(Click(None, pane[0], pane[1], 0, 0, 1, False, False,
                               False, screen_x=pane[0], screen_y=pane[1]))
        overlay._paint()
        await pilot.pause()
        check("wave overlay: clicking a pane opens the agent's run",
              overlay.web.detail.kind == "agent"
              and overlay.web.detail.key == "agent-1",
              f"{overlay.web.detail.kind}/{overlay.web.detail.key}")

        # The wheel scrolls the drawer rather than rotating the panes.
        overlay.on_mouse_scroll_down(None)
        await pilot.pause()
        check("wave overlay: the wheel scrolls the open drawer",
              overlay.web.detail.kind == "agent",
              str(overlay.web.detail.kind))
        await pilot.press("escape")
        await pilot.pause()
        check("wave overlay: escape closes the agent drawer",
              overlay.web.detail.kind == "", str(overlay.web.detail.kind))

        # The resize keys. Plain arrows move the rail, shift+arrows the drawer —
        # and the drawer only exists while it is open, so that half is checked
        # with a lane drawer open.
        before = overlay.web._layout.rail_w
        await pilot.press("right")
        await pilot.pause()
        check("wave overlay: right widens the rail",
              overlay.web._layout.rail_w > before,
              f"{before} -> {overlay.web._layout.rail_w}")
        await pilot.press("left")
        await pilot.pause()
        check("wave overlay: left narrows it back",
              overlay.web._layout.rail_w == before,
              str(overlay.web._layout.rail_w))

        overlay.web.open_findings("medium")
        overlay._paint()
        await pilot.pause()
        # The drawer is resized at a wide terminal on purpose. At 118 columns it
        # is already pinned at 37 by the room the two pane columns must keep, so
        # a wider request is clamped straight back and the keys would look
        # broken when they are in fact working.
        await pilot.resize_terminal(200, 50)
        overlay._paint()
        await pilot.pause()
        drawer_before = overlay.web._layout.drawer_w
        check("wave overlay: the drawer opens at a default width",
              drawer_before > 0, str(drawer_before))
        await pilot.press("shift+right")
        await pilot.pause()
        check("wave overlay: shift+right widens the drawer",
              overlay.web._layout.drawer_w > drawer_before,
              f"{drawer_before} -> {overlay.web._layout.drawer_w}")
        await pilot.press("shift+left")
        await pilot.pause()
        check("wave overlay: shift+left narrows the drawer back",
              overlay.web._layout.drawer_w == drawer_before,
              str(overlay.web._layout.drawer_w))
        # And the panes survive the drawer: it must never be the only thing left.
        check("wave overlay: the panes are still drawn beside the drawer",
              overlay.web._layout.cols >= 2,
              str(overlay.web._layout.cols))

        overlay.on_mouse_move(MouseMove(None, 3, 6, 0, 0, 0, False, False,
                                        False))
        await pilot.pause()
        check("wave overlay: a move event updates the hover",
              overlay.web.hover == (3, 6), str(overlay.web.hover))


async def test_wave_overlay_can_still_be_escaped(wd: Path) -> None:
    """A campaign can be stopped *gracefully* while the wave view has the screen.

    This is pinned because the obvious claim about it is wrong. ``can_focus =
    False`` is often read as "the prompt stays usable underneath", and that is
    what the docstrings used to say — but pushing a screen moves focus off the
    prompt, and the wave view paints over it at 100% width and height, so
    ``/stop-hunt`` cannot be typed while it is up. That half is asserted below
    as a fact rather than a hope.

    The consequence used to be that ``ctrl+c`` was the only way out, and that is
    a bad way out: ``interrupt`` cancels the workers outright instead of setting
    the hunt's stop flag, so the wave does not unwind and the closing report may
    never be written. So the wave view now carries its own ``ctrl+x``, which
    routes through the same ``_cmd_stop_hunt`` the typed command uses. Both
    halves are pinned here — the key that works and the keystrokes that do not —
    because a runaway campaign must always have a way out and it has to be known
    which one it is.
    """
    from zimzilla.ui.widgets import PromptInput

    app = ZimZillaApp(_cfg(wd, boot_rain=False))
    app._team_agent_factory = _hunt_factory(
        {"rosters": ['{"summary": "s", "workers": '
                     '[{"name": "p", "brief": "b", "owns": ["p"]}]}'],
         "planner_calls": 0, "prompts": [], "recon_final": "r", "final": "none"})

    async with app.run_test(size=(118, 36)) as pilot:
        app.pop_screen()
        await pilot.pause()
        prompt = app.query_one(PromptInput)
        prompt.focus()
        await pilot.pause()

        app._run_hunt("x.test")
        await pilot.pause()
        await pilot.pause()
        check("escape hatch: the campaign is in flight", app.busy is True)
        check("escape hatch: the wave view has the screen",
              type(app.screen).__name__ == "WaveOverlay",
              type(app.screen).__name__)
        overlay = app.screen

        # The key is discoverable: the prompt it replaces is covered, so a key
        # that ends the campaign is only usable if the view says so.
        canvas = overlay.web.render(118, 36).plain
        check("escape hatch: the stop key is written in the rail",
              "ctrl+x" in canvas, canvas[-400:])
        check("escape hatch: and it is not yet claiming to be stopping",
              "stopping" not in canvas)

        # The typed command does not arrive — this is the honest half.
        await pilot.press(*"/stop-hunt")
        await pilot.pause()
        check("escape hatch: /stop-hunt cannot be typed behind the wave view",
              prompt.value == "", repr(prompt.value))
        check("escape hatch: and so the stop flag is not set by typing",
              app._hunt_stop.is_set() is False)

        # ctrl+x is the graceful path, and it must set the *stop flag* — not
        # merely cancel workers, which is what ctrl+c does. Asserted against the
        # app rather than the captured overlay on purpose: a hunt replaces its
        # wave view at every wave boundary, so by the time the key lands the
        # screen object captured above may already be a previous wave's. The
        # flag is the thing that has to be right.
        await pilot.press("ctrl+x")
        check("escape hatch: ctrl+x sets the hunt's stop flag",
              app._hunt_stop.is_set() is True)
        await pilot.pause()
        check("escape hatch: the campaign stops", app.busy is False)
        check("escape hatch: and it stopped gracefully, not by cancellation",
              app._cancelled is False)


async def test_wave_overlay_stop_key_confirms_itself(wd: Path) -> None:
    """``ctrl+x`` shows that it was heard, without racing a live hunt.

    The end-to-end path is covered above; this covers the part that a live hunt
    makes untestable, because a hunt swaps its wave view at every wave boundary
    and the screen under test can be replaced mid-keypress. So the campaign here
    is stood up by hand — a pushed overlay and a non-``None`` run — which is
    exactly the precondition ``_cmd_stop_hunt`` checks before it will set the
    flag.

    The confirmation matters because the line ``_cmd_stop_hunt`` writes goes to
    the transcript, and the wave view is covering the transcript. Without the
    footer changing, the key would look inert for as long as the wave takes to
    unwind.
    """
    from zimzilla.ui.app import WaveOverlay

    app = ZimZillaApp(_cfg(wd, boot_rain=False))

    async with app.run_test(size=(118, 36)) as pilot:
        app.pop_screen()
        await pilot.pause()

        overlay = WaveOverlay("target.test", app.palette, wave=1, size=10)
        app.push_screen(overlay)
        await pilot.pause()

        before = overlay.web.render(118, 36).plain
        check("stop key: the rail advertises it", "ctrl+x" in before)
        check("stop key: and does not claim to be stopping yet",
              "stopping" not in before)

        # A campaign is live, which is what the command handler gates on.
        app._hunt_stop = asyncio.Event()
        app._hunt_run = object()
        await pilot.press("ctrl+x")
        await pilot.pause()

        check("stop key: the flag is set", app._hunt_stop.is_set() is True)
        check("stop key: the view records it", overlay.web.stopping is True)
        after = overlay.web.render(118, 36).plain
        check("stop key: the footer confirms it", "stopping" in after)
        check("stop key: the hint is replaced, not duplicated",
              "ctrl+x" not in after)

        # Pressing it twice is harmless — the wave unwinds on its own schedule.
        await pilot.press("ctrl+x")
        await pilot.pause()
        check("stop key: a second press is a no-op",
              app._hunt_stop.is_set() is True and overlay.web.stopping is True)


async def test_wave_overlay_ctrl_c_still_interrupts(wd: Path) -> None:
    """``ctrl+c`` still works from the wave view, as the blunt fallback.

    Kept separate from the graceful path above because they are different
    mechanisms with different consequences: this one cancels the workers and
    leaves ``_hunt_stop`` unset, so the wave does not unwind. It is the right
    key when the graceful path is not responding, and the wrong one to rely on.
    """
    app = ZimZillaApp(_cfg(wd, boot_rain=False))
    app._team_agent_factory = _hunt_factory(
        {"rosters": ['{"summary": "s", "workers": '
                     '[{"name": "p", "brief": "b", "owns": ["p"]}]}'],
         "planner_calls": 0, "prompts": [], "recon_final": "r", "final": "none"})

    async with app.run_test(size=(118, 36)) as pilot:
        app.pop_screen()
        await pilot.pause()
        app._run_hunt("x.test")
        await pilot.pause()
        await pilot.pause()
        check("ctrl+c: the campaign is in flight", app.busy is True)

        await pilot.press("ctrl+c")
        await pilot.pause()
        check("ctrl+c: it reaches the app through the wave view",
              app._cancelled is True)
        await pilot.pause()
        check("ctrl+c: the campaign is stopped", app.busy is False)
        check("ctrl+c: it does not set the graceful stop flag",
              app._hunt_stop.is_set() is False)


async def main() -> int:
    test_parse_findings()
    test_fallback()
    test_planner_fallback_path()
    test_brief_context_truncation()
    test_hunt_tool_panel_renders_bash()
    await test_loop_rail_hunt_render()
    test_pane()
    test_contract_reaches_the_worker()
    test_report_finding_tool()
    test_finding_from_tool()
    test_tracker_model()
    test_findings_panel()
    await test_stream_turn_watchdog()
    await test_planner_feed_reaches_the_wave_view()

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        test_reports(wd)
        test_scope_naming(wd)
        test_migrate_hunts(wd)
        test_hunt_findings_tool(wd)
        await test_campaign_notes(wd)
        await test_repeat_hunt_reuses_recon(wd)
        test_find_recon_reads_the_newest(wd)
        await test_plan_records_a_fallback(wd)
        await test_hunt_loop(wd)
        await test_contract_in_worker_prompts(wd)
        await test_live_reporting_mid_wave(wd)
        await test_live_reporting_is_per_agent(wd)
        await test_hunt_replans_from_findings(wd)
        await test_hunt_fallback_wave(wd)
        await test_brief_context(wd)
        await test_planner_retry(wd)
        await test_planner_fallback_shows_raw(wd)
        await test_hunt_result_event_carries_args(wd)
        await test_hunt_stops_midwave(wd)
        await test_hunt_summary_flag(wd)
        await test_recon_reports_live(wd)
        await test_campaign_reaches_the_main_agent(wd)
        await test_boot_migrates_hunts(wd)
        await test_recon_overlay(wd)
        await test_wave_overlay_live(wd)
        await test_wave_overlay_can_still_be_escaped(wd)
        await test_wave_overlay_stop_key_confirms_itself(wd)
        await test_wave_overlay_ctrl_c_still_interrupts(wd)
        await test_stop_hunt_reaches_the_loop(wd)
        await test_recon_overlay_fast_target(wd)
        await test_overlays_do_not_stack(wd)
        await test_transcript_shows_full_command(wd)
        await test_loop_rail_shows_hack_during_hunt(wd)
        await test_busy_gate(wd)
        await test_findings_command(wd)
        await test_bug_hunt_reuses_recon_from_the_ui(wd)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
