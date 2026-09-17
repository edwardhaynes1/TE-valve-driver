"""Heater controller — the control law and interlocks, with no threads,
locks, clock, logging, files or hardware.

    new_state()                          a fresh heater state (a dict)
    command(h, now, **kw)                operator commands: armed, mode,
                                         duty_cmd, setpoint_C, p_target_mbar
    step(h, now, dt, temp, tc_healthy, vac, vac_status, vac_healthy,
         p_up, p_up_t) -> (duty, msgs)   one control step: interlocks first,
                                         then manual / auto-t / auto-p
    trip(h, reason, msgs)                latch the heater off
    record_edge(h, state, duty, now, note) -> row
                                         ON-time accounting for one gate edge
    take_on_time(h, now) -> seconds      ON time since the previous call
    force_off(h, device_lost)            device-side disarm (connect / lost)

Everything it needs comes in as arguments: the state `h` (changed in place),
the time `now`, and the readings. Messages for the event log come back in
`msgs`. control.py wraps this for the threads; tests call it directly.
"""

import math
from collections import deque
from datetime import datetime

from .config import (
    HEATER_HOLD_AMBIENT_C, HEATER_HOLD_DUTY_40C, HEATER_LAG_S,
    HEATER_MAX_DUTY, HEATER_MAX_RUN_S, LABJACK_SAMPLE_HZ, PID_D_FILTER_S,
    PID_KD, PID_KI, PID_KP, PID_SETPOINT_DEFAULT, PRESSURE_BAD_READS_TO_TRIP,
    PRESSURE_BASE_GUARD_S, PRESSURE_BASE_MIN_S, PRESSURE_BASE_WINDOW_S,
    PRESSURE_BURST_BRAKE_K, PRESSURE_BURST_ENABLE, PRESSURE_BURST_MAX_S,
    PRESSURE_BURST_MAX_START_C, PRESSURE_COAST_MAX_S, PRESSURE_DEADBAND_DEC,
    PRESSURE_ERR_CLAMP_DEC, PRESSURE_FF_EFOLD_K, PRESSURE_FF_ENABLE,
    PRESSURE_FF_FRACTION, PRESSURE_FF_MAX_C, PRESSURE_FF_REF_C,
    PRESSURE_FF_REF_RISE_MBAR, PRESSURE_FILTER_S, PRESSURE_HEAT_OPENS,
    PRESSURE_HOLD_SHUT_C, PRESSURE_KI, PRESSURE_KP, PRESSURE_MIN_STEP_MBAR,
    PRESSURE_NO_AUTHORITY_S, PRESSURE_OPEN_DEC, PRESSURE_OPEN_FLOOR_C,
    PRESSURE_SEEK_BAND_C, PRESSURE_SEEK_HOLD_S, PRESSURE_SEEK_MAX_C,
    PRESSURE_SEEK_RATE_C_MIN, PRESSURE_SEEK_START_C, PRESSURE_TARGET_DEFAULT,
    PRESSURE_TRIP_ALL_MODES, PRESSURE_TRIP_MBAR, PRESSURE_TSP_MAX_C,
    PRESSURE_TSP_MIN_C, PRESSURE_UP_ENABLE, PRESSURE_UP_K_PER_BAR,
    PRESSURE_UP_MAX_AGE_S, PRESSURE_UP_MAX_SHIFT_K, PRESSURE_UP_REF_BAR,
    TEMP_BURST_BRAKE_K, TEMP_BURST_ENABLE, TEMP_BURST_LEARN, TEMP_BURST_MAX_S,
    TEMP_BURST_MIN_STEP_K, TEMP_COAST_MAX_S, TEMP_TRIP_C, VAC_HIGH_STATES,
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
    live electrical readout the device thread writes. One dict, so the
    GUI and logger can read it under a single lock."""
    return HeaterState(
        armed        = False,      # operator has armed the heater
        mode         = MANUAL,     # one of MODES
        duty_cmd     = 0.0,        # commanded duty in manual mode, 0-1
        setpoint_C   = PID_SETPOINT_DEFAULT,
        duty_actual  = 0.0,        # what the output is actually doing right now
        armed_at     = None,       # time.time() when armed
        trip_reason  = None,       # non-None = latched trip, needs re-arm
        integral     = 0.0,        # PI integral term
        d_prev       = None,       # last valve temperature seen by the D term
        d_filt       = 0.0,        # filtered dT/dt, °C/s
        # ── live electrical readout (written by the device thread) ────────────
        out_high     = False,      # FIO0 state right now
        v_now        = 0.0,        # calculated element voltage right now, V
        i_now        = 0.0,        # calculated element current right now, A
        rail_meas    = None,       # measured supply (HEATER_V_AIN), V
        v_meas       = None,       # measured element voltage right now, V
        i_meas       = None,       # measured element current right now, A
        v_meas_mean  = None,       # ... averaged over one switching period
        i_meas_mean  = None,
        p_meas_mean  = None,       # measured power, mean of per-tick V × I over one period, W
        on_since     = None,       # time.time() the gate last went ON (None while OFF)
        on_acc_from  = None,       # start of the not-yet-counted part of the current ON time
        on_time_acc  = 0.0,        # ON seconds since the last CSV row (logger resets it)
        # ── pressure (outer) loop ─────────────────────────────────────────────
        p_target_mbar   = PRESSURE_TARGET_DEFAULT,
        p_init          = True,    # re-initialise bumplessly on the next valid read
        p_filt          = None,    # filtered log10(p / mbar)
        p_err           = None,    # last error, decades (target − filtered)
        p_y0            = None,    # filtered log10(p) at loop start (P-on-measurement reference)
        p_t0            = 0.0,     # setpoint at loop start, °C
        p_integral      = 0.0,     # decade·s
        p_pinned_since  = None,
        p_pinned_warned = False,
        p_phase         = 'seek',  # 'seek' (valve shut) or 'track' (PI on pressure)
        p_ramping       = False,   # seek: creeping upwards
        p_band_since    = None,    # seek: when the TC first reached the seek band
        p_creep         = 0.0,     # seek: °C added by the creep on top of the goal
        p_goal          = None,    # seek: current goal temperature (for display/logging)
        p_shift         = 0.0,     # upstream-pressure shift applied to all temperatures, K
        p_shift_logged  = None,    # last shift reported in the event log
        p_up_bar        = None,    # upstream pressure the shift was computed from
        p_burst         = None,    # seek: None, 'burst', 'coast'
        t_check         = False,   # auto: re-evaluate a burst (armed / setpoint changed)
        t_burst         = None,    # auto: None, 'burst', 'coast'
        t_burst_t0      = None,
        t_burst_peak    = None,
        t_burst_cut     = None,    # auto: (TC at cut, heater charge fraction at cut)
        t_brake         = TEMP_BURST_BRAKE_K,
        p_burst_t0      = None,    # when the current burst/coast stage began
        p_burst_peak    = None,    # coast: highest TC seen
        p_override      = None,    # duty the outer loop imposes (burst/coast), else None
        p_seek_capped   = False,   # seek: reached PRESSURE_SEEK_MAX_C
        p_hist          = deque(maxlen=int((PRESSURE_BASE_WINDOW_S + 5) * LABJACK_SAMPLE_HZ * 2)),
        p_base          = None,    # baseline log10(p / mbar), frozen when the valve opens
        p_warned_target = None,    # last target warned about (below what the valve can hold)
    )


def command(h, now, **kwargs):
    """Apply an operator command (arm/disarm, mode, duty, setpoints) to h.

    Arming clears the latched trip and resets both integrators. Disarming
    always succeeds and always wins. The inner (temperature) integrator is
    reset only when switching to or from manual; auto ↔ pressure keeps it,
    since both use the same inner loop and a reset would just cause a sag."""
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
    is satisfied. Interlocks are checked
    before the control law, never after."""
    msgs     = []
    armed    = h['armed']
    mode     = h['mode']
    duty_cmd = h['duty_cmd']
    setpoint = h['setpoint_C']
    armed_at = h['armed_at']
    p_init   = h['p_init']

    if not armed:
        return 0.0, msgs

    # Interlock 1 — no trustworthy temperature means no heat.
    if not tc_healthy or temp is None:
        trip(h, "thermocouple unavailable or faulted", msgs)
        return 0.0, msgs

    # Interlock 2 — over-temperature.
    if temp > TEMP_TRIP_C:
        trip(h, f"over-temperature {temp:.1f} °C > {TEMP_TRIP_C:.0f} °C", msgs)
        return 0.0, msgs

    # Interlock 3 — maximum unattended run time.
    if armed_at is not None and (now - armed_at) > HEATER_MAX_RUN_S:
        trip(h, f"maximum armed time ({HEATER_MAX_RUN_S/60:.0f} min) reached", msgs)
        return 0.0, msgs

    # Interlock 4 — chamber over-pressure.
    if mode == AUTO_P or PRESSURE_TRIP_ALL_MODES:
        if vac_status in VAC_HIGH_STATES:
            trip(h, f"chamber pressure too high to measure ({vac_status})", msgs)
            return 0.0, msgs
        if vac is not None and vac > PRESSURE_TRIP_MBAR:
            trip(h, f"chamber over-pressure {vac:.2e} mbar > "
                    f"{PRESSURE_TRIP_MBAR:.0e} mbar", msgs)
            return 0.0, msgs

    if mode == MANUAL:
        return max(0.0, min(HEATER_MAX_DUTY, duty_cmd)), msgs

    if mode == AUTO_P:
        # Interlock 5 — pressure mode is blind without a live gauge.
        if not vac_healthy:
            trip(h, f"no valid chamber pressure for {PRESSURE_BAD_READS_TO_TRIP} "
                    f"reads ({vac_status or 'no reading'}) — pressure mode "
                    f"needs a live gauge", msgs)
            return 0.0, msgs
        if vac is not None:
            setpoint = _pressure_outer_loop(h, vac, temp, dt, now, p_up, p_up_t, msgs)
        elif p_init:
            return 0.0, msgs      # not initialised yet — don't heat on a stale setpoint
        # else: a single missed read — hold the last setpoint / override
        override = h['p_override']
        if override is not None:                # burst / coast: duty set directly
            return max(0.0, min(HEATER_MAX_DUTY, override)), msgs

    if mode == AUTO_T:
        burst_duty = _temp_burst_step(h, temp, setpoint, now, msgs)
        if burst_duty is not None:
            return max(0.0, min(HEATER_MAX_DUTY, burst_duty)), msgs

    # ── auto-t / auto-p: PI(D) on valve temperature, with anti-windup ──────
    error = setpoint - temp
    prev = h['d_prev']
    if prev is None or PID_KD == 0.0:
        h['d_filt'] = 0.0
    else:
        rate = (temp - prev) / dt
        h['d_filt'] += dt / (PID_D_FILTER_S + dt) * (rate - h['d_filt'])
    h['d_prev'] = temp
    integral = h['integral'] + error * dt
    duty     = (PID_KP * error + PID_KI * integral
                - PID_KD * h['d_filt'])
    clamped  = max(0.0, min(HEATER_MAX_DUTY, duty))
    # Only accumulate when not saturated — keeps the integrator honest.
    if duty == clamped:
        h['integral'] = integral
    return clamped, msgs


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


# Live electrical readout fields, written by the device thread.
ELECTRICAL = ('out_high', 'v_now', 'i_now', 'rail_meas', 'v_meas', 'i_meas',
              'v_meas_mean', 'i_meas_mean', 'p_meas_mean')


def force_off(h, device_lost=False):
    """Heater off and disarmed by the device thread (not the operator): on
    connecting, and — with device_lost — when the LabJack goes away, which
    also restarts auto-p and clears the electrical readout."""
    h['armed'] = False
    h['duty_cmd'] = 0.0
    h['duty_actual'] = 0.0
    if device_lost:
        h['p_init'] = True
        h.update(out_high=False, v_now=0.0, i_now=0.0, rail_meas=None,
                 v_meas=None, i_meas=None, v_meas_mean=None,
                 i_meas_mean=None, p_meas_mean=None)


def take_on_time(h, now):
    """Gate ON seconds since the previous call (for one log row)."""
    on_s = h['on_time_acc']
    if h['on_acc_from'] is not None:
        on_s += now - h['on_acc_from']
        h['on_acc_from'] = now
    h['on_time_acc'] = 0.0
    return on_s


def _hold_integral(t_sp):
    """Integrator value that makes the PI output roughly the duty that holds
    t_sp — used to hand over smoothly after a burst."""
    hold = HEATER_HOLD_DUTY_40C * max(0.0, t_sp - HEATER_HOLD_AMBIENT_C) / max(
        1.0, 40.0 - HEATER_HOLD_AMBIENT_C)
    return (min(HEATER_MAX_DUTY, hold) / PID_KI) if PID_KI > 0 else 0.0


def _temp_burst_step(h, temp, setpoint, now, msgs):
    """Auto mode: run the burst/coast sequence if one is due.
    Returns the duty to impose, or None to let the PI run."""
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
        el     = now - h['t_burst_t0']
        charge = 1.0 - math.exp(-el / HEATER_LAG_S)
        if temp >= setpoint - h['t_brake'] * charge:
            msgs.append(f"Temperature burst: cut after {el:.1f} s at {temp:.1f} °C "
                        f"(brake {h['t_brake']:.1f} K) — coasting")
            h.update(t_burst='coast', t_burst_t0=now, t_burst_peak=temp,
                     t_burst_cut=(temp, charge))
            duty = 0.0
        elif el > TEMP_BURST_MAX_S:
            h.update(t_burst=None, d_prev=None, integral=_hold_integral(setpoint))
            msgs.append(f"Temperature burst: time limit ({TEMP_BURST_MAX_S:g} s) at "
                        f"{temp:.1f} °C — PI resumes; check the thermocouple")
        else:
            duty = HEATER_MAX_DUTY
    elif stage == 'coast':
        # Heater stays off until the TC peaks, even if it passes the
        # setpoint (the PI would be near zero there anyway), so the whole
        # rise is measured.
        h['t_burst_peak'] = max(h['t_burst_peak'], temp)
        past_peak = temp < h['t_burst_peak'] - 0.3
        if past_peak or now - h['t_burst_t0'] > TEMP_COAST_MAX_S:
            note = ""
            cut_T, charge = h['t_burst_cut']
            if past_peak and charge > 0.3:
                measured = (h['t_burst_peak'] - cut_T) / charge
                h['t_brake'] = min(10.0, max(1.0, (1 - TEMP_BURST_LEARN) * h['t_brake']
                                             + TEMP_BURST_LEARN * measured))
                note = f", brake now {h['t_brake']:.1f} K"
            h.update(t_burst=None, d_prev=None, integral=_hold_integral(setpoint))
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


def _pressure_outer_loop(h, vac, temp, dt, now, p_up, p_up_t, msgs):
    """Outer loop of the cascade: chamber pressure → valve temperature setpoint.

    SEEK (valve shut). The setpoint jumps to PRESSURE_SEEK_START_C, at or just
    below the cracking point, so the inner loop heats at full speed. Once the
    TC is there (and, optionally, has stayed there PRESSURE_SEEK_HOLD_S), the
    setpoint creeps up at PRESSURE_SEEK_RATE_C_MIN. If the target is at or
    below the baseline, the setpoint waits at PRESSURE_HOLD_SHUT_C instead. Meanwhile the
    chamber baseline is measured (median over the last 30 s, allowed to fall
    with the pump-down but never to rise). When the
    filtered pressure rises PRESSURE_OPEN_DEC above that baseline, the valve
    has opened: the baseline is frozen and control passes to TRACK. Creeping
    slowly keeps the setpoint from running far ahead of the lagging valve
    body, which is what would otherwise cause an overshoot on opening.

    TRACK (valve open). PI on log10(p), because chamber pressure moves in
    decades, with target r = log10(target):

        T_sp = T_open − s·KP·(y − y_open) + s·KI·∫clamp(r − y) dt

    with y the filtered log10(p) and s = ±1 from PRESSURE_HEAT_OPENS.
      * Bumpless handover: T_open and y_open are the values at opening.
      * P acts on the measurement, not the error, so changing the target
        causes no setpoint kick — only the integrator responds to it.
      * The integrator input is clamped to ±PRESSURE_ERR_CLAMP_DEC, which caps
        how fast the setpoint can ramp without a separate slew limiter.
      * Output clamped to PRESSURE_OPEN_FLOOR_C … max, so the loop trims flow
        above the valve's minimum step instead of shutting it; the integrator
        stops only when it would push further into a clamp.

    p_up is the upstream pressure (bar abs.) and p_up_t when it was read.

    Returns the temperature setpoint in °C (also written to h)."""
    y      = math.log10(vac)
    s      = 1.0 if PRESSURE_HEAT_OPENS else -1.0
    tsp_hi = min(PRESSURE_TSP_MAX_C, TEMP_TRIP_C - 5.0)
    if p_up is not None and (p_up_t is None or now - p_up_t > PRESSURE_UP_MAX_AGE_S):
        p_up = None
    shift = _upstream_shift(p_up)

    h['p_shift'], h['p_up_bar'] = shift, p_up
    starting = h['p_init'] or h['p_filt'] is None     # start-up logs it itself
    if PRESSURE_UP_ENABLE and not starting and (
            h['p_shift_logged'] is None or abs(shift - h['p_shift_logged']) >= 0.5):
        h['p_shift_logged'] = shift
        msgs.append(f"Pressure loop: upstream {p_up:.3f} bar → temperatures shifted "
                    f"{shift:+.1f} K" if p_up is not None else
                    "Pressure loop: no upstream pressure reading — no upstream shift")

    # ── (re)start: always begin by seeking ────────────────────────────
    if h['p_init'] or h['p_filt'] is None:
        warm  = temp > PRESSURE_SEEK_START_C + shift + PRESSURE_SEEK_BAND_C
        start = min(tsp_hi, max(PRESSURE_SEEK_START_C + shift, temp))
        h['p_hist'].clear()
        h['p_hist'].append((now, y))
        burst = PRESSURE_BURST_ENABLE and temp <= PRESSURE_BURST_MAX_START_C
        h.update(p_init=False, p_phase='seek', p_filt=y, p_y0=y, p_t0=start,
                 p_err=None, p_integral=0.0, setpoint_C=start,
                 p_ramping=warm, p_band_since=None, p_creep=0.0, p_goal=start,
                 p_burst='burst' if burst else None, p_burst_t0=now,
                 p_burst_peak=None,
                 p_override=HEATER_MAX_DUTY if burst else None,
                 p_seek_capped=False, p_base=None,
                 p_warned_target=None,
                 p_pinned_since=None, p_pinned_warned=False)
        if PRESSURE_UP_ENABLE:
            msgs.append("Pressure loop: upstream "
                        + (f"{p_up:.3f} bar → temperatures shifted {shift:+.1f} K"
                           if p_up is not None else "pressure unavailable — no shift"))
            h['p_shift_logged'] = shift
        if h['p_burst']:
            msgs.append(f"Pressure loop: full power until "
                        f"{PRESSURE_BURST_BRAKE_K:g} K below the goal (≥ {start:.1f} °C, "
                        f"set from the target once the baseline is known), coast, then "
                        f"+{PRESSURE_SEEK_RATE_C_MIN:g} °C/min until the valve opens")
        else:
            msgs.append(f"Pressure loop: seeking — T_sp {start:.1f} °C, then "
                        f"+{PRESSURE_SEEK_RATE_C_MIN:g} °C/min until the valve opens")
        if warm:
            msgs.append(f"Pressure loop: valve already at {temp:.1f} °C — if it is "
                        f"open, the baseline will include flow. Let it cool below "
                        f"{PRESSURE_SEEK_START_C + shift:.1f} °C for a clean baseline")
    else:
        h['p_filt'] += dt / (PRESSURE_FILTER_S + dt) * (y - h['p_filt'])

        if h['p_phase'] == 'seek':
            h['p_hist'].append((now, y))
            base = _pressure_baseline(h, now)
            tsp  = h['setpoint_C']
            if base is not None and h['p_base'] is not None:
                # The chamber only pumps down, so the baseline may fall but
                # never rise — otherwise a gradual opening drags it upwards
                # and is never detected.
                base = min(base, h['p_base'])
            if base is not None:
                h['p_base'] = base
                h['p_err']  = math.log10(h['p_target_mbar']) - h['p_filt']
                _check_target(h, msgs)

            if base is not None and h['p_filt'] - base > PRESSURE_OPEN_DEC:
                # Opened — freeze the baseline, hand over bumplessly.
                if h['p_burst']:
                    _end_burst(h, tsp, msgs, f"valve opened during {h['p_burst']}")
                h.update(p_phase='track', p_y0=h['p_filt'], p_t0=tsp,
                         p_integral=0.0, p_ramping=False)
                msgs.append(f"Pressure loop: valve opened at T_sp {tsp:.2f} °C "
                            f"(TC {temp:.2f} °C), baseline {10 ** base:.2e} mbar — "
                            f"now controlling to {h['p_target_mbar']:.2e} mbar")
            elif base is not None and h['p_target_mbar'] <= 10 ** base:
                # Target at/below baseline: opening would only move away from
                # it, so park safely below the cracking point (resumes if raised).
                if h['p_burst']:
                    h.update(p_burst=None, p_override=None)
                h.update(setpoint_C=min(tsp, PRESSURE_HOLD_SHUT_C + shift), p_creep=0.0,
                         p_ramping=False, p_band_since=None)
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
                                    f"creeping +{PRESSURE_SEEK_RATE_C_MIN:g} °C/min")
                    if h['p_ramping']:
                        cap = min(max(PRESSURE_SEEK_MAX_C, goal), tsp_hi)
                        h['p_creep'] += PRESSURE_SEEK_RATE_C_MIN / 60.0 * dt
                        tsp = min(cap, goal + h['p_creep'])
                        if tsp >= cap and not h['p_seek_capped']:
                            h['p_seek_capped'] = True
                            msgs.append(f"Pressure loop: reached {cap:g} °C and the "
                                        f"valve has not opened — holding there")
                h['setpoint_C'] = tsp

    if h['p_phase'] == 'seek':
        tsp = h['setpoint_C']
    else:
        tsp = _pressure_track(h, s, tsp_hi, now, dt, msgs)

    return tsp


