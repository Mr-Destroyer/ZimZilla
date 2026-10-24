"""Regression suite for `/effort` — the model's reasoning effort, per protocol.

Run:  ~/.local/share/zimzilla/.venv/bin/python tests/test_effort.py

The operator asked for a command that raises the effort of the current model.
There is no single "effort" field to raise, so what this file pins is the
translation and the honesty around it:

1. **The registry.** Three levels, cheapest first, each with both dialects —
   an ``reasoning_effort`` value for Chat Completions and a thinking budget for
   Messages.
2. **The body.** ``body()`` must produce ``{"reasoning_effort": …}`` on the
   OpenAI protocol and ``{"thinking": {"type": "enabled", "budget_tokens": …}}``
   on Anthropic, clamped strictly under the request ceiling (the API refuses a
   budget that is not below ``max_tokens``), and empty when there is no room.
3. **The request.** ``_stream_once`` really carries it in ``extra_body``, and a
   session that never ran ``/effort`` sends byte-for-byte what it did before.
4. **The refusal path.** A model without the knob drops exactly that field and
   retries, rather than failing the turn — the pre-existing mechanism, extended
   with the two new names.
5. **Persistence and wiring.** The level is remembered; the command exists in
   every surface a grep finds.

No network: the SDK stream is faked, as ``test_reasoning`` does.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import effort as E  # noqa: E402
from zimzilla import openai_compat  # noqa: E402
from zimzilla.agent import Agent  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.complete import SLASH_COMMANDS  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


# ---------------------------------------------------------------------------
# Fakes — the shape the SDK hands `_stream_once`, plus kwargs capture
# ---------------------------------------------------------------------------

class _Blk:
    def __init__(self, **k):
        self.__dict__.update(k)

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


class _Msg:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = None


class _Stream:
    def __init__(self, final, seen: dict):
        self._final = final
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        return
        yield  # pragma: no cover - an empty stream

    async def get_final_message(self):
        return self._final


class _Messages:
    def __init__(self, final, seen: dict):
        self._final = final
        self._seen = seen

    def stream(self, **kwargs):
        # Every request the loop builds lands here, which is how the body is
        # inspected without a server.
        self._seen.clear()
        self._seen.update(kwargs)
        return _Stream(self._final, self._seen)


class _Client:
    def __init__(self, final, seen: dict):
        self.messages = _Messages(final, seen)


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


def _send_sync(tmp: Path, **kw) -> dict:
    """Run one real `_stream_once` and return the kwargs it sent.

    Synchronous wrapper so the non-UI checks can stay plain functions in the
    repo's idiom; the event loop is created here, never nested inside one.
    """
    seen: dict = {}
    agent = Agent(_cfg(tmp, **kw))
    agent._client = _Client(_Msg([_Blk(type="text", text="ok")]), seen)

    async def run():
        async for _ev, _final in agent._stream_once():
            pass

    asyncio.run(run())
    return seen


def _bad_request(message: str) -> Exception:
    """A 400 in the shape the harness itself builds for a Chat Completions error.

    Built through ``openai_compat.error_for`` rather than hand-rolled, so the
    test exercises the real status→exception mapping the drop path depends on.
    """
    raw = json.dumps({"error": {"message": message}}).encode()
    return openai_compat.error_for(400, raw, {}, "http://localhost:4001/v1")


# ---------------------------------------------------------------------------
# 1 — the registry
# ---------------------------------------------------------------------------

def test_registry_shape(tmp: Path) -> None:
    check("registry: three levels, cheapest first",
          E.LEVEL_ORDER == ("low", "medium", "high"), str(E.LEVEL_ORDER))
    check("registry: every level is in the table",
          all(n in E.LEVELS for n in E.LEVEL_ORDER), str(sorted(E.LEVELS)))
    check("registry: no level outside the order",
          set(E.LEVELS) == set(E.LEVEL_ORDER), str(sorted(E.LEVELS)))
    budgets = [E.LEVELS[n].budget for n in E.LEVEL_ORDER]
    check("registry: budgets rise with the level", budgets == sorted(budgets),
          str(budgets))
    check("registry: every budget clears the API floor",
          all(b >= E.MIN_BUDGET for b in budgets), str(budgets))
    check("registry: every level names its Chat Completions value",
          all(E.LEVELS[n].wire == n for n in E.LEVEL_ORDER),
          str([E.LEVELS[n].wire for n in E.LEVEL_ORDER]))
    check("registry: every level has a blurb",
          all(E.LEVELS[n].blurb for n in E.LEVEL_ORDER))


# ---------------------------------------------------------------------------
# 2 — validation and normalisation
# ---------------------------------------------------------------------------

def test_validation(tmp: Path) -> None:
    for level in E.LEVEL_ORDER:
        ok, _ = E.validate(level)
        check(f"validate: accepts {level}", ok)
    for word in ("OFF", "none", "default", "reset", " unset "):
        ok, _ = E.validate(word)
        check(f"validate: accepts off-word {word.strip()!r}", ok)
    ok, msg = E.validate("")
    check("validate: empty is a usage error", not ok and "usage" in msg, msg)
    ok, msg = E.validate("turbo")
    check("validate: unknown level names the alternatives",
          not ok and "turbo" in msg and "high" in msg, msg)
    ok, msg = E.validate("!!")
    check("validate: punctuation-only is rejected", not ok, msg)

    check("normalise: keeps a real level", E.normalise("HIGH") == "high")
    check("normalise: off-word is None", E.normalise("off") is None)
    check("normalise: junk is None", E.normalise("wat") is None)
    check("normalise: empty is None", E.normalise("") is None)


# ---------------------------------------------------------------------------
# 3 — the body, per protocol
# ---------------------------------------------------------------------------

def test_body_per_protocol(tmp: Path) -> None:
    check("body: Chat Completions takes reasoning_effort",
          E.body("high", "openai", 32768) == {"reasoning_effort": "high"},
          str(E.body("high", "openai", 32768)))
    anthropic = E.body("high", "anthropic", 32768)
    check("body: Messages takes an enabled thinking block",
          anthropic.get("thinking", {}).get("type") == "enabled", str(anthropic))
    budget = anthropic.get("thinking", {}).get("budget_tokens", 0)
    check("body: the budget is the level's own", budget == 16384, str(budget))
    check("body: the budget stays strictly under the ceiling",
          budget < 32768, f"{budget} vs 32768")
    clamped = E.body("high", "anthropic", 8192).get("thinking", {})
    check("body: a small ceiling clamps the budget",
          clamped.get("budget_tokens") == 8192 - E.CEILING_HEADROOM, str(clamped))
    check("body: no room under the ceiling sends nothing",
          E.body("high", "anthropic", 1500) == {},
          str(E.body("high", "anthropic", 1500)))
    check("body: off sends nothing on either protocol",
          E.body(None, "openai", 32768) == {}
          and E.body("off", "anthropic", 32768) == {})
    check("body: an unknown level sends nothing",
          E.body("turbo", "openai", 32768) == {})

    text = E.describe("high", "openai", 32768)
    check("describe: names the field it will send",
          "reasoning_effort=high" in text, text)
    text = E.describe("high", "anthropic", 32768)
    check("describe: names the budget it will send", "16384" in text, text)
    text = E.describe(None, "openai", 32768)
    check("describe: off reads as nothing sent", "nothing sent" in text, text)


# ---------------------------------------------------------------------------
# 4 — the request really carries it
# ---------------------------------------------------------------------------

def test_request_carries_the_level(tmp: Path) -> None:
    seen = _send_sync(tmp, protocol="openai", reasoning_effort="high")
    extra = seen.get("extra_body") or {}
    check("request: Chat Completions carries reasoning_effort",
          extra.get("reasoning_effort") == "high", str(extra))
    check("request: the sampling knob is still sent",
          "temperature" in extra, str(extra))

    seen = _send_sync(tmp, protocol="anthropic", reasoning_effort="medium")
    extra = seen.get("extra_body") or {}
    check("request: Messages carries a thinking block",
          (extra.get("thinking") or {}).get("type") == "enabled", str(extra))
    check("request: the thinking budget matches the level",
          (extra.get("thinking") or {}).get("budget_tokens") == 8192, str(extra))

    seen = _send_sync(tmp, protocol="openai")
    extra = seen.get("extra_body") or {}
    check("request: with no level nothing new is sent",
          set(extra) == {"temperature"}, str(extra))


# ---------------------------------------------------------------------------
# 5 — a model without the knob drops it instead of failing the turn
# ---------------------------------------------------------------------------

def test_refusal_is_dropped(tmp: Path) -> None:
    exc = _bad_request("unsupported parameter: reasoning_effort")
    got = Agent._unsupported_sampling_param(exc)
    check("drop: reasoning_effort is recognised", got == "reasoning_effort", got)

    exc = _bad_request("thinking is not supported by this model")
    got = Agent._unsupported_sampling_param(exc)
    check("drop: thinking is recognised", got == "thinking", got)

    # Anthropic's own sentence when a thinking budget meets temperature=0. The
    # sampling knob has to go first, or the retry fails on the same wall.
    exc = _bad_request(
        "`temperature` may only be set to 1 when thinking is enabled")
    got = Agent._unsupported_sampling_param(exc)
    check("drop: temperature is dropped before thinking", got == "temperature",
          got)

    exc = _bad_request("model not found")
    check("drop: an unrelated 400 names no parameter",
          Agent._unsupported_sampling_param(exc) == "")


# ---------------------------------------------------------------------------
# 6 — persistence
# ---------------------------------------------------------------------------

def test_persistence(tmp: Path) -> None:
    home = tmp / "zim-home"
    E.ZIMZILLA_HOME = home
    check("persist: nothing remembered on a fresh home",
          E.read_persisted() is None)
    E.persist("high")
    check("persist: the level is written",
          E.pref_file().exists() and E.read_persisted() == "high",
          str(E.read_persisted()))
    E.persist("off")
    check("persist: off removes the file rather than writing 'off'",
          not E.pref_file().exists())
    E.pref_file().parent.mkdir(parents=True, exist_ok=True)
    E.pref_file().write_text("turbo\n", encoding="utf-8")
    check("persist: a junk file reads as no preference",
          E.read_persisted() is None)

    # from_env must prefer an explicit value over the remembered one, and fall
    # back to it only when nothing was passed.
    E.persist("low")
    cfg = Config.from_env(workdir=tmp, api_key="x",
                          base_url="http://localhost:4001")
    check("config: the remembered level is picked up",
          cfg.reasoning_effort == "low", str(cfg.reasoning_effort))
    cfg = Config.from_env(workdir=tmp, api_key="x",
                          base_url="http://localhost:4001",
                          reasoning_effort="high")
    check("config: an explicit level beats the remembered one",
          cfg.reasoning_effort == "high", str(cfg.reasoning_effort))
    cfg = Config.from_env(workdir=tmp, api_key="x",
                          base_url="http://localhost:4001",
                          reasoning_effort="turbo")
    check("config: junk never reaches the request body",
          cfg.reasoning_effort is None, str(cfg.reasoning_effort))
    E.persist(None)


# ---------------------------------------------------------------------------
# 7 — wiring: every surface a grep finds
# ---------------------------------------------------------------------------

def test_wiring(tmp: Path) -> None:
    from zimzilla.ui.app import ZimZillaApp

    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    check("wiring: dispatch has /effort",
          '"effort": lambda a: self._cmd_effort(a)' in app_src)
    check("wiring: dispatch has /tokenjuice-api-setup",
          '"tokenjuice-api-setup": lambda a: self._cmd_tokenjuice_key(a)'
          in app_src)
    check("wiring: the short spelling is accepted too",
          '"tokenjuic-api-setup": lambda a: self._cmd_tokenjuice_key(a)'
          in app_src)
    check("wiring: the help table names /effort",
          '("/effort [low|medium|high|off]"' in app_src)
    check("wiring: the help table names /tokenjuice-api-setup",
          '("/tokenjuice-api-setup [key|clear]"' in app_src)
    check("wiring: the palette names /effort",
          '("/effort", "raise or lower how hard the model thinks", "effort")'
          in app_src)
    check("wiring: the palette names /tokenjuice-api-setup",
          '"/tokenjuice-api-setup", "your own Token Juice key' in app_src)
    check("wiring: /effort answers while busy", "effort" in ZimZillaApp.BUSY_OK)
    check("wiring: /tokenjuice-api-setup answers while busy",
          "tokenjuice-api-setup" in ZimZillaApp.BUSY_OK)

    names = [n for n, _ in SLASH_COMMANDS]
    check("wiring: completion popup lists /effort", "/effort" in names)
    check("wiring: completion popup lists /tokenjuice-api-setup",
          "/tokenjuice-api-setup" in names)

    main_src = (ROOT / "zimzilla" / "__main__.py").read_text(encoding="utf-8")
    check("wiring: --effort is a launch flag", '"--effort"' in main_src)
    cfg_src = (ROOT / "zimzilla" / "config.py").read_text(encoding="utf-8")
    check("wiring: Config carries the field",
          "reasoning_effort: str | None = None" in cfg_src)


# ---------------------------------------------------------------------------
# 8 — driven through the live TUI
# ---------------------------------------------------------------------------

async def test_ui(tmp: Path) -> None:
    from zimzilla.ui.app import ZimZillaApp

    E.ZIMZILLA_HOME = tmp / "ui-home"
    cfg = _cfg(tmp, boot_rain=False)
    cfg.state_dir = tmp / "ui-state"
    app = ZimZillaApp(cfg)

    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        app._handle_command("/effort high")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("ui: /effort high sets the level",
              cfg.reasoning_effort == "high", str(cfg.reasoning_effort))
        check("ui: the confirmation names the level", "effort high" in blob,
              blob[-400:])
        check("ui: the preference was written", E.pref_file().exists())
        check("ui: the warning about unsupported models is said",
              "drops it" in blob, blob[-400:])

        app._handle_command("/effort")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("ui: bare /effort shows the menu", "REASONING EFFORT" in blob,
              blob[-400:])
        check("ui: the menu marks the current level", "◉" in blob)

        app._handle_command("/effort off")
        await pilot.pause()
        check("ui: /effort off clears the level", cfg.reasoning_effort is None,
              str(cfg.reasoning_effort))
        check("ui: off is forgotten on disk, not remembered",
              not E.pref_file().exists())

        app._handle_command("/effort turbo")
        await pilot.pause()
        blob = "\n".join(s.text for s in app.query_one("#transcript").lines)
        check("ui: an unknown level is refused by name",
              "unknown effort: turbo" in blob, blob[-400:])
        check("ui: a refused level changes nothing",
              cfg.reasoning_effort is None, str(cfg.reasoning_effort))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_registry_shape(tmp)
        test_validation(tmp)
        test_body_per_protocol(tmp)
        test_request_carries_the_level(tmp)
        test_refusal_is_dropped(tmp)
        test_persistence(tmp)
        test_wiring(tmp)
        # Only the UI check needs a loop of its own; the rest stay plain
        # functions, which is what lets them call asyncio.run themselves.
        asyncio.run(test_ui(tmp))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
