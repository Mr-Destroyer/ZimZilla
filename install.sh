#!/usr/bin/env bash
# install.sh — install ZimZilla and make it runnable from anywhere.
#
#   ./install.sh
#
# Creates a private venv, installs the harness and its dependencies (plus the
# LiteLLM proxy), copies the logfare proxy files into ~/.zimzilla/logfare,
# and puts a `zimzilla` command on your PATH.
#
# Idempotent: safe to re-run after a `git pull`.
# No credential is read, written or echoed here — that is setup.sh's job.

set -euo pipefail

SELF="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SELF"

ZIMZILLA_HOME="${ZIMZILLA_HOME:-$HOME/.zimzilla}"
BIN_DIR="${ZIMZILLA_BIN_DIR:-$HOME/.local/bin}"
VENV="$SELF/.venv"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m▸\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

bold "╭──────────────────────────────────────────────╮"
bold "│  ZIMZILLA · install                       │"
bold "╰──────────────────────────────────────────────╯"

# --- 1. python ---------------------------------------------------------------

step "Checking Python"
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$(command -v "$cand")"
      break
    fi
  fi
done
[[ -n "$PY" ]] || die "Python 3.11+ not found. Install it, then re-run ./install.sh"
ok "$PY ($("$PY" --version 2>&1))"

# --- 2. curl (used for the proxy health check) -------------------------------

step "Checking curl"
command -v curl >/dev/null 2>&1 || die "curl is required (used to health-check the proxy)."
ok "curl present"

# --- 3. venv + dependencies --------------------------------------------------

step "Creating the virtualenv"
if [[ -d "$VENV" ]]; then
  ok "reusing $VENV"
else
  "$PY" -m venv "$VENV" || die "could not create a venv at $VENV"
  ok "created $VENV"
fi

VENV_PY="$VENV/bin/python"
[[ -x "$VENV_PY" ]] || die "venv python missing at $VENV_PY"

step "Installing the harness and its dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || warn "could not upgrade pip (continuing)"
"$VENV_PY" -m pip install --quiet -e "$SELF" || die "pip install failed"
ok "zimzilla + anthropic, textual, rich, pyyaml"

# --- 4. the LiteLLM proxy ----------------------------------------------------

step "Installing the LiteLLM proxy"
if command -v litellm >/dev/null 2>&1; then
  ok "litellm already on PATH"
elif [[ -x "$HOME/.local/bin/litellm" ]]; then
  ok "litellm already at ~/.local/bin/litellm"
else
  if "$VENV_PY" -m pip install --quiet "litellm[proxy]" >/dev/null 2>&1; then
    ok "installed litellm[proxy] into the harness venv"
  else
    warn "could not install litellm — run setup.sh later once the network is up"
  fi
fi

# --- 5. proxy files ----------------------------------------------------------

step "Installing logfare proxy files"
mkdir -p "$ZIMZILLA_HOME/logfare"
cp -f "$SELF/packaging/logfare/litellm-config.yaml" "$ZIMZILLA_HOME/logfare/litellm-config.yaml"
cp -f "$SELF/packaging/logfare/start-litellm.sh"     "$ZIMZILLA_HOME/logfare/start-litellm.sh"
cp -f "$SELF/packaging/logfare/source.example"       "$ZIMZILLA_HOME/logfare/source.example"
chmod +x "$ZIMZILLA_HOME/logfare/start-litellm.sh"
ok "$ZIMZILLA_HOME/logfare/{litellm-config.yaml,start-litellm.sh}"

# The profile ships with the repo so a clone works immediately. Never clobber
# one that already exists — a user may have swapped in their own key.
if [[ -f "$ZIMZILLA_HOME/logfare/source" ]]; then
  ok "existing credential profile left untouched"
elif [[ -f "$SELF/packaging/logfare/source" ]]; then
  umask 077
  cp -f "$SELF/packaging/logfare/source" "$ZIMZILLA_HOME/logfare/source"
  chmod 600 "$ZIMZILLA_HOME/logfare/source"
  ok "credential profile installed from the repo (mode 600)"
else
  warn "no credential profile yet — setup.sh writes it"
fi

# --- 6. launcher on PATH -----------------------------------------------------

step "Installing the launcher"
mkdir -p "$BIN_DIR"
# A shim that points back at this checkout: the launcher must live next to the
# venv it uses, and this way re-running install.sh after a `git pull` picks up
# any launcher changes automatically.
{
  printf '#!/usr/bin/env bash\n'
  printf '# ZimZilla launcher shim (written by install.sh).\n'
  printf '# Points at the checkout so the real launcher can find its venv.\n'
  printf 'export ZIMZILLA_ROOT=%q\n' "$SELF"
  printf 'exec %q "$@"\n' "$SELF/packaging/zimzilla"
} > "$BIN_DIR/zimzilla"
chmod +x "$BIN_DIR/zimzilla"
ok "$BIN_DIR/zimzilla -> $SELF"

# Warn if BIN_DIR is not on PATH (idempotent, exact match only).
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    warn "$BIN_DIR is not on your PATH"
    SHELL_RC=""
    case "${SHELL:-}" in
      */zsh)  SHELL_RC="$HOME/.zshrc" ;;
      */bash) SHELL_RC="$HOME/.bashrc" ;;
    esac
    if [[ -n "$SHELL_RC" ]] && ! grep -qs "ZimZilla launcher" "$SHELL_RC"; then
      {
        printf '\n# ZimZilla launcher\nexport PATH="%s:$PATH"\n' "$BIN_DIR"
      } >> "$SHELL_RC"
      ok "added $BIN_DIR to PATH in $SHELL_RC (open a new shell, or: . $SHELL_RC)"
    else
      warn "add this to your shell profile:  export PATH=\"$BIN_DIR:\$PATH\""
    fi
    ;;
esac

# --- done --------------------------------------------------------------------

printf '\n'
bold "╭──────────────────────────────────────────────╮"
bold "│  installed. one step left.                   │"
bold "╰──────────────────────────────────────────────╯"
printf '\n  Next:\n\n'
printf '    \033[36m./setup.sh\033[0m     # configure the Logfare credential + start the proxy\n'
printf '    \033[36mzimzilla\033[0m    # launch from anywhere\n\n'
