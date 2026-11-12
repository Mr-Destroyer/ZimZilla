"""See a web page the way a person does: render it, look at it, read it.

A plain ``GET`` returns the HTML a server sent, which on YouTube, TikTok,
Instagram or Facebook is a JavaScript shell with none of the content in it.
``web_fetch`` can therefore tell you a page exists and nothing about what is on
it. This module opens the page in a real Chromium, waits for the JavaScript to
draw it, scrolls (which is what loads comments and lazy thumbnails), captures
the screen, and reads the rendered DOM back out:

* the visible text, headings and meta tags,
* every image, video, GIF and thumbnail with its URL and dimensions,
* links, JSON-LD, and the embedded state a site uses to render itself,
* comments — with site-specific selectors first, then a conservative generic
  pass, because a comment is the thing a page shows and its HTML usually hides,
* a viewport screenshot and a full-page screenshot, saved where the operator
  can open them and attached to the model's next request.

The browser is Playwright's Chromium. A bundled browser is used when one is
installed (``playwright install chromium``); otherwise the system Chromium is
launched directly, which is what makes the feature work on a machine that
already has a browser and no Playwright download. Both are optional at import
time: a harness without them reports the install line instead of failing to
start.

Nothing here fetches a page for its own sake. The caller (a tool or a command)
is responsible for the scope guard, and :func:`normalise_url` accepts only
``http`` and ``https`` — never ``file:`` or ``javascript:``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

#: A real Chrome UA. The default headless UA announces itself, and several
#: sites answer that announcement with a challenge page instead of content.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

#: A desktop viewport. Wide enough for the three-column layouts these sites
#: use, small enough that a screenshot stays legible and cheap.
VIEWPORT = {"width": 1280, "height": 800}

#: Ceiling on a page screenshot's height. A feed can be 40,000px tall; sending
#: that as one image costs more tokens than the page is worth, and the top few
#: screens are where the identity, title and first comments live.
MAX_SHOT_HEIGHT = 4200

#: How much visible text one capture carries into the conversation.
MAX_TEXT = 14_000

#: How many items of any one kind (images, links, comments) are returned.
MAX_ITEMS = 40

#: How many screenshots/thumbnails are downloaded for the model to look at.
MAX_ATTACHMENTS = 6

#: Download cap for a thumbnail. Anything larger is not a thumbnail.
THUMB_BYTES = 6_000_000

#: Timeout for one navigation, in milliseconds.
NAV_TIMEOUT = 45_000


class BrowserError(Exception):
    """A browser that cannot start, or a page that cannot be opened."""


@dataclass
class Media:
    """One image, video, GIF or thumbnail found in the rendered page."""

    kind: str                 # image | video | gif | thumbnail
    url: str
    alt: str = ""
    width: int = 0
    height: int = 0
    poster: str = ""
    source: str = ""          # element or meta tag it came from

    def line(self) -> str:
        bits = [f"{self.kind}: {self.url}"]
        if self.alt:
            bits.append(f"alt={self.alt[:80]!r}")
        if self.width or self.height:
            bits.append(f"{self.width}×{self.height}")
        if self.poster and self.poster != self.url:
            bits.append(f"poster={self.poster}")
        return "  " + "  ".join(bits)


@dataclass
class Comment:
    """One comment as the page rendered it."""

    author: str
    text: str
    likes: str = ""

    def line(self) -> str:
        who = self.author or "?"
        likes = f"  ({self.likes})" if self.likes else ""
        return f"  {who}{likes}: {self.text}"


@dataclass
class Capture:
    """Everything one rendered page gave up."""

    url: str
    final_url: str = ""
    title: str = ""
    description: str = ""
    site: str = ""
    text: str = ""
    headings: list[str] = field(default_factory=list)
    images: list[Media] = field(default_factory=list)
    videos: list[Media] = field(default_factory=list)
    gifs: list[Media] = field(default_factory=list)
    thumbnails: list[Media] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    jsonld: list[dict] = field(default_factory=list)
    extras: dict = field(default_factory=dict)
    screenshots: list[Path] = field(default_factory=list)
    attachments: list[Path] = field(default_factory=list)
    html_path: Path | None = None
    console: list[str] = field(default_factory=list)
    gated: bool = False
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def all_media(self) -> list[Media]:
        return [*self.images, *self.videos, *self.gifs, *self.thumbnails]

    def report(self, *, max_text: int = MAX_TEXT,
               max_items: int = MAX_ITEMS) -> str:
        """A compact, model-readable account of the page.

        Deliberately not the HTML and not the screenshot: this is the index the
        model reads before deciding what to look at. The pictures themselves
        travel alongside as attachments.
        """
        if self.error:
            return f"browse failed: {self.error}"
        out: list[str] = []
        out.append(f"PAGE: {self.title or '(no title)'}")
        out.append(f"URL: {self.final_url or self.url}")
        if self.site:
            out.append(f"SITE: {self.site}")
        if self.gated:
            out.append("GATED: a login or consent wall is covering the content")
        if self.notes:
            out.append("NOTES: " + " · ".join(self.notes[:6]))
        if self.description:
            out.append(f"DESCRIPTION: {self.description[:1200]}")
        if self.headings:
            out.append("HEADINGS: " + " | ".join(self.headings[:16]))
        if self.extras:
            out.append("PAGE DATA:")
            for key, value in self.extras.items():
                if value in (None, "", [], {}):
                    continue
                text = json.dumps(value, ensure_ascii=False) if not isinstance(
                    value, str) else value
                out.append(f"  {key}: {str(text)[:1200]}")
        if self.text:
            body = self.text.strip()
            if len(body) > max_text:
                body = body[:max_text].rstrip() + "\n…(visible text truncated)"
            out.append("VISIBLE TEXT:\n" + body)

        def _section(label: str, items: list, limit: int) -> None:
            if not items:
                return
            out.append(f"{label} ({len(items)}):")
            out.extend(item.line() for item in items[:limit])
            if len(items) > limit:
                out.append(f"  … {len(items) - limit} more")

        _section("IMAGES", self.images, max_items)
        _section("VIDEOS", self.videos, max_items)
        _section("GIFS", self.gifs, max_items)
        _section("THUMBNAILS", self.thumbnails, max_items)
        if self.comments:
            out.append(f"COMMENTS ({len(self.comments)}):")
            out.extend(c.line() for c in self.comments[:max_items])
            if len(self.comments) > max_items:
                out.append(f"  … {len(self.comments) - max_items} more")
        if self.links:
            out.append(f"LINKS ({len(self.links)}):")
            for text, href in self.links[:max_items]:
                out.append(f"  {text[:80]!r} -> {href}")
        if self.screenshots:
            out.append("SCREENSHOTS: " + ", ".join(str(p) for p in self.screenshots))
        if self.attachments:
            out.append("ATTACHED: " + ", ".join(str(p) for p in self.attachments))
        if self.console:
            out.append("CONSOLE ERRORS: " + " | ".join(self.console[:5]))
        return "\n".join(out)


# ---------------------------------------------------------------------------
# URLs, browsers and cookies
# ---------------------------------------------------------------------------

def normalise_url(url: str) -> str:
    """An http(s) URL, or :class:`BrowserError`.

    A bare host gets ``https://`` — that is what an operator types. Anything
    with a scheme other than http/https is refused: the browser can do a great
    deal more than a page render, and none of it belongs behind a model tool.
    """
    raw = (url or "").strip().strip("<>")
    if not raw:
        raise BrowserError("no URL")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https"):
        raise BrowserError(f"only http(s) URLs are allowed, not {parsed.scheme}:")
    if not parsed.hostname:
        raise BrowserError(f"not a URL: {url!r}")
    return urllib.parse.urlunsplit(parsed)


def _launch_browser(playwright):
    """Start Chromium, falling back to the system binary.

    The bundled browser is preferred because it matches the driver; a machine
    that has Chromium but has never run ``playwright install`` is the common
    case here, and launching it directly is what keeps the feature working
    there.
    """
    headless = os.environ.get("ZIMZILLA_BROWSER_HEADED") not in ("1", "true", "yes")
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
    ]
    kwargs = {"headless": headless, "args": args}
    try:
        return playwright.chromium.launch(**kwargs)
    except Exception as bundled:
        exe = _system_chromium()
        if exe is None:
            raise BrowserError(
                "no Chromium for Playwright — run `python -m playwright install "
                "chromium`, or install the chromium package"
            ) from bundled
        kwargs["executable_path"] = exe
        try:
            return playwright.chromium.launch(**kwargs)
        except Exception as e2:
            raise BrowserError(f"could not start Chromium ({exe}): {e2}") from e2


def _system_chromium() -> str | None:
    for env in ("ZIMZILLA_CHROMIUM", "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"):
        value = os.environ.get(env)
        if value and Path(value).exists():
            return value
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _cookies(path: Path | None) -> list[dict]:
    """Cookies from a Playwright storage-state file or a Netscape jar.

    This is how a logged-in session is used: export the browser's cookies once,
    point ``ZIMZILLA_BROWSER_COOKIES`` at the file, and the pages that require a
    session render as they do for the operator. Nothing here logs in, solves a
    challenge or guesses a credential.
    """
    path = path or Path(os.environ.get("ZIMZILLA_BROWSER_COOKIES")
                        or (Path.home() / ".zimzilla" / "browser" / "cookies.json"))
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            cookie = {
                "name": str(entry["name"]),
                "value": str(entry.get("value") or ""),
                "domain": str(entry.get("domain") or ""),
                "path": str(entry.get("path") or "/"),
            }
            if entry.get("expires") not in (None, -1):
                try:
                    cookie["expires"] = float(entry["expires"])
                except (TypeError, ValueError):
                    pass
            if entry.get("httpOnly"):
                cookie["httpOnly"] = True
            if entry.get("secure"):
                cookie["secure"] = True
            same = str(entry.get("sameSite") or "").lower()
            if same in ("strict", "lax", "none"):
                cookie["sameSite"] = same.capitalize()
            out.append(cookie)
        return out
    # Netscape cookies.txt: domain, flag, path, secure, expiry, name, value.
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, cpath, secure, expires, name, value = parts[:7]
        cookie = {"name": name, "value": value,
                  "domain": domain, "path": cpath or "/"}
        if secure.upper() == "TRUE":
            cookie["secure"] = True
        try:
            exp = float(expires)
            if exp > 0:
                cookie["expires"] = exp
        except ValueError:
            pass
        out.append(cookie)
    return out


def playwright_available() -> bool:
    """Whether the Playwright package is importable."""
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def evidence_dir(state_dir, host: str) -> Path:
    """A fresh directory for one capture's screenshots and downloaded media.

    Under the state dir rather than the working tree: a page's pictures are
    evidence, and a checkout should not collect them. The leaf is unique, so
    two captures in the same second cannot overwrite each other.
    """
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", host or "page").strip("-")[:48] or "page"
    base = Path(state_dir) / "vision"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(
        prefix=f"{slug}-{time.strftime('%Y%m%d-%H%M%S')}-", dir=base))


# ---------------------------------------------------------------------------
# The page extractor
# ---------------------------------------------------------------------------

#: One evaluate() call does the whole read. It is deliberately a single script:
#: every round-trip to the page is a chance for a navigation to invalidate the
#: context, and one pass is both faster and more consistent than a dozen
#: selectors asked one at a time.
_EXTRACT_JS = r"""
() => {
  const abs = (u) => {
    if (!u) return "";
    try { const x = new URL(u, location.href).href;
          return (x.startsWith("http://") || x.startsWith("https://")) ? x : ""; }
    catch (e) { return ""; }
  };
  const txt = (el) => (el && (el.innerText || el.textContent) || "").trim();
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const rect = (el) => { try { return el.getBoundingClientRect(); }
                         catch (e) { return {width:0,height:0}; } };
  const meta = {};
  document.querySelectorAll("meta").forEach((m) => {
    const key = m.getAttribute("property") || m.getAttribute("name");
    const value = m.getAttribute("content");
    if (key && value && !(key in meta)) meta[key] = value;
  });
  const attr = (sel, name) => {
    const el = document.querySelector(sel);
    return el ? (el.getAttribute(name) || "") : "";
  };

  // ---- images, including CSS backgrounds -------------------------------
  const images = [];
  const seen = new Set();
  const pushImage = (url, alt, w, h, source) => {
    const full = abs(url);
    if (!full || seen.has(full)) return;
    if (full.startsWith("data:") || full.startsWith("blob:")) return;
    seen.add(full);
    images.push({url: full, alt: clean(alt).slice(0, 160),
                 width: w || 0, height: h || 0, source});
  };
  document.querySelectorAll("img").forEach((img) => {
    const r = rect(img);
    if (r.width < 32 || r.height < 32) return;
    let src = img.currentSrc || img.src;
    if (!src && img.srcset) src = (img.srcset.split(",")[0] || "").trim().split(" ")[0];
    pushImage(src, img.alt, img.naturalWidth || Math.round(r.width),
              img.naturalHeight || Math.round(r.height), "img");
  });
  let scanned = 0;
  document.querySelectorAll("*").forEach((el) => {
    if (scanned++ > 2500) return;
    let bg = "";
    try { bg = getComputedStyle(el).backgroundImage || ""; } catch (e) { return; }
    if (!bg || bg === "none" || !bg.includes("url(")) return;
    const m = bg.match(/url\(["']?(.*?)["']?\)/);
    if (!m) return;
    const r = rect(el);
    if (r.width < 48 || r.height < 48) return;
    pushImage(m[1], "", Math.round(r.width), Math.round(r.height), "background");
  });

  // ---- video ------------------------------------------------------------
  const videos = [];
  const vSeen = new Set();
  const pushVideo = (url, poster, w, h, source) => {
    const full = abs(url);
    if (!full || vSeen.has(full)) return;
    vSeen.add(full);
    videos.push({url: full, poster: abs(poster), width: w || 0, height: h || 0, source});
  };
  document.querySelectorAll("video").forEach((v) => {
    const r = rect(v);
    pushVideo(v.currentSrc || v.src, v.poster,
              v.videoWidth || Math.round(r.width),
              v.videoHeight || Math.round(r.height), "video");
    v.querySelectorAll("source").forEach((s) => pushVideo(s.src, v.poster,
      Math.round(r.width), Math.round(r.height), "source"));
  });
  ["og:video", "og:video:url", "twitter:player:stream"].forEach((key) => {
    if (meta[key]) pushVideo(meta[key], meta["og:image"], 0, 0, "meta:" + key);
  });

  // ---- links ------------------------------------------------------------
  const links = [];
  const lSeen = new Set();
  document.querySelectorAll("a[href]").forEach((a) => {
    const href = abs(a.href);
    if (!href || lSeen.has(href)) return;
    const label = clean(a.innerText || a.getAttribute("aria-label") || "");
    if (!label || label.length < 2) return;
    lSeen.add(href);
    links.push([label.slice(0, 140), href]);
  });

  // ---- comments ---------------------------------------------------------
  const comments = [];
  const cSeen = new Set();
  const pushComment = (author, text, likes) => {
    const body = clean(text);
    if (!body || body.length < 2 || body.length > 2000) return;
    const key = body.slice(0, 160);
    if (cSeen.has(key)) return;
    cSeen.add(key);
    comments.push({author: clean(author).slice(0, 80), text: body,
                   likes: clean(likes).slice(0, 24)});
  };
  document.querySelectorAll("ytd-comment-thread-renderer").forEach((el) => {
    pushComment(txt(el.querySelector("#author-text")),
                txt(el.querySelector("#content-text")),
                txt(el.querySelector("#vote-count-middle")));
  });
  document.querySelectorAll('[data-e2e="comment-level-1"]').forEach((el) => {
    pushComment(txt(el.querySelector('[data-e2e="comment-username-1"]'))
                  || txt(el.querySelector("a")),
                txt(el.querySelector("p")) || txt(el),
                txt(el.querySelector('[data-e2e="comment-like-count"]')));
  });
  // Instagram obfuscates its class names, but a post's comment list is still
  // an <article> with a <ul> of <li> rows, each led by the author's link.
  if (/\/(p|reel)\//.test(location.pathname)) {
    document.querySelectorAll("article ul li").forEach((li) => {
      const author = txt(li.querySelector("a"));
      const body = clean(li.innerText).replace(author, "").trim();
      pushComment(author, body, "");
    });
  }
  document.querySelectorAll('div[role="article"]').forEach((el) => {
    const body = clean(el.innerText);
    if (!body || body.length < 24) return;
    const author = txt(el.querySelector("h3, h4, strong, a"));
    pushComment(author, body, "");
  });
  document.querySelectorAll(
    '[class*="comment" i], [data-testid*="comment" i], [id*="comment" i]'
  ).forEach((el) => {
    if (el.querySelector('[class*="comment" i], [data-testid*="comment" i]')) return;
    const body = clean(el.innerText);
    if (!body || body.length < 12 || body.length > 1200) return;
    const author = txt(el.querySelector('[class*="author" i], [class*="user" i], a'));
    pushComment(author, body, "");
  });

  // ---- embedded page state ---------------------------------------------
  const extras = {};
  try {
    const pr = window.ytInitialPlayerResponse;
    if (pr && pr.videoDetails) {
      const d = pr.videoDetails;
      extras["youtube"] = {
        title: d.title, author: d.author, channel_id: d.channelId,
        views: d.viewCount, length_seconds: d.lengthSeconds,
        live: d.isLive, description: (d.shortDescription || "").slice(0, 3000),
        keywords: (d.keywords || []).slice(0, 20),
        thumbnail: ((d.thumbnail && d.thumbnail.thumbnails) || []).slice(-1)[0]?.url || "",
      };
      const tracks = pr.captions && pr.captions.playerCaptionsTracklistRenderer
        && pr.captions.playerCaptionsTracklistRenderer.captionTracks;
      if (Array.isArray(tracks)) {
        extras["youtube_captions"] = tracks.slice(0, 8).map((t) => ({
          language: t.languageCode, name: t.name && t.name.simpleText,
          url: t.baseUrl,
        }));
      }
    }
  } catch (e) {}
  try {
    const u = window.__UNIVERSAL_DATA_FOR_REHYDRATION__;
    if (u) {
      const scope = u.__DEFAULT_SCOPE__ || u.defaultScope || {};
      const detail = scope["webapp.video-detail"] || scope.webapp?.videoDetail;
      if (detail && detail.itemInfo && detail.itemInfo.itemStruct) {
        const it = detail.itemInfo.itemStruct;
        extras["tiktok"] = {
          id: it.id, desc: it.desc, create_time: it.createTime,
          author: it.author && {unique_id: it.author.uniqueId,
                                nickname: it.author.nickname,
                                verified: it.author.verified},
          stats: it.stats,
          music: it.music && it.music.title,
          video: it.video && {duration: it.video.duration,
                              cover: it.video.cover,
                              play_addr: it.video.playAddr},
        };
      }
    }
  } catch (e) {}
  try {
    const next = document.getElementById("__NEXT_DATA__");
    if (next && next.textContent && next.textContent.length < 120000) {
      extras["next_data"] = JSON.parse(next.textContent);
    }
  } catch (e) {}

  const jsonld = [];
  document.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
    try { const v = JSON.parse(s.textContent); jsonld.push(v); } catch (e) {}
  });

  const bodyText = (document.body && document.body.innerText) || "";
  const gated = /log in to (continue|see)|sign up to (continue|see)|you must log in|accept all cookies|before you continue to/i
    .test(bodyText.slice(0, 6000));

  return {
    title: clean(document.title),
    description: meta["og:description"] || meta["description"]
                 || meta["twitter:description"] || "",
    canonical: abs(attr('link[rel="canonical"]', "href")),
    lang: document.documentElement.lang || "",
    og: Object.fromEntries(Object.entries(meta).filter(([k]) => k.startsWith("og:"))),
    text: bodyText,
    headings: [...document.querySelectorAll("h1,h2,h3")]
      .filter((h) => !h.closest(
        'ytd-comment-thread-renderer, [class*="comment" i], [data-e2e*="comment" i]'))
      .map((h) => clean(h.innerText)).filter((s) => s && s.length < 300),
    images, videos, links, comments, extras, jsonld, gated,
    scroll_height: document.documentElement.scrollHeight || 0,
    channel: clean(txt(document.querySelector(
      '#owner #channel-name, ytd-channel-name, [data-e2e="browse-user-detail"]'))),
  };
}
"""


def _strip_html(html: str, limit: int = 2_000_000) -> str:
    """Keep the HTML evidence bounded; a feed page can be tens of megabytes."""
    if len(html) <= limit:
        return html
    return html[:limit] + "\n<!-- truncated -->\n"


def _download(url: str, dest: Path, *, timeout: float = 12.0) -> Path | None:
    """Fetch one image for the model to look at. Never raises."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8",
        "Referer": urllib.parse.urlsplit(url).scheme + "://" +
                   (urllib.parse.urlsplit(url).hostname or ""),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(THUMB_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not data or len(data) > THUMB_BYTES:
        return None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    except OSError:
        return None
    return dest


def download_image(url: str, dest: Path, *, timeout: float = 12.0) -> Path | None:
    """Fetch one image, bounded and never raising. Public wrapper for tools."""
    return _download(url, dest, timeout=timeout)


def _media_kind(url: str, mtype: str = "") -> str:
    low = (url or "").lower()
    if mtype == "image/gif" or low.split("?")[0].endswith(".gif"):
        return "gif"
    if "tenor.com" in low or "giphy.com" in low or "giphy.gif" in low:
        return "gif"
    if "i.ytimg.com" in low or "img.youtube.com" in low:
        return "thumbnail"
    return "image"


def _collect_media(raw: dict) -> tuple[list[Media], list[Media], list[Media],
                                      list[Media]]:
    """Split the extractor's media lists into images, videos, GIFs, thumbs."""
    images: list[Media] = []
    videos: list[Media] = []
    gifs: list[Media] = []
    thumbs: list[Media] = []
    for item in raw.get("images") or []:
        kind = _media_kind(item.get("url", ""))
        media = Media(kind=kind, url=item.get("url", ""),
                      alt=item.get("alt", ""), width=item.get("width") or 0,
                      height=item.get("height") or 0,
                      source=item.get("source", ""))
        (gifs if kind == "gif" else thumbs if kind == "thumbnail" else images).append(media)
    for item in raw.get("videos") or []:
        url = item.get("url", "")
        kind = "gif" if url.lower().split("?")[0].endswith(".gif") else "video"
        media = Media(kind=kind, url=url, poster=item.get("poster", ""),
                      width=item.get("width") or 0, height=item.get("height") or 0,
                      source=item.get("source", ""))
        (gifs if kind == "gif" else videos).append(media)
    return images, videos, gifs, thumbs


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def capture(
    url: str,
    outdir: Path,
    *,
    scroll: int = 2,
    wait_ms: int = 1500,
    full_page: bool = True,
    viewport: bool = True,
    click: list[str] | None = None,
    dismiss: bool = True,
    load_more: bool = True,
    include_comments: bool = True,
    max_text: int = MAX_TEXT,
    max_items: int = MAX_ITEMS,
    timeout: float = NAV_TIMEOUT,
    cookies: Path | None = None,
) -> Capture:
    """Render *url* and return what a person would see.

    Blocking: Playwright's sync API is deliberate, because every caller is
    already off the UI thread (tools run in ``asyncio.to_thread`` and the
    ``/see`` command captures in a worker). The browser is started and closed
    per call — a fresh context is what keeps one page's cookies and storage out
    of the next, and a capture is seconds, not milliseconds.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        raise BrowserError(
            "the visual browser needs Playwright — run "
            "`pip install playwright` (it is in requirements.txt)") from e

    clean_url = normalise_url(url)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cap = Capture(url=clean_url, site=urllib.parse.urlsplit(clean_url).hostname or "")
    console: list[str] = []
    started = time.monotonic()

    with sync_playwright() as p:
        browser = _launch_browser(p)
        try:
            context = browser.new_context(
                viewport=VIEWPORT,
                user_agent=USER_AGENT,
                locale="en-US",
                color_scheme="light",
                ignore_https_errors=False,
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            jar = _cookies(cookies)
            if jar:
                try:
                    context.add_cookies(jar)
                    cap.notes.append(f"{len(jar)} cookie(s) loaded")
                except Exception as e:  # noqa: BLE001
                    cap.notes.append(f"cookies rejected: {e}")
            page = context.new_page()
            page.on("console", lambda msg: console.append(msg.text)
                    if getattr(msg, "type", "") == "error" else None)
            page.on("pageerror", lambda exc: console.append(str(exc)))
            try:
                page.goto(clean_url, wait_until="domcontentloaded",
                          timeout=int(timeout))
            except Exception as e:  # noqa: BLE001
                # A slow page is not a failed page. If the document arrived,
                # continue and let the extraction decide; only a page that
                # never navigated is a hard failure.
                if page.url in ("about:blank", ""):
                    raise BrowserError(f"could not open {clean_url}: {e}") from e
                cap.notes.append(f"navigation: {str(e).splitlines()[0][:160]}")
            try:
                page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:  # noqa: BLE001 - a feed never goes idle
                pass
            if wait_ms:
                page.wait_for_timeout(int(wait_ms))
            if dismiss:
                _dismiss_overlays(page)
            # The top of the page, before scrolling: the title, hero image and
            # player are what a person sees first. The full-page shot below is
            # taken after the scroll that loads comments and lazy media.
            if viewport:
                shot = outdir / "viewport.png"
                try:
                    page.screenshot(path=str(shot), animations="disabled",
                                    caret="hide")
                    cap.screenshots.append(shot)
                except Exception as e:  # noqa: BLE001
                    cap.notes.append(f"viewport screenshot failed: {e}")
            for selector in click or []:
                try:
                    page.locator(selector).first.click(timeout=4000)
                    page.wait_for_timeout(700)
                except Exception as e:  # noqa: BLE001
                    cap.notes.append(f"click {selector!r} failed: "
                                     f"{str(e).splitlines()[0][:100]}")
            for _ in range(max(0, int(scroll))):
                try:
                    page.mouse.wheel(0, 1800)
                    page.wait_for_timeout(500)
                except Exception:  # noqa: BLE001
                    break
            if load_more and include_comments:
                _click_load_more(page)
                page.wait_for_timeout(700)

            # The full page after the scroll, so the comments and lazy media
            # have landed. The viewport shot above is the unscrolled top.
            if full_page:
                shot = outdir / "full.png"
                try:
                    height = page.evaluate(
                        "Math.min(document.documentElement.scrollHeight || 0,"
                        f" {MAX_SHOT_HEIGHT})")
                    clip = {"x": 0, "y": 0, "width": VIEWPORT["width"],
                            "height": max(VIEWPORT["height"], int(height or 0))}
                    page.screenshot(path=str(shot), full_page=True, clip=clip,
                                    animations="disabled", caret="hide")
                    cap.screenshots.append(shot)
                except Exception as e:  # noqa: BLE001
                    cap.notes.append(f"full-page screenshot failed: {e}")

            raw = page.evaluate(_EXTRACT_JS)
            html = page.content()
            cap.html_path = outdir / "page.html"
            try:
                cap.html_path.write_text(_strip_html(html), encoding="utf-8")
            except OSError:
                cap.html_path = None
            cap.final_url = page.url
            cap.title = raw.get("title") or ""
            cap.description = raw.get("description") or ""
            cap.text = raw.get("text") or ""
            cap.headings = list(dict.fromkeys(raw.get("headings") or []))
            cap.links = [(t, u) for t, u in (raw.get("links") or [])
                         if isinstance(t, str) and isinstance(u, str)][:200]
            cap.meta = raw.get("og") or {}
            cap.jsonld = [x for x in (raw.get("jsonld") or []) if isinstance(x, (dict, list))]
            cap.extras = raw.get("extras") or {}
            cap.gated = bool(raw.get("gated"))
            if raw.get("channel"):
                cap.notes.append(f"channel: {raw['channel']}")

            images, videos, gifs, thumbs = _collect_media(raw)
            cap.images = images[:max_items * 2]
            cap.videos = videos[:max_items]
            cap.gifs = gifs[:max_items]
            cap.thumbnails = thumbs[:max_items]
            if include_comments:
                cap.comments = [
                    Comment(author=str(c.get("author") or ""),
                            text=str(c.get("text") or ""),
                            likes=str(c.get("likes") or ""))
                    for c in (raw.get("comments") or [])
                    if isinstance(c, dict) and c.get("text")
                ][:max_items * 2]
            # A page that drew nothing is a finding, not an empty page. TikTok
            # in particular answers a headless browser with a shell of its own;
            # saying so stops the model from concluding the account is empty.
            if len(cap.text.strip()) < 80 and not cap.images and not cap.comments:
                cap.notes.append(
                    "the page rendered almost no content — likely a bot "
                    "challenge or login wall; cookies may be needed")
                if "tiktok.com" in (cap.site or ""):
                    cap.notes.append(
                        "TikTok served its JavaScript shell; export cookies to "
                        "~/.zimzilla/browser/cookies.json to see it as you do")
            # YouTube's own thumbnail is the single most useful picture of a
            # video, and the extractor already named it.
            yt = cap.extras.get("youtube") if isinstance(cap.extras, dict) else None
            if isinstance(yt, dict) and yt.get("thumbnail"):
                cap.thumbnails.insert(0, Media(kind="thumbnail",
                                               url=str(yt["thumbnail"]),
                                               alt=str(yt.get("title") or ""),
                                               source="youtube"))
            # Download the first few pictures so the model sees them. The
            # screenshots lead: they show the page as a human met it. The
            # budget is what is left after the screenshots, and it is enforced
            # while building the list *and* while downloading — the first
            # version checked only the first, and a YouTube page attached 37
            # thumbnails to one request.
            wanted: list[tuple[str, str]] = []
            for shot in cap.screenshots:
                cap.attachments.append(shot)
            budget = max(0, MAX_ATTACHMENTS - len(cap.attachments))
            seen_urls: set[str] = set()
            for media in [*cap.thumbnails, *cap.gifs, *cap.images]:
                if len(wanted) >= budget:
                    break
                if not media.url or media.url in seen_urls:
                    continue
                seen_urls.add(media.url)
                wanted.append((media.url, media.kind))
            for index, (media_url, kind) in enumerate(wanted, 1):
                if len(cap.attachments) >= MAX_ATTACHMENTS:
                    break
                suffix = ".gif" if kind == "gif" else ".jpg"
                dest = outdir / f"media-{index}{suffix}"
                got = _download(media_url, dest)
                if got is not None:
                    cap.attachments.append(got)
            cap.console = console[:20]
            cap.notes.append(f"rendered in {time.monotonic() - started:.1f}s")
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return cap


def _dismiss_overlays(page) -> None:
    """Click the consent button that hides the page behind a wall.

    Best-effort and bounded: one matching button, a short timeout, and every
    failure ignored. A consent wall is not a security boundary — it is a
    nuisance that covers the content the operator asked to see.
    """
    labels = ("Accept all", "Accept All", "I agree", "Agree", "Allow all",
              "Accept cookies", "OK")
    for label in labels:
        try:
            button = page.get_by_role("button", name=re.compile(
                rf"^{re.escape(label)}$", re.I))
            if button.count():
                button.first.click(timeout=1500)
                page.wait_for_timeout(500)
                return
        except Exception:  # noqa: BLE001
            continue


def _click_load_more(page) -> None:
    """Open the "more comments" controls, without following anything else."""
    try:
        page.evaluate(
            """() => {
              const rx = /(view|load|show|see)\\s+(more\\s+)?(\\d+\\s+)?(repl|comment)/i;
              let clicked = 0;
              document.querySelectorAll('button,[role="button"]').forEach((b) => {
                if (clicked >= 4) return;
                const t = (b.innerText || '').trim();
                if (t && t.length < 60 && rx.test(t)) {
                  try { b.click(); clicked++; } catch (e) {}
                }
              });
            }"""
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Video: metadata, thumbnails and frames
# ---------------------------------------------------------------------------

def _ytdlp() -> str | None:
    return shutil.which("yt-dlp")


def video_info(url: str, *, timeout: float = 90.0) -> tuple[dict | None, str]:
    """Metadata for a video URL, via yt-dlp's JSON. ``(info, error)``.

    This is the part a screenshot cannot give: the exact title, channel, view
    count, duration, description, caption tracks and every format. It works for
    YouTube, TikTok, Instagram and Facebook where the public page is enough for
    yt-dlp to resolve, and reports the extractor's own reason when it is not.
    """
    exe = _ytdlp()
    if exe is None:
        return None, "yt-dlp is not installed — install it to inspect videos"
    try:
        clean = normalise_url(url)
    except BrowserError as e:
        return None, str(e)
    cmd = [exe, "-J", "--no-warnings", "--no-playlist", "--skip-download", clean]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"yt-dlp timed out after {timeout:.0f}s"
    except OSError as e:
        return None, f"could not run yt-dlp: {e}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, (detail[-1] if detail else f"yt-dlp exited {proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None, "yt-dlp returned something that is not JSON"
    info = {
        "id": data.get("id"),
        "title": data.get("title"),
        "channel": data.get("channel") or data.get("uploader"),
        "channel_url": data.get("channel_url") or data.get("uploader_url"),
        "duration": data.get("duration"),
        "view_count": data.get("view_count"),
        "like_count": data.get("like_count"),
        "comment_count": data.get("comment_count"),
        "upload_date": data.get("upload_date"),
        "description": (data.get("description") or "")[:4000],
        "thumbnail": data.get("thumbnail"),
        "thumbnails": [t.get("url") for t in (data.get("thumbnails") or [])
                       if isinstance(t, dict)][-6:],
        "subtitles": sorted((data.get("subtitles") or {}).keys())[:12],
        "automatic_captions": sorted((data.get("automatic_captions") or {}).keys())[:12],
        "webpage_url": data.get("webpage_url") or clean,
        "extractor": data.get("extractor_key") or data.get("extractor"),
        "formats": len(data.get("formats") or []),
        "is_live": data.get("is_live"),
    }
    return info, ""


def download_thumbnail(url: str, outdir: Path) -> Path | None:
    """Save a video thumbnail so the model can look at it."""
    if not url:
        return None
    outdir = Path(outdir)
    return _download(url, outdir / "thumbnail.jpg")


def sample_frames(video_url: str, outdir: Path, *, frames: int = 4,
                  start: float = 0.0, duration: float = 20.0,
                  timeout: float = 180.0) -> tuple[list[Path], str]:
    """Download a slice of a video and return still frames from it.

    This is how "see a video" is made real without a video-capable model: the
    opening seconds are downloaded at low resolution, and a handful of frames
    are handed over as images. ``duration`` bounds both the download and the
    meaning — four frames spanning the whole of a two-hour stream would show
    nothing useful, four frames of its first twenty seconds show what it is.
    """
    exe = _ytdlp()
    ffmpeg = shutil.which("ffmpeg")
    if exe is None:
        return [], "yt-dlp is not installed"
    if ffmpeg is None:
        return [], "ffmpeg is not installed"
    try:
        clean = normalise_url(video_url)
    except BrowserError as e:
        return [], str(e)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    template = str(outdir / "video.%(ext)s")
    base = [exe, "--no-warnings", "--no-playlist", "--max-filesize", "120M",
            "-f", "bv*[height<=480]+ba/b[height<=480]/b[ext=mp4]/b",
            "-o", template]
    section = f"*{max(0.0, start):.1f}-{max(0.0, start) + max(1.0, duration):.1f}"
    attempts = [
        [*base, "--download-sections", section, "--force-keyframes-at-cuts", clean],
        [*base, clean],
    ]
    last = ""
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return [], f"download timed out after {timeout:.0f}s"
        except OSError as e:
            return [], f"could not run yt-dlp: {e}"
        if proc.returncode == 0:
            break
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        last = detail[-1] if detail else f"yt-dlp exited {proc.returncode}"
    else:
        return [], last

    files = [p for p in outdir.glob("video.*")
             if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov")]
    if not files:
        return [], "the download produced no video file"
    video = max(files, key=lambda p: p.stat().st_size)
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=20)
        total = float((probe.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        total = 0.0
    count = max(1, min(int(frames), 8))
    span = total if total > 0 else max(1.0, duration)
    points = [span * (i + 0.5) / count for i in range(count)]
    shots: list[Path] = []
    for i, at in enumerate(points, 1):
        dest = outdir / f"frame-{i}.jpg"
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{at:.2f}",
               "-i", str(video), "-frames:v", "1", "-vf",
               "scale='min(1280,iw)':-2", str(dest)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if dest.exists() and dest.stat().st_size > 0:
            shots.append(dest)
    if not shots:
        return [], "the video produced no frames"
    return shots, ""
