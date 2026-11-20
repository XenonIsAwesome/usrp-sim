"""NoC block register models and the block registry (noc_id -> class).

Each block answers CHDR ctrl register reads/writes (`read(addr)`, `write(addr, data)`).
`write()` may return extra reply latency in seconds (configuration delays that are
visible to the host as the time a register access takes).

Register maps mirror the host drivers: host/lib/rfnoc/{radio_control_impl,ddc_block_control,
duc_block_control}.cpp, host/lib/usrp/x300/x300_radio_control.cpp (x300_regs namespace).
To add a block: subclass NocBlock, set NOC_ID/ports and decorate with @register_block.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Optional, Type

from .. import chdr
from .. import scheduler as sch
from ..devices import chips
from ..devices.ubx import DboardModel
from .engine import Engine

log = logging.getLogger("usrp_sim.blocks")

BLOCK_REGISTRY: Dict[int, Type["NocBlock"]] = {}


def register_block(cls):
    BLOCK_REGISTRY[cls.NOC_ID] = cls
    return cls


class NocBlock:
    NOC_ID = 0
    NAME = "block"
    NUM_IN = 1
    NUM_OUT = 1
    CTRL_FIFO_LOG2 = 5          # ctrl fifo = 2**5 entries
    MAX_ASYNC = 0
    DATA_MTU_LOG2 = 10          # 2**10 * 8 bytes = 8192 B (x310_rfnoc_image_core: BYTE_MTU)
    PROTOVER = 0

    def __init__(self, engine: Engine, index: int, params: Optional[dict] = None):
        self.e = engine
        self.index = index                 # instance number among blocks of this type
        self.params = params or {}
        self.regs: Dict[int, int] = {}     # generic write-back store
        self.graph = None                  # set by the graph
        self.name = "%s%d" % (self.NAME, index)

    def read(self, addr: int) -> int:
        return self.regs.get(addr, 0)

    def write(self, addr: int, data: int) -> float:
        self.regs[addr] = data
        return 0.0

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------- DDC/DUC
class _RateBlock(NocBlock):
    """DDC/DUC: per-channel registers at chan*2048; decimation/interpolation rate."""
    CHAN_OFFSET = 2048
    RATE_REG = 0
    MAJOR, MINOR = 0, 1

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rate = [1] * self.NUM_IN

    def read(self, addr):
        off = addr % self.CHAN_OFFSET
        if addr == 0:                       # RB_COMPAT_NUM (read first)
            return (self.MAJOR << 16) | self.MINOR
        if off == 8:                        # RB_NUM_HB
            return 2
        if off == 16:                       # RB_CIC_MAX_DECIM / INTERP
            return 255
        return self.regs.get(addr, 0)

    def write(self, addr, data):
        self.regs[addr] = data
        chan, off = addr // self.CHAN_OFFSET, addr % self.CHAN_OFFSET
        if off == self.RATE_REG and chan < len(self.rate):          # SR_N_ADDR (DDC) / SR_M_ADDR (DUC)
            old, new = self.rate[chan], max(1, data)
            self.rate[chan] = new
            if old != new:
                key = "%s:%d" % (self.DIRKEY, self.index)
                return self.e.delay("dsp_rate", key, self.e.clock.tick_rate / old,
                                    self.e.clock.tick_rate / new)
        return 0.0


@register_block
class DdcBlock(_RateBlock):
    NOC_ID = 0xDDC00000
    NAME = "ddc"
    DIRKEY = "rx"
    NUM_IN = NUM_OUT = 2
    RATE_REG = 128 * 8          # SR_N_ADDR: set_decim() writes SR_N = decim


@register_block
class DucBlock(_RateBlock):
    NOC_ID = 0xD0C00000
    NAME = "duc"
    DIRKEY = "tx"
    NUM_IN = NUM_OUT = 1
    RATE_REG = 129 * 8          # SR_M_ADDR: set_interp() writes SR_M = interp


@register_block
class ReplayBlock(NocBlock):
    """Register-compatible stub: accepts writes, reads back what was written."""
    NOC_ID = 0x4E91A000
    NAME = "replay"
    NUM_IN = NUM_OUT = 2

    STRIDE = 128            # REPLAY_BLOCK_OFFSET (replay_block_control.cpp)

    def read(self, addr):
        off = addr % self.STRIDE
        if off == 0x00:                      # REG_COMPAT_ADDR: major 1, minor 2
            return (1 << 16) | 2
        if off == 0x04:                      # REG_MEM_SIZE_ADDR: word size 64 bit, 2**30 B
            return (64 << 16) | 30
        if off == 0x64:                      # REG_PLAY_CMD_FIFO_SPACE_ADDR
            return 32
        return super().read(addr)


# ---------------------------------------------------------------------------- Radio
class SpiCore:
    """simple_spi_core as used by spi_core_3000.cpp: div @+0, ctrl @+1*stride, data @+2*stride;
    writing the data register runs the transaction; RB holds the MISO word."""

    def __init__(self, slaves: Dict[int, Callable[[int, int, int], int]]):
        self.slaves = slaves        # mask bit -> fn(word, nbits, edge_flags) -> miso
        self.ctrl = 0
        self.rb = 0

    def write_data(self, data: int) -> None:
        nbits = (self.ctrl >> 24) & 0x3F or 32
        word = (data >> (32 - nbits)) & ((1 << nbits) - 1) if nbits < 32 else data & 0xFFFFFFFF
        mask = self.ctrl & 0xFFFFFF
        rb = 0
        for bit, fn in self.slaves.items():
            if mask & bit:
                rb = fn(word, nbits, self.ctrl >> 30) or rb
        self.rb = rb & 0xFFFFFFFF


@register_block
class RadioX300(NocBlock):
    """X300/X310 radio block incl. its peripheral cores (x300_regs namespace)."""
    NOC_ID = 0x12AD1000
    NAME = "radio"
    NUM_IN = NUM_OUT = 2
    MAX_ASYNC = 4
    MAJOR, MINOR = 0, 1
    # x300_regs (x300_radio_control.cpp), PERIPH_BASE 0x80000, stride 8
    PB, ST = 0x80000, 8
    SR_MISC_OUTS, SR_SPI, SR_LEDS = PB + 160 * ST, PB + 168 * ST, PB + 176 * ST
    SR_FP_GPIO, SR_DB_GPIO = PB + 184 * ST, PB + 192 * ST
    RB_MISC_IO, RB_SPI, RB_DB_GPIO, RB_FP_GPIO = PB + 16 * ST, PB + 17 * ST, PB + 19 * ST, PB + 20 * ST
    SR_TX_FE, SR_RX_FE = PB + 208 * ST, PB + 224 * ST
    # SPI slave selects (x300_regs.hpp)
    DB_DAC_SEN, DB_ADC_SEN, DB_RX_SEN, DB_TX_SEN = 1 << 7, 1 << 6, 1 << 1, 1 << 0
    # radio regmap
    BASE, CHAN = 0x1000, 128

    def __init__(self, engine, index, params=None):
        super().__init__(engine, index, params)
        self.dboard: Optional[DboardModel] = None
        self.adc = chips.Ads62p48(engine)
        self.dac = chips.Ad9146(engine)
        self.spi = SpiCore({
            self.DB_ADC_SEN: lambda w, n, f: self.adc.write(w) or 0,
            self.DB_DAC_SEN: self._dac_xfer,
            self.DB_TX_SEN: lambda w, n, f: self._db_spi("tx", w, n),
            self.DB_RX_SEN: lambda w, n, f: self._db_spi("rx", w, n),
        })
        self.misc_outs = 0
        self.gpio = {"db": [0] * 6, "fp": [0] * 6, "leds": [0] * 6}
        # per-channel RX state
        self.rx_max_words = [0] * self.NUM_OUT
        self.rx_has_time = [1] * self.NUM_OUT      # RX packets carry timestamps by default
        self.rx_cmd_time = [0] * self.NUM_OUT
        self.rx_cmd_words = [0] * self.NUM_OUT
        self.rx_err = [dict() for _ in range(self.NUM_OUT)]
        self.tx_err = [dict() for _ in range(self.NUM_IN)]
        self.rx_streams: List = [None] * self.NUM_OUT     # RxChannel, set by the graph
        self.lock = threading.Lock()
        self._extra_latency = 0.0       # chips behind the SPI cores add reply latency here (adc_init ...)
        self.adc.on_reset = lambda: self.add_latency(self.e.delay("adc_init", "mboard:0"))

    # ---- helpers
    def _dac_xfer(self, w, n, f):
        if w & 0x8000:
            return self.dac.read(w)
        self.dac.write(w)
        return 0

    def _db_spi(self, which, w, n):
        if self.dboard is not None:
            self.dboard.spi_write(which, w, n)
        return 0

    def set_master_clock(self, rate: float):
        if self.dboard is not None:
            self.dboard.mcr = rate

    def add_latency(self, seconds: float) -> None:
        self._extra_latency += seconds

    # ---- register access
    def read(self, addr: int) -> int:
        if addr == 0x00:
            return (self.MAJOR << 16) | self.MINOR            # REG_COMPAT_NUM
        if addr in (0x04, 0x08):
            t = self.e.clock.ticks_now()
            return (t & 0xFFFFFFFF) if addr == 0x04 else (t >> 32)
        if addr == self.BASE + 4:                              # REG_RADIO_WIDTH: 32-bit (sc16) samples, 1 spc
            return (32 << 16) | 1
        if self.BASE <= addr < self.BASE + 2 * self.CHAN:
            chan, off = (addr - self.BASE) // self.CHAN, (addr - self.BASE) % self.CHAN
            if off == 0x3C:                                    # REG_RX_DATA: ADC readback
                return self.adc.rx_word() if chan == 0 else 0
            if off == 0x10:                                    # REG_RX_STATUS
                return 0
            return self.regs.get(addr, 0)
        if addr == self.RB_MISC_IO:
            return 0                                           # low word of the 64-bit register
        if addr == self.RB_MISC_IO + 4:
            return self._misc_ins_hi()
        if addr == self.RB_SPI:
            return self.spi.rb
        if addr == self.RB_DB_GPIO:
            return self.dboard.gpio_readback() if self.dboard else 0
        if addr == self.RB_FP_GPIO:
            return self.regs.get(self.SR_FP_GPIO, 0)
        return self.regs.get(addr, 0)

    def _misc_ins_hi(self) -> int:
        """ADC checker status bits [39:32] (x300_radio_control.cpp misc_ins_reg_t)."""
        en = (self.misc_outs >> 9) & 1
        i_lock = int(en and self.adc.pat_a == "ramp")
        q_lock = int(en and self.adc.pat_b == "ramp")
        return (q_lock << 0) | (i_lock << 1) | (q_lock << 2) | (i_lock << 3)   # error bits stay 0

    def write(self, addr: int, data: int) -> float:
        data &= 0xFFFFFFFF
        extra = 0.0
        if self.BASE <= addr < self.BASE + 2 * self.CHAN:
            chan, off = (addr - self.BASE) // self.CHAN, (addr - self.BASE) % self.CHAN
            self.regs[addr] = data
            extra = self._chan_write(chan, off, data)
        elif addr == self.SR_MISC_OUTS:
            self.misc_outs = data
            self.regs[addr] = data
        elif self.SR_SPI <= addr <= self.SR_SPI + 2 * self.ST:
            k = (addr - self.SR_SPI) // self.ST
            if k == 1:
                self.spi.ctrl = data
            elif k == 2:
                self.spi.write_data(data)
            self.regs[addr] = data
        elif self.SR_DB_GPIO <= addr < self.SR_DB_GPIO + 6 * self.ST:
            k = (addr - self.SR_DB_GPIO) // self.ST
            old = self.gpio["db"][k]
            self.gpio["db"][k] = data
            self.regs[addr] = data
            if k == 0 and self.dboard is not None and old != data:       # IDLE register = GPIO output
                extra += self.dboard.gpio_out("tx", (old >> 16) & 0xFFFF, (data >> 16) & 0xFFFF)
                extra += self.dboard.gpio_out("rx", old & 0xFFFF, data & 0xFFFF)
        else:
            self.regs[addr] = data
        extra += self._extra_latency
        self._extra_latency = 0.0
        return extra

    def _chan_write(self, chan, off, data) -> float:
        if off == 0x14:                                         # REG_RX_CMD
            self.rx_command(chan, data)
        elif off == 0x18:
            self.rx_cmd_words[chan] = (self.rx_cmd_words[chan] & ~0xFFFFFFFF) | data
        elif off == 0x1C:
            self.rx_cmd_words[chan] = (self.rx_cmd_words[chan] & 0xFFFFFFFF) | (data << 32)
        elif off == 0x20:
            self.rx_cmd_time[chan] = (self.rx_cmd_time[chan] & ~0xFFFFFFFF) | data
        elif off == 0x24:
            self.rx_cmd_time[chan] = (self.rx_cmd_time[chan] & 0xFFFFFFFF) | (data << 32)
        elif off == 0x28:
            self.rx_max_words[chan] = data
        elif off == 0x2C:
            self.rx_err[chan]["port"] = data
        elif off == 0x30:
            self.rx_err[chan]["rem_port"] = data
        elif off == 0x34:
            self.rx_err[chan]["rem_epid"] = data
        elif off == 0x38:
            self.rx_err[chan]["addr"] = data
        elif off == 0x70:
            self.rx_has_time[chan] = data & 1
        elif off == 0x48:
            self.tx_err[chan]["port"] = data
        elif off == 0x4C:
            self.tx_err[chan]["rem_port"] = data
        elif off == 0x50:
            self.tx_err[chan]["rem_epid"] = data
        elif off == 0x54:
            self.tx_err[chan]["addr"] = data
        return 0.0

    def rx_command(self, chan: int, word: int) -> None:
        """REG_RX_CMD: [1:0] mode (0 stop, 1 finite, 2 continuous), bit 31 = timed."""
        mode, timed = word & 0x3, bool(word >> 31)
        st = self.rx_streams[chan]
        if st is None:
            log.warning("%s: RX command on unconnected channel %d", self.name, chan)
            return
        st.command(mode, timed, self.rx_cmd_time[chan], self.rx_cmd_words[chan])

    # ---- async messages (late command / overrun) to the host, see radio_control_impl.cpp
    def send_async(self, kind: str, chan: int, code: int) -> None:
        cfg = (self.rx_err if kind == "rx" else self.tx_err)[chan]
        if "rem_epid" not in cfg:
            return
        self.graph.send_async_ctrl(cfg["rem_epid"], cfg.get("rem_port", 0),
                                   cfg.get("addr", 0), [code], self.e.clock.ticks_now())
