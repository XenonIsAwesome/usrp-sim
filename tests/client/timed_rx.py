"""PPS alignment + timed RX start + late command, via the UHD Python API."""
import json
import sys
import time

import numpy as np
import uhd

u = uhd.usrp.MultiUSRP((sys.argv[1] if "=" in sys.argv[1] else "type=x300,addr=%s" % sys.argv[1]))
u.set_rx_rate(1e6)
u.set_rx_freq(1e9)
u.set_time_next_pps(uhd.types.TimeSpec(100.0))
time.sleep(1.4)
sys_frac = time.time() % 1.0
now = u.get_time_now().get_real_secs()
res = {"pps_dev_frac": now % 1.0, "pps_sys_frac": sys_frac,
       "last_pps": u.get_time_last_pps().get_real_secs(), "dev_now": now}
st = uhd.usrp.StreamArgs("sc16", "sc16")
st.channels = [0]
rx = u.get_rx_stream(st)
buf = np.zeros((1, 2000), dtype="<i4")
md = uhd.types.RXMetadata()
want = u.get_time_now().get_real_secs() + 0.6
cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
cmd.stream_now = False
cmd.time_spec = uhd.types.TimeSpec(want)
w0 = time.time()
rx.issue_stream_cmd(cmd)
rx.recv(buf, md, 3.0)
res.update(requested=want, first_ts=md.time_spec.get_real_secs(), wall_wait=time.time() - w0,
           err=str(md.error_code))
rx.issue_stream_cmd(uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont))
time.sleep(0.2)
while rx.recv(buf, md, 0.2):
    pass
cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
cmd.stream_now = False
cmd.time_spec = uhd.types.TimeSpec(u.get_time_now().get_real_secs() - 1.0)
rx.issue_stream_cmd(cmd)
rx.recv(buf, md, 1.5)
res["late_err"] = str(md.error_code)
print(json.dumps(res))
