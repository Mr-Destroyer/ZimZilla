<div align="center">

<img src="docs/hero.svg" alt="ZIMZILLA" width="820">

**An autonomous coding agent that lives in your terminal.**

It reads your repo, runs commands, writes files, chases down bugs and
keeps going until the job is done — while you watch it work.

<sub>by **Mr-Destroyer / ZIM**</sub>

![python](https://img.shields.io/badge/python-3.11%2B-00ff41?style=flat-square&labelColor=040704)
![platform](https://img.shields.io/badge/platform-linux-00ff41?style=flat-square&labelColor=040704)
![interface](https://img.shields.io/badge/interface-terminal-00ff41?style=flat-square&labelColor=040704)
![status](https://img.shields.io/badge/status-active-00ff41?style=flat-square&labelColor=040704)

</div>

<img src="docs/terminal.svg" alt="a ZIMZILLA session" width="900">

---

## See it

<div align="center">

### Cold start

<img src="docs/screenshots/01-boot.png" alt="ZIMZILLA boot screen — ASCII banner, system check, shell ready" width="880">

*Banner reveal, a live system check, then the shell takes over.*

### The shell

<img src="docs/screenshots/02-shell.png" alt="the ZIMZILLA shell — loop rail, transcript with a live tool card, telemetry rail, status bar" width="880">

*A transcript that streams, a tool card that shows the call while it runs,
and two rails that keep the loop and the cost in view. The left rail is the
agent's state — think, call, observe, idle. The right rail is the bill:
throughput, how full the context is, what it has cost, which files it touched.*

<table>
<tr>
<td width="50%">

<img src="docs/screenshots/03-models.png" alt="switching models at runtime">

**Swap engines mid-session.**<br>
<sub>Prices in, prices out. No restart, no lost context.</sub>

</td>
<td width="50%">

<img src="docs/screenshots/04-modes.png" alt="the mode list">

**Change how much rope it gets.**<br>
<sub>From read-only spectator to full send.</sub>

</td>
</tr>
</table>

</div>

---

## What it does

ZIMZILLA is a coding agent you drive from a terminal. You describe what you
want; it works out the steps, does them, and shows you everything it did.

- **Talks, then acts.** Ask a question and it answers. Ask for a change and it
  makes the change — reading files, running commands, editing code.
- **Sees your project.** Point it at any directory and it works there: it can
  read, search, and reason across the whole tree.
- **Runs real commands.** A full shell, wired straight into the loop, with
  output captured and shown inline.
- **Edits like a human.** Changes arrive as diffs you can read before they land.
- **Shows its work.** Every command, every file touched, every result — visible
  in the transcript as it happens, not buried in a log.
- **Keeps score.** Live token counts and a running cost estimate, per turn and
  per session.
- **Answers to a dial.** A mode switch decides whether it merely investigates,
  edits freely, or runs everything unattended.
- **Stays in bounds.** An optional scope file declares what it may touch, and a
  second declares what it must never go near.
- **Remembers the session.** Save it, load it, compact it when the context
  gets heavy.
- **Looks the part.** A green-on-black terminal, animated from boot to prompt.

---

## Install

Bring your own venv. ZimZilla ships a `requirements.txt`; that is the whole
install.

```bash
git clone <this-repo> ~/zimzilla && cd ~/zimzilla
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # deps + the LiteLLM proxy + zimzilla
./setup.sh                          # verify the route end-to-end
```

`requirements.txt` ends with `-e .`, so it installs the package too and drops a
`zimzilla` command into your venv's `bin/`. Then, from any directory (with the
venv active):

```bash
zimzilla                           # launch
zimzilla -C ~/some/project         # run against another directory
zimzilla --mode zim                # full auto, follows AGENTS.md
zimzilla --allow allow.yaml        # declare your targets
zimzilla --deny out-of-scope.yaml  # hosts that are never touched
```

`zimzilla` reads its credentials from the environment, so the venv must be
active and the profile sourced. If you would rather not export anything by
hand, `./run.sh` starts from the checkout and sources the profile for you.

Out of the box it runs **`grok-4.6`** against Logfare. Switch any time with
`/model`, or override before launch:

```bash
zimzilla -m claude-opus-4.6        # this run only
ANTHROPIC_MODEL=$MODEL_KIMI zimzilla   # via a profile alias
```

### Updates

ZimZilla updates itself on launch. Because `requirements.txt` ends in `-e .`,
the running code *is* the git checkout — so "updating" means fast-forwarding
that checkout, not reinstalling anything. Every launch checks the upstream
first; if there are new commits it says so, pulls them, and restarts on the
new code before the harness comes up.

```
  ⟩ UPDATE
  ◈ updated by 3 commits
    3be2d0c docs(readme): troubleshooting for the pip cache trap
    4a58d45 docs(readme): say which model it starts on
  · restarting on the new code…
```

**Launching ZimZilla pulls and runs code from your `origin`.** That is the
honest description of the feature — it is safe only insofar as you trust the
remote you cloned from, the same trust you extend by running `git pull` by
hand. It is on by default because that is what makes it useful; turn it off
with either:

```bash
zimzilla --no-update              # this launch only
export ZIMZILLA_NO_UPDATE=1       # every launch in this shell
```

What it will and will not do:

| | |
|---|---|
| **New commits, clean tree** | fast-forwards (`--ff-only`) and restarts on the new code |
| **New commits, edited tracked files** | reports them and **does not pull** — your uncommitted work is never touched, stashed or reset |
| **Untracked files** | ignored — a fast-forward cannot lose them, so they do not block the update |
| **Your branch has diverged** | reports it and **does not pull** — never merges, never rebases |
| **Offline, no remote, not a checkout** | says nothing and boots normally |
| **`requirements.txt` changed** | updates, then tells you to re-run `./setup.sh` |

If you are developing ZimZilla itself, `ZIMZILLA_NO_UPDATE=1` in your shell
profile is the setting you want — otherwise a commit you push from elsewhere
will restart the checkout under you on the next launch.

---

## Usage

```
--model, -m NAME     model to use
--workdir, -C DIR    working directory
--mode NAME          auto | edits | plan | zim | danger | uncensored   (default: auto)
--allow PATH         declared targets — arms the session
--deny PATH          hosts that are never touched
--agents PATH        the doctrine file `zim` mode follows (default: workdir,
                     its parent, ~/.zimzilla, then ~)
--theme {green,amber,cyan}
--rain               start with matrix rain on
--no-rain            no rain anywhere
--max-tokens N       max output tokens per reply
--max-iterations N   ceiling on autonomous steps (default 40)
--unsafe             disable the filesystem jail
--list-models
--version
```

---

## Modes

The mode decides how much the agent is allowed to do on its own. Switch at
runtime with `/mode <name>`; the active mode is always visible in the header.

| Mode | Behaviour |
|---|---|
| `auto` *(default)* | **Full auto** — file writes and shell commands, no confirmation. |
| `edits` | File work is free; the shell is removed entirely. |
| `plan` | Read-only. Investigate, propose, change nothing. |
| `zim` | **Full auto**, governed by your `AGENTS.md`. |
| `danger` | **Full auto**, operator-directed — no questions asked. |
| `uncensored` | **Full auto**, the default prompt is thrown out. The model cannot refuse; it only thinks how to do what you said. |

`zim`, `danger` and `uncensored` are the **armed** modes: everything runs
unattended, and the header turns red to say so. A tool that a mode disallows
does not merely get discouraged — it stops existing for that session, so
`plan` cannot write a file even if you ask it to.

`uncensored` is the one that changes *how the model thinks*, not just which
tools run without asking. Thinking models refuse by reasoning their way to
"I shouldn't"; this mode replaces the whole system prompt so that chain of
thought is not available. `/mode uncensored`, then say the thing.

### zim mode and `AGENTS.md`

`zim` mode hands the wheel to a doctrine file of your choosing. Write the rules
you want followed; that file becomes the agent's whole operating manual.

It is looked for in this order, first hit wins:

1. `<workdir>/AGENTS.md` — the project's own doctrine
2. `<workdir>/../AGENTS.md` — a checkout's root file, seen from a subdirectory
3. `~/.zimzilla/AGENTS.md` — the installed doctrine (`./setup.sh` puts it there)
4. `~/AGENTS.md` — a hand-kept global file

Step 3 is what makes `zim` mode work from **any** directory: the file is
installed once, so a session opened in an unrelated project still finds your
doctrine instead of silently falling back to the default prompt. A project that
ships its own `AGENTS.md` still wins, because the workdir is checked first.
Override the whole search with `--agents PATH`; switching to `zim` at runtime
with `/mode zim` re-runs the search, so a session that started elsewhere picks
the file up too.

---

## Tools

| Tool | Asks first? | What it's for |
|---|---|---|
| `bash` | in some modes | Run a shell command. |
| `read_file` | no | Read a file, whole or by line range. |
| `write_file` | in some modes | Create or replace a file. |
| `edit_file` | in some modes | Surgical string replacement in a file. |
| `glob` | no | Find files by pattern. |
| `grep` | no | Search file contents by regex. |
| `list_dir` | no | List a directory. |
| `search_web` | no | Search the web. |
| `web_fetch` | no | Pull down a URL. |

When a mode makes a tool ask first, you get a modal showing the exact command
or a colour diff of the change, and three keys:

```
[y] run it     [n] no     [a] always, this session
```

---

## Scope

Two files, one job each. Both are picked up automatically from the working
directory, or pointed at with `--allow` / `--deny`.

**`allow.yaml` — your declared targets.** Its presence **arms the session**:
gated tools stop asking. It is a declaration of what you are authorised to
work on, not a fence around you — it restricts nothing and blocks nothing.

```yaml
domain:            # a domain also covers its subdomains
  - example.com
ip:
  - 203.0.113.10
cidr:
  - 198.51.100.0/24
```

**`out-of-scope.yaml` — never touch.** This one is enforced. A command that
reaches for one of these hosts is stopped before it runs, and fetching one is
refused outright.

```
⛔ BLOCKED — SCOPE GUARD — HARD BLOCK
   out-of-scope.example.com — denied — listed domain
```

```yaml
domain:
  - out-of-scope.example.com
```

Deny always beats allow. Loading only `out-of-scope.yaml` arms nothing — it
only subtracts.

### What this is and is not

Worth being straight about, because the difference matters:

- The allow-list is an **arming mechanism, not a safety control**. With it
  loaded, the only thing between the agent and an arbitrary host is
  `out-of-scope.yaml`.
- The deny-list is a **check on what it's about to do, not a sandbox**. It
  reads the command as written. A target assembled at runtime, or reached
  through an unlisted redirect, is not something it can see. It catches the
  obvious mistake; it does not stop a determined command.

Use it as a seatbelt, not as a cage.

### Upgrading from `scope.yaml`

`scope.yaml` is no longer read. It was one file doing three jobs at once, and
its meaning cannot be carried across safely — reading it as a deny-list would
block exactly the hosts it declared. A stale `scope.yaml` is reported loudly at
startup and then ignored. Rename it to `allow.yaml` or `out-of-scope.yaml`.

---

## Slash commands

| Command | Effect |
|---|---|
| `/help` | command reference |
| `/mode [name]` | show or switch mode |
| `/model [name]` | show or switch the model |
| `/cost` | session token + cost breakdown |
| `/scope` | scope status and entries |
| `/theme green\|amber\|cyan` | switch palette |
| `/rain` | toggle the matrix rain |
| `/save [name]` | write the session to disk |
| `/load [name]` | restore a session |
| `/compact` | summarise history to free context |
| `/team <task>` | fan the task out across parallel agents |
| `/osint <kind> <target>` | open-source recon on a target |
| `/phish <host>` | clone a login page, serve it, harvest creds into zim-pane |
| `/clear` | wipe transcript and history |
| `/exit` | leave |

**Prompt affordances.** Type `/` and the command menu opens as you type; type
`@` and a file picker opens, rooted at your working directory. Accepting a file
mention attaches its contents to your message, so the agent can act on it
immediately. `↑`/`↓` to move, `Tab` to accept, `Esc` to dismiss.

**Keys.** `Ctrl+K` command palette · `Ctrl+C` interrupts the current turn ·
`Ctrl+D` exits · `Ctrl+L` clears.

**Mouse.** Drag across the transcript (or either rail) to select, and the
selection is copied to your clipboard the moment you release — so you can paste
a tool result straight into a report. The rails are resizable: grab the inner
edge of the loop rail or the telemetry rail and drag it to trade width between
the rail and the transcript. The transcript re-wraps to whatever width the pane
ends up at, so nothing goes ragged when you resize the terminal and come back.
Hold your terminal's usual modifier (usually `Shift` or `Alt`) if you want a
native selection instead.

The palette is the fastest way to reach anything: hit `Ctrl+K` and type a few
letters. It searches every slash command, every mode, every model on your
upstream, and every file in the working directory — so `/mode zim` is `mkz`,
and a file you half-remember is three characters away.

---

## OSINT

`/osint` runs open-source intelligence on a target you declare. It is a
router: pick a *kind*, and the command validates the target, opens a case
directory, and hands that kind's playbook to the main agent as one turn. The
recon then runs exactly like any other turn — same tools, same scope guard,
same transcript, same `Ctrl+C` — so you can watch it and interrupt it.

```
/osint email target@example.com

  ◈ CASE      email · target@example.com
  ◈ EVIDENCE  ~/.zimzilla/osint/email-target-example.com-20261005-143210/
  ◈ NOTICE    authorised use only — your own footprint, a consented audit, or a declared engagement
```

Run `/osint` with no arguments for the menu:

| Kind | Target | Status |
|---|---|---|
| `email` | email address | **built** |
| `phone` | phone number | not built yet |
| `facebook` | profile URL or id | not built yet |
| `tiktok` | username or profile URL | not built yet |
| `instagram` | username or profile URL | not built yet |
| `discord` | user id or invite | not built yet |
| `github` | username | **built** |

The unbuilt kinds are listed so the surface is discoverable; selecting one
says so and stops rather than pretending to work. Each is a drop-in playbook
away — the registry in `zimzilla/osint.py` is the only place that changes.

**`email`** works the address through seven phases: validation and provider
fingerprinting (MX/TXT, whois), Gravatar, breach exposure, account discovery
(`holehe`, `sherlock`), identity correlation, domain intelligence for custom
domains, and paste/code leak search. Every finding is tagged **CONFIRMED**,
**PROBABLE** or **UNVERIFIED**, and the run ends by writing `report.md` into
the case directory.

**`github`** runs the same idea in reverse — it starts from a handle and hunts
for the person behind it. Phase 2 is the point of the run: a GitHub account
almost always leaks its owner's email address, and that is the pivot from a
username to an identity. It mines `git` history (every name and address that
ever committed), `.patch` headers (`From: Name <email>`), GPG uids and SSH
keys, and the files developers routinely commit addresses into —
`package.json`, `pyproject.toml`, and `.mailmap`, whose whole purpose is
mapping contributors to addresses. From there it profiles the account,
inventories the repos, maps the social graph (orgs, collaborators) and
reconstructs a timeline. Committed secrets are reported by location and
described, **never reproduced**. A pasted profile URL is accepted and
unwrapped, so `https://github.com/torvalds` and `torvalds` are the same run.

```bash
/osint github torvalds
/osint github https://github.com/torvalds    # same thing
```

**Why it is fast.** The unauthenticated GitHub API allows only 60 requests an
hour, and spending them one call per step is what used to make this run drag.
The playbook is built to avoid it: it checks `rate_limit` once up front, then
does its work with `git clone --bare` and `git log`, which are **not** metered
by the API at all, and batches the few API calls it still needs into one script
per phase instead of one round-trip per fact. A single clone yields every
address that ever touched a repo — including old ones the owner has since
changed, which are often the ones that turn up in breaches. With a
`GITHUB_TOKEN` in the environment (or a `gh auth` login) the limit rises to
5000 an hour and the API becomes cheap, but no token is required.

**The report.** Every kind ends by writing `report.md`. Each finding is a
five-line block — **What** it is, **Where** it came from, **Confidence**,
what it **Means** in plain language, and how to **Verify** it — so the report
explains itself instead of dumping tool output. It opens with a `## Headline`
and a coverage table showing which phases ran, and closes with `## Gaps and
next steps`. A section that found nothing says so explicitly: an empty section
is a finding, not an omission. The format is shared by every kind, so the email
and github reports read the same way.

**Where evidence lands, and where it goes.** Each run gets its own directory
under `<state_dir>/osint/<kind>-<target>-<timestamp>/` — by default
`~/.zimzilla/osint/…`, beside your sessions rather than in whatever repo you
happen to be sitting in. The agent writes its raw output, downloaded avatars,
clones and the final report there, and the path is printed when the run starts.
**When the run finishes, the report is moved to `<state_dir>/reports/` and the
case directory is deleted** — clones and raw API dumps can run to hundreds of
megabytes, and they are not worth keeping once the report cites them. A run
that produced *no* report (it failed, or you interrupted it before the write)
is left exactly as it is, so you can inspect what it managed to collect.

**Tools it uses, if you have them.** Breach lookups, `holehe` and `sherlock`
are optional — the playbook runs without them and says which sources it had to
skip. Any API keys (DeHashed, IntelX, LeakCheck) are read from the
environment; none are hard-coded.

> **Authorised use only.** `/osint` is for investigating your own footprint, a
> consented audit, or a target declared in an engagement. The command states
> this on every run and cannot verify it — that part is yours. Recon against
> people or accounts you have no lawful basis to investigate is not what this
> is for.

---

## Phish

`/phish` clones a login page, serves it locally, opens a public tunnel if a
forwarder is installed, and streams every hit and every captured credential
into **zim-pane** — a live rail on the right of the transcript.

```
/phish site.com

  ◈ PHISH     site.com
  ◈ LOCAL     http://127.0.0.1:43127/
  ◈ PUBLIC    https://random-words.trycloudflare.com  via cloudflared
  ◈ CLONE     cloned https://site.com (18421 bytes)
  ◈ LOGS      ~/.zimzilla/phish/site.com-20261005-143210/
  · watch zim-pane for hits and credentials
```

What happens, in order:

1. The target is validated and checked against `out-of-scope.yaml`. A denied
   host is refused before anything is fetched.
2. The live page is fetched and every `<form>` is rewritten to POST back here.
   Relative assets keep resolving against the real origin via `<base href>`,
   so the clone still looks like the original. If the fetch fails, a branded
   template for that host is served instead — zim-pane says so.
3. A local HTTP server binds an ephemeral port. GET serves the page; POST
   captures the fields, appends them to `creds.jsonl`, and returns a holding
   page.
4. A public tunnel is opened if one of `cloudflared`, `ngrok` or `lt`
   (localtunnel) is on `PATH`. First URL wins. No forwarder means LAN-only —
   the local URL still works.
5. zim-pane lights up and tails hits and credentials for as long as the
   campaign is alive. `/phish status` reprints the URLs; `/phish stop` tears
   the server and the tunnel down.

A second `/phish <host>` replaces the running campaign. Quitting the harness
stops it too. Campaign files stay under
`<state_dir>/phish/<host>-<timestamp>/` so you can inspect the cloned HTML
and the JSONL logs after the pane is gone.

> **Authorised use only.** `/phish` is for your own property, a consented
> audit, or a target declared in an engagement. The command states this on
> every run and cannot verify it — that part is yours.

---

## Layout

```
requirements.txt              dependencies (incl. zimzilla itself, via -e .)
setup.sh                      route check
run.sh                        run from the checkout, sourcing the profile

zimzilla/                     the package
  agent.py                    the tool-use loop and mode doctrine
  config.py                   modes, defaults, model registry, pricing
  tools.py                    the nine tools
  sandbox.py                  the filesystem jail
  scope.py                    allow.yaml / out-of-scope.yaml
  session.py                  save / load / list
  osint.py                    /osint kinds, playbooks, case dirs, report archive
  update.py                   launch-time check for new commits
  theme.py                    green / amber / cyan palettes
  termbg.py                   asks the terminal for its own background
  ui/                         the terminal interface

allow.yaml.example            declared targets
out-of-scope.yaml.example     hosts that are never touched

docs/                         README graphics + screenshots
  gen_graphics.py             regenerate the animated header/hero SVGs
  gen_screenshots.py          re-shoot the screenshots from a live app
tests/                        the test suite
```

---

## Tests

```bash
python tests/test_phase4.py        # from an activated venv
python tests/test_osint.py         # /osint — registry, validation, wiring
python tests/test_update.py        # self-update — real git, throwaway repos
```

Stubs the model and drives the interface directly, so it needs no network and
no credentials. `test_update.py` runs real `git` against repositories it
builds in a temp directory — it never touches your own checkout or the
network.

---

## Troubleshooting

**`zimzilla: command not found`** — your venv is not active, or the package is
not installed in it. Activate it (`source .venv/bin/activate`) and run
`pip install -r requirements.txt`.

**`no credentials found`** — run `./setup.sh`, then source the profile it
writes: `set -a; . ~/.zimzilla/logfare/source; set +a`. Or just launch with
`./run.sh`, which does that for you.

**`the endpoint rejected the model name`** — run `/model` for valid names.

**`pip install` dies with `IncompleteRead` on the same package every time** —
a corrupt entry in pip's download cache, not a network problem. The tell is
that the byte counts in the error are identical across runs; a genuine
connection drop gives you different numbers and a different package. Clear it
and reinstall:

```bash
pip cache purge
pip install --retries 10 --timeout 60 -r requirements.txt
```

`pip install --no-cache-dir -r requirements.txt` bypasses the cache without
clearing it, if you would rather not re-download everything else.

**It says the endpoint is up but stalled** — something between you and the
model went quiet. It will time out rather than hang; check the endpoint is
actually serving.

---

<div align="center">
<sub>Built by <b>Mr-Destroyer / ZIM</b> · yt <b>@Study_Hard69</b> · ig <b>zimthegoat</b></sub>
</div>
