"""Batch sequencer — the logic of a batch of opening-point runs, with no
threads, clock, files or hardware (like controller.py). See context.md,
"Batches", and history-log entry 27.

    new_batch(n_tests, torque_nm, now)  a fresh batch
    step(b, now, temp, vac, vac_status, heater, p_up, p_up_t)
        -> (commands, msgs, events)     one step, ~4 Hz. heater: the heater
                                        state's armed, trip_reason, t_burst.
                                        commands: dicts for heater_command,
                                        in order. events: ('run_start', run),
                                        ('run_end', run), ('batch_end', b)
    abort(b, reason) -> (commands, msgs, events)
    resume(b, now) -> msgs              after a top-up: carry on
    labels(b) -> (run name, phase)      for the log rows
    run_summary(b, run, rows) -> dict   schema.RUN_SUMMARY for one run;
                                        rows: [(t, main-log row dict)]
    batch_summary(b, summaries) -> dict schema.BATCH_SUMMARY
    status_text(b) -> str               one line for the window

A run: settle (heater off, until the chamber isn't rising) → approach
(auto-t to the start temperature) → creep (setpoint up at BATCH_CREEP_C_MIN)
→ detection (heater disarmed) → cooldown. Scout 1 settles at least
BATCH_BASELINE_S and heats straight towards the ceiling instead. With a
top-up limit set, a run whose upstream pressure has fallen that far waits in
top-up (heater off) until resume().
"""

import math
import statistics
from collections import deque
from datetime import datetime

from .config import (
    BATCH_BASELINE_S, BATCH_CEILING_C, BATCH_CEILING_HOLD_S, BATCH_COOL_BELOW_K,
    BATCH_COOL_MAX_S, BATCH_COOL_MIN_C, BATCH_CREEP_C_MIN, BATCH_MAX_FAILS,
    BATCH_ONSET_DEC, BATCH_RECOVER_DEC, BATCH_SETTLE_MAX_FALL_DEC_MIN,
    BATCH_SETTLE_MAX_RISE_DEC_MIN, BATCH_SETTLE_MAX_S,
    BATCH_SETTLE_WINDOW_S, BATCH_UP_FIT_MIN_SPREAD_BAR, BATCH_UP_MAX_AGE_S, BATCH_SCOUT2_BELOW_K, BATCH_SCOUT2_TRIES,
    BATCH_START_BAND_K,
    BATCH_TEST_BELOW_K, LABJACK_SAMPLE_HZ, PRESSURE_BAD_READS_TO_TRIP,
    PRESSURE_BASE_GUARD_S, PRESSURE_BASE_MIN_S, PRESSURE_BASE_WINDOW_S,
    PRESSURE_FILTER_S, PRESSURE_OPEN_DEC, PRESSURE_TRIP_MBAR, VAC_HIGH_STATES,
    heater_power_w,
)
from .controller import AUTO_T

SETTLE, APPROACH, CREEP, COOLDOWN, TOPUP = 'settle', 'approach', 'creep', 'cooldown', 'top-up'
RUNNING, COMPLETE, STOPPED, ABORTED = 'running', 'complete', 'stopped', 'aborted'
OPENED, NO_OPENING, RUN_ABORTED = 'opened', 'no opening', 'aborted'
_CEILING_BAND_K = 1.0            # "at the ceiling": TC within this of it


def run_name(index, attempt=0):
    """0 → scout1, 1 → scout2 (repeats: scout2b, scout2c), 2 → testrun01, …"""
    if index < 2:
        return ('scout1', 'scout2')[index] + ('' if attempt == 0 else 'abcdefgh'[attempt])
    return f"testrun{index - 1:02d}"


