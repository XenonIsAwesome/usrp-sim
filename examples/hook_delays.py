"""Example delay hook:  [delays] model = "hook" / hook = "hook_delays:delay"  (module must be importable,
e.g. PYTHONPATH=examples).  Return seconds; the sim multiplies by time_scale."""


def delay(event, old_state, new_state):
    if event == "lo_retune":
        try:
            return 0.005 + abs(float(new_state) - float(old_state)) / 1e9 * 0.02   # 20 ms per GHz step
        except (TypeError, ValueError):
            return 0.01
    return {"lmk_reprogram": 0.2, "gain_change": 0.001}.get(event, 0.0)
