"""Runs cycling: its thread, its files, the fit after every opening. The
logic is in cycle.py; this module adds the clock, the thread and the I/O
(as batchrun.py does for batches, whose interface it shares).

    start(main_log="", retorqued=None, deep_every=CYCLE_DEEP_EVERY)
        -> (ok, message)                    GUI: start cycling
    last_setting(torque) -> name or None    the newest setting at this torque
    stop(reason)                            GUI: stop cycling, DISARM, closing
    running() / status() / labels()         for the window and the log rows
    record_row(row)                         logger: a main-log row was written
    tick()                                  one step (the thread; tests)

Files, in logs/cycles/<setting>/<session>/: cycle001.csv … (the cycle's
rows, main-log columns, from its hold to the end of its cooldown) and
session.json (settings). Every creeping opening becomes a row of the
openings table (openmap.py); the fit is redone, its status line logged,
and logs/opening-map.png redrawn.
"""

import csv
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import config, control, cycle, openmap, schema, shared
from .shared import log_event

PLOT = True                    # redraw the map figure after each opening (tests: off)
_PLOTTER = Path(__file__).resolve().parent.parent / "TE_PLOTTER.py"

_lock = threading.RLock()
_c = None                      # the current (or last) cycling session
_folder = None
_files = {}
_fit = None                    # the opening-map fit the cycles plan from
_thread = None


def running():
    with _lock:
        return _c is not None and _c['state'] == cycle.RUNNING


def status():
    """Two things for the window: the cycling line and the map line."""
    with _lock:
        if _c is None:
            return None
        line = cycle.status_text(_c)
        if _fit is not None and _fit.n:
            line += "\n" + _fit.status_text(_c['setting'])
        return line


def labels():
    with _lock:
        return cycle.labels(_c)


def folder():
    with _lock:
        return _folder


def setting():
    with _lock:
        return _c['setting'] if _c is not None else None


def last_setting(torque):
    """The newest setting in the openings table at this torque (within
    SEAT_SCREW_TOL_NM), or None."""
    best = None
    for r in openmap.load():
        try:
            tq = float(r.get('torque_Nm'))
        except (TypeError, ValueError):
            continue
        if abs(tq - torque) > config.SEAT_SCREW_TOL_NM + 1e-9:
            continue
        t = openmap.parse_time(r.get('time'))
        if t is not None and (best is None or t > best[0]):
            best = (t, r.get('setting'))
    return best[1] if best else None


def start_problem():
    torque = shared.seat_screw_torque()
    if torque is None:
        return "enter the seat screw torque first"
    if running():
        return "cycling is already running"
    if control.snapshot()['armed']:
        return "disarm the heater first; cycling arms it itself"
    r = shared.latest()
    if r['te_temperature_degC'] is None:
        return "no valve temperature — check the thermocouple"
    if r['vacuum_chamber_mbar'] is None:
        return (f"no valid chamber pressure ({r['vacuum_status'] or 'no reading'}) — "
                f"cycling detects the opening from it")
    return None


def _settings():
    names = [n for n in dir(config) if n.startswith(("CYCLE_", "MAP_", "BATCH_"))
             and not n.endswith(("_DIR", "WORKBOOK"))]
    return {n: getattr(config, n) for n in sorted(names)}


def start(main_log="", retorqued=None, deep_every=None, start_thread=True):
    """Start cycling at the torque entered. retorqued False continues the
    newest setting at this torque; True (or None, or no setting yet)
    starts a new one."""
    global _c, _folder, _fit, _thread
    problem = start_problem()
    if problem:
        return False, f"Cycling not started — {problem}"
    torque = shared.seat_screw_torque()
    deep_every = deep_every or config.CYCLE_DEEP_EVERY
    old = last_setting(torque)
    now_dt = datetime.now()
    if retorqued is False and old is not None:
        name, how = old, f"continuing setting {old}"
    else:
        name = f"{now_dt:%Y%m%d_%H%M%S}_{torque:.2f}Nm"
        how = f"new setting {name}" + (" (re-torqued)" if retorqued else "")
    path = os.path.join(config.CYCLE_DIR, name, f"{now_dt:%Y%m%d_%H%M%S}")
    try:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "session.json"), "w", encoding="utf-8") as f:
            json.dump(dict(setting=name, started=now_dt.isoformat(timespec='seconds'),
                           seat_screw_torque_Nm=torque, retorqued=retorqued,
                           continued=(name == old), deep_every=deep_every,
                           main_log=main_log, detect_rule=openmap.current_rule(),
                           settings=_settings()), f, indent=2)
    except OSError as e:
        return False, f"Cycling not started — can't create {path}: {e}"
    fit = _refit()
    with _lock:
        _c = cycle.new_cycling(torque, name, control.clock(), shared.vacuum_history(),
                               deep_every=deep_every)
        _folder, _fit = path, fit
        _files.clear()
    p = fit.predict(name, torque, shared.upstream()[0]) if fit is not None else None
    log_event(f"Cycling started at {torque:.2f} N·m, {how}; every {deep_every} cycle"
              f"{'s' * (deep_every > 1)} deep; "
              + (f"expecting ~{p[0]:.1f} °C ({p[1]})" if p else
                 "nothing measured at this torque yet: the first cycle scouts")
              + f" — {path}")
    if start_thread:
        _thread = threading.Thread(target=_loop, daemon=True, name="cycling")
        _thread.start()
    return True, name


