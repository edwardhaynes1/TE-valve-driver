"""Runs t-min-tune: its thread, its files, the estimate after every test.
The logic is in tmin.py; this module adds the clock, the thread and the I/O
(as cyclerun.py does for cycling, which it replaced in the window; history 36).

    start(main_log="", retorqued=None, target=None, band=None)
        -> (ok, message)                    GUI: start
    set_band(target, band)                  GUI: the operator changed them
    last_seating(torque) -> name or None    the newest seating at this torque
    stop(reason)                            GUI: stop, DISARM, closing
    running() / status() / labels()         for the window and the log rows
    record_row(row)                         logger: a main-log row was written
    tick()                                  one step (the thread; tests)

Files: every test is a row of logs/t-min.csv (tminlog.py) — the central
record, only ever appended to — and its rows (main-log columns) go to
logs/t-min/<seating>/<session>/test001.csv …, with session.json (settings).
"""

import csv
import json
import os
import threading
from datetime import datetime, timedelta

from . import config, control, openmap, schema, shared, tmin, tminlog
from .shared import log_event

_lock = threading.RLock()
_s = None
_folder = None
_files = {}
_fit = None
_thread = None


def running():
    with _lock:
        return _s is not None and _s['state'] == tmin.RUNNING


def status():
    with _lock:
        if _s is None:
            return None
        line = tmin.status_text(_s)
        est = _s.get('est')
        if est is not None and _s['state'] == tmin.RUNNING:
            line += (f"\nestimate at {_s['target']:g} bar: {est['T']:.1f} °C ({est['how']})"
                     + (f", scatter {est['sd']:.1f} K" if est['sd'] is not None else ""))
        return line


def band():
    """(target, lo, hi) in bar while it runs, else None."""
    with _lock:
        if _s is None or _s['state'] != tmin.RUNNING:
            return None
        t, b = _s['target'], _s['band']
        return t, t - b, t + b


def labels():
    with _lock:
        return tmin.labels(_s)


def folder():
    with _lock:
        return _folder


def seating():
    with _lock:
        return _s['seating'] if _s is not None else None


def last_seating(torque):
    """The newest seating at this torque in t-min.csv, else in the openings
    table (within SEAT_SCREW_TOL_NM), or None."""
    best = None
    for r in tminlog.load() + openmap.load():
        try:
            tq = float(r.get('torque_Nm'))
        except (TypeError, ValueError):
            continue
        if abs(tq - torque) > config.SEAT_SCREW_TOL_NM + 1e-9:
            continue
        t = openmap.parse_time(r.get('time'))
        if t is not None and (best is None or t > best[0]):
            best = (t, r.get('seating'))
    return best[1] if best else None


def band_problem(target, band):
    if target is None:
        return "enter the upstream target (bar)"
    if not (0.0 < target < 20.0):
        return f"upstream target {target:g} bar is out of range"
    if band is None or not (0.0 < band < 5.0):
        return "enter the ± band (bar)"
    return None


def start_problem(target=None, band=None):
    torque = shared.seat_screw_torque()
    if torque is None:
        return "enter the seat screw torque first"
    if running():
        return "t-min-tune is already running"
    if control.snapshot()['armed']:
        return "disarm the heater first; t-min-tune arms it itself"
    problem = band_problem(target, band)
    if problem:
        return problem
    r = shared.latest()
    if r['te_temperature_degC'] is None:
        return "no valve temperature — check the thermocouple"
    if r['vacuum_chamber_mbar'] is None:
        return (f"no valid chamber pressure ({r['vacuum_status'] or 'no reading'}) — "
                f"t-min-tune detects the opening from it")
    return None


def _settings():
    names = [n for n in dir(config) if n.startswith(("TMIN_", "BATCH_", "PID_", "TEMP_BURST"))
             and not n.endswith(("_DIR", "WORKBOOK", "_CSV"))]
    return {n: getattr(config, n) for n in sorted(names)}


