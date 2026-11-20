"""Shared fixtures: simulator subprocess launcher and UHD client discovery.

End-to-end tests need a stock UHD client (installed, or the in-tree build under
tools/usrp_sim/build/uhd).  Without one they are skipped with a clear message.
"""
import itertools
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def uhd_env():
    """Environment (dict) for running UHD client tools, or None if no client is available."""
    env = dict(os.environ)
    build = Path(os.environ.get("USRP_SIM_UHD_BUILD", ROOT / "build" / "uhd"))
    if (build / "utils" / "uhd_usrp_probe").exists():
        env["LD_LIBRARY_PATH"] = "%s%s" % (build / "lib", (":" + env["LD_LIBRARY_PATH"]) if env.get("LD_LIBRARY_PATH") else "")
        env["PATH"] = "%s:%s:%s" % (build / "utils", build / "examples", env["PATH"])
        env["PYTHONPATH"] = "%s%s" % (build / "python", (":" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
        env["UHD_PKG_PATH"] = str(build)
        env["UHD_LOG_CONSOLE_LEVEL"] = "off"
        env["_UHD_CLIENT"] = "local build %s" % build
        return env
    if shutil.which("uhd_usrp_probe"):
        env["_UHD_CLIENT"] = "installed UHD"
        return env
    return None


UHD_ENV = uhd_env()
needs_uhd = pytest.mark.skipif(
    UHD_ENV is None,
    reason="no stock UHD client found: install UHD or build it in tools/usrp_sim/build/uhd (see README)")

_ip_counter = itertools.count(20)


class SimProcess:
    def __init__(self, cfg_text: str, tmp: Path, bind: str, name="sim"):
        self.bind, self.tmp = bind, tmp
        cfg_text = textwrap.dedent(cfg_text)
        if "[device]" in cfg_text:
            cfg_text = cfg_text.replace("[device]", '[device]\nbind = "%s"\nstate_dir = "%s"' % (bind, tmp / "state"), 1)
        else:
            cfg_text = '[device]\nbind = "%s"\nstate_dir = "%s"\n' % (bind, tmp / "state") + cfg_text
        self.cfg_path = tmp / (name + ".toml")
        self.cfg_path.write_text(cfg_text)
        self.log_path = tmp / (name + ".log")
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen([sys.executable, "-m", "usrp_sim.cli", "-c", str(self.cfg_path), "-v"],
                                     cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        self.wait_ready()

    def wait_ready(self, timeout=10.0):
        end = time.time() + timeout
        mpmd = 'profile = "custom"' in self.cfg_path.read_text()
        req = b"MPM-DISC" if mpmd else struct.pack(">IIII", 1, 0x1234, 0, 0)
        port = 49600 if mpmd else 49152
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError("simulator died:\n" + self.log_path.read_text())
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            try:
                s.sendto(req, (self.bind, port))
                resp = s.recvfrom(512)[0]
                if resp.startswith(b"USRP-MPM") or resp == req:
                    return
            except OSError:
                pass
            finally:
                s.close()
            time.sleep(0.1)
        raise RuntimeError("simulator did not start:\n" + self.log_path.read_text())

    @property
    def args(self):
        return "type=x300,addr=%s" % self.bind

    def stop(self, sig=signal.SIGINT):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()
        return self.proc.returncode

    def logtext(self):
        return self.log_path.read_text()


@pytest.fixture
def start_sim(tmp_path):
    sims = []

    def _start(cfg_text="", bind=None):
        bind = bind or "127.0.0.%d" % next(_ip_counter)
        s = SimProcess(cfg_text, tmp_path, bind, name="sim%d" % len(sims))
        sims.append(s)
        return s
    yield _start
    for s in sims:
        s.stop()


def run_client(cmd, timeout=120, **kw):
    return subprocess.run(cmd, env=UHD_ENV, capture_output=True, text=True, timeout=timeout, **kw)


def ramp_bytes(n, start=0):
    import numpy as np
    k = np.arange(start, start + n)
    a = np.empty(2 * n, dtype="<u2")
    a[0::2] = k & 0xFFFF
    a[1::2] = (~k) & 0xFFFF
    return a.tobytes()
