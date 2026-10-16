"""Regression suite for the LDXHub document gateway.

Run:  python tests/test_ldxhub.py   (from an activated venv)

Protects what is particular about this provider:

* **It is not a chat source, and the tests hold that line.** ``/v1/messages``
  and ``/v1/chat/completions`` both 404 on gw.ldxhub.io, so LDXHub can never
  become ``ANTHROPIC_BASE_URL``. ``ldxhub.py`` therefore exposes no
  ``apply_to``, no ``active_endpoint`` and no ``is_ldxhub`` predicate, and
  ``sources.py`` never mentions it. All three are asserted: a future "tidy up"
  that wires it in like openrouter would otherwise 404 every turn, silently.
* **Two catalog kinds.** Model services (structflow / analyzedoc /
  refineloop) live at ``/{service}/models``; engine services (renderocr /
  castdoc / extractdoc) at ``/{service}/engines``. The wrong kind 404s, so the
  service→kind map is asserted directly rather than trusted.
* **``example_output`` must be a native JSON object.** A string — even a
  JSON-encoded object — is rejected by the gateway with ``malformed_json``.
  The coercion is defensive and the refusal is tested.
* **``inputs`` XOR ``file_id``.** StructFlow requires exactly one; both
  together 400s. The handler picks one, and the test asserts the other key is
  absent rather than merely falsy.
* **The key gates the tools.** Six ``ldxhub_*`` schemas are withheld entirely
  when ``has_key()`` is false — not advertised-and-erroring, which would waste
  a turn on every attempt. Both halves are asserted.
* **Nothing here echoes a key.** The profile is mode 600, the token never
  leaves ``load_credentials``, and the TUI confirmation shows the last four
  characters only.

No test here touches the network: every fetch is served by a stub urlopen.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


# Shaped like the real ``GET /{service}/models`` response: a ``data`` list of
# rows with ``id`` / ``display_name`` / ``provider`` / ``description`` and
# optional ``supported_conversions`` / ``supported_languages``. The fixture
# covers the cases that matter:
#   - a row with a provider, conversions and languages  → full summary
#   - a bare row (id only)                              → label falls back to id
#   - a row with more than four conversions             → the summary elides
#   - a row with a blank id                             → skipped
#   - a non-dict row                                    → skipped
CATALOG = {
    "data": [
        {
            "id": "sf-alpha",
            "display_name": "StructFlow Alpha",
            "provider": "ldx",
            "description": "the good one",
            "supported_conversions": [{"from": "pdf", "to": "docx"}],
            "supported_languages": [{"code": "en", "display_name": "English"},
                                    {"code": "de", "display_name": "German"}],
        },
        {"id": "sf-beta"},
        {
            "id": "sf-gamma",
            "supported_conversions": [
                {"from": "a", "to": "b"}, {"from": "c", "to": "d"},
                {"from": "e", "to": "f"}, {"from": "g", "to": "h"},
                {"from": "i", "to": "j"},
            ],
        },
        {"id": "   "},
        "not-a-row",
    ]
}

#: Every env var the module reads. Saved and popped for hermetic runs.
LDXHUB_VARS = (
    "LDXHUB_API_KEY",
    "LDXHUB_STRUCTFLOW_MODEL",
    "LDXHUB_ANALYZEDOC_MODEL",
    "LDXHUB_REFINELOOP_MODEL",
    "LDXHUB_RENDEROCR_ENGINE",
    "LDXHUB_CASTDOC_ENGINE",
    "LDXHUB_EXTRACTDOC_ENGINE",
)

TOOL_NAMES = (
    "ldxhub_structflow", "ldxhub_analyze", "ldxhub_extract",
    "ldxhub_convert", "ldxhub_refine", "ldxhub_list",
)


class _Resp(io.BytesIO):
    """A urlopen() result: a context manager that also reports a status."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub(payload=None, error: Exception | None = None, raw: bytes | None = None):
    def _open(req, timeout=None):
        if error is not None:
            raise error
        if raw is not None:
            return _Resp(raw)
        return _Resp(json.dumps(payload if payload is not None else CATALOG).encode())
    return _open


def _capture(payload=None):
    """A urlopen stub that records the Request it was handed."""
    seen: dict = {}
    body = json.dumps(CATALOG if payload is None else payload).encode()

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["data"] = req.data
        return _Resp(body)

    return seen, _open


class _clean_env:
    """Remove every LDXHUB_* var for the duration, then restore it exactly."""

    def __enter__(self):
        self.saved = {v: os.environ.pop(v, None) for v in LDXHUB_VARS}
        return self

    def __exit__(self, *exc):
        for var, val in self.saved.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val
        return False


def _cfg(tmp: Path, **kw):
    from zimzilla.config import Config

    return Config.from_env(workdir=tmp, api_key="x",
                           base_url="http://localhost:4001", **kw)


def _entry(entry_id: str = "sf-alpha", **kw):
    from zimzilla.ldxhub import Entry

    kw.setdefault("label", entry_id)
    return Entry(id=entry_id, **kw)


def _capture_job():
    """Patch the tool layer's job boundary and record what a handler built.

    ``_ldx_job`` is resolved as a module global inside every handler, so
    patching it here intercepts the payload without any network.
    """
    from zimzilla import tools as t

    box: dict = {}

    def fake_job(service, payload, token, cfg, dest="", *, as_text=False):
        box["service"] = service
        box["payload"] = payload
        box["token"] = token
        box["dest"] = dest
        box["as_text"] = as_text
        return t.ToolResult("ok")

    return box, mock.patch.object(t, "_ldx_job", fake_job)


def _run(name: str, args: dict, cfg):
    from zimzilla import tools as t

    return t.execute(name, args, cfg)


# ---------------------------------------------------------------------------

def test_looks_like_key() -> None:
    """``zpka_…`` passes — the underscore openrouter's regex would reject."""
    from zimzilla import ldxhub as ldx

    check("key: a zpka_ key is accepted",
          ldx.looks_like_key("zpka_" + "a" * 32))
    check("key: dots and dashes are fine",
          ldx.looks_like_key("zpka-abc.def_" + "x" * 20))
    check("key: too short is refused", not ldx.looks_like_key("zpka_short"))
    check("key: whitespace anywhere is refused",
          not ldx.looks_like_key("zpka_" + "a" * 20 + " b"))
    check("key: a whole export line is refused",
          not ldx.looks_like_key('export LDXHUB_API_KEY="zpka_' + "a" * 20 + '"'))
    check("key: empty is refused", not ldx.looks_like_key(""))


