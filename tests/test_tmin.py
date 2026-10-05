"""t-min-tune: the lowest opening temperature, step by step (history 36).

Agreed behaviour (30 Sept 2026):
  * the estimate: this seating's results, else (1 Oct, history 42) the
    operator's estimate typed at the start, else the other seatings at this
    torque (or the opening map); with nothing at all, one scout ramp
  * start 10 K below it (no result at this seating), 5 K (one), then
    (1 Oct, history 38) 2 K below the lowest of the latest 3, within 3-10 K
    of the estimate; hold until the chamber is settled
  * step +1 K every 5 min (once the TC is within 0.5 K); the step where it
    opens is T_min
  * the upstream outside target ± band cuts the heater and abandons the
    test; the next starts afresh inside the band
  * after an opening: closed = chamber within +0.02 dec of its baseline and
    settled; T_close logged; 30 min cap
  * stop when the last 3 results agree within ±1 K, or stopped
  * every test is a row of logs/t-min.csv, the central record, only appended to;
    the row that converged carries the converged T_min (history 38), filled
    in for rows written before
"""
import csv
import math
import json
from pathlib import Path

import pytest

from batch_sim import Rig
from driver import config, controller, control, schema, shared, tmin, tminlog, tminrun

# A valve that snaps open at 120 °C at 3 bar (−12 K/bar), closes 15 K below.
VALVE = dict(open_c=120.0, k_up=-12.0, hysteresis=15.0, a=0.6, efold=2.0, seed=3)


def run(max_s=8 * 3600, **kw):
    rig = Rig(**{**VALVE, **kw.pop('rig', {})})
    s, rows, msgs = rig.run_tmin(max_s=max_s, **kw)
    return rig, s, rows, msgs


def results(rows):
    return [r for r in rows if r['outcome'] == tminlog.T_MIN and r['counted'] == '1']


def test_a_session_scouts_then_converges_on_the_opening_point():
    rig, s, rows, msgs = run()
    assert s['state'] == tmin.CONVERGED and not rig.h['armed']
    assert rows[0]['outcome'] == tminlog.SCOUT and rows[0]['counted'] == '0'
    got = results(rows)
    assert len(got) == 3
    assert all(119.0 <= float(r['t_min_degC']) <= 121.5 for r in got)
    # the margin narrows: 10 K (no result), 5 K (one), then 2 K below the lowest, ≥ 3 K
    assert [float(r['margin_K']) for r in got] == [10.0, 5.0, pytest.approx(3.0, abs=0.01)]
    for r in got:
        # started below it, opened on a step, closed well below
        assert float(r['start_degC']) < float(r['t_min_degC']) <= float(r['step_degC']) + 0.5
        assert float(r['t_close_degC']) < float(r['t_min_degC']) - 5.0
        assert r['in_band'] == '1'
    assert "within ±1 K" in s["note"]


def test_the_steps_are_1_K_and_5_min_and_dont_overshoot():
    seen = []

    def hook(rig, s):
        if s['phase'] == tmin.STEP:
            seen.append((rig.now, s['test']['sp'], rig.T))
    run(hook=hook, stop_after=1)
    sps = sorted({sp for _, sp, _ in seen})
    assert all(b - a == pytest.approx(1.0) for a, b in zip(sps, sps[1:]))
    for sp in sps[:-1]:
        at = [(t, T) for t, x, T in seen if x == sp]
        assert at[-1][0] - at[0][0] >= config.TMIN_DWELL_S - 1.0
        assert max(T for _, T in at) - sp < 0.5


def test_the_event_log_says_what_settled_and_names_every_step():
    """History 47: the hold is the valve's temperature, what settles is the
    chamber pressure, and each step is logged, so the last line is current."""
    rig, s, rows, msgs = run(stop_after=2)
    msgs = [m if isinstance(m, str) else m[-1] for m in msgs]
    first = [m for m in msgs if "step increase from" in m]
    assert first and "valve held at" in first[0] and "chamber pressure settled (" in first[0]
    assert "+1 K every 5 min until the valve opens" in first[0]
    later = [m for m in msgs if "no opening at" in m and "next step" in m]
    assert later, "every step after the first is logged"
    assert not any("stepping:" in m for m in msgs)


