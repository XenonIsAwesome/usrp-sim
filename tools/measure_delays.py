#!/usr/bin/env python3
"""Measure how long real configuration changes take on a USRP and write the CSV that the
simulator's ``table`` delay model replays  (see usrp_sim/delay_events.py for the schema).

Everything goes through the UHD Python API (``uhd.usrp.MultiUSRP``) -- no raw register access,
no EEPROM / image / non-volatile writes.  Each timing is wall-clock (perf_counter_ns) from just
before the call until the device is actually ready, like an application would wait:

  lo_retune     set_*_freq() + poll the ``lo_locked`` sensor
  gain_change   set_*_gain()                      antenna     set_*_antenna()
  bandwidth     set_*_bandwidth()                 dsp_rate    set_*_rate()  (DDC/DUC)
  stream_start  issue_stream_cmd() -> first received packet
  time_source / clock_source   set_time_source() / set_clock_source()  (clock_source needs a
                reference signal on real hardware: it only runs when asked for explicitly)
  device_init, lmk_reprogram   re-creating the device with different master_clock_rate args
                (lmk_reprogram = device_init - --lmk-baseline-ms; see the README)

RX only by default.  Nothing transmits RF unless --allow-tx is given (and even then only the TX
LO / gain / rate settings are exercised -- no TX stream is ever started).

Examples
  measure_delays.py --dry-run
  measure_delays.py --args "type=x300,addr=192.168.10.2" --repeat 10 --output measured.csv
  measure_delays.py --args "type=x300,addr=127.0.0.1" --events lo_retune,gain_change --resume
"""
from __future__ import annotations

import argparse
import itertools
import os
import random
import signal
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from usrp_sim import delay_events as de   # noqa: E402  (shared event names / CSV schema)

try:
    import tomllib
except ImportError:      # pragma: no cover
    import tomli as tomllib

DEFAULT_SPEC = {
    "rx_freq": [500e6, 900e6, 1.0e9, 1.001e9, 2.4e9, 3.5e9, 5.8e9],   # near and far steps, band edges
    "tx_freq": [900e6, 1.0e9, 2.4e9, 5.8e9],
    "gain": [0.0, 10.0, 20.0, 31.5],
    "antenna_rx": ["TX/RX", "RX2"],
    "antenna_tx": ["TX/RX", "CAL"],
    "rate": [1e6, 5e6, 10e6, 25e6, 50e6, 100e6],
    "bandwidth": [],                     # UBX bandwidth is fixed; list values for boards that support it
    "time_source": ["internal", "external"],
    "clock_source": [],                  # only when requested: needs a reference on real hardware
    "master_clock_rate": [200e6, 184.32e6],
}
DEFAULT_EVENTS = ["lo_retune", "gain_change", "antenna", "dsp_rate", "stream_start", "time_source", "device_init"]
ALL_MEASURABLE = DEFAULT_EVENTS + ["bandwidth", "clock_source", "lmk_reprogram"]
NOT_MEASURABLE = ["adc_init", "dac_init", "dac_sync", "reset", "reg_access", "ctrl_access"]
TX_EVENTS = {"lo_retune": "tx", "gain_change": "tx", "antenna": "tx", "dsp_rate": "tx", "bandwidth": "tx"}

stop_requested = False


def _sigint(sig, frm):
    global stop_requested
    if stop_requested:
        raise KeyboardInterrupt
    stop_requested = True
    print("\n[Ctrl-C] finishing the current measurement, then stopping (CSV is already saved) ...", flush=True)


def ms_since(t0_ns: int) -> float:
    return (time.perf_counter_ns() - t0_ns) / 1e6


def pairs(values, max_pairs, rng):
    allp = [(a, b) for a, b in itertools.permutations(values, 2)]
    if max_pairs and len(allp) > max_pairs:
        allp = rng.sample(allp, max_pairs)
        allp.sort(key=lambda p: (values.index(p[0]), values.index(p[1])))
    return allp


class Plan:
    """A list of transitions to measure: (event, key, from, to, callable(measure_once))."""

    def __init__(self):
        self.items = []

    def add(self, event, key, frm, to, fn):
        self.items.append((event, key, frm, to, fn))


