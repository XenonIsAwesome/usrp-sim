"""Tier 2 end-to-end: stock UHD <-> usrp-sim over the MPMD protocol (profile = "custom")."""
import json
import subprocess
import sys
import time

import pytest

from conftest import ROOT, UHD_ENV, needs_uhd, ramp_bytes, run_client

pytestmark = needs_uhd
CLIENT = ROOT / "tests" / "client"


def py_client(script, *args, timeout=180):
    r = subprocess.run([sys.executable, "-u", str(CLIENT / script), *map(str, args)], env=UHD_ENV,
                       capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def cfg(extra="", channels=2, ts=0):
    return ('[device]\nprofile = "custom"\n[delays]\ntime_scale = %s\n[custom]\nchannels = %d\n' % (ts, channels)) + extra


def test_custom_rejects_more_channels_than_the_client_radio_supports(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[device]\nprofile = "custom"\n[custom]\nchannels = 4\n')
    r = subprocess.run([sys.executable, "-m", "usrp_sim.cli", "-c", str(p), "--check"], cwd=ROOT, capture_output=True,
                       text=True)
    assert r.returncode == 2 and "1 or 2 channels" in r.stderr


def test_mpmd_discovery_and_probe(start_sim):
    sim = start_sim(cfg('[device]\n') if False else cfg())
    r = run_client(["uhd_find_devices", "--args", "mgmt_addr=" + sim.bind])
    assert "serial: SIM0001" in r.stdout and "type: mpm" in r.stdout
    r = run_client(["uhd_usrp_probe", "--args", "mgmt_addr=" + sim.bind])
    assert r.returncode == 0, r.stdout + r.stderr
    for needle in ("0/Radio#0", "0/DDC#0", "0/DUC#0", "RX Frontend: 1", "TX Frontend: 1", "MPM Version", "Time sources"):
        assert needle in r.stdout, needle


def test_mpmd_rx_file_source_and_tx_sink_bit_identical(start_sim, tmp_path):
    n = 200_000
    src, out = tmp_path / "in.sc16", tmp_path / "tx.sc16"
    src.write_bytes(ramp_bytes(n + 10_000))
    sim = start_sim(cfg('[rx.0]\nsource = "file"\npath = "%s"\n[tx.0]\nsink = "file"\npath = "%s"\n' % (src, out)))
    r = run_client(["rx_samples_to_file", "--args", "mgmt_addr=" + sim.bind, "--rate", "1e6", "--freq", "1e9",
                    "--nsamps", str(n), "--file", str(tmp_path / "rx.sc16"), "--type", "short"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "rx.sc16").read_bytes() == src.read_bytes()[: n * 4]
    res = py_client("tx_exact.py", "mgmt_addr=" + sim.bind, n)
    assert res["sent"] == n
    time.sleep(0.5)
    assert out.read_bytes() == ramp_bytes(n)


def test_mpmd_timed_rx_pps_and_late(start_sim):
    sim = start_sim(cfg('[rx.0]\nsource = "ramp"\n'))
    r = py_client("timed_rx.py", "mgmt_addr=" + sim.bind)
    assert abs(r["pps_dev_frac"] - r["pps_sys_frac"]) < 0.05 and abs(r["last_pps"] - 100.0) < 1e-6
    assert abs(r["first_ts"] - r["requested"]) < 1e-5 and r["late_err"].endswith("late")


def test_mpmd_delays_visible(start_sim):
    sim = start_sim(cfg("", ts=1) + "[delays.fixed_ms]\nlo_retune = 100\ngain_change = 30\n")
    code = ("import uhd,time,json\n"
            "u=uhd.usrp.MultiUSRP('mgmt_addr=%s')\n"
            "def t(f):\n a=time.perf_counter(); f(); return (time.perf_counter()-a)*1e3\n"
            "def tune(f):\n u.set_rx_freq(uhd.types.TuneRequest(f),0)\n"
            " while not u.get_rx_sensor('lo_locked',0).to_bool(): time.sleep(0.001)\n"
            "print(json.dumps({'tune':[t(lambda f=f: tune(f)) for f in (1e9,1.1e9,2.4e9)],"
            "'gain':[t(lambda g=g: u.set_rx_gain(g,0)) for g in (10,20,30)]}))\n" % sim.bind)
    r = subprocess.run([sys.executable, "-c", code], env=UHD_ENV, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert all(95 < x < 140 for x in d["tune"]) and all(28 < x < 60 for x in d["gain"]), d


def test_mpmd_claim_blocks_second_client_and_is_released(start_sim):
    sim = start_sim(cfg())
    code = ("import uhd,time\nu=uhd.usrp.MultiUSRP('mgmt_addr=%s')\nprint('claimed',flush=True)\ntime.sleep(60)\n" % sim.bind)
    p = subprocess.Popen([sys.executable, "-u", "-c", code], env=UHD_ENV, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "claimed"
        r = run_client([sys.executable, "-c", "import uhd; uhd.usrp.MultiUSRP('mgmt_addr=%s')" % sim.bind])
        assert r.returncode != 0                           # claimed by someone else
    finally:
        p.kill()
        p.wait()
    time.sleep(7.0)                                        # claim not refreshed -> expires
    r = run_client([sys.executable, "-c", "import uhd; uhd.usrp.MultiUSRP('mgmt_addr=%s')" % sim.bind])
    assert r.returncode == 0, r.stderr
