"""`/osint` — targeted open-source-intelligence recon.

The command is a **router**, not an engine. It picks a *kind* (email, phone,
facebook, …), validates the target for that kind, opens a case directory, and
then hands a complete, self-contained playbook to the main agent as a single
turn. Everything after that is the ordinary agent loop: the same tools, the
same scope guard, the same transcript. That is deliberate — a recon run is
exactly the long, tool-heavy, interruptible turn the loop already does well,
and routing it through the loop means `/osint` inherits mode gating, the
permission gate, Ctrl+C and the cost accounting for free.

This module holds the parts that are not the UI: the kind registry, target
validation, the case-directory layout, and the email playbook prompt. No
Textual, no colours — so the whole thing is testable without a terminal, the
same way zimzilla/team.py is.

Why the playbook lives here rather than in a prompt string at the call site:
it is long, it is the actual product of this command, and it wants to be
read and edited as prose next to the registry that selects it.

The one rule the playbook cannot enforce and the operator must: this is for
authorised investigation only — your own footprint, a consented audit, or a
declared engagement. The command says so on every run; it does not and cannot
check it.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------
# Every kind `/osint` knows about. `built` is the honest flag: the six unbuilt
# kinds are listed so the surface is discoverable, but selecting one stops with
# "not built yet" rather than pretending to work. Adding a kind later means
# filling in `playbook` and flipping `built` — nothing else in the codebase
# moves, because the UI reads this table rather than hard-coding names.


@dataclass(frozen=True)
class Kind:
    """One OSINT target kind."""

    name: str
    blurb: str
    target_label: str
    built: bool = False
    playbook: str = ""


# ---------------------------------------------------------------------------
# The report format — shared by every kind
# ---------------------------------------------------------------------------
# Both playbooks end in a report, and the operator asked for one that explains
# each finding rather than dumping it: what was found, where it came from, how
# sure we are, what it means, and how to check it. That shape is the same
# whether the target was an address or a handle, so it lives here once and is
# interpolated into both playbooks. Keeping it in one place is the point: the
# email and github reports cannot drift apart in structure.
#
# These are plain strings interpolated with str.format at prompt-build time,
# not f-strings, so the literal `{target}` and `{case_dir}` in the playbooks
# survive into the template. The guides themselves carry no braces.

_REPORT_GUIDE = """\
EVIDENCE, NOT ASSERTION
-----------------------
The report is read by a human deciding what to do next, so every finding has
to explain itself. Do not write a wall of prose and do not dump raw tool
output — one block per finding, always these five lines, always in this order:

    ### <short title — what this is>
    **What**       — the finding itself, in one sentence.
    **Where**      — the exact source: a URL, a command, a file and line, a
                     commit sha. A finding with no `Where` is not a finding.
    **Confidence** — CONFIRMED | PROBABLE | UNVERIFIED (defined below).
    **Means**      — what this tells the operator, in plain language. This is
                     the line that answers "so what?" — never leave it out.
    **Verify**     — the one step a human would take to confirm or refute it.

Group the blocks under the numbered sections below. A section with nothing in
it still gets its heading and the single line `Nothing found — <why>`, because
an empty section is itself a finding and must never be silently omitted.

COVERAGE
--------
The report opens with a coverage table, so the operator can see at a glance
what ran and what did not — a phase skipped for want of a tool or a key is
reported as skipped, never quietly dropped:

    | Phase | Ran? | Result |
    |-------|------|--------|
    | 1. ... | yes/no/skipped (why) | one line |

Then a `## Headline` block at the very top: three to six lines of plain prose
stating who or what this target turned out to be and the single most important
thing about it. An operator who reads only this must not be misled.

CLOSING
-------
End the file with a `## Gaps and next steps` section: every question the run
could not answer, every source that was unavailable, and the concrete manual
checks that would close them. Do not pad the report with a narrative of what
you tried — the operator wants the intelligence, the confidence, and the gaps.
"""

_CONFIDENCE_GUIDE = """\
CONFIDENCE
----------
Every finding carries one of three levels, and you never upgrade one without
evidence:
  CONFIRMED   — verified against a primary source you can cite.
  PROBABLE    — two or more independent sources agree.
  UNVERIFIED  — a single source, not yet corroborated.
