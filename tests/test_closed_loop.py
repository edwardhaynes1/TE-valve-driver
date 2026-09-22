"""The control law in closed loop against models of the rig fitted to the
21 Sept 2026 logs (tests/plant_21sept.py).

These pin down the redesign's purpose:
  * auto-t: no overshoot after a burst (the old hold rule overshot 5-8 K,
    reaching 159.5 °C against a 160 °C trip)
  * auto-p: works for every calibrated seat screw torque, finds a valve
    that opens away from the table, and follows upstream pressure changes
Each fitted thermal model is used only within the range of its run.
"""
import math

import pytest

from driver import controller
from plant_21sept import Thermal, Valve

DT = 0.25


def run_t(fit, t0, setpoints, seconds):
    """setpoints: [(time, °C)]. Returns [(t, TC)], messages, final state."""
    th, h, now = Thermal(fit, t0=t0), controller.new_state(), 1000.0
    controller.command(h, now, mode='auto-t', setpoint_C=setpoints[0][1], armed=True)
    out, msgs, todo = [], [], list(setpoints[1:])
    for k in range(int(seconds / DT)):
        t = k * DT
        if todo and t >= todo[0][0]:
            controller.command(h, now + t, setpoint_C=todo.pop(0)[1])
        temp = th.temp()
        duty, m = controller.step(h, now + t, DT, temp, True)
        msgs += m
        th.step(duty, DT)
        out.append((t, temp))
    return out, msgs, h


def overshoot(out, t_from, setpoint):
    return max(T for t, T in out if t >= t_from) - setpoint


@pytest.mark.parametrize("fit, t0, setpoint", [
    ("150128", 25.0, 40.0),
    ("152054", 25.0, 40.0),
    ("152054", 25.0, 90.0),      # real run: 94.1 °C for 90
    ("173217", 25.0, 90.0),
    ("173217", 25.0, 145.0),     # real run: 151 °C for 145
    ("173217", 60.0, 145.0),
    ("173217", 25.0, 155.0),     # real run: 159.5 °C for 155
])
def test_a_burst_lands_without_overshoot(fit, t0, setpoint):
    out, _, h = run_t(fit, t0, [(0, setpoint)], 400)
    assert overshoot(out, 0, setpoint) < 1.0
    assert abs(out[-1][1] - setpoint) < 0.5 and h['trip_reason'] is None


def test_a_small_step_high_up_lands_without_overshoot():
    # real run 173217: 140 → 145 °C overshot to 152 °C
    out, _, _ = run_t("173217", 140.0, [(0, 140.0), (400, 145.0)], 700)
    assert overshoot(out, 400, 145.0) < 1.0


def test_tau_is_learned_so_the_next_burst_lands_better():
    out, msgs, h = run_t("173217", 25.0, [(0, 40.0), (300, 30.0), (700, 40.0)], 1000)
    first, second = overshoot(out[:1200], 0, 40.0), overshoot(out, 700, 40.0)
    assert h['t_tau'] != pytest.approx(2.5)
    assert any("tau now" in m for m in msgs)
    assert second < first / 2 and second < 2.0


# ── auto-p ───────────────────────────────────────────────────────────────────

VALVES = {   # torque: (thermal fit, true opening point, at bar, e-fold, rise at opening, baseline)
    0.25: ("150128", 40.0, 2.10, 3.5, 1.0e-7, 4.6e-7),
    0.40: ("152054", 88.0, 2.38, 7.5, 1.6e-7, 4.5e-7),
    0.45: ("173217", 150.0, 1.02, 12.0, 4.8e-6, 2.7e-7),
}


def run_p(torque, target, seconds, opens_off_table=0.0, hyst=10.0, upstream=None):
    fit, open_c, bar, efold, rise, base = VALVES[torque]
    upstream = upstream or (lambda t: bar)
    th = Thermal(fit)
    valve = Valve(open_c + opens_off_table, bar, efold, rise_open=rise, hyst=hyst, base=base)
    h, now = controller.new_state(), 1000.0
    controller.command(h, now, mode='auto-p', p_target_mbar=target, armed=True)
    out, msgs = [], []
    for k in range(int(seconds / DT)):
        t = k * DT
        up = upstream(t)
        valve.step(th.t, up, DT)
        vac = valve.vac()
        duty, m = controller.step(h, now + t, DT, th.temp(), True, vac=vac,
                                  p_up=up, p_up_t=now + t, seat_nm=torque)
        msgs += m
        th.step(duty, DT)
        out.append((t, vac))
    return out, msgs, h


def reached(out, target, tol_dec=0.08):
    return next((t for t, p in out if abs(math.log10(p / target)) < tol_dec), None)


def worst_after(out, target, t_from):
    return max(abs(math.log10(p / target)) for t, p in out if t >= t_from)


@pytest.mark.parametrize("torque, target", [(0.25, 1.2e-6), (0.40, 1.5e-6), (0.45, 8e-6)])
def test_every_calibrated_torque_reaches_its_target(torque, target):
    out, msgs, h = run_p(torque, target, 1200)
    t = reached(out, target)
    assert t is not None and t < 600
    assert worst_after(out, target, t + 120) < 0.1          # then stays within ~±25 %
    assert h['trip_reason'] is None


def test_a_valve_opening_higher_than_the_table_is_found_and_learned():
    out, msgs, h = run_p(0.40, 1.5e-6, 1500, opens_off_table=+8.0)
    assert reached(out, 1.5e-6) is not None
    assert h['p_ref_how'] == 'learned'
    assert any("this session's opening point for 0.40 N·m is now" in m for m in msgs)
    assert h['p_ref'] == pytest.approx(88.0 + 8.0, abs=3.0)


def test_a_valve_opening_lower_than_the_table_opens_early_without_a_big_overshoot():
    out, msgs, h = run_p(0.40, 1.5e-6, 1500, opens_off_table=-8.0)
    t = reached(out, 1.5e-6)
    assert t is not None and t < 400
    assert max(p for s, p in out) < 1.5e-6 * 1.3


def test_a_snap_valve_does_not_hunt():
    # 1 K hysteresis (the 16 Sept valve) at 0.45 N·m gains: at worst pinned
    # a little above target at the floor, not cycling open/shut
    out, _, _ = run_p(0.45, 4e-6, 1500, opens_off_table=-8.0, hyst=1.0)
    tail = [math.log10(p / 4e-6) for t, p in out if t > 1200]
    assert max(tail) - min(tail) < 0.1


def test_upstream_leak_and_refill_are_followed():
    # like 173217: upstream leaking ~1.5 mbar/s, then a refill back to 5.2 bar
    def upstream(t):
        return 5.2 - 0.0015 * (t if t < 900 else t - 900)
    out, msgs, h = run_p(0.45, 5e-5, 1500, upstream=upstream)
    t = reached(out, 5e-5)
    assert t is not None and t < 400
    assert max(abs(math.log10(p / 5e-5)) for s, p in out if t + 120 <= s < 900) < 0.1
    assert reached([(s, p) for s, p in out if s > 1000], 5e-5) is not None
    assert h['p_up_ff'] != 0.0 and h['trip_reason'] is None