def test_profile_roundtrip(tmp: Path) -> None:
    """save_key writes mode 600; load_credentials reads it back as 'profile'."""
    from zimzilla import ldxhub as ldx

    own = tmp / "profile" / "ldxhub" / "source"
    with _clean_env(), mock.patch.object(ldx, "PROFILE", own):
        token = "zpka_" + "b" * 30
        path = ldx.save_key(token, {"structflow": "sf-alpha"})
        check("profile: save_key returns the profile path", path == own)

        mode = stat.S_IMODE(own.stat().st_mode)
        check("profile: the file is mode 600", mode == 0o600, oct(mode))
        parent_mode = stat.S_IMODE(own.parent.stat().st_mode)
        check("profile: the directory is mode 700", parent_mode == 0o700,
              oct(parent_mode))

        creds = ldx.load_credentials()
        check("profile: the key reads back", creds is not None and creds[0] == token)
        check("profile: origin is 'profile'", creds[2] == "profile")
        check("profile: the default round-trips",
              creds[1].get("structflow") == "sf-alpha")
        check("profile: has_key is True", ldx.has_key())

        # Replacing the key must not drop the pinned default.
        token2 = "zpka_" + "c" * 30
        ldx.save_key(token2)
        again = ldx.load_credentials()
        check("profile: a new key keeps the old default",
              again[0] == token2 and again[1].get("structflow") == "sf-alpha")

        # A new default overrides, and the others survive.
        ldx.save_key(token2, {"renderocr": "ki/render"})
        final = ldx.load_credentials()
        check("profile: defaults merge rather than replace",
              final[1].get("renderocr") == "ki/render"
              and final[1].get("structflow") == "sf-alpha")

        raw = own.read_text(encoding="utf-8")
        check("profile: it is the shared export shape",
              raw.startswith('export LDXHUB_API_KEY="')
              and 'export LDXHUB_STRUCTFLOW_MODEL="sf-alpha"' in raw)


def test_read_exports_is_total(tmp: Path) -> None:
    """A missing profile is not an error — it is an empty mapping."""
    from zimzilla import ldxhub as ldx

    missing = tmp / "nope" / "source"
    check("exports: a missing file is {}", ldx._read_exports(missing) == {})

    odd = tmp / "odd.txt"
    odd.write_text('export A="1"\nnot an export\nB=2\nexport C=\'3\'\n'
                   "export D=plain\nexport =\n# export E=no\n", encoding="utf-8")
    got = ldx._read_exports(odd)
    check("exports: quoted values are unwrapped",
          got.get("A") == "1" and got.get("C") == "3")
    check("exports: an unquoted value still parses", got.get("D") == "plain")
    check("exports: a bare assignment outside an export is ignored",
          "B" not in got)
    check("exports: an empty name is skipped", "" not in got)
    check("exports: a commented-out line is ignored", "E" not in got)
    check("exports: exactly the three real lines parse", len(got) == 3, str(got))


def test_credential_precedence(tmp: Path) -> None:
    """The environment wins over the profile, and reports origin 'env'."""
    from zimzilla import ldxhub as ldx

    own = tmp / "prec" / "ldxhub" / "source"
    with _clean_env(), mock.patch.object(ldx, "PROFILE", own):
        ldx.save_key("zpka_" + "p" * 30, {"structflow": "from-profile"})

        os.environ["LDXHUB_API_KEY"] = "zpka_" + "e" * 30
        os.environ["LDXHUB_STRUCTFLOW_MODEL"] = "from-env"
        creds = ldx.load_credentials()
        check("creds: env beats the profile", creds[0] == "zpka_" + "e" * 30)
        check("creds: origin says env", creds[2] == "env")
        check("creds: the env default is read",
              creds[1].get("structflow") == "from-env")

        # A blank env value is falsy after strip, so the profile is used.
        os.environ["LDXHUB_API_KEY"] = "   "
        back = ldx.load_credentials()
        check("creds: a blank env value falls through to the profile",
              back[2] == "profile" and back[1].get("structflow") == "from-profile")


def test_no_key_is_echoed(tmp: Path) -> None:
    """With no env and no profile there is no credential at all."""
    from zimzilla import ldxhub as ldx

    with _clean_env(), mock.patch.object(ldx, "PROFILE", tmp / "absent" / "source"):
        check("creds: no key reads as None", ldx.load_credentials() is None)
        check("creds: has_key is False", not ldx.has_key())
        check("creds: save_default refuses without a key",
              ldx.save_default("structflow", "sf-alpha") is None)

        entries, err = ldx.fetch_catalog("structflow")
        check("creds: a keyless catalog fetch explains itself",
              entries is None and "/ldxhub-api-setup" in err, err)

        # A token that was never given back is never printed either.
        src = (ROOT / "zimzilla" / "ldxhub.py").read_text(encoding="utf-8")
        check("creds: the module prints no token",
              "print(" not in src and "logging" not in src)


def test_json_is_total() -> None:
    """``_json`` never raises — every failure becomes an error string."""
    from zimzilla import ldxhub as ldx

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.HTTPError("u", 401, "unauth", {}, None))):
        obj, err = ldx._json("GET", ldx.BASE_URL + "/structflow/models", "t")
        check("json: 401 is mapped, not raised",
              obj is None and "rejected" in err, err)

    body = io.BytesIO(json.dumps({"detail": "model is required"}).encode())
    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.HTTPError("u", 400, "bad", {}, body))):
        obj, err = ldx._json("POST", ldx.BASE_URL + "/structflow/jobs", "t",
                             payload={"x": 1})
        check("json: a problem+json detail is surfaced",
              obj is None and err == "model is required", err)

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("no route"))):
        obj, err = ldx._json("GET", ldx.BASE_URL + "/structflow/models", "t")
        check("json: an unreachable host names the host",
              obj is None and ldx.HOST in err, err)

    with mock.patch("urllib.request.urlopen", _stub(raw=b"<html>nope</html>")):
        obj, err = ldx._json("GET", ldx.BASE_URL + "/structflow/models", "t")
        check("json: a non-JSON body is reported as such",
              obj is None and err == "response was not JSON", err)


def test_json_sends_a_bearer_and_a_body() -> None:
    """The credential goes in the Authorization header; JSON bodies set the type."""
    from zimzilla import ldxhub as ldx

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        ldx._json("GET", ldx.BASE_URL + "/structflow/models", "tok-123")
    check("http: GET carries a bearer",
          seen["headers"].get("authorization") == "Bearer tok-123",
          str(seen["headers"]))
    check("http: a bodyless GET sends no content-type",
          "content-type" not in seen["headers"])
    check("http: a bodyless GET sends no data", seen["data"] is None)
    check("http: the method is GET", seen["method"] == "GET")

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        ldx._json("POST", ldx.BASE_URL + "/structflow/jobs", "tok-123",
                  payload={"model": "sf-alpha"})
    check("http: a JSON POST sets the content type",
          seen["headers"].get("content-type") == "application/json")
    check("http: the payload is JSON-encoded",
          json.loads(seen["data"].decode()) == {"model": "sf-alpha"})
    check("http: the method is POST", seen["method"] == "POST")