def test_other_seatings_at_the_torque_give_the_first_estimate():
    old = [dict(time="t", seating="old", torque_Nm="0.45", outcome=tminlog.T_MIN,
                counted="1", t_min_degC=str(T), upstream_at_open_bar="3.0")
           for T in (121.0, 122.0)]
    rig, s, rows, msgs = run(rows=list(old), stop_after=1)
    new = rows[len(old):]
    assert new[0]['outcome'] == tminlog.T_MIN          # no scout
    assert float(new[0]['estimate_degC']) == pytest.approx(121.5)
    assert float(new[0]['start_degC']) == 111.0 and "other seating" in new[0]['estimate_from']


def test_other_seatings_count_with_their_latest_results_only():
    # 1 Oct, 0.50 N·m: the first tests walked down from a high start (upper
    # bounds); a new seating starts from that seating's last 3, not all six
    rows = [dict(time=f"t{i}", seating="old", torque_Nm="0.5", outcome=tminlog.T_MIN,
                 counted="1", t_min_degC=str(T), upstream_at_open_bar="0.952")
            for i, T in enumerate((134.97, 130.26, 125.16, 122.16, 120.08, 120.20))]
    est = tminlog.estimate(rows, "new", 0.5, 0.952)
    assert est['T'] == pytest.approx((122.16 + 120.08 + 120.20) / 3) and est['n'] == 0
    assert math.floor(est['T'] - tminlog.margin(est)) == 110


def test_the_operators_estimate_comes_before_the_other_seatings():
    # history 42: a seating started from the value the operator expects (the
    # lock-nut seatings), with the usual 10 K margin; no scout
    old = [dict(time="t", seating="old", torque_Nm="0.45", outcome=tminlog.T_MIN,
                counted="1", t_min_degC=str(T), upstream_at_open_bar="3.0")
           for T in (121.0, 122.0)]
    rig, s, rows, msgs = run(rows=list(old), stop_after=1, operator=118.0)
    new = rows[len(old):]
    assert new[0]['outcome'] == tminlog.T_MIN
    assert float(new[0]['estimate_degC']) == 118.0 and float(new[0]['margin_K']) == 10.0
    assert float(new[0]['start_degC']) == 108.0
    assert new[0]['estimate_from'].startswith(tminlog.OPERATOR)
    # nothing measured at the torque: no scout either
    rig, s, rows, msgs = run(stop_after=1, operator=118.0)
    assert rows[0]['outcome'] == tminlog.T_MIN and float(rows[0]['start_degC']) == 108.0


def test_the_seatings_own_results_come_before_the_operators_estimate():
    own = [dict(time="t", seating="a", torque_Nm="0.4", outcome=tminlog.T_MIN, counted="1",
                t_min_degC="90.0", upstream_at_open_bar="0.95")]
    est = tminlog.estimate(own, "a", 0.4, 0.95, operator=100.0)
    assert est['T'] == 90.0 and "this seating" in est['how']
    scout = [dict(time="t", seating="a", torque_Nm="0.4", outcome=tminlog.SCOUT,
                  t_min_degC="92.0", upstream_at_open_bar="0.95")]
    assert tminlog.estimate(scout, "a", 0.4, 0.95, operator=100.0)['how'] == "this seating's scout"
    est = tminlog.estimate(own, "b", 0.4, 0.95, operator=100.0)
    assert est['T'] == 100.0 and est['n'] == 0 and tminlog.margin(est) == 10.0


def test_results_are_corrected_to_the_target_pressure():
    row = dict(t_min_degC="100.0", upstream_at_open_bar="3.2")
    assert tminlog.at_target(row, 3.0, -12.0) == pytest.approx(102.4)