# ------------------------------------------------------------------ real measurements
class Measurer:
    def __init__(self, usrp, uhd, args):
        self.u, self.uhd, self.args = usrp, uhd, args

    def wait_lock(self, direction, ch, timeout=5.0):
        end = time.perf_counter() + timeout
        get = self.u.get_rx_sensor if direction == "rx" else self.u.get_tx_sensor
        names = (self.u.get_rx_sensor_names if direction == "rx" else self.u.get_tx_sensor_names)(ch)
        if "lo_locked" not in names:
            return True
        while not get("lo_locked", ch).to_bool():
            if time.perf_counter() > end:
                raise RuntimeError("lo_locked did not assert within %.0f s" % timeout)
            time.sleep(0.0002)
        return True

    def lo_retune(self, d, ch, f_from, f_to):
        tune = self.u.set_rx_freq if d == "rx" else self.u.set_tx_freq
        tune(self.uhd.types.TuneRequest(f_from), ch)
        self.wait_lock(d, ch)
        time.sleep(0.02)
        t0 = time.perf_counter_ns()
        tune(self.uhd.types.TuneRequest(f_to), ch)
        self.wait_lock(d, ch)
        return ms_since(t0)

    def gain(self, d, ch, g_from, g_to):
        s = self.u.set_rx_gain if d == "rx" else self.u.set_tx_gain
        s(g_from, ch)
        t0 = time.perf_counter_ns()
        s(g_to, ch)
        return ms_since(t0)

    def antenna(self, d, ch, a_from, a_to):
        s = self.u.set_rx_antenna if d == "rx" else self.u.set_tx_antenna
        s(a_from, ch)
        t0 = time.perf_counter_ns()
        s(a_to, ch)
        return ms_since(t0)

    def bandwidth(self, d, ch, b_from, b_to):
        s = self.u.set_rx_bandwidth if d == "rx" else self.u.set_tx_bandwidth
        s(b_from, ch)
        t0 = time.perf_counter_ns()
        s(b_to, ch)
        return ms_since(t0)

    def rate(self, d, ch, r_from, r_to):
        s = self.u.set_rx_rate if d == "rx" else self.u.set_tx_rate
        s(r_from, ch)
        t0 = time.perf_counter_ns()
        s(r_to, ch)
        return ms_since(t0)

    def time_source(self, s_from, s_to):
        self.u.set_time_source(s_from)
        t0 = time.perf_counter_ns()
        self.u.set_time_source(s_to)
        return ms_since(t0)

    def clock_source(self, s_from, s_to):
        self.u.set_clock_source(s_from)
        t0 = time.perf_counter_ns()
        self.u.set_clock_source(s_to)
        end = time.perf_counter() + 30
        while not self.u.get_mboard_sensor("ref_locked").to_bool():
            if time.perf_counter() > end:
                raise RuntimeError("ref_locked did not assert")
            time.sleep(0.001)
        return ms_since(t0)

    def stream_start(self, ch):
        import numpy as np
        st = self.uhd.usrp.StreamArgs("sc16", "sc16")
        st.channels = [ch]
        rx = self.u.get_rx_stream(st)
        buf = np.zeros((1, rx.get_max_num_samps()), dtype="<i4")
        md = self.uhd.types.RXMetadata()
        for _ in range(3):                              # drain anything left from a previous run
            if not rx.recv(buf, md, 0.05):
                break
        cmd = self.uhd.types.StreamCMD(self.uhd.types.StreamMode.start_cont)
        cmd.stream_now = True
        t0 = time.perf_counter_ns()
        rx.issue_stream_cmd(cmd)
        n = rx.recv(buf, md, 5.0)
        dt = ms_since(t0)
        rx.issue_stream_cmd(self.uhd.types.StreamCMD(self.uhd.types.StreamMode.stop_cont))
        time.sleep(0.05)
        while rx.recv(buf, md, 0.1):
            pass
        if not n:
            raise RuntimeError("no packet received after the stream command")
        return dt


def measure_init(uhd, args_str, mcr):
    t0 = time.perf_counter_ns()
    u = uhd.usrp.MultiUSRP("%s,master_clock_rate=%g" % (args_str, mcr))
    dt = ms_since(t0)
    del u
    import gc
    gc.collect()
    if dt < 150:
        raise RuntimeError("device creation took only %.0f ms: UHD returned a cached device instance "
                           "(another reference is alive); init time is not meaningful" % dt)
    return dt


