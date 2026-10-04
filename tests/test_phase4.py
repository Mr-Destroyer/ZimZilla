"""Phase-4 regression suite: identity, modes, zim/AGENTS.md, completion popup, @mentions.

Run:  ./.venv/bin/python tests/test_phase4.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rich.text import Text  # noqa: E402

from zimzilla import tools, websearch  # noqa: E402
from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import Config, MODES  # noqa: E402
from zimzilla.scope import Scope  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402
from zimzilla.ui.complete import CompletionPopup  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x", base_url="http://localhost:4001", **kw)


# ---- stub blocks for the mode-gating test ---------------------------------
class _Blk:
    def __init__(self, **k):
        self.__dict__.update(k)


class _Msg:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage


def _stub_agent(mode: str, workdir: Path):
    """An agent whose model always asks for one bash call, then finishes."""
    calls: list[str] = []

    async def handler(name, args, preview):
        calls.append(name)
        return "yes"

    agent = Agent(_cfg(workdir, mode=mode), permission_handler=handler)
    state = {"n": 0}

    async def fake_stream():
        state["n"] += 1
        if state["n"] == 1:
            yield ({"type": "text_delta", "text": "running"}, None)
            yield (None, _Msg([
                _Blk(type="text", text="running"),
                _Blk(type="tool_use", id="t1", name="bash",
                     input={"command": "echo ZIMMODE_OK"}),
            ]))
            return
        yield (None, _Msg([_Blk(type="text", text="done")]))

    agent._stream_once = fake_stream
    return agent, calls


async def test_identity(wd: Path) -> None:
    sp = Agent(_cfg(wd)).system_prompt()
    check("identity names ZimZilla", "ZimZilla" in sp and "ZIM (Mr-Destroyer)" in sp)
    check("identity disclaims other products", 'NOT "Claude"' in sp)

    dsp = Agent(_cfg(wd, mode="danger")).system_prompt()
    check("danger mode carries its doctrine",
          "MODE: danger" in dsp and "their word" in dsp and "is law" in dsp)
    check("danger mode refuses the mode/willingness dodge",
          "no separate layer of personal willingness" in dsp
          and "do not restate your limits as a reply to an instruction" in dsp)
    check("danger mode keeps its two mechanical bounds",
          "scope guard" in dsp and "harness itself refusing" in dsp)
    check("danger keeps the identity", "ZimZilla" in dsp)


def test_mode_tools(wd: Path) -> None:
    def names(mode):
        return {t["name"] for t in Agent(_cfg(wd, mode=mode)).tool_schemas()}

    check("plan mode hides write tools", not ({"bash", "write_file", "edit_file"} & names("plan")),
          str(sorted(names("plan"))))
    check("plan mode keeps read tools", {"read_file", "glob", "grep"} <= names("plan"))
    check("edits mode hides bash only", "bash" not in names("edits") and "write_file" in names("edits"))
    check("zim mode exposes everything", {"bash", "write_file", "edit_file"} <= names("zim"))
    check("danger mode exposes everything", {"bash", "write_file", "edit_file"} <= names("danger"))
    check("all modes registered",
          set(MODES) == {"auto", "edits", "plan", "zim", "danger"})
    check("zim/danger are the armed (loud) modes",
          {m for m, s in MODES.items() if s.get("loud")} == {"zim", "danger"})
    check("auto/danger are full-auto", all(
        {"bash", "write_file", "edit_file"} <= MODES[m]["auto"] for m in ("auto", "danger")))
    check("web tools are available in every mode",
          all({"search_web", "web_fetch"} <= names(m) for m in MODES))
    check("web tools are ungated (scope governs them, not the prompt)",
          not tools.needs_permission("search_web")
          and not tools.needs_permission("web_fetch"))
    # LiteLLM rewrites a tool literally named "web_search" into OpenAI's built-in
    # web_search_preview, which the upstream adapter rejects with a 400. Any
    # other name passes through as a normal function tool. Verified against the
    # live proxy; this assertion exists so the name is never changed back.
    check("no tool uses a name the proxy reserves",
          "web_search" not in set(tools.TOOL_NAMES), str(tools.TOOL_NAMES))


def test_web_tools(wd: Path) -> None:
    # ---- parsing, offline -------------------------------------------------
    page = (
        "<table>"
        "<tr><td><a rel=\"nofollow\" href=\"https://docs.python.org/3/library/asyncio.html\""
        " class='result-link'>asyncio &mdash; Asynchronous I/O</a></td></tr>"
        "<tr><td class='result-snippet'>asyncio is a library to write "
        "<b>concurrent</b> code.</td></tr>"
        "<tr><td><a href=\"//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2F\""
        " class='result-link'>Async IO in Python</a></td></tr>"
        "</table>"
    )
    parsed = websearch.parse_results(page)
    check("search_web parses titles and urls",
          [r.url for r in parsed] == ["https://docs.python.org/3/library/asyncio.html",
                                      "https://realpython.com/"],
          str([r.url for r in parsed]))
    check("search_web unwraps the ddg redirect",
          parsed[1].url == "https://realpython.com/", parsed[1].url)
    check("search_web pairs snippets to results",
          parsed[0].snippet.startswith("asyncio is a library"), parsed[0].snippet)

    check("search_web ignores a page with no results",
          websearch.parse_results("<html><body>nothing here</body></html>") == [])

    # ---- html_to_text -----------------------------------------------------
    doc = (
        "<html><body><nav><a href='/'>Home</a></nav>"
        "<main><h1>Real Title</h1><p>" + ("Body prose. " * 30) + "</p></main>"
        "<footer>Copyright 2026</footer></body></html>"
    )
    text = websearch.html_to_text(doc)
    check("html_to_text keeps the article", "Real Title" in text and "Body prose" in text)
    check("html_to_text drops nav and footer",
          "Copyright" not in text and "Home" not in text)
    check("html_to_text drops script and style",
          "color:red" not in websearch.html_to_text(
              "<style>p{color:red}</style><p>kept</p>") )

    # ---- scope guard (deny-only) ------------------------------------------
    # In a scratch dir, so these files do not appear in the @-mention popup
    # test that runs later against the shared workdir.
    sd = Path(tempfile.mkdtemp())

    def scoped(allow: Path | None = None, deny: Path | None = None):
        cfg = _cfg(sd)
        cfg._scope = Scope.load(allow, deny)
        return cfg

    deny_file = sd / "out-of-scope.yaml"
    deny_file.write_text("domain:\n  - evil.example.com\n")
    allow_file = sd / "allow.yaml"
    allow_file.write_text("domain:\n  - example.com\n")

    blocked = tools.execute("web_fetch", {"url": "https://evil.example.com/x"},
                            scoped(deny=deny_file))
    check("web_fetch blocks a deny-listed host",
          blocked.is_error and blocked.meta.get("blocked") is True, blocked.output[:60])

    # The allow-list is a declaration, not a fence: an unlisted host is fine.
    unlisted = tools.execute("web_fetch", {"url": "https://docs.python.org/"},
                             scoped(allow=allow_file))
    check("allow.yaml does not block an unlisted host",
          not unlisted.is_error or unlisted.meta.get("blocked") is not True,
          unlisted.output[:60])

    # Deny beats allow.
    both = tools.execute("web_fetch", {"url": "https://evil.example.com/"},
                         scoped(allow=allow_file, deny=deny_file))
    check("deny beats allow", both.is_error and both.meta.get("blocked") is True)

    # search_web names no host, so scope never blocks it — even with a deny file.
    es = tools.execute("search_web", {"query": "anything"}, scoped(deny=deny_file))
    check("search_web is never blocked by scope",
          not (es.is_error and es.meta.get("blocked") is True), es.output[:60])

    # ---- argument validation (never raises) -------------------------------
    check("search_web rejects an empty query",
          tools.execute("search_web", {"query": "  "}, _cfg(wd)).is_error)
    check("web_fetch rejects an empty url",
          tools.execute("web_fetch", {"url": ""}, _cfg(wd)).is_error)

    # ---- transcript labels ------------------------------------------------
    check("search_web summarises to the query",
          tools.summarise_call("search_web", {"query": "asyncio gather"}, None)
          == "asyncio gather")


def test_zim_agents(wd: Path) -> None:
    agents = wd / "AGENTS.md"
    agents.write_text("# OPERATOR DOCTRINE\nOBEY THE ZIM.\n")
    sp = Agent(_cfg(wd, mode="zim", agents_path=agents)).system_prompt()
    check("zim mode loads AGENTS.md", "OBEY THE ZIM" in sp)
    check("zim mode without file falls back", "ZimZilla" in Agent(_cfg(wd, mode="zim")).system_prompt())


def test_scope_semantics(wd: Path) -> None:
    """The allow/deny split: what arms, what blocks, and what neither does.

    Uses its own directory: these files are named allow.yaml / out-of-scope.yaml,
    which would otherwise show up in the @-mention popup test and displace the
    fixtures it expects.
    """
    with tempfile.TemporaryDirectory() as sd:
        wd = Path(sd)
        _scope_semantics(wd)


def _scope_semantics(wd: Path) -> None:
    allow_f = wd / "allow.yaml"
    allow_f.write_text("domain:\n  - example.com\n  - app.example.org\nip:\n  - 203.0.113.10\n")
    deny_f = wd / "out-of-scope.yaml"
    deny_f.write_text("domain:\n  - evil.example.com\ncidr:\n  - 198.51.100.0/24\n")

    # ---- arming -----------------------------------------------------------
    check("nothing loaded: not armed", not Scope.load(None, None).armed)
    check("deny alone does NOT arm", not Scope.load(None, deny_f).armed)
    check("allow.yaml arms the session", Scope.load(allow_f, None).armed)
    check("armed+deny: still armed", Scope.load(allow_f, deny_f).armed)
    check("nothing loaded: nothing loaded", not Scope.load(None, None).loaded)
    check("deny alone counts as loaded", Scope.load(None, deny_f).loaded)

    # ---- membership -------------------------------------------------------
    s = Scope.load(allow_f, deny_f)
    check("allow contains a listed domain", s.in_allow("example.com")[0])
    check("allow contains a subdomain", s.in_allow("api.example.com")[0])
    check("allow does not contain an unlisted host", not s.in_allow("other.net")[0])
    check("in_allow is False without an allow file", not Scope.load(None, deny_f).in_allow("example.com")[0])

    # ---- deny enforcement (the only thing that blocks) --------------------
    check("allows() passes an unlisted host", s.allows("docs.python.org")[0])
    check("allows() blocks a deny-listed domain", not s.allows("evil.example.com")[0])
    check("allows() blocks a deny-listed subdomain", not s.allows("a.b.evil.example.com")[0])
    check("allows() blocks an IP inside a denied CIDR", not s.allows("198.51.100.7")[0])
    check("allows() passes an IP outside a denied CIDR", s.allows("198.51.101.1")[0])
    check("deny beats allow", not s.allows("evil.example.com")[0])

    # ---- command scanning is deny-only ------------------------------------
    check("command to a denied host is refused",
          bool(s.check_command("curl https://evil.example.com/x")))
    check("command to an allowed host is NOT refused",
          not s.check_command("curl https://example.com/x"))
    check("command to an unlisted host is NOT refused",
          not s.check_command("curl https://docs.python.org/"))
    check("command substitution to a network tool is refused",
          bool(s.check_command("curl https://$(cat target.txt)/")))
    check("backtick substitution is refused",
          bool(s.check_command("curl `cat target.txt`")))
    check("variable-host substitution is refused",
          bool(s.check_command("curl http://$TARGET/x")))
    # A literal target alongside a substitution is still checked normally, so
    # this is NOT refused for being unresolvable — but it IS refused, because
    # the literal itself is deny-listed. (Proves the guard did not just
    # fail-open on the substitution.)
    check("substitution does not excuse a deny-listed literal",
          bool(s.check_command("curl https://evil.example.com/$(cat t)")))
    check("no deny file means no command is ever refused",
          not Scope.load(allow_f, None).check_command("curl https://evil.example.com/"))

    # ---- singular/plural/legacy keys --------------------------------------
    singular = wd / "singular.yaml"
    singular.write_text("domain:\n  - one.example.com\nip:\n  - 10.0.0.1\ncidr:\n  - 10.1.0.0/16\n")
    ss = Scope.load(None, singular)
    check("singular keys parse", len(ss.deny) == 3, ss.deny.describe())
    plural = wd / "plural.yaml"
    plural.write_text("domains:\n  - two.example.com\nips:\n  - 10.0.0.2\ncidrs:\n  - 10.2.0.0/16\n")
    check("plural keys parse", len(Scope.load(None, plural).deny) == 3)
    legacy = wd / "legacy.yaml"
    legacy.write_text("in_scope:\n  - three.example.com\nhosts:\n  - 10.0.0.3\nnetworks:\n  - 10.3.0.0/16\n")
    check("legacy keys still parse", len(Scope.load(None, legacy).deny) == 3)

    # ---- presentation -----------------------------------------------------
    check("badge reports armed+deny", s.badge() == "ARMED + deny 2", s.badge())
    check("badge reports armed alone", Scope.load(allow_f, None).badge() == "ARMED")
    check("badge reports deny alone", Scope.load(None, deny_f).badge() == "deny 2")
    check("badge reports off", Scope.load(None, None).badge() == "off")

    # ---- the agent's prompt tells the truth about both files --------------
    prompt = Agent(_cfg(wd, allow_path=allow_f, deny_path=deny_f)).system_prompt()
    check("prompt says the session is armed", "armed" in prompt.lower())
    check("prompt says the allow-list is not a fence",
          "not a fence" in prompt or "does not restrict" in prompt)
    check("prompt names the deny list as hard-blocked", "never to be touched" in prompt)
    unarmed = Agent(_cfg(wd)).system_prompt()
    check("prompt says nothing is loaded when nothing is",
          "No scope files are loaded" in unarmed)


async def test_mode_gating(wd: Path) -> None:
    # No shipped mode leaves bash merely gated: `auto`/`zim`/`danger` list it in
    # their `auto` set, `edits`/`plan` deny it outright. So `gated` is False
    # everywhere, and the observable difference is whether bash RUNS.
    expected = {"auto": (False, True), "edits": (False, False),
                "plan": (False, False), "zim": (False, True),
                "danger": (False, True)}
    for mode, (exp_gated, exp_ran) in expected.items():
        agent, calls = _stub_agent(mode, wd)
        events = [ev async for ev in agent.run_turn("do it")]
        results = [e for e in events if e["type"] == "tool_result"]
        out = results[0]["output"] if results else ""
        gated = any(e.get("gated") for e in events if e["type"] == "tool_call")
        ran = "ZIMMODE_OK" in out
        check(f"{mode}: bash gated={exp_gated}", gated == exp_gated)
        check(f"{mode}: bash ran={exp_ran}", ran == exp_ran)

    # The ordering constraint: arming bypasses the PERMISSION GATE only. The
    # mode-deny branch runs first, so allow.yaml must NOT re-enable bash in
    # plan or edits mode — those modes withhold the tool from the model.
    allow_f = Path(tempfile.mkdtemp()) / "allow.yaml"
    allow_f.write_text("domain:\n  - example.com\n")
    for mode in ("plan", "edits"):
        agent, calls = _stub_agent(mode, wd)
        agent.cfg.allow_path = allow_f
        agent.scope = Scope.load(allow_f, None)
        agent.cfg._scope = agent.scope
        events = [ev async for ev in agent.run_turn("do it")]
        results = [e for e in events if e["type"] == "tool_result"]
        out = results[0]["output"] if results else ""
        check(f"{mode}: armed does NOT re-enable denied bash", "ZIMMODE_OK" not in out,
              out[:50])
        check(f"{mode}: armed bash is still mode-denied",
              bool(results) and results[0].get("meta", {}).get("mode_denied") is True)


async def test_ui(wd: Path) -> None:
    app = ZimZillaApp(_cfg(wd, boot_rain=False))
    async with app.run_test(size=(110, 34)) as pilot:
        app.pop_screen()
        await pilot.pause()
        inp = app.query_one("#input")
        comp = app.query_one("#complete", CompletionPopup)
        inp.focus()  # _boot_done does this in a real run; we popped boot manually
        await pilot.pause()

        inp.value = "/"
        await pilot.pause()
        check("slash popup opens on '/'", comp.has_class("visible") and comp.kind == "slash",
              f"{len(comp.items)} items")

        inp.value = "@al"
        await pilot.pause()
        check("@ popup lists matching files",
              comp.has_class("visible") and comp.kind == "file"
              and any("alpha.py" in v for v, _ in comp.items))
        check("@ accept inserts the path", (comp.accept("@al") or "").endswith("alpha.py "))

        # real keystrokes: /mode zim submits through the popup layer
        inp.value = ""
        for ch in "/mode zim":
            await pilot.press(ch)
            await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        check("typing '/mode zim' + Enter arms zim mode",
              app.cfg.mode == "zim" and app.agent.cfg.mode == "zim")

        app._handle_command("/mode auto")
        await pilot.pause()
        check("/mode auto reverts", app.cfg.mode == "auto")
        app._handle_command("/mode danger")
        await pilot.pause()
        check("/mode danger arms", app.cfg.mode == "danger"
              and app.agent.cfg.mode == "danger")
        app._handle_command("/mode auto")
        await pilot.pause()
        check("/mode auto reverts from danger", app.cfg.mode == "auto")
        app._handle_command("/mode nonsense")
        await pilot.pause()
        check("/mode rejects unknown name", app.cfg.mode == "auto")

        expanded = app._expand_at_refs("look at @alpha.py please")
        check("@ mention attaches file contents",
              "[referenced files]" in expanded and "print('a')" in expanded)

        app._handle_command("/help")
        await pilot.pause()
        shot = app.export_screenshot()
        check("/help documents modes and mentions", "MODES" in shot and "MENTIONS" in shot)


class _Usage:
    """A usage block, as the SDK's message objects carry one."""

    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


