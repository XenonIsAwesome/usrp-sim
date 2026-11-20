"""Daughterboard catalogue and EEPROM images.

IDs come from host/lib/usrp/dboard/{db_ubx.hpp,db_sbx_common.cpp,db_wbx_simple.cpp,
db_basic_and_lf.hpp} (rx_id, tx_id).  EEPROM layout: host/lib/usrp/dboard_eeprom.cpp.
"""
from __future__ import annotations

from typing import Dict, Tuple

# name -> (rx_id, tx_id, family)
DBOARDS: Dict[str, Tuple[int, int, str]] = {
    "UBX-160":    (0x7E, 0x7D, "ubx"),
    "UBX-160-v1": (0x7A, 0x79, "ubx"),
    "UBX-40":     (0x7C, 0x7B, "ubx"),
    "UBX-40-v1":  (0x78, 0x77, "ubx"),
    "UBX-160-LP": (0x0201, 0x0200, "ubx"),
    "UBX-TDD":    (0x0203, 0x0202, "ubx"),
    "SBX":        (0x54, 0x55, "sbx"),
    "SBX-v4":     (0x65, 0x64, "sbx"),
    "SBX-v5":     (0x69, 0x68, "sbx"),
    "CBX":        (0x67, 0x66, "sbx"),
    "SBX-120":    (0x83, 0x82, "sbx"),
    "CBX-120":    (0x85, 0x84, "sbx"),
    "WBX":        (0x53, 0x52, "wbx"),
    "WBX-v3":     (0x57, 0x56, "wbx"),
    "WBX-v4":     (0x63, 0x62, "wbx"),
    "WBX-120":    (0x81, 0x80, "wbx"),
    "BASIC":      (0x0001, 0x0000, "basic"),
    "LF":         (0x000F, 0x000E, "basic"),
    "EMPTY":      (0xFFFF, 0xFFFF, "empty"),
}

# EEPROM i2c address offsets (host/lib/usrp/x300/x300_impl.cpp get_dboard_ids, x300_radio_control.cpp)
RX_EEPROM, TX_EEPROM, GDB_EEPROM = 0x5, 0x4, 0x1
BASE_ADDR = 0x50


def dboard_eeprom_image(board_id: int, serial: str, rev: int) -> bytearray:
    """32-byte common EEPROM section (magic 0xDB ... checksum), padded to 256 B."""
    b = bytearray(32)
    b[0x00] = 0xDB
    b[0x01], b[0x02] = board_id & 0xFF, (board_id >> 8) & 0xFF
    b[0x03], b[0x04] = rev & 0xFF, (rev >> 8) & 0xFF
    ser = serial.encode("ascii")[:9].ljust(9, b"\x00")
    b[0x09:0x12] = ser
    s = 0
    for i in range(0x1F):          # dboard_eeprom.cpp checksum(): negative sum of bytes [0, 0x1e]
        s -= b[i]
    b[0x1F] = s & 0xFF
    return bytearray(b) + bytearray(b"\xff" * (256 - 32))


def mb_eeprom_image(product: int, revision: int, revision_compat: int, name: str,
                    serial: str, ips) -> bytearray:
    """x300_eeprom_map (host/lib/usrp/x300/x300_mb_eeprom.cpp); 256 bytes."""
    import socket
    import struct
    b = bytearray(b"\xff" * 256)
    struct.pack_into("<HHH", b, 0, revision, product, revision_compat)
    b[6:8] = b"\x00\x00"
    b[8:14] = bytes([0x00, 0x50, 0xC2, 0x85, 0x3F, 0xFF])
    b[16:22] = bytes([0x00, 0x50, 0xC2, 0x85, 0x3F, 0x33])
    b[14:16] = b"\x00\x00"
    b[22:24] = b"\x00\x00"
    b[24:28] = socket.inet_aton("192.168.10.1")
    for i in range(4):
        b[28 + 4 * i:32 + 4 * i] = socket.inet_aton("255.255.255.0")
        b[44 + 4 * i:48 + 4 * i] = socket.inet_aton(ips[i])
    b[60:76] = b"\x00" * 16
    b[76:99] = name.encode()[:22].ljust(23, b"\x00")
    b[99:108] = serial.encode()[:8].ljust(9, b"\x00")
    return b
