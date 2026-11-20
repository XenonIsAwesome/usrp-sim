"""Shared definition of delay events, keys and the measured-delay CSV schema.

Imported by BOTH the simulator (usrp_sim.delays) and tools/measure_delays.py so
event names, key naming and the CSV layout cannot drift apart.

CSV schema (UTF-8, header row required, '.' decimal point)::

    event,key,from,to,ms,n,min_ms,max_ms,mean_ms,std_ms,p95_ms

* ``ms``      median of the measurements; this is the value the sim replays.
* ``key``     the target: ``rx:0``, ``tx:0``, ``mboard:0`` ...
* ``from/to`` parameter values of the transition in plain units (Hz, dB,
              samples/s, antenna name, source name); empty if not applicable.
* the statistics columns are always written by the script and optional for the sim.
"""
from __future__ import annotations

import csv
import statistics
from typing import Dict, Iterable, List, NamedTuple, Optional

# event name -> (description, key kind, unit of from/to)
EVENTS: Dict[str, tuple] = {
    "lmk_reprogram": ("Master clock change: LMK04816 reprogram + PLL lock", "mboard:0", "Hz"),
    "clock_source":  ("Clock source switch (internal/external/gpsdo)", "mboard:0", "name"),
    "time_source":   ("Time (PPS) source switch", "mboard:0", "name"),
    "device_init":   ("Complete device initialisation (usrp creation)", "mboard:0", "Hz"),
    "adc_init":      ("ADC (ADS62P48) reset/init", "mboard:0", ""),
    "dac_init":      ("DAC (AD9146) reset/init + PLL lock", "mboard:0", ""),
    "dac_sync":      ("DAC back-end/front-end synchronisation", "mboard:0", ""),
    "lo_retune":     ("Daughterboard LO retune + lock (rx and tx)", "rx:0", "Hz"),
    "lo_retune_small": ("LO retune, small step (<= small_step_hz); falls back to lo_retune", "rx:0", "Hz"),
    "lo_retune_large": ("LO retune, large step; falls back to lo_retune", "rx:0", "Hz"),
    "lo_retune_band":  ("LO retune crossing a band edge; falls back to lo_retune", "rx:0", "Hz"),
    "gain_change":   ("Gain change", "rx:0", "dB"),
    "antenna":       ("Antenna switch", "rx:0", "name"),
    "bandwidth":     ("Analog bandwidth/filter change", "rx:0", "Hz"),
    "dsp_rate":      ("DDC/DUC rate change", "rx:0", "samples/s"),
    "stream_start":  ("Stream command -> first packet", "rx:0", ""),
    "reset":         ("Full reset", "mboard:0", ""),
    "reg_access":    ("Extra latency of every ZPU register access", "mboard:0", ""),
    "ctrl_access":   ("Extra latency of every CHDR ctrl (NoC register) transaction", "mboard:0", ""),
}

# LO retune classification (used by the simulator; the CSV only uses lo_retune).
LO_SMALL_STEP_HZ = 10e6
LO_BAND_EDGES_HZ = (500e6, 1000e6, 1500e6, 2200e6, 3000e6, 4000e6, 5000e6)

CSV_FIELDS = ["event", "key", "from", "to", "ms", "n", "min_ms", "max_ms",
              "mean_ms", "std_ms", "p95_ms"]


class Row(NamedTuple):
    event: str
    key: str
    frm: str
    to: str
    ms: float
    n: int = 1
    min_ms: float = 0.0
    max_ms: float = 0.0
    mean_ms: float = 0.0
    std_ms: float = 0.0
    p95_ms: float = 0.0


def classify_lo_step(f_from: float, f_to: float) -> str:
    """Return 'small', 'band' or 'large' for an LO retune."""
    if abs(f_to - f_from) <= LO_SMALL_STEP_HZ:
        return "small"
    for edge in LO_BAND_EDGES_HZ:
        if (f_from < edge) != (f_to < edge):
            return "band"
    return "large"


def fmt_value(v) -> str:
    """Canonical text form of a from/to value (shortest round-trip repr)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) and abs(f) < 1e15 else repr(f)
    return str(v)


def summarize(samples_ms: List[float]) -> dict:
    s = sorted(samples_ms)
    n = len(s)
    p95 = s[min(n - 1, int(round(0.95 * (n - 1))))] if n else 0.0
    return dict(ms=statistics.median(s) if n else 0.0, n=n, min_ms=s[0] if n else 0.0,
                max_ms=s[-1] if n else 0.0, mean_ms=statistics.fmean(s) if n else 0.0,
                std_ms=statistics.pstdev(s) if n > 1 else 0.0, p95_ms=p95)


def row_from_samples(event: str, key: str, frm, to, samples_ms: List[float]) -> Row:
    st = summarize(samples_ms)
    return Row(event, key, fmt_value(frm), fmt_value(to), st["ms"], st["n"], st["min_ms"],
               st["max_ms"], st["mean_ms"], st["std_ms"], st["p95_ms"])


def read_csv(path) -> List[Row]:
    rows: List[Row] = []
    with open(path, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        missing = {"event", "key", "from", "to", "ms"} - set(rd.fieldnames or [])
        if missing:
            raise ValueError("%s: missing CSV columns: %s" % (path, ", ".join(sorted(missing))))
        for i, r in enumerate(rd, start=2):
            if not (r.get("event") or "").strip():
                continue
            ev = r["event"].strip()
            if ev not in EVENTS:
                raise ValueError("%s line %d: unknown event %r" % (path, i, ev))

            def fl(name, default=0.0):
                v = (r.get(name) or "").strip()
                return float(v) if v else default
            try:
                rows.append(Row(ev, (r["key"] or "").strip(), (r["from"] or "").strip(),
                                (r["to"] or "").strip(), fl("ms"), int(fl("n", 1)), fl("min_ms"),
                                fl("max_ms"), fl("mean_ms"), fl("std_ms"), fl("p95_ms")))
            except ValueError as e:
                raise ValueError("%s line %d: %s" % (path, i, e))
    return rows


def append_rows(path, rows: Iterable[Row]) -> None:
    """Append rows, writing the header if the file is new/empty. Flushes each call."""
    import os
    new = (not os.path.exists(path)) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(CSV_FIELDS)
        for r in rows:
            w.writerow([r.event, r.key, r.frm, r.to, "%.4f" % r.ms, r.n, "%.4f" % r.min_ms,
                        "%.4f" % r.max_ms, "%.4f" % r.mean_ms, "%.4f" % r.std_ms,
                        "%.4f" % r.p95_ms])
        f.flush()
        os.fsync(f.fileno())