def test_parse_entries() -> None:
    """Rows parse into Entries; junk rows are skipped rather than crashing."""
    from zimzilla import ldxhub as ldx

    entries = ldx._parse_entries(CATALOG)
    check("catalog: three good rows parse", len(entries) == 3, str(len(entries)))
    by_id = {e.id: e for e in entries}
    check("catalog: a blank id is skipped", "   " not in by_id)
    check("catalog: a non-dict row is skipped",
          all(isinstance(e, ldx.Entry) for e in entries))

    alpha = by_id["sf-alpha"]
    check("catalog: display_name becomes the label",
          alpha.label == "StructFlow Alpha")
    check("catalog: provider and blurb are kept",
          alpha.provider == "ldx" and alpha.blurb == "the good one")
    check("catalog: conversions are tuples",
          alpha.conversions == (("pdf", "docx"),))
    check("catalog: languages are tuples",
          alpha.languages == (("en", "English"), ("de", "German")))

    beta = by_id["sf-beta"]
    check("catalog: a bare row falls back to the id as label", beta.label == "sf-beta")

    check("catalog: a non-dict payload is empty",
          ldx._parse_entries(["nope"]) == [])
    check("catalog: a payload with no data is empty",
          ldx._parse_entries({}) == [])


def test_entry_summary() -> None:
    """The one-line summary carries the id, the label and the shape hints."""
    from zimzilla import ldxhub as ldx

    alpha = ldx._parse_entries(CATALOG)[0]
    s = alpha.summary
    check("summary: starts with the id", s.startswith("sf-alpha"))
    check("summary: names the label", "StructFlow Alpha" in s)
    check("summary: counts languages", "[2 languages]" in s)
    check("summary: shows a conversion", "pdf→docx" in s)

    gamma = {e.id: e for e in ldx._parse_entries(CATALOG)}["sf-gamma"]
    check("summary: more than four conversions elide", "…" in gamma.summary,
          gamma.summary)
    check("summary: only four pairs are shown",
          gamma.summary.count("→") == 4, gamma.summary)

    bare = ldx.Entry(id="sf-beta", label="sf-beta")
    check("summary: a bare entry is just the id", bare.summary == "sf-beta")

    # The provider is dropped when it is already inside the id.
    dup = ldx.Entry(id="ldx/sf-alpha", label="ldx/sf-alpha", provider="ldx")
    check("summary: a redundant provider is not repeated",
          "(ldx)" not in dup.summary, dup.summary)


def test_fetch_catalog() -> None:
    """A live fetch hits the right kind path and caches the result."""
    from zimzilla import ldxhub as ldx

    ldx.clear_cache()
    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        entries, err = ldx.fetch_catalog("structflow", "tok-abc")
    check("fetch: a model service reads /models",
          seen["url"] == f"{ldx.BASE_URL}/structflow/models", seen["url"])
    check("fetch: it succeeds",
          err == "" and entries is not None and len(entries) == 3,
          f"err={err!r} n={len(entries) if entries else None}")

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        ldx.fetch_catalog("renderocr", "tok-abc")
    check("fetch: an engine service reads /engines",
          seen["url"] == f"{ldx.BASE_URL}/renderocr/engines", seen["url"])

    check("fetch: an unknown service is refused without a request",
          ldx.fetch_catalog("nope", "tok-abc")[1].startswith("unknown service"))

    # Cached: the second call must not reach the network at all.
    calls = {"n": 0}

    def counting(req, timeout=None):
        calls["n"] += 1
        return _Resp(json.dumps(CATALOG).encode())

    ldx.clear_cache()

    with mock.patch("urllib.request.urlopen", counting):
        ldx.fetch_catalog("structflow", "tok-abc")
        ldx.fetch_catalog("structflow", "tok-abc")
    check("fetch: the second call is served from cache", calls["n"] == 1,
          str(calls["n"]))

    with mock.patch("urllib.request.urlopen", counting):
        ldx.fetch_catalog("structflow", "tok-abc", use_cache=False)
    check("fetch: use_cache=False always re-fetches", calls["n"] == 2)

    ldx.clear_cache()


def test_cache_is_keyed_and_expires() -> None:
    """The cache is per-token, and an expired entry reads as absent."""
    from zimzilla import ldxhub as ldx

    ldx.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub()):
        ldx.fetch_catalog("structflow", "tok-a")
        ldx.fetch_catalog("structflow", "tok-b")
    check("cache: two tokens hold two entries",
          ("tok-a", "structflow") in ldx._CACHE
          and ("tok-b", "structflow") in ldx._CACHE)

    with _clean_env():
        os.environ["LDXHUB_API_KEY"] = "tok-a"
        check("cache: cached_catalog reads by the credential token",
              ldx.cached_catalog("structflow") is not None)
        check("cache: catalog_age is a small positive number",
              isinstance(ldx.catalog_age("structflow"), float))

        # Age the entry past the TTL: reads go None so the next action refetches.
        entries, _ts = ldx._CACHE[("tok-a", "structflow")]
        ldx._CACHE[("tok-a", "structflow")] = (
            entries, __import__("time").monotonic() - ldx.CACHE_TTL - 1.0)
        check("cache: an expired entry reads as None",
              ldx.cached_catalog("structflow") is None)
        check("cache: an expired entry still reports its age",
              ldx.catalog_age("structflow") > ldx.CACHE_TTL)

        ldx.clear_cache()
        check("cache: clear_cache empties it", ldx._CACHE == {})
        check("cache: no credentials means no cached read",
              ldx.cached_catalog("structflow") is None)

    with _clean_env(), mock.patch.object(ldx, "PROFILE", Path("/nonexistent/x")):
        check("cache: catalog_age is None without a key",
              ldx.catalog_age("structflow") is None)
    ldx.clear_cache()


def test_resolve_entry() -> None:
    """explicit → profile default → first live entry, with honest errors."""
    from zimzilla import ldxhub as ldx

    ldx.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub()):
        entry, err = ldx.resolve_entry("structflow", "sf-gamma", token="tok")
        check("resolve: an explicit id wins", err == "" and entry.id == "sf-gamma")

        entry, err = ldx.resolve_entry("structflow", "nope", token="tok")
        check("resolve: an unknown id is refused by name",
              entry is None and "not in structflow catalog" in err, err)

        entry, err = ldx.resolve_entry("structflow", token="tok")
        check("resolve: with no pin the first entry is used",
              err == "" and entry.id == "sf-alpha")

        # A pinned default is honoured when it is really in the catalog.
        creds = ("tok", {"structflow": "sf-beta"}, "profile")
        entry, err = ldx.resolve_entry("structflow", creds=creds)
        check("resolve: the pinned default is used",
              err == "" and entry.id == "sf-beta")

        # A stale pin falls back to the first entry rather than failing.
        stale = ("tok", {"structflow": "long-gone"}, "profile")
        entry, err = ldx.resolve_entry("structflow", creds=stale)
        check("resolve: a stale pin falls back to the first entry",
              err == "" and entry.id == "sf-alpha")

        entry, err = ldx.resolve_entry("structflow", token="tok")
        check("resolve: default_for is empty with no pin",
              ldx.default_for("structflow", ("tok", {}, "profile")) == "")

    ldx.clear_cache()
    with mock.patch("urllib.request.urlopen", _stub({"data": []})):
        entry, err = ldx.resolve_entry("structflow", token="tok")
        check("resolve: an empty catalog says so",
              entry is None and err == "structflow catalog is empty", err)
    ldx.clear_cache()


