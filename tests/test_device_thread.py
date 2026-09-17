"""The LabJack thread against a fake U3: gate switching, the
thermocouple interlock, write errors and shutdown. Runs in real time
(a few seconds) with a shortened PWM period."""
import time

import pytest


from device_harness import STEP, TICK, edges_with, start, stop, wait_for  # noqa: E402
from fake_u3 import FakeU3  # noqa: E402
from driver import config, control, shared  # noqa: E402

PERIOD = 1.0


@pytest.fixture
def lj(monkeypatch):
    fake = FakeU3(config.VACUUM_DIVIDER_RATIO)
    fake.thread = start(monkeypatch, fake, HEATER_PWM_PERIOD_S=PERIOD)
    yield fake
    stop(fake.thread)


def test_connect_forces_the_gate_low_first(lj):
    assert lj.writes[0][1] == 0
    assert edges_with("connect: forced low")
    assert lj.config["NumberOfTimersEnabled"] == 0              # FIO4 stays SPI
    assert lj.watchdog_cfg["SetDIOStateOnTimeout"] is True
    r = shared.latest()
    assert r['vacuum_chamber_mbar'] == pytest.approx(1.5e-7, rel=1e-3)
    assert r['te_temperature_degC'] == pytest.approx(30.0, abs=0.01)


def test_manual_duty_becomes_gate_switching(lj):
    control.heater_command(mode='manual', duty_cmd=0.4, armed=True)
    wait_for(lambda: lj.gate_now() == 1)
    t0 = time.time()
    time.sleep(3 * PERIOD)
    frac = lj.on_fraction(t0, time.time())
    # each edge lands on a tick, and Windows stretches ticks: allow 1.5 ticks per period
    assert frac == pytest.approx(0.4, abs=1.5 * TICK / PERIOD)
    edges = shared.pending_edges()
    on_edges = [e for e in edges if e['gate'] == 1]
    off_edges = [e for e in edges if e['gate'] == 0 and e['on_s'] != '']
    assert len(on_edges) >= 3
    # the first on-period can be partial: arming lands anywhere in the running PWM cycle
    full = [e['on_s'] for e in off_edges[1:]]
    assert full, "no complete on-periods recorded"
    assert all(s == pytest.approx(0.4 * PERIOD, abs=2 * TICK) for s in full), full


def test_disarm_stops_heating_within_a_control_step(lj):
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: lj.gate_now() == 1)
    control.heater_command(armed=False)
    wait_for(lambda: lj.gate_now() == 0, timeout=STEP + 0.5)


def test_thermocouple_fault_trips_and_drops_the_gate(lj):
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: lj.gate_now() == 1)
    lj.fault = 0x01                                         # open circuit
    wait_for(lambda: control.snapshot()['trip_reason'] is not None, timeout=1.0)
    wait_for(lambda: lj.gate_now() == 0, timeout=0.3)
    assert "thermocouple" in control.snapshot()['trip_reason']
    lj.fault = 0
    time.sleep(0.5)
    assert lj.gate_now() == 0                               # latched until re-armed


def test_over_temperature_trips(lj):
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: lj.gate_now() == 1)
    lj.temp_c = config.TEMP_TRIP_C + 1
    wait_for(lambda: lj.gate_now() == 0, timeout=1.0)
    assert "over-temperature" in control.snapshot()['trip_reason']


def test_write_error_reconnects_with_the_gate_low(lj):
    lj.fail_next_on_write = True
    control.heater_command(mode='manual', duty_cmd=0.5, armed=True)
    wait_for(lambda: edges_with("write error"))
    wait_for(lambda: edges_with("reconnect: forced low"))
    assert not control.snapshot()['armed']                  # reconnect disarms


def test_shutdown_leaves_the_gate_low(lj):
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: lj.gate_now() == 1)
    shared.stop.set()
    lj.thread.join(timeout=3)
    assert lj.gate_now() == 0
    assert edges_with("shutdown: forced low")
    assert lj.watchdog_cfg["SetDIOStateOnTimeout"] is False  # watchdog released
