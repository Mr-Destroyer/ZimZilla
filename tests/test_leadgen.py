"""Regression suite for `/leadgen`.

Run:  python tests/test_leadgen.py   (from an activated venv)

Two layers, matching the split in zimzilla/leadgen.py itself:

* the pure logic — the phase registry, the key gating, input validation, the
  case directory, the scope gate, the archive-and-clean lifecycle, and the
  prompt — exercised directly, with no terminal, no model and **no network**.
  Every API call the playbook describes is the agent's job in the turn, so
  nothing here needs a Places or Hunter key to pass;
* the wiring — that the slash dispatch, help table, palette and completion
  menu all expose the command, so a rename in one place cannot orphan it in
  another.

The one thing this file asserts about *absence* is deliberate: `/leadgen` must
never grow a send capability. That is checked against the source, because the
promise is structural — drafts are files, and the module has no way to post
them anywhere.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from zimzilla import leadgen  # noqa: E402
from zimzilla.config import Config  # noqa: E402
from zimzilla.scope import Scope  # noqa: E402
from zimzilla.ui.app import ZimZillaApp  # noqa: E402
from zimzilla.ui.complete import SLASH_COMMANDS  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {extra}")


def _cfg(workdir: Path, **kw) -> Config:
    return Config.from_env(workdir=workdir, api_key="x",
                           base_url="http://localhost:4001", **kw)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry() -> None:
    # The five phases the operator named, in run order. A missing entry here
    # is a missing section in the prompt and a missing row in the coverage
    # table, so the set and the order are both pinned.
    want = ["discovery", "site_audit", "enrichment", "draft", "report"]
    check("registry: all five phases exist",
          set(leadgen.PHASES) == set(want), f"{sorted(leadgen.PHASES)}")
    check("registry: PHASE_ORDER is the run order",
          leadgen.PHASE_ORDER == want, f"{leadgen.PHASE_ORDER}")

    # Names are unique keys with non-empty blurbs and bodies, so the menu and
    # the prompt both render. `report` is last by contract: the run ends by
    # writing it.
    for name, phase in leadgen.PHASES.items():
        check(f"registry: '{name}' is well-formed",
              phase.name == name and bool(phase.blurb) and bool(phase.body))
    check("registry: report is the last phase",
          leadgen.PHASE_ORDER[-1] == "report", leadgen.PHASE_ORDER[-1])

    # The key gates are declared on the registry, not hard-coded at the call
    # site — that is what makes `plan()` able to answer before a run starts.
    check("registry: discovery needs the places key",
          leadgen.PHASES["discovery"].needs == (leadgen.PLACES_KEY,),
          f"{leadgen.PHASES['discovery'].needs}")
    check("registry: enrichment needs the hunter key",
          leadgen.PHASES["enrichment"].needs == (leadgen.HUNTER_KEY,),
          f"{leadgen.PHASES['enrichment'].needs}")
    check("registry: enrichment is the optional phase",
          leadgen.PHASES["enrichment"].optional
          and not leadgen.PHASES["site_audit"].optional
          and not leadgen.PHASES["report"].optional)
    check("registry: the audit and the report need no key",
          leadgen.PHASES["site_audit"].needs == ()
          and leadgen.PHASES["draft"].needs == ()
          and leadgen.PHASES["report"].needs == ())

    # No phase may carry a send step. The registry is prose, so this is the
    # only place a "and then email them" sentence could sneak in — the draft
    # phase names SMTP and the bulk senders precisely to forbid them, so what
    # is checked here is the mechanism, not the mention.
    for name, phase in leadgen.PHASES.items():
        low = phase.body.lower()
        check(f"registry: '{name}' has no send mechanism",
              "smtplib" not in low and "sendmail(" not in low
              and "requests.post" not in low)
    check("registry: the draft phase forbids sending explicitly",
          "Never sent by ZimZilla" in leadgen.PHASES["draft"].body
          and "no send step" in leadgen.PHASES["draft"].body)


def test_key_gating() -> None:
    # An empty environment skips the two keyed phases and runs the other
    # three. This is the "say so and skip rather than fail" contract.
    empty = {}
    ok, reason = leadgen.available(leadgen.PHASES["discovery"], empty)
    check("gating: discovery is skipped with no key", not ok, reason)
    check("gating: the skip reason names the key",
          leadgen.PLACES_KEY in reason, reason)
    check("gating: the skip reason reads as skipped",
          reason.startswith("skipped"), reason)

    ok, reason = leadgen.available(leadgen.PHASES["enrichment"], empty)
    check("gating: enrichment is skipped with no key", not ok, reason)
    check("gating: enrichment names its own key", leadgen.HUNTER_KEY in reason)

    for name in ("site_audit", "draft", "report"):
        ok, reason = leadgen.available(leadgen.PHASES[name], empty)
        check(f"gating: {name} runs without any key", ok, reason)

    # A key present (even as a blank) is the only thing that flips it, and a
    # whitespace-only value is not a key.
    ok, _ = leadgen.available(leadgen.PHASES["discovery"],
                              {leadgen.PLACES_KEY: "k"})
    check("gating: discovery runs with a key", ok)
    ok, _ = leadgen.available(leadgen.PHASES["discovery"],
                              {leadgen.PLACES_KEY: "   "})
    check("gating: a blank key does not count", not ok)

    # plan() is the whole shape, in order, with a reason per phase.
    rows = leadgen.plan(empty)
    check("gating: plan lists every phase in order",
          [r["phase"] for r in rows] == leadgen.PHASE_ORDER,
          f"{[r['phase'] for r in rows]}")
    check("gating: plan marks exactly two phases skipped",
          [r["phase"] for r in rows if not r["ready"]]
          == ["discovery", "enrichment"],
          f"{[r['phase'] for r in rows if not r['ready']]}")

    full = leadgen.plan({leadgen.PLACES_KEY: "k", leadgen.HUNTER_KEY: "h"})
    check("gating: every phase is ready with both keys",
          all(r["ready"] for r in full), f"{[r['phase'] for r in full if not r['ready']]}")

    # env_key reads the environment and treats blank as absent.
    check("gating: env_key reads the mapping",
          leadgen.env_key("X", {"X": " v "}) == "v")
    check("gating: env_key treats blank as absent",
          leadgen.env_key("X", {"X": "  "}) == "")


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def test_validation() -> None:
    # Both halves are free text, so the accepted set is wide on purpose.
    good = [
        ("plumbers", "Austin, TX"),
        ("emergency plumbers", "Manchester"),
        ("vegan restaurant", "Kreuzberg, Berlin"),
        ("dentist", "São Paulo"),
        ("bicycle repair", "Portland OR 97201"),
        ("dog groomer", "Dublin 8"),
    ]
    for niche, loc in good:
        ok, err = leadgen.validate(niche, loc)
        check(f"validate: accepts {niche!r} in {loc!r}", ok, err)

    bad = [
        ("", "Austin", "no niche given"),
        ("   ", "Austin", "no niche given"),
        ("plumbers", "", "no location given"),
        ("plumbers", "   ", "no location given"),
        ("!!!", "Austin", "no letters or digits"),
        ("plumbers", "???", "no letters or digits"),
    ]
    for niche, loc, needle in bad:
        ok, err = leadgen.validate(niche, loc)
        check(f"validate: rejects ({niche!r}, {loc!r})", not ok, err)
        check(f"validate: names the problem for ({niche!r}, {loc!r})",
              needle in err, err)

    # A pasted paragraph is not a niche. The cap catches it rather than
    # sending a 4 KB query to a paid API.
    ok, err = leadgen.validate("x" * (leadgen.MAX_FIELD + 1), "Austin")
    check("validate: rejects an over-long niche", not ok, err)
    ok, err = leadgen.validate("plumbers", "y" * (leadgen.MAX_FIELD + 1))
    check("validate: rejects an over-long location", not ok, err)

    # Normalisation collapses whitespace and is what the case path and the
    # prompt title both use, so they cannot disagree.
    check("normalise: collapses inner whitespace",
          leadgen.normalise("  emergency   plumbers ", " Austin,   TX ")
          == ("emergency plumbers", "Austin, TX"),
          f"{leadgen.normalise('  emergency   plumbers ', ' Austin,   TX ')}")
    check("normalise: the label reads niche · location",
          leadgen.label("plumbers", "Austin, TX") == "plumbers · Austin, TX",
          leadgen.label("plumbers", "Austin, TX"))


# ---------------------------------------------------------------------------
# Case directory
# ---------------------------------------------------------------------------

def test_case_dir(tmp: Path) -> None:
    cfg = _cfg(tmp)
    cfg.state_dir = tmp / "state"

    check("slug: keeps safe characters, drops the rest",
          leadgen.case_slug("Austin, TX") == "austin-tx",
          leadgen.case_slug("Austin, TX"))
    check("slug: never empty", leadgen.case_slug("!!!") == "target",
          leadgen.case_slug("!!!"))
    check("slug: lowercases", leadgen.case_slug("Manchester") == "manchester")

    d = leadgen.case_dir(cfg, "plumbers", "Austin, TX")
    check("case_dir: is created", d.is_dir(), str(d))
    check("case_dir: lives under state_dir/leadgen",
          d.parent == cfg.state_dir / "leadgen", str(d.parent))
    check("case_dir: named <niche>-<location>-<stamp>",
          d.name.startswith("plumbers-austin-tx-"), d.name)

    # The stamp is the last two dash-separated segments: the slugs contain
    # dashes themselves, so a single rsplit would return only the clock half.
    stamp = "-".join(d.name.rsplit("-", 2)[-2:])
    check("case_dir: stamp is YYYYMMDD-HHMMSS",
          len(stamp) == 15 and stamp[8] == "-"
          and stamp.replace("-", "").isdigit(), stamp)

    # A multi-word niche slugs into one path segment rather than nesting.
    d2 = leadgen.case_dir(cfg, "emergency plumbers", "Kreuzberg, Berlin")
    check("case_dir: a multi-word pair is one segment",
          d2.parent == d.parent and "/" not in d2.name,
          d2.name)
    check("case_dir: the multi-word name is slugged",
          d2.name.startswith("emergency-plumbers-kreuzberg-berlin-"), d2.name)

    # Drafts live inside the case, one file per lead, named after the lead.
    drafts = leadgen.drafts_dir(d)
    check("drafts_dir: is created inside the case", drafts.is_dir()
          and drafts.parent == d, str(drafts))
    p = leadgen.draft_path(d, "Ace Plumbing & Heating")
    check("draft_path: slugged, under drafts/",
          p.parent == drafts and p.name == "ace-plumbing-heating.md", p.name)


# ---------------------------------------------------------------------------
# Scope gate
# ---------------------------------------------------------------------------

def test_gate(tmp: Path) -> None:
    deny = tmp / "gate-out-of-scope.yaml"
    deny.write_text("domain:\n  - blocked.example\nip:\n  - 10.9.9.9\n")
    allow = tmp / "gate-allow.yaml"
    allow.write_text("domain:\n  - allowed.example\n")

    scope = Scope.load(None, deny)
    check("gate: the deny-list loaded", scope.loaded and not scope.allow.empty
          or scope.deny_path is not None)

    ok, reason = leadgen.gate(scope, "https://blocked.example/")
    check("gate: a denied host is refused", not ok, reason)
    check("gate: the refusal carries the guard's reason",
          "listed domain" in reason, reason)

    # Subdomains inherit the denial — the guard's own matching, not a string
    # comparison invented here.
    ok, reason = leadgen.gate(scope, "https://shop.blocked.example/path?q=1")
    check("gate: a subdomain of a denied domain is refused", not ok, reason)

    ok, reason = leadgen.gate(scope, "https://fine.example/")
    check("gate: an unlisted host is allowed", ok, reason)

    # A host on the ALLOW list is not thereby protected from the deny list —
    # deny beats allow, which is the whole point of having two files.
    scope_both = Scope.load(allow, deny)
    ok, _ = leadgen.gate(scope_both, "https://allowed.example/")
    check("gate: an allow-listed host is allowed", ok)
    ok, _ = leadgen.gate(scope_both, "https://blocked.example/")
    check("gate: deny still beats allow", not ok)

    # Ports, userinfo and case must not slip a denied host past the guard.
    for url in ("https://blocked.example:8443/",
                "https://BLOCKED.example/",
                "https://user:pw@blocked.example/x",
                "http://blocked.example"):
        ok, _ = leadgen.gate(scope, url)
        check(f"gate: refuses the {url!r} spelling", not ok)

    # No scope files at all means nothing is denied — the same answer the
    # bash and web-fetch tools give.
    ok, reason = leadgen.gate(None, "https://blocked.example/")
    check("gate: no scope means allow", ok, reason)
    ok, reason = leadgen.gate(Scope.load(None, None), "https://blocked.example/")
    check("gate: an unloaded scope means allow", ok, reason)

    # gate_all splits a lead list, keeping the URL with its reason so the
    # report can say *why* a host was skipped.
    allowed, denied = leadgen.gate_all(
        scope,
        ["https://fine.example/", "https://blocked.example/",
         "https://also-fine.example/"],
    )
    check("gate: gate_all keeps the allowed set",
          [u for u, _ in allowed]
          == ["https://fine.example/", "https://also-fine.example/"],
          f"{allowed}")
    check("gate: gate_all keeps the denied set with reasons",
          len(denied) == 1 and denied[0][0] == "https://blocked.example/"
          and denied[0][1], f"{denied}")

    # host_of is the one place a URL becomes a host, so it is pinned.
    check("gate: host_of strips scheme, port and path",
          leadgen.host_of("https://Example.COM:8443/a/b?c=d") == "example.com",
          leadgen.host_of("https://Example.COM:8443/a/b?c=d"))
    check("gate: host_of handles a bare host",
          leadgen.host_of("example.com/x") == "example.com")
    check("gate: host_of of nothing is empty", leadgen.host_of("") == "")

    # The discovered paths are the ones the CLI uses: working dir first.
    (tmp / "scope-probe").mkdir(exist_ok=True)
    (tmp / "scope-probe" / "out-of-scope.yaml").write_text("domain: [x.example]\n")
    a, d = leadgen.find_scope_files(tmp / "scope-probe", tmp / "nowhere")
    check("gate: find_scope_files finds the deny file in the workdir",
          d is not None and d.parent == tmp / "scope-probe", str(d))
    check("gate: find_scope_files finds no allow file", a is None, str(a))


# ---------------------------------------------------------------------------
# Finalising a case — archive the report and the drafts, delete the rest
# ---------------------------------------------------------------------------

def test_finalise(tmp: Path) -> None:
    cfg = _cfg(tmp)
    cfg.state_dir = tmp / "fin-state"

    # ---- a completed run: report and drafts archived, case dir gone --------
    case = leadgen.case_dir(cfg, "plumbers", "Done, TX")
    (case / "report.md").write_text("# Lead gen — plumbers in Done, TX\n")
    leadgen.draft_path(case, "Ace Plumbing").write_text("Subject: HSTS\n")
    leadgen.draft_path(case, "Best Drains").write_text("Subject: TLS\n")
    # Bulky raw data that must NOT survive the cleanup.
    (case / "audit.json").write_text("[" + "{}," * 5000 + "{}]")
    (case / "page.html").write_text("x" * 5000)

    report, cleaned = leadgen.finalise_case(cfg, case)
    check("finalise: returns the archived report path", report is not None,
          str(report))
    check("finalise: the report is archived under reports/",
          report is not None and report.parent == cfg.state_dir / "reports",
          str(report) if report else "")
    check("finalise: the archived report keeps the case name",
          report is not None and report.name == f"{case.name}.md",
          report.name if report else "")
    check("finalise: the report content survived the move",
          report is not None and "plumbers in Done, TX" in report.read_text())

    drafts = leadgen.archived_drafts(cfg, case)
    check("finalise: the drafts are archived beside the report",
          drafts.is_dir() and drafts.parent == cfg.state_dir / "reports",
          str(drafts))
    check("finalise: every draft survived",
          len(list(drafts.glob("*.md"))) == 2,
          f"{[p.name for p in drafts.glob('*.md')]}")
    check("finalise: the draft content survived",
          (drafts / "ace-plumbing.md").read_text() == "Subject: HSTS\n")

    check("finalise: the case directory was deleted", not case.exists())
    check("finalise: reports that cleaned is True", cleaned is True)

    # ---- a failed run: no report, so nothing is deleted --------------------
    # The whole point: a run that produced no report keeps its evidence AND
    # its drafts, rather than being tidied away on the assumption it finished.
    case2 = leadgen.case_dir(cfg, "plumbers", "Failed, TX")
    (case2 / "audit.json").write_text("[]")
    leadgen.draft_path(case2, "Half Done").write_text("Subject: x\n")
    report2, cleaned2 = leadgen.finalise_case(cfg, case2)
    check("finalise: no report yields no path", report2 is None)
    check("finalise: no report leaves the case dir in place", case2.exists())
    check("finalise: no report reports cleaned False", cleaned2 is False)
    check("finalise: no report keeps the drafts",
          (case2 / leadgen.DRAFTS / "half-done.md").is_file())

    # ---- a report under a different name or in a subdir is still found -----
    case3 = leadgen.case_dir(cfg, "plumbers", "Nested, TX")
    (case3 / "out").mkdir()
    (case3 / "out" / "Report.md").write_text("# nested\n")
    report3, cleaned3 = leadgen.finalise_case(cfg, case3)
    check("finalise: finds a report in a subdirectory", report3 is not None,
          str(report3))
    check("finalise: nested case dir still deleted", not case3.exists())
    check("finalise: nested run cleaned True", cleaned3 is True)

    # A non-report .md must not be mistaken for the report.
    case4 = leadgen.case_dir(cfg, "plumbers", "Notes, TX")
    (case4 / "notes.md").write_text("scratch\n")
    report4, cleaned4 = leadgen.finalise_case(cfg, case4)
    check("finalise: a stray .md is not treated as the report", report4 is None)
    check("finalise: a stray .md leaves the case dir alone", case4.exists())

    # A report.md belonging to a CLONED repo is not this run's report.
    case5 = leadgen.case_dir(cfg, "plumbers", "Cloned, TX")
    (case5 / "report.md").write_text("# the run's report\n")
    repo = case5 / "clone" / "some.git"
    repo.mkdir(parents=True)
    (repo / "report.md").write_text("# the repo's own report\n")
    report5, _ = leadgen.finalise_case(cfg, case5)
    check("finalise: the run's report wins over a clone's",
          report5 is not None and "the run's report" in report5.read_text(),
          report5.name if report5 else "")

    # With NO top-level report, a clone's report.md must not be promoted.
    case6 = leadgen.case_dir(cfg, "plumbers", "Cloneonly, TX")
    repo6 = case6 / "clone" / "only.git"
    repo6.mkdir(parents=True)
    (repo6 / "report.md").write_text("# not the run's\n")
    report6, _ = leadgen.finalise_case(cfg, case6)
    check("finalise: a clone's report is never promoted", report6 is None)
    check("finalise: a clone-only case dir is kept", case6.exists())

    # A run with no drafts at all still finalises: the report is the product,
    # and "every lead was clean" is a legitimate outcome.
    case7 = leadgen.case_dir(cfg, "plumbers", "Clean, TX")
    (case7 / "report.md").write_text("# nothing to draft\n")
    report7, cleaned7 = leadgen.finalise_case(cfg, case7)
    check("finalise: a run with no drafts still archives",
          report7 is not None and cleaned7 is True)
    check("finalise: no drafts means no drafts dir",
          not leadgen.archived_drafts(cfg, case7).exists())

    check("finalise: reports_dir matches the archive parent",
          leadgen.reports_dir(cfg) == cfg.state_dir / "reports")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def test_prompt(tmp: Path) -> None:
    case = tmp / "case"
    keys = {leadgen.PLACES_KEY: "k", leadgen.HUNTER_KEY: "h"}
    p = leadgen.build_prompt("emergency plumbers", "Austin, TX", case, keys)

    check("prompt: carries the niche", "emergency plumbers" in p)
    check("prompt: carries the location", "Austin, TX" in p)
    check("prompt: carries the case dir", str(case) in p)
    check("prompt: no unsubstituted placeholders",
          "{gate}" not in p and "{target}" not in p and "{case_dir}" not in p)

    # The authorised-use condition is stated in the prompt, not only the UI.
    check("prompt: states the legitimate-outreach condition",
          "Legitimate outreach only" in p)
    check("prompt: states it is for businesses with a reason to be contacted",
          "real reason to contact" in p)

    # Every phase survives into the briefing, in order, so an edit that drops
    # one is caught here rather than in a wasted run.
    positions = [p.find(f". {name} —") for name in leadgen.PHASE_ORDER]
    check("prompt: every phase is present",
          all(pos > 0 for pos in positions), f"{positions}")
    check("prompt: the phases are in run order",
          positions == sorted(positions), f"{positions}")

    # The two keyed phases say what they need, so a skipped phase reads as a
    # skip in the report rather than as "nothing was found".
    check("prompt: names the places key", leadgen.PLACES_KEY in p)
    check("prompt: names the hunter key", leadgen.HUNTER_KEY in p)
    check("prompt: tells the agent to report a skipped phase as skipped",
          "skipped" in p and "coverage table" in p)

    # The scope gate is a command in the prompt, not an adjective.
    check("prompt: gives the gate command", "zimzilla.leadgen gate" in p)
    check("prompt: says a denied host is skipped and reported",
          "skipped and *reported*" in p)

    # The read-only contract for the audit.
    for needle in ("GET and HEAD only", "No form submission, no login attempt",
                   "no port scan", "read-only"):
        check(f"prompt: audit contract has {needle!r}", needle in p)

    # The never-send contract, and the absence of every bulk sender by name.
    check("prompt: says drafts are never sent", "never sent" in p)
    check("prompt: names SMTP and the bulk senders as absent",
          "SMTP" in p and "Instantly" in p and "Smartlead" in p)
    check("prompt: says sending is a human step",
          "human, reviewed step" in p)

    # Business contact details only.
    check("prompt: forbids personal-profile scraping",
          "No personal social profiles" in p)

    # The shared report format, identical to /osint's — this is the assertion
    # that keeps the two commands' reports structurally the same.
    for needle in ("**What**", "**Where**", "**Confidence**", "**Means**",
                   "**Verify**", "| Phase | Ran? | Result |",
                   "## Headline", "Gaps and next steps"):
        check(f"prompt: report format has {needle!r}", needle in p)

    # The sections the operator asked for, including the skipped-hosts one.
    for section in ("DISCOVERY", "SITE AUDIT", "ENRICHMENT", "DRAFTS",
                    "SKIPPED"):
        check(f"prompt: names the {section} section", section in p)
    check("prompt: asks for an explicit empty section",
          "Nothing found — " in p)

    # A missing key replaces the phase body with an instruction to report the
    # skip — not with a workaround, and not with an empty section.
    q = leadgen.build_prompt("plumbers", "Austin", case, {})
    check("prompt: no key means the discovery body is withheld",
          "places:searchText" not in q)
    check("prompt: no key still leaves a skip instruction",
          "Do not attempt this phase" in q)
    check("prompt: no key still names the phase in the briefing",
          ". discovery —" in q)
    # The phases that need no key are untouched by the gate.
    check("prompt: the audit body survives with no keys",
          "openssl s_client" in q and "strict-transport-security" in q)

    # The gate command is pinned to this interpreter, not to a bare `python`.
    check("prompt: the gate command names an absolute interpreter",
          leadgen.gate_command().startswith("/")
          or leadgen.gate_command().startswith('"'), leadgen.gate_command())


def test_no_send_capability() -> None:
    """The promise is structural: the module cannot send anything."""
    src = (ROOT / "zimzilla" / "leadgen.py").read_text().lower()
    # The names appear in prose, as things this command does not do. What
    # must not appear is a client, an endpoint or an import.
    for forbidden in ("smtplib", "sendmail", "mailgun", "sendgrid", "postmark",
                      "import instantly", "instantly.ai", "api.instantly",
                      "import smartlead", "smartlead.ai", "api.smartlead"):
        check(f"no-send: module has no {forbidden}", forbidden not in src)
    # No outbound HTTP client either — the agent does the fetching, in the
    # turn, through the gated tools.
    for client in ("import requests", "import httpx", "urllib.request",
                   "http.client", "aiohttp"):
        check(f"no-send: module imports no HTTP client ({client})",
              client not in src)

    # And no function that looks like a dispatcher.
    for name in dir(leadgen):
        low = name.lower()
        check(f"no-send: no send-shaped attribute '{name}'",
              not any(w in low for w in ("send", "dispatch", "deliver",
                                         "smtp", "mail")))


# ---------------------------------------------------------------------------
# Wiring — the command must be reachable from every surface
# ---------------------------------------------------------------------------

def test_wiring() -> None:
    names = [c for c, _ in SLASH_COMMANDS]
    check("wiring: /leadgen is in the completion menu", "/leadgen" in names,
          f"{names}")

    app_src = (ROOT / "zimzilla" / "ui" / "app.py").read_text()
    check("wiring: dispatch has a leadgen entry",
          '"leadgen": lambda a:' in app_src)
    check("wiring: _cmd_leadgen is defined", "def _cmd_leadgen(" in app_src)
    check("wiring: _finalise_leadgen is defined",
          "def _finalise_leadgen(" in app_src)
    check("wiring: help table lists it",
          '("/leadgen <niche> <location>"' in app_src)
    check("wiring: palette lists it",
          '("/leadgen", "local leads: passive audit + outreach drafts"'
          in app_src)
    check("wiring: the module is imported", "leadgen as leadgen_mod" in app_src)

    # The turn is handed to the agent with a declared deliverable, so a model
    # that narrates instead of writing is nudged rather than silently passing.
    check("wiring: the run declares report.md as its deliverable",
          'Deliverable(path=case / "report.md", label="report.md")' in app_src)
    check("wiring: the case is finalised through the run's hook",
          "finalise=lambda: self._finalise_leadgen(case," in app_src)
    check("wiring: an interrupted run is passed to the finaliser",
          "interrupted=self._cancelled" in app_src)

    # Nothing in the command starts a turn while busy, and it is not in the
    # works-while-busy set — a lead run is a turn like any other.
    check("wiring: leadgen is not in the works-while-busy set",
          '"leadgen",' not in app_src.split("_WORK_WHILE_BUSY")[-1]
          .split("}")[0] if "_WORK_WHILE_BUSY" in app_src else True)


class _Blk:
    def __init__(self, **k):
        self.__dict__.update(k)

    def model_dump(self, exclude_none: bool = False) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if not (exclude_none and v is None)}


class _Msg:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage = usage


async def test_ui(wd: Path) -> None:
    """Drive the command through the live app: menu, refusals, a full run.

    The model is stubbed so no request leaves the machine, and the turn is
    held open on an event so `busy` is observed while it is genuinely in
    flight rather than raced against an instant completion.
    """
    cfg = _cfg(wd, boot_rain=False)
    cfg.state_dir = wd / "ui-state"

    app = ZimZillaApp(cfg)
    async with app.run_test(size=(110, 40)) as pilot:
        app.pop_screen()
        await pilot.pause()

        gate = asyncio.Event()

        async def held_stream(max_tokens=None):
            await gate.wait()
            yield (None, _Msg([_Blk(type="text", text="done")]))

        app.agent._stream_once = held_stream

        # ---- every refusal path leaves the harness idle --------------------
        app._handle_command("/leadgen")
        await pilot.pause()
        check("ui: bare /leadgen does not start a turn", app.busy is False)

        app._handle_command("/leadgen plumbers")
        await pilot.pause()
        check("ui: a missing location is refused", app.busy is False)

        app._handle_command("/leadgen --in Austin")
        await pilot.pause()
        check("ui: an empty niche before --in is refused", app.busy is False)

        app._handle_command("/leadgen !!! ???")
        await pilot.pause()
        check("ui: a punctuation-only pair is refused", app.busy is False)
        check("ui: a refused run creates no case dir",
              not (cfg.state_dir / "leadgen").exists()
              or not list((cfg.state_dir / "leadgen").glob("plumbers-*")))

        # ---- the split: first word is the niche, the rest the location -----
        niche, location, err = app._leadgen_split(["plumbers", "Austin", "TX"])
        check("ui: default split takes the first word as the niche",
              (niche, location, err) == ("plumbers", "Austin TX", ""),
              f"{(niche, location, err)}")
        niche, location, err = app._leadgen_split(
            ["emergency", "plumbers", "--in", "Austin,", "TX"])
        check("ui: --in takes everything before it as the niche",
              (niche, location, err) == ("emergency plumbers", "Austin, TX", ""),
              f"{(niche, location, err)}")
        niche, location, err = app._leadgen_split(
            ["plumbers", "--in", '"Austin,', 'TX"'])
        check("ui: stray quotes around the location are stripped",
              (niche, location, err) == ("plumbers", "Austin, TX", ""),
              f"{(niche, location, err)}")
        _, _, err = app._leadgen_split(["plumbers", "--in", "--in", "Austin"])
        check("ui: a doubled --in is refused", bool(err), err)

        # ---- the happy path launches a turn --------------------------------
        app._handle_command("/leadgen plumbers Austin, TX")
        await pilot.pause()
        check("ui: a valid pair launches the run", app.busy is True)

        made = list((cfg.state_dir / "leadgen").glob("plumbers-austin-tx-*"))
        check("ui: the case directory was created", len(made) == 1,
              f"{[p.name for p in made]}")

        gate.set()
        for _ in range(5):
            await pilot.pause()
        check("ui: the turn completes and the harness idles", app.busy is False)

        # The stub wrote no report, so the case must be KEPT.
        check("ui: a run with no report keeps its case dir",
              len(made) == 1 and made[0].is_dir(),
              f"{[p.name for p in made]}")

        # ---- a run that writes a report and a draft is archived -----------
        gate.clear()

        async def reporting_stream(max_tokens=None):
            await gate.wait()
            case = next((cfg.state_dir / "leadgen").glob(
                "emergency-plumbers-austin-tx-*"))
            (case / "report.md").write_text("# Lead gen — emergency plumbers\n")
            leadgen.draft_path(case, "Ace Plumbing").write_text(
                "Subject: no HSTS header\n")
            yield (None, _Msg([_Blk(type="text", text="done")]))

        app.agent._stream_once = reporting_stream

        app._handle_command(
            "/leadgen emergency plumbers --in Austin, TX")
        await pilot.pause()
        check("ui: a --in pair launches the run", app.busy is True)

        emade = list((cfg.state_dir / "leadgen").glob(
            "emergency-plumbers-austin-tx-*"))
        check("ui: the multi-word case dir is one slugged segment",
              len(emade) == 1, f"{[p.name for p in emade]}")

        gate.set()
        for _ in range(5):
            await pilot.pause()
        check("ui: the reporting turn completes", app.busy is False)

        check("ui: the case dir is cleared after a reported run",
              not emade[0].exists(), str(emade[0]))
        archived = list((cfg.state_dir / "reports").glob(
            "emergency-plumbers-austin-tx-*.md"))
        check("ui: the report is archived under reports/",
              len(archived) == 1, f"{[p.name for p in archived]}")
        drafts = list((cfg.state_dir / "reports").glob(
            "emergency-plumbers-austin-tx-*-drafts"))
        check("ui: the drafts are archived beside it", len(drafts) == 1,
              f"{[p.name for p in drafts]}")
        check("ui: the archived draft is the one the run wrote",
              drafts and (drafts[0] / "ace-plumbing.md").is_file())

        # ---- an interrupted run keeps its case, report or not --------------
        # The one deliberate difference from /osint's lifecycle: Ctrl+C leaves
        # the working directory in place even when the report was written, so
        # the operator can see which leads were reached and which were refused.
        case = leadgen.case_dir(cfg, "plumbers", "Interrupted, TX")
        (case / "report.md").write_text("# half a run\n")
        leadgen.draft_path(case, "Some Lead").write_text("Subject: x\n")
        app._finalise_leadgen(case, interrupted=True)
        await pilot.pause()
        check("ui: an interrupted run keeps its case dir", case.exists())
        check("ui: an interrupted run archives nothing",
              not list((cfg.state_dir / "reports").glob(
                  f"{case.name}.md")), f"{case.name}")
        check("ui: an interrupted run keeps its drafts",
              (case / leadgen.DRAFTS / "some-lead.md").is_file())

        # ...and the same case, finalised normally, is archived and cleared —
        # so the interrupt branch is a policy, not a broken cleanup.
        report, cleaned = leadgen.finalise_case(cfg, case)
        check("ui: the same case finalises once the run is not interrupted",
              report is not None and cleaned is True)
        check("ui: the interrupted case dir is gone after a normal finalise",
              not case.exists())


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_registry()
        test_key_gating()
        test_validation()
        test_case_dir(tmp)
        test_gate(tmp)
        test_finalise(tmp)
        test_prompt(tmp)
        test_no_send_capability()
        test_wiring()
        await test_ui(tmp)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
