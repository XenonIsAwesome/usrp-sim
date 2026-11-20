"""MPMD front-end unit tests with a pure-Python msgpack-rpc client (no UHD needed)."""
import socket
import time

import msgpack
import pytest

from usrp_sim.frontends import mpmd


class Dev:
    def discovery_fields(self):
        return dict(type="n3xx", product="n310", serial="X1", name="n", fpga="HG")

    def rpc_methods(self):
        return {"get_device_info": lambda: {"serial": "X1"}, "add": lambda a, b: a + b,
                "boom": lambda: 1 / 0, "tokened": lambda x: x * 2}


class Client:
    def __init__(self, port_host):
        self.s = socket.create_connection(port_host, timeout=3)
        self.up = msgpack.Unpacker(raw=False, strict_map_key=False)
        self.i = 0

    def call(self, method, *params):
        self.i += 1
        self.s.sendall(msgpack.packb([0, self.i, method, list(params)], use_bin_type=True))
        while True:
            for msg in self.up:
                assert msg[0] == 1 and msg[1] == self.i
                return msg[2], msg[3]
            self.up.feed(self.s.recv(4096))

    def notify(self, method, *params):
        self.s.sendall(msgpack.packb([2, method, list(params)], use_bin_type=True))


@pytest.fixture
def srv():
    s = mpmd.MpmdServer(Dev(), "127.0.0.90", claim_timeout_s=0.5)
    s.start()
    yield s
    s.close()


def test_discovery_and_echo(srv):
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(1)
    u.sendto(b"MPM-DISC", ("127.0.0.90", mpmd.DISCOVERY_PORT))
    r = u.recvfrom(4096)[0].decode().split(";")
    assert r[0] == "USRP-MPM" and "type=n3xx" in r and "serial=X1" in r and "claimed=False" in r
    u.sendto(b"MPM-ECHO12345", ("127.0.0.90", mpmd.DISCOVERY_PORT))
    assert u.recvfrom(4096)[0] == b"MPM-ECHO12345"


def test_rpc_requests_errors_and_token_rules(srv):
    c = Client(("127.0.0.90", mpmd.RPC_PORT))
    assert c.call("get_device_info") == (None, {"serial": "X1"})        # no token needed
    assert c.call("ping", "1234567890") == (None, "1234567890")
    err, _ = c.call("tokened", 4)
    assert "token" in err                                               # needs the claim token
    err, _ = c.call("nope")
    assert "Unknown method" in err
    tok = c.call("claim", "sessionA")[1]
    assert isinstance(tok, str) and len(tok) == 32
    assert c.call("tokened", tok, 4) == (None, 8)
    assert c.call("add", tok, 2, 3) == (None, 5)
    err, _ = c.call("boom", tok)
    assert err and "division" in err
    assert "division" in c.call("get_last_error")[1]
    c.notify("tokened", tok, 1)                                         # notifications get no reply


def test_claim_exclusive_reclaim_expiry_unclaim(srv):
    a, b = Client(("127.0.0.90", mpmd.RPC_PORT)), Client(("127.0.0.90", mpmd.RPC_PORT))
    tok = a.call("claim", "A")[1]
    assert "already claimed" in b.call("claim", "B")[0]
    time.sleep(0.3)
    assert a.call("reclaim", tok) == (None, True)                       # refresh
    time.sleep(0.3)
    assert a.call("reclaim", tok) == (None, True)
    assert srv.claim.claimed()
    time.sleep(0.7)                                                     # not refreshed -> expires
    assert not srv.claim.claimed() and a.call("reclaim", tok) == (None, False)
    tok2 = b.call("claim", "B")[1]
    assert b.call("unclaim", tok2) == (None, True) and not srv.claim.claimed()
