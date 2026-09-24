"""Runs a batch: its thread, its files, and its heater commands. The logic
is in batch.py; this module adds the clock, the threads and the I/O.

    start(n_tests, main_log="", topup_drop_bar=None, retorqued=None)
        -> (ok, message)                           GUI: start batch
    remembered(torque) -> (entry, usable, why)     the remembered opening point
                                                   for the start dialog
    abort(reason)                                  GUI: abort batch, or closing
    resume()                                       GUI: continue after a top-up
    running() / paused() / status() / labels()     for the window and the log rows
    record_row(row)                                logger: a main-log row was written
    tick()                                         one step (the thread; tests)

Files, in logs/batches/<date>_<time>_<torque>Nm_<upstream>bar/:
    scout1.csv, scout2.csv, testrun01.csv …   the run's rows, main-log columns
    summary.csv                                a row per run (schema.RUN_SUMMARY)
    batch.json                                 settings at the start; results at the end
    batch.png                                  drawn by TE_PLOTTER at the end
and a row per run and per batch in the opening map (config.BATCH_WORKBOOK).
At the end the result may become the remembered opening point for the
torque (openings.py; batch.remembered_entry decides).
"""

import csv
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import batch, config, control, openings, schema, shared, workbook
from .shared import log_event

PLOT = True                    # draw batch.png at the end (tests switch it off)
_PLOTTER = Path(__file__).resolve().parent.parent / "TE_PLOTTER.py"

_lock = threading.RLock()      # guards everything below
_b = None                      # the current (or last) batch
_folder = None
_name = ""
_files = {}                    # run name -> open file handle
_rows = {}                     # run name -> [(t, row)]
_summaries = []
_thread = None


def running():
    with _lock:
        return _b is not None and _b['state'] == batch.RUNNING


def paused():
    """Waiting for the operator to top up upstream?"""
    with _lock:
        return running() and _b['phase'] == batch.TOPUP


def status():
    """One line for the window, or None if no batch has run this session."""
    with _lock:
        return batch.status_text(_b) if _b is not None else None


def labels():
    """(run, phase) for a main-log row."""
    with _lock:
        return batch.labels(_b)


def folder():
    with _lock:
        return _folder


def _settings():
    names = [n for n in dir(config) if n.startswith("BATCH_") and not n.endswith(("_DIR", "WORKBOOK"))]
    names += ["PRESSURE_OPEN_DEC", "PRESSURE_BASE_WINDOW_S", "PRESSURE_BASE_GUARD_S",
              "PRESSURE_FILTER_S", "HEATER_MAX_RUN_S", "TEMP_TRIP_C"]
    return {n: getattr(config, n) for n in sorted(names)}


def start_problem(n_tests=1, topup_drop_bar=None):
    """Why a batch can't start now, or None. A rising chamber is not a
    reason: the batch waits for it to settle."""
    torque = shared.seat_screw_torque()
    if torque is None:
        return "enter the seat screw torque first"
    if running():
        return "one is already running"
    if control.snapshot()['armed']:
        return "disarm the heater first; the batch arms it itself"
    if not 1 <= n_tests <= config.BATCH_TEST_RUNS_MAX:
        return f"test runs must be 1 to {config.BATCH_TEST_RUNS_MAX}"
    r = shared.latest()
    if r['te_temperature_degC'] is None:
        return "no valve temperature — check the thermocouple"
    if r['vacuum_chamber_mbar'] is None:
        return (f"no valid chamber pressure ({r['vacuum_status'] or 'no reading'}) — "
                f"the batch detects the opening from it")
    if topup_drop_bar is not None and _fresh_upstream() is None:
        return "no upstream reading — the top-up pause needs the Keller"
    return None