def test_leaving_the_band_cuts_the_heater_and_restarts_the_test():
    state = dict(done=False, back_at=None)

    def hook(rig, s):
        if not state['done'] and s['phase'] == tmin.STEP and s['results'] == 1 \
                and s['test']['sp'] >= s['test']['start_c'] + 2:
            rig.upstream = 3.2                       # outside 3.0 ± 0.05 (and T_open 2.4 K lower)
            state['done'], state['back_at'] = True, rig.now + 600
        if state['back_at'] and rig.now >= state['back_at']:
            rig.upstream, state['back_at'] = 3.0, None
    rig, s, rows, msgs = run(hook=hook)
    ab = [r for r in rows if r['outcome'] == tminlog.ABORTED_BAND]
    assert len(ab) == 1 and ab[0]['counted'] == '0'
    i = rows.index(ab[0])
    assert rows[i + 1]['outcome'] == tminlog.T_MIN          # the next test, afresh
    assert float(rows[i + 1]['start_degC']) == float(ab[0]['start_degC'])
    assert any("abandoned" in m and "outside 3 ± 0.05 bar" in m for m in msgs)
    assert s['state'] == tmin.CONVERGED


def test_an_opening_out_of_band_is_logged_not_counted():
    state = dict(done=False)

    def hook(rig, s):
        # push the upstream up 1.5 bar near the top of a test: the opening point
        # falls 18 K below the TC, it opens out of band
        if not state['done'] and s['phase'] == tmin.STEP and s['results'] == 1 \
                and s['test']['sp'] >= 118.0:
            rig.upstream, state['done'] = 4.5, True
        elif state['done'] and s['phase'] == tmin.WAIT and not rig.is_open:
            rig.upstream = 3.0
    rig, s, rows, msgs = run(hook=hook)
    oob = [r for r in rows if r['outcome'] in (tminlog.OPENED_OUT_OF_BAND,)]
    assert len(oob) == 1 and oob[0]['counted'] == '0' and oob[0]['in_band'] == '0'
    assert s['state'] == tmin.CONVERGED


def test_opening_during_the_hold_starts_the_next_lower():
    state = dict(moved=False)

    def hook(rig, s):
        if not state['moved'] and s['results'] == 1 and s['phase'] == tmin.WAIT:
            rig.open_c, state['moved'] = 108.0, True     # it moved 12 K down
    rig, s, rows, msgs = run(hook=hook, max_s=10 * 3600)
    at = [r for r in rows if r['outcome'] == tminlog.OPENED_AT_START]
    assert at and at[0]['counted'] == '0'
    i = rows.index(at[0])
    assert float(rows[i + 1]['start_degC']) <= float(at[0]['start_degC']) - 5.0
    assert "too high" in at[0]['note']
    later = results(rows[i + 1:])
    assert later and all(107.0 <= float(r['t_min_degC']) <= 109.5 for r in later)


def test_a_scout_that_opens_at_the_start_starts_lower_next_time():
    # 1 Oct, 0.20 N·m: the torque table said 40 °C (+10 K for the pressure);
    # the valve opened at 26 °C while heating to the 40 °C start, and the
    # next scout held 40 °C again. Now it starts 10 K below where it opened.
    rig, s, rows, msgs = run(rig=dict(open_c=72.0), stop_after=1, max_s=6 * 3600)
    at = [i for i, r in enumerate(rows) if r['outcome'] == tminlog.OPENED_AT_START]
    assert at
    for i in at:                                      # each next start: below where it opened
        assert float(rows[i + 1]['start_degC']) <= float(rows[i]['t_min_degC']) - 9.0
    assert any(r['outcome'] == tminlog.SCOUT for r in rows)      # it gets there
    assert results(rows)


def test_a_scout_remembers_openings_at_the_start_from_earlier_sessions():
    # 1 Oct, 0.20 N·m: the next session scouted from 40 °C again although the
    # valve had opened at 24-26 °C in the one before
    rows = [dict(time="t", seating="a", torque_Nm="0.20", outcome=tminlog.OPENED_AT_START,
                 t_min_degC=str(T)) for T in (25.344, 24.38)]
    rows.append(dict(time="t", seating="b", torque_Nm="0.45", outcome=tminlog.OPENED_AT_START,
                     t_min_degC="10"))
    assert tminlog.opened_low(rows, 0.2) == pytest.approx(24.38)
    assert tminlog.opened_low(rows, 0.3) is None
    s = tmin.new_session(0.2, "c", 0.959, 0.05, 0.0, opened_low=24.38)
    assert s['opened_low'] == pytest.approx(24.38)


