#!/usr/bin/env bash
# setup.sh — configure the Logfare route for ZimZilla.
#
#   ./setup.sh
#
# Finds a Logfare credential — an existing profile, then $ANTHROPIC_AUTH_TOKEN —
# writes the profile to ~/.zimzilla/logfare/source with mode 600, installs the
# LiteLLM proxy if it is missing, starts it, and verifies the route end to end.
#
# It never prompts. If there is no key it does not invent one: the route is
# installed and left unstarted, and the script says where a key goes. A
# credential committed to this repository is not a credential — it is a public
# budget that anyone can spend, and it cannot be taken back out of the history
# of everyone who cloned it. So none ships.
#
# The venv is the user's own: activate it, `pip install -r requirements.txt`,
# then run this. Idempotent. The key is never echoed back and never leaves this
# machine.

set -euo pipefail

SELF="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ZIMZILLA_HOME="${ZIMZILLA_HOME:-$HOME/.zimzilla}"
LOG_DIR="$ZIMZILLA_HOME/logfare"
PROFILE="$LOG_DIR/source"
CONFIG="$LOG_DIR/litellm-config.yaml"
SERVICE="$LOG_DIR/start-litellm.sh"
PORT="${LITELLM_PORT:-4001}"

# The interpreter that carries ZimZilla's dependencies.
#
# Prefer the checkout's own venv — the one install.sh builds — so `./setup.sh`
# works with no activation after a global install. Then an activated venv
# ($VIRTUAL_ENV), then a python3 on PATH. This script never creates a venv.
_self_dir="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"
_root="${ZIMZILLA_ROOT:-$_self_dir}"
if [[ -x "$_root/.venv/bin/python" ]]; then
  PY="$_root/.venv/bin/python"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
  PY="$VIRTUAL_ENV/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  PY=""
