"""Batch sequencer — the logic of a batch of opening-point runs, with no
threads, clock, files or hardware (like controller.py). See context.md,
"Batches", and history-log entries 27-30.

    new_batch(n_tests, torque_nm, now, topup_drop_bar, history, known, old,
              retorqued)                a fresh batch. known: a remembered
                                        opening point to start from (the
                                        scouts are skipped); old: the one on
                                        file (replaced or not at the end)
    step(b, now, temp, vac, vac_status, heater, p_up, p_up_t)
        -> (commands, msgs, events)     one step, ~4 Hz. heater: the heater
                                        state's armed, trip_reason, t_burst.
                                        commands: dicts for heater_command,
                                        in order. events: ('run_start', run),
                                        ('run_end', run), ('batch_end', b)
    abort(b, reason) -> (commands, msgs, events)
    resume(b, now) -> msgs              after a top-up: carry on
    labels(b) -> (run name, phase)      for the log rows
    precision(b) -> (mean, std, n, ±K)  the stopping rule's numbers
    remembered_entry(b, name) -> entry  what to remember now, or None
    run_summary(b, run, rows) -> dict   schema.RUN_SUMMARY for one run;
                                        rows: [(t, main-log row dict)]
    batch_summary(b, summaries) -> dict schema.BATCH_SUMMARY
    status_text(b) -> str               one line for the window

A run: cooldown (heater off, to the hold temperature, chamber back at its
baseline) → hold (auto-t at the hold temperature for BATCH_HOLD_S, until the
chamber is settled) → approach (auto-t to the start temperature) → creep
(setpoint up at BATCH_CREEP_C_MIN) → detection (heater disarmed) → the next
run's cooldown. Scout 1 heats straight towards the ceiling instead. The
batch stops once the mean T_open of its test runs is precise enough, or
after N test runs. A test run that opens while still approaching started
above the opening point: a scout 2 follows (find again). With a top-up
limit set, a run whose upstream pressure has fallen that far waits in
top-up (heater off) until resume(); the chamber is then judged from when
the upstream pressure stopped rising (the fill), or from resume() if no
fill was seen.
"""

import math
import statistics
from collections import deque
from datetime import datetime

from .config import (
    BATCH_CEILING_C, BATCH_CEILING_HOLD_S, BATCH_COOL_BELOW_K, BATCH_COOL_MAX_S,
    BATCH_COOL_MIN_C, BATCH_COOL_TO_C, BATCH_CREEP_C_MIN, BATCH_DETECT_ABS_MBAR,
    BATCH_DETECT_FLOOR_DEC, BATCH_DETECT_REL_DEC, BATCH_FILL_RISE_BAR,
    BATCH_HOLD_BAND_K, BATCH_HOLD_S, BATCH_MARGIN_ADD_K, BATCH_MARGIN_MAX_K,
    BATCH_MARGIN_MIN_K, BATCH_MARGIN_SCATTER_X, BATCH_MARGIN_UP_ADD_K,
    BATCH_MARGIN_UP_BAR, BATCH_MAX_FAILS, BATCH_MIN_TESTS, BATCH_ONSET_DEC,
    BATCH_PRECISION_K, BATCH_RECOVER_FRACTION, BATCH_SCOUT2_BELOW_K,
    BATCH_SCOUT2_TRIES, BATCH_SETTLE_MAX_FALL_DEC_MIN, BATCH_SETTLE_MAX_RISE_DEC_MIN,
    BATCH_SETTLE_MAX_S, BATCH_SETTLE_WINDOW_S, BATCH_SLOPE_MAX_SE,
    BATCH_SLOPE_MIN_TESTS, BATCH_START_BAND_K, BATCH_UP_FIT_MIN_SPREAD_BAR,
    BATCH_UP_MAX_AGE_S, LABJACK_SAMPLE_HZ, PRESSURE_BAD_READS_TO_TRIP,
    PRESSURE_BASE_GUARD_S, PRESSURE_BASE_MIN_S, PRESSURE_BASE_WINDOW_S,
    PRESSURE_FILTER_S, PRESSURE_TRIP_MBAR, PRESSURE_UP_K_PER_BAR, VAC_HIGH_STATES,
    heater_power_w,
)
from .controller import AUTO_T

COOLDOWN, HOLD, APPROACH, CREEP, TOPUP = 'cooldown', 'hold', 'approach', 'creep', 'top-up'
HEATING = (HOLD, APPROACH, CREEP)
RUNNING, COMPLETE, STOPPED, ABORTED = 'running', 'complete', 'stopped', 'aborted'
OPENED, NO_OPENING, RUN_ABORTED = 'opened', 'no opening', 'aborted'
_CEILING_BAND_K = 1.0            # "at the ceiling": TC within this of it

# Student t, two-sided 95 %, by degrees of freedom (above 30: an expansion
# accurate to 0.003).
_T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
         2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
         2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def t975(dof):
    if dof < 1:
        return None
    if dof <= len(_T975):
        return _T975[dof - 1]
    z = 1.959964
    return z + (z ** 3 + z) / (4 * dof) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * dof * dof)


def open_threshold_dec(base):
    """Rise above the baseline (decades) that counts as open, for a baseline
    of log10(p) = base: BATCH_DETECT_ABS_MBAR or BATCH_DETECT_REL_DEC,
    whichever is smaller (comes first), but at least BATCH_DETECT_FLOOR_DEC."""
    thr = BATCH_DETECT_REL_DEC
    if BATCH_DETECT_ABS_MBAR:
        thr = min(thr, math.log10(1.0 + BATCH_DETECT_ABS_MBAR / 10 ** base))
    return max(BATCH_DETECT_FLOOR_DEC, thr)


def _onset_dec(base):
    return min(BATCH_ONSET_DEC, 0.5 * open_threshold_dec(base))


def run_name(index, attempt=0):
    """0 → scout1, 1 → scout2 (repeats: scout2b, scout2c …), 2 → testrun01, …"""
    if index < 2:
        name = ('scout1', 'scout2')[index]
        if attempt == 0:
            return name
        return name + ('abcdefgh'[attempt] if attempt < 8 else f"_{attempt + 1}")
    return f"testrun{index - 1:02d}"


