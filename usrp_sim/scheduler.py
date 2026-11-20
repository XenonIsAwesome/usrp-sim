"""Single timer/scheduler shared by delays, timed commands and stream pacing.

* ``call_later`` / ``call_at``  : heap of callbacks run on one worker thread
  (callbacks must be short; blocking work belongs in its own thread).
* ``wait_until(t)``             : precise pacing primitive used by stream threads
  (coarse sleep, then a short spin for the last ~150 us).
* all times are ``time.monotonic()`` seconds.  Device time <-> monotonic
  conversion is done by :class:`usrp_sim.clock.DeviceClock`.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("usrp_sim.sched")

now = time.monotonic


class Timer:
    __slots__ = ("when", "fn", "cancelled", "seq")

    def __init__(self, when, fn, seq):
        self.when, self.fn, self.seq, self.cancelled = when, fn, seq, False

    def cancel(self):
        self.cancelled = True

    def __lt__(self, o):
        return (self.when, self.seq) < (o.when, o.seq)


class Scheduler:
    SPIN_S = 150e-6

    def __init__(self, realtime: bool = True):
        self.realtime = realtime
        self._heap: list = []
        self._cv = threading.Condition()
        self._seq = itertools.count()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()

    # -- timers
    def call_at(self, when: float, fn: Callable[[], None]) -> Timer:
        t = Timer(when, fn, next(self._seq))
        with self._cv:
            heapq.heappush(self._heap, t)
            self._cv.notify()
        return t

    def call_later(self, delay: float, fn: Callable[[], None]) -> Timer:
        return self.call_at(now() + max(0.0, delay), fn)

    def pending(self) -> int:
        with self._cv:
            return sum(1 for t in self._heap if not t.cancelled)

    def _run(self):
        while True:
            with self._cv:
                while not self._stop:
                    while self._heap and self._heap[0].cancelled:
                        heapq.heappop(self._heap)
                    if not self._heap:
                        self._cv.wait()
                        continue
                    dt = self._heap[0].when - now()
                    if dt <= 0:
                        break
                    self._cv.wait(dt)
                if self._stop:
                    return
                t = heapq.heappop(self._heap)
            try:
                if not t.cancelled:
                    t.fn()
            except Exception:
                log.exception("scheduled callback failed")

    # -- pacing for stream threads
    def wait_until(self, when: float, stop: Optional[threading.Event] = None) -> bool:
        """Sleep until monotonic time `when`. Returns False if `stop` got set."""
        while True:
            dt = when - now()
            if dt <= 0:
                return not (stop is not None and stop.is_set())
            if stop is not None:
                if stop.wait(dt - self.SPIN_S if dt > self.SPIN_S else 0):
                    return False
                if dt <= self.SPIN_S:
                    continue
            elif dt > self.SPIN_S:
                time.sleep(dt - self.SPIN_S)

    def close(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2)