def new_batch(n_tests, torque_nm, now, topup_drop_bar=None):
    if torque_nm is None:
        raise ValueError("a batch needs the seat screw torque")
    b = dict(
        n_tests=int(n_tests), torque=torque_nm, started=now, ended=None,
        topup_drop=topup_drop_bar, up=None, up_start=None, top_ups=0, trend=None,
        state=RUNNING, note="", phase=SETTLE, phase_t0=now, begun=False,
        run=None, runs=[], fails_in_row=0,
        settle_from=None,
        hist=deque(maxlen=int((max(PRESSURE_BASE_WINDOW_S, BATCH_SETTLE_WINDOW_S) + 10)
                              * LABJACK_SAMPLE_HZ * 2)),
        y_filt=None, last_t=None, base=None, bad_vac=0,
        sp=None, creep_t0=None, ceiling_since=None,
    )
    b['run'] = _new_run(b, 0, now, start_c=None)
    return b


def _new_run(b, index, now, start_c, attempt=0):
    return dict(
        index=index, attempt=attempt, name=run_name(index, attempt), counts=index >= 2,
        t_start=now,
        start_c=start_c, status=None, note="", opened_during=None,
        samples=[],                 # (t, TC, log10 p or None)
        i_detect=None, base=None,
        t_onset=None, T_onset=None, y_onset=None,
        t_detect=None, T_detect=None, y_detect=None,
        closed_t=None, closed_T=None, t_fail=None, cool_end_t=None, efold=None,
        why="",
    )


def labels(b):
    """(run name, phase) for the log rows; blanks once the batch is over."""
    if b is None or b['state'] != RUNNING:
        return '', ''
    return b['run']['name'], b['phase']


def _t_open(b, index):
    """T_open of the run that set the reference (the last attempt)."""
    found = None
    for r in b['runs']:
        if r['index'] == index:
            found = r['T_onset']
    return found


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
        msgs.append(f"Batch: {run['name']} — heater off, waiting at least "
                    f"{BATCH_BASELINE_S:g} s for the chamber to settle")
    _update_baseline(b, now)
    run['samples'][-1] = (now, temp, y, b['base'])      # the baseline as it stood then
    phase = b['phase']

    if phase in (APPROACH, CREEP):
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
        if (b['base'] is not None and b['y_filt'] is not None
                and b['y_filt'] - b['base'] > PRESSURE_OPEN_DEC and y is not None):
            _detected(b, now, temp, cmds, msgs)
        elif temp is not None and _at_ceiling(b, now, temp):
            _failed(b, now, f"no opening by {BATCH_CEILING_C:g} °C "
                    f"(held {BATCH_CEILING_HOLD_S:g} s)", cmds, msgs, events)
        elif phase == APPROACH:
            _approach(b, now, temp, heater, msgs)
        else:
            _creep(b, now, cmds)
    elif phase == SETTLE:
        _settle(b, now, temp, cmds, msgs, events)
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
    slope = sum((t - mt) * (y - my) for t, y in pts) / sxx if sxx > 0 else 0.0
    return mt, my, slope


def chamber_trend(hist, now, window=BATCH_SETTLE_WINDOW_S, since=None):
    """Slope of a straight-line fit of log10(p) over the last `window` s
    (and not before `since`), in decades per minute. None until the window
    is at least 80 % covered."""
    lo = now - window if since is None else max(now - window, since)
    pts = [(t, y) for t, y in hist if lo <= t <= now]
    if len(pts) < 10 or pts[-1][0] - pts[0][0] < 0.8 * window:
        return None
    return 60.0 * _line(pts)[2]