def test_near_room_temperature_it_starts_where_it_can():
    # T_min 33 °C: 10 K below is 23 °C, which the heater can't cool to
    old = [dict(time="t", seating="old", torque_Nm="0.45", outcome=tminlog.T_MIN,
                counted="1", t_min_degC="33.0", upstream_at_open_bar="3.0")]
    rig, s, rows, msgs = run(rows=list(old), rig=dict(open_c=33.0, ambient=26.0),
                             stop_after=1)
    first = rows[1]
    assert "won't cool further" in first['note'] and float(first['start_degC']) >= 26.0
    assert first['outcome'] == tminlog.T_MIN


def test_no_opening_by_the_ceiling_stops_it():
    old = [dict(time="t", seating="s1", torque_Nm="0.45", outcome=tminlog.T_MIN,
                counted="1", t_min_degC="150.0", upstream_at_open_bar="3.0")]
    rig, s, rows, msgs = run(rows=list(old), rig=dict(open_c=400.0), max_s=12 * 3600)
    assert s['state'] == tmin.STOPPED and rows[-1]['outcome'] == tminlog.NO_OPENING
    assert not rig.h['armed']


def test_steps_stop_15_K_above_the_estimate():
    # e.g. a valve already open at the start: nothing to detect
    old = [dict(time="t", seating="s1", torque_Nm="0.45", outcome=tminlog.T_MIN,
                counted="1", t_min_degC="100.0", upstream_at_open_bar="3.0")]
    rig, s, rows, msgs = run(rows=list(old), rig=dict(open_c=400.0))
    last = rows[-1]
    assert last['outcome'] == tminlog.NO_OPENING and float(last['step_degC']) <= 115.0
    assert "no opening by 115 °C" in last['note'] and "already open" in s['note']


def test_a_valve_that_does_not_close_stops_it():
    def hook(rig, s):
        if s['phase'] == tmin.COOL:                  # it keeps flowing, whatever the TC does
            rig.extra, rig.tau_extra = 2.0 * rig.base, 1e9
    rig, s, rows, msgs = run(hook=hook)
    assert s['state'] == tmin.STOPPED and "did not close" in s['note']


def test_disarming_aborts_it():
    def hook(rig, s):
        if s['phase'] == tmin.STEP:
            controller.command(rig.h, rig.now, armed=False)
    rig, s, rows, msgs = run(hook=hook)
    assert s['state'] == tmin.ABORTED and rows[-1]['outcome'] == tminlog.STOPPED


def test_a_long_test_is_not_cut_by_the_60_min_armed_limit():
    # 10 K below with no result, 5 min per step: over an hour armed
    old = [dict(time="t", seating="old", torque_Nm="0.45", outcome=tminlog.T_MIN,
                counted="1", t_min_degC="130.0", upstream_at_open_bar="3.0")]
    rig, s, rows, msgs = run(rows=list(old), stop_after=1)
    assert not any("maximum armed time" in m for m in msgs)
    assert rows[1]['outcome'] == tminlog.T_MIN


def test_renew_restarts_the_armed_clock_only():
    h = controller.new_state()
    controller.command(h, 100.0, armed=True, mode='auto-t', setpoint_C=50.0)
    h['integral'] = 3.0
    controller.command(h, 200.0, renew=True)
    assert h['armed_at'] == 200.0 and h['integral'] == 3.0 and h['armed']
    controller.command(h, 300.0, armed=False)
    controller.command(h, 400.0, renew=True)
    assert not h['armed'] and h['armed_at'] is None


# ── the central file ────────────────────────────────────────────────────────

def test_the_file_only_grows_and_gains_new_columns(tmp_path):
    p = tmp_path / "t-min.csv"
    old_cols = list(schema.TMIN)[:6]
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(old_cols)
        w.writerow(["2026-09-30T10:00:00", "s0", "0.45", "3.0", "0.05", "t_min"])
    ok, msg = tminlog.append(dict(time="2026-09-30T11:00:00", seating="s1", torque_Nm=0.45,
                                  outcome="t_min", t_min_degC=120.5), path=str(p), sheet=False)
    assert ok
    rows = tminlog.load(str(p))
    assert [r['seating'] for r in rows] == ["s0", "s1"]
    assert rows[0]['outcome'] == "t_min" and rows[0]['t_min_degC'] == ""
    assert rows[1]['t_min_degC'] == "120.5"
    header = next(csv.reader(open(p, encoding="utf-8")))
    assert header[:6] == old_cols and set(schema.TMIN) <= set(header)


