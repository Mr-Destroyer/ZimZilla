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

#: Public aliases for the two guides above, so another command can reuse the
#: same report shape instead of copying it. `/leadgen` imports these: the
#: point of the pair being here is that a report's structure is defined once
#: and the two commands cannot drift into different formats.
REPORT_GUIDE = _REPORT_GUIDE
CONFIDENCE_GUIDE = _CONFIDENCE_GUIDE


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


# ---------------------------------------------------------------------------
# The discord playbook
# ---------------------------------------------------------------------------
# Discord is the most closed of the three built kinds, and the playbook is
# written around that rather than pretending otherwise. Measured on this box,
# unauthenticated:
#
#   * `api/v10/users/<id>` and `users/@me` -> 401. There is NO unauthenticated
#     user endpoint. Discord removed anonymous profile browsing; the old
#     `discordapp.com/api/v6/users/<id>` path that old guides cite is gone.
#   * `discord.com/users/<id>` returns a 75 KB HTML shell with generic
#     `og:title` "Discord - Group Chat That's All Fun & Games" and no snowflake
#     anywhere in it — it is a JS app, not a server-rendered profile.
#   * `guilds/<id>/widget.json` works and is the one rich native source, but it
#     is **anonymised**: member ids come back as "0","1","2"… and usernames
#     truncated to "4...", with discriminator "0000". It is a roster of who is
#     online *in a guild*, not an identity.
#   * `unique-username/username-attempt-unauthed` answers taken/available, but
#     rate-limits hard and *escalates* — the retry_after climbed from 161s to
#     2028s across a handful of probes during research. It is a one-shot check,
#     not a loop.
#
# So the honest shape of a Discord run is: pin down the numeric id, get what
# the id alone yields (creation date, default avatar, account era), then do the
# real work *off* Discord — the cross-platform handle pivot, which is where a
# username actually turns into a person. The playbook says all of this plainly,
# including what is impossible, because an agent that spends its phases
# hammering a 401 endpoint produces a report that says nothing.

DISCORD_PLAYBOOK = """\
# Discord OSINT recon — {target}

Authorised investigation only. You are running a recon pass on a Discord
account the operator has declared in scope. Work autonomously through the
phases below; do not stop to ask permission between steps. If a tool is
missing or a request is refused, note it and continue with what works.

EVIDENCE DIRECTORY
------------------
{case_dir}

Write every artefact you produce into that directory: raw API responses, HTML
captures, downloaded avatars, and the final `report.md`. Use absolute paths.

WHAT DISCORD WILL AND WILL NOT GIVE YOU — READ THIS FIRST
--------------------------------------------------------
This run is the most constrained of the OSINT kinds, and knowing the walls in
advance is most of the work. Unauthenticated, Discord gives you **almost
nothing about a user**:

  * **There is no unauthenticated user endpoint.** `GET
    https://discord.com/api/v10/users/<id>` returns `401 Unauthorized`. Every
    older guide that cites `discordapp.com/api/v6/users/<id>` is describing an
    API that no longer answers; do not spend a phase rediscovering the 401.
  * **The profile page is a JavaScript shell.** `https://discord.com/users/<id>`
    returns ~75 KB of HTML whose `og:title` is the generic Discord marketing
    string. There is no profile JSON embedded in it. Fetch it once for the
    record, then move on — do not parse it looking for data that is not there.
  * **The one username probe is rate-limited and escalates.** `POST
    https://discord.com/api/v10/unique-username/username-attempt-unauthed`
    with body `{{"username":"<handle>"}}` returns `{{"taken":true|false}}` — the
    only unauthenticated way to test whether a handle exists. It rate-limits
    almost immediately and the `retry_after` **climbs** (measured: 161s, then
    2028s within a handful of requests). Spend it **once**, deliberately, and
    record the answer. Never put it in a loop or run it per-candidate.
  * **What the id alone gives you is real and free** — do that first, it costs
    nothing and cannot be rate-limited (see phase 2).

The consequence: **the payload of this run is off Discord, not on it.** A
Discord handle is very often the same string as the person's handle elsewhere,
and those platforms are far more generous. Phases 4 and 5 are where this run
earns its keep; phases 1-3 exist to give you the id and the exact spelling to
pivot on.

BUDGET AND PACE
---------------
  * **One script per phase, not one command per fact.** Each tool call is a
    full model round-trip; a script that writes five files in one call is five
    times faster than five calls. This matters most here, because so much of
    the run is fetches.
  * **`curl -sS -m 15`** on every request, with a real User-Agent. A hang with
    no timeout is a stalled run.
  * **Record every refusal as a finding.** A 401, a 429, a dead service — each
    is a coverage row, not a silence. The report's `Gaps and next steps`
    section is where the operator learns what this run could not reach.

METHOD
------
Work the phases in order. Phases 1-3 establish the account and the exact
identifier; phases 4-5 turn it into a person.

**1. Resolve the target to a numeric id.**
   Everything downstream keys on the snowflake, so establish it first. The
   operator may hand you either form:
   - **A numeric id** (17-20 digits) — that is the target; use it directly.
   - **A username** — Discord dropped discriminators in 2023, so a modern
     handle is a bare string (`someuser`), while an old account may still be
     cited as `someuser#1234`. Note which form you were given.
   To turn a handle into an id, in order of preference:
   - **`https://discord.id/` and `https://discordlookup.com/`** — public lookup
     sites that resolve a username or id to a profile card. Both are
     Cloudflare-fronted and their JSON API paths move; fetch the HTML page and
     read the card rather than guessing an endpoint. Treat anything they return
     as UNVERIFIED — they are third-party caches, not Discord.
   - **The username probe** (one call, see above) to confirm the handle exists
     at all before you invest in it.
   - **`sherlock`/`maigret`** if installed — note that sherlock's Discord check
     uses the same rate-limited endpoint, so it will not do better than one
     call, and it may be blocked entirely.

**2. The free id-derived facts — do these first, they cost nothing.**
   A snowflake is not opaque: its high bits are a timestamp, and the default
   avatar is derived from it. Both are computed locally, with no request and no
   rate limit. Write a script that, for the id, produces:
   - **Account creation time.** `(id >> 22) + 1420070400000` is the Unix
     millisecond the account was created. This is exact and is the anchor fact
     of the whole run — an account created in 2016 and one created last month
     are different kinds of finding.
   - **The default avatar.** If the account has never set a custom avatar, its
     avatar is `https://cdn.discordapp.com/embed/avatars/<(id >> 22) % 6>.png`.
     Fetch it (a plain CDN GET, no auth, no limit) and save it. A default avatar
     is itself a finding: it is weak evidence the account is a lurker, a bot, or
     freshly made.
   - **The custom avatar**, if you have the avatar hash from any source, is
     `https://cdn.discordapp.com/avatars/<id>/<hash>.png?size=1024`. Save it and
     reverse-image-search it by URL (Google Images, Yandex, TinEye) — the one
     route from a Discord account to a face.
   Record the creation timestamp and both avatar results before anything else,
   so that even if every later phase is blocked the run has produced something.

**3. Discord-native surface — what is actually reachable.**
   - **Guild widget** — the one rich unauthenticated endpoint.
     `https://discord.com/api/v10/guilds/<guild_id>/widget.json` returns the
     guild's name, `presence_count`, channel names and an `instant_invite`.
     **It is anonymised: member `id` values come back as sequential strings
     ("0","1","2"…), usernames are truncated to a few characters plus "…", and
     `discriminator` is "0000".** Do not try to attribute members from it — the
     ids are not real and the names are cut. Its value is the *guild*: which
     servers the target is associated with, their size, and the invite code.
     Not every guild enables it; a `403`/`404` means widgets are off, which is
     normal and is a coverage row, not a failure.
   - **Invites** — `https://discord.com/api/v10/invites/<code>?with_counts=true`
     resolves an invite to its guild: name, description, `approximate_member_count`,
     `approximate_presence_count`, channel, and the guild's features. Useful for
     characterising a community the target is part of.
   - **Guild metadata** — `https://discord.com/api/v10/guilds/<id>` and
     `/guilds/<id>/preview` are worth one attempt each; they are sometimes open
     for large public guilds. Do not retry a 401.
   - **If a token is available** (`DISCORD_TOKEN` in the environment), the
     picture changes completely — `users/<id>`, `users/<id>/profile` and mutual
     guilds all answer, giving username, global display name, avatar/banner
     hashes, account flags and connected accounts. **Use it only when the
     operator has provided their own token, never a scraped or self-bot one**,
     and never hard-code it. If no token is present, say so in the report as a
     coverage gap rather than treating it as a dead end — it is the single
     largest difference between a thin run and a rich one.
   - **Note explicitly in the report** which of the above were unavailable and
     why. "Discord exposes no user data without authentication" is the correct,
     useful finding here, not an empty section.

**4. The cross-platform handle pivot — the payload of this run.**
   This is where a Discord username becomes a person, and it is almost entirely
   off Discord. The handle the operator gave you is frequently the same string
   the target uses on platforms that *do* answer.
   - **Run `sherlock <handle>` and `maigret <handle>` if installed** (maigret
     first if present — far more sites, and it reports confidence per hit).
     Write the full output to the evidence directory. These are the highest
     yield per second in the entire run.
   - **Try the obvious variants** — the handle with and without separators,
     with a trailing digit, capitalisation changes. A Discord handle is often a
     slight mutation of the same person's GitHub or Reddit name.
   - **For every confirmed hit, capture the platform, the URL and the evidence
     that it is the same person** (matching avatar, matching bio, linked
     account, matching creation era). A shared handle is PROBABLE at best; it
     is CONFIRMED only when something links the two accounts explicitly.
   - **Feed any email or other handle you recover back through the existing
     kinds** — `holehe` for accounts on an address, Gravatar for a face, the
     breach sources for a name. This is the same logic `/osint email` and
     `/osint github` already use; reuse it rather than reimplementing it.
   - **Reverse-image-search the avatar** from phase 2. A Discord avatar reused
     as a profile picture elsewhere is a strong same-person link.

**5. Identity correlation and breach exposure.**
   - **Breach sources keyed on whatever the pivot produced.** Discord itself
     has had significant breaches (a 2025 support-ticket exposure among them) —
     if the target's data appears, that is a CONFIRMED finding about the
     *account*, and it is worth stating precisely which breach and what fields.
   - **Paste and code search** for the handle: GitHub code/commit search, grep.app,
     psbdmp, searchcode. Developers leak Discord webhooks and bot tokens in
     repos constantly — if the target has, note it as exposure **without
     reproducing the token itself** (describe it and its location; the
     operator's job is to tell the owner, not to use it).
   - **Correlate everything against the creation date from phase 2.** An account
     created in 2016 that shares a handle with a 2016 GitHub account is a much
     stronger link than one created last week. The timeline is the disambiguator
     between "the same person" and "a common word".

**6. Timeline.**
   Reconstruct a dated sequence: account creation (phase 2, exact), the first
   and last activity you can date from the guild and platform evidence, and any
   breach dates. Gaps and bursts are as informative here as in the other kinds.

{report_guide}
{confidence_guide}
A handle match across platforms is **PROBABLE** by default. It is CONFIRMED
only when something explicitly links the accounts — a shared avatar hash, a
cross-reference in a bio, a linked account, a shared recovery address. Do not
upgrade a plausible handle match to CONFIRMED on the strength of the handle
alone; a common word is not an identity.

The account-creation timestamp decoded from the snowflake is **CONFIRMED** —
it is arithmetic on the id, not a source's claim.

REPORT
------
Finish by writing `report.md` into the evidence directory. It opens with the
`## Headline` block and the coverage table, then these sections in order:

    1. ACCOUNT          id, username (and legacy discriminator if given),
                        creation date, account age, avatar (custom or default)
    2. ID DERIVATION    the snowflake arithmetic — creation time, default
                        avatar index — shown so the operator can check it
    3. GUILDS           guilds and invites reached, with size and description;
                        state plainly that widget members are anonymised
    4. HANDLE PIVOT     table: platform · URL · evidence · confidence
    5. IDENTITY         names, aliases, linked accounts, avatar reuse
    6. EXPOSURE         breaches, leaked tokens/webhooks (described, not quoted)
    7. TIMELINE         dated reconstruction, anchored on the creation time
    8. LIMITS           exactly what Discord refused without a token, and what
                        a token would have added

Section 8 is not padding — for this kind it is one of the most useful parts of
the report, because it tells the operator precisely what was reachable and what
was not, and whether supplying a token is worth it next time.

Every finding inside those sections is written as a five-line block — What,
Where, Confidence, Means, Verify — and for every pivot give the URL and the
evidence that links it. A section that found nothing says so explicitly rather
than being dropped. The format, the coverage table and the closing `## Gaps and
next steps` section are specified in full above; follow them exactly, they are
what makes the report readable.

Close the turn with a short prose summary of the headline findings: what the id
established, which other platforms the handle resolved on, and what the operator
should do with that.
"""







