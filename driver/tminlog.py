"""The t-min-tune results (history 36): logs/t-min.csv, one row per test,
and the estimates recomputed from it. The file is the single record of what
t-min-tune has learned: the code only appends to it (a newer version with
more columns adds them at the end, rewriting the file once, and fills in a
new column's value for old rows where it can be worked out from them), and
the logs folder is not in git, so no commit or update touches it.

    load(path=None) -> [row]                 rows as dicts of strings
    append(row, path=None, sheet=True) -> (ok, message)
    upgrade(path=None) -> (filled, message)  new columns, and the converged
                                             T_min filled in where missing
    fill_converged(rows, fit=None) -> n      the latter, on rows in memory
    at_target(row, target, slope) -> °C or None
    estimate(rows, seating, torque, target, fit=None) -> dict or None
    margin(est), converged(values)           the rules (tmin.py), here for convenience
"""

import csv
import math
import os
import statistics

from . import config, schema, workbook
from .openmap import rename_header, torque_key
from .tmin import (ABORTED_BAND, NO_OPENING, OPENED_AT_START, OPENED_OUT_OF_BAND,  # noqa: F401
                   SCOUT_RESULT as SCOUT, STOPPED_TEST as STOPPED, T_MIN, converged,
                   converged_value, margin)


def _path(path):
    return path or config.TMIN_CSV


def load(path=None):
    try:
        with open(_path(path), newline="", encoding="utf-8") as f:
            return [schema.upgrade_row(r) for r in csv.DictReader(f)]
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


def fill_converged(rows, fit=None):
    """Give each row that converged (converged = 1) without a
    t_min_converged_degC the value it would have been written with: the
    mean of its seating's last TMIN_CONVERGE_N counted results up to and
    including it, at that row's upstream target (history 38). Results at
    that target keep their t_min_at_target_degC; others are moved to it
    along the slope. Changes rows in place; returns how many were filled."""
    filled = 0
    for i, r in enumerate(rows):
        if str(r.get('converged', '')).strip() != '1' or \
                _num(r.get('t_min_converged_degC')) is not None:
            continue
        target = _num(r.get('upstream_target_bar'))
        k = slope_for(_num(r.get('torque_Nm')), fit)
        values = []
        for q in rows[:i + 1]:
            if q.get('seating') != r.get('seating') or not _counted(q):
                continue
            same = target is not None and _num(q.get('upstream_target_bar')) == target
            v = _num(q.get('t_min_at_target_degC')) if same else None
            values.append(v if v is not None else at_target(q, target, k))
        values = [v for v in values if v is not None]
        T = converged_value(values)
        if T is not None:
            r['t_min_converged_degC'] = _cell(round(T, 3))
            filled += 1
    return filled


def _rewrite(p, header, rows):
    tmp = p + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(c, '') for c in header])
    os.replace(tmp, p)


def upgrade(path=None):
    """Bring a file written by an older version up to date: renamed columns,
    new columns at the end, and the converged T_min filled in on rows that
    converged. Nothing already in it is changed or dropped. Returns (rows
    filled, message — '' when there was nothing to report)."""
    p = _path(path)
    try:
        rename_header(p)
        header = _header(p)
        if not header:
            return 0, ""
        missing = [c for c in schema.TMIN if c not in header]
        rows = load(p)
        filled = fill_converged(rows)
        if not missing and not filled:
            return 0, ""
        _rewrite(p, header + missing, rows)
    except OSError as e:
        return 0, f"t-min.csv not brought up to date — can't write {p}: {e}"
    return filled, (f"t-min.csv: converged T_min filled in on {filled} row"
                    f"{'s' * (filled != 1)}" if filled else "")


def append(row, path=None, sheet=True):
    """Add one test to the file (and a copy to the workbook's T_min sheet).
    A file written by an older version is brought up to date first
    (upgrade); nothing already in it is changed or dropped."""
    p = _path(path)
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        header = _header(p)
        if header is None or header == []:
            header = list(schema.TMIN)
            with open(p, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(header)
        else:
            upgrade(p)
            header = _header(p)
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


def opened_low(rows, torque):
    """The lowest temperature any test at this torque opened at while
    holding its start ('opened at start' rows), or None (history 41)."""
    vals = [_num(r.get('t_min_degC')) for r in rows
            if r.get('outcome') == OPENED_AT_START and _same_torque(_num(r.get('torque_Nm')), torque)]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def estimate(rows, seating, torque, target, fit=None):
    """The T_min expected at the target for this seating, as a dict
    (T, how, n, sd, values), or None. In order: this seating's counted
    results (the mean of the last TMIN_ESTIMATE_LAST_N); this seating's
    scout; the other seatings at this torque (the mean, over them, of each
    one's last TMIN_ESTIMATE_LAST_N counted results — history 40); the
    opening map's prediction for a new seating at this torque. n and sd are
    this seating's own (they set the margin)."""
    k = slope_for(torque, fit)
    own = [at_target(r, target, k) for r in rows
           if r.get('seating') == seating and _counted(r)]
    own = [v for v in own if v is not None]
    if own:
        last = own[-config.TMIN_ESTIMATE_LAST_N:]
        sd = statistics.stdev(last) if len(last) >= 2 else None
        return dict(T=statistics.mean(last), n=len(own), sd=sd, values=own,
                    how=f"this seating's {len(last)} latest result{'s' * (len(last) > 1)}")
    scouts = [at_target(r, target, k) for r in rows
              if r.get('seating') == seating and r.get('outcome') == SCOUT]
    scouts = [v for v in scouts if v is not None]
    if scouts:
        return dict(T=scouts[-1], n=0, sd=None, values=[], how="this seating's scout")
    by_seating = {}
    for r in rows:
        if r.get('seating') != seating and _counted(r) \
                and _same_torque(_num(r.get('torque_Nm')), torque):
            v = at_target(r, target, k)
            if v is not None:
                by_seating.setdefault(r.get('seating'), []).append(v)
    if by_seating:
        # each seating's latest results only: its early tests, walking down
        # from a high start, are upper bounds (1 Oct, 0.50 N·m: 135 → 120 °C)
        means = [statistics.mean(v[-config.TMIN_ESTIMATE_LAST_N:]) for v in by_seating.values()]
        m = len(means)
        return dict(T=statistics.mean(means), n=0, sd=None, values=[],
                    how=f"the mean of {m} other seating{'s' * (m > 1)} at {torque:.2f} N·m")
    if fit is not None:
        p = fit.predict(seating, torque, target)
        if p is not None:
            return dict(T=p[0], n=0, sd=None, values=[], how=f"the opening map ({p[1]})")
    return None