"""


# ---------------------------------------------------------------------------
# The email playbook
# ---------------------------------------------------------------------------
# Handed to the agent verbatim as the turn's user message, with {target} and
# {case_dir} substituted. It is written as an operator briefing, not a chat
# message: phases in order, the tools and exact commands for each, and the
# report shape to end on. The agent has bash, read/write/edit, glob/grep and
# the web tools, so it can run the whole thing itself.

EMAIL_PLAYBOOK = """\
# Email OSINT recon — {target}

Authorised investigation only. You are running a recon pass on an email
address the operator has declared in scope. Work autonomously through the
phases below; do not stop to ask permission between steps. If a tool is
missing or a request is refused, note it and continue with what works.

EVIDENCE DIRECTORY
------------------
{case_dir}

Write every artefact you produce into that directory: raw command output,
downloaded avatars, and the final `report.md`. Use absolute paths. Create
subfiles rather than letting long output scroll away — the report cites them.

METHOD
------
Work the phases in order. Each phase feeds the next: validation tells you
whether the address is worth chasing, breaches hand you the usernames and
names that make the social phase productive, and the domain phase only
matters when the domain is not a free provider.

**1. Validation and fingerprinting.**
   Confirm the address is real and learn what kind of address it is.
   - `dig +short MX <domain>` and `dig +short TXT <domain>` — mail provider,
     and whether SPF/DMARC are set up (a corporate tell).
   - `whois <domain>` — registrant, dates, nameservers.
   - Classify the provider: free (gmail/outlook/yahoo), corporate/Workspace,
     or custom domain. This decides the rest of the run — a Gmail address is
     an identity problem, a corporate one is a domain problem.

**2. Gravatar and avatar.**
   The one lookup that needs no API key and often returns a face.
   - `echo -n "<address>" | md5sum` gives the hash.
   - Fetch `https://gravatar.com/avatar/<hash>?s=500&d=404`. A 404 means no
     Gravatar; a 200 is a real profile photo — save it into the evidence dir
     and reverse-image-search it (Google Images, Yandex, TinEye) by URL.

**3. Breach exposure.**
   The fastest source of names, old usernames, passwords and IPs.
   - `https://haveibeenpwned.com/unifiedsearch/<address>` returns the breach
     list without an API key (the `breachedaccount` API endpoint needs one).
   - If the operator has keys for DeHashed / IntelX / LeakCheck, use them —
     read them from the environment, never hard-code them, and say plainly in
     the report which sources actually returned data and which were skipped
     for want of a key.
   - From every breach, record the breach name, its date, the data classes,
     and any username or password (note the breach date beside any password —
     they are almost always old).

**4. Account discovery.**
   Which services hold an account for this address.
   - If `holehe` is installed (`holehe <address>`), run it; it checks 100+
     sites. If it is not installed, say so — do not silently skip the phase.
   - Use the usernames found in phase 3 to run `sherlock`/`maigret` if they
     are available.
   - Back this with manual search where the tools are absent.

**5. Identity correlation.**
   Turn the fragments into a person.
   - Quoted exact-phrase searches for the address across Google, Bing and
     Yandex (they index different things).
   - Correlate the name from the breaches with the handle from the accounts.
   - Collect any phone number, location or secondary email that surfaces,
     and mark where each came from.

**6. Domain intelligence** (only if the domain is not a free provider).
   - `crt.sh` certificate transparency (`https://crt.sh/?q=%25.<domain>`) for
     subdomains, `whois` for registrant data, `theHarvester` for emails and
     hosts if it is installed.

**7. Paste and code leaks.**
   - Search paste sites and code hosts for the address: GitHub code/gist
     search, `psbdmp.ws`, and `site:pastebin.com "<address>"` style queries.

{report_guide}
{confidence_guide}
REPORT
------
Finish by writing `report.md` into the evidence directory. It opens with the
`## Headline` block and the coverage table, then these sections in order:

    1. VALIDATION        provider, MX, free/corporate, disposable?
    2. IDENTITY          name, aliases, handles — each with confidence
    3. CONTACT           phones, addresses, recovery emails — with source
    4. BREACH EXPOSURE   table: breach · date · data classes · password?
    5. ACCOUNTS          table: platform · handle · url · confidence
    6. PROFESSIONAL      employer, title, LinkedIn, email pattern
    7. DOMAIN / TECH     whois, subdomains, cert transparency (if custom)
    8. LEAKS             paste/code exposures
    9. TIMELINE          reconstruction of the digital footprint

