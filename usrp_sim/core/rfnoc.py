"""RFNoC graph: transport/crossbar/stream-endpoint nodes (management packets), client-zero
registers, CHDR control routing to NoC blocks, flow-controlled RX/TX streaming.

Logic reused from mpm/python/usrp_mpm/simulator/ (mgmt hop handling, xbar, stream endpoint
flow control, client-zero registers) but re-implemented on top of usrp_sim.chdr (no libuhd).
Reference for client behaviour: host/lib/rfnoc/{mgmt_portal,client_zero,chdr_*}.cpp.
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .. import chdr
from .. import scheduler as sch
from ..config import Config
from . import sampleio
from .blocks import NocBlock, RadioX300, DdcBlock, DucBlock
from .engine import Engine

log = logging.getLogger("usrp_sim.rfnoc")

NODE_XBAR, NODE_SEP, NODE_XPORT = 1, 2, 3
# stream endpoint register map (mgmt_portal.cpp)
REG_EPID_SELF, REG_RESET_FLUSH, REG_OSTRM_CTRL_STATUS, REG_OSTRM_DST_EPID = 0x00, 0x04, 0x08, 0x0C
REG_OSTRM_FC_FREQ_BYTES_LO, REG_OSTRM_FC_FREQ_BYTES_HI = 0x10, 0x14
REG_OSTRM_FC_FREQ_PKTS, REG_OSTRM_FC_HEADROOM = 0x18, 0x1C
REG_OSTRM_CAP_BYTES_LO, REG_OSTRM_CAP_BYTES_HI, REG_OSTRM_CAP_PKTS = 0x20, 0x24, 0x28
REG_ISTRM_CTRL_STATUS, REG_OSTRM_THROTTLE = 0x38, 0x3C
STRM_FC_ENABLED, STRM_SETUP_ERR, STRM_SETUP_PENDING = 0x80000000, 0x40000000, 0x20000000

ERR_RX_LATE_CMD, ERR_RX_OVERRUN = 1, 2          # radio_control_impl.hpp err_codes
ERR_TX_UNDERRUN, ERR_TX_LATE_DATA, EVENT_TX_BURST_ACK = 1, 2, 3
SWREG_TX_ERR, SWREG_RX_ERR, SWREG_CHAN_OFFSET = 0x0000, 0x1000, 64

MAX_FC_PKTS = 0xFFFFFF


# ------------------------------------------------------------------- topology description
@dataclass
class SepSpec:
    ctrl: bool
    data: bool = True
    buff_size: int = 0          # ingress buffer (bytes), 0 = cannot sink data


@dataclass
class Topology:
    """Static image description (mirrors fpga/usrp3/top/x300/x310_rfnoc_image_core.yml)."""
    seps: List[SepSpec]
    blocks: List[NocBlock]
    # (src_kind, src_idx, src_port, dst_kind, dst_idx, dst_port); kind in {"ep","blk"}
    edges: List[Tuple[str, int, int, str, int, int]]
    device_type: int = 0xA300
    num_xports: int = 2
    protover: int = 0x0100
    num_ctrl_ep: int = 1


# ------------------------------------------------------------------- stream endpoint
class OutStream:
    """Source-side flow control state of a stream endpoint (device -> host data)."""

    def __init__(self):
        self.dst_epid = 0
        self.fc_freq_bytes = 0
        self.fc_freq_pkts = 0
        self.cap_bytes = 0
        self.cap_pkts = 0
        self.sent_bytes = 0
        self.sent_pkts = 0
        self.acked_bytes = 0
        self.acked_pkts = 0
        self.pending = False
        self.enabled = False
        self.cv = threading.Condition()
        self.seq = 0

    MAX_INFLIGHT = 4 << 20     # never keep more than this unacknowledged: the host's real socket
                               # buffer is often smaller than the capacity it advertises

    def has_credit(self, nbytes: int) -> bool:
        if not self.enabled or (self.cap_bytes == 0 and self.cap_pkts == 0):
            return True
        if self.sent_bytes - self.acked_bytes + nbytes > self.MAX_INFLIGHT:
            return False
        return ((self.cap_bytes == 0 or self.sent_bytes - self.acked_bytes + nbytes <= self.cap_bytes)
                and (self.cap_pkts == 0 or self.sent_pkts - self.acked_pkts + 1 <= self.cap_pkts))

    def wait_credit(self, nbytes: int, timeout: float) -> bool:
        end = time.monotonic() + timeout
        with self.cv:
            while not self.has_credit(nbytes):
                rem = end - time.monotonic()
                if rem <= 0:
                    return False
                self.cv.wait(min(rem, 0.05))
        return True

    def on_strs(self, s: chdr.StrsPayload):
        with self.cv:
            if s.status != chdr.STRS_OKAY:
                log.warning("STRS with error status %d", s.status)
            self.acked_bytes = s.xfer_count_bytes
            self.acked_pkts = s.xfer_count_pkts
            if self.pending:
                self.cap_bytes, self.cap_pkts = s.capacity_bytes, s.capacity_pkts
                if self.cap_pkts >= MAX_FC_PKTS:
                    self.cap_pkts = 0
                self.pending = False
            self.cv.notify_all()


class SepNode:
    def __init__(self, graph: "RfnocGraph", idx: int, spec: SepSpec):
        self.g, self.idx, self.spec = graph, idx, spec
        self.epid = 0
        self.out = OutStream()
        self.out_ctrl_status = 0
        self.in_ctrl_status = 0
        self.rx: Optional["RxChannel"] = None       # data source behind this SEP
        self.tx: Optional["TxChannel"] = None       # data sink behind this SEP
        self.async_seq = 0

    @property
    def ext_info(self) -> int:
        ni = 1 if self.spec.buff_size or self.spec.ctrl else 0
        return ((1 if self.spec.ctrl else 0) | (2 if self.spec.data else 0)
                | (1 << 2) | (1 << 8))

    # ---- mgmt registers
    def reg_read(self, addr: int) -> int:
        o = self.out
        return {REG_EPID_SELF: self.epid, REG_OSTRM_CTRL_STATUS: self.out_ctrl_status,
                REG_ISTRM_CTRL_STATUS: self.in_ctrl_status,
                REG_OSTRM_CAP_BYTES_LO: o.cap_bytes & 0xFFFFFFFF,
                REG_OSTRM_CAP_BYTES_HI: o.cap_bytes >> 32,
                REG_OSTRM_CAP_PKTS: o.cap_pkts}.get(addr, 0)

    def reg_write(self, addr: int, v: int) -> None:
        o = self.out
        if addr == REG_EPID_SELF:
            self.epid = v & 0xFFFF
        elif addr == REG_RESET_FLUSH:
            if v & 1:                                   # reset/flush output stream
                o.sent_bytes = o.sent_pkts = o.acked_bytes = o.acked_pkts = 0
                if self.rx:
                    self.rx.seq = 0                     # a new host stream expects sequence number 0
            if v & 2 and self.tx:                       # input stream
                self.tx.reset_fc()
        elif addr == REG_OSTRM_DST_EPID:
            o.dst_epid = v & 0xFFFF
        elif addr == REG_OSTRM_FC_FREQ_BYTES_LO:
            o.fc_freq_bytes = (o.fc_freq_bytes & ~0xFFFFFFFF) | v
        elif addr == REG_OSTRM_FC_FREQ_BYTES_HI:
            o.fc_freq_bytes = (o.fc_freq_bytes & 0xFFFFFFFF) | (v << 32)
        elif addr == REG_OSTRM_FC_FREQ_PKTS:
            o.fc_freq_pkts = v
        elif addr == REG_OSTRM_CTRL_STATUS:
            self._ostrm_ctrl(v)
        elif addr == REG_ISTRM_CTRL_STATUS:
            self.in_ctrl_status = STRM_FC_ENABLED
            if self.tx:
                self.tx.reset_fc()
        # REG_OSTRM_FC_HEADROOM / THROTTLE: accepted, ignored

    def _ostrm_ctrl(self, v: int) -> None:
        o = self.out
        o.enabled = bool(o.fc_freq_bytes or o.fc_freq_pkts)
        if v & 1:                                       # cfg_start: send STRC(INIT) and wait for STRS
            o.sent_bytes = o.sent_pkts = o.acked_bytes = o.acked_pkts = 0
            if self.rx:
                self.rx.seq = 0
            o.pending = o.enabled
            strc = chdr.StrcPayload(src_epid=self.epid, op_code=chdr.STRC_INIT,
                                    num_pkts=o.fc_freq_pkts, num_bytes=o.fc_freq_bytes)
            pkt = chdr.make_packet(chdr.PKT_STRC, o.dst_epid, strc.serialize())
            if not self.g.e.send_to_epid(o.dst_epid, pkt):
                log.warning("SEP%d: no route to host EPID %d for STRC init", self.idx, o.dst_epid)
                o.pending = False
        self.out_ctrl_status = (STRM_FC_ENABLED if o.enabled else 0) | (STRM_SETUP_PENDING if o.pending else 0)

    def poll_out_status(self) -> int:
        o = self.out
        self.out_ctrl_status = (STRM_FC_ENABLED if o.enabled else 0) | (STRM_SETUP_PENDING if o.pending else 0)
        return self.out_ctrl_status

    # ---- data-plane entry points
    def on_data(self, raw: bytes, hdr: chdr.Header) -> None:
        if self.tx is not None:
            self.tx.enqueue(raw, hdr)
        else:
            self.g.e.stat("sep%d" % self.idx).dropped += 1

    def on_strc(self, s: chdr.StrcPayload) -> None:
        if self.tx is not None:
            self.tx.on_strc(s)
        else:   # reply with an empty-capacity STRS so the host does not hang
            strs = chdr.StrsPayload(src_epid=self.epid, capacity_bytes=0, capacity_pkts=0)
            self.g.e.send_to_epid(s.src_epid, chdr.make_packet(chdr.PKT_STRS, s.src_epid, strs.serialize()))

    def on_strs(self, s: chdr.StrsPayload) -> None:
        self.out.on_strs(s)


# ------------------------------------------------------------------- RX streaming
class RxChannel:
    """Radio output port -> (DDC) -> SEP. One command-processing thread per channel."""

    def __init__(self, g: "RfnocGraph", name: str, radio: RadioX300, port: int, sep: SepNode,
                 ddc: Optional[DdcBlock], source_factory: Callable[[], sampleio.Source]):
        self.g, self.e, self.name, self.radio, self.port, self.sep, self.ddc = g, g.e, name, radio, port, sep, ddc
        self.source_factory = source_factory
        self.source: Optional[sampleio.Source] = None
        self.cmds: Deque[tuple] = deque()
        self.cv = threading.Condition()
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._run, name="rx-" + name, daemon=True)
        self.thread.start()
        self.st = self.e.stat("rx " + name)
        self.seq = 0
        self.streaming = False

    def rate(self) -> float:
        dec = self.ddc.rate[self.port] if self.ddc else 1
        return self.e.clock.tick_rate / dec

    def command(self, mode: int, timed: bool, ticks: int, words: int) -> None:
        log.debug("rx %s: stream command mode=%d timed=%s time=%d words=%d", self.name, mode, timed, ticks, words)
        with self.cv:
            if mode == 0:                      # STOP: drop queued, abort running
                self.cmds.clear()
                self.stop_evt.set()
            else:
                self.cmds.append((mode, timed, ticks, words))
            self.cv.notify_all()

    def _run(self):
        while not self.e.closed.is_set():
            with self.cv:
                while not self.cmds and not self.e.closed.is_set():
                    self.cv.wait(0.2)
                if self.e.closed.is_set():
                    return
                mode, timed, ticks, words = self.cmds.popleft()
                self.stop_evt.clear()
            try:
                self._stream(mode, timed, ticks, words)
            except Exception:
                log.exception("rx stream %s failed", self.name)

    def _stream(self, mode, timed, ticks, words):
        e, sep, st = self.e, self.sep, self.st
        clock = e.clock
        # ---- wait for the command time (timed) / apply stream_start latency
        if timed:
            now_t = clock.ticks_now()
            if ticks < now_t:                                        # late command
                st.late += 1
                self.radio.send_async("rx", self.port, ERR_RX_LATE_CMD)
                log.warning("rx %s: late stream command (%.3f ms late)", self.name,
                            (now_t - ticks) / clock.tick_rate * 1e3)
                return
            if not e.sched.wait_until(clock.ticks_to_mono(ticks), self.stop_evt):
                return
        if not timed:     # a timed command starts exactly at its time; only immediate ones see the start latency
            delay = e.delay("stream_start", "rx:%s" % self.name[0:1].replace("A", "0").replace("B", "1"))
            if delay > 0 and not e.sched.wait_until(sch.now() + delay, self.stop_evt):
                return
        rate = self.rate()
        if self.source is None:
            self.source = self.source_factory()
        src = self.source
        spp = max(1, self.radio.rx_max_words[self.port] or 1996)
        if mode == 1:
            remaining = words
        else:
            remaining = None
        k = 0                                         # samples sent in this command
        t0_mono = clock.ticks_to_mono(ticks) if timed else sch.now()
        t0_ticks = ticks if timed else clock.mono_to_ticks(t0_mono)
        step = clock.tick_rate / rate
        first = True
        self.streaming = True
        realtime = e.cfg.realtime
        grace = 1.5 if src.can_starve else 0.0        # allow sockets a moment to deliver data
        try:
            while not self.stop_evt.is_set() and not e.closed.is_set():
                n = spp if remaining is None else min(spp, remaining)
                if n <= 0:
                    break
                data, starved = src.read(n, wait=grace if first else (0.0 if realtime else 0.5))
                first = False
                got = len(data) // sampleio.BYTES_PER_SAMPLE
                if got == 0 and src.eof:
                    self._send(b"", t0_ticks + int(k * step), True, 0)     # clean end of stream
                    break
                if starved:
                    log.warning("rx %s: source starved (%d of %d samples available): zero-filling", self.name, got, n)
                    st.underruns += 1
                    st.zero_filled += n - got
                    data += bytes((n - got) * sampleio.BYTES_PER_SAMPLE)
                    got = n
                n = got                                     # a source at EOF may deliver a short last packet
                if realtime and not e.sched.wait_until(t0_mono + (k + n) / rate, self.stop_evt):
                    break
                eob = (remaining is not None and n >= remaining) or src.eof
                nbytes = 8 + (8 if self.radio.rx_has_time[self.port] else 0) + len(data)
                if not sep.out.has_credit(nbytes):
                    if realtime and not sep.out.wait_credit(nbytes, 0.5 + 2 * spp / rate):
                        # host is not consuming: the radio FIFO would overflow
                        st.overruns += 1
                        self.radio.send_async("rx", self.port, ERR_RX_OVERRUN)
                        log.warning("rx %s: overrun (no flow-control credit)", self.name)
                        break
                    elif not realtime:
                        while not sep.out.wait_credit(nbytes, 0.2):
                            if self.stop_evt.is_set() or e.closed.is_set():
                                return
                self._send(data, t0_ticks + int(round(k * step)), eob, nbytes)
                if starved and e.cfg.rx_overrun_on_underrun:      # flag it after the (zero-filled) data
                    st.overruns += 1
                    self.radio.send_async("rx", self.port, ERR_RX_OVERRUN)
                k += n
                st.samples += n
                st.packets += 1
                if remaining is not None:
                    remaining -= n
                    if remaining <= 0:
                        break
                if eob and src.eof:
                    break
        finally:
            self.streaming = False

    def _send(self, data: bytes, ticks: int, eob: bool, nbytes: int):
        o = self.sep.out
        has_ts = bool(self.radio.rx_has_time[self.port])
        hdr = chdr.Header(pkt_type=chdr.PKT_DATA_WITH_TS if has_ts else chdr.PKT_DATA_NO_TS,
                          dst_epid=o.dst_epid, seq_num=self.seq & 0xFFFF, eob=eob)
        self.seq += 1
        raw = chdr.Packet(hdr, ticks if has_ts else None, b"", data).serialize()
        if self.seq <= 3:
            log.log(5, "rx %s pkt#%d: %d bytes hdr=%s dst=%d", self.name, self.seq, len(raw), raw[:16].hex(), o.dst_epid)
        with o.cv:
            o.sent_bytes += len(raw)
            o.sent_pkts += 1
        self.e.send_to_epid(o.dst_epid, raw)

    def close(self):
        self.stop_evt.set()
        with self.cv:
            self.cv.notify_all()
        self.thread.join(timeout=2)
        if self.source:
            self.source.close()


# ------------------------------------------------------------------- TX streaming
class TxChannel:
    """SEP -> (DUC) -> radio input port -> sink.

    Flow control: the STRS counts report what was actually *processed*:
    ``consumed = seen - pending`` where `seen` counts every packet read off the socket and
    `pending` what is still queued / being consumed.  A host STRC(RESYNC) (sent when it
    suspects loss, tx_flow_ctrl_state.hpp) is only believed when this channel has been idle;
    otherwise packets still sitting in the kernel socket buffer would be acknowledged and the
    host would run away from us."""
    UNDERRUN_MARGIN_S = 0.02
    IDLE_RESYNC_S = 0.03
    STRC_BYTES = 24                    # 8 B header + 16 B STRC payload (counted by the host)

    def __init__(self, g: "RfnocGraph", name: str, radio: RadioX300, port: int, sep: SepNode,
                 duc: Optional[DucBlock], sink: sampleio.Sink):
        self.g, self.e, self.name, self.radio, self.port, self.sep, self.duc, self.sink = g, g.e, name, radio, port, sep, duc, sink
        self.q: Deque[Tuple[bytes, chdr.Header]] = deque()
        self.cv = threading.Condition()
        self.st = self.e.stat("tx " + name)
        self.fc_epid = 0
        self.fc_pkts = 0
        self.fc_bytes = 0
        self.seen_pkts = self.seen_bytes = 0
        self.pend_pkts = self.pend_bytes = 0
        self.since_pkts = self.since_bytes = 0
        self.last_rx = 0.0
        self._strs_lock = threading.Lock()
        self._last_strs = (0, 0)
        self.in_burst = False
        self._next = 0.0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="tx-" + name, daemon=True)
        self.thread.start()

    def rate(self) -> float:
        return self.e.clock.tick_rate / (self.duc.rate[self.port] if self.duc else 1)

    def reset_fc(self):
        self._last_strs = (0, 0)
        with self.cv:
            self.seen_pkts = self.seen_bytes = self.since_pkts = self.since_bytes = 0
            self.pend_pkts = sum(1 for _ in self.q)
            self.pend_bytes = sum(h.length for _, h in self.q)
            self.seen_pkts, self.seen_bytes = self.pend_pkts, self.pend_bytes

    def _send_strs(self) -> None:
        """Emit a STRS.  Serialised and monotonic: the host's tx_flow_ctrl_state computes
        `capacity - (sent - acked)` in unsigned arithmetic, so an ack count that ever goes
        backwards (e.g. two threads racing) wraps the window and disables flow control."""
        with self._strs_lock:
            pk, by = self.seen_pkts - self.pend_pkts, self.seen_bytes - self.pend_bytes
            pk, by = max(pk, self._last_strs[0]), max(by, self._last_strs[1])
            self._last_strs = (pk, by)
            s = chdr.StrsPayload(src_epid=self.sep.epid, status=chdr.STRS_OKAY,
                                 capacity_bytes=self.sep.spec.buff_size, capacity_pkts=MAX_FC_PKTS,
                                 xfer_count_pkts=pk, xfer_count_bytes=by)
            self.e.send_to_epid(self.fc_epid, chdr.make_packet(chdr.PKT_STRS, self.fc_epid, s.serialize()))

    def on_strc(self, s: chdr.StrcPayload):
        log.debug("tx %s: STRC op=%d from EPID %d, %d pkts/%d bytes", self.name, s.op_code,
                  s.src_epid, s.num_pkts, s.num_bytes)
        if s.op_code == chdr.STRC_INIT:
            self.reset_fc()
            self.fc_epid, self.fc_pkts, self.fc_bytes = s.src_epid, s.num_pkts, s.num_bytes
        elif s.op_code == chdr.STRC_RESYNC:
            log.debug("tx %s: resync host_sent=%d seen=%d pend=%d q=%d", self.name, s.num_bytes,
                      self.seen_bytes, self.pend_bytes, len(self.q))
            with self.cv:
                idle = (not self.q and self.pend_bytes == 0
                        and sch.now() - self.last_rx > self.IDLE_RESYNC_S)
                if idle:                       # whatever the host sent beyond what we saw was lost
                    self.seen_pkts, self.seen_bytes = s.num_pkts, s.num_bytes
                else:
                    self.seen_pkts += 1
                    self.seen_bytes += self.STRC_BYTES
        self.fc_epid = s.src_epid
        self._send_strs()

    def enqueue(self, raw: bytes, hdr: chdr.Header):
        with self.cv:
            self.q.append((raw, hdr))
            self.seen_pkts += 1
            self.seen_bytes += hdr.length
            self.pend_pkts += 1
            self.pend_bytes += hdr.length
            self.last_rx = sch.now()
            self.cv.notify()

    def _consumed(self, nbytes: int):
        with self.cv:
            self.pend_pkts -= 1
            self.pend_bytes -= nbytes
        self.since_pkts += 1
        self.since_bytes += nbytes
        if self.fc_epid and ((self.fc_bytes and self.since_bytes >= self.fc_bytes)
                             or (self.fc_pkts and self.since_pkts >= self.fc_pkts)):
            self.since_pkts = self.since_bytes = 0
            self._send_strs()

    def _run(self):
        e, st = self.e, self.st
        while not self.stop.is_set():
            with self.cv:
                if not self.q:
                    timeout = 0.1
                    if self.in_burst and self._next:
                        timeout = max(0.0, self._next + self.UNDERRUN_MARGIN_S - sch.now())
                        if timeout == 0.0:
                            st.underruns += 1
                            self.in_burst = False
                            self.radio.send_async("tx", self.port, ERR_TX_UNDERRUN)
                            log.warning("tx %s: underrun", self.name)
                            continue
                    self.cv.wait(max(timeout, 0.001))
                    continue
                raw, hdr = self.q.popleft()
            try:
                self._consume(raw, hdr)
            except Exception:
                log.exception("tx %s failed", self.name)
                self._consumed(hdr.length)

    def _consume(self, raw: bytes, hdr: chdr.Header):
        e, st = self.e, self.st
        off = 16 if hdr.pkt_type == chdr.PKT_DATA_WITH_TS else 8
        off += hdr.num_mdata * 8
        payload = raw[off:hdr.length]
        nsamp = len(payload) // sampleio.BYTES_PER_SAMPLE
        late = False
        if hdr.pkt_type == chdr.PKT_DATA_WITH_TS and e.cfg.realtime:
            ts = struct.unpack_from("<Q", raw, 8)[0]
            if ts < e.clock.ticks_now() - int(e.clock.tick_rate * 0.0005) and not self.in_burst:
                late = True
            elif not e.sched.wait_until(e.clock.ticks_to_mono(ts), self.stop):
                return
        if late:
            st.late += 1
            self.radio.send_async("tx", self.port, ERR_TX_LATE_DATA)
            log.warning("tx %s: late data packet", self.name)
        else:
            if e.cfg.realtime and nsamp:
                now = sch.now()
                due = (max(self._next, now) if self.in_burst else now) + nsamp / self.rate()
                self._next = due
                e.sched.wait_until(due, self.stop)
            self.sink.write(payload)
            st.samples += nsamp
            st.packets += 1
            self.in_burst = not hdr.eob
            if not self.in_burst:
                self.radio.send_async("tx", self.port, EVENT_TX_BURST_ACK)
        self._consumed(hdr.length)

    def close(self):
        self.stop.set()
        with self.cv:
            self.cv.notify_all()
        self.thread.join(timeout=2)
        self.sink.close()


# ------------------------------------------------------------------- graph
class RfnocGraph:
    def __init__(self, engine: Engine, topo: Topology, device_id_fn: Callable[[], int]):
        self.e = engine
        self.topo = topo
        self.device_id = device_id_fn
        self.seps = [SepNode(self, i, s) for i, s in enumerate(topo.seps)]
        self.blocks = topo.blocks
        for b in self.blocks:
            b.graph = self
        self.xbar_routes: Dict[int, int] = {}
        self.rx_channels: Dict[str, RxChannel] = {}
        self.tx_channels: Dict[str, TxChannel] = {}
        self.async_seq = 0
        self.entry_port = 0                    # xbar port that the UDP transport is attached to
        self._edges_adj = self._build_adjacency()
        self.unknown_regs = set()
        self._pipe_lock = threading.Lock()
        self._pipe_free: Dict[tuple, float] = {}

    # ---- client-zero register image
    def _flat_port(self, kind, idx):               # port numbering of client zero (see client_zero.cpp)
        return 1 + idx if kind == "ep" else 1 + len(self.seps) + idx

    def _build_adjacency(self):
        return [(self._flat_port(sk, si), sp, self._flat_port(dk, di), dp)
                for (sk, si, sp, dk, di, dp) in self.topo.edges]

    def cz_read(self, addr: int) -> int:
        t = self.topo
        nb, ns = len(self.blocks), len(self.seps)
        if addr == 0x00:
            return t.protover & 0xFFFF
        if addr == 0x04:
            return (ns & 0x3FF) | ((nb & 0x3FF) << 10) | ((t.num_xports & 0x3FF) << 20) | (1 << 31)
        if addr == 0x08:
            return len(self._edges_adj)
        if addr == 0x0C:
            return (t.device_type & 0xFFFF) << 16
        if addr == 0x10:
            return t.num_ctrl_ep & 0x3FF
        if addr >= 0x10000:
            off = addr - 0x10000
            if off == 0:
                return len(self._edges_adj)
            i = off // 4 - 1
            if 0 <= i < len(self._edges_adj):
                sb, sp, db, dp = self._edges_adj[i]
                return ((sb & 0x3FF) << 22) | ((sp & 0x3F) << 16) | ((db & 0x3FF) << 6) | (dp & 0x3F)
            return 0
        port, off = addr // 0x40, addr % 0x40
        bi = port - 1 - ns
        if 0 <= bi < nb:
            b = self.blocks[bi]
            if off == 0x00:   # block info
                return ((b.PROTOVER & 0x3F) | ((b.NUM_IN & 0x3F) << 6) | ((b.NUM_OUT & 0x3F) << 12)
                        | ((b.CTRL_FIFO_LOG2 & 0x3F) << 18) | ((b.MAX_ASYNC & 0xFF) << 24))
            if off == 0x04:
                return b.NOC_ID
            if off == 0x08:   # flush status (done) + mtu info
                return ((b.DATA_MTU_LOG2 & 0x3F) << 2) | (1 << 1)
        return 0

    def cz_write(self, addr: int, v: int) -> None:
        pass     # flush timeout / flush / reset: accepted (blocks have no queues to flush)

    # ---- packet entry
    def handle_packet(self, buf: bytes, addr) -> None:
        try:
            h = chdr.Header.unpack(struct.unpack_from("<Q", buf, 0)[0])
            if h.length > len(buf) or h.length < 8:
                raise chdr.ChdrError("bad length")
            t = h.pkt_type
            if t in (chdr.PKT_DATA_NO_TS, chdr.PKT_DATA_WITH_TS):
                sep = self.sep_by_epid(h.dst_epid)
                if sep is not None:
                    sep.on_data(buf, h)
                return
            pkt = chdr.Packet.parse(buf)
            if t == chdr.PKT_MGMT:
                self._mgmt(pkt, addr)
            elif t == chdr.PKT_CTRL:
                self._ctrl(pkt, addr)
            elif t == chdr.PKT_STRC:
                s = chdr.StrcPayload.deserialize(pkt.payload)
                self.e.learn_route(s.src_epid, addr)
                sep = self.sep_by_epid(h.dst_epid)
                if sep:
                    sep.on_strc(s)
            elif t == chdr.PKT_STRS:
                s = chdr.StrsPayload.deserialize(pkt.payload)
                self.e.learn_route(s.src_epid, addr)
                sep = self.sep_by_epid(h.dst_epid)
                if sep:
                    sep.on_strs(s)
        except (chdr.ChdrError, struct.error) as ex:
            log.warning("dropping malformed CHDR packet from %s:%d (%s)", addr[0], addr[1], ex)
        except Exception:
            log.exception("error handling CHDR packet")

    def sep_by_epid(self, epid: int) -> Optional[SepNode]:
        for s in self.seps:
            if s.epid == epid and (s.epid or True):
                return s
        return None

    # ---- management packets (xport -> xbar -> sep)
    def _mgmt(self, pkt: chdr.Packet, addr) -> None:
        p = chdr.MgmtPayload.deserialize(pkt.payload)
        self.e.learn_route(p.src_epid, addr)            # xport ADVERTISE semantics
        dev = self.device_id()
        nxports = self.topo.num_xports
        nports = nxports + len(self.seps)
        resp: List[chdr.MgmtOp] = [chdr.MgmtOp(chdr.MGMT_NOP)]
        node = ("xport", self.entry_port)
        returned = False
        for hop in p.hops:
            nxt = None
            for op in hop:
                c = op.op_code
                if c == chdr.MGMT_INFO_REQ:
                    if node[0] == "xport":
                        resp.append(chdr.MgmtOp.node_info(dev, NODE_XPORT, node[1], 0))
                    elif node[0] == "xbar":
                        resp.append(chdr.MgmtOp.node_info(dev, NODE_XBAR, self.entry_port,
                                                          (nxports << 8) | nports))
                    else:
                        s = self.seps[node[1]]
                        resp.append(chdr.MgmtOp.node_info(dev, NODE_SEP, s.idx, s.ext_info))
                elif c == chdr.MGMT_RETURN:
                    returned = True
                elif c == chdr.MGMT_ADVERTISE:
                    pass
                elif c == chdr.MGMT_SEL_DEST and node[0] == "xbar":
                    d = op.sel_dest
                    nxt = ("sep", d - nxports) if nxports <= d < nports else ("xport", d)
                elif c == chdr.MGMT_CFG_WR_REQ:
                    if node[0] == "xbar":
                        self.xbar_routes[op.cfg_addr] = op.cfg_data
                    elif node[0] == "sep":
                        self.seps[node[1]].reg_write(op.cfg_addr, op.cfg_data)
                elif c == chdr.MGMT_CFG_RD_REQ and node[0] == "sep":
                    s = self.seps[node[1]]
                    v = s.poll_out_status() if op.cfg_addr == REG_OSTRM_CTRL_STATUS else s.reg_read(op.cfg_addr)
                    resp.append(chdr.MgmtOp.cfg(chdr.MGMT_CFG_RD_RESP, op.cfg_addr, v))
                elif c == chdr.MGMT_NOP:
                    pass
                else:
                    log.debug("mgmt op %d not handled at %s", c, node)
            if returned:
                break
            if nxt is None:                              # default: xport -> xbar
                if node[0] == "xport":
                    nxt = ("xbar", 0)
                else:
                    break
            if nxt[0] == "xport" and nxt[1] >= nxports:
                break
            if nxt[0] == "sep" and not (0 <= nxt[1] < len(self.seps)):
                break
            node = nxt
        if not returned:
            return
        rp = chdr.MgmtPayload(src_epid=pkt.header.dst_epid, protover=p.protover, hops=[resp])
        raw = chdr.make_packet(chdr.PKT_MGMT, p.src_epid, rp.serialize(), seq_num=pkt.header.seq_num)
        self.e.send_to_addr(raw, addr)

    # ---- control packets
    def _ctrl(self, pkt: chdr.Packet, addr) -> None:
        cp = chdr.CtrlPayload.deserialize(pkt.payload)
        self.e.learn_route(cp.src_epid, addr)
        if cp.is_ack:           # host's ack of one of our async messages: never ack an ack
            return
        our_epid = pkt.header.dst_epid
        if self.sep_by_epid(our_epid) is None:
            log.warning("ctrl packet for unknown EPID %d", our_epid)
            return
        if cp.timestamp is not None:
            now_t = self.e.clock.ticks_now()
            if cp.timestamp > now_t:
                self.e.sched.call_at(self.e.clock.ticks_to_mono(cp.timestamp),
                                     lambda: self._exec_ctrl(pkt.header, cp, addr, False))
                return
            self._exec_ctrl(pkt.header, cp, addr, True)       # late -> TSERR
            return
        self._exec_ctrl(pkt.header, cp, addr, False)

    def _target(self, port: int):
        if port == 0:
            return self, 0
        bi = port - 2                                    # ctrl xbar: 0 = client zero, 1 = ep0 ctrl, then blocks
        if 0 <= bi < len(self.blocks):
            return self.blocks[bi], bi
        return None, 0

    def _exec_ctrl(self, hdr: chdr.Header, cp: chdr.CtrlPayload, addr, late: bool) -> None:
        tgt, _ = self._target(cp.dst_port)
        status = chdr.CMD_OKAY
        data = list(cp.data)
        extra = 0.0
        if tgt is None:
            status = chdr.CMD_CMDERR
            log.warning("ctrl to unknown port %d", cp.dst_port)
        elif late:
            status = chdr.CMD_TSERR
        else:
            rd = tgt.cz_read if tgt is self else tgt.read
            wr = tgt.cz_write if tgt is self else tgt.write
            try:
                if cp.op_code == chdr.OP_WRITE:
                    r = wr(cp.address, cp.data[0])
                    extra += r or 0.0
                elif cp.op_code == chdr.OP_READ:
                    data = [rd(cp.address) & 0xFFFFFFFF]
                elif cp.op_code == chdr.OP_BLOCK_WRITE:
                    for i, v in enumerate(cp.data):
                        extra += wr(cp.address + 4 * i, v) or 0.0
                elif cp.op_code == chdr.OP_BLOCK_READ:
                    data = [rd(cp.address + 4 * i) & 0xFFFFFFFF for i in range(len(cp.data))]
                elif cp.op_code == chdr.OP_READ_WRITE:
                    data = [rd(cp.address) & 0xFFFFFFFF]
                    wr(cp.address, cp.data[0])
                elif cp.op_code == chdr.OP_SLEEP:
                    pass
                elif cp.op_code == chdr.OP_POLL:
                    pass
                else:
                    status = chdr.CMD_CMDERR
            except Exception:
                log.exception("ctrl op failed (port %d addr 0x%x)", cp.dst_port, cp.address)
                status = chdr.CMD_CMDERR
        extra += self.e.delay("ctrl_access", "mboard:0")
        ack = chdr.CtrlPayload(dst_port=cp.src_port, src_port=cp.dst_port, seq_num=cp.seq_num,
                               timestamp=None, is_ack=True, src_epid=hdr.dst_epid, address=cp.address,
                               byte_enable=cp.byte_enable, op_code=cp.op_code, status=status, data=data)
        raw = chdr.make_packet(chdr.PKT_CTRL, cp.src_epid, ack.serialize(), seq_num=hdr.seq_num)
        self._reply_in_order(raw, addr, extra)

    def _reply_in_order(self, raw: bytes, addr, extra: float) -> None:
        """Ctrl acks must reach the host in request order (ctrlport_endpoint.cpp treats a
        skipped sequence number as a dropped response), so a delayed ack also delays every
        ack behind it.  Latency is thus modelled as head-of-line blocking, like the hardware."""
        with self._pipe_lock:
            now = sch.now()
            free = self._pipe_free.get(addr, 0.0)
            when = max(now + extra, free)
            self._pipe_free[addr] = when
            if when <= now and free <= now:
                direct = True
            else:
                direct = False
        if direct:
            self.e.send_to_addr(raw, addr)
        else:
            self.e.sched.call_at(when, lambda: self.e.send_to_addr(raw, addr))

    def send_async_ctrl(self, rem_epid: int, rem_port: int, address: int, data: List[int], ts: Optional[int]):
        sep0 = next((s for s in self.seps if s.spec.ctrl), None)
        if sep0 is None:
            return
        self.async_seq = (self.async_seq + 1) & 0x3F
        cp = chdr.CtrlPayload(dst_port=rem_port, src_port=0, seq_num=self.async_seq, timestamp=ts,
                              is_ack=False, src_epid=sep0.epid, address=address,
                              op_code=chdr.OP_WRITE, data=data)
        self.e.send_to_epid(rem_epid, chdr.make_packet(chdr.PKT_CTRL, rem_epid, cp.serialize()))

    # ---- stream wiring
    def downstream_chain(self, kind: str, idx: int, port: int) -> List[Tuple[str, int, int]]:
        """Follow edges from (kind,idx,port) output through blocks; returns visited nodes."""
        chain = [(kind, idx, port)]
        cur = (kind, idx, port)
        for _ in range(16):
            nxt = [e for e in self.topo.edges if (e[0], e[1], e[2]) == cur]
            if not nxt:
                break
            e = nxt[0]
            tgt = (e[3], e[4], e[5])
            chain.append(tgt)
            if tgt[0] == "ep":
                break
            cur = ("blk", tgt[1], tgt[2])           # pass-through: out port == in port for ddc/duc
        return chain

    def close(self):
        for c in list(self.rx_channels.values()):
            c.close()
        for c in list(self.tx_channels.values()):
            c.close()
        for b in self.blocks:
            b.close()