def test_run_job() -> None:
    """Polling walks to a terminal status; sleep is injected so nothing waits."""
    from zimzilla import ldxhub as ldx

    statuses = iter(["queued", "processing", "completed"])
    polls = {"n": 0}

    def fake_get(service, jid, token, **kw):
        polls["n"] += 1
        return {"status": next(statuses), "summary": "done"}, ""

    with mock.patch.object(ldx, "create_job", lambda s, p, t, **k: ("j1", "")), \
         mock.patch.object(ldx, "get_job", fake_get):
        job, err = ldx.run_job("structflow", {"model": "sf-alpha"}, "tok",
                               sleep=lambda _s: None)
    check("job: polling stops at the first terminal status",
          err == "" and job["status"] == "completed", err)
    check("job: it polled three times", polls["n"] == 3, str(polls["n"]))

    # A failed create short-circuits before any polling.
    def boom(*a, **k):
        raise AssertionError("should not poll after a failed create")

    with mock.patch.object(ldx, "create_job", lambda s, p, t, **k: ("", "nope")), \
         mock.patch.object(ldx, "get_job", boom):
        job, err = ldx.run_job("structflow", {}, "tok", sleep=lambda _s: None)
    check("job: a failed create returns its error", job is None and err == "nope")

    # A job that never leaves 'processing' times out, and says what it last saw.
    with mock.patch.object(ldx, "create_job", lambda s, p, t, **k: ("j2", "")), \
         mock.patch.object(ldx, "get_job",
                           lambda s, j, t, **k: ({"status": "processing"}, "")):
        job, err = ldx.run_job("structflow", {}, "tok", timeout=0.0,
                               sleep=lambda _s: None)
    check("job: a timeout reports the last status",
          "timed out" in err and "processing" in err, err)

    # A failing poll with no payload at all still returns a usable error.
    with mock.patch.object(ldx, "create_job", lambda s, p, t, **k: ("j3", "")), \
         mock.patch.object(ldx, "get_job", lambda s, j, t, **k: (None, "boom")):
        job, err = ldx.run_job("structflow", {}, "tok", timeout=0.0,
                               sleep=lambda _s: None)
    check("job: a poll failure is carried out", job is None and err == "boom")


def test_create_and_upload_errors(tmp: Path) -> None:
    """create_job and upload_file turn every failure into an error string."""
    from zimzilla import ldxhub as ldx

    with mock.patch("urllib.request.urlopen", _stub({"detail": "nope"})):
        jid, err = ldx.create_job("structflow", {"model": "x"}, "tok")
        check("create: a response with no job_id is an error",
              jid == "" and "no job_id" in err, err)

    with mock.patch("urllib.request.urlopen", _stub({"id": "j9"})):
        jid, err = ldx.create_job("structflow", {"model": "x"}, "tok")
        check("create: an `id` is accepted as a job_id", jid == "j9" and err == "")

    with mock.patch("urllib.request.urlopen", _stub({"job_id": "j10"})):
        jid, err = ldx.create_job("structflow", {"model": "x"}, "tok")
        check("create: job_id wins", jid == "j10")

    src = tmp / "doc.pdf"
    src.write_bytes(b"%PDF-1.4 tiny")

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        # The upload stub returns the catalog shape, so it has no file_id.
        fid, err = ldx.upload_file(src, "tok")
    check("upload: a response with no file_id is an error",
          fid == "" and "no file_id" in err, err)
    check("upload: it posts to /files",
          seen["url"] == f"{ldx.BASE_URL}/files" and seen["method"] == "POST",
          seen["url"])
    check("upload: the body is multipart with the field named `file`",
          seen["headers"].get("content-type", "").startswith("multipart/form-data; boundary=")
          and b'name="file"' in seen["data"], str(seen["headers"]))
    check("upload: the filename and bytes are in the body",
          b'filename="doc.pdf"' in seen["data"] and b"%PDF-1.4 tiny" in seen["data"])
    check("upload: a PDF is labelled as one",
          b"Content-Type: application/pdf" in seen["data"])

    with mock.patch("urllib.request.urlopen", _stub({"file_id": "f1"})):
        fid, err = ldx.upload_file(src, "tok")
        check("upload: a file_id comes back", fid == "f1" and err == "")

    fid, err = ldx.upload_file(tmp / "missing.pdf", "tok")
    check("upload: a missing file is refused locally, before any request",
          fid == "" and "no such file" in err, err)


def test_download_file(tmp: Path) -> None:
    """A result file is written to disk; every failure is an error string."""
    from zimzilla import ldxhub as ldx

    dest = tmp / "out" / "result.docx"
    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", _stub(raw=b"DOCXBYTES")):
        err = ldx.download_file("f1", "tok", dest)
    check("download: success returns an empty error", err == "")
    check("download: the bytes are written",
          dest.read_bytes() == b"DOCXBYTES")
    check("download: the parent directory is created", dest.parent.is_dir())

    seen, opener = _capture()
    with mock.patch("urllib.request.urlopen", opener):
        ldx.download_file("f1", "tok", dest)
    check("download: it reads the content endpoint",
          seen["url"] == f"{ldx.BASE_URL}/files/f1/content", seen["url"])

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.HTTPError("u", 403, "no", {}, None))):
        err = ldx.download_file("f1", "tok", dest)
        check("download: a 403 is mapped", "rejected" in err, err)

    with mock.patch("urllib.request.urlopen",
                    _stub(error=urllib.error.URLError("down"))):
        err = ldx.download_file("f1", "tok", dest)
        check("download: an unreachable host is reported",
              "could not fetch result" in err, err)

    # An unwritable destination is a clean error, not a traceback.
    with mock.patch("urllib.request.urlopen", _stub(raw=b"x")):
        err = ldx.download_file("f1", "tok", Path("/proc/nope/out.bin"))
    check("download: an unwritable dest is reported",
          "could not write" in err, err)


