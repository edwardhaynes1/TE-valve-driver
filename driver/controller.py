"""Heater controller — the control law and interlocks, with no threads,
locks, clock, logging, files or hardware.

    new_state()                          a fresh heater state (a dict)
    command(h, now, **kw)                operator commands: armed, mode,
                                         duty_cmd, setpoint_C, p_target_mbar
    step(h, now, dt, temp, tc_healthy, vac, vac_status, vac_healthy,
         p_up, p_up_t) -> (duty, msgs)       one control step:
                                         interlocks first, then manual /
                                         auto-t / auto-p
    trip(h, reason, msgs)                latch the heater off
    record_edge(h, state, duty, now, note) -> row
                                         ON-time accounting for one gate edge
    take_on_time(h, now) -> seconds      ON time since the previous call
    force_off(h, device_lost)            device-side disarm (connect / lost)
    hold_duty(t_c) -> duty               measured hold power at t_c, as a duty
    opening_point(seat_nm)               the torque table's guess at where the
                                         valve opens (batches, t-min-tune)

Everything it needs comes in as arguments: the state `h` (changed in place),
the time `now`, and the readings. Messages for the event log come back in
`msgs`. control.py wraps this for the threads; tests call it directly.
"""

import math
from collections import deque
from datetime import datetime

from .config import (
    HEATER_HOLD_AMBIENT_C, HEATER_HOLD_W_PER_K, HEATER_HOLD_W_PER_K2,
    HEATER_MAX_DUTY, HEATER_MAX_RUN_S, LABJACK_SAMPLE_HZ, PID_D_FILTER_S,
    PID_KD, PID_KI, PID_KP, PID_SETPOINT_DEFAULT, PRESSURE_APPROACH_FRACTION,
    PRESSURE_BAD_READS_TO_TRIP, PRESSURE_BASE_GUARD_S, PRESSURE_BASE_MIN_S,
    PRESSURE_BODY_LAG_S, PRESSURE_FALSE_ALARM_S,
    PRESSURE_BASE_WINDOW_S, PRESSURE_CREEP_C_MIN, PRESSURE_CREEP_MAX_C_MIN,
    PRESSURE_CREEP_MIN_C_MIN, PRESSURE_CREEP_REF_BAR, PRESSURE_CREEP_UP_EXP,
    PRESSURE_EFOLD_REF_K, PRESSURE_FILTER_S, PRESSURE_FLOW_MARGINS,
    PRESSURE_FREEZE_BELOW_K, PRESSURE_MAX_MBAR, PRESSURE_MOVE_MIN_DEC,
    PRESSURE_MOVE_SIGMAS, PRESSURE_NEAR_AIM_DEC, PRESSURE_PREDICT_S,
    PRESSURE_RESUME_FRACTION, PRESSURE_SEEK_START_C, PRESSURE_SLOPE_WINDOW_S,
    PRESSURE_SOAK_FACTOR, PRESSURE_STEADY_S, PRESSURE_TARGET_DEFAULT,
    PRESSURE_TRIM_BELOW_K, PRESSURE_TRIP_ALL_MODES, PRESSURE_AIM_BAND_DEC, PRESSURE_TRIP_MBAR,
    PRESSURE_TSP_MAX_C,
    PRESSURE_UP_K_PER_BAR, PRESSURE_UP_MAX_AGE_S, PRESSURE_UP_REF_BAR,
    SEAT_SCREW_TOL_NM, SEAT_SCREW_VALVE, TC_MAX_RATE_K_S, TC_RESPONSE_DUTY,
    TC_RESPONSE_MIN_K, TC_RESPONSE_S, TEMP_BURST_ENABLE, TEMP_BURST_LEARN,
    TEMP_BURST_MAX_S, TEMP_BURST_MIN_STEP_K, TEMP_BURST_SHORT_FRAC,
    TEMP_BURST_SHORT_MIN_K, TEMP_BURST_TAU_MAX_S, TEMP_BURST_TAU_MIN_S,
    TEMP_BURST_TAU_S, TEMP_COAST_MAX_S, TEMP_RATE_FILTER_S, TEMP_TRIP_C,
    VAC_HIGH_STATES, heater_power_w,
)


# Heater modes — the one set of names, used on screen, in code and in the
# CSV heater_mode column (logs before 17 Sept 2026 say 'auto' / 'pressure').
MANUAL = 'manual'    # fixed duty
AUTO_T = 'auto-t'    # PI holding a valve temperature setpoint
AUTO_P = 'auto-p'    # cascade: chamber pressure → temperature setpoint → PI
MODES = (MANUAL, AUTO_T, AUTO_P)


class HeaterState(dict):
    """A dict whose fields are fixed when it is made. Reading or writing a
    field that doesn't exist — a typo such as h['p_targte'] — raises
    KeyError at once instead of quietly creating a new field. Fields can't
    be removed either."""
    __slots__ = ()

    def __setitem__(self, key, value):
        if key not in self:
            raise KeyError(f"heater state has no field {key!r}")
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def _no_removal(self, *args, **kwargs):
        raise TypeError("heater state fields can't be removed")

    __delitem__ = pop = popitem = clear = setdefault = _no_removal


