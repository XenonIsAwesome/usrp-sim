# usrp-sim: an offline USRP simulator that stock UHD can talk to

`usrp-sim` pretends to be a USRP on the network so that unmodified UHD applications
(`uhd_usrp_probe`, `rx_samples_to_file`, `tx_samples_from_file`, `benchmark_rate`, your own C++/Python
code) run with no hardware.  It is a standalone, **pure-Python** program (stdlib + `numpy` + `msgpack`);
it does not use libuhd, `uhd.chdr`, `mprpc`, `gevent`, `pyudev` or any of the dependencies of the old
MPM-based E320 simulator.

* **Tier 1 (done): X310 / X300** over the X300 network protocol (UDP 49152/49153/49158).
* **Tier 2 (done, limited): generic "custom" device** over the MPMD protocol (UDP 49600 + msgpack-RPC TCP 49601
  + CHDR over UDP 49153) with an E3xx/AD9361-class radio, **1 or 2 channels** (see *Known gaps*).
* **Tier 3 (USB B200/B210 via libusb interposer): NOT implemented** (see *Tier 3* below).

What it simulates: device discovery, firmware/FPGA compat, EEPROMs, clocking (LMK04816 lock behaviour), ADC/DAC
chips, daughterboard LO lock detect, the RFNoC graph (crossbar, stream endpoints, client zero, DDC/DUC/radio/replay
register models), CHDR flow control, timed commands and PPS.  What it does **not** do: RF physics.  RX data comes from
files/sockets/generators, TX data goes to files/sockets.  Delays are faked through a pluggable model, and can be
replayed from timings measured on a real device (`tools/measure_delays.py`).

## Quick start

```bash
cd tools/usrp_sim
./setup.sh                      # creates ./.venv (numpy, pytest, msgpack); nothing is installed system-wide
./tools/build_uhd.sh            # optional: build a stock UHD client (Python API, E320 on, no ENABLE_SIM) in ./build/uhd; same as ./setup.sh --build-uhd
source ./uhd-env.sh             # use that local build:  PATH / LD_LIBRARY_PATH / PYTHONPATH / UHD_PKG_PATH

./bin/usrp-sim --print-example-config > sim.toml
./bin/usrp-sim -c sim.toml &    # logs to stderr; Ctrl-C / SIGTERM shuts down cleanly, SIGHUP reloads [delays]

uhd_find_devices --args addr=127.0.0.1
uhd_usrp_probe   --args type=x300,addr=127.0.0.1
rx_samples_to_file --args type=x300,addr=127.0.0.1 --rate 1e6 --freq 1e9 --nsamps 1000000 --file out.sc16 --type short
```

* On loopback the client's broadcast discovery skips `127.0.0.1`, so always pass `addr=127.0.0.1`
  (`mgmt_addr=127.0.0.1` for the MPMD profile).
* Several simulated devices: give each its own `bind = "127.0.0.N"` (ports are hard-coded in UHD, one device per IP;
  every `127.x.y.z` address is loopback on Linux).  Use them together with `addr0=127.0.0.1,addr1=127.0.0.2`.
