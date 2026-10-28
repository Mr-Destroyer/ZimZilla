#!/usr/bin/env bash
# run.sh — run ZimZilla straight from the checkout.
#
# The real launcher lives in packaging/; it needs ZIMZILLA_ROOT to point at the
# checkout (its own directory is packaging/). This shim does exactly that, so
# `./run.sh` starts the harness directly — no local proxy in the default route.
# The launcher then uses the checkout's own venv (./.venv) when it exists, so
# there is nothing to activate:
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

# Resolve this script's directory without `readlink -f`: GNU's -f is not on
# macOS (BSD readlink has no -f), so symlinks are followed by hand.
_self_path="${BASH_SOURCE[0]}"
while [[ -L "$_self_path" ]]; do
  _self_link_dir="$(cd -- "$(dirname "$_self_path")" && pwd -P)"
  _self_path="$(readlink "$_self_path")"
  [[ "$_self_path" == /* ]] || _self_path="$_self_link_dir/$_self_path"
done
_self_dir="$(cd -- "$(dirname "$_self_path")" && pwd -P)"
export ZIMZILLA_ROOT="$_self_dir"
exec "$_self_dir/packaging/zimzilla" "$@"
