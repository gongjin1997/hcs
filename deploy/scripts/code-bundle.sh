#!/bin/sh
# Build-machine wrapper. The target host does not need Python or npm.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
exec python3 "$ROOT/scripts/code_bundle.py" "$@"