# ------------------------------------------------------------------ planning
def build_plan(a, spec, rng, m, usrp_info):
    """usrp_info: dict(rx_channels, tx_channels, rx_ants{ch:[..]}, tx_ants{ch:[..]}) or None (dry-run)."""
    plan = Plan()
    events = a.events
    chans = a.channels

    def dirs():
        yield "rx"
        if a.allow_tx:
            yield "tx"
    for d in dirs():
        for ch in chans:
            key = "%s:%d" % (d, ch)
            if "lo_retune" in events:
                for f0, f1 in pairs(spec["rx_freq" if d == "rx" else "tx_freq"], a.max_pairs, rng):
                    plan.add("lo_retune", key, f0, f1, (lambda d=d, ch=ch, f0=f0, f1=f1: m.lo_retune(d, ch, f0, f1)))
            if "gain_change" in events:
                for g0, g1 in pairs(spec["gain"], a.max_pairs, rng):
                    plan.add("gain_change", key, g0, g1, (lambda d=d, ch=ch, g0=g0, g1=g1: m.gain(d, ch, g0, g1)))
            if "antenna" in events:
                ants = spec["antenna_" + d]
                if usrp_info:
                    ants = [x for x in ants if x in usrp_info["%s_ants" % d].get(ch, [])] or usrp_info["%s_ants" % d].get(ch, [])
                for a0, a1 in pairs(ants, a.max_pairs, rng):
                    plan.add("antenna", key, a0, a1, (lambda d=d, ch=ch, a0=a0, a1=a1: m.antenna(d, ch, a0, a1)))
            if "dsp_rate" in events:
                for r0, r1 in pairs(spec["rate"], a.max_pairs, rng):
                    plan.add("dsp_rate", key, r0, r1, (lambda d=d, ch=ch, r0=r0, r1=r1: m.rate(d, ch, r0, r1)))
            if "bandwidth" in events and spec["bandwidth"]:
                for b0, b1 in pairs(spec["bandwidth"], a.max_pairs, rng):
                    plan.add("bandwidth", key, b0, b1, (lambda d=d, ch=ch, b0=b0, b1=b1: m.bandwidth(d, ch, b0, b1)))
            if d == "rx" and "stream_start" in events:
                plan.add("stream_start", key, "", "", (lambda ch=ch: m.stream_start(ch)))
    if "time_source" in events:
        for s0, s1 in pairs(spec["time_source"], a.max_pairs, rng):
            plan.add("time_source", "mboard:0", s0, s1, (lambda s0=s0, s1=s1: m.time_source(s0, s1)))
    if "clock_source" in events and spec["clock_source"]:
        for s0, s1 in pairs(spec["clock_source"], a.max_pairs, rng):
            plan.add("clock_source", "mboard:0", s0, s1, (lambda s0=s0, s1=s1: m.clock_source(s0, s1)))
    return plan


def init_pairs(spec, a, rng):
    mcrs = spec["master_clock_rate"]
    allp = list(itertools.product(mcrs, repeat=2))      # includes same-rate re-creation
    return allp if not a.max_pairs else (allp if len(allp) <= a.max_pairs else sorted(rng.sample(allp, a.max_pairs)))


# ------------------------------------------------------------------ CLI
def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--args", default="type=x300", help='UHD device args, e.g. "type=x300,addr=192.168.10.2"')
    p.add_argument("--channels", type=int, nargs="+", default=[0], help="channel indices to measure (default: 0)")
    p.add_argument("--events", default=",".join(DEFAULT_EVENTS),
                   help="comma list; default: %s. Also available: %s" % (",".join(DEFAULT_EVENTS), ",".join(ALL_MEASURABLE[len(DEFAULT_EVENTS):])))
    p.add_argument("--spec", help="TOML with sweep values (keys: %s)" % ", ".join(DEFAULT_SPEC))
    p.add_argument("--repeat", type=int, default=5, help="measured repetitions per transition (default 5)")
    p.add_argument("--warmup", type=int, default=1, help="discarded repetitions per transition (default 1)")
    p.add_argument("--max-pairs", type=int, default=0, help="random sample of at most N ordered pairs per sweep (0 = all)")
    p.add_argument("--output", default="measured.csv")
    p.add_argument("--resume", action="store_true", help="skip transitions already in --output and append")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--dry-run", action="store_true", help="print what would be measured and exit (device untouched)")
    p.add_argument("--allow-tx", action="store_true",
                   help="also measure TX events (TX LO/gain/rate only; antennas/ports should be terminated)")
    p.add_argument("--lmk-baseline-ms", type=float, default=0.0,
                   help="client-side init time without any LMK wait, subtracted from device_init to get lmk_reprogram "
                        "(measure it on the simulator with time_scale = 0; default 0 = lmk rows equal device_init)")
    a = p.parse_args(argv)
    a.events = [e.strip() for e in a.events.split(",") if e.strip()]
    for e in a.events:
        if e not in de.EVENTS:
            p.error("unknown event %r (known: %s)" % (e, ", ".join(sorted(de.EVENTS))))
        if e in NOT_MEASURABLE or e.startswith("lo_retune_"):
            p.error("event %r cannot be measured through the UHD Python API; set it in the sim config by hand" % e)
    if "lmk_reprogram" in a.events and "device_init" not in a.events:
        a.events.append("device_init")
    return a


