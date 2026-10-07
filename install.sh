#!/usr/bin/env bash
# install.sh — one-line installer for ZimZilla.
#
#   curl -fsSL https://raw.githubusercontent.com/Mr-Destroyer/ZimZilla/main/install.sh | bash
#
# Clones ZimZilla into ~/.local/share/zimzilla, builds a virtualenv there and
# installs the dependencies into it, then drops a `zimzilla` launcher into
# ~/.local/bin. After this the operator never creates, activates or pip-installs
# anything by hand: `zimzilla` finds its own venv and runs from any directory.
#
# The venv is built here, on this machine, rather than shipped — a venv is not
# portable (its interpreter symlink, the console-script shebangs and pyvenv.cfg
# all bake in absolute paths and one exact base interpreter), so the only way
# to hand someone a working one is to build it where it will run. That is what
# this does, and it is the same thing pipx and uv do.
#
# Idempotent: re-run it any time to update an existing install. The launcher it
# writes refreshes the venv itself when requirements.txt changes, so this only
# needs to run once.
#
# Environment overrides (all optional):
#   ZIMZILLA_REPO          git URL to install from
#                          (default https://github.com/Mr-Destroyer/ZimZilla.git)
#   ZIMZILLA_INSTALL_DIR   where the checkout and venv live
#                          (default ${XDG_DATA_HOME:-~/.local/share}/zimzilla)
#   ZIMZILLA_BIN_DIR       where the `zimzilla` launcher is written
#                          (default ~/.local/bin)
#   ZIMZILLA_SKIP_DEPS=1   lay the files down but do not build the venv
#   ZIMZILLA_SKIP_SETUP=1  do not run setup.sh at the end
#
# This is meant to be run from a pipe (`curl … | bash`), where standard input
# is the script itself — so nothing here may read stdin. External commands get
# </dev/null, and the one prompt (setup.sh asking for a key) is reconnected to
# the controlling terminal explicitly.

set -euo pipefail

REPO="${ZIMZILLA_REPO:-https://github.com/Mr-Destroyer/ZimZilla.git}"
INSTALL_DIR="${ZIMZILLA_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/zimzilla}"
BIN_DIR="${ZIMZILLA_BIN_DIR:-$HOME/.local/bin}"
ZIMZILLA_HOME="${ZIMZILLA_HOME:-$HOME/.zimzilla}"
PROFILE="$ZIMZILLA_HOME/logfare/source"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m▸\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31m✖ %s\033[0m\n' "$*" >&2; exit 1; }
note() { printf '      \033[36m%s\033[0m\n' "$*" >&2; }

# The hash the launcher also uses to decide whether the venv is stale. Same
# algorithm in both places, so the stamp written here is honoured there.
hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    return 1
  fi
}

bold "╭──────────────────────────────────────────────╮"
bold "│  ZIMZILLA · install                        │"
bold "╰──────────────────────────────────────────────╯"

# --- 0. preconditions --------------------------------------------------------

step "Checking prerequisites"
command -v git >/dev/null 2>&1 || die "git is required."

PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "python3 not found — install Python 3.11 or newer."
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11+ is required; $PY is $("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')."
ok "git and $("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"

# --- 1. get the source -------------------------------------------------------

step "Fetching ZimZilla"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  # An existing install. Fast-forward only — never merge, never touch local
  # edits; a failed pull is a warning, not a stop, because the launcher's own
  # update check will report the same thing at run time.
  if git -C "$INSTALL_DIR" pull --ff-only --quiet </dev/null 2>/dev/null; then
    ok "updated $INSTALL_DIR"
  else
    warn "could not update $INSTALL_DIR (local changes or offline) — using what is there"
  fi
elif [[ -e "$INSTALL_DIR" ]]; then
  die "$INSTALL_DIR exists but is not a git checkout — move it aside and re-run."
else
  mkdir -p "$(dirname -- "$INSTALL_DIR")"
  # A full clone, not --depth 1: the launcher's update check counts how far
  # behind the checkout is and fast-forwards it, and both want real history.
  # The repository is a couple of MB, so there is nothing to save by shallow.
  git clone --quiet "$REPO" "$INSTALL_DIR" </dev/null \
    || die "could not clone $REPO"
  ok "cloned into $INSTALL_DIR"
fi

# --- 2. build the venv -------------------------------------------------------

VENV_PY="$INSTALL_DIR/.venv/bin/python"

if [[ "${ZIMZILLA_SKIP_DEPS:-0}" == "1" ]]; then
  warn "ZIMZILLA_SKIP_DEPS=1 — skipping the venv (the launcher will not run yet)"
else
  step "Building the virtualenv"
  if [[ -x "$VENV_PY" ]]; then
    ok "venv already present"
  else
    "$PY" -m venv "$INSTALL_DIR/.venv" </dev/null || die "could not create the venv"
    ok "venv at $INSTALL_DIR/.venv"
  fi

  step "Installing dependencies (a minute or two the first time)"
  "$VENV_PY" -m pip install --quiet --disable-pip-version-check --upgrade pip </dev/null || true
  # Run from the checkout: requirements.txt ends in `-e .`, and pip resolves
  # that against the current directory rather than the requirements file. Under
  # `curl | bash` the cwd is wherever the operator happened to be.
  ( cd "$INSTALL_DIR" && \
    "$VENV_PY" -m pip install --quiet --disable-pip-version-check \
      -r "$INSTALL_DIR/requirements.txt" </dev/null ) \
    || die "could not install the dependencies — see the pip output above."
  # Stamp the requirements hash so the launcher's incremental refresh sees a
  # matching venv and does nothing on the first launch.
  if _h="$(hash_file "$INSTALL_DIR/requirements.txt")"; then
    printf '%s' "$_h" > "$INSTALL_DIR/.venv/.requirements.sha"
  fi
  ok "dependencies installed"
fi

# --- 3. the launcher on PATH -------------------------------------------------

step "Installing the launcher"
mkdir -p "$BIN_DIR"
# A tiny shim, not the launcher itself: it pins ZIMZILLA_ROOT to the checkout
# so the real launcher (which lives in packaging/) finds the venv and the
# profile no matter where `zimzilla` is invoked from. Written fresh each run so
# a moved install re-points itself.
{
  printf '#!/usr/bin/env bash\n'
  printf '# zimzilla — installed launcher (written by install.sh).\n'
  printf '# Pins the checkout and hands every argument to the real launcher.\n'
  printf 'export ZIMZILLA_ROOT=%q\n' "$INSTALL_DIR"
  printf 'exec %q "$@"\n' "$INSTALL_DIR/packaging/zimzilla"
} > "$BIN_DIR/zimzilla"
chmod 755 "$BIN_DIR/zimzilla"
ok "$BIN_DIR/zimzilla"

case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR is on PATH" ;;
  *)
    warn "$BIN_DIR is not on your PATH — add it, then open a new shell:"
    note "echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc"
    ;;
esac

# --- 4. the credential -------------------------------------------------------

if [[ "${ZIMZILLA_SKIP_SETUP:-0}" == "1" ]]; then
  warn "ZIMZILLA_SKIP_SETUP=1 — not configuring the route"
elif [[ -r "$PROFILE" ]]; then
  step "Route already configured"
  ok "$PROFILE exists"
elif [[ -r /dev/tty ]]; then
  # setup.sh prompts for the key. Reconnect its stdin to the terminal: under
  # `curl | bash` this script's own stdin is the pipe, so a bare `read` would
  # eat the rest of the script instead of waiting for the operator.
  step "Configuring the Logfare route"
  "$INSTALL_DIR/setup.sh" </dev/tty \
    || warn "setup did not finish — run $INSTALL_DIR/setup.sh when you have your key"
else
  warn "no terminal available to prompt for a key"
  note "run: $INSTALL_DIR/setup.sh"
fi

# --- done --------------------------------------------------------------------

printf '\n'
bold "╭──────────────────────────────────────────────╮"
bold "│  installed.                                  │"
bold "╰──────────────────────────────────────────────╯"
printf '\n'
printf '    \033[36mzimzilla\033[0m                 # launch, from anywhere\n'
printf '    \033[36mzimzilla -C ~/proj\033[0m       # run against another directory\n'
printf '    \033[36mzimzilla --mode zim\033[0m      # full auto, follows AGENTS.md\n\n'
printf '  Re-run this installer to update:  %s/install.sh\n' "$INSTALL_DIR"
printf '  It keeps its own venv, so nothing to activate and nothing to pip-install.\n\n'
