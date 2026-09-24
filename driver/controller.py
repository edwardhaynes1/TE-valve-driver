"""Heater controller — the control law and interlocks, with no threads,
locks, clock, logging, files or hardware.

    new_state()                          a fresh heater state (a dict)
    command(h, now, **kw)                operator commands: armed, mode,
                                         duty_cmd, setpoint_C, p_target_mbar
    step(h, now, dt, temp, tc_healthy, vac, vac_status, vac_healthy,
         p_up, p_up_t, seat_nm, remembered) -> (duty, msgs)   one control step:
                                         interlocks first, then manual /
                                         auto-t / auto-p
    trip(h, reason, msgs)                latch the heater off
    record_edge(h, state, duty, now, note) -> row
                                         ON-time accounting for one gate edge
    take_on_time(h, now) -> seconds      ON time since the previous call
    force_off(h, device_lost)            device-side disarm (connect / lost)
    hold_duty(t_c) -> duty               measured hold power at t_c, as a duty
    opening_point(seat_nm, learned, remembered)
                                         where the valve opens for this torque

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
    PID_KD, PID_KI, PID_KP, PID_SETPOINT_DEFAULT, PRESSURE_BAD_READS_TO_TRIP,
    PRESSURE_BASE_GUARD_S, PRESSURE_BASE_MIN_S, PRESSURE_BASE_WINDOW_S,
    PRESSURE_BURST_ENABLE, PRESSURE_BURST_MARGIN_K, PRESSURE_BURST_MAX_S,
    PRESSURE_BURST_MIN_STEP_K, PRESSURE_COAST_MAX_S, PRESSURE_DEADBAND_DEC,
    PRESSURE_EFOLD_REF_K, PRESSURE_ERR_CLAMP_DEC, PRESSURE_FF_ENABLE,
    PRESSURE_FF_FRACTION, PRESSURE_FF_MAX_ABOVE_K, PRESSURE_FF_REF_ABOVE_K,
    PRESSURE_FF_REF_RISE_MBAR, PRESSURE_FILTER_S, PRESSURE_GAIN_EXP, PRESSURE_HEAT_OPENS,
    PRESSURE_HOLD_SHUT_BELOW_K, PRESSURE_KI, PRESSURE_KP, PRESSURE_MIN_STEP_MBAR,
    PRESSURE_NO_AUTHORITY_S, PRESSURE_OPEN_DEC, PRESSURE_OPEN_FLOOR_BELOW_K,
    PRESSURE_OPEN_LAG_S,
    PRESSURE_SEEK_ABOVE_K, PRESSURE_SEEK_BAND_C, PRESSURE_SEEK_HOLD_S,
    PRESSURE_SEEK_RATE_C_MIN, PRESSURE_SEEK_START_C, PRESSURE_TARGET_DEFAULT,
    PRESSURE_TRIP_ALL_MODES, PRESSURE_TRIP_MBAR, PRESSURE_TSP_MAX_C,
    PRESSURE_TSP_MIN_C, PRESSURE_UP_ENABLE, PRESSURE_UP_FF_MAX_K,
    PRESSURE_UP_FLOW_EXP, PRESSURE_UP_K_PER_BAR, PRESSURE_UP_MAX_AGE_S,
    PRESSURE_UP_MAX_DOWN_K, PRESSURE_UP_MAX_SHIFT_K, PRESSURE_UP_REF_BAR,
    SEAT_SCREW_TOL_NM, SEAT_SCREW_VALVE, TC_MAX_RATE_K_S, TC_RESPONSE_DUTY,
    TC_RESPONSE_MIN_K, TC_RESPONSE_S, TEMP_BURST_ENABLE, TEMP_BURST_LEARN,
    TEMP_BURST_MAX_S, TEMP_BURST_MIN_STEP_K, TEMP_BURST_TAU_MAX_S,
    TEMP_BURST_TAU_MIN_S, TEMP_BURST_TAU_S, TEMP_COAST_MAX_S, TEMP_RATE_FILTER_S,
    TEMP_TRIP_C, VAC_HIGH_STATES, heater_power_w,
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
        # ── pressure (outer) loop ─────────────────────────────────────────────
        p_target_mbar   = PRESSURE_TARGET_DEFAULT,
        p_init          = True,    # re-initialise bumplessly on the next valid read
        p_filt          = None,    # filtered log10(p / mbar)
        p_err           = None,    # last error, decades (target − filtered)
        p_y0            = None,    # filtered log10(p) at loop start (P-on-measurement reference)
        p_t0            = 0.0,     # setpoint when the valve opened, °C
        p_integral      = 0.0,     # decade·s
        p_pinned_since  = None,
        p_pinned_warned = False,
        p_phase         = 'seek',  # 'seek' (valve shut) or 'track' (PI on pressure)
        p_ramping       = False,   # seek: creeping upwards
        p_band_since    = None,    # seek: when the TC first reached the seek band
        p_creep         = 0.0,     # seek: °C added by the creep on top of the goal
        p_goal          = None,    # seek: current goal temperature (for display/logging)
        p_ref           = None,    # opening point at the current upstream pressure, °C
        p_ref_how       = None,    # where p_ref comes from: see _opening_point
        p_efold         = PRESSURE_EFOLD_REF_K,   # flow e-fold used for the gains, K
        p_learned       = None,    # (opening °C at cal. pressure, cal. bar, torque) seen this session
        p_shift         = 0.0,     # upstream-pressure part of p_ref, K
        p_ref_logged    = None,    # last p_ref reported in the event log
        p_up_bar        = None,    # upstream pressure used this step (None if stale)
        p_up_open       = None,    # track: upstream pressure when the valve opened
        p_up_ff         = 0.0,     # track: upstream feedforward on the setpoint, K
        p_burst         = None,    # seek: None, 'burst', 'coast'
        t_check         = False,   # auto-t: re-evaluate a burst (armed / setpoint changed)
        t_burst         = None,    # auto-t: None, 'burst', 'coast'
        t_burst_t0      = None,
        t_burst_peak    = None,
        t_burst_cut     = None,    # (TC at the cut, rate at the cut)
        t_tau           = TEMP_BURST_TAU_S,   # coast rise ÷ rate at the cut, s (learned)
        p_burst_t0      = None,    # when the current burst/coast stage began
        p_burst_peak    = None,    # coast: highest TC seen
        p_override      = None,    # duty the outer loop imposes (burst/coast), else None
        p_seek_capped   = False,   # seek: reached the creep limit
        p_hist          = deque(maxlen=int((PRESSURE_BASE_WINDOW_S + 5) * LABJACK_SAMPLE_HZ * 2)),
        p_base          = None,    # baseline log10(p / mbar), frozen when the valve opens
        p_warned_target = None,    # last target warned about (below what the valve can hold)
    )


def command(h, now, **kwargs):
    """Apply an operator command (arm/disarm, mode, duty, setpoints) to h.

    Arming clears the latched trip and resets both integrators. Disarming
    always succeeds and always wins. The inner (temperature) integrator is
    reset only when switching to or from manual; auto-t ↔ auto-p keeps it,
    since both use the same inner loop and a reset would just cause a sag.
    What auto-p has learned about the opening point is kept for the session."""
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
         vac_healthy=True, p_up=None, p_up_t=None, seat_nm=None, remembered=None):
    """One control step: evaluate all interlocks, then the control law.

    Returns (duty 0-1, event messages). Duty is 0.0 unless every interlock
    is satisfied. Interlocks are checked before the control law, never after."""
    msgs = []
    if not h['armed']:
        return 0.0, msgs
    duty = _interlocks_then_law(h, now, dt, temp, tc_healthy, vac, vac_status,
                                vac_healthy, p_up, p_up_t, seat_nm, msgs, remembered)
    # Response check bookkeeping: when did the current full-power stretch start?
    if h['armed'] and duty >= TC_RESPONSE_DUTY:
        if h['heat_t0'] is None:
            h['heat_t0'], h['heat_T0'] = now, temp
    else:
        h['heat_t0'] = None
    h['duty_last'] = duty
    return duty, msgs


def _interlocks_then_law(h, now, dt, temp, tc_healthy, vac, vac_status,
                         vac_healthy, p_up, p_up_t, seat_nm, msgs, remembered=None):
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
            setpoint = _pressure_outer_loop(h, vac, temp, dt, now, p_up, p_up_t,
                                            seat_nm, msgs, remembered)
        elif h['p_init']:
            return 0.0            # not initialised yet — don't heat on a stale setpoint
        # else: a single missed read — hold the last setpoint / override
        override = h['p_override']
        if override is not None:                # burst / coast: duty set directly
            return max(0.0, min(HEATER_MAX_DUTY, override))

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
            h.update(t_burst='burst', t_burst_t0=now, t_burst_peak=None)
            msgs.append(f"Temperature burst: full power from {temp:.1f} °C toward "
                        f"{setpoint:.1f} °C")
        elif h['t_burst']:
            h['t_burst'] = None
    stage = h['t_burst']
    if stage == 'burst':
        el = now - h['t_burst_t0']
        if _cut_now(h, temp, setpoint):
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


def _pressure_baseline(h, now):
    """Median log10(p) over the baseline window, skipping the newest seconds.
    None until PRESSURE_BASE_MIN_S of samples are available."""
    ys = sorted(y for t, y in h['p_hist']
                if now - PRESSURE_BASE_WINDOW_S <= t <= now - PRESSURE_BASE_GUARD_S)
    need = PRESSURE_BASE_MIN_S * LABJACK_SAMPLE_HZ
    if len(ys) < need:
        return None
    n = len(ys)
    return ys[n // 2] if n % 2 else 0.5 * (ys[n // 2 - 1] + ys[n // 2])


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


def opening_point(seat_nm, learned=None, remembered=None):
    """Where the valve opens for this torque, before any upstream shift:
        (opening_C, at_upstream_bar, efold_K, how, detail)
    how is one of 'learned' (seen this session), 'remembered' (a batch's
    result, openings.py: remembered = (opening °C, upstream bar or None,
    detail) for this torque), 'calibrated' (a table entry), 'interpolated'
    (between two), 'nearest' (outside the table), 'no torque'
    (PRESSURE_SEEK_START_C). at_upstream_bar is None when the upstream
    pressure wasn't read: then no upstream shift is applied."""
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
    if learned is not None and abs(learned[2] - seat_nm) <= SEAT_SCREW_TOL_NM:
        return (learned[0], learned[1], efold, 'learned',
                f"seen opening this session at {seat_nm:.2f} N·m")
    if remembered is not None:
        c, bar, detail = remembered
        return c, bar, efold, 'remembered', detail
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


