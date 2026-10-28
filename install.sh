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
# </dev/null. setup.sh does not prompt either: it writes the direct Logfare
# profile without inventing a credential, so a fresh install finishes
# unattended.

set -euo pipefail

REPO="${ZIMZILLA_REPO:-https://github.com/Mr-Destroyer/ZimZilla.git}"
INSTALL_DIR="${ZIMZILLA_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/zimzilla}"
BIN_DIR="${ZIMZILLA_BIN_DIR:-$HOME/.local/bin}"
ZIMZILLA_HOME="${ZIMZILLA_HOME:-$HOME/.zimzilla}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[36m▸\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m⚠\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[31m✖ %s\033[0m\n' "$*" >&2; exit 1; }
note() { printf '      \033[36m%s\033[0m\n' "$*" >&2; }

# Animate while a command runs, then report it exactly like ok() would have.
#
# The command's output is captured and replayed only if it fails, so a spinner
# never interleaves with pip or git mid-line — and on failure the real error
# lands right above the ✖ that explains it. When stdout is not a terminal (a
# pipe, a redirect, CI, the test harness) this falls back to plain execution
# and emits no control characters, so the log stays byte-clean.
spin() {
  local msg="$1"; shift
  local rc=0

  if [[ ! -t 1 ]]; then
    "$@" || rc=$?
    if [[ $rc -eq 0 ]]; then ok "$msg"; fi
    return "$rc"
  fi

  if [[ -z "$SPIN_LOG" ]]; then
    SPIN_LOG="$(mktemp "${TMPDIR:-/tmp}/zimzilla-install.XXXXXX")"
  fi
  : > "$SPIN_LOG"
  "$@" >"$SPIN_LOG" 2>&1 &

  # A braille spinner, plus the elapsed seconds once a step drags — the
  # dependency install is a minute or two and wants a heartbeat.
  local -a frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
  local pid=$! i=0 start=$SECONDS el
  while kill -0 "$pid" 2>/dev/null; do
    el=$((SECONDS - start))
    printf '\r\033[K  \033[36m%s\033[0m %s' "${frames[i++ % ${#frames[@]}]}" "$msg"
    if (( el > 0 )); then printf ' \033[2m%ds\033[0m' "$el"; fi
    sleep 0.08
  done
  wait "$pid" || rc=$?
  printf '\r\033[K'

  if [[ $rc -eq 0 ]]; then
    ok "$msg"
  elif [[ -s "$SPIN_LOG" ]]; then
    cat "$SPIN_LOG" >&2
  fi
  return "$rc"
}

# The spinner's scratch file, created on first use and removed on any exit.
SPIN_LOG=""
trap 'if [[ -n "$SPIN_LOG" ]]; then rm -f "$SPIN_LOG"; fi' EXIT

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