def _est(values):
    last = values[-3:]
    return dict(T=sum(last) / len(last), n=len(values), sd=None if len(last) < 2 else 0.0,
                values=list(values))


def test_margin_and_convergence_rules():
    assert tminlog.margin(None) == 10.0
    assert tminlog.margin(dict(n=1, sd=None, values=[67.0])) == 5.0
    # from two results: 2 K below the lowest of the latest 3, 3-10 K below the estimate
    assert tminlog.margin(_est([67.0, 67.2, 67.1])) == pytest.approx(3.0)        # floor
    assert tminlog.margin(_est([75.0, 67.0, 67.0])) == pytest.approx(4.667, abs=1e-3)
    assert tminlog.margin(_est([90.0, 90.0, 60.0])) == pytest.approx(10.0)       # ceiling
    assert tminlog.margin(_est([60.0, 67.0, 67.0, 67.0])) == pytest.approx(3.0)  # latest 3 only
    assert not tminlog.converged([120.0, 121.0])
    assert tminlog.converged([125.0, 120.0, 121.0, 120.5])
    assert not tminlog.converged([120.0, 121.0, 122.5])
    assert tminlog.converged_value([125.0, 120.0, 121.0, 120.5]) == pytest.approx(120.5)
    assert tminlog.converged_value([120.0, 121.0, 122.5]) is None


# 1 Oct 2026, 0.30 N·m at 0.959 bar: (seating, outcome, counted, converged,
# t_min_degC, t_min_at_target_degC, upstream_at_open_bar)
OCT1 = [("a", "scout", "0", "0", 90.953, 90.955, 0.9592),
        ("a", "t_min", "1", "0", 70.969, 70.97, 0.9591),
        ("a", "t_min", "1", "0", 66.984, 66.984, 0.959),
        ("a", "t_min", "1", "0", 67.062, 67.064, 0.9591),
        ("a", "t_min", "1", "1", 67.93, 67.935, 0.9594),
        ("b", "scout", "0", "0", 107.742, 107.858, 0.9596)]


def _oct1_rows():
    return [dict(time=f"2026-10-01T09:{i:02d}", seating=se, torque_Nm="0.3" if se == "a" else "0.4",
                 upstream_target_bar="0.959" if se == "a" else "0.95", band_bar="0.05",
                 outcome=o, counted=c, converged=v, t_min_degC=str(t), t_min_at_target_degC=str(tt),
                 upstream_at_open_bar=str(p))
            for i, (se, o, c, v, t, tt, p) in enumerate(OCT1)]


def test_the_start_is_2_K_below_the_lowest_recent_result():
    # 1 Oct: the estimate before test007 was 68.3 °C with 2.3 K scatter; the old
    # rule started at 62 °C, 2 K below the lowest (66.98) is 64 °C
    rows = _oct1_rows()[:4]
    est = tminlog.estimate(rows, "a", 0.3, 0.959)
    assert est['T'] == pytest.approx(68.339, abs=1e-3)
    assert math.floor(est['T'] - tminlog.margin(est)) == 64


def test_the_start_can_be_capped_at_the_last_T_close(monkeypatch):
    # off by default; on, no test starts above the previous T_close + K
    # (the valve closes 15 K below where it opened, so the cap bites)
    assert config.TMIN_START_ABOVE_CLOSE_K is None
    monkeypatch.setattr(tmin, "TMIN_START_ABOVE_CLOSE_K", 3.0)
    rig, s, rows, msgs = run(stop_after=3)
    done = [r for r in rows if r['t_close_degC']]
    assert len(done) >= 3
    for prev, r in zip(done, done[1:]):
        assert float(r['start_degC']) <= float(prev['t_close_degC']) + 3.0
    assert any("capped at the last T_close" in m for m in msgs)