def new_batch(n_tests, torque_nm, now, topup_drop_bar=None, history=(), known=None,
              old=None, retorqued=None):
    """history: [(time, mbar)] of recent chamber readings (e.g. the driver's
    last few minutes), so an already settled chamber needs no waiting.
    known: a remembered opening point (openings.py entry) to start from —
    the scouts are skipped. old: the remembered one on file, whether used or
    not; retorqued: the operator's answer (None if not asked)."""
    if torque_nm is None:
        raise ValueError("a batch needs the seat screw torque")
    b = dict(
        n_tests=int(n_tests), torque=torque_nm, started=now, ended=None,
        topup_drop=topup_drop_bar, up=None, up_start=None, top_ups=0, trend=None,
        state=RUNNING, note="", phase=COOLDOWN, phase_t0=now, begun=False,
        run=None, runs=[], fails_in_row=0, tests_started=0, scout2_tries=0,
        ref_from=0, finds=0, known=known, old=old, retorqued=retorqued,
        settle_from=None, fill_t=None, up_min=None, up_prev=None,
        hist=deque(maxlen=int((max(PRESSURE_BASE_WINDOW_S, BATCH_SETTLE_WINDOW_S) + 10)
                              * LABJACK_SAMPLE_HZ * 2)),
        y_filt=None, last_t=None, base=None, bad_vac=0,
        sp=None, creep_t0=None, ceiling_since=None, hold_since=None, hold_c=None,
        cold_base=None, hold_check=False,
    )
    for t, mbar in history:
        if mbar and mbar > 0 and t <= now:
            b['hist'].append((t, math.log10(mbar)))
    if b['hist']:
        b['y_filt'] = b['hist'][-1][1]
    if known is not None:
        b['tests_started'] = 1
        b['run'] = _new_run(b, 2, now, None)
    else:
        b['run'] = _new_run(b, 0, now, None)
    b['hold_c'] = hold_temperature(b)
    return b


def _new_run(b, index, now, start_c, attempt=0):
    return dict(
        index=index, attempt=attempt, name=run_name(index, attempt), counts=index >= 2,
        t_start=now,
        start_c=start_c, status=None, note="", opened_during=None, averaged=False,
        samples=[],                 # (t, TC, log10 p or None, baseline then)
        i_detect=None, base=None, up_open=None,
        t_onset=None, T_onset=None, y_onset=None,
        t_detect=None, T_detect=None, y_detect=None,
        closed_t=None, closed_T=None, t_fail=None, cool_end_t=None, efold=None,
        why="", ref_c=None, margin=None, hold_c=None, held_s=None,
    )


def labels(b):
    """(run name, phase) for the log rows; blanks once the batch is over."""
    if b is None or b['state'] != RUNNING:
        return '', ''
    return b['run']['name'], b['phase']


# ── what the batch knows so far ─────────────────────────────────────────────

def _all_runs(b):
    """Finished runs, and the current one if it isn't among them yet."""
    run = b['run']
    return b['runs'] + ([run] if run is not None and run not in b['runs'] else [])


def _averaged(b, since=0):
    """Test runs that opened while creeping (the ones averaged), including
    the current run as soon as it has."""
    return [r for r in _all_runs(b)[since:] if r['averaged']]


def _last_scout2(b, since=0):
    found = None
    for r in _all_runs(b)[since:]:
        if r['index'] == 1 and r['status'] == OPENED and r['opened_during'] == CREEP:
            found = r
    return found


def slope(b):
    """(K/bar, where from) for correcting T_open to another upstream
    pressure: the batch's own fit (≥ BATCH_SLOPE_MIN_TESTS test runs,
    standard error ≤ BATCH_SLOPE_MAX_SE), else the remembered one, else
    −PRESSURE_UP_K_PER_BAR."""
    runs = _averaged(b)
    if len(runs) >= BATCH_SLOPE_MIN_TESTS:
        fit = upstream_fit([(r['up_open'], r['T_onset']) for r in runs])
        if fit and fit[1] is not None and fit[1] <= BATCH_SLOPE_MAX_SE:
            return fit[0], 'batch fit'
    for e in (b['known'], b['old']):
        if e is not None and e.get('k_per_bar') is not None:
            return e['k_per_bar'], 'remembered'
    return -PRESSURE_UP_K_PER_BAR, 'default'


def _corrected(runs, k):
    """T_open of each run corrected to the runs' mean upstream pressure
    (unchanged if any upstream reading is missing), and that pressure."""
    ups = [r['up_open'] for r in runs]
    if not runs or any(u is None for u in ups):
        return [r['T_onset'] for r in runs], None
    u_mean = statistics.mean(ups)
    return [r['T_onset'] - k * (r['up_open'] - u_mean) for r in runs], u_mean


def precision(b):
    """(mean T_open, scatter after correcting to the mean upstream, n,
    95 % half-width) over the averaged test runs; None where undefined.
    The correction can't move the mean — it only removes the scatter the
    leak causes."""
    runs = _averaged(b)
    if not runs:
        return None, None, 0, None
    vals, _ = _corrected(runs, slope(b)[0])
    n = len(vals)
    mean = statistics.mean(vals)
    if n < 2:
        return mean, None, n, None
    sd = statistics.stdev(vals)
    return mean, sd, n, t975(n - 1) * sd / math.sqrt(n)


def _scatter(b):
    """This batch's scatter (≥ 2 test runs), else the remembered one's."""
    _, sd, n, _ = precision(b)
    if n >= 2:
        return sd
    if b['known'] is not None:
        return b['known'].get('scatter_K')
    return None


def reference(b):
    """(T_open °C, at upstream bar or None, how) that the next test run
    starts from: this batch's test runs (since the last find-again), else
    its scout 2, else the remembered opening point. None if none."""
    runs = _averaged(b, b['ref_from'])
    if runs:
        vals, u = _corrected(runs, slope(b)[0])
        n = len(runs)
        return statistics.mean(vals), u, f"the mean of {n} test run{'s' * (n > 1)}"
    s2 = _last_scout2(b, b['ref_from'])
    if s2 is not None:
        return s2['T_onset'], s2['up_open'], f"{s2['name']}'s reading"
    if b['known'] is not None:
        k = b['known']
        return k['t_open_C'], k.get('upstream_bar'), "the remembered opening point"
    return None


def hold_temperature(b):
    """BATCH_COOL_TO_C, or BATCH_COOL_BELOW_K below the best guess of the
    opening point if that is lower, but not below BATCH_COOL_MIN_C."""
    ref = reference(b)
    guess = ref[0] if ref else None
    opened = [r['T_onset'] for r in _all_runs(b) if r['status'] == OPENED]
    if opened:                      # after a find-again the reference can be stale
        guess = opened[-1] if guess is None else min(guess, opened[-1])
    target = BATCH_COOL_TO_C
    if guess is not None:
        target = min(target, guess - BATCH_COOL_BELOW_K)
    return max(BATCH_COOL_MIN_C, target)