def remembered(torque):
    """(entry or None, usable, why not) for this torque. A batch only uses a
    remembered opening point measured at its own creep rate."""
    entry = openings.lookup(torque)
    if entry is None:
        return None, False, "none remembered for this torque"
    if entry.get('creep_C_min') != config.BATCH_CREEP_C_MIN:
        return entry, False, (f"it was measured creeping at {entry.get('creep_C_min')} "
                              f"°C/min, not {config.BATCH_CREEP_C_MIN:g}")
    return entry, True, ""


def _fresh_upstream():
    up, t = shared.upstream()
    if up is None or t is None or control.clock() - t > config.BATCH_UP_MAX_AGE_S:
        return None
    return up


def start(n_tests, main_log="", topup_drop_bar=None, retorqued=None, start_thread=True):
    """Start a batch of at most n_tests test runs (see start_problem for
    refusals). topup_drop_bar: pause for a top-up when upstream falls this
    far below its value at the start; None = never. retorqued: the answer to
    the re-torque question — False starts from the remembered opening point
    (no scouts), True or None scouts."""
    global _b, _folder, _name, _thread, _summaries
    problem = start_problem(n_tests, topup_drop_bar)
    if problem:
        return False, f"Batch not started — {problem}"
    torque = shared.seat_screw_torque()
    up, _ = shared.upstream()
    now = control.clock()
    old, usable, why_not = remembered(torque)
    known = old if (usable and retorqued is False) else None
    up_txt = f"{up:.2f}bar" if up is not None else "upstream-unknown"
    name = f"{datetime.now():%Y%m%d_%H%M%S}_{torque:.2f}Nm_{up_txt}"
    path = os.path.join(config.BATCH_DIR, name)
    try:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "batch.json"), "w", encoding="utf-8") as f:
            json.dump(dict(batch=name, started=datetime.now().isoformat(timespec='seconds'),
                           seat_screw_torque_Nm=torque, upstream_at_start_bar=up,
                           test_runs_max=n_tests, topup_drop_bar=topup_drop_bar,
                           retorqued=retorqued, started_from_remembered=known,
                           remembered_on_file=old, main_log=main_log,
                           settings=_settings()),
                      f, indent=2)
    except OSError as e:
        return False, f"Batch not started — can't create {path}: {e}"
    with _lock:
        _b = batch.new_batch(n_tests, torque, now, topup_drop_bar,
                             history=shared.vacuum_history(), known=known, old=old,
                             retorqued=retorqued)
        _folder, _name, _summaries = path, name, []
        _files.clear()
        _rows.clear()
    topup = (f", pause for a top-up {topup_drop_bar:g} bar below the start"
             if topup_drop_bar is not None else "")
    if known is not None:
        how = f"from the remembered opening point {openings.describe(known)}"
    elif old is not None and retorqued:
        how = "with scouts (valve disturbed since the remembered opening point)"
    elif old is not None:
        how = f"with scouts (remembered opening point not used: {why_not})"
    else:
        how = "with scouts"
    log_event(f"Batch started: up to {n_tests} test runs at {torque:.2f} N·m, upstream "
              f"{'%.3f bar' % up if up is not None else 'not read'} (measured){topup}, "
              f"{how} — {path}")
    if start_thread:
        _thread = threading.Thread(target=_loop, daemon=True, name="batch")
        _thread.start()
    return True, name


def _loop():
    period = 1.0 / config.LABJACK_SAMPLE_HZ
    while running():
        if shared.stop.is_set():
            abort("driver closed")
            break
        try:
            tick()
        except Exception as e:                     # never leave the heater armed
            abort(f"batch error — {type(e).__name__}: {e}")
            break
        shared.stop.wait(period)


def tick():
    """One batch step on the current readings."""
    r = shared.latest()
    h = control.snapshot()
    up, up_t = shared.upstream()
    with _lock:
        if _b is None or _b['state'] != batch.RUNNING:
            return
        cmds, msgs, events = batch.step(_b, control.clock(), r['te_temperature_degC'],
                                        r['vacuum_chamber_mbar'], r['vacuum_status'], h,
                                        p_up=up, p_up_t=up_t)
    _apply(cmds, msgs, events)


