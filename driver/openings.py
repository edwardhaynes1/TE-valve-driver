"""Remembered opening points: the result of the last batch at each seat screw
torque, kept in config.OPENINGS_FILE (logs/opening-points.json) so the next
batch can skip its scouts and auto-p can seek from a measured value instead
of the SEAT_SCREW_VALVE guesses. See context.md, "Remembered opening point".

    load(path=None) -> message     read the file (start-up); a missing file is
                                   simply no remembered points
    lookup(torque_nm) -> entry     the entry for this torque (within
                                   SEAT_SCREW_TOL_NM), or None
    for_controller(torque_nm)      (opening °C, upstream bar or None, detail)
                                   for controller.opening_point, or None
    remember(entry, path=None)     store an entry (replacing the one for its
        -> (ok, message)           torque) and write the file
    entries()                      a copy of everything
    describe(entry) -> str         one line for the event log and dialogs
    reset()                        forget everything in memory (tests)

An entry is a dict (batch.remembered_entry makes it): torque_Nm, t_open_C,
upstream_bar, k_per_bar, k_per_bar_from, scatter_K, n, ci95_K, from,
creep_C_min, settings, batch, date. The file is written whole each time,
through a temporary file, so a crash can't leave half of it.
"""

import json
import os
import threading

from . import config

_lock = threading.Lock()
_table = {}                      # "0.30" -> entry


def _key(torque_nm):
    return f"{torque_nm:.2f}"


def _path(path):
    return path or config.OPENINGS_FILE


def load(path=None):
    """Read the remembered opening points. Returns a line for the event log."""
    p = _path(path)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        with _lock:
            _table.clear()
        return "Remembered opening points: none yet"
    except (OSError, ValueError) as e:
        with _lock:
            _table.clear()
        return (f"Remembered opening points: can't read {os.path.basename(p)} "
                f"({type(e).__name__}: {e}) — using the config table")
    good = {k: v for k, v in (data.get("points") or {}).items()
            if isinstance(v, dict) and isinstance(v.get("t_open_C"), (int, float))
            and isinstance(v.get("torque_Nm"), (int, float))}
    with _lock:
        _table.clear()
        _table.update(good)
    if not good:
        return "Remembered opening points: none yet"
    return "Remembered opening points: " + "; ".join(
        f"{v['torque_Nm']:.2f} N·m {v['t_open_C']:.1f} °C" for _, v in sorted(good.items()))


def lookup(torque_nm):
    """The remembered entry nearest this torque, within SEAT_SCREW_TOL_NM."""
    if torque_nm is None:
        return None
    with _lock:
        best = None
        for v in _table.values():
            d = abs(v["torque_Nm"] - torque_nm)
            if d <= config.SEAT_SCREW_TOL_NM + 1e-9 and (best is None or d < best[0]):
                best = (d, v)
        return dict(best[1]) if best else None


def describe(entry):
    """e.g. '92.4 °C at 2.71 bar (±0.6 K, n = 4; batch 20260924_1612, 24 Sep 2026)'."""
    up = entry.get("upstream_bar")
    at = f" at {up:.2f} bar" if up is not None else " (upstream not read)"
    prec = []
    if entry.get("ci95_K") is not None:
        prec.append(f"±{entry['ci95_K']:.1f} K")
    if entry.get("n") is not None:
        prec.append(f"n = {entry['n']}")
    if entry.get("from") and entry["from"] != "test runs":
        prec.append(f"from {entry['from']}")
    src = []
    if entry.get("batch"):
        src.append(f"batch {entry['batch']}")
    if entry.get("date"):
        src.append(entry["date"])
    tail = "; ".join(x for x in (", ".join(prec), ", ".join(src)) if x)
    return f"{entry['t_open_C']:.1f} °C{at}" + (f" ({tail})" if tail else "")


def for_controller(torque_nm):
    """(opening °C, upstream bar or None, detail) for auto-p, or None."""
    e = lookup(torque_nm)
    if e is None:
        return None
    return (e["t_open_C"], e.get("upstream_bar"), f"remembered: {describe(e)}")


def entries():
    with _lock:
        return {k: dict(v) for k, v in _table.items()}


def remember(entry, path=None):
    """Store entry (replacing its torque's) and write the file. Returns
    (ok, message). On a write error the entry is still used this session."""
    p = _path(path)
    with _lock:
        _table[_key(entry["torque_Nm"])] = dict(entry)
        data = {"about": "Remembered TE-Valve opening points, one per seat screw torque "
                         "(written by the TE-VALVE-DRIVER batches; see context.md)",
                "points": {k: _table[k] for k in sorted(_table)}}
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        return False, (f"Opening point for {entry['torque_Nm']:.2f} N·m kept for this "
                       f"session only — can't write {os.path.basename(p)} "
                       f"({type(e).__name__}: {e})")
    return True, (f"Opening point for {entry['torque_Nm']:.2f} N·m remembered: "
                  f"{describe(entry)}")


def reset():
    with _lock:
        _table.clear()
