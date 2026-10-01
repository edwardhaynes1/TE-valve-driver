"""t-min-tune: the lowest temperature at which the valve opens, at an
upstream pressure the operator holds (history 36) — the logic, with no
threads, clock, files or hardware (tminrun.py adds those).

    new_session(torque, seating, target, band, now, history=())
    set_band(s, target, band)            the operator changed the target or band
    step(s, now, temp, vac, vac_status, heater, p_up, p_up_t, estimate)
        -> (commands, msgs, events)      one step, ~4 Hz. estimate(target) ->
                                         estimate's dict or None, asked
                                         when a test is planned.
                                         events: ('test_start', test),
                                         ('test_end', test), ('end', s)
    stop(s, now, reason) -> (commands, msgs, events)
    labels(s) -> (test name, phase)      for the log rows
    status_text(s) -> str                one line for the window
    in_band(s, p_up) -> bool

A test: hold (auto-t at the start temperature — the estimate minus the
margin, or from two results 2 K below the lowest of the latest — until the
chamber is settled) → step (+TMIN_STEP_K, each held
TMIN_DWELL_S once the TC is within TMIN_STEP_BAND_K of it) until the valve
opens: that step is T_min → cool (heater off) until closed: the chamber
within TMIN_CLOSED_DEC of its baseline before the opening, and settled; the
TC then is T_close → the next test. With no estimate at all the first test
scouts: a 3 °C/min ramp from 10 K below the torque table's guess.

The upstream outside target ± band (or not read) during the hold or a step
cuts the heater and abandons the test; the next starts afresh once it is
back inside. Opening during the hold means the start was too high: the next
test starts TMIN_OPENED_AT_START_K lower. The session stops when the last
TMIN_CONVERGE_N counted results agree within ±TMIN_CONVERGE_K, when stopped,
or on a fault (heater disarmed or tripped, chamber pressure, no opening by
BATCH_CEILING_C, the valve not closing).
"""

import math
import statistics
from collections import deque

from .batch import chamber_trend, find_onset, median3, open_threshold_dec, settled
from .config import (
    BATCH_CEILING_C, BATCH_CREEP_C_MIN, BATCH_SETTLE_MAX_S, BATCH_SETTLE_WINDOW_S,
    BATCH_UP_MAX_AGE_S, LABJACK_SAMPLE_HZ, PRESSURE_BAD_READS_TO_TRIP,
    PRESSURE_BASE_GUARD_S, PRESSURE_BASE_MIN_S, PRESSURE_BASE_WINDOW_S, PRESSURE_FILTER_S,
    PRESSURE_TRIP_MBAR, PRESSURE_UP_K_PER_BAR, PRESSURE_UP_MAX_DOWN_K,
    PRESSURE_UP_MAX_SHIFT_K, TMIN_CLOSE_MAX_S, TMIN_CLOSED_DEC, TMIN_CONVERGE_K,
    TMIN_ABOVE_EST_K, TMIN_CONVERGE_N, TMIN_DWELL_S, TMIN_ESTIMATE_LAST_N, TMIN_MARGIN_MAX_K,
    TMIN_MARGIN_MIN_K, TMIN_MARGIN_NEW_K, TMIN_MARGIN_ONE_K, TMIN_OPENED_AT_START_K,
    TMIN_START_ABOVE_CLOSE_K, TMIN_START_BELOW_LOWEST_K, TMIN_STEP_BAND_K, TMIN_STEP_K,
    VAC_HIGH_STATES,
)
from .controller import AUTO_T, opening_point

RUNNING, STOPPED, CONVERGED, ABORTED = 'running', 'stopped', 'converged', 'aborted'
# a test's outcome (t-min.csv's outcome column)
T_MIN = 't_min'
OPENED_AT_START = 'opened at start'
ABORTED_BAND = 'aborted: out of band'
OPENED_OUT_OF_BAND = 'opened out of band'
NO_OPENING = 'no opening'
SCOUT_RESULT = 'scout'
STOPPED_TEST = 'stopped'
WAIT, HOLD, STEP, SCOUT, COOL = 'wait', 'hold', 'step', 'scout', 'cool'
HEATING = (HOLD, STEP, SCOUT)
SCOUT_BELOW_K = 10.0
_ROOM_SLOW_K_MIN = 0.2          # a hold above its start that cools slower than this…
_ROOM_SLOW_S = 120.0            # …for this long starts where it is (it can't go lower)


