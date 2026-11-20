#!/usr/bin/env bash
# One-time setup: creates ./.venv with the Python dependencies (nothing is installed system-wide).
# Optional: --build-uhd [opts] runs tools/build_uhd.sh (local stock UHD client build, no install).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "need Python >= 3.9" >&2; exit 1; }
if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; fi
.venv/bin/pip install -q --upgrade pip >/dev/null 2>&1 || true
# tomli only when the interpreter lacks tomllib (< 3.11)
.venv/bin/pip install -q numpy pytest msgpack
.venv/bin/python -c 'import tomllib' 2>/dev/null || .venv/bin/pip install -q tomli
[ "${1:-}" = "--build-uhd" ] && exec "$HERE/tools/build_uhd.sh" "${@:2}"
echo "ready: $HERE/bin/usrp-sim --print-example-config > sim.toml && $HERE/bin/usrp-sim -c sim.toml"