* `[device] fw_compat/fpga_compat` must match the client.  Defaults are this checkout's (fw 6.1 / FPGA 39.0).
  `usrp-sim --probe-compat [--uhd-bin DIR]` asks the installed client what it expects (it runs a scratch sim with
  deliberately wrong numbers and parses the client's error); `--uhd-version 4.6.0` uses the verified table.
* The host prints "recv buffer could not be resized" warnings on loopback when `net.core.rmem_max` is small; harmless.

## Configuration (TOML)

`usrp-sim --print-example-config` prints a complete annotated file; `usrp-sim -c file --check` validates it
(unknown keys / bad values are errors with the offending section named).  Examples live in `examples/`.

| section | keys |
|---|---|
| `[device]` | `profile` (`x310`/`x300`/`custom`), `serial` (<=8 chars), `name`, `fw_compat`, `fpga_compat`, `bind` (IPv4), `fpga` (`HG`/`XG`/`1G`), `master_clock_rate` (200e6, 184.32e6, 120e6; custom: 1e6..500e6), `realtime`, `state_dir`, `rx_overrun_on_underrun`, `claim_timeout_s` (unrefreshed claim expires), `tx_buffer_bytes` (TX ingress buffer advertised to the host) |
| `[dboards]` | `A`/`B = { id, serial, rev }`; ids: `UBX-160`, `UBX-160-v1`, `UBX-40`, `UBX-40-v1`, `UBX-160-LP`, `UBX-TDD`, `SBX`, `SBX-v4`, `SBX-v5`, `CBX`, `SBX-120`, `CBX-120`, `WBX`, `WBX-v3`, `WBX-v4`, `WBX-120`, `BASIC`, `LF`, `EMPTY` |
| `[time]` | `source = "system"`, `pps = "system-second"`/`"none"`, `initial_offset_s` |
| `[delays]` | see below |
| `[rx.N]` | channel index N (0=`A:0`, 1=`B:0`, 2=`A:1`, 3=`B:1`; or a quoted name like `[rx."A:1"]`; custom profile: plain index). `source` = `file` `udp` `tcp` `unix` `zeros` `tone` `noise` `ramp`; `path`, `format` (`sc16`/`fc32`), `repeat`, `listen`, `connect`, `freq_hz`/`amplitude` (tone, noise), `seed` |
| `[tx.N]` | `sink` = `file` `udp` `tcp` `unix` `null`; `path`, `format`, `connect`/`listen` |
| `[custom]` | `channels` or `rx_channels`/`tx_channels` (1..2), `product` |

**Data formats.** Internally interleaved little-endian int16 I/Q (`sc16`, 4 bytes/sample).  `format = "fc32"`
(interleaved float32, +-1.0 full scale) is converted on the fly.  `ramp` is a counter pattern
(`I = n mod 2^16`, `Q = ~n mod 2^16`) which makes bit-exact checks trivial.

**Source/sink behaviour.** Sources are opened at start-up (sockets listen immediately, so a sender can start before
the stream does).  Socket and file reads are paced by the simulated sample clock when `realtime = true`; with
`realtime = false` streams free-run as fast as flow control allows.  If a UDP/TCP source starves, the missing samples are
zero-filled, a warning is logged, and (if `rx_overrun_on_underrun`) an overrun is signalled to the host; a TCP/unix sender
that hangs up, or a non-repeating file that ends, ends the stream cleanly with EOB.  UDP has no EOF, so feed a UDP source
a little more than the application takes.  TX sinks never block the stream: with no TCP/unix peer the data is dropped and
counted.  Per-channel counters (samples, packets, underruns, overruns, late, zero-filled) are logged every
`--stats-interval` seconds and printed at exit.

## Delays

Every delay comes from `[delays]`; none is hard-coded.  They are visible *the way hardware shows them*, not only as
sleeps: lock/ready bits (LMK PLL lock, DAC PLL/sync flags, LO lock detect GPIO, `lo_locked` sensor) read "unlocked"
until the delay since the last relevant write has elapsed, and register/ctrl replies are delayed without blocking
unrelated traffic.  CHDR control acknowledgements must stay in order (UHD treats a skipped sequence number as a dropped
response), so a delayed ack also holds back the acks behind it: head-of-line blocking, like the hardware.

```toml
[delays]
time_scale = 1.0      # global multiplier; 0 = no delays (fast tests)
model      = "fixed"  # fixed | uniform | normal | table | hook
default_ms = 0        # any event that is not listed
table      = "measured.csv"    # model = "table"
hook       = "mod:func"        # model = "hook": func(event, old_state, new_state) -> seconds
jitter = 0.1                   # uniform: +-fraction of the per-event value
std_fraction = 0.1             # normal: std = fraction * per-event value
[delays.fixed_ms]              # number, or { min, max } (uniform), or { mean, std } (normal): wins over the model
lo_retune = 20
```

Models: `fixed`; `uniform` (value +- `jitter`); `normal` (mean = value, std = `std_fraction`*value); `table`
(look up the A->B transition in the CSV; fallback: same event+key with the numerically nearest from/to, then any key,
then the event's median, then `default_ms`); `hook` (your function; exceptions fall back to `default_ms`).
`SIGHUP` reloads `[delays]` from the config file without restarting; a broken new section keeps the old one.
Each applied delay is logged at DEBUG (`-v`) with event, key, from/to and value.

| event | applied when (simulator) |
|---|---|
| `lmk_reprogram` | LMK04816 reset/reprogram burst (device init, master clock change): clock-status lock bit; custom profile: `set_catalina_clock_rate` |
| `clock_source` | clock-source switch (replaces `lmk_reprogram` for that burst) |
| `time_source` | latency of the PPS-select register write / `set_time_source` RPC |
| `adc_init` / `dac_init` / `dac_sync` | ADC reset; AD9146 PLL (re)configured -> PLL lock flags; AD9146 sync/FIFO align -> sync flags |
| `lo_retune` (+ `lo_retune_small`/`_large`/`_band`) | UBX MAX2871 R0 write -> LO lock-detect pin (frequency decoded from the synthesiser registers; classes are used by non-table models and fall back to `lo_retune`) |
| `gain_change`, `antenna` | UBX gain field / RX_ANT bit change (latency of that write); custom: `set_gain` RPC |
| `bandwidth` | custom profile `set_bw_filter` RPC (UBX bandwidth is fixed: not applied) |
| `dsp_rate` | DDC decimation / DUC interpolation register write |
| `stream_start` | immediate RX stream command -> first sample (timed commands start exactly at their time) |
| `reset` | ZPU software-reset register |
| `reg_access` / `ctrl_access` | extra latency of every ZPU peek/poke / every CHDR control transaction |
| `device_init` | *measure-only* (total device creation); not applied by the simulator |

`key` identifies the target (`rx:0`, `tx:1`, `mboard:0`); `from`/`to` are plain-unit parameter values.
The schema (events, key naming, CSV columns) is defined once in `usrp_sim/delay_events.py` and imported by both the
simulator and the measurement script.

### Measuring a real device: `tools/measure_delays.py`

```bash
source tools/usrp_sim/uhd-env.sh        # or any environment where `import uhd` works (same version as libuhd)
python tools/usrp_sim/tools/measure_delays.py --args "type=x300,addr=192.168.10.2" --repeat 10 --output measured.csv
```

Measures wall-clock time (`perf_counter_ns`) through `uhd.usrp.MultiUSRP` only (no register access, nothing
non-volatile): LO retune (including waiting for `lo_locked`), gain, antenna, DDC rate, stream start (command -> first
packet), time source, and device re-creation with different `master_clock_rate` (device_init, lmk_reprogram).  Options:
`--channels`, `--events`, `--spec` (sweep values, see `examples/measure_spec.toml`), `--repeat`, `--warmup`,
`--max-pairs`, `--output`, `--resume`, `--seed`, `--dry-run`, `--allow-tx`, `--lmk-baseline-ms`.  Output is
`event,key,from,to,ms,n,min_ms,max_ms,mean_ms,std_ms,p95_ms` (`ms` = median), written and fsynced after every
transition, so Ctrl-C loses nothing.  Safety: receive-only unless `--allow-tx`; even then only TX LO/gain/rate/antenna
settings are exercised, TX gain is forced to 0 first, and no TX stream is ever started.  `clock_source` is opt-in
(`--events clock_source`, needs a reference signal on real hardware).

`lmk_reprogram` is derived: `device_init - --lmk-baseline-ms`, where the baseline is the client-side init time without
any LMK wait (measure it by running the script once against the simulator with `time_scale = 0`).  Not measurable
through MultiUSRP, set by hand: `adc_init`, `dac_init`, `dac_sync`, `reset`, `reg_access`, `ctrl_access`.

Replay: `[delays] model = "table"`, `table = "measured.csv"`.  The real-hardware run is untested unless you run it;
the self-test (`tests/test_measure.py`) runs the script against the simulator and replays its CSV.

## Architecture

```
core engine   usrp_sim/core/   engine.py (clock, scheduler, delays, stats, routes), rfnoc.py (CHDR mgmt/ctrl
              routing, client zero, stream endpoints, flow-controlled RX/TX streaming), blocks.py (NoC block
              register models + noc_id registry), sampleio.py (sources/sinks)
protocol      usrp_sim/frontends/   x300_udp.py (UDP 49152 fw peek/poke + discovery, 49153 CHDR, 49158 MTU),
front-ends                          mpmd.py (MPM discovery, msgpack-RPC server, claim logic)
device        usrp_sim/devices/     x300.py (X310/X300 ZPU register map, topology, claim, EEPROMs),
profiles                            custom.py (MPMD device), chips.py (I2C core + EEPROM, LMK04816, ADS62P48,
                                    AD9146), ubx.py (LO lock / gain / antenna), dboards.py (catalogue + EEPROM images)
shared        usrp_sim/chdr.py (pure-struct CHDR codec), delays.py, delay_events.py, scheduler.py, clock.py, config.py
```

Device time is the system clock (`ticks = base + (wall - base_wall) * tick_rate`); PPS edges fall on system-second
boundaries; timed CTRL packets and RX stream commands are executed by the single scheduler when the clock-derived
device time arrives; late ones return a timestamp error / the async `late command` message UHD expects.  Streaming
threads pace themselves through `Scheduler.wait_until` (coarse sleep + short spin).

**Add a NoC block:** subclass `NocBlock` in `core/blocks.py` (set `NOC_ID`, `NUM_IN/OUT`, implement `read`/`write`,
return extra latency from `write` if a delay applies) and decorate it with `@register_block`; reference it from a
device profile's `Topology(seps, blocks, edges)`.  **Add a device profile:** write `devices/<name>.py` that builds a
`Topology` + `RfnocGraph` and a protocol front-end (copy `x300.py` + `frontends/x300_udp.py`, or `custom.py` +
`frontends/mpmd.py`), and select it in `cli.py` by `[device] profile`.

## Supported / not supported

Supported: discovery, MTU detection, claim (with timeout), EEPROMs, clocking and lock bits for 200/184.32/120 MHz,
ADC test patterns + capture-delay self-calibration, DAC PLL/sync, UBX/SBX/WBX/CBX/Basic/LF daughterboard probing,
UBX LO lock/gain/antenna timing, DDC/DUC rate registers, RX/TX streaming with flow control, timed commands, PPS,
multiple simulated devices, SIGHUP/SIGINT.  GPSDO is reported absent.  Not supported: PCIe/NI-RIO, Aurora, FPGA/fw
loading (ports 49157/49159 refuse), calibration, thermal, a second Ethernet link (`second_addr`), SBX/WBX LO lock timing
(their lock pins are asserted, tuning works but is not delay-accurate), the replay block's behaviour (register stub),
replacing a TX/RX data path by DDC/DUC *processing* (rates are honoured for pacing/timestamps; samples pass through
unchanged), RX packets are `spp` samples after the DDC (the real DDC shortens packets by the decimation).

## Performance (loopback, this development machine, one Python process)

Free-run (`realtime = false`), `benchmark_rate --rx_rate 10e6 --tx_rate 10e6 --duration 8`: RX 10-13 Msps and TX
>=30 Msps simultaneously, no drops/overruns/underruns/sequence errors.  Realtime (`realtime = true`): 10 Msps duplex
per channel runs clean (RX exactly 10 Msps; TX paced at 10 Msps, the host side reports ~9 Msps over the measurement
window); 20-25 Msps drops packets (single-process Python limit).

## Tests

`tools/run_tests.sh [--unit|--e2e|--tier2|--measure|--all]` (or `.venv/bin/python -m pytest tests -q`) -- unit tests (CHDR codec incl. byte-exact round trips of UHD's real packet
captures, register/I2C/SPI/chip models, delay models, scheduler/clock, config validation, MPMD RPC) and end-to-end tests
against a stock UHD client (skipped with a clear message if none is found: installed UHD, or the in-tree build under
`build/uhd`).  The end-to-end suite takes ~3-4 minutes.

## Tier 3 (USB B200/B210): not implemented

The only acceptable route (no kernel modules, no root) is a userspace `LD_PRELOAD` libusb-1.0 interposer forwarding
to the simulator over a unix socket.  UHD talks to a B2xx through FX3 vendor control requests, a legacy ctrl/data bulk
protocol, `b200_iface` FPGA registers and the full AD9361 driver (calibrations and lock-bit polling), none of which has a
model here; this is a large project of its own and was not started rather than faked.  Constraints a future
implementation would have: only dynamically linked libusb, only processes started through a wrapper, device presented
as already firmware- and FPGA-loaded.

## Known gaps

* Real-hardware run of `measure_delays.py` is untested (no device available); only the simulator self-test ran.
* Tier 2 supports 1-2 channels: UHD's E3xx client radio (`e3xx_radio_control_impl`) asserts a single radio block.
  More channels need an N3xx/X4xx-class radio emulation (magnesium/ZBX RPC + CPLD models), not implemented.
* Python limits realtime throughput (see Performance).  No `second_addr` dual-link.  SBX/WBX are probe-level only.
