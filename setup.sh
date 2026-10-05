#!/usr/bin/env bash
# setup.sh — configure the Logfare route for ZimZilla.
#
#   ./setup.sh
#
# Finds a Logfare credential (reusing an existing profile or
# $ANTHROPIC_AUTH_TOKEN if present, otherwise prompting for one), writes the
# profile to ~/.zimzilla/logfare/source with mode 600, installs the LiteLLM
# proxy if it is missing, starts it, and verifies the route end to end.
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

# The interpreter that carries ZimZilla's dependencies — the user's own venv.
# Prefer the active one ($VIRTUAL_ENV), then a python3 on PATH. This script
# never creates a venv.
if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
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
#    The shipped repo profile comes last — it is the shared fallback.
for candidate in "$PROFILE" "$HOME/logfare/source" "$SELF/../logfare/source" \
                 "$SELF/packaging/logfare/source"; do
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

if [[ -n "$KEY" ]]; then
  ok "using the credential from $KEY_SRC"
else
  warn "no credential found"
  printf '\n  Paste your Logfare API key (starts with lfu_).\n'
  printf '  Get one at https://logfare.ai — it is stored with mode 600 and never echoed.\n\n'
  printf '  key: '
  # -s: do not echo the key to the terminal.
  read -rs KEY || true
  printf '\n'
  [[ -n "$KEY" ]] || die "no key entered."
  KEY_SRC="prompt"
  ok "key received (${#KEY} chars)"
fi

[[ "$KEY" == lfu_* ]] || warn "that key does not start with 'lfu_' — continuing anyway"

# --- 2. write the profile ----------------------------------------------------

step "Writing the credential profile"
mkdir -p "$LOG_DIR"
# umask is process-global, so the 077 has to stay inside this subshell: leaking
# it would make every file created later in this script (the proxy config, the
# manager, the Token Juice route) 0600/0700 instead of 0644/0755.
(
  umask 077   # create the file 600 from the start — never world-readable
  {
    printf '# ZimZilla — Logfare profile (generated by setup.sh)\n'
    printf '# Treat this file as a credential. Never commit it.\n\n'
    printf 'export ANTHROPIC_BASE_URL="http://localhost:%s"\n' "$PORT"
    printf 'export ANTHROPIC_AUTH_TOKEN="%s"\n' "$KEY"
    printf 'export ANTHROPIC_MODEL="claude-opus-4.6"\n\n'
    printf '# Load-bearing: an empty string keeps ANTHROPIC_AUTH_TOKEN authoritative\n'
    printf '# and clears any real API key inherited from the shell.\n'
    printf 'export ANTHROPIC_API_KEY=""\n\n'
    printf '# Model aliases — see litellm-config.yaml for the full list.\n'
    printf 'export MODEL_DEFAULT="claude-opus-4.6"\n'
    printf 'export MODEL_OPUS="claude-opus-4.6"\n'
    printf 'export MODEL_SONNET="claude-sonnet-4.6"\n'
    printf 'export MODEL_DEEPSEEK="deepseek-v3.2"\n'
    printf 'export MODEL_KIMI="kimi-k2.5"\n'
    printf 'export MODEL_KIMI_THINKING="kimi-k2-thinking"\n'
    printf 'export MODEL_GLM="glm-5"\n'
    printf 'export MODEL_GROK="grok-4.6"\n'
    printf 'export MODEL_QWEN="qwen-3.8-27b"\n'
    printf 'export MODEL_GEMMA="gemma-4-26b"\n'
    printf 'export MODEL_GEMMA_31B="gemma-4-31b"\n'
    printf 'export MODEL_GPT_OSS="gpt-oss-120b"\n'
    printf 'export MODEL_AUTO="logfare/auto"\n'
  } > "$PROFILE"
)
chmod 600 "$PROFILE"
ok "$PROFILE (mode 600)"

# --- 3. proxy files ----------------------------------------------------------

step "Checking the proxy files"
if [[ ! -f "$CONFIG" || ! -x "$SERVICE" ]]; then
  cp -f "$SELF/packaging/logfare/litellm-config.yaml" "$CONFIG"
  cp -f "$SELF/packaging/logfare/start-litellm.sh"     "$SERVICE"
  chmod +x "$SERVICE"
  ok "installed from packaging/"
else
  ok "already in place"
fi

# The Token Juice route is a sibling install, not a variant: its own port,
# profile and manager. This step only lays the files down — it does not start
# anything or ask for a credential, because the route ships with a working
# profile and is used on demand via /zim-tokenjuice. Without it a machine that
# ran setup.sh but never laid the route files down would have no Token Juice
# route at all.
TJ_DIR="$ZIMZILLA_HOME/tokenjuice"
if [[ -x "$TJ_DIR/start-litellm.sh" && -f "$TJ_DIR/litellm-config.yaml" ]]; then
  ok "tokenjuice route already in place"
elif [[ -d "$SELF/packaging/tokenjuice" ]]; then
  mkdir -p "$TJ_DIR"
  cp -f "$SELF/packaging/tokenjuice/litellm-config.yaml" "$TJ_DIR/litellm-config.yaml"
  cp -f "$SELF/packaging/tokenjuice/start-litellm.sh"    "$TJ_DIR/start-litellm.sh"
  cp -f "$SELF/packaging/tokenjuice/source.example"      "$TJ_DIR/source.example"
  chmod +x "$TJ_DIR/start-litellm.sh"
  if [[ ! -f "$TJ_DIR/source" && -f "$SELF/packaging/tokenjuice/source" ]]; then
    ( umask 077; cp -f "$SELF/packaging/tokenjuice/source" "$TJ_DIR/source" )
    chmod 600 "$TJ_DIR/source"
  fi
  ok "installed tokenjuice route (:4000)"
else
  warn "no tokenjuice packaging — /zim-tokenjuice will be unavailable"
fi

# --- 4. litellm --------------------------------------------------------------

step "Checking the LiteLLM proxy"
if command -v litellm >/dev/null 2>&1; then
  ok "litellm on PATH"
elif [[ -x "$HOME/.local/bin/litellm" || -x "$(dirname "$PY")/litellm" ]]; then
  ok "litellm present"
else
  # requirements.txt already lists litellm[proxy]; this is a safety net for a
  # venv installed before it did, or one built without the file.
  printf '  installing litellm[proxy] (this can take a minute)...\n'
  "$PY" -m pip install --quiet "litellm[proxy]" || die "could not install litellm"
  ok "installed into $PY's environment"
fi

# --- 5. start the proxy ------------------------------------------------------

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
BODY='{"model":"grok-4.6","max_tokens":8,"messages":[{"role":"user","content":"say ok"}]}'
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
  head -c 300 /tmp/zim-setup-probe.json 2>/dev/null; printf '\n'
fi
rm -f /tmp/zim-setup-probe.json

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
