import threading
import time

import pytest

from usrp_sim import scheduler as sch
from usrp_sim.clock import DeviceClock


@pytest.fixture
def s():
    x = sch.Scheduler()
    yield x
    x.close()


def test_callbacks_run_in_time_order(s):
    out, ev = [], threading.Event()
    t0 = sch.now()
    s.call_at(t0 + 0.06, lambda: (out.append("c"), ev.set()))
    s.call_at(t0 + 0.02, lambda: out.append("a"))
    s.call_at(t0 + 0.04, lambda: out.append("b"))
    assert ev.wait(1.0)
    assert out == ["a", "b", "c"]


def test_same_time_callbacks_keep_fifo_order(s):
    out, ev = [], threading.Event()
    t = sch.now() + 0.03
    for i in range(20):
        s.call_at(t, lambda i=i: out.append(i))
    s.call_at(t, ev.set)
    assert ev.wait(1.0)
    assert out == list(range(20))


def test_cancel(s):
    out = []
    h = s.call_later(0.03, lambda: out.append(1))
    h.cancel()
    time.sleep(0.08)
    assert out == [] and s.pending() == 0


def test_callback_exception_does_not_kill_scheduler(s):
    ev = threading.Event()
    s.call_later(0.0, lambda: 1 / 0)
    s.call_later(0.02, ev.set)
    assert ev.wait(1.0)


def test_wait_until_accuracy_and_stop(s):
    t = sch.now() + 0.02
    assert s.wait_until(t) is True
    assert 0 <= sch.now() - t < 0.003
    stop = threading.Event()
    threading.Timer(0.02, stop.set).start()
    t0 = sch.now()
    assert s.wait_until(t0 + 2.0, stop) is False
    assert sch.now() - t0 < 0.5


def test_clock_follows_system_time_and_set_time():
    c = DeviceClock(200e6)
    assert abs(c.ticks_now() / 200e6 - time.time()) < 0.01
    c.set_ticks_now(int(5 * 200e6))
    time.sleep(0.1)
    assert 5.09 < c.ticks_now() / 200e6 < 5.25


def test_next_pps_aligns_with_system_second():
    c = DeviceClock(200e6)
    c.set_ticks_next_pps(int(100 * 200e6))
    time.sleep(1.1 - (time.time() % 1.0) + 0.05)
    now = c.ticks_now() / 200e6
    assert abs(now - (100 + time.time() % 1.0)) < 0.01
    assert abs(c.ticks_last_pps() / 200e6 - 100.0) < 1e-6


def test_tick_rate_change_keeps_time_continuous():
    c = DeviceClock(200e6)
    c.set_ticks_now(int(10 * 200e6))
    before = c.ticks_now() / 200e6
    c.set_tick_rate(184.32e6)
    assert abs(c.ticks_now() / 184.32e6 - before) < 0.01


def test_ticks_to_mono_roundtrip():
    c = DeviceClock(200e6)
    target = c.ticks_now() + int(0.25 * 200e6)
    assert 0.24 < c.ticks_to_mono(target) - sch.now() < 0.26
    assert abs(c.mono_to_ticks(c.ticks_to_mono(target)) - target) < 0.005 * 200e6
