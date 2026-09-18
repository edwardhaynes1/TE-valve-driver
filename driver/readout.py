"""What the window says, as text: pure functions from the current readings
and heater state to strings. No Tk and no shared state, so every line can be
checked directly (tests/test_readout.py).

    status_segments(...)      -> [(text, tag)] for the status panel
    heater_vi_lines(...)      -> [(label, value, note, tag)] for V / I / P
    heater_status(heater)     -> (text, tag)   the armed / tripped line
    loop_status(heater)       -> (text, tag)   what the control loop is doing
    mode_summary(mode, …)     -> "auto-t · setpoint 60.0 °C"

A tag names a colour, which gui.py looks up in the palette: 'bright', 'dim',
'ok', 'err' or 'warn'.
"""

import time

from .config import (
    HEATER_MAX_RUN_S, HEATER_R_OHM, P20_REF_K,
    PRESSURE_BURST_BRAKE_K, PRESSURE_MIN_STEP_MBAR, PRESSURE_OPEN_FLOOR_C,
    PRESSURE_SEEK_RATE_C_MIN, PRESSURE_SEEK_START_C, PRESSURE_TSP_MAX_C,
    PRESSURE_TSP_MIN_C, TEMP_TRIP_C, heater_current_a, heater_power_w,
    heater_voltage_v,
)
from .control import AUTO_P, AUTO_T, MANUAL
from .thermocouple import FAULT_BITS


def _mean(values):
    return (sum(values) / len(values)) if values else None


def status_segments(readings, health, heater, output, labjack_available):
    """The status panel, top to bottom: title, health flags, sensor readings
    and the heater's voltage / current / power lines."""
    r, ok, h = readings, health, heater
    p = _mean(r['keller_pressure_samples'])
    t = _mean(r['keller_temperature_samples'])
    vac, vac_st, vac_u = (r['vacuum_chamber_mbar'], r['vacuum_status'],
                          r['vacuum_gauge_V'])
    te_temp, fault = r['te_temperature_degC'], r['tc_fault']

    p_s  = f"{p:.4f} bar"      if p       is not None else "---"
    t_s  = f"{t:.1f} °C"       if t       is not None else "---"
    v_s  = f"{vac:.2e} mbar"   if vac     is not None else "---"
    te_s = f"{te_temp:.2f} °C" if te_temp is not None else "---"
    p20  = p * P20_REF_K / (t + 273.15) if (p is not None and t is not None) else None
    p20_s = f"{p20:.4f} bar" if p20 is not None else "---"
    vac_note = f"  [{vac_st}]" if (vac is None and vac_st) else ""
    vac_volt = f"  ({vac_u:.2f} V at gauge)" if vac_u is not None else ""

    # thermocouple fault annotation
    fault_note = ""
    if fault:
        names = [d for b, d in FAULT_BITS.items() if fault & b]
        fault_note = "  [" + ", ".join(names) + "]"

    seg = [("TE-VALVE-DRIVER\n", "bright")]
    for label, healthy, available in (
        (f"[KELLER:{'OK' if ok['keller'] else '--'}]",   ok['keller'],  True),
        (f"[VACUUM:{'OK' if ok['labjack'] else '--'}]",  ok['labjack'], labjack_available),
        (f"[VALVE-T:{'OK' if ok['tc'] else '--'}]",      ok['tc'],      labjack_available),
        (f"[CSV:{'OK' if ok['csv'] else ('ERR' if ok['csv'] is False else '--')}]",
         bool(ok['csv']), True),
    ):
        seg.append((label + "  ", "ok" if healthy else ("dim" if not available else "err")))
    seg.append(("\n\n", "dim"))

    seg += [("UPSTREAM P   ", "dim"), (p_s + "\n", "bright" if p is not None else "dim"),
            ("KELLER T     ", "dim"), (t_s + "\n", "bright" if t is not None else "dim"),
            ("UPSTREAM P20 ", "dim"), (p20_s, "bright" if p20 is not None else "dim"),
            ("  (at 20 °C, uses Keller chip T — not the gas T)\n", "dim"),
            ("VACUUM       ", "dim"), (v_s, "bright" if vac is not None else "dim"),
            (vac_note, "err"), (vac_volt + "\n", "dim"),
            ("VALVE T      ", "dim"), (te_s, "bright" if te_temp is not None else "dim"),
            (fault_note + "\n", "err" if fault_note else "dim"),
            ("\n", "dim")]
    for label, value, note, tag in heater_vi_lines(h, output, ok['labjack']):
        seg += [(label, "dim"), (value, tag), (note + "\n", "dim")]
    return seg


