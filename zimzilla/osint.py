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

CONFIDENCE
----------
Every finding carries one of three levels, and you never upgrade one without
evidence:
  CONFIRMED   — verified against a primary source you can cite.
  PROBABLE    — two or more independent sources agree.
  UNVERIFIED  — a single source, not yet corroborated.

REPORT
------
Finish by writing `report.md` into the evidence directory, in this shape:

    # Email OSINT — <address>
    Date: <date>   Analyst: ZimZilla /osint

    1. VALIDATION        provider, MX, free/corporate, disposable?
    2. IDENTITY          name, aliases, handles — each with confidence
    3. CONTACT           phones, addresses, recovery emails — with source
    4. BREACH EXPOSURE   table: breach · date · data classes · password?
    5. ACCOUNTS          table: platform · handle · url · confidence
    6. PROFESSIONAL      employer, title, LinkedIn, email pattern
    7. DOMAIN / TECH     whois, subdomains, cert transparency (if custom)
    8. LEAKS             paste/code exposures
    9. TIMELINE          reconstruction of the digital footprint
    10. NEXT STEPS       what a human should check that you could not

Cite the source beside every claim. Where a phase produced nothing, say so
explicitly — an empty section is a finding. Do not pad the report with a
narrative of what you tried; the operator wants the intelligence and the
gaps, not a diary.

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
cloned metadata, and the final `report.md`. Use absolute paths. Create
subfiles rather than letting long output scroll away — the report cites them.

METHOD
------
Work the phases in order. Phase 2 is the point of the run: a GitHub account
almost always leaks its owner's email address, and once you have that, the
account stops being a handle and becomes a person you can pivot on.

**1. Account profiling.**
   Establish who this account is, without authenticating.
   - `curl -s https://api.github.com/users/<username>` — the public profile:
     name, company, blog, location, bio, twitter_username, public email (often
     null, but when present it is a CONFIRMED address), created_at, followers,
     public_repos.
   - `https://api.github.com/users/<username>/events/public` — recent activity,
     which reveals which repos and orgs they actually touch now.
   - `https://api.github.com/users/<username>/orgs` — organisation
     memberships, which usually name their employer.
   - If a token is available (`GITHUB_TOKEN` in the environment), send it as
     `-H "Authorization: Bearer $GITHUB_TOKEN"` — it raises the rate limit from
     60 to 5000 requests an hour. Never hard-code a token; if there is none,
     work within the unauthenticated limit and space the calls out.

**2. Email discovery — the pivot.**
   This is the phase the run exists for. Try every one of these; they leak
   independently and often disagree, which is itself a finding.
   - **Commit metadata.** The single richest source. Every commit carries an
     author name and email. Fetch a few:
     `https://api.github.com/repos/<username>/<repo>/commits?per_page=100`
     — read `commit.author.email` and `commit.committer.email`.
   - **Patch headers.** For any commit sha,
     `https://github.com/<username>/<repo>/commit/<sha>.patch` begins with
     `From: Name <email>` in plain text. This works even when the JSON API
     redacts, and it is the most reliable single trick here.
   - **The full clone.** When the API is rate-limited, clone shallow and read
     the history directly: `git clone --bare <repo-url> && git -C <repo> log
     --format='%an <%ae>%n%cn <%ce>' | sort -u`. This gives every address that
     ever touched the repo, including old ones the owner has since changed —
     and old addresses are frequently the ones that appear in breaches.
   - **GPG and SSH keys.** `https://api.github.com/users/<username>/gpg_keys`
     returns the public key, whose uid typically carries a real name and email.
     `https://github.com/<username>.keys` returns SSH public keys — record the
     fingerprints as identifiers even though they hold no address.
   - **Manifest and config files.** Read the account's repos for the addresses
     developers habitually commit: `package.json` (`author.email`),
     `setup.py`/`pyproject.toml` (`author_email`, `maintainer_email`),
     `Cargo.toml`, `.mailmap` (a file whose entire purpose is mapping
     contributor names to addresses — check it first when it exists),
     `CODE_OF_CONDUCT.md` and `SECURITY.md` (contact addresses), and
     `.github/` templates.
   - **The web profile.** If the API shows a null email, the rendered profile
     at `https://github.com/<username>` sometimes still displays one.
   - Deduplicate every address you find and record, for each, exactly where it
     came from and the date of the commit that carried it. An address from a
     2014 commit is a historical artefact, not necessarily a current contact.

