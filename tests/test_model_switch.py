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

**Why this test covers every gateway and not one.** The original warm-up branch
was written for TokenHarbour alone — ``if active == "tokenharbour"``. OpenRouter
was added later and silently inherited the broken path, because the guard named
one gateway rather than the *class* of gateway that needs it. A test that pinned
only OpenRouter would let the next provider repeat it. So both are driven here,
through the real app, and the final case asserts that a non-gateway source is
left alone.

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

from zimzilla.config import KNOWN_MODELS, Config  # noqa: E402
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
    from zimzilla import openrouter as or_mod
    from zimzilla import tokenharbour as th_mod

    mod = {"openrouter": or_mod, "tokenharbour": th_mod}[key]

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
    await _case_gateway(wd / "or", "openrouter",
                        "https://openrouter.ai/api",
                        "dots-studio/dots-3-note-preview:free")
    await _case_gateway(wd / "th", "tokenharbour",
                        "https://tokenharbor.ai", "tokenharbour/fast")


async def test_logfare_is_policed_against_its_own_live_catalog(wd: Path) -> None:
    """A model Logfare serves *now* must switch, even if the fallback lacks it.

    Logfare was the last source still policed against the hardcoded
    ``KNOWN_MODELS`` list, and the symptom was exact: ``mimo-v2.6-flash`` was
    live and served, absent from that list, and refused with

        ⚠ mimo-v2.6-flash is not served by logfare — see /model for the list

    …leaving the session on the previous model. Logfare's own comment calls the
    fallback a *stale* list that is only safe when the active source has no
    catalog; Logfare has one, so the fallback was a list from the wrong moment
    being used to refuse something that worked.

    The mount warm-up is patched off so this drives the switch path alone — it
    is the switch that must fetch, and a warm cache left by mount would hide
    whether it does.
    """
    from zimzilla import logfare as lf_mod
    from zimzilla.ui import app as app_mod

    live = [_Model("deepseek-v4.1-flash"), _Model("mimo-v2.6-flash")]
    calls = {"n": 0}

    def stub(*a, **k):
        calls["n"] += 1
        return (live, "")

    cfg = _cfg(wd, "http://localhost:4001")
    app = ZimZillaApp(cfg)
    with mock.patch.object(app_mod.ZimZillaApp, "_warm_logfare_catalog"), \
            mock.patch.object(lf_mod, "fetch_models", stub):
        lf_mod.clear_cache()
        async with app.run_test(size=(110, 40)) as pilot:
            app.pop_screen()
            await pilot.pause()
            check("logfare: the catalog really is cold",
                  lf_mod.cached_ids() is None, str(lf_mod.cached_ids()))
            check("logfare: the live model is absent from the fallback",
                  "mimo-v2.6-flash" not in KNOWN_MODELS)
            await _switch(app, pilot, "mimo-v2.6-flash")

            blob = _transcript(app)
            check("logfare: a live model the fallback lacks was accepted",
                  app.cfg.model == "mimo-v2.6-flash", f"model is {app.cfg.model!r}")
            check("logfare: it was not refused as unserved",
                  "is not served by" not in blob, blob[-300:])
            check("logfare: the switch fetched the catalog rather than assuming",
                  calls["n"] >= 1, f"{calls['n']} fetches")
    lf_mod.clear_cache()