def start(main_log="", retorqued=None, target=None, band=None, start_thread=True):
    """Start at the torque entered. retorqued False continues the newest
    seating at this torque; True (or None, or none yet) starts a new one."""
    global _s, _folder, _fit, _thread
    band = config.TMIN_BAND_BAR if band is None else band
    problem = start_problem(target, band)
    if problem:
        return False, f"t-min-tune not started — {problem}"
    torque = shared.seat_screw_torque()
    old = last_seating(torque)
    now_dt = datetime.now()
    if retorqued is False and old is not None:
        name, how = old, f"continuing seating {old}"
    else:
        name = f"{now_dt:%Y%m%d_%H%M%S}_{torque:.2f}Nm"
        while name == old:              # started within the same second: a later name
            now_dt += timedelta(seconds=1)
            name = f"{now_dt:%Y%m%d_%H%M%S}_{torque:.2f}Nm"
        how = f"new seating {name}" + (" (re-torqued)" if retorqued else "")
    path = os.path.join(config.TMIN_DIR, name, f"{now_dt:%Y%m%d_%H%M%S}")
    try:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "session.json"), "w", encoding="utf-8") as f:
            json.dump(dict(seating=name, started=now_dt.isoformat(timespec='seconds'),
                           seat_screw_torque_Nm=torque, retorqued=retorqued,
                           continued=(name == old), upstream_target_bar=target, band_bar=band,
                           main_log=main_log, detect_rule=openmap.current_rule(),
                           results=config.TMIN_CSV, settings=_settings()), f, indent=2)
    except OSError as e:
        return False, f"t-min-tune not started — can't create {path}: {e}"
    filled, message = tminlog.upgrade()
    if message:
        log_event(message)
    fit = _refit()
    with _lock:
        _s = tmin.new_session(torque, name, target, band, control.clock(),
                              shared.vacuum_history())
        _folder, _fit = path, fit
        _files.clear()
    est = _estimate(target)
    log_event(f"t-min-tune started at {torque:.2f} N·m, {how}; upstream {target:g} ± {band:g} "
              f"bar; " + (f"estimate {est['T']:.1f} °C ({est['how']})" if est else
                          "nothing measured at this torque yet: the first test scouts")
              + f" — results to {config.TMIN_CSV}")
    if start_thread:
        _thread = threading.Thread(target=_loop, daemon=True, name="t-min-tune")
        _thread.start()
    return True, name


def set_band(target, band):
    """The operator changed the target or band while it runs (or before)."""
    if band_problem(target, band):
        return False
    with _lock:
        if _s is None or _s['state'] != tmin.RUNNING:
            return False
        if (_s['target'], _s['band']) == (target, band):
            return True
        tmin.set_band(_s, target, band)
    log_event(f"t-min-tune: upstream band now {target:g} ± {band:g} bar")
    return True


def _refit():
    try:
        return openmap.fit(openmap.load())
    except Exception as e:                           # the fit must not stop the run
        log_event(f"Opening map not fitted — {type(e).__name__}: {e}")
        return None


def _estimate(target):
    """tmin.step's estimate: from t-min.csv (re-read each time, so it is the
    file that holds what has been learned) and the opening map."""
    s, f = _s, _fit
    if s is None:
        return None
    try:
        return tminlog.estimate(tminlog.load(), s['seating'], s['torque'], target, f)
    except Exception as e:                           # a bad file must not stop the run
        log_event(f"t-min estimate failed — {type(e).__name__}: {e}")
        return None


def _loop():
    period = 1.0 / config.LABJACK_SAMPLE_HZ
    while running():
        if shared.stop.is_set():
            stop("driver closed")
            break
        try:
            tick()
        except Exception as e:                       # never leave the heater armed
            stop(f"t-min-tune error — {type(e).__name__}: {e}")
            break
        shared.stop.wait(period)


