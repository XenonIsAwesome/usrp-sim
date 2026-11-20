"""Transmit exactly N counter-pattern samples (one burst) through the UHD Python API."""
import json
import sys

import numpy as np
import uhd

addr, n = sys.argv[1], int(sys.argv[2])
rate = float(sys.argv[3]) if len(sys.argv) > 3 else 1e6
u = uhd.usrp.MultiUSRP((addr if "=" in addr else "type=x300,addr=%s" % addr))
u.set_tx_rate(rate)
u.set_tx_freq(1e9)
st = uhd.usrp.StreamArgs("sc16", "sc16")
st.channels = [0]
tx = u.get_tx_stream(st)
k = np.arange(n)
a = np.empty(2 * n, dtype="<u2")
a[0::2] = k & 0xFFFF
a[1::2] = (~k) & 0xFFFF
buf = a.view("<i4").reshape(1, -1)
md = uhd.types.TXMetadata()
md.start_of_burst = True
md.end_of_burst = False
sent, chunk = 0, 1000
while sent < n:
    m = min(chunk, n - sent)
    md.end_of_burst = (sent + m == n)
    got = tx.send(buf[:, sent:sent + m], md)
    md.start_of_burst = False
    sent += got
print(json.dumps({"sent": sent}))
