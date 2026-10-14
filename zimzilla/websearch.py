"""Web search and page fetch — the tools the model uses to learn something
that is not in the repository.

Search is a *chain* of key-free backends rather than a single engine, and the
reason for that is measured rather than defensive. DuckDuckGo Lite used to be
the only backend; from this box it answers exactly two queries and then serves
an HTTP 202 anti-bot interstitial — ``cc=botnet``, a form posting to
``anomaly.js`` — in place of a results page. That is a rate limit, but a 202 is
a 2xx, so ``urllib`` reported success, the parser found no ``result-link``, and
the operator was told the results page format had changed. That was false, and
intermittent with it, because the first two searches of a session worked.

Both halves of that are fixed here, and both are needed:

* **A blocked backend must say so.** The interstitial is detected by its own
  signature and reported as a rate limit. It is never reported as a format
  change, because it is not one.
* **One engine cannot carry search.** Three more key-free backends are tried in
  order — 4get, Mwmbl, RightDao — and the first that answers wins. All four were
  measured answering from this IP on a day DDG was refusing; every one of them
  parses with the standard library alone, so no network-facing dependency enters
  the hot path of every session.

The order is overridable with ``ZIMZILLA_SEARCH_BACKENDS`` (comma-separated
names) because each of these is somebody else's free service, and which one is
down this month is not something this code can predict.

The markup each backend parses is deliberately the plainest each service has to
offer. All of it is still somebody else's output, so a shape that is not
recognised raises rather than being read as an empty result set — the
distinction the original DDG-only code got wrong.

Scope: fetching an arbitrary URL is reaching a host, so ``fetch()`` checks the
session's scope guard and refuses out-of-scope targets. Searching is egress to
a fixed engine rather than a chosen target, so it is allowed whenever the
session authorises any network at all — see the callers in ``tools.py``.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser

# DDG Lite answers a bare urllib UA with an empty page, so present as a browser.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: The key-free endpoints, tried in whatever order ``_backend_order`` returns.
_DDG_ENDPOINT = "https://lite.duckduckgo.com/lite/"
_FOURGET_ENDPOINT = "https://4get.zzls.xyz/api/v1/web?s="
_MWMBL_ENDPOINT = "https://mwmbl.org/api/v1/search/?s="
_RIGHTDAO_ENDPOINT = "https://rightdao.com/search?q="

#: What DDG's anti-bot interstitial actually served: a 202, a ``cc=botnet``
#: query parameter, and a form posting to ``anomaly.js``. The status and the
#: markers are both checked — the status alone is a convention that could move,
#: and the markers alone would miss a page whose body had been rewritten.
_DDG_CHALLENGE_MARKERS = (
    "cc=botnet", "anomaly.js", "challenge-form", "anomaly-modal",
)

_FETCH_LIMIT_BYTES = 2_000_000  # a page larger than this is not a document


@dataclass
class Result:
    """One search hit."""

    title: str
    url: str
    snippet: str = ""


class WebError(Exception):
    """A search or fetch that could not be completed."""


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def _decompress(raw: bytes, encoding: str) -> bytes:
    """Undo the content-encoding a server applied despite our identity request."""
    encoding = (encoding or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        if "deflate" in encoding:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        # A body we cannot decode is not worth failing the whole call over —
        # fall through and let the parser make what it can of the raw bytes.
        return raw
    return raw


def _request(url: str, *, data: bytes | None = None, timeout: float = 15.0):
    """GET or POST *url*, returning (status, final_url, content_type, text).

    The status is returned rather than assumed to be 200. ``urlopen`` raises
    only for a 4xx/5xx, so every other status — including the 202 that DDG
    serves in place of a results page when it is rate-limiting — arrives here
    looking like a success. Handing the caller the number is what lets it tell
    the two apart; without it a challenge page is indistinguishable from a
    results page that happened to parse to nothing.
    """
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", _USER_AGENT)
    req.add_header("Accept-Encoding", "identity")
    req.add_header("Accept", "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(_FETCH_LIMIT_BYTES)
            raw = _decompress(raw, resp.headers.get("Content-Encoding", ""))
            charset = resp.headers.get_content_charset() or "utf-8"
            ctype = resp.headers.get_content_type() or "text/html"
            status = getattr(resp, "status", None) or resp.getcode() or 200
            return int(status), resp.geturl(), ctype, raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        raise WebError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}") from e
    except urllib.error.URLError as e:
        raise WebError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise WebError(f"request failed: {e}") from e


# ---------------------------------------------------------------------------
# DDG Lite parsing
# ---------------------------------------------------------------------------

class _ResultParser(HTMLParser):
    """Pull ``result-link`` anchors and ``result-snippet`` cells, in order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []  # (url, title)
        self.snippets: list[str] = []
        self._link: tuple[str, str] | None = None
        self._snippet: list[str] | None = None

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        for name, value in attrs:
            if name == "class" and value:
                return set(value.split())
        return set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "a" and "result-link" in classes:
            href = next((v for n, v in attrs if n == "href" and v), "")
            self._link = (href, "")
        elif tag == "td" and "result-snippet" in classes:
            self._snippet = []

    def handle_data(self, data: str) -> None:
        if self._link is not None:
            url, title = self._link
            self._link = (url, title + data)
        if self._snippet is not None:
            self._snippet.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            url, title = self._link
            self.links.append((url, " ".join(title.split())))
            self._link = None
        elif tag == "td" and self._snippet is not None:
            self.snippets.append(" ".join("".join(self._snippet).split()))
            self._snippet = None


