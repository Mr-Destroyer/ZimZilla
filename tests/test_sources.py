"""Regression suite for upstream source discovery and proxy start.

Run:  python tests/test_sources.py   (from an activated venv)

Protects the /zim-tokenjuice route: it is direct-only, so the installed
~/.zimzilla/tokenjuice profile must win over a leftover
$HOME/claude-source/deepseek-claude from an older layout, and a proxy left in
the same directory must not be adopted. Also protects start_proxy, which must
not wrap the service in flock: a leaked flock on /tmp/zimzilla-proxy.lock is
what made the in-TUI switch hang until timeout while a manual start of the same
script finished in seconds.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import sources  # noqa: E402
# Imported for the ``switchable`` suite: ``sources`` reaches for AntSeed inside
# the function rather than at module level (to avoid a circular import), so the
# module has to be patched where it actually lives.
from zimzilla import antseed  # noqa: E402

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
        _touch(home / "claude-source" / "deepseek-claude")
        installed_src = _touch(zhome / "tokenjuice" / "source")
        # A proxy left behind by an older install is not part of the route.
        _touch(zhome / "tokenjuice" / "start-litellm.sh", 0o755)
        _touch(zhome / "tokenjuice" / "litellm-config.yaml")

        with mock.patch.object(sources, "DEFAULT_HOME", home), \
             mock.patch.object(sources, "ZIMZILLA_HOME", zhome):
            found = sources.discover()

        tj = found.get("tokenjuice")
        check("tokenjuice is discovered when both layouts exist", tj is not None)
        if tj is None:
            return
        check("tokenjuice profile is the installed copy",
              tj.profile == installed_src, str(tj.profile))
        check("tokenjuice has no proxy to start",
              tj.service is None and tj.config is None,
              f"{tj.service} {tj.config}")
        check("tokenjuice is direct-only", tj.is_direct_only,
              str(tj.is_direct_only))
        route = sources.direct_route(tj)
        check("tokenjuice routes to its upstream, with no proxy",
              route is not None
              and route.base_url == "https://api.tokenjuice.ai/v1",
              str(route and route.base_url))
        check("tokenjuice translates the alias a profile may still name",
              route is not None
              and route.upstream_model("deepseek-v4.1-flash")
              == "deepseek-ai/DeepSeek-V4.1-Flash-1017",
              str(route and route.upstream_model("deepseek-v4.1-flash")))
        # Nothing to start, and saying so is the answer — not a timeout on a
        # port that has no owner.
        ok, detail = sources.start_proxy(tj, timeout=1)
        check("tokenjuice: start_proxy says there is no proxy",
              ok is False and "no local proxy" in detail, f"{ok!r} {detail!r}")


def test_tokenjuice_falls_back_to_home_layout() -> None:
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "zimzilla"
        zhome.mkdir()
        home_src = _touch(home / "claude-source" / "deepseek-claude")

        with mock.patch.object(sources, "DEFAULT_HOME", home), \
             mock.patch.object(sources, "ZIMZILLA_HOME", zhome):
            found = sources.discover()

        tj = found.get("tokenjuice")
        check("tokenjuice falls back to $HOME when not installed", tj is not None)
        if tj is None:
            return
        check("fallback profile is $HOME/claude-source/deepseek-claude",
              tj.profile == home_src, str(tj.profile))
        # The profile is the whole install: there is nothing else to find.
        check("fallback still needs no proxy",
              tj.is_direct_only and tj.port == 0,
              f"{tj.is_direct_only} {tj.port}")


def test_tokenjuice_ships_a_shared_key() -> None:
    """The route works on a fresh clone: the key ships in the tree.

    That is the whole point of the shared profile — no signup, and no
    ``/zim-tokenjuice <key>`` on first run. It is committed on purpose, so the
    checks are that it is *there*, that gitignore does not swallow it (an
    ignored file sits in one checkout and vanishes from everyone else's clone),
    and that it carries a real ``tj_`` key rather than the template's
    placeholder.
    """
    shipped = ROOT / "packaging" / "tokenjuice" / "source"
    check("tokenjuice: the shared profile ships in the tree",
          shipped.is_file(), str(shipped))
    if not shipped.is_file():
        return
    text = shipped.read_text(encoding="utf-8")
    check("tokenjuice: the shared profile carries a tj_ token",
          'ANTHROPIC_AUTH_TOKEN="tj_' in text)
    check("tokenjuice: the token is not the template placeholder",
          "tj_REPLACE_ME" not in text)
    check("tokenjuice: the shared profile points at the upstream",
          'ANTHROPIC_BASE_URL="https://api.tokenjuice.ai/v1"' in text)

    # Committed, not merely present. `check-ignore` exits 1 when the path is
    # not ignored, which is the answer wanted here. Skipped where git or a
    # checkout is absent: that is about the repository, not the code.
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q",
             "packaging/tokenjuice/source"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        check("tokenjuice: gitignore does not swallow the shared profile", True,
              "skipped (no git)")
    else:
        check("tokenjuice: gitignore does not swallow the shared profile",
              proc.returncode == 1, f"check-ignore exit {proc.returncode}")

    # And it is discovered with no per-user profile anywhere — the fresh-clone
    # case, where the tree copy is the only one there is.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        zhome = Path(td) / "zimzilla"
        home.mkdir()
        zhome.mkdir()
        with mock.patch.object(sources, "DEFAULT_HOME", home), \
             mock.patch.object(sources, "ZIMZILLA_HOME", zhome):
            found = sources.discover()
        tj = found.get("tokenjuice")
        check("tokenjuice: discovered with no profile of its own",
              tj is not None, str(sorted(found)))
        if tj is None:
            return
        check("tokenjuice: and the profile it found is the shared one",
              tj.profile == shipped, str(tj.profile))
        profile = sources.load_profile(tj)
        check("tokenjuice: the shared profile parses to a usable key",
              sources.is_tokenjuice_key(profile.get("ANTHROPIC_AUTH_TOKEN")),
              str(sorted(profile)))


def test_start_proxy_does_not_flock() -> None:
    with tempfile.TemporaryDirectory() as td:
        svc = _touch(Path(td) / "start-litellm.sh", 0o755)
        # A proxy source, whatever its name: start_proxy is only ever reached
        # for one of those now that Token Juice has no proxy at all.
        src = sources.Source(
            key="logfare",
            label="Logfare",
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
    """The hosted gateways have no port to match on.

    Logfare and Token Juice are local proxies, found by the port in their URL.
    TokenHarbour and OpenRouter are remote, so ``active_key`` has to recognise
    them by host instead — and it must check them *before* the port regex, or a
    hosted URL falls through to ``None`` and the session is reported as pointing
    at nothing.
    """
    check("host: tokenharbour is recognised",
          sources.active_key("https://tokenharbor.ai") == "tokenharbour")
    check("host: openrouter is recognised",
          sources.active_key("https://openrouter.ai/api/v1") == "openrouter")

    # The two must not be confused for one another, whatever order the checks
    # happen to run in.
    keys = {
        sources.active_key("https://tokenharbor.ai"),
        sources.active_key("https://openrouter.ai/api/v1"),
    }
    check("host: the two gateways are distinct", len(keys) == 2, str(sorted(keys)))

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
    check("builtin: both remote gateways are listed",
          {"tokenharbour", "openrouter"} <= sources.BUILTIN_KEYS,
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
        # The checkout is pinned away for the same reason: the shared Token
        # Juice profile ships in the tree, so a real DEFAULT_ROOT would
        # discover the route on any machine and make the same check vacuous.
        empty_root = Path(td) / "root"
        empty_root.mkdir()
        with mock.patch.object(sources, "ZIMZILLA_HOME", zhome), \
             mock.patch.object(sources, "DEFAULT_HOME", empty_home), \
             mock.patch.object(sources, "DEFAULT_ROOT", empty_root):
            for key in ("tokenharbour", "openrouter"):
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
    from zimzilla import openrouter as or_mod
    from zimzilla import tokenharbour as th_mod

    for key, mod, url in (
        ("tokenharbour", th_mod, "https://tokenharbor.ai"),
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


def test_logfare_is_a_switchable_route() -> None:
    """Logfare is a full route: switchable to, remembered, and shipped.

    It used to be parked while the shipped key looked like a starting point
    only. That made ``/zim-logfare`` refuse the one thing it exists to do. The
    shared key still limits *how much* traffic it can carry, but that is a 429
    the turn path reports, not a reason to hide the route.
    """
    check("logfare: it is not parked",
          "logfare" not in sources.UNDER_CONSTRUCTION,
          str(sorted(sources.UNDER_CONSTRUCTION)))

    # A selection of it must survive a restart, like any other discovered
    # source. This is the exact case the old park refused.
    with tempfile.TemporaryDirectory() as td:
        zhome = Path(td) / "zimzilla"
        zhome.mkdir()
        empty_home = Path(td) / "home"
        empty_home.mkdir()
        # Same as above: the shared profile in the checkout would otherwise be
        # found, and this check is about an *undiscovered* local source. Only
        # the tokenjuice check is wrapped in it — logfare's own files live in
        # DEFAULT_ROOT, so pinning it away would break the check above.
        empty_root = Path(td) / "root"
        empty_root.mkdir()
        with mock.patch.object(sources, "ZIMZILLA_HOME", zhome), \
             mock.patch.object(sources, "DEFAULT_HOME", empty_home):
            sources.set_current("logfare")
            check("logfare: a remembered selection is honoured",
                  sources.current_key() == "logfare", str(sources.current_key()))

            # Token Juice is a live route now, not a parked one — but with no
            # profile on this machine it is simply not discovered, and an
            # undiscovered local source is still dropped.
            sources.set_current("tokenjuice")
            with mock.patch.object(sources, "DEFAULT_ROOT", empty_root):
                check("logfare: token juice without a profile is still dropped",
                      sources.current_key() is None, str(sources.current_key()))

    # The shipped profile is what makes the route work out of the box, so it
    # has to be in the tree — not gitignored, and carrying a usable key.
    shipped = ROOT / "packaging" / "logfare" / "source"
    check("logfare: the shared profile ships in the tree",
          shipped.is_file(), str(shipped))
    if shipped.is_file():
        text = shipped.read_text()
        check("logfare: the shipped profile carries an lfu_ token",
              'ANTHROPIC_AUTH_TOKEN="lfu_' in text)
        check("logfare: the shipped profile points at the local proxy",
              'ANTHROPIC_BASE_URL="http://localhost:4001"' in text)


def test_logfare_offers_every_chat_model() -> None:
    """The fallback list and the proxy config must both cover the whole catalog.

    ``/model`` reads the fetched catalog first, so the declared list is what a
    cold cache shows. Leaving a served model out of it hid that model for the
    first seconds of every session. The proxy config is the other half: a name
    it does not register is rejected before the upstream is ever asked, so a
    model missing there cannot be used at all — the wildcard is what keeps that
    from happening for a model Logfare adds later.
    """
    from zimzilla.config import KNOWN_MODELS

    # The nine chat models Logfare served on 2026-10-19. The non-chat entries
    # (flux-*, whisper-*, aura-*, nova-*, phoenix-*, lucid-origin, sdxl-*) must
    # stay out: they reject a Messages turn.
    served = {
        "claude-haiku-5-5", "deepseek-v4-pro-0813", "deepseek-v4.1-flash",
        "gemma-4-26b", "glm-5.3", "glm-5.3-flash", "kimi-k2.6",
        "logfare/auto", "qwen-3.8-27b",
    }
    check("catalog: the fallback lists every chat model",
          served <= set(sources.LOGFARE_MODELS),
          f"missing {sorted(served - set(sources.LOGFARE_MODELS))}")
    check("catalog: KNOWN_MODELS agrees with the fallback",
          served <= set(KNOWN_MODELS),
          f"missing {sorted(served - set(KNOWN_MODELS))}")
    for non_chat in ("whisper-large-v3-turbo", "flux-2-dev", "aura-2-en",
                     "nova-3", "phoenix-1.0", "lucid-origin", "sdxl-lightning"):
        check(f"catalog: {non_chat} (non-chat) is not offered",
              non_chat not in sources.LOGFARE_MODELS)

    # The proxy config is the real ceiling. Every named chat model must be
    # registered, and the wildcard must be present so a future one routes.
    config = (ROOT / "packaging" / "logfare" / "litellm-config.yaml").read_text()
    for model in served:
        check(f"proxy: {model} is registered", f"model_name: {model}" in config)
    check("proxy: a wildcard route exists so new models need no release",
          'model_name: "*"' in config, "no wildcard in litellm-config.yaml")


def test_nothing_ships_parked() -> None:
    """A parked route stays visible but is never selectable.

    Token Juice was the last thing parked, and it is a working direct route
    now. The mechanism is still the way a route is turned off without also
    hiding that it exists, so it is asserted directly rather than left to the
    next route that needs it.
    """
    check("park: nothing is parked in the shipped tree",
          sources.UNDER_CONSTRUCTION == {}, str(sources.UNDER_CONSTRUCTION))

    with tempfile.TemporaryDirectory() as td:
        zhome = Path(td) / "zimzilla"
        zhome.mkdir()
        empty_home = Path(td) / "home"
        empty_home.mkdir()
        with mock.patch.object(sources, "ZIMZILLA_HOME", zhome), \
             mock.patch.object(sources, "DEFAULT_HOME", empty_home), \
             mock.patch.dict(sources.UNDER_CONSTRUCTION, {"logfare": "test"}):
            sources.set_current("logfare")
            check("park: a remembered selection of a parked source is dropped",
                  sources.current_key() is None, str(sources.current_key()))


def test_switchable_names_only_what_would_work() -> None:
    """``switchable`` answers "where can this session go next?" honestly.

    It exists because the 429 message used to hardcode four hosted gateways.
    On a box with none of them configured, that sentence moved the operator
    from one error to the next; on this box it never mentioned the local
    proxies, which were the sources actually set up. The caller is a *failure*
    message, so a wrong answer is worse than a short one.

    Fully stubbed: ``discover`` and the gateway modules are replaced, so this
    asserts the policy without reading the real ~/.zimzilla or the network.
    """
    class _Cred:
        """Stands in for a gateway module; ``key`` is what it holds, if any."""

        def __init__(self, key: str | None):
            self.key = key

        def load_credentials(self):
            return None if self.key is None else (self.key, "m", "test")

    def run(found: dict, creds: dict, exclude: str | None = None) -> list[str]:
        """``switchable`` minus logfare, which is asserted separately.

        Logfare ships a profile, so it is offered on every machine and would
        otherwise appear in every expectation below. Dropping it here keeps
        each check about the rule it names.
        """
        def fake_module(name: str):
            return _Cred(creds.get(name))
        with mock.patch.object(sources, "discover", lambda: found), \
             mock.patch.object(sources, "_gateway_module", fake_module), \
             mock.patch.object(antseed, "reachable", lambda: False):
            return [k for k in sources.switchable(exclude=exclude)
                    if k != "logfare"]

    # Nothing configured anywhere: the message must not invent a source. The
    # empty list is what makes _friendly_error fall back to /zim-source.
    check("switchable: an empty machine offers nothing",
          run({}, {}) == [], str(run({}, {})))

    # A gateway with no key is not offered — this is the bug the function was
    # written for.
    check("switchable: a gateway with no credential is omitted",
          run({}, {"openrouter": None}) == [], str(run({}, {"openrouter": None})))
    check("switchable: a gateway with a credential is offered",
          run({}, {"openrouter": "sk-x"}) == ["openrouter"],
          str(run({}, {"openrouter": "sk-x"})))

    # The source that just refused must never be recommended again.
    check("switchable: exclude drops the failing source",
          run({}, {"openrouter": "sk-x", "unorouter": "u"}, exclude="openrouter")
          == ["unorouter"],
          str(run({}, {"openrouter": "sk-x", "unorouter": "u"},
                  exclude="openrouter")))

    # A discovered local proxy is offered without asking any module: its
    # files are the proof it can be started.
    fake = sources.Source(
        key="infronai", label="InfronAI", port=4003,
        profile=Path("/nope"), service=Path("/nope"), config=Path("/nope"),
        model="m", root=Path("/nope"), models=(), under_construction=None)
    check("switchable: a discovered local proxy is offered",
          "infronai" in run({"infronai": fake}, {}),
          str(run({"infronai": fake}, {})))

    # Parked sources are never offered, discovered or not. Nothing ships parked
    # today, so this parks a key the preference loop does reach and checks that
    # the guard still works.
    with mock.patch.dict(sources.UNDER_CONSTRUCTION, {"openrouter": "test"}):
        check("switchable: a parked source is never offered",
              "openrouter" not in run({}, {"openrouter": "sk-x"}),
              str(run({}, {"openrouter": "sk-x"})))

    # A source whose module raises is dropped, not propagated: this runs
    # inside an error handler, and a raise here would replace the real 429
    # with a traceback about the hint.
    class _Boom:
        def load_credentials(self):
            raise RuntimeError("unreadable profile")

    with mock.patch.object(sources, "discover", lambda: {}), \
         mock.patch.object(sources, "_gateway_module", lambda n: _Boom()):
        try:
            got = sources.switchable()
            ok = "openrouter" not in got
        except Exception as exc:  # noqa: BLE001
            ok, got = False, f"raised {exc!r}"
    check("switchable: a gateway that raises is dropped", ok, str(got))

    # Logfare always answers: its profile ships, so it is a real option on a
    # fresh clone even when nothing else is set up. Asserted on the raw call,
    # since ``run`` deliberately strips it.
    with mock.patch.object(sources, "discover", lambda: {}), \
         mock.patch.object(sources, "_gateway_module", lambda n: _Cred(None)), \
         mock.patch.object(antseed, "reachable", lambda: False):
        raw = sources.switchable()
    check("switchable: logfare is offered on an empty machine",
          raw == ["logfare"], str(raw))


def main() -> int:
    test_tokenjuice_prefers_installed_route()
    test_tokenjuice_falls_back_to_home_layout()
    test_tokenjuice_ships_a_shared_key()
    test_start_proxy_does_not_flock()
    test_remote_gateways_are_identified_by_host()
    test_builtin_keys_survive_a_restart()
    test_models_for_reads_the_remote_catalogs()
    test_logfare_is_a_switchable_route()
    test_logfare_offers_every_chat_model()
    test_nothing_ships_parked()
    test_switchable_names_only_what_would_work()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
