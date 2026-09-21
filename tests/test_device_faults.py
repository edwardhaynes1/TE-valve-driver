"""The LabJack thread's error and sensing paths, against a fake U3:
lost readings, gauge range changes, thermocouple faults and recovery,
measured heater voltage / current, and a failed force-low at shutdown."""
import time

import pytest


from device_harness import (  # noqa: E402
    STEP, TICK, edges_with, events_with, start, stop, wait_for,
)
from fake_u3 import I_SENSE_FIO, V_SENSE_FIO, FakeU3  # noqa: E402
from driver import config, control, shared  # noqa: E402

PERIOD = 1.0
SENSE = dict(HEATER_V_AIN=V_SENSE_FIO, HEATER_I_AIN=I_SENSE_FIO)


@pytest.fixture
def fake():
    return FakeU3(config.VACUUM_DIVIDER_RATIO)


@pytest.fixture
def running(monkeypatch, fake):
    """Start the thread with the given settings; always stopped afterwards."""
    threads = []

    def run(**settings):
        settings.setdefault("HEATER_PWM_PERIOD_S", PERIOD)
        threads.append(start(monkeypatch, fake, **settings))
        return threads[-1]
    yield run
    for th in threads:
        stop(th)


# ── vacuum gauge ─────────────────────────────────────────────────────────────

def test_vacuum_read_error_reconnects_with_the_gate_low(running, fake):
    running()
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: fake.gate_now() == 1)
    fake.fail_vacuum = True
    wait_for(lambda: events_with("LabJack vacuum read error"))
    wait_for(lambda: edges_with("reconnect: forced low"))
    assert fake.gate_now() == 0
    assert not control.snapshot()['armed']
    assert events_with("LabJack disconnected — heater forced off")
    fake.fail_vacuum = False
    wait_for(lambda: shared.health()['labjack']
             and shared.latest()['vacuum_chamber_mbar'] is not None)


def test_gauge_range_changes_are_logged_once(running, fake):
    running()
    fake.gauge_volts = 9.0                                   # above 8.6 V: overrange
    wait_for(lambda: events_with("Vacuum gauge overrange"))
    time.sleep(4 * STEP)
    assert len(events_with("Vacuum gauge overrange")) == 1
    r = shared.latest()
    assert r['vacuum_chamber_mbar'] is None
    assert r['vacuum_status'] == config.VAC_OVER
    assert r['vacuum_gauge_V'] == pytest.approx(9.0)
    fake.gauge_volts = None
    wait_for(lambda: events_with("Vacuum gauge back in range"))
    assert shared.latest()['vacuum_chamber_mbar'] == pytest.approx(1.5e-7, rel=1e-3)


# ── thermocouple ─────────────────────────────────────────────────────────────

def test_thermocouple_read_error_trips_then_recovers(running, fake):
    running(TC_RETRY_S=0.5)
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: fake.gate_now() == 1)
    fake.fail_spi = True
    wait_for(lambda: not shared.health()['tc'])
    assert shared.latest()['te_temperature_degC'] is None
    wait_for(lambda: fake.gate_now() == 0, timeout=STEP + 0.5)
    assert "thermocouple" in control.snapshot()['trip_reason']
    assert events_with("MAX31856 read error")
    fake.fail_spi = False
    wait_for(lambda: shared.health()['tc'])
    wait_for(lambda: shared.latest()['te_temperature_degC'] is not None)
    assert len(events_with("MAX31856 thermocouple init OK")) == 2
    assert fake.gate_now() == 0                              # still latched


def test_absent_thermocouple_is_reported_once_and_retried(running, fake):
    fake.tc_absent = True
    running(wait_for_tc=False, TC_RETRY_S=0.3)
    wait_for(lambda: events_with("MAX31856 not responding"))
    assert "all-zero" in events_with("MAX31856 not responding")[0]
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: control.snapshot()['trip_reason'] is not None)
    assert "thermocouple" in control.snapshot()['trip_reason']
    time.sleep(1.0)                                          # several retries
    assert len(events_with("MAX31856 not responding")) == 1
    assert fake.gate_now() == 0
    fake.tc_absent = False
    wait_for(lambda: shared.health()['tc'])


