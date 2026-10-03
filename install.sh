#!/usr/bin/env bash
# install.sh — install ZimZilla and make it runnable from anywhere.
#
#   ./install.sh
#
# Creates a private venv, installs the harness and its dependencies (plus the
# LiteLLM proxy), copies the logfare proxy files into ~/.zimzilla/logfare,
# and puts a `zimzilla` command on your PATH.
#
# The launcher goes on PATH *before* the dependency install, so `zimzilla` is
# reachable even when pip cannot finish. If pip does fail, this script prints
# the exact commands to run by hand and stops; `./setup.sh` afterwards picks up
# where it left off.
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
# A command the user should run by hand. stderr, like the warnings around it.
note() { printf '      \033[36m%s\033[0m\n' "$*" >&2; }

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

# --- 3. venv -----------------------------------------------------------------

step "Creating the virtualenv"
if [[ -d "$VENV" ]]; then
  ok "reusing $VENV"
else
  "$PY" -m venv "$VENV" || die "could not create a venv at $VENV"
  ok "created $VENV"
fi

VENV_PY="$VENV/bin/python"
[[ -x "$VENV_PY" ]] || die "venv python missing at $VENV_PY"

# A venv built (or populated) with sudo leaves root-owned files behind. pip can
# still *unlink* inside site-packages, but it cannot write the __pycache__ next
# to a root-owned one, and the install dies with a "Permission denied" naming a
# .pyc path that looks nothing like the real cause. Reclaim the tree up front.
step "Checking venv ownership"
STRAY="$(find "$VENV" ! -user "$(id -un)" 2>/dev/null | wc -l)"
if [[ "$STRAY" -eq 0 ]]; then
  ok "all files owned by $(id -un)"
else
  warn "$STRAY files in $VENV belong to another user (built with sudo?)"
  printf '  Taking them back needs root — sudo will ask for your password.\n' >&2
  # Prompt only when there is a terminal to prompt on (or sudo is already
  # passwordless). Without either, sudo would hang or fail, so print the fix.
  if command -v sudo >/dev/null 2>&1 && { sudo -n true 2>/dev/null || [[ -t 0 ]]; }; then
    if sudo chown -R "$(id -u):$(id -g)" "$VENV"; then
      ok "took ownership of $VENV"
    else
      warn "chown failed — pip will likely fail below"
      note "sudo chown -R $(id -un):$(id -gn) \"$VENV\""
    fi
  else
    warn "no terminal for a sudo prompt — pip will likely fail below"
    note "sudo chown -R $(id -un):$(id -gn) \"$VENV\""
  fi
fi

# --- 4. launcher on PATH -----------------------------------------------------
# Deliberately before the dependency install: wiring PATH does not depend on
# pip, and the user should end up with a `zimzilla` command either way.

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

# Put BIN_DIR on PATH. Skips silently when it is already there.
if ! case ":$PATH:" in *":$BIN_DIR:"*) true ;; *) false ;; esac; then
  warn "$BIN_DIR is not on your PATH"
  SHELL_RC=""
  case "${SHELL:-}" in
    */zsh)  SHELL_RC="$HOME/.zshrc" ;;
    */bash) SHELL_RC="$HOME/.bashrc" ;;
  esac
  # $SHELL is unset under cron and some launchers; fall back to whichever
  # profile actually exists rather than giving up with just a warning.
  if [[ -z "$SHELL_RC" ]]; then
    for cand in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile" "$HOME/.bash_profile"; do
      [[ -f "$cand" ]] && { SHELL_RC="$cand"; break; }
    done
  fi
  [[ -n "$SHELL_RC" ]] || SHELL_RC="$HOME/.profile"

  if grep -qs "ZimZilla launcher" "$SHELL_RC" 2>/dev/null; then
    ok "$BIN_DIR already exported in $SHELL_RC"
  else
    {
      printf '\n# ZimZilla launcher\nexport PATH="%s:$PATH"\n' "$BIN_DIR"
    } >> "$SHELL_RC"
    ok "added $BIN_DIR to PATH in $SHELL_RC (open a new shell, or: . $SHELL_RC)"
  fi
fi