def margin(scatter, up_moved_bar=None):
    """How far below the reference a test run starts, K."""
    m = BATCH_MARGIN_MAX_K if scatter is None else min(
        BATCH_MARGIN_MAX_K, max(BATCH_MARGIN_MIN_K,
                                BATCH_MARGIN_SCATTER_X * scatter + BATCH_MARGIN_ADD_K))
    if up_moved_bar is not None and abs(up_moved_bar) > BATCH_MARGIN_UP_BAR:
        m += BATCH_MARGIN_UP_ADD_K
    return m


def _plan_start(b, run):
    """Set a test run's start (and why) from the reference, shifted to the
    upstream pressure now."""
    ref_c, ref_up, how = reference(b)
    up = b['up']
    moved = (up - ref_up) if (up is not None and ref_up is not None) else None
    k = slope(b)[0]
    at_now = ref_c + (k * moved if moved is not None else 0.0)
    m = margin(_scatter(b), moved)
    run.update(ref_c=at_now, margin=m,
               start_c=min(BATCH_CEILING_C - _CEILING_BAND_K, at_now - m))
    shift = (f", shifted {k * moved:+.1f} K for upstream {moved:+.2f} bar"
             if moved is not None and abs(k * moved) >= 0.05 else "")
    run['why'] = f"{m:.1f} K below {how}, {at_now:.2f} °C{shift}"


# ── one step ────────────────────────────────────────────────────────────────

def step(b, now, temp, vac, vac_status, heater, p_up=None, p_up_t=None):
    cmds, msgs, events = [], [], []
    if b['state'] != RUNNING:
        return cmds, msgs, events
    run = b['run']
    fresh = p_up is not None and (p_up_t is None or now - p_up_t <= BATCH_UP_MAX_AGE_S)
    b['up'] = p_up if fresh else None
    if b['up_start'] is None and b['up'] is not None:
        b['up_start'] = b['up']
    if b['phase'] == TOPUP and b['up'] is not None:
        _watch_fill(b, now)
    y = math.log10(vac) if (vac is not None and vac > 0) else None
    dt = 0.0 if b['last_t'] is None else max(0.0, now - b['last_t'])
    b['last_t'] = now
    if y is not None:
        b['y_filt'] = y if b['y_filt'] is None else (
            b['y_filt'] + dt / (PRESSURE_FILTER_S + dt) * (y - b['y_filt']))
        b['hist'].append((now, y))
    run['samples'].append((now, temp, y, None))
    if not b['begun']:
        b['begun'] = True
        events.append(('run_start', run))
        if b['known'] is not None:
            msgs.append(f"Batch: starting from the remembered opening point for "
                        f"{b['torque']:.2f} N·m, {b['known']['t_open_C']:.1f} °C — no scouts")
        msgs.append(f"Batch: {run['name']} — heater off, cooling to "
                    f"{b['hold_c']:.1f} °C to hold there")
    _update_baseline(b, now)
    run['samples'][-1] = (now, temp, y, b['base'])      # the baseline as it stood then
    phase = b['phase']

    if phase in HEATING:
        # The heater must still be armed, and the chamber readable and low.
        if not heater['armed']:
            why = heater.get('trip_reason') or ("heater disarmed (by the operator, "
                                                "or the LabJack reconnecting)")
            return _abort(b, now, f"{why}", cmds, msgs, events)
        if vac_status in VAC_HIGH_STATES or (vac is not None and vac > PRESSURE_TRIP_MBAR):
            return _abort(b, now, f"chamber pressure too high "
                          f"({vac_status or f'{vac:.1e} mbar'})", cmds, msgs, events)
        b['bad_vac'] = 0 if y is not None else b['bad_vac'] + 1
        if b['bad_vac'] >= PRESSURE_BAD_READS_TO_TRIP:
            return _abort(b, now, f"no valid chamber pressure "
                          f"({vac_status or 'no reading'}) — can't detect the opening",
                          cmds, msgs, events)
        opened = (b['base'] is not None and b['y_filt'] is not None and y is not None
                  and b['y_filt'] - b['base'] > open_threshold_dec(b['base']))
        if phase == HOLD:
            _hold(b, now, temp, cmds, msgs, events)
        elif opened:
            _detected(b, now, temp, cmds, msgs, events)
        elif temp is not None and _at_ceiling(b, now, temp):
            _failed(b, now, f"no opening by {BATCH_CEILING_C:g} °C "
                    f"(held {BATCH_CEILING_HOLD_S:g} s)", cmds, msgs, events)
        elif phase == APPROACH:
            _approach(b, now, temp, heater, msgs)
        else:
            _creep(b, now, cmds)
    elif phase == COOLDOWN:
        _cooldown(b, now, temp, cmds, msgs, events)
    return cmds, msgs, events


def _enter(b, phase, now):
    b['phase'], b['phase_t0'] = phase, now


def _line(pts):
    """Least-squares (mean t, mean y, slope per s) through (t, y) points."""
    mt = statistics.mean(t for t, _ in pts)
    my = statistics.mean(y for _, y in pts)
    sxx = sum((t - mt) ** 2 for t, _ in pts)
    slope_ = sum((t - mt) * (y - my) for t, y in pts) / sxx if sxx > 0 else 0.0
    return mt, my, slope_


def chamber_trend(hist, now, window=BATCH_SETTLE_WINDOW_S, since=None):
    """Slope of a straight-line fit of log10(p) over the last `window` s
    (and not before `since`), in decades per minute. None until the window
    is at least 80 % covered."""
    lo = now - window if since is None else max(now - window, since)
    pts = [(t, y) for t, y in hist if lo <= t <= now]
    if len(pts) < 10 or pts[-1][0] - pts[0][0] < 0.8 * window:
        return None
    return 60.0 * _line(pts)[2]


def _settled(trend):
    return (trend is not None
            and -BATCH_SETTLE_MAX_FALL_DEC_MIN <= trend <= BATCH_SETTLE_MAX_RISE_DEC_MIN)


def _enter_hold(b, now, cmds, msgs):
    """Arm the heater in auto-t at the hold temperature."""
    run = b['run']
    b['hold_c'] = hold_c = hold_temperature(b)
    run['hold_c'] = hold_c
    # Was the chamber already rising before the heater came on? Then a rise
    # during the hold is the chamber's own (outgassing, a fill): just wait
    # for it. Unknown (no readings yet, or only just after a fill) counts
    # as rising.
    trend = chamber_trend(b['hist'], now, since=b['settle_from'])
    b.update(hold_since=None, base=None, cold_base=None, bad_vac=0,
             hold_check=trend is not None and trend <= BATCH_SETTLE_MAX_RISE_DEC_MIN)
    cmds += [dict(mode=AUTO_T, setpoint_C=round(hold_c, 3)), dict(armed=True)]
    _enter(b, HOLD, now)
    msgs.append(f"Batch: {run['name']} — heater armed, holding {hold_c:.1f} °C for "
                f"{BATCH_HOLD_S / 60:g} min")