def new_state():
    """A fresh heater state: operator commands, loop internals, and the
    duty being applied. The measured voltage / current readout is not here:
    the device thread owns that, in shared.py."""
    return HeaterState(
        armed        = False,      # operator has armed the heater
        mode         = MANUAL,     # one of MODES
        duty_cmd     = 0.0,        # commanded duty in manual mode, 0-1
        setpoint_C   = PID_SETPOINT_DEFAULT,
        duty_actual  = 0.0,        # what the output is actually doing right now
        armed_at     = None,       # time.time() when armed
        trip_reason  = None,       # non-None = latched trip, needs re-arm
        integral     = 0.0,        # PI integral, °C·s — a trim on top of the hold feedforward
        d_prev       = None,       # last valve temperature seen by the D term
        d_filt       = 0.0,        # filtered dT/dt for the D term, °C/s
        # ── thermocouple checks and rate ─────────────────────────────────────
        tc_prev      = None,       # last valve temperature seen while armed…
        tc_prev_t    = None,       # …and when
        rate         = 0.0,        # filtered dT/dt, °C/s (burst cut prediction)
        heat_t0      = None,       # start of the current full-power stretch (response check)…
        heat_T0      = None,       # …and the valve temperature then
        duty_last    = 0.0,        # duty returned by the previous step
        # ── gate ON-time accounting ───────────────────────────────────────────
        on_since     = None,       # time.time() the gate last went ON (None while OFF)
        on_acc_from  = None,       # start of the not-yet-counted part of the current ON time
        on_time_acc  = 0.0,        # ON seconds since the last CSV row (logger resets it)
        # ── pressure (outer) loop: auto-p (history 50) ──────────────────────
        p_target_mbar   = PRESSURE_TARGET_DEFAULT,   # P_vacuum_target
        p_init          = True,    # restart on the next valid read
        p_phase         = 'baseline',   # see the auto-p section below
        p_since         = None,    # when the current phase began
        p_raw           = None,    # latest raw log10(P_vacuum / mbar)
        p_filt          = None,    # filtered log10(P_vacuum / mbar), for display
        p_hist          = deque(maxlen=int((PRESSURE_BASE_WINDOW_S + 5) * LABJACK_SAMPLE_HZ * 2)),
        p_base          = None,    # baseline log10(P_vacuum / mbar), valve shut
        p_margin        = PRESSURE_MOVE_MIN_DEC,   # rise above baseline that counts as movement
        p_quiet_since   = None,    # baseline and margin use only readings since then
        p_up_bar        = None,    # P_up used this step (None if stale)
        p_rate_c_min    = 0.0,     # creep rate now, °C/min (0 while not creeping)
        p_override      = None,    # 0.0 while the outer loop has cut the heater
        p_capped        = False,   # creep reached the setpoint limit
        p_aim           = None,    # log10 of where auto-p aims, mbar (≥ the target)
        p_creeping      = 0.0,     # the last creep rate, °C/min (sets the freeze)
        p_sp_before     = None,    # setpoint when creeping last stopped…
        p_moved_at      = None,    # …and when (a false alarm resumes from it)
        t_check         = False,   # auto-t: re-evaluate a burst (armed / setpoint changed)
        t_burst         = None,    # auto-t: None, 'burst', 'coast'
        t_burst_t0      = None,
        t_burst_peak    = None,
        t_burst_cut     = None,    # (TC at the cut, rate at the cut)
        t_burst_from    = None,    # TC where the burst began (the step it aims short of)
        t_tau           = TEMP_BURST_TAU_S,   # coast rise ÷ rate at the cut, s (learned)
    )


def command(h, now, **kwargs):
    """Apply an operator command (arm/disarm, mode, duty, setpoints) to h.

    Arming clears the latched trip and resets both integrators. Disarming
    always succeeds and always wins. The inner (temperature) integrator is
    reset only when switching to or from manual; auto-t ↔ auto-p keeps it,
    since both use the same inner loop and a reset would just cause a sag.
    renew=True restarts the maximum-armed-time clock of an armed heater and
    changes nothing else (t-min-tune: one test can outlast it, step by step)."""
    if kwargs.pop('renew', False) and h['armed']:
        h['armed_at'] = now
    if 'armed' in kwargs:
        if kwargs['armed']:
            h['armed']       = True
            h['armed_at']    = now
            h['trip_reason'] = None
            h['integral']    = 0.0
            h['p_init']      = True
            h['d_prev']      = None
            h['t_check']     = True
            h['t_burst']     = None
        else:
            h['armed']    = False
            h['armed_at'] = None
            h['duty_cmd'] = 0.0
            h['t_burst']  = None
        h.update(tc_prev=None, tc_prev_t=None, rate=0.0, heat_t0=None, duty_last=0.0)
    new_mode = kwargs.get('mode')
    if new_mode is not None and new_mode not in MODES:
        raise ValueError(f"unknown heater mode {new_mode!r}; use one of {MODES}")
    if new_mode is not None and new_mode != h['mode']:
        if (new_mode == MANUAL) != (h['mode'] == MANUAL):
            h['integral'] = 0.0
            h['d_prev']   = None
        if new_mode == AUTO_P:
            h['p_init'] = True
        h['t_burst'] = None
        h['t_check'] = new_mode == AUTO_T
    if ('setpoint_C' in kwargs and h['mode'] != AUTO_P
            and kwargs['setpoint_C'] != h['setpoint_C']):
        h['t_check'] = True
    for k in ('mode', 'duty_cmd', 'setpoint_C', 'p_target_mbar'):
        if k in kwargs:
            h[k] = kwargs[k]


