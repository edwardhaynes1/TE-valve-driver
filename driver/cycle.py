"""Cycling: openings one after another, until stopped — the logic, with no
threads, clock, files or hardware (like batch.py, whose detection it uses).
See context.md, "Cycling", and history entry 34.

    new_cycling(torque, setting, now, history=(), deep_every=CYCLE_DEEP_EVERY)
    step(c, now, temp, vac, vac_status, heater, p_up, p_up_t, predict)
        -> (commands, msgs, events)     one step, ~4 Hz. predict(bar, deep)
                                        -> (°C, how, scatter K or None) or
                                        None: the opening-map fit's prediction
                                        for this setting (cyclerun supplies it).
                                        events: ('cycle_start', cyc),
                                        ('cycle_end', cyc), ('opening', row),
                                        ('end', c)
    stop(c, now, reason) -> (commands, msgs, events)
    labels(c) -> (cycle name, phase)    for the log rows
    status_text(c) -> str               one line for the window

A cycle: hold (auto-t at the bottom temperature: 4 min for a deep cycle,
none for a shallow one, until the chamber is settled) → approach (auto-t to
the margin below the prediction at the upstream pressure now) → creep
(+BATCH_CREEP_C_MIN) → detection (heater off) → cooldown, which ends when
the valve has closed (the chamber back at its baseline) and is the margin +
CYCLE_BELOW_START_K below the prediction — or, for a deep cycle (every
CYCLE_DEEP_EVERY-th, the first included), at the batch hold target (35 °C,
or 20 K below the opening point; adaptive cooling near room temperature).
Then the next cycle. A creeping opening becomes a row of the openings table
('opening' event, at the end of its cooldown so the closing temperature is
known). One that opens while still approaching started above the opening
point: not a row, and the next start is CYCLE_APPROACH_EXTRA_K lower. No
prediction yet (a new torque): the cycle scouts from the table guess, as
scout 1 did. A refill while heating pauses detection (as in batches).
"""

import math
from collections import deque
from datetime import datetime

from . import batch
from .batch import (APPROACH, COOLDOWN, CREEP, HEATING, HOLD, chamber_trend,
                    find_onset, margin, median3, open_threshold_dec, settled)
from .config import (
    BATCH_CEILING_C, BATCH_CEILING_HOLD_S, BATCH_COOL_BELOW_K, BATCH_COOL_MAX_S,
    BATCH_COOL_SLOW_K, BATCH_COOL_SLOW_S, BATCH_COOL_TO_C, BATCH_CREEP_C_MIN,
    BATCH_GAP_BUFFER_K, BATCH_GUESS_ABOVE_K, BATCH_HOLD_BAND_K, BATCH_HOLD_S,
    BATCH_MARGIN_MAX_K, BATCH_MIN_GAP_K, BATCH_RECOVER_FRACTION, CYCLE_REFILL_MAX_S,
    BATCH_REFILL_RISE_BAR, BATCH_SCOUT2_BELOW_K, BATCH_SETTLE_MAX_S, BATCH_SETTLE_WINDOW_S,
    BATCH_START_BAND_K, BATCH_UP_MAX_AGE_S, CYCLE_APPROACH_EXTRA_K, CYCLE_BELOW_START_K,
    CYCLE_CREEP_GUARD_S, CYCLE_DEEP_EVERY, CYCLE_MAX_FAILS, LABJACK_SAMPLE_HZ, PRESSURE_BAD_READS_TO_TRIP,
    PRESSURE_BASE_GUARD_S, PRESSURE_BASE_MIN_S, PRESSURE_BASE_WINDOW_S, PRESSURE_FILTER_S,
    PRESSURE_TRIP_MBAR, PRESSURE_UP_K_PER_BAR, PRESSURE_UP_MAX_DOWN_K,
    PRESSURE_UP_MAX_SHIFT_K, VAC_HIGH_STATES,
)
from .controller import AUTO_T, opening_point

RUNNING, STOPPED, ABORTED = 'running', 'stopped', 'aborted'
REFILL = 'refill'                # phase label while detection is paused after a refill
_CEILING_BAND_K = 1.0
_UP_STILL_BAR = 0.01
_REFILL_OPEN_RISE_DEC_MIN = 0.005