# ---------------------------------------------------------------------------
# The Facebook playbook
# ---------------------------------------------------------------------------
# Handed to the agent verbatim as the turn's user message, with {target},
# {case_dir}, {report_guide} and {confidence_guide} substituted by
# `build_prompt`. LITERAL BRACES ARE DOUBLED so `str.format` does not raise.
#
# The "walls" section is the point of this playbook. Facebook is the most
# closed of the OSINT kinds measured so far — more so than Discord, which at
# least has a widget endpoint and a username probe. What Facebook leaves open
# is exactly two things: an avatar endpoint that doubles as an existence
# oracle, and the Internet Archive. Everything else the agent might try is a
# measured dead end, and the playbook names each one so no phase is wasted
# rediscovering a 400.

FACEBOOK_PLAYBOOK = """\
# Facebook OSINT recon — {target}

Authorised investigation only. You are running a recon pass on a Facebook
account the operator has declared in scope. Work autonomously through the
phases below; do not stop to ask permission between steps. If a tool is
missing or a request is refused, note it and continue with what works.

EVIDENCE DIRECTORY
------------------
{case_dir}

Write every artefact you produce into that directory: raw API responses, HTML
captures, the downloaded avatar, Wayback captures, and the final `report.md`.
Use absolute paths.

WHAT FACEBOOK WILL AND WILL NOT GIVE YOU — READ THIS FIRST
----------------------------------------------------------
This is the most closed surface of the OSINT kinds, and knowing the walls in
advance is most of the work. **Facebook serves an unauthenticated client
almost nothing, and — unusually — it does not even serve a login page.** The
public web surface returns a generic error, not a sign-in wall:

  * **`https://www.facebook.com/<user>` returns HTTP 400** with a 1542-byte
    `text/html` body that is nothing but `<title>Error</title>` and the line
    "Sorry, something went wrong. We're working on getting this fixed as soon
    as we can." It is **not** a profile and **not** a login redirect. There is
    no `og:` tag, no JSON blob, nothing to parse. Fetch it **once** for the
    record and then stop — do not spend a phase trying to extract data that is
    not in the response.
  * **The mobile surfaces are no better.** `mbasic.facebook.com/<user>` returns
    a 400 XHTML-MP error page of the same kind, and `m.facebook.com/<user>`
    **301-redirects straight back to `www`** — which is the 400 above. Neither
    is a route around anything.
  * **The crawler User-Agent trick is dead.** Spoofing `Googlebot` gets you a
    301 to the same error shell and **no** `og:` meta tags. Every guide that
    still recommends this is describing a Facebook that no longer answers.
  * **The Graph node endpoints demand a token.** `https://graph.facebook.com/<id>`
    returns 400 `{{"error":{{"message":"An access token is required to request this
    resource.","code":104}}}}`. A *bare username* node fails differently —
    `https://graph.facebook.com/<username>` returns **403**
    `{{"error":{{"message":"(#200) Provide valid app ID","code":200}}}}`. Both are
    walls; note which one you hit and move on. Do not retry either.
  * **`graph.facebook.com/me`** returns 400, code 2500 — meaningless without a
    token, do not run it.

  What Facebook *does* leave open is exactly two things, and the run is built
  on them:

  * **The avatar endpoint, which is also an existence oracle** (phase 2). It
    needs no token at all.
  * **The Internet Archive**, which holds far more than the live site will
    ever give you (phase 3).

BUDGET AND PACE
---------------
  * **One script per phase, not one command per fact.** Each tool call is a
    full model round-trip; a script that writes five files in one call is five
    times faster than five calls.
  * **`curl -sS -m 15`** on every request, with a real desktop User-Agent.
    A hang with no timeout is a stalled run.
  * **Do not loop against the live site.** This is a recon pass, not a
    scraper: one deliberate request per fact, and record what it returned.
    The playbook's value is telling you what a *single* request yields.
  * **Record every refusal as a finding.** A 400, a 403, a dead service — each
    is a coverage row, not a silence.

METHOD
------
Work the phases in order. Phases 1-2 establish the account and its avatar;
phase 3 mines the archive; phases 4-5 turn it into a person.

**1. Normalise the target.**
   Establish which of the four identifier forms the operator handed you — a
   numeric id, a vanity username, a `pfbid`, or a URL that contains one. Note
   which form it was: a numeric id is durable, a vanity username can be
   changed by its owner, and a `pfbid` is opaque. If the operator gave you a
   URL of the `/people/<Display-Name>/<id>/` shape, **keep the display name** —
   it is frequently the account holder's real name, which the vanity username
   usually is not, and it is a free finding.

**2. The picture oracle — the one live unauthenticated endpoint.**
   This is the highest-value request in the run and it costs nothing.
   - **Fetch the avatar:** `curl -sS -m 15 -A "$UA" -o avatar.jpg -w '%{{http_code}}'
     'https://graph.facebook.com/<id-or-username>/picture?type=large'`
     A real account returns **302** to a `scontent.*.fbcdn.net` URL; `-L` follows
     it to the actual JPEG. Both a numeric id and a vanity username work, and
     `'https://graph.facebook.com/picture?id=<x>'` is equivalent. Save the image
     into the evidence directory.
   - **The same request is an existence test.** A **400** with
     `{{"error":{{"message":"Unsupported get request. Object with ID '<x>' does not
     exist, cannot be loaded due to missing permissions, or does not support
     this operation..."}}}}` means the object does not exist (or is not
     publicly reachable). A 302 means it does. **This is the only free,
     unauthenticated "does this account exist" probe Facebook offers** — use it
     to confirm the target is real before investing in later phases, and to
     check a candidate username. Spend it deliberately, once per candidate.
   - **Hash the avatar to detect the default.** Accounts with no custom picture
     return a shared silhouette. Measured: ids 4, 5 and 6 all return a
     byte-identical 1876-byte image, md5 `3e8f62364b0f574a7d18a6c8b26730f1`.
     If your fetched avatar matches that digest, the account has **no custom
     profile picture** — a real finding about how the account is used, not a
     failure.
   - **`?type=large` requests a 200x200 image** (the CDN URL's `stp=` parameter
     reads `s200x200` where the default reads `s50x50`). Always ask for
     `large`; the default is a 50px thumbnail.
   - **Reverse-image-search the real avatar** by URL (Google Images, Yandex,
     TinEye). This is the one route from a Facebook account to a face and to
     the same photo reused on other platforms.
   - **Do NOT try to read the numeric id out of the CDN filename.** The avatar
     filename looks like `<a>_<b>_<c>_n.jpg` and it is tempting to read a field
     as the owner's id. Measured and **refuted**: for the `nasa` page, the
     middle and last fields both return 400 (they are not object ids), and the
     first field returns a *different* real avatar, not the page's own. The
     first field is worth **one** deliberate check as a possible related
     account (it may be the admin who uploaded a Page's picture) but it is
     **UNVERIFIED** and must never be reported as the account owner.

**3. Wayback — the richest source, and it needs no key.**
   The live site gives you nothing (see above). The archive gives you whole
   profile pages. This phase carries the run.
   - **Find out what is held, cheaply:**
     `curl -sS 'https://archive.org/wayback/available?url=facebook.com/<user>'`
     returns JSON with a `closest` snapshot (status, timestamp, URL) or an empty
     `archived_snapshots` object. No key required.
   - **Enumerate every capture** with the CDX API:
     `curl -sS 'http://web.archive.org/cdx/search/cdx?url=facebook.com/<user>&output=json&limit=50&collapse=timestamp:6'`
     Each row is `[urlkey, timestamp, original, mimetype, statuscode, digest,
     length]` — i.e. **every archived capture with its exact date and size**.
     The sequence of dates and sizes is itself a timeline: it shows when the
     account was active and when its page changed.
   - **Fetch the captures.** Use the timestamped URL from CDX rather than a
     bare year, and **add `_fb_noscript=1` to the target URL** — measured, the
     plain capture can be a 32 KB JavaScript shell while the `_fb_noscript=1`
     variant returns 122 KB of real content:
     `curl -sSL -m 30 -A "$UA" -o snap.html
      'https://web.archive.org/web/<timestamp>/https://www.facebook.com/<user>?_fb_noscript=1'`
   - **Mine each capture for:** `og:title` and `og:description` (measured: a
     2023 capture of a personal profile returned `og:title` = the person's real
     name and a populated `og:description`), the page's own visible text, any
     `entity_id`/`userID`/`profile_id` numeric identifiers in the embedded
     JSON (measured: a 2016 capture leaked `"entity_id":"54971236771"`, an id
     that is **not** visible on the live surface), friends/connections counts,
     and any linked external URL.
   - **Note the asymmetry** in the report: a *Page* is archived far more often
     than a personal profile, because crawlers reach it. An empty result for a
     personal account is normal and is a coverage row, not a failure.

**4. Cross-platform pivot — where a Facebook account becomes a person.**
   Facebook is closed, so the payload is off-platform, exactly as with the
   Discord kind.
   - **The display name is the pivot.** A real name from phase 1 or phase 3
     resolves on the platforms that *do* answer — LinkedIn, GitHub, X, Reddit,
     Instagram, and the person-search engines. Search the name with the
     location or employer that the archived capture supplied.
   - **The avatar is the second pivot.** Reverse-image-search it (phase 2) and
     compare it against the avatars of any candidate account on another
     platform — a shared image is a strong same-person link.
   - **Run `sherlock`/`maigret` if installed** on the vanity username and on
     plausible variants (separators added or removed, trailing digits). A
     Facebook vanity username is often the same string as the person's handle
     elsewhere.
   - **For every hit, capture the URL and the evidence that links it** —
     matching avatar, matching name, cross-reference in a bio. A shared name
     alone is **PROBABLE** at most; it is CONFIRMED only when something
     explicitly ties the accounts together.

**5. Identity correlation and breach exposure.**
   - **Breach sources keyed on whatever the pivot produced.** Facebook's own
     breaches are the obvious ones to check, and the significant ones are
     historical — if the target's data appears, state precisely which breach
     and which fields, and mark it as a finding about the *account*.
   - **The classic Facebook-specific exposure to check:** a phone number or
     email used as the account's recovery contact, if any source exposes it.
     Treat it as the strongest kind of link between the Facebook account and
     the rest of the person's footprint.
   - **Paste and code search** for the vanity username and any recovered email.
   - **Correlate everything against the archive timeline from phase 3.** An
     account whose captures begin in 2011 and a GitHub account created in 2011
     sharing a name is a far stronger link than a name match alone. The dated
     captures are the disambiguator between "the same person" and "a common
     name".
   - **Describe a leaked secret, never reproduce it.** If a breach or a paste
     hands you a password, a token or a session cookie, record *that it exists*,
     which breach it came from, and which account it belongs to — do not write
     the value into the report. The report is an intelligence product that gets
     read and archived; a credential copied into it becomes a live credential in
     a file, which is a worse outcome than the breach being reported at all.

**6. Timeline.**
   Reconstruct a dated sequence from the CDX capture list: earliest capture,
   latest capture, the points where the page size or content changed (a rename,
   a profile-picture change, a status change), and any breach dates. Gaps and
   bursts are as informative here as in the other kinds.

{report_guide}
{confidence_guide}
The **existence result** from phase 2 is CONFIRMED — it is Facebook's own
answer to a direct request, not a third party's claim.

The **default-avatar determination** from phase 2 is CONFIRMED — it is a hash
comparison against a measured fingerprint.

Anything read from a **Wayback capture** is CONFIRMED as *what the page said on
that date*, and must be reported with that date attached. It is **not**
evidence of the account's state today; a 2016 capture is a 2016 fact.

A **name match** across platforms is PROBABLE by default and CONFIRMED only
when something explicitly links the accounts. A common name is not an identity.

The **CDN-filename first field** from phase 2 is UNVERIFIED and must be
labelled as such — it resolves to a real object, but nothing measured ties it
to the account owner.

REPORT
------
Finish by writing `report.md` into the evidence directory. It opens with the
`## Headline` block and the coverage table, then these sections in order:

    1. ACCOUNT          the identifier as given and as normalised, which form
                        it was, whether it exists (phase 2), and the display
                        name if the target URL carried one
    2. AVATAR           the fetched image, its hash, whether it is the default
                        silhouette, and the reverse-image-search result
    3. ARCHIVE          the CDX capture table — date, status, size — and what
                        the richest capture contained (og:title, description,
                        any leaked numeric ids)
    4. PIVOT            table: platform · URL · evidence · confidence
    5. IDENTITY         names, aliases, linked accounts, avatar reuse
    6. EXPOSURE         breaches and any recovery contact that surfaced
    7. TIMELINE         dated reconstruction, anchored on the capture list
    8. LIMITS           exactly what Facebook refused without a token, and what
                        a token would have added

Section 8 is not padding — for this kind it is one of the most useful parts of
the report. Facebook is the most closed surface measured so far, and an
operator needs to know precisely which of the walls above were hit, that the
live web surface returned an error rather than a profile, and that the run's
substance therefore came from the archive and the off-platform pivot. Say
plainly whether an authenticated session or an app token is worth obtaining
next time.

Every finding inside those sections is written as a five-line block — What,
Where, Confidence, Means, Verify — and for every pivot give the URL and the
evidence that links it. A section that found nothing says so explicitly rather
than being dropped. The format, the coverage table and the closing `## Gaps and
next steps` section are specified in full above; follow them exactly, they are
what makes the report readable.

Close the turn with a short prose summary of the headline findings: what the
identifier established, what the archive held, which platforms the name and
avatar resolved on, and what the operator should do with that.
"""




