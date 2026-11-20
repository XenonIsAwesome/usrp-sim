"""Register-file models of the chips behind the ZPU/radio SPI and I2C cores.

Goal: UHD's own init/tune code paths succeed and observe lock bits asserting only
after the configured delay (sources: x300_clock_ctrl.cpp, x300_adc_ctrl.cpp,
x300_dac_ctrl.cpp, db_ubx.cpp, dboard_eeprom.cpp).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, List, Optional

from .. import scheduler as sch
from ..core.engine import Engine

log = logging.getLogger("usrp_sim.chips")

VCXO_HZ = 96e6          # x300_clock_ctrl.cpp: VCXO_FREQ (PLL2 ref is VCXO*2)


class I2CEeprom:
    """24Cxx-style EEPROM. 8-bit offset (daughterboards) or 16-bit (MB; only used by
    old firmware paths). File-backed when `path` is given (state_dir)."""

    def __init__(self, image: bytearray, path: Optional[str] = None, addr16: bool = False):
        self.mem = image
        self.path = path
        self.addr16 = addr16
        self.ptr = 0
        self._wbytes: List[int] = []
        if path:
            self._flush()     # image is always regenerated from the config; host writes persist for the run

    def _flush(self):
        if self.path:
            with open(self.path, "wb") as f:
                f.write(self.mem)

    def start_write(self):
        self._wbytes = []

    def write_byte(self, b: int):
        n_off = 2 if self.addr16 else 1
        if len(self._wbytes) < n_off:
            self._wbytes.append(b)
            if len(self._wbytes) == n_off:
                self.ptr = int.from_bytes(bytes(self._wbytes), "big") % len(self.mem)
        else:
            self.mem[self.ptr % len(self.mem)] = b
            self.ptr += 1
            self._flush()

    def read_byte(self) -> int:
        v = self.mem[self.ptr % len(self.mem)]
        self.ptr += 1
        return v


class I2CCore:
    """OpenCores-style i2c master as used by i2c_core_100_wb32.cpp (regs 0..4 at 4-byte
    stride: prescaler lo/hi, ctrl, data, cmd/status).  Transfers complete instantly
    (TIP never set).  Missing slaves NACK (RXACK set)."""

    CMD_START, CMD_STOP, CMD_RD, CMD_WR, CMD_NACK = 1 << 7, 1 << 6, 1 << 5, 1 << 4, 1 << 3
    ST_RXACK, ST_BUSY = 1 << 7, 1 << 6

    def __init__(self):
        self.slaves: Dict[int, I2CEeprom] = {}
        self.regs = [0xFF, 0xFF, 0, 0, 0]
        self.status = 0
        self.cur: Optional[I2CEeprom] = None
        self.reading = False

    def read(self, off: int) -> int:
        i = off // 4
        if i == 3:
            return self.regs[3]
        if i == 4:
            return self.status
        return self.regs[i] if i < 5 else 0

    def write(self, off: int, val: int) -> None:
        i = off // 4
        if i < 4:
            self.regs[i] = val & 0xFF
            return
        if i != 4:
            return
        cmd = val & 0xFF
        st = self.status & ~self.ST_RXACK
        if cmd & self.CMD_START:
            st |= self.ST_BUSY
        if cmd & self.CMD_WR:
            if cmd & self.CMD_START:                     # address byte
                a = self.regs[3]
                self.reading = bool(a & 1)
                self.cur = self.slaves.get(a >> 1)
                if self.cur is None:
                    st |= self.ST_RXACK
                elif not self.reading:
                    self.cur.start_write()
            elif self.cur is not None and not self.reading:
                self.cur.write_byte(self.regs[3])
            elif self.cur is None:
                st |= self.ST_RXACK
        if cmd & self.CMD_RD:
            self.regs[3] = self.cur.read_byte() if self.cur is not None else 0xFF
        if cmd & self.CMD_STOP:
            st &= ~self.ST_BUSY
            self.cur = None
        self.status = st


# ------------------------------------------------------------------ LMK04816
class Lmk04816:
    """Clock chip behind the ZPU SPI core (slave mask 0x1, 32-bit write-only).

    Decodes the master clock the host programs (VCO / CLKout0_1_DIV; bit fields from
    host/lib/ic_reg_maps/gen_lmk04816_regs.py) and exposes the PLL lock status.  A reset
    write to register 0 starts a (re)programming burst (x300_clock_ctrl.cpp reset_clocks);
    lock reads 0 until the `lmk_reprogram` (or `clock_source`) delay after the LAST write
    of the burst has elapsed."""
    BURST_WINDOW_S = 0.5

    def __init__(self, engine: Engine, initial_mcr: float):
        self.e = engine
        self.regs: Dict[int, int] = {}
        self.mcr = initial_mcr
        self.lock_at = 0.0                 # monotonic time at which LMK reports lock
        self.pending_event = "lmk_reprogram"
        self.reprogram_count = 0
        self.burst_start = -1e9
        self.burst_event = "lmk_reprogram"
        self.burst_old = initial_mcr
        self.on_mcr_change: Optional[Callable[[float, float], None]] = None

    def decode_mcr(self) -> Optional[float]:
        r = self.regs
        try:
            div = (r[0] >> 5) & 0x7FF
            n = (r[30] >> 5) & 0x3FFFF
            p = {0: 8, 1: 2, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7}[(r[30] >> 24) & 7]
            rr = (r[28] >> 20) & 0xFFF
        except KeyError:
            return None
        if not (div and n and rr):
            return None
        return VCXO_HZ * 2 * n * p / rr / div

    def write(self, word: int) -> None:
        addr = word & 0x1F
        self.regs[addr] = word
        now = sch.now()
        if addr == 0 and (word >> 17) & 1:                   # RESET bit: new burst
            self.burst_start = now
            self.burst_event = self.pending_event
            self.pending_event = "lmk_reprogram"
            self.burst_old = self.mcr
            self.lock_at = float("inf")
            return
        in_burst = now - self.burst_start < self.BURST_WINDOW_S
        if not (in_burst or addr in (28, 30)):
            return
        new = self.decode_mcr()
        if new and 100e6 < new < 250e6 and abs(new - self.mcr) > 1.0:
            old, self.mcr = self.mcr, new
            if self.on_mcr_change:
                self.on_mcr_change(old, new)
        self.lock_at = now + self.e.delay(self.burst_event if in_burst else "lmk_reprogram",
                                          "mboard:0", self.burst_old, self.mcr)
        self.reprogram_count += 1

    def locked(self) -> bool:
        return sch.now() >= self.lock_at


# ------------------------------------------------------------------ ADS62P48
class Ads62p48:
    """ADC behind the radio SPI core (16-bit writes: addr<<8|data). Test patterns are
    decoded so the radio's RX_DATA readback / checker behave like x300_adc_ctrl.cpp expects
    (register map: host/lib/ic_reg_maps/gen_ads62p48_regs.py)."""
    PATTERNS = {0: "normal", 1: "zeros", 2: "ones", 3: "toggle", 4: "ramp", 5: "custom"}

    def __init__(self, engine: Engine):
        self.e = engine
        self.regs: Dict[int, int] = {}
        self.resets = 0
        self.on_reset: Optional[Callable[[], None]] = None

    def write(self, word: int) -> None:
        addr, data = (word >> 8) & 0xFF, word & 0xFF
        if addr == 0 and data & 0x80:
            self.regs.clear()
            self.resets += 1
            if self.on_reset:
                self.on_reset()
            return
        self.regs[addr] = data

    @property
    def pat_a(self) -> str:
        return self.PATTERNS.get(self.regs.get(0x62, 0) & 7, "normal")

    @property
    def pat_b(self) -> str:
        return self.PATTERNS.get(self.regs.get(0x75, 0) & 7, "normal")

    @property
    def custom(self) -> int:
        return ((self.regs.get(0x52, 0) & 0x3F) << 8) | self.regs.get(0x51, 0)

    def _val(self, p: str) -> int:
        return {"ones": 0x3FFF, "zeros": 0, "custom": self.custom}.get(p, 0)

    def rx_word(self) -> int:
        """Radio RX_DATA readback (x300_radio_control.cpp _check_adc: word ^ 0xfffc0000)."""
        w = ((self._val(self.pat_a) & 0x3FFF) << 18) | ((self._val(self.pat_b) & 0x3FFF) << 2)
        return w ^ 0xFFFC0000


# ------------------------------------------------------------------ AD9146
class Ad9146:
    """DAC (16-bit SPI: bit15=read, [14:8]=addr, [7:0]=data). Models what x300_dac_ctrl.cpp
    polls: PLL lock (0x0E bit7, event flag 0x06 bit6), back-end sync (0x12 bit6, flag 0x06
    bit4) and front-end FIFO depth (0x19 == 0x0F). Lock/sync assert after the configured
    `dac_init` / `dac_sync` delays counted from the last relevant write."""

    def __init__(self, engine: Engine):
        self.e = engine
        self.regs: Dict[int, int] = {}
        self.pll_lock_at = 0.0
        self.sync_ok_at = 0.0
        self.pll_flags = 0     # latched event flags
        self.sync_flags = 0

    def write(self, word: int) -> None:
        addr, data = (word >> 8) & 0x7F, word & 0xFF
        if addr == 0x00 and (data & 0x20):          # soft reset
            self.regs.clear()
            self.pll_lock_at = float("inf")
            self.sync_ok_at = float("inf")
            return
        self.regs[addr] = data
        if addr == 0x06:                            # write-1-to-clear event flags
            self.pll_flags &= ~data
            return
        if addr in (0x0A, 0x0C, 0x0D):              # PLL (re)configured
            d = self.e.delay("dac_init", "mboard:0")
            self.pll_lock_at = sch.now() + d
        elif addr in (0x10, 0x18):                  # back-end sync mode / FIFO align
            d = self.e.delay("dac_sync", "mboard:0")
            self.sync_ok_at = sch.now() + d
        elif addr == 0x12:
            self.sync_flags &= ~data

    def read(self, word: int) -> int:
        addr = (word >> 8) & 0x7F
        now = sch.now()
        pll = now >= self.pll_lock_at and 0x0A in self.regs
        sync = now >= self.sync_ok_at and 0x10 in self.regs
        if addr == 0x0E:
            return 0x80 if pll else 0x00
        if addr == 0x06:                            # event flags: bit6 PLL locked, bit4 sync
            v = 0
            if pll:
                v |= 0x40
            if sync:
                v |= 0x10
            return v
        if addr == 0x12:
            return 0x40 if sync else 0x00
        if addr == 0x19:
            return 0x0F if sync else 0x00
        return self.regs.get(addr, 0)
