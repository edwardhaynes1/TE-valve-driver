"""The t-min-tune results (history 36): logs/t-min.csv, one row per test,
and the estimates recomputed from it. The file is the single record of what
t-min-tune has learned: the code only appends to it (a newer version with
more columns adds them at the end, rewriting the header once), and the logs
folder is not in git, so no commit or update touches it.

    load(path=None) -> [row]                 rows as dicts of strings
    append(row, path=None, sheet=True) -> (ok, message)
    at_target(row, target, slope) -> °C or None
    estimate(rows, setting, torque, target, fit=None) -> dict or None
    margin(est), converged(values)           the rules (tmin.py), here for convenience
"""

import csv
import math
import os
import statistics

from . import config, schema, workbook
from .openmap import torque_key
from .tmin import (ABORTED_BAND, NO_OPENING, OPENED_AT_START, OPENED_OUT_OF_BAND,  # noqa: F401
                   SCOUT_RESULT as SCOUT, STOPPED_TEST as STOPPED, T_MIN, converged, margin)


def _path(path):
    return path or config.TMIN_CSV


def load(path=None):
    try:
        with open(_path(path), newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _cell(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, float):
        if not math.isfinite(v):
            return ''
        return f"{v:.6g}" if abs(v) < 1e-3 and v != 0 else round(v, 4)
    return v


def _header(path):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return next(csv.reader(f), None)
    except FileNotFoundError:
        return None


def append(row, path=None, sheet=True):
    """Add one test to the file (and a copy to the workbook's T_min sheet).
    A file written by an older version, with fewer columns, gains the new
    ones at the end first; nothing already in it is changed or dropped."""
    p = _path(path)
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        header = _header(p)
        if header is None or header == []:
            header = list(schema.TMIN)
            with open(p, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(header)
        missing = [c for c in schema.TMIN if c not in header]
        if missing:
            old = load(p)
            header = header + missing
            tmp = p + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(header)
                for r in old:
                    w.writerow([r.get(c, '') for c in header])
            os.replace(tmp, p)
        with open(p, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([_cell(row.get(c)) for c in header])
    except OSError as e:
        return False, f"t-min result NOT stored — can't write {p}: {e}"
    if sheet:
        ok, message = workbook.append(config.BATCH_WORKBOOK, 'T_min', schema.TMIN,
                                      {c: _cell(row.get(c)) for c in schema.TMIN})
        if not ok:
            return True, message
    return True, ""


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _same_torque(a, b):
    return a is not None and b is not None and abs(a - b) <= config.SEAT_SCREW_TOL_NM + 1e-9


def slope_for(torque, fit=None):
    """K per bar (signed: negative, T_min falls as upstream rises): the opening
    map's slope for this torque, else −PRESSURE_UP_K_PER_BAR."""
    if fit is not None and torque is not None:
        try:
            return fit.slope(torque_key(torque))
        except Exception:
            pass
    return -config.PRESSURE_UP_K_PER_BAR


def at_target(row, target, slope):
    """The row's T_min moved to the target pressure along the slope."""
    t = _num(row.get('t_min_degC'))
    p = _num(row.get('upstream_at_open_bar'))
    if t is None:
        return None
    if p is None or target is None:
        return t
    return t + slope * (target - p)


def _counted(r):
    return r.get('outcome') == T_MIN and str(r.get('counted')) in ('1', 'True', '1.0')


def estimate(rows, setting, torque, target, fit=None):
    """The T_min expected at the target for this setting, as a dict
    (T, how, n, sd, values), or None. In order: this setting's counted
    results (the mean of the last TMIN_ESTIMATE_LAST_N); this setting's
    scout; the other settings at this torque (their counted results); the
    opening map's prediction for a new setting at this torque. n and sd are
    this setting's own (they set the margin)."""
    k = slope_for(torque, fit)
    own = [at_target(r, target, k) for r in rows
           if r.get('setting') == setting and _counted(r)]
    own = [v for v in own if v is not None]
    if own:
        last = own[-config.TMIN_ESTIMATE_LAST_N:]
        sd = statistics.stdev(last) if len(last) >= 2 else None
        return dict(T=statistics.mean(last), n=len(own), sd=sd, values=own,
                    how=f"this setting's {len(last)} latest result{'s' * (len(last) > 1)}")
    scouts = [at_target(r, target, k) for r in rows
              if r.get('setting') == setting and r.get('outcome') == SCOUT]
    scouts = [v for v in scouts if v is not None]
    if scouts:
        return dict(T=scouts[-1], n=0, sd=None, values=[], how="this setting's scout")
    by_setting = {}
    for r in rows:
        if r.get('setting') != setting and _counted(r) \
                and _same_torque(_num(r.get('torque_Nm')), torque):
            v = at_target(r, target, k)
            if v is not None:
                by_setting.setdefault(r.get('setting'), []).append(v)
    if by_setting:
        means = [statistics.mean(v) for v in by_setting.values()]
        m = len(means)
        return dict(T=statistics.mean(means), n=0, sd=None, values=[],
                    how=f"the mean of {m} other setting{'s' * (m > 1)} at {torque:.2f} N·m")
    if fit is not None:
        p = fit.predict(setting, torque, target)
        if p is not None:
            return dict(T=p[0], n=0, sd=None, values=[], how=f"the opening map ({p[1]})")
    return None
