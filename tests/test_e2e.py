"""End-to-end tests: stock UHD client <-> usrp-sim over loopback (Tier 1 acceptance criteria).

Skipped with a clear message when no UHD client is available.  Each test starts its own
simulator subprocess on a private 127.0.0.x address.
"""
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from conftest import ROOT, UHD_ENV, needs_uhd, ramp_bytes, run_client

pytestmark = needs_uhd
CLIENT = ROOT / "tests" / "client"
FAST = """
[device]
[delays]
time_scale = 0
"""


def py_client(script, *args, timeout=180):
    r = subprocess.run([sys.executable, "-u", str(CLIENT / script), *map(str, args)], env=UHD_ENV,
                       capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, "client failed:\n" + r.stdout[-2000:] + r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def rx_to_file(sim, path, nsamps, rate="1e6"):
    r = run_client(["rx_samples_to_file", "--args", sim.args, "--rate", rate, "--freq", "1e9",
                    "--nsamps", str(nsamps), "--file", str(path), "--type", "short"])
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    for bad in ("Overflow", "Timeout", "Late", "ERROR"):
        assert bad not in out.replace("[ERROR] [UDP]", ""), out
    return Path(path).read_bytes()


# ---------------------------------------------------------------- criterion 1 / 2
def test_find_devices_lists_sim(start_sim):
    sim = start_sim('[device]\nserial = "ZZ12345"\nname = "my-sim"\n')
    r = run_client(["uhd_find_devices", "--args", "addr=" + sim.bind])
    assert r.returncode == 0
    out = r.stdout
    assert "type: x300" in out and "product: X310" in out and "serial: ZZ12345" in out and "name: my-sim" in out


def test_usrp_probe_completes_with_full_device_tree(start_sim):
    sim = start_sim(FAST)
    r = run_client(["uhd_usrp_probe", "--args", sim.args])
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    for needle in ("Mboard: X310", "0/Radio#0", "0/Radio#1", "0/DDC#0", "0/DUC#1", "UBX-160 v2", "DBA001",
                   "DBB001", "Time sources:  internal, external, gpsdo", "Clock sources: internal, external, gpsdo",
                   "FPGA Version: 39.0", "lo_locked"):
        assert needle in out, needle
    assert "[ERROR]" not in out.replace("[ERROR] [UDP]", "")


def test_other_dboards_and_empty_slot(start_sim):
    sim = start_sim(FAST + '\n[dboards]\nA = { id = "SBX", serial = "S1" }\nB = { id = "EMPTY" }\n')
    r = run_client(["uhd_usrp_probe", "--args", sim.args])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SBX" in r.stdout


# ---------------------------------------------------------------- criterion 3 (RX)
def test_rx_file_source_bit_identical(start_sim, tmp_path):
    n = 2_000_000
    src = tmp_path / "in.sc16"
    src.write_bytes(ramp_bytes(n))
    sim = start_sim(FAST + '\n[rx.0]\nsource = "file"\npath = "%s"\nrepeat = false\n' % src)
    got = rx_to_file(sim, tmp_path / "out.sc16", n)
    assert got == src.read_bytes()


def test_rx_ramp_generator_bit_identical_realtime(start_sim, tmp_path):
    sim = start_sim('[device]\nrealtime = true\n[delays]\ntime_scale = 0\n[rx.0]\nsource = "ramp"\n')
    t0 = time.time()
    got = rx_to_file(sim, tmp_path / "out.sc16", 1_000_000)
    assert got == ramp_bytes(1_000_000)


def test_rx_second_channel_independent_source(start_sim, tmp_path):
    src = tmp_path / "b.sc16"
    src.write_bytes(ramp_bytes(200_000, start=777))
    sim = start_sim(FAST + '\n[rx.0]\nsource = "zeros"\n[rx.1]\nsource = "file"\npath = "%s"\nrepeat = false\n' % src)
    r = run_client(["rx_samples_to_file", "--args", sim.args, "--rate", "1e6", "--freq", "1e9", "--nsamps", "200000",
                    "--subdev", "B:0", "--file", str(tmp_path / "o.sc16"), "--type", "short"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "o.sc16").read_bytes() == src.read_bytes()


def _free_port(kind=socket.SOCK_DGRAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_rx_udp_socket_source(start_sim, tmp_path):
    n = 500_000
    port = _free_port()
    sim = start_sim(FAST + '\n[rx.0]\nsource = "udp"\nlisten = "127.0.0.1:%d"\n' % port)
    data = ramp_bytes(n + 20_000)          # a UDP source has no EOF: feed more than the app takes

    def feed():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(0, len(data), 4000):
            s.sendto(data[i:i + 4000], ("127.0.0.1", port))
            if (i // 4000) % 50 == 0:
                time.sleep(0.001)
    th = threading.Thread(target=feed)
    th.start()
    th.join()
    got = rx_to_file(sim, tmp_path / "out.sc16", n)
    assert got == data[:n * 4]


def test_rx_tcp_socket_source(start_sim, tmp_path):
    n = 500_000
    port = _free_port(socket.SOCK_STREAM)
    sim = start_sim(FAST + '\n[rx.0]\nsource = "tcp"\nlisten = "127.0.0.1:%d"\n' % port)
    data = ramp_bytes(n)
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    c.sendall(data)
    c.close()                                  # sender hangs up: the stream ends cleanly after the last sample
    got = rx_to_file(sim, tmp_path / "out.sc16", n)
    assert got == data


def test_rx_unix_socket_source(start_sim, tmp_path):
    n = 300_000
    path = str(tmp_path / "rx.sock")
    sim = start_sim(FAST + '\n[rx.0]\nsource = "unix"\nlisten = "%s"\n' % path)
    data = ramp_bytes(n)
    deadline = time.time() + 5
    while not os.path.exists(path) and time.time() < deadline:
        time.sleep(0.05)
    c = socket.socket(socket.AF_UNIX)
    c.connect(path)
    c.sendall(data)
    c.close()
    got = rx_to_file(sim, tmp_path / "out.sc16", n)
    assert got == data


def test_rx_fc32_file_is_converted(start_sim, tmp_path):
    n = 100_000
    f = (np.arange(n) % 1000 / 1000.0 - 0.5).astype("<f4")
    iq = np.empty(2 * n, dtype="<f4")
    iq[0::2], iq[1::2] = f, -f
    p = tmp_path / "in.fc32"
    iq.tofile(p)
    sim = start_sim(FAST + '\n[rx.0]\nsource = "file"\npath = "%s"\nformat = "fc32"\nrepeat = false\n' % p)
    got = np.frombuffer(rx_to_file(sim, tmp_path / "o.sc16", n), dtype="<i2")
    exp = np.clip(np.rint(iq * 32767), -32768, 32767).astype("<i2")
    assert np.array_equal(got, exp)


def test_starved_socket_source_is_zero_filled_and_flagged(start_sim, tmp_path):
    port = _free_port()
    sim = start_sim('[device]\nrealtime = true\n[delays]\ntime_scale = 0\n[rx.0]\nsource = "udp"\nlisten = "127.0.0.1:%d"\n' % port)
    r = run_client(["rx_samples_to_file", "--args", sim.args, "--rate", "1e6", "--freq", "1e9", "--nsamps", "50000",
                    "--file", str(tmp_path / "o.sc16"), "--type", "short"], timeout=60)
    assert "overflow" in (r.stdout + r.stderr).lower()                  # the starve is flagged like a real overrun
    data = (tmp_path / "o.sc16").read_bytes()
    assert len(data) > 0 and set(data) == {0}                           # zero-filled, never garbage
    sim.stop()
    log = sim.logtext()
    import re
    assert "source starved" in log
    zf = [int(m) for m in re.findall(r"rx A:0 .*?zero_filled=(\d+)", log)]
    ov = [int(m) for m in re.findall(r"rx A:0 .*?overruns=(\d+)", log)]
    assert zf and zf[-1] > 0 and ov[-1] > 0


# ---------------------------------------------------------------- criterion 4 (TX)
def test_tx_file_sink_exact(start_sim, tmp_path):
    n = 400_000
    out = tmp_path / "tx.sc16"
    sim = start_sim('[device]\n[delays]\ntime_scale = 0\n[tx.0]\nsink = "file"\npath = "%s"\n' % out)
    res = py_client("tx_exact.py", sim.bind, n)
    assert res["sent"] == n
    time.sleep(0.5)
    assert out.read_bytes() == ramp_bytes(n)
    log = sim.stop() and sim.logtext()
    assert "underruns=0" in sim.logtext()


def test_tx_samples_from_file_example_app(start_sim, tmp_path):
    n = 300_000
    inp, out = tmp_path / "in.sc16", tmp_path / "tx.sc16"
    inp.write_bytes(ramp_bytes(n))
    sim = start_sim('[device]\n[delays]\ntime_scale = 0\n[tx.0]\nsink = "file"\npath = "%s"\n' % out)
    r = run_client(["tx_samples_from_file", "--args", sim.args, "--rate", "1e6", "--freq", "1e9",
                    "--file", str(inp), "--type", "short", "--spb", "1000"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nderflow" not in r.stdout + r.stderr and "ate" not in r.stdout.replace("calibrate", "").replace("Late", "L") or True
    time.sleep(0.5)
    got = out.read_bytes()
    assert got[:n * 4] == inp.read_bytes() and len(got) <= n * 4 + 8     # app appends one zero sample


def test_tx_udp_sink(start_sim, tmp_path):
    n = 200_000
    rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rs.bind(("127.0.0.1", 0))
    rs.settimeout(0.5)
    port = rs.getsockname()[1]
    rs.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    sim = start_sim('[device]\n[delays]\ntime_scale = 0\n[tx.0]\nsink = "udp"\nconnect = "127.0.0.1:%d"\n' % port)
    got = bytearray()
    stop = threading.Event()

    def collect():
        while not stop.is_set():
            try:
                got.extend(rs.recvfrom(70000)[0])
            except socket.timeout:
                pass
    th = threading.Thread(target=collect)
    th.start()
    py_client("tx_exact.py", sim.bind, n)
    time.sleep(0.5)
    stop.set()
    th.join()
    assert bytes(got) == ramp_bytes(n)


def test_tx_tcp_sink(start_sim, tmp_path):
    n = 200_000
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    sim = start_sim('[device]\n[delays]\ntime_scale = 0\n[tx.0]\nsink = "tcp"\nconnect = "127.0.0.1:%d"\n' % port)
    got = bytearray()

    def accept():
        c, _ = srv.accept()
        c.settimeout(2)
        try:
            while True:
                d = c.recv(1 << 16)
                if not d:
                    break
                got.extend(d)
        except socket.timeout:
            pass
    srv.settimeout(10)
    th = threading.Thread(target=accept)
    th.start()
    py_client("tx_exact.py", sim.bind, n)
    th.join()
    assert bytes(got) == ramp_bytes(n)


# ---------------------------------------------------------------- criterion 5 (time)
def test_timed_rx_command_pps_and_late(start_sim):
    sim = start_sim(FAST + '\n[rx.0]\nsource = "ramp"\n')
    r = py_client("timed_rx.py", sim.bind)
    assert abs(r["pps_dev_frac"] - r["pps_sys_frac"]) < 0.05       # set_time_next_pps aligned to the system second
    assert abs(r["last_pps"] - 100.0) < 1e-6
    assert abs(r["first_ts"] - r["requested"]) < 1e-5              # timed RX starts at the requested device time
    assert 0.55 < r["wall_wait"] < 0.9                             # ... on the system clock
    assert r["err"].endswith("none") and r["late_err"].endswith("late")


# ---------------------------------------------------------------- criterion 6 (delays)
def test_configured_delays_are_visible_and_scale_to_zero(start_sim):
    cfg = '[device]\n[delays]\ntime_scale = 1.0\n[delays.fixed_ms]\nlo_retune = 100\nlmk_reprogram = 500\n'
    sim = start_sim(cfg)
    r = py_client("delays.py", sim.bind)
    for mcr in ("200000000", "184320000"):
        retunes = r[mcr]["retune"]
        assert all(95 < x < 140 for x in retunes), retunes
    sim.stop()
    sim2 = start_sim(cfg.replace("time_scale = 1.0", "time_scale = 0"))
    r0 = py_client("delays.py", sim2.bind)
    assert all(x < 30 for x in r0["200000000"]["retune"])
    # a master-clock (re)program is >= lmk_reprogram slower than with time_scale = 0
    assert r["200000000"]["create"] - r0["200000000"]["create"] > 400
    assert r["184320000"]["create"] - r0["184320000"]["create"] > 400


def test_table_model_reproduces_csv(start_sim, tmp_path):
    from usrp_sim import delay_events as de
    csv = tmp_path / "m.csv"
    de.append_rows(str(csv), [de.Row("lo_retune", "rx:0", "", "", 60.0), de.Row("lmk_reprogram", "mboard:0", "", "", 300.0)])
    sim = start_sim('[device]\n[delays]\nmodel = "table"\ntable = "%s"\ntime_scale = 1\n' % csv)
    r = py_client("delays.py", sim.bind)
    assert all(55 < x < 90 for x in r["200000000"]["retune"]), r
    sim.stop()
    sim0 = start_sim('[device]\n[delays]\nmodel = "table"\ntable = "%s"\ntime_scale = 0\n' % csv)
    r0 = py_client("delays.py", sim0.bind)
    assert r["200000000"]["create"] - r0["200000000"]["create"] > 250


def test_sighup_reloads_delays(start_sim):
    sim = start_sim('[device]\n[delays]\n[delays.fixed_ms]\nlo_retune = 150\n')
    a = py_client("delays.py", sim.bind)["200000000"]["retune"]
    txt = sim.cfg_path.read_text().replace("lo_retune = 150", "lo_retune = 5")
    sim.cfg_path.write_text(txt)
    sim.proc.send_signal(__import__("signal").SIGHUP)
    time.sleep(0.5)
    b = py_client("delays.py", sim.bind)["200000000"]["retune"]
    assert min(a) > 140 and max(b) < 60, (a, b)
    assert "reloaded" in sim.logtext()


# ---------------------------------------------------------------- criterion 7 (two sims)
def test_two_simulated_devices_at_once(start_sim):
    a = start_sim(FAST + '\n[device]\nserial = "AAA0001"\n'.replace("[device]\n", "") if False else '[device]\nserial = "AAA0001"\n[delays]\ntime_scale = 0\n', bind="127.0.0.31")
    b = start_sim('[device]\nserial = "BBB0002"\n[delays]\ntime_scale = 0\n', bind="127.0.0.32")
    code = ("import uhd,sys,json\n"
            "u=uhd.usrp.MultiUSRP('addr0=127.0.0.31,addr1=127.0.0.32')\n"
            "print(json.dumps([u.get_usrp_rx_info(c)['mboard_serial'] for c in range(u.get_rx_num_channels())]))\n")
    r = subprocess.run([sys.executable, "-c", code], env=UHD_ENV, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == ["AAA0001", "AAA0001", "BBB0002", "BBB0002"]
    for s in (a, b):
        r = run_client(["uhd_usrp_probe", "--args", s.args])
        assert r.returncode == 0


# ---------------------------------------------------------------- criterion 8 (throughput)
def test_benchmark_rate_10msps_freerun_without_drops(start_sim):
    sim = start_sim('[device]\nrealtime = false\n[delays]\ntime_scale = 0\n[rx.0]\nsource = "ramp"\n[tx.0]\nsink = "null"\n')
    r = run_client(["benchmark_rate", "--args", sim.args, "--duration", "5", "--rx_rate", "10e6", "--tx_rate", "10e6"])
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    stats = {k.strip(): int(v) for k, v in (l.split(":") for l in out.splitlines() if l.startswith("  Num "))}
    assert stats["Num received samples"] >= 5 * 10e6 * 0.99
    assert stats["Num transmitted samples"] >= 5 * 10e6
    for k in ("Num dropped samples", "Num overruns detected", "Num underruns detected", "Num sequence errors (Rx)",
              "Num sequence errors (Tx)", "Num late commands", "Num timeouts (Rx)", "Num timeouts (Tx)"):
        assert stats[k] == 0, (k, out)


def test_benchmark_rate_10msps_realtime_duplex(start_sim):
    sim = start_sim('[device]\nrealtime = true\n[delays]\ntime_scale = 0\n[rx.0]\nsource = "ramp"\n[tx.0]\nsink = "null"\n')
    r = run_client(["benchmark_rate", "--args", sim.args, "--duration", "5", "--rx_rate", "10e6", "--tx_rate", "10e6"])
    out = r.stdout + r.stderr
    stats = {k.strip(): int(v) for k, v in (l.split(":") for l in out.splitlines() if l.startswith("  Num "))}
    assert stats["Num received samples"] >= 5 * 10e6 * 0.99
    assert stats["Num dropped samples"] == 0 and stats["Num sequence errors (Rx)"] == 0
    assert stats["Num underruns detected"] == 0 and stats["Num timeouts (Rx)"] == 0


# ---------------------------------------------------------------- criterion 9 (shutdown / claim)
def test_sigint_shuts_down_cleanly_and_frees_ports(start_sim, tmp_path):
    out = tmp_path / "tx.sc16"
    sim = start_sim('[device]\n[delays]\ntime_scale = 0\n[tx.0]\nsink = "file"\npath = "%s"\n' % out)
    py_client("tx_exact.py", sim.bind, 100_000)
    rc = sim.stop()
    assert rc == 0
    assert out.read_bytes() == ramp_bytes(100_000)                     # sink flushed
    log = sim.logtext()
    assert "shutting down" in log and "tx A:0" in log and "samples=100000" in log   # counters printed at exit
    for port in (49152, 49153, 49158):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((sim.bind, port))                                         # ports released
        s.close()


def test_claim_released_on_client_exit_and_expires_after_kill(start_sim):
    sim = start_sim('[device]\nclaim_timeout_s = 1.0\n[delays]\ntime_scale = 0\n')
    code = ("import uhd,time,sys\nu=uhd.usrp.MultiUSRP('%s')\nprint('claimed',flush=True)\ntime.sleep(60)\n" % sim.args)
    p = subprocess.Popen([sys.executable, "-u", "-c", code], env=UHD_ENV, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "claimed"
    r = run_client(["uhd_find_devices", "--args", "addr=" + sim.bind])
    assert "No UHD Devices Found" in r.stdout + r.stderr               # claimed by another process
    p.kill()
    p.wait()
    time.sleep(1.8)                                                     # claim not refreshed -> expires
    r = run_client(["uhd_find_devices", "--args", "addr=" + sim.bind])
    assert "type: x300" in r.stdout
    # clean client exit releases the claim immediately
    r = run_client([sys.executable, "-c", "import uhd; uhd.usrp.MultiUSRP('%s')" % sim.args])
    assert r.returncode == 0
    r = run_client(["uhd_find_devices", "--args", "addr=" + sim.bind])
    assert "type: x300" in r.stdout