def test_job_output() -> None:
    """Every terminal job shape renders to the right (inline, file, error)."""
    from zimzilla import ldxhub as ldx

    inline, fid, err = ldx.job_output({"status": "failed", "error": "bad model"})
    check("output: a failed job reports its problem",
          inline is None and err == "job failed: bad model", err)

    inline, fid, err = ldx.job_output({"status": "completed", "summary": "all good"})
    check("output: a bare summary is returned as text",
          inline == "all good" and err == "")

    inline, fid, err = ldx.job_output(
        {"status": "completed", "summary": "s", "results": [{"a": 1}]})
    check("output: summary and results render as JSON",
          json.loads(inline) == {"summary": "s", "results": [{"a": 1}]})

    inline, fid, err = ldx.job_output({"status": "completed", "results": [1, 2]})
    check("output: results alone render as JSON", json.loads(inline) == [1, 2])

    inline, fid, err = ldx.job_output({"status": "completed", "output": "text out"})
    check("output: a plain output field is returned",
          inline == "text out" and err == "")

    inline, fid, err = ldx.job_output({"status": "completed", "output_file_id": "f7"})
    check("output: a lone file id is handed back for download",
          inline is None and fid == "f7" and err == "")

    inline, fid, err = ldx.job_output({"status": "completed"})
    check("output: a job with nothing says so",
          inline is None and err == "job finished with no output", err)

    inline, fid, err = ldx.job_output("not a dict")
    check("output: a non-dict payload is refused", err == "no job payload")


def test_service_help() -> None:
    """The help text names every service the module will accept."""
    from zimzilla import ldxhub as ldx

    text = ldx.service_help()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    check("help: one line per service", len(lines) == len(ldx.SERVICES),
          str(len(lines)))
    for svc in ldx.SERVICES:
        check(f"help: names {svc}", svc in text)
    check("help: the kinds are the two the API has",
          set(ldx.SERVICES.values()) == {"models", "engines"})
    check("help: every service has a default var",
          set(ldx.DEFAULT_VAR) == set(ldx.SERVICES))


def test_tool_gating(tmp: Path) -> None:
    """The six tools are registered, ungated by permission, and key-gated."""
    from zimzilla import agent as agent_mod
    from zimzilla import ldxhub as ldx
    from zimzilla import tools as t

    check("tools: LDXHUB_TOOLS is exactly the six",
          t.LDXHUB_TOOLS == set(TOOL_NAMES), str(sorted(t.LDXHUB_TOOLS)))

    schema_names = {s["name"] for s in t.TOOL_SCHEMAS}
    for name in TOOL_NAMES:
        check(f"tools: {name} has a schema", name in schema_names)
        check(f"tools: {name} is dispatched", name in t._DISPATCH)
        check(f"tools: {name} is not permission-gated",
              not t.needs_permission(name))

    check("tools: every tool schema has a description",
          all(s.get("description") for s in t.TOOL_SCHEMAS))
    check("tools: every input_schema is an object",
          all(s["input_schema"]["type"] == "object" for s in t.TOOL_SCHEMAS))

    cfg = _cfg(tmp)
    with _clean_env(), mock.patch.object(ldx, "PROFILE", tmp / "none" / "source"):
        a = agent_mod.Agent(cfg)
        advertised = {s["name"] for s in a.tool_schemas()}
        check("gate: no key means no ldxhub schemas",
              not (advertised & set(TOOL_NAMES)),
              str(sorted(advertised & set(TOOL_NAMES))))
        reason = a._denied("ldxhub_list")
        check("gate: the refusal names the setup command",
              reason is not None and "/ldxhub-api-setup" in reason, str(reason))

    with _clean_env():
        os.environ["LDXHUB_API_KEY"] = "zpka_" + "k" * 30
        a = agent_mod.Agent(cfg)
        advertised = {s["name"] for s in a.tool_schemas()}
        check("gate: a key advertises all six",
              advertised >= set(TOOL_NAMES),
              str(sorted(set(TOOL_NAMES) - advertised)))
        check("gate: with a key the tools are not denied",
              a._denied("ldxhub_list") is None)

    # A mode that denies a normal tool must not accidentally deny these, and
    # the six must survive the mode policy untouched.
    from zimzilla.config import MODES

    for mode, policy in MODES.items():
        hit = set(policy.get("deny", set())) & set(TOOL_NAMES)
        check(f"gate: {mode} mode denies no ldxhub tool", not hit, str(sorted(hit)))


def test_structflow_inputs_xor_file(tmp: Path) -> None:
    """StructFlow takes a prompt or a file — never both, and never neither."""
    from zimzilla import ldxhub as ldx
    from zimzilla import tools as t

    cfg = _cfg(tmp)
    box, patch = _capture_job()
    with _clean_env(), \
         mock.patch.object(t, "_ldx_entry", lambda s, e="": (_entry(), "tok")), \
         mock.patch.object(t, "_ldx_upload", lambda p, c, k: "fid"), \
         patch:
        out = _run("ldxhub_structflow", {"prompt": "write a memo"}, cfg)
        check("structflow: a prompt job succeeds", not out.is_error, out.output)
        check("structflow: the service is structflow", box["service"] == "structflow")
        check("structflow: the prompt becomes an inputs array",
              box["payload"]["inputs"] == [{"role": "user", "content": "write a memo"}],
              str(box["payload"].get("inputs")))
        check("structflow: file_id is absent when a prompt is given",
              "file_id" not in box["payload"])
        check("structflow: the chosen model is sent",
              box["payload"]["model"] == "sf-alpha")
        check("structflow: a default system prompt is supplied",
              bool(box["payload"]["system_prompt"]))
        check("structflow: example_output defaults to an object",
              box["payload"]["example_output"] == {"result": ""})

        out = _run("ldxhub_structflow", {"file_path": "doc.pdf"}, cfg)
        check("structflow: a file job succeeds", not out.is_error, out.output)
        check("structflow: a file job sends file_id", box["payload"]["file_id"] == "fid")
        check("structflow: inputs is absent when a file is given",
              "inputs" not in box["payload"])

        out = _run("ldxhub_structflow", {"prompt": "x", "file_path": "doc.pdf"}, cfg)
        check("structflow: both prompt and file is refused",
              out.is_error and "exactly one" in out.output, out.output)

        out = _run("ldxhub_structflow", {}, cfg)
        check("structflow: neither prompt nor file is refused", out.is_error)

        out = _run("ldxhub_structflow",
                   {"prompt": "x", "example_output": '{"a": 1}'}, cfg)
        check("structflow: a JSON-encoded example_output string is coerced",
              not out.is_error and box["payload"]["example_output"] == {"a": 1},
              out.output)

        out = _run("ldxhub_structflow",
                   {"prompt": "x", "example_output": "[1, 2]"}, cfg)
        check("structflow: a non-object example_output is refused",
              out.is_error and "must be a JSON object" in out.output, out.output)

        out = _run("ldxhub_structflow", {"prompt": "x", "model": "nope"}, cfg)
        check("structflow: an explicit model is passed to resolve_entry",
              not out.is_error)