def _upstream_shift(p_up, ref_bar=PRESSURE_UP_REF_BAR):
    """Opening-point shift for upstream pressure p_up (bar abs.), K. None
    for either pressure: no shift."""
    if not PRESSURE_UP_ENABLE or p_up is None or ref_bar is None:
        return 0.0
    shift = -PRESSURE_UP_K_PER_BAR * (p_up - ref_bar)
    return max(-PRESSURE_UP_MAX_DOWN_K, min(PRESSURE_UP_MAX_SHIFT_K, shift))


def _ref_message(h, seat_nm, open_c, open_bar, detail):
    up = h['p_up_bar']
    upstream = (f"upstream {up:.3f} bar → shift {h['p_shift']:+.1f} K" if up is not None
                else "no upstream reading — no shift")
    at = f"{open_bar:g} bar" if open_bar is not None else "upstream not read"
    return (f"Pressure loop: opening point {h['p_ref']:.1f} °C — {detail} "
            f"({open_c:.1f} °C at {at}), {upstream}")


def _pressure_outer_loop(h, vac, temp, dt, now, p_up, p_up_t, seat_nm, msgs,
                        remembered=None):
    """Outer loop of the cascade: chamber pressure → valve temperature setpoint.

    Everything is relative to the OPENING POINT p_ref: the temperature
    where flow starts, for this seat screw torque (what was seen this
    session, else a batch's remembered result, else SEAT_SCREW_VALVE),
    shifted for upstream pressure.

    SEEK (valve shut). The setpoint jumps to the goal — p_ref, raised a
    little by the feedforward for large targets. From well below it
    (PRESSURE_BURST_MIN_STEP_K), a burst heats at full power and is cut on
    the rate prediction to land PRESSURE_BURST_MARGIN_K short; then the
    setpoint creeps up at PRESSURE_SEEK_RATE_C_MIN until the chamber rises
    PRESSURE_OPEN_DEC above its baseline (measured meanwhile: median of the
    last 30 s, allowed to fall but never to rise). If the target is at or
    below the baseline, the setpoint parks PRESSURE_HOLD_SHUT_BELOW_K below
    p_ref instead.

    TRACK (valve open). PI on log10(p), with target r = log10(target):

        T_sp = T_open + g·(−s·KP·(y − y_open) + s·KI·∫clamp(r − y) dt) + ff_up

      * g = e-fold ÷ PRESSURE_EFOLD_REF_K scales the gains to this torque.
      * ff_up = −PRESSURE_UP_FLOW_EXP · e-fold · ln(P_up / P_up at opening):
        upstream pressure changes are fed forward instead of waited for.
      * Bumpless handover (T_open, y_open at opening); P on the measurement;
        integrator input clamped; output kept between p_ref −
        PRESSURE_OPEN_FLOOR_BELOW_K and the maximum.
      * The TC temperature at opening becomes this session's opening point.

    Returns the temperature setpoint in °C (also written to h)."""
    y      = math.log10(vac)
    s      = 1.0 if PRESSURE_HEAT_OPENS else -1.0
    tsp_hi = min(PRESSURE_TSP_MAX_C, TEMP_TRIP_C - 5.0)
    if p_up is not None and (p_up_t is None or now - p_up_t > PRESSURE_UP_MAX_AGE_S):
        p_up = None
    open_c, open_bar, efold, how, detail = opening_point(seat_nm, h['p_learned'], remembered)
    shift = _upstream_shift(p_up, open_bar)
    h.update(p_ref=open_c + shift, p_ref_how=how, p_efold=efold,
             p_shift=shift, p_up_bar=p_up)
    starting = h['p_init'] or h['p_filt'] is None
    # Reported when it moves: by 0.5 K while seeking, by 2 K once open (where
    # it only sets the floor, and the upstream leak moves it continuously).
    if not starting and (h['p_ref_logged'] is None or abs(h['p_ref'] - h['p_ref_logged'])
                         >= (0.5 if h['p_phase'] == 'seek' else 2.0)):
        h['p_ref_logged'] = h['p_ref']
        msgs.append(_ref_message(h, seat_nm, open_c, open_bar, detail))

    if starting:
        _start_seek(h, y, temp, now, tsp_hi, seat_nm, open_c, open_bar, detail, msgs)
    else:
        h['p_filt'] += dt / (PRESSURE_FILTER_S + dt) * (y - h['p_filt'])
        if h['p_phase'] == 'seek':
            _seek_step(h, y, temp, now, dt, tsp_hi, p_up, seat_nm, msgs, remembered)

    if h['p_phase'] == 'seek':
        return h['setpoint_C']
    return _pressure_track(h, s, tsp_hi, now, dt, p_up, msgs)