def load_spec(path):
    spec = {k: list(v) for k, v in DEFAULT_SPEC.items()}
    if path:
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if k not in DEFAULT_SPEC:
                raise SystemExit("spec: unknown key %r (known: %s)" % (k, ", ".join(DEFAULT_SPEC)))
            if not isinstance(v, list):
                raise SystemExit("spec: %s must be a list" % k)
            spec[k] = v
    return spec


def done_set(path):
    if not os.path.exists(path):
        return set()
    return {(r.event, r.key, r.frm, r.to) for r in de.read_csv(path)}


def print_plan(a, spec, plan, init_prs):
    print("measurement plan (%s)" % ("DRY RUN: the device is not touched" if a.dry_run else "live"))
    print("  device args : %s" % a.args)
    print("  channels    : %s   TX events: %s" % (a.channels, "ENABLED (--allow-tx)" if a.allow_tx else "off (receive-only)"))
    print("  repeat/warmup: %d/%d   output: %s%s" % (a.repeat, a.warmup, a.output, " (resume)" if a.resume else ""))
    counts = {}
    for ev, key, f, t, _ in plan.items:
        counts[(ev, key)] = counts.get((ev, key), 0) + 1
    for (ev, key), n in sorted(counts.items()):
        print("  %-14s %-9s %3d transitions" % (ev, key, n))
    if "device_init" in a.events:
        print("  %-14s %-9s %3d transitions (device re-creation) %s" % ("device_init", "mboard:0", len(init_prs), init_prs))
    skipped = [e for e in a.events if e not in {i[0] for i in plan.items} and e not in ("device_init", "lmk_reprogram")]
    if skipped:
        print("  nothing to measure for: %s (empty sweep list)" % ", ".join(skipped))
    print("  not measurable via MultiUSRP (configure manually in the sim): %s" % ", ".join(NOT_MEASURABLE))
    total = len(plan.items) + len(init_prs if "device_init" in a.events else [])
    print("  total transitions: %d  ->  %d timed runs (+%d warm-up)" % (total, total * a.repeat, total * a.warmup))


def summary(rows):
    print("\nsummary (median of medians per event/key, ms):")
    by = {}
    for r in rows:
        by.setdefault((r.event, r.key), []).append(r)
    print("  %-14s %-9s %5s %10s %10s %10s" % ("event", "key", "trans", "median", "min", "max"))
    for (ev, key), rs in sorted(by.items()):
        ms = [r.ms for r in rs]
        print("  %-14s %-9s %5d %10.2f %10.2f %10.2f" % (ev, key, len(rs), statistics.median(ms), min(ms), max(ms)))