# ---------------------------------------------------------------------------
# The TikTok playbook
# ---------------------------------------------------------------------------
# Handed to the agent verbatim as the turn's user message, with {target},
# {case_dir}, {report_guide} and {confidence_guide} substituted by
# `osint.build_prompt`. LITERAL BRACES ARE DOUBLED throughout — the JSON
# snippets below are `{{` / `}}` in the source and render as single braces in
# the prompt. Every wall named in the first section is a measured result; the
# full provenance — what was measured, against which handle, and what stayed
# UNVERIFIED — is the `# TikTok — measured on this box` block below the
# registry, beside the other two.

TIKTOK_PLAYBOOK = """\
# TikTok OSINT recon — {target}

Authorised investigation only. You are running a recon pass on a TikTok
account the operator has declared in scope. Work autonomously through the
phases below; do not stop to ask permission between steps. If a tool is
missing or a request is refused, note it and continue with what works.

EVIDENCE DIRECTORY
------------------
{case_dir}

Write every artefact you produce into that directory: the raw HTML, the
extracted JSON, downloaded avatars, and the final `report.md`. Use absolute
paths.

WHAT TIKTOK WILL AND WILL NOT GIVE YOU — READ THIS FIRST
--------------------------------------------------------
TikTok is generous with one thing and hostile with everything else. The good
news is that a **complete profile — id, nickname, bio, creation date, avatar,
follower counts, external bio link — is sitting in the profile HTML and needs
no login**. The bad news is that every *other* surface is either dead, empty,
or rate-limited, and the two obvious ways to test whether a handle exists both
lie to you. Knowing the walls in advance is most of the work. All of the
following were measured on a live unauthenticated request:

  * **The profile HTML is the payload, and it is unauthenticated.**
    `https://www.tiktok.com/@<handle>` returns ~85-89 KB of HTML containing a
    `<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">` element. Inside it,
    under `__DEFAULT_SCOPE__["webapp.user-detail"]`, is the full profile. This
    one request is worth more than every other phase combined. Do it first.

  * **There are ZERO `og:` meta tags in the raw HTML.** Every older guide
    tells you to read `og:title` / `og:description`. Measured: `grep` for
    `og:title` returns **nothing**, on a real profile and on a nonexistent
    one. Those tags are injected by JavaScript after load; `curl` never sees
    them. Do not write a parser around them and do not conclude the page is
    broken when they are absent — they are always absent.

  * **`<title>` is always the generic `TikTok - Make Your Day`** — for a real
    handle and for a nonexistent one. It carries no information at all.

  * **The HTTP status code does not tell you whether a handle exists.** A
    taken handle, a never-registered handle, a 1-character handle and a
    26-character handle ALL return `HTTP 200` with a ~85 KB body. You cannot
    probe existence with `curl -o /dev/null -w '%{{http_code}}'`. The only
    signal is inside the JSON (see phase 2).

  * **The "free handle" signal is real but its message is a lie.** For a
    handle that does not exist, `webapp.user-detail` is
    `{{"statusCode": 10221, "statusMsg": "user banned", "needFix": false}}`
    with **no `userInfo` key**. Measured: a handle that had *never been
    registered* came back as `"user banned"`. Do not report that string as
    fact — report "no profile served for this handle".

  * **oembed is rate-limited to roughly one call per long cool-off, and the
    limit is sticky.** `https://www.tiktok.com/oembed?url=...` returns a clean
    748-byte JSON on success, but the dominant response is
    `HTTP 429` with the plain-text body `ratelimit triggered`. Measured:
    429 on the first call; 200 once after a ~20s pause; then 429 on three
    rapid calls; still 429 after 75s; still 429 after ~2 minutes. **Spend it
    once, deliberately, and never loop it.** Its payload is only a display
    snippet — `title`, `author_name`, `author_url`, an embed `html` block — no
    id, no avatar, no counts. It is not worth burning the budget on.

  * **The oembed limiter and the HTML limiter are independent.** Measured in
    the same second: profile HTML `200` (88737 bytes) while oembed `429`. So
    an oembed 429 does **not** mean the run is blocked — keep going on HTML.

  * **`m.tiktok.com` is dead.** `https://m.tiktok.com/@<handle>` returns
    `HTTP 404`, 250 bytes, `<title>404 Not Found</title>`, no blob. There is
    no mobile surface to fall back on. Do not spend a phase on it.

  * **The embed path is an empty shell.** `https://www.tiktok.com/embed/@<handle>`
    returns `HTTP 200` and 43 KB but contains **no rehydration blob**. It also
    sits under an explicit `Disallow: /embed/@` in robots.txt.

  * **The internal API returns 200 with an EMPTY BODY.**
    `https://www.tiktok.com/api/user/detail/?uniqueId=<handle>&aid=1988`
    answers `HTTP 200`, `content_type: application/json`, and **size 0**. It
    needs a request signature (X-Bogus / msToken) that an unauthenticated
    client does not have. A 200 here is not a success — check the byte count,
    not the status. Do not retry it and do not treat the empty body as "user
    not found".

  * **robots.txt names Claude and most other AI agents with `Disallow: /`**,
    and disallows `/embed/@`, `/auth`, `/link`, `/search?`,
    `/search/user?q=`, `*/directory/`, `/api/share/settings` and
    `/api/recommend/embed_videos`. The `/@handle` profile path is **not** in
    the `*` allow-list. Record this in the report's LIMITS section: the
    operator's scope decision governs, but the position should be stated, not
    silently ignored.

  * **`itemList` is always empty.** The profile blob ships no videos. Do not
    wait for a video list that never arrives.

  * **The Discord snowflake trick does NOT work here.** TikTok's `id` looks
    like a snowflake on newer accounts but is not one. Measured: for an
    account whose `createTime` is 2019-12-12, `(id >> 22) + 1420070400000`
    yields 2021-02-22 — a two-year error. Early accounts have short sequential
    ids (`5831967`, `107955`) where the shift yields 0 or 1. **Use
    `createTime` directly; it is a plain Unix timestamp in seconds and needs
    no arithmetic.**

  * **`secUid` is opaque.** It is base64url over 41 bytes: a fixed 8-byte
    header `1.0.0\\x01\\x00\\x00` plus 33 bytes. Measured: the numeric `id`
    does **not** appear in the decoded bytes. It is not a reversible encoding
    of the id and leaks nothing on its own — treat it as a stable
    cross-reference key, nothing more.

  * **No credentials are used and none are required for the profile.** Do not
    supply a session cookie, do not use a scraped token, and do not
    authenticate. Everything above is reachable anonymously; anything that
    needs a signature is out of scope for this run.

BUDGET AND PACE
---------------
  * **The HTML path is not rate-limited in practice** — it answered 200
    repeatedly throughout measurement. The oembed path is. Spend accordingly:
    many HTML fetches, at most one oembed.
  * **One script per phase, not one command per fact.** Each tool call is a
    full model round-trip; a script that fetches and parses in one call is far
    faster than fetch-then-parse as two.
  * **`curl -sS -m 15`** on every request, with a real desktop User-Agent.
    A hang with no timeout is a stalled run.
  * **Record every refusal as a finding.** A 429, a 404, an empty body — each
    is a coverage row, not a silence.
  * **Write a defensive parser.** The blob's key names are undocumented and
    have changed before. Read every field with `.get()` and report a missing
    key as a finding rather than crashing the phase.

METHOD
------
Work the phases in order. Phase 1 is the whole run; the rest exist to turn
what it returns into a person.

**1. Fetch the profile blob — the payload, and it needs no login.**
   One request gets you everything:
   - `curl -sS -m 25 -A "$UA" -L --compressed "https://www.tiktok.com/@<handle>"`
     and save the HTML into the evidence directory.
   - The blob is HTML-entity-escaped, so **unescape before parsing**:
     `html.unescape(...)` then `json.loads(...)`. Parsing the raw text without
     unescaping fails on `&quot;` and is the most common way this breaks.
   - Pull `__DEFAULT_SCOPE__["webapp.user-detail"]` and write it to
     `user-detail.json`. That file is the run's primary evidence.
   - Record the oembed result **only if you spend the call** — one
     `curl` to `https://www.tiktok.com/oembed?url=https://www.tiktok.com/@<handle>`,
     and if it 429s, write that down and move on. Do not retry it.

**2. Establish existence, then decode what the blob gave you.**
   Existence first, because the status code cannot tell you:
   - **`userInfo` present, `statusCode` 0 => the handle is TAKEN.** Extract:
     - `user.id` — the numeric account id. Note its *shape*: short and
       sequential for old accounts, long for newer ones. Do not decode it as
       a snowflake; that was falsified.
     - `user.uniqueId` — the canonical handle, which may differ in case from
       what the operator typed. Record both.
     - `user.nickname` — the display name. This is usually the real name and
       is often the most valuable single field in the run.
     - `user.signature` — the bio. Frequently contains an email, a link, a
       location, an age, or a second handle.
     - `user.createTime` — **Unix seconds**. Convert directly:
       `datetime.fromtimestamp(createTime, timezone.utc)`. This is exact,
       authoritative, and needs no arithmetic. It anchors the whole timeline.
     - `user.verified`, `user.privateAccount`, `user.secret` — account flags.
       A private or secret account changes what the rest of the run can claim.
     - `user.bioLink.link` — an outbound link, usually to a Linktree, a
       YouTube channel, or a shop. **This is the highest-value pivot in the
       entire run** — feed it to the other kinds.
     - `user.language` and any region field present — a coarse locale hint.
     - `user.avatarLarger` / `avatarMedium` / `avatarThumb` — signed CDN URLs.
       **They carry `x-expires` and `x-signature` and will expire**, so
       download them in this same session. Save the largest.
   - **`userInfo` ABSENT (`statusCode` 10221) => no profile is served.** Stop
     the profile phase. Report it as "no profile served for this handle" —
     **not** as "user banned", which is what the misleading `statusMsg` says.
     The handle may simply be unregistered.
   - **Counts** come from `userInfo.statsV2` (strings), **not** `userInfo.stats`
     (ints) — measured, `stats.heart` came back as a corrupted negative
     (`-521294677`) while `statsV2.heart` was the sane `12363607211`. Report
     `followerCount`, `followingCount`, `heart`, `videoCount`, `friendCount`
     from `statsV2`, and note the discrepancy if `stats` disagrees.
   - **`secUid`** — record it as a stable cross-reference key. State in the
     report that it is opaque (41 bytes, fixed `1.0.0` header, id not
     recoverable) so nobody wastes time trying to decode it.

**3. The avatar and the bio link — the two routes off-platform.**
   - **Reverse-image-search the avatar by URL** (Google Images, Yandex,
     TinEye). This is the one route from a TikTok account to a face, and the
     signed URL is directly usable while it lasts.
   - **Follow `bioLink.link`** and treat it as a lead, not a conclusion: a
     Linktree resolves to several other platforms at once, a YouTube channel
     gives a display name, a shop gives a business identity.
   - **Search the `nickname` and `signature` text** for emails, other handles,
     phone numbers, locations and dates. A bio is self-reported and therefore
     UNVERIFIED, but it is the richest free text in the run.

**4. The cross-platform handle pivot — usually the payload.**
   A TikTok handle is very often the same string the target uses elsewhere,
   and those platforms answer far more readily.
   - **Run `sherlock <handle>` and `maigret <handle>` if installed** (maigret
     first if present — more sites, and a confidence per hit). Write the full
     output to the evidence directory.
   - **Try the obvious variants** — with and without separators, trailing
     digits, case changes. A TikTok handle is often a slight mutation of the
     same person's Instagram or GitHub name.
   - **For every hit, capture the platform, the URL, and the evidence it is
     the same person** — matching avatar, matching bio, linked account,
     matching creation era.
   - **Feed a recovered email back through the email kind** (`holehe`,
     Gravatar, the breach sources) and a recovered GitHub handle through the
     GitHub kind. Reuse the existing logic rather than reimplementing it.

**5. Identity correlation and exposure.**
   - **Breach and paste sources keyed on whatever the pivot produced** — the
     handle, the nickname, any email from the bio.
   - **Code and paste search** for the handle: GitHub code/commit search,
     grep.app, psbdmp, searchcode. Note leaked secrets as exposure
     **without reproducing the secret itself** — describe it and its location;
     the operator's job is to tell the owner, not to use it.
   - **Correlate against `createTime`.** An account created in 2019 sharing a
     handle with a 2019 GitHub account is a far stronger link than one created
     last week. The creation date is the disambiguator between "the same
     person" and "a common word".

**6. Timeline.**
   Reconstruct a dated sequence: account creation (phase 2, exact from
   `createTime`), the earliest and latest activity you can date from any
   platform evidence, and any breach dates. Gaps and bursts are as
   informative here as in the other kinds.

{report_guide}
{confidence_guide}
The account-creation date decoded from `createTime` is **CONFIRMED** — it is a
plain Unix timestamp served by TikTok, not a source's claim.

Fields read straight out of `webapp.user-detail` are **CONFIRMED** as
*TikTok's own statement about the account*, which is a narrower claim than the
field being true: a `nickname` is what the account calls itself, not a
verified name. Say which of the two you mean.

A handle match across platforms is **PROBABLE** by default. It is CONFIRMED
only when something explicitly links the accounts — a shared avatar hash, a
cross-reference in a bio, a linked account, a shared recovery address. Do not
upgrade a plausible handle match to CONFIRMED on the strength of the handle
alone; a common word is not an identity.

The `statusMsg` string from a missing profile is **UNVERIFIED and misleading**
— measured, an unregistered handle reported `"user banned"`. Never repeat it
as fact; report "no profile served", which is what was actually observed.

REPORT
------
Finish by writing `report.md` into the evidence directory. It opens with the
`## Headline` block and the coverage table, then these sections in order:

    1. ACCOUNT          handle (as typed and as canonical), id and its shape,
                        nickname, creation date and account age, verified /
                        private / secret flags, language
    2. PROFILE          bio text, bio link (followed), avatar (saved path,
                        reverse-image-search result), counts from statsV2
    3. IDS              the raw `id` and `secUid`, with an explicit note that
                        the id is NOT a snowflake and the secUid is opaque —
                        so the operator does not re-derive them
    4. HANDLE PIVOT     table: platform · URL · evidence · confidence
    5. IDENTITY         names, aliases, linked accounts, avatar reuse
    6. EXPOSURE         breaches, leaked secrets (described, not quoted)
    7. TIMELINE         dated reconstruction, anchored on `createTime`
    8. LIMITS           exactly what was refused and what it would have taken

Section 8 is not padding — for this kind it is one of the most useful parts of
the report. State plainly: whether oembed answered or 429'd (and that it was
spent only once), that the internal API returned an empty body, that
`m.tiktok.com` is a 404, that the embed shell carries no data, that no
credentials were used, and the robots.txt position. The operator needs to know
which walls were hit and which were simply not attempted.

Every finding inside those sections is written as a five-line block — What,
Where, Confidence, Means, Verify — and for every pivot give the URL and the
evidence that links it. A section that found nothing says so explicitly rather
than being dropped. The format, the coverage table and the closing
`## Gaps and next steps` section are specified in full above; follow them
exactly, they are what makes the report readable.

Close the turn with a short prose summary of the headline findings: what the
profile established, which other platforms the handle resolved on, and what
the operator should do with that.
"""