def margin(est):
    """K below the estimate to start: TMIN_MARGIN_NEW_K without a result at
    this seating, TMIN_MARGIN_ONE_K with one; from two (history 38),
    TMIN_START_BELOW_LOWEST_K below the lowest of the latest
    TMIN_ESTIMATE_LAST_N, kept within TMIN_MARGIN_MIN_K … _MAX_K of the
    estimate — so one high result doesn't push the start down."""
    if est is None or est['n'] == 0:
        return TMIN_MARGIN_NEW_K
    if est['n'] == 1 or not est.get('values'):
        return TMIN_MARGIN_ONE_K
    lowest = min(est['values'][-TMIN_ESTIMATE_LAST_N:])
    below = est['T'] - (lowest - TMIN_START_BELOW_LOWEST_K)
    return min(TMIN_MARGIN_MAX_K, max(TMIN_MARGIN_MIN_K, below))


def converged_value(values):
    """The converged T_min: the mean of the last TMIN_CONVERGE_N values, or
    None when they haven't converged."""
    if not converged(values):
        return None
    return statistics.mean(values[-TMIN_CONVERGE_N:])


def converged(values):
    """The last TMIN_CONVERGE_N values all within ± TMIN_CONVERGE_K of their mean."""
    if len(values) < TMIN_CONVERGE_N:
        return False
    last = values[-TMIN_CONVERGE_N:]
    m = statistics.mean(last)
    return all(abs(v - m) <= TMIN_CONVERGE_K + 1e-9 for v in last)


def test_name(n):
    return f"test{n:03d}"


def new_session(torque, seating, target, band, now, history=(), opened_low=None,
                operator_est=None, lock_nut=None):
    """history: [(time, mbar)] recent chamber readings. opened_low: the
    lowest temperature an earlier test at this torque opened at during its
    hold (t-min.csv), so a scout starts below it (history 41). operator_est:
    the T_min (°C) the operator expects, used until this seating has a
    result of its own (history 42). lock_nut: its torque, N·m, as entered
    (recorded only; history 43)."""
    if torque is None:
        raise ValueError("t-min-tune needs the seat screw torque")
    s = dict(torque=torque, seating=seating, target=target, band=band, started=now,
             ended=None, state=RUNNING, note="", n=0, test=None, phase=WAIT, phase_t0=now,
             hist=deque(maxlen=int((max(PRESSURE_BASE_WINDOW_S, BATCH_SETTLE_WINDOW_S) + 10)
                                   * LABJACK_SAMPLE_HZ * 2)),
             y_filt=None, last_t=None, base=None, bad_vac=0, up=None, trend=None,
             extra=0.0, results=0, est=None, waiting_note=False, cool_since=None, last_close=None,
             opened_low=opened_low, operator_est=operator_est, lock_nut=lock_nut,
             room=[])
    for t, mbar in history:
        if mbar and mbar > 0 and t <= now:
            s['hist'].append((t, math.log10(mbar)))
    if s['hist']:
        s['y_filt'] = s['hist'][-1][1]
    return s


def set_band(s, target, band):
    s['target'], s['band'] = target, band


def in_band(s, p_up):
    return (p_up is not None and s['target'] is not None and s['band'] is not None
            and abs(p_up - s['target']) <= s['band'] + 1e-9)


def labels(s):
    if s is None or s['state'] != RUNNING or s['test'] is None:
        return '', ''
    return s['test']['name'], s['phase']


def _enter(s, phase, now):
    s['phase'], s['phase_t0'] = phase, now


def _new_test(s, now):
    s['n'] += 1
    return dict(n=s['n'], name=test_name(s['n']), t_start=now, scout=False, start_c=None,
                sp=None, est=None, how="", margin=None, outcome=None, samples=[], base=None,
                hold_since=None, step_since=None, t_onset=None, T_onset=None, t_detect=None,
                T_detect=None, up_open=None, in_band=None, closed_T=None, closed_t=None,
                note="", t_end=None, done=False)


# ── one step ────────────────────────────────────────────────────────────────