# Bring an existing checkout up to the upstream head, without throwing away
# work that belongs to the operator.
#
# Returns 0 when the checkout now matches upstream, 3 when local work means it
# was deliberately left alone, 2 when the upstream could not be reached.
#
# `git pull --ff-only` is the ordinary path. When it refuses, the cause is one
# of three, and only the first can be repaired without touching the operator's
# own work:
#
#   * the upstream history was rewritten — a filtered history, a re-created
#     repository. The checkout then shares no commit with upstream, so every
#     fast-forward aborts for ever. That is how an install ends up frozen at
#     the commit it was cloned at, missing every command shipped since
#     (`/effort`, `/obfuscate`, and whatever comes next), while the installer
#     still prints "installed." and exits 0 — the operator re-runs it, reads
#     success, and the command is simply not there. Nothing of the operator's
#     is at stake in a *clean* checkout, so it is reset onto the upstream head
#     and the abandoned head is kept in a ref in case anything in it mattered.
#   * the operator has local commits on top of a history upstream still shares.
#     Those are replayed onto the upstream head — and if that conflicts, the
#     branch is put back exactly as it was and the install is reported as not
#     updated.
#   * local edits, or a network that is down. Never discarded, never merged
#     behind the operator's back: the checkout is left alone and said so.
update_checkout() {
  local dir="$1" target
  local -a gitc=(git -C "$dir")

  if "${gitc[@]}" pull --ff-only --quiet </dev/null >/dev/null 2>&1; then
    return 0
  fi
  "${gitc[@]}" fetch --quiet origin </dev/null >/dev/null 2>&1 || return 2

  target="$("${gitc[@]}" rev-parse --verify --quiet 'refs/remotes/origin/HEAD^{commit}' 2>/dev/null || true)"
  [[ -n "$target" ]] || target="$("${gitc[@]}" rev-parse --verify --quiet 'refs/remotes/origin/main^{commit}' 2>/dev/null || true)"
  [[ -n "$target" ]] || target="$("${gitc[@]}" rev-parse --verify --quiet 'refs/remotes/origin/master^{commit}' 2>/dev/null || true)"
  [[ -n "$target" ]] || return 2

  # Already there: a failed pull with nothing to pull is not a failure.
  if [[ "$("${gitc[@]}" rev-parse --quiet --verify HEAD 2>/dev/null || true)" == "$target" ]]; then
    return 0
  fi

  # Tracked-file changes belong to the operator. Untracked files cannot be lost
  # by a reset, so they do not block one.
  if [[ -n "$("${gitc[@]}" status --porcelain --untracked-files=no 2>/dev/null || true)" ]]; then
    return 3
  fi

  # Related history: the checkout carries commits of its own on top of what
  # upstream still has. Replay them; on conflict, restore the branch untouched.
  if "${gitc[@]}" merge-base HEAD "$target" >/dev/null 2>&1; then
    if "${gitc[@]}" rebase --quiet "$target" </dev/null >/dev/null 2>&1; then
      return 0
    fi
    "${gitc[@]}" rebase --abort </dev/null >/dev/null 2>&1 || true
    return 3
  fi

  # Unrelated history: a rewritten upstream. Keep the head being abandoned, so
  # the reset is recoverable, then move onto upstream.
  "${gitc[@]}" update-ref \
    "refs/zimzilla/pre-update-$(date -u +%Y%m%dT%H%M%SZ)" HEAD 2>/dev/null || true
  "${gitc[@]}" reset --hard --quiet "$target" </dev/null >/dev/null 2>&1 || return 2
  return 0
}

bold "╭──────────────────────────────────────────────╮"
bold "│  ZIMZILLA · install                        │"
bold "╰──────────────────────────────────────────────╯"

# --- 0. preconditions --------------------------------------------------------

step "Checking prerequisites"
if ! command -v git >/dev/null 2>&1; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    die "git is required — run: xcode-select --install"
  fi
  die "git is required."
fi

PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "python3 not found — install Python 3.11 or newer."
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  _pyver="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo unknown)"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    die "Python 3.11+ is required; $PY is $_pyver. Install it with: brew install python@3.12"
  fi
  die "Python 3.11+ is required; $PY is $_pyver."
fi
ok "git and $("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"

# --- 1. get the source -------------------------------------------------------

step "Fetching ZimZilla"
STALE_NOTE=""
if [[ -d "$INSTALL_DIR/.git" ]]; then
  # An existing install. `update_checkout` fast-forwards when it can and
  # recovers when the upstream history has been rewritten; it never discards
  # local work, and it never lets a checkout that did *not* update pass as one
  # that did — that silence is what leaves an install without `/effort` while
  # the operator is told the update worked.
  _was="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
  _rc=0
  spin "updated $INSTALL_DIR" update_checkout "$INSTALL_DIR" || _rc=$?
  _now="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
  case "$_rc" in
    0)
      [[ "$_now" == "$_was" ]] || note "$_was → $_now"
      # A reset onto rewritten history left the old head in a ref. Say where,
      # so "the update moved me off a commit I had" is answerable.
      _kept="$(git -C "$INSTALL_DIR" for-each-ref --sort=-creatordate \
                 --format='%(refname)' refs/zimzilla/ 2>/dev/null | head -n 1 || true)"
      if [[ -n "$_kept" && "$(git -C "$INSTALL_DIR" rev-parse "$_kept" 2>/dev/null || true)" \
            == "$(git -C "$INSTALL_DIR" rev-parse "$_was" 2>/dev/null || true)" ]]; then
        note "the upstream history was rewritten — old head kept at $_kept"
      fi
      ;;
    3)
      warn "local work in $INSTALL_DIR — left exactly as it was, still at $_now"
      note "stash or commit it, then re-run this installer, to pick up new commands"
      STALE_NOTE="this install was NOT updated — it still runs $_now"
      ;;
    *)
      warn "could not reach the upstream — using what is there ($_now)"
      STALE_NOTE="this install may be out of date — it runs $_now and the upstream was unreachable"
      ;;
  esac