def _settle(b, now, temp, cmds, msgs, events):
    """Heater off until the chamber isn't rising; then arm and heat."""
    run = b['run']
    b['trend'] = trend = chamber_trend(b['hist'], now, since=b['settle_from'])
    min_s = BATCH_BASELINE_S if run['index'] == 0 else 0.0
    if (now - b['phase_t0'] >= min_s and trend is not None
            and -BATCH_SETTLE_MAX_FALL_DEC_MIN <= trend <= BATCH_SETTLE_MAX_RISE_DEC_MIN
            and b['base'] is not None
            and temp is not None):
        b['base'] = min(b['base'], b['y_filt']) if b['y_filt'] is not None else b['base']
        waited = now - b['phase_t0']
        if run['index'] == 0:
            b['sp'] = BATCH_CEILING_C
            cmds += [dict(mode=AUTO_T, setpoint_C=BATCH_CEILING_C), dict(armed=True)]
            msgs.append(f"Batch: scout1 — chamber settled ({trend:+.3f} dec/min, baseline "
                        f"{10 ** b['base']:.2e} mbar); heating towards "
                        f"{BATCH_CEILING_C:g} °C until the valve opens")
        else:
            cmds += [dict(mode=AUTO_T, setpoint_C=round(run['start_c'], 3)), dict(armed=True)]
            msgs.append(f"Batch: {run['name']} — chamber settled after {waited:.0f} s "
                        f"({trend:+.3f} dec/min); heater armed, heating to "
                        f"{run['start_c']:.1f} °C ({run['why']}), then creep")
        b['settle_from'] = None
        _enter(b, APPROACH, now)
    elif now - b['phase_t0'] > BATCH_SETTLE_MAX_S:
        run['note'] = (f"chamber not settled after {BATCH_SETTLE_MAX_S / 60:g} min "
                       f"({trend:+.3f} dec/min)" if trend is not None else
                       f"no chamber trend after {BATCH_SETTLE_MAX_S / 60:g} min")
        run['status'] = RUN_ABORTED
        _end_run(b, now, events)
        _finish(b, now, STOPPED, f"{run['name']}: {run['note']}", msgs, events)