def _start_seek(h, y, temp, now, tsp_hi, seat_nm, open_c, open_bar, detail, msgs):
    """(Re)start auto-p: always begin by seeking."""
    ref   = h['p_ref']
    warm  = temp > ref + PRESSURE_SEEK_BAND_C
    start = min(tsp_hi, max(ref, temp))
    h['p_hist'].clear()
    h['p_hist'].append((now, y))
    burst = PRESSURE_BURST_ENABLE and start - temp >= PRESSURE_BURST_MIN_STEP_K
    h.update(p_init=False, p_phase='seek', p_filt=y, p_y0=y, p_t0=start,
             p_err=None, p_integral=0.0, setpoint_C=start,
             p_ramping=warm, p_band_since=None, p_creep=0.0, p_goal=start,
             p_burst='burst' if burst else None, p_burst_t0=now,
             p_burst_peak=None,
             p_override=HEATER_MAX_DUTY if burst else None,
             p_seek_capped=False, p_base=None, p_warned_target=None,
             p_up_open=None, p_up_ff=0.0,
             p_pinned_since=None, p_pinned_warned=False)
    h['p_ref_logged'] = ref
    msgs.append(_ref_message(h, seat_nm, open_c, open_bar, detail))
    if burst:
        msgs.append(f"Pressure loop: full power from {temp:.1f} °C, cut to land "
                    f"{PRESSURE_BURST_MARGIN_K:g} K below the goal (≥ {start:.1f} °C, "
                    f"set from the target once the baseline is known), coast, then "
                    f"+{PRESSURE_SEEK_RATE_C_MIN * _gain_scale(h):.1f} °C/min until the "
                    f"valve opens")
    else:
        msgs.append(f"Pressure loop: seeking — T_sp {start:.1f} °C, then "
                    f"+{PRESSURE_SEEK_RATE_C_MIN * _gain_scale(h):.1f} °C/min until the "
                    f"valve opens")
    if warm:
        msgs.append(f"Pressure loop: valve already at {temp:.1f} °C, above its opening "
                    f"point — if it is open, the baseline will include flow. Let it cool "
                    f"below {ref:.1f} °C for a clean baseline")


