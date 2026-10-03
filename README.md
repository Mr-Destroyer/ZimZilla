# ZIMZILLA

An autonomous coding agent in the terminal — a green-on-black hacker TUI that
streams model output token by token, runs sandboxed tools behind a permission
gate, and enforces an optional network scope file for bug-bounty work.

> **Identity.** ZimZilla is created by **ZIM (Mr-Destroyer)** and answers as
> ZimZilla — not as "Claude" or any underlying vendor model. The model string
> is the engine; the harness is the product.

It speaks the **Anthropic Messages API** (`/v1/messages`) directly, so it points
at whatever `ANTHROPIC_BASE_URL` you give it. On this box that is the local
LiteLLM → Logfare proxy on `:4001`, default model **`deepseek-v4.1-flash`**.

```
zimzilla  ──▶  Anthropic Messages API  ──▶  local LiteLLM :4001  ──▶  Logfare
```

---

## Install

Two commands, then it works from anywhere:

```bash
git clone <this-repo> ~/zimzilla && cd ~/zimzilla
./install.sh        # venv, dependencies, the LiteLLM proxy, a `zimzilla` command on PATH
./setup.sh          # start the proxy + verify the route
```

The Logfare key ships in the repo at `packaging/logfare/source`, so a fresh
clone needs no prompting. `install.sh` copies it to
`~/.zimzilla/logfare/source` with mode 600; `setup.sh` starts the proxy and
proves the route with a live request.

> ⚠️ **This repo contains a live credential.** It is a shared key for a private
> team — a deliberate trade of zero setup friction for the key being in the
> tree. Keep the repo private, and rotate the key at
> <https://logfare.ai> if it is ever published.

To use a different key, edit `packaging/logfare/source` or run `./setup.sh` and
paste one when prompted. An existing `~/.zimzilla/logfare/source` is never
overwritten, so a per-machine key survives re-installs.

Then, from any directory:

```bash
zimzilla                        # starts the proxy if it is down, then launches
zimzilla -C ~/some/project      # run against another directory
zimzilla --mode zim             # full auto, follows AGENTS.md
zimzilla --scope scope.yaml     # enable the network scope guard
zimzilla --unsafe               # disable the filesystem jail (careful)
```

### What goes where

| Path | Contents |
|---|---|
| `packaging/logfare/source` | the shipped credential profile (committed) |
| `~/.zimzilla/logfare/source` | the active profile (mode 600, never overwritten) |
| `~/.zimzilla/logfare/litellm-config.yaml` | proxy config — every Logfare model |
| `~/.zimzilla/logfare/start-litellm.sh` | proxy manager (`start`/`stop`/`status`/`logs`) |
| `~/.local/bin/zimzilla` | launcher shim pointing at the checkout |

Manage the proxy directly with
`~/.zimzilla/logfare/start-litellm.sh {status|stop|restart|logs}`.

### Pointing at something else

The harness speaks the Anthropic Messages API, so it is not tied to Logfare.
Set `ANTHROPIC_BASE_URL` (and `ANTHROPIC_API_KEY`) and skip the proxy entirely
with `ZIMZILLA_NO_PROXY=1`. Any endpoint offering `/v1/messages` works.

### Troubleshooting the install

- **`zimzilla: command not found`** — `~/.local/bin` is not on your PATH.
  `install.sh` adds it to `~/.bashrc`/`~/.zshrc` if it can; otherwise add
  `export PATH="$HOME/.local/bin:$PATH"` yourself and open a new shell.
- **`profile not found … Refusing to run`** — deliberate. Without the profile
  the harness would silently use whatever `ANTHROPIC_*` your shell already
  exports, which is how you end up talking to the wrong provider. Run
  `./setup.sh`, or set `ZIMZILLA_NO_PROXY=1` to use a direct endpoint.
- **A proxy is already running on :4001** — reused, not fought over. `setup.sh`
  and the launcher both detect a healthy port and leave it alone.
- **`litellm not found`** — `./install.sh` installs it into the harness venv;
  re-run it once you have network access.