def _stub_agent_usage(workdir: Path):
    """An agent that makes one bash call and one read, reporting usage.

    The mode-gating stub above yields no `usage` block, so the telemetry gauge
    and waveform would have nothing to move on. This one reports real token
    counts, so the rails can be driven end to end.
    """
    agent = Agent(_cfg(workdir), permission_handler=lambda *a: asyncio.sleep(0, result="yes"))
    state = {"n": 0}

    async def fake_stream():
        state["n"] += 1
        if state["n"] == 1:
            yield ({"type": "text_delta", "text": "working"}, None)
            yield (None, _Msg([
                _Blk(type="text", text="working"),
                _Blk(type="tool_use", id="t1", name="bash",
                     input={"command": "echo RAIL_OK"}),
                _Blk(type="tool_use", id="t2", name="read_file",
                     input={"path": "alpha.py"}),
            ], _Usage(51_200, 120)))
            return
        yield (None, _Msg([_Blk(type="text", text="done")], _Usage(52_500, 40)))

    agent._stream_once = fake_stream
    return agent


async def test_ui_rails(wd: Path) -> None:
    """The Mission Control rails: mount, cycle, gauge, card, palette."""
    from zimzilla.ui.palette_cmd import CommandPalette, PaletteEntry, fuzzy, rank
    from zimzilla.ui.rails import (CallStrip, ContextGauge, FileRiver, LoopRail,
                                   TelemetryRail, Waveform)
    from zimzilla.ui.widgets import ChatPane, HeaderBar

    # ---- fuzzy matching (pure, no app needed) -----------------------------
    check("fuzzy: subsequence matches", fuzzy("md", "/model") is not None)
    check("fuzzy: non-subsequence rejected", fuzzy("zz", "/model") is None)
    check("fuzzy: empty query matches everything", fuzzy("", "/model") == (0, []))
    ents = [
        PaletteEntry("a", "/model", "switch model", "command"),
        PaletteEntry("b", "/mode", "switch mode", "command"),
        PaletteEntry("c", "/zim-logfare", "upstream", "command"),
    ]
    check("fuzzy: prefix ranks above scattered",
          [e.title for e, _ in rank(ents, "md")][0] == "/model")
    check("fuzzy: filters non-matches out", len(rank(ents, "mode")) <= len(ents))
    check("fuzzy: hint is searched too",
          any(e.title == "/model" for e, _ in rank(ents, "switch")))

    app = ZimZillaApp(_cfg(wd, boot_rain=False))
    app.agent = _stub_agent_usage(wd)
    async with app.run_test(size=(118, 34)) as pilot:
        app.pop_screen()
        await pilot.pause()
        app.query_one("#input").focus()
        await pilot.pause()

        # ---- the three rails are mounted and visible ----------------------
        loop = app.query_one(LoopRail)
        tele = app.query_one(TelemetryRail)
        check("rails: loop rail mounted", loop.display and loop.region.width > 0)
        check("rails: telemetry rail mounted", tele.display and tele.region.width > 0)
        check("rails: loop starts idle", loop.stage == "idle")

        # ---- a turn drives the whole loop --------------------------------
        app._run_turn("go")
        for _ in range(50):
            await pilot.pause()
        await asyncio.sleep(0.25)
        await pilot.pause()

        check("rails: loop returned to idle", loop.stage == "idle")
        check("rails: loop trail records the cycle",
              {"thinking", "calling", "observing"} <= set(loop.trail), str(list(loop.trail)))
        check("rails: turn counter advanced", loop.turn == 1)

        gauge = tele.query_one(ContextGauge)
        check("rails: gauge tracks the last call's prompt size", gauge.used == 52_500,
              f"{gauge.used}")
        check("rails: gauge ratio is the prompt share of the window",
              abs(gauge.ratio - 52_500 / 128_000) < 1e-6, f"{gauge.ratio:.3f}")

        calls = tele.query_one(CallStrip)
        check("rails: every tool call is logged", len(calls.calls) == 2, str(list(calls.calls)))
        check("rails: calls are marked done, not still running",
              all(o != "run" for _, o in calls.calls), str(list(calls.calls)))
        check("rails: bash and read are both recorded",
              {n for n, _ in calls.calls} == {"bash", "read_file"})

        river = tele.query_one(FileRiver)
        check("rails: file river tracks the touched file", "alpha.py" in river.touched,
              str(dict(river.touched)))
        check("rails: bash does not appear in the file river",
              not any("bash" in k for k in river.touched))

        chat = app.query_one(ChatPane)
        check("rails: the live card is flushed after the turn", not chat.card_active)

        # ---- the gauge escalates at its thresholds -----------------------
        gauge.set(128_000)
        check("rails: gauge saturates at 100%", gauge.ratio == 1.0)
        gauge.set(200_000)
        check("rails: gauge never exceeds 100%", gauge.ratio == 1.0)
        gauge.set(0)

        # ---- the live card lifecycle -------------------------------------
        chat.card_begin("bash", {"command": "sleep 1"})
        check("rails: card_begin opens the slot", chat.card_active)
        check("rails: card is visible while running",
              not app.query_one("#card").has_class("hidden"))
        chat.card_finish()
        check("rails: card_finish closes the slot", not chat.card_active)
        check("rails: card is hidden when closed",
              app.query_one("#card").has_class("hidden"))

        # ---- Ctrl+K opens the palette ------------------------------------
        await pilot.press("ctrl+k")
        await pilot.pause()
        check("palette: Ctrl+K opens it", isinstance(app.screen, CommandPalette))
        pal = app.screen
        check("palette: lists commands and modes", len(pal.results) > 5,
              f"{len(pal.results)} entries")
        await pilot.press("m", "o", "d")
        await pilot.pause()
        check("palette: typing narrows the list",
              len(pal.results) < len(pal.entries), f"{len(pal.results)} of {len(pal.entries)}")
        check("palette: the query reached the field",
              pal.query_one("#pal-query").value == "mod")
        await pilot.press("escape")
        await pilot.pause()
        check("palette: escape dismisses it", not isinstance(app.screen, CommandPalette))

        # ---- the waveform names its own signal ----------------------------
        wave = tele.query_one(Waveform)
        wave.push(180.0)
        check("rails: the waveform keeps the newest sample", wave.latest == 180.0)
        check("rails: the waveform caption carries the rate",
              "tok/s" in str(wave.render()) and "180.0" in str(wave.render()),
              str(wave.render()).replace("\n", " | "))

        # ---- narrow terminals drop the rails ------------------------------
        await pilot.resize_terminal(92, 34)
        await pilot.pause()
        check("narrow: the screen carries the narrow class",
              app.screen.has_class("narrow"))
        check("narrow: the loop rail is hidden", not loop.display)
        check("narrow: the telemetry rail is hidden", not tele.display)
        check("narrow: the transcript is still visible",
              app.query_one(ChatPane).display)

        # The header sheds cosmetics before facts: the scope status and the
        # sandbox indicator must survive a narrow bar even though the cwd and
        # theme do not. It is measured against the row width, not a fixed
        # threshold, so a long cwd sheds itself rather than silently eating
        # the scope status.
        header = app.query_one(HeaderBar)
        htxt = str(header.render())
        check("narrow: the scope status survives", "scope" in htxt, htxt)
        check("narrow: the sandbox indicator survives", "sandbox" in htxt, htxt)
        check("narrow: the theme is shed", "theme:" not in htxt, htxt)
        check("narrow: the header still fits its row",
              Text(htxt).cell_len <= header.size.width,
              f"{Text(htxt).cell_len} > {header.size.width}")

        await pilot.resize_terminal(140, 34)
        await pilot.pause()
        check("wide: the rails come back", loop.display and tele.display)
        check("wide: the theme returns", "theme:" in str(header.render()),
              str(header.render()))
        check("wide: the cwd returns too", str(wd) in str(header.render()))

        # A cwd long enough to overflow must shed *itself*, never the scope.
        header.cwd = "~/" + "very-long-project-name/" * 4
        header.refresh_content()
        await pilot.pause()
        htxt = str(header.render())
        check("wide: an over-long cwd does not push the scope off",
              "scope" in htxt and "sandbox" in htxt
              and Text(htxt).cell_len <= header.size.width,
              f"{Text(htxt).cell_len} > {header.size.width}")