Every finding inside those sections is written as a five-line block — What,
Where, Confidence, Means, Verify — and a section that found nothing says so
explicitly rather than being dropped. The format, the coverage table and the
closing `## Gaps and next steps` section are specified in full above; follow
them exactly, they are what makes the report readable.

Close the turn with a short prose summary of the headline findings: how
identifying the address turned out to be, and what the operator should do
with it.
"""


def _stub(name: str, blurb: str, target_label: str) -> Kind:
    return Kind(name=name, blurb=blurb, target_label=target_label, built=False)


# ---------------------------------------------------------------------------
# The github playbook
# ---------------------------------------------------------------------------
# Same shape as the email one, but the target is a handle rather than an
# address, so the run is inverted: email recon starts from an address and hunts
# for a person, this starts from a person and hunts for the address. The commit
# history is the richest vein — a GitHub account leaks its owner's email in
# several places whether or not they meant it to, and that is the pivot that
# turns a username into a whole identity.

GITHUB_PLAYBOOK = """\
# GitHub OSINT recon — {target}

Authorised investigation only. You are running a recon pass on a GitHub
account the operator has declared in scope. Work autonomously through the
phases below; do not stop to ask permission between steps. If a tool is
missing or a request is refused, note it and continue with what works.

EVIDENCE DIRECTORY
------------------
{case_dir}

Write every artefact you produce into that directory: raw API responses,
cloned metadata, and the final `report.md`. Use absolute paths.

BUDGET AND PACE — READ THIS FIRST
---------------------------------
This run is timed and the unauthenticated GitHub API allows only **60 requests
an hour**. Burning them one call per step is what makes this command slow, so
the whole method is built to avoid it:

  * **Prefer `git` over the API.** `git clone --bare` and `git log` are *not*
    metered by the API rate limit. They are the primary tool here, not a
    fallback. A single clone yields every address that ever touched a repo.
  * **One script per phase, not one command per fact.** Write a small bash
    script that fetches everything a phase needs in one shot and writes each
    response to a file under the evidence directory. Do **not** run a separate
    tool call per URL — each tool call is a full model round-trip and is what
    makes the run take minutes.
  * **Check the budget once.** `curl -s https://api.github.com/rate_limit`
    tells you how many calls remain. Read it, then spend them deliberately.

If a token is present (`GITHUB_TOKEN` in the environment, or `gh auth token`),
use it — it raises the limit to 5000/hour and the API becomes cheap. Never
hard-code a token. With no token, keep API calls to the handful that matter
and get everything else from git.

METHOD
------
Work the phases in order. Phase 2 is the point of the run: a GitHub account
almost always leaks its owner's email address, and once you have that, the
account stops being a handle and becomes a person you can pivot on.

**1. Account profiling.**
   Establish who this account is, and do it in ONE script. Write a bash script
   that curls each of these into its own file under the evidence directory,
   sending the auth header only when a token exists:

     - `https://api.github.com/users/<username>`            -> profile.json
     - `https://api.github.com/users/<username>/events/public` -> events.json
     - `https://api.github.com/users/<username>/orgs`       -> orgs.json
     - `https://api.github.com/users/<username>/repos?per_page=100&sort=updated`
                                                            -> repos.json
     - `https://api.github.com/users/<username>/gpg_keys`   -> gpg.json
     - `https://github.com/<username>.keys`                 -> ssh.keys

   Then read the files. From `profile.json`: name, company, blog, location,
   bio, twitter_username, public email (often null, but when present it is a
   CONFIRMED address), created_at, followers, public_repos. `orgs.json`
   usually names their employer. `ssh.keys` holds no address but the
   fingerprints are identifiers worth recording.