---

## Layout

```
install.sh                    venv + deps + proxy + PATH command
setup.sh                      Logfare credential + proxy start + route check
run.sh                        run from the checkout without installing
requirements.txt
pyproject.toml
scope.yaml.example            template scope file

zimzilla/                     the Python package (the agent + TUI)
  agent.py                    the tool-use loop and mode doctrine
  config.py                   modes, defaults, environment plumbing
  tools.py                    bash / file / search tools
  ui/
    app.py                    the Textual app
    boot.py                   boot screen (banner + system check + credits)
    banner.py                 the ZIMZILLA ASCII font
    widgets.py                header, chat pane, status bar

packaging/
  zimzilla                    the launcher, as installed
  logfare/
    litellm-config.yaml       proxy config — every Logfare model
    start-litellm.sh          proxy manager (start/stop/status/logs)
    source.example            credential profile template

zimzilla/
  __main__.py                 CLI entrypoint (argparse)
  config.py                   config, model registry, pricing
  theme.py                     green / amber / cyan palettes
  agent.py                    streaming loop, retry/backoff, accounting
  tools.py                    the seven tools + schemas + previews
  sandbox.py                  path jail
  scope.py                    network scope guard
  session.py                  save / load / list
  ui/
    app.py                    main Textual app + slash commands
    boot.py                   boot screen (banner reveal + system check)
    banner.py                 5x6 block font
    complete.py               slash-command + @file completion popup
    rain.py                   matrix rain (canvas model + rain-aware log)
    renderers.py              tool panels, diffs, syntax theme
    widgets.py                header, chat, activity, status bar

tests/
  test_phase4.py              identity, modes, zim/AGENTS.md, popup, @mentions
```

---

## The agent loop

1. Your message is appended to the history and sent with the tool schemas.
2. The response **streams** — text renders token by token with a blinking block
   cursor and a rotating hacker verb (`decrypting`, `probing`, `enumerating`…).
3. Any `tool_use` blocks are executed (through the permission gate), their
   `tool_result`s appended, and the model is called again.
4. The loop repeats until the model stops requesting tools, or `--max-iterations`
   (default 40) is hit.

Transient failures (`429`, `5xx`, connection errors, timeouts) are retried up to
5 times with exponential backoff and jitter. The full message history is kept;
multiple tool calls per turn are supported.

Tokens and estimated cost are tracked **per turn and per session**, shown live in
the status bar and in full via `/cost`.

---

## Tools

| Tool | Gated? | Notes |
|---|---|---|
| `bash` | **yes**¹ | Timeout + output truncation. Scope-checked. |
| `read_file` | no | Optional line range. |
| `write_file` | **yes**¹ | Diff preview shown at the gate. |
| `edit_file` | **yes**¹ | Exact string replace; must be unique unless `replace_all`. |
| `glob` | no | Pattern search. Jailed (see below). |
| `grep` | no | Regex content search. Jailed (see below). |
| `list_dir` | no | Directory listing. |

¹ Gating is per mode: `auto`, `zim` and `danger` auto-approve all three; `edits`
drops bash entirely; `plan` drops all three. See **Modes**.

### Permission gate

Reads are auto-approved. In a mode that does not auto-approve a tool, `bash`,
`write_file` and `edit_file` raise a bordered modal showing the exact **command**
or a coloured **diff**:

```
[y] execute    [n] deny    [a] always this session
```

`a` auto-approves that tool for the rest of the session. With the default `auto`
mode (and `zim`/`danger`) nothing reaches this modal — every tool runs straight
away.

---

## Modes

The current mode decides which tools exist at all and which run without asking.
Switch at runtime with `/mode <name>` or at launch with `--mode <name>`; the
active mode is shown as a badge in the header.

| Mode | Tools | Behaviour |
|---|---|---|
| `auto` *(default)* | all | **Full auto** — file writes *and* bash run with no confirmation. |
| `edits` | all but `bash` | File work is free; the shell is removed entirely. |
| `plan` | read-only | No writes, no shell — investigate and produce a plan. |
| `zim` | all | **Full auto**, and the system prompt is replaced by `AGENTS.md`. |
| `danger` | all | **Full auto**, operator-directed: obeys without questioning. |

