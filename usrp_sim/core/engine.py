"""Engine: the shared runtime (scheduler, device clock, delay model, stats, routing)."""
from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from .. import clock as clk
from .. import delays as dl
from .. import scheduler as sch
from ..config import Config

log = logging.getLogger("usrp_sim.engine")


@dataclass
class ChanStats:
    samples: int = 0
    packets: int = 0
    underruns: int = 0      # TX: data not there in time / RX: source starved (zero-filled)
    overruns: int = 0       # RX overrun flagged to the host
    late: int = 0           # late commands / late data
    zero_filled: int = 0    # samples that were zero-filled because the source ran dry
    dropped: int = 0

    def line(self) -> str:
        return ("samples=%d packets=%d underruns=%d overruns=%d late=%d zero_filled=%d dropped=%d"
                % (self.samples, self.packets, self.underruns, self.overruns, self.late,
                   self.zero_filled, self.dropped))


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sched = sch.Scheduler(realtime=cfg.realtime)
        self.clock = clk.DeviceClock(cfg.master_clock_rate, cfg.time_pps, cfg.time_initial_offset_s)
        self.delays = dl.DelayModel(cfg.delays, cfg.base_dir)
        self.stats: Dict[str, ChanStats] = {}
        self._send: Optional[Callable[[bytes, Tuple[str, int]], None]] = None
        self.routes: Dict[int, Tuple[str, int]] = {}     # epid -> UDP address (learned)
        self.lock = threading.RLock()
        self.closed = threading.Event()

    # ---- statistics
    def stat(self, name: str) -> ChanStats:
        with self.lock:
            return self.stats.setdefault(name, ChanStats())

    # ---- packet output
    def set_sender(self, fn):
        self._send = fn

    def learn_route(self, epid: int, addr) -> None:
        if self.routes.get(epid) != addr:
            self.routes[epid] = addr
            log.debug("route: EPID %d -> %s:%d", epid, addr[0], addr[1])

    def send_to_epid(self, epid: int, data: bytes) -> bool:
        addr = self.routes.get(epid)
        if addr is None or self._send is None:
            log.debug("no route to EPID %d, dropping %d bytes", epid, len(data))
            return False
        self._send(data, addr)
        return True

    def send_to_addr(self, data: bytes, addr) -> None:
        if self._send is not None:
            self._send(data, addr)

    # ---- delays
    def delay(self, event: str, key: str = "", old=None, new=None) -> float:
        return self.delays.delay(event, key, old, new)

    def close(self):
        self.closed.set()
        self.sched.close()