def _hold(b, now, temp, cmds, msgs, events):
    """Hold the hold temperature for BATCH_HOLD_S, then approach once the
    chamber is settled.

    The chamber level is followed through the hold two ways: the baseline
    (the median of the last 5-30 s, fresh) and its lowest value since the
    hold began (cold_base, from before the heater warmed the valve). When
    the hold is done and the chamber settled, a baseline clearly above that
    lowest level means the valve opened while warming to the hold
    temperature: its opening point is below it. Only checked if the chamber
    wasn't already rising when the hold began (hold_check): a rise that was
    under way before the heater came on is the chamber's own, and is waited
    out. A fill's jump can't trip it either — it only falls."""
    run = b['run']
    if b['base'] is not None:
        b['cold_base'] = b['base'] if b['cold_base'] is None else min(b['cold_base'],
                                                                        b['base'])
    if temp is not None and abs(temp - b['hold_c']) <= BATCH_HOLD_BAND_K:
        if b['hold_since'] is None:
            b['hold_since'] = now
    held = now - b['hold_since'] if b['hold_since'] is not None else 0.0
    b['trend'] = trend = chamber_trend(b['hist'], now, since=b['settle_from'])
    if held >= BATCH_HOLD_S and _settled(trend) and b['base'] is not None \
            and temp is not None:
        cold = b['cold_base']
        if b['hold_check'] and cold is not None and b['base'] - cold > open_threshold_dec(cold):
            return _opened_while_holding(b, now, temp, cold, cmds, msgs, events)
        b['base'] = min(b['base'], b['y_filt']) if b['y_filt'] is not None else b['base']
        run['held_s'] = held
        b['settle_from'] = None
        if run['index'] == 0:
            b['sp'] = BATCH_CEILING_C
            cmds.append(dict(setpoint_C=BATCH_CEILING_C))
            msgs.append(f"Batch: scout1 — held {held:.0f} s, chamber settled "
                        f"({trend:+.3f} dec/min, baseline {10 ** b['base']:.2e} mbar); "
                        f"heating towards {BATCH_CEILING_C:g} °C until the valve opens")
        else:
            if run['counts']:
                _plan_start(b, run)
            b['sp'] = run['start_c']
            cmds.append(dict(setpoint_C=round(run['start_c'], 3)))
            msgs.append(f"Batch: {run['name']} — held {held:.0f} s, chamber settled "
                        f"({trend:+.3f} dec/min); heating to {run['start_c']:.1f} °C "
                        f"({run['why']}), then creep")
        _enter(b, APPROACH, now)
    elif now - b['phase_t0'] > BATCH_HOLD_S + BATCH_SETTLE_MAX_S:
        why = (f"chamber not settled ({trend:+.3f} dec/min)" if trend is not None
               else "no chamber trend") if held >= BATCH_HOLD_S else \
            f"valve temperature never reached {b['hold_c']:.1f} °C"
        run['note'] = f"{why} after {(now - b['phase_t0']) / 60:.0f} min of holding"
        run['status'] = RUN_ABORTED
        cmds.append(dict(armed=False))
        _end_run(b, now, events)
        _finish(b, now, STOPPED, f"{run['name']}: {run['note']}", msgs, events)


def _opened_while_holding(b, now, temp, cold, cmds, msgs, events):
    run = b['run']
    rise = 100 * (10 ** (b['base'] - cold) - 1)
    run.update(status=RUN_ABORTED,
               note=f"the chamber settled {rise:.0f} % above its level before the hold "
                    f"({10 ** cold:.2e} → {10 ** b['base']:.2e} mbar): the valve opened "
                    f"while warming to {b['hold_c']:.1f} °C (or the valve outgasses)")
    cmds.append(dict(armed=False))
    _end_run(b, now, events)
    _finish(b, now, STOPPED, f"{run['note']} — its opening point is below the hold "
            f"temperature; lower BATCH_COOL_TO_C (and BATCH_COOL_MIN_C) in config.py",
            msgs, events)