`zim` and `danger` are the *armed* modes — both run every tool unattended, and
both show in red in the header. They differ only in what governs the model:
`zim` hands control to `AGENTS.md`, while `danger` keeps the harness's own
prompt plus a doctrine that the operator's instructions are carried out
directly, without second-guessing or asking for confirmation.

A denied tool is not merely discouraged: it is withheld from the model's tool
list, and if the model calls it anyway the call returns a `mode_denied` result
rather than executing. So `plan` cannot write even if asked, and `edits` cannot
shell out.

### zim mode and `@AGENTS.md`

`zim` mode follows the operator's **AGENTS.md** to the letter, with every tool
running unattended. The file's contents *replace* the default system prompt, so
the harness becomes whatever doctrine that file declares.

The path is auto-discovered in this order — `<workdir>/AGENTS.md`,
`<workdir>/../AGENTS.md`, then `~/AGENTS.md` — or set explicitly with
`--agents PATH`. If no file is found, zim mode falls back to the standard prompt
and says so.

```
/mode zim        # header arms in red:  ◆ ZIM
/zim              full auto · follows AGENTS.md
```

---

## Prompt affordances

**Slash-command popup.** Type `/` and the command menu opens immediately, before
you finish typing; keep typing to filter. `/mode ` and `/theme ` open their own
argument list.

**File mentions.** Type `@` and a picker rooted at the working directory opens;
keep typing to filter by path. Accepting `@alpha.py` attaches that file's
contents to the message the model receives (directories attach a listing), so a
mention is immediately actionable rather than just a pointer.

**Keys in the popup:** `↑`/`↓` select · `Tab` accept · `Esc` close. The popup
never takes focus, so typing continues normally.

### Sandbox

Every file tool resolves paths through a jail rooted at the working directory.
Escapes are refused — including `..` traversal and **symlinks pointing outside**
the jail. `--unsafe` disables the jail.

`glob` and `grep` take a pattern as well as a path, and `Path.glob` will follow
`..` and symlinks on its own. Both tools therefore reject patterns containing
`..` or an absolute/`~` prefix, and re-validate **every** match against the jail
before returning it (or reading its contents).

`bash` runs with the working directory as its `cwd`, but a shell can by nature
reach absolute paths; that is why **bash is always gated** — you see and approve
every command. File *reads/writes* remain jailed regardless.

### Scope guard (bug bounty)

Drop a `scope.yaml` in the working directory (or pass `--scope`). Any bash
command that appears to reach a host outside the allow-list is **hard-blocked
before it runs**:

```
⛔ BLOCKED — SCOPE GUARD — HARD BLOCK
   evil-corp.com — host not in scope
```

```yaml
domains:            # a domain also authorises its subdomains
  - example.com
ips:
  - 203.0.113.10
cidrs:
  - 198.51.100.0/24
```

Rules:

- **No scope file** → guard off (ordinary coding session).
- **Empty scope file** → *nothing* is authorised; all network targets blocked.
- **Populated** → only listed hosts are reachable.

The detector recognises URLs, `host:port`, IPs, CIDRs and bare hostnames, and
understands tool prefixes (`sudo nmap …`, `FOO=bar curl …`). To avoid punishing
normal work, a bare dotted token is only treated as a host when the command runs
a known network tool or the TLD is unambiguously a domain — so `cat report.json`
is never mistaken for a target. `localhost` is always allowed (the proxy).

Hardening notes:

- A trailing-dot FQDN (`evil.com.`) is recognised as the same host as
  `evil.com`, so it cannot slip past the guard.
- A network command containing **runtime command substitution** (`$(…)` or
  backticks) builds a target the token scan cannot see; such a command is
  blocked as unresolvable rather than allowed. Literal targets are still
  checked normally.

---

## Slash commands