def _upstream_shift(p_up):
    """Temperature shift for upstream pressure p_up (bar abs.), K."""
    if not PRESSURE_UP_ENABLE or p_up is None:
        return 0.0
    shift = -PRESSURE_UP_K_PER_BAR * (p_up - PRESSURE_UP_REF_BAR)
    return max(-PRESSURE_UP_MAX_SHIFT_K, min(PRESSURE_UP_MAX_SHIFT_K, shift))


def _seek_goal(h):
    """Seek goal temperature: PRESSURE_SEEK_START_C, raised by the feedforward
    map for larger targets, plus the upstream-pressure shift
    ."""
    goal = PRESSURE_SEEK_START_C
    if PRESSURE_FF_ENABLE and h['p_base'] is not None:
        rise = h['p_target_mbar'] - 10 ** h['p_base']
        if rise > 0:
            goal = max(goal, PRESSURE_FF_REF_C + PRESSURE_FF_EFOLD_K * math.log(
                PRESSURE_FF_FRACTION * rise / PRESSURE_FF_REF_RISE_MBAR))
    return min(goal + h['p_shift'], PRESSURE_FF_MAX_C)


def _burst_step(h, temp, tsp, now, msgs):
    """Advance the burst/coast sequence toward seek setpoint tsp
    ."""
    stage = h['p_burst']
    if stage == 'burst':
        h['p_override'] = HEATER_MAX_DUTY
        if temp >= tsp - PRESSURE_BURST_BRAKE_K:
            msgs.append(f"Pressure loop: burst done after {now - h['p_burst_t0']:.1f} s "
                        f"at {temp:.1f} °C (goal {tsp:.1f} °C) — coasting")
            h.update(p_burst='coast', p_burst_t0=now, p_burst_peak=temp, p_override=0.0)
        elif now - h['p_burst_t0'] > PRESSURE_BURST_MAX_S:
            _end_burst(h, tsp, msgs, f"burst time limit ({PRESSURE_BURST_MAX_S:g} s) "
                                     f"reached at {temp:.1f} °C — check the thermocouple")
    elif stage == 'coast':
        h['p_override'] = 0.0
        h['p_burst_peak'] = max(h['p_burst_peak'], temp)
        if (temp < h['p_burst_peak'] - 0.3        # clearly past the peak (TC noise ~0.05 K)
                or temp >= tsp - PRESSURE_SEEK_BAND_C
                or now - h['p_burst_t0'] > PRESSURE_COAST_MAX_S):
            _end_burst(h, tsp, msgs, f"coast done, TC peaked at {h['p_burst_peak']:.1f} °C")


