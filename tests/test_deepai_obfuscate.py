"""Regressions for two commands that crashed on every invocation.

Run:  python tests/test_deepai_obfuscate.py    (from an activated venv)

Both were shipped by the same window of work and both died the moment the
operator used them, for the same underlying reason: a name or an index that
nothing had ever exercised.

1. **`/deepai-api-setup <key>` raised ``NameError: DEFAULT_MODEL``.**
   ``deepcode.save_key`` interpolated ``DEFAULT_MODEL`` and the module never
   defined one — every other source module does (``coarena``, ``flex``,
   ``gemini``, ``crownllm``, ``respan``), so the missing definition was an
   omission, not a design. The key was lost after the profile directory had
   already been created, so the operator saw a traceback and no file. The
   default is the first free lane, the same id ``default_free_model`` returns
   and ``sources.DEEPAI_MODELS`` opens with, so the alias map strips the
   ``deepai/`` prefix on the way upstream. ``/deepai-api-setup`` now also keeps
   the model the session is on, the rule every gateway key path follows.

2. **`/obfuscate` never reached the operator at all.** Three separate faults
   stacked in one command:

   * the dispatcher called ``self._cmd_obfuscate(args)`` while the winning
     definition took no ``self`` (a second definition had shadowed the first
     in a merge), so the call was a ``TypeError`` before it did anything;
   * the surviving body printed through ``os`` — never imported in ``app.py`` —
     and called its helpers as bare names that only exist as methods;
   * ``zimzilla.obfuscate`` had been *replaced* by a different feature (file
     analysis, commit a870c31) while the msfvenom half of the command stayed
     in the TUI, so ``ob_mod.SPECS``/``generate``/``encoders`` were gone.

   The module now carries both features behind one ``handle(argv)`` entry
   point, the TUI has a single ``self``-taking method that captures its output
   onto a worker (``list`` and a real build shell out to msfvenom), and two
   bugs the restore exposed are pinned here as well: ``--dry-run`` never
   advanced the flag index, so it looped forever, and the encoder
   recommendations stored a *string* where every consumer joins a sequence —
   ``"; ".join("base64 command encoder")`` renders ``b; a; s; e; 6; 4; ...``.

3. **`/obfuscate list` could raise inside the UI thread.** The intruder's
   error callback in ``suiteui`` closed over ``e`` — a name Python unbinds
   when the ``except`` block exits, so the failure being reported was replaced
   by a ``NameError``. The message is built before the callback is.

4. **`/models deepai` answered `HTTP 404 from api.deepai.org` on a key that
   works.** `MODELS_PATH` was defined twice in `deepcode.py` — first the API
   route, then the DeepAI Code app's cache as a `Path`. The second definition
   won, the f-string stringified the Path, and every fetch requested
   `https://api.deepai.org/home/<user>/.deepai-code/.../models.json`. Probing
   the real route with the operator's key answers **200**: 11 free lanes, 27
   entries. Three faults sat behind that one:

   * the live body is `{"models": [...]}` while `_parse_models` read `data`,
     so even a 200 parsed into an empty catalog;
   * `load_api_key()` read only the environment and the app's own secret file,
     so the profile `/deepai-api-setup` writes was invisible to
     `/zim-zimthegoat`, which answered "no DeepAI api_key found" over a key
     sitting in `~/.zimzilla/deepai/source`;
   * `/models deepai` was handed `chat_models` where every other source gets
     `usable_free_models`, so the paid lanes — which answer 200 and spend
     Theos — would have been listed under a heading that says FREE MODELS.

   The 404 branch now falls back to the app's cache and then to the declared
   table, and says which one it is showing.

No network: msfvenom is only *listed* (``--list encoders``), never asked to
build, and its absence is a pass — the report has to arrive either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set before anything imports ``zimzilla.sources``: the profile path every
# source module writes through is read once, at import.
_HOME = Path(tempfile.mkdtemp(prefix="zim-deepai-obf-"))
os.environ["ZIMZILLA_HOME"] = str(_HOME)

from zimzilla import deepcode, obfuscate          # noqa: E402
from zimzilla.config import Config                # noqa: E402
from unittest import mock                        # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

KEY = "f259f412-8882-4917-a218-be9ca5e09f12"


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


@contextlib.contextmanager
def guard(seconds: int = 20):
    """Turn a stuck loop into a failure instead of a hung suite."""

    def _boom(_signum, _frame):
        raise TimeoutError(f"handle() still running after {seconds}s "
                           "— the flag loop never advances")

    previous = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def captured(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = obfuscate.handle(argv)
    return (0 if rc is None else rc), buf.getvalue()


def test_deepai_profile() -> None:
    """The reported traceback: save_key with no model argument."""
    check("deepai: DEFAULT_MODEL is defined",
          isinstance(getattr(deepcode, "DEFAULT_MODEL", None), str)
          and bool(deepcode.DEFAULT_MODEL),
          str(getattr(deepcode, "DEFAULT_MODEL", None)))
    check("deepai: the default is a lane the alias map knows",
          deepcode.DEFAULT_MODEL.startswith("deepai/")
          and deepcode.DEFAULT_MODEL == deepcode.default_free_model(),
          deepcode.DEFAULT_MODEL)

    try:
        path = deepcode.save_key(KEY)
        check("deepai: save_key writes the profile", path.exists(), str(path))
    except Exception as exc:  # noqa: BLE001
        check("deepai: save_key writes the profile", False,
              f"{type(exc).__name__}: {exc}")
        return

    body = path.read_text(encoding="utf-8")
    check("deepai: profile is mode 600",
          (path.stat().st_mode & 0o777) == 0o600, oct(path.stat().st_mode & 0o777))
    check("deepai: profile carries the key", f'DEEPAI_API_KEY="{KEY}"' in body)
    check("deepai: profile carries a model", f'DEEPAI_MODEL="{deepcode.DEFAULT_MODEL}"' in body,
          [ln for ln in body.splitlines() if "MODEL" in ln])
    check("deepai: a named model wins", deepcode.save_key(KEY, "deepai/gpt-6-luna")
          and 'DEEPAI_MODEL="deepai/gpt-6-luna"' in path.read_text(encoding="utf-8"))
    # Put the profile back on the default: every later check reads it back and
    # must not inherit this deliberate override.
    deepcode.save_key(KEY)
    creds = deepcode.load_credentials()
    check("deepai: the key round-trips",
          creds is not None and creds[0] == KEY, str(creds and creds[0][-4:]))


def test_obfuscate_module() -> None:
    """The command bodies, without the TUI: every shape, no hang, no traceback."""
    rc, out = captured([])
    check("obfuscate: bare invocation prints usage",
          rc == 0 and "/obfuscate <file>" in out and "msfvenom" in out)

    rc, out = captured(["virus.apk"])
    check("obfuscate: a file is read by extension",
          rc == 0 and "Obfuscation Analysis" in out and "ANDROID" in out,
          out.splitlines()[1] if out.splitlines() else "")

    with guard(20):
        rc, out = captured(["windows/x64/meterpreter_reverse_tcp", "x64/xor_dynamic",
                            "--dry-run", "--lhost", "10.0.0.5", "--lport", "5555"])
    check("obfuscate: --dry-run returns instead of looping",
          rc == 0 and "COMMAND (copy-paste this into your shell)" in out)
    check("obfuscate: --dry-run prints the msfvenom command",
          "LHOST=10.0.0.5" in out and "LPORT=5555" in out and "msfvenom -p" in out)

    rc, out = captured(["nope", "bad", "--bogus"])
    check("obfuscate: a bad flag is refused, not swallowed",
          rc == 1 and "unexpected argument" in out)

    rc, out = captured(["linux/x64/meterpreter_reverse_tcp", "x64/xor_dynamic",
                        "--dry-run"])
    check("obfuscate: recommendations read as sentences",
          rc == 0 and "x64/xor_dynamic encoder for linux payloads" in out
          and "x; 6; 4; /" not in out,
          [ln for ln in out.splitlines() if "xor" in ln][:2])

    rc, out = captured(["list"])
    check("obfuscate: list is a report either way",
          rc == 0 and "/obfuscate <payload> <encoder>" in out
          and ("Available pairings" in out or "msfvenom not installed" in out))

    check("obfuscate: the module serves both features",
          hasattr(obfuscate, "SPECS") and hasattr(obfuscate, "EXTENSION_SPECS")
          and hasattr(obfuscate, "generate") and hasattr(obfuscate, "analyze_payload_file"))


class _FakeResponse:
    """Just enough of an HTTP response for ``fetch_models`` to read."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_catalog() -> None:
    """The reported `HTTP 404 from api.deepai.org` on `/models deepai`."""
    import urllib.error
    import urllib.request

    check("catalog: the API route is a route, not a file",
          not isinstance(deepcode.MODELS_ROUTE, Path)
          and deepcode.MODELS_ROUTE == "/deepai_code/models",
          str(deepcode.MODELS_ROUTE))
    check("catalog: the app's cache is a file, under its own name",
          isinstance(deepcode.CACHE_FILE, Path)
          and deepcode.CACHE_FILE.suffix == ".json",
          str(deepcode.CACHE_FILE))

    live = {"models": [
        {"id": "glm-5.3-flash", "name": "GLM 5.3 Flash",
         "theosPerRequest": "0", "contextWindow": 320000},
        {"id": "claude-opus-5-5", "name": "Claude Opus 5.5",
         "theosPerRequest": "9", "contextWindow": 320000},
    ]}
    seen: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _FakeResponse(live)

    deepcode._CACHE.clear()
    with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
        models, err = deepcode.fetch_models("test-key", use_cache=False)
        free_only, free_err = deepcode.usable_free_models(
            "test-key", use_cache=False)

    check("catalog: fetch requests the API route",
          seen.get("url") == "https://api.deepai.org/deepai_code/models",
          seen.get("url", "(not called)"))
    check("catalog: a 200 body of {models: [...]} parses",
          err == "" and models is not None and len(models) == 2,
          f"n={len(models) if models is not None else None} err={err!r}")
    check("catalog: the free lane keeps its context window",
          bool(models) and models[0].free and models[0].context_window == 320000,
          str(models[0] if models else None))
    check("catalog: the pane's list leaves the paid lanes out",
          free_only is not None
          and [m.id for m in free_only] == ["deepai/glm-5.3-flash"],
          str([m.id for m in (free_only or [])]))
    check("catalog: the free list still carries the fallback note",
          free_err == err, repr(free_err))

    # The 404 branch has two rungs — the app's own cache, then the declared
    # table — and which one answers depends on whether the DeepAI Code app is
    # installed on the host running this. That is not a property of the code,
    # so both rungs are pinned explicitly: `load_cached_models` is stubbed
    # rather than left to read whatever is in the operator's home directory.
    # (Left unpinned, this check passed only on a host without the app, and
    # failed on one with it — the cache answered and the note said nothing.)
    def raise_404(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found",
                                     hdrs=None, fp=io.BytesIO(b"{}"))

    declared = len(deepcode.FREE_MODELS) + len(deepcode.PAID_MODELS)

    # Rung 2: no app installed, so the declared table keeps the catalog usable.
    deepcode._CACHE.clear()
    with mock.patch.object(urllib.request, "urlopen", raise_404), \
            mock.patch.object(deepcode, "load_cached_models", lambda: []):
        models, err = deepcode.fetch_models("test-key-404", use_cache=False)

    check("catalog: a 404 falls back to the declared table",
          models is not None and len(models) == declared,
          f"n={len(models) if models is not None else None} err={err!r}")
    check("catalog: the fallback says what it is showing",
          "declared" in (err or ""), err or "")

    # Rung 1: the app's cache is the better list when it exists, and it is
    # preferred over the declared table.
    deepcode._CACHE.clear()
    with mock.patch.object(urllib.request, "urlopen", raise_404), \
            mock.patch.object(deepcode, "load_cached_models",
                              lambda: [{"id": "cached-lane", "name": "Cached"}]):
        models, err = deepcode.fetch_models("test-key-404b", use_cache=False)

    check("catalog: the app's cache is preferred over the declared table",
          models is not None and [m.id for m in models] == ["deepai/cached-lane"],
          str([m.id for m in (models or [])]))
    check("catalog: a cache hit reports no fallback note", err == "", repr(err))
    deepcode._CACHE.clear()

    creds = deepcode.load_credentials()
    check("catalog: load_api_key reads ZimZilla's own profile",
          creds is not None and deepcode.load_api_key() == creds[0],
          (deepcode.load_api_key() or "no key")[:6])
    check("catalog: the profile's saved model is read back",
          creds is not None and creds[1] == deepcode.DEFAULT_MODEL,
          repr(creds[1] if creds else None))