# ---------------------------------------------------------------------------
# The Instagram playbook
# ---------------------------------------------------------------------------
# Handed to the agent verbatim as the turn's user message, with {target},
# {case_dir}, {report_guide} and {confidence_guide} substituted by
# `build_prompt`. LITERAL BRACES ARE DOUBLED so `str.format` does not raise.
#
# The "walls" section is the point. Instagram is unusual among the OSINT kinds:
# its JSON API is *entirely* login-walled, and its HTML profile page is a
# byte-identical shell for a real account and a nonexistent one — so an agent
# that does the obvious thing (fetch the profile, parse the JSON) spends its
# whole run producing nothing and reporting "not found" for a handle that
# plainly exists. The playbook names that trap first, then hands over the one
# measured route that works.

INSTAGRAM_PLAYBOOK = """\
# Instagram OSINT recon — {target}

Authorised investigation only. You are running a recon pass on an Instagram
account the operator has declared in scope. Work autonomously through the
phases below; do not stop to ask permission between steps. If a tool is
missing or a request is refused, note it and continue with what works.

EVIDENCE DIRECTORY
------------------
{case_dir}

Write every artefact you produce into that directory: raw HTML captures, the
extracted profile JSON, the downloaded avatar, and the final `report.md`. Use
absolute paths.

WHAT INSTAGRAM WILL AND WILL NOT GIVE YOU — READ THIS FIRST
-----------------------------------------------------------
Instagram has **two** surfaces and they behave in opposite ways. Knowing which
is which is most of this run.

**The obvious routes are all dead, and they fail in ways that look like
success.** Do not spend a phase on any of these:

  * **`?__a=1` is a corpse.** `https://www.instagram.com/<handle>/?__a=1&__d=dis`
    returns **HTTP 201 with a zero-byte body**. It is not 200 and it is not
    JSON. Do not read the 201 as a hit.
  * **Every JSON API endpoint requires a login.** With the long-public web app
    id (`X-IG-App-ID: 936619743392459`):
    `https://www.instagram.com/api/v1/users/web_profile_info/?username=<handle>`
    returns **401** with `{{"require_login":true,"status":"fail"}}`. The
    `graphql/query/` endpoint returns the same 401. The mobile host
    `i.instagram.com/api/v1/users/<handle>/info/` returns **400**. There is no
    unauthenticated JSON profile endpoint. Do not hunt for one.
  * **The enumeration oracles are rate-limited into uselessness.**
    `web/accounts/web_create_ajax/attempt/` (the username-taken check) and
    `web/accounts/account_recovery_send_ajax/` both returned **429** with an
    empty body from this network. Do not build a phase on them.
  * **`profile.php?id=<pk>` no longer serves a profile.** It **301**s, and the
    redirect target carries no record.

**The profile HTML is the same for a real account and a fake one.** This is the
trap, and it is worth stating precisely:

  * `https://www.instagram.com/<handle>/` returns **200 and ~634 KB** for a
    handle that exists **and** for one that has never existed. There is no 404.
  * That body is a JavaScript application shell: no `og:` tags, no profile
    JSON, `<title>Instagram`. A real page and a fake page are **byte-identical
    apart from a per-response CSP `nonce`** — two fetches of the *same* handle
    differ the same way. So this URL tells you nothing, and an agent that
    greps it will conclude "not found" about an account that plainly exists.
    Fetch it **once** for the record and move on.

**What DOES work is the crawler surface** (phase 2), and it works completely:

  * Sending the **full Googlebot user-agent** — exactly
    `Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)` —
    gets a **server-rendered** page, roughly **1.3 MB** for a real handle
    (about double the shell), containing a GraphQL `RelayPreloader` blob with
    the entire profile record, plus real `og:` tags.
  * A handle that does not exist gets the **634 KB shell** under the same UA —
    no blob, no `og:` tags. **That size difference plus the presence of
    `xig_user_by_igid_v2` is a free, unauthenticated existence test.**
  * **The gate is the exact string, not the word.** A bare `Googlebot/2.1` or
    `googlebot` gets **nothing**. `Googlebot-Image/1.0` works. `bingbot`,
    `Twitterbot` and `curl` get nothing. Use the full string above, verbatim.

BUDGET AND PACE
---------------
  * **One script per phase, not one command per fact.** Each tool call is a
    full model round-trip; a script that writes five files in one call is five
    times faster than five calls.
  * **`curl -sS -m 20`** on every request. The Googlebot fetch is ~1.3 MB and
    is slower than a normal page; a hang with no timeout is a stalled run.
  * **Do not loop against the live site.** This is a recon pass, not a scraper:
    one deliberate request per fact, and record what it returned. Do not
    enumerate handles, and do not re-fetch the same profile repeatedly to watch
    a counter move.
  * **Record every refusal as a finding.** The 401s, the 429s, the 201-with-no-
    body — each is a coverage row, not a silence. For this kind the refusals are
    a large part of what the operator needs to know.

PHASE 1 — RESOLVE THE TARGET
----------------------------
Reduce what the operator gave you to a bare handle, and record both the form as
typed and the canonical form. If the target is a numeric id (an IGID, or the
`pk` from a previous run), note that the live `profile.php?id=` route no longer
serves it and that the id's value is as a cross-reference key, not a URL.

If the operator gave you a post URL (`/<handle>/p/<code>/`), the author is the
target — say so explicitly, and record the post URL as the evidence for the
link between the handle and that content.

PHASE 2 — THE GOOGLEBOT RECORD (the core of the run)
----------------------------------------------------
This is where the intelligence is. Do it carefully and save everything.

    UA='Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'
    curl -sS -m 20 -A "$UA" "https://www.instagram.com/<handle>/" -o "$CASE/ig.html"

Then, from that one capture:

  * **Extract the profile record.** It lives in a RelayPreloader blob keyed
    `"xig_user_by_igid_v2":{{...}}`. The blob is JSON, so brace-balance it and
    `json.loads` it rather than regexing fields out one at a time. Save the
    extracted object to `profile.json` — it is the primary evidence of this run.
    Expect these keys: `username`, `full_name`, `biography`, `follower_count`,
    `following_count`, `is_verified`, `is_private`, `pk`, `id`, `pronouns`,
    `latest_reel_media`, `has_any_clips`, `profile_pic_url`.
  * **Read the `og:` tags** as an independent second source for the same facts:
    `og:title` gives the display name and handle; `og:description` gives
    follower/following/post counts in human form ("104M Followers, 94 Following,
    4,963 Posts - ..."). Where the two agree, the finding is PROBABLE or better;
    where they disagree, say so — do not silently prefer one.
  * **Fetch and hash the avatar.** `og:image` is a signed CDN URL that really
    fetches (`image/jpeg`). Save it and record its md5. A shared avatar hash
    across platforms is the strongest cross-platform link you can produce, so
    this hash is the seed of phase 4.
  * **Record the existence result explicitly.** State whether the blob was
    present, and the byte size of the capture. "Blob present at ~1.3 MB" is a
    CONFIRMED existence finding; "shell at ~634 KB" is a CONFIRMED absence.

Interpret what you have: `is_private` tells the operator whether the account is
public; `is_verified` whether it is a notable account; the follower/following
ratio and post count sketch the account's role (broadcaster vs personal);
`biography` is free text that often carries a contact address, a link, or a
real name, and the bio link is a pivot in its own right — follow it.

PHASE 3 — WHAT THE NUMBERS DATE
-------------------------------
The two ids and the reel timestamp are the only timestamps available, and they
are weak. Handle them honestly rather than over-reading them:

  * `pk` is the classic short numeric id and `id` is the 17-digit IGID. All
    accounts measured begin `178414`, so the IGID prefix is a fixed namespace
    and carries no per-account information — do not report it as a finding.
  * There is **no creation timestamp** in the record. `latest_reel_media` is a
    Unix timestamp for the most recent reel, which dates *activity*, not
    creation. Do not present it as the account's age.
  * **Do not attempt a snowflake decode.** Instagram ids are not Discord
    snowflakes; the arithmetic from that playbook does not apply here.

PHASE 4 — CROSS-PLATFORM PIVOT (where a handle becomes a person)
----------------------------------------------------------------
A handle alone is not an identity. The run earns its value here:

  * **Re-use the handle** across the platforms an operator would expect —
    TikTok, X/Twitter, GitHub, Reddit, Threads, YouTube, Telegram, a personal
    domain. Use a handle-reuse tool if one is installed (sherlock, maigret) and
    note the exact command; otherwise check the handful that matter by hand.
  * **Reverse-image the avatar.** You have the file and its hash — that is the
    input a reverse-image search needs, and it is the pivot that survives a
    handle change.
  * **Search the biography text verbatim.** A bio often contains an email, a
    phone number, a business name or a real name; a quoted search on a
    distinctive phrase is frequently what actually names the account.
  * **Check the display name** (`full_name`) against public records — it is
    often the real name where the handle is a persona.
  * **Do not trust third-party Instagram viewers.** They were measured to be
    unreliable: one had been hijacked to serve TikTok content under an
    Instagram URL, another refused outright. If you use one, verify what it
    returned is actually about *this* account before reporting it, and say
    which viewer and what you checked. A viewer's numbers that contradict the
    Googlebot record are the viewer's problem, not a finding.
  * **The Meta sibling endpoints do not resolve an Instagram account.**
    `graph.facebook.com/<pk>/picture` — the trick that works for a Facebook id
    — returns **302** to the *generic Meta silhouette* for every Instagram
    `pk` tried, including real ones. It is not a hit and it is not a miss; it
    carries no information. Do not read the 302 as a resolution, and do not
    spend a phase trying to make it one. `threads.net/@<handle>` is likewise a
    JS shell with no profile keys in the HTML. If the operator wants the Meta
    footprint, that is a `facebook` run on the recovered name, not a phase here.

A handle match across platforms is **PROBABLE** by default, and **CONFIRMED**
only when something explicitly links the accounts — a matching avatar hash, a
cross-reference in a bio, a linked account, a shared contact address. Do not
upgrade a plausible handle match to CONFIRMED on the strength of the handle
alone; a common word is not an identity.

PHASE 5 — EXPOSURE AND TIMELINE
-------------------------------
  * Check the handle and any address or name you recovered against breach
    corpora, and record what surfaced without quoting secrets. Note that a
    breach hit on a *handle* is weak; a hit on a *recovered address* is strong.
  * Build a dated timeline from what you actually have: the `latest_reel_media`
    timestamp, dated posts or captures if you found any, and the archive. Do not
    invent a creation date to anchor it — if the timeline is thin, say the
    timeline is thin.

{report_guide}
{confidence_guide}
The existence result from phase 2 is **CONFIRMED** — it is a direct
observation of a server response, not a source's claim.

A handle match across platforms is **PROBABLE** by default and CONFIRMED only
on an explicit link, as stated in phase 4. The follower and following counts are
**PROBABLE** — they come from a single source (the crawler record), corroborated
only where `og:description` agrees.

REPORT
------
Finish by writing `report.md` into the evidence directory. It opens with the
`## Headline` block and the coverage table, then these sections in order:

    1. ACCOUNT          handle as typed and canonical, display name, bio,
                        verification, private/public, and the pk / IGID
    2. RECORD           the extracted profile object, and which fields the
                        `og:` tags independently corroborated
    3. EXISTENCE        the measured result — blob present at ~1.3 MB, or shell
                        at ~634 KB — and what it establishes
    4. AVATAR           the fetched image, its saved path and md5, and any
                        cross-platform match on that hash
    5. HANDLE PIVOT     table: platform · URL · evidence · confidence
    6. IDENTITY         names, aliases, linked accounts, avatar reuse, bio-link
                        target
    7. EXPOSURE         breaches and any contact detail that surfaced
    8. TIMELINE         dated reconstruction, and an explicit statement that no
                        creation date was available
    9. LIMITS           exactly what Instagram refused without a login — the
                        401s, the 429s, the empty 201 — and what a session
                        would have added

Section 9 is not padding — for this kind it is one of the most useful parts of
the report, because Instagram's walls are the reason the run is shaped the way
it is, and the operator needs to know precisely what was reachable and what was
not, and whether an authenticated session is worth obtaining next time.

Every finding inside those sections is written as a five-line block — What,
Where, Confidence, Means, Verify — and for every pivot give the URL and the
evidence that links it. A section that found nothing says so explicitly rather
than being dropped. The format, the coverage table and the closing `## Gaps and
next steps` section are specified in full above; follow them exactly, they are
what makes the report readable.

Close the turn with a short prose summary of the headline findings: who the
account turned out to be, what the record established, which platforms the
handle and avatar resolved on, and what the operator should do with that.
"""
#: The registry. `email`, `github`, `discord`, `facebook`, `tiktok` and
#: `instagram` are built; `phone` is advertised and stubbed.
OSINT_KINDS: dict[str, Kind] = {
    "email": Kind(
        name="email",
        blurb="reverse-email recon — breaches, accounts, identity",
        target_label="email address",
        built=True,
        playbook=EMAIL_PLAYBOOK,
    ),
    "phone": _stub("phone", "phone-number recon — carrier, accounts, name", "phone number"),
    "facebook": Kind(
        name="facebook",
        blurb="Facebook profile recon — avatar oracle, archive, pivot",
        target_label="Facebook profile URL, id or username",
        built=True,
        playbook=FACEBOOK_PLAYBOOK,
    ),
    "tiktok": Kind(
        name="tiktok",
        blurb="TikTok account recon — rehydration blob, ids, pivot",
        target_label="TikTok username or profile URL",
        built=True,
        playbook=TIKTOK_PLAYBOOK,
    ),
    "instagram": Kind(
        name="instagram",
        blurb="Instagram account recon — crawler record, avatar, pivot",
        target_label="Instagram username or profile URL",
        built=True,
        playbook=INSTAGRAM_PLAYBOOK,
    ),
    "discord": Kind(
        name="discord",
        blurb="Discord account recon — id, guilds, cross-platform pivot",
        target_label="Discord username or user id",
        built=True,
        playbook=DISCORD_PLAYBOOK,
    ),
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

# A Discord snowflake. Discord's epoch is 2015-01-01, so a real id is 17-20
# digits today; 17 is the floor that still admits the earliest accounts.
_DISCORD_ID_RE = re.compile(r"^\d{17,20}$")

# Discord's own username rule since discriminators were dropped in 2023:
# 2-32 characters, lowercase letters, digits, `.` and `_`. An *older* account
# may still be written with a legacy discriminator (`name#1234`), which is kept
# because the discriminator dates the account — so that form is validated as
# name-plus-four-digits rather than rejected for the `#`.
_DISCORD_NAME_RE = re.compile(r"^[a-z0-9._]{2,32}$")
_DISCORD_LEGACY_RE = re.compile(r"^[A-Za-z0-9._]{2,32}#\d{4}$")


# ---------------------------------------------------------------------------
# Facebook — measured on this box, 2026-10-13, unauthenticated
# ---------------------------------------------------------------------------
# Facebook is the most closed surface of the OSINT kinds. Measured:
#
#   * `www.facebook.com/<user>` -> **400**, a 1542-byte `text/html` body that
#     is nothing but `<title>Error</title>` and "Sorry, something went wrong."
#     It is NOT a login wall and NOT a profile — there is no `og:` tag and no
#     JSON to parse. `mbasic.facebook.com` gives the same 400 in XHTML-MP, and
#     `m.facebook.com` 301s straight back to the `www` 400.
#   * The crawler-UA trick is dead: a Googlebot UA gets a 301 and no `og:` tags.
#   * `graph.facebook.com/<id>` -> 400 code 104 "access token required";
#     `graph.facebook.com/<username>` -> **403** code 200 "Provide valid app ID".
#     Two different walls, neither worth retrying.
#   * **The one endpoint that answers is `graph.facebook.com/<id|username>/picture`**
#     — 302 to a real `scontent.*.fbcdn.net` avatar, no token. It accepts both a
#     numeric id and a vanity username, and `?type=large` widens the CDN image.
#     It is also a free **existence oracle**: a real object 302s, a nonexistent
#     one 400s `{{"error":{{"message":"Unsupported get request. Object with ID
#     '…' does not exist…"}}}}` with no rate-limit escalation observed.
#   * Ids 4/5/6 return byte-identical 1876-byte avatars (md5
#     `3e8f62364b0f574a7d18a6c8b26730f1`) — so hashing the fetched image tells
#     you whether the account has a custom picture at all.
#   * The Internet Archive is the richest source and needs no key: a 2023
#     capture of `facebook.com/zuck` is 478 KB with real `og:` tags, the CDX
#     API enumerates every capture, and `_fb_noscript=1` turns a 32 KB JS shell
#     into 122 KB of real content. A 2016 capture leaked an `entity_id` that is
#     not visible on the live surface.
#
# UNVERIFIED, and flagged as such in the playbook: the `pfbid` shape (no real
# specimen was obtainable — the CDX filter query 502'd), the vanity charset and
# length, and the id digit bounds. The `/people/<Name>/<id>/` and `?id=` URL
# forms are trusted on structure rather than on a measured digit floor.


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------
# A Facebook target arrives in four shapes and all four are legitimate, so all
# four are accepted and reduced to one canonical form rather than refused:
#
#   * a numeric profile/page id      `100012345678901`
#   * a vanity username              `nasa`, `mark.zuckerberg`
#   * a `pfbid` opaque id            `pfbid0AbC...`   (format UNVERIFIED, A1)
#   * any of the above as a pasted URL, on any of the surfaces Facebook serves
#     the same profile from: www / web / m / mbasic / business, with or without
#     a trailing slash, query string or fragment.
#
# The `/people/<Display-Name>/<id>/` form is the one URL shape that carries BOTH
# a name and an id. The id is the durable identifier, so it is what survives;
# the display name is kept only because it is a free finding (it is often the
# real name, where the vanity username is a handle).

# A numeric Facebook object id. Facebook user ids are ~15 digits today and
# older page ids are shorter (`54971236771` is 11), so the floor is set low
# enough to keep genuine old pages while still rejecting stray small numbers.
FACEBOOK_ID_RE = re.compile(r"^\d{6,20}$")

# A vanity username: letters, digits and periods.
#
# The floor is 1, not the 5 that Facebook's signup form documents, because that
# documented minimum is **contradicted by measurement**: `nasa` is four
# characters and resolves (M2). Facebook grandfathers short legacy page names,
# so any length floor above 1 would reject real targets. The trade-off is
# deliberate: this layer errs permissive because a false *rejection* stops the
# operator investigating a real account, while a false *acceptance* costs
# exactly one request to the existence oracle in phase 2 — which is the
# authoritative check, not this regex.
FACEBOOK_NAME_RE = re.compile(r"^[A-Za-z0-9.]{1,50}$")

# The opaque modern id form. ASSUMED per A1 — no specimen was obtainable, so
# this deliberately accepts a generous tail rather than pinning a length.
FACEBOOK_PFBID_RE = re.compile(r"^pfbid[0-9A-Za-z]{20,}$")

# `/people/<Display-Name>/<id>/` — the numeric id is the second segment.
# The floor is deliberately 1 digit, not the 6 of FACEBOOK_ID_RE: the
# `/people/<name>/` path *structure* already proves the following segment is an
# id, so it needs no length heuristic to disambiguate. Real specimens include
# the very low ids (`/people/Mark-Zuckerberg/4/`), which a 6-digit floor would
# silently reject.
FACEBOOK_PEOPLE_RE = re.compile(r"/people/[^/]+/(\d{1,20})")

# `/profile.php?id=<id>` — the legacy numeric URL form, still widely pasted.
# Floor of 1 for the same reason as FACEBOOK_PEOPLE_RE: the `id=` parameter
# proves the value is an id, so no length heuristic is needed to disambiguate,
# and a 6-digit floor would reject the genuine low ids.
FACEBOOK_PROFILE_PHP_RE = re.compile(r"[?&]id=(\d{1,20})")

# Every host Facebook serves a profile from. `m.` and `mbasic.` are measured to
# be no more generous than `www.` (M8, M9), but they are still unwrapped so that
# a pasted mobile link normalises to the same target as the desktop one.
_FACEBOOK_HOSTS = (
    "https://www.facebook.com/",
    "http://www.facebook.com/",
    "https://web.facebook.com/",
    "http://web.facebook.com/",
    "https://m.facebook.com/",
    "http://m.facebook.com/",
    "https://mbasic.facebook.com/",
    "http://mbasic.facebook.com/",
    "https://business.facebook.com/",
    "https://facebook.com/",
    "http://facebook.com/",
    "www.facebook.com/",
    "web.facebook.com/",
    "m.facebook.com/",
    "mbasic.facebook.com/",
    "facebook.com/",
)

# Path segments that are site furniture rather than a profile. A URL whose first
# segment is one of these is not an account target and is left to fail.
_FACEBOOK_NON_PROFILE = frozenset({
    "home.php", "login", "login.php", "recover", "reg", "privacy", "policies",
    "help", "settings", "messages", "notifications", "friends", "groups",
    "marketplace", "watch", "gaming", "events", "pages", "search", "photo",
    "photos", "video", "videos", "story.php", "share", "sharer", "dialog",
    "plugins", "ads", "business", "legal", "about", "careers",
})


# ---------------------------------------------------------------------------
# TikTok — measured on this box, 2026-10-13, unauthenticated
# ---------------------------------------------------------------------------
#   * The profile HTML **is** the payload. `tiktok.com/@<handle>` -> 200, 85-89
#     KB, and `<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">` still ships.
#     `__DEFAULT_SCOPE__["webapp.user-detail"]` holds id, uniqueId, nickname,
#     signature, createTime, verified, privateAccount, secUid, bioLink and the
#     avatar URLs. It is HTML-entity-escaped, so `html.unescape()` before
#     `json.loads()`. No login needed.
#   * **There are ZERO `og:`/`twitter:` meta tags in the raw HTML** — they are
#     JS-injected, so `curl` never sees them. `<title>` is the generic
#     "TikTok - Make Your Day" for a real *and* a nonexistent handle. Any parser
#     built on `og:` tags is dead on arrival.
#   * oembed is the wall: 429 `text/plain` `ratelimit triggered` is the
#     *dominant* response, and the limiter is sticky (429, one 200 at +20s, then
#     429 at +75s and +2min). It is independent of the HTML limiter, so an
#     oembed 429 does not block the run. Treat it as one call per long cool-off.
#   * Taken vs free: the HTTP status is useless — both are 200 with ~85 KB. The
#     signal is inside the JSON: `userInfo` present + `statusCode` 0 = taken;
#     `userInfo` **absent** + `statusCode` 10221 = free. `statusMsg` **lies**
#     (a never-registered handle came back as "user banned"), so report "no
#     profile served" rather than quoting it.
#   * `m.tiktok.com/@<handle>` -> 404, the mobile surface is gone. `/embed/@`
#     -> 200 but no blob. `/api/user/detail/?uniqueId=…&aid=1988` -> 200
#     `application/json` with a **zero-byte body** (it needs an X-Bogus/msToken
#     signature) — a 200 that is not a success.
#   * `stats.heart` came back corrupted (-521294677) where `statsV2.heart` was
#     sane (12363607211) — use `statsV2`. `itemList` is always empty.
#   * **The Discord snowflake decode does NOT work here.** `(id >> 22) +
#     1420070400000` on an account whose own `createTime` is 2019-12-12 yields
#     2021-02-22 — a two-year error — and early accounts have short sequential
#     ids (5831967, 107955) where the shift gives 0 or 1. `createTime` is a
#     plain Unix timestamp in **seconds**; use it directly, no arithmetic.
#   * `secUid` is opaque: base64url -> 41 bytes, a fixed 8-byte header
#     `1.0.0\x01\x00\x00` plus 33 opaque bytes, and the numeric id does not
#     appear in the decoded bytes. Not reversible; leaks nothing alone.
#   * robots.txt names `anthropic-ai`, `ClaudeBot`, `GPTBot` and ~20 other AI
#     agents with `Disallow: /`, and disallows `/embed/@`, `/search?`,
#     `/api/share/settings`. The `/@handle` path is NOT in the `*` allow-list.
#     The operator's scope decision governs; the playbook records the position.
#
# UNVERIFIED: the limiter's scope (tested from one IP only), whether 10221 ever
# means genuinely-banned rather than merely-unregistered, and the blob's key
# stability (it is undocumented and has changed before).


# ---------------------------------------------------------------------------
# Handle validation
# ---------------------------------------------------------------------------
# TikTok's own rule: 2-24 characters, letters, digits, underscore and period.
# Enforced loosely, exactly as the email/github/discord patterns in osint.py
# are — the job is to catch an empty argument or an obvious typo (a pasted
# video URL, a space, a stray `@`), not to re-implement the signup form. The
# live site does not enforce it either: a 1-char and a 26-char handle both
# returned HTTP 200, so validation here is a courtesy to the operator, not a
# mirror of a server rule.
TIKTOK_NAME_RE = re.compile(r"^[A-Za-z0-9._]{2,24}$")

# A pasted profile URL, in the forms an operator actually types. The `www.` is
# optional and the scheme is optional, because both arrive constantly.
_TIKTOK_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?tiktok\.com/@(?P<handle>[^/?#\s]+)",
    re.IGNORECASE,
)

# A bare `@handle` with no host at all.
_TIKTOK_AT_RE = re.compile(r"^@(?P<handle>[^/?#\s]+)$")

# Anything after the handle in a URL or a paste: `?lang=en`,
# `?is_from_webapp=1&sender_device=pc`, a trailing `/`, a fragment, a
# `/video/<id>` path. Measured: all of these forms 200 on the live site and
# all of them reduce to the same profile, so they are stripped rather than
# treated as errors.
_TIKTOK_TRAILING_RE = re.compile(r"[/?#].*$", re.DOTALL)


# ---------------------------------------------------------------------------
# Instagram — measured on this box, 2026-10-13, unauthenticated
# ---------------------------------------------------------------------------
# Instagram is the one kind where the *obvious* route fails in a way that looks
# like success, so the walls matter more here than anywhere else:
#
#   * `?__a=1&__d=dis` -> **201 Created with a zero-byte body**. Not 200, not
#     JSON — the corpse of the old profile JSON.
#   * `api/v1/users/web_profile_info/` (with the long-public
#     `X-IG-App-ID: 936619743392459`) -> **401** `require_login`. The
#     `graphql/query/` endpoint gives the same 401; `i.instagram.com`'s mobile
#     `users/<h>/info/` gives 400. There is no unauthenticated JSON profile
#     endpoint.
#   * Both enumeration oracles (`web_create_ajax/attempt/` and
#     `account_recovery_send_ajax/`) -> **429**, empty body.
#   * **`instagram.com/<handle>/` is 200 and ~634 KB for a real handle AND for
#     one that has never existed.** There is no 404. The body is a JS shell with
#     no `og:` tags and no profile JSON; diffing a real page against a fake one
#     with the handle normalised out leaves **only a per-response CSP `nonce`**
#     — two fetches of the *same* handle differ identically. So the profile URL
#     carries no existence signal at all. Even `robots.txt` returns the shell.
#   * **The one live route is the crawler surface.** The full
#     `Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)`
#     UA gets a server-rendered ~1.3 MB page carrying a RelayPreloader blob
#     `"xig_user_by_igid_v2":{{…}}` with the whole record (username, full_name,
#     biography, follower_count, following_count, is_verified, is_private, pk,
#     id, pronouns, latest_reel_media, profile_pic_url) plus real `og:` tags
#     whose `og:image` is a fetchable signed avatar. A nonexistent handle gets
#     the 634 KB shell instead — **that size jump plus the presence of the blob
#     is a free existence oracle.**
#   * **The gate is the exact UA string, not the word.** A bare `Googlebot/2.1`
#     or `googlebot` gets nothing; `Googlebot-Image/1.0` works; `bingbot`,
#     `Twitterbot` and `curl` get nothing.
#   * `pk` is the classic short numeric id; `id` is a 17-digit IGID and all four
#     accounts measured begin `178414`, so the prefix is a fixed namespace, not
#     derived from `pk`. `profile.php?id=<pk>` -> 301 and serves no record.
#   * `graph.facebook.com/<pk>/picture` -> 302 to the generic Meta silhouette
#     for every id tried, so it does NOT resolve an Instagram account.
#     `picuki.com` had been hijacked to serve TikTok content under an Instagram
#     URL and `imginn.com` 403s — do not trust third-party viewers. The Wayback
#     CDX returned no rows for the handles tried.
#
# UNVERIFIED: that the crawler record is not a decoy (it is internally
# consistent across four accounts and cross-checks against `og:description`,
# but it is served *because* we claim to be Googlebot), the UA gate's stability,
# and the handle charset/length boundaries.


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------

# A handle: 1-30 characters of letters, digits, period and underscore. ASSUMED
# per A4 — Instagram's documented rule, not a measured boundary.
INSTAGRAM_NAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# The IGID — a 17-digit numeric id. Measured specimens are all 17 digits and
# all begin `178414`; the pattern is left slightly loose (15-20) so it does not
# reject a genuine id from an account outside the measured sample.
INSTAGRAM_IGID_RE = re.compile(r"^\d{15,20}$")

# `instagram.com/<handle>` and its `www.` / `m.` forms, with an optional
# trailing slash, query string or fragment.
_INSTAGRAM_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?instagram\.com/"
    r"(?P<handle>[^/?#\s]+)",
    re.IGNORECASE,
)

# A pasted `@handle`.
_INSTAGRAM_AT_RE = re.compile(r"^@(?P<handle>[^/?#\s]+)$")

# Everything from the first `/`, `?` or `#` onward — query and fragment are
# tracking, and a deeper path (`/<handle>/p/<code>/`) is a post, not a profile.
_INSTAGRAM_TRAILING_RE = re.compile(r"[/?#].*$")

# The first path segment of an Instagram URL that is site furniture rather than
# a profile. A URL whose first segment is one of these is not an account.
_INSTAGRAM_NON_PROFILE = frozenset({
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct",
    "about", "legal", "developer", "api", "graphql", "web", "challenge",
    "emails", "session", "oauth", "directory", "nametag", "locations",
})
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


def normalise_discord(target: str) -> str:
    """Reduce a target to a bare Discord handle or numeric id.

    Discord accepts two forms and both are legitimate targets: a **snowflake**
    (the numeric id, 17-20 digits) and a **username**. Since discriminators
    were dropped in 2023 a modern handle is a bare string; an older account may
    still be written `name#1234`, and that form is kept as typed because the
    discriminator is itself a finding about the account's age.

    A pasted profile URL is unwrapped the same way `normalise_github` does it —
    it is the common way this gets typed, and a bare handle is one edit away.
    Unlike GitHub, a Discord profile URL carries the *numeric id*
    (`discord.com/users/<id>`), so unwrapping it yields an id, not a name.
    """
    handle = target.strip().rstrip("/")
    for prefix in ("https://discord.com/users/", "http://discord.com/users/",
                   "https://discordapp.com/users/", "discord.com/users/",
                   "https://discord.com/", "discord.com/"):
        if handle.lower().startswith(prefix):
            handle = handle[len(prefix):]
            break
    # A pasted `<@id>` / `<@!id>` mention is another way an id arrives.
    if handle.startswith("<@") and handle.endswith(">"):
        handle = handle[2:-1].lstrip("!&")
    return handle.strip()







def normalise_facebook(target: str) -> str:
    """Reduce a target to the bare Facebook identifier we can actually use.

    A pasted profile URL is the common way this gets typed, and a bare
    identifier is one edit away from it, so the URL form is unwrapped rather
    than refused — the same contract `normalise_github` and `normalise_discord`
    honour. The reduction is chosen so the *most durable* identifier wins:

      * `/people/<Display-Name>/<id>/` yields the numeric id, not the name,
        because the id survives a rename and the display name does not.
      * `/profile.php?id=<id>` yields the id.
      * `/<vanity>` yields the vanity string.
      * a bare id, vanity or `pfbid` is returned unchanged.

    Query strings and fragments are dropped, because they are tracking
    parameters (notably `?id=` on any path but `profile.php`, and the `__cft__`
    / `__tn__` cruft Facebook appends) and never part of the identity.
    """
    value = target.strip()
    if not value:
        return value

    # Strip the scheme-relative and scheme-ful host, longest prefix first, so
    # `https://mbasic.facebook.com/` is not eaten by a shorter `m.` entry.
    lowered = value.lower()
    for prefix in _FACEBOOK_HOSTS:
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            break

    # `/people/<name>/<id>/` — take the id, the name is not the identity.
    people = FACEBOOK_PEOPLE_RE.search("/" + value.lstrip("/"))
    if people:
        return people.group(1)

    # `/profile.php?id=<id>` — the legacy numeric form.
    if value.lower().startswith("profile.php"):
        php = FACEBOOK_PROFILE_PHP_RE.search("?" + value.partition("?")[2])
        if php:
            return php.group(1)
        return value

    # Drop fragment and query on everything else — they are tracking, not id.
    value = value.split("#", 1)[0].split("?", 1)[0]
    # A trailing slash is cosmetic; a remaining slash means this was a deeper
    # path (`/nasa/photos`), which is not the account URL. Keep only the first
    # segment so the account can still be recovered from a pasted subpage.
    value = value.strip("/")
    if "/" in value:
        value = value.split("/", 1)[0]
    return value




def normalise_tiktok(target: str) -> str:
    """Reduce a target to a bare TikTok handle.

    Accepts every form an operator actually pastes, all of which were measured
    against the live site:

        https://www.tiktok.com/@user          -> user
        https://www.tiktok.com/@user?lang=en  -> user
        https://tiktok.com/@user              -> user   (302s to www)
        tiktok.com/@user                      -> user
        www.tiktok.com/@user/video/123        -> user
        @user                                 -> user
        user                                  -> user

    The trailing-slash and query-string forms are handled by stripping
    everything from the first `/`, `?` or `#` after the handle — measured, a
    profile URL with `?lang=en`, with `?is_from_webapp=1&sender_device=pc`,
    and with a trailing slash all serve the same profile, so the suffix is
    noise rather than a different resource.

    The `m.` host is unwrapped too even though `m.tiktok.com` is dead (404,
    measured) — an operator may still paste an old `m.` link, and unwrapping
    it to a handle that works on `www.` is strictly more useful than passing
    a dead host through.
    """
    target = target.strip()

    match = _TIKTOK_URL_RE.match(target)
    if match:
        return match.group("handle").strip()

    match = _TIKTOK_AT_RE.match(target)
    if match:
        return match.group("handle").strip()

    # No host and no `@`: a bare handle, possibly with a query string glued on
    # by a copy-paste. Strip the suffix, then any leading `@` left over.
    target = _TIKTOK_TRAILING_RE.sub("", target).strip()
    return target.lstrip("@").strip()




def normalise_instagram(target: str) -> str:
    """Reduce a target to a bare Instagram handle.

    Accepts every form an operator pastes, all measured against the live site:

        https://www.instagram.com/nasa        -> nasa
        https://www.instagram.com/nasa/       -> nasa
        instagram.com/nasa                    -> nasa
        www.instagram.com/nasa?hl=en          -> nasa
        @nasa                                 -> nasa
        nasa                                  -> nasa

    A `/<handle>/p/<code>/` post URL reduces to its author, because the author
    is the account and the post is not — the same choice `normalise_github`
    makes when it refuses to treat a repo URL as an account. A URL whose first
    segment is site furniture (`/p/...`, `/explore/...`) reduces to that
    segment and is then rejected by `validate_instagram`, which is the right
    outcome: it is not a profile and should not silently become one.
    """
    value = target.strip()
    if not value:
        return value

    match = _INSTAGRAM_URL_RE.match(value)
    if match:
        return match.group("handle").strip()

    match = _INSTAGRAM_AT_RE.match(value)
    if match:
        return match.group("handle").strip()

    # No host and no `@`: a bare handle, possibly with a query glued on by a
    # copy-paste. Strip the suffix, then any leading `@` left over.
    value = _INSTAGRAM_TRAILING_RE.sub("", value).strip()
    return value.lstrip("@").strip()


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
    if kind.name == "discord":
        return normalise_discord(target)
    if kind.name == "facebook":
        return normalise_facebook(target)
    if kind.name == "tiktok":
        return normalise_tiktok(target)
    if kind.name == "instagram":
        return normalise_instagram(target)
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
    elif kind.name == "discord":
        # Either form is a valid target: a snowflake id, a modern username, or
        # a legacy `name#1234`. A *pasted URL* is already unwrapped by
        # `normalise_discord`, so by here a URL has become its id and passes.
        handle = normalise_discord(target)
        if not (_DISCORD_ID_RE.match(handle)
                or _DISCORD_NAME_RE.match(handle)
                or _DISCORD_LEGACY_RE.match(handle)):
            return False, f"not a valid Discord username or user id: {target}"
    elif kind.name == "facebook":
        # Four legitimate shapes: a numeric id, a vanity username, a `pfbid`
        # opaque id, or a pasted URL that `normalise_facebook` has already
        # reduced to one of those. Site furniture (`login`, `groups`, …) is
        # rejected rather than being mistaken for a vanity name.
        value = normalise_facebook(target)
        if not value:
            return False, f"not a valid Facebook id or username: {target}"
        if value.lower() in _FACEBOOK_NON_PROFILE:
            return False, f"not a Facebook profile: {target}"
        if not (FACEBOOK_ID_RE.match(value)
                or FACEBOOK_PFBID_RE.match(value)
                or FACEBOOK_NAME_RE.match(value)):
            return False, f"not a valid Facebook id or username: {target}"
    elif kind.name == "tiktok":
        # One shape: a handle. A pasted URL or `@handle` is already unwrapped,
        # so by here anything still holding a slash or a space is a typo.
        handle = normalise_tiktok(target)
        if not TIKTOK_NAME_RE.match(handle):
            return False, f"not a valid TikTok handle: {target}"
    elif kind.name == "instagram":
        # A handle, or the numeric IGID the crawler record yields. Site
        # furniture (`p`, `reel`, `explore`, …) is rejected, so a pasted post
        # URL is refused rather than silently becoming its author.
        handle = normalise_instagram(target)
        if not handle:
            return False, f"not a valid Instagram handle: {target}"
        if handle.lower() in _INSTAGRAM_NON_PROFILE:
            return False, f"not an Instagram profile: {target}"
        if not (INSTAGRAM_IGID_RE.match(handle)
                or INSTAGRAM_NAME_RE.match(handle)):
            return False, f"not a valid Instagram handle: {target}"
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
