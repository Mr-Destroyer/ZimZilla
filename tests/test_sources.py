"""Regression suite for upstream source discovery and proxy start.

Run:  python tests/test_sources.py   (from an activated venv)

Protects the /zim-tokenjuice switch: the installed ~/.zimzilla/tokenjuice
route must win over a leftover $HOME/start-litellm.sh from an older
deepseek-claude layout, and start_proxy must not wrap the service in flock.
A leaked flock on /tmp/zimzilla-proxy.lock is what made the in-TUI switch
hang until timeout while a manual start of the same script finished in
seconds.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import sources  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _touch(path: Path, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# test\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_tokenjuice_prefers_installed_route() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "zimzilla"
        # Both layouts exist — the leftover $HOME copy must lose.
        _touch(home / "start-litellm.sh", 0o755)
        _touch(home / "litellm-config.yaml")
        _touch(home / "claude-source" / "deepseek-claude")
        installed_svc = _touch(zhome / "tokenjuice" / "start-litellm.sh", 0o755)
        installed_cfg = _touch(zhome / "tokenjuice" / "litellm-config.yaml")
        installed_src = _touch(zhome / "tokenjuice" / "source")

        with mock.patch.object(sources, "DEFAULT_HOME", home), \
             mock.patch.object(sources, "ZIMZILLA_HOME", zhome):
            found = sources.discover()

        tj = found.get("tokenjuice")
        check("tokenjuice is discovered when both layouts exist", tj is not None)
        if tj is None:
            return
        check("tokenjuice service is the installed copy",
              tj.service == installed_svc, str(tj.service))
        check("tokenjuice config is the installed copy",
              tj.config == installed_cfg, str(tj.config))
        check("tokenjuice profile is the installed copy",
              tj.profile == installed_src, str(tj.profile))


def test_tokenjuice_falls_back_to_home_layout() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "zimzilla"
        zhome.mkdir()
        home_svc = _touch(home / "start-litellm.sh", 0o755)
        home_cfg = _touch(home / "litellm-config.yaml")
        home_src = _touch(home / "claude-source" / "deepseek-claude")

        with mock.patch.object(sources, "DEFAULT_HOME", home), \
             mock.patch.object(sources, "ZIMZILLA_HOME", zhome):
            found = sources.discover()

        tj = found.get("tokenjuice")
        check("tokenjuice falls back to $HOME when not installed", tj is not None)
        if tj is None:
            return
        check("fallback service is $HOME/start-litellm.sh",
              tj.service == home_svc, str(tj.service))
        check("fallback config is $HOME/litellm-config.yaml",
              tj.config == home_cfg, str(tj.config))
        check("fallback profile is $HOME/claude-source/deepseek-claude",
              tj.profile == home_src, str(tj.profile))


def test_start_proxy_does_not_flock() -> None:
    with tempfile.TemporaryDirectory() as td:
        svc = _touch(Path(td) / "start-litellm.sh", 0o755)
        src = sources.Source(
            key="tokenjuice",
            label="Token Juice",
            port=3999,
            profile=Path(td) / "source",
            service=svc,
            config=Path(td) / "config.yaml",
            model="deepseek-v4.1-flash",
            root=Path(td),
        )
        captured: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)

            class _P:
                returncode = 0
                stdout = ""
                stderr = "boom"

            return _P()

        with mock.patch.object(sources, "proxy_healthy", return_value=False), \
             mock.patch.object(sources.subprocess, "run", side_effect=fake_run):
            ok, detail = sources.start_proxy(src, timeout=5)

        argv = captured.get("argv") or []
        check("start_proxy invokes the service directly",
              argv[:2] == [str(svc), "start"], str(argv))
        check("start_proxy does not wrap the start in flock",
              "flock" not in argv, str(argv))
        check("start_proxy reports the service failure, not a hang",
              ok is False and detail == "boom", f"{ok!r} {detail!r}")


def test_remote_gateways_are_identified_by_host() -> None:
    """The three hosted gateways have no port to match on.

    Logfare and Token Juice are local proxies, found by the port in their URL.
    TokenHarbour, BeatAPI and OpenRouter are remote, so ``active_key`` has to
    recognise them by host instead — and it must check them *before* the port
    regex, or a hosted URL falls through to ``None`` and the session is reported
    as pointing at nothing.
    """
    check("host: tokenharbour is recognised",
          sources.active_key("https://tokenharbor.ai") == "tokenharbour")
    check("host: beatapi is recognised",
          sources.active_key("https://api.beatapi.io") == "beatapi")
    check("host: openrouter is recognised",
          sources.active_key("https://openrouter.ai/api/v1") == "openrouter")

    # The three must not be confused for one another, whatever order the checks
    # happen to run in.
    keys = {
        sources.active_key("https://tokenharbor.ai"),
        sources.active_key("https://api.beatapi.io"),
        sources.active_key("https://openrouter.ai/api/v1"),
    }
    check("host: the three gateways are distinct", len(keys) == 3, str(sorted(keys)))

    check("host: an unknown host is still unknown",
          sources.active_key("https://api.example.com") is None)
    check("host: an empty endpoint is still unknown",
          sources.active_key("") is None)


def test_builtin_keys_survive_a_restart() -> None:
    """A remote selection must not be discarded for having nothing on disk.

    ``current_key`` validates a *discovered* source by checking its files are
    present. The remote gateways have no profile, service or config to discover
    — so gating them the same way would silently drop the selection and put the
    next launch back on Logfare.
    """
    check("builtin: all three remote gateways are listed",
          {"tokenharbour", "beatapi", "openrouter"} <= sources.BUILTIN_KEYS,
          str(sorted(sources.BUILTIN_KEYS)))

    with tempfile.TemporaryDirectory() as td:
        zhome = Path(td) / "zimzilla"
        zhome.mkdir()
        # BOTH homes are pinned to the empty temp tree. Patching only
        # ZIMZILLA_HOME would leave the real $HOME in play, and a box with the
        # legacy $HOME/start-litellm.sh layout would legitimately discover
        # tokenjuice — making the "absent source is discarded" check below fail
        # for a reason that has nothing to do with the code under test.
        empty_home = Path(td) / "home"
        empty_home.mkdir()
        with mock.patch.object(sources, "ZIMZILLA_HOME", zhome), \
             mock.patch.object(sources, "DEFAULT_HOME", empty_home):
            for key in ("tokenharbour", "beatapi", "openrouter"):
                sources.set_current(key)
                check(f"builtin: {key} survives a restart",
                      sources.current_key() == key, str(sources.current_key()))

            # A discovered source that is NOT present must still be dropped —
            # the builtin exemption must not have loosened that.
            sources.set_current("tokenjuice")
            check("builtin: an absent local source is still discarded",
                  sources.current_key() is None, str(sources.current_key()))


def test_models_for_reads_the_remote_catalogs() -> None:
    """models_for returns None on a cold cache, never an empty tuple.

    ``None`` means "no live catalog, use your fallback"; ``()`` would mean
    "this source serves nothing", and the two must not be confused or /model
    would show an empty list on a cold start instead of the declared one.
    """
    from zimzilla import beatapi as beat_mod
    from zimzilla import openrouter as or_mod
    from zimzilla import tokenharbour as th_mod

    for key, mod, url in (
        ("tokenharbour", th_mod, "https://tokenharbor.ai"),
        ("beatapi", beat_mod, "https://api.beatapi.io"),
        ("openrouter", or_mod, "https://openrouter.ai/api/v1"),
    ):
        mod.clear_cache()
        check(f"models_for: {key} yields None on a cold cache",
              sources.models_for(url) is None, repr(sources.models_for(url)))

        with mock.patch.object(mod, "cached_ids", return_value=["m-one", "m-two"]):
            check(f"models_for: {key} reads its module's cache",
                  sources.models_for(url) == ("m-one", "m-two"),
                  repr(sources.models_for(url)))

    check("models_for: an unknown source is still None",
          sources.models_for("https://api.example.com") is None)


def main() -> int:
    test_tokenjuice_prefers_installed_route()
    test_tokenjuice_falls_back_to_home_layout()
    test_start_proxy_does_not_flock()
    test_remote_gateways_are_identified_by_host()
    test_builtin_keys_survive_a_restart()
    test_models_for_reads_the_remote_catalogs()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
