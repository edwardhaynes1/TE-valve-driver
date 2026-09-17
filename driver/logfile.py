"""CSV logging: the main per-interval log and the per-edge PWM log.
"""


import csv
import math
import os
import time
from datetime import datetime

from . import control, schema, shared
from .control import AUTO_P, AUTO_T, MANUAL
from .config import HEATER_R_OHM, HEATER_V_RAIL, LOG_DIR, LOG_INTERVAL_S
from .shared import ascii_text, log_event


def _make_log_path():
    """Return a writable CSV path, retrying with a unique suffix if locked."""
    base = datetime.now().strftime('%Y%m%d_%H%M%S')
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"_{attempt}"
        path = os.path.join(LOG_DIR, f"te-sensor_{base}{suffix}.csv")
        try:
            fh = open(path, 'x', newline='')
            fh.close()
            return path
        except (PermissionError, FileExistsError):
            continue
    return f"te-sensor_{base}.csv"


# Set by init_paths() at start-up, not on import, so importing this module
# (e.g. from tests or the plotter) never creates files.
LOG_FILE = None
PWM_LOG_FILE = None


def init_paths():
    """Create the log folder and choose this session's file names."""
    global LOG_FILE, PWM_LOG_FILE
    os.makedirs(LOG_DIR, exist_ok=True)
    LOG_FILE = _make_log_path()
    PWM_LOG_FILE = (LOG_FILE[:-4] if LOG_FILE.endswith(".csv") else LOG_FILE) + schema.PWM_SUFFIX
    return LOG_FILE, PWM_LOG_FILE


# ═══════════════════════════════════════════════════════════════════════════════
# CSV LOGGING THREAD  — drift-free cadence, immediate first row
#
# One output file per session: te-sensor_<ts>.csv
#
# Columns:
#   timestamp, keller_pressure_bar (mean), keller_temperature_degC (mean),
#   n_keller_samples, vacuum_chamber_mbar, te_temperature_degC, tc_fault,
#   heater_duty, heater_mode, heater_setpoint_degC, heater_V/I (calc + meas),
#   pressure_target_mbar, vacuum_status, ..., heater_P (calc + meas)
# ═══════════════════════════════════════════════════════════════════════════════