def _seek_step(h, y, temp, now, dt, tsp_hi, p_up, seat_nm, msgs, remembered=None):
    """One SEEK step: baseline, opening detection, parking, burst, creep."""
    h['p_hist'].append((now, y))
    base = _pressure_baseline(h, now)
    tsp  = h['setpoint_C']
    if base is not None and h['p_base'] is not None:
        # The chamber only pumps down, so the baseline may fall but never
        # rise — otherwise a gradual opening drags it upwards and is never
        # detected.
        base = min(base, h['p_base'])
    if base is not None:
        h['p_base'] = base
        h['p_err']  = math.log10(h['p_target_mbar']) - h['p_filt']
        _check_target(h, msgs)

    if base is not None and h['p_filt'] - base > PRESSURE_OPEN_DEC:
        # Opened — freeze the baseline, remember where, hand over bumplessly.
        if h['p_burst']:
            _end_burst(h, msgs, f"valve opened during {h['p_burst']}")
        h.update(p_phase='track', p_y0=h['p_filt'], p_t0=tsp,
                 p_integral=0.0, p_ramping=False, p_up_open=p_up, p_up_ff=0.0)
        note = ""
        if seat_nm is not None:
            table = h['p_ref'] if h['p_ref_how'] != 'learned' else None
            src = "remembered" if h['p_ref_how'] == 'remembered' else "table"
            # The valve lags the TC: on a rising TC it opened a little earlier.
            seen = min(temp, temp - PRESSURE_OPEN_LAG_S * h['rate'])
            _, open_bar, _, _, _ = opening_point(seat_nm, None, remembered)
            h['p_learned'] = (seen - h['p_shift'], open_bar, seat_nm)   # at the calibration pressure
            h['p_ref'] = h['p_ref_logged'] = seen
            note = (f"; this session's opening point for {seat_nm:.2f} N·m is now "
                    f"{seen:.1f} °C" + (f" ({src}: {table:.1f} °C)" if table is not None else ""))
        msgs.append(f"Pressure loop: valve opened at T_sp {tsp:.2f} °C "
                    f"(TC {temp:.2f} °C), baseline {10 ** base:.2e} mbar — "
                    f"now controlling to {h['p_target_mbar']:.2e} mbar{note}")
    elif base is not None and h['p_target_mbar'] <= 10 ** base:
        # Target at/below baseline: opening would only move away from it,
        # so park safely below the opening point (resumes if raised).
        if h['p_burst']:
            h.update(p_burst=None, p_override=None)
        h.update(setpoint_C=min(tsp, h['p_ref'] - PRESSURE_HOLD_SHUT_BELOW_K),
                 p_creep=0.0, p_ramping=False, p_band_since=None)
    else:
        goal = min(tsp_hi, _seek_goal(h))
        if h['p_goal'] is None or abs(goal - h['p_goal']) > 0.05:
            if base is not None:
                msgs.append(f"Pressure loop: seek goal {goal:.1f} °C for target "
                            f"{h['p_target_mbar']:.2e} mbar (baseline "
                            f"{10 ** base:.2e})")
            h['p_goal'] = goal
        tsp = min(tsp_hi, goal + h['p_creep'])
        if h['p_burst']:
            _burst_step(h, temp, tsp, now, msgs)
        else:
            if not h['p_ramping'] and temp >= tsp - PRESSURE_SEEK_BAND_C:
                if h['p_band_since'] is None:
                    h['p_band_since'] = now
            if (not h['p_ramping'] and h['p_band_since'] is not None
                    and now - h['p_band_since'] >= PRESSURE_SEEK_HOLD_S):
                h['p_ramping'] = True
                msgs.append(f"Pressure loop: at {temp:.1f} °C, valve shut — "
                            f"creeping +{PRESSURE_SEEK_RATE_C_MIN * _gain_scale(h):.1f} °C/min")
            if h['p_ramping']:
                cap = min(max(h['p_ref'] + PRESSURE_SEEK_ABOVE_K, goal), tsp_hi)
                h['p_creep'] += PRESSURE_SEEK_RATE_C_MIN * _gain_scale(h) / 60.0 * dt
                tsp = min(cap, goal + h['p_creep'])
                if tsp >= cap and not h['p_seek_capped']:
                    h['p_seek_capped'] = True
                    msgs.append(f"Pressure loop: reached {cap:.1f} °C and the "
                                f"valve has not opened — holding there. Its opening "
                                f"point may be higher than {h['p_ref']:.1f} °C: check "
                                f"the seat screw torque")
        h['setpoint_C'] = tsp


