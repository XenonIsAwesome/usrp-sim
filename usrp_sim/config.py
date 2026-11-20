"""TOML configuration: loading, defaults and validation (clear error messages)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import tomllib  # Python >= 3.11
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from . import delays as dl

# Compat numbers of THIS checkout (x300_fw_common.h); override in [device] to match the
# installed UHD (see `usrp-sim --uhd-version` / `--probe-compat`).
DEFAULT_FW_COMPAT = "6.1"
DEFAULT_FPGA_COMPAT = "39.0"        # 0x27.0
MASTER_CLOCKS = (200e6, 184.32e6, 120e6)

SOURCES = ("file", "udp", "tcp", "unix", "zeros", "tone", "noise", "ramp")
SINKS = ("file", "udp", "tcp", "unix", "null")
FORMATS = ("sc16", "fc32")


class ConfigError(ValueError):
    pass


EXAMPLE_CONFIG = '''\
# usrp-sim example configuration (X310, two UBX-160)
[device]
profile  = "x310"            # x310 | x300 | (tier 2) custom
serial   = "SIM0001"
name     = "sim-x310"
# fw_compat   = "6.1"        # override to match the installed UHD (see --probe-compat)
# fpga_compat = "39.0"
bind     = "127.0.0.1"       # per-device IP; 127.0.0.2, .3 ... for more devices
fpga     = "XG"              # HG | XG
master_clock_rate = 200e6    # 200e6 | 184.32e6 | 120e6 (the host programs the LMK for it)
realtime = true              # false = free-run streams as fast as possible
state_dir = "./state"        # file-backed EEPROM etc.
rx_overrun_on_underrun = true  # raise the RX overrun flag when a source starves
claim_timeout_s = 1.5        # a claim not refreshed for this long expires
tx_buffer_bytes = 262144     # TX ingress buffer advertised to the host (FPGA image: 65536); the
                             # host can run this far ahead of the simulated sample clock

[dboards]
A = { id = "UBX-160", serial = "DBA001", rev = 1 }
B = { id = "UBX-160", serial = "DBB001", rev = 1 }

[time]
source = "system"            # device time = system clock
pps    = "system-second"     # PPS edges on system-second boundaries (or "none")
initial_offset_s = 0.0

[delays]
time_scale = 1.0             # global multiplier (0 = no delays)
model      = "fixed"         # fixed | uniform | normal | table | hook
default_ms = 0
# table    = "measured.csv"  # model = "table": output of tools/measure_delays.py
# hook     = "mymod:myfunc"  # model = "hook": myfunc(event, old, new) -> seconds
# jitter = 0.1               # uniform: +-fraction of the per-event value
# std_fraction = 0.1         # normal: std = fraction * per-event value
[delays.fixed_ms]            # numbers, or { min = 10, max = 30 } / { mean = 20, std = 3 }
lmk_reprogram = 150
lo_retune     = 20
gain_change   = 1
antenna       = 1
dsp_rate      = 2
stream_start  = 5
dac_sync      = 80
reg_access    = 0.0

[rx.0]                       # channel "A:0" (index 0); "B:0" = 1, "A:1" = 2, "B:1" = 3
source = "ramp"              # file | udp | tcp | unix | zeros | tone | noise | ramp
# path   = "in_ch0.sc16"     # raw interleaved int16 I/Q
format = "sc16"              # sc16 | fc32 (converted)
repeat = true
[rx.1]
source = "zeros"
# source = "udp"
# listen = "127.0.0.1:5001"  # or connect = "..."; payload = raw sc16 stream

[tx.0]
sink = "null"                # file | udp | tcp | unix | null
# path = "out_ch0.sc16"
[tx.1]
sink = "null"
# sink    = "udp"
# connect = "127.0.0.1:5002"
'''


@dataclass
class DBoard:
    id: str = "UBX-160"
    serial: str = "DB00001"
    rev: int = 1


@dataclass
class ChannelIO:
    kind: str = "zeros"              # source/sink type
    path: str = ""
    format: str = "sc16"
    repeat: bool = True
    listen: str = ""
    connect: str = ""
    freq_hz: float = 1e6             # tone
    amplitude: float = 0.5           # tone/noise (fraction of full scale)
    seed: int = 1


@dataclass
class Config:
    profile: str = "x310"
    serial: str = "SIM0001"
    name: str = "sim-x310"
    fw_compat: str = DEFAULT_FW_COMPAT
    fpga_compat: str = DEFAULT_FPGA_COMPAT
    bind: str = "127.0.0.1"
    fpga: str = "XG"
    master_clock_rate: float = 200e6
    realtime: bool = True
    state_dir: str = "./state"
    rx_overrun_on_underrun: bool = True
    claim_timeout_s: float = 1.5
    tx_buffer_bytes: int = 262144
    dboards: Dict[str, DBoard] = field(default_factory=dict)
    time_pps: str = "system-second"
    time_initial_offset_s: float = 0.0
    delays: Dict[str, Any] = field(default_factory=dict)
    rx: Dict[str, ChannelIO] = field(default_factory=dict)   # key "A:0" ...
    tx: Dict[str, ChannelIO] = field(default_factory=dict)
    base_dir: str = "."
    custom: Dict[str, Any] = field(default_factory=dict)

    def compat(self, s: str) -> tuple:
        a, b = s.split(".")
        return int(a), int(b)


CHAN_NAMES = ["A:0", "B:0", "A:1", "B:1"]


def _chan_key(k: str, where: str, custom: bool = False) -> str:
    if custom:
        if not k.isdigit():
            raise ConfigError("[%s.%s]: for profile = \"custom\" the channel must be an integer index" % (where, k))
        return "c%d" % int(k)
    if k.isdigit():
        i = int(k)
        if i >= len(CHAN_NAMES):
            raise ConfigError("[%s.%s]: channel index out of range (0..%d)" % (where, k, len(CHAN_NAMES) - 1))
        return CHAN_NAMES[i]
    if k in CHAN_NAMES:
        return k
    raise ConfigError("[%s.%s]: channel must be an index 0..3 or one of %s" % (where, k, CHAN_NAMES))


def _check_keys(d: dict, allowed: set, where: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise ConfigError("[%s]: unknown key(s): %s (allowed: %s)"
                          % (where, ", ".join(sorted(extra)), ", ".join(sorted(allowed))))


def _typed(d: dict, key: str, types, default, where: str):
    v = d.get(key, default)
    if isinstance(v, bool) and bool not in (types if isinstance(types, tuple) else (types,)):
        raise ConfigError("[%s] %s: expected %s, got boolean" % (where, key, types))
    if not isinstance(v, types):
        raise ConfigError("[%s] %s: expected %s, got %r" % (where, key, types, v))
    return v


def _hostport(s: str, where: str, key: str):
    host, _, port = s.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError("[%s] %s = %r: expected \"host:port\"" % (where, key, s))


def _parse_io(raw: dict, where: str, is_rx: bool) -> ChannelIO:
    kinds = SOURCES if is_rx else SINKS
    kkey = "source" if is_rx else "sink"
    allowed = {kkey, "path", "format", "repeat", "listen", "connect", "freq_hz", "amplitude", "seed"}
    _check_keys(raw, allowed, where)
    io = ChannelIO()
    io.kind = _typed(raw, kkey, str, "zeros" if is_rx else "null", where)
    if io.kind not in kinds:
        raise ConfigError("[%s] %s = %r: must be one of %s" % (where, kkey, io.kind, ", ".join(kinds)))
    io.path = _typed(raw, "path", str, "", where)
    io.format = _typed(raw, "format", str, "sc16", where)
    if io.format not in FORMATS:
        raise ConfigError("[%s] format = %r: must be one of %s" % (where, io.format, ", ".join(FORMATS)))
    io.repeat = _typed(raw, "repeat", bool, True, where)
    io.listen = _typed(raw, "listen", str, "", where)
    io.connect = _typed(raw, "connect", str, "", where)
    io.freq_hz = float(_typed(raw, "freq_hz", (int, float), 1e6, where))
    io.amplitude = float(_typed(raw, "amplitude", (int, float), 0.5, where))
    io.seed = int(_typed(raw, "seed", int, 1, where))
    if io.kind in ("file",) and not io.path:
        raise ConfigError("[%s] %s = \"file\" requires path" % (where, kkey))
    if io.kind == "unix" and not (io.path or io.listen or io.connect):
        raise ConfigError("[%s] %s = \"unix\" requires path (unix socket path)" % (where, kkey))
    if io.kind in ("udp", "tcp"):
        if is_rx and not (io.listen or io.connect):
            raise ConfigError("[%s] %s = %r requires listen = \"host:port\" or connect = \"host:port\""
                              % (where, kkey, io.kind))
        if not is_rx and not (io.connect or io.listen):
            raise ConfigError("[%s] %s = %r requires connect = \"host:port\"" % (where, kkey, io.kind))
        for k in ("listen", "connect"):
            if getattr(io, k):
                _hostport(getattr(io, k), where, k)
    return io


def parse(raw: dict, base_dir: str = ".") -> Config:
    _check_keys(raw, {"device", "dboards", "time", "delays", "rx", "tx", "custom"}, "root")
    c = Config(base_dir=base_dir)
    dev = raw.get("device", {})
    _check_keys(dev, {"profile", "serial", "name", "fw_compat", "fpga_compat", "bind", "fpga",
                      "master_clock_rate", "realtime", "state_dir", "rx_overrun_on_underrun",
                      "claim_timeout_s", "tx_buffer_bytes"}, "device")
    c.profile = _typed(dev, "profile", str, "x310", "device")
    if c.profile not in ("x310", "x300", "custom"):
        raise ConfigError("[device] profile = %r: must be x310, x300 or custom" % c.profile)
    c.serial = _typed(dev, "serial", str, "SIM0001", "device")
    if not 0 < len(c.serial) <= 8:
        raise ConfigError("[device] serial must be 1..8 characters (EEPROM field is 9 bytes incl. NUL)")
    c.name = _typed(dev, "name", str, "sim-" + c.profile, "device")
    if len(c.name) > 22:
        raise ConfigError("[device] name must be at most 22 characters")
    for k, attr, dflt in (("fw_compat", "fw_compat", DEFAULT_FW_COMPAT),
                          ("fpga_compat", "fpga_compat", DEFAULT_FPGA_COMPAT)):
        v = _typed(dev, k, str, dflt, "device")
        parts = v.split(".")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ConfigError("[device] %s = %r: expected \"major.minor\"" % (k, v))
        setattr(c, attr, v)
    c.bind = _typed(dev, "bind", str, "127.0.0.1", "device")
    import ipaddress
    try:
        ipaddress.IPv4Address(c.bind)
    except ValueError:
        raise ConfigError("[device] bind = %r: not an IPv4 address" % c.bind)
    c.fpga = _typed(dev, "fpga", str, "XG", "device")
    if c.fpga not in ("HG", "XG", "1G"):
        raise ConfigError("[device] fpga = %r: must be HG, XG or 1G" % c.fpga)
    c.master_clock_rate = float(_typed(dev, "master_clock_rate", (int, float), 200e6, "device"))
    if c.profile == "custom":
        if "master_clock_rate" not in dev:
            c.master_clock_rate = 61.44e6
        if not 1e6 <= c.master_clock_rate <= 500e6:
            raise ConfigError("[device] master_clock_rate = %g: custom profile supports 1e6..500e6" % c.master_clock_rate)
    elif not any(abs(c.master_clock_rate - m) < 1 for m in MASTER_CLOCKS):
        raise ConfigError("[device] master_clock_rate = %g: supported: %s"
                          % (c.master_clock_rate, ", ".join("%g" % m for m in MASTER_CLOCKS)))
    c.realtime = _typed(dev, "realtime", bool, True, "device")
    c.state_dir = _typed(dev, "state_dir", str, "./state", "device")
    c.rx_overrun_on_underrun = _typed(dev, "rx_overrun_on_underrun", bool, True, "device")
    c.claim_timeout_s = float(_typed(dev, "claim_timeout_s", (int, float), 1.5, "device"))
    c.tx_buffer_bytes = _typed(dev, "tx_buffer_bytes", int, 262144, "device")
    if not 8192 <= c.tx_buffer_bytes <= (1 << 26):
        raise ConfigError("[device] tx_buffer_bytes must be between 8192 and 67108864")

    from .devices import dboards as dbm
    for slot, spec in (raw.get("dboards") or {"A": {"id": "UBX-160"}, "B": {"id": "UBX-160"}}).items():
        if slot not in ("A", "B"):
            raise ConfigError("[dboards]: slot must be A or B, got %r" % slot)
        if not isinstance(spec, dict):
            raise ConfigError("[dboards] %s: expected a table like { id = \"UBX-160\" }" % slot)
        _check_keys(spec, {"id", "serial", "rev"}, "dboards." + slot)
        db = DBoard(id=_typed(spec, "id", str, "UBX-160", "dboards." + slot),
                    serial=_typed(spec, "serial", str, "DB%s001" % slot, "dboards." + slot),
                    rev=_typed(spec, "rev", int, 1, "dboards." + slot))
        if db.id not in dbm.DBOARDS:
            raise ConfigError("[dboards] %s: unknown daughterboard %r (known: %s)"
                              % (slot, db.id, ", ".join(sorted(dbm.DBOARDS))))
        if len(db.serial) > 9:
            raise ConfigError("[dboards] %s: serial must be at most 9 characters" % slot)
        c.dboards[slot] = db
    for slot in ("A", "B"):
        c.dboards.setdefault(slot, DBoard(id="EMPTY", serial=""))

    t = raw.get("time", {})
    _check_keys(t, {"source", "pps", "initial_offset_s"}, "time")
    if _typed(t, "source", str, "system", "time") != "system":
        raise ConfigError("[time] source: only \"system\" is supported")
    c.time_pps = _typed(t, "pps", str, "system-second", "time")
    if c.time_pps not in ("system-second", "none"):
        raise ConfigError("[time] pps = %r: must be \"system-second\" or \"none\"" % c.time_pps)
    c.time_initial_offset_s = float(_typed(t, "initial_offset_s", (int, float), 0.0, "time"))

    c.delays = dict(raw.get("delays", {}))
    try:
        dl.validate_delays(c.delays)
    except dl.DelayConfigError as e:
        raise ConfigError(str(e))

    for sect, is_rx, dest in (("rx", True, c.rx), ("tx", False, c.tx)):
        for k, v in (raw.get(sect) or {}).items():
            if not isinstance(v, dict):
                raise ConfigError("[%s.%s]: expected a table" % (sect, k))
            dest[_chan_key(k, sect, c.profile == "custom")] = _parse_io(v, "%s.%s" % (sect, k), is_rx)
    c.custom = dict(raw.get("custom", {}))
    if c.profile == "custom":
        cu = c.custom
        _check_keys(cu, {"channels", "rx_channels", "tx_channels", "product"}, "custom")
        n = _typed(cu, "channels", int, 2, "custom")
        c.custom["rx_channels"] = _typed(cu, "rx_channels", int, n, "custom")
        c.custom["tx_channels"] = _typed(cu, "tx_channels", int, n, "custom")
        for k in ("rx_channels", "tx_channels"):
            if not 1 <= c.custom[k] <= 2:
                raise ConfigError(
                    "[custom] %s = %d: the MPMD client radio that this profile presents (E3xx/AD9361 class, "
                    "e3xx_radio_control_impl asserts one radio block) supports 1 or 2 channels; more channels "
                    "need an N3xx/X4xx-class radio emulation, which is not implemented" % (k, c.custom[k]))
        c.custom["product"] = _typed(cu, "product", str, "e320", "custom")
        for sect, dest, key in (("rx", c.rx, "rx_channels"), ("tx", c.tx, "tx_channels")):
            for k in dest:
                if int(k[1:]) >= c.custom[key]:
                    raise ConfigError("[%s.%s]: channel index >= [custom] %s = %d" % (sect, k[1:], key, c.custom[key]))
    elif c.custom:
        raise ConfigError("[custom] is only valid with profile = \"custom\"")
    return c


def load(path: str) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise ConfigError("cannot read %s: %s" % (path, e))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError("%s: TOML syntax error: %s" % (path, e))
    cfg = parse(raw, os.path.dirname(os.path.abspath(path)))
    return cfg


def load_delays_section(path: str) -> Dict[str, Any]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    d = dict(raw.get("delays", {}))
    dl.validate_delays(d)
    return d
