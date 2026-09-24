"""Runs a batch: its thread, its files, and its heater commands. The logic
is in batch.py; this module adds the clock, the threads and the I/O.

    start(n_tests, main_log="") -> (ok, message)   GUI: start batch
    abort(reason)                                  GUI: abort batch, or closing
    running() / status() / labels()                for the window and the log rows
    record_row(row)                                logger: a main-log row was written
    tick()                                         one step (the thread; tests)

Files, in logs/batches/<date>_<time>_<torque>Nm_<upstream>bar/:
    scout1.csv, scout2.csv, testrun01.csv …   the run's rows, main-log columns
    summary.csv                                a row per run (schema.RUN_SUMMARY)
    batch.json                                 settings at the start; results at the end
    batch.png                                  drawn by TE_PLOTTER at the end
and a row per run and per batch in the opening map (config.BATCH_WORKBOOK).
"""

import csv
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import batch, config, control, schema, shared, workbook
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


def start(n_tests, main_log="", start_thread=True):
    """Start a batch of n_tests test runs. Refused without a seat screw
    torque, while one is running, or while the heater is armed."""
    global _b, _folder, _name, _thread, _summaries
    torque = shared.seat_screw_torque()
    if torque is None:
        return False, "Batch not started — enter the seat screw torque first"
    if running():
        return False, "Batch not started — one is already running"
    if control.snapshot()['armed']:
        return False, "Batch not started — disarm the heater first; the batch arms it itself"
    if not 1 <= n_tests <= config.BATCH_TEST_RUNS_MAX:
        return False, f"Batch not started — test runs must be 1 to {config.BATCH_TEST_RUNS_MAX}"
    up, _ = shared.upstream()
    now = control.clock()
    up_txt = f"{up:.2f}bar" if up is not None else "upstream-unknown"
    name = f"{datetime.now():%Y%m%d_%H%M%S}_{torque:.2f}Nm_{up_txt}"
    path = os.path.join(config.BATCH_DIR, name)
    try:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "batch.json"), "w", encoding="utf-8") as f:
            json.dump(dict(batch=name, started=datetime.now().isoformat(timespec='seconds'),
                           seat_screw_torque_Nm=torque, upstream_at_start_bar=up,
                           test_runs=n_tests, main_log=main_log, settings=_settings()),
                      f, indent=2)
    except OSError as e:
        return False, f"Batch not started — can't create {path}: {e}"
    with _lock:
        _b = batch.new_batch(n_tests, torque, now)
        _folder, _name, _summaries = path, name, []
        _files.clear()
        _rows.clear()
    log_event(f"Batch started: {n_tests} test runs at {torque:.2f} N·m, upstream "
              f"{'%.3f bar' % up if up is not None else 'not read'} (measured) — {path}")
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
    with _lock:
        if _b is None or _b['state'] != batch.RUNNING:
            return
        cmds, msgs, events = batch.step(_b, control.clock(), r['te_temperature_degC'],
                                        r['vacuum_chamber_mbar'], r['vacuum_status'], h)
    _apply(cmds, msgs, events)


def abort(reason):
    with _lock:
        if _b is None or _b['state'] != batch.RUNNING:
            return
        cmds, msgs, events = batch.abort(_b, control.clock(), reason)
    _apply(cmds, msgs, events)


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
        row = batch.batch_summary(_b, _summaries, _name, _folder)
        path = os.path.join(_folder, "batch.json")
        try:
            with open(path, encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            info = {}
        info.update(ended=datetime.now().isoformat(timespec='seconds'), results=row)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
        folder_ = _folder
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