def _unwrap_ddg_url(url: str) -> str:
    """DDG sometimes wraps hits in a ``/l/?uddg=`` redirect — show the target."""
    parts = urllib.parse.urlsplit(url)
    if parts.path.startswith("/l/") or "uddg=" in parts.query:
        target = urllib.parse.parse_qs(parts.query).get("uddg")
        if target:
            return target[0]
    if url.startswith("//"):
        return "https:" + url
    return url


def parse_results(html: str) -> list[Result]:
    """Extract hits from a DDG Lite results page. Order-preserving."""
    parser = _ResultParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML must not sink the search
        pass

    results: list[Result] = []
    for i, (url, title) in enumerate(parser.links):
        snippet = parser.snippets[i] if i < len(parser.snippets) else ""
        url = _unwrap_ddg_url(url)
        if not url.startswith(("http://", "https://")):
            continue
        if not title:
            continue
        results.append(Result(title=title, url=url, snippet=snippet))
    return results


def ddg_is_challenged(status: int, text: str) -> bool:
    """Is this DDG's anti-bot interstitial rather than a results page?

    Measured: after two queries from one IP, DDG Lite answers **202** with a
    page whose form posts to ``anomaly.js`` and whose query string carries
    ``cc=botnet``. 202 is a 2xx, so nothing raised; the parser found no
    ``result-link`` and the old code blamed the page format. Checking the
    signature here is what turns that into a true statement about a rate limit.
    """
    if status == 202:
        return True
    if status != 200:
        return False
    lowered = text.lower()
    return any(m in lowered for m in _DDG_CHALLENGE_MARKERS)


# -- 4get -------------------------------------------------------------------
# JSON. {"status": "ok", "web": [{"title", "description", "url", ...}, ...]}
# A non-"ok" status is the instance saying so — reported, not parsed around.

def parse_4get(payload: str) -> list[Result]:
    """Extract hits from a 4get ``/api/v1/web`` JSON response."""
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("4get: expected a JSON object")
    status = str(data.get("status", "")).lower()
    if status != "ok":
        raise ValueError(f"4get: instance returned status {status or '<missing>'!r}")
    hits = data.get("web")
    if not isinstance(hits, list):
        raise ValueError("4get: no 'web' list in the response")

    results: list[Result] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url = str(hit.get("url") or "").strip()
        title = str(hit.get("title") or "").strip()
        if not url.startswith(("http://", "https://")) or not title:
            continue
        results.append(Result(
            title=" ".join(title.split()),
            url=url,
            snippet=" ".join(str(hit.get("description") or "").split()),
        ))
    return results


# -- Mwmbl ------------------------------------------------------------------
# A bare JSON LIST at the root. ``title`` and ``extract`` are run-lists of
# {"value", "is_bold"} — not strings — so they are joined rather than used
# directly. Reading them as strings is the one way to get this wrong quietly.