def heater_vi_lines(h, out, labjack_ok):
    """[(label, value, note, tag)] for the V, I and P readouts. h is the
    heater state (control.snapshot()); out is what the device thread
    measures (shared.heater_output())."""
    if not labjack_ok:
        return [("HEATER V     ", "---", "", "dim"),
                ("HEATER I     ", "---", "", "dim"),
                ("HEATER P     ", "---", "", "dim")]
    d      = h['duty_actual']
    v_mean = heater_voltage_v(d)
    i_mean = heater_current_a(d)
    state  = "ON " if out['out_high'] else "off"
    lines  = []

    if out['v_meas'] is not None and out['v_meas_mean'] is not None:
        rail = out['rail_meas']
        lines.append(("HEATER V     ",
                      f"{out['v_meas']:6.2f} V {state} · {out['v_meas_mean']:6.2f} V mean",
                      f"  meas · rail {rail:.2f} V · calc {v_mean:.2f} V", "bright"))
    else:
        lines.append(("HEATER V     ",
                      f"{out['v_now']:6.2f} V {state} · {v_mean:6.2f} V mean",
                      "  calc — assumes SW171 on, 24 V present", "bright"))

    if out['i_meas'] is not None and out['i_meas_mean'] is not None:
        lines.append(("HEATER I     ",
                      f"{out['i_meas']:6.3f} A {state} · {out['i_meas_mean']:6.3f} A mean",
                      f"  meas · calc {i_mean:.3f} A", "bright"))
    else:
        lines.append(("HEATER I     ",
                      f"{out['i_now']:6.3f} A {state} · {i_mean:6.3f} A mean",
                      f"  calc ({HEATER_R_OHM:g} Ω element)", "bright"))

    p_full = heater_power_w()
    p_calc = heater_power_w(d)
    if out['p_meas_mean'] is not None:
        lines.append(("HEATER P     ",
                      f"{out['p_meas_mean']:6.3f} W mean",
                      f"  meas · calc {p_calc:.3f} W", "bright"))
    else:
        lines.append(("HEATER P     ",
                      f"{p_calc:6.3f} W mean",
                      f"  calc (duty × {p_full:.2f} W full)", "bright"))
    return lines


def heater_status(h, now=None):
    """The line under the heater controls: tripped, armed, or disarmed."""
    now = time.time() if now is None else now
    mode = h['mode']
    power = heater_power_w(h['duty_actual'])
    if h['trip_reason']:
        return (f"TRIPPED — {h['trip_reason']}   (disarm, then arm to clear)", "warn")
    if not h['armed']:
        return ("disarmed · output low", "dim")
    left = "" if h['armed_at'] is None else \
        f" · off in {max(0, HEATER_MAX_RUN_S - (now - h['armed_at']))/60:.0f} min"
    tgt = {MANUAL: "",
           AUTO_T: f" · T_sp {h['setpoint_C']:.1f} °C",
           AUTO_P: f" · T_sp {h['setpoint_C']:.1f} °C (from pressure)"}[mode]
    return (f"ARMED · {mode} · duty {h['duty_actual']*100:4.1f} % · "
            f"{power:.2f} W{tgt}{left}", "bright")


