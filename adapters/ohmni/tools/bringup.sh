#!/bin/sh
# Host stack entrypoint. Configuration is private; see README for its explicit inputs.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/stack.py" "${1:-start}"