def _mwmbl_runs(value) -> str:
    """Join Mwmbl's rich-text runs into plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [str(run.get("value", "")) for run in value if isinstance(run, dict)]
        return " ".join("".join(parts).split())
    return ""


def parse_mwmbl(payload: str) -> list[Result]:
    """Extract hits from an Mwmbl ``/api/v1/search`` JSON response."""
    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError("mwmbl: expected a JSON list at the root")

    results: list[Result] = []
    for hit in data:
        if not isinstance(hit, dict):
            continue
        url = str(hit.get("url") or "").strip()
        title = _mwmbl_runs(hit.get("title"))
        if not url.startswith(("http://", "https://")) or not title:
            continue
        results.append(Result(
            title=title, url=url, snippet=_mwmbl_runs(hit.get("extract")),
        ))
    return results


# -- RightDao ---------------------------------------------------------------
# HTML. Each hit is a ``<div class="title">`` holding the anchor, followed by a
# ``<div class="description">`` holding the snippet — so the two are paired by
# order, exactly as DDG's link/snippet cells are.

class _RightDaoParser(HTMLParser):
    """Pull RightDao's ``div.title`` anchors and ``div.description`` text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.snippets: list[str] = []
        self._depth = 0          # nesting inside a tracked div
        self._want = ""          # "title" | "description" | ""
        self._href = ""
        self._buf: list[str] = []
        self._in_anchor = False

    @staticmethod
    def _classes(attrs) -> set[str]:
        for name, value in attrs:
            if name == "class" and value:
                return set(value.split())
        return set()

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._want:
            self._depth += 1
            if tag == "a" and self._want == "title" and not self._href:
                self._href = next((v for n, v in attrs if n == "href" and v), "")
                self._in_anchor = True
            return
        classes = self._classes(attrs)
        if tag == "div" and ("title" in classes or "description" in classes):
            self._want = "title" if "title" in classes else "description"
            self._depth = 1
            self._href = ""
            self._buf = []
            self._in_anchor = False

    def handle_data(self, data: str) -> None:
        if self._want == "description":
            self._buf.append(data)
        elif self._want == "title" and self._in_anchor:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._want:
            return
        if tag == "a":
            self._in_anchor = False
        self._depth -= 1
        if self._depth > 0:
            return
        text = " ".join("".join(self._buf).split())
        if self._want == "title":
            self.links.append((self._href, text))
        else:
            self.snippets.append(text)
        self._want = ""
        self._buf = []
        self._href = ""


def parse_rightdao(html: str) -> list[Result]:
    """Extract hits from a RightDao results page. Order-preserving."""
    parser = _RightDaoParser()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - see parse_results
        pass

    results: list[Result] = []
    for i, (url, title) in enumerate(parser.links):
        snippet = parser.snippets[i] if i < len(parser.snippets) else ""
        if not url.startswith(("http://", "https://")) or not title:
            continue
        results.append(Result(title=title, url=url, snippet=snippet))
    return results


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def _search_ddg(query: str, timeout: float) -> list[Result]:
    """DuckDuckGo Lite, over POST."""
    body = urllib.parse.urlencode({"q": query}).encode("utf-8")
    status, _, ctype, text = _request(_DDG_ENDPOINT, data=body, timeout=timeout)

    if ddg_is_challenged(status, text):
        raise WebError(
            "duckduckgo is rate-limiting this IP (HTTP "
            f"{status} anti-bot page) — trying another backend"
        )
    results = parse_results(text)
    if not results and "text/html" in ctype and "result-link" not in text:
        raise WebError("duckduckgo returned no parsable results — "
                       "the results page format may have changed")
    return results


def _search_4get(query: str, timeout: float) -> list[Result]:
    """4get, a key-free metasearch API returning JSON."""
    url = _FOURGET_ENDPOINT + urllib.parse.quote_plus(query)
    _, _, _, text = _request(url, timeout=timeout)
    try:
        return parse_4get(text)
    except (ValueError, json.JSONDecodeError) as e:
        raise WebError(f"4get returned an unrecognised response: {e}") from e


def _search_mwmbl(query: str, timeout: float) -> list[Result]:
    """Mwmbl, a key-free community index returning JSON."""
    url = _MWMBL_ENDPOINT + urllib.parse.quote_plus(query)
    _, _, _, text = _request(url, timeout=timeout)
    try:
        return parse_mwmbl(text)
    except (ValueError, json.JSONDecodeError) as e:
        raise WebError(f"mwmbl returned an unrecognised response: {e}") from e


def _search_rightdao(query: str, timeout: float) -> list[Result]:
    """RightDao, key-free HTML results. Its parameter is ``q``, not ``query``."""
    url = _RIGHTDAO_ENDPOINT + urllib.parse.quote_plus(query)
    _, _, _, text = _request(url, timeout=timeout)
    results = parse_rightdao(text)
    if not results and "description" not in text:
        raise WebError("rightdao returned no parsable results — "
                       "the results page format may have changed")
    return results


#: name -> the callable that queries it. Each raises WebError on its own
#: failure so the chain can move on without knowing which kind it was.
_BACKENDS = {
    "duckduckgo": _search_ddg,
    "4get": _search_4get,
    "mwmbl": _search_mwmbl,
    "rightdao": _search_rightdao,
}

#: The order tried when nothing overrides it. DDG leads because its results are
#: the best of the four when it answers — the point of the chain is that a
#: refusal there is no longer the end of the search.
_DEFAULT_BACKEND_ORDER = ("duckduckgo", "4get", "mwmbl", "rightdao")