def _end_burst(h, tsp, msgs, why):
    """Hand heating back to the temperature PI, preloaded near holding power."""
    h.update(p_burst=None, p_override=None, d_prev=None, integral=_hold_integral(tsp))
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
                        f"at {PRESSURE_HOLD_SHUT_C + h['p_shift']:.1f} °C. "
                        f"The lowest holdable pressure is ≈ {lowest:.1e} mbar")
        else:
            msgs.append(f"Pressure loop: target {tgt:.2e} mbar is below the lowest "
                        f"pressure the valve can hold once open (baseline {base:.2e} + "
                        f"step ~{PRESSURE_MIN_STEP_MBAR:.1e} ≈ {lowest:.1e} mbar) — "
                        f"expect it to sit above target at the "
                        f"{PRESSURE_OPEN_FLOOR_C + h['p_shift']:.1f} °C floor")


def _pressure_track(h, s, tsp_hi, now, dt, msgs):
    """TRACK step of the outer loop ."""
    tsp_lo = max(PRESSURE_TSP_MIN_C, PRESSURE_OPEN_FLOOR_C + h['p_shift'])
    r   = math.log10(h['p_target_mbar'])
    err = r - h['p_filt']
    h['p_err'] = err
    _check_target(h, msgs)

    e_i = 0.0 if abs(err) < PRESSURE_DEADBAND_DEC else err
    e_i = max(-PRESSURE_ERR_CLAMP_DEC, min(PRESSURE_ERR_CLAMP_DEC, e_i))

    def raw_for(integral):
        return (h['p_t0'] - s * PRESSURE_KP * (h['p_filt'] - h['p_y0'])
                + s * PRESSURE_KI * integral)

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
                     f"floor ({tsp_lo:g} °C, kept to hold the valve open)")
            msgs.append(f"Pressure loop: setpoint held at {limit} for "
                        f"{PRESSURE_NO_AUTHORITY_S/60:.0f} min and pressure is still "
                        f"{where} target")
    else:
        h['p_pinned_since']  = None
        h['p_pinned_warned'] = False
    return tsp
