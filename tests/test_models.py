"""Register-level tests of the X300 ZPU model, I2C/SPI cores and chip models (no network)."""
import struct
import time

import pytest

from usrp_sim import config as cm
from usrp_sim.devices import dboards as dbm
from usrp_sim.devices import x300 as x3
from usrp_sim.devices.x300 import X300Device


def make_dev(tmp_path, extra="", **delays):
    d = {"time_scale": 1, "model": "fixed", "fixed_ms": delays}
    txt = cm.EXAMPLE_CONFIG
    cfg = cm.parse(cm.tomllib.loads(txt))
    cfg.state_dir = str(tmp_path)
    cfg.delays = d
    cfg.claim_timeout_s = 0.3
    return X300Device(cfg)


@pytest.fixture
def dev(tmp_path):
    d = make_dev(tmp_path, lmk_reprogram=100, dac_init=50, dac_sync=40)
    yield d
    d.close()


def req(dev, flags, addr=0, data=0, seq=0x42):
    out, _ = dev.fw_request(struct.pack(">IIII", flags, seq, addr, data))
    return struct.unpack(">IIII", out)


def test_discovery_ping_echoes_flags_and_sequence(dev):
    assert req(dev, 1, seq=0xABCD1234) == (1, 0xABCD1234, 0, 0)


def test_peek_poke_protocol(dev):
    f, seq, addr, data = req(dev, x3.FLAG_ACK | x3.FLAG_PEEK, x3.SET0_BASE + 4 * x3.RB_COMPAT)
    assert f == (x3.FLAG_ACK | x3.FLAG_PEEK) and data == (0x27 << 16)
    f, seq, addr, data = req(dev, x3.FLAG_ACK | x3.FLAG_POKE, x3.SET0_BASE + 4 * x3.SR_DEVICE_ID, 7)
    assert (f, addr, data) == (x3.FLAG_ACK | x3.FLAG_POKE, x3.SET0_BASE + 4 * x3.SR_DEVICE_ID, 7)
    assert req(dev, 9, x3.SET0_BASE + 4 * x3.RB_DEVICE_ID)[3] == 7
    assert dev.fw_request(b"short")[0] is None


def test_compat_numbers_and_rfnoc_info(dev):
    peek = lambda r: req(dev, 9, x3.SET0_BASE + 4 * r)[3]
    assert peek(x3.RB_RFNOC_INFO) == (64 << 16) | 0x0100     # CHDR width 64, protover
    assert req(dev, 9, x3.SHMEM_BASE)[3] == (6 << 16) | 1    # fw compat > 0x50001: eeprom via shmem
    assert peek(x3.RB_SFP0_TYPE) == 1 and peek(x3.RB_SFP1_TYPE) == 1   # XG


def test_mb_eeprom_in_shmem_matches_x300_eeprom_map(dev):
    words = [req(dev, 9, x3.SHMEM_BASE + 4 * (x3.SHMEM_IDENT + i))[3] for i in range(27)]
    raw = b"".join(struct.pack(">I", w) for w in words)
    rev, prod, compat = struct.unpack("<HHH", raw[:6])
    assert (rev, prod, compat) == (7, 0x76CA, 7)
    assert raw[76:84] == b"sim-x310" and raw[99:106] == b"SIM0001"


def test_claim_protocol_and_expiry(dev):
    status = lambda: req(dev, 9, x3.SHMEM_BASE + 4 * x3.SHMEM_CLAIM_STATUS)[3]
    src = lambda: req(dev, 9, x3.SHMEM_BASE + 4 * x3.SHMEM_CLAIM_SRC)[3]
    assert status() == 0
    req(dev, 5, x3.SHMEM_BASE + 4 * x3.SHMEM_CLAIM_TIME, int(time.time()))
    req(dev, 5, x3.SHMEM_BASE + 4 * x3.SHMEM_CLAIM_SRC, 0xCAFE)
    assert status() == 1 and src() == 0xCAFE
    time.sleep(0.2)
    req(dev, 5, x3.SHMEM_BASE + 4 * x3.SHMEM_CLAIM_TIME, 0)   # refresh keeps it
    assert status() == 1
    time.sleep(0.45)                                           # not refreshed -> expires
    assert status() == 0 and src() == 0
    req(dev, 5, x3.SHMEM_BASE + 4 * x3.SHMEM_CLAIM_SRC, 0xCAFE)
    req(dev, 5, x3.SHMEM_BASE + 4 * x3.SHMEM_CLAIM_SRC, 0)     # release
    assert status() == 0