def _backend_order() -> list[str]:
    """The backends to try, in order.

    ``ZIMZILLA_SEARCH_BACKENDS=4get,mwmbl`` pins the order, because these are
    other people's free services and which one is refusing traffic this month
    is not something this code can know. Unknown names are ignored rather than
    fatal: a typo should not leave the operator with no search at all.
    """
    raw = (os.environ.get("ZIMZILLA_SEARCH_BACKENDS") or "").strip()
    if not raw:
        return list(_DEFAULT_BACKEND_ORDER)
    named = [p.strip().lower() for p in raw.split(",") if p.strip()]
    ordered = [n for n in named if n in _BACKENDS]
    return ordered or list(_DEFAULT_BACKEND_ORDER)


def search(query: str, max_results: int = 5,
           timeout: float = 15.0) -> tuple[list[Result], str]:
    """Search the web across the backend chain.

    Returns ``(results, backend_name)`` for the first backend that produced a
    usable answer — the name so the caller can say which engine answered, since
    they are not interchangeable and the operator may want to pin one.

    An empty list with a backend name means that backend genuinely had no hits.
    Every backend failing raises ``WebError`` naming each one and why, so a
    total failure is diagnosable rather than a shrug.
    """
    query = (query or "").strip()
    if not query:
        raise WebError("empty query")

    order = _backend_order()
    failures: list[str] = []
    for name in order:
        try:
            results = _BACKENDS[name](query, timeout)
        except WebError as e:
            failures.append(f"{name}: {e}")
            continue
        # An empty answer is still an answer: this backend worked and found
        # nothing. Falling through would ask four services the same question
        # to be told "nothing" four times.
        return results[: max(1, max_results)], name

    raise WebError("every search backend failed — " + "; ".join(failures))


class _TextExtractor(HTMLParser):
    """Turn a document into readable text: drop chrome, keep the prose.

    With ``region="main"`` only the inside of ``<main>``/``<article>`` is
    captured — documentation and article pages put their prose there and their
    navigation outside it, which is the difference between reading a page and
    reading its menu. Pages without either element yield nothing in that mode,
    so the caller falls back to the whole document.
    """

    _DROP = {
        "script", "style", "noscript", "svg", "head", "template", "iframe",
        "nav", "aside", "footer", "form", "button", "select",
    }
    _BLOCK = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "header", "blockquote", "pre", "table",
    }
    _REGION = {"main", "article"}

    def __init__(self, region: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.region = region
        self._out: list[str] = []
        self._drop_depth = 0
        self._region_depth = 0

    def _capturing(self) -> bool:
        if self._drop_depth:
            return False
        return self.region != "main" or self._region_depth > 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._DROP:
            self._drop_depth += 1
            return
        if self.region == "main" and tag in self._REGION:
            self._region_depth += 1
            if self._region_depth == 1:
                self._out.append("\n")
            return
        if self._capturing() and tag in self._BLOCK:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROP:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self.region == "main" and tag in self._REGION:
            if self._region_depth:
                self._region_depth -= 1
            if self._region_depth == 0:
                self._out.append("\n")
            return
        if self._capturing() and tag in self._BLOCK:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._capturing():
            self._out.append(data)

    def text(self) -> str:
        joined = "".join(self._out)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)
        return joined.strip()


def _extract(html: str, region: str | None) -> str:
    extractor = _TextExtractor(region=region)
    try:
        extractor.feed(html)
    except Exception:  # noqa: BLE001 - see parse_results
        pass
    return extractor.text()


# Below this, a "main region" is not a document — it is a stub or a mis-detect,
# and the whole page is the better answer.
_MAIN_MIN_CHARS = 200


def html_to_text(html: str) -> str:
    """Readable text from an HTML document, without a parser dependency."""
    main = _extract(html, "main")
    if len(main) >= _MAIN_MIN_CHARS:
        return main
    return _extract(html, None)


def fetch(url: str, max_chars: int = 20_000, timeout: float = 20.0) -> tuple[str, str]:
    """Fetch *url* as text. Returns (final_url, text). Raises WebError."""
    url = (url or "").strip()
    if not url:
        raise WebError("empty url")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    _, final_url, ctype, body = _request(url, timeout=timeout)

    if "html" in ctype or "xml" in ctype:
        text = html_to_text(body)
    elif ctype.startswith("text/") or "json" in ctype:
        text = body
    else:
        raise WebError(f"{ctype} is not readable text (only text and HTML pages)")

    if not text.strip():
        raise WebError(f"{final_url} returned no readable text")

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"
    return final_url, text
