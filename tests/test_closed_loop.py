"""The control law in closed loop against models of the rig fitted to the
21 Sept 2026 logs (tests/plant_21sept.py).

These pin down the redesign's purpose:
  * auto-t: no overshoot after a burst (the old hold rule overshot 5-8 K,
    reaching 159.5 °C against a 160 °C trip)
  * auto-p (history 50): told nothing about where the valve opens, it never
    lets P_vacuum past P_vacuum_target, settles with the valve open, and
    cuts the heater at once when a refill pushes P_vacuum up
Each fitted thermal model is used only within the range of its run.
"""

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
    # 30 Sept 2026: at most 0.5 °C past the setpoint (t-min-tune steps 1 K)
    out, _, h = run_t(fit, t0, [(0, setpoint)], 400)
    assert overshoot(out, 0, setpoint) < 0.5
    assert abs(out[-1][1] - setpoint) < 0.5 and h['trip_reason'] is None


def test_a_small_step_high_up_lands_without_overshoot():
    # real run 173217: 140 → 145 °C overshot to 152 °C
    out, _, _ = run_t("173217", 140.0, [(0, 140.0), (400, 145.0)], 700)
    assert overshoot(out, 400, 145.0) < 0.5


@pytest.mark.parametrize("fit, start", [("150128", 35.0), ("152054", 80.0), ("173217", 115.0)])
def test_1_K_steps_land_within_half_a_kelvin_and_quickly(fit, start):
    # t-min-tune's staircase: +1 K every 300 s from a settled hold
    sps = [(0, start)] + [(900 + 300 * i, start + 1 + i) for i in range(4)]
    out, _, h = run_t(fit, start, sps, 900 + 300 * 4 + 300)
    for t0, sp in sps[1:]:
        seg = [(t, T) for t, T in out if t0 <= t < t0 + 300]
        assert max(T for _, T in seg) - sp < 0.5
        assert all(abs(T - sp) < 0.4 for t, T in seg if t >= t0 + 60)   # settled within 60 s


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
TARGETS = {0.25: 1.2e-6, 0.40: 1.5e-6, 0.45: 8e-6}


def run_p(torque, seconds, hyst=10.0, upstream=None, below=3.0):
    """auto-p from `below` K under the valve's (unknown to it) opening point.
    Returns [(t, P_vacuum, duty)], messages, final state."""
    fit, open_c, bar, efold, rise, base = VALVES[torque]
    upstream = upstream or (lambda t: bar)
    th = Thermal(fit, t0=open_c - below)
    valve = Valve(open_c, bar, efold, rise_open=rise, hyst=hyst, base=base)
    h, now = controller.new_state(), 1000.0
    controller.command(h, now, mode='auto-p', p_target_mbar=TARGETS[torque], armed=True)
    out, msgs = [], []
    for k in range(int(seconds / DT)):
        t = k * DT
        up = upstream(t)
        valve.step(th.t, up, DT)
        vac = valve.vac()
        duty, m = controller.step(h, now + t, DT, th.temp(), True, vac=vac,
                                  p_up=up, p_up_t=now + t)
        msgs += m
        th.step(duty, DT)
        out.append((t, vac, duty))
    return out, msgs, h


@pytest.mark.parametrize("hyst", [10.0, 1.0])
@pytest.mark.parametrize("torque", sorted(VALVES))
def test_p_vacuum_never_passes_the_target(torque, hyst):
    out, msgs, h = run_p(torque, 1800, hyst=hyst)
    target = TARGETS[torque]
    assert max(p for _, p, _ in out) < target
    assert any("moved" in m for m in msgs)                     # it did open
    assert h['trip_reason'] is None


@pytest.mark.parametrize("torque", sorted(VALVES))
def test_a_valve_with_wide_hysteresis_settles_open_below_the_cut_line(torque):
    out, _, h = run_p(torque, 1800)
    base, cut = VALVES[torque][5], 0.7 * TARGETS[torque]
    tail = [p for t, p, _ in out if t > 1500]
    assert min(tail) > 1.05 * base and max(tail) < cut


def test_a_refill_cuts_the_heater_on_the_first_reading_over_the_line():
    # 0.25 N·m, open and settled; then the upstream is refilled 2.1 → 4 bar,
    # which multiplies the flow by ~2.6 at a fixed opening
    out, msgs, h = run_p(0.25, 1800, upstream=lambda t: 2.1 if t < 1500 else 4.0)
    cut = 0.7 * TARGETS[0.25]
    after = [(t, p, d) for t, p, d in out if t >= 1500]
    first = next(i for i, (_, p, _) in enumerate(after) if p > cut)
    assert all(d == 0.0 for _, _, d in after[first:first + 8])
    assert any("heater OFF" in m for m in msgs)
