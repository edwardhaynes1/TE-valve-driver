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


def test_a_misspelt_field_is_an_error_not_a_new_field(h):
    with pytest.raises(KeyError):
        h['p_targte_mbar'] = 1e-6
    with pytest.raises(KeyError):
        h.update(armd=True)
    with pytest.raises(KeyError):
        h['armd']
    with pytest.raises(TypeError):
        h.pop('armed')
    h['armed'] = True                                    # real fields still work
    h.update(duty_cmd=0.3)
    assert h['armed'] and h['duty_cmd'] == 0.3


def test_the_state_holds_no_measured_readout():
    # the measured voltage / current belongs to the device thread (shared.py)
    h = controller.new_state()
    assert not [k for k in h if 'meas' in k or k in ('out_high', 'v_now', 'i_now')]


# ── where the valve opens: seat screw torque and upstream pressure ──────────

def arm_pressure(h, target=1e-6, seat_nm=None, **kw):
    armed(h, mode=controller.AUTO_P, p_target_mbar=target)
    return step(h, temp=25.0, vac=1.5e-7, seat_nm=seat_nm, **kw)


@pytest.mark.parametrize("torque", sorted(config.SEAT_SCREW_VALVE))
def test_a_calibrated_torque_sets_the_opening_point(h, torque):
    open_c, bar, _ = config.SEAT_SCREW_VALVE[torque]
    duty, msgs = arm_pressure(h, seat_nm=torque, p_up=bar, p_up_t=T0)
    # at the calibration's own upstream pressure: no further shift
    assert h['p_ref'] == pytest.approx(open_c) and h['p_ref_how'] == 'calibrated'
    assert h['setpoint_C'] == pytest.approx(open_c)
    assert any(f"{torque:.2f} N·m calibrated" in m for m in msgs)
    assert duty == config.HEATER_MAX_DUTY          # cold start, far below: burst


def test_within_tolerance_still_matches(h):
    open_c, bar, _ = config.SEAT_SCREW_VALVE[0.40]
    arm_pressure(h, seat_nm=0.40 + config.SEAT_SCREW_TOL_NM / 2, p_up=bar, p_up_t=T0)
    assert h['p_ref'] == pytest.approx(open_c) and h['p_ref_how'] == 'calibrated'


def test_between_entries_is_interpolated_at_a_common_upstream_pressure():
    (c0, b0, _), (c1, b1, _) = config.SEAT_SCREW_VALVE[0.40], config.SEAT_SCREW_VALVE[0.45]
    c, bar, efold, how, detail = controller.opening_point(0.425)
    c1_at_b0 = c1 - config.PRESSURE_UP_K_PER_BAR * (b0 - b1)
    assert how == 'interpolated' and bar == b0 and "UNVERIFIED" in detail
    assert c == pytest.approx((c0 + c1_at_b0) / 2)
    assert config.SEAT_SCREW_VALVE[0.40][2] < efold < config.SEAT_SCREW_VALVE[0.45][2]


def test_a_missing_efold_is_interpolated_from_its_neighbours():
    _, _, efold, how, _ = controller.opening_point(0.30)
    lo, hi = config.SEAT_SCREW_VALVE[0.25][2], config.SEAT_SCREW_VALVE[0.40][2]
    assert how == 'calibrated'
    assert efold == pytest.approx(lo + (hi - lo) * (0.30 - 0.25) / (0.40 - 0.25))


@pytest.mark.parametrize("torque, nearest", [(0.75, 0.45), (0.10, 0.25)])
def test_outside_the_table_uses_the_nearest_entry_flagged(h, torque, nearest):
    _, msgs = arm_pressure(h, seat_nm=torque, p_up=2.76, p_up_t=T0)
    assert h['p_ref_how'] == 'nearest'
    assert any("outside the calibrated" in m and "UNVERIFIED" in m for m in msgs)
    c, bar, _ = config.SEAT_SCREW_VALVE[nearest]
    assert h['p_ref'] == pytest.approx(c + controller._upstream_shift(2.76, bar))


def test_no_torque_uses_the_16_sept_reference(h):
    _, msgs = arm_pressure(h, seat_nm=None, p_up=config.PRESSURE_UP_REF_BAR, p_up_t=T0)
    assert h['p_ref'] == config.PRESSURE_SEEK_START_C and h['p_ref_how'] == 'no torque'
    assert any("no seat screw torque entered" in m for m in msgs)


def test_more_upstream_pressure_lowers_the_opening_point(h):
    open_c, bar, _ = config.SEAT_SCREW_VALVE[0.40]
    arm_pressure(h, seat_nm=0.40, p_up=bar + 1.0, p_up_t=T0)
    assert h['p_ref'] == pytest.approx(open_c - config.PRESSURE_UP_K_PER_BAR)


def test_the_upstream_shift_is_limited_more_upwards_than_downwards():
    # far above the calibration pressure: the opening point goes down, a lot
    assert controller._upstream_shift(11.0, 1.0) == -config.PRESSURE_UP_MAX_DOWN_K
    # far below it: up, but only a little
    assert controller._upstream_shift(1.0, 11.0) == config.PRESSURE_UP_MAX_SHIFT_K
    assert config.PRESSURE_UP_MAX_SHIFT_K < config.PRESSURE_UP_MAX_DOWN_K


def test_a_stale_upstream_reading_gives_no_shift(h):
    open_c, _, _ = config.SEAT_SCREW_VALVE[0.40]
    arm_pressure(h, seat_nm=0.40, p_up=5.0, p_up_t=T0 - config.PRESSURE_UP_MAX_AGE_S - 1)
    assert h['p_ref'] == pytest.approx(open_c) and h['p_up_bar'] is None


def test_the_burst_is_decided_by_distance_to_the_goal_not_absolute_temperature(h):
    # 60 °C is a warm start, but 90 K below the 0.45 N·m opening point
    open_c, bar, _ = config.SEAT_SCREW_VALVE[0.45]
    armed(h, mode=controller.AUTO_P, p_target_mbar=5e-6)
    duty, _ = step(h, temp=60.0, vac=2.7e-7, seat_nm=0.45, p_up=bar, p_up_t=T0)
    assert h['p_burst'] == 'burst' and duty == config.HEATER_MAX_DUTY
    h2 = controller.new_state()
    armed(h2, mode=controller.AUTO_P, p_target_mbar=5e-6)
    step(h2, temp=open_c - config.PRESSURE_BURST_MIN_STEP_K + 1, vac=2.7e-7,
         seat_nm=0.45, p_up=bar, p_up_t=T0)
    assert h2['p_burst'] is None


def test_hold_power_matches_the_measured_holds():
    # 21 Sept 2026 steady holds (W): the fitted curve is within ±15 % of each
    for t_c, watts in [(40.2, 0.507), (55.1, 0.916), (65.4, 1.399), (125.0, 3.012),
                       (140.1, 3.793), (145.5, 4.039)]:
        assert controller.hold_duty(t_c) * config.heater_power_w() == \
            pytest.approx(watts, rel=0.15)
    assert controller.hold_duty(config.HEATER_HOLD_AMBIENT_C - 5) == 0.0
