"""Tier 2: generic / custom device behind the MPMD protocol.

The X300 client driver hard-codes X310 hardware, so a user-defined channel count only makes
sense where UHD *asks the device* what it has: MPMD.  The device presents R RFNoC "radio" slices
(DUC + radio + DDC, two ports each, E320-style AD9361 controlled over RPC `db_0_*`), so
``channels = N`` gives ``ceil(N/2)`` slices.  Radios reuse the RadioX300 register model
(radio_control_impl registers are common to all UHD radios); chip-level control is served by the
RPC methods below (host/lib/usrp/dboard/e3xx/e3xx_ad9361_iface.cpp).
"""
from __future__ import annotations

import logging
import math
import socket
import struct
import threading
from typing import Dict, List

from .. import scheduler as sch
from ..config import Config
from ..core import sampleio
from ..core.blocks import DdcBlock, DucBlock, RadioX300, register_block
from ..core.engine import Engine
from ..core.rfnoc import RfnocGraph, RxChannel, SepSpec, Topology, TxChannel
from ..frontends import mpmd

log = logging.getLogger("usrp_sim.custom")

MPM_COMPAT = [5, 3]          # host/lib/usrp/mpmd/mpmd_impl.cpp MPM_COMPAT_NUM
E320_DEVICE_TYPE = 0xE320    # rfnoc::E320 (defaults.hpp) -> e320_radio_control_impl is instantiated
DEFAULT_LINK_RATE = 1e9


class RadioMpmd(RadioX300):
    """Same radio register file; peripheral cores (GPIO/ATR/LEDs) are accepted and stored."""

    def __init__(self, engine, index, params=None):
        super().__init__(engine, index, params)
        self.dboard = None

    def read(self, addr: int) -> int:
        # e3xx_radio_control_impl.cpp codec loopback self-test: the RX data word mirrors the TX idle word
        if self.BASE <= addr < self.BASE + 2 * self.CHAN and (addr - self.BASE) % self.CHAN == 0x3C:
            return self.regs.get(addr + 0x04, 0)
        return super().read(addr)


