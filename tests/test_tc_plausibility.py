"""Thermocouple plausibility: the heater trips when the valve temperature
reading cannot be the valve.

On 21 Sept 2026 (17:20-17:31) the TC gave nonsense with no MAX31856 fault
bit, and the controller kept heating on it: at full power for ~40 s while the
reading FELL from 72 to -30 °C (then again, bursting "from -15.9 °C"), and
readings jumping 26 → 51 → 27 → 77 °C within seconds. Two checks
(TC_MAX_RATE_K_S, TC_RESPONSE_*) now trip on both. Excerpts of those logs are
in tests/data/.
"""
import csv
from pathlib import Path

import pytest

from driver import config, controller
from plant_21sept import Thermal

DATA = Path(__file__).parent / "data"
DT = 0.25


def replay(name, setpoint):
    """Arm auto-t at the start of a logged TC trace and feed it in (linearly
    interpolated to the control rate). Returns (seconds to trip, reason)."""
    rows = [r for r in csv.reader(open(DATA / name, encoding="utf-8"))
            if r and not r[0].startswith("#")][1:]
    ts = [float(r[0]) for r in rows]
    temps = [float(r[1]) for r in rows]
    h = controller.new_state()
    now = 1000.0
    controller.command(h, now, mode='auto-t', setpoint_C=setpoint, armed=True)
    t, i = 0.0, 0
    while t <= ts[-1]:
        while ts[i + 1] < t:
            i += 1
        f = (t - ts[i]) / (ts[i + 1] - ts[i])
        temp = temps[i] + f * (temps[i + 1] - temps[i])
        controller.step(h, now + t, DT, temp, True)
        if h['trip_reason']:
            return t, h['trip_reason']
        t += DT
    return None, None


def test_a_reading_that_falls_at_full_power_trips():
    # 172024: the real run heated at full power on this for ~40 s
    t, reason = replay("tc_fault_20260921_172024.csv", setpoint=75.0)
    assert reason is not None and "did not respond to full power" in reason
    assert "SW171" in reason
    assert t < 35.0


def test_a_jumping_reading_trips():
    t, reason = replay("tc_fault_20260921_173050.csv", setpoint=30.0)
    assert reason is not None and "jumped" in reason


def closed_loop(thermal, setpoint, seconds, h=None):
    h = h or controller.new_state()
    now = 1000.0
    controller.command(h, now, mode='auto-t', setpoint_C=setpoint, armed=True)
    for k in range(int(seconds / DT)):
        duty, _ = controller.step(h, now + k * DT, DT, thermal.temp(), True)
        thermal.step(duty, DT)
        if h['trip_reason']:
            break
    return h


def test_heater_supply_off_trips():
    th = Thermal("152054", t0=25.0)
    th.powered = False                          # SW171 open: no heat at all
    h = closed_loop(th, 90.0, 60)
    assert "did not respond to full power" in (h['trip_reason'] or "")


@pytest.mark.parametrize("fit, setpoint", [("150128", 40.0), ("152054", 90.0),
                                           ("173217", 155.0)])
def test_real_heating_never_trips(fit, setpoint):
    # the three fitted rigs, from room temperature to the top of each fit's range
    h = closed_loop(Thermal(fit, t0=25.0), setpoint, 400)
    assert h['trip_reason'] is None and h['armed']


def test_the_response_check_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(controller, "TC_RESPONSE_S", None)
    th = Thermal("152054", t0=25.0)
    th.powered = False
    h = closed_loop(th, 90.0, 60)
    assert h['trip_reason'] is None


def test_rearming_does_not_count_the_change_while_disarmed_as_a_jump():
    h = controller.new_state()
    controller.command(h, 0.0, mode='auto-t', setpoint_C=30.0, armed=True)
    controller.step(h, 0.0, DT, 80.0, True)
    controller.command(h, 1.0, armed=False)
    controller.command(h, 60.0, armed=True)
    controller.step(h, 60.0, DT, 30.0, True)    # cooled 50 K while disarmed
    assert h['trip_reason'] is None


def test_limits_sit_well_outside_what_the_real_valve_did():
    # 21 Sept 2026 good runs: at most ~3.6 °C/s between 0.5 s rows (noise
    # included), and never less than 3.5 K in 15 s at full power.
    assert config.TC_MAX_RATE_K_S >= 5 * 3.6
    assert config.TC_RESPONSE_MIN_K <= 3.5 / 3