def abort(reason):
    with _lock:
        if _b is None or _b['state'] != batch.RUNNING:
            return
        cmds, msgs, events = batch.abort(_b, control.clock(), reason)
    _apply(cmds, msgs, events)


def resume():
    """The operator has topped up: carry on."""
    with _lock:
        if _b is None:
            return
        msgs = batch.resume(_b, control.clock())
    for m in msgs:
        log_event(m)


def _apply(cmds, msgs, events):
    for c in cmds:
        control.heater_command(**c)
    for m in msgs:
        log_event(m)
    for kind, obj in events:
        try:
            if kind == 'run_start':
                _open_run(obj)
            elif kind == 'run_end':
                _end_run(obj)
            elif kind == 'batch_end':
                _end_batch()
        except Exception as e:                     # files must not stop the batch logic
            log_event(f"Batch file error ({kind}) — {type(e).__name__}: {e}")


def _open_run(run):
    with _lock:
        fh = open(os.path.join(_folder, f"{run['name']}.csv"), "w", newline="",
                  encoding="utf-8")
        csv.writer(fh).writerow(schema.MAIN)
        fh.flush()
        _files[run['name']] = fh
        _rows[run['name']] = []


def record_row(row):
    """The logger wrote a main-log row: copy it into the run's file."""
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
        _rows[name].append((control.clock(), dict(row)))


def _end_run(run):
    with _lock:
        fh = _files.pop(run['name'], None)
        if fh is not None:
            fh.close()
        summary = batch.run_summary(_b, run, _rows.get(run['name'], []), _name,
                                    f"{run['name']}.csv")
        _summaries.append(summary)
        path = os.path.join(_folder, "summary.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(schema.RUN_SUMMARY)
            w.writerow([summary[c] for c in schema.RUN_SUMMARY])
    ok, message = workbook.append(config.BATCH_WORKBOOK, 'Runs', schema.RUN_SUMMARY, summary)
    if message:
        log_event(("" if ok else "WARNING: ") + message)


def _end_batch():
    with _lock:
        for fh in _files.values():
            fh.close()
        _files.clear()
        entry = batch.remembered_entry(_b, _name)
        path = os.path.join(_folder, "batch.json")
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            info = {}
        folder_ = _folder
    stored = False
    if entry is not None:
        stored, message = openings.remember(entry)
        log_event(("" if stored else "WARNING: ") + message)
    else:
        log_event(f"Remembered opening point for {_b['torque']:.2f} N·m kept: this batch "
                  f"didn't replace it (fewer than {config.BATCH_MIN_TESTS} test runs, and "
                  f"no sign the valve moved)" if _b['old'] is not None else
                  "No opening point to remember from this batch")
    with _lock:
        row = batch.batch_summary(_b, _summaries, _name, _folder, remembered=stored)
        info.update(ended=datetime.now().isoformat(timespec='seconds'), results=row,
                    remembered=entry if stored else None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
    ok, message = workbook.append(config.BATCH_WORKBOOK, 'Batches', schema.BATCH_SUMMARY, row)
    if message:
        log_event(("" if ok else "WARNING: ") + message)
    if PLOT:
        _plot(folder_)


def _plot(folder_):
    """batch.png, drawn by TE_PLOTTER in its own process (so the plot can't
    disturb the driver, and the plotter's drawing is the only one)."""
    try:
        subprocess.Popen([sys.executable, str(_PLOTTER), folder_, "-o",
                          os.path.join(folder_, "batch.png"), "--no-show"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(_PLOTTER.parent),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log_event(f"Batch plot: {os.path.join(folder_, 'batch.png')}")
    except Exception as e:
        log_event(f"Batch plot not drawn — {type(e).__name__}: {e}")


def reset():
    """Back to no batch (tests)."""
    global _b, _folder, _name, _summaries
    with _lock:
        for fh in _files.values():
            fh.close()
        _files.clear()
        _rows.clear()
        _b, _folder, _name, _summaries = None, None, "", []