def tick():
    r = shared.latest()
    h = control.snapshot()
    up, up_t = shared.upstream()
    with _lock:
        if _s is None or _s['state'] != tmin.RUNNING:
            return
        cmds, msgs, events = tmin.step(_s, control.clock(), r['te_temperature_degC'],
                                       r['vacuum_chamber_mbar'], r['vacuum_status'], h,
                                       p_up=up, p_up_t=up_t, estimate=_estimate)
    _apply(cmds, msgs, events)


def stop(reason):
    with _lock:
        if _s is None or _s['state'] != tmin.RUNNING:
            return
        cmds, msgs, events = tmin.stop(_s, control.clock(), reason)
    _apply(cmds, msgs, events)


def _apply(cmds, msgs, events):
    for cmd in cmds:
        control.heater_command(**cmd)
    for m in msgs:
        log_event(m)
    for kind, obj in events:
        try:
            if kind == 'test_start':
                _open_test(obj)
            elif kind == 'test_end':
                _result(obj)
                _close_test(obj)
            elif kind == 'end':
                _close_all()
        except Exception as e:                       # files must not stop the logic
            log_event(f"t-min-tune file error ({kind}) — {type(e).__name__}: {e}")


def _open_test(test):
    with _lock:
        fh = open(os.path.join(_folder, f"{test['name']}.csv"), "w", newline="",
                  encoding="utf-8")
        csv.writer(fh).writerow(schema.MAIN)
        fh.flush()
        _files[test['name']] = fh


def record_row(row):
    """The logger wrote a main-log row: copy it into the test's file."""
    name = row.get('batch_run')
    if not name:
        return
    with _lock:
        fh = _files.get(name)
        if fh is None:
            return
        try:
            csv.writer(fh).writerow([row.get(c, '') for c in schema.MAIN])
            fh.flush()
        except (OSError, ValueError):
            pass


def _close_test(test):
    with _lock:
        fh = _files.pop(test['name'], None)
        if fh is not None:
            fh.close()


def _close_all():
    with _lock:
        for fh in _files.values():
            fh.close()
        _files.clear()


def _iso(t):
    return datetime.fromtimestamp(t).isoformat(timespec='milliseconds') if t else ''


def _result(test):
    """A finished test: a row of t-min.csv, and the estimate it gives."""
    with _lock:
        s, f, path = _s, _fit, _folder
        slope = tminlog.slope_for(s['torque'], f)
        est = _estimate(s['target'])
        values = list(est['values']) if est is not None and est['n'] else []
        if tmin.counted(test) and test['T_onset'] is not None:
            corr = test['T_onset'] + (slope * (s['target'] - test['up_open'])
                                      if test['up_open'] is not None else 0.0)
            values.append(corr)
        conv_T = tmin.converged_value(values) if tmin.counted(test) else None
        conv = conv_T is not None
        trace = _source(os.path.join(path, f"{test['name']}.csv")) if path else ""
        row = tmin.result_row(s, test, slope, _iso(test['t_onset'] or test['t_end']),
                              trace=trace, converged=conv, converged_T=conv_T)
    ok, message = tminlog.append(row)
    if message:
        log_event(("" if ok else "WARNING: ") + message)
    if tmin.counted(test):
        est = _estimate(s['target'])
        if est is not None:
            log_event(f"t-min-tune: T_min estimate at {s['target']:g} bar "
                      f"{est['T']:.1f} °C ({est['how']}"
                      + (f", scatter {est['sd']:.1f} K" if est['sd'] is not None else "")
                      + f"); next margin {tminlog.margin(est):.1f} K")


def _source(path):
    """The test's trace, relative to the logs folder — or the full path if
    there is no relative one (Windows: another drive)."""
    try:
        rel = os.path.relpath(path, os.path.dirname(config.TMIN_CSV))
    except ValueError:
        return path
    return rel.replace(os.sep, "/")


def reset():
    """Back to nothing running (tests)."""
    global _s, _folder, _fit
    with _lock:
        for fh in _files.values():
            fh.close()
        _files.clear()
        _s, _folder, _fit = None, None, None