def _refit():
    try:
        return openmap.fit(openmap.load())
    except Exception as e:                           # the fit must not stop cycling
        log_event(f"Opening map not fitted — {type(e).__name__}: {e}")
        return None


def _predict(bar, deep):
    """The fit's prediction for the current setting (cycle.step calls it)."""
    f, c = _fit, _c
    if f is None or c is None:
        return None
    p = f.predict(c['setting'], c['torque'], bar, deep)
    return None if p is None else (p[0], p[1], f.sd)


def _loop():
    period = 1.0 / config.LABJACK_SAMPLE_HZ
    while running():
        if shared.stop.is_set():
            stop("driver closed")
            break
        try:
            tick()
        except Exception as e:                       # never leave the heater armed
            stop(f"cycling error — {type(e).__name__}: {e}")
            break
        shared.stop.wait(period)


def tick():
    r = shared.latest()
    h = control.snapshot()
    up, up_t = shared.upstream()
    with _lock:
        if _c is None or _c['state'] != cycle.RUNNING:
            return
        cmds, msgs, events = cycle.step(_c, control.clock(), r['te_temperature_degC'],
                                        r['vacuum_chamber_mbar'], r['vacuum_status'], h,
                                        p_up=up, p_up_t=up_t, predict=_predict)
    _apply(cmds, msgs, events)


def stop(reason):
    with _lock:
        if _c is None or _c['state'] != cycle.RUNNING:
            return
        cmds, msgs, events = cycle.stop(_c, control.clock(), reason)
    _apply(cmds, msgs, events)


def _apply(cmds, msgs, events):
    for cmd in cmds:
        control.heater_command(**cmd)
    for m in msgs:
        log_event(m)
    for kind, obj in events:
        try:
            if kind == 'cycle_start':
                _open_cycle(obj)
            elif kind == 'opening':
                _opening(obj)
            elif kind in ('cycle_end',):
                _close_cycle(obj)
            elif kind == 'end':
                _close_all()
        except Exception as e:                       # files must not stop the logic
            log_event(f"Cycling file error ({kind}) — {type(e).__name__}: {e}")


def _open_cycle(cyc):
    with _lock:
        fh = open(os.path.join(_folder, f"{cyc['name']}.csv"), "w", newline="",
                  encoding="utf-8")
        csv.writer(fh).writerow(schema.MAIN)
        fh.flush()
        _files[cyc['name']] = fh


def record_row(row):
    """The logger wrote a main-log row: copy it into the cycle's file."""
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


def _close_cycle(cyc):
    with _lock:
        fh = _files.pop(cyc['name'], None)
        if fh is not None:
            fh.close()


def _close_all():
    with _lock:
        for fh in _files.values():
            fh.close()
        _files.clear()


def _opening(cyc):
    """A creeping opening: into the table, refit, log the status, redraw."""
    global _fit
    with _lock:
        rel = os.path.relpath(os.path.join(_folder, f"{cyc['name']}.csv"),
                              os.path.dirname(config.OPENINGS_CSV))
        row = cycle.opening_row(_c, cyc, openmap.current_rule(), rel.replace(os.sep, "/"))
    if row is None:
        return
    ok, message = openmap.append(row)
    if message:
        log_event(("" if ok else "WARNING: ") + message)
    f = _refit()
    with _lock:
        _fit = f
        name = _c['setting']
    if f is not None:
        log_event(f.status_text(name))
        terms = f.terms_text()
        if terms:
            log_event(f"Opening map terms: {terms}")
    if PLOT:
        _plot_map()


def _plot_map():
    try:
        subprocess.Popen([sys.executable, str(_PLOTTER), "--map", config.OPENINGS_CSV,
                          "-o", config.MAP_FIGURE, "--no-show"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(_PLOTTER.parent),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log_event(f"Opening map figure not drawn — {type(e).__name__}: {e}")


def reset():
    """Back to no cycling (tests)."""
    global _c, _folder, _fit
    with _lock:
        for fh in _files.values():
            fh.close()
        _files.clear()
        _c, _folder, _fit = None, None, None
