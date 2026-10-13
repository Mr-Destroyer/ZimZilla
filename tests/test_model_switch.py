"""`/model` must police against the ACTIVE gateway's catalog, not Logfare's.

Run:  python tests/test_model_switch.py   (from an activated venv)

This is the regression for a bug that only showed up once a session was actually
pointed at OpenRouter and asked to switch model:

    ⚠ dots-studio/dots-3-note-preview:free is not served by openrouter —
      see /model for the list

…for a model that was the first row of `/models openrouter`, a second earlier.
The switch was refused, the session stayed on the Logfare default name, and the
next turn was sent to OpenRouter carrying `deepseek-v4.1-flash` — which that
gateway does not serve, so it answered 402 and the operator saw a billing error
for a model they had never chosen.

**The mechanism, because it is the part worth pinning.** Every hosted gateway's
catalog is *fetched*, not declared, so on a cold cache ``sources.models_for``
returns ``None`` and ``_cmd_model`` falls back to ``KNOWN_MODELS`` — which is
Logfare's list. A name not in *that* list is then rejected as "not served by
<gateway>". The fallback is only safe when the active source has no catalog of
its own; against a gateway it is a list from the wrong provider, and a wrong
list is worse than no list, because it refuses things that work.

**Why this test covers three gateways and not one.** The original warm-up branch
was written for TokenHarbour alone — ``if active == "tokenharbour"``. BeatAPI and
OpenRouter were added later and silently inherited the broken path, because the
guard named one gateway rather than the *class* of gateway that needs it. A test
that pinned only OpenRouter would let the next provider repeat it. So all three
are driven here, through the real app, and the fourth case asserts that a
non-gateway source is left alone.

No network: each gateway's ``fetch_models`` is stubbed.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla.config import Config  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _transcript(app) -> str:
    return "\n".join(s.text for s in app.query_one("#transcript").lines)


class _Model:
    """The one attribute the warm-up reads off a fetched catalog entry."""

    def __init__(self, mid: str):
        self.id = mid


def _cfg(workdir: Path, base_url: str) -> Config:
    cfg = Config.from_env(workdir=workdir, api_key="x",
                          base_url=base_url, boot_rain=False)
    cfg.state_dir = workdir / "ui-state"
    return cfg


async def _switch(app, pilot, name: str) -> None:
    """Run /model <name> and let the warm-up's work settle."""
    app._handle_command(f"/model {name}")
    for _ in range(200):
        await asyncio.sleep(0.02)
        await pilot.pause()
        if app.cfg.model == name or "is not served by" in _transcript(app):
            return
    await asyncio.sleep(0.2)
    await pilot.pause()


async def _case_gateway(wd: Path, key: str, base_url: str, model: str) -> None:
    """A gateway's own model must be switchable, with a cold cache.

    The cache is cold by construction — nothing has fetched for this process's
    gateway module in this temp home — which is the state that triggered the bug.
    """
    from zimzilla import beatapi as beat_mod
    from zimzilla import openrouter as or_mod
    from zimzilla import tokenharbour as th_mod

    mod = {"beatapi": beat_mod, "openrouter": or_mod,
           "tokenharbour": th_mod}[key]

    cfg = _cfg(wd, base_url)
    app = ZimZillaApp(cfg)
    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        # A cold cache is the precondition, so assert it rather than assume it.
        check(f"{key}: the catalog really is cold", mod.cached_ids() is None,
              str(mod.cached_ids()))

        # fetch_models is what the warm-up calls; give it the gateway's own list.
        fetched = ([_Model(model), _Model(f"{key}-other")], "")
        with mock.patch.object(mod, "fetch_models", lambda *a, **k: fetched):
            # load_credentials is patched so the case does not depend on whether
            # a real key happens to exist on this machine.
            with mock.patch.object(mod, "load_credentials",
                                   lambda: ("sk-test", "", "test")):
                await _switch(app, pilot, model)

        blob = _transcript(app)
        check(f"{key}: the gateway's own model was accepted",
              app.cfg.model == model, f"model is {app.cfg.model!r}")
        check(f"{key}: it was not refused as unserved",
              "is not served by" not in blob, blob[-300:])
        check(f"{key}: the switch was confirmed",
              f"model switched to {model}" in blob, blob[-300:])


async def test_a_gateway_model_is_not_policed_against_logfare(wd: Path) -> None:
    await _case_gateway(wd / "beat", "beatapi",
                        "https://api.beatapi.io", "deepseek-v3.2-free")
    await _case_gateway(wd / "or", "openrouter",
                        "https://openrouter.ai/api",
                        "dots-studio/dots-3-note-preview:free")
    await _case_gateway(wd / "th", "tokenharbour",
                        "https://tokenharbor.ai", "tokenharbour/fast")


async def test_a_non_gateway_source_is_left_alone(wd: Path) -> None:
    """The warm-up must not fire for a source with no fetched catalog.

    Logfare's list is fetched too, but it has its own refresh command and its
    own fallback semantics; firing this branch for it would change behaviour that
    is already correct. The guard is a list of gateways for that reason.
    """
    cfg = _cfg(wd, "http://localhost:4001")
    app = ZimZillaApp(cfg)
    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        from zimzilla import openrouter as or_mod
        called = {"n": 0}

        def spy(*a, **k):
            called["n"] += 1
            return ([_Model("x")], "")

        with mock.patch.object(or_mod, "fetch_models", spy):
            app._handle_command("/model deepseek-v4.1-flash")
            for _ in range(40):
                await asyncio.sleep(0.02)
                await pilot.pause()

        check("local: no gateway fetch was triggered for a local proxy",
              called["n"] == 0, f"{called['n']} calls")
        check("local: a Logfare model still switches",
              app.cfg.model == "deepseek-v4.1-flash", app.cfg.model)


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for name in ("gateway", "nongateway"):
            (tmp / name).mkdir(parents=True, exist_ok=True)
        await test_a_gateway_model_is_not_policed_against_logfare(tmp / "gateway")
        await test_a_non_gateway_source_is_left_alone(tmp / "nongateway")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed — FAILED: "
              + ", ".join(failed))
        return 1
    print(f"{len(RESULTS)}/{len(RESULTS)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
