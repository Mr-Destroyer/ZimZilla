"""`/bug-hunt` — an autonomous, self-iterating hunt campaign.

AGENTS.md arms a doctrine but ships no methodology: it says *you are an
autonomous pentest operator*, not *here is how to run an engagement*. So a zim
session improvises one pass and stops. `/bug-hunt` supplies the missing loop:

    recon -> plan a wave -> run the wave -> harvest findings -> replan

and repeats until the operator says `/stop-hunt`.

The shape is deliberately `/team`'s. A read-only planner reads what is known,
emits a JSON roster of independent briefs, the briefs run concurrently, and the
results come back as structured text. Three things are different:

*   **The planner sees the findings so far.** `WAVE_PLANNER_PROMPT` carries every
    finding from every earlier wave, so wave N+1 is aimed at what wave N did not
    cover rather than re-running the same ten probes. That feedback edge is the
    whole point of the command; without it this is just `/team` with a bigger
    roster.

*   **The loop does not end on its own.** There is no wave cap and no cost cap —
    the operator owns the stop. `stop` is an `asyncio.Event` the UI sets from
    `/stop-hunt`, and a wave races it so a stop lands mid-wave instead of after
    the slowest worker.

*   **Workers are told to report in JSON.** Every brief ends with the same
    finding contract, and `parse_findings` reads it back off the worker's final
    text. That is what makes a wave's output machine-readable enough to feed the
    next planner.

Like team.py this module holds orchestration and nothing else — no Textual, no
widgets, no colours. Everything is reported as plain event dicts through an
`on_event` sink, so the whole campaign is testable without a terminal.

The workers inherit the session's mode, so under `/mode zim` they run the
operator's AGENTS.md doctrine with every tool armed. The wave planner does not:
it is forced to `plan` mode, because a planner with bash will start doing the
work itself instead of deciding who should.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import team as team_mod
from .agent import Agent
from .config import Config

#: Briefs per wave. All of them run at once — see HUNT_CONCURRENCY.
HUNT_WAVE_SIZE = 10

#: Workers in flight at any instant. Equal to the wave size by design: the
#: operator asked for all ten to run simultaneously.
HUNT_CONCURRENCY = 10

#: Severities worth a report file. Anything quieter is still surfaced in the
#: transcript and fed to the next planner, but it does not earn an artefact.
SAVE_SEVERITIES = {"critical", "high", "medium"}

#: Severity spellings a model reaches for, mapped onto the four we accept.
_SEVERITY_ALIASES = {
    "crit": "critical",
    "severe": "critical",
    "moderate": "medium",
    "med": "medium",
    "low": "low",
    "info": "info",
    "informational": "info",
    "note": "info",
    "none": "info",
}

VALID_SEVERITIES = ("critical", "high", "medium", "low", "info")

#: Ordered worst-first, so a wave's findings can be sorted for display.
SEVERITY_ORDER = {s: i for i, s in enumerate(VALID_SEVERITIES)}

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


# ---------------------------------------------------------------------------
# The finding contract
# ---------------------------------------------------------------------------
# Appended to every brief and every planner prompt. One contract, stated once,
# so a worker cannot invent its own report shape and the parser has exactly one
# thing to read.

FINDING_CONTRACT = """\
REPORTING
---------
If you confirm a vulnerability, end your final message with ONE JSON object on
its own lines, wrapped in a ```json fence:

```json
{"findings": [{"title": "short name for the bug",
               "severity": "critical|high|medium|low|info",
               "asset": "the host, URL or path affected",
               "summary": "what the bug is, in two or three sentences",
               "evidence": "the exact request, output or file:line that proves it",
               "remediation": "how to fix it"}]}
