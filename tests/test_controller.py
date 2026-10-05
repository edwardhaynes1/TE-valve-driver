"""controller.py on its own: the interlock and command rules, stated as
small examples. No threads, clock or shared state are involved."""
import math

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


def test_auto_p_starts_by_holding_where_the_valve_is_never_a_burst(h):
    armed(h, mode='auto-p', p_target_mbar=1e-6)
    duty, msgs = step(h, temp=25.0, vac=1.5e-7, p_up=1.0, p_up_t=T0)
    assert h['p_phase'] == 'baseline' and h['setpoint_C'] == 25.0
    assert duty < config.HEATER_MAX_DUTY
    assert any("no opening point assumed" in m for m in msgs)


def test_stale_upstream_readings_are_ignored(h):
    armed(h, mode='auto-p', p_target_mbar=1e-6)
    old = T0 - config.PRESSURE_UP_MAX_AGE_S - 1
    _, msgs = step(h, temp=25.0, vac=1.5e-7, p_up=1.0, p_up_t=old)
    assert h['p_up_bar'] is None
    assert any("not read — slowest creep" in m for m in msgs)


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


# ── the torque table's guess (batches and t-min-tune; not auto-p) ──────────

@pytest.mark.parametrize("torque", sorted(config.SEAT_SCREW_VALVE))
def test_a_calibrated_torque_gives_its_table_entry(torque):
    open_c, bar, _ = config.SEAT_SCREW_VALVE[torque]
    c, at_bar, _, how, detail = controller.opening_point(torque)
    assert (c, at_bar, how) == (open_c, bar, 'calibrated')
    assert f"{torque:.2f} N·m calibrated" in detail


def test_within_tolerance_still_matches():
    open_c, _, _ = config.SEAT_SCREW_VALVE[0.40]
    c, _, _, how, _ = controller.opening_point(0.40 + config.SEAT_SCREW_TOL_NM / 2)
    assert c == pytest.approx(open_c) and how == 'calibrated'


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
def test_outside_the_table_uses_the_nearest_entry_flagged(torque, nearest):
    c, _, _, how, detail = controller.opening_point(torque)
    assert how == 'nearest' and "outside the calibrated" in detail and "UNVERIFIED" in detail
    assert c == config.SEAT_SCREW_VALVE[nearest][0]


def test_no_torque_uses_the_16_sept_reference():
    c, bar, _, how, _ = controller.opening_point(None)
    assert (c, bar, how) == (config.PRESSURE_SEEK_START_C, config.PRESSURE_UP_REF_BAR,
                             'no torque')


# ── auto-p (history 50): no opening point, creep, back off, cut ─────────────

BASE = 1.5e-7
TARGET = 5e-7                                   # the aim
CUT = config.PRESSURE_MAX_MBAR / config.PRESSURE_SOAK_FACTOR    # 6.43e-7


def run_p(h, vacs, temp=40.0, p_up=1.0, start=T0, armed_now=True):
    """Step auto-p through raw readings vacs (one per 0.25 s) at a fixed TC.
    Returns (duties, messages, next time)."""
    if armed_now:
        armed(h, mode=controller.AUTO_P, p_target_mbar=TARGET)
    duties, msgs, now = [], [], start
    for vac in vacs:
        d, m = controller.step(h, now, 0.25, temp, True, vac=vac, p_up=p_up, p_up_t=now)
        duties.append(d)
        msgs += m
        now += 0.25
    return duties, msgs, now


def creeping(h, temp=40.0, p_up=1.0, seconds=20, base=BASE):
    """Armed, baseline measured, creeping with the valve shut."""
    _, msgs, now = run_p(h, [base] * int(seconds * 4), temp=temp, p_up=p_up)
    assert h['p_phase'] == 'seek'
    return msgs, now


@pytest.mark.parametrize("p_up, rate", [
    (1.0, 1.5), (2.0, 1.5 / 2 ** 1.5), (0.5, 2.0),   # 0.5 bar: 4.24, kept at the fastest
    (5.0, 0.3), (None, 0.3),                         # slowest: high P_up, or not read
])
def test_the_creep_rate_follows_only_the_upstream_pressure(p_up, rate):
    assert controller._creep_rate(p_up) == pytest.approx(rate)


def test_the_cut_line_leaves_room_for_the_soak_below_p_vacuum_max():
    assert CUT == pytest.approx(6.43e-7, rel=1e-3)
    assert TARGET < CUT < config.PRESSURE_MAX_MBAR == 9e-7


def test_it_creeps_once_the_baseline_is_measured(h):
    msgs, _ = creeping(h)
    assert any("baseline 1.50e-07 mbar" in m and "aiming at 5.00e-07" in m
               and "creeping 1.50 °C/min" in m for m in msgs)
    assert 40.0 < h['setpoint_C'] < 40.0 + 1.5 * 20 / 60