def _update_baseline(b, now):
    """The chamber baseline as it stands now, log10(p), from the window
    PRESSURE_BASE_WINDOW_S … PRESSURE_BASE_GUARD_S ago.

    Its median: measured fresh while holding (after a top-up only from after
    the fill), so the run starts from the level the chamber is at; while
    heating towards the opening it may fall but never rise, so a gradual
    opening can't drag it up. Frozen once the valve opens. The median lags a
    falling chamber by ~17 s, which is why the hold also waits until the
    chamber isn't falling fast (BATCH_SETTLE_MAX_FALL_DEC_MIN). A trend line
    extrapolated to now was tried instead: its noise, with the never-rise
    rule, walked the baseline down and gave false openings in simulation."""
    if b['phase'] in (COOLDOWN, TOPUP) or b['run']['T_detect'] is not None:
        return
    lo = now - PRESSURE_BASE_WINDOW_S
    if b['phase'] == HOLD and b['settle_from'] is not None:
        lo = max(lo, b['settle_from'])
    pts = [(t, y) for t, y in b['hist'] if lo <= t <= now - PRESSURE_BASE_GUARD_S]
    if len(pts) < PRESSURE_BASE_MIN_S * LABJACK_SAMPLE_HZ:
        return
    ys = sorted(y for _, y in pts)
    n = len(ys)
    med = ys[n // 2] if n % 2 else 0.5 * (ys[n // 2 - 1] + ys[n // 2])
    b['base'] = med if (b['phase'] == HOLD or b['base'] is None) else min(med, b['base'])


def _approach(b, now, temp, heater, msgs):
    run = b['run']
    if run['start_c'] is None or temp is None:
        return                                   # scout 1: heats to the ceiling
    if heater.get('t_burst') is None and temp >= run['start_c'] - BATCH_START_BAND_K:
        b['creep_t0'] = now
        _enter(b, CREEP, now)
        msgs.append(f"Batch: {run['name']} — at {temp:.1f} °C, creeping "
                    f"+{BATCH_CREEP_C_MIN:g} °C/min from {run['start_c']:.1f} °C")


def _creep(b, now, cmds):
    run = b['run']
    sp = min(BATCH_CEILING_C,
             run['start_c'] + BATCH_CREEP_C_MIN / 60.0 * (now - b['creep_t0']))
    if b['sp'] is None or abs(sp - b['sp']) >= 0.01:
        b['sp'] = sp
        cmds.append(dict(setpoint_C=round(sp, 3)))


def _at_ceiling(b, now, temp):
    if temp >= BATCH_CEILING_C - _CEILING_BAND_K:
        if b['ceiling_since'] is None:
            b['ceiling_since'] = now
        return now - b['ceiling_since'] >= BATCH_CEILING_HOLD_S
    b['ceiling_since'] = None
    return False


def _base_at(sample, base):
    """The baseline that applied to a sample (its own if recorded)."""
    return sample[3] if len(sample) > 3 and sample[3] is not None else base


def _median3(samples, i):
    ys = [samples[j][2] for j in (i - 1, i, i + 1)
          if 0 <= j < len(samples) and samples[j][2] is not None]
    return statistics.median(ys) if ys else None


def find_onset(samples, base, i_detect):
    """Index of the onset: the last sample at or before detection whose
    3-point median log10 p is within the onset margin of the baseline (as it
    stood at that sample): BATCH_ONSET_DEC, or half the detection threshold
    if that is smaller. The best guess of when flow started. Falls back to
    the first sample."""
    for j in range(i_detect, -1, -1):
        y = _median3(samples, j)
        b0 = _base_at(samples[j], base)
        if y is not None and b0 is not None and y <= b0 + _onset_dec(b0):
            return j
    return 0


def flow_efold(samples, base, i_onset, i_detect, min_rise=0.02):
    """e-fold (K) of the chamber pressure's rise above the baseline between
    onset and detection: least-squares slope of ln(p − base) against the
    valve temperature, over samples whose rise is at least min_rise × base.
    None with fewer than 4 such samples or a non-rising fit."""
    pts = []
    for s in samples[i_onset:i_detect + 1]:
        T, y, b0 = s[1], s[2], 10 ** _base_at(s, base)
        if T is not None and y is not None and 10 ** y - b0 >= min_rise * b0:
            pts.append((T, math.log(10 ** y - b0)))
    if len(pts) < 4:
        return None
    mt = statistics.mean(T for T, _ in pts)
    ml = statistics.mean(l for _, l in pts)
    sxx = sum((T - mt) ** 2 for T, _ in pts)
    if sxx < 0.04:                     # under ~0.2 K of spread: no slope to fit
        return None
    s_ = sum((T - mt) * (l - ml) for T, l in pts) / sxx
    return 1.0 / s_ if s_ > 0 else None


def _detected(b, now, temp, cmds, msgs, events):
    run = b['run']
    i = len(run['samples']) - 1
    j = find_onset(run['samples'], b['base'], i)
    t_on, T_on = run['samples'][j][:2]
    if T_on is None:                             # no TC at that sample: nearest earlier
        T_on = next((s[1] for s in reversed(run['samples'][:j]) if s[1] is not None), temp)
    phase = b['phase']
    run.update(status=OPENED, opened_during=phase, i_detect=i, base=b['base'],
               efold=flow_efold(run['samples'], b['base'], j, i), up_open=b['up'],
               t_onset=t_on, T_onset=T_on, y_onset=_median3(run['samples'], j),
               t_detect=now, T_detect=temp, y_detect=_median3(run['samples'], i),
               averaged=run['counts'] and phase == CREEP)
    cmds.append(dict(armed=False))
    b['ceiling_since'] = None
    if run['counts']:
        b['fails_in_row'] = 0
    if run['index'] == 1 and phase == CREEP:
        b['scout2_tries'] = 0
    note = ""
    if run['counts'] and phase == APPROACH:
        run['note'] = ("opened while still approaching, not averaged: it started above "
                       "the opening point — finding it again")
        note = " — it started above the opening point: not averaged, finding it again"
    msgs.append(f"Batch: {run['name']} — valve opened: T_open {T_on:.2f} °C "
                f"(onset, {now - t_on:.1f} s before detection at {temp:.2f} °C), "
                f"chamber {10 ** run['y_detect']:.2e} mbar — heater disarmed{note}")
    if run['averaged']:
        mean, sd, n, half = precision(b)
        if half is not None:
            msgs.append(f"Batch: T_open so far {mean:.2f} °C ± {half:.2f} K (95 %), "
                        f"scatter {sd:.2f} K, n = {n}")
        if n >= BATCH_MIN_TESTS and half is not None and half <= BATCH_PRECISION_K:
            _end_run(b, now, events)
            _finish(b, now, COMPLETE, f"precise enough: ±{half:.2f} K after {n} test runs",
                    msgs, events)
            return
        if b['tests_started'] >= b['n_tests']:
            _end_run(b, now, events)
            prec = f", ±{half:.2f} K" if half is not None else ""
            _finish(b, now, COMPLETE, f"all {b['n_tests']} test runs done{prec}",
                    msgs, events)
            return
    _enter(b, COOLDOWN, now)


def _failed(b, now, why, cmds, msgs, events):
    run = b['run']
    run.update(status=NO_OPENING, note=why, t_fail=now, base=b['base'])
    cmds.append(dict(armed=False))
    b['ceiling_since'] = None
    msgs.append(f"Batch: {run['name']} — {why}; heater disarmed")
    if not run['counts']:
        _end_run(b, now, events)
        return _finish(b, now, STOPPED, f"{run['name']}: {why}", msgs, events)
    b['fails_in_row'] += 1
    if b['fails_in_row'] >= BATCH_MAX_FAILS:
        _end_run(b, now, events)
        return _finish(b, now, STOPPED, f"{BATCH_MAX_FAILS} test runs in a row "
                       f"did not open", msgs, events)
    _enter(b, COOLDOWN, now)


def _cooldown(b, now, temp, cmds, msgs, events):
    """Heater off: to the hold temperature, and (after an opening) until the
    chamber is back at the run's baseline. Then the hold (the same run, if
    it hasn't heated yet) or the next run."""
    run = b['run']
    fresh = run['status'] is None                  # this run hasn't heated yet
    target = hold_temperature(b)
    base = run['base'] if run['base'] is not None else (None if fresh else b['base'])
    yf = b['y_filt']
    if (run['T_detect'] is not None and run['closed_t'] is None and yf is not None
            and base is not None and yf <= base + open_threshold_dec(base)):
        run['closed_t'], run['closed_T'] = now, temp
    cooled = temp is not None and temp <= target + BATCH_HOLD_BAND_K
    recovered = base is None or (yf is not None and yf <= base + BATCH_RECOVER_FRACTION
                                 * open_threshold_dec(base))
    if cooled and recovered:
        if fresh:
            return _enter_hold(b, now, cmds, msgs)
        run['cool_end_t'] = now
        _end_run(b, now, events)
        return _after_run(b, now, temp, cmds, msgs, events)
    if now - b['phase_t0'] > BATCH_COOL_MAX_S:
        what = ("valve still above" if not cooled else "chamber still above")
        run['note'] = (run['note'] + "; " if run['note'] else "") + \
            f"cooldown over {BATCH_COOL_MAX_S / 60:g} min ({what} target)"
        if fresh:
            run['status'] = RUN_ABORTED
        run['cool_end_t'] = now
        _end_run(b, now, events)
        return _finish(b, now, STOPPED, f"{run['name']}: {run['note']}", msgs, events)


def _after_run(b, now, temp, cmds, msgs, events):
    """Choose the next run once one has cooled down."""
    run = b['run']
    below = BATCH_SCOUT2_BELOW_K
    if run['index'] == 0:
        return _next_run(b, now, temp, cmds, msgs, events, 1, run['T_onset'] - below,
                         f"{below:g} K below scout1's {run['T_onset']:.2f} °C")
    if run['index'] == 1 and run['opened_during'] == APPROACH:
        b['scout2_tries'] += 1
        if b['scout2_tries'] >= BATCH_SCOUT2_TRIES:
            return _finish(b, now, STOPPED, f"scout 2 opened before it could creep "
                           f"{BATCH_SCOUT2_TRIES} times — the opening point is well "
                           f"below where it was looked for", msgs, events)
        start = min(run['start_c'], run['T_onset']) - below
        return _next_run(b, now, temp, cmds, msgs, events, 1, start,
                         f"{below:g} K lower: {run['name']} opened at "
                         f"{run['T_onset']:.2f} °C before it could creep")
    if run['counts'] and b['tests_started'] >= b['n_tests']:
        _, _, n, half = precision(b)
        prec = f", ±{half:.2f} K" if half is not None else ""
        return _finish(b, now, COMPLETE, f"all {b['n_tests']} test runs done{prec}",
                       msgs, events)
    if run['counts'] and run['opened_during'] == APPROACH:
        # Find again: the valve opened below where this run started.
        b['finds'] += 1
        b['ref_from'] = len(b['runs'])
        start = min(run['start_c'], run['T_onset']) - below
        return _next_run(b, now, temp, cmds, msgs, events, 1, start,
                         f"{below:g} K below where {run['name']} opened, "
                         f"{run['T_onset']:.2f} °C (finding the opening point again)")
    return _next_run(b, now, temp, cmds, msgs, events, None, None, "")


def _next_run(b, now, temp, cmds, msgs, events, index, start, why):
    """index None: the next test run (its start is set after the hold)."""
    attempt = 0
    if index is None:
        b['tests_started'] += 1
        index = b['tests_started'] + 1
    elif index == 1:
        attempt = sum(1 for r in b['runs'] if r['index'] == 1)
    if start is not None:
        start = min(BATCH_CEILING_C - _CEILING_BAND_K, start)
    run = _new_run(b, index, now, start, attempt)
    run['why'] = why
    b['run'] = run
    b.update(base=None, bad_vac=0, sp=start, creep_t0=None, ceiling_since=None,
             hold_since=None)
    run['samples'].append((now, temp, b['hist'][-1][1] if b['hist'] else None, None))
    events.append(('run_start', run))
    up, drop = b['up'], b['topup_drop']
    if (drop is not None and up is not None and b['up_start'] is not None
            and up < b['up_start'] - drop):
        b.update(fill_t=None, up_min=None, up_prev=None)
        _enter(b, TOPUP, now)
        msgs.append(f"Batch: PAUSED for a top-up — upstream {up:.3f} bar, "
                    f"{b['up_start'] - up:.2f} bar below the batch's start "
                    f"({b['up_start']:.3f} bar). Top up, then press 'continue'")
    else:
        _enter_hold(b, now, cmds, msgs)
    return cmds, msgs, events


def _watch_fill(b, now):
    """During the top-up pause: note when the upstream pressure last rose,
    once it has risen BATCH_FILL_RISE_BAR above its lowest (the fill)."""
    up = b['up']
    b['up_min'] = up if b['up_min'] is None else min(b['up_min'], up)
    if (b['up_prev'] is not None and up > b['up_prev'] + 0.002
            and up >= b['up_min'] + BATCH_FILL_RISE_BAR):
        b['fill_t'] = now
    b['up_prev'] = up


def resume(b, now):
    """After a top-up: cool and hold as usual (the chamber jumps when
    upstream is filled: the hold only counts it from after the fill)."""
    if b['state'] != RUNNING or b['phase'] != TOPUP:
        return []
    b['top_ups'] += 1
    b['base'] = None
    # judge the chamber only from after the fill: when upstream stopped
    # rising, if the Keller saw it; else from now
    b['settle_from'] = b['fill_t'] if b['fill_t'] is not None else now
    _enter(b, COOLDOWN, now)
    up = f"{b['up']:.3f} bar" if b['up'] is not None else "not read"
    return [f"Batch: top-up done (upstream {up}) — {b['run']['name']} holds, then "
            f"waits for the chamber to settle"]


def _end_run(b, now, events):
    run = b['run']
    if run not in b['runs']:
        b['runs'].append(run)
        events.append(('run_end', run))


def _finish(b, now, state, note, msgs, events):
    b.update(state=state, note=note, ended=now)
    mean, sd, n, half = precision(b)
    stats = ""
    if n:
        stats = f"; T_open {mean:.2f} °C"
        if half is not None:
            stats += f" ± {half:.2f} K (95 %), scatter {sd:.2f} K"
        stats += f", n = {n}"
    msgs.append(f"Batch {state}{': ' + note if note else ''}{stats}")
    events.append(('batch_end', b))
    return [], msgs, events


def _abort(b, now, reason, cmds, msgs, events):
    run = b['run']
    if run['status'] is None:
        run['status'], run['note'] = RUN_ABORTED, reason
    elif run['cool_end_t'] is None:
        run['note'] = (run['note'] + "; " if run['note'] else "") + f"aborted: {reason}"
    cmds.append(dict(armed=False))
    _end_run(b, now, events)
    _finish(b, now, ABORTED, reason, msgs, events)
    return cmds, msgs, events


def abort(b, now, reason):
    """Operator abort (or the driver closing). Disarms at once."""
    if b['state'] != RUNNING:
        return [], [], []
    return _abort(b, now, reason, [], [], [])


# ── remembering the result ──────────────────────────────────────────────────

def remembered_entry(b, batch_name=""):
    """What to remember for this torque now the batch is over, or None:
    the mean of the averaged test runs (at their mean upstream pressure),
    else the last scout 2's reading. It replaces the old one if there was
    none, if the operator said the valve was disturbed (or wasn't asked),
    if it rests on ≥ BATCH_MIN_TESTS test runs, or if it lies outside the
    old one's expected range (the valve moved)."""
    runs = _averaged(b)
    k, k_from = slope(b)
    if runs:
        mean, sd, n, half = precision(b)
        _, u = _corrected(runs, k)
        src = "test runs"
    else:
        s2 = _last_scout2(b)
        if s2 is None:
            return None
        mean, sd, n, half, u, src = s2['T_onset'], None, 0, None, s2['up_open'], "scout 2"
    old = b['old']
    replace = old is None or b['retorqued'] is not False or n >= BATCH_MIN_TESTS
    if not replace:
        at_old = mean
        if u is not None and old.get('upstream_bar') is not None:
            at_old = mean + k * (old['upstream_bar'] - u)
        expected = max(BATCH_MARGIN_MIN_K, BATCH_MARGIN_SCATTER_X * (old.get('scatter_K') or 0.0))
        replace = abs(at_old - old['t_open_C']) > expected
    if not replace:
        return None
    when = datetime.fromtimestamp(b['ended'] or b['started'])
    return dict(
        torque_Nm=b['torque'], t_open_C=round(mean, 2),
        upstream_bar=round(u, 4) if u is not None else None,
        k_per_bar=round(k, 2), k_per_bar_from=k_from,
        scatter_K=round(sd, 3) if sd is not None else None, n=n,
        ci95_K=round(half, 3) if half is not None else None, **{'from': src},
        creep_C_min=BATCH_CREEP_C_MIN,
        settings=dict(cool_to_C=BATCH_COOL_TO_C, cool_below_K=BATCH_COOL_BELOW_K,
                      cool_min_C=BATCH_COOL_MIN_C, hold_s=BATCH_HOLD_S,
                      detect_abs_mbar=BATCH_DETECT_ABS_MBAR,
                      detect_rel_dec=BATCH_DETECT_REL_DEC,
                      detect_floor_dec=BATCH_DETECT_FLOOR_DEC),
        batch=batch_name, date=when.strftime("%d %b %Y"),
        time=when.isoformat(timespec='seconds'),
    )


# ── summaries ───────────────────────────────────────────────────────────────

def _iso(t):
    return datetime.fromtimestamp(t).isoformat(timespec='seconds') if t else ''


def _iso_ms(t):
    return datetime.fromtimestamp(t).isoformat(timespec='milliseconds') if t else ''


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _rnd(v, digits):
    return '' if v is None else round(v, digits)


def run_summary(b, run, rows, batch_name="", file=""):
    """One row of schema.RUN_SUMMARY. rows: [(t, main-log row dict)] of this
    run, t on the same clock as the batch."""
    # Heater energy from the start of the approach: gate ON time × full power.
    energy, e_t = 0.0, []
    started = False
    for t, row in rows:
        started = started or row.get('batch_phase') in (APPROACH, CREEP)
        if started:
            energy += (_num(row.get('heater_on_s')) or 0.0) * heater_power_w()
        e_t.append((t, energy, row))

    def at(t, key=None):
        """Energy (or a row value) at the last row not after t."""
        best = None
        for tt, e, row in e_t:
            if tt <= t + 1e-6:
                best = (e, row)
        if best is None:
            return None
        return best[0] if key is None else _num(best[1].get(key))

    def near(t, key):
        """Nearest row value within 5 s (the Keller is averaged per row)."""
        cands = [(abs(tt - t), _num(row.get(key))) for tt, _, row in e_t]
        cands = [c for c in cands if c[1] is not None and c[0] <= 5.0]
        return min(cands)[1] if cands else None

    opened = run['status'] == OPENED
    dT = (run['T_detect'] - run['T_onset']) if opened else None
    dy = ((run['y_detect'] - run['y_onset'])
          if opened and run['y_detect'] is not None and run['y_onset'] is not None else None)
    slope = dy / dT if (dy is not None and dT is not None and dT >= 0.2) else None
    end_t = run['cool_end_t']
    ref_t = run['t_detect'] if opened else run['t_fail']
    base = run['base']
    return {
        'batch': batch_name,
        'run': run['name'],
        'in_average': 1 if run['averaged'] else 0,
        'status': run['status'] or RUN_ABORTED,
        'start_time': _iso(run['t_start']),
        'seat_screw_torque_Nm': b['torque'],
        'start_degC': _rnd(run['start_c'], 2),
        'creep_degC_per_min': BATCH_CREEP_C_MIN if run['index'] > 0 else '',
        'opened_during': run['opened_during'] or '',
        'onset_time': _iso_ms(run['t_onset']),
        'detect_time': _iso_ms(run['t_detect']),
        't_open_degC': _rnd(run['T_onset'], 2),
        't_detect_degC': _rnd(run['T_detect'], 2),
        'open_to_detect_s': _rnd(run['t_detect'] - run['t_onset'], 1) if opened else '',
        'chamber_baseline_mbar': f"{10 ** base:.3e}" if base is not None else '',
        'chamber_at_open_mbar': f"{10 ** run['y_onset']:.3e}" if opened and run['y_onset'] is not None else '',
        'chamber_at_detect_mbar': f"{10 ** run['y_detect']:.3e}" if opened and run['y_detect'] is not None else '',
        'dlog10p_dT_dec_per_K': _rnd(slope, 4),
        'efold_K': _rnd(run['efold'], 2),
        'energy_at_open_J': _rnd(at(run['t_onset']), 1) if opened else '',
        'energy_at_detect_J': _rnd(at(run['t_detect']), 1) if opened else '',
        'heater_power_at_open_W': _rnd(at(run['t_onset'], 'heater_P_mean_calc'), 3) if opened else '',
        'upstream_at_open_bar': _rnd(near(run['t_onset'], 'keller_pressure_bar'), 4) if opened else '',
        'upstream_at_detect_bar': _rnd(near(run['t_detect'], 'keller_pressure_bar'), 4) if opened else '',
        'free_cooling_closed_degC': _rnd(run['closed_T'], 2),
        'free_cooling_closed_s': _rnd(run['closed_t'] - run['t_detect'], 1) if run['closed_t'] else '',
        'cooldown_s': _rnd(end_t - ref_t, 0) if (end_t and ref_t) else '',
        'note': run['note'],
        'file': file,
        'reference_degC': _rnd(run['ref_c'], 2),
        'margin_K': _rnd(run['margin'], 2),
        'hold_degC': _rnd(run['hold_c'], 2),
        'hold_s': _rnd(run['held_s'], 0),
    }


def _stats(values):
    v = [x for x in (_num(x) for x in values) if x is not None]
    if not v:
        return None, None, None, None
    return (statistics.mean(v), statistics.stdev(v) if len(v) > 1 else None,
            min(v), max(v))


def upstream_fit(pairs):
    """Straight line T_open = a + slope × upstream through (bar, °C) pairs.
    (slope K/bar, its standard error, residual std K), or None with fewer
    than 3 pairs or less than BATCH_UP_FIT_MIN_SPREAD_BAR of spread. The
    standard error needs 4 pairs; with 3 it is None."""
    pts = [(u, t) for u, t in pairs if u is not None and t is not None]
    if len(pts) < 3 or max(u for u, _ in pts) - min(u for u, _ in pts) < BATCH_UP_FIT_MIN_SPREAD_BAR:
        return None
    mu = statistics.mean(u for u, _ in pts)
    mt = statistics.mean(t for _, t in pts)
    sxx = sum((u - mu) ** 2 for u, _ in pts)
    slope = sum((u - mu) * (t - mt) for u, t in pts) / sxx
    res = [t - (mt + slope * (u - mu)) for u, t in pts]
    dof = len(pts) - 2
    resid = math.sqrt(sum(r * r for r in res) / dof)
    se = resid / math.sqrt(sxx) if dof >= 2 else None
    return slope, se, resid


def batch_summary(b, summaries, batch_name="", folder="", remembered=False):
    """One row of schema.BATCH_SUMMARY: averages over the test runs that
    opened while creeping (in_average = 1). remembered: this result is now
    the remembered opening point."""
    avg = [s for s in summaries if s['in_average'] == 1]
    col = lambda k: [s[k] for s in avg]                       # noqa: E731
    t_mean, t_sd, t_min, t_max = _stats(col('t_open_degC'))
    e_mean, e_sd, _, _ = _stats(col('energy_at_open_J'))
    u_mean, _, u_min, u_max = _stats(col('upstream_at_open_bar'))
    base = _stats(col('chamber_baseline_mbar'))[0]
    fit = upstream_fit([(_num(s['upstream_at_open_bar']), _num(s['t_open_degC'])) for s in avg])
    scout = {s['run']: s['t_open_degC'] for s in summaries}
    _, sd_c, _, half = precision(b)
    k, k_from = slope(b)
    return {
        'batch': batch_name,
        'start_time': _iso(b['started']),
        'end_time': _iso(b['ended']),
        'status': b['state'],
        'note': b['note'],
        'seat_screw_torque_Nm': b['torque'],
        'test_runs_requested': b['n_tests'],
        'test_runs_opened': len(avg),
        'scout1_t_open_degC': scout.get('scout1', ''),
        'scout2_t_open_degC': scout.get('scout2', ''),
        'creep_degC_per_min': BATCH_CREEP_C_MIN,
        't_open_mean_degC': _rnd(t_mean, 2),
        't_open_std_K': _rnd(t_sd, 2),
        't_open_min_degC': _rnd(t_min, 2),
        't_open_max_degC': _rnd(t_max, 2),
        't_detect_mean_degC': _rnd(_stats(col('t_detect_degC'))[0], 2),
        'open_to_detect_mean_s': _rnd(_stats(col('open_to_detect_s'))[0], 1),
        'efold_mean_K': _rnd(_stats(col('efold_K'))[0], 2),
        'energy_at_open_mean_J': _rnd(e_mean, 1),
        'energy_at_open_std_J': _rnd(e_sd, 1),
        'upstream_at_open_mean_bar': _rnd(u_mean, 4),
        'upstream_at_open_min_bar': _rnd(u_min, 4),
        'upstream_at_open_max_bar': _rnd(u_max, 4),
        'chamber_baseline_mean_mbar': f"{base:.3e}" if base is not None else '',
        't_open_vs_upstream_K_per_bar': _rnd(fit[0], 2) if fit else '',
        't_open_vs_upstream_se_K_per_bar': _rnd(fit[1], 2) if fit else '',
        't_open_resid_std_K': _rnd(fit[2], 2) if fit else '',
        'top_ups': b['top_ups'],
        'free_cooling_closed_mean_degC': _rnd(_stats(col('free_cooling_closed_degC'))[0], 2),
        'folder': folder,
        'started_from': ('remembered opening point' + (f" ({b['known']['batch']})"
                                                       if b['known'].get('batch') else '')
                         if b['known'] is not None else 'scouts'),
        't_open_ci95_K': _rnd(half, 2),
        't_open_corrected_std_K': _rnd(sd_c, 2),
        'slope_used_K_per_bar': _rnd(k, 2),
        'slope_from': k_from,
        'find_agains': b['finds'],
        'hold_degC': _rnd(_stats([r['hold_c'] for r in b['runs'] if r['averaged']])[0], 2),
        'hold_s': BATCH_HOLD_S,
        'detect_abs_mbar': BATCH_DETECT_ABS_MBAR if BATCH_DETECT_ABS_MBAR else '',
        'detect_rel_dec': BATCH_DETECT_REL_DEC,
        'remembered_after': 'yes' if remembered else 'no',
    }


def status_text(b):
    """One line for the window."""
    if b is None:
        return "batch: none running"
    run = b['run']
    head = f"batch {b['state']}"
    if b['state'] != RUNNING:
        mean, _, n, half = precision(b)
        res = ""
        if n:
            res = f" · T_open {mean:.2f} °C" + (f" ± {half:.2f} K" if half is not None
                                                 else "") + f", n = {n}"
        return head + (f" — {b['note']}" if b['note'] else "") + res
    where = (f"{run['name']} (of up to {b['n_tests']})" if run['counts']
             else run['name'])
    extra = ""
    if b['phase'] == TOPUP:
        up = f"{b['up']:.3f}" if b['up'] is not None else "?"
        return (f"{head} · PAUSED for a top-up · upstream {up} bar, started at "
                f"{b['up_start']:.3f} bar · top up, then press 'continue'")
    if b['phase'] == HOLD:
        if b['hold_since'] is None:
            extra = f" · heating to {b['hold_c']:.1f} °C"
        else:
            left = BATCH_HOLD_S - (b['last_t'] - b['hold_since'])
            if left > 0:
                extra = f" · {b['hold_c']:.1f} °C, {left / 60:.1f} min left"
            elif b['trend'] is not None:
                extra = (f" · waiting for the chamber: {b['trend']:+.3f} dec/min (needs "
                         f"{-BATCH_SETTLE_MAX_FALL_DEC_MIN:+g} to "
                         f"{BATCH_SETTLE_MAX_RISE_DEC_MIN:+g})")
    elif b['phase'] == CREEP and b['sp'] is not None:
        extra = f" · setpoint {b['sp']:.1f} °C"
    elif b['phase'] == COOLDOWN:
        extra = f" · to {hold_temperature(b):.1f} °C"
    mean, _, n, half = precision(b)
    so_far = ""
    if n:
        so_far = f" · T_open {mean:.1f} °C" + (f" ± {half:.1f} K" if half is not None else "") \
            + f" (n = {n}, stops at ±{BATCH_PRECISION_K:g} K)"
    return f"{head} · {where} · {b['phase']}{extra}{so_far}"