def _update_baseline(b, now):
    """The chamber baseline as it stands now, log10(p), from the window
    PRESSURE_BASE_WINDOW_S … PRESSURE_BASE_GUARD_S ago.

    Its median: measured fresh while settling (after a top-up only from
    after the fill), so the run starts from the level the chamber is at;
    while heating it may fall but never rise, so a gradual opening can't
    drag it up. Frozen once the valve opens. The median lags a falling
    chamber by ~17 s, which is why settling also waits until the chamber
    isn't falling fast (BATCH_SETTLE_MAX_FALL_DEC_MIN). A trend line
    extrapolated to now was tried instead: its noise, with the never-rise
    rule, walked the baseline down and gave false openings in simulation."""
    if b['phase'] in (COOLDOWN, TOPUP) or b['run']['T_detect'] is not None:
        return
    lo = now - PRESSURE_BASE_WINDOW_S
    if b['phase'] == SETTLE and b['settle_from'] is not None:
        lo = max(lo, b['settle_from'])
    pts = [(t, y) for t, y in b['hist'] if lo <= t <= now - PRESSURE_BASE_GUARD_S]
    if len(pts) < PRESSURE_BASE_MIN_S * LABJACK_SAMPLE_HZ:
        return
    ys = sorted(y for _, y in pts)
    n = len(ys)
    med = ys[n // 2] if n % 2 else 0.5 * (ys[n // 2 - 1] + ys[n // 2])
    b['base'] = med if (b['phase'] == SETTLE or b['base'] is None) else min(med, b['base'])


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
    3-point median log10 p is within BATCH_ONSET_DEC of the baseline (as it
    stood at that sample). The best guess of when flow started. Falls back
    to the first sample."""
    for j in range(i_detect, -1, -1):
        y = _median3(samples, j)
        if y is not None and y <= _base_at(samples[j], base) + BATCH_ONSET_DEC:
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
    slope = sum((T - mt) * (l - ml) for T, l in pts) / sxx
    return 1.0 / slope if slope > 0 else None


def _detected(b, now, temp, cmds, msgs):
    run = b['run']
    i = len(run['samples']) - 1
    j = find_onset(run['samples'], b['base'], i)
    t_on, T_on = run['samples'][j][:2]
    if T_on is None:                             # no TC at that sample: nearest earlier
        T_on = next((s[1] for s in reversed(run['samples'][:j]) if s[1] is not None), temp)
    run.update(status=OPENED, opened_during=b['phase'], i_detect=i, base=b['base'],
               efold=flow_efold(run['samples'], b['base'], j, i),
               t_onset=t_on, T_onset=T_on, y_onset=_median3(run['samples'], j),
               t_detect=now, T_detect=temp, y_detect=_median3(run['samples'], i))
    cmds.append(dict(armed=False))
    b['fails_in_row'] = 0
    b['ceiling_since'] = None
    _enter(b, COOLDOWN, now)
    msgs.append(f"Batch: {run['name']} — valve opened: T_open {T_on:.2f} °C "
                f"(onset, {now - t_on:.1f} s before detection at {temp:.2f} °C), "
                f"chamber {10 ** run['y_detect']:.2e} mbar — heater disarmed, cooling")


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


def _cool_target(b):
    run = b['run']
    ref = run['T_onset'] if run['T_onset'] is not None else _t_open(b, 1)
    return max(BATCH_COOL_MIN_C, (ref if ref is not None else BATCH_COOL_MIN_C)
               - BATCH_COOL_BELOW_K)


def _cooldown(b, now, temp, cmds, msgs, events):
    run = b['run']
    base = run['base'] if run['base'] is not None else b['base']
    yf = b['y_filt']
    if (run['T_detect'] is not None and run['closed_t'] is None and yf is not None
            and base is not None and yf <= base + PRESSURE_OPEN_DEC):
        run['closed_t'], run['closed_T'] = now, temp
    last = run['index'] >= b['n_tests'] + 1
    cooled = temp is not None and temp <= _cool_target(b)
    recovered = base is None or (yf is not None and yf <= base + BATCH_RECOVER_DEC)
    if cooled and recovered:
        run['cool_end_t'] = now
        _end_run(b, now, events)
        if last:
            return _finish(b, now, COMPLETE, "", msgs, events)
        if run['index'] == 1 and run['opened_during'] == APPROACH:
            if run['attempt'] + 1 >= BATCH_SCOUT2_TRIES:
                return _finish(b, now, STOPPED, f"scout 2 opened before it could creep "
                               f"{BATCH_SCOUT2_TRIES} times — the opening point is well "
                               f"below scout 1's reading", msgs, events)
            return _next_run(b, now, temp, cmds, msgs, events, repeat=True)
        return _next_run(b, now, temp, cmds, msgs, events)
    if now - b['phase_t0'] > BATCH_COOL_MAX_S:
        what = ("valve still above" if not cooled else "chamber still above")
        run['note'] = (run['note'] + "; " if run['note'] else "") + \
            f"cooldown over {BATCH_COOL_MAX_S / 60:g} min ({what} target)"
        run['cool_end_t'] = now
        _end_run(b, now, events)
        if last:
            return _finish(b, now, COMPLETE, run['note'], msgs, events)
        return _finish(b, now, STOPPED, f"{run['name']}: {run['note']}", msgs, events)


def _next_run(b, now, temp, cmds, msgs, events, repeat=False):
    prev = b['run']
    if repeat:
        # scout 2 opened while still heating: start another 10 K lower (and
        # below where it opened, which was itself read on a fast rise)
        index, attempt = 1, prev['attempt'] + 1
        start = min(prev['start_c'], prev['T_onset']) - BATCH_SCOUT2_BELOW_K
        why = (f"{BATCH_SCOUT2_BELOW_K:g} K lower: {prev['name']} opened at "
               f"{prev['T_onset']:.2f} °C before it could creep")
    else:
        index, attempt = prev['index'] + 1, 0
        ref = _t_open(b, 0 if index == 1 else 1)
        below = BATCH_SCOUT2_BELOW_K if index == 1 else BATCH_TEST_BELOW_K
        start = ref - below
        ref_name = next(r['name'] for r in reversed(b['runs'])
                        if r['index'] == (0 if index == 1 else 1))
        why = f"{below:g} K below {ref_name}'s {ref:.2f} °C"
    start = min(BATCH_CEILING_C - _CEILING_BAND_K, start)
    run = _new_run(b, index, now, start, attempt)
    run['why'] = why
    b['run'] = run
    b.update(base=None, bad_vac=0, sp=start, creep_t0=None, ceiling_since=None)
    run['samples'].append((now, temp, b['hist'][-1][1] if b['hist'] else None, None))
    events.append(('run_start', run))
    up, drop = b['up'], b['topup_drop']
    if (drop is not None and up is not None and b['up_start'] is not None
            and up < b['up_start'] - drop):
        _enter(b, TOPUP, now)
        msgs.append(f"Batch: PAUSED for a top-up — upstream {up:.3f} bar, "
                    f"{b['up_start'] - up:.2f} bar below the batch's start "
                    f"({b['up_start']:.3f} bar). Top up, then press 'continue'")
    else:
        _enter(b, SETTLE, now)
        msgs.append(f"Batch: {run['name']} — waiting for the chamber to settle")
    return cmds, msgs, events


def resume(b, now):
    """After a top-up: settle (the chamber jumps when upstream is filled),
    then carry on with the run."""
    if b['state'] != RUNNING or b['phase'] != TOPUP:
        return []
    b['top_ups'] += 1
    b['base'] = None
    b['settle_from'] = now          # judge the chamber only from after the fill
    _enter(b, SETTLE, now)
    up = f"{b['up']:.3f} bar" if b['up'] is not None else "not read"
    return [f"Batch: top-up done (upstream {up}) — {b['run']['name']} waits for the "
            f"chamber to settle"]


def _end_run(b, now, events):
    run = b['run']
    if run not in b['runs']:
        b['runs'].append(run)
        events.append(('run_end', run))


def _finish(b, now, state, note, msgs, events):
    b.update(state=state, note=note, ended=now)
    opened = [r['T_onset'] for r in b['runs'] if r['counts'] and r['status'] == OPENED]
    stats = ""
    if opened:
        sd = f" ± {statistics.stdev(opened):.2f} K (1σ)" if len(opened) > 1 else ""
        stats = f"; T_open {statistics.mean(opened):.2f} °C{sd}, n = {len(opened)}"
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
        'in_average': 1 if (run['counts'] and opened) else 0,
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


def batch_summary(b, summaries, batch_name="", folder=""):
    """One row of schema.BATCH_SUMMARY: averages over the test runs that
    opened (in_average = 1)."""
    avg = [s for s in summaries if s['in_average'] == 1]
    col = lambda k: [s[k] for s in avg]                       # noqa: E731
    t_mean, t_sd, t_min, t_max = _stats(col('t_open_degC'))
    e_mean, e_sd, _, _ = _stats(col('energy_at_open_J'))
    u_mean, _, u_min, u_max = _stats(col('upstream_at_open_bar'))
    base = _stats(col('chamber_baseline_mbar'))[0]
    fit = upstream_fit([(_num(s['upstream_at_open_bar']), _num(s['t_open_degC'])) for s in avg])
    scout = {s['run']: s['t_open_degC'] for s in summaries}
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
    }


def status_text(b):
    """One line for the window."""
    if b is None:
        return "batch: none running"
    run = b['run']
    done = [r for r in b['runs'] if r['counts'] and r['status'] == OPENED]
    head = f"batch {b['state']}"
    if b['state'] != RUNNING:
        return head + (f" — {b['note']}" if b['note'] else "") + \
            (f" · {len(done)} test runs opened" if done else "")
    where = f"{run['name']} ({run['index'] - 1}/{b['n_tests']})" if run['counts'] \
        else run['name']
    extra = ""
    if b['phase'] == TOPUP:
        up = f"{b['up']:.3f}" if b['up'] is not None else "?"
        return (f"{head} · PAUSED for a top-up · upstream {up} bar, started at "
                f"{b['up_start']:.3f} bar · top up, then press 'continue'")
    if b['phase'] == SETTLE and b['trend'] is not None:
        extra = (f" · waiting for the chamber: {b['trend']:+.3f} dec/min (needs "
                 f"{-BATCH_SETTLE_MAX_FALL_DEC_MIN:+g} to {BATCH_SETTLE_MAX_RISE_DEC_MIN:+g})")
    elif b['phase'] == CREEP and b['sp'] is not None:
        extra = f" · setpoint {b['sp']:.1f} °C"
    elif b['phase'] == COOLDOWN:
        extra = f" · to {_cool_target(b):.1f} °C"
    t_opens = ", ".join(f"{r['T_onset']:.1f}" for r in done)
    return (f"{head} · {where} · {b['phase']}{extra}"
            + (f" · T_open so far: {t_opens} °C" if t_opens else ""))
