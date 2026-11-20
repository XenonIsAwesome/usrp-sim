"""usrp-sim command line interface."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time

from . import __version__, config as cfgmod


def build_parser():
    p = argparse.ArgumentParser(prog="usrp-sim", description="Offline USRP simulator for stock UHD")
    p.add_argument("-c", "--config", help="TOML configuration file")
    p.add_argument("--print-example-config", action="store_true", help="print an example config and exit")
    p.add_argument("--check", action="store_true", help="validate the configuration and exit")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v = debug, -vv = trace-ish")
    p.add_argument("--stats-interval", type=float, default=10.0, help="seconds between counter logs (0 = off)")
    p.add_argument("--bind", help="override [device] bind")
    p.add_argument("--time-scale", type=float, help="override [delays] time_scale")
    p.add_argument("--uhd-version", metavar="X.Y.Z", help="select the fw/fpga compat numbers known for a UHD version")
    p.add_argument("--probe-compat", action="store_true",
                   help="ask the installed UHD client which compat numbers it expects and print them")
    p.add_argument("--uhd-bin", help="directory with uhd_usrp_probe (for --probe-compat)")
    p.add_argument("--version", action="version", version="usrp-sim " + __version__)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=5 if args.verbose > 1 else (logging.DEBUG if args.verbose else logging.INFO),
                        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    if args.print_example_config:
        sys.stdout.write(cfgmod.EXAMPLE_CONFIG)
        return 0
    if args.probe_compat:
        from . import compat
        return compat.probe_main(args.uhd_bin)
    try:
        if args.config:
            cfg = cfgmod.load(args.config)
        else:
            import tomllib
            cfg = cfgmod.parse(tomllib.loads(cfgmod.EXAMPLE_CONFIG))
            logging.info("no --config given: using the built-in example configuration")
        if args.bind:
            cfg.bind = args.bind
        if args.time_scale is not None:
            cfg.delays["time_scale"] = args.time_scale
        if args.uhd_version:
            from . import compat
            fw, fpga = compat.for_version(args.uhd_version)
            cfg.fw_compat, cfg.fpga_compat = fw, fpga
        if args.check:
            print("configuration OK")
            return 0
    except (cfgmod.ConfigError, ValueError) as e:
        print("usrp-sim: configuration error: %s" % e, file=sys.stderr)
        return 2

    sys.setswitchinterval(0.0005)     # many busy threads: keep control-plane latency low
    try:
        if cfg.profile == "custom":                     # Tier 2: MPMD protocol
            from .devices.custom import ChdrUdp, CustomDevice
            from .frontends.mpmd import MpmdServer
            dev = CustomDevice(cfg)
            chdr_srv = ChdrUdp(dev)
            mpm = MpmdServer(dev, cfg.bind, cfg.claim_timeout_s * 2)

            class _Both:
                def start(self):
                    chdr_srv.start()
                    mpm.start()

                def close(self):
                    mpm.close()
                    chdr_srv.close()
            srv = _Both()
        else:
            from .devices.x300 import X300Device
            from .frontends.x300_udp import X300Server
            dev = X300Device(cfg)
            srv = X300Server(dev)
    except (OSError, ValueError) as e:
        print("usrp-sim: %s" % e, file=sys.stderr)
        return 1
    srv.start()

    stop = threading.Event()
    reload_evt = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGHUP, lambda *a: reload_evt.set())
    last = time.monotonic()
    while not stop.is_set():
        stop.wait(0.2)
        if reload_evt.is_set():
            reload_evt.clear()
            try:
                if args.config:
                    d = cfgmod.load_delays_section(args.config)
                    if args.time_scale is not None:
                        d["time_scale"] = args.time_scale
                    dev.engine.delays.reload(d)
            except Exception as e:
                logging.error("SIGHUP: delay reload failed, keeping the old delay configuration: %s", e)
        if args.stats_interval and time.monotonic() - last >= args.stats_interval:
            last = time.monotonic()
            dev.print_stats()
    logging.info("shutting down")
    srv.close()
    dev.close()
    dev.print_stats(sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