def loop_status(h):
    """What the control loop is doing: the burst/coast stages in auto-t, and
    the seek / track phases in auto-p. Empty in manual."""
    mode = h['mode']
    if mode == AUTO_T and h['armed'] and h['t_burst'] == 'burst':
        return (f"auto-t · BURST full power, cut ~{h['t_brake']:.1f} K below "
                f"{h['setpoint_C']:.1f} °C", "bright")
    if mode == AUTO_T and h['armed'] and h['t_burst'] == 'coast':
        return (f"auto-t · coasting, heater off (peak {h['t_burst_peak']:.1f} °C) — "
                f"PI resumes at the peak", "bright")
    if mode != AUTO_P:
        return ("", "dim")
    if not h['armed'] or h['p_filt'] is None or h['p_init']:
        return (f"auto-p idle · target {h['p_target_mbar']:.2e} mbar · "
                f"on arm: T_sp → {PRESSURE_SEEK_START_C:g} °C, then "
                f"+{PRESSURE_SEEK_RATE_C_MIN:g} °C/min until the valve opens", "dim")
    if h['p_phase'] == 'seek':
        return _seek_status(h)
    return _track_status(h)


def _seek_status(h):
    base = (f"{10 ** h['p_base']:.2e} mbar" if h['p_base'] is not None
            else "measuring…")
    low = ""
    if h['p_base'] is not None:
        lowest = 10 ** h['p_base'] + PRESSURE_MIN_STEP_MBAR
        if 10 ** h['p_base'] < h['p_target_mbar'] < lowest:
            low = (f" — BELOW lowest holdable ≈ {lowest:.1e}, "
                   f"will hold minimum flow")
    if h['p_burst'] == 'burst':
        step = f"BURST full power to {h['setpoint_C'] - PRESSURE_BURST_BRAKE_K:.1f} °C"
    elif h['p_burst'] == 'coast':
        step = f"coasting (peak {h['p_burst_peak']:.1f} °C)"
    elif h['p_base'] is not None and h['p_target_mbar'] <= 10 ** h['p_base']:
        step = "holding shut (target ≤ baseline)"
    elif h['p_ramping']:
        step = f"creeping +{PRESSURE_SEEK_RATE_C_MIN:g} °C/min"
    else:
        step = "heating"
    return (f"auto-p seeking · valve shut · {step} · goal {h['p_goal']:.1f} "
            f"(upstream shift {h['p_shift']:+.1f} K) · "
            f"T_sp {h['setpoint_C']:.2f} °C · "
            f"baseline {base} · target {h['p_target_mbar']:.2e} mbar{low}",
            "warn" if (h['p_seek_capped'] or low) else "bright")


def _track_status(h):
    tsp_hi = min(PRESSURE_TSP_MAX_C, TEMP_TRIP_C - 5.0)
    floor  = max(PRESSURE_TSP_MIN_C, PRESSURE_OPEN_FLOOR_C + h['p_shift'])
    lowest = 10 ** h['p_base'] + PRESSURE_MIN_STEP_MBAR
    if h['p_target_mbar'] < lowest:
        return (f"auto-p · target {h['p_target_mbar']:.2e} is BELOW the lowest "
                f"holdable ≈ {lowest:.1e} (baseline {10 ** h['p_base']:.2e} + "
                f"min. flow) — holding minimum flow at the {floor:.1f} °C floor · "
                f"now {10 ** h['p_filt']:.2e}", "warn")
    return (f"auto-p · target {h['p_target_mbar']:.2e} · "
            f"baseline {10 ** h['p_base']:.2e} · "
            f"filt {10 ** h['p_filt']:.2e} · err {h['p_err']:+.2f} dec · "
            f"T_sp {floor:.1f}-{tsp_hi:g} °C · upstream shift {h['p_shift']:+.1f} K",
            "warn" if h['p_pinned_since'] else "bright")


def mode_summary(mode, duty, setpoint_c, target_mbar):
    """'auto-t · setpoint 60.0 °C' — the mode and the value it uses."""
    value = {MANUAL: f"duty {duty*100:g} %",
             AUTO_T: f"setpoint {setpoint_c:g} °C",
             AUTO_P: f"target {target_mbar:.2e} mbar"}[mode]
    return f"{mode} · {value}"