def _i2c_write(dev, addr, data):
    base = x3.I2C1_BASE
    dev.zpu.poke(base + 12, (addr << 1))
    dev.zpu.poke(base + 16, 0x90 | (0x40 if not data else 0))
    for i, b in enumerate(data):
        dev.zpu.poke(base + 12, b)
        dev.zpu.poke(base + 16, 0x10 | (0x40 if i == len(data) - 1 else 0))
    return not (dev.zpu.peek(base + 16) & 0x80)


def _i2c_read(dev, addr, n):
    base = x3.I2C1_BASE
    dev.zpu.poke(base + 12, (addr << 1) | 1)
    dev.zpu.poke(base + 16, 0x90)
    out = []
    for i in range(n):
        dev.zpu.poke(base + 16, 0x20 | (0x48 if i == n - 1 else 0))
        out.append(dev.zpu.peek(base + 12))
    return bytes(out)


def test_i2c_dboard_eeprom_read_and_checksum(dev):
    assert _i2c_write(dev, 0x55, [0])
    e = _i2c_read(dev, 0x55, 32)                   # slot A RX eeprom (0x50|0x5)
    assert e[0] == 0xDB and e[1] | e[2] << 8 == 0x7E and e[9:15] == b"DBA001"
    assert (-sum(e[:31])) & 0xFF == e[31]          # dboard_eeprom.cpp checksum
    _i2c_write(dev, 0x54, [0])
    assert _i2c_read(dev, 0x54, 3)[1] == 0x7D      # TX id
    _i2c_write(dev, 0x57, [0])
    assert _i2c_read(dev, 0x57, 15)[9:15] == b"DBB001"   # slot B


def test_i2c_missing_slave_nacks(dev):
    assert not _i2c_write(dev, 0x33, [0])
    assert _i2c_write(dev, 0x50, [0, 0])           # MB eeprom (16 bit offset) acks


def _spi_zpu(dev, slave, word, nbits):
    b = x3.SET0_BASE
    dev.zpu.poke(b + 4 * (x3.SR_SPI + 1), (nbits << 24) | slave)
    dev.zpu.poke(b + 4 * (x3.SR_SPI + 2), (word << (32 - nbits)) & 0xFFFFFFFF)


def test_lmk_lock_deasserts_then_asserts_after_delay(dev):
    clk = lambda: req(dev, 9, x3.SET0_BASE + 4 * x3.RB_CLK_STATUS)[3]
    assert clk() & (1 << 2)                                     # locked at power-up
    _spi_zpu(dev, 1, (1 << 17) | 0, 32)                         # LMK reg0 with RESET -> reprogram
    t0 = time.perf_counter()
    assert not clk() & (1 << 2)                                 # unlocked immediately
    _spi_zpu(dev, 1, 11 | (5 << 5), 32)                         # more writes in the burst
    while not clk() & (1 << 2):
        time.sleep(0.005)
        assert time.perf_counter() - t0 < 1
    assert 0.09 < time.perf_counter() - t0 < 0.25               # ~lmk_reprogram = 100 ms


def test_lmk_decodes_master_clock(dev):
    def r(a, **f):
        return a  # noqa
    # 184.32 MHz: VCO 2211.84 MHz = 96e6*2*N*P/R with N=48,P=2,R=?  pick N=48 P=2 R=8 -> 2304? use R=8.333; use the 200 MHz recipe instead
    seen = []
    dev.zpu.lmk.on_mcr_change = lambda o, n: seen.append((o, n))
    # 200 MHz recipe from x300_clock_ctrl.cpp: PLL2 N=5, P=div_5, R=2 -> 96e6*2*5*5/2 = 2400 MHz, CLKout0_1_DIV=12
    # 120 MHz: same VCO, DIV=20
    for div, want in ((12, 200e6), (20, 120e6)):
        _spi_zpu(dev, 1, (1 << 17) | (div << 5) | 0, 32)
        _spi_zpu(dev, 1, (2 << 20) | (5 << 6) | 28, 32)
        _spi_zpu(dev, 1, (5 << 5) | (5 << 24) | 30, 32)
        _spi_zpu(dev, 1, (div << 5) | 0, 32)
        assert dev.zpu.lmk.decode_mcr() == pytest.approx(want, rel=1e-9)