def cycle_name(n):
    return f"cycle{n:03d}"


def new_cycling(torque, setting, now, history=(), deep_every=CYCLE_DEEP_EVERY):
    """history: [(time, mbar)] recent chamber readings, so a settled chamber
    needs no waiting."""
    if torque is None:
        raise ValueError("cycling needs the seat screw torque")
    c = dict(
        torque=torque, setting=setting, started=now, ended=None, state=RUNNING, note="",
        deep_every=max(1, int(deep_every)), n=0, cyc=None, phase=COOLDOWN, phase_t0=now,
        begun=False, openings=0, fails_in_row=0, extra=0.0, guess=None,
        hist=deque(maxlen=int((max(PRESSURE_BASE_WINDOW_S, BATCH_SETTLE_WINDOW_S) + 10)
                              * LABJACK_SAMPLE_HZ * 2)),
        y_filt=None, last_t=None, base=None, bad_vac=0, sp=None, creep_t0=None,
        ceiling_since=None, hold_since=None, hold_c=None, next_hold=None,
        next_deep=True, up=None, up_low=None, mask=None, trend=None, cool_hist=[],
        prev=None, pred=None,
    )
    for t, mbar in history:
        if mbar and mbar > 0 and t <= now:
            c['hist'].append((t, math.log10(mbar)))
    if c['hist']:
        c['y_filt'] = c['hist'][-1][1]
    c['cyc'] = _new_cycle(c, now, deep=True)
    return c


def _new_cycle(c, now, deep):
    c['n'] += 1
    return dict(n=c['n'], name=cycle_name(c['n']), deep=deep, scout=False, fast=False,
                t_start=now, bottom_c=None, bottom_s=None, start_c=None, ref_c=None,
                how="", margin=None, guess=None, status=None, opened_during=None,
                samples=[], base=None, up_open=None, t_onset=None, T_onset=None,
                y_onset=None, t_detect=None, T_detect=None, y_detect=None,
                closed_T=None, closed_t=None, refills=0, refill_T=None, note="",
                t_end=None)


def labels(c):
    if c is None or c['state'] != RUNNING:
        return '', ''
    return c['cyc']['name'], (REFILL if c['mask'] is not None else c['phase'])


def _enter(c, phase, now):
    c['phase'], c['phase_t0'], c['mask'] = phase, now, None
    if phase == COOLDOWN:
        c['cool_hist'] = []


# ── the reference: the fit's prediction, else what this session has seen ──

def _table_guess(c):
    """(°C, how): auto-p's opening point for the torque, shifted for upstream."""
    t, bar, _, how, _ = opening_point(c['torque'])
    shift = 0.0
    if c['up'] is not None and bar is not None:
        shift = max(-PRESSURE_UP_MAX_DOWN_K,
                    min(PRESSURE_UP_MAX_SHIFT_K, -PRESSURE_UP_K_PER_BAR * (c['up'] - bar)))
    return t + shift, f"the torque table's {t:.1f} °C" + (
        f", shifted {shift:+.1f} K for upstream" if shift else "")


def reference(c, predict, deep):
    """(T_open °C expected now, how, scatter or None), or None: the fit's
    prediction at the upstream pressure now, else this session's last
    opening (shifted −12 K/bar for upstream). The lower of the two if both
    (a just-measured opening the fit hasn't taken in yet may be lower)."""
    out = None
    if predict is not None:
        p = predict(c['up'], deep)
        if p is not None:
            out = (p[0], p[1], p[2] if len(p) > 2 else None)
    g = c['guess']
    if g is not None:
        t, up = g
        if up is not None and c['up'] is not None:
            t += -PRESSURE_UP_K_PER_BAR * (c['up'] - up)
        if out is None or t < out[0]:
            out = (t, "this session's last opening", out[2] if out else None)
    return out


# ── one step ────────────────────────────────────────────────────────────────