def _seek_goal(h):
    """Seek goal: the opening point, raised by the feedforward for targets
    well above the baseline (scaled to this torque's e-fold and the
    upstream pressure), by at most PRESSURE_FF_MAX_ABOVE_K."""
    goal = h['p_ref']
    if PRESSURE_FF_ENABLE and h['p_base'] is not None:
        rise = h['p_target_mbar'] - 10 ** h['p_base']
        if rise > 0:
            up = h['p_up_bar'] or PRESSURE_UP_REF_BAR
            rise_at_ref = rise * (PRESSURE_UP_REF_BAR / up) ** PRESSURE_UP_FLOW_EXP
            above = PRESSURE_FF_REF_ABOVE_K + h['p_efold'] * math.log(
                PRESSURE_FF_FRACTION * rise_at_ref / PRESSURE_FF_REF_RISE_MBAR)
            goal += min(PRESSURE_FF_MAX_ABOVE_K, max(0.0, above))
    return goal


def _burst_step(h, temp, tsp, now, msgs):
    """Advance the seek's burst/coast sequence toward setpoint tsp."""
    stage = h['p_burst']
    aim = tsp - PRESSURE_BURST_MARGIN_K
    if stage == 'burst':
        h['p_override'] = HEATER_MAX_DUTY
        if _cut_now(h, temp, aim):
            msgs.append(f"Pressure loop: burst done after {now - h['p_burst_t0']:.1f} s "
                        f"at {temp:.1f} °C, rising {h['rate']:.2f} °C/s (goal "
                        f"{tsp:.1f} °C) — coasting")
            h.update(p_burst='coast', p_burst_t0=now, p_burst_peak=temp, p_override=0.0,
                     t_burst_cut=(temp, h['rate']))
        elif now - h['p_burst_t0'] > PRESSURE_BURST_MAX_S:
            _end_burst(h, msgs, f"burst time limit ({PRESSURE_BURST_MAX_S:g} s) "
                                f"reached at {temp:.1f} °C — check the thermocouple")
    elif stage == 'coast':
        h['p_override'] = 0.0
        h['p_burst_peak'] = max(h['p_burst_peak'], temp)
        past_peak = temp < h['p_burst_peak'] - 0.3        # TC noise ~0.05 K
        if past_peak or temp >= tsp - PRESSURE_SEEK_BAND_C \
                or now - h['p_burst_t0'] > PRESSURE_COAST_MAX_S:
            note = _learn_tau(h, h['t_burst_cut'], h['p_burst_peak']) if past_peak else ""
            _end_burst(h, msgs, f"coast done, TC peaked at {h['p_burst_peak']:.1f} °C{note}")