async def test_a_non_gateway_source_is_left_alone(wd: Path) -> None:
    """A Logfare endpoint must not trigger another gateway's fetch.

    The guard is a list of gateways with fetched catalogs, and Logfare is now
    one of them — but the fetch it triggers must be *its own*. This pins the
    other half: OpenRouter's fetch never fires for a local Logfare proxy, which
    would be a round-trip to an unrelated host carrying an unrelated key.
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
        # Why no fetch fired: the name is in the declared fallback, so the
        # membership test passes before the fetch branch is reached. Stated
        # rather than left implicit — if the fallback ever drops this name the
        # check above would start passing for the wrong reason.
        check("local: the name was in the fallback, so no fetch was needed",
              "deepseek-v4.1-flash" in KNOWN_MODELS)


async def test_the_footer_reports_the_catalogs_age(wd: Path) -> None:
    """/model must say how current its list is, not merely that one exists.

    A cache filled at startup and one filled a second ago render identically,
    and the wording used to assert "the catalog is live" on the strength of the
    list being non-empty — so a three-hour-old catalog was called live. The
    three states are pinned here: cold, fresh, expired.
    """
    import io
    import json
    import time
    import urllib.request

    from zimzilla import logfare as lf_mod
    from zimzilla.ui import app as app_mod

    # A real payload through the real parser, so the cache holds real Model
    # objects and the render path reads the same shapes it does in production.
    # `mimo-v2.6-flash` is the model that was live, absent from the fallback,
    # and refused — the whole reason this line exists.
    payload = {"data": [
        {"model_id": "deepseek-v4.1-flash", "display_name": "D",
         "status": "operational", "uptime_percent": 93.6,
         "endpoints": ["chat/completions"], "real_traffic_total": 10},
        {"model_id": "mimo-v2.6-flash", "display_name": "M",
         "status": "operational", "uptime_percent": 90.0,
         "endpoints": ["chat/completions"], "real_traffic_total": 10},
    ]}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def stub(req, timeout=None):
        return _Resp(json.dumps(payload).encode())

    def footer(app) -> str:
        """The newest catalog line only.

        The transcript accumulates, so an unscoped search would find the
        "catalog live" printed by an earlier /model and report the expired
        case as a failure — or, worse, pass a stale assertion.
        """
        lines = [l for l in _transcript(app).splitlines() if "catalog" in l]
        return lines[-1] if lines else ""

    cfg = _cfg(wd, "http://localhost:4001")
    app = ZimZillaApp(cfg)
    with mock.patch.object(app_mod.ZimZillaApp, "_warm_logfare_catalog"), \
            mock.patch.object(urllib.request, "urlopen", stub):
        async with app.run_test(size=(110, 40)) as pilot:
            app.pop_screen()
            await pilot.pause()

            lf_mod.clear_cache()
            app._handle_command("/model")
            await pilot.pause()
            line = footer(app)
            check("footer: a cold cache says so rather than claiming live",
                  "not fetched yet" in line and "live" not in line, line)

            lf_mod.fetch_models()
            app._handle_command("/model")
            await pilot.pause()
            line = footer(app)
            check("footer: a fresh cache reports live, with its age",
                  "catalog live (just now)" in line, line)

            # Age it past the TTL. The list is already gone by then — the
            # module returns None — so calling it live would contradict the
            # very list printed above the line.
            lf_mod._CACHE[:] = [(lf_mod._CACHE[0][0],
                                 time.monotonic() - (lf_mod.CACHE_TTL + 3600))]
            app._handle_command("/model")
            await pilot.pause()
            line = footer(app)
            check("footer: an expired cache says expired, not live",
                  "expired" in line and "live" not in line, line)
            check("footer: it names the real age, not the TTL",
                  "1h old" in line, line)
            # And the list above it must be the fallback, not the dead catalog.
            block = _transcript(app).rsplit("usage: /model <name>", 1)[0]
            block = block.rsplit("current model:", 1)[-1]
            check("footer: the expired list is not printed as current",
                  "mimo-v2.6-flash" not in block, block[-300:])
    lf_mod.clear_cache()


async def test_a_retired_stored_model_is_replaced_on_switch(wd: Path) -> None:
    """A profile naming a retired model must not carry it into the session.

    ``/zim-logfare`` takes the model from ``ANTHROPIC_MODEL`` in the stored
    profile — written by hand at setup and never revisited — so it can name a
    model the upstream has since retired. Left alone, the first turn 404s with
    a model name the operator never chose and has no reason to suspect. The
    switch is already paying for a round-trip, so it checks there.
    """
    from zimzilla import logfare as lf_mod

    class _Src:
        key, model = "logfare", "deepseek-v4.1-flash"

    live = [_Model("deepseek-v4.1-flash"), _Model("mimo-v2.6-flash")]

    cfg = _cfg(wd, "http://localhost:4001")
    app = ZimZillaApp(cfg)
    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        with mock.patch.object(lf_mod, "fetch_models", lambda *a, **k: (live, "")):
            kept, replaced = await app._verify_model_for(_Src(), "mimo-v2.6-flash")
            check("switch: a model still served is kept unchanged",
                  (kept, replaced) == ("mimo-v2.6-flash", None), f"{kept}/{replaced}")

            kept, replaced = await app._verify_model_for(_Src(), "deepseek-v3.2")
            check("switch: a retired stored model is replaced by the default",
                  (kept, replaced) == ("deepseek-v4.1-flash", "deepseek-v3.2"),
                  f"{kept}/{replaced}")

        # An unreachable status API is not evidence a model was retired.
        with mock.patch.object(lf_mod, "fetch_models",
                               lambda *a, **k: (None, "boom")):
            kept, replaced = await app._verify_model_for(_Src(), "deepseek-v3.2")
            check("switch: a failed fetch leaves the stored name alone",
                  (kept, replaced) == ("deepseek-v3.2", None), f"{kept}/{replaced}")

        # Only Logfare's local proxy has a stored profile to go stale.
        class _Other:
            key, model = "tokenjuice", "x"
        kept, replaced = await app._verify_model_for(_Other(), "anything")
        check("switch: a non-Logfare source is never second-guessed",
              (kept, replaced) == ("anything", None), f"{kept}/{replaced}")


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for name in ("gateway", "nongateway", "logfare", "footer", "stored"):
            (tmp / name).mkdir(parents=True, exist_ok=True)
        await test_a_gateway_model_is_not_policed_against_logfare(tmp / "gateway")
        await test_logfare_is_policed_against_its_own_live_catalog(tmp / "logfare")
        await test_the_footer_reports_the_catalogs_age(tmp / "footer")
        await test_a_retired_stored_model_is_replaced_on_switch(tmp / "stored")
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