def step(s, now, temp, vac, vac_status, heater, p_up=None, p_up_t=None, estimate=None):
    cmds, msgs, events = [], [], []
    if s['state'] != RUNNING:
        return cmds, msgs, events
    fresh = p_up is not None and (p_up_t is None or now - p_up_t <= BATCH_UP_MAX_AGE_S)
    s['up'] = p_up if fresh else None
    y = math.log10(vac) if (vac is not None and vac > 0) else None
    dt = 0.0 if s['last_t'] is None else max(0.0, now - s['last_t'])
    s['last_t'] = now
    if y is not None:
        s['y_filt'] = y if s['y_filt'] is None else (
            s['y_filt'] + dt / (PRESSURE_FILTER_S + dt) * (y - s['y_filt']))
        s['hist'].append((now, y))
    test = s['test']
    _update_baseline(s, now)
    if test is not None and not test['done']:
        test['samples'].append((now, temp, y, s['base']))
    s['trend'] = chamber_trend(s['hist'], now)
    phase = s['phase']
    opened = (s['base'] is not None and s['y_filt'] is not None and y is not None
              and s['y_filt'] - s['base'] > open_threshold_dec(s['base']))

    if phase in HEATING:
        if not heater['armed']:
            why = heater.get('trip_reason') or ("heater disarmed (by the operator, "
                                                "or the LabJack reconnecting)")
            return _abort(s, now, why, cmds, msgs, events)
        if vac_status in VAC_HIGH_STATES or (vac is not None and vac > PRESSURE_TRIP_MBAR):
            return _abort(s, now, f"chamber pressure too high "
                          f"({vac_status or f'{vac:.1e} mbar'})", cmds, msgs, events)
        s['bad_vac'] = 0 if y is not None else s['bad_vac'] + 1
        if s['bad_vac'] >= PRESSURE_BAD_READS_TO_TRIP:
            return _abort(s, now, f"no valid chamber pressure ({vac_status or 'no reading'})"
                          " — can't detect the opening", cmds, msgs, events)
        if opened:
            _detected(s, now, temp, cmds, msgs)
        elif not in_band(s, s['up']):
            _out_of_band(s, now, cmds, msgs)
        elif phase == HOLD:
            _hold(s, now, temp, cmds, msgs, events)
        elif phase == STEP:
            _step(s, now, temp, cmds, msgs, events)
        else:
            _scout(s, now, temp, cmds, msgs, events)
    elif phase == COOL:
        _cool(s, now, temp, msgs, events)
    else:
        _wait(s, now, temp, opened, estimate, cmds, msgs, events)
    return cmds, msgs, events