| Command | Effect |
|---|---|
| `/help` | command reference |
| `/mode [name]` | show or switch mode: `auto` \| `edits` \| `plan` \| `zim` \| `danger` |
| `/clear` | wipe transcript and history |
| `/model [name]` | show or switch the model |
| `/cost` | session token + cost breakdown |
| `/scope` | scope guard status and entries |
| `/rain` | toggle matrix rain |
| `/theme green\|amber\|cyan` | switch palette |
| `/save [name]` | write session to `~/.zimzilla/sessions/` |
| `/load [name]` | restore a session (bare `/load` lists them) |
| `/compact` | summarise history to free context |
| `/exit` | leave (or `Ctrl+D`) |

**Keys:** `Ctrl+C` interrupts the current turn · `Ctrl+D` exits · `Ctrl+L` clears.

---

## Model switching

```
/model                             # list names + prices
/model claude-sonnet-5-5           # switch at runtime
ANTHROPIC_MODEL=gpt-6-sol ./run.sh
```

The list of known names lives in `zimzilla/config.py`. Any model the proxy
offers works even if unlisted; cost falls back to a default rate and is labelled
as an estimate.

---

## CLI flags

```
--model, -m NAME     model to use
--workdir, -C DIR    working directory
--scope PATH         scope.yaml
--unsafe             disable the filesystem jail
--theme {green,amber,cyan}
--rain               start with rain on in the shell (off by default)
--no-rain            disable rain entirely, including the boot screen
--mode {auto,edits,plan,zim,danger}
                     agent mode (default: auto)
--agents PATH        AGENTS.md followed by zim mode
--max-tokens N       max output tokens per reply
--list-models
--version
```

---

## Design notes

- **Boot screen.** Typewriter/glitch reveal of the ASCII banner, a system-check
  readout (API key, endpoint, model, cwd, scope, sandbox, tools), then fade in.
  Keystrokes typed during boot are buffered and replayed into the prompt —
  press `Enter` to submit them immediately, or just wait for auto-continue.
- **Matrix rain.** The boot screen always shows the full-canvas animation. In
  the shell it is **off by default** — it competes with the transcript — and is
  turned on with `/rain` or `--rain`. When on, a per-column drop model
  (`RainCanvas`) is painted *into* the transcript and activity panes by
  `RainRichLog`, which overlays dim glyphs only on blank cells, at low density,
  so text is never obscured. (Textual's compositor paints front-to-back and
  gives each cell to the frontmost widget without alpha blending, so a widget on
  a lower layer would simply be hidden behind the panes.) Also paused while a
  turn is running.
- **Colour discipline.** Only the palette's black, green, dim green, cyan-green,
  red and amber are used anywhere in the UI — no stray colours.
- **Retry visibility.** Backoff events are shown inline rather than hidden, so
  a slow proxy is visible rather than looking like a hang.

---

## Tests

```bash
cd ~/zimzilla
.venv/bin/python tests/test_phase4.py
```

The suite stubs the model and drives the app with Textual's pilot, so it needs
no network and no API key. It covers the identity prompt, per-mode tool
exposure and gating (including that `zim` runs bash ungated while `plan` and
`edits` cannot), `AGENTS.md` loading, the slash/`@` popup, and `@`-mention
expansion.

A live smoke test of the real endpoint is `python -m zimzilla` in a pty; the
harness itself is exercised end-to-end by typing `/mode`, `/help` and a prompt.

---

## Troubleshooting

**`no credentials found`.** Run `./setup.sh`. It writes
`~/.zimzilla/logfare/source`; set `ANTHROPIC_AUTH_TOKEN` (preferred) or
`ANTHROPIC_API_KEY` yourself if you would rather not use the proxy.

**`cannot reach the API endpoint`.** The LiteLLM proxy is down:
`~/logfare/start-litellm.sh status` (or `start`).

**`the endpoint rejected the model name`.** Run `/model` for valid names.

**Commands look slow.** Logfare's upstream is sometimes "temporarily
unavailable"; the harness retries with backoff and reports each attempt.
