"""CSV logging: the main per-interval log and the per-edge PWM log.
"""


import csv
import math
import os
import time
from datetime import datetime

from . import control, schema, shared
from .control import AUTO_P, AUTO_T, MANUAL
from .config import (
    LOG_DIR, LOG_INTERVAL_S, heater_current_a, heater_power_w,
    heater_voltage_v,
)
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
            edges = shared.take_edges()
            if edges:
                try:
                    pwm_writer.writerows([[e[c] for c in schema.PWM] for e in edges])
                    fp.flush()
                except Exception as e:
                    shared.put_back_edges(edges)
                    if shared.health()['csv'] is not False:
                        shared.set_health(csv=False)
                        log_event(f"PWM CSV WRITE FAILED — {type(e).__name__}: {e}")
                    return

            r = shared.take_log_readings()
            taken = shared.take_events()
            events = ' | '.join(ascii_text(e) for e in taken)

            h = control.snapshot()
            on_s = round(control.take_on_time(), 3)
            duty = h['duty_actual']
            in_p = h['mode'] == AUTO_P and not h['p_init']

            def blank_or(value, digits):
                return round(value, digits) if value is not None else ''

            ts = datetime.now().isoformat(timespec='milliseconds')
            row = {
                'timestamp': ts,
                'keller_pressure_bar': r['p_mean'],
                'keller_temperature_degC': r['t_mean'],
                'n_keller_samples': r['n_keller'],
                'vacuum_chamber_mbar': r['vac'],
                'te_temperature_degC': r['valve_temp'],
                'tc_fault': r['fault'] if r['fault'] is not None else '',
                'heater_duty': round(duty, 4),
                'heater_mode': h['mode'] if h['armed'] else 'off',
                'heater_setpoint_degC': (round(h['setpoint_C'], 2)
                                         if h['mode'] in (AUTO_T, AUTO_P) else ''),
                'heater_V_mean_calc': round(heater_voltage_v(duty), 3),
                'heater_I_mean_calc': round(heater_current_a(duty), 4),
                'heater_V_mean_meas': blank_or(h['v_meas_mean'], 3),
                'heater_I_mean_meas': blank_or(h['i_meas_mean'], 4),
                'pressure_target_mbar': h['p_target_mbar'] if in_p else '',
                'vacuum_status': r['vac_status'] or '',
                'heater_duty_cmd': round(h['duty_cmd'], 4) if h['mode'] == MANUAL else '',
                'events': events,
                'pressure_baseline_mbar': (10 ** h['p_base']
                                           if in_p and h['p_base'] is not None else ''),
                'heater_P_mean_calc': round(heater_power_w(duty), 4),
                'heater_P_mean_meas': blank_or(h['p_meas_mean'], 4),
                'heater_on_s': on_s,
            }
            try:
                writer.writerow([row[c] for c in schema.MAIN])
                f.flush()
            except Exception as e:
                shared.put_back_events(taken)           # keep the events for next time
                if shared.health()['csv'] is not False:  # report once per outage
                    shared.set_health(csv=False)
                    log_event(f"CSV WRITE FAILED — {type(e).__name__}: {e} — "
                              f"data is NOT being logged, retrying every row")
                return
            if shared.health()['csv'] is False:
                log_event("CSV logging resumed")
            shared.set_health(csv=True)

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