**3. Identity correlation.**
   Turn the account into a person.
   - The `name` and `company` fields against the emails from phase 2.
   - `twitter_username` and any `blog` URL — fetch the blog, it is often a
     personal site with a contact page and a fuller bio.
   - Search the username across other services (the same handle on GitLab,
     HackerNews, Reddit, Stack Overflow, Keybase). `sherlock`/`maigret` if
     installed; manual search otherwise.
   - Run each discovered email through the same logic `/osint email` uses —
     Gravatar, breach exposure — to deepen what the handle alone gave you.

**4. Repository inventory.**
   What they build, and what that says about them.
   - `https://api.github.com/users/<username>/repos?per_page=100&sort=updated`
     — languages, topics, descriptions, creation dates, whether each is a fork.
   - Read the READMEs. A README often states an employer, a team, a
     conference talk, or a personal URL.
   - Commit timing across repos reconstructs a working pattern: timezone from
     commit hours, and employment changes from activity gaps.

**5. Secrets and exposure.**
   What the account has accidentally published.
   - Search their repos for committed secrets: `.env`, `*.pem`, `id_rsa`,
     `credentials.json`, API-key-shaped strings. Use
     `https://github.com/search?q=user:<username>+<term>&type=code` and, if
     installed, `trufflehog` or `gitleaks` against a clone.
   - Note leaked secrets in the report **without reproducing the secret
     itself** — describe it and its location. The operator's job is to tell
     the owner, not to use it.

**6. Social graph.**
   - Collaborators: who else commits to their repos
     (`https://api.github.com/repos/<user>/<repo>/contributors`), and which
     orgs they belong to. Co-authors and reviewers are the closest signal to
     colleagues.
   - Who they follow, and who follows them, for the professional circle.

**7. Timeline.**
   - `created_at` is the account's birthday; the first and last commit dates
     across all repos bound the active period; bursts and gaps map to projects
     and job changes. Reconstruct it as a dated sequence.

CONFIDENCE
----------
Every finding carries one of three levels, and you never upgrade one without
evidence:
  CONFIRMED   — verified against a primary source you can cite.
  PROBABLE    — two or more independent sources agree.
  UNVERIFIED  — a single source, not yet corroborated.

An email taken from a commit is CONFIRMED as *an email this account
committed under*; whether it is the person's current address is a separate
claim, and a weaker one.

REPORT
------
Finish by writing `report.md` into the evidence directory, in this shape:

    # GitHub OSINT — <username>
    Date: <date>   Analyst: ZimZilla /osint

    1. ACCOUNT          name, id, created, followers, company, location, bio
    2. EMAILS           table: address · source · commit date · confidence
    3. IDENTITY         real name, aliases, handles, blog, twitter — w/ source
    4. ORGANISATIONS    orgs and employer signals
    5. REPOSITORIES     notable repos, languages, what they build
    6. KEYS             GPG uids, SSH fingerprints
    7. EXPOSURE         leaked secrets, sensitive files (described, not quoted)
    8. SOCIAL GRAPH     collaborators, orgs, follows
    9. TIMELINE         dated reconstruction of the account's life
    10. NEXT STEPS      what a human should check that you could not

Cite the source beside every claim, and for every email give the commit or
file it came from. Where a phase produced nothing, say so explicitly — an
empty section is a finding. Do not pad the report with a narrative of what
you tried; the operator wants the intelligence and the gaps, not a diary.

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
        return header + kind.playbook.format(target=target, case_dir=case)
    # Not reachable through the UI (it refuses unbuilt kinds first), but a
    # caller should get something sane rather than a KeyError on the format.
    return header + f"(no playbook is defined for the {kind.name} kind yet)"
