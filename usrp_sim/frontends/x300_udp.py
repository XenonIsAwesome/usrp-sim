"""X300 network protocol front-end: UDP 49152 (firmware/ZPU peek-poke + discovery),
49153 (CHDR), 49158 (MTU discovery echo); 49157/49159 (FPGA programming/readback) refuse."""
from __future__ import annotations

import logging
import socket
import struct
import threading
from typing import List

from ..devices.x300 import X300Device

log = logging.getLogger("usrp_sim.udp")

FW_PORT, CHDR_PORT, MTU_PORT, PROG_PORT, READ_PORT = 49152, 49153, 49158, 49157, 49159
MTU_REQUEST, MTU_REPLY, MTU_ERROR = 1, 2, 4


class X300Server:
    def __init__(self, dev: X300Device):
        self.dev = dev
        self.bind = dev.cfg.bind
        self.socks = {}
        self.threads: List[threading.Thread] = []
        self.stop = threading.Event()
        for port in (FW_PORT, CHDR_PORT, MTU_PORT, PROG_PORT, READ_PORT):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            if port in (CHDR_PORT, FW_PORT):
                for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
                    try:
                        s.setsockopt(socket.SOL_SOCKET, opt, 16 << 20)
                    except OSError:
                        pass
            try:
                s.bind((self.bind, port))
            except OSError as e:
                for o in self.socks.values():
                    o.close()
                raise OSError("cannot bind %s:%d (%s) -- is another usrp-sim or a UHD device using it?"
                              % (self.bind, port, e))
            s.settimeout(0.2)
            self.socks[port] = s
        dev.engine.set_sender(self._send_chdr)

    def _send_chdr(self, data: bytes, addr):
        try:
            self.socks[CHDR_PORT].sendto(data, addr)
        except OSError as e:
            log.debug("chdr send to %s failed: %s", addr, e)

    def start(self):
        for port, fn in ((FW_PORT, self._fw), (CHDR_PORT, self._chdr), (MTU_PORT, self._mtu),
                         (PROG_PORT, self._refuse), (READ_PORT, self._refuse)):
            t = threading.Thread(target=fn, args=(self.socks[port],), name="udp-%d" % port, daemon=True)
            t.start()
            self.threads.append(t)
        log.info("%s listening on %s (UDP %d/%d/%d)", self.dev.cfg.name, self.bind, FW_PORT, CHDR_PORT, MTU_PORT)

    def _fw(self, s):
        e = self.dev.engine
        while not self.stop.is_set():
            try:
                data, addr = s.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                reply, extra = self.dev.fw_request(data)
            except Exception:
                log.exception("fw request failed")
                continue
            if reply is None:
                continue
            if extra > 0:
                e.sched.call_later(extra, lambda r=reply, a=addr: s.sendto(r, a))
            else:
                s.sendto(reply, addr)

    def _chdr(self, s):
        buf = bytearray(70000)
        mv = memoryview(buf)
        handle = self.dev.graph.handle_packet
        while not self.stop.is_set():
            try:
                n, addr = s.recvfrom_into(buf)
            except socket.timeout:
                continue
            except OSError:
                return
            handle(bytes(mv[:n]), addr)

    def _mtu(self, s):
        """x300_eth_mgr.cpp determine_max_frame_size: reply length = requested size,
        size field = received length."""
        while not self.stop.is_set():
            try:
                data, addr = s.recvfrom(70000)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 8:
                continue
            flags, size = struct.unpack(">II", data[:8])
            if not flags & MTU_REQUEST:
                continue
            out = struct.pack(">II", MTU_REPLY, len(data)).ljust(max(8, min(size, 65000)), b"\x00")
            try:
                s.sendto(out, addr)
            except OSError:
                pass

    def _refuse(self, s):
        while not self.stop.is_set():
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                s.sendto(struct.pack(">I", 2), addr)       # *_FLAGS_ERROR
            except OSError:
                pass

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=1.5)
        for s in self.socks.values():
            s.close()
