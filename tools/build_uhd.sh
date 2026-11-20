#!/usr/bin/env bash
# Build a stock UHD client (Python API on, NO ENABLE_SIM) for testing usrp-sim.
# Out-of-tree, never installed, never uses sudo.  Needs (installed by you): cmake, a C++17 compiler,
# Boost, Python dev headers.  Python packages go into ../.venv only.
#
#   tools/build_uhd.sh [--build-dir DIR] [--jobs N] [--configure-only] [--no-e320] [--minimal]
#
# Result: DIR (default ../build/uhd); use it with `source ../uhd-env.sh`.
# E320 is ON by default: the Tier 2 (MPMD "custom") profile needs e320_radio_control in libuhd.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"          # tools/usrp_sim
SRC="$(cd "$HERE/../../host" && pwd)"              # <repo>/host
BUILD="$HERE/build/uhd"; JOBS="$(nproc)"; CONF_ONLY=0; E320=ON; EXAMPLES=ON
while [ $# -gt 0 ]; do case "$1" in
  --build-dir) BUILD="$(realpath -m "$2")"; shift 2;;
  --jobs) JOBS="$2"; shift 2;;
  --configure-only) CONF_ONLY=1; shift;;
  --no-e320) E320=OFF; shift;;
  --minimal) EXAMPLES=OFF; shift;;
  -h|--help) sed -n '2,11p' "$0"; exit 0;;
  *) echo "unknown option $1" >&2; exit 2;; esac; done

for t in cmake make g++; do command -v $t >/dev/null || { echo "missing $t: please install it (no sudo is used here)" >&2; exit 1; }; done
[ -f /usr/include/boost/version.hpp ] || echo "warning: Boost headers not found in /usr/include" >&2
[ -x "$HERE/.venv/bin/python" ] || "$HERE/setup.sh"
PY="$HERE/.venv/bin/python"
"$PY" -m pip install -q mako requests ruamel.yaml pybind11 setuptools numpy

cmake -S "$SRC" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_PYTHON_API=ON -DPYTHON_EXECUTABLE="$PY" \
  -DENABLE_SIM=OFF \
  -DENABLE_X300=ON -DENABLE_MPMD=ON -DENABLE_B200=OFF -DENABLE_USB=OFF -DENABLE_E320=$E320 \
  -DENABLE_E300=OFF -DENABLE_N300=OFF -DENABLE_N320=OFF -DENABLE_X400=OFF -DENABLE_OCTOCLOCK=OFF \
  -DENABLE_USRP1=OFF -DENABLE_USRP2=OFF -DENABLE_B100=OFF \
  -DENABLE_RFNOC=ON -DENABLE_UTILS=ON -DENABLE_EXAMPLES=$EXAMPLES \
  -DENABLE_TESTS=OFF -DENABLE_MANUAL=OFF -DENABLE_DOXYGEN=OFF -DENABLE_MAN_PAGES=OFF -DENABLE_STATIC_LIBS=OFF \
  -DENABLE_QEMU_UNITTESTS=OFF -DENABLE_DPDK=OFF -DENABLE_LIBURING=OFF -DENABLE_C_API=OFF
[ $CONF_ONLY = 1 ] && { echo "configured: $BUILD"; exit 0; }
make -C "$BUILD" -j"$JOBS"
echo "built: $BUILD   ->  source $HERE/uhd-env.sh   (USRP_SIM_UHD_BUILD=$BUILD if not the default)"
