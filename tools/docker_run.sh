#!/usr/bin/env bash
# Run the usrp-sim container.
#   tools/docker_run.sh [--tag IMG] [--name NAME] [--config FILE] [--bridge [--publish]] [-d] [-- usrp-sim args]
# Default: --network host (the sim binds the address in the config, default 127.0.0.1, so a UHD client on the
# host connects with addr=127.0.0.1).  --bridge uses docker's bridge network with SIM_BIND=auto (the container IP):
# connect with addr=<container ip> (docker inspect -f '{{.NetworkSettings.IPAddress}}' NAME); --publish also
# publishes the ports on the host (then use a host-bound config, as UHD matches the client address against the
# device's EEPROM IPs, so --bridge without a client inside the same bridge usually needs --network host instead).
# --config FILE mounts FILE as /config/sim.toml (read-only); without it a default is generated in the
# `usrp-sim-config` volume.  Ctrl-C (foreground) or `docker stop NAME` shuts the sim down cleanly.
set -euo pipefail
DOCKER=${DOCKER:-docker}; TAG=usrp-sim:latest; NAME=usrp-sim; CONFIG=""; NET=(--network host); BRIDGE=0; PUBLISH=0; DETACH=()
while [ $# -gt 0 ]; do case "$1" in
  --tag) TAG="$2"; shift 2;;
  --name) NAME="$2"; shift 2;;
  --config) CONFIG="$(realpath "$2")"; shift 2;;
  --bridge) BRIDGE=1; NET=(); shift;;
  --publish) PUBLISH=1; shift;;
  -d) DETACH=(-d); shift;;
  -h|--help) sed -n '2,13p' "$0"; exit 0;;
  --) shift; break;;
  *) echo "unknown option $1 (use -- before usrp-sim args)" >&2; exit 2;; esac; done
ARGS=(--rm --init --name "$NAME" "${DETACH[@]}" "${NET[@]}" -v usrp-sim-state:/var/lib/usrp-sim)
if [ -n "$CONFIG" ]; then ARGS+=(-v "$CONFIG":/config/sim.toml:ro); else ARGS+=(-v usrp-sim-config:/config); fi
if [ $BRIDGE = 1 ]; then
  ARGS+=(-e SIM_BIND=auto)
  [ $PUBLISH = 1 ] && ARGS+=(-p 49152-49153:49152-49153/udp -p 49158:49158/udp -p 49600:49600/udp -p 49601:49601/tcp)
fi
exec "$DOCKER" run "${ARGS[@]}" "$TAG" "$@"
