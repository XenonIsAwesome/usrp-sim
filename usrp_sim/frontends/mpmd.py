"""MPMD protocol front-end (Tier 2): MPM discovery (UDP 49600) and msgpack-RPC (TCP 49601).

Pure Python (stdlib + the `msgpack` package); no mprpc/gevent.  Client side reference:
host/lib/usrp/mpmd/{mpmd_find.cpp,mpmd_mboard_impl.cpp,mpmd_mb_iface.cpp,mpmd_prop_tree.cpp},
host/lib/include/uhdlib/usrp/common/rpc.py (every RPC the client can issue), server side
behaviour: mpm/python/usrp_mpm/{discovery.py,rpc_server.py,periph_manager/base.py}.

msgpack-rpc framing (rpclib): request [0, msgid, method, params], response
[1, msgid, error, result], notification [2, method, params].
"""
from __future__ import annotations

import logging
import secrets
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional

import msgpack

log = logging.getLogger("usrp_sim.mpmd")

DISCOVERY_PORT, RPC_PORT = 49600, 49601
# RPCs that the client issues WITHOUT the claim token (request<> instead of request_with_token<>)
NO_TOKEN = {"claim", "ping", "get_device_info", "get_dboard_info", "get_mpm_compat_num", "get_proto_ver",
            "get_chdr_width", "get_last_error", "list_updateable_components", "get_device_id_nt"}


class RpcError(Exception):
    pass


class ClaimState:
    """MPM claim logic: one session owns the device until it unclaims or stops refreshing."""

    def __init__(self, timeout_s: float):
        self.timeout_s = timeout_s
        self.token = ""
        self.session = ""
        self.seen = 0.0
        self.lock = threading.Lock()

    def claimed(self) -> bool:
        with self.lock:
            if self.token and time.monotonic() - self.seen > self.timeout_s:
                log.info("claim by session %r expired", self.session)
                self.token = ""
            return bool(self.token)

    def claim(self, session: str) -> str:
        if self.claimed():
            raise RpcError("Device is already claimed")
        with self.lock:
            self.token = secrets.token_hex(16)
            self.session, self.seen = session, time.monotonic()
            return self.token

    def reclaim(self, token: str) -> bool:
        if not self.claimed():
            return False
        with self.lock:
            if token != self.token:
                return False
            self.seen = time.monotonic()
            return True

    def unclaim(self, token: str) -> bool:
        with self.lock:
            if token == self.token:
                self.token = ""
                return True
            return False

    def valid(self, token) -> bool:
        return self.claimed() and token == self.token


class MpmdServer:
    """Serves discovery + RPC for a device object exposing:
       ``discovery_fields() -> dict`` and ``rpc_methods() -> {name: callable}``."""

    def __init__(self, device, bind: str, claim_timeout_s: float = 3.0):
        self.dev, self.bind = device, bind
        self.claim = ClaimState(claim_timeout_s)
        self.stop = threading.Event()
        self.last_error = ""
        self.methods: Dict[str, Callable] = dict(device.rpc_methods())
        self.methods.update({"claim": self._claim, "reclaim": lambda tok: self.claim.reclaim(tok),
                             "unclaim": lambda tok: self.claim.unclaim(tok), "ping": lambda p="": p,
                             "get_last_error": lambda: self.last_error})
        self.disc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.disc.bind((bind, DISCOVERY_PORT))
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.bind((bind, RPC_PORT))
        except OSError as e:
            self.disc.close()
            self.srv.close()
            raise OSError("cannot bind %s ports %d/%d for MPMD (%s)" % (bind, DISCOVERY_PORT, RPC_PORT, e))
        self.srv.listen(16)
        self.disc.settimeout(0.2)
        self.srv.settimeout(0.2)
        self.threads = []
        self.conns = []

    def _claim(self, session=""):
        return self.claim.claim(str(session))

    # ---- discovery (mpm/python/usrp_mpm/discovery.py)
    def _discovery_response(self) -> bytes:
        f = self.dev.discovery_fields()
        parts = ["USRP-MPM", "type=%s" % f["type"], "product=%s" % f["product"], "serial=%s" % f["serial"],
                 "name=%s" % f.get("name", ""), "fpga=%s" % f.get("fpga", ""),
                 "claimed=%s" % self.claim.claimed()]
        return ";".join(parts).encode()

    def _disc_loop(self):
        while not self.stop.is_set():
            try:
                data, sender = self.disc.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                return
            d = data.strip(b"\0")
            if d == b"MPM-DISC":
                self.disc.sendto(self._discovery_response(), sender)
            elif d.startswith(b"MPM-ECHO"):
                self.disc.sendto(data, sender)

    # ---- RPC
    def dispatch(self, method: str, params: list):
        fn = self.methods.get(method)
        if fn is None:
            log.warning("RPC method not implemented by the simulator: %s%r", method, tuple(params)[:4])
            raise RpcError("Unknown method: %s" % method)
        log.debug("rpc %s%s", method, tuple(params)[:4])
        if method not in NO_TOKEN and method not in ("reclaim", "unclaim") and params and isinstance(params[0], str) \
                and self.claim.token and params[0] == self.claim.token:
            params = params[1:]
        elif method not in NO_TOKEN and method not in ("reclaim", "unclaim"):
            if not self.claim.valid(params[0] if params else None):
                raise RpcError("Invalid or missing claim token for %s" % method)
            params = params[1:]
        return fn(*params)

    def _serve(self, conn: socket.socket, addr):
        up = msgpack.Unpacker(raw=False, strict_map_key=False)
        conn.settimeout(0.5)
        try:
            while not self.stop.is_set():
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    return
                up.feed(data)
                for msg in up:
                    if not isinstance(msg, (list, tuple)) or not msg:
                        continue
                    if msg[0] == 0 and len(msg) == 4:
                        _, msgid, method, params = msg
                        try:
                            res, err = self.dispatch(method, list(params)), None
                        except Exception as e:
                            if not isinstance(e, RpcError):
                                log.exception("RPC %s failed", method)
                            self.last_error = str(e)
                            res, err = None, str(e)
                        conn.sendall(msgpack.packb([1, msgid, err, res], use_bin_type=True))
                    elif msg[0] == 2 and len(msg) == 3:
                        try:
                            self.dispatch(msg[1], list(msg[2]))
                        except Exception as e:
                            self.last_error = str(e)
                            log.warning("RPC notification %s failed: %s", msg[1], e)
        except OSError:
            pass
        finally:
            conn.close()

    def _accept_loop(self):
        while not self.stop.is_set():
            try:
                conn, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            t = threading.Thread(target=self._serve, args=(conn, addr), daemon=True, name="rpc-conn")
            t.start()
            self.threads.append(t)

    def start(self):
        for fn, n in ((self._disc_loop, "mpm-discovery"), (self._accept_loop, "mpm-rpc")):
            t = threading.Thread(target=fn, daemon=True, name=n)
            t.start()
            self.threads.append(t)
        log.info("MPMD discovery %s:%d and RPC %s:%d ready", self.bind, DISCOVERY_PORT, self.bind, RPC_PORT)

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=2)
        self.disc.close()
        self.srv.close()
