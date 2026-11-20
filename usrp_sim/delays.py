"""Pluggable delay model.

Every delay in the simulator goes through :class:`DelayModel`; no delay value is
hard-coded anywhere else.  All values come from the ``[delays]`` config section.

Models
------
fixed    per-event value from ``[delays.fixed_ms]`` (ms), others ``default_ms``
uniform  value v -> U(v*(1-jitter), v*(1+jitter)); a ``{min,max}`` entry is exact
normal   value v -> N(v, (v*std_fraction)); a ``{mean,std}`` entry is exact
table    measured CSV (see delay_events.py); fallback chain documented in
         :meth:`TableModel.lookup`
hook     ``module:function(event, old_state, new_state) -> seconds``

A per-event entry in ``[delays.fixed_ms]`` always wins over the model chosen:
a number = fixed value, ``{min=,max=}`` = uniform, ``{mean=,std=}`` = normal.
"""
from __future__ import annotations

import importlib
import logging
import random
import threading
from typing import Any, Dict, Optional

from . import delay_events as de

log = logging.getLogger("usrp_sim.delays")

MODELS = ("fixed", "uniform", "normal", "table", "hook")


class DelayConfigError(ValueError):
    pass


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _distance(a: str, b: str) -> Optional[float]:
    """Distance between two from/to strings; None if not comparable (0 if equal)."""
    if a == b:
        return 0.0
    fa, fb = _num(a), _num(b)
    if fa is None or fb is None:
        return None
    return abs(fa - fb) / max(abs(fa), abs(fb), 1.0)