def logger_thread():
    """CSV writer. Encoding is explicit UTF-8 — Windows would otherwise use
    cp1252 — and the events text is additionally reduced to ASCII, so any
    reader (Excel, LOG-PLOTTER.py, default-encoding open()) copes.

    A failed row never kills the thread: the error is shown in the GUI
    ([CSV:ERR] plus an event-log line), the row's events are kept for the
    next attempt, and logging resumes as soon as a write succeeds."""
    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f, \
         open(PWM_LOG_FILE, 'a', newline='', encoding='utf-8') as fp:
        pwm_writer = csv.writer(fp)
        pwm_writer.writerow(schema.PWM)
        fp.flush()
        writer = csv.writer(f)
        writer.writerow(schema.MAIN)
        f.flush()

        def write_row():
            # gate edges first, so the _pwm file is never behind the main one
            with shared.lock:
                edges = list(shared.pwm_edges)
                shared.pwm_edges.clear()
            if edges:
                try:
                    pwm_writer.writerows([[e[c] for c in schema.PWM] for e in edges])
                    fp.flush()
                except Exception as e:
                    with shared.lock:
                        shared.pwm_edges[:0] = edges
                    if shared.csv_ok is not False:
                        shared.csv_ok = False
                        log_event(f"PWM CSV WRITE FAILED — {type(e).__name__}: {e}")
                    return

            with shared.lock:
                p_samp = shared.readings['keller_pressure_samples']
                t_samp = shared.readings['keller_temperature_samples']
                p_mean = round(sum(p_samp) / len(p_samp), 4) if p_samp else None
                t_mean = round(sum(t_samp) / len(t_samp), 2) if t_samp else None
                n_k    = len(p_samp)
                shared.readings['keller_pressure_samples']    = []
                shared.readings['keller_temperature_samples'] = []

                vac     = shared.readings['vacuum_chamber_mbar']
                te_temp = shared.readings['te_temperature_degC']
                fault   = shared.readings['tc_fault']
                vac_st  = shared.readings['vacuum_status'] or ''
                taken   = list(shared.events_pending)
                shared.events_pending.clear()
            events = ' | '.join(ascii_text(e) for e in taken)

            with shared.heater_lock:
                h_duty = round(shared.heater['duty_actual'], 4)
                h_mode = shared.heater['mode'] if shared.heater['armed'] else 'off'
                h_set  = (round(shared.heater['setpoint_C'], 2)
                          if shared.heater['mode'] in (AUTO_T, AUTO_P) else '')
                v_calc = round(shared.heater['duty_actual'] * HEATER_V_RAIL, 3)
                i_calc = round(shared.heater['duty_actual'] * HEATER_V_RAIL / HEATER_R_OHM, 4)
                v_m    = shared.heater['v_meas_mean']
                i_m    = shared.heater['i_meas_mean']
                v_m    = round(v_m, 3) if v_m is not None else ''
                i_m    = round(i_m, 4) if i_m is not None else ''
                p_calc = round(shared.heater['duty_actual'] * HEATER_V_RAIL ** 2 / HEATER_R_OHM, 4)
                p_m    = shared.heater['p_meas_mean']
                p_m    = round(p_m, 4) if p_m is not None else ''
                now    = control.clock()   # same clock as the edge times
                on_s   = shared.heater['on_time_acc']
                if shared.heater['on_acc_from'] is not None:
                    on_s += now - shared.heater['on_acc_from']
                    shared.heater['on_acc_from'] = now
                shared.heater['on_time_acc'] = 0.0
                on_s   = round(on_s, 3)
                in_p   = shared.heater['mode'] == AUTO_P and not shared.heater['p_init']
                p_tgt  = shared.heater['p_target_mbar'] if in_p else ''
                p_base = (10 ** shared.heater['p_base']
                          if in_p and shared.heater['p_base'] is not None else '')
                d_cmd  = (round(shared.heater['duty_cmd'], 4)
                          if shared.heater['mode'] == MANUAL else '')

            ts = datetime.now().isoformat(timespec='milliseconds')
            try:
                row = {
                    'timestamp': ts,
                    'keller_pressure_bar': p_mean,
                    'keller_temperature_degC': t_mean,
                    'n_keller_samples': n_k,
                    'vacuum_chamber_mbar': vac,
                    'te_temperature_degC': te_temp,
                    'tc_fault': fault if fault is not None else '',
                    'heater_duty': h_duty,
                    'heater_mode': h_mode,
                    'heater_setpoint_degC': h_set,
                    'heater_V_mean_calc': v_calc,
                    'heater_I_mean_calc': i_calc,
                    'heater_V_mean_meas': v_m,
                    'heater_I_mean_meas': i_m,
                    'pressure_target_mbar': p_tgt,
                    'vacuum_status': vac_st,
                    'heater_duty_cmd': d_cmd,
                    'events': events,
                    'pressure_baseline_mbar': p_base,
                    'heater_P_mean_calc': p_calc,
                    'heater_P_mean_meas': p_m,
                    'heater_on_s': on_s,
                }
                writer.writerow([row[c] for c in schema.MAIN])
                f.flush()
            except Exception as e:
                with shared.lock:                       # keep the events for next time
                    shared.events_pending[:0] = taken
                if shared.csv_ok is not False:          # report once per outage
                    shared.csv_ok = False
                    log_event(f"CSV WRITE FAILED — {type(e).__name__}: {e} — "
                              f"data is NOT being logged, retrying every row")
                return
            if shared.csv_ok is False:
                log_event("CSV logging resumed")
            shared.csv_ok = True

        start = time.time()
        n = 0
        while not shared.stop.is_set():
            write_row()
            n += 1
            target = start + n * LOG_INTERVAL_S
            sleep_for = target - time.time()
            if sleep_for < 0:
                n = max(n, math.ceil((time.time() - start) / LOG_INTERVAL_S))
                target = start + n * LOG_INTERVAL_S
                sleep_for = max(0.0, target - time.time())
            shared.stop.wait(timeout=sleep_for)

        time.sleep(0.2)    # let the device thread record its forced-low edge
        write_row()        # final row: captures the shutdown events
