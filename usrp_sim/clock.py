"""Device clock: device time derived from the host system clock.

``ticks = base_ticks + (wall_now - base_wall) * tick_rate`` -- implemented through the
timekeeper registers of x300_mb_controller.cpp (TK_*): set ticks now / at next PPS,
read ticks now / last PPS, period (tick rate).  PPS edges fall on system-second
boundaries (``pps = "system-second"``) or never (``"none"``).
"""
from __future__ import annotations

import math
import threading
import time
from typing import Optional

from . import scheduler as sch


class DeviceClock:
    def __init__(self, tick_rate: float, pps: str = "system-second", initial_offset_s: float = 0.0):
        self._lock = threading.RLock()
        self.pps_mode = pps
        self.tick_rate = float(tick_rate)
        self.base_wall = time.time()
        self.base_ticks = int((self.base_wall + initial_offset_s) * self.tick_rate)
        self._pending_pps_ticks: Optional[int] = None   # set_time_next_pps request
        self._pps_applied_edge = math.floor(self.base_wall)

    # --- wall <-> ticks
    def _poll_pps(self, wall: float) -> None:
        """Apply a pending set-time-at-next-PPS once the edge has passed."""
        if self._pending_pps_ticks is None or self.pps_mode == "none":
            return
        edge = math.floor(wall)
        if edge > self._pps_applied_edge:
            self._pps_applied_edge = edge
            # the clock now shows `pending` at the instant of the edge
            self.base_ticks = self._pending_pps_ticks + int(round((wall - edge) * self.tick_rate))
            self.base_wall = wall
            self._pending_pps_ticks = None

    def _ticks_at(self, wall: float) -> int:
        return self.base_ticks + int(round((wall - self.base_wall) * self.tick_rate))

    def ticks_now(self) -> int:
        with self._lock:
            w = time.time()
            self._poll_pps(w)
            return self._ticks_at(w)

    def set_ticks_now(self, ticks: int) -> None:
        with self._lock:
            self.base_wall, self.base_ticks = time.time(), int(ticks)

    def set_ticks_next_pps(self, ticks: int) -> None:
        with self._lock:
            self._poll_pps(time.time())
            self._pending_pps_ticks = int(ticks)

    def ticks_last_pps(self) -> int:
        with self._lock:
            w = time.time()
            self._poll_pps(w)
            if self.pps_mode == "none":
                return 0
            return self._ticks_at(math.floor(w))

    def set_tick_rate(self, rate: float) -> None:
        """Change the tick rate keeping device time (seconds) continuous (master clock change)."""
        with self._lock:
            w = time.time()
            self._poll_pps(w)
            self.base_ticks = int(round(self._ticks_at(w) * float(rate) / self.tick_rate))
            self.base_wall = w
            self.tick_rate = float(rate)

    # --- scheduling helpers
    def ticks_to_mono(self, ticks: int) -> float:
        """Monotonic deadline at which device time reaches `ticks`."""
        with self._lock:
            wall = self.base_wall + (ticks - self.base_ticks) / self.tick_rate
        return sch.now() + (wall - time.time())

    def mono_to_ticks(self, mono: float) -> int:
        with self._lock:
            return self._ticks_at(time.time() + (mono - sch.now()))

    def seconds_to_ticks(self, s: float) -> int:
        return int(round(s * self.tick_rate))
