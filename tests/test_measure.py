"""tools/measure_delays.py against the simulator: measured medians match the configured
delays, and feeding the CSV back through the `table` model reproduces them (section 4a)."""
import statistics
import subprocess
import sys
import textwrap

import pytest

from conftest import ROOT, UHD_ENV, needs_uhd
from usrp_sim import delay_events as de

SCRIPT = ROOT / "tools" / "measure_delays.py"
CONFIGURED = {"lo_retune": 60, "gain_change": 12, "antenna": 18, "dsp_rate": 25, "stream_start": 30,
              "time_source": 15, "lmk_reprogram": 300}

SPEC = textwrap.dedent("""
    rx_freq = [1.0e9, 1.001e9, 2.4e9]
    gain = [0.0, 20.0]
    antenna_rx = ["TX/RX", "RX2"]
    rate = [1e6, 10e6]
    time_source = ["internal", "external"]
    master_clock_rate = [200e6, 184.32e6]
""")
EVENTS = "lo_retune,gain_change,antenna,dsp_rate,stream_start,time_source,device_init,lmk_reprogram"


def run_script(args, out, spec, baseline=0.0, extra=()):
    cmd = [sys.executable, str(SCRIPT), "--args", args, "--events", EVENTS, "--spec", str(spec), "--repeat", "3",
           "--warmup", "1", "--max-pairs", "3", "--seed", "1", "--lmk-baseline-ms", str(baseline),
           "--output", str(out), *extra]
    r = subprocess.run(cmd, env=UHD_ENV, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    return r.stdout


def med(rows, ev):
    return statistics.median(r.ms for r in rows if r.event == ev)


def test_dry_run_touches_nothing_and_lists_plan(tmp_path):
    spec = tmp_path / "s.toml"
    spec.write_text(SPEC)
    r = subprocess.run([sys.executable, str(SCRIPT), "--dry-run", "--spec", str(spec), "--events", EVENTS,
                        "--output", str(tmp_path / "x.csv")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "DRY RUN" in r.stdout and "lo_retune" in r.stdout and "device_init" in r.stdout
    assert not (tmp_path / "x.csv").exists()


def test_allow_tx_off_by_default_and_event_validation():
    r = subprocess.run([sys.executable, str(SCRIPT), "--dry-run"], capture_output=True, text=True, timeout=30)
    assert "receive-only" in r.stdout
    assert "tx:0" not in r.stdout
    r = subprocess.run([sys.executable, str(SCRIPT), "--dry-run", "--events", "dac_sync"], capture_output=True, text=True)
    assert r.returncode != 0 and "cannot be measured" in r.stderr
    r = subprocess.run([sys.executable, str(SCRIPT), "--dry-run", "--allow-tx"], capture_output=True, text=True)
    assert "tx:0" in r.stdout


@needs_uhd
def test_measured_medians_match_configured_delays_and_table_replay_reproduces(start_sim, tmp_path):
    spec = tmp_path / "spec.toml"
    spec.write_text(SPEC)
    # 1. client-side init overhead with every delay off (needed to turn device_init into lmk_reprogram)
    zero = start_sim("[device]\n[delays]\ntime_scale = 0\n")
    run_script(zero.args, tmp_path / "zero.csv", spec)
    zrows = de.read_csv(str(tmp_path / "zero.csv"))
    baseline = med(zrows, "device_init")
    zero.stop()
    # 2. measure the delayed sim
    lines = "\n".join("%s = %s" % (k, v) for k, v in CONFIGURED.items())
    sim = start_sim("[device]\n[delays]\ntime_scale = 1\n[delays.fixed_ms]\n" + lines + "\n")
    out1 = run_script(sim.args, tmp_path / "m1.csv", spec, baseline)
    rows1 = de.read_csv(str(tmp_path / "m1.csv"))
    assert "summary" in out1
    for ev, want in CONFIGURED.items():
        got = med(rows1, ev)
        assert want - 3 <= got <= want * 1.25 + 12, (ev, want, got)
    # CSV has the documented schema and the statistics columns
    assert open(tmp_path / "m1.csv").readline().strip().split(",") == de.CSV_FIELDS
    assert all(r.n == 3 and r.max_ms >= r.ms >= r.min_ms for r in rows1)
    sim.stop()
    # 3. replay: table model fed with the measured CSV, then measure again
    sim2 = start_sim('[device]\n[delays]\nmodel = "table"\ntable = "%s"\ntime_scale = 1\n' % (tmp_path / "m1.csv"))
    run_script(sim2.args, tmp_path / "m2.csv", spec, baseline)
    rows2 = de.read_csv(str(tmp_path / "m2.csv"))
    for ev in CONFIGURED:
        a, b = med(rows1, ev), med(rows2, ev)
        assert abs(a - b) <= 0.2 * a + 12, (ev, a, b)


@needs_uhd
def test_resume_skips_finished_transitions_and_ctrl_c_safe_csv(start_sim, tmp_path):
    spec = tmp_path / "spec.toml"
    spec.write_text("gain = [0.0, 20.0]\n")
    sim = start_sim("[device]\n[delays]\ntime_scale = 0\n")
    out = tmp_path / "r.csv"
    cmd = [sys.executable, str(SCRIPT), "--args", sim.args, "--events", "gain_change", "--spec", str(spec),
           "--repeat", "2", "--warmup", "0", "--output", str(out)]
    r1 = subprocess.run(cmd, env=UHD_ENV, capture_output=True, text=True, timeout=300)
    assert r1.returncode == 0, r1.stderr
    n1 = len(de.read_csv(str(out)))
    assert n1 == 2                                              # 0->20 and 20->0
    r2 = subprocess.run(cmd + ["--resume"], env=UHD_ENV, capture_output=True, text=True, timeout=300)
    assert r2.returncode == 0 and len(de.read_csv(str(out))) == n1     # nothing re-measured
    r3 = subprocess.run(cmd, env=UHD_ENV, capture_output=True, text=True, timeout=300)
    assert r3.returncode != 0 and "--resume" in (r3.stdout + r3.stderr)   # refuses to clobber an existing CSV