def _end_burst(h, msgs, why):
    """Hand heating back to feedforward + PI."""
    h.update(p_burst=None, p_override=None, d_prev=None)
    msgs.append(f"Pressure loop: {why} — temperature control resumes")


def _check_target(h, msgs):
    """Warn once per target value if the valve cannot hold it
    (needs a measured baseline)."""
    tgt = h['p_target_mbar']
    if h['p_base'] is None or h['p_warned_target'] == tgt:
        return
    base   = 10 ** h['p_base']
    lowest = base + PRESSURE_MIN_STEP_MBAR
    if tgt < lowest:
        h['p_warned_target'] = tgt
        if tgt <= base and h['p_phase'] == 'seek':
            msgs.append(f"Pressure loop: target {tgt:.2e} mbar is at or below the "
                        f"chamber baseline {base:.2e} mbar — keeping the valve shut "
                        f"at {h['p_ref'] - PRESSURE_HOLD_SHUT_BELOW_K:.1f} °C. "
                        f"The lowest holdable pressure is ≈ {lowest:.1e} mbar")
        else:
            msgs.append(f"Pressure loop: target {tgt:.2e} mbar is below the lowest "
                        f"pressure the valve can hold once open (baseline {base:.2e} + "
                        f"step ~{PRESSURE_MIN_STEP_MBAR:.1e} ≈ {lowest:.1e} mbar) — "
                        f"expect it to sit above target at the "
                        f"{h['p_ref'] - PRESSURE_OPEN_FLOOR_BELOW_K:.1f} °C floor")


