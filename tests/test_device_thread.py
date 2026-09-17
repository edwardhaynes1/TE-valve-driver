"""devices.labjack_thread against a fake U3: gate switching, the
thermocouple interlock, write errors and shutdown. Runs in real time
(a few seconds) with a shortened PWM period."""
import threading
import time
import types

import pytest

pytest.importorskip("serial")

from fake_u3 import FakeU3  # noqa: E402
from tevalve import config, control, devices, shared  # noqa: E402

PERIOD = 0.5


@pytest.fixture
def lj(monkeypatch):
    fake = FakeU3(config.VACUUM_DIVIDER_RATIO)
    monkeypatch.setattr(control, "clock", time.time)          # real time here
    monkeypatch.setattr(devices, "LABJACK_AVAILABLE", True)
    monkeypatch.setattr(devices, "u3", types.SimpleNamespace(U3=lambda: fake), raising=False)
    monkeypatch.setattr(devices, "HEATER_PWM_PERIOD_S", PERIOD)
    th = threading.Thread(target=devices.labjack_thread, daemon=True)
    th.start()
    wait_for(lambda: shared.tc_ok and shared.readings['te_temperature_degC'] is not None)
    fake.thread = th
    yield fake
    shared.stop.set()
    th.join(timeout=3)


def wait_for(cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def edges_with(note):
    with shared.lock:
        return [e for e in shared.pwm_edges if note in e['note']]


def test_connect_forces_the_gate_low_first(lj):
    assert lj.writes[0][1] == 0
    assert edges_with("connect: forced low")
    assert lj.config["NumberOfTimersEnabled"] == 0              # FIO4 stays SPI
    assert lj.watchdog_cfg["SetDIOStateOnTimeout"] is True
    assert shared.readings['vacuum_chamber_mbar'] == pytest.approx(1.5e-7, rel=1e-3)
    assert shared.readings['te_temperature_degC'] == pytest.approx(30.0, abs=0.01)


def test_manual_duty_becomes_gate_switching(lj):
    control.heater_command(mode='manual', duty_cmd=0.4, armed=True)
    wait_for(lambda: lj.gate_now() == 1)
    t0 = time.time()
    time.sleep(4 * PERIOD)
    frac = lj.on_fraction(t0, time.time())
    assert frac == pytest.approx(0.4, abs=0.06)               # 50 ms tick resolution
    with shared.lock:
        on_edges = [e for e in shared.pwm_edges if e['gate'] == 1]
        off_edges = [e for e in shared.pwm_edges if e['gate'] == 0 and e['on_s'] != '']
    assert len(on_edges) >= 3
    assert all(e['on_s'] == pytest.approx(0.4 * PERIOD, abs=0.06) for e in off_edges)


def test_disarm_stops_heating_within_a_tick(lj):
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: lj.gate_now() == 1)
    control.heater_command(armed=False)
    wait_for(lambda: lj.gate_now() == 0, timeout=0.3)


def test_thermocouple_fault_trips_and_drops_the_gate(lj):
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: lj.gate_now() == 1)
    lj.fault = 0x01                                         # open circuit
    wait_for(lambda: shared.heater['trip_reason'] is not None, timeout=1.0)
    wait_for(lambda: lj.gate_now() == 0, timeout=0.3)
    assert "thermocouple" in shared.heater['trip_reason']
    lj.fault = 0
    time.sleep(0.5)
    assert lj.gate_now() == 0                               # latched until re-armed


def test_over_temperature_trips(lj):
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: lj.gate_now() == 1)
    lj.temp_c = config.TEMP_TRIP_C + 1
    wait_for(lambda: lj.gate_now() == 0, timeout=1.0)
    assert "over-temperature" in shared.heater['trip_reason']


def test_write_error_reconnects_with_the_gate_low(lj):
    lj.fail_next_on_write = True
    control.heater_command(mode='manual', duty_cmd=0.5, armed=True)
    wait_for(lambda: edges_with("write error"))
    wait_for(lambda: edges_with("reconnect: forced low"))
    assert not shared.heater['armed']                       # reconnect disarms


def test_shutdown_leaves_the_gate_low(lj):
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: lj.gate_now() == 1)
    shared.stop.set()
    lj.thread.join(timeout=3)
    assert lj.gate_now() == 0
    assert edges_with("shutdown: forced low")
    assert lj.watchdog_cfg["SetDIOStateOnTimeout"] is False  # watchdog released
