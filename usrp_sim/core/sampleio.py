"""Sample sources (RX data) and sinks (TX data): files, sockets, generators.

All data is handled internally as interleaved little-endian int16 I/Q ("sc16",
4 bytes/sample).  ``format = "fc32"`` files/sockets (interleaved float32) are
converted on the fly.  Sources never block the stream for long: when a source runs
dry the missing samples are zero-filled and reported through ``starved``.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Optional, Tuple

import numpy as np

from ..config import ChannelIO

log = logging.getLogger("usrp_sim.io")
BYTES_PER_SAMPLE = 4


def _fc32_to_sc16(b: bytes) -> bytes:
    a = np.frombuffer(b, dtype="<f4")
    return np.clip(np.rint(a * 32767.0), -32768, 32767).astype("<i2").tobytes()


def _sc16_to_fc32(b: bytes) -> bytes:
    return (np.frombuffer(b, dtype="<i2").astype("<f4") / 32768.0).astype("<f4").tobytes()


def _split_hostport(s: str) -> Tuple[str, int]:
    host, _, port = s.rpartition(":")
    return host, int(port)


class Source:
    """Base class. ``read(n)`` returns (bytes, starved) with len(bytes) <= 4*n.

    ``eof`` becomes True when a non-repeating source is exhausted (stream ends).
    """
    eof = False
    can_starve = False

    def read(self, nsamps: int, wait: float = 0.0) -> Tuple[bytes, bool]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ZerosSource(Source):
    def read(self, n, wait=0.0):
        return bytes(n * BYTES_PER_SAMPLE), False


class RampSource(Source):
    """Counter test pattern: sample k = (I = k mod 2**16, Q = ~k mod 2**16), int16."""

    def __init__(self):
        self.k = 0

    def read(self, n, wait=0.0):
        k = np.arange(self.k, self.k + n, dtype=np.int64)
        self.k += n
        out = np.empty(2 * n, dtype="<u2")
        out[0::2] = (k & 0xFFFF).astype("<u2")
        out[1::2] = ((~k) & 0xFFFF).astype("<u2")
        return out.tobytes(), False


class ToneSource(Source):
    def __init__(self, freq_hz: float, rate_hz_getter, amp: float):
        self.freq, self.rate_get, self.amp, self.k = freq_hz, rate_hz_getter, amp, 0

    def read(self, n, wait=0.0):
        rate = max(1.0, float(self.rate_get()))
        t = (np.arange(self.k, self.k + n)) / rate
        self.k += n
        out = np.empty(2 * n, dtype="<i2")
        out[0::2] = np.rint(32767 * self.amp * np.cos(2 * np.pi * self.freq * t))
        out[1::2] = np.rint(32767 * self.amp * np.sin(2 * np.pi * self.freq * t))
        return out.tobytes(), False


class NoiseSource(Source):
    def __init__(self, amp: float, seed: int):
        self.amp, self.rng = amp, np.random.default_rng(seed)

    def read(self, n, wait=0.0):
        return (self.rng.normal(0, 32767 * self.amp / 3, 2 * n).clip(-32768, 32767)
                .astype("<i2").tobytes()), False


class FileSource(Source):
    def __init__(self, path: str, fmt: str, repeat: bool):
        self.path, self.fmt, self.repeat = path, fmt, repeat
        self.f = open(path, "rb")
        self.bps = BYTES_PER_SAMPLE
        if os.fstat(self.f.fileno()).st_size < self.bps:
            raise ValueError("source file %s holds no complete sample" % path)

    def read(self, n, wait=0.0):
        want = n * self.bps
        buf = b""
        while len(buf) < want:
            chunk = self.f.read(want - len(buf))
            if not chunk:
                if self.repeat:
                    self.f.seek(0)
                    continue
                self.eof = True
                break
            buf += chunk
        buf = buf[: len(buf) - len(buf) % self.bps]
        if self.fmt == "fc32":
            buf = _fc32_to_sc16(buf)
        return buf, False

    def close(self):
        self.f.close()


class SocketSource(Source):
    """udp/tcp/unix payload reader with a background thread and bounded buffer."""
    can_starve = True
    MAX_BUF = 64 << 20

    def __init__(self, io: ChannelIO):
        self.io, self.buf = io, bytearray()
        self.cv = threading.Condition()
        self.stop = False
        self.conn_error: Optional[str] = None
        self.peer_closed = False          # stream sockets: the sender closed -> end of data
        self.srv: Optional[socket.socket] = None
        self.in_bps = BYTES_PER_SAMPLE if io.format == "sc16" else 8   # wire bytes/sample
        self.t = threading.Thread(target=self._run, daemon=True, name="rx-src-" + io.kind)
        self._open()
        self.t.start()

    # -- socket setup
    def _open(self):
        io = self.io
        if io.kind == "udp":
            host, port = _split_hostport(io.listen or io.connect)
            self.srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            except OSError:
                pass
            self.srv.bind((host, port))
        elif io.kind == "tcp" and io.listen:
            host, port = _split_hostport(io.listen)
            self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.bind((host, port))
            self.srv.listen(1)
        elif io.kind == "unix" and (io.listen or (io.path and not io.connect)):
            path = io.listen or io.path
            if os.path.exists(path):
                os.unlink(path)
            self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.srv.bind(path)
            self.srv.listen(1)
            self._unlink = path
        self.srv and self.srv.settimeout(0.2)

    def _push(self, b: bytes):
        if self.io.format == "fc32":
            n = len(b) - len(b) % 8
            b = _fc32_to_sc16(b[:n])
        with self.cv:
            if len(self.buf) + len(b) > self.MAX_BUF:      # drop oldest-new data; counts as overrun
                b = b[: max(0, self.MAX_BUF - len(self.buf))]
            self.buf += b
            self.cv.notify_all()

    def _run(self):
        io = self.io
        try:
            while not self.stop:
                if io.kind == "udp":
                    try:
                        data, _ = self.srv.recvfrom(65536)
                    except socket.timeout:
                        continue
                    self._push(data)
                    continue
                # stream sockets: accept or connect, then read until EOF
                conn = None
                if self.srv is not None:
                    try:
                        conn, _ = self.srv.accept()
                    except socket.timeout:
                        continue
                else:
                    try:
                        if io.kind == "tcp":
                            host, port = _split_hostport(io.connect)
                            conn = socket.create_connection((host, port), timeout=1.0)
                        else:
                            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                            conn.settimeout(1.0)
                            conn.connect(io.connect or io.path)
                    except OSError:
                        time.sleep(0.2)
                        continue
                conn.settimeout(0.2)
                self.peer_closed = False
                carry = b""
                while not self.stop:
                    try:
                        data = conn.recv(1 << 18)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not data:
                        with self.cv:
                            self.peer_closed = True
                            self.cv.notify_all()
                        break
                    data = carry + data
                    cut = len(data) - len(data) % self.in_bps
                    carry = data[cut:]
                    if cut:
                        self._push(data[:cut])
                conn.close()
                if not (io.listen or io.kind in ("tcp", "unix")):
                    break
        except Exception as e:  # pragma: no cover
            self.conn_error = str(e)
            log.exception("socket source failed")

    def read(self, n, wait=0.0):
        want = n * BYTES_PER_SAMPLE
        deadline = time.monotonic() + wait
        with self.cv:
            while len(self.buf) < want and wait > 0:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    break
                self.cv.wait(rem)
            got = bytes(self.buf[:want])
            del self.buf[:len(got)]
            if len(got) < want and self.peer_closed and not self.buf:
                self.eof = True                      # sender hung up and everything was read: clean end
        return got, len(got) < want and not self.eof   # caller zero-fills a real shortfall

    def close(self):
        self.stop = True
        with self.cv:
            self.cv.notify_all()
        for s in (self.srv,):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self.t.join(timeout=1.5)
        if getattr(self, "_unlink", None):
            try:
                os.unlink(self._unlink)
            except OSError:
                pass


def make_source(io: Optional[ChannelIO], rate_getter=lambda: 1e6) -> Source:
    if io is None:
        return ZerosSource()
    k = io.kind
    if k == "zeros":
        return ZerosSource()
    if k == "ramp":
        return RampSource()
    if k == "tone":
        return ToneSource(io.freq_hz, rate_getter, io.amplitude)
    if k == "noise":
        return NoiseSource(io.amplitude, io.seed)
    if k == "file":
        return FileSource(io.path, io.format, io.repeat)
    return SocketSource(io)


# ------------------------------------------------------------------------ sinks
class Sink:
    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class NullSink(Sink):
    def write(self, data):
        pass


class FileSink(Sink):
    def __init__(self, path: str, fmt: str):
        self.f = open(path, "wb", buffering=0)   # unbuffered: data is visible to readers immediately
        self.fmt = fmt

    def write(self, data):
        self.f.write(_sc16_to_fc32(data) if self.fmt == "fc32" else data)

    def close(self):
        self.f.flush()
        self.f.close()


class SocketSink(Sink):
    """udp (sendto), tcp/unix (client, or server accepting lazily). Never blocks the
    stream for long: with no peer the data is dropped and counted."""

    def __init__(self, io: ChannelIO):
        self.io, self.sock, self.srv = io, None, None
        self.dropped = 0
        self.peer = None
        self._unlink = None
        if io.kind == "udp":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.peer = _split_hostport(io.connect or io.listen)
        elif io.kind == "tcp" and io.listen and not io.connect:
            host, port = _split_hostport(io.listen)
            self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.bind((host, port))
            self.srv.listen(1)
            self.srv.setblocking(False)
        elif io.kind == "unix" and (io.listen or (io.path and not io.connect)):
            path = io.listen or io.path
            if os.path.exists(path):
                os.unlink(path)
            self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.srv.bind(path)
            self.srv.listen(1)
            self.srv.setblocking(False)
            self._unlink = path
        self.last_try = 0.0

    def _ensure(self):
        if self.sock is not None:
            return True
        if self.srv is not None:
            try:
                self.sock, _ = self.srv.accept()
                self.sock.settimeout(2.0)
                return True
            except (BlockingIOError, OSError):
                return False
        if time.monotonic() - self.last_try < 0.2:
            return False
        self.last_try = time.monotonic()
        try:
            if self.io.kind == "tcp":
                host, port = _split_hostport(self.io.connect)
                self.sock = socket.create_connection((host, port), timeout=0.5)
            else:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(0.5)
                self.sock.connect(self.io.connect or self.io.path)
            self.sock.settimeout(2.0)
            return True
        except OSError:
            self.sock = None
            return False

    def write(self, data):
        if self.io.format == "fc32":
            data = _sc16_to_fc32(data)
        if self.io.kind == "udp":
            for i in range(0, len(data), 8192):
                try:
                    self.sock.sendto(data[i:i + 8192], self.peer)
                except OSError:
                    self.dropped += 1
            return
        if not self._ensure():
            self.dropped += len(data)
            return
        try:
            self.sock.sendall(data)
        except OSError:
            self.sock.close()
            self.sock = None
            self.dropped += len(data)

    def close(self):
        for s in (self.sock, self.srv):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        if self._unlink:
            try:
                os.unlink(self._unlink)
            except OSError:
                pass


def make_sink(io: Optional[ChannelIO]) -> Sink:
    if io is None or io.kind == "null":
        return NullSink()
    if io.kind == "file":
        return FileSink(io.path, io.format)
    return SocketSink(io)
