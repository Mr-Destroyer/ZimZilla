#!/usr/bin/env bash
# start-litellm.sh — run the LiteLLM translation proxy for ZimZilla.
#
#   ZimZilla (Anthropic /v1/messages) --▶ LiteLLM :4002 --▶ IteraCompute (OpenAI)
#
# Usage:  ./start-litellm.sh [start|stop|restart|status|logs|help]
#
# No secrets live here. The upstream key is read from whatever environment
# variable the config references (api_key: os.environ/<VAR>); that variable must
# be exported by the profile sourced below.

set -euo pipefail

# Self-locating: this script, its config and its profile live together in one
# directory, so a relocated install still finds its siblings.
# Resolve this script's directory without `readlink -f`: GNU's -f is not on
# macOS (BSD readlink has no -f), so symlinks are followed by hand.
_self_path="${BASH_SOURCE[0]}"
while [[ -L "$_self_path" ]]; do
  _self_link_dir="$(cd -- "$(dirname "$_self_path")" && pwd -P)"
  _self_path="$(readlink "$_self_path")"
  [[ "$_self_path" == /* ]] || _self_path="$_self_link_dir/$_self_path"
done
_self_dir="$(cd -- "$(dirname "$_self_path")" && pwd -P)"

# --------------------------- configurable ---------------------------
ENV_FILE="${LITELLM_ENV_FILE:-$_self_dir/source}"
CONFIG="${LITELLM_CONFIG:-$_self_dir/litellm-config.yaml}"
PORT="${LITELLM_PORT:-4002}"
PID_FILE="${LITELLM_PID_FILE:-/tmp/litellm-itera.pid}"
# Bind loopback by default. litellm's own default is 0.0.0.0, which on a laptop
# means every other machine on the network can reach the proxy and spend the
# IteraCompute key. The harness only ever talks to it over localhost (the profile's
# ANTHROPIC_BASE_URL is http://localhost:$PORT), so nothing needs the wider
# bind. Override with LITELLM_HOST if a container or VM genuinely needs it.
HOST="${LITELLM_HOST:-127.0.0.1}"

# NOTE: litellm reads the env var LITELLM_LOG as its *log level* ("INFO",
# "DEBUG"), so it must never carry a file path — doing so crashes it at import
# with "module 'logging' has no attribute '/tmp/...'". The log-file path lives
# in LITELLM_LOGFILE. A path-valued LITELLM_LOG is still accepted for
# backwards compatibility, but is stripped before litellm is launched.
LOG="${LITELLM_LOGFILE:-/tmp/litellm-itera.log}"
if [[ "${LITELLM_LOG:-}" == */* ]]; then
  LOG="$LITELLM_LOG"
  unset LITELLM_LOG
fi

# litellm may live in a venv rather than on PATH; try PATH first, then the usual
# install spots, then the profile's own venv, then give up loudly below.
if [[ -z "${LITELLM_BIN:-}" ]]; then
  LITELLM_BIN="$(command -v litellm 2>/dev/null || true)"
fi
if [[ -z "$LITELLM_BIN" ]]; then
  for _c in "$HOME/.local/bin/litellm" \
            "${ZIMZILLA_HOME:-$HOME/.zimzilla}/venv/bin/litellm" \
            "$HOME/.zimzilla/venv/bin/litellm" "$HOME/venv/bin/litellm"; do
    [[ -x "$_c" ]] && LITELLM_BIN="$_c" && break
  done
fi
LITELLM_BIN="${LITELLM_BIN:-litellm}"
# --------------------------------------------------------------------

log() { printf '\033[36m[liteLLM]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[liteLLM]\033[0m %s\n' "$*" >&2; }

# A pidfile is only a number, and pids get reused. Confirm the recorded pid is
# actually a litellm serving THIS config before believing it — otherwise a stale
# file makes do_start report a false "already running" and leaves the route
# pointing at a port nothing is answering on.
pid_is_our_proxy() {
  local pid="$1" args=""
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if [[ -r "/proc/$pid/cmdline" ]]; then
    args="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  else
    args="$(ps -o args= -p "$pid" 2>/dev/null || true)"
  fi
  [[ "$args" == *litellm* && "$args" == *"$CONFIG"* ]]
}

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  pid_is_our_proxy "$(cat "$PID_FILE" 2>/dev/null || true)"
}

port_answers() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:$PORT/health/liveliness" --max-time 2 || true)"
  [[ "$code" == "200" ]]
}

# Pids LISTENing on $PORT. `sport = :$PORT` matches the local port rather than a
# peer port that merely mentions the number. ss is Linux-only and lsof is not
# installed everywhere, so try both: lsof is what macOS ships, ss is what a bare
# Linux box usually has. Empty when nothing is listening.
port_owner_pids() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnpH "sport = :$PORT" 2>/dev/null \
      | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | sort -u
  fi
}