def main(argv=None):
    a = parse_args(argv)
    spec = load_spec(a.spec)
    rng = random.Random(a.seed)
    plan = build_plan(a, spec, rng, None, None)
    init_prs = init_pairs(spec, a, rng) if "device_init" in a.events else []
    if a.dry_run:
        print_plan(a, spec, plan, init_prs)
        return 0
    try:
        import uhd
    except ImportError:
        sys.exit("cannot import uhd. Use the Python API of the UHD you test against, e.g.\n"
                 "  source tools/usrp_sim/uhd-env.sh   # local build\n"
                 "or set PYTHONPATH to the directory containing the 'uhd' package.")
    if a.allow_tx:
        print("WARNING: --allow-tx: TX LO/gain/rate/antenna settings will be exercised. Terminate or attenuate "
              "all TX ports/antennas. TX gain is forced to the minimum first; no TX stream is started.", flush=True)
    signal.signal(signal.SIGINT, _sigint)
    already = done_set(a.output) if a.resume else set()
    if not a.resume and os.path.exists(a.output) and os.path.getsize(a.output) > 0:
        sys.exit("%s exists: use --resume to append or choose another --output" % a.output)

    written = []

    def record(event, key, frm, to, samples):
        row = de.row_from_samples(event, key, frm, to, samples)
        de.append_rows(a.output, [row])        # written (and fsynced) immediately: Ctrl-C loses nothing
        written.append(row)
        return row

    todo = [(ev, key, de.fmt_value(f), de.fmt_value(t), fn) for ev, key, f, t, fn in plan.items]
    todo = [x for x in todo if (x[0], x[1], x[2], x[3]) not in already]
    init_todo = [(A, B) for A, B in init_prs if ("device_init", "mboard:0", de.fmt_value(A), de.fmt_value(B)) not in already]
    print("opening device %s ..." % a.args, flush=True)
    t_open = time.perf_counter()
    if todo:
        usrp = uhd.usrp.MultiUSRP(a.args)
        info = dict(rx_ants={c: list(usrp.get_rx_antennas(c)) for c in a.channels},
                    tx_ants={c: list(usrp.get_tx_antennas(c)) for c in a.channels} if a.allow_tx else {})
        if a.allow_tx:
            for c in a.channels:
                usrp.set_tx_gain(0.0, c)
        m = Measurer(usrp, uhd, a)
        # rebuild the plan with the real callables and the antennas this device actually has
        plan = build_plan(a, spec, random.Random(a.seed), m, info)
        todo = [(ev, key, de.fmt_value(f), de.fmt_value(t), fn) for ev, key, f, t, fn in plan.items]
        todo = [x for x in todo if (x[0], x[1], x[2], x[3]) not in already]
        print("device ready in %.1f s: %s" % (time.perf_counter() - t_open, usrp.get_pp_string().splitlines()[2:4]))
        total = len(todo) + len(init_todo)
        for i, (ev, key, f, t, fn) in enumerate(todo, 1):
            if stop_requested:
                break
            samples = []
            try:
                for _ in range(a.warmup):
                    fn()
                for _ in range(a.repeat):
                    samples.append(fn())
                    if stop_requested:
                        break
            except Exception as e:
                print("  [%d/%d] %-13s %-8s %s -> %s FAILED: %s" % (i, total, ev, key, f, t, e), flush=True)
                continue
            if samples:
                r = record(ev, key, f, t, samples)
                print("  [%d/%d] %-13s %-8s %12s -> %-12s median %8.2f ms  (n=%d, min %.2f, max %.2f)"
                      % (i, total, ev, key, f or "-", t or "-", r.ms, r.n, r.min_ms, r.max_ms), flush=True)
        # drop every reference to the device (the plan's closures hold the Measurer): UHD returns a
        # cached instance for identical args while one is alive, which would make device_init ~0
        fn = ev = key = f = t = samples = None      # the loop variables still point at the last closure
        del m, usrp
        todo.clear()
        plan.items.clear()
        import gc
        gc.collect()
    if init_todo and not stop_requested:
        print("re-creating the device for master clock / init measurements ...", flush=True)
        for j, (A, B) in enumerate(init_todo, 1):
            if stop_requested:
                break
            try:
                measure_init(uhd, a.args, A)                       # put the device in state A (not timed)
                samples = [measure_init(uhd, a.args, B) for _ in range(max(1, a.repeat))]
            except Exception as e:
                print("  device_init %g -> %g FAILED: %s" % (A, B, e), flush=True)
                continue
            r = record("device_init", "mboard:0", A, B, samples)
            print("  [init %d/%d] device_init %g -> %g: median %.1f ms (n=%d)" % (j, len(init_todo), A, B, r.ms, r.n), flush=True)
            if "lmk_reprogram" in a.events:
                lm = [max(0.0, s - a.lmk_baseline_ms) for s in samples]
                r2 = record("lmk_reprogram", "mboard:0", A, B, lm)
                print("               lmk_reprogram (init - baseline %.0f ms): median %.1f ms" % (a.lmk_baseline_ms, r2.ms), flush=True)
    rows = de.read_csv(a.output) if os.path.exists(a.output) else []
    summary(rows)
    print("\nwrote %s (%d rows, %d new)%s" % (a.output, len(rows), len(written), "  [interrupted]" if stop_requested else ""))
    return 130 if stop_requested else 0


if __name__ == "__main__":
    sys.exit(main())
