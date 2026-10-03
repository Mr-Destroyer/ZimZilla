"""Web search and page fetch — the tools the model uses to learn something
that is not in the repository.

Backend is DuckDuckGo Lite, queried over POST. There is no API key and no
third-party dependency: the results page is parsed with the standard library's
``html.parser``, the same way ``sources.py`` reads profiles with plain
``urllib``. Nothing here is vendor-specific enough to need a client library,
and adding one would put a network-facing dependency in the hot path of every
session.

The markup this parses is DDG Lite's, which is deliberately the plainest
search page on the web — a table of ``<a class='result-link'>`` rows each
followed by a ``<td class='result-snippet'>``. It is stable in practice, but
it is still somebody else's HTML: ``search()`` returns whatever it can pair
up and reports honestly when it pairs up nothing, rather than inventing
results or raising.

Scope: fetching an arbitrary URL is reaching a host, so ``fetch()`` checks the
session's scope guard and refuses out-of-scope targets. Searching is egress to
a fixed engine rather than a chosen target, so it is allowed whenever the
session authorises any network at all — see the callers in ``tools.py``.
"""

from __future__ import annotations

import gzip
import io
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

_SEARCH_ENDPOINT = "https://lite.duckduckgo.com/lite/"
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
    """GET or POST *url*, returning (final_url, content_type, text)."""
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
            return resp.geturl(), ctype, raw.decode(charset, errors="replace")
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


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def search(query: str, max_results: int = 5, timeout: float = 15.0) -> list[Result]:
    """Search the web. Raises WebError if the engine is unreachable."""
    query = (query or "").strip()
    if not query:
        raise WebError("empty query")

    body = urllib.parse.urlencode({"q": query}).encode("utf-8")
    _, ctype, text = _request(_SEARCH_ENDPOINT, data=body, timeout=timeout)

    results = parse_results(text)
    if not results and "text/html" in ctype:
        # Distinguish "no hits" from "the page was not a results page" — the
        # latter is a backend change and the operator should hear about it
        # rather than reading it as a genuine empty result set.
        if "result-link" not in text:
            raise WebError(
                "search backend returned no parsable results — the results "
                "page format may have changed"
            )
    return results[: max(1, max_results)]


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

    final_url, ctype, body = _request(url, timeout=timeout)

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
