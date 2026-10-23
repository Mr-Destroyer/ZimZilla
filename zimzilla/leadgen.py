"""`/leadgen` — local lead generation, passive audit, and outreach drafts.

The command is a **router**, not an engine, exactly as `/osint` is. It takes a
niche and a location, opens a case directory, and hands a complete phase
sequence to the main agent as a single turn. Everything after that is the
ordinary agent loop: the same tools, the same scope guard, the same
transcript, the same Ctrl+C. Routing through the loop is what makes a run
watchable and interruptible without writing a second execution engine.

The phases are a **registry** rather than a hard-coded sequence, for the same
reason `/osint`'s kinds are: the prompt is assembled from the table, so a
phase can be added, reordered, or gated behind a key in one place, and the
tests can assert the shape of the run without running a model.

Three constraints are structural here, not just stated in the prompt:

* **Nothing is ever sent.** There is no SMTP client, no Instantly/Smartlead
  call, no dispatch function, and no dependency that could carry one. Drafts
  are files on disk; sending is a human step outside this harness. A test
  pins this, because the honest way to keep a "never sends" promise is to
  make the code incapable of it rather than to ask the model nicely.
* **The audit is read-only.** Phase 2 fetches a lead's homepage with GET/HEAD
  and reads what came back — headers, certificate, timing, fingerprint. It
  does not submit forms, log in, request admin paths, fuzz parameters, or
  scan ports. The prompt says so and this module offers no tool that could
  do otherwise.
* **Business contact details only.** Enrichment is a domain-search for a
  role address (`info@`, `contact@`). No personal social profile is scraped.

Scope enforcement is real, not advisory: phase 2 runs every lead host through
`scope.py`'s deny-list *before* the first fetch, via the `gate` subcommand in
this module, and a refused host is written down with the guard's own reason.
The bash and web-fetch tools apply the same guard independently, so the gate
is belt and braces — but it is the one that leaves a record.

No Textual, no colours, no model — the same split as `zimzilla/osint.py`, so
the whole thing is testable without a terminal or an API key.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .osint import CONFIDENCE_GUIDE, REPORT_GUIDE

# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
# Read from the environment at run time, never written down and never
# hard-coded. Each phase declares which keys it needs; a phase whose key is
# absent is *skipped and reported*, not failed — a lead run with no Places key
# still has value if the operator supplied their own list, and a run with no
# Hunter key simply has no email column.

PLACES_KEY = "GOOGLE_PLACES_API_KEY"
PAGESPEED_KEY = "PAGESPEED_API_KEY"
HUNTER_KEY = "HUNTER_API_KEY"


def env_key(name: str, environ: dict | None = None) -> str:
    """The value of *name* from the environment, or "" when unset/blank."""
    source = os.environ if environ is None else environ
    return str(source.get(name) or "").strip()


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------
# `needs` is the honest gate: a phase with a key requirement cannot run
# without it, and `plan()` says which phases are ready before a single request
# is made. `optional` marks a phase whose absence is not a failure of the run
# — enrichment is a nice-to-have, discovery is not (a run with no discovery
# has no leads at all, and the report says exactly that).


@dataclass(frozen=True)
class Phase:
    """One step of a lead-gen run."""

    name: str
    blurb: str
    body: str = ""
    needs: tuple[str, ...] = ()
    optional: bool = False


DISCOVERY_BODY = """\
Find the businesses. One endpoint, at most three queries, at most 40 leads.

The Google Places API (New) Text Search is the source:

    curl -sS 'https://places.googleapis.com/v1/places:searchText' \\
      -H "X-Goog-Api-Key: $GOOGLE_PLACES_API_KEY" \\
      -H 'Content-Type: application/json' \\
      -H 'X-Goog-FieldMask: places.displayName,places.websiteUri,places.nationalPhoneNumber,places.formattedAddress,places.primaryTypeDisplayName,places.businessStatus,places.rating,places.userRatingCount' \\
      -d '{"textQuery": "<niche> in <location>", "pageSize": 20}'