def step(h, now, dt, temp, tc_healthy, vac=None, vac_status=None,
         vac_healthy=True, p_up=None, p_up_t=None):
    """One control step: evaluate all interlocks, then the control law.

    Returns (duty 0-1, event messages). Duty is 0.0 unless every interlock
    is satisfied. Interlocks are checked before the control law, never after."""
    msgs = []
    if not h['armed']:
        return 0.0, msgs
    duty = _interlocks_then_law(h, now, dt, temp, tc_healthy, vac, vac_status,
                                vac_healthy, p_up, p_up_t, msgs)
    # Response check bookkeeping: when did the current full-power stretch start?
    if h['armed'] and duty >= TC_RESPONSE_DUTY:
        if h['heat_t0'] is None:
            h['heat_t0'], h['heat_T0'] = now, temp
    else:
        h['heat_t0'] = None
    h['duty_last'] = duty
    return duty, msgs


def _interlocks_then_law(h, now, dt, temp, tc_healthy, vac, vac_status,
                         vac_healthy, p_up, p_up_t, msgs):
    mode     = h['mode']
    armed_at = h['armed_at']

    # Interlock 1 — no trustworthy temperature means no heat.
    if not tc_healthy or temp is None:
        trip(h, "thermocouple unavailable or faulted", msgs)
        return 0.0

    # Interlock 2 — over-temperature.
    if temp > TEMP_TRIP_C:
        trip(h, f"over-temperature {temp:.1f} °C > {TEMP_TRIP_C:.0f} °C", msgs)
        return 0.0

    # Interlock 3 — maximum unattended run time.
    if armed_at is not None and (now - armed_at) > HEATER_MAX_RUN_S:
        trip(h, f"maximum armed time ({HEATER_MAX_RUN_S/60:.0f} min) reached", msgs)
        return 0.0

    # Interlock 4 — a thermocouple reading that can't be the valve: it jumped
    # faster than the valve can change, or it did not rise at full power.
    prev, prev_t = h['tc_prev'], h['tc_prev_t']
    if prev is not None and now > prev_t:
        rate = (temp - prev) / (now - prev_t)
        if abs(rate) > TC_MAX_RATE_K_S:
            trip(h, f"thermocouple reading jumped {prev:.1f} → {temp:.1f} °C in "
                    f"{now - prev_t:.2f} s — faster than the valve can change; "
                    f"check the thermocouple", msgs)
            return 0.0
        h['rate'] += dt / (TEMP_RATE_FILTER_S + dt) * (rate - h['rate'])
    h['tc_prev'], h['tc_prev_t'] = temp, now
    if (TC_RESPONSE_S is not None and h['heat_t0'] is not None
            and now - h['heat_t0'] >= TC_RESPONSE_S):
        rise = temp - h['heat_T0']
        if rise < TC_RESPONSE_MIN_K:
            trip(h, f"valve temperature did not respond to full power: "
                    f"{h['heat_T0']:.1f} → {temp:.1f} °C in {now - h['heat_t0']:.0f} s "
                    f"— check the thermocouple, and that the heater supply "
                    f"(SW171) is on", msgs)
            return 0.0
        h['heat_t0'], h['heat_T0'] = now, temp      # passed: check the next stretch

    # Interlock 5 — chamber over-pressure.
    if mode == AUTO_P or PRESSURE_TRIP_ALL_MODES:
        if vac_status in VAC_HIGH_STATES:
            trip(h, f"chamber pressure too high to measure ({vac_status})", msgs)
            return 0.0
        if vac is not None and vac > PRESSURE_TRIP_MBAR:
            trip(h, f"chamber over-pressure {vac:.2e} mbar > "
                    f"{PRESSURE_TRIP_MBAR:.0e} mbar", msgs)
            return 0.0

    if mode == MANUAL:
        return max(0.0, min(HEATER_MAX_DUTY, h['duty_cmd']))

    setpoint = h['setpoint_C']
    if mode == AUTO_P:
        # Interlock 6 — pressure mode is blind without a live gauge.
        if not vac_healthy:
            trip(h, f"no valid chamber pressure for {PRESSURE_BAD_READS_TO_TRIP} "
                    f"reads ({vac_status or 'no reading'}) — pressure mode "
                    f"needs a live gauge", msgs)
            return 0.0
        if vac is not None:
            _pressure_outer_loop(h, vac, temp, dt, now, p_up, p_up_t, msgs)
        elif h['p_init']:
            return 0.0            # not initialised yet — don't heat on a stale setpoint
        # else: a single missed read — hold the last setpoint / cut
        if h['p_override'] is not None:         # heater cut by the outer loop
            return max(0.0, min(HEATER_MAX_DUTY, h['p_override']))
        setpoint = h['setpoint_C']

    if mode == AUTO_T:
        burst_duty = _temp_burst_step(h, temp, setpoint, now, msgs)
        if burst_duty is not None:
            return max(0.0, min(HEATER_MAX_DUTY, burst_duty))

    # ── auto-t / auto-p: hold feedforward + PI(D) on valve temperature ─────
    error = setpoint - temp
    prev = h['d_prev']
    if prev is None or PID_KD == 0.0:
        h['d_filt'] = 0.0
    else:
        rate = (temp - prev) / dt
        h['d_filt'] += dt / (PID_D_FILTER_S + dt) * (rate - h['d_filt'])
    h['d_prev'] = temp
    integral = h['integral'] + error * dt
    duty     = (hold_duty(setpoint) + PID_KP * error + PID_KI * integral
                - PID_KD * h['d_filt'])
    clamped  = max(0.0, min(HEATER_MAX_DUTY, duty))
    # Only accumulate when not saturated — keeps the integrator honest.
    if duty == clamped:
        h['integral'] = integral
    return clamped


