# Source this file to use the local (unmodified) UHD build as the test client:
#   source tools/usrp_sim/uhd-env.sh
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_B="${USRP_SIM_UHD_BUILD:-$_HERE/build/uhd}"
export LD_LIBRARY_PATH="$_B/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="$_B/utils:$_B/examples:$PATH"
export PYTHONPATH="$_B/python${PYTHONPATH:+:$PYTHONPATH}"
export UHD_PKG_PATH="$_B"