def test_two_readings_above_baseline_plus_margin_stop_the_creep(h):
    _, now = creeping(h, temp=40.0)
    moved = BASE * 10 ** (config.PRESSURE_MOVE_MIN_DEC + 0.005)
    _, _, now = run_p(h, [moved], temp=40.0, start=now, armed_now=False)
    assert h['p_phase'] == 'seek'                    # one reading: not yet
    _, msgs, now = run_p(h, [moved], temp=40.0, start=now, armed_now=False)
    assert h['p_phase'] == 'hold'
    # frozen below the TC by the body's lag at 1.5 °C/min: 1.5 × 170 / 60 = 4.25 K
    assert h['setpoint_C'] == pytest.approx(40.0 - 4.25)
    assert any("moved" in m and "creep stopped" in m for m in msgs)
    # no creep while P_vacuum is still rising, nor for 30 s after
    sp = h['setpoint_C']
    run_p(h, [moved * 1.05 ** (k / 40) for k in range(100)], temp=40.0, start=now,
          armed_now=False)
    assert h['setpoint_C'] == sp and h['p_phase'] == 'hold'


def test_above_the_cut_line_the_heater_is_off_at_once(h):
    _, now = creeping(h)
    duties, msgs, _ = run_p(h, [1.01 * CUT], start=now, armed_now=False)
    assert duties == [0.0] and h['p_phase'] == 'cut'
    assert any("heater OFF" in m and "6.43e-07" in m for m in msgs)


def test_a_one_reading_spike_changes_nothing(h):
    # 5 Oct 2026, 16:21: single readings 2.2 → 3.4e-7 and straight back
    _, now = creeping(h)
    sp = h['setpoint_C']
    duties, msgs, _ = run_p(h, [3.4e-7, BASE, BASE], start=now, armed_now=False)
    assert h['p_phase'] == 'seek' and 0.0 not in duties and msgs == []
    assert h['setpoint_C'] > sp                     # still creeping


@pytest.mark.parametrize("rate, freeze", [(1.5, 4.25), (0.3, 1.0)])
def test_the_freeze_grows_with_the_creep_rate(rate, freeze):
    assert controller._freeze_k(rate) == pytest.approx(freeze)


def test_a_fast_rise_cuts_before_it_reaches_the_line(h):
    _, now = creeping(h)
    rise = [BASE * 1.3 ** k for k in range(1, 8)]
    duties, msgs, _ = run_p(h, rise, start=now, armed_now=False)
    first_off = duties.index(0.0)
    assert rise[first_off] < CUT
    assert any("predicted" in m for m in msgs)


def test_after_a_cut_it_holds_the_tc_as_soon_as_it_is_back_below(h):
    _, now = creeping(h, temp=45.0)
    _, _, now = run_p(h, [1.1 * CUT] * 8, temp=45.0, start=now, armed_now=False)
    duties, _, now = run_p(h, [0.97 * CUT] * 4, temp=44.0, start=now, armed_now=False)
    assert duties == [0.0] * 4                  # still above 0.95 × the cut line
    duties, msgs, now = run_p(h, [0.9 * CUT], temp=43.0, start=now, armed_now=False)
    assert h['p_phase'] == 'hold' and h['setpoint_C'] == pytest.approx(43.0)
    assert duties[0] > 0.0 and any("holding 43.0 °C" in m for m in msgs)


def test_above_p_vacuum_max_it_stops_until_restarted(h):
    _, now = creeping(h)
    duties, msgs, now = run_p(h, [1.2e-6], start=now, armed_now=False)
    assert h['p_phase'] == 'stopped' and duties == [0.0]
    assert any("above P_vacuum_max" in m for m in msgs)
    duties, _, now = run_p(h, [BASE] * 240, start=now, armed_now=False)     # 60 s
    assert h['p_phase'] == 'stopped' and set(duties) == {0.0}
    run_p(h, [BASE], start=now)                                            # re-armed
    assert h['p_phase'] == 'baseline'


def test_above_the_aim_the_setpoint_eases_down_to_3_K_below_the_tc(h):
    _, now = creeping(h, temp=40.0)
    _, _, now = run_p(h, [1.1 * CUT] * 2, temp=40.0, start=now, armed_now=False)
    assert h['p_phase'] == 'cut'
    # back under the cut line but above the aim: hold at the TC, then trim
    _, msgs, now = run_p(h, [1.1 * TARGET] * 4 * 60, temp=40.0, start=now,
                         armed_now=False)
    assert h['p_phase'] == 'trim' and any("above the aim" in m for m in msgs)
    assert h['setpoint_C'] == pytest.approx(40.0 - 0.375 * (60 - 0.5) / 60, abs=0.01)
    run_p(h, [1.1 * TARGET] * 4 * 600, temp=40.0, start=now, armed_now=False)
    assert h['setpoint_C'] == pytest.approx(40.0 - config.PRESSURE_TRIM_BELOW_K)


