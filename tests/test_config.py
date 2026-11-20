import pytest

from usrp_sim import config as cm


def parse(text):
    return cm.parse(cm.tomllib.loads(text))


def test_example_config_is_valid():
    c = parse(cm.EXAMPLE_CONFIG)
    assert c.profile == "x310" and c.master_clock_rate == 200e6
    assert c.dboards["A"].id == "UBX-160" and "A:0" in c.rx and c.delays["fixed_ms"]["lo_retune"] == 20


def test_defaults_for_empty_config():
    c = parse("")
    assert c.bind == "127.0.0.1" and c.fw_compat == "6.1" and c.fpga_compat == "39.0"


@pytest.mark.parametrize("text,msg", [
    ('[device]\nprofile = "e320"', "profile"),
    ('[device]\nbind = "not-an-ip"', "bind"),
    ('[device]\nmaster_clock_rate = 123e6', "master_clock_rate"),
    ('[device]\nfpga = "ZZ"', "fpga"),
    ('[device]\nserial = "TOOLONGSERIAL"', "serial"),
    ('[device]\nfw_compat = "six"', "fw_compat"),
    ('[device]\nbogus = 1', "unknown key"),
    ('[dboards]\nC = { id = "UBX-160" }', "slot"),
    ('[dboards]\nA = { id = "NOPE" }', "unknown daughterboard"),
    ('[time]\npps = "gps"', "pps"),
    ('[delays.fixed_ms]\nlo_retunee = 1', "unknown event"),
    ('[rx.7]\nsource = "zeros"', "channel"),
    ('[rx.0]\nsource = "file"', "requires path"),
    ('[rx.0]\nsource = "udp"', "listen"),
    ('[rx.0]\nsource = "udp"\nlisten = "nohost"', "host:port"),
    ('[rx.0]\nsource = "teleport"', "must be one of"),
    ('[tx.0]\nsink = "udp"', "connect"),
    ('[tx.0]\nsink = "file"\nformat = "sc8"\npath = "x"', "format"),
    ('[nonsense]\nx = 1', "unknown key"),
])
def test_validation_errors_are_clear(text, msg):
    with pytest.raises(cm.ConfigError, match=msg):
        parse(text)


def test_channel_names_and_indices():
    c = parse('[rx.1]\nsource="zeros"\n[rx."A:1"]\nsource="zeros"\n[tx.0]\nsink="null"')
    assert set(c.rx) == {"B:0", "A:1"} and set(c.tx) == {"A:0"}


def test_toml_syntax_error_reported(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text("[device\n")
    with pytest.raises(cm.ConfigError, match="syntax"):
        cm.load(str(p))