# --- 5. dependencies ---------------------------------------------------------

step "Installing the harness and its dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || warn "could not upgrade pip (continuing)"

if "$VENV_PY" -m pip install --quiet -e "$SELF"; then
  ok "zimzilla + anthropic, textual, rich, pyyaml"
else
  printf '\n\033[31m✖ pip install failed\033[0m\n\n' >&2
  printf '  Finish it by hand, then run \033[36m./setup.sh\033[0m to wire up the proxy:\n\n' >&2
  note "$VENV_PY -m pip install --upgrade pip"
  note "$VENV_PY -m pip install -e \"$SELF\""
  printf '\n  If pip reported "Permission denied" on a .pyc under the venv, the\n' >&2
  printf '  venv holds root-owned files (it was built with sudo). Take it back:\n\n' >&2
  note "sudo chown -R $(id -un):$(id -gn) \"$VENV\""
  printf '\n  Do not just "rm -rf" the venv: its __pycache__ is root-owned, so the\n' >&2
  printf '  delete stops halfway and leaves a broken venv install.sh will reuse.\n' >&2
  printf '  To rebuild it cleanly instead, remove it with sudo:\n\n' >&2
  note "sudo rm -rf \"$VENV\"   # then re-run ./install.sh"
  printf '\n' >&2
  exit 1
fi

# --- 6. the LiteLLM proxy ----------------------------------------------------

step "Installing the LiteLLM proxy"
if command -v litellm >/dev/null 2>&1; then
  ok "litellm already on PATH"
elif [[ -x "$HOME/.local/bin/litellm" ]]; then
  ok "litellm already at ~/.local/bin/litellm"
else
  if "$VENV_PY" -m pip install --quiet "litellm[proxy]" >/dev/null 2>&1; then
    ok "installed litellm[proxy] into the harness venv"
  else
    warn "could not install litellm — ./setup.sh retries it, or install it by hand:"
    note "$VENV_PY -m pip install \"litellm[proxy]\""
  fi
fi

# --- 7. proxy files ----------------------------------------------------------

# Both routes are installed, not just the default. They are independent — each
# has its own port, profile and manager — and /zim-logfare / /zim-tokenjuice
# switch between them at runtime. Installing only Logfare would leave
# /zim-tokenjuice reporting the source unavailable on a fresh machine, which is
# exactly what it did before this step copied both.
install_route() {
  local key="$1" src="$SELF/packaging/$1" dest="$ZIMZILLA_HOME/$1"
  [[ -d "$src" ]] || { warn "no packaging for '$key' — skipping"; return 0; }

  step "Installing $key proxy files"
  mkdir -p "$dest"
  cp -f "$src/litellm-config.yaml" "$dest/litellm-config.yaml"
  cp -f "$src/start-litellm.sh"    "$dest/start-litellm.sh"
  cp -f "$src/source.example"      "$dest/source.example"
  chmod +x "$dest/start-litellm.sh"
  ok "$dest/{litellm-config.yaml,start-litellm.sh}"

  # The profile ships with the repo so a clone works immediately. Never clobber
  # one that already exists — a user may have swapped in their own key.
  if [[ -f "$dest/source" ]]; then
    ok "existing credential profile left untouched"
  elif [[ -f "$src/source" ]]; then
    # umask is process-global, so it must not leak out of this function: a
    # second install_route call would otherwise create its config and manager
    # 0600/0700 instead of 0644/0755. Run the copy in a subshell.
    (
      umask 077
      cp -f "$src/source" "$dest/source"
    )
    chmod 600 "$dest/source"
    ok "credential profile installed from the repo (mode 600)"
  else
    warn "no credential profile yet — setup.sh writes it"
  fi
}

install_route logfare
install_route tokenjuice

# --- done --------------------------------------------------------------------

printf '\n'
bold "╭──────────────────────────────────────────────╮"
bold "│  installed. one step left.                   │"
bold "╰──────────────────────────────────────────────╯"
printf '\n  Next:\n\n'
printf '    \033[36m./setup.sh\033[0m     # configure the Logfare credential + start the proxy\n'
printf '    \033[36mzimzilla\033[0m    # launch from anywhere\n\n'