def test_structflow_model_selection(tmp: Path) -> None:
    """The explicit model reaches resolve_entry; a failure surfaces cleanly."""
    from zimzilla import ldxhub as ldx
    from zimzilla import tools as t

    cfg = _cfg(tmp)
    seen: dict = {}

    def fake_entry(service, explicit=""):
        seen["service"] = service
        seen["explicit"] = explicit
        return _entry("sf-gamma"), "tok"

    with _clean_env(), mock.patch.object(t, "_ldx_entry", fake_entry), \
         mock.patch.object(t, "_ldx_job", lambda *a, **k: t.ToolResult("ok")):
        _run("ldxhub_structflow", {"prompt": "x", "model": "sf-gamma"}, cfg)
    check("structflow: the service is passed through", seen["service"] == "structflow")
    check("structflow: the explicit model is passed through",
          seen["explicit"] == "sf-gamma", str(seen))

    def no_entry(service, explicit=""):
        raise t.ToolError(f"{service} catalog is empty")

    with _clean_env(), mock.patch.object(t, "_ldx_entry", no_entry):
        out = _run("ldxhub_structflow", {"prompt": "x"}, cfg)
    check("structflow: an empty catalog is a clean tool error",
          out.is_error and "catalog is empty" in out.output, out.output)


def test_analyze_extract_refine(tmp: Path) -> None:
    """The three file-in handlers each build the payload the gateway requires."""
    from zimzilla import ldxhub as ldx
    from zimzilla import tools as t

    cfg = _cfg(tmp)
    box, patch = _capture_job()
    with _clean_env(), \
         mock.patch.object(t, "_ldx_entry", lambda s, e="": (_entry("m1"), "tok")), \
         mock.patch.object(t, "_ldx_upload", lambda p, c, k: "fid"), \
         patch:
        out = _run("ldxhub_analyze",
                   {"file_path": "doc.pdf", "prompt": "find the totals"}, cfg)
        check("analyze: it succeeds", not out.is_error, out.output)
        check("analyze: the service is analyzedoc", box["service"] == "analyzedoc")
        p = box["payload"]
        check("analyze: the prompt becomes the system_prompt",
              p["system_prompt"] == "find the totals")
        check("analyze: the file is uploaded",
              p["file_id"] == "fid" and p["model"] == "m1")
        check("analyze: output_format defaults to json", p["output_format"] == "json")
        check("analyze: example_output is an object", isinstance(p["example_output"], dict))

        out = _run("ldxhub_analyze", {"file_path": "doc.pdf"}, cfg)
        check("analyze: a missing prompt is refused", out.is_error, out.output)

        out = _run("ldxhub_analyze", {"prompt": "x"}, cfg)
        check("analyze: a missing file is refused", out.is_error, out.output)

        out = _run("ldxhub_extract", {"file_path": "doc.pdf"}, cfg)
        check("extract: it succeeds", not out.is_error, out.output)
        check("extract: the service is extractdoc", box["service"] == "extractdoc")
        check("extract: the engine is sent as `engine`",
              box["payload"]["engine"] == "m1" and "model" not in box["payload"])
        check("extract: output_format defaults to text",
              box["payload"]["output_format"] == "text")
        check("extract: the result is delivered as text", box["as_text"] is True)

        out = _run("ldxhub_extract", {"file_path": "doc.pdf", "output_format": "jsonl"}, cfg)
        check("extract: an explicit format is passed through",
              box["payload"]["output_format"] == "jsonl")

        out = _run("ldxhub_extract", {}, cfg)
        check("extract: a missing file is refused", out.is_error)

        out = _run("ldxhub_refine", {"file_path": "doc.xliff"}, cfg)
        check("refine: it succeeds", not out.is_error, out.output)
        check("refine: the service is refineloop", box["service"] == "refineloop")
        check("refine: only model and file_id are sent",
              set(box["payload"]) == {"model", "file_id"}, str(sorted(box["payload"])))
        check("refine: the result is delivered as text", box["as_text"] is True)

        out = _run("ldxhub_refine", {}, cfg)
        check("refine: a missing file is refused", out.is_error)


def test_convert_modes(tmp: Path) -> None:
    """Convert picks renderocr or castdoc, and normalises the OCR languages."""
    from zimzilla import ldxhub as ldx
    from zimzilla import tools as t

    cfg = _cfg(tmp)
    box, patch = _capture_job()
    with _clean_env(), \
         mock.patch.object(t, "_ldx_entry", lambda s, e="": (_entry("ki/cast"), "tok")), \
         mock.patch.object(t, "_ldx_upload", lambda p, c, k: "fid"), \
         patch:
        out = _run("ldxhub_convert",
                   {"file_path": "scan.pdf", "mode": "castdoc", "dest": "out.docx"}, cfg)
        check("convert: castdoc succeeds", not out.is_error, out.output)
        check("convert: the service is castdoc", box["service"] == "castdoc")
        check("convert: output_format defaults to docx",
              box["payload"]["output_format"] == "docx")
        check("convert: castdoc sends no languages",
              "languages" not in box["payload"])
        check("convert: the destination is passed to the job layer",
              box["dest"] == "out.docx")

        out = _run("ldxhub_convert",
                   {"file_path": "scan.pdf", "mode": "renderocr", "dest": "out.docx"}, cfg)
        check("convert: renderocr succeeds", not out.is_error, out.output)
        check("convert: the service is renderocr", box["service"] == "renderocr")
        check("convert: languages default to ['en']",
              box["payload"]["languages"] == ["en"], str(box["payload"].get("languages")))

        out = _run("ldxhub_convert",
                   {"file_path": "scan.pdf", "mode": "renderocr", "dest": "o.docx",
                    "languages": "en, de ,fr"}, cfg)
        check("convert: a comma-separated string becomes a list",
              box["payload"]["languages"] == ["en", "de", "fr"],
              str(box["payload"].get("languages")))

        out = _run("ldxhub_convert",
                   {"file_path": "scan.pdf", "mode": "renderocr", "dest": "o.docx",
                    "languages": ["ja"]}, cfg)
        check("convert: an explicit list passes through",
              box["payload"]["languages"] == ["ja"])

        out = _run("ldxhub_convert",
                   {"file_path": "scan.pdf", "mode": "renderocr", "dest": "o.docx",
                    "languages": []}, cfg)
        check("convert: an empty list falls back to ['en']",
              box["payload"]["languages"] == ["en"])

        out = _run("ldxhub_convert",
                   {"file_path": "scan.pdf", "mode": "nope", "dest": "o.docx"}, cfg)
        check("convert: an unknown mode is refused by name",
              out.is_error and "renderocr" in out.output and "castdoc" in out.output,
              out.output)

        out = _run("ldxhub_convert", {"file_path": "scan.pdf", "mode": "castdoc"}, cfg)
        check("convert: a missing dest is refused", out.is_error, out.output)

        out = _run("ldxhub_convert", {"mode": "castdoc", "dest": "o.docx"}, cfg)
        check("convert: a missing file is refused", out.is_error)