def _radio_spi(radio, slave, word, nbits):
    radio.write(radio.SR_SPI + 8, (nbits << 24) | slave)
    radio.write(radio.SR_SPI + 16, (word << (32 - nbits)) & 0xFFFFFFFF)
    return radio.read(radio.RB_SPI)


def test_adc_test_pattern_readback_matches_x300_self_test(dev):
    r = dev.radios[0]
    adc = lambda a, d: _radio_spi(r, r.DB_ADC_SEN, (a << 8) | d, 16)
    # test_patterns_chA = ones (2) @0x62, chB = zeros (1) @0x75  -> expected 0xfffc0000 after ^0xfffc0000
    adc(0x62, 2)
    adc(0x75, 1)
    assert r.read(r.BASE + 0x3C) ^ 0xFFFC0000 == 0xFFFC0000
    adc(0x75, 5)
    adc(0x51, 0x04)
    adc(0x52, 0x00)                                            # custom pattern 4 on chB
    assert r.read(r.BASE + 0x3C) ^ 0xFFFC0000 == (0x3FFF << 18) | (4 << 2)


def test_dac_pll_and_sync_bits_follow_delays(dev):
    r = dev.radios[0]
    dac = lambda a, d: _radio_spi(r, r.DB_DAC_SEN, (a << 8) | d, 16)
    rd = lambda a: _radio_spi(r, r.DB_DAC_SEN, (1 << 15) | (a << 8), 16) & 0xFF
    dac(0x00, 0x20)                                            # reset
    dac(0x0A, 0xA0)                                            # PLL config
    t0 = time.perf_counter()
    assert rd(0x0E) & 0x80 == 0
    while rd(0x0E) & 0x80 == 0:
        time.sleep(0.005)
        assert time.perf_counter() - t0 < 1
    assert 0.04 < time.perf_counter() - t0 < 0.2               # dac_init = 50 ms
    assert (rd(0x06) >> 6) & 3 == 1                            # lock flag set, lost flag clear
    dac(0x10, 0xC7)
    assert rd(0x12) & 0x40 == 0 and rd(0x19) != 0x0F
    time.sleep(0.06)
    assert rd(0x12) & 0x40 and rd(0x19) == 0x0F and (rd(0x06) >> 4) & 3 == 1


def test_timekeeper_registers(dev):
    base = x3.SET0_BASE + 4 * (x3.TK_BASE)
    dev.zpu.poke(base + 4 * 0x08, 1000)
    dev.zpu.poke(base + 4 * 0x0C, 0)
    dev.zpu.poke(base + 4 * 0x10, 1)                           # set ticks now
    lo = dev.zpu.peek(base + 4 * 0x00)
    assert 1000 <= lo < 1000 + 200e6 * 0.05
    # period register is Q32 ns: 1e9/184.32e6 * 2**32
    per = int(1e9 / 184.32e6 * (1 << 32))
    dev.zpu.poke(base + 4 * 0x1C, per & 0xFFFFFFFF)
    dev.zpu.poke(base + 4 * 0x20, per >> 32)
    assert dev.engine.clock.tick_rate == pytest.approx(184.32e6, rel=1e-6)


def test_gpsdo_reported_absent(dev):
    assert req(dev, 9, x3.SHMEM_BASE + 4 * x3.SHMEM_GPSDO)[3] == 0x1234ABCD


def test_unknown_registers_are_benign(dev):
    assert req(dev, 9, 0x3FFC)[3] == 0
    assert req(dev, 5, x3.BOOT_LDR_BASE, 0xFFFF)[0] & x3.FLAG_ACK
