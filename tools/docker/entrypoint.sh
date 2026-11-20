#!/bin/sh
# Container entrypoint.  Config: /config/sim.toml (generated on first start if missing).
# SIM_BIND overrides [device] bind: an IPv4 address, or "auto" = this container's IP (bridge networking).
# Extra arguments are passed to usrp-sim.
set -e
CONF=${SIM_CONFIG:-/config/sim.toml}
if [ ! -f "$CONF" ]; then
  python -m usrp_sim.cli --print-example-config \
    | sed 's#^state_dir = .*#state_dir = "/var/lib/usrp-sim"#' > "$CONF"
  echo "generated default config $CONF"
fi
BIND_ARG=""
case "${SIM_BIND:-}" in
  "") ;;
  auto) BIND_ARG="--bind $(hostname -i | awk '{print $1}')";;
  *) BIND_ARG="--bind $SIM_BIND";;
esac
# shellcheck disable=SC2086
exec python -m usrp_sim.cli --config "$CONF" $BIND_ARG --stats-interval "${SIM_STATS_INTERVAL:-60}" "$@"