elif [[ -e "$INSTALL_DIR" ]]; then
  die "$INSTALL_DIR exists but is not a git checkout — move it aside and re-run."
else
  mkdir -p "$(dirname "$INSTALL_DIR")"
  # A full clone, not --depth 1: the launcher's update check counts how far
  # behind the checkout is and fast-forwards it, and both want real history.
  # The repository is a couple of MB, so there is nothing to save by shallow.
  spin "cloned into $INSTALL_DIR" \
    git clone --quiet "$REPO" "$INSTALL_DIR" </dev/null \
    || die "could not clone $REPO"
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
    spin "venv at $INSTALL_DIR/.venv" \
      "$PY" -m venv "$INSTALL_DIR/.venv" </dev/null \
      || die "could not create the venv"
  fi

  step "Installing dependencies (a minute or two the first time)"
  # One spinner covers the whole step — the pip self-upgrade and then
  # requirements.txt — because together they are the wait worth animating.
  _install_deps() {
    "$VENV_PY" -m pip install --quiet --disable-pip-version-check --upgrade pip </dev/null || true
    # Run from the checkout: requirements.txt ends in `-e .`, and pip resolves
    # that against the current directory rather than the requirements file.
    # Under `curl | bash` the cwd is wherever the operator happened to be.
    ( cd "$INSTALL_DIR" && \
      "$VENV_PY" -m pip install --quiet --disable-pip-version-check \
        -r "$INSTALL_DIR/requirements.txt" </dev/null )
  }
  spin "dependencies installed" _install_deps \
    || die "could not install the dependencies — see the pip output above."
  # Stamp the requirements hash so the launcher's incremental refresh sees a
  # matching venv and does nothing on the first launch.
  if _h="$(hash_file "$INSTALL_DIR/requirements.txt")"; then
    printf '%s' "$_h" > "$INSTALL_DIR/.venv/.requirements.sha"
  fi
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
    # macOS defaults to zsh, where ~/.bashrc is not read at all. Print the file
    # the operator's shell actually loads rather than a line that does nothing.
    _rc=".bashrc"
    case "${SHELL:-}" in *zsh) _rc=".zshrc" ;; esac
    warn "$BIN_DIR is not on your PATH — add it, then open a new shell:"
    note "echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/$_rc"
    ;;
esac

# --- 4. the credential -------------------------------------------------------

if [[ "${ZIMZILLA_SKIP_SETUP:-0}" == "1" ]]; then
  warn "ZIMZILLA_SKIP_SETUP=1 — not configuring the route"
else
  # Run it even when a profile already exists: an install from before the
  # direct route has a profile pointing at the retired :4001 proxy, and setup.sh
  # is what migrates it. It no longer builds or starts a proxy, so this is a
  # quick, idempotent step.
  #
  # A setup.sh that fails must not fail the install: an upstream that is down or
  # a machine with no network still leaves a working checkout, and the launcher
  # retries on launch. Hence `|| warn` rather than a bare call under `set -e`.
  step "Configuring the Logfare route (direct)"
  "$INSTALL_DIR/setup.sh" </dev/null \
    || warn "setup did not finish — run $INSTALL_DIR/setup.sh to retry"
fi

# --- done --------------------------------------------------------------------

# An install that did not update is not a silent one. The banner is what the
# operator actually reads at the end of a `curl … | bash`, so the state of the
# code is printed here rather than left to be discovered as a missing command.
if [[ -n "${STALE_NOTE:-}" ]]; then
  printf '\n'
  warn "$STALE_NOTE"
  note "fix that, then re-run: $INSTALL_DIR/install.sh"
fi

_code_sha="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
_code_branch="$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"

printf '\n'
bold "╭──────────────────────────────────────────────╮"
bold "│  installed.                                  │"
bold "╰──────────────────────────────────────────────╯"
printf '\n'
printf '    code:  \033[36m%s\033[0m on %s\n' "$_code_sha" "$_code_branch"
printf '\n'
printf '    \033[36mzimzilla\033[0m                 # launch, from anywhere\n'
printf '    \033[36mzimzilla -C ~/proj\033[0m       # run against another directory\n'
printf '    \033[36mzimzilla --mode zim\033[0m      # full auto, follows AGENTS.md\n\n'
printf '  Re-run this installer to update:  %s/install.sh\n' "$INSTALL_DIR"
printf '  It keeps its own venv, so nothing to activate and nothing to pip-install.\n\n'