def test_wiring() -> None:
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    check("wiring: one /obfuscate definition, and it takes self",
          app.count("def _cmd_obfuscate(self, args)") == 1
          and "def _cmd_obfuscate(argv)" not in app
          and "def _cmd_obfuscate_list():" not in app)
    check("wiring: /obfuscate runs off the UI thread",
          'self.run_worker(_run, thread=True' in app)
    check("wiring: deepcode defines its own default",
          'DEFAULT_MODEL = "deepai/' in
          (ROOT / "zimzilla" / "deepcode.py").read_text(encoding="utf-8"))
    check("wiring: /models deepai is handed the free lanes",
          "usable_free_models(creds[0] if creds else \"\")" in app)
    check("wiring: /zim-zimthegoat points at the setup command",
          "no DeepAI api_key — save yours with /deepai-api-setup" in app)
    suite = (ROOT / "zimzilla" / "ui" / "suiteui.py").read_text(encoding="utf-8")
    check("wiring: the intruder error message is built before the callback",
          "message = f\"  error: {exc}\\n\"" in suite
          and "lambda msg=message:" in suite)


async def test_screen(tmp: Path) -> None:
    """Both commands through the dispatcher a real keystroke reaches."""
    from zimzilla.ui.app import ZimZillaApp
    from zimzilla.ui.widgets import ChatPane

    cfg = Config.from_env(workdir=tmp, base_url="https://logfare.ai/v1",
                          api_key="test")
    cfg.state_dir = tmp / "state"

    seen: list[str] = []
    original = ChatPane.write_block

    def spy(self, renderable, *args, **kwargs):
        seen.append(getattr(renderable, "plain", str(renderable)))
        return original(self, renderable, *args, **kwargs)

    ChatPane.write_block = spy
    app = ZimZillaApp(cfg)

    def raised(fn, *a):
        try:
            fn(*a)
            return ""
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()

        # The profile lives under ZIMZILLA_HOME (the module constant read at
        # import), not the Config workdir — clear it so the next check proves
        # this command wrote it, rather than reading back what the module test
        # already put there.
        profile = _HOME / "deepai" / "source"
        if profile.exists():
            profile.unlink()

        err = raised(app._handle_command, f"/deepai-api-setup {KEY}")
        await pilot.pause()
        check("screen: /deepai-api-setup answers instead of crashing", not err, err)

        check("screen: it wrote the profile",
              profile.exists() and KEY in profile.read_text(encoding="utf-8"),
              str(profile))

        # `clear` is the branch that used to bind `sources_mod` locally — the
        # one behind the UnboundLocalError the save path then hit.
        err = raised(app._handle_command, "/deepai-api-setup clear")
        await pilot.pause()
        check("screen: /deepai-api-setup clear answers and removes it",
              not err and not profile.exists(), err or str(profile))

        # The command the operator ran next. With only ZimZilla's profile key
        # — no DeepAI Code app on this host — it must repoint the session, not
        # answer "no DeepAI api_key found" over a key we had just saved.
        err = raised(app._handle_command, f"/deepai-api-setup {KEY}")
        await pilot.pause()
        seen.clear()
        err = raised(app._handle_command, "/zim-zimthegoat")
        await pilot.pause()
        check("screen: /zim-zimthegoat accepts the profile key", not err, err)
        check("screen: the session points at the chat endpoint",
              app.cfg.base_url.endswith("/deepai_code/chat/completions"),
              app.cfg.base_url)
        check("screen: the saved key travels with it",
              (app.cfg.extra_headers or {}).get("api-key", "").endswith("9f12"))
        check("screen: no false 'no DeepAI api_key' refusal",
              not any("no DeepAI api_key" in b for b in seen),
              next((b.strip() for b in seen if "DeepAI" in b), "clean"))
        # …and on the lane the profile names, not whichever free slug sorts
        # first (alphabetically `deepai-cyber`, which nothing asked for).
        check("screen: it lands on the profile's saved model",
              app.cfg.model in (deepcode.DEFAULT_MODEL,
                                deepcode.DEFAULT_MODEL.split("/", 1)[-1]),
              app.cfg.model)

        seen.clear()
        err = raised(app._handle_command, "/obfuscate virus.apk")
        await pilot.pause()
        check("screen: /obfuscate <file> answers instead of crashing", not err, err)
        check("screen: the file report reaches the pane",
              any("Obfuscation Analysis" in b for b in seen),
              f"{len(seen)} block(s)")

        seen.clear()
        err = raised(app._handle_command,
                     "/obfuscate windows/x64/meterpreter_reverse_tcp "
                     "x64/xor_dynamic --dry-run")
        await pilot.pause()
        await asyncio.sleep(1.0)
        check("screen: /obfuscate --dry-run answers instead of crashing", not err, err)
        check("screen: the build command reaches the pane",
              any("COMMAND (copy-paste this into your shell)" in b for b in seen),
              f"{len(seen)} block(s)")

        seen.clear()
        err = raised(app._handle_command, "/obfuscate list")
        # msfvenom takes seconds; the report arrives when the worker is done.
        for _ in range(60):
            await asyncio.sleep(0.5)
            if any("/obfuscate <payload> <encoder>" in b for b in seen):
                break
        check("screen: /obfuscate list answers instead of crashing", not err, err)
        check("screen: the listing reaches the pane",
              any("/obfuscate <payload> <encoder>" in b for b in seen),
              f"{len(seen)} block(s)")

        seen.clear()
        err = raised(app._handle_command, "/obfuscate")
        await pilot.pause()
        check("screen: bare /obfuscate answers instead of crashing", not err, err)
        check("screen: usage reaches the pane",
              any("/obfuscate <file>" in b for b in seen))

    ChatPane.write_block = original


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zim-fixes-") as td:
        base = Path(td)
        test_deepai_profile()
        test_obfuscate_module()
        test_catalog()
        test_wiring()
        asyncio.run(test_screen(base / "screen"))
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