def test_the_converged_T_min_is_filled_in_for_rows_written_before(tmp_path):
    p = tmp_path / "t-min.csv"
    cols = list(schema.TMIN)[:schema.TMIN.index('t_min_converged_degC')]  # before history 38
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in _oct1_rows():
            w.writerow([r.get(c, '') for c in cols])
    before = tminlog.load(str(p))
    filled, msg = tminlog.upgrade(str(p))
    assert filled == 1 and "1 row" in msg
    rows = tminlog.load(str(p))
    assert [r['t_min_converged_degC'] for r in rows] == ['', '', '', '', '67.328', '']
    for a, b in zip(before, rows):                    # nothing else changed
        assert all(a[c] == b[c] for c in cols)
    assert tminlog.upgrade(str(p)) == (0, "")         # once only
    # the next row appended keeps it
    tminlog.append(dict(time="t", seating="b", outcome="t_min"), path=str(p), sheet=False)
    assert tminlog.load(str(p))[4]['t_min_converged_degC'] == '67.328'
    assert not tminlog.converged([120.0, 121.0])
    assert tminlog.converged([125.0, 120.0, 121.0, 120.5])
    assert not tminlog.converged([120.0, 121.0, 122.5])


# ── tminrun: the thread's work, the files ──────────────────────────────────

def drive(clock, rig, max_s=4 * 3600):
    from driver import control as ctl
    dt, next_row = 0.25, clock.t
    rig.now = clock.t
    t0 = clock.t
    while tminrun.running() and clock.t - t0 < max_s:
        vac, status = rig.vac()
        shared.store_valve_temp(rig.T, 0)
        shared.store_vacuum(vac, status, None)
        shared.store_keller(rig.upstream, None, clock.t)
        tminrun.tick()
        rig.duty = ctl.compute_duty(rig.T, True, dt, vac=vac, vac_status=status)
        rig.advance(dt)
        clock.advance(dt)
        if clock.t >= next_row:
            name, phase = tminrun.labels()
            row = {c: '' for c in schema.MAIN}
            row.update(timestamp=__import__('datetime').datetime.fromtimestamp(clock.t)
                       .isoformat(timespec='milliseconds'),
                       te_temperature_degC=round(rig.T, 3), vacuum_chamber_mbar=vac,
                       keller_pressure_bar=rig.upstream, batch_run=name, batch_phase=phase)
            tminrun.record_row(row)
            next_row += config.LOG_INTERVAL_S


def ready(clock):
    shared.set_seat_screw_torque(0.45)
    shared.store_valve_temp(25.0, 0)
    shared.store_vacuum(4e-7, None, 1.7)
    shared.store_keller(3.0, None, clock.t)


def test_tminrun_writes_the_central_file_and_the_traces(clock):
    ready(clock)
    ok, name = tminrun.start(target=3.0, band=0.05, start_thread=False)
    assert ok and name.endswith("_0.45Nm")
    drive(clock, Rig(**VALVE), max_s=8 * 3600)
    tminrun.stop("test")
    rows = tminlog.load()
    # the row that converged carries the converged T_min: the mean of the last 3
    conv = [i for i, r in enumerate(rows) if r['converged'] == '1']
    assert len(conv) == 1 and conv[0] == len(rows) - 1
    last3 = [float(r['t_min_at_target_degC']) for r in rows if r['counted'] == '1'][-3:]
    assert float(rows[-1]['t_min_converged_degC']) == pytest.approx(sum(last3) / 3, abs=1e-3)
    assert all(r['t_min_converged_degC'] == '' for r in rows[:-1])
    assert rows and rows[0]['outcome'] == tminlog.SCOUT
    assert any(r['outcome'] == tminlog.T_MIN and r['counted'] == '1' for r in rows)
    assert {r['seating'] for r in rows} == {name}
    trace = Path(config.TMIN_CSV).parent / rows[1]['trace']
    lines = list(csv.DictReader(open(trace, encoding="utf-8")))
    assert {'hold', 'step', 'cool'} <= {r['batch_phase'] for r in lines}
    info = json.load(open(Path(tminrun.folder()) / "session.json", encoding="utf-8"))
    assert info['upstream_target_bar'] == 3.0 and info['band_bar'] == 0.05
    events = " | ".join(t for _, t in shared.recent_events())
    assert "file error" not in events and "T_min estimate at 3 bar" in events
    # a later run at this torque continues the seating from the file
    assert tminrun.last_seating(0.45) == name
    ok, again = tminrun.start(retorqued=False, target=3.0, band=0.05, start_thread=False)
    assert ok and again == name
    assert "this seating" in tminrun._estimate(3.0)['how']