# Kill whatever litellm holds $PORT, when it is not one this script tracks.
#
# This exists because matching on our own --config path is not enough. A proxy
# left behind by an *older* layout was started with a different --config, so the
# exact-config pkill in do_stop can never see it: the port stays claimed, no pid
# file explains why, and do_start then refuses to start forever. Matching on the
# port plus "is a litellm" reclaims those without guessing at a path that has
# changed since.
#
# Both conditions must hold before anything is signalled — it must be a litellm,
# and it must hold this port — so an unrelated service on :$PORT is never a
# target. Takes an optional signal name, defaulting to TERM.
kill_port_orphans() {
  local sig="${1:-TERM}" pid args
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    args="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null \
            || ps -o args= -p "$pid" 2>/dev/null || true)"
    [[ "$args" == *litellm* ]] || continue
    log "sending SIG$sig to stray litellm on :$PORT (pid $pid)"
    kill "-$sig" "$pid" 2>/dev/null || true
  done < <(port_owner_pids)
}

do_stop() {
  if is_running; then
    local pid; pid="$(cat "$PID_FILE")"
    log "stopping proxy (pid $pid)..."
    kill "$pid" 2>/dev/null || true
    local i
    for ((i = 1; i <= 20; i++)); do
      if ! kill -0 "$pid" 2>/dev/null; then break; fi
      sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
      log "force-killing pid $pid"
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    log "stopped."
  else
    rm -f "$PID_FILE"
    if ! port_answers; then
      log "not tracked as running."
      return 0
    fi
    log "no pid file, but something is still serving :$PORT — reclaiming it"
    # Precise case first: a proxy of ours whose cmdline names this exact config.
    pkill -f "litellm --config $CONFIG" 2>/dev/null || true
    # Then the general one, which reaches a stray from an older layout.
    kill_port_orphans TERM
    local w
    for ((w = 1; w <= 20; w++)); do
      port_answers || break
      sleep 0.25
    done
    if port_answers; then
      kill_port_orphans KILL
      for ((w = 1; w <= 8; w++)); do
        port_answers || break
        sleep 0.25
      done
    fi
    if port_answers; then
      err "something is still serving :$PORT and it is not a litellm I can identify."
      err "find it with:  lsof -nP -iTCP:$PORT -sTCP:LISTEN"
      return 1
    fi
    log "port :$PORT freed."
  fi
}

do_start() {
  if is_running; then
    log "already running (pid $(cat "$PID_FILE")) on :$PORT."
    return 0
  fi
  if port_answers; then
    err "port :$PORT is already answering but no pid file exists."
    err "run '$0 restart' (or stop any stray 'litellm --config' process) first."
    exit 1
  fi
  command -v "$LITELLM_BIN" >/dev/null 2>&1 || [[ -x "$LITELLM_BIN" ]] \
    || { err "litellm not found at '$LITELLM_BIN'. Install: pip install 'litellm[proxy]'"; exit 1; }
  [[ -f "$CONFIG" ]]      || { err "config not found: $CONFIG"; exit 1; }
  [[ -f "$ENV_FILE" ]]    || { err "profile not found: $ENV_FILE — run ./setup.sh first"; exit 1; }

  # Load the profile and export everything it defines (no key is echoed).
  set -a; . "$ENV_FILE"; set +a

  # Verify every os.environ/<VAR> referenced by the config is now set.
  local missing=0 var
  while IFS= read -r var; do
    [[ -z "$var" ]] && continue
    if [[ -z "${!var:-}" ]]; then
      err "required env var '$var' (referenced by config) is unset after sourcing $ENV_FILE"
      missing=1
    fi
  done < <(grep -oE 'os\.environ/[A-Za-z_][A-Za-z0-9_]*' "$CONFIG" 2>/dev/null | sed 's|os\.environ/||' | sort -u)
  if [[ "$missing" -ne 0 ]]; then exit 1; fi

  log "starting proxy on $HOST:$PORT  (config: $CONFIG)"
  log "log: $LOG"
  : > "$LOG"
  nohup "$LITELLM_BIN" --config "$CONFIG" --port "$PORT" --host "$HOST" >>"$LOG" 2>&1 </dev/null &
  echo $! > "$PID_FILE"
  disown 2>/dev/null || true

  local i
  for ((i = 1; i <= 180; i++)); do
    if port_answers; then
      log "up and healthy (pid $(cat "$PID_FILE")) ✓"
      return 0
    fi
    sleep 1
  done
  err "proxy did not become healthy within 180s — last log lines:"
  tail -n 20 "$LOG" >&2 || true
  exit 1
}

do_status() {
  if is_running; then
    log "running (pid $(cat "$PID_FILE")) on :$PORT"
    curl -s "http://127.0.0.1:$PORT/health/liveliness" -w ' | HTTP %{http_code}\n' --max-time 3 || echo
  else
    log "not running."
  fi
}

do_help() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; do_start ;;
  status)  do_status ;;
  logs)    tail -f "$LOG" ;;
  help|-h|--help) do_help ;;
  *) err "usage: $0 [start|stop|restart|status|logs|help]"; exit 2 ;;
esac