def _gain_scale(h):
    """How much harder than the 16 Sept valve this torque's valve must be
    driven, in temperature, for the same change in flow."""
    return (h['p_efold'] / PRESSURE_EFOLD_REF_K) ** PRESSURE_GAIN_EXP


def _pressure_track(h, s, tsp_hi, now, dt, p_up, msgs):
    """TRACK step of the outer loop."""
    tsp_lo = max(PRESSURE_TSP_MIN_C, h['p_ref'] - PRESSURE_OPEN_FLOOR_BELOW_K)
    g   = _gain_scale(h)
    r   = math.log10(h['p_target_mbar'])
    err = r - h['p_filt']
    h['p_err'] = err
    _check_target(h, msgs)

    # Upstream feedforward: hold the last value while the Keller is stale.
    if PRESSURE_UP_ENABLE and p_up is not None and h['p_up_open']:
        ff = -PRESSURE_UP_FLOW_EXP * h['p_efold'] * math.log(p_up / h['p_up_open'])
        h['p_up_ff'] = max(-PRESSURE_UP_FF_MAX_K, min(PRESSURE_UP_FF_MAX_K, ff))
    elif h['p_up_open'] is None and p_up is not None:
        h['p_up_open'] = p_up                 # opened while the Keller was stale

    e_i = 0.0 if abs(err) < PRESSURE_DEADBAND_DEC else err
    e_i = max(-PRESSURE_ERR_CLAMP_DEC, min(PRESSURE_ERR_CLAMP_DEC, e_i))

    def raw_for(integral):
        return (h['p_t0'] + h['p_up_ff']
                + g * (-s * PRESSURE_KP * (h['p_filt'] - h['p_y0'])
                       + s * PRESSURE_KI * integral))

    integral = h['p_integral'] + e_i * dt
    raw      = raw_for(integral)
    push     = s * e_i                         # >0: integrator raising T_sp
    if (raw > tsp_hi and push > 0) or (raw < tsp_lo and push < 0):
        integral = h['p_integral']             # don't wind further into a clamp
        raw      = raw_for(integral)
    h['p_integral'] = integral
    tsp = max(tsp_lo, min(tsp_hi, raw))
    h['setpoint_C'] = tsp

    # Authority check — is the loop asking for more than it can have?
    at_hi = tsp >= tsp_hi and push > 0
    at_lo = tsp <= tsp_lo and push < 0
    if at_hi or at_lo:
        if h['p_pinned_since'] is None:
            h['p_pinned_since'] = now
        elif (not h['p_pinned_warned']
              and now - h['p_pinned_since'] > PRESSURE_NO_AUTHORITY_S):
            h['p_pinned_warned'] = True
            where = "below" if err > 0 else "above"
            limit = (f"maximum ({tsp_hi:.0f} °C)" if at_hi else
                     f"floor ({tsp_lo:.1f} °C, kept to hold the valve open)")
            msgs.append(f"Pressure loop: setpoint held at {limit} for "
                        f"{PRESSURE_NO_AUTHORITY_S/60:.0f} min and pressure is still "
                        f"{where} target")
    else:
        h['p_pinned_since']  = None
        h['p_pinned_warned'] = False
    return tsp