def step(c, now, temp, vac, vac_status, heater, p_up=None, p_up_t=None, predict=None):
    cmds, msgs, events = [], [], []
    if c['state'] != RUNNING:
        return cmds, msgs, events
    cyc = c['cyc']
    fresh = p_up is not None and (p_up_t is None or now - p_up_t <= BATCH_UP_MAX_AGE_S)
    c['up'] = p_up if fresh else None
    if c['up'] is not None and c['phase'] in HEATING:
        c['up_low'] = c['up'] if c['up_low'] is None else min(c['up_low'], c['up'])
    y = math.log10(vac) if (vac is not None and vac > 0) else None
    dt = 0.0 if c['last_t'] is None else max(0.0, now - c['last_t'])
    c['last_t'] = now
    if y is not None:
        c['y_filt'] = y if c['y_filt'] is None else (
            c['y_filt'] + dt / (PRESSURE_FILTER_S + dt) * (y - c['y_filt']))
        c['hist'].append((now, y))
    cyc['samples'].append((now, temp, y, None))
    if not c['begun']:
        c['begun'] = True
        events.append(('cycle_start', cyc))
        msgs.append(f"Cycling: {cyc['name']} (deep) — heater off, cooling first")
    _update_baseline(c, now)
    cyc['samples'][-1] = (now, temp, y, c['base'])
    phase = c['phase']

    if phase in HEATING:
        if not heater['armed']:
            why = heater.get('trip_reason') or ("heater disarmed (by the operator, "
                                                "or the LabJack reconnecting)")
            return _abort(c, now, why, cmds, msgs, events)
        if vac_status in VAC_HIGH_STATES or (vac is not None and vac > PRESSURE_TRIP_MBAR):
            return _abort(c, now, f"chamber pressure too high "
                          f"({vac_status or f'{vac:.1e} mbar'})", cmds, msgs, events)
        c['bad_vac'] = 0 if y is not None else c['bad_vac'] + 1
        if c['bad_vac'] >= PRESSURE_BAD_READS_TO_TRIP:
            return _abort(c, now, f"no valid chamber pressure ({vac_status or 'no reading'})"
                          " — can't detect the opening", cmds, msgs, events)
        opened = (c['base'] is not None and c['y_filt'] is not None and y is not None
                  and c['y_filt'] - c['base'] > open_threshold_dec(c['base']))
        if phase == HOLD:
            _hold(c, now, temp, predict, cmds, msgs, events)
        elif c['mask'] is not None:
            _masked(c, now, temp, predict, cmds, msgs)
        elif (c['up'] is not None and c['up_low'] is not None
              and c['up'] - c['up_low'] >= BATCH_REFILL_RISE_BAR):
            _refill_seen(c, now, temp, cmds, msgs)
        elif opened:
            _detected(c, now, temp, cmds, msgs)
        elif temp is not None and _at_ceiling(c, now, temp):
            _failed(c, now, cmds, msgs, events)
        elif phase == APPROACH:
            _approach(c, now, temp, heater, msgs)
        else:
            _creep(c, now, cmds, msgs)
    elif phase == COOLDOWN:
        _cooldown(c, now, temp, predict, cmds, msgs, events)
    return cmds, msgs, events