fi

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m▸\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31m✖ %s\033[0m\n' "$*" >&2; exit 1; }
# A command the user should run by hand. stderr, like the warnings around it.
note() { printf '      \033[36m%s\033[0m\n' "$*" >&2; }

bold "╭──────────────────────────────────────────────╮"
bold "│  ZIMZILLA · setup                         │"
bold "╰──────────────────────────────────────────────╯"

[[ -n "$PY" ]] || die "no python3 on PATH — create a venv, activate it, then: pip install -r requirements.txt"
command -v curl >/dev/null 2>&1 || die "curl is required."

# The harness must be runnable by this interpreter, or this script would wire
# up a proxy for something that cannot start.
#
# Check the THIRD-PARTY dependencies, not `import zimzilla`: zimzilla is
# imported from the checkout itself (see PYTHONPATH below), so `import zimzilla`
# would succeed from the source tree even in a bare venv with nothing
# installed — passing the check that exists to catch exactly that. The deps are
# what the user's `pip install -r requirements.txt` actually provides.
if ! ( cd / && "$PY" -c 'import anthropic, textual, rich, yaml' ) >/dev/null 2>&1; then
  printf '\n\033[31m✖ ZimZilla dependencies are not installed for %s\033[0m\n\n' "$PY" >&2
  printf '  Activate your venv and install them, then re-run ./setup.sh:\n\n' >&2
  note "python3 -m venv .venv && source .venv/bin/activate"
  note "pip install -r requirements.txt"
  printf '\n' >&2
  exit 1
fi

# --- 1. locate a credential --------------------------------------------------

step "Looking for a Logfare credential"

KEY=""
KEY_SRC=""

# A Logfare key starts with `lfu_`. Only a key with that prefix is trusted
# automatically — a shell often has ANTHROPIC_AUTH_TOKEN exported for a
# *different* provider (another proxy, another vendor), and silently adopting
# it would point the harness at the wrong service with no visible symptom.
# That is exactly the failure this whole profile mechanism exists to prevent.
is_logfare_key() { [[ "${1:-}" == lfu_* ]]; }

# 1. An existing profile. These files are Logfare-specific, so try them first.
#    The shipped repo profile is deliberately NOT in this list: it is shared,
#    so it has to lose to a personal key in the environment. See step 3.
for candidate in "$PROFILE" "$HOME/logfare/source" "$SELF/../logfare/source"; do
  [[ -f "$candidate" ]] || continue
  found="$(set -a; . "$candidate" 2>/dev/null; printf '%s' "${ANTHROPIC_AUTH_TOKEN:-}")"
  if is_logfare_key "$found"; then
    KEY="$found"; KEY_SRC="$candidate"; break
  elif [[ -n "$found" ]]; then
    warn "ignoring $candidate — its token is not a Logfare key (no lfu_ prefix)"
  fi
done

# 2. The ambient token, but only when it is genuinely a Logfare key.
if [[ -z "$KEY" ]] && is_logfare_key "${ANTHROPIC_AUTH_TOKEN:-}"; then
  KEY="$ANTHROPIC_AUTH_TOKEN"; KEY_SRC="\$ANTHROPIC_AUTH_TOKEN"
fi

# 3. Nothing else. There is deliberately no shipped key: packaging/logfare/source
#    is a template whose token is a placeholder, so a key committed there would
#    be public, spendable by anyone, and impossible to unpublish. A fresh install
#    therefore finishes without a Logfare credential and says how to add one —
#    `/logfare-api-setup <key>` in the TUI, or the profile file directly.
#
#    A profile that already exists is never overwritten, so anyone with their own
#    key keeps it by doing nothing.

if [[ -n "$KEY" ]]; then
  ok "using the credential from $KEY_SRC"
  [[ "$KEY" == lfu_* ]] || warn "that key does not start with 'lfu_' — continuing anyway"
else
  warn "no Logfare key found — installing the route without starting it"
  warn "get one at https://logfare.ai (they start with 'lfu_'), then either"
  warn "  run  /logfare-api-setup <key>  in the TUI, or"
  warn "  write it to $PROFILE"
fi

# --- 2. write the profile ----------------------------------------------------

step "Writing the credential profile"
mkdir -p "$LOG_DIR"
# umask is process-global, so the 077 has to stay inside this subshell: leaking
# it would make every file created later in this script (the proxy config, the
# manager, the Token Juice profile) 0600/0700 instead of 0644/0755.
(
  umask 077   # create the file 600 from the start — never world-readable
  {
    printf '# ZimZilla — Logfare profile (generated by setup.sh)\n'
    printf '# Treat this file as a credential. Never commit it.\n\n'
    printf 'export ANTHROPIC_BASE_URL="http://localhost:%s"\n' "$PORT"
    if [[ -n "$KEY" ]]; then
      printf 'export ANTHROPIC_AUTH_TOKEN="%s"\n' "$KEY"
    else
      printf '# No key yet — /logfare-api-setup <key> in the TUI writes this line.\n'
      printf 'export ANTHROPIC_AUTH_TOKEN=""\n'
    fi
    printf 'export ANTHROPIC_MODEL="deepseek-v4.1-flash"\n\n'
    printf '# Load-bearing: an empty string keeps ANTHROPIC_AUTH_TOKEN authoritative\n'
    printf '# and clears any real API key inherited from the shell.\n'
    printf 'export ANTHROPIC_API_KEY=""\n\n'
    printf '# Model aliases — the models Logfare currently serves.\n'
    printf '# Logfare retires models without notice; /logfare-models in the TUI\n'
    printf '# lists what is live right now.\n'
    printf 'export MODEL_DEFAULT="deepseek-v4.1-flash"\n'
    printf 'export MODEL_DEEPSEEK_FLASH="deepseek-v4.1-flash"\n'
    printf 'export MODEL_QWEN="qwen-3.8-27b"\n'
    printf 'export MODEL_GEMMA="gemma-4-26b"\n'
    printf 'export MODEL_AUTO="logfare/auto"\n'
  } > "$PROFILE"
)
chmod 600 "$PROFILE"
ok "$PROFILE (mode 600)"

# --- 3. proxy files ----------------------------------------------------------

step "Checking the proxy files"

# These are shipped artifacts, not user config: the catalog and the manager are
# maintained in packaging/ and an install has to track them. Copying only when
# the file is *missing* left every existing install frozen at whatever it first
# received — so a fix in packaging/ (a required config key, a pidfile guard)
# never reached the copy that actually runs, and the proxy kept failing on a bug
# that was already fixed in the tree. Sync on content instead, but keep a .bak
# of any locally-edited copy rather than silently overwriting someone's work.
sync_shipped() {
  local src="$1" dst="$2" mode="${3:-}"
  if [[ ! -f "$src" ]]; then
    warn "missing from packaging/: $(basename "$src") — leaving $dst alone"
    return 0
  fi
  if [[ ! -f "$dst" ]]; then
    cp -f "$src" "$dst"; [[ -n "$mode" ]] && chmod "$mode" "$dst"
    ok "installed $(basename "$dst")"
  elif cmp -s "$src" "$dst"; then
    [[ -n "$mode" ]] && chmod "$mode" "$dst"
    ok "$(basename "$dst") up to date"
  else
    cp -f "$dst" "$dst.bak"
    cp -f "$src" "$dst"; [[ -n "$mode" ]] && chmod "$mode" "$dst"
    warn "$(basename "$dst") updated — previous copy kept at $dst.bak"
  fi
}

sync_shipped "$SELF/packaging/logfare/litellm-config.yaml" "$CONFIG"
sync_shipped "$SELF/packaging/logfare/start-litellm.sh"     "$SERVICE" 755

# The Token Juice route is a profile and nothing else. Its upstream serves Chat
# Completions itself, so there is no proxy to install, start or keep healthy:
# ZimZilla talks to api.tokenjuice.ai directly and this file carries the key.
#
# A shared key ships in the tree (packaging/tokenjuice/source), so the route
# works on a fresh clone with no signup. A personal profile always wins: an
# existing ~/.zimzilla profile is left alone, and a $ANTHROPIC_AUTH_TOKEN that
# already looks like a Token Juice key is written to one. That is what
# `/zim-tokenjuice <key>` does in the TUI as well.
TJ_DIR="$ZIMZILLA_HOME/tokenjuice"
if [[ -d "$SELF/packaging/tokenjuice" ]]; then
  mkdir -p "$TJ_DIR"
  sync_shipped "$SELF/packaging/tokenjuice/source.example" "$TJ_DIR/source.example"

  # The proxy this route used to run is gone. Remove the copies an older
  # install left behind rather than leaving a manager nothing will ever start —
  # and, worse, a config naming a port that nothing answers.
  for _stale in start-litellm.sh litellm-config.yaml; do
    if [[ -f "$TJ_DIR/$_stale" ]]; then
      rm -f "$TJ_DIR/$_stale"
      ok "removed the retired tokenjuice proxy file: $_stale"
    fi
  done

  # A profile written before the proxy was removed points at :4000. The key in
  # it is still the right key — only the address and the model id changed — so
  # it is rewritten in place rather than thrown away.
  migrate_tokenjuice_profile() {
    local file="$1" tmp
    [[ -r "$file" ]] || return 0
    grep -qE '^export ANTHROPIC_BASE_URL="http://(localhost|127\.0\.0\.1):4000"' \
      "$file" || return 0
    tmp="$(mktemp)"
    sed -E \
      -e 's|^export ANTHROPIC_BASE_URL=.*|export ANTHROPIC_BASE_URL="https://api.tokenjuice.ai/v1"|' \
      -e 's|^export ANTHROPIC_MODEL=.*|export ANTHROPIC_MODEL="deepseek-ai/DeepSeek-V4.1-Flash-1017"|' \
      "$file" > "$tmp"
    grep -q '^export ANTHROPIC_MODEL=' "$tmp" \
      || printf 'export ANTHROPIC_MODEL="deepseek-ai/DeepSeek-V4.1-Flash-1017"\n' >> "$tmp"
    cp -p "$file" "$file.bak"
    mv "$tmp" "$file"
    chmod 600 "$file"
    ok "tokenjuice profile migrated to the direct route"
  }

  if [[ -f "$TJ_DIR/source" ]]; then
    migrate_tokenjuice_profile "$TJ_DIR/source"
    ok "tokenjuice profile in place ($TJ_DIR/source)"
  elif [[ "${ANTHROPIC_AUTH_TOKEN:-}" == tj_* ]]; then
    (
      umask 077
      {
        printf '# ZimZilla — Token Juice profile (generated by setup.sh)\n'
        printf '# Treat this file as a credential. Never commit it.\n'
        printf '# Direct route: no proxy, nothing to start.\n\n'
        printf 'export ANTHROPIC_BASE_URL="https://api.tokenjuice.ai/v1"\n'
        printf 'export ANTHROPIC_AUTH_TOKEN="%s"\n' "$ANTHROPIC_AUTH_TOKEN"
        printf 'export ANTHROPIC_MODEL="deepseek-ai/DeepSeek-V4.1-Flash-1017"\n'
        printf 'export ANTHROPIC_API_KEY=""\n'
      } > "$TJ_DIR/source"
    )
    chmod 600 "$TJ_DIR/source"
    ok "tokenjuice profile written from \$ANTHROPIC_AUTH_TOKEN"
  elif [[ -f "$SELF/packaging/tokenjuice/source" ]]; then
    # Nothing to copy. The shared profile is read straight from the checkout,
    # so a rotated key arrives with the next update instead of sitting frozen
    # in a per-user file. A personal profile, if written later, wins over it.
    ok "tokenjuice: using the shared profile shipped in the checkout"
  else
    warn "no Token Juice credential — the shared profile is missing from this checkout"
    note "to enable it, write your tj_ key to:"
    note "  $TJ_DIR/source   (mode 600)"
    note "the format is in $TJ_DIR/source.example"
  fi
  ok "installed tokenjuice route (direct, no proxy)"
else
  warn "no tokenjuice packaging — /zim-tokenjuice will be unavailable"
fi

# The IteraCompute route is a sibling install like Token Juice: its own port,
# profile and manager. This step lays the files down and seeds a profile only
# when a key can be found — it does not start anything, and the route is used on
# demand via /zim-iteracompute.
#
# IteraCompute serves the OpenAI protocol only, so the proxy is not optional:
# a session pointed straight at the upstream 404s every turn. No credential
# ships in the tree, and none could — the key is per-account. The route is
# seeded from an existing ~/.zimzilla profile (left alone) or a
# $ANTHROPIC_AUTH_TOKEN that looks like an IteraCompute key; with neither it is
# not discovered, and `/zim-iteracompute` says to run /iteracompute-api-setup
# rather than failing later at request time.
IC_DIR="$ZIMZILLA_HOME/iteracompute"
if [[ -d "$SELF/packaging/iteracompute" ]]; then
  mkdir -p "$IC_DIR"
  sync_shipped "$SELF/packaging/iteracompute/litellm-config.yaml" "$IC_DIR/litellm-config.yaml"
  sync_shipped "$SELF/packaging/iteracompute/start-litellm.sh"    "$IC_DIR/start-litellm.sh" 755

  if [[ -f "$IC_DIR/source" ]]; then
    ok "iteracompute profile in place ($IC_DIR/source)"
  elif [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" \
          && "${ANTHROPIC_BASE_URL:-}" == *"iteracompute"* ]]; then
    # Only when the environment is ALREADY aimed at IteraCompute: a bare token
    # in the shell is far more likely to be Logfare's, and seeding this route
    # with another provider's key would fail at request time with a 401 the
    # operator has no reason to connect to this file.
    (
      umask 077
      {
        printf '# ZimZilla — IteraCompute profile (generated by setup.sh)\n'
        printf '# Treat this file as a credential. Never commit it.\n\n'
        printf 'export ANTHROPIC_BASE_URL="http://localhost:4002"\n'
        printf 'export ANTHROPIC_AUTH_TOKEN="%s"\n' "$ANTHROPIC_AUTH_TOKEN"
        printf 'export ANTHROPIC_MODEL="qwen/qwen3.8-27b"\n'
        printf 'export ANTHROPIC_API_KEY=""\n'
      } > "$IC_DIR/source"
    )
    chmod 600 "$IC_DIR/source"
    ok "iteracompute profile written from \$ANTHROPIC_AUTH_TOKEN"
  else
    note "to enable /zim-iteracompute, set your key in the TUI:"
    note "  /iteracompute-api-setup <key>"
    note "  (the free tier is 30M tokens per UTC month on qwen/qwen3.8-27b)"
  fi
  ok "installed iteracompute route (:4002)"
else
  warn "no iteracompute packaging — /zim-iteracompute will be unavailable"
fi

# The InfronAI route is the same shape as IteraCompute's, one port over: its own
# port, profile and manager. Files are laid down here; nothing is started, and
# the route is used on demand via /zim-infronai.
#
# InfronAI does serve the Anthropic protocol natively, but its free models are
# `openai`-endpoint only in the catalog, so the proxy is not optional for the
# free tier. No credential ships in the tree, and none could — the key is
# per-account, and a free account still needs a $5 balance. The route is seeded
# from a $ANTHROPIC_AUTH_TOKEN only when the environment is already aimed at
# InfronAI; with neither that nor an existing profile it is not discovered, and
# `/zim-infronai` names /infronai-api-setup rather than failing at request time.
INFRON_DIR="$ZIMZILLA_HOME/infronai"
if [[ -d "$SELF/packaging/infronai" ]]; then
  mkdir -p "$INFRON_DIR"
  sync_shipped "$SELF/packaging/infronai/litellm-config.yaml" "$INFRON_DIR/litellm-config.yaml"
  sync_shipped "$SELF/packaging/infronai/start-litellm.sh"    "$INFRON_DIR/start-litellm.sh" 755

  if [[ -f "$INFRON_DIR/source" ]]; then
    ok "infronai profile in place ($INFRON_DIR/source)"
  elif [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" \
          && "${ANTHROPIC_BASE_URL:-}" == *"infron"* ]]; then
    # Only when the environment is ALREADY aimed at InfronAI: a bare token in
    # the shell is far more likely to be Logfare's, and seeding this route with
    # another provider's key would fail at request time with a 401 the operator
    # has no reason to connect to this file.
    (
      umask 077
      {
        printf '# ZimZilla — InfronAI profile (generated by setup.sh)\n'
        printf '# Treat this file as a credential. Never commit it.\n\n'
        printf 'export ANTHROPIC_BASE_URL="http://localhost:4003"\n'
        printf 'export ANTHROPIC_AUTH_TOKEN="%s"\n' "$ANTHROPIC_AUTH_TOKEN"
        printf 'export ANTHROPIC_MODEL="deepseek/deepseek-v4.1-flash:free"\n'
        printf 'export ANTHROPIC_API_KEY=""\n'
      } > "$INFRON_DIR/source"
    )
    chmod 600 "$INFRON_DIR/source"
    ok "infronai profile written from \$ANTHROPIC_AUTH_TOKEN"
  else
    note "to enable /zim-infronai, set your key in the TUI:"
    note "  /infronai-api-setup <key>"
    note "  (free models need a \$5 balance; 1000 requests/day across all of them)"
  fi
  ok "installed infronai route (:4003)"
else
  warn "no infronai packaging — /zim-infronai will be unavailable"
fi

# The doctrine file zim mode follows. Installed into ZIMZILLA_HOME so that
# `zimzilla --mode zim` works from ANY directory: without a copy here, a session
# opened in an unrelated project finds no AGENTS.md and silently falls back to
# the default prompt. A project's own AGENTS.md still wins — the workdir is
# searched first — so this is a fallback, not an override.
#
# Synced on content like the proxy files, but a locally-edited copy is kept as
# .bak rather than overwritten silently: the operator may have tuned their
# doctrine, and that is the one file here that is genuinely theirs.
if [[ -f "$SELF/AGENTS.md" ]]; then
  sync_shipped "$SELF/AGENTS.md" "$ZIMZILLA_HOME/AGENTS.md"
else
  warn "no AGENTS.md in the checkout — zim mode will fall back to the default prompt"
fi

# --- 3b. pagekite (worldwide /phish forwarder) -------------------------------

step "Checking PageKite"
PK_DIR="$ZIMZILLA_HOME/pagekite"
mkdir -p "$PK_DIR"
if [[ -f "$SELF/packaging/pagekite/pagekite.py" ]]; then
  sync_shipped "$SELF/packaging/pagekite/pagekite.py" "$PK_DIR/pagekite.py" 755
else
  printf '  downloading pagekite.py...\n'
  if curl -fsSL -o "$PK_DIR/pagekite.py" https://pagekite.net/pk/pagekite.py; then
    chmod 755 "$PK_DIR/pagekite.py"
    ok "downloaded $PK_DIR/pagekite.py"
  else
    warn "could not download pagekite.py — /phish will fall back to cloudflared/ngrok"
  fi
fi
if [[ ! -f "$ZIMZILLA_HOME/pagekite.name" ]]; then
  warn "no kite name at $ZIMZILLA_HOME/pagekite.name"
  warn "write your kite (e.g. zim.pagekite.me) there, or export PAGEKITE_NAME."
  warn "first-time signup:  $PK_DIR/pagekite.py --signup"
else
  ok "kite name: $(tr -d '[:space:]' < "$ZIMZILLA_HOME/pagekite.name")"
fi

# --- 4. litellm --------------------------------------------------------------

# The proxy gets its OWN venv. It cannot live in the harness's: litellm[proxy]
# requires rich<14.0 and textual 8.x requires rich>=14.2, so an environment
# holding both cannot resolve at all. start-litellm.sh already looks in
# $ZIMZILLA_HOME/venv, so that is where this goes. A litellm that already
# exists anywhere is reused rather than duplicated.
PROXY_VENV="$ZIMZILLA_HOME/venv"
PROXY_PY="$PROXY_VENV/bin/python"

step "Checking the LiteLLM proxy"
if command -v litellm >/dev/null 2>&1; then
  ok "litellm on PATH"
elif [[ -x "$HOME/.local/bin/litellm" ]]; then
  ok "litellm present at ~/.local/bin/litellm"
elif [[ -x "$PROXY_PY" ]] && "$PROXY_PY" -c 'import litellm' >/dev/null 2>&1; then
  ok "litellm in $PROXY_VENV"
else
  printf '  building the proxy venv at %s (this can take a minute)...\n' "$PROXY_VENV"
  "$PY" -m venv "$PROXY_VENV" || die "could not create $PROXY_VENV"
  "$PROXY_PY" -m pip install --quiet --disable-pip-version-check --upgrade pip || true
  # Both floors are load-bearing, and they live here rather than in
  # requirements.txt because this is the environment that has to satisfy them.
  #
  # litellm[proxy]>=1.100.1 — the harness talks to this proxy over Anthropic's
  # /v1/messages, and every Logfare model is registered as `openai/<name>`.
  # Older litellm only served /v1/messages for provider == anthropic, so an
  # openai/* model raised "Anthropic messages provider config not found for
  # model: ..." — a 500 on the one route ZimZilla actually uses, while
  # /v1/chat/completions kept working and made the proxy look healthy.
  #
  # uvloop>=0.22.1 — litellm hardcodes uvicorn's event loop to uvloop on Linux
  # (litellm/proxy/proxy_cli.py::_get_loop_type — not configurable, no flag or
  # env var overrides it). uvloop <0.22 still imports
  # BaseDefaultEventLoopPolicy, which Python 3.14 removed, so the proxy dies at
  # startup with "cannot import name 'BaseDefaultEventLoopPolicy'" and never
  # binds its port.
  "$PROXY_PY" -m pip install --quiet --disable-pip-version-check \
    "litellm[proxy]>=1.100.1" "uvloop>=0.22.1" </dev/null \
    || die "could not install litellm[proxy] into $PROXY_VENV"
  ok "litellm installed into $PROXY_VENV"
fi

# --- 5. start the proxy ------------------------------------------------------

if [[ -z "$KEY" ]]; then
  step "Skipping the proxy start"
  warn "no key to send — start it later with $SERVICE start"
else
step "Starting the proxy on :$PORT"
# Reuse anything already healthy on the port — the user may already run a
# proxy (another tool, or an earlier install) and starting a second one would
# only fail to bind.
if curl -s -o /dev/null -w '%{http_code}' \
     "http://127.0.0.1:$PORT/health/liveliness" --max-time 2 | grep -q 200; then
  ok "a proxy is already serving :$PORT — reusing it"
elif "$SERVICE" start; then
  ok "proxy healthy on :$PORT"
else
  warn "proxy did not start — see /tmp/litellm-zim.log"
  warn "continuing; the launcher will retry on next run."
fi

# --- 6. verify the route -----------------------------------------------------

step "Verifying the route"
if curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health/liveliness" --max-time 3 | grep -q 200; then
  ok "proxy answers on :$PORT"
else
  warn "proxy not answering yet"
fi

# A live round-trip through the proxy proves the key actually works. The
# upstream reports "temporarily unavailable" under load, so give it one retry
# before calling the key bad.
BODY='{"model":"deepseek-v4.1-flash","max_tokens":8,"messages":[{"role":"user","content":"say ok"}]}'
probe() {
  curl -s -o /tmp/zim-setup-probe.json -w '%{http_code}' \
    -X POST "http://127.0.0.1:$PORT/v1/messages" \
    -H "content-type: application/json" \
    -H "anthropic-version: 2023-06-01" \
    -H "x-api-key: $KEY" \
    --max-time 30 -d "$BODY" || true
}
CODE="$(probe)"
if [[ "$CODE" != "200" ]]; then
  printf '  upstream busy (HTTP %s) — retrying once...\n' "$CODE"
  sleep 3
  CODE="$(probe)"
fi
if [[ "$CODE" == "200" ]]; then
  ok "live request through the proxy succeeded"
else
  warn "live request returned HTTP $CODE"
  warn "either the key is wrong or Logfare's upstream is down right now:"
  # The probe file only exists if curl got far enough to write it. Under
  # `set -e` an unconditional `head` on a missing file would end the script
  # here — after the profile is written and the proxy is installed — and exit
  # non-zero, making a completed setup look like a failed one.
  head -c 300 /tmp/zim-setup-probe.json 2>/dev/null || true
  printf '\n'
fi
rm -f /tmp/zim-setup-probe.json
fi

# --- done --------------------------------------------------------------------

printf '\n'
bold "╭──────────────────────────────────────────────╮"
bold "│  setup complete.                             │"
bold "╰──────────────────────────────────────────────╯"
printf '\n'
printf '    \033[36mzimzilla\033[0m                 # launch (starts the proxy if needed)\n'
printf '    \033[36mzimzilla -C ~/proj\033[0m       # run against another directory\n'
printf '    \033[36mzimzilla --mode zim\033[0m      # full auto, follows AGENTS.md\n\n'
printf '  Manage the proxy:\n'
printf '    %s {status|stop|restart|logs}\n\n' "$SERVICE"
