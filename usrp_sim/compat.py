"""fw/FPGA compatibility numbers of the UHD client.

The X300 driver refuses to talk to a device whose compat numbers differ from the ones it
was built with.  This checkout expects fw 6.1 / FPGA 39.0 (x300_fw_common.h).  For other
UHD versions `probe_main()` asks the installed client: it starts a scratch simulator with
deliberately wrong numbers and parses the "Expected ... compatibility number" error.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

KNOWN = {"4.6": ("6.1", "39.0")}      # verified against host/lib/usrp/x300/x300_fw_common.h


def for_version(v: str):
    key = ".".join(v.split(".")[:2])
    if key in KNOWN:
        return KNOWN[key]
    raise ValueError("no verified compat numbers for UHD %s; run `usrp-sim --probe-compat` "
                     "to read them from the installed client" % v)


def _probe_once(probe_bin: str, fw: str, fpga: str, ip: str, env) -> str:
    import tomllib
    from . import config as cfgmod
    from .devices.x300 import X300Device
    from .frontends.x300_udp import X300Server
    cfg = cfgmod.parse(tomllib.loads(cfgmod.EXAMPLE_CONFIG))
    cfg.bind, cfg.fw_compat, cfg.fpga_compat = ip, fw, fpga
    cfg.delays["time_scale"] = 0.0
    cfg.state_dir = tempfile.mkdtemp(prefix="usrp_sim_probe_")
    dev = X300Device(cfg)
    srv = X300Server(dev)
    srv.start()
    try:
        r = subprocess.run([probe_bin, "--args", "type=x300,addr=%s" % ip], capture_output=True,
                           text=True, timeout=60, env=env)
        return r.stdout + r.stderr
    finally:
        srv.close()
        dev.close()


def probe_main(uhd_bin=None) -> int:
    probe = shutil.which("uhd_usrp_probe", path=uhd_bin) if uhd_bin else shutil.which("uhd_usrp_probe")
    if not probe:
        print("uhd_usrp_probe not found (use --uhd-bin DIR)", file=sys.stderr)
        return 1
    env = dict(os.environ)
    ip = "127.0.0.250"
    out = _probe_once(probe, "6.1", "0.0", ip, env)
    m = re.search(r"Expected FPGA compatibility number (\d+)\.(\d+)", out)
    fpga = "%s.%s" % (m.group(1), m.group(2)) if m else None
    out = _probe_once(probe, "0.0", fpga or "39.0", ip, env)
    m = re.search(r"Expected firmware compatibility number (\d+)", out)
    fw = "%s.1" % m.group(1) if m else None
    if not (fpga and fw):
        print("could not determine the compat numbers; client output was:\n" + out, file=sys.stderr)
        return 1
    print('fw_compat = "%s"\nfpga_compat = "%s"' % (fw, fpga))
    return 0