def _update_baseline(c, now):
    """As batch._update_baseline: the median of the last 5-30 s, fresh while
    holding, never rising while heating, frozen once open or paused."""
    if c['phase'] == COOLDOWN or c['cyc']['T_detect'] is not None or c['mask'] is not None:
        return
    lo = now - PRESSURE_BASE_WINDOW_S
    pts = [yy for t, yy in c['hist'] if lo <= t <= now - PRESSURE_BASE_GUARD_S]
    if len(pts) < PRESSURE_BASE_MIN_S * LABJACK_SAMPLE_HZ:
        return
    ys = sorted(pts)
    n = len(ys)
    med = ys[n // 2] if n % 2 else 0.5 * (ys[n // 2 - 1] + ys[n // 2])
    c['base'] = med if (c['phase'] == HOLD or c['base'] is None) else min(med, c['base'])


# ── cooldown → hold → approach → creep ─────────────────────────────────────

def _cooldown(c, now, temp, predict, cmds, msgs, events):
    """Heater off. The valve must have closed (after an opening: the chamber
    back within half the threshold of that cycle's baseline) and be cold
    enough for the next cycle: shallow — the margin + CYCLE_BELOW_START_K
    below the reference; deep — the batch hold target, or where it stops
    cooling (adaptive) once 6 K below the reference; no reference — 35 °C,
    or held where it is if colder."""
    cyc = c['cyc']
    fresh = cyc['status'] is None
    if temp is not None:
        c['cool_hist'].append((now, temp))
    base = cyc['base']
    yf = c['y_filt']
    thr = open_threshold_dec(base) if base is not None else None
    if (cyc['T_detect'] is not None and cyc['closed_t'] is None and yf is not None
            and base is not None and yf <= base + thr):
        cyc['closed_t'], cyc['closed_T'] = now, temp
    recovered = base is None or (yf is not None and yf <= base + BATCH_RECOVER_FRACTION * thr)
    deep = True if fresh else _next_deep(c)
    ref = reference(c, predict, deep)
    m = (margin(ref[2]) + c['extra']) if ref else BATCH_MARGIN_MAX_K
    slowed = False
    if ref is None:
        target = BATCH_COOL_TO_C
    elif deep:
        target = min(BATCH_COOL_TO_C, ref[0] - BATCH_COOL_BELOW_K)
    else:
        target = ref[0] - m - CYCLE_BELOW_START_K
    reached = temp is not None and temp <= target + BATCH_HOLD_BAND_K
    if temp is not None and not reached and deep:
        then = [T for t, T in c['cool_hist'] if t <= now - BATCH_COOL_SLOW_S]
        slowed = bool(then) and then[-1] - temp < BATCH_COOL_SLOW_K
        if slowed and ref is not None:
            slowed = ref[0] - math.ceil(temp * 2.0) / 2.0 >= BATCH_MIN_GAP_K + BATCH_GAP_BUFFER_K
        elif slowed and ref is None:
            slowed = False
    if (reached or slowed) and recovered:
        if reached and (ref is None or not deep):
            # don't warm a valve we know nothing of; and a shallow cycle whose
            # valve took longer to close than to reach the target starts from
            # where it is (no point warming it back up)
            hold = min(target, math.ceil(temp * 2.0) / 2.0)
        elif reached:
            hold = target
        else:
            hold = math.ceil(temp * 2.0) / 2.0
        c['next_hold'], c['next_deep'] = hold, deep
        if fresh:
            cyc['deep'] = deep
            return _enter_hold(c, now, cmds, msgs)
        cyc['t_end'] = now
        _end_cycle(c, now, msgs, events)
        c['cyc'] = _new_cycle(c, now, deep)
        c['cyc']['samples'].append((now, temp, c['hist'][-1][1] if c['hist'] else None, None))
        events.append(('cycle_start', c['cyc']))
        c.update(base=None, bad_vac=0, sp=None, creep_t0=None, ceiling_since=None,
                 hold_since=None)
        return _enter_hold(c, now, cmds, msgs)
    if now - c['phase_t0'] > BATCH_COOL_MAX_S:
        what = ("the chamber still above its baseline (the valve not closed?)"
                if (reached or slowed) else f"the valve still above {target:.1f} °C")
        cyc['note'] = (cyc['note'] + "; " if cyc['note'] else "") + \
            f"cooldown over {BATCH_COOL_MAX_S / 60:g} min — {what}"
        cyc['t_end'] = now
        _end_cycle(c, now, msgs, events)
        return _finish(c, now, STOPPED, f"{cyc['name']}: {cyc['note']}", msgs, events)


def _next_deep(c):
    """Is the next cycle deep? Every deep_every-th, counting the first."""
    return (c['n']) % c['deep_every'] == 0          # the next is n + 1


def _enter_hold(c, now, cmds, msgs):
    cyc = c['cyc']
    hold_c = c['next_hold'] if c['next_hold'] is not None else BATCH_COOL_TO_C
    c['next_hold'] = None
    c['hold_c'] = cyc['bottom_c'] = hold_c
    c.update(hold_since=None, base=None, bad_vac=0, hold_base=None, hold_refill=False)
    cmds += [dict(mode=AUTO_T, setpoint_C=round(hold_c, 3)), dict(armed=True)]
    c['up_low'] = c['up']
    _enter(c, HOLD, now)
    need = BATCH_HOLD_S if cyc['deep'] else 0.0
    msgs.append(f"Cycling: {cyc['name']}{' (deep)' if cyc['deep'] else ''} — heater armed, "
                f"holding {hold_c:.1f} °C "
                + (f"for {need / 60:g} min" if need else "until the chamber is settled"))


def _hold(c, now, temp, predict, cmds, msgs, events):
    cyc = c['cyc']
    if (c['up'] is not None and c['up_low'] is not None
            and c['up'] - c['up_low'] >= BATCH_REFILL_RISE_BAR):
        c['up_low'] = c['up']              # a refill while holding: its jump is waited out
        c['hold_refill'] = True
    # The valve can open during the hold if it has moved down below the
    # hold temperature: the chamber rises above its lowest level in the hold
    # (not after a refill, whose jump is waited out). Then it opened here.
    if c['base'] is not None:
        c['hold_base'] = c['base'] if c['hold_base'] is None else min(c['hold_base'],
                                                                     c['base'])
    hb = c['hold_base']
    if (not c['hold_refill'] and hb is not None and c['y_filt'] is not None
            and c['y_filt'] - hb > open_threshold_dec(hb)):
        c['base'] = hb
        return _detected(c, now, temp, cmds, msgs)
    if temp is not None and abs(temp - c['hold_c']) <= BATCH_HOLD_BAND_K \
            and c['hold_since'] is None:
        c['hold_since'] = now
    held = now - c['hold_since'] if c['hold_since'] is not None else 0.0
    need = BATCH_HOLD_S if cyc['deep'] else 0.0
    c['trend'] = trend = chamber_trend(c['hist'], now)
    if held >= need and settled(trend) and c['base'] is not None and temp is not None \
            and c['hold_since'] is not None:
        cyc['bottom_s'] = held
        _plan(c, now, temp, predict, cmds, msgs)
        c['up_low'] = c['up']
        _enter(c, APPROACH, now)
    elif now - c['phase_t0'] > need + BATCH_SETTLE_MAX_S:
        why = (f"chamber not settled ({trend:+.3f} dec/min)" if trend is not None
               else "no chamber trend") if held >= need else \
            f"valve temperature never reached {c['hold_c']:.1f} °C"
        cyc['note'] = f"{why} after {(now - c['phase_t0']) / 60:.0f} min of holding"
        cyc['status'] = batch.RUN_ABORTED
        cmds.append(dict(armed=False))
        cyc['t_end'] = now
        _end_cycle(c, now, msgs, events)
        _finish(c, now, STOPPED, f"{cyc['name']}: {cyc['note']}", msgs, events)


def _plan(c, now, temp, predict, cmds, msgs):
    """Set the cycle's start: the margin below the reference; with none, a
    scout from 10 K below the torque table's guess."""
    cyc = c['cyc']
    ref = reference(c, predict, cyc['deep'])
    if ref is None:
        guess, how = _table_guess(c)
        cyc.update(scout=True, guess=guess, ref_c=guess, how=how,
                   margin=BATCH_SCOUT2_BELOW_K,
                   start_c=min(BATCH_CEILING_C - _CEILING_BAND_K, guess - BATCH_SCOUT2_BELOW_K))
        why = (f"scouting: {BATCH_SCOUT2_BELOW_K:g} K below {how}; fast to "
               f"{BATCH_CEILING_C:g} °C if not open by {guess + BATCH_GUESS_ABOVE_K:.1f} °C")
    else:
        m = margin(ref[2]) + c['extra']
        cyc.update(ref_c=ref[0], how=ref[1], margin=m,
                   start_c=min(BATCH_CEILING_C - _CEILING_BAND_K, ref[0] - m))
        why = f"{m:.1f} K below {ref[0]:.1f} °C ({ref[1]})"
    c['sp'] = cyc['start_c']
    cmds.append(dict(setpoint_C=round(cyc['start_c'], 3)))
    msgs.append(f"Cycling: {cyc['name']} — chamber settled; heating to "
                f"{cyc['start_c']:.1f} °C ({why}), then creep")


def _approach(c, now, temp, heater, msgs):
    cyc = c['cyc']
    if cyc['start_c'] is None or temp is None:
        return
    if heater.get('t_burst') is None and temp >= cyc['start_c'] - BATCH_START_BAND_K:
        c['creep_t0'] = now
        _enter(c, CREEP, now)


def _creep(c, now, cmds, msgs):
    cyc = c['cyc']
    if cyc['fast']:
        return
    if cyc['scout'] and c['sp'] is not None and c['sp'] >= cyc['guess'] + BATCH_GUESS_ABOVE_K:
        cyc['fast'] = True
        c['sp'] = BATCH_CEILING_C
        cmds.append(dict(setpoint_C=BATCH_CEILING_C))
        msgs.append(f"Cycling: {cyc['name']} — not open {BATCH_GUESS_ABOVE_K:g} K above the "
                    f"table guess: heating fast towards {BATCH_CEILING_C:g} °C (reads high; "
                    f"the next cycle creeps from it)")
        return
    sp = min(BATCH_CEILING_C, cyc['start_c'] + BATCH_CREEP_C_MIN / 60.0 * (now - c['creep_t0']))
    if c['sp'] is None or abs(sp - c['sp']) >= 0.01:
        c['sp'] = sp
        cmds.append(dict(setpoint_C=round(sp, 3)))


def _at_ceiling(c, now, temp):
    if temp >= BATCH_CEILING_C - _CEILING_BAND_K:
        if c['ceiling_since'] is None:
            c['ceiling_since'] = now
        return now - c['ceiling_since'] >= BATCH_CEILING_HOLD_S
    c['ceiling_since'] = None
    return False


# ── the opening, and failures ───────────────────────────────────────────────

def _detected(c, now, temp, cmds, msgs):
    cyc = c['cyc']
    i = len(cyc['samples']) - 1
    j = find_onset(cyc['samples'], c['base'], i)
    t_on, T_on = cyc['samples'][j][:2]
    if T_on is None:
        T_on = next((s[1] for s in reversed(cyc['samples'][:j]) if s[1] is not None), temp)
    phase = c['phase']
    if phase == CREEP and c['creep_t0'] is not None and t_on < c['creep_t0'] + CYCLE_CREEP_GUARD_S:
        phase = APPROACH            # the rise began before (or just as) the creep started
    cyc.update(status=batch.OPENED, opened_during=phase, base=c['base'], up_open=c['up'],
               t_onset=t_on, T_onset=T_on, y_onset=median3(cyc['samples'], j),
               t_detect=now, T_detect=temp, y_detect=median3(cyc['samples'], i))
    cmds.append(dict(armed=False))
    c['ceiling_since'] = None
    c['fails_in_row'] = 0
    c['guess'] = (T_on, c['up'])
    if phase == CREEP and not cyc['fast']:
        c['extra'] = 0.0
        note = ""
    elif phase in (APPROACH, HOLD):
        c['extra'] += CYCLE_APPROACH_EXTRA_K
        where = "approaching" if phase == APPROACH else "holding"
        cyc['note'] = f"opened while still {where}: started above the opening point"
        note = (f" — while still {where} (not an opening row); the next starts "
                f"{c['extra']:g} K further below")
    else:
        cyc['note'] = "read while heating fast (not an opening row)"
        note = " — read while heating fast: the next cycle creeps from it"
    msgs.append(f"Cycling: {cyc['name']} — valve opened: T_open {T_on:.2f} °C "
                f"(detected at {temp:.2f} °C), upstream "
                + (f"{c['up']:.3f} bar" if c['up'] is not None else "not read")
                + f" — heater off{note}")
    _enter(c, COOLDOWN, now)


def _failed(c, now, cmds, msgs, events):
    cyc = c['cyc']
    cyc.update(status=batch.NO_OPENING, note=f"no opening by {BATCH_CEILING_C:g} °C",
               base=c['base'])
    cmds.append(dict(armed=False))
    c['ceiling_since'] = None
    c['fails_in_row'] += 1
    msgs.append(f"Cycling: {cyc['name']} — {cyc['note']}; heater off")
    if c['fails_in_row'] >= CYCLE_MAX_FAILS:
        cyc['t_end'] = now
        _end_cycle(c, now, msgs, events)
        return _finish(c, now, STOPPED, f"{CYCLE_MAX_FAILS} cycles in a row did not open "
                       f"by {BATCH_CEILING_C:g} °C", msgs, events)
    c['guess'] = None          # the prediction was wrong: scout next if the fit has none
    _enter(c, COOLDOWN, now)


# ── refills while heating (as batches, history 34) ─────────────────────────

def _refill_seen(c, now, temp, cmds, msgs):
    cyc = c['cyc']
    rise = c['up'] - c['up_low']
    hold = temp if temp is not None else c['sp']
    c['mask'] = dict(t0=now, base=c['base'], up0=c['up_low'], up_top=c['up'], t_top=now,
                     phase=c['phase'], sp=hold)
    cyc['refills'] += 1
    if cyc['refill_T'] is None:
        cyc['refill_T'] = temp
    c['up_low'] = c['up']
    c['ceiling_since'] = None
    if hold is not None:
        c['sp'] = hold
        cmds.append(dict(setpoint_C=round(hold, 3)))
    msgs.append(f"Cycling: {cyc['name']} — upstream rose {rise:.2f} bar (a refill): "
                f"detection paused, setpoint held at "
                + (f"{hold:.1f} °C" if hold is not None else "where it was")
                + " until the chamber is back")


def _masked(c, now, temp, predict, cmds, msgs):
    cyc, m = c['cyc'], c['mask']
    if c['up'] is not None:
        c['up_low'] = c['up']
        if m['up_top'] is None or c['up'] > m['up_top'] + _UP_STILL_BAR:
            m['t_top'] = now
        m['up_top'] = c['up'] if m['up_top'] is None else max(m['up_top'], c['up'])
    c['trend'] = trend = chamber_trend(c['hist'], now, since=m['t_top'])
    base0, yf = m['base'], c['y_filt']
    back = base0 is None or (yf is not None and yf <= base0 + BATCH_RECOVER_FRACTION
                             * open_threshold_dec(base0))
    opened = (trend is not None and trend > _REFILL_OPEN_RISE_DEC_MIN and base0 is not None
              and yf is not None and yf > base0 + open_threshold_dec(base0))
    if settled(trend) and back:
        c['mask'] = None
        c['base'] = None
        held = m['sp']
        ref = reference(c, predict, cyc['deep'])
        if ref is not None and not cyc['scout']:
            m_ = margin(ref[2]) + c['extra']
            cyc.update(ref_c=ref[0], margin=m_,
                       start_c=min(BATCH_CEILING_C - _CEILING_BAND_K, ref[0] - m_))
        if cyc['fast'] or cyc['start_c'] is None:
            c['sp'] = BATCH_CEILING_C
            cmds.append(dict(setpoint_C=BATCH_CEILING_C))
            c['phase'] = CREEP if cyc['fast'] else APPROACH
        elif held is None or held < cyc['start_c'] - BATCH_START_BAND_K:
            c['sp'] = cyc['start_c']
            cmds.append(dict(setpoint_C=round(cyc['start_c'], 3)))
            c['phase'] = APPROACH
        else:
            c['creep_t0'] = now - (held - cyc['start_c']) * 60.0 / BATCH_CREEP_C_MIN
            c['sp'] = held
            c['phase'] = CREEP
        msgs.append(f"Cycling: {cyc['name']} — chamber back after the refill "
                    f"({now - m['t0']:.0f} s): detection resumed, {c['phase']}")
    elif opened or now - m['t0'] > CYCLE_REFILL_MAX_S:
        cyc.update(status=batch.RUN_ABORTED, base=None,
                   note="the valve opened while detection was paused after a refill "
                        "(a refill lowers the opening point)" if opened else
                        f"the chamber didn't come back within {CYCLE_REFILL_MAX_S / 60:g} "
                        f"min of a refill")
        cmds.append(dict(armed=False))
        c['base'] = None
        msgs.append(f"Cycling: {cyc['name']} — {cyc['note']}: cycle dropped, heater off")
        _enter(c, COOLDOWN, now)


# ── ending cycles and the session ───────────────────────────────────────────

def _iso(t):
    return datetime.fromtimestamp(t).isoformat(timespec='milliseconds') if t else ''


def opening_row(c, cyc, rule="", source=""):
    """The openings-table row of a cycle that opened while creeping, or None."""
    if cyc['status'] != batch.OPENED or cyc['opened_during'] != CREEP or cyc['fast']:
        return None
    return dict(
        time=_iso(cyc['t_onset']), setting=c['setting'], torque_Nm=c['torque'],
        upstream_bar=cyc['up_open'], t_open_degC=round(cyc['T_onset'], 3),
        t_detect_degC=round(cyc['T_detect'], 3) if cyc['T_detect'] is not None else None,
        baseline_mbar=10 ** cyc['base'] if cyc['base'] is not None else None,
        closed_degC=round(cyc['closed_T'], 2) if cyc['closed_T'] is not None else None,
        bottom_degC=cyc['bottom_c'], bottom_s=round(cyc['bottom_s'] or 0.0, 1),
        deep=1 if cyc['deep'] else 0, refills=cyc['refills'], detect_rule=rule,
        creep_degC_per_min=BATCH_CREEP_C_MIN, source=source,
        note="scout (from the table guess)" if cyc['scout'] else "")


def _end_cycle(c, now, msgs, events):
    cyc = c['cyc']
    c['prev'] = cyc
    if cyc['status'] == batch.OPENED and cyc['opened_during'] == CREEP and not cyc['fast']:
        c['openings'] += 1
        events.append(('opening', cyc))
    events.append(('cycle_end', cyc))


def _finish(c, now, state, note, msgs, events):
    c.update(state=state, note=note, ended=now)
    msgs.append(f"Cycling {state}: {note}; {c['openings']} opening"
                f"{'s' * (c['openings'] != 1)} this session")
    events.append(('end', c))
    return [], msgs, events


def _abort(c, now, reason, cmds, msgs, events):
    cyc = c['cyc']
    if cyc['status'] is None:
        cyc['status'], cyc['note'] = batch.RUN_ABORTED, reason
    cmds.append(dict(armed=False))
    cyc['t_end'] = now
    _end_cycle(c, now, msgs, events)
    _finish(c, now, ABORTED, reason, msgs, events)
    return cmds, msgs, events


def stop(c, now, reason):
    """Operator stop (or the driver closing): disarms at once. An opening
    already detected in this cycle is kept."""
    if c['state'] != RUNNING:
        return [], [], []
    cmds, msgs, events = [dict(armed=False)], [], []
    cyc = c['cyc']
    if cyc['status'] is None:
        cyc['status'], cyc['note'] = batch.RUN_ABORTED, reason
    cyc['t_end'] = now
    _end_cycle(c, now, msgs, events)
    _finish(c, now, STOPPED, reason, msgs, events)
    return cmds, msgs, events


def status_text(c):
    if c is None:
        return None
    head = f"cycling {c['state']}"
    if c['state'] != RUNNING:
        return f"{head} — {c['note']} · {c['openings']} openings this session"
    cyc = c['cyc']
    extra = ""
    ph = labels(c)[1]
    if c['mask'] is not None:
        extra = " · refill: waiting for the chamber"
    elif c['phase'] == HOLD:
        if c['hold_since'] is None:
            extra = f" · to {c['hold_c']:.1f} °C"
        elif cyc['deep'] and BATCH_HOLD_S - (c['last_t'] - c['hold_since']) > 0:
            extra = (f" · {c['hold_c']:.1f} °C, "
                     f"{(BATCH_HOLD_S - (c['last_t'] - c['hold_since'])) / 60:.1f} min left")
        elif c['trend'] is not None:
            extra = f" · waiting for the chamber ({c['trend']:+.3f} dec/min)"
    elif c['phase'] in (APPROACH, CREEP) and c['sp'] is not None:
        extra = f" · setpoint {c['sp']:.1f} °C" + (f" (open expected ~{cyc['ref_c']:.1f})"
                                                   if cyc['ref_c'] is not None else "")
    return (f"{head} · {cyc['name']}{' (deep)' if cyc['deep'] else ''}"
            f"{' (scout)' if cyc['scout'] else ''} · {ph}{extra} · "
            f"{c['openings']} opening{'s' * (c['openings'] != 1)} this session")