That POST goes to the *API*, not to a business. No request in this run ever
touches a lead's own server except the read-only GET in phase 2.

- Query at most three ways: "<niche> in <location>", "<niche> near
  <location>", and the niche's most common local synonym. Stop at 40 leads.
- Drop `CLOSED_PERMANENTLY` and `CLOSED_TEMPORARILY`, and anything with no
  name. **Keep leads with no website**: they are still leads, and "no site at
  all" is a finding phase 4 can draft from — a Facebook page or a directory
  listing is not a website, and it should be recorded as `site: null` with the
  listing URL in `address`.
- Deduplicate by website host first, then by phone. The same business comes
  back several times in a dense area, and three copies of one plumber in a
  report reads as three plumbers.
- Write `<case>/leads.json`: a JSON array of objects with the keys `name`,
  `site`, `phone`, `category`, `address`, `source`. Business contact details
  only — nothing about an owner, an employee, or a personal profile.
- If `<case>/leads.json` already exists when you start, the operator supplied
  it. Use it as-is, skip the API entirely, and say so in the report.
"""

SITE_AUDIT_BODY = """\
For every lead with a website, and **only** with a GET or a HEAD. One request
per check, 20-second cap, no concurrency. This phase is read-only in the sense
`/mode plan` is read-only: it cannot write to the target, authenticate to it,
submit a form, or change a byte of state on it.

**0. Gate the host before you fetch it.** Run

    {gate} <url>

and append the verdict to `<case>/skipped.jsonl` for every DENY, with the
reason the guard gave. A denied host is skipped and *reported* — never
silently dropped, and never retried with another spelling, port or scheme. If
that interpreter cannot find the module, run it from the harness's checkout
root; and if it still will not run, the bash tool's own scope guard refuses
deny-listed hosts anyway — record that the gate command was unavailable rather
than assuming the host was clean.

**1. Response and headers** — one GET, headers kept:

    curl -sS -o page.html -D headers.txt -w '%{http_code} %{time_total}\\n' \\
      --max-time 20 -A '<a real browser user-agent>' -L --max-redirs 5 <url>

Record presence *and* value for: `strict-transport-security`,
`content-security-policy`, `x-frame-options`, `x-content-type-options`,
`referrer-policy`, `permissions-policy`. Absence is the finding, not a
failure — most local-business sites have none of them.

**2. TLS** — certificate and expiry, no handshake games:

    openssl s_client -connect <host>:443 -servername <host> </dev/null 2>/dev/null \\
      | openssl x509 -noout -subject -issuer -dates

Compute the days to `notAfter`. An expired or soon-to-expire certificate is a
CONFIRMED finding with a date on it. A site that does not answer on 443 at all
is its own finding — record it and stop testing it.

**3. Redirect and canonical host** — one HEAD to `http://<host>/`, record the
`Location`. A site that serves plain http and does not redirect to https is a
finding.

**4. Load time.** Use the `%{time_total}` you already have, and label it
honestly: it is time-to-last-byte for one request from this machine, not a
field measurement. If `PAGESPEED_API_KEY` is set, one call to

    https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url=<url>&strategy=mobile&key=$PAGESPEED_API_KEY

gives the lab metrics; quote `largest-contentful-paint` and `speed-index` as
the API's own `displayValue`, and name the strategy. With no key, say
"PageSpeed skipped — no key" in the report and use the curl figure only.

**5. Fingerprint from what the response already said.** Headers: `server`,
`x-powered-by`, `via`, `cf-ray`, `x-vercel-id`, `x-shopify-stage`,
`x-litespeed-cache`, and cookie names (`PHPSESSID`, `laravel_session`,
`ASP.NET_SessionId`, `wp-settings-*`, `_shopify_s`). HTML: `<meta
name="generator">`, `wp-content/`, `wp-includes/`, `_next/static`,
`__NUXT__`, `/sites/default/files`, `squarespace`, `wix`, `joomla`. Two
independent signals before you name a CMS — one header is a guess.
This is passive: the page is already fetched. Do **not** request
`/wp-login.php`, `/admin`, `/phpmyadmin` or any path to "confirm" it. That is
scanning, and it is out of scope for this command.