def test_above_the_aim_the_setpoint_never_rises(h):
    _, now = creeping(h, temp=40.0)
    _, _, now = run_p(h, [1.1 * CUT] * 2, temp=40.0, start=now, armed_now=False)
    _, _, now = run_p(h, [1.1 * TARGET] * 20, temp=40.0, start=now, armed_now=False)
    assert h['p_phase'] == 'trim'
    h['setpoint_C'] = 35.0                           # e.g. frozen below the 3 K floor
    run_p(h, [1.1 * TARGET] * 400, temp=40.0, start=now, armed_now=False)
    assert h['setpoint_C'] <= 35.0


def test_well_below_the_aim_it_creeps_at_the_full_rate_once_steady(h):
    _, now = creeping(h, temp=50.0)
    _, msgs, _ = run_p(h, [BASE * 1.3] * 4 * 35, temp=50.0, start=now, armed_now=False)
    assert h['p_phase'] == 'approach' and h['p_rate_c_min'] == pytest.approx(1.5)


def test_close_below_the_aim_it_creeps_at_a_quarter(h):
    _, now = creeping(h, temp=50.0)
    _, msgs, _ = run_p(h, [0.9 * TARGET] * 4 * 35, temp=50.0, start=now, armed_now=False)
    assert h['p_phase'] == 'approach' and h['p_rate_c_min'] == pytest.approx(0.375)
    assert any("steady" in m and "creeping 0.38 °C/min" in m for m in msgs)


def test_back_at_baseline_it_creeps_at_the_full_rate_again(h):
    _, now = creeping(h)
    _, _, now = run_p(h, [BASE * 1.3] * 8, start=now, armed_now=False)
    _, msgs, _ = run_p(h, [BASE, BASE], start=now, armed_now=False)
    assert h['p_phase'] == 'seek' and h['p_rate_c_min'] == pytest.approx(1.5)
    assert any("back at baseline" in m for m in msgs)


def test_a_high_baseline_raises_the_aim_so_that_flow_shows(h):
    msgs, _ = creeping(h, base=4.8e-7)
    assert 10 ** h['p_aim'] > TARGET
    assert any("raised from 5.0e-07 so that flow shows" in m for m in msgs)


def test_a_baseline_just_under_the_cut_line_parks(h):
    _, _, _ = run_p(h, [0.98 * CUT] * 80)       # baseline + margin is past the cut line
    assert h['p_phase'] == 'park' and h['setpoint_C'] <= 40.0


def test_a_baseline_above_the_cut_line_keeps_the_heater_off_and_says_why(h):
    duties, msgs, _ = run_p(h, [8e-7] * 80)
    assert h['p_phase'] == 'cut' and duties[-1] == 0.0
    assert any("no room for gas flow below P_vacuum_max" in m for m in msgs)


def test_started_with_the_valve_open_it_cuts_then_creeps_once_it_shuts(h):
    # armed with flow already over the cut line: the heater goes off, and the
    # valve shuts as it cools
    _, _, now = run_p(h, [8e-7] * 60)
    assert h['p_phase'] == 'cut'
    run_p(h, [BASE] * 4 * 40, start=now, armed_now=False)
    assert h['p_phase'] == 'seek' and h['p_base'] == pytest.approx(math.log10(BASE))


def test_the_cut_applies_while_the_baseline_is_still_being_measured(h):
    duties, _, _ = run_p(h, [8e-7] * 4)
    assert h['p_base'] is None and duties[1:] == [0.0] * 3


def test_a_noisy_gauge_widens_the_margin(h):
    noisy = [BASE * 10 ** (0.02 * (-1) ** k) for k in range(80)]      # ±0.02 decades
    run_p(h, noisy)
    assert h['p_margin'] > 4 * 0.015


def test_the_seat_screw_torque_is_not_an_input():
    import inspect
    assert 'seat_nm' not in inspect.signature(controller.step).parameters


def test_hold_power_matches_the_measured_holds():
    # 21 Sept 2026 steady holds (W): the fitted curve is within ±15 % of each
    for t_c, watts in [(40.2, 0.507), (55.1, 0.916), (65.4, 1.399), (125.0, 3.012),
                       (140.1, 3.793), (145.5, 4.039)]:
        assert controller.hold_duty(t_c) * config.heater_power_w() == \
            pytest.approx(watts, rel=0.15)
    assert controller.hold_duty(config.HEATER_HOLD_AMBIENT_C - 5) == 0.0