# ── measured heater voltage and current ─────────────────────────────────────

# The measured means must follow what the heater actually did over the last
# PWM period, however the ticks fall: on time (0.0), all late (0.075 s: every
# tick ~75 ms instead of 50, as when Windows rounds waits up), or irregular
# (up to 0.12 s of random stalls, like a busy shared machine). The reference
# is the fake gate's own record of when it was on, not an ideal 50 %, because
# a stalled tick really does keep the heater on (or off) for longer.
@pytest.mark.parametrize("delay, jitter", [(0.0, 0.0), (0.075, 0.0), (0.0, 0.12)])
def test_measured_power_follows_the_duty(running, fake, delay, jitter):
    fake.sense_delay_s = delay
    fake.sense_jitter_s = jitter
    running(**SENSE)
    assert fake.config["FIOAnalog"] == (1 << 1) | (1 << 2) | (1 << 3)
    control.heater_command(mode='manual', duty_cmd=0.5, armed=True)
    wait_for(lambda: fake.gate_now() == 1)
    time.sleep(1.5 * PERIOD)
    full = 24.0 ** 2 / 88.0
    worst = 0.0
    t_start = time.time()
    t_end = t_start + 2 * PERIOD           # watch the readout for two periods
    while time.time() < t_end:
        now = time.time()
        measured = shared.heater_output()['p_meas_mean'] / full
        # The reading covers the period up to the driver's latest tick, which
        # can lag `now` by a stalled tick; compare with windows ending anywhere
        # in the last 0.3 s and take the closest.
        ends = [now - 0.01 * k for k in range(31)]
        worst = max(worst, min(abs(measured - fake.on_fraction(e - PERIOD, e))
                               for e in ends))
        time.sleep(TICK)
    # A sampled measurement is only as fine as its sample spacing: allow one
    # gap for where the window starts and one for reading-vs-switching skew,
    # using the longest gap actually seen (a busy machine stretches them).
    times = [t for t in fake.sense_times if t >= t_start - PERIOD]
    gap = max(b - a for a, b in zip(times, times[1:]))
    assert gap < 0.5, f"ticks up to {gap:.2f} s apart: machine too busy to judge"
    slack = 2 * max(gap, TICK) / PERIOD
    assert worst <= slack, (f"measured power differs from the gate's by {worst:.3f} "
                            f"of full power (limit {slack:.3f}, longest gap {gap * 1000:.0f} ms)")
    out = shared.heater_output()
    assert out['rail_meas'] == pytest.approx(24.0)
    assert out['v_meas_mean'] / 24.0 == pytest.approx(out['p_meas_mean'] / full, abs=1e-9)
    assert 0.3 < out['p_meas_mean'] / full < 0.7          # and it is about 50 %


def test_no_current_with_the_gate_on_is_warned(running, fake):
    fake.element_ohm = None                                  # SW171 off / element open
    running(**SENSE)
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: events_with("gate ON but heater current"), timeout=3.0)
    assert control.snapshot()['armed']                       # a warning, not a trip


def test_current_with_the_gate_off_trips(running, fake):
    fake.stray_a = 0.25                                      # Q171 shorted
    running(**SENSE)
    wait_for(lambda: control.snapshot()['trip_reason'] is not None, timeout=3.0)
    assert control.snapshot()['trip_reason'] == "heater current with gate OFF"
    assert events_with("Q171 may be shorted")


# ── connecting and stopping ─────────────────────────────────────────────────

def test_connect_failure_is_reported_and_stop_still_works(monkeypatch, fake):
    def broken():
        raise IOError("no U3 found")
    th = start(monkeypatch, fake, wait_for_tc=False, u3_factory=broken)
    wait_for(lambda: events_with("LabJack connect failed: no U3 found"))
    assert not shared.health()['labjack'] and not shared.health()['tc']
    stop(th)                                                 # wakes it from the 5 s wait


def test_failed_force_low_at_shutdown_is_logged(running, fake):
    th = running()
    control.heater_command(mode='manual', duty_cmd=1.0, armed=True)
    wait_for(lambda: fake.gate_now() == 1)
    fake.fail_all_writes = True
    stop(th)
    assert edges_with("shutdown: force-low write FAILED")
