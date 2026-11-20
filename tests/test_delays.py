import csv

import pytest

from usrp_sim import delay_events as de
from usrp_sim import delays as dl


def model(**kw):
    return dl.DelayModel(kw)


def test_fixed_values_and_default():
    m = model(model="fixed", default_ms=3, fixed_ms={"lo_retune": 100, "lmk_reprogram": 500})
    assert m.delay("lo_retune") == pytest.approx(0.100)
    assert m.delay("lmk_reprogram") == pytest.approx(0.500)
    assert m.delay("gain_change") == pytest.approx(0.003)      # falls back to default_ms


def test_time_scale_and_zero():
    cfg = dict(model="fixed", time_scale=2.0, fixed_ms={"lo_retune": 10})
    assert model(**cfg).delay("lo_retune") == pytest.approx(0.020)
    cfg["time_scale"] = 0
    assert model(**cfg).delay("lo_retune") == 0.0


def test_uniform_and_normal_statistics():
    u = model(model="uniform", jitter=0.5, seed=1, fixed_ms={"lo_retune": 100})
    xs = [u.delay("lo_retune") * 1e3 for _ in range(2000)]
    assert min(xs) >= 50 and max(xs) <= 150 and 95 < sum(xs) / len(xs) < 105
    n = model(model="normal", std_fraction=0.1, seed=2, fixed_ms={"lo_retune": 100})
    ys = [n.delay("lo_retune") * 1e3 for _ in range(4000)]
    mean = sum(ys) / len(ys)
    sd = (sum((y - mean) ** 2 for y in ys) / len(ys)) ** 0.5
    assert 98 < mean < 102 and 8 < sd < 12


def test_per_event_table_overrides_model():
    m = model(model="fixed", seed=3, default_ms=1,
              fixed_ms={"lo_retune": {"min": 10, "max": 30}, "gain_change": {"mean": 50, "std": 0}})
    xs = [m.delay("lo_retune") * 1e3 for _ in range(200)]
    assert 10 <= min(xs) and max(xs) <= 30 and max(xs) - min(xs) > 5
    assert m.delay("gain_change") == pytest.approx(0.050)


def test_lo_retune_class_entries_fall_back():
    m = model(model="fixed", fixed_ms={"lo_retune": 20, "lo_retune_small": 2})
    assert m.lo_retune("rx:0", 1e9, 1e9 + 1e6) == pytest.approx(0.002)     # small step
    assert m.lo_retune("rx:0", 1e9, 2e9) == pytest.approx(0.020)           # large -> lo_retune
    assert de.classify_lo_step(1e9, 1.2e9) == "large"
    assert de.classify_lo_step(900e6, 1.1e9) == "band"
    assert de.classify_lo_step(1e9, 1e9 + 5e6) == "small"


def test_hook_model(tmp_path, monkeypatch):
    (tmp_path / "myhook.py").write_text("def f(event, old, new):\n    return 0.007 if event == 'antenna' else 0.001\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    m = model(model="hook", hook="myhook:f")
    assert m.delay("antenna", "rx:0", "A", "B") == pytest.approx(0.007)
    assert m.delay("gain_change") == pytest.approx(0.001)


def test_hook_errors_do_not_kill_the_sim(tmp_path, monkeypatch):
    (tmp_path / "badhook.py").write_text("def f(e, o, n):\n    raise RuntimeError('boom')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    m = model(model="hook", hook="badhook:f", default_ms=4)
    assert m.delay("antenna") == pytest.approx(0.004)


def write_csv(path, rows):
    de.append_rows(str(path), [de.Row(*r) for r in rows])


def test_table_exact_nearest_median_default(tmp_path):
    p = tmp_path / "m.csv"
    write_csv(p, [("lo_retune", "rx:0", "1000000000", "2000000000", 30.0),
                  ("lo_retune", "rx:0", "1000000000", "1010000000", 5.0),
                  ("lo_retune", "rx:1", "1000000000", "2000000000", 50.0),
                  ("antenna", "rx:0", "TX/RX", "RX2", 2.0)])
    m = model(model="table", table=str(p), default_ms=9)
    assert m.delay("lo_retune", "rx:0", 1e9, 2e9) == pytest.approx(0.030)           # exact
    assert m.delay("lo_retune", "rx:0", 1e9, 1.011e9) == pytest.approx(0.005)       # nearest same key
    assert m.delay("lo_retune", "rx:1", 1e9, 1.9e9) == pytest.approx(0.050)         # nearest, key rx:1
    assert m.delay("lo_retune", "tx:0", 1e9, 2e9) == pytest.approx(0.030)           # other key: nearest overall (exact)
    assert m.delay("antenna", "rx:0", "RX2", "TX/RX") == pytest.approx(0.009 * 0 + 0.002)  # non-numeric -> event median
    assert m.delay("gain_change", "rx:0", 0, 10) == pytest.approx(0.009)            # no rows -> default_ms


def test_table_median_fallback_values(tmp_path):
    p = tmp_path / "m.csv"
    write_csv(p, [("dsp_rate", "rx:0", "a", "b", 10.0), ("dsp_rate", "rx:0", "c", "d", 20.0),
                  ("dsp_rate", "rx:0", "e", "f", 40.0)])
    m = model(model="table", table=str(p))
    assert m.delay("dsp_rate", "rx:0", "x", "y") == pytest.approx(0.020)


def test_csv_schema_roundtrip_with_statistics(tmp_path):
    p = tmp_path / "m.csv"
    de.append_rows(str(p), [de.row_from_samples("lo_retune", "rx:0", 1e9, 2.4e9, [10, 12, 11, 30, 9])])
    hdr = next(csv.reader(open(p)))
    assert hdr == de.CSV_FIELDS
    (r,) = de.read_csv(str(p))
    assert r.ms == pytest.approx(11.0) and r.n == 5 and r.min_ms == 9 and r.max_ms == 30
    assert (r.frm, r.to) == ("1000000000", "2400000000")


def test_csv_unknown_event_rejected(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("event,key,from,to,ms\nfoo,rx:0,,,1\n")
    with pytest.raises(ValueError, match="unknown event"):
        de.read_csv(str(p))


@pytest.mark.parametrize("cfg,msg", [
    ({"model": "bogus"}, "model must be"),
    ({"fixed_ms": {"nonsense": 1}}, "unknown event"),
    ({"fixed_ms": {"lo_retune": -1}}, "non-negative"),
    ({"fixed_ms": {"lo_retune": {"min": 1}}}, "must be"),
    ({"time_scale": "fast"}, "time_scale"),
    ({"model": "table"}, "requires table"),
    ({"model": "hook"}, "requires hook"),
    ({"nope": 1}, "unknown key"),
])
def test_validation_errors(cfg, msg):
    with pytest.raises(dl.DelayConfigError, match=msg):
        dl.DelayModel(cfg)


def test_reload_swaps_configuration():
    m = model(model="fixed", fixed_ms={"lo_retune": 100})
    assert m.delay("lo_retune") == pytest.approx(0.1)
    m.reload({"model": "fixed", "fixed_ms": {"lo_retune": 5}, "time_scale": 1})
    assert m.delay("lo_retune") == pytest.approx(0.005)
    with pytest.raises(dl.DelayConfigError):
        m.reload({"model": "nope"})
    assert m.delay("lo_retune") == pytest.approx(0.005)      # failed reload keeps the old config


def test_unknown_event_name_at_runtime():
    with pytest.raises(KeyError):
        model().delay("does_not_exist")