def hold_duty(t_c):
    """Duty that holds the valve at t_c in the lab, from the measured hold
    power (HEATER_HOLD_* in config.py). 0 at or below ambient."""
    d = max(0.0, t_c - HEATER_HOLD_AMBIENT_C)
    watts = HEATER_HOLD_W_PER_K * d + HEATER_HOLD_W_PER_K2 * d * d
    return min(HEATER_MAX_DUTY, watts / heater_power_w())


def trip(h, reason, msgs):
    """Latch the heater off. Cleared only by an explicit disarm→arm cycle."""
    if h['trip_reason'] is None:
        h['trip_reason'] = reason
    h['armed']       = False
    h['armed_at']    = None
    h['duty_cmd']    = 0.0
    h['duty_actual'] = 0.0
    msgs.append(f"HEATER TRIP — {reason}")


def record_edge(h, state, duty, now, note=""):
    """Account for a heater gate edge at time `now` and return the row for
    the PWM switching log. Keeps the ON-time total for the main log."""
    on_s = ""
    since = h['on_since']
    if state:
        if since is None:
            h['on_since'] = h['on_acc_from'] = now
    elif since is not None:
        h['on_time_acc'] += now - h['on_acc_from']
        h['on_since'] = h['on_acc_from'] = None
        on_s = round(now - since, 3)
    return dict(timestamp=datetime.fromtimestamp(now).isoformat(timespec='milliseconds'),
                gate=1 if state else 0, duty=round(duty, 4), on_s=on_s, note=note)


def force_off(h, device_lost=False):
    """Heater off and disarmed by the device thread (not the operator): on
    connecting, and — with device_lost — when the LabJack goes away, which
    also restarts auto-p. The measured readout is the device thread's own
    (shared.clear_heater_output)."""
    h['armed'] = False
    h['duty_cmd'] = 0.0
    h['duty_actual'] = 0.0
    if device_lost:
        h['p_init'] = True


def take_on_time(h, now):
    """Gate ON seconds since the previous call (for one log row)."""
    on_s = h['on_time_acc']
    if h['on_acc_from'] is not None:
        on_s += now - h['on_acc_from']
        h['on_acc_from'] = now
    h['on_time_acc'] = 0.0
    return on_s


def _cut_now(h, temp, aim):
    """Burst cut rule: the heat left in the element carries the TC on by
    about tau × its present rate of rise."""
    return temp + h['t_tau'] * max(0.0, h['rate']) >= aim


def _burst_aim(h, setpoint):
    """Where the burst aims: short of the setpoint by TEMP_BURST_SHORT_FRAC
    of the step (at least TEMP_BURST_SHORT_MIN_K), so an underestimated
    coast still lands below it; the PID finishes the approach."""
    step = setpoint - (h['t_burst_from'] if h['t_burst_from'] is not None else setpoint)
    return setpoint - max(TEMP_BURST_SHORT_MIN_K, TEMP_BURST_SHORT_FRAC * step)


def _learn_tau(h, cut, peak):
    """Update tau from one coast: measured rise ÷ rate at the cut. Returns a
    note for the event log."""
    cut_T, cut_rate = cut
    if cut_rate < 0.2:                     # too slow to learn anything from
        return ""
    measured = (peak - cut_T) / cut_rate
    h['t_tau'] = min(TEMP_BURST_TAU_MAX_S, max(TEMP_BURST_TAU_MIN_S,
                     (1 - TEMP_BURST_LEARN) * h['t_tau'] + TEMP_BURST_LEARN * measured))
    return f", tau now {h['t_tau']:.1f} s"


