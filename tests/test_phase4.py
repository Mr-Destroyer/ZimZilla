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
    def __init__(self, content):
        self.content = content
        self.usage = None


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

    # ---- scope guard ------------------------------------------------------
    def scoped(path: Path):
        cfg = _cfg(wd)
        cfg._scope = Scope.load(path)
        return cfg

    scoped_file = wd / "scope.yaml"
    scoped_file.write_text("domains:\n  - example.com\n")
    blocked = tools.execute("web_fetch", {"url": "https://docs.python.org/"}, scoped(scoped_file))
    check("web_fetch blocks an out-of-scope host",
          blocked.is_error and blocked.meta.get("blocked") is True, blocked.output[:60])

    empty_file = wd / "empty.yaml"
    empty_file.write_text("domains: []\n")
    ef = tools.execute("web_fetch", {"url": "https://example.com/"}, scoped(empty_file))
    es = tools.execute("search_web", {"query": "anything"}, scoped(empty_file))
    check("empty scope blocks web_fetch", ef.is_error and ef.meta.get("blocked") is True)
    check("empty scope blocks search_web", es.is_error and es.meta.get("blocked") is True)

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


async def test_mode_gating(wd: Path) -> None:
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


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        (wd / "alpha.py").write_text("print('a')\n")
        (wd / "beta.md").write_text("# beta\n")
        await test_identity(wd)
        test_mode_tools(wd)
        test_web_tools(wd)
        test_zim_agents(wd)
        await test_mode_gating(wd)
        await test_ui(wd)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
