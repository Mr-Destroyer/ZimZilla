#!/usr/bin/env bash
# run.sh — run ZimZilla straight from the checkout, without installing.
#
# The real launcher lives in packaging/ because that is what install.sh puts on
# PATH; it needs ZIMZILLA_ROOT to point at the checkout (its own directory is
# packaging/, which has no venv). This shim does exactly that, so `./run.sh`
# here and `zimzilla` on PATH behave identically.
#
# (The repo root cannot be named `zimzilla` — that name belongs to the Python
# package directory — hence run.sh.)
#
# See packaging/zimzilla for the configuration knobs.

set -euo pipefail

_self_dir="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"
export ZIMZILLA_ROOT="$_self_dir"
exec "$_self_dir/packaging/zimzilla" "$@"