def _temp_burst_step(h, temp, setpoint, now, msgs):
    """auto-t: run the burst/coast sequence if one is due.
    Returns the duty to impose, or None to let feedforward + PI run."""
    duty = None
    if h['t_check']:
        h['t_check'] = False
        if TEMP_BURST_ENABLE and setpoint - temp >= TEMP_BURST_MIN_STEP_K:
            h.update(t_burst='burst', t_burst_t0=now, t_burst_peak=None, t_burst_from=temp)
            msgs.append(f"Temperature burst: full power from {temp:.1f} °C toward "
                        f"{setpoint:.1f} °C")
        elif h['t_burst']:
            h['t_burst'] = None
    stage = h['t_burst']
    if stage == 'burst':
        el = now - h['t_burst_t0']
        if _cut_now(h, temp, _burst_aim(h, setpoint)):
            msgs.append(f"Temperature burst: cut after {el:.1f} s at {temp:.1f} °C, "
                        f"rising {h['rate']:.2f} °C/s (tau {h['t_tau']:.1f} s) — coasting")
            h.update(t_burst='coast', t_burst_t0=now, t_burst_peak=temp,
                     t_burst_cut=(temp, h['rate']))
            duty = 0.0
        elif el > TEMP_BURST_MAX_S:
            h.update(t_burst=None, d_prev=None)
            msgs.append(f"Temperature burst: time limit ({TEMP_BURST_MAX_S:g} s) at "
                        f"{temp:.1f} °C — PI resumes; check the thermocouple")
        else:
            duty = HEATER_MAX_DUTY
    elif stage == 'coast':
        # Heater off until the TC peaks, even past the setpoint, so the
        # whole rise is measured.
        h['t_burst_peak'] = max(h['t_burst_peak'], temp)
        past_peak = temp < h['t_burst_peak'] - 0.3
        if past_peak or now - h['t_burst_t0'] > TEMP_COAST_MAX_S:
            note = _learn_tau(h, h['t_burst_cut'], h['t_burst_peak']) if past_peak else ""
            h.update(t_burst=None, d_prev=None)
            msgs.append(f"Temperature burst: coast done, peak {h['t_burst_peak']:.1f} °C "
                        f"vs setpoint {setpoint:.1f} °C{note} — PI resumes")
        else:
            duty = 0.0
    return duty


# ── where the valve opens ────────────────────────────────────────────────────

def _entry_efold(i, items):
    """e-fold of table entry i; a missing one is interpolated in torque
    between the nearest entries that have one."""
    tq, (_, _, ef) = items[i]
    if ef is not None:
        return ef
    lo = next(((t, e[2]) for t, e in reversed(items[:i]) if e[2] is not None), None)
    hi = next(((t, e[2]) for t, e in items[i + 1:] if e[2] is not None), None)
    if lo and hi:
        return lo[1] + (hi[1] - lo[1]) * (tq - lo[0]) / (hi[0] - lo[0])
    return (lo or hi or (None, PRESSURE_EFOLD_REF_K))[1]


def opening_point(seat_nm):
    """The torque table's guess at where the valve opens, before any upstream
    shift — used by batches and t-min-tune, not by auto-p (history 50):
        (opening_C, at_upstream_bar, efold_K, how, detail)
    how is one of 'calibrated' (a table entry), 'interpolated' (between
    two), 'nearest' (outside the table), 'no torque' (PRESSURE_SEEK_START_C)."""
    items = sorted(SEAT_SCREW_VALVE.items())
    if seat_nm is None:
        return (PRESSURE_SEEK_START_C, PRESSURE_UP_REF_BAR, PRESSURE_EFOLD_REF_K,
                'no torque', "no seat screw torque entered — 16 Sept reference")
    efolds = [_entry_efold(i, items) for i in range(len(items))]
    torques = [t for t, _ in items]
    # e-fold for this torque: interpolated, clamped to the table's ends
    if seat_nm <= torques[0]:
        efold = efolds[0]
    elif seat_nm >= torques[-1]:
        efold = efolds[-1]
    else:
        j = next(i for i in range(1, len(torques)) if seat_nm <= torques[i])
        f = (seat_nm - torques[j - 1]) / (torques[j] - torques[j - 1])
        efold = efolds[j - 1] + f * (efolds[j] - efolds[j - 1])
    for (tq, (c, bar, _)), ef in zip(items, efolds):
        if abs(seat_nm - tq) <= SEAT_SCREW_TOL_NM:
            return c, bar, ef, 'calibrated', f"{tq:.2f} N·m calibrated"
    if seat_nm < torques[0] or seat_nm > torques[-1]:
        tq = torques[0] if seat_nm < torques[0] else torques[-1]
        c, bar, _ = SEAT_SCREW_VALVE[tq]
        return (c, bar, efold, 'nearest',
                f"{seat_nm:.2f} N·m is outside the calibrated {torques[0]:g}-"
                f"{torques[-1]:g} N·m — using the {tq:g} N·m point, UNVERIFIED")
    # Between two entries: refer both to the lower one's upstream pressure
    # (so the upstream effect isn't mixed into the torque effect), interpolate.
    j = next(i for i in range(1, len(torques)) if seat_nm < torques[i])
    (t0, (c0, b0, _)), (t1, (c1, b1, _)) = items[j - 1], items[j]
    c1_at_b0 = c1 - PRESSURE_UP_K_PER_BAR * (b0 - b1)
    f = (seat_nm - t0) / (t1 - t0)
    return (c0 + f * (c1_at_b0 - c0), b0, efold, 'interpolated',
            f"{seat_nm:.2f} N·m interpolated between {t0:g} and {t1:g} N·m, UNVERIFIED")


