"""Measure wall-clock times of config operations through the UHD Python API."""
import json
import sys
import time

import uhd

addr = sys.argv[1]
res = {}


def timeit(f):
    a = time.perf_counter()
    f()
    return (time.perf_counter() - a) * 1e3


def tune(u, f):
    u.set_rx_freq(uhd.types.TuneRequest(f), 0)
    while not u.get_rx_sensor("lo_locked", 0).to_bool():
        time.sleep(0.001)


for mcr in (200e6, 184.32e6):
    t = {}
    box = {}
    t["create"] = timeit(lambda: box.__setitem__("u", uhd.usrp.MultiUSRP("%s,master_clock_rate=%g" % (addr if "=" in addr else "type=x300,addr=%s" % addr, mcr))))
    u = box["u"]
    tune(u, 1.0e9)
    t["retune"] = [timeit(lambda f=f: tune(u, f)) for f in (1.1e9, 2.4e9, 1.1e9)]
    res[str(int(mcr))] = t
    del u, box
print(json.dumps(res))