class CustomDevice:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = Engine(cfg)
        self.engine.clock.set_tick_rate(cfg.master_clock_rate)
        self.nrx, self.ntx = cfg.custom["rx_channels"], cfg.custom["tx_channels"]
        self.nrad = int(math.ceil(max(self.nrx, self.ntx) / 2))
        self.device_id = 1
        self.mcr = cfg.master_clock_rate
        self.product = cfg.custom.get("product", "e320")
        self.freqs: Dict[str, float] = {}
        self.gains: Dict[str, float] = {}
        self.sources = {}
        self._build()
        self.methods = self._make_methods()

    # ---- RFNoC image: per slice  ep(2r),ep(2r+1) <-> duc_r/radio_r/ddc_r
    def _build(self):
        e = self.engine
        ducs = [DucBlock(e, r) for r in range(self.nrad)]
        ddcs = [DdcBlock(e, r) for r in range(self.nrad)]
        for d in ducs:
            d.NUM_IN = d.NUM_OUT = 2
        for d in ddcs:
            d.NUM_IN = d.NUM_OUT = 2
        radios = [RadioMpmd(e, r) for r in range(self.nrad)]
        blocks, edges = [], []
        for r in range(self.nrad):
            b0 = len(blocks)
            blocks += [ducs[r], ddcs[r], radios[r]]
            duc, ddc, rad = b0, b0 + 1, b0 + 2
            for p in range(2):
                ep = 2 * r + p
                edges += [("ep", ep, 0, "blk", duc, p), ("blk", duc, p, "blk", rad, p),
                          ("blk", rad, p, "blk", ddc, p), ("blk", ddc, p, "ep", ep, 0)]
        txb = self.cfg.tx_buffer_bytes
        seps = [SepSpec(i == 0, True, txb) for i in range(2 * self.nrad)]
        topo = Topology(seps, blocks, edges, device_type=E320_DEVICE_TYPE, num_xports=1)
        self.graph = RfnocGraph(e, topo, lambda: self.device_id)
        self.radios, self.ducs, self.ddcs = radios, ducs, ddcs
        for r in range(self.nrad):
            for p in range(2):
                ch = 2 * r + p
                sep = self.graph.seps[ch]
                chain = self.graph.downstream_chain("blk", 3 * r + 2, p)
                if self.nrx > ch:
                    io = self.cfg.rx.get("c%d" % ch)
                    rch = RxChannel(self.graph, "c%d" % ch, radios[r], p, sep, ddcs[r], lambda: None)
                    rch.source_factory = (lambda io=io, rr=rch: sampleio.make_source(io, rr.rate))
                    if io is not None:
                        rch.source = rch.source_factory()
                    radios[r].rx_streams[p] = rch
                    sep.rx = rch
                    self.graph.rx_channels["c%d" % ch] = rch
                if self.ntx > ch:
                    tch = TxChannel(self.graph, "c%d" % ch, radios[r], p, sep, ducs[r],
                                    sampleio.make_sink(self.cfg.tx.get("c%d" % ch)))
                    sep.tx = tch
                    self.graph.tx_channels["c%d" % ch] = tch

    # ---- discovery / device info
    def discovery_fields(self):
        return dict(type="mpm", product=self.product, serial=self.cfg.serial, name=self.cfg.name,
                    fpga=self.cfg.fpga)

    def device_info(self):
        return {"type": "e3xx", "product": self.product, "serial": self.cfg.serial, "name": self.cfg.name,
                "rev": "3", "pid": "0xe320", "mpm_version": "%d.%d.0" % tuple(MPM_COMPAT),
                "fpga_version": "7.3", "fpga": self.cfg.fpga, "addr": self.cfg.bind, "claimed": "False",
                "eeprom_version": "3", "mpm_sw_version": "usrp-sim",
                "chdr_width": "64", "simulated": "True"}

    # ---- RPC surface (client: rpc.py mpmd_rpc iface + mpmd_mb_iface + e3xx_ad9361_iface)
    def rpc_methods(self):
        return self.methods

    def _make_methods(self):
        clk = self.engine.clock
        e = self.engine
        m = {}
        # --- identity / init
        m["get_device_info"] = lambda: self.device_info()
        m["get_dboard_info"] = lambda: [{"pid": "0x0110", "serial": "DB0001", "rev": "1", "eeprom_version": "0"}]
        m["get_mpm_compat_num"] = lambda: list(MPM_COMPAT)
        m["get_proto_ver"] = lambda: 0x0100
        m["get_chdr_width"] = lambda: 64
        m["get_init_status"] = lambda: ["true", ""]
        m["init"] = lambda args=None: True
        m["get_log_buf"] = lambda: []
        m["pop_host_tasks"] = lambda task="": []
        m["list_updateable_components"] = lambda: []
        m["get_device_id"] = lambda: self.device_id
        m["set_device_id"] = self._set_device_id
        m["reset_timer_and_mgr"] = lambda: None
        m["supports_feature"] = lambda f: False
        m["get_mb_eeprom"] = lambda: {"serial": self.cfg.serial, "pid": "0xe320", "rev": "3"}
        m["get_db_eeprom"] = lambda slot=0: {"pid": "0x0110", "serial": "DB0001", "rev": "1"}
        m["set_db_eeprom"] = lambda slot=0, data=None: None
        m["set_channel_mode"] = lambda mode="": None
        # --- links / clocks
        m["synchronize"] = lambda data=None, finalize=False: {}
        m["aggregate_sync_data"] = lambda data=None: {}
        m["get_chdr_xport_adapters"] = lambda: {}
        m["add_remote_chdr_route"] = lambda *a: 0
        m["get_chdr_link_types"] = lambda: ["udp"]
        m["get_chdr_link_options"] = lambda t="udp": ([{"ipv4": self.cfg.bind, "port": "49153",
                                                         "link_rate": str(int(DEFAULT_LINK_RATE)),
                                                         "type": "sfp0", "mtu": "8000"}]
                                                       if t == "udp" else [])
        m["get_clocks"] = lambda: [{"name": "radio_clk", "freq": str(self.mcr), "mutable": "true"},
                                   {"name": "bus_clk", "freq": str(200e6)},
                                   {"name": "ctrl_clk", "freq": str(40e6)}]
        # --- timekeeper / sources / sensors
        m["get_num_timekeepers"] = lambda: 1
        m["get_timekeeper_time"] = lambda idx, last_pps: clk.ticks_last_pps() if last_pps else clk.ticks_now()
        m["set_timekeeper_time"] = self._set_tk_time
        m["set_tick_period"] = self._set_tick_period
        self.time_src, self.clk_src = "internal", "internal"
        m["get_time_sources"] = lambda: ["internal", "external"]
        m["get_clock_sources"] = lambda: ["internal", "external"]
        m["get_time_source"] = lambda: self.time_src
        m["get_clock_source"] = lambda: self.clk_src
        m["set_time_source"] = self._set_time_src
        m["set_clock_source"] = self._set_clk_src
        m["set_sync_source"] = lambda s: (self._set_clk_src(s["clock_source"]) if "clock_source" in s else None,
                                          self._set_time_src(s["time_source"]) if "time_source" in s else None)[0]
        m["get_sync_source"] = lambda: {"clock_source": self.clk_src, "time_source": self.time_src}
        m["get_sync_sources"] = lambda: [{"clock_source": "internal", "time_source": "internal"},
                                         {"clock_source": "external", "time_source": "external"}]
        m["set_clock_source_out"] = lambda enb: None
        m["set_trigger_io"] = lambda d: None
        m["get_mb_sensors"] = lambda: ["ref_locked"]
        m["get_mb_sensor"] = lambda name: {"name": name, "type": "BOOLEAN", "unit": "locked", "value": "true"}
        m["get_gpio_banks"] = lambda: []
        m["get_gpio_srcs"] = lambda bank="": []
        m["get_gpio_src"] = lambda bank="": []
        m["set_gpio_src"] = lambda bank="", src=None: None
        # --- AD9361 ("db_0_*")
        m["db_0_get_master_clock_rate"] = lambda: self.mcr
        m["db_0_set_catalina_clock_rate"] = self._set_mcr
        m["db_0_set_bw_filter"] = self._set_bw
        m["db_0_set_gain"] = self._set_gain
        m["db_0_set_agc"] = lambda which, on: None
        m["db_0_set_agc_mode"] = lambda which, mode: None
        m["db_0_set_active_chains"] = lambda tx1, tx2, rx1, rx2: None
        m["db_0_catalina_tune"] = self._tune
        m["db_0_set_dc_offset_auto"] = lambda which, on: None
        m["db_0_set_iq_balance_auto"] = lambda which, on: None
        m["db_0_set_timing_mode"] = lambda mode: None
        m["db_0_get_freq"] = lambda which: self.freqs.get(which, 0.0)
        m["db_0_data_port_loopback"] = lambda on: None
        m["db_0_get_rssi"] = lambda which: {"name": "rssi", "type": "REALNUM", "unit": "dB", "value": "-60.0"}
        m["db_0_get_temperature"] = lambda: {"name": "temp", "type": "REALNUM", "unit": "C", "value": "40.0"}
        m["db_0_get_filter_names"] = lambda which: []
        m["db_0_output_digital_test_tone"] = lambda on: None
        m["db_0_get_sensors"] = lambda d="RX": ["rssi", "lo_locked", "ad9361_temperature"]
        m["db_0_get_sensor"] = self._get_sensor
        self.lock_at: Dict[str, float] = {}
        return m

    def _get_sensor(self, d, name, chan=0):
        if name == "lo_locked":
            key = "%s%d" % (d, int(chan) + 1)
            locked = sch.now() >= self.lock_at.get(key, 0.0)
            return {"name": name, "type": "BOOLEAN", "unit": "locked", "value": str(locked).lower()}
        if name == "rssi":
            return {"name": name, "type": "REALNUM", "unit": "dB", "value": "-60.0"}
        return {"name": name, "type": "REALNUM", "unit": "C", "value": "40.0"}

    # ---- RPC implementations with configurable delays
    def _set_device_id(self, dev_id):
        self.device_id = int(dev_id)

    def _set_tk_time(self, idx, ticks, next_pps):
        (self.engine.clock.set_ticks_next_pps if next_pps else self.engine.clock.set_ticks_now)(int(ticks))

    def _set_tick_period(self, idx, period_ns):
        # period is Q32 ns (mb_controller.cpp); tick rate = 1e9 * 2^32 / period
        if period_ns:
            self.engine.clock.set_tick_rate(1e9 * (1 << 32) / period_ns)

    def _set_time_src(self, src):
        d = self.engine.delay("time_source", "mboard:0", self.time_src, src)
        self.time_src = src
        self.engine.sched.wait_until(sch.now() + d)

    def _set_clk_src(self, src):
        d = self.engine.delay("clock_source", "mboard:0", self.clk_src, src)
        self.clk_src = src
        self.engine.sched.wait_until(sch.now() + d)

    def _set_mcr(self, rate):
        rate = float(rate)
        d = self.engine.delay("lmk_reprogram", "mboard:0", self.mcr, rate)
        self.engine.sched.wait_until(sch.now() + d)
        self.mcr = rate
        self.engine.clock.set_tick_rate(rate)
        return rate

    def _tune(self, which, freq):
        which = str(which)
        d = self.engine.delays.lo_retune("%s:%s" % (which[:2].lower(), which[2:] or "0"),
                                         self.freqs.get(which, 0.0), freq)
        self.freqs[which] = float(freq)
        self.lock_at[which] = sch.now() + d        # lo_locked sensor follows the delay
        return float(freq)

    def _set_bw(self, which, bw):
        d = self.engine.delay("bandwidth", "%s:%s" % (str(which)[:2].lower(), str(which)[2:] or "0"), None, bw)
        self.engine.sched.wait_until(sch.now() + d)
        return float(bw)

    def _set_gain(self, which, gain):
        d = self.engine.delay("gain_change", "%s:%s" % (str(which)[:2].lower(), str(which)[2:] or "0"),
                              self.gains.get(which), gain)
        self.gains[which] = float(gain)
        self.engine.sched.wait_until(sch.now() + d)
        return float(gain)

    def close(self):
        self.engine.closed.set()
        self.graph.close()
        self.engine.close()

    def print_stats(self, out=None):
        for name, st in sorted(self.engine.stats.items()):
            line = "%-10s %s" % (name, st.line())
            (out.write(line + "\n") if out else log.info(line))


class ChdrUdp:
    """CHDR data plane of the MPMD device: UDP 49153 on the bound address."""

    def __init__(self, dev: CustomDevice):
        self.dev = dev
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, opt, 16 << 20)
            except OSError:
                pass
        try:
            self.sock.bind((dev.cfg.bind, 49153))
        except OSError as e:
            raise OSError("cannot bind %s:49153 (%s)" % (dev.cfg.bind, e))
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        dev.engine.set_sender(lambda data, addr: self.sock.sendto(data, addr))
        self.t = threading.Thread(target=self._loop, daemon=True, name="udp-chdr")

    def start(self):
        self.t.start()

    def _loop(self):
        buf = bytearray(70000)
        mv = memoryview(buf)
        handle = self.dev.graph.handle_packet
        while not self.stop.is_set():
            try:
                n, addr = self.sock.recvfrom_into(buf)
            except socket.timeout:
                continue
            except OSError:
                return
            handle(bytes(mv[:n]), addr)

    def close(self):
        self.stop.set()
        self.t.join(timeout=1.5)
        self.sock.close()