**2. Email discovery — the pivot.**
   This is the phase the run exists for, and it is done with **git, not the
   API**. One script, in this order, all writing to the evidence directory:

   - **The clone harvest — do this first, it is unmetered and the richest.**
     From `repos.json`, take the account's own non-fork repos (skip forks —
     the history is someone else's) and clone each shallow-bare, then read the
     whole history in one pass:

         for r in <their repos>; do
           git clone --bare --quiet "https://github.com/<username>/$r.git" \\
             "{case_dir}/clone/$r.git" 2>/dev/null
         done
         for d in {case_dir}/clone/*.git; do
           git -C "$d" log --format='%an <%ae>%n%cn <%ce>' 2>/dev/null
         done | sort -u > {case_dir}/emails.txt

     This gives every name and address that ever committed, including old ones
     the owner has since changed — and old addresses are frequently the ones
     that appear in breaches. It costs zero API calls.
   - **`.mailmap` and manifest files — check the clone for these first.**
     `.mailmap` exists solely to map contributor names to addresses, so it is
     the single best file to read when present. Then grep the clone for the
     addresses developers habitually commit: `package.json` (`author.email`),
     `setup.py`/`pyproject.toml` (`author_email`, `maintainer_email`),
     `Cargo.toml`, `CODE_OF_CONDUCT.md` and `SECURITY.md` (contact addresses),
     `.github/` templates.
   - **Patch headers.** `https://github.com/<username>/<repo>/commit/<sha>.patch`
     begins with `From: Name <email>` in plain text. This is a plain web
     fetch, not an API call, so it does not touch the rate limit — use it to
     confirm an address the clone gave you, or to recover one from a repo you
     did not clone.
   - **Commit metadata via the API** — only if the clone was impossible (no
     network to git, private repos). `https://api.github.com/repos/<username>/<repo>/commits?per_page=100`,
     read `commit.author.email` and `commit.committer.email`. It costs a call,
     so spend it last.
   - **The web profile.** If everything shows a null email, the rendered
     profile at `https://github.com/<username>` sometimes still displays one.
   - Deduplicate every address you find and record, for each, exactly where it
     came from and the date of the commit that carried it. An address from a
     2014 commit is a historical artefact, not necessarily a current contact.

**3. Identity correlation.**
   Turn the account into a person. This phase is mostly local — read what the
   earlier phases already put on disk before fetching anything new.
   - The `name` and `company` fields from `profile.json` against the addresses
     in `emails.txt`.
   - `twitter_username` and any `blog` URL — fetch the blog (a plain web
     fetch), it is often a personal site with a contact page and a fuller bio.
   - Search the username across other services (the same handle on GitLab,
     HackerNews, Reddit, Stack Overflow, Keybase). `sherlock`/`maigret` if
     installed; manual search otherwise.
   - Run each discovered email through the same logic `/osint email` uses —
     Gravatar, breach exposure — to deepen what the handle alone gave you.

**4. Repository inventory.**
   What they build, and what that says about them. Read `repos.json` — it is
   already on disk, so this phase needs no new API calls: languages, topics,
   descriptions, creation dates, fork status.
   - Read the READMEs from the clones you already have. A README often states
     an employer, a team, a conference talk, or a personal URL.
   - Commit timing across the clones reconstructs a working pattern: timezone
     from commit hours, and employment changes from activity gaps.

**5. Secrets and exposure.**
   What the account has accidentally published. Work from the clones — no API
   calls needed.
   - Search the cloned trees for committed secrets: `.env`, `*.pem`, `id_rsa`,
     `credentials.json`, API-key-shaped strings. If `trufflehog` or `gitleaks`
     is installed, run it against the clone; otherwise grep.
   - `https://github.com/search?q=user:<username>+<term>&type=code` reaches
     code you did not clone, but code search is heavily rate-limited — use it
     sparingly and only after the clones are exhausted.
   - Note leaked secrets in the report **without reproducing the secret
     itself** — describe it and its location. The operator's job is to tell
     the owner, not to use it.

**6. Social graph.**
   - Collaborators: who else commits to their repos. The clone already holds
     this — `git -C <clone> shortlog -sne` lists every co-author and their
     address in one command. Also note which orgs they belong to (`orgs.json`).
     Co-authors and reviewers are the closest signal to colleagues.
   - Who they follow, and who follows them, for the professional circle
     (`https://api.github.com/users/<username>/followers` and `/following` —
     one call each, only if the budget allows).

**7. Timeline.**
   - `created_at` is the account's birthday; the first and last commit dates
     across the clones bound the active period; bursts and gaps map to projects
     and job changes. `git log --format='%ad' --date=short` across the clones
     gives this with no API calls. Reconstruct it as a dated sequence.

{report_guide}
{confidence_guide}
An email taken from a commit is CONFIRMED as *an email this account
committed under*; whether it is the person's current address is a separate
claim, and a weaker one.

REPORT
------
Finish by writing `report.md` into the evidence directory. It opens with the
`## Headline` block and the coverage table, then these sections in order:

    1. ACCOUNT          name, id, created, followers, company, location, bio
    2. EMAILS           table: address · source · commit date · confidence
    3. IDENTITY         real name, aliases, handles, blog, twitter — w/ source
    4. ORGANISATIONS    orgs and employer signals
    5. REPOSITORIES     notable repos, languages, what they build
    6. KEYS             GPG uids, SSH fingerprints
    7. EXPOSURE         leaked secrets, sensitive files (described, not quoted)
    8. SOCIAL GRAPH     collaborators, orgs, follows
    9. TIMELINE         dated reconstruction of the account's life

Every finding inside those sections is written as a five-line block — What,
Where, Confidence, Means, Verify — and for every email give the commit or file
it came from. A section that found nothing says so explicitly rather than
being dropped. The format, the coverage table and the closing `## Gaps and
next steps` section are specified in full above; follow them exactly, they are
what makes the report readable.

Close the turn with a short prose summary of the headline findings: who this
account appears to belong to, which email addresses are attributable to it,
and what the operator should do with that.
"""


#: The registry. `email` is built; the rest are advertised and stubbed.
OSINT_KINDS: dict[str, Kind] = {
    "email": Kind(
        name="email",
        blurb="reverse-email recon — breaches, accounts, identity",
        target_label="email address",
        built=True,
        playbook=EMAIL_PLAYBOOK,
    ),
    "phone": _stub("phone", "phone-number recon — carrier, accounts, name", "phone number"),
    "facebook": _stub("facebook", "Facebook profile recon", "profile URL or id"),
    "tiktok": _stub("tiktok", "TikTok account recon", "username or profile URL"),
    "instagram": _stub("instagram", "Instagram account recon", "username or profile URL"),
    "discord": _stub("discord", "Discord account recon", "user id or invite"),
    "github": Kind(
        name="github",
        blurb="GitHub account recon — emails, keys, orgs, repos",
        target_label="GitHub username",
        built=True,
        playbook=GITHUB_PLAYBOOK,
    ),
}

#: Display order for `/osint` with no arguments. `email` first, then the stubs
#: in the order the operator named them.
KIND_ORDER: list[str] = [
    "email", "phone", "facebook", "tiktok", "instagram", "discord", "github",
]


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

# Deliberately loose. A stricter pattern would reject real addresses (long TLDs,
# `+` tags, subdomains, quoted locals); the point is to catch an empty argument
# or an obvious typo, not to be the authority on what an address is.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

# GitHub's own rule is alphanumeric and single hyphens, up to 39 characters,
# and no leading or trailing hyphen. Enforced loosely for the same reason as
# the email pattern: the job is to catch an empty argument or an obvious typo
# (a pasted URL, an email, a space), not to re-implement GitHub's signup form.
_GITHUB_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def normalise_email(target: str) -> str:
    """Trim and lowercase the domain, keeping the local part's case.

    The local part is technically case-sensitive, so it is left alone; the
    domain is not, and lowercasing it makes the gravatar hash and the domain
    lookups agree with what the operator sees.
    """
    target = target.strip()
    if "@" not in target:
        return target
    local, _, domain = target.rpartition("@")
    return f"{local}@{domain.lower()}"


def normalise_github(target: str) -> str:
    """Reduce a target to a bare GitHub handle.

    A pasted profile URL is the common way this gets typed, and a bare handle
    is one edit away from it, so the URL form is accepted and unwrapped rather
    than refused. Only the profile form is handled — a URL with a path is a
    repo, not an account, and is left alone to fail validation.
    """
    handle = target.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if handle.lower().startswith(prefix):
            return handle[len(prefix):]
    return handle


def normalise(kind: Kind, target: str) -> str:
    """The canonical form of *target* for *kind*.

    One dispatch point, so the UI label, the evidence path and the playbook
    cannot disagree about what the target is. Kinds without a normaliser of
    their own just get trimmed.
    """
    if kind.name == "email":
        return normalise_email(target)
    if kind.name == "github":
        return normalise_github(target)
    return target.strip()


def validate(kind: Kind, target: str) -> tuple[bool, str]:
    """Check a target against its kind. Returns (ok, error_message)."""
    target = target.strip()
    if not target:
        return False, f"no {kind.target_label} given"
    if kind.name == "email":
        if not _EMAIL_RE.match(target):
            return False, f"not a valid email address: {target}"
    elif kind.name == "github":
        if not _GITHUB_RE.match(normalise_github(target)):
            return False, f"not a valid GitHub username: {target}"
    return True, ""


# ---------------------------------------------------------------------------
# Case directory
# ---------------------------------------------------------------------------

# Kept conservative on purpose: the label ends up as a path segment, so only
# characters that are safe in a filename survive. A target that is all symbols
# degrades to "target" rather than an empty or dotted name.
_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def case_slug(target: str) -> str:
    """A filesystem-safe, recognisable label for a target."""
    s = _SLUG_RE.sub("-", target.strip()).strip("-.")
    return (s[:64] or "target").lower()


def case_dir(cfg, kind: Kind, target: str) -> Path:
    """Create and return this run's evidence directory.

    ``<state_dir>/osint/<kind>-<slug>-<timestamp>/`` — under state_dir so case
    files land beside sessions rather than in whatever repo the operator
    happens to be in. Created eagerly so the agent can write into it from its
    first tool call.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = case_slug(normalise(kind, target))
    d = Path(cfg.state_dir) / "osint" / f"{kind.name}-{label}-{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _inside_git_tree(path: Path, stop: Path) -> bool:
    """True if *path* sits inside a git checkout somewhere beneath *stop*.

    Used to keep a cloned repository's own files out of the report search: a
    repo can ship a `report.md` of its own, and mistaking it for the run's
    report would archive the wrong file and then delete the real evidence.
    A bare clone is caught by its `.git` suffix, a normal one by its `.git`
    directory.
    """
    for parent in path.parents:
        if parent == stop:
            return False
        if (parent / ".git").exists() or parent.name.endswith(".git"):
            return True
    return False


def _find_report(case: Path) -> Path | None:
    """Locate the report inside *case*, or None if there isn't one.

    The playbook asks for `report.md` at the top level, but a run that wrote it
    into a subdirectory, or under a different name, should still be picked up
    rather than deleted along with the evidence.

    Deliberately not a full `rglob`: the case directory holds git clones, and a
    recursive walk would both trawl a huge checkout and risk matching a file
    that belongs to a cloned repo rather than to this run. Only the top level
    and its immediate subdirectories are considered, and anything inside a git
    tree is skipped.
    """
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


def finalise_case(cfg, case: Path) -> tuple[Path | None, bool]:
    """Archive the report, then delete the bulky case directory.

    Returns ``(report_path, cleaned)``. The contract, in order:

      * No report -> delete nothing. The case dir is left exactly as it is, so
        a run that failed or was interrupted keeps its evidence for the
        operator to inspect. ``(None, False)``.
      * Report found -> move it to ``<state_dir>/reports/<case-name>.md`` (a
        flat, permanent store that outlives the case), then remove the whole
        case directory — clones, raw JSON, avatars and all. ``(path, True)``.

    The move is a real move, not a copy, so the report is never left behind in
    a directory that is about to be deleted. If the move fails for any reason,
    the deletion is skipped entirely: an error here must never destroy the only
    copy of the report. Cleanup is best-effort and must not raise into the UI —
    a recon run that produced a report has succeeded regardless of whether the
    disk tidy-up worked.
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
        # Could not archive — leave everything untouched rather than delete.
        return None, False

    try:
        shutil.rmtree(case)
    except OSError:
        # Report is safely archived; a failed tidy-up is not worth surfacing.
        return dest, False
    return dest, True


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_prompt(kind: Kind, target: str, case: Path) -> str:
    """The turn the agent actually runs.

    A built kind contributes its playbook; the framing around it is shared so
    every kind reads the same way and states the authorised-use condition.
    """
    target = normalise(kind, target)
    header = (
        f"/osint {kind.name} — {kind.target_label}: {target}\n"
        "Authorised investigation only (your own footprint, a consented audit, "
        "or a declared engagement). Work autonomously; do not ask for "
        "confirmation between steps.\n\n"
    )
    if kind.playbook:
        return header + kind.playbook.format(
            target=target,
            case_dir=case,
            report_guide=_REPORT_GUIDE,
            confidence_guide=_CONFIDENCE_GUIDE,
        )
    # Not reachable through the UI (it refuses unbuilt kinds first), but a
    # caller should get something sane rather than a KeyError on the format.
    return header + f"(no playbook is defined for the {kind.name} kind yet)"