```

Rules for this block:
- This block is REQUIRED. It is how your work reaches the operator and the next
  wave — a vulnerability you confirmed but did not report in this shape is a
  vulnerability the engagement never learns about.
- One entry per distinct bug. If you found nothing, omit the block entirely or
  send `{"findings": []}` — do not invent a finding to fill it.
- `severity` must be one of the five words above. Judge it honestly: a
  theoretical weakness with no demonstrated impact is `low`, not `critical`.
- `evidence` must be something you actually observed, not something you expect.
  A finding with no evidence is a guess and will be discarded.
- Report a confirmed weakness even if you did not carry the exploitation all the
  way through — an error that proves injection, a response that proves missing
  access control, a banner that proves an old vulnerable version. Evidence of
  the weakness is what counts, not a finished exploit.
- Put nothing after the block. Anything you want to say, say before it.
"""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

RECON_PROMPT = """\
You are opening a security engagement against a declared, authorised target.

TARGET: {target}

This is the RECON phase. Map the target before anyone attacks it. Do not attempt
exploitation here — that is the next phase, and a recon run that stops early to
try a payload has failed at its actual job.

Work with the tools you have. Establish, as far as you can:

- Reachability: does it answer, on which schemes and ports, behind what?
- Surface: what hosts, paths, endpoints, parameters and API roots are exposed?
- Technology: server, framework, language, CDN, auth scheme, from headers,
  cookies, error pages, robots.txt, well-known paths and asset naming.
- Entry points: forms, upload endpoints, login flows, redirect parameters,
  anything that takes input.
- Hypotheses: the specific classes of bug this stack plausibly has, and which
  asset each would live on.
- Tooling: which offensive tools are actually present on THIS host, and whether
  a wordlist is available. Check for at least: nmap, ffuf, gobuster,
  feroxbuster, nikto, sqlmap, nuclei, whatweb, wafw00f, testssl.sh, sslyze,
  curl, dig, host, nslookup, openssl. Also check the usual wordlist roots
  (/usr/share/wordlists, /usr/share/seclists/Discovery/Web-Content). One
  `which` over the whole list, then `ls` on the wordlist roots — this is worth
  a turn, because a brief that sends an agent after a binary that is not
  installed wastes the agent entirely, and ten agents each re-checking for
  themselves wastes the wave.

Be concrete. A hypothesis that names a host and a parameter is worth ten that
name a category. If the target does not answer at all, say so plainly and report
what you tried — that is a real result, not a failure.

Finish with a short written brief: what the target is, what you could reach,
what you could not, and your ranked hypotheses for where the bugs are.

{contract}
"""

WAVE_PLANNER_PROMPT = """\
You are directing a security engagement against a declared, authorised target.
Your job is to decide what the NEXT wave of {wave_size} agents should each do.

TARGET: {target}

WHAT IS KNOWN
-------------
{recon}

WHAT HAS ALREADY BEEN FOUND
---------------------------
{findings}

WAVE HISTORY
------------
{waves}

You have READ-ONLY tools. Use them to check a hypothesis before you brief an
agent on it — enough to know the asset is real and the brief is not a guess —
but do not attempt the work itself. That is what the agents are for.

Rules:
- Return AT MOST {wave_size} workers. Fewer is better than padding.
- Every brief must be INDEPENDENT: no agent may depend on another's output,
  because they all run at the same time and cannot talk to each other.
- Do not repeat work already done. Read the findings and the wave history and
  aim at what is still open. If an earlier finding suggests a follow-up — a
  different endpoint of the same service, a second parameter, a bypass of the
  same control — that is the highest-value brief you can write.
- If an earlier finding was marked low-confidence or a worker failed, a brief
  that re-tests it properly is worth a slot.
- Spread the wave across different bug classes. Ten briefs that all test
  injection waste nine slots. The classes worth covering, roughly in order of
  how often they pay: broken access control, authentication and session flaws,
  injection, business-logic abuse, sensitive-data exposure, misconfiguration,
  input validation, race conditions, crypto and transport, and the dependency
  and supply chain.
- Each worker gets a short lowercase `name` (e.g. "auth-bypass") and a `brief`
  that is a complete, self-contained instruction. The worker sees the brief and
  nothing else — it must say what to test, on which asset, and how to prove it.
- `owns` is the asset, endpoint or vector that brief is responsible for. Two
  workers must not claim the same one; the wave is deduplicated on this.

Reply with ONE JSON object and nothing else:

{{"summary": "one line on how you are reading the target now",
  "workers": [{{"name": "short-name",
               "brief": "the full instruction for this agent",
               "owns": ["the asset or vector it owns"]}}]}}

{contract}
"""

#: Appended to the planner prompt for one retry, after a reply that could not be
#: parsed. Deliberately blunt: the failure mode is almost always a model that
#: wrapped its JSON in prose or a fence, and saying so plainly fixes it.
PLANNER_RETRY_NUDGE = """\
Your previous reply could not be read as JSON. Reply with ONE JSON object and
nothing else — no prose before it, no prose after it, no code fence around it.
Start your reply with `{` and end it with `}`.
"""


SUMMARY_PROMPT = """\
An engagement against a target has been running as a sequence of waves of
parallel agents. Write the closing report.

TARGET: {target}
WAVES RUN: {waves}

RECON
-----
{recon}

EVERY FINDING, IN THE ORDER THEY WERE CONFIRMED
-----------------------------------------------
{findings}

Write the report the operator will actually read. It must cover, in this order:

1. **What was tested** — the ground covered, wave by wave, in plain language.
   Not a list of agent names; a description of the attack surface that was
   actually exercised.
2. **What was found** — each confirmed finding, worst first, with its severity
   and the evidence that proves it. Be precise about impact. If something was
   reported by an agent but the evidence does not hold up, say so and say why.
3. **What is worth checking next** — the specific, still-open leads, each
   concrete enough that someone could pick it up and run with it.
4. **What is not worth the time** — the avenues that were checked and are clean,
   or that look promising but are dead ends. This section matters as much as the
   third: it is what stops the next person repeating this engagement.

Be honest about coverage. If a whole class of bug was never tested, say so
rather than letting silence read as "clean". Do not pad. If the engagement found
nothing real, the report says that and explains what was ruled out.
"""


# ---------------------------------------------------------------------------
# Fallback vectors
# ---------------------------------------------------------------------------
# Used when the planner's reply cannot be read. A model that rambles should cost
# the wave its shape, never the wave itself — so the campaign keeps running on a
# fixed matrix rather than stopping to wait for a human.

HUNT_VECTORS: list[tuple[str, str, str]] = [
    (
        "access-control",
        "Broken access control. Enumerate every endpoint and object reference you "
        "can reach and test whether an unauthenticated or lower-privileged "
        "request can reach it: IDOR on object ids, forced browsing to admin "
        "paths, HTTP method tampering, missing function-level checks. Prove each "
        "with the exact request and response.",
        "access control",
    ),
    (
        "auth-session",
        "Authentication and session handling. Probe login for user enumeration, "
        "credential stuffing resistance, lockout, password-reset flow flaws and "
        "token predictability. Inspect session cookies for missing HttpOnly / "
        "Secure / SameSite, weak entropy, and whether logout actually invalidates "
        "the session server-side.",
        "authentication",
    ),
    (
        "injection",
        "Injection. Test every input you found — query parameters, form fields, "
        "headers, cookies, JSON bodies — for SQL, NoSQL, command, template and "
        "LDAP injection. Use safe, non-destructive probes first and escalate only "
        "on a positive signal. Report the payload and the evidence of execution.",
        "injection",
    ),
    (
        "business-logic",
        "Business logic. Walk the application's real workflows and look for ways "
        "to abuse them: skipping steps, replaying a one-time action, negative or "
        "overflowing quantities, price or role tampering in a request body, "
        "self-approval, and race windows in anything that should happen once.",
        "business logic",
    ),
    (
        "data-exposure",
        "Sensitive data exposure. Look for secrets and personal data where they "
        "should not be: verbose errors and stack traces, debug endpoints, source "
        "maps, .git and backup files, directory listings, API responses that "
        "return more fields than the UI shows, and credentials in client-side "
        "bundles.",
        "data exposure",
    ),
    (
        "misconfiguration",
        "Security misconfiguration. Check TLS and certificate validity, security "
        "headers (CSP, HSTS, X-Frame-Options, X-Content-Type-Options), CORS "
        "policy including credentialed origins, default credentials on any "
        "admin surface, exposed management or metrics ports, and permissive "
        "file-upload handling.",
        "misconfiguration",
    ),
    (
        "input-validation",
        "Input validation and client-side trust. Test for reflected and stored "
        "XSS, HTML injection, open redirects, SSRF via any URL-shaped parameter, "
        "path traversal in file parameters, XXE in any XML endpoint, and "
        "deserialization of attacker-controlled data.",
        "input validation",
    ),
    (
        "race-conditions",
        "Race conditions and state. Find operations that should be atomic — "
        "coupon redemption, transfers, invites, rate limits, vote or like "
        "counters — and fire them concurrently to test for double-spend, limit "
        "bypass and inconsistent state. Report the timing evidence, not just the "
        "outcome.",
        "race conditions",
    ),
    (
        "crypto-transport",
        "Cryptography and transport. Assess the TLS configuration and cipher "
        "suites, check for mixed content and downgrade paths, examine how tokens "
        "and password hashes are constructed and whether they are reversible, "
        "and look for predictable randomness in anything security-relevant.",
        "crypto",
    ),
    (
        "dependencies",
        "Dependency and supply chain. Fingerprint the components the target "
        "serves or references — JS libraries, frameworks, server banners, "
        "package manifests if reachable — and identify versions with known "
        "vulnerabilities. Confirm exploitability against this deployment rather "
        "than reporting a bare version match.",
        "dependencies",
    ),
]


def fallback_roster(wave: int, wave_size: int = HUNT_WAVE_SIZE) -> team_mod.Roster:
    """A fixed matrix roster, used when the planner's reply cannot be read."""
    workers = [
        team_mod.WorkerSpec(name=name, brief=brief, owns=[owns])
        for name, brief, owns in HUNT_VECTORS[:wave_size]
    ]
    return team_mod.Roster(
        summary=f"planner reply unreadable — falling back to the fixed vector "
                f"matrix for wave {wave}",
        workers=workers,
    )


#: How much of the recon report is prepended to every worker brief. Generous,
#: because the alternative is ten agents re-reading the same files to find it.
BRIEF_CONTEXT_CHARS = 6000


def brief_context(run: HuntRun, limit: int = BRIEF_CONTEXT_CHARS) -> str:
    """The shared briefing prepended to every worker's brief.

    A worker used to see only its own brief, so all ten would open with the same
    ``pwd && ls -la`` and the same re-read of every recon file — a whole wave
    spending its first turns rediscovering what recon already knew. This hands
    each of them the same picture up front and says, plainly, not to go looking
    for it again.

    Trimmed from the *end*: a recon report leads with reachability and surface
    and trails into hypotheses, so the head is the part that saves the most
    work. The full text still reaches the planner and the closing summary, which
    see ``run.recon`` directly.
    """
    recon = (run.recon or "").strip()
    if not recon:
        return ""
    if len(recon) > limit:
        recon = recon[:limit].rstrip() + "\n…(recon report truncated here)"
    return (
        "WHAT IS ALREADY KNOWN\n"
        "---------------------\n"
        "Recon has already mapped this target. The findings below are "
        "established fact — do not spend a turn re-deriving any of it. In "
        "particular, do not re-run `ls`, `pwd`, or re-read the same recon "
        "files: that ground is covered, and the wave is timed.\n\n"
        f"{recon}\n\n"
        "If the recon report names the tooling available on this host, treat "
        "that as the tooling you have. Do not probe for binaries it says are "
        "absent.\n"
        "---------------------"
    )


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """One confirmed bug, as the worker reported it."""

    wave: int
    agent: str
    title: str
    severity: str
    asset: str = ""
    summary: str = ""
    evidence: str = ""
    remediation: str = ""

    @property
    def saved(self) -> bool:
        """Whether this earns a report file."""
        return self.severity in SAVE_SEVERITIES


@dataclass
class HuntRun:
    """The state of one campaign, handed to the summary and the UI."""

    target: str
    directory: Path
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    wave: int = 0
    recon: str = ""
    findings: list[Finding] = field(default_factory=list)
    waves: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)

    def digest(self) -> str:
        """Every finding so far, as the next planner sees it.

        Deliberately terse: the planner needs to know what is closed so it can
        aim elsewhere, not to re-read the evidence. The full text lives in the
        report files.
        """
        if not self.findings:
            return "(nothing confirmed yet)"
        lines = []
        for f in self.ranked():
            asset = f" @ {f.asset}" if f.asset else ""
            lines.append(f"- [{f.severity}] {f.title}{asset} — {f.summary}".rstrip())
        return "\n".join(lines)

    def wave_digest(self) -> str:
        """One line per completed wave, so the planner can see the ground covered."""
        if not self.waves:
            return "(this is the first wave)"
        return "\n".join(self.waves)

    def add_finding(self, f: Finding) -> bool:
        """Record a finding, deduplicated. Returns whether it was new.

        Ten agents run at once against one target, so the same bug gets reported
        more than once — "missing HSTS" is a title four different vectors might
        each land on — and an undeduped tracker would show it four times and
        inflate every count. Two findings are the same bug when their title
        slugs and their assets match; the first one wins, because it is the one
        whose evidence was captured against the asset as originally found.

        The return value is what stops the transcript and the panel
        double-counting: a repeat is dropped silently rather than announced
        twice.
        """
        key = (_slug(f.title), (f.asset or "").strip().lower())
        for existing in self.findings:
            if (_slug(existing.title),
                    (existing.asset or "").strip().lower()) == key:
                return False
        self.findings.append(f)
        return True

    def ranked(self) -> list[Finding]:
        """The findings, worst first, then by wave and title.

        One ordering, used by the findings panel, the transcript and the closing
        summary, so a critical never sorts below an info in one place and above
        it in another. `wave` breaks severity ties so an early confirmation
        reads above a late restatement; `title` keeps it stable run to run.
        """
        return sorted(
            self.findings,
            key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.wave, f.title),
        )


def _slug(text: str) -> str:
    """A short, filesystem-safe label. Never empty."""
    s = _SLUG_RE.sub("-", str(text or "").strip()).strip("-.")
    return (s[:64] or "finding").lower()


def _iter_json_objects(text: str):
    """Yield every balanced ``{...}`` span in *text*, string-aware.

    A worker may report several findings in one block or several blocks in one
    message, so this walks the whole text rather than stopping at the first
    object the way team._first_json_object does. Brace counting skips braces
    inside string literals, or an evidence field containing ``{`` would
    unbalance the scan.
    """
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        escaped = False
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[i : j + 1]
                    i = j + 1
                    break
        else:
            return  # unbalanced from here on; nothing more to read


def normalise_severity(raw: str) -> str:
    """Fold a model's severity spelling onto one of the five we accept."""
    s = str(raw or "").strip().lower()
    s = _SEVERITY_ALIASES.get(s, s)
    return s if s in VALID_SEVERITIES else "info"


def parse_findings(text: str, *, wave: int, agent: str) -> list[Finding]:
    """Read a worker's findings back off its final text.

    Accepts both shapes a model produces: a ``{"findings": [...]}`` envelope, and
    a bare object that is itself one finding. Anything without a title is
    dropped — an entry the model could not even name is not a report.
    """
    if not text or not text.strip():
        return []

    out: list[Finding] = []
    for span in _iter_json_objects(text):
        try:
            data = json.loads(span)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue

        entries = data.get("findings")
        if not isinstance(entries, list):
            entries = [data] if data.get("title") else []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title", "") or "").strip()
            if not title:
                continue
            out.append(Finding(
                wave=wave,
                agent=agent,
                title=title[:200],
                severity=normalise_severity(entry.get("severity")),
                asset=str(entry.get("asset", "") or "").strip()[:300],
                summary=str(entry.get("summary", "") or "").strip(),
                evidence=str(entry.get("evidence", "") or "").strip(),
                remediation=str(entry.get("remediation", "") or "").strip(),
            ))
    return out


def _finding_markdown(run: HuntRun, f: Finding) -> str:
    """The body of one finding's report file."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return (
        "---\n"
        f"target: {run.target}\n"
        f"wave: {f.wave}\n"
        f"agent: {f.agent}\n"
        f"severity: {f.severity}\n"
        f"asset: {f.asset}\n"
        f"timestamp: {stamp}\n"
        "---\n\n"
        f"# {f.title}\n\n"
        f"**Severity:** {f.severity}  \n"
        f"**Asset:** {f.asset or '(not stated)'}  \n"
        f"**Found by:** {f.agent} (wave {f.wave})\n\n"
        "## Summary\n\n"
        f"{f.summary or '(no summary given)'}\n\n"
        "## Evidence\n\n"
        f"{f.evidence or '(no evidence recorded)'}\n\n"
        "## Remediation\n\n"
        f"{f.remediation or '(no remediation suggested)'}\n"
    )


def case_dir(cfg: Config, target: str) -> Path:
    """Create and return this campaign's directory.

    ``<state_dir>/hunts/<slug>-<stamp>/`` — under state_dir, beside sessions and
    osint cases rather than in whatever repo the operator happens to be in.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = Path(cfg.state_dir) / "hunts" / f"{_slug(target)}-{stamp}"
    (d / "findings").mkdir(parents=True, exist_ok=True)
    return d


def write_report(run: HuntRun, f: Finding) -> Path:
    """Write one finding to ``<run.directory>/findings/wave-<n>-<slug>.md``.

    Two agents in one wave can land on the same title — "missing HSTS" is a
    title four different vectors might each report — and the slug would be
    identical, so the second write would silently destroy the first agent's
    evidence. A numeric suffix keeps every finding's report on disk, which is
    the whole point of writing them.
    """
    base = run.directory / "findings" / f"wave-{f.wave}-{_slug(f.title)}"
    path = base.with_suffix(".md")
    n = 2
    while path.exists():
        path = base.with_name(f"{base.name}-{n}.md")
        n += 1
    path.write_text(_finding_markdown(run, f), encoding="utf-8")
    return path


def reports_dir(cfg: Config) -> Path:
    """Where finished campaign summaries are kept, beside the osint reports."""
    return Path(cfg.state_dir) / "reports"


def write_summary(cfg: Config, run: HuntRun, text: str) -> Path:
    """Archive the closing report.

    Written into the campaign directory first so it sits with the evidence, then
    copied into ``<state_dir>/reports/`` — the flat, permanent store that
    outlives the run, the same one `/osint` archives into. The copy is
    best-effort: the report exists in the campaign directory regardless, so a
    failed archive must not raise into the UI.
    """
    body = (
        "---\n"
        f"target: {run.target}\n"
        f"waves: {run.wave}\n"
        f"findings: {len(run.findings)}\n"
        f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        "---\n\n"
        f"{text}\n"
    )
    local = run.directory / "summary.md"
    local.write_text(body, encoding="utf-8")

    dest_dir = reports_dir(cfg)
    dest = dest_dir / f"hunt-{run.directory.name}.md"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, dest)
    except OSError:
        return local
    return dest


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

async def _run_agent(
    spec: team_mod.WorkerSpec,
    index: int,
    cfg: Config,
    *,
    on_event: Callable[[dict], Awaitable[None]],
    agent_factory: Callable[..., Agent],
    tool_hook,
    context: str = "",
    contract: str = "",
) -> team_mod.WorkerResult:
    """Run one hunt agent to completion. Never raises except on cancellation.

    A near-copy of team._run_worker, and deliberately so: that one emits
    ``team_*`` events, and a hunt agent's events carry a different payload (the
    wave, the pane slot). The shared machinery — the write lock, the digest, the
    touched-path extraction — is imported from team rather than duplicated.

    ``context`` is the shared recon digest, prepended to the brief. Without it
    every agent starts blind and spends its first turns re-deriving what recon
    already established — ten agents running the same ``ls`` and reading the
    same six files — which is most of a wave's budget spent on nothing.

    ``contract`` is the finding-reporting block, appended *after* the brief so
    it is the last thing the agent reads before it answers. It has to be here
    and not only on the planner: a worker is the only one that runs a test, so
    it is the only one that can report what the test found. Without it the agent
    narrates a methodology, ``parse_findings`` reads no JSON, and the whole wave
    reports nothing no matter what it actually discovered.
    """
    result = team_mod.WorkerResult(spec=spec)

    # Its own Config copy, so each agent gets its own Scope and its own
    # cfg._scope — see team.py's module docstring for why sharing one clobbers
    # the scope guard. The mode is inherited: under /mode zim these agents run
    # the operator's AGENTS.md doctrine with every tool armed.
    agent_cfg = dataclasses.replace(cfg)
    worker = agent_factory(agent_cfg, tool_hook=tool_hook)

    await on_event({"type": "hunt_agent_start", "name": spec.name, "index": index,
                    "brief": spec.brief})

    buf: list[str] = []

    async def flush() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if text:
            await on_event({"type": "hunt_agent_text", "name": spec.name, "text": text})

    # Order matters: the shared context first, then the brief, then the contract
    # last — the reporting block is what the agent should be holding in mind as
    # it writes its final message.
    prompt = f"{context}\n\n{spec.brief}" if context else spec.brief
    if contract:
        prompt = f"{prompt}\n\n{contract}"

    try:
        async for ev in worker.run_turn(prompt):
            etype = ev.get("type")
            if etype == "text_delta":
                buf.append(ev.get("text", ""))
            elif etype == "tool_call":
                # Flush first, so the agent's reasoning lands above the call it
                # explains rather than after it.
                await flush()
                await on_event({"type": "hunt_agent_tool", "name": spec.name,
                                "tool": ev.get("name", ""), "args": ev.get("args") or {}})
            elif etype == "tool_result":
                result.touched.extend(
                    team_mod._touched_paths(ev.get("name", ""), ev.get("args") or {})
                )
                await on_event({
                    "type": "hunt_agent_result", "name": spec.name,
                    "tool": ev.get("name", ""), "ok": not ev.get("is_error"),
                    "output": ev.get("output", ""),
                    # The command and the run metadata travel with the result so
                    # the transcript can render a full panel rather than a
                    # truncated one-liner. This is the operator's only view of
                    # what the engagement is actually doing.
                    "args": ev.get("args") or {},
                    "meta": ev.get("meta") or {},
                })
            elif etype == "blocked":
                await on_event({"type": "hunt_agent_result", "name": spec.name,
                                "tool": ev.get("name", ""), "ok": False,
                                "output": ev.get("targets", ""),
                                "args": {}, "meta": {}, "blocked": True})
            elif etype == "usage":
                result.input_tokens += ev.get("input", 0) or 0
                result.output_tokens += ev.get("output", 0) or 0
                result.cost += ev.get("cost", 0.0) or 0.0
            elif etype == "error":
                result.ok = False
                result.error = ev.get("message", "") or "error"
                await flush()
        await flush()
    except asyncio.CancelledError:
        # Never swallow cancellation — /stop-hunt has to unwind the whole wave.
        raise
    except Exception as e:  # noqa: BLE001
        result.ok = False
        result.error = f"{type(e).__name__}: {e}"
        await flush()

    result.digest = team_mod._digest(worker, result)
    return result


async def _run_wave(
    specs: list[team_mod.WorkerSpec],
    cfg: Config,
    *,
    on_event: Callable[[dict], Awaitable[None]],
    agent_factory: Callable[..., Agent],
    stop: asyncio.Event,
    concurrency: int = HUNT_CONCURRENCY,
    context: str = "",
    contract: str = "",
) -> list[team_mod.WorkerResult]:
    """Run one wave concurrently, racing the stop signal.

    All the workers start together; ``concurrency`` is a semaphore, not a batch
    boundary, so with the default of ten a ten-brief wave is genuinely ten
    simultaneous streams. The wave is raced against ``stop`` rather than merely
    checked before it: `/stop-hunt` cancels the in-flight agents instead of
    waiting out the slowest one, which is the difference between stopping a hunt
    and waiting for it.
    """
    sem = asyncio.Semaphore(max(1, concurrency))
    hook = team_mod._write_lock_hook(asyncio.Lock())

    async def _one(index: int, spec: team_mod.WorkerSpec):
        async with sem:
            if stop.is_set():
                return None
            return await _run_agent(
                spec, index, cfg,
                on_event=on_event, agent_factory=agent_factory, tool_hook=hook,
                context=context, contract=contract,
            )

    tasks = [asyncio.create_task(_one(i, s)) for i, s in enumerate(specs)]
    if not tasks:
        return []

    all_done = asyncio.gather(*tasks, return_exceptions=True)
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({all_done, stopper},
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()

    if stop.is_set():
        for t in tasks:
            t.cancel()

    gathered = await all_done
    return [r for r in gathered if isinstance(r, team_mod.WorkerResult)]


async def _collect_text(agent: Agent, prompt: str) -> str:
    """Run one turn and return only its text, discarding the tool chatter."""
    parts: list[str] = []
    async for ev in agent.run_turn(prompt):
        if ev.get("type") == "text_delta":
            parts.append(ev.get("text", ""))
    return "".join(parts)


async def _collect_text_live(
    agent: Agent,
    prompt: str,
    on_event: Callable[[dict], Awaitable[None]],
) -> str:
    """Run one turn, reporting what it is doing as it does it.

    Recon is the one turn the operator watches directly: it can run for minutes
    against a live target, and a frozen "mapping the target…" line for that long
    is indistinguishable from a hang. So unlike ``_collect_text`` this forwards
    each tool call and each flushed block of prose as ``recon_*`` events, and
    returns the same text it would have returned silently.

    Only the *tool call* is reported, not its output: a recon tool result is
    arbitrary target text, and pushing it into a UI sink unescaped is how a
    scan of a hostile page ends up painting the overlay. The model's own prose
    is the interesting part and it is what gets shown.
    """
    parts: list[str] = []
    buf: list[str] = []

    async def flush() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if text:
            await on_event({"type": "hunt_recon_text", "text": text})

    async for ev in agent.run_turn(prompt):
        etype = ev.get("type")
        if etype == "text_delta":
            chunk = ev.get("text", "")
            parts.append(chunk)
            buf.append(chunk)
        elif etype == "tool_call":
            # Flush first, so the reasoning lands above the call it explains.
            await flush()
            await on_event({
                "type": "hunt_recon_tool",
                "tool": ev.get("name", ""),
                "args": ev.get("args") or {},
                "blocked": False,
            })
        elif etype == "blocked":
            await on_event({
                "type": "hunt_recon_tool",
                "tool": ev.get("name", ""),
                "args": ev.get("args") or {},
                "blocked": True,
            })
        elif etype == "error":
            await on_event({"type": "hunt_recon_text",
                            "text": f"(recon error: {ev.get('message', '')})"})
    await flush()
    return "".join(parts)


async def run_hunt(
    cfg: Config,
    target: str,
    *,
    on_event: Callable[[dict], Awaitable[None]],
    agent_factory: Callable[..., Agent] = Agent,
    stop: asyncio.Event | None = None,
    wave_size: int = HUNT_WAVE_SIZE,
    concurrency: int = HUNT_CONCURRENCY,
    summary_flag: Callable[[], bool] | None = None,
    run: HuntRun | None = None,
) -> HuntRun:
    """Recon, then waves until stopped. Returns the run for the closing report.

    Every step is reported through ``on_event`` as it happens. The caller owns
    the final summary text — this returns the run it needs to write it.

    ``summary_flag`` is polled at each wave boundary: `/summary-hunt` cannot run
    concurrently with the hunt (both stream through the same transcript), so it
    sets a flag and the summary is generated here, between waves, where nothing
    else is streaming.

    ``run`` lets the caller supply the ``HuntRun`` it wants used. The UI passes
    one it has already published, so ``/stop-hunt`` and ``/summary-hunt`` have
    something to act on *while the campaign is running* — this function returns
    only when the campaign is over, which is far too late to be told to stop.
    """
    if run is None:
        run = HuntRun(target=target, directory=case_dir(cfg, target))
    if stop is not None:
        run.stop = stop

    # ---- recon. Runs in the session's own mode, not read-only: mapping a live
    # target needs the shell and the network tools, and a recon phase that
    # cannot scan is not recon. The prompt is what bounds it to reconnaissance,
    # not the mode.
    await on_event({"type": "hunt_recon_start", "target": target,
                    "directory": str(run.directory)})
    recon_agent = agent_factory(dataclasses.replace(cfg))
    run.recon = await _collect_text_live(
        recon_agent, RECON_PROMPT.format(target=target, contract=FINDING_CONTRACT),
        on_event,
    )
    await on_event({"type": "hunt_recon_done", "text": run.recon})

    # A recon run can itself turn up something — an exposed debug endpoint, a
    # stack trace in an error page. Harvest it before the first wave, or the
    # planner starts out blind to what recon already knew.
    for f in parse_findings(run.recon, wave=0, agent="recon"):
        if run.add_finding(f):
            await on_event({"type": "hunt_finding", "finding": _finding_dict(f),
                            "saved": _save(run, f)})

    # ---- waves, until the operator stops it.
    while not run.stop.is_set():
        run.wave += 1
        wave = run.wave

        await on_event({"type": "hunt_wave_start", "wave": wave, "size": wave_size})

        # Plan. Read-only, so the planner cannot start doing the work itself —
        # the same reason team.py forces its planner to plan mode.
        planner = agent_factory(dataclasses.replace(cfg, mode="plan"))
        plan_prompt = WAVE_PLANNER_PROMPT.format(
            target=target,
            wave_size=wave_size,
            recon=run.recon or "(recon produced nothing)",
            findings=run.digest(),
            waves=run.wave_digest(),
            contract=FINDING_CONTRACT,
        )
        plan_text = await _collect_text(planner, plan_prompt)
        roster = team_mod.parse_roster(plan_text, max_agents=wave_size)

        # One retry before giving up on the planner. A model that answered in
        # prose or wrapped the object in a fence it malformed usually gets it
        # right when told plainly what was wrong, and a real roster is worth
        # far more than the fixed matrix.
        if roster is None or not roster.workers:
            retry_text = await _collect_text(
                planner, plan_prompt + "\n\n" + PLANNER_RETRY_NUDGE)
            if retry_text.strip():
                plan_text = retry_text
                roster = team_mod.parse_roster(retry_text, max_agents=wave_size)

        fell_back = roster is None or not roster.workers
        if fell_back:
            roster = fallback_roster(wave, wave_size)

        await on_event({
            "type": "hunt_plan", "wave": wave,
            "summary": roster.summary,
            "fell_back": fell_back,
            # When the planner could not be read, hand the operator what it
            # actually said. Silently swapping in the fixed matrix hides whether
            # the fault is the prompt, the model, or a truncated turn — and that
            # is not diagnosable from the outside.
            "raw": plan_text if fell_back else "",
            "workers": [{"name": w.name, "brief": w.brief, "owns": w.owns}
                        for w in roster.workers],
        })

        if run.stop.is_set():
            break

        results = await _run_wave(
            roster.workers, cfg,
            on_event=on_event, agent_factory=agent_factory,
            stop=run.stop, concurrency=concurrency,
            context=brief_context(run),
            contract=FINDING_CONTRACT,
        )

        # ---- harvest. Findings come off each agent's final text, so the loop
        # can re-plan from them even though the agents never spoke to each other.
        found: list[Finding] = []
        for r in results:
            for f in parse_findings(r.digest, wave=wave, agent=r.spec.name):
                found.append(f)

        for r in results:
            await on_event({
                "type": "hunt_agent_done", "name": r.spec.name, "wave": wave,
                "ok": r.ok, "error": r.error, "cost": r.cost,
                "input": r.input_tokens, "output": r.output_tokens,
            })

        # `found` is every finding reported this wave; `fresh` is the ones the
        # tracker had not already seen. Only the fresh ones are announced, and
        # the wave counter uses `fresh` too, so a bug three agents each
        # rediscovered reads as one finding rather than three.
        fresh: list[Finding] = [f for f in found if run.add_finding(f)]
        for f in fresh:
            await on_event({"type": "hunt_finding", "finding": _finding_dict(f),
                            "saved": _save(run, f)})

        ok = sum(1 for r in results if r.ok)
        run.waves.append(
            f"wave {wave}: {len(roster.workers)} agents, {ok} ok, "
            f"{len(fresh)} finding(s)"
            + (" (fixed vector fallback)" if fell_back else "")
        )
        await on_event({
            "type": "hunt_wave_end", "wave": wave,
            "agents": len(roster.workers), "ok": ok, "found": len(fresh),
            "cost": sum(r.cost for r in results),
            "input": sum(r.input_tokens for r in results),
            "output": sum(r.output_tokens for r in results),
            "fell_back": fell_back,
        })

        # ---- the queued summary, run here: between waves nothing else is
        # streaming, so it cannot corrupt the transcript.
        if summary_flag is not None and summary_flag():
            text = await _collect_text(agent_factory(dataclasses.replace(cfg)),
                                       summary_prompt(run))
            path = write_summary(cfg, run, text)
            await on_event({"type": "hunt_summary_done", "path": str(path),
                            "text": text, "wave": wave})

    await on_event({
        "type": "hunt_end", "wave": run.wave,
        "findings": len(run.findings),
        "saved": sum(1 for f in run.findings if f.saved),
        "directory": str(run.directory),
    })
    return run


def _finding_dict(f: Finding) -> dict:
    """A finding as a plain dict, for the UI event stream."""
    return {
        "wave": f.wave, "agent": f.agent, "title": f.title,
        "severity": f.severity, "asset": f.asset, "summary": f.summary,
        "evidence": f.evidence, "remediation": f.remediation,
    }


def _save(run: HuntRun, f: Finding) -> str:
    """Write the finding's report if it is worth one. Returns the path or ''."""
    if not f.saved:
        return ""
    try:
        return str(write_report(run, f))
    except OSError:
        # A campaign must not die because a report file could not be written —
        # the finding is already in the transcript and the next planner's digest.
        return ""


def summary_prompt(run: HuntRun) -> str:
    """The closing brief: everything the run knows, for the final report."""
    if run.findings:
        blocks = []
        for f in run.ranked():
            blocks.append(
                f"### [{f.severity}] {f.title}\n"
                f"asset: {f.asset or '(not stated)'}\n"
                f"found by: {f.agent} (wave {f.wave})\n\n"
                f"{f.summary or '(no summary)'}\n\n"
                f"evidence: {f.evidence or '(none recorded)'}\n\n"
                f"suggested fix: {f.remediation or '(none)'}"
            )
        findings = "\n\n".join(blocks)
    else:
        findings = "(no findings were confirmed)"

    return SUMMARY_PROMPT.format(
        target=run.target,
        waves=run.wave_digest(),
        recon=run.recon or "(recon produced nothing)",
        findings=findings,
    )


def load_run(directory: Path) -> HuntRun | None:
    """Rebuild a run from an archived campaign directory.

    Used by `/summary-hunt` when no hunt is live: the findings are re-read from
    the report files rather than kept in memory, so a summary can be asked for
    after a restart, or for a campaign that ran in an earlier session. Returns
    None when the directory holds nothing to summarise.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return None

    findings: list[Finding] = []
    for path in sorted((directory / "findings").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        meta = _read_frontmatter(text)
        findings.append(Finding(
            wave=_as_int(meta.get("wave")),
            agent=meta.get("agent", "?"),
            title=_first_heading(text) or path.stem,
            severity=normalise_severity(meta.get("severity", "info")),
            asset=meta.get("asset", ""),
            summary=_section(text, "Summary"),
            evidence=_section(text, "Evidence"),
            remediation=_section(text, "Remediation"),
        ))

    if not findings:
        return None

    return HuntRun(
        target=directory.name,
        directory=directory,
        findings=findings,
        waves=[f"(recovered from {directory})"],
    )


def _as_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _read_frontmatter(text: str) -> dict[str, str]:
    """The YAML frontmatter of a report file, as flat strings.

    Hand-rolled rather than pulling in a YAML dependency: the frontmatter this
    module writes is a flat map of scalars, and that is all it needs to read.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _section(text: str, name: str) -> str:
    """The body under ``## <name>``, up to the next heading."""
    marker = f"## {name}"
    start = text.find(marker)
    if start < 0:
        return ""
    start = text.find("\n", start)
    if start < 0:
        return ""
    rest = text[start + 1 :]
    end = rest.find("\n## ")
    body = rest if end < 0 else rest[:end]
    return body.strip()