# ── auto-p (history 50) ──────────────────────────────────────────────────────
# No opening point is assumed and the seat screw torque is not used: the
# valve is heated slowly from where it is, and the heater backs right off
# as soon as P_vacuum moves. Only the upstream pressure P_up is trusted; it
# sets how slowly to creep. Decisions use the RAW gauge reading (the 2 s
# filter would add ~2 s of lag just when the valve snaps open).
#
#   baseline  hold the start temperature while the baseline is measured
#   seek      valve shut: creep up at the P_up rate
#   hold      P_vacuum moved: setpoint frozen below the TC by the body's lag
#             at the creep rate (≥ 1 K), no creep
#             until P_vacuum has not risen for 30 s
#   approach  open and steady, below the aim: creep at ¼ rate
#   trim      above the aim (P_vacuum_target, or more if the baseline is
#             high): the setpoint eases down at ¼ rate
#   cut       P_vacuum above, or predicted within 3 s to go above, the cut
#             line P_vacuum_max ÷ 1.4: heater off until below 0.95 × that
#   stopped   P_vacuum went above P_vacuum_max: heater off until restarted
#   park      the cut line is at or below baseline + margin: no heating up

def _creep_rate(p_up):
    """°C/min with the valve shut. Flow through a given opening grows as
    P_up^1.5, so the creep slows by the same factor. No live P_up: the
    slowest."""
    if p_up is None or p_up <= 0:
        return PRESSURE_CREEP_MIN_C_MIN
    rate = PRESSURE_CREEP_C_MIN * (PRESSURE_CREEP_REF_BAR / p_up) ** PRESSURE_CREEP_UP_EXP
    return max(PRESSURE_CREEP_MIN_C_MIN, min(PRESSURE_CREEP_MAX_C_MIN, rate))


def _fit(pts):
    """(slope in decades/s, residual standard deviation in decades) of a
    straight line through [(t, log10 p)]; (0, 0) for fewer than 3 points."""
    n = len(pts)
    if n < 3:
        return 0.0, 0.0
    tm = sum(t for t, _ in pts) / n
    ym = sum(y for _, y in pts) / n
    sxx = sum((t - tm) ** 2 for t, _ in pts)
    slope = sum((t - tm) * (y - ym) for t, y in pts) / sxx if sxx else 0.0
    res = sum((y - ym - slope * (t - tm)) ** 2 for t, y in pts)
    return slope, math.sqrt(res / (n - 2))


def _recent(h, now, seconds):
    """Confirmed readings of the last `seconds`: the lower of each reading
    and the one before it, so a one-reading spike drops out."""
    pts = list(h['p_hist'])
    return [(t, min(y, y0)) for (_, y0), (t, y) in zip(pts, pts[1:]) if t >= now - seconds]