def _update_baseline(s, now):
    """The median log10 p of the last 5-30 s: fresh while waiting or holding,
    never rising while stepping, frozen once the valve has opened."""
    test = s['test']
    if s['phase'] == COOL or (test is not None and test['T_detect'] is not None
                              and not test['done']):
        return
    lo = now - PRESSURE_BASE_WINDOW_S
    pts = sorted(yy for t, yy in s['hist'] if lo <= t <= now - PRESSURE_BASE_GUARD_S)
    if len(pts) < PRESSURE_BASE_MIN_S * LABJACK_SAMPLE_HZ:
        return
    n = len(pts)
    med = pts[n // 2] if n % 2 else 0.5 * (pts[n // 2 - 1] + pts[n // 2])
    fresh = s['phase'] in (WAIT, HOLD) or s['base'] is None
    s['base'] = med if fresh else min(med, s['base'])


# ── between tests ───────────────────────────────────────────────────────────

def _wait(s, now, temp, opened, estimate, cmds, msgs, events):
    """Heater off. An unfinished test (abandoned for the band) is ended here;
    if the valve opens meanwhile, that test becomes 'opened out of band'.
    Converged → stop. The upstream in band → the next test."""
    test = s['test']
    if test is not None and not test['done']:
        if opened:
            test['outcome'] = OPENED_OUT_OF_BAND
            return _detected(s, now, temp, cmds, msgs, keep_outcome=True)
        if in_band(s, s['up']) or test['outcome'] != ABORTED_BAND:
            _end_test(s, now, events)
    est = estimate(s['target']) if estimate is not None else None
    s['est'] = est
    if est is not None and converged(est['values']):
        return _finish(s, now, CONVERGED,
                       f"T_min = {est['T']:.1f} °C at {s['target']:g} bar, the last "
                       f"{min(len(est['values']), 3)} results within ±1 K", msgs, events)
    if not in_band(s, s['up']):
        if not s['waiting_note']:
            s['waiting_note'] = True
            up = s['up']
            msgs.append("t-min-tune: waiting for the upstream to be inside "
                        f"{s['target']:g} ± {s['band']:g} bar (now "
                        + (f"{up:.3f} bar" if up is not None else "not read") + ")")
        return
    s['waiting_note'] = False
    if temp is None or s['base'] is None:
        return
    _begin_test(s, now, temp, est, cmds, msgs, events)


def _table_guess(s):
    """(°C, how): auto-p's opening point for the torque, shifted to the target."""
    t, bar, _, how, _ = opening_point(s['torque'])
    shift = 0.0
    if s['target'] is not None and bar is not None:
        shift = max(-PRESSURE_UP_MAX_DOWN_K,
                    min(PRESSURE_UP_MAX_SHIFT_K, -PRESSURE_UP_K_PER_BAR * (s['target'] - bar)))
    return t + shift, f"the torque table's {t:.1f} °C" + (
        f", shifted {shift:+.1f} K to {s['target']:g} bar" if shift else "")


def _begin_test(s, now, temp, est, cmds, msgs, events):
    test = s['test'] = _new_test(s, now)
    if est is None:
        guess, how = _table_guess(s)
        # a scout that opened during its hold (history 40): the next one
        # starts lower — by the extra, and 10 K below where it opened
        start = guess - SCOUT_BELOW_K - s['extra']
        if s['opened_low'] is not None:
            start = min(start, s['opened_low'] - SCOUT_BELOW_K)
        test.update(scout=True, est=guess, how=how, margin=round(guess - start, 2),
                    start_c=math.floor(min(BATCH_CEILING_C - 5.0, start)))
        why = (f"scouting (nothing measured at this torque): {SCOUT_BELOW_K:g} K below "
               f"{how}" + (f", lowered after opening at the start" if guess - start >
                           SCOUT_BELOW_K + 1e-9 else "")
               + f", then {BATCH_CREEP_C_MIN:g} °C/min until it opens")
    else:
        m = round(margin(est) + s['extra'], 2)
        start = math.floor(min(BATCH_CEILING_C - 5.0, est['T'] - m))
        cap = ""
        if TMIN_START_ABOVE_CLOSE_K is not None and s['last_close'] is not None:
            limit = math.floor(s['last_close'] + TMIN_START_ABOVE_CLOSE_K)
            if limit < start:
                start, m = limit, round(est['T'] - limit, 2)
                cap = (f", capped at the last T_close {s['last_close']:.1f} °C + "
                       f"{TMIN_START_ABOVE_CLOSE_K:g} K")
        test.update(est=est['T'], how=est['how'], margin=m, start_c=start)
        why = (f"{m:g} K below the estimate {est['T']:.1f} °C ({est['how']}){cap}, then "
               f"+{TMIN_STEP_K:g} K every {TMIN_DWELL_S / 60:g} min")
    test['sp'] = test['start_c']
    s.update(bad_vac=0, room=[])
    events.append(('test_start', test))
    cmds += [dict(mode=AUTO_T, setpoint_C=float(test['start_c'])), dict(armed=True)]
    msgs.append(f"t-min-tune: {test['name']} — upstream in band; holding "
                f"{test['start_c']:.1f} °C until the chamber is settled ({why})")
    _enter(s, HOLD, now)


# ── hold → steps (or the scout ramp) ────────────────────────────────────────

def _hold(s, now, temp, cmds, msgs, events):
    test = s['test']
    if temp is None:
        return
    if abs(temp - test['start_c']) <= TMIN_STEP_BAND_K and test['hold_since'] is None:
        test['hold_since'] = now
    if test['hold_since'] is None and temp > test['start_c']:
        # Above the start and not getting there: near room temperature the
        # heater can't cool it. Start where it is.
        s['room'].append((now, temp))
        then = [T for t, T in s['room'] if t <= now - _ROOM_SLOW_S]
        if then and then[-1] - temp < _ROOM_SLOW_K_MIN * _ROOM_SLOW_S / 60.0:
            new = float(math.ceil(temp))
            test['note'] = (f"started at {new:.0f} °C, not {test['start_c']:.0f} °C: "
                            f"it won't cool further")
            msgs.append(f"t-min-tune: {test['name']} — {test['note']}")
            test['start_c'] = test['sp'] = new
            cmds.append(dict(setpoint_C=new))
            s['room'] = []
    if (test['hold_since'] is not None and settled(s['trend'])
            and s['base'] is not None):
        cmds.append(dict(renew=True))
        if test['scout']:
            test['sp'] = test['start_c']
            test['step_since'] = now
            msgs.append(f"t-min-tune: {test['name']} — chamber settled at "
                        f"{test['start_c']:.1f} °C; scouting up at {BATCH_CREEP_C_MIN:g} °C/min")
            _enter(s, SCOUT, now)
        else:
            _next_step(s, now, cmds, msgs, first=True)
        return
    if now - s['phase_t0'] > BATCH_SETTLE_MAX_S:
        what = (f"the chamber not settled ({s['trend']:+.3f} dec/min)"
                if s['trend'] is not None else "no chamber trend") \
            if test['hold_since'] is not None else \
            f"the valve never reached {test['start_c']:.1f} °C"
        test['outcome'] = STOPPED_TEST
        test['note'] = f"{what} after {BATCH_SETTLE_MAX_S / 60:g} min of holding"
        cmds.append(dict(armed=False))
        _end_test(s, now, events)
        _finish(s, now, STOPPED, f"{test['name']}: {test['note']}", msgs, events)


def _next_step(s, now, cmds, msgs, first=False):
    test = s['test']
    sp = test['sp'] + TMIN_STEP_K
    if sp > BATCH_CEILING_C or (test['est'] is not None and sp > test['est'] + TMIN_ABOVE_EST_K):
        return None
    test['sp'], test['step_since'] = sp, None
    cmds += [dict(setpoint_C=float(sp)), dict(renew=True)]
    if first:
        msgs.append(f"t-min-tune: {test['name']} — chamber settled at {test['start_c']:.1f} °C;"
                    f" stepping: {sp:.1f} °C")
    _enter(s, STEP, now)
    return sp


def _step(s, now, temp, cmds, msgs, events):
    test = s['test']
    if temp is None:
        return
    if test['step_since'] is None and abs(temp - test['sp']) <= TMIN_STEP_BAND_K:
        test['step_since'] = now
    if test['step_since'] is not None and now - test['step_since'] >= TMIN_DWELL_S:
        if _next_step(s, now, cmds, msgs) is None:
            _no_opening(s, now, cmds, msgs, events)


def _scout(s, now, temp, cmds, msgs, events):
    test = s['test']
    sp = test['start_c'] + BATCH_CREEP_C_MIN / 60.0 * (now - test['step_since'])
    if sp > BATCH_CEILING_C:
        return _no_opening(s, now, cmds, msgs, events)
    if abs(sp - test['sp']) >= 0.01:
        test['sp'] = sp
        cmds.append(dict(setpoint_C=round(sp, 3)))
    if now - s['phase_t0'] > 1800.0:
        s['phase_t0'] = now
        cmds.append(dict(renew=True))


def _no_opening(s, now, cmds, msgs, events):
    test = s['test']
    top = min(BATCH_CEILING_C, test['est'] + TMIN_ABOVE_EST_K) \
        if test['est'] is not None and not test['scout'] else BATCH_CEILING_C
    test.update(outcome=NO_OPENING, note=f"no opening by {top:.0f} °C")
    cmds.append(dict(armed=False))
    _end_test(s, now, events)
    return _finish(s, now, STOPPED, f"{test['name']}: {test['note']} — the estimate is "
                   f"far off, the valve has changed, or it was already open at the start "
                   f"(no rise to see)", msgs, events)


# ── out of band, the opening, closing ──────────────────────────────────────

def _out_of_band(s, now, cmds, msgs):
    test = s['test']
    test.update(outcome=ABORTED_BAND,
                note=f"upstream " + (f"{s['up']:.3f} bar" if s['up'] is not None
                                     else "not read")
                + f" at {test['sp']:.1f} °C ({s['phase']})")
    cmds.append(dict(armed=False))
    msgs.append(f"t-min-tune: {test['name']} abandoned — {test['note']}, outside "
                f"{s['target']:g} ± {s['band']:g} bar; heater off. The next test starts "
                f"afresh when it is back inside")
    s['waiting_note'] = True
    _enter(s, WAIT, now)


def _detected(s, now, temp, cmds, msgs, keep_outcome=False):
    test = s['test']
    i = len(test['samples']) - 1
    j = find_onset(test['samples'], s['base'], i) if i >= 0 else 0
    t_on, T_on = (test['samples'][j][:2] if test['samples'] else (now, temp))
    if T_on is None:
        T_on = next((x[1] for x in reversed(test['samples'][:j]) if x[1] is not None), temp)
    band_ok = in_band(s, s['up'])
    if not keep_outcome:
        if not band_ok:
            test['outcome'] = OPENED_OUT_OF_BAND
        elif s['phase'] == HOLD:
            test['outcome'] = OPENED_AT_START
        elif s['phase'] == SCOUT:
            test['outcome'] = SCOUT_RESULT
        else:
            test['outcome'] = T_MIN
    test.update(base=s['base'], up_open=s['up'], in_band=band_ok, t_onset=t_on, T_onset=T_on,
                t_detect=now, T_detect=temp)
    cmds.append(dict(armed=False))
    o = test['outcome']
    if o == OPENED_AT_START:
        s['extra'] += TMIN_OPENED_AT_START_K
        if T_on is not None:
            s['opened_low'] = T_on if s['opened_low'] is None else min(s['opened_low'], T_on)
        test['note'] = (f"opened while holding {test['start_c']:.1f} °C: the start was too "
                        f"high; the next starts {s['extra']:g} K lower")
    elif o == T_MIN:
        s['extra'] = 0.0
    what = {T_MIN: f"T_min {T_on:.2f} °C on the {test['sp']:.1f} °C step",
            OPENED_AT_START: f"opened at the start ({T_on:.2f} °C)",
            SCOUT_RESULT: f"scout opened at {T_on:.2f} °C",
            OPENED_OUT_OF_BAND: f"opened out of band at {T_on:.2f} °C"}[o]
    msgs.append(f"t-min-tune: {test['name']} — {what} (detected at "
                + (f"{temp:.2f}" if temp is not None else "?") + " °C), upstream "
                + (f"{s['up']:.3f} bar" if s['up'] is not None else "not read")
                + " — heater off; waiting for the valve to close")
    s['cool_since'] = now
    _enter(s, COOL, now)


def _cool(s, now, temp, msgs, events):
    """Closed: the chamber within TMIN_CLOSED_DEC of the baseline it had
    before the opening, and settled. T_close is the TC when it first got
    back within that."""
    test = s['test']
    base, yf = test['base'], s['y_filt']
    back = base is None or (yf is not None and yf <= base + TMIN_CLOSED_DEC)
    if back and test['closed_t'] is None:
        test['closed_t'], test['closed_T'] = now, temp
        s['last_close'] = temp
    if back and settled(s['trend']):
        msgs.append(f"t-min-tune: {test['name']} — closed (chamber back at its baseline"
                    + (f"; T_close {test['closed_T']:.1f} °C" if test['closed_T'] is not None
                       else "") + ")")
        _end_test(s, now, events)
        s['base'] = None
        _enter(s, WAIT, now)
        return
    if now - s['phase_t0'] > TMIN_CLOSE_MAX_S:
        test['note'] = ((test['note'] + "; ") if test['note'] else "") + \
            f"not closed after {TMIN_CLOSE_MAX_S / 60:g} min"
        _end_test(s, now, events)
        _finish(s, now, STOPPED, f"{test['name']}: the valve did not close within "
                f"{TMIN_CLOSE_MAX_S / 60:g} min (chamber still above its baseline)",
                msgs, events)


# ── ending tests and the session ────────────────────────────────────────────

def counted(test):
    return test['outcome'] == T_MIN and bool(test['in_band']) and not test['scout']


def _end_test(s, now, events):
    test = s['test']
    if test is None or test['done']:
        return
    test['done'] = True
    test['t_end'] = now
    if counted(test):
        s['results'] += 1
    events.append(('test_end', test))


def _finish(s, now, state, note, msgs, events):
    s.update(state=state, note=note, ended=now)
    msgs.append(f"t-min-tune {state}: {note}; {s['results']} result"
                f"{'s' * (s['results'] != 1)} this session")
    events.append(('end', s))
    return [], msgs, events


def _abort(s, now, reason, cmds, msgs, events):
    test = s['test']
    if test is not None and not test['done'] and test['outcome'] is None:
        test['outcome'], test['note'] = STOPPED_TEST, reason
    cmds.append(dict(armed=False))
    _end_test(s, now, events)
    _finish(s, now, ABORTED, reason, msgs, events)
    return cmds, msgs, events


def stop(s, now, reason):
    """Operator stop (or the driver closing): heater off at once. An opening
    already detected in this test is kept."""
    if s['state'] != RUNNING:
        return [], [], []
    cmds, msgs, events = [dict(armed=False)], [], []
    test = s['test']
    if test is not None and not test['done'] and test['outcome'] is None:
        test['outcome'], test['note'] = STOPPED_TEST, reason
    _end_test(s, now, events)
    _finish(s, now, STOPPED, reason, msgs, events)
    return cmds, msgs, events


def result_row(s, test, slope, time_iso, trace="", converged=False, converged_T=None):
    """The t-min.csv row of a finished test. converged_T: the converged
    T_min, on the row that converged."""
    t_min = test['T_onset']
    corr = None
    if t_min is not None:
        corr = t_min + (slope * (s['target'] - test['up_open'])
                        if test['up_open'] is not None and s['target'] is not None else 0.0)
    return dict(
        time=time_iso, seating=s['seating'], torque_Nm=s['torque'],
        upstream_target_bar=s['target'], band_bar=s['band'], outcome=test['outcome'],
        start_degC=test['start_c'], step_degC=test['sp'] if not test['scout'] else
        (round(test['sp'], 2) if test['sp'] is not None else None),
        t_min_degC=round(t_min, 3) if t_min is not None else None,
        t_min_at_target_degC=round(corr, 3) if corr is not None else None,
        t_detect_degC=round(test['T_detect'], 3) if test['T_detect'] is not None else None,
        t_close_degC=round(test['closed_T'], 2) if test['closed_T'] is not None else None,
        upstream_at_open_bar=test['up_open'],
        in_band=None if test['in_band'] is None else (1 if test['in_band'] else 0),
        baseline_mbar=10 ** test['base'] if test['base'] is not None else None,
        estimate_degC=round(test['est'], 2) if test['est'] is not None else None,
        margin_K=test['margin'], estimate_from=test['how'],
        counted=1 if counted(test) else 0, converged=1 if converged else 0,
        trace=trace, note=test['note'],
        t_min_converged_degC=round(converged_T, 3) if converged_T is not None else None,
        lock_nut_torque_Nm=s.get('lock_nut'))


def status_text(s):
    if s is None:
        return None
    head = f"t-min-tune {s['state']}"
    if s['state'] != RUNNING:
        return f"{head} — {s['note']}"
    test, ph = s['test'], s['phase']
    band = f"{s['target']:g} ± {s['band']:g} bar"
    if test is None or (test['done'] and ph == WAIT):
        extra = f"waiting for the upstream ({band})" if not in_band(s, s['up']) \
            else "starting the next test"
        return f"{head} · {extra} · {s['results']} result{'s' * (s['results'] != 1)}"
    extra = ""
    if ph == WAIT:
        extra = f"abandoned — waiting for the upstream ({band})"
    elif ph == HOLD:
        extra = (f"holding {test['start_c']:.1f} °C"
                 + (f", waiting for the chamber ({s['trend']:+.3f} dec/min)"
                    if test['hold_since'] is not None and s['trend'] is not None else ""))
    elif ph == STEP:
        now = s['last_t'] if s['last_t'] is not None else test['step_since']
        left = (TMIN_DWELL_S - (now - test['step_since'])) / 60 \
            if test['step_since'] is not None else None
        extra = f"step {test['sp']:.1f} °C" + (f", {left:.1f} min left" if left is not None
                                               else ", getting there")
    elif ph == SCOUT:
        extra = f"scouting, setpoint {test['sp']:.1f} °C"
    elif ph == COOL:
        extra = "cooling until the valve has closed"
    est = (f" · estimate {test['est']:.1f} °C, start {test['start_c']:.0f} °C"
           if test['est'] is not None else "")
    return (f"{head} · {test['name']}{' (scout)' if test['scout'] else ''} · {extra}{est}"
            f" · {s['results']} result{'s' * (s['results'] != 1)}")
