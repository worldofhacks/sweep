#!/bin/sh
# Retired compatibility entrypoint: reports the canonical launcher without process changes.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/stack.py" "${1:-start}"
