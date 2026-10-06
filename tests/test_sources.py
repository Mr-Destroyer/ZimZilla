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


def main() -> int:
    test_tokenjuice_prefers_installed_route()
    test_tokenjuice_falls_back_to_home_layout()
    test_start_proxy_does_not_flock()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