def test_list_handler(tmp: Path) -> None:
    """The list tool reads the live catalogs and marks the pinned default."""
    from zimzilla import ldxhub as ldx
    from zimzilla import tools as t

    cfg = _cfg(tmp)
    creds = ("tok", {"structflow": "sf-beta"}, "env")

    with mock.patch.object(t, "_ldx_token", lambda: "tok"), \
         mock.patch.object(ldx, "fetch_catalog",
                           lambda s, tok="", **k: (ldx._parse_entries(CATALOG), "")), \
         mock.patch.object(ldx, "load_credentials", lambda: creds):
        out = _run("ldxhub_list", {}, cfg)
        check("list: all six services are listed", not out.is_error, out.output)
        for svc in ldx.SERVICES:
            check(f"list: {svc} appears", svc in out.output)
        check("list: the entry count is shown", "3 models" in out.output, out.output)
        check("list: the pinned default is starred",
              any("sf-beta *" in ln for ln in out.output.splitlines()), out.output)
        check("list: the star is explained", "default for that service" in out.output)

        out = _run("ldxhub_list", {"service": "structflow"}, cfg)
        check("list: a single service works", not out.is_error, out.output)
        check("list: a single service omits the legend",
              "default for that service" not in out.output)

        out = _run("ldxhub_list", {"service": "nope"}, cfg)
        check("list: an unknown service is refused by name",
              out.is_error and "unknown service" in out.output, out.output)
        check("list: the refusal lists the real services",
              all(s in out.output for s in ldx.SERVICES), out.output)

    with mock.patch.object(t, "_ldx_token", lambda: "tok"), \
         mock.patch.object(ldx, "fetch_catalog", lambda s, tok="", **k: (None, "boom")), \
         mock.patch.object(ldx, "load_credentials", lambda: creds):
        out = _run("ldxhub_list", {"service": "structflow"}, cfg)
        check("list: a catalog error is reported, not raised",
              not out.is_error and "boom" in out.output, out.output)

    # No key at all: the handler refuses before any fetch.
    with _clean_env(), mock.patch.object(ldx, "PROFILE", tmp / "none" / "source"):
        out = _run("ldxhub_list", {}, cfg)
        check("list: without a key the tool refuses and names the fix",
              out.is_error and "/ldxhub-api-setup" in out.output, out.output)


def test_deliver(tmp: Path) -> None:
    """``_ldx_deliver`` saves a file, reads one back, or explains the miss."""
    from zimzilla import ldxhub as ldx
    from zimzilla import tools as t

    cfg = _cfg(tmp)

    # Inline only: no file, nothing to download.
    out = t._ldx_deliver("hello", None, "tok", cfg)
    check("deliver: inline text is returned as-is",
          not out.is_error and out.output == "hello", out.output)

    # Nothing at all is an error the model can read.
    try:
        t._ldx_deliver(None, None, "tok", cfg)
        check("deliver: no output at all raises", False)
    except t.ToolError as e:
        check("deliver: no output at all raises", "no output" in str(e), str(e))

    # A dest writes the file and reports a relative path.
    def ok_download(file_id, token, dest, **kw):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"RESULT")
        return ""

    with mock.patch.object(ldx, "download_file", ok_download):
        out = t._ldx_deliver(None, "f1", "tok", cfg, "out/result.docx")
    check("deliver: a dest download succeeds", not out.is_error, out.output)
    check("deliver: the file landed", (tmp / "out" / "result.docx").read_bytes() == b"RESULT")
    check("deliver: the report names the relative path",
          "wrote out/result.docx" in out.output, out.output)

    # A failing download is a clean tool error naming the reason.
    with mock.patch.object(ldx, "download_file", lambda *a, **k: "key rejected (403)"):
        try:
            t._ldx_deliver(None, "f1", "tok", cfg, "out/result.docx")
            check("deliver: a failed download raises", False)
        except t.ToolError as e:
            check("deliver: a failed download raises", "download failed" in str(e), str(e))

    # as_text reads the file back inline.
    def text_download(file_id, token, dest, **kw):
        Path(dest).write_bytes(b"extracted text")
        return ""

    with mock.patch.object(ldx, "download_file", text_download):
        out = t._ldx_deliver(None, "f2", "tok", cfg, as_text=True)
    check("deliver: as_text returns the file body",
          not out.is_error and out.output == "extracted text", out.output)

    # A file with no dest and no as_text hands back the id instead of guessing.
    out = t._ldx_deliver(None, "f3", "tok", cfg)
    check("deliver: a bare file id is reported, with the hint",
          "output file id: f3" in out.output and "dest" in out.output, out.output)

    # Inline text plus a file: both are reported.
    with mock.patch.object(ldx, "download_file", ok_download):
        out = t._ldx_deliver("summary", "f4", "tok", cfg, "out/two.docx")
    check("deliver: inline text and a saved file both appear",
          "summary" in out.output and "wrote out/two.docx" in out.output, out.output)

    # _ldx_job drives the whole path from a canned terminal job.
    with mock.patch.object(ldx, "run_job",
                           lambda s, p, tok, **k: ({"status": "completed",
                                                    "summary": "done"}, "")), \
         mock.patch.object(t, "_ldx_entry", lambda s, e="": (_entry(), "tok")), \
         mock.patch.object(t, "_ldx_upload", lambda p, c, k: "fid"):
        out = _run("ldxhub_extract", {"file_path": "doc.pdf"}, cfg)
    check("deliver: an inline job result reaches the model",
          not out.is_error and "done" in out.output, out.output)

    with mock.patch.object(ldx, "run_job",
                           lambda s, p, tok, **k: ({"status": "failed",
                                                    "error": "bad input"}, "")), \
         mock.patch.object(t, "_ldx_entry", lambda s, e="": (_entry(), "tok")), \
         mock.patch.object(t, "_ldx_upload", lambda p, c, k: "fid"):
        out = _run("ldxhub_extract", {"file_path": "doc.pdf"}, cfg)
    check("deliver: a failed job becomes a tool error",
          out.is_error and "bad input" in out.output, out.output)

    with mock.patch.object(ldx, "run_job", lambda s, p, tok, **k: (None, "timed out")), \
         mock.patch.object(t, "_ldx_entry", lambda s, e="": (_entry(), "tok")), \
         mock.patch.object(t, "_ldx_upload", lambda p, c, k: "fid"):
        out = _run("ldxhub_extract", {"file_path": "doc.pdf"}, cfg)
    check("deliver: a job timeout becomes a tool error",
          out.is_error and "timed out" in out.output, out.output)