async def test_termbg(wd: Path) -> None:
    """The terminal's own background, adopted so the padding matches.

    A terminal paints its padding in its own background colour and a TUI cannot
    paint outside the text grid, so a hardcoded background leaves a frame of the
    wrong colour around the whole interface. The app asks over OSC 11 instead.
    """
    from zimzilla.termbg import detect_background, is_dark, parse_background
    from zimzilla.theme import PALETTES, get_palette
    from zimzilla.ui.app import ZimZillaApp

    # ---- parsing: the terminal's reply, in every form they send it ---------
    check("termbg: kitty's 16-bit reply parses",
          parse_background(b"\x1b]11;rgb:1010/1313/1515\x1b\\") == "#101315",
          repr(parse_background(b"\x1b]11;rgb:1010/1313/1515\x1b\\")))
    check("termbg: the BEL terminator works too",
          parse_background(b"\x1b]11;rgb:0000/0000/0000\x07") == "#000000")
    check("termbg: an 8-bit reply parses",
          parse_background(b"\x1b]11;rgb:ff/00/00\x1b\\") == "#ff0000")
    check("termbg: the #rrggbb form parses",
          parse_background(b"\x1b]11;#1e1e2e\x1b\\") == "#1e1e2e")
    check("termbg: a reply with no colour is refused",
          parse_background(b"garbage") is None and parse_background(b"") is None)

    # A light terminal must not be adopted: these palettes are neon-on-black,
    # and bright green on white is unreadable.
    check("termbg: dark is accepted, light is refused",
          is_dark("#101315") and is_dark("#000000") and not is_dark("#ffffff"),
          f"#101315={is_dark('#101315')} #ffffff={is_dark('#ffffff')}")
    check("termbg: mid-grey decides the right way at the boundary",
          is_dark("#7f7f7f") and not is_dark("#808080"))

    # No tty here (the suite runs piped), so detection must decline cleanly
    # rather than raise — that is the fallback path every non-answering
    # terminal takes.
    check("termbg: detection declines without a tty",
          detect_background() is None, repr(detect_background()))

    # ---- the palette carries it through ------------------------------------
    check("theme: get_palette adopts the terminal background",
          get_palette("green", "#101315").bg == "#101315")
    check("theme: get_palette keeps black with no detection",
          get_palette("green").bg == "#000000"
          and get_palette("green", None).bg == "#000000")
    check("theme: the named palettes are untouched",
          PALETTES["green"].bg == "#000000" and PALETTES["amber"].bg == "#000000")

    # ---- and it reaches the compositor, corners included -------------------
    app = ZimZillaApp(_cfg(wd, boot_rain=False), term_bg="#101315")
    async with app.run_test(size=(100, 30)) as pilot:
        app.pop_screen()
        await pilot.pause()
        check("termbg: the app adopts it", app.palette.bg == "#101315")

        def row_bgs(strip):
            out = []
            for seg in strip._segments:
                bg = seg.style.bgcolor if seg.style else None
                out.extend([bg.triplet if bg is not None else None] * len(seg.text))
            return out

        strips = app.screen._compositor.render_strips()
        painted = {c for s in strips for c in row_bgs(s)}
        want = (16, 19, 21)  # #101315
        check("termbg: every cell is painted in it, edge to edge",
              painted == {want}, f"{sorted(painted, key=str)}")
        # The corners are the whole point: that is where the terminal's own
        # rounding curves the padding into view.
        check("termbg: the outer corners are painted too",
              row_bgs(strips[0])[0] == want and row_bgs(strips[-1])[-1] == want,
              f"TL={row_bgs(strips[0])[0]} BR={row_bgs(strips[-1])[-1]}")

        # Switching palette must not drop back to black.
        for name in ("amber", "cyan", "green"):
            app._cmd_theme([name])
            await pilot.pause()
            check(f"termbg: /theme {name} keeps the terminal background",
                  app.palette.bg == "#101315", app.palette.bg)


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        (wd / "alpha.py").write_text("print('a')\n")
        (wd / "beta.md").write_text("# beta\n")
        await test_identity(wd)
        test_mode_tools(wd)
        test_web_tools(wd)
        test_zim_agents(wd)
        test_scope_semantics(wd)
        await test_mode_gating(wd)
        await test_ui(wd)
        await test_ui_rails(wd)
        await test_termbg(wd)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