Write `<case>/audit.json`: one object per lead, keyed by site, carrying the
status code, the header findings, `tls`, `timing`, `redirect`, `tech`, and the
exact curl command used. The report cites this file, so it has to be true.
"""

ENRICHMENT_BODY = """\
Attach one likely contact address per lead, from a domain search — business
contact only.

    curl -sS "https://api.hunter.io/v2/domain-search?domain=<host>&limit=5&api_key=$HUNTER_API_KEY"

- Prefer a role address (`info@`, `contact@`, `sales@`, `hello@`) over a named
  individual, even when the named one has the higher confidence score. The
  operator is writing to a business, not to a person, and a named address is
  also the one most likely to be stale or personal data they should not hold.
- Record the address, the source (`hunter`), the API's confidence, and the
  position/department when given. An address the API did not return is never
  invented — no `info@<domain>` guesses presented as found. If you record a
  guessed pattern at all, label it `UNVERIFIED — pattern, not confirmed` and
  never put it in a draft as a fact.
- One lookup per domain, not per lead: several leads may share a host.
- No scraping of personal social profiles, no people-search sites, no
  address-and-phone guessing. If the only address is a personal one, record
  `no role address found` and let the draft go to the contact form.

Write `<case>/contacts.json`: `{site: {email, confidence, position, source}}`.
"""

DRAFT_BODY = """\
One draft per lead that has at least one finding. No finding, no draft — and
the report says so rather than inventing a reason to write.

Write each draft to `<case>/drafts/<lead-slug>.md`:

    To:      <business name> — <contact email, or "contact form">
    Subject: <one line, specific, no hype>
    Finding: <the single finding this draft is about, with its evidence>
    ---
    <body, 90-120 words>
    ---
    Never sent by ZimZilla. Review it, personalise it, send it yourself.

Rules for the body — these are the difference between a useful note and spam:

- Cite **exactly one** finding, as observed: "your homepage returns no
  `Strict-Transport-Security` header" or "the certificate expires on 3
  November". Never "we found vulnerabilities", never a severity rating, never
  a scan claim that is not in `audit.json`.
- One sentence on why it matters in *their* terms — a browser warning, a
  customer seeing "Not secure". No fear, no invented breach, no claim about
  their revenue, customers or reputation.
- One sentence offering the fix. No price, no package, no urgency, no call to
  action beyond "reply if you want the details".
- Address the business, never an individual by name. No personal profile was
  read and none may be referenced.
- Match the lead's own spelling (British or American). No emoji, no
  exclamation marks, no "I hope this email finds you well".

Then stop. There is no send step, no queue, no SMTP, no Instantly or
Smartlead call — this command has no dispatch capability and must not gain
one. Sending is a human, reviewed action taken outside the harness.
"""

REPORT_BODY = """\
Write `<case>/report.md`. It opens with `## Headline` — three to six lines
saying how many businesses were found, how many had a site, the commonest
weakness, and how many drafts are ready. (The shared guide below describes the
headline as "who this target turned out to be"; for this command the target is
a *set* of businesses, so the headline is the lead set and the pattern across
it.) Then the coverage table, then these sections in order:

    1. DISCOVERY     table: business · category · site · phone
    2. SITE AUDIT    one five-line block per lead
    3. ENRICHMENT    contact address per lead, with source and confidence
    4. DRAFTS        one line per draft: file · lead · the finding it cites
    5. SKIPPED       every host the scope guard refused, and why