def test_start_needs_the_torque_the_target_and_a_disarmed_heater(clock):
    ok, msg = tminrun.start(target=3.0, start_thread=False)
    assert not ok and "torque" in msg
    ready(clock)
    ok, msg = tminrun.start(target=None, start_thread=False)
    assert not ok and "upstream target" in msg
    control.heater_command(armed=True)
    ok, msg = tminrun.start(target=3.0, start_thread=False)
    assert not ok and "disarm" in msg


def test_tminrun_starts_a_new_seating_from_the_operators_estimate(clock):
    ready(clock)
    ok, msg = tminrun.start(target=3.0, band=0.05, estimate_c=200.0, start_thread=False)
    assert not ok and "out of range" in msg
    ok, name = tminrun.start(target=3.0, band=0.05, estimate_c=98.5, start_thread=False)
    assert ok
    est = tminrun._estimate(3.0)
    assert est['T'] == 98.5 and est['how'].startswith(tminlog.OPERATOR)
    info = json.load(open(Path(tminrun.folder()) / "session.json", encoding="utf-8"))
    assert info['operator_estimate_degC'] == 98.5
    assert "estimate 98.5 °C (the operator's estimate" in shared.recent_events()[-1][1]
    tminrun.stop("test")
    # a seating with a result of its own: the estimate is noted and not used
    tminlog.append(dict(time="2026-10-01T09:00:00", seating=name, torque_Nm=0.45,
                        upstream_target_bar=3.0, outcome="t_min", t_min_degC=101.0,
                        upstream_at_open_bar=3.0, counted=1), sheet=False)
    ok, again = tminrun.start(retorqued=False, target=3.0, band=0.05, estimate_c=98.5,
                              start_thread=False)
    assert ok and again == name and tminrun._estimate(3.0)['T'] == 101.0
    assert any("98.5 °C is not used" in t for _, t in shared.recent_events())


def test_the_band_can_change_while_it_runs(clock):
    ready(clock)
    ok, _ = tminrun.start(target=3.0, band=0.05, start_thread=False)
    assert tminrun.set_band(2.0, 0.1)
    assert tminrun._s['target'] == 2.0 and tminrun._s['band'] == 0.1
    assert not tminrun.set_band(None, 0.1)


@pytest.mark.parametrize("runner", ["tminrun", "cyclerun"])
def test_a_new_seating_in_the_same_second_gets_its_own_name(clock, monkeypatch, runner):
    # seating names are to the second; a re-torque started within the same
    # second as the last seating (fast test machines) must not reuse its name
    import datetime as dt
    from driver import cyclerun
    mod = {"tminrun": tminrun, "cyclerun": cyclerun}[runner]
    fixed = dt.datetime(2026, 10, 1, 8, 14, 48)

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    monkeypatch.setattr(mod, "datetime", Frozen)
    ready(clock)
    kw = dict(target=3.0, band=0.05) if runner == "tminrun" else {}
    ok, first = mod.start(start_thread=False, **kw)
    mod.stop("test")
    if runner == "tminrun":
        tminlog.append(dict(time="2026-10-01T08:14:48", seating=first, torque_Nm=0.45,
                            outcome="stopped"), sheet=False)
    else:
        from driver import openmap
        openmap.append(dict(time="2026-10-01T08:14:48", seating=first, torque_Nm=0.45,
                            upstream_bar=3.0, t_open_degC=128.0, deep=1), sheet=False)
    ok, second = mod.start(retorqued=True, start_thread=False, **kw)
    assert ok and second != first and second.endswith("_0.45Nm")
    assert second == "20261001_081449_0.45Nm"