def test_upload_is_jailed(tmp: Path) -> None:
    """An upload path goes through the sandbox, like every other file tool."""
    from zimzilla import ldxhub as ldx
    from zimzilla import tools as t

    cfg = _cfg(tmp)
    loose = _cfg(tmp, unsafe=True)

    def no_entry(service, explicit=""):
        return _entry(), "tok"

    def fake_upload(path, token, **kw):
        # Records the path the sandbox actually resolved, so the assertion is
        # about the jail rather than about the network.
        return f"fid:{Path(path).name}", ""

    with mock.patch.object(t, "_ldx_entry", no_entry), \
         mock.patch.object(ldx, "upload_file", fake_upload), \
         mock.patch.object(t, "_ldx_job", lambda *a, **k: t.ToolResult("ok")):
        out = _run("ldxhub_extract", {"file_path": "../../etc/passwd"}, cfg)
        check("jail: an escaping upload path is refused",
              out.is_error and "escapes the working directory" in out.output,
              out.output)

        out = _run("ldxhub_extract", {"file_path": "not-here.pdf"}, cfg)
        check("jail: a missing file is refused before any upload",
              out.is_error and "does not exist" in out.output, out.output)

        out = _run("ldxhub_extract", {"file_path": "doc.pdf"}, cfg)
        check("jail: an in-jail file uploads",
              not out.is_error, out.output)

        # With --unsafe the jail is lifted, so an outside path that exists is
        # permitted — asserted so the flag keeps working for uploads too.
        out = _run("ldxhub_extract", {"file_path": "../../etc/hostname"}, loose)
        check("jail: --unsafe permits an outside path that exists",
              not out.is_error, out.output)

        # The jail is lifted but existence is still required — must_exist is
        # enforced by resolve_in_jail before the flag is consulted.
        out = _run("ldxhub_extract", {"file_path": "../../etc/definitely-absent"}, loose)
        check("jail: --unsafe still requires the file to exist",
              out.is_error and "does not exist" in out.output, out.output)


def test_not_a_source() -> None:
    """LDXHub is a tool surface, not an upstream — and the tests hold that."""
    from zimzilla import ldxhub as ldx
    from zimzilla import sources

    check("source: there is no apply_to", not hasattr(ldx, "apply_to"))
    check("source: there is no active_endpoint", not hasattr(ldx, "active_endpoint"))
    check("source: there is no is_ldxhub predicate", not hasattr(ldx, "is_ldxhub"))
    check("source: there is no DEFAULT_BASE_URL to switch to",
          not hasattr(ldx, "DEFAULT_BASE_URL"))

    src = (ROOT / "zimzilla" / "sources.py").read_text(encoding="utf-8")
    check("source: sources.py never mentions ldxhub", "ldxhub" not in src.lower())
    check("source: LDXHub is not a builtin key",
          "ldxhub" not in sources.BUILTIN_KEYS)
    check("source: active_key does not recognise the host",
          sources.active_key("https://gw.ldxhub.io") is None)

    # The module carries no Claude fallback (openrouter has one; this must not).
    body = (ROOT / "zimzilla" / "ldxhub.py").read_text(encoding="utf-8")
    check("source: ldxhub.py has no claude fallback",
          "claude" not in body.lower())
    check("source: it documents that it is not a chat source",
          "not a chat source" in body.lower() or "no messages" in body.lower())


def test_tui_surfaces() -> None:
    """The two commands are wired into every surface the TUI has."""
    app = (ROOT / "zimzilla" / "ui" / "app.py").read_text(encoding="utf-8")
    comp = (ROOT / "zimzilla" / "ui" / "complete.py").read_text(encoding="utf-8")

    check("tui: app.py imports the module", "from .. import ldxhub as ldx_mod" in app)
    check("tui: the setup command is dispatched",
          '"ldxhub-api-setup": lambda a: self._cmd_ldxhub_key(a)' in app)
    check("tui: the models command is dispatched",
          '"ldxhub-models": lambda a: self._cmd_ldxhub_models(a)' in app)
    check("tui: both commands are busy-safe",
          '"ldxhub-api-setup", "ldxhub-models",' in app)
    check("tui: /help lists the setup command",
          '("/ldxhub-api-setup [key]"' in app)
    check("tui: /help lists the models command",
          '("/ldxhub-models [service]"' in app)
    check("tui: the status pane shows the keyless hint",
          "no key — /ldxhub-api-setup (document jobs)" in app)
    check("tui: the fetch runs off the UI thread",
          "@work(exclusive=True, thread=True)" in app
          and "def _fetch_ldxhub_models(self, service: str) -> None:" in app)
    check("tui: the key is never echoed back",
          "…{key[-4:]}" in app or "key[-4:]" in app)

    check("tui: completion offers the setup command",
          '("/ldxhub-api-setup", "store your LDXHub API key (document jobs)")' in comp)
    check("tui: completion offers the models command",
          '("/ldxhub-models", "LDXHub models/engines — pick the default per service")' in comp)

    # The catalogs command must not pretend to be a chat-model list.
    check("tui: /ldxhub-models is not advertised as a chat model list",
          "put in /model" in app)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zim-ldx-test-"))
    # Pin the module's paths to the temp tree before anything imports it:
    # ZIMZILLA_HOME is read at import time.
    os.environ["ZIMZILLA_HOME"] = str(tmp / ".zimzilla")
    os.environ["ZIMZILLA_CLAUDE_SOURCE"] = str(tmp / "claude-source")
    for mod in [m for m in list(sys.modules) if m.startswith("zimzilla")]:
        del sys.modules[mod]

    saved = {v: os.environ.pop(v, None) for v in LDXHUB_VARS}
    try:
        test_looks_like_key()
        test_profile_roundtrip(tmp)
        test_read_exports_is_total(tmp)
        test_credential_precedence(tmp)
        test_no_key_is_echoed(tmp)
        test_json_is_total()
        test_json_sends_a_bearer_and_a_body()
        test_parse_entries()
        test_entry_summary()
        test_fetch_catalog()
        test_cache_is_keyed_and_expires()
        test_resolve_entry()
        test_run_job()
        test_create_and_upload_errors(tmp)
        test_download_file(tmp)
        test_job_output()
        test_service_help()
        test_tool_gating(tmp)
        test_structflow_inputs_xor_file(tmp)
        test_structflow_model_selection(tmp)
        test_analyze_extract_refine(tmp)
        test_convert_modes(tmp)
        test_list_handler(tmp)
        test_deliver(tmp)
        test_upload_is_jailed(tmp)
        test_not_a_source()
        test_tui_surfaces()
    finally:
        for var, val in saved.items():
            if val is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = val

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed — FAILED: "
              + ", ".join(failed))
        return 1
    print(f"{len(RESULTS)}/{len(RESULTS)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
