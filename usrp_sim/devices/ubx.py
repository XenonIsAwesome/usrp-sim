"""Daughterboard behaviour models: LO lock-detect timing (UBX) and gain/antenna change
detection.  Source of truth: host/lib/usrp/dboard/db_ubx.cpp (gpio layout, SPI dests) and
the MAX2871 register map (frequency decode, only used to key the `lo_retune` delay).
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from .. import scheduler as sch
from ..core.engine import Engine
from . import dboards as dbm

log = logging.getLogger("usrp_sim.dboard")

# db_ubx.cpp spi_dest_t
TXLO1, TXLO2, RXLO1, RXLO2, CPLD = range(5)
# GPIO layout (db_ubx.cpp ubx_proto_gpio_info / ubx_v1_gpio_info)
UBX_PROTO_IDS = {0x73, 0x74, 0x75, 0x76}


def max2871_freq(regs: Dict[int, int], fref: float) -> Optional[float]:
    try:
        r0, r1, r2, r4 = regs[0], regs[1], regs[2], regs[4]
    except KeyError:
        return None
    n, frac = (r0 >> 15) & 0xFFFF, (r0 >> 3) & 0xFFF
    m = max(1, (r1 >> 3) & 0xFFF)
    r = max(1, (r2 >> 14) & 0x3FF)
    dbl, rdiv2 = (r2 >> 25) & 1, (r2 >> 24) & 1
    diva = (r4 >> 20) & 7
    fpfd = fref * (1 + dbl) / (r * (1 + rdiv2))
    return fpfd * (n + frac / m) / (1 << diva)


class DboardModel:
    """One daughterboard slot ("A"/"B"). Generic: SPI writes are accepted and the lock
    pins assert `lo_retune` after the last SPI write."""

    def __init__(self, engine: Engine, slot: str, name: str):
        self.e, self.slot, self.name = engine, slot, name
        self.rx_id, self.tx_id, self.family = dbm.DBOARDS[name]
        self.chan = 0 if slot == "A" else 1
        self.lock_at = {"rx": 0.0, "tx": 0.0}
        self.freq = {"rx": 0.0, "tx": 0.0}
        self.lo_regs: Dict[int, Dict[int, int]] = {d: {} for d in range(4)}
        self.gpio_idle = {"tx": 0, "rx": 0}      # last driven values (units TX / RX)
        self.mcr = 200e6

    # -- hooks called by the radio
    def dboard_clock(self) -> float:
        i = 1
        while self.mcr / i > 50e6:
            i += 1
        return self.mcr / i

    def spi_write(self, which: str, word: int, nbits: int) -> None:
        """`which` = 'tx' | 'rx' (the SPI slave select that was addressed)."""
        if self.family != "ubx" or which != "tx":
            if self.family != "ubx":
                self._generic_lo(which)
            return
        dest = self.gpio_idle["tx"] & 0x7
        if dest >= CPLD:
            return
        reg = word & 0x7
        self.lo_regs[dest][reg] = word
        if reg == 0:                                 # R0 write triggers the VCO retune
            grp = "tx" if dest in (TXLO1, TXLO2) else "rx"
            fnew = max2871_freq(self.lo_regs[dest], self.dboard_clock()) if dest in (TXLO1, RXLO1) else None
            fold = self.freq[grp]
            if fnew is not None and dest in (TXLO1, RXLO1):
                self.freq[grp] = fnew
                d = self.e.delays.lo_retune("%s:%d" % (grp, self.chan), fold, fnew)
            else:
                d = self.e.delay("lo_retune", "%s:%d" % (grp, self.chan), fold, fold)
            self.lock_at[grp] = sch.now() + d

    def _generic_lo(self, which: str):
        grp = "tx" if which == "tx" else "rx"
        self.lock_at[grp] = sch.now() + self.e.delays.lo_retune("%s:%d" % (grp, self.chan), 0.0, 0.0)

    def gpio_out(self, unit: str, old: int, new: int) -> float:
        """The host drove the DB GPIO of `unit` ('tx'/'rx'); returns extra latency (s)."""
        self.gpio_idle[unit] = new
        extra = 0.0
        if self.family == "ubx":
            if (old ^ new) & (0x3F << 10):           # TX_GAIN / RX_GAIN field
                # UBX gain code = floor(2 * gain_dB) (db_ubx.cpp set_rx_gain/set_tx_gain)
                extra += self.e.delay("gain_change", "%s:%d" % (unit, self.chan),
                                      (old >> 10 & 0x3F) / 2.0, (new >> 10 & 0x3F) / 2.0)
            ant_bit = (1 << 5) if int(self.rx_id) in UBX_PROTO_IDS else (1 << 4)
            if unit == "tx" and (old ^ new) & ant_bit:
                names = lambda v: "RX2" if v & ant_bit else "TX/RX"     # RX_ANT=0 selects TX/RX
                extra += self.e.delay("antenna", "rx:%d" % self.chan, names(old), names(new))
        return extra

    def gpio_readback(self) -> int:
        """Value of RB_DB_GPIO: RX unit in [15:0], TX unit in [31:16] (gpio_atr_3000.cpp)."""
        now = sch.now()
        tx_locked, rx_locked = now >= self.lock_at["tx"], now >= self.lock_at["rx"]
        rx_unit = tx_unit = 0
        if self.family == "ubx":
            if int(self.rx_id) in UBX_PROTO_IDS:     # proto: lock pins on the TX unit
                tx_unit |= (int(tx_locked) << 6) | (int(rx_locked) << 7)
            else:                                    # v1+: lock pins on the RX unit
                rx_unit |= int(rx_locked) | (int(tx_locked) << 1)
        elif self.family in ("sbx", "wbx"):
            rx_unit |= int(rx_locked) << 2
            tx_unit |= int(tx_locked) << 2
        return (tx_unit << 16) | rx_unit
