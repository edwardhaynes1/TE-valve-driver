"""What the window says, as text: pure functions from the current readings
and heater state to strings. No Tk and no shared state, so every line can be
checked directly (tests/test_readout.py).

    status_segments(...)      -> [(text, tag)] for the status panel, made of
      sensor_segments / seat_screw_segments / heater_segments
    parse_seat_screw_torque(text) -> N·m, or ValueError with the reason
    torque_gate(seat_screw_nm) -> (locked, tag)   controls locked until entered
    heater_vi_lines(...)      -> [(label, value, note, tag)] for V / I / P
    heater_status(heater)     -> (text, tag)   the armed / tripped line
    loop_status(heater)       -> (text, tag)   what the control loop is doing
    mode_summary(mode, …)     -> "auto-t · setpoint 60.0 °C"

A tag names a colour, which gui.py looks up in the palette: 'bright', 'dim',
'ok', 'err', 'warn' or 'prompt' (an input still to be filled in).
"""

import time

from .config import (
    HEATER_MAX_RUN_S, HEATER_R_OHM, P20_REF_K, PRESSURE_MAX_MBAR, PRESSURE_SOAK_FACTOR,
    SEAT_SCREW_TORQUE_MAX_NM, heater_current_a, heater_power_w, heater_voltage_v,
)
from .control import AUTO_P, AUTO_T, MANUAL
from .thermocouple import FAULT_BITS


def _mean(values):
    return (sum(values) / len(values)) if values else None


def status_segments(readings, health, heater, output, labjack_available,
                    seat_screw_nm=None):
    """The status panel, top to bottom: title, health flags, sensor readings,
    the seat screw torque, and the heater's voltage / current / power lines.
    The window draws the three parts separately (the seat screw line holds
    the torque input), but this is the whole panel as text."""
    return (sensor_segments(readings, health, labjack_available)
            + seat_screw_segments(seat_screw_nm)
            + heater_segments(heater, output, health['labjack']))


def seat_screw_segments(seat_screw_nm):
    """The SEAT SCREW line: the torque, or that it hasn't been entered."""
    seg = [("SEAT SCREW   ", "dim")]
    if seat_screw_nm is None:
        seg += [("---", "dim"), ("  (not entered)\n", "prompt")]
    else:
        seg += [(f"{seat_screw_nm:.2f} N·m\n", "bright")]
    return seg


def heater_segments(heater, output, labjack_ok):
    """The heater's voltage / current / power lines."""
    seg = []
    for label, value, note, tag in heater_vi_lines(heater, output, labjack_ok):
        seg += [(label, "dim"), (value, tag), (note + "\n", "dim")]
    return seg


def sensor_segments(readings, health, labjack_available):
    """Title, health flags and the sensor readings, down to VALVE T."""
    r, ok = readings, health
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
    seg.append(("\n", "dim"))

    seg += [("UPSTREAM P   ", "dim"), (p_s + "\n", "bright" if p is not None else "dim"),
            ("KELLER T     ", "dim"), (t_s + "\n", "bright" if t is not None else "dim"),
            ("UPSTREAM P20 ", "dim"), (p20_s, "bright" if p20 is not None else "dim"),
            ("  (at 20 °C, uses Keller chip T — not the gas T)\n", "dim"),
            ("VACUUM       ", "dim"), (v_s, "bright" if vac is not None else "dim"),
            (vac_note, "err"), (vac_volt + "\n", "dim"),
            ("VALVE T      ", "dim"), (te_s, "bright" if te_temp is not None else "dim"),
            (fault_note + "\n", "err" if fault_note else "dim")]
    return seg


def torque_gate(seat_screw_nm):
    """(locked, tag) for the window. Until the seat screw torque is entered,
    every other control is locked and the torque input is shown in the
    'prompt' colour; once entered (0 counts), everything unlocks and the
    torque input turns 'bright'."""
    if seat_screw_nm is None:
        return True, "prompt"
    return False, "bright"


def parse_seat_screw_torque(text):
    """The operator's entry as N·m. Accepts a decimal point or comma;
    anything else, or a value outside 0 … SEAT_SCREW_TORQUE_MAX_NM, raises
    ValueError saying why."""
    return _parse_torque(text, "Seat screw", SEAT_SCREW_TORQUE_MAX_NM, "0.4")


def _parse_torque(text, what, top, example):
    cleaned = text.strip().replace(",", ".")
    try:
        nm = float(cleaned)
    except ValueError:
        raise ValueError(f"{what} torque: '{text.strip()}' is not a number "
                         f"(enter N·m, e.g. {example})") from None
    if not (0.0 <= nm <= top):
        raise ValueError(f"{what} torque {nm:g} N·m is outside "
                         f"0 to {top:g} N·m — not changed")
    return nm


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
    the auto-p phase. Empty in manual."""
    mode = h['mode']
    if mode == AUTO_T and h['armed'] and h['t_burst'] == 'burst':
        return (f"auto-t · BURST full power toward {h['setpoint_C']:.1f} °C, cut when "
                f"T + {h['t_tau']:.1f} s × rate reaches it", "bright")
    if mode == AUTO_T and h['armed'] and h['t_burst'] == 'coast':
        return (f"auto-t · coasting, heater off (peak {h['t_burst_peak']:.1f} °C) — "
                f"PI resumes at the peak", "bright")
    if mode != AUTO_P:
        return ("", "dim")
    cut = PRESSURE_MAX_MBAR / PRESSURE_SOAK_FACTOR
    if not h['armed'] or h['p_raw'] is None or h['p_init']:
        return (f"auto-p idle · aim {h['p_target_mbar']:.1e} mbar · on arm: measure "
                f"the baseline, creep up slowly, heater off above {cut:.2e} so "
                f"P_vacuum stays under {PRESSURE_MAX_MBAR:.1e}", "dim")
    phase = h['p_phase']
    what = {
        'baseline': "measuring the baseline",
        'seek':     f"valve shut · creeping {h['p_rate_c_min']:.2f} °C/min",
        'hold':     "valve moved · holding, waiting for P_vacuum to settle",
        'approach': f"gas flowing, below the aim · creeping {h['p_rate_c_min']:.2f} °C/min",
        'trim':     f"gas flowing, above the aim · easing down {-h['p_rate_c_min']:.2f} °C/min",
        'cut':      "HEATER OFF · P_vacuum at or heading over the cut line",
        'park':     "no room for flow below P_vacuum_max · not heating further",
    }[phase]
    base = f"{10 ** h['p_base']:.2e}" if h['p_base'] is not None else "…"
    aim = f"{10 ** h['p_aim']:.2e}" if h['p_aim'] is not None else f"{h['p_target_mbar']:.2e}"
    up = f"{h['p_up_bar']:.2f} bar" if h['p_up_bar'] is not None else "not read"
    return (f"auto-p · {what} · P_vacuum {10 ** h['p_raw']:.2e} · baseline {base} · "
            f"aim {aim} · cut {cut:.2e} mbar · T_sp {h['setpoint_C']:.1f} °C · P_up {up}",
            "warn" if phase in ('cut', 'park') or h['p_capped'] else "bright")


def mode_summary(mode, duty, setpoint_c, target_mbar):
    """'auto-t · setpoint 60.0 °C' — the mode and the value it uses."""
    value = {MANUAL: f"duty {duty*100:g} %",
             AUTO_T: f"setpoint {setpoint_c:g} °C",
             AUTO_P: f"target {target_mbar:.2e} mbar"}[mode]
    return f"{mode} · {value}"