An empty phase keeps its heading and says `Nothing found — <why>`: no key, no
leads, no findings, or every host denied. The coverage table says `skipped (no
GOOGLE_PLACES_API_KEY)` for a phase that never ran — never `yes`.
"""

#: The registry, in run order. `PHASE_ORDER` is the order the agent works them
#: and the order the coverage table lists them, so the two cannot disagree.
PHASES: dict[str, Phase] = {
    "discovery": Phase(
        name="discovery",
        blurb="find the local businesses for the niche and location",
        body=DISCOVERY_BODY,
        needs=(PLACES_KEY,),
    ),
    "site_audit": Phase(
        name="site_audit",
        blurb="passive audit — headers, TLS, timing, fingerprint",
        body=SITE_AUDIT_BODY,
        needs=(),
    ),
    "enrichment": Phase(
        name="enrichment",
        blurb="attach a business contact address, if a key is configured",
        body=ENRICHMENT_BODY,
        needs=(HUNTER_KEY,),
        optional=True,
    ),
    "draft": Phase(
        name="draft",
        blurb="one short outreach draft per lead with a finding",
        body=DRAFT_BODY,
    ),
    "report": Phase(
        name="report",
        blurb="report.md — headline, coverage, one block per lead, gaps",
        body=REPORT_BODY,
    ),
}

PHASE_ORDER: list[str] = ["discovery", "site_audit", "enrichment", "draft", "report"]


def available(phase: Phase, environ: dict | None = None) -> tuple[bool, str]:
    """Whether *phase* can run here, and the reason either way.

    The reason is written for the coverage table, so a skip reads as
    `skipped (no GOOGLE_PLACES_API_KEY)` rather than a bare `no`.
    """
    missing = [k for k in phase.needs if not env_key(k, environ)]
    if missing:
        return False, "skipped (no " + " / ".join(missing) + ")"
    return True, "ready"


def plan(environ: dict | None = None) -> list[dict]:
    """The run's shape before it starts: one row per phase, in order."""
    rows = []
    for name in PHASE_ORDER:
        phase = PHASES[name]
        ok, reason = available(phase, environ)
        rows.append(
            {
                "phase": phase.name,
                "blurb": phase.blurb,
                "ready": ok,
                "reason": reason,
                "optional": phase.optional,
                "needs": list(phase.needs),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
# Both halves are free text: a niche is "emergency plumbers" as often as it is
# "plumber", and a location is "Austin, TX" as often as it is "Austin". So
# validation only catches what would break the run — an empty half, a half
# that is all punctuation, or one long enough to be a pasted paragraph.

MAX_FIELD = 120


def _clean(text: str) -> str:
    """Collapse whitespace and trim — the one normalisation both halves get."""
    return " ".join(str(text or "").split()).strip()


def validate(niche: str, location: str) -> tuple[bool, str]:
    """Whether a niche/location pair is runnable. Returns (ok, error)."""
    n, loc = _clean(niche), _clean(location)
    if not n:
        return False, "no niche given"
    if not loc:
        return False, "no location given"
    if len(n) > MAX_FIELD:
        return False, f"niche is too long (max {MAX_FIELD} characters)"
    if len(loc) > MAX_FIELD:
        return False, f"location is too long (max {MAX_FIELD} characters)"
    if not any(ch.isalnum() for ch in n):
        return False, "niche has no letters or digits in it"
    if not any(ch.isalnum() for ch in loc):
        return False, "location has no letters or digits in it"
    return True, ""


def normalise(niche: str, location: str) -> tuple[str, str]:
    """The pair as the run, the label and the case path all use it."""
    return _clean(niche), _clean(location)


def label(niche: str, location: str) -> str:
    """One line for the transcript: `plumbers · Austin, TX`."""
    n, loc = normalise(niche, location)
    return f"{n} · {loc}"


# ---------------------------------------------------------------------------
# Scope gate
# ---------------------------------------------------------------------------
# Phase 2 fetches hosts that nobody declared — they came out of a places API.
# That is exactly the case `out-of-scope.yaml` exists for: a lead whose site is
# on the never-touch list must be refused before the first request, and the
# refusal has to be *recorded*, because "we skipped three" and "we found
# nothing on three" are different reports.
#
# This is a thin wrapper on purpose. The polarity, the subdomain matching and
# the IP/CIDR handling all live in scope.py; duplicating any of it here would
# give the harness two answers to the same question.

_SCOPE_NAMES = {
    "allow": ("allow.yaml", "allow.yml"),
    "deny": ("out-of-scope.yaml", "out-of-scope.yml"),
}


def find_scope_files(workdir: Path | None = None,
                     home: Path | None = None) -> tuple[Path | None, Path | None]:
    """Locate `allow.yaml` and `out-of-scope.yaml`, the way the CLI does.

    Working directory first, then `$ZIMZILLA_HOME` (or `~/.zimzilla`). The
    same order `zimzilla/__main__.py` uses, so the gate answers with the file
    the running session actually loaded.
    """
    from .config import DEFAULT_HOME

    bases = [Path(workdir) if workdir is not None else Path.cwd(),
             Path(home) if home is not None else DEFAULT_HOME]
    found: list[Path | None] = []
    for kind in ("allow", "deny"):
        hit = None
        for base in bases:
            for name in _SCOPE_NAMES[kind]:
                candidate = Path(base) / name
                if candidate.exists():
                    hit = candidate
                    break
            if hit is not None:
                break
        found.append(hit)
    return found[0], found[1]


def host_of(url: str) -> str:
    """The host a URL names, lowercased and without userinfo or port."""
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "//" + text
    try:
        from urllib.parse import urlsplit

        host = urlsplit(text).hostname or ""
    except ValueError:
        return ""
    return host.lower().strip(".")


def gate(scope, url: str) -> tuple[bool, str]:
    """Whether *url* may be fetched under *scope*. Returns (allowed, reason).

    `scope` is a `zimzilla.scope.Scope` or None. No scope files at all means
    nothing is denied — the same answer the bash and web-fetch tools give, so
    the gate never refuses a host the tools would allow.
    """
    if scope is None or not getattr(scope, "loaded", False):
        return True, "no scope files"
    host = host_of(url)
    if not host:
        return False, "no host in the URL"
    return scope.allows(host)


def gate_all(scope, urls) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Split *urls* into (allowed, denied), each as (url, reason) pairs."""
    allowed: list[tuple[str, str]] = []
    denied: list[tuple[str, str]] = []
    for url in urls:
        ok, reason = gate(scope, url)
        (allowed if ok else denied).append((url, reason))
    return allowed, denied


def load_scope(allow: Path | None = None, deny: Path | None = None):
    """A `Scope` from explicit paths, or from the discovered ones."""
    from .scope import Scope

    if allow is None and deny is None:
        allow, deny = find_scope_files()
    return Scope.load(allow, deny)


# ---------------------------------------------------------------------------
# Case directory
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")

#: Drafts live here inside the case directory, and are archived beside the
#: report on a successful run — they are the point of the command, not
#: evidence to be thrown away.
DRAFTS = "drafts"


def case_slug(text: str) -> str:
    """A filesystem-safe, recognisable label. Never empty."""
    s = _SLUG_RE.sub("-", str(text or "").strip()).strip("-.")
    return (s[:64] or "target").lower()


def case_dir(cfg, niche: str, location: str) -> Path:
    """Create and return this run's working directory.

    ``<state_dir>/leadgen/<niche>-<location>-<timestamp>/`` — under state_dir
    so case files land beside sessions rather than in whatever repo the
    operator happens to be in. Created eagerly so the agent can write into it
    from its first tool call.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    n, loc = normalise(niche, location)
    d = (Path(cfg.state_dir) / "leadgen"
         / f"{case_slug(n)}-{case_slug(loc)}-{stamp}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def drafts_dir(case: Path) -> Path:
    """The drafts directory inside a case, created on demand."""
    d = Path(case) / DRAFTS
    d.mkdir(parents=True, exist_ok=True)
    return d


def draft_path(case: Path, lead_name: str) -> Path:
    """Where the draft for one lead goes: `<case>/drafts/<slug>.md`."""
    return drafts_dir(case) / f"{case_slug(lead_name)}.md"


def _inside_git_tree(path: Path, stop: Path) -> bool:
    """True if *path* sits inside a git checkout somewhere beneath *stop*.

    The same guard osint uses, for the same reason: a cloned repo can ship a
    `report.md` of its own, and archiving that instead of the run's report
    would archive the wrong file and then delete the right one.
    """
    for parent in path.parents:
        if parent == stop:
            return False
        if (parent / ".git").exists() or parent.name.endswith(".git"):
            return True
    return False


def _find_report(case: Path) -> Path | None:
    """Locate the run's report inside *case*, or None if there isn't one."""
    direct = case / "report.md"
    if direct.is_file():
        return direct
    for p in (*sorted(case.glob("*.md")), *sorted(case.glob("*/*.md"))):
        if "report" in p.name.lower() and not _inside_git_tree(p, case):
            return p
    return None


def reports_dir(cfg) -> Path:
    """Where finished reports are kept, under state_dir beside the sessions."""
    return Path(cfg.state_dir) / "reports"


def archived_drafts(cfg, case: Path) -> Path:
    """Where this case's drafts were archived: `<reports>/<case>-drafts`."""
    return reports_dir(cfg) / f"{Path(case).name}-drafts"


def finalise_case(cfg, case: Path) -> tuple[Path | None, bool]:
    """Archive the report and the drafts, then delete the working directory.

    Returns ``(report_path, cleaned)``. The contract, in order:

      * No report -> delete nothing. A run that failed or was interrupted
        keeps everything, drafts included, so the operator can see what it
        managed to do. ``(None, False)``.
      * Report found -> move it to ``<state_dir>/reports/<case-name>.md``,
        move ``drafts/`` to ``<state_dir>/reports/<case-name>-drafts/``, then
        remove the case directory — the raw API dumps and fetched HTML are
        not worth keeping, and the report cites them.

    The drafts are archived rather than deleted because they are the
    deliverable: the report says *what* was found, the drafts are what the
    operator actually sends. If either move fails, the deletion is skipped
    entirely — a tidy-up must never destroy the only copy of the work.
    """
    report = _find_report(case)
    if report is None:
        return None, False

    dest_dir = reports_dir(cfg)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{case.name}.md"
        shutil.move(str(report), str(dest))
    except OSError:
        return None, False

    drafts = Path(case) / DRAFTS
    if drafts.is_dir():
        try:
            shutil.move(str(drafts), str(archived_drafts(cfg, case)))
        except OSError:
            # The report is archived but the drafts are still in the case —
            # keep the directory rather than delete the deliverable.
            return dest, False

    try:
        shutil.rmtree(case)
    except OSError:
        return dest, False
    return dest, True


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def gate_command() -> str:
    """The exact command the agent should run to gate a URL.

    Pinned to the interpreter running the harness rather than to a bare
    `python`: the harness's own venv is the one place the package is certainly
    importable, and a run that silently gated nothing because `python` was the
    system interpreter would be worse than no gate at all.
    """
    return f"{shlex.quote(sys.executable or 'python3')} -m zimzilla.leadgen gate"


def _phase_section(index: int, phase: Phase, environ: dict | None) -> str:
    """One phase as the agent reads it, with its gate stated up front."""
    ok, reason = available(phase, environ)
    head = f"**{index}. {phase.name} — {phase.blurb}**\n"
    if not ok:
        return head + (
            f"   Status: {reason}. Do not attempt this phase and do not "
            "improvise around it: record it in the coverage table exactly as "
            "skipped, with that reason, and say what the operator would get "
            "by setting the key in `## Gaps and next steps`.\n\n"
        )
    if phase.optional:
        head += "   Status: ready (optional — a failure here does not fail the run)\n"
    body = phase.body.replace("{gate}", gate_command())
    return head + "\n" + body + "\n"


def build_prompt(niche: str, location: str, case: Path,
                 environ: dict | None = None) -> str:
    """The turn the agent actually runs.

    The phase bodies are interpolated from the registry in `PHASE_ORDER`, so
    the prompt cannot list a phase the registry does not have, and a phase
    whose key is missing is replaced by an instruction to report the skip
    rather than to work around it.
    """
    n, loc = normalise(niche, location)
    phases = "\n".join(
        _phase_section(i, PHASES[name], environ)
        for i, name in enumerate(PHASE_ORDER, 1)
    )
    header = (
        f"/leadgen — {n} in {loc}\n"
        "Legitimate outreach only: this is for businesses the operator has a "
        "real reason to contact. Work autonomously through the phases; do not "
        "ask for confirmation between steps.\n\n"
        "EVIDENCE DIRECTORY\n"
        "------------------\n"
        f"{case}\n\n"
        "Write every artefact into that directory: `leads.json`, `audit.json`, "
        "`contacts.json`, the drafts under `drafts/`, and finally "
        "`report.md`. Use absolute paths.\n\n"
        "WHAT THIS COMMAND IS NOT\n"
        "------------------------\n"
        "- It does not send anything: drafts are never sent by this command. "
        "They are files, and sending is a human, reviewed step outside the "
        "harness. There is no SMTP, no Instantly, no Smartlead, no dispatch — "
        "and you must not add one.\n"
        "- The audit is passive. GET and HEAD only, one request per check. No "
        "form submission, no login attempt, no parameter fuzzing, no port "
        "scan, no path brute force. Nothing in this run may change state on a "
        "lead's site.\n"
        "- Business contact details only. No personal social profiles, no "
        "people-search sites, no owner names.\n\n"
        "METHOD\n"
        "------\n"
        "Work the phases in order. Each one feeds the next: discovery decides "
        "who is a lead, the audit decides which lead has something worth "
        "saying, enrichment decides where to say it, and the draft is only "
        "written for a lead whose audit produced a finding.\n\n"
        f"{phases}\n"
        f"{REPORT_GUIDE}\n"
        f"{CONFIDENCE_GUIDE}\n"
        "FINISHING\n"
        "---------\n"
        "Finish by writing `report.md`, then close the turn with a short prose "
        "summary: how many businesses, how many had a site, what the commonest "
        "weakness was, how many drafts are ready, and which hosts the scope "
        "guard refused.\n"
    )
    return header


# ---------------------------------------------------------------------------
# CLI — the gate the agent runs, and the plan the operator can read
# ---------------------------------------------------------------------------
# `python -m zimzilla.leadgen gate <url>...` is what phase 2 calls before it
# fetches anything. It exists as a subcommand rather than as prose in the
# prompt so the refusal comes from the same code the tools use, carries the
# guard's own reason, and can be logged verbatim.

_USAGE = """\
usage: python -m zimzilla.leadgen gate [--deny PATH] [--allow PATH] <url>...
       python -m zimzilla.leadgen plan

gate   check URLs against out-of-scope.yaml — prints ALLOW/DENY per URL,
       exits 1 if any URL is denied
plan   which phases can run here, and which are skipped for want of a key
"""


def _cmd_gate(argv: list[str]) -> int:
    deny = allow = None
    urls: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--deny", "--allow") and i + 1 < len(argv):
            if arg == "--deny":
                deny = Path(argv[i + 1]).expanduser()
            else:
                allow = Path(argv[i + 1]).expanduser()
            i += 2
            continue
        if arg.startswith("-"):
            print(f"unknown option: {arg}", file=sys.stderr)
            return 2
        urls.append(arg)
        i += 1

    if not urls:
        print(_USAGE, file=sys.stderr)
        return 2

    scope = load_scope(allow, deny)
    allowed, denied = gate_all(scope, urls)
    for url, reason in allowed:
        print(f"ALLOW  {url} — {reason}")
    for url, reason in denied:
        print(f"DENY   {url} — {reason}")
    return 1 if denied else 0


def _cmd_plan() -> int:
    rows = plan()
    for row in rows:
        mark = "ready" if row["ready"] else "skip "
        print(f"{mark}  {row['phase']:<11} {row['reason']:<40} {row['blurb']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0
    verb, rest = args[0], args[1:]
    if verb == "gate":
        return _cmd_gate(rest)
    if verb == "plan":
        return _cmd_plan()
    print(f"unknown subcommand: {verb}\n\n{_USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
