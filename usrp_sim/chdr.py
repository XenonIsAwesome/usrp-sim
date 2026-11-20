"""RFNoC CHDR (de)serialization, 64-bit CHDR width, little-endian.

Mirrors host/include/uhd/rfnoc/chdr_types.hpp and host/lib/rfnoc/chdr_types.cpp
(field offsets/widths are copied from there) and host/lib/rfnoc/chdr_packet.cpp
(packet layout: header word, optional timestamp word, metadata words, payload).
Pure stdlib (`struct`); no dependency on libuhd/pyuhd.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional

# ---- packet types (chdr_types.hpp packet_type_t)
PKT_MGMT = 0x0
PKT_STRS = 0x1
PKT_STRC = 0x2
PKT_CTRL = 0x4
PKT_DATA_NO_TS = 0x6
PKT_DATA_WITH_TS = 0x7

# ---- ctrl opcodes / status
OP_SLEEP, OP_WRITE, OP_READ, OP_READ_WRITE, OP_BLOCK_WRITE, OP_BLOCK_READ, OP_POLL = range(7)
CMD_OKAY, CMD_CMDERR, CMD_TSERR, CMD_WARNING = range(4)

# ---- strs status / strc opcodes
STRS_OKAY, STRS_CMDERR, STRS_SEQERR, STRS_DATAERR, STRS_RTERR = range(5)
STRC_INIT, STRC_PING, STRC_RESYNC = range(3)

# ---- mgmt opcodes
(MGMT_NOP, MGMT_ADVERTISE, MGMT_SEL_DEST, MGMT_RETURN, MGMT_INFO_REQ, MGMT_INFO_RESP,
 MGMT_CFG_WR_REQ, MGMT_CFG_RD_REQ, MGMT_CFG_RD_RESP) = range(9)

CHDR_W_64 = 0
M64 = (1 << 64) - 1


class ChdrError(ValueError):
    """Raised for malformed CHDR packets (callers drop the packet and carry on)."""


@dataclass
class Header:
    vc: int = 0
    eob: bool = False
    eov: bool = False
    pkt_type: int = PKT_DATA_NO_TS
    num_mdata: int = 0
    seq_num: int = 0
    length: int = 0
    dst_epid: int = 0

    def pack(self) -> int:
        return (((self.vc & 0x3F) << 58) | (int(self.eob) << 57) | (int(self.eov) << 56)
                | ((self.pkt_type & 0x7) << 53) | ((self.num_mdata & 0x1F) << 48)
                | ((self.seq_num & 0xFFFF) << 32) | ((self.length & 0xFFFF) << 16)
                | (self.dst_epid & 0xFFFF))

    @classmethod
    def unpack(cls, w: int) -> "Header":
        return cls(vc=(w >> 58) & 0x3F, eob=bool((w >> 57) & 1), eov=bool((w >> 56) & 1),
                   pkt_type=(w >> 53) & 0x7, num_mdata=(w >> 48) & 0x1F,
                   seq_num=(w >> 32) & 0xFFFF, length=(w >> 16) & 0xFFFF,
                   dst_epid=w & 0xFFFF)


# --------------------------------------------------------------------- ctrl
@dataclass
class CtrlPayload:
    dst_port: int = 0
    src_port: int = 0
    seq_num: int = 0
    timestamp: Optional[int] = None
    is_ack: bool = False
    src_epid: int = 0
    address: int = 0
    byte_enable: int = 0xF
    op_code: int = OP_WRITE
    status: int = CMD_OKAY
    data: List[int] = field(default_factory=lambda: [0])

    def serialize(self) -> bytes:
        n = len(self.data)
        if not 0 < n < 16:
            raise ChdrError("ctrl payload needs 1..15 data words")
        words = [(self.dst_port & 0x3FF) | ((self.src_port & 0x3FF) << 10) | ((n & 0xF) << 20)
                 | ((self.seq_num & 0x3F) << 24) | (int(self.timestamp is not None) << 30)
                 | (int(self.is_ack) << 31) | ((self.src_epid & 0xFFFF) << 32)]
        if self.timestamp is not None:
            words.append(self.timestamp & M64)
        words.append((self.address & 0xFFFFF) | ((self.byte_enable & 0xF) << 20)
                     | ((self.op_code & 0xF) << 24) | ((self.status & 0x3) << 30)
                     | ((self.data[0] & 0xFFFFFFFF) << 32))
        for i in range(1, n, 2):
            hi = 0 if (i + 1 >= n) else self.data[i + 1]
            words.append(((hi & 0xFFFFFFFF) << 32) | (self.data[i] & 0xFFFFFFFF))
        return struct.pack("<%dQ" % len(words), *words)

    @classmethod
    def deserialize(cls, buf: bytes) -> "CtrlPayload":
        w = struct.unpack("<%dQ" % (len(buf) // 8), buf[: len(buf) // 8 * 8])
        try:
            h = w[0]
            n = (h >> 20) & 0xF
            if not 0 < n < 16:
                raise ChdrError("bad ctrl num_data")
            p = cls(dst_port=h & 0x3FF, src_port=(h >> 10) & 0x3FF, seq_num=(h >> 24) & 0x3F,
                    is_ack=bool((h >> 31) & 1), src_epid=(h >> 32) & 0xFFFF, data=[0] * n)
            i = 1
            if (h >> 30) & 1:
                p.timestamp = w[i]
                i += 1
            op = w[i]
            i += 1
            p.address = op & 0xFFFFF
            p.byte_enable = (op >> 20) & 0xF
            p.op_code = (op >> 24) & 0xF
            p.status = (op >> 30) & 0x3
            p.data[0] = (op >> 32) & 0xFFFFFFFF
            for j in range(1, n, 2):
                d = w[i]
                i += 1
                p.data[j] = d & 0xFFFFFFFF
                if j + 1 < n:
                    p.data[j + 1] = (d >> 32) & 0xFFFFFFFF
            return p
        except IndexError:
            raise ChdrError("truncated ctrl payload")


# --------------------------------------------------------------------- strs/strc
@dataclass
class StrsPayload:
    src_epid: int = 0
    status: int = STRS_OKAY
    capacity_bytes: int = 0
    capacity_pkts: int = 0
    xfer_count_pkts: int = 0
    xfer_count_bytes: int = 0
    buff_info: int = 0
    status_info: int = 0

    def serialize(self) -> bytes:
        w0 = (self.src_epid & 0xFFFF) | ((self.status & 0xF) << 16) | \
             ((self.capacity_bytes & ((1 << 40) - 1)) << 24)
        w1 = (self.capacity_pkts & 0xFFFFFF) | ((self.xfer_count_pkts & ((1 << 40) - 1)) << 24)
        w3 = (self.buff_info & 0xFFFF) | ((self.status_info & ((1 << 48) - 1)) << 16)
        return struct.pack("<4Q", w0, w1, self.xfer_count_bytes & M64, w3)

    @classmethod
    def deserialize(cls, buf: bytes) -> "StrsPayload":
        if len(buf) < 32:
            raise ChdrError("truncated strs")
        w0, w1, w2, w3 = struct.unpack("<4Q", buf[:32])
        return cls(src_epid=w0 & 0xFFFF, status=(w0 >> 16) & 0xF, capacity_bytes=w0 >> 24,
                   capacity_pkts=w1 & 0xFFFFFF, xfer_count_pkts=w1 >> 24,
                   xfer_count_bytes=w2, buff_info=w3 & 0xFFFF, status_info=w3 >> 16)


@dataclass
class StrcPayload:
    src_epid: int = 0
    op_code: int = STRC_INIT
    op_data: int = 0
    num_pkts: int = 0
    num_bytes: int = 0

    def serialize(self) -> bytes:
        w0 = (self.src_epid & 0xFFFF) | ((self.op_code & 0xF) << 16) | \
             ((self.op_data & 0xF) << 20) | ((self.num_pkts & ((1 << 40) - 1)) << 24)
        return struct.pack("<2Q", w0, self.num_bytes & M64)

    @classmethod
    def deserialize(cls, buf: bytes) -> "StrcPayload":
        if len(buf) < 16:
            raise ChdrError("truncated strc")
        w0, w1 = struct.unpack("<2Q", buf[:16])
        return cls(src_epid=w0 & 0xFFFF, op_code=(w0 >> 16) & 0xF, op_data=(w0 >> 20) & 0xF,
                   num_pkts=w0 >> 24, num_bytes=w1)


# --------------------------------------------------------------------- mgmt
@dataclass
class MgmtOp:
    op_code: int
    payload: int = 0

    # sel_dest: dest[15:0]; cfg: addr[15:0], data[47:16]; node_info: see below
    @property
    def cfg_addr(self) -> int:
        return self.payload & 0xFFFF

    @property
    def cfg_data(self) -> int:
        return (self.payload >> 16) & 0xFFFFFFFF

    @property
    def sel_dest(self) -> int:
        return self.payload & 0xFFFF

    @staticmethod
    def cfg(op_code: int, addr: int, data: int = 0) -> "MgmtOp":
        return MgmtOp(op_code, ((data & 0xFFFFFFFF) << 16) | (addr & 0xFFFF))

    @staticmethod
    def node_info(device_id: int, node_type: int, node_inst: int, ext_info: int) -> "MgmtOp":
        return MgmtOp(MGMT_INFO_RESP, (device_id & 0xFFFF) | ((node_type & 0xF) << 16)
                      | ((node_inst & 0x3FF) << 20) | ((ext_info & 0x3FFFF) << 30))


@dataclass
class MgmtPayload:
    src_epid: int = 0
    protover: int = 0x0100
    chdr_w: int = CHDR_W_64
    hops: List[List[MgmtOp]] = field(default_factory=list)

    def serialize(self) -> bytes:
        words = [(self.protover << 48) | ((self.chdr_w & 7) << 45)
                 | ((len(self.hops) & 0x3FF) << 16) | (self.src_epid & 0xFFFF)]
        for hop in self.hops:
            for i, op in enumerate(hop):
                words.append(((op.payload & ((1 << 48) - 1)) << 16) | ((op.op_code & 0xFF) << 8)
                             | ((len(hop) - i - 1) & 0xFF))
        return struct.pack("<%dQ" % len(words), *words)

    @classmethod
    def deserialize(cls, buf: bytes) -> "MgmtPayload":
        n = len(buf) // 8
        if n < 1:
            raise ChdrError("truncated mgmt")
        w = struct.unpack("<%dQ" % n, buf[: n * 8])
        hdr = w[0]
        p = cls(src_epid=hdr & 0xFFFF, protover=(hdr >> 48) & 0xFFFF, chdr_w=(hdr >> 45) & 7)
        if p.chdr_w != CHDR_W_64:
            raise ChdrError("only 64-bit CHDR supported")
        nhops = (hdr >> 16) & 0x3FF
        i = 1
        for _ in range(nhops):
            hop = []
            while True:
                if i >= n:
                    raise ChdrError("truncated mgmt hop")
                ow = w[i]
                i += 1
                hop.append(MgmtOp((ow >> 8) & 0xFF, ow >> 16))
                if (ow & 0xFF) == 0:
                    break
            p.hops.append(hop)
        return p


# --------------------------------------------------------------------- packet
@dataclass
class Packet:
    header: Header
    timestamp: Optional[int] = None
    mdata: bytes = b""
    payload: bytes = b""

    def serialize(self) -> bytes:
        h = self.header
        has_ts = h.pkt_type == PKT_DATA_WITH_TS
        if has_ts and self.timestamp is None:
            raise ChdrError("DATA_WITH_TS needs a timestamp")
        if len(self.mdata) % 8:
            raise ChdrError("metadata must be a multiple of 8 bytes")
        h.num_mdata = len(self.mdata) // 8
        total = 8 + (8 if has_ts else 0) + len(self.mdata) + len(self.payload)
        h.length = total
        out = [struct.pack("<Q", h.pack())]
        if has_ts:
            out.append(struct.pack("<Q", self.timestamp & M64))
        out.append(self.mdata)
        out.append(self.payload)
        return b"".join(out)

    @classmethod
    def parse(cls, buf: bytes) -> "Packet":
        if len(buf) < 8:
            raise ChdrError("runt packet")
        h = Header.unpack(struct.unpack_from("<Q", buf, 0)[0])
        if h.length < 8 or h.length > len(buf):
            raise ChdrError("bad length field %d (got %d bytes)" % (h.length, len(buf)))
        off = 8
        ts = None
        if h.pkt_type == PKT_DATA_WITH_TS:
            if h.length < 16:
                raise ChdrError("runt timestamped packet")
            ts = struct.unpack_from("<Q", buf, 8)[0]
            off = 16
        md_end = off + h.num_mdata * 8
        if md_end > h.length:
            raise ChdrError("metadata exceeds packet length")
        return cls(h, ts, bytes(buf[off:md_end]), bytes(buf[md_end:h.length]))


def make_packet(pkt_type: int, dst_epid: int, payload: bytes, seq_num: int = 0,
                eob: bool = False, timestamp: Optional[int] = None, mdata: bytes = b"") -> bytes:
    return Packet(Header(pkt_type=pkt_type, dst_epid=dst_epid, seq_num=seq_num, eob=eob),
                  timestamp, mdata, payload).serialize()
