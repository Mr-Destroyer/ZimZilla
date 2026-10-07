#!/usr/bin/env bash
# run.sh — run ZimZilla straight from the checkout.
#
# The real launcher lives in packaging/; it needs ZIMZILLA_ROOT to point at the
# checkout (its own directory is packaging/). This shim does exactly that, so
# `./run.sh` starts the proxy and the harness for you. The launcher then uses
# the checkout's own venv (./.venv) when it exists, so there is nothing to
# activate:
#
#   python3 -m venv .venv && pip install -r requirements.txt
#   ./setup.sh                          # verify the route end-to-end
#   ./run.sh                            # launch
#
# If you installed with install.sh, a `zimzilla` command is already on your PATH
# and this shim is not needed.
#
# (The repo root cannot be named `zimzilla` — that name belongs to the Python
# package directory — hence run.sh.)
#
# See packaging/zimzilla for the configuration knobs.

set -euo pipefail

_self_dir="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)"
export ZIMZILLA_ROOT="$_self_dir"
exec "$_self_dir/packaging/zimzilla" "$@"
