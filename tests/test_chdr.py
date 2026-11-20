import importlib.util
import random
import struct
from pathlib import Path

import pytest

from usrp_sim import chdr

GOLDEN_DIR = Path(__file__).resolve().parents[3] / "host" / "tests" / "common" / "chdr_resource"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, GOLDEN_DIR / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_header_roundtrip_all_fields():
    h = chdr.Header(vc=0x2A, eob=True, eov=True, pkt_type=chdr.PKT_DATA_WITH_TS, num_mdata=3,
                    seq_num=0xBEEF, length=0x1234, dst_epid=0xCAFE)
    assert chdr.Header.unpack(h.pack()) == h


def test_header_field_positions():
    # offsets from chdr_types.hpp: vc 58, eob 57, eov 56, type 53, nmdata 48, seq 32, len 16, dst 0
    w = chdr.Header(pkt_type=chdr.PKT_CTRL, length=24, dst_epid=2).pack()
    assert w == (4 << 53) | (24 << 16) | 2


@pytest.mark.parametrize("n", range(1, 16))
@pytest.mark.parametrize("ts", [None, 0x0123456789ABCDEF])
def test_ctrl_roundtrip(n, ts):
    rnd = random.Random(n)
    p = chdr.CtrlPayload(dst_port=rnd.randrange(1024), src_port=rnd.randrange(1024), seq_num=rnd.randrange(64),
                         timestamp=ts, is_ack=bool(n & 1), src_epid=rnd.randrange(65536),
                         address=rnd.randrange(1 << 20), byte_enable=0xF, op_code=chdr.OP_BLOCK_WRITE,
                         data=[rnd.randrange(1 << 32) for _ in range(n)])
    raw = p.serialize()
    assert len(raw) % 8 == 0
    assert chdr.CtrlPayload.deserialize(raw) == p


def test_ctrl_rejects_bad_num_data():
    with pytest.raises(chdr.ChdrError):
        chdr.CtrlPayload(data=[]).serialize()
    with pytest.raises(chdr.ChdrError):
        chdr.CtrlPayload(data=[0] * 16).serialize()


def test_strs_strc_roundtrip():
    s = chdr.StrsPayload(src_epid=7, status=chdr.STRS_SEQERR, capacity_bytes=(1 << 40) - 1, capacity_pkts=0xFFFFFF,
                         xfer_count_pkts=12345, xfer_count_bytes=1 << 50, buff_info=0xABCD, status_info=(1 << 48) - 1)
    assert chdr.StrsPayload.deserialize(s.serialize()) == s
    c = chdr.StrcPayload(src_epid=9, op_code=chdr.STRC_RESYNC, op_data=5, num_pkts=(1 << 40) - 1, num_bytes=1 << 60)
    assert chdr.StrcPayload.deserialize(c.serialize()) == c


def test_mgmt_roundtrip_and_layout():
    p = chdr.MgmtPayload(src_epid=3, protover=0x0100, hops=[
        [chdr.MgmtOp(chdr.MGMT_NOP)],
        [chdr.MgmtOp(chdr.MGMT_INFO_REQ), chdr.MgmtOp(chdr.MGMT_RETURN)],
        [chdr.MgmtOp.cfg(chdr.MGMT_CFG_WR_REQ, 8, 0xDEADBEEF), chdr.MgmtOp(chdr.MGMT_SEL_DEST, 5)]])
    raw = p.serialize()
    q = chdr.MgmtPayload.deserialize(raw)
    assert q == p
    # header word: protover << 48 | chdr_w << 45 | nhops << 16 | src_epid
    assert struct.unpack_from("<Q", raw)[0] == (0x0100 << 48) | (3 << 16) | 3
    # ops_pending counts down inside a hop
    w1, w2 = struct.unpack_from("<2Q", raw, 16)
    assert (w1 & 0xFF, w2 & 0xFF) == (1, 0)


def test_node_info_and_cfg_payload_fields():
    op = chdr.MgmtOp.node_info(device_id=0x1234, node_type=3, node_inst=0x2A5, ext_info=0x3FFFF)
    assert op.payload & 0xFFFF == 0x1234 and (op.payload >> 16) & 0xF == 3
    assert (op.payload >> 20) & 0x3FF == 0x2A5 and (op.payload >> 30) == 0x3FFFF
    c = chdr.MgmtOp.cfg(chdr.MGMT_CFG_RD_RESP, 0x28, 0x1000)
    assert (c.cfg_addr, c.cfg_data) == (0x28, 0x1000)


def test_packet_data_with_timestamp_and_mdata():
    payload = bytes(range(64))
    raw = chdr.make_packet(chdr.PKT_DATA_WITH_TS, 7, payload, seq_num=9, eob=True, timestamp=123456789,
                           mdata=b"\x01" * 16)
    p = chdr.Packet.parse(raw)
    assert (p.header.length, p.header.num_mdata, p.header.eob) == (8 + 8 + 16 + 64, 2, True)
    assert p.timestamp == 123456789 and p.mdata == b"\x01" * 16 and p.payload == payload


@pytest.mark.parametrize("bad", [b"", b"\x00" * 7, struct.pack("<Q", (6 << 53) | (4000 << 16))])
def test_malformed_packets_raise(bad):
    with pytest.raises(chdr.ChdrError):
        chdr.Packet.parse(bad)


def test_golden_vectors_from_hardware_captures():
    """Real captures shipped with UHD (host/tests/common/chdr_resource): parse + re-serialize
    must reproduce every control/management packet byte for byte."""
    if not (GOLDEN_DIR / "rfnoc_packets_ctrl_mgmt.py").exists():
        pytest.skip("golden vector module not found in the UHD checkout")
    m = _load("rfnoc_packets_ctrl_mgmt")
    pkts = [v for k, v in vars(m).items() if k.startswith("peer") and "_" in k and isinstance(v, (bytes, bytearray))]
    assert len(pkts) > 20
    kinds = set()
    for raw in pkts:
        p = chdr.Packet.parse(raw)
        t = p.header.pkt_type
        kinds.add(t)
        if t == chdr.PKT_CTRL:
            pl = chdr.CtrlPayload.deserialize(p.payload)
            assert chdr.make_packet(t, p.header.dst_epid, pl.serialize(), seq_num=p.header.seq_num) == bytes(raw)
        elif t == chdr.PKT_MGMT:
            pl = chdr.MgmtPayload.deserialize(p.payload)
            assert chdr.make_packet(t, p.header.dst_epid, pl.serialize(), seq_num=p.header.seq_num) == bytes(raw)
    assert chdr.PKT_CTRL in kinds and chdr.PKT_MGMT in kinds


def test_golden_data_packets_parse():
    if not (GOLDEN_DIR / "rfnoc_packets_data.py").exists():
        pytest.skip("golden vector module not found in the UHD checkout")
    m = _load("rfnoc_packets_data")
    raws = [v for k, v in vars(m).items() if k.startswith("peer") and isinstance(v, (bytes, bytearray))]
    raws += [x for k, v in vars(m).items() if k.startswith("peer") and isinstance(v, list) for x in v]
    n = 0
    for raw in raws:
        p = chdr.Packet.parse(bytes(raw))
        assert chdr.Packet.parse(p.serialize()).payload == p.payload
        n += 1
    assert n > 0