def _update_baseline(h, now):
    """Median of the raw log10(p) over the baseline window, skipping the
    newest seconds. It may fall but never rise (the chamber only pumps down;
    a slow opening must not drag it up). The margin that counts as movement
    is PRESSURE_MOVE_SIGMAS × the scatter about a straight line (so the
    pump-down drift isn't counted as noise), at least PRESSURE_MOVE_MIN_DEC.
    Only readings since the valve last shut count, so an opening never
    widens the margin."""
    lo = max(now - PRESSURE_BASE_WINDOW_S, h['p_quiet_since'])
    pts = [(t, y) for t, y in h['p_hist'] if lo <= t <= now - PRESSURE_BASE_GUARD_S]
    if len(pts) < PRESSURE_BASE_MIN_S * LABJACK_SAMPLE_HZ:
        return
    ys = sorted(y for _, y in pts)
    n = len(ys)
    med = ys[n // 2] if n % 2 else 0.5 * (ys[n // 2 - 1] + ys[n // 2])
    _, sd = _fit(pts)
    h['p_base'] = med if h['p_base'] is None else min(med, h['p_base'])
    h['p_margin'] = max(PRESSURE_MOVE_MIN_DEC, PRESSURE_MOVE_SIGMAS * sd)


def _cut_lines():
    """log10 of the cut line and the resume line (mbar). They don't need the
    baseline, so they apply from the first reading."""
    cut = math.log10(PRESSURE_MAX_MBAR / PRESSURE_SOAK_FACTOR)
    return cut, cut + math.log10(PRESSURE_RESUME_FRACTION)


def _aim(h, moving_line, cut):
    """log10 of where to aim: P_vacuum_target, raised to the baseline +
    PRESSURE_FLOW_MARGINS margins so that gas visibly flows, and kept below
    the cut line (halfway between movement and cut if it can't be)."""
    aim = max(math.log10(h['p_target_mbar']),
              h['p_base'] + PRESSURE_FLOW_MARGINS * h['p_margin'])
    return aim if aim < cut else 0.5 * (moving_line + cut)


def _freeze_k(rate_c_min):
    """How far below the TC to freeze: the valve body's lag behind the TC at
    this creep rate, at least PRESSURE_FREEZE_BELOW_K."""
    return max(PRESSURE_FREEZE_BELOW_K, rate_c_min * PRESSURE_BODY_LAG_S / 60.0)


def _enter(h, phase, now, temp, msgs, why):
    """Change phase. Leaving a creeping phase for one that must not heat
    further freezes the setpoint below the TC: the TC sits by the heater and
    leads the valve body, the more so the faster it creeps."""
    if h['p_phase'] in ('seek', 'approach') and phase in ('hold', 'trim', 'park', 'cut'):
        h.update(p_sp_before=h['setpoint_C'], p_moved_at=now)
    if h['p_phase'] in ('seek', 'approach') and phase in ('hold', 'trim', 'park'):
        h['setpoint_C'] = min(h['setpoint_C'], temp - _freeze_k(h['p_creeping']))
    h.update(p_phase=phase, p_since=now)
    msgs.append(f"auto-p: {why} · T_sp {h['setpoint_C']:.1f} °C")


def _creep(h, rate, dt, tsp_hi, msgs):
    h['p_rate_c_min'] = h['p_creeping'] = rate
    h['setpoint_C'] = min(tsp_hi, h['setpoint_C'] + rate / 60.0 * dt)
    if h['setpoint_C'] >= tsp_hi and not h['p_capped']:
        h['p_capped'] = True
        msgs.append(f"auto-p: reached the {tsp_hi:g} °C limit with P_vacuum "
                    f"{10 ** h['p_raw']:.2e} mbar — holding there")


def _pressure_outer_loop(h, vac, temp, dt, now, p_up, p_up_t, msgs):
    """Outer loop: P_vacuum → valve temperature setpoint, or heater off.
    Sets h['p_override'] to 0.0 while the heater must be off."""
    y = math.log10(vac)
    tsp_hi = min(PRESSURE_TSP_MAX_C, TEMP_TRIP_C - 5.0)
    if p_up is not None and (p_up_t is None or now - p_up_t > PRESSURE_UP_MAX_AGE_S):
        p_up = None
    h['p_up_bar'] = p_up
    rate = _creep_rate(p_up)

    if h['p_init'] or h['p_filt'] is None:
        h['p_hist'].clear()
        h['p_hist'].append((now, y))
        h.update(p_init=False, p_phase='baseline', p_since=now, p_filt=y, p_raw=y,
                 p_base=None, p_quiet_since=now, p_margin=PRESSURE_MOVE_MIN_DEC, p_override=None,
                 p_capped=False, p_rate_c_min=0.0, p_aim=None, p_creeping=0.0,
                 p_sp_before=None, p_moved_at=None,
                 setpoint_C=min(tsp_hi, temp))
        up = f"{p_up:.2f} bar" if p_up is not None else "not read — slowest creep"
        msgs.append(f"auto-p: no opening point assumed — holding {temp:.1f} °C "
                    f"while the baseline is measured, then creeping {rate:.2f} °C/min "
                    f"(P_up {up}); aim {h['p_target_mbar']:.1e} mbar, heater off above "
                    f"{PRESSURE_MAX_MBAR / PRESSURE_SOAK_FACTOR:.2e} so P_vacuum stays "
                    f"under {PRESSURE_MAX_MBAR:.1e}")
        return

    h['p_raw'] = y
    h['p_filt'] += dt / (PRESSURE_FILTER_S + dt) * (y - h['p_filt'])
    h['p_hist'].append((now, y))
    phase = h['p_phase']
    cut, resume = _cut_lines()
    if h['p_base'] is None or (phase in ('seek', 'park')
                               and y <= h['p_base'] + h['p_margin']):
        first = h['p_base'] is None
        _update_baseline(h, now)        # parked: a baseline taken with the valve
                                        # still open falls once it shuts
        if first and h['p_base'] is not None and h['p_base'] > cut:
            msgs.append(f"auto-p: baseline {10 ** h['p_base']:.2e} mbar is above the "
                        f"cut line {10 ** cut:.2e}: no room for gas flow below "
                        f"P_vacuum_max {PRESSURE_MAX_MBAR:.1e} — heater stays off")
    # Confirmed level: the lower of this reading and the one before, so a
    # one-reading spike moves nothing; a raw reading over a line still cuts.
    yc = min(y, h['p_hist'][-2][1]) if len(h['p_hist']) > 1 else y
    slope, _ = _fit(_recent(h, now, PRESSURE_SLOPE_WINDOW_S))
    predicted = yc + max(0.0, slope) * PRESSURE_PREDICT_S

    # Stopped: past P_vacuum_max the valve opens faster than any heater cut.
    if phase == 'stopped' or vac > PRESSURE_MAX_MBAR:
        if phase != 'stopped':
            _enter(h, 'stopped', now, temp, msgs,
                   f"P_vacuum {vac:.2e} mbar is above P_vacuum_max "
                   f"{PRESSURE_MAX_MBAR:.1e}: the valve opened too abruptly for the "
                   f"heater to hold it. Heater OFF until auto-p is restarted")
        h.update(p_override=0.0, p_rate_c_min=0.0)
        return

    # Cut: above the cut line, or about to be.
    if y > cut or predicted > cut:
        if phase != 'cut':
            why = (f"P_vacuum {vac:.2e} mbar" if y > cut else
                   f"P_vacuum {vac:.2e} mbar rising, predicted {10 ** predicted:.2e} "
                   f"in {PRESSURE_PREDICT_S:g} s")
            _enter(h, 'cut', now, temp, msgs,
                   f"{why} — above the cut line {10 ** cut:.2e} (P_vacuum_max "
                   f"{PRESSURE_MAX_MBAR:.1e} ÷ {PRESSURE_SOAK_FACTOR:g}): heater OFF at "
                   f"{temp:.1f} °C")
        h['p_override'] = 0.0
        h['p_rate_c_min'] = 0.0
        return
    if phase == 'cut':
        if y >= resume:                         # predicted is below the cut line here
            h['p_override'] = 0.0
            return
        h['setpoint_C'] = min(h['setpoint_C'], temp)
        h.update(p_override=None, d_prev=None)
        _enter(h, 'hold', now, temp, msgs,
               f"P_vacuum {vac:.2e} mbar, back below {10 ** resume:.2e} — holding "
               f"{h['setpoint_C']:.1f} °C, no creep until it has not risen for "
               f"{PRESSURE_STEADY_S:g} s")
        return
    h['p_override'] = None
    h['p_rate_c_min'] = 0.0
    if h['p_base'] is None:
        return                                  # still measuring: hold
    moving_line = h['p_base'] + h['p_margin']
    aim = h['p_aim'] = _aim(h, moving_line, cut)

    # Park: the target is too low to open the valve for at all.
    if cut <= moving_line:
        if phase != 'park':
            _enter(h, 'park', now, temp, msgs,
                   f"the cut line ({10 ** cut:.2e} mbar) is not above the baseline "
                   f"+ margin ({10 ** moving_line:.2e}): no room for flow below "
                   f"P_vacuum_max — not heating further")
        return

    if yc <= moving_line:                       # valve shut
        if phase not in ('baseline', 'seek'):
            h['p_quiet_since'] = now
            moved = h['p_moved_at']
            if moved is not None and now - moved <= PRESSURE_FALSE_ALARM_S:
                h['setpoint_C'] = max(h['setpoint_C'], min(h['p_sp_before'], temp))
                what = f"false alarm, back at baseline after {now - moved:.1f} s"
            else:
                what = "P_vacuum back at baseline"
            h['p_moved_at'] = None
            _enter(h, 'seek', now, temp, msgs,
                   f"{what} ({vac:.2e} mbar) — creeping "
                   f"{rate:.2f} °C/min from {h['setpoint_C']:.1f} °C")
        elif phase == 'baseline':
            raised = (f", raised from {h['p_target_mbar']:.1e} so that flow shows"
                      if aim > math.log10(h['p_target_mbar']) + 1e-9 else "")
            _enter(h, 'seek', now, temp, msgs,
                   f"baseline {10 ** h['p_base']:.2e} mbar (moves at "
                   f"{10 ** moving_line:.2e}); aiming at {10 ** aim:.2e}{raised}, "
                   f"cut at {10 ** cut:.2e} — creeping {rate:.2f} °C/min")
        _creep(h, rate, dt, tsp_hi, msgs)
        return

    # The valve is moving.
    rise, _ = _fit(_recent(h, now, PRESSURE_STEADY_S))
    rising = rise * PRESSURE_STEADY_S > h['p_margin']
    slow = PRESSURE_APPROACH_FRACTION * rate
    approach = slow if yc > aim - PRESSURE_NEAR_AIM_DEC else rate
    over = yc > aim + PRESSURE_AIM_BAND_DEC or (phase == 'trim' and yc > aim)
    if over:
        if phase != 'trim':
            _enter(h, 'trim', now, temp, msgs,
                   f"P_vacuum {vac:.2e} mbar, above the aim {10 ** aim:.2e} — "
                   f"easing the setpoint down {slow:.2f} °C/min")
        h['p_rate_c_min'] = -slow
        h['setpoint_C'] = min(h['setpoint_C'],          # never up
                              max(temp - PRESSURE_TRIM_BELOW_K,
                                  h['setpoint_C'] - slow / 60.0 * dt))
        return
    if phase in ('baseline', 'seek', 'trim') or (phase == 'approach' and rising):
        what = {'trim': "fell below the aim",
                'approach': "rising again"}.get(phase, "moved")
        _enter(h, 'hold', now, temp, msgs,
               f"P_vacuum {what} ({vac:.2e} mbar, baseline "
               f"{10 ** h['p_base']:.2e}) at {temp:.1f} °C — creep stopped")
        return
    if phase == 'hold':
        if rising or now - h['p_since'] < PRESSURE_STEADY_S:
            return
        _enter(h, 'approach', now, temp, msgs,
               f"P_vacuum steady at {vac:.2e} mbar for {PRESSURE_STEADY_S:g} s, below "
               f"the aim {10 ** aim:.2e} — creeping {approach:.2f} °C/min")
    _creep(h, approach, dt, tsp_hi, msgs)
