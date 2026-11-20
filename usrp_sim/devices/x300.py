"""X300/X310 device profile + ZPU (firmware) register model.

Register space mirrors host/lib/usrp/x300/{x300_regs.hpp,x300_fw_common.h,x300_mb_controller.cpp,
x300_claim.cpp,x300_mb_eeprom_iface.cpp}; everything the host touches through the
UDP 49152 peek/poke channel is decoded in :class:`ZpuRegs`.
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import threading
from typing import Dict, List, Optional, Tuple

from .. import chdr
from .. import scheduler as sch
from ..config import Config
from ..core import sampleio
from ..core.blocks import DdcBlock, DucBlock, RadioX300, ReplayBlock
from ..core.engine import Engine
from ..core.rfnoc import (RfnocGraph, RxChannel, SepSpec, SepNode, Topology, TxChannel)
from . import chips, dboards as dbm
from .ubx import DboardModel

log = logging.getLogger("usrp_sim.x300")

# x300_fw_common.h
SHMEM_BASE = 0x6000
SHMEM_COMPAT, SHMEM_GPSDO, SHMEM_CLAIM_STATUS, SHMEM_CLAIM_TIME, SHMEM_CLAIM_SRC, SHMEM_IDENT = 0, 1, 5, 6, 7, 13
DONT_LOOK_FOR_GPSDO = 0x1234ABCD
SET0_BASE, BOOT_LDR_BASE, I2C0_BASE, I2C1_BASE = 0xA000, 0xFA00, 0xFE00, 0xFF00
# x300_regs.hpp
SR_LEDS, SR_SW_RST, SR_CLOCK_CTRL, SR_DEVICE_ID, SR_REF_FREQ, SR_SPI = 0, 1, 2, 3, 4, 32
RB_SPI, RB_CLK_STATUS, RB_SFP0_TYPE, RB_SFP1_TYPE, RB_COMPAT, RB_RFNOC_INFO = 2, 3, 4, 5, 6, 7
RB_GIT_HASH, RB_NUM_TK, RB_FP_GPIO_SRC, RB_DEVICE_ID, SR_FP_GPIO_SRC = 10, 12, 13, 14, 72
TK_BASE, TK_STRIDE = 100, 48
X310_PCIE_SSID_ADC_33, X300_PCIE_SSID_ADC_33 = 0x76CA, 0x7736   # product ids (x300_regs.hpp)

FLAG_ACK, FLAG_ERROR, FLAG_POKE, FLAG_PEEK = 1, 2, 4, 8


class ZpuRegs:
    def __init__(self, dev: "X300Device"):
        self.d = dev
        self.e = dev.engine
        cfg = dev.cfg
        self.shmem: Dict[int, int] = {SHMEM_GPSDO: DONT_LOOK_FOR_GPSDO}
        fwmaj, fwmin = cfg.compat(cfg.fw_compat)
        self.fw_compat = (fwmaj << 16) | fwmin
        fmaj, fmin = cfg.compat(cfg.fpga_compat)
        self.fpga_compat = (fmaj << 16) | fmin
        self.set0: Dict[int, int] = {}
        self.device_id = 0
        self.claim_src = 0
        self.claim_at = -1e9
        self.claimed_since = None
        self.clock_ctrl = 0x6A        # power-on: CLK_SOURCE internal, PPS internal, TCXO + GPSDO power on (x300_regs.hpp)
        self.sw_rst = 0
        self.radio_clk_locked = True
        self.idelay_locked = True
        self.pps_toggle = 0
        self.lmk = chips.Lmk04816(self.e, cfg.master_clock_rate)
        self.lmk.on_mcr_change = dev.on_master_clock
        self.spi_ctrl = 0
        self.spi_rb = 0
        self.i2c = {0: chips.I2CCore(), 1: chips.I2CCore()}
        self.tk_event = [0, 0]                         # lo, hi
        self.tk_period = 0
        self.mb_eeprom = chips.I2CEeprom(dev.mb_eeprom_image(), os.path.join(dev._state_dir(), "mb_eeprom.bin"),
                                         addr16=True)
        self.i2c[1].slaves[0x50] = self.mb_eeprom
        self.unknown = set()

    # ---- claim (x300_claim.cpp; the firmware's claimer expires an unrefreshed claim)
    def _claimed(self) -> bool:
        if self.claim_src and sch.now() - self.claim_at <= self.d.cfg.claim_timeout_s:
            return True
        if self.claim_src:
            log.info("claim by 0x%08x expired (not refreshed for %.1f s)", self.claim_src,
                     self.d.cfg.claim_timeout_s)
            self.claim_src = 0
        return False

    # ---- peek
    def peek(self, addr: int) -> int:
        if SHMEM_BASE <= addr < SHMEM_BASE + 0x1000:
            w = (addr - SHMEM_BASE) // 4
            if w == SHMEM_COMPAT:
                return self.fw_compat
            if w == SHMEM_CLAIM_STATUS:
                return 1 if self._claimed() else 0
            if w == SHMEM_CLAIM_TIME:
                return int(self.shmem.get(w, 0))
            if w == SHMEM_CLAIM_SRC:
                return self.claim_src if self._claimed() else 0
            if SHMEM_IDENT <= w < SHMEM_IDENT + 27:     # mirrors x300_eeprom_map_t (big-endian words)
                i = (w - SHMEM_IDENT) * 4
                return struct.unpack(">I", bytes(self.mb_eeprom.mem[i:i + 4]))[0]
            return self.shmem.get(w, 0)
        if I2C0_BASE <= addr < I2C0_BASE + 0x100:
            return self.i2c[0].read(addr - I2C0_BASE)
        if I2C1_BASE <= addr < I2C1_BASE + 0x100:
            return self.i2c[1].read(addr - I2C1_BASE)
        if SET0_BASE <= addr < SET0_BASE + 0x1000:
            return self._peek_set0((addr - SET0_BASE) // 4)
        return 0

    def _peek_set0(self, r: int) -> int:
        d = self.d
        if r == RB_SPI:
            return self.spi_rb
        if r == RB_CLK_STATUS:
            v = 0
            if self.lmk.locked():
                v |= (1 << 2)
            if self.radio_clk_locked and self.lmk.locked():
                v |= (1 << 5)
            if self.idelay_locked and self.lmk.locked():
                v |= (1 << 6)
            v |= ((int(sch.now() * 4) & 1) << 4) if self.d.cfg.time_pps != "none" else 0   # PPS_DETECT toggles
            return v
        if r == RB_SFP0_TYPE:
            return 1 if d.cfg.fpga == "XG" else 0
        if r == RB_SFP1_TYPE:
            return 0 if d.cfg.fpga == "1G" else 1
        if r == RB_COMPAT:
            return self.fpga_compat
        if r == RB_RFNOC_INFO:
            return 0x0100 | (64 << 16)
        if r == RB_GIT_HASH:
            return 0x0A5E1234
        if r == RB_NUM_TK:
            return 1
        if r == RB_FP_GPIO_SRC:
            return self.set0.get(SR_FP_GPIO_SRC, 0)
        if r == RB_DEVICE_ID:
            return self.device_id
        if r >= TK_BASE and r < TK_BASE + TK_STRIDE * 12:
            return self._tk_read((r - TK_BASE) % TK_STRIDE)
        return 0

    def _tk_read(self, off: int) -> int:
        clk = self.e.clock
        if off in (0x00, 0x04):
            t = clk.ticks_now()
            return (t & 0xFFFFFFFF) if off == 0 else (t >> 32) & 0xFFFFFFFF
        if off in (0x14, 0x18):
            t = clk.ticks_last_pps()
            return (t & 0xFFFFFFFF) if off == 0x14 else (t >> 32) & 0xFFFFFFFF
        if off == 0x1C:
            return self.tk_period & 0xFFFFFFFF
        if off == 0x20:
            return self.tk_period >> 32
        return 0

    # ---- poke; returns extra reply latency in seconds
    def poke(self, addr: int, v: int) -> float:
        v &= 0xFFFFFFFF
        if SHMEM_BASE <= addr < SHMEM_BASE + 0x1000:
            w = (addr - SHMEM_BASE) // 4
            if w == SHMEM_CLAIM_TIME:
                self.claim_at = sch.now()
                self.shmem[w] = v
            elif w == SHMEM_CLAIM_SRC:
                self.claim_src = v
                self.claim_at = sch.now() if v else -1e9
            else:
                self.shmem[w] = v
            return 0.0
        if I2C0_BASE <= addr < I2C0_BASE + 0x100:
            self.i2c[0].write(addr - I2C0_BASE, v)
            return 0.0
        if I2C1_BASE <= addr < I2C1_BASE + 0x100:
            self.i2c[1].write(addr - I2C1_BASE, v)
            return 0.0
        if SET0_BASE <= addr < SET0_BASE + 0x1000:
            return self._poke_set0((addr - SET0_BASE) // 4, v)
        return 0.0            # boot loader etc.: accepted and ignored

    def _poke_set0(self, r: int, v: int) -> float:
        self.set0[r] = v
        if r == SR_CLOCK_CTRL:
            extra = 0.0
            old = self.clock_ctrl
            self.clock_ctrl = v
            names = {0: "external", 2: "internal", 3: "gpsdo"}
            if (old ^ v) & 0x3:                       # CLK_SOURCE
                self.lmk.pending_event = "clock_source"
                self.lmk.pending_old, self.lmk.pending_new = names.get(old & 3), names.get(v & 3)
                self._pending_clk = (names.get(old & 3, "?"), names.get(v & 3, "?"))
            if (old ^ v) & 0xC:                       # PPS_SELECT (time source)
                extra += self.e.delay("time_source", "mboard:0", names.get((old >> 2) & 3), names.get((v >> 2) & 3))
            return extra
        if r == SR_SW_RST:
            if v & (1 << 2):
                self.radio_clk_locked = False
            elif not (v & (1 << 2)):
                self.radio_clk_locked = True
            self.idelay_locked = not (v & (1 << 3))
            return self.e.delay("reset", "mboard:0") if v & 0x3 else 0.0     # ETH_PHY / RADIO reset
        if r == SR_DEVICE_ID:
            self.device_id = v & 0xFFFF
            return 0.0
        if r in (SR_SPI, SR_SPI + 1, SR_SPI + 2):
            k = r - SR_SPI
            if k == 1:
                self.spi_ctrl = v
            elif k == 2:
                nbits = (self.spi_ctrl >> 24) & 0x3F or 32
                word = (v >> (32 - nbits)) & ((1 << nbits) - 1) if nbits < 32 else v
                if self.spi_ctrl & 1:                 # slave 1 = LMK04816
                    self.lmk.write(word)
            return 0.0
        if TK_BASE <= r < TK_BASE + TK_STRIDE * 12:
            return self._tk_write((r - TK_BASE) % TK_STRIDE, v)
        return 0.0

    def _tk_write(self, off: int, v: int) -> float:
        clk = self.e.clock
        if off == 0x08:
            self.tk_event[0] = v
        elif off == 0x0C:
            self.tk_event[1] = v
        elif off == 0x10:
            ticks = self.tk_event[0] | (self.tk_event[1] << 32)
            if v & 1:
                clk.set_ticks_now(ticks)
            elif v & 2:
                clk.set_ticks_next_pps(ticks)
        elif off == 0x1C:
            self.tk_period = (self.tk_period & ~0xFFFFFFFF) | v
        elif off == 0x20:
            self.tk_period = (self.tk_period & 0xFFFFFFFF) | (v << 32)
            if self.tk_period:                        # period = 1e9/rate ns in Q32
                rate = 1e9 * (1 << 32) / self.tk_period
                if abs(rate - clk.tick_rate) > 1.0:
                    log.info("timekeeper tick rate -> %.6f MHz", rate / 1e6)
                    clk.set_tick_rate(rate)
                    self.d.on_master_clock(None, rate)
        return 0.0


class X300Device:
    """Assembles engine + graph + ZPU model for the x310/x300 profiles."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = Engine(cfg)
        self.engine.clock.set_tick_rate(cfg.master_clock_rate)
        os.makedirs(self._state_dir(), exist_ok=True)
        self.zpu = None
        self.is_x310 = cfg.profile != "x300"
        self.radios: List[RadioX300] = []
        self.dboard_models: Dict[str, DboardModel] = {}
        self.zpu = ZpuRegs(self)
        self._build()

    # ---- state / eeprom
    def _state_dir(self) -> str:
        sd = self.cfg.state_dir
        if not os.path.isabs(sd):
            sd = os.path.join(self.cfg.base_dir, sd)
        return os.path.join(sd, self.cfg.bind.replace(".", "_"))

    def mb_eeprom_image(self) -> bytearray:
        c = self.cfg
        ips = [c.bind, "192.168.20.2", "192.168.30.2", "192.168.40.2"]
        img = dbm.mb_eeprom_image(X310_PCIE_SSID_ADC_33 if self.is_x310 else X300_PCIE_SSID_ADC_33,
                                  7, 7, c.name, c.serial, ips)
        return img

    # ---- construction
    def _build(self):
        cfg, e = self.cfg, self.engine
        ddc = [DdcBlock(e, i) for i in range(2)]
        duc = [DucBlock(e, i) for i in range(2)]
        radio = [RadioX300(e, i) for i in range(2)]
        replay = ReplayBlock(e, 0)
        self.radios = radio
        blocks = [duc[0], ddc[0], radio[0], duc[1], ddc[1], radio[1], replay]
        edges = [
            ("ep", 0, 0, "blk", 0, 0), ("blk", 0, 0, "blk", 2, 0), ("blk", 2, 0, "blk", 1, 0),
            ("blk", 1, 0, "ep", 0, 0), ("blk", 2, 1, "blk", 1, 1), ("blk", 1, 1, "ep", 1, 0),
            ("ep", 2, 0, "blk", 3, 0), ("blk", 3, 0, "blk", 5, 0), ("blk", 5, 0, "blk", 4, 0),
            ("blk", 4, 0, "ep", 2, 0), ("blk", 5, 1, "blk", 4, 1), ("blk", 4, 1, "ep", 3, 0),
            ("ep", 4, 0, "blk", 6, 0), ("blk", 6, 0, "ep", 4, 0), ("ep", 5, 0, "blk", 6, 1),
            ("blk", 6, 1, "ep", 5, 0),
        ]
        txb = cfg.tx_buffer_bytes
        seps = [SepSpec(True, True, txb), SepSpec(False, True, 0), SepSpec(False, True, txb),
                SepSpec(False, True, 0), SepSpec(False, True, 4096), SepSpec(False, True, 4096)]
        topo = Topology(seps, blocks, edges, device_type=0xA300, num_xports=1)   # one transport: the bound interface
        self.graph = RfnocGraph(e, topo, lambda: self.zpu.device_id)
        # daughterboards: I2C EEPROMs + behaviour models
        i2c1 = self.zpu.i2c[1]
        for slot, r in (("A", radio[0]), ("B", radio[1])):
            db = cfg.dboards[slot]
            rx_id, tx_id, fam = dbm.DBOARDS[db.id]
            if fam == "empty":
                continue
            off = 0x0 if slot == "A" else 0x2
            for name, base, bid in (("rx", dbm.RX_EEPROM, rx_id), ("tx", dbm.TX_EEPROM, tx_id)):
                addr = dbm.BASE_ADDR | (base + off)
                img = dbm.dboard_eeprom_image(bid, db.serial, db.rev)
                path = os.path.join(self._state_dir(), "db%s_%s_eeprom.bin" % (slot, name))
                i2c1.slaves[addr] = chips.I2CEeprom(img, path)
            model = DboardModel(e, slot, db.id)
            model.mcr = cfg.master_clock_rate
            r.dboard = model
            self.dboard_models[slot] = model
        self._wire_channels(ddc, duc, radio)

    def _wire_channels(self, ddc, duc, radio):
        cfg, g = self.cfg, self.graph
        blocks = self.graph.blocks
        for r in range(2):
            for port in range(2):
                name = "%s:%d" % ("AB"[r], port)
                chain = g.downstream_chain("blk", 2 + 3 * r, port)
                sep_ref = chain[-1]
                if sep_ref[0] != "ep":
                    continue
                sep = g.seps[sep_ref[1]]
                dd = next((blocks[c[1]] for c in chain if c[0] == "blk" and isinstance(blocks[c[1]], DdcBlock)), None)
                io = cfg.rx.get(name)
                rch = RxChannel(g, name, radio[r], port, sep, dd, lambda: None)
                rch.source_factory = (lambda io=io, rr=rch: sampleio.make_source(io, rr.rate))
                if io is not None:       # configured channels open their file/socket immediately, so
                    rch.source = rch.source_factory()     # that senders can start before the stream does
                radio[r].rx_streams[port] = rch
                sep.rx = rch
                g.rx_channels[name] = rch
            # TX: ep0 -> duc0 -> radio0 ; ep2 -> duc1 -> radio1  (port 0 only)
            sep = g.seps[2 * r]
            name = "%s:0" % "AB"[r]
            tch = TxChannel(g, name, radio[r], 0, sep, duc[r], sampleio.make_sink(cfg.tx.get(name)))
            sep.tx = tch
            g.tx_channels[name] = tch

    # ---- callbacks
    def on_master_clock(self, old: Optional[float], new: float):
        if old is not None:
            log.info("master clock %.6f MHz -> %.6f MHz", old / 1e6, new / 1e6)
            self.engine.clock.set_tick_rate(new)
        for r in self.radios:
            r.set_master_clock(new)

    # ---- front-end helpers
    def fw_request(self, data: bytes) -> Tuple[Optional[bytes], float]:
        """Handle one 16-byte x300_fw_comms_t. Returns (reply, extra_latency_s)."""
        if len(data) != 16:
            return None, 0.0
        flags, seq, addr, val = struct.unpack(">IIII", data)
        extra = self.engine.delay("reg_access", "mboard:0")
        if flags & FLAG_POKE:
            extra += self.zpu.poke(addr, val) or 0.0
            log.log(5, "poke 0x%05x <- 0x%08x", addr, val)
            return struct.pack(">IIII", flags | FLAG_ACK, seq, addr, val), extra
        if flags & FLAG_PEEK:
            r = self.zpu.peek(addr) & 0xFFFFFFFF
            log.log(5, "peek 0x%05x -> 0x%08x", addr, r)
            return struct.pack(">IIII", flags | FLAG_ACK, seq, addr, r), extra
        return data, 0.0       # discovery ping (flags == ACK): echo flags + sequence

    def close(self):
        self.engine.closed.set()       # wake every stream thread first, then join them
        self.graph.close()
        self.engine.close()

    def print_stats(self, out=None):
        for name, st in sorted(self.engine.stats.items()):
            line = "%-10s %s" % (name, st.line())
            (out.write(line + "\n") if out else log.info(line))
