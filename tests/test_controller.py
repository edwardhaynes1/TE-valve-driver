"""controller.py on its own: the interlock and command rules, stated as
small examples. No threads, clock or shared state are involved."""
import pytest

from driver import config, controller

T0 = 1_000.0


@pytest.fixture
def h():
    return controller.new_state()


def step(h, now=T0, temp=30.0, healthy=True, **kw):
    return controller.step(h, now, 0.25, temp, healthy, **kw)


def armed(h, mode='manual', **kw):
    controller.command(h, T0, armed=True, mode=mode, **kw)
    return h


def test_disarmed_never_heats(h):
    controller.command(h, T0, mode='manual', duty_cmd=0.5)
    assert step(h) == (0.0, [])


def test_manual_duty_is_clamped(h):
    armed(h, duty_cmd=1.7)
    assert step(h)[0] == config.HEATER_MAX_DUTY
    controller.command(h, T0, duty_cmd=-0.2)
    assert step(h)[0] == 0.0


def test_disarming_zeroes_the_manual_duty(h):
    armed(h, duty_cmd=0.4)
    controller.command(h, T0, armed=False)
    assert h['duty_cmd'] == 0.0 and not h['armed']


@pytest.mark.parametrize("kw, reason", [
    (dict(healthy=False), "thermocouple unavailable"),
    (dict(temp=None), "thermocouple unavailable"),
    (dict(temp=config.TEMP_TRIP_C + 0.1), "over-temperature"),
    (dict(now=T0 + config.HEATER_MAX_RUN_S + 1), "maximum armed time"),
])
def test_interlocks_trip_in_every_mode(h, kw, reason):
    armed(h, duty_cmd=0.5)
    duty, msgs = step(h, **kw)
    assert duty == 0.0
    assert reason in h['trip_reason']
    assert msgs == [f"HEATER TRIP — {h['trip_reason']}"]
    assert not h['armed']


def test_a_trip_latches_until_disarm_then_arm(h):
    armed(h, duty_cmd=0.5)
    step(h, temp=200.0)
    controller.command(h, T0, duty_cmd=0.5)            # re-commanding does not clear it
    assert step(h) == (0.0, [])
    controller.command(h, T0, armed=False)
    controller.command(h, T0, armed=True, duty_cmd=0.5)
    assert h['trip_reason'] is None
    assert step(h)[0] == 0.5


def test_the_first_trip_reason_is_kept(h):
    msgs = []
    controller.trip(h, "first", msgs)
    controller.trip(h, "second", msgs)
    assert h['trip_reason'] == "first"
    assert msgs == ["HEATER TRIP — first", "HEATER TRIP — second"]


def test_over_pressure_trips_only_in_pressure_mode(h):
    armed(h, duty_cmd=0.2)
    assert step(h, vac=1e-3)[0] == 0.2                 # manual: ignored
    armed(h, mode='auto-p', p_target_mbar=1e-6)
    duty, _ = step(h, vac=1e-3)
    assert duty == 0.0 and "over-pressure" in h['trip_reason']


def test_unreadable_high_pressure_trips_pressure_mode(h):
    armed(h, mode='auto-p')
    step(h, vac=None, vac_status=config.VAC_OVER)
    assert "too high to measure" in h['trip_reason']


def test_pressure_mode_waits_for_its_first_reading(h):
    armed(h, mode='auto-p')
    assert step(h, vac=None) == (0.0, [])
    assert h['p_init'] and h['armed']


def test_pressure_mode_needs_a_live_gauge(h):
    armed(h, mode='auto-p')
    step(h, vac=None, vac_healthy=False, vac_status=config.VAC_ERROR)
    assert "needs a live gauge" in h['trip_reason']


def test_a_cold_start_in_pressure_mode_bursts_at_full_power(h):
    armed(h, mode='auto-p', p_target_mbar=1e-6)
    duty, msgs = step(h, temp=25.0, vac=1.5e-7, p_up=2.76, p_up_t=T0)
    assert duty == config.HEATER_MAX_DUTY
    assert h['p_phase'] == 'seek' and h['p_burst'] == 'burst'
    assert any("full power" in m for m in msgs)


def test_stale_upstream_readings_are_ignored(h):
    armed(h, mode='auto-p', p_target_mbar=1e-6)
    old = T0 - config.PRESSURE_UP_MAX_AGE_S - 1
    step(h, temp=25.0, vac=1.5e-7, p_up=1.0, p_up_t=old)
    assert h['p_up_bar'] is None and h['p_shift'] == 0.0


def test_gate_edges_account_for_on_time(h):
    controller.record_edge(h, True, 0.5, T0)
    row = controller.record_edge(h, False, 0.5, T0 + 1.25)
    assert row['gate'] == 0 and row['on_s'] == 1.25
    assert h['on_time_acc'] == pytest.approx(1.25)


def test_new_state_is_independent(h):
    other = controller.new_state()
    h['p_hist'].append((0, 0))
    assert not other['p_hist']


def test_unknown_mode_is_refused(h):
    # e.g. an old name: silently accepting it would run the temperature PI
    with pytest.raises(ValueError):
        controller.command(h, T0, mode='pressure')
    assert h['mode'] == controller.MANUAL


def test_mode_names_are_the_documented_ones():
    assert controller.MODES == ('manual', 'auto-t', 'auto-p')