class TableModel:
    def __init__(self, rows):
        self.rows = list(rows)
        self.by_event: Dict[str, list] = {}
        for r in self.rows:
            self.by_event.setdefault(r.event, []).append(r)

    @classmethod
    def from_csv(cls, path):
        return cls(de.read_csv(path))

    def lookup(self, event: str, key: str, frm: str, to: str) -> Optional[float]:
        """ms for (event,key,from,to), or None if the event has no rows.

        1. exact event+key+from+to
        2. same event+key: row with the nearest (from,to) (numeric relative distance;
           non-numeric values must match exactly, else that row is skipped)
        3. same event, any key, nearest (from,to) -- then
        4. the event's median over all rows
        (the caller then falls back to default_ms when this returns None)
        """
        rows = self.by_event.get(event)
        if not rows:
            return None
        for r in rows:
            if r.key == key and r.frm == frm and r.to == to:
                return r.ms
        for pool in ([r for r in rows if r.key == key], rows):
            best, best_d = None, None
            for r in pool:
                d1, d2 = _distance(r.frm, frm), _distance(r.to, to)
                if d1 is None or d2 is None:
                    continue
                d = d1 + d2
                if best_d is None or d < best_d:
                    best, best_d = r, d
            if best is not None:
                return best.ms
        mss = sorted(r.ms for r in rows)
        return mss[len(mss) // 2] if len(mss) % 2 else 0.5 * (mss[len(mss) // 2 - 1] + mss[len(mss) // 2])


def validate_delays(cfg: Dict[str, Any]) -> None:
    unknown = set(cfg) - {"time_scale", "model", "default_ms", "table", "hook", "jitter",
                          "std_fraction", "seed", "fixed_ms"}
    if unknown:
        raise DelayConfigError("[delays]: unknown key(s): %s" % ", ".join(sorted(unknown)))
    if cfg.get("model", "fixed") not in MODELS:
        raise DelayConfigError("[delays] model must be one of %s" % ", ".join(MODELS))
    for k in ("time_scale", "default_ms", "jitter", "std_fraction"):
        if k in cfg and (_num(cfg[k]) is None or float(cfg[k]) < 0):
            raise DelayConfigError("[delays] %s must be a non-negative number" % k)
    for ev, val in (cfg.get("fixed_ms") or {}).items():
        if ev not in de.EVENTS:
            raise DelayConfigError("[delays.fixed_ms]: unknown event %r (known: %s)"
                                   % (ev, ", ".join(sorted(de.EVENTS))))
        if isinstance(val, dict):
            ks = set(val)
            if ks not in ({"min", "max"}, {"mean", "std"}):
                raise DelayConfigError("[delays.fixed_ms] %s: table must be {min,max} or {mean,std}" % ev)
            if any(_num(v) is None or float(v) < 0 for v in val.values()):
                raise DelayConfigError("[delays.fixed_ms] %s: values must be non-negative numbers" % ev)
        elif _num(val) is None or float(val) < 0:
            raise DelayConfigError("[delays.fixed_ms] %s must be a non-negative number or table" % ev)


class DelayModel:
    """Thread-safe; ``reload()`` swaps the configuration atomically (SIGHUP)."""

    def __init__(self, cfg: Dict[str, Any], base_dir: str = "."):
        self._lock = threading.Lock()
        self._rng = random.Random(cfg.get("seed"))
        self.base_dir = base_dir
        self._apply(cfg)

    def _apply(self, cfg: Dict[str, Any]) -> None:
        validate_delays(cfg)
        import os
        model = cfg.get("model", "fixed")
        table = None
        if model == "table":
            path = cfg.get("table", "")
            if not path:
                raise DelayConfigError("[delays] model = \"table\" requires table = \"file.csv\"")
            if not os.path.isabs(path):
                path = os.path.join(self.base_dir, path)
            try:
                table = TableModel.from_csv(path)
            except (OSError, ValueError) as e:
                raise DelayConfigError("[delays] cannot load table: %s" % e)
        hook = None
        if model == "hook":
            spec = cfg.get("hook", "")
            if ":" not in spec:
                raise DelayConfigError("[delays] model = \"hook\" requires hook = \"module:function\"")
            mod, fn = spec.split(":", 1)
            try:
                hook = getattr(importlib.import_module(mod), fn)
            except (ImportError, AttributeError) as e:
                raise DelayConfigError("[delays] cannot import hook %r: %s" % (spec, e))
        new = dict(model=model, table=table, hook=hook,
                   scale=float(cfg.get("time_scale", 1.0)),
                   default_ms=float(cfg.get("default_ms", 0.0)),
                   jitter=float(cfg.get("jitter", 0.1)),
                   std_fraction=float(cfg.get("std_fraction", 0.1)),
                   per_event=dict(cfg.get("fixed_ms") or {}))
        with self._lock:
            self._c = new

    def reload(self, cfg: Dict[str, Any]) -> None:
        self._apply(cfg)
        log.info("delay configuration reloaded (model=%s, time_scale=%g)",
                 self._c["model"], self._c["scale"])

    @property
    def time_scale(self) -> float:
        return self._c["scale"]

    def _sample(self, spec, mode) -> float:
        if isinstance(spec, dict):
            if "min" in spec:
                return self._rng.uniform(float(spec["min"]), float(spec["max"]))
            return max(0.0, self._rng.gauss(float(spec["mean"]), float(spec["std"])))
        v = float(spec)
        c = self._c
        if mode == "uniform":
            return self._rng.uniform(v * (1 - c["jitter"]), v * (1 + c["jitter"]))
        if mode == "normal":
            return max(0.0, self._rng.gauss(v, v * c["std_fraction"]))
        return v

    def delay(self, event: str, key: str = "", old: Any = None, new: Any = None) -> float:
        """Delay in SECONDS (already multiplied by time_scale) for an event."""
        if event not in de.EVENTS:
            raise KeyError("unknown delay event %r" % event)
        c = self._c
        with self._lock:
            ms = self._lookup(c, event, key, old, new)
            ms = max(0.0, ms)
        sec = ms * 1e-3 * c["scale"]
        if sec > 0 or event not in ("reg_access", "ctrl_access"):
            log.debug("delay event=%s key=%s from=%s to=%s: %.3f ms (scale %g)", event, key,
                      de.fmt_value(old), de.fmt_value(new), ms * c["scale"], c["scale"])
        return sec

    def _lookup(self, c, event, key, old, new) -> float:
        per = c["per_event"]
        model = c["model"]
        sampled = model if model in ("uniform", "normal") else "fixed"
        if event in per:                       # explicit per-event override wins
            return self._sample(per[event], sampled)
        if model == "hook":
            try:
                return float(c["hook"](event, old, new)) * 1e3
            except Exception as e:  # a broken hook must not kill the sim
                log.error("delay hook failed for %s: %s", event, e)
                return c["default_ms"]
        if model == "table":
            ms = c["table"].lookup(event, key, de.fmt_value(old), de.fmt_value(new))
            return c["default_ms"] if ms is None else ms
        return self._sample(c["default_ms"], sampled)

    def lo_retune(self, key: str, f_old: float, f_new: float) -> float:
        """LO retune delay (s). Non-table models may have per-class entries
        (lo_retune_small/large/band) which fall back to lo_retune, then default_ms."""
        c = self._c
        if c["model"] not in ("table", "hook"):
            cls = de.classify_lo_step(f_old, f_new)
            for ev in ("lo_retune_" + cls, "lo_retune"):
                if ev in c["per_event"]:
                    return self.delay(ev, key, f_old, f_new)
        return self.delay("lo_retune", key, f_old, f_new)
