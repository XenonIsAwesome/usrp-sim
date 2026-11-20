#!/usr/bin/env bash
# Run the usrp-sim test suite with the project venv.
#   tools/run_tests.sh [--unit | --e2e | --tier2 | --measure | --all] [pytest args...]
# --unit    fast tests, no UHD client needed (codec, models, delays, scheduler, config, MPMD RPC)
# --e2e     Tier 1 end-to-end against a stock UHD client      --tier2   Tier 2 (MPMD) end-to-end
# --measure tools/measure_delays.py self-test (~1.5 min)      --all     everything (default, ~5 min)
# The UHD client is found automatically: $USRP_SIM_UHD_BUILD, ./build/uhd, or an installed UHD
# (build one with tools/build_uhd.sh).  Without a client the e2e tests are skipped with a message.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || { echo "no venv: run $HERE/setup.sh first" >&2; exit 1; }
cd "$HERE"
SEL=(tests)
case "${1:-}" in
  --unit)    SEL=(tests/test_chdr.py tests/test_delays.py tests/test_scheduler_clock.py tests/test_config.py tests/test_models.py tests/test_mpmd_rpc.py "tests/test_measure.py::test_dry_run_touches_nothing_and_lists_plan" "tests/test_measure.py::test_allow_tx_off_by_default_and_event_validation"); shift;;
  --e2e)     SEL=(tests/test_e2e.py); shift;;
  --tier2)   SEL=(tests/test_e2e_mpmd.py); shift;;
  --measure) SEL=(tests/test_measure.py); shift;;
  --all)     shift;;
esac
exec "$PY" -m pytest "${SEL[@]}" -q "$@"
