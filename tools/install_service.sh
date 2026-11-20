#!/usr/bin/env bash
# Install usrp-sim as a systemd service.  Run as root (e.g. `sudo tools/install_service.sh`).
#   install_service.sh [--prefix DIR] [--no-start] [--uninstall [--purge]]
# Installs the program to DIR (default /opt/usrp-sim, own venv), creates the `usrp-sim` system user,
# generates /etc/usrp-sim/sim.toml from `usrp-sim --print-example-config` (never overwritten if it
# exists), installs /etc/systemd/system/usrp-sim.service, enables and starts it.
#   --uninstall   stop/disable and remove the unit and program;  --purge also removes config, state, user.
# Needs: python3 (>= 3.9) with the venv module and network access for pip (numpy, msgpack), or a
# reachable pip mirror.  Edit /etc/usrp-sim/sim.toml, then `systemctl restart usrp-sim`
# (`systemctl reload usrp-sim` re-reads only [delays]).  Logs: journalctl -u usrp-sim -f
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/usrp-sim; START=1; UNINSTALL=0; PURGE=0
while [ $# -gt 0 ]; do case "$1" in
  --prefix) PREFIX="$(realpath -m "$2")"; shift 2;;
  --no-start) START=0; shift;;
  --uninstall) UNINSTALL=1; shift;;
  --purge) PURGE=1; shift;;
  -h|--help) sed -n '2,12p' "$0"; exit 0;;
  *) echo "unknown option $1" >&2; exit 2;; esac; done
[ "$(id -u)" = 0 ] || { echo "must run as root (sudo $0 ...)" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd not found" >&2; exit 1; }
UNIT=/etc/systemd/system/usrp-sim.service; CONF=/etc/usrp-sim/sim.toml

if [ $UNINSTALL = 1 ]; then
  systemctl disable --now usrp-sim 2>/dev/null || true
  rm -f "$UNIT"; systemctl daemon-reload
  [ -f "$PREFIX/bin/usrp-sim" ] && rm -rf "$PREFIX"
  if [ $PURGE = 1 ]; then rm -rf /etc/usrp-sim /var/lib/usrp-sim; id usrp-sim >/dev/null 2>&1 && userdel usrp-sim || true; fi
  echo "usrp-sim removed$([ $PURGE = 1 ] && echo ' (config, state and user purged)' || echo ' (config /etc/usrp-sim and state kept)')"
  exit 0
fi

echo "installing program to $PREFIX"
install -d "$PREFIX"
cp -r "$HERE/usrp_sim" "$HERE/bin" "$HERE/README.md" "$HERE/examples" "$PREFIX/"
find "$PREFIX" -name __pycache__ -prune -exec rm -rf {} +
python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install -q numpy msgpack
"$PREFIX/.venv/bin/python" -c 'import tomllib' 2>/dev/null || "$PREFIX/.venv/bin/pip" install -q tomli

id usrp-sim >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin usrp-sim
install -d -o usrp-sim -g usrp-sim /var/lib/usrp-sim
install -d /etc/usrp-sim
if [ -e "$CONF" ]; then
  echo "keeping existing $CONF"
else
  PYTHONPATH="$PREFIX" "$PREFIX/.venv/bin/python" -m usrp_sim.cli --print-example-config \
    | sed 's#^state_dir = .*#state_dir = "/var/lib/usrp-sim"   \# file-backed EEPROM images (writable by the service)#' > "$CONF"
  chmod 644 "$CONF"; echo "generated $CONF"
fi
PYTHONPATH="$PREFIX" "$PREFIX/.venv/bin/python" -m usrp_sim.cli -c "$CONF" --check

sed "s#@PREFIX@#$PREFIX#g" "$HERE/tools/systemd/usrp-sim.service" > "$UNIT"
systemctl daemon-reload
systemctl enable usrp-sim
if [ $START = 1 ]; then systemctl restart usrp-sim; sleep 1; systemctl --no-pager --lines=5 status usrp-sim || true; fi
echo "done. Config: $CONF   Logs: journalctl -u usrp-sim -f   Client: uhd_usrp_probe --args type=x300,addr=127.0.0.1"
