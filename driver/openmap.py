"""The opening map: every opening ever measured, in one table, and one fit
over all of it (context.md, "Mapping the opening point"; history 34).

    load(path=None) -> [row]              the openings table (config.OPENINGS_CSV)
    append(row, path=None) -> (ok, msg)   one opening: to the table, and to the
                                          workbook's Openings sheet
    import_batches(folder=None, path=None) -> msg
                                          old batch folders into the table (once
                                          each): their creeping openings, one
                                          setting per batch
    rows_from_batch(folder) -> [row]      those rows for one batch folder
    fit(rows) -> Fit                      the opening-point fit (below)
    Fit.status(setting) -> dict           the status line's numbers for a setting
    Fit.status_text(setting) -> str       the status line
    Fit.predict(setting, torque, bar, deep=False) -> (°C, how) or None
    latest(rows) -> setting               the setting of the newest opening
    summary(path=None) -> str             the status line of the newest setting,
                                          and the optional terms (event log)

The fit, by least squares over every opening with an upstream reading:

    T_open = offset[setting] + slope[torque] × (upstream − MAP_REF_BAR)
             [+ warm-start × deep] [+ drift × hours] [+ background × Δlog10 p]

One offset per setting (one tightening of the seat screw), one pressure
slope per torque (shared by its settings). A torque whose openings span
less than MAP_MIN_SPREAD_BAR uses −PRESSURE_UP_K_PER_BAR ("assumed"). Each
optional term is tried on its own and kept only if significant at 95 %;
the ones not kept are still reported (with their ±) for information.

Pure Python (the driver doesn't need numpy): the systems are small (a
parameter per setting and per torque).
"""

import csv
import json
import math
import os
import statistics
from datetime import datetime

from . import config, schema, workbook

# ── the table ───────────────────────────────────────────────────────────────


def _path(path):
    return path or config.OPENINGS_CSV


def load(path=None):
    """The openings table as a list of dicts (strings, as in the file)."""
    p = _path(path)
    try:
        with open(p, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _write_rows(rows, path=None):
    p = _path(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    new = not os.path.exists(p) or os.path.getsize(p) == 0
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(schema.OPENINGS)
        for row in rows:
            w.writerow([_cell(row.get(c, '')) for c in schema.OPENINGS])


def _cell(v):
    if v is None:
        return ''
    if isinstance(v, float):
        return f"{v:.6g}" if abs(v) < 1e-3 and v != 0 else round(v, 4)
    return v


def append(row, path=None, sheet=True):
    """One opening: to the table (the fit's source), then a copy to the
    workbook's Openings sheet (a locked workbook gets a side file, as the
    other sheets do)."""
    try:
        _write_rows([row], path)
    except OSError as e:
        return False, f"Opening not stored — can't write {_path(path)}: {e}"
    if sheet:
        ok, message = workbook.append(config.BATCH_WORKBOOK, 'Openings', schema.OPENINGS,
                                      {c: _cell(row.get(c, '')) for c in schema.OPENINGS})
        if not ok:
            return True, message
    return True, ""


# ── importing old batch folders ─────────────────────────────────────────────

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _detect_rule(settings):
    """How a batch detected openings, from its batch.json settings."""
    a = settings.get('BATCH_DETECT_ABS_MBAR')
    r = settings.get('BATCH_DETECT_REL_DEC')
    if a is None and r is None and not settings:
        return "old: +12 % or +1e-7 mbar"
    parts = []
    if a:
        parts.append(f"+{a:g} mbar")
    if r:
        parts.append(f"+{100 * (10 ** r - 1):.0f} %")
    return " or ".join(parts) if parts else "unknown"


def rows_from_batch(folder):
    """The openings of one batch folder: every run that opened while
    creeping (scouts included — a creeping scout is as good an opening as
    any; a scout heated fast is not). One setting per batch. Cold starts
    (at least MAP_DEEP_GAP_K below where it opened) count as deep."""
    name = os.path.basename(os.path.normpath(folder))
    try:
        with open(os.path.join(folder, "summary.csv"), newline="", encoding="utf-8") as f:
            runs = list(csv.DictReader(f))
    except OSError:
        return []
    try:
        with open(os.path.join(folder, "batch.json"), encoding="utf-8") as f:
            settings = json.load(f).get('settings') or {}
    except (OSError, ValueError):
        settings = {}
    rule = _detect_rule(settings)
    return [row for row in (row_from_run(r, name, rule) for r in runs) if row]


def current_rule():
    """The detection rule in force, as the openings table names it."""
    return _detect_rule({'BATCH_DETECT_ABS_MBAR': config.BATCH_DETECT_ABS_MBAR,
                         'BATCH_DETECT_REL_DEC': config.BATCH_DETECT_REL_DEC})


def row_from_run(r, name, rule, note="imported from a batch"):
    """An openings row from a batch run's summary (schema.RUN_SUMMARY, as
    strings or numbers), or None if it isn't a creeping opening."""
    if r.get('status') != 'opened' or r.get('opened_during') != 'creep' \
            or _num(r.get('creep_degC_per_min')) is None:
        return None
    t_open = _num(r.get('t_open_degC'))
    if t_open is None:
        return None
    gap = _num(r.get('hold_gap_K'))
    return {
        'time': r.get('onset_time') or r.get('start_time') or '',
        'setting': name,
        'torque_Nm': _num(r.get('seat_screw_torque_Nm')),
        'upstream_bar': _num(r.get('upstream_at_open_bar')),
        't_open_degC': t_open,
        't_detect_degC': _num(r.get('t_detect_degC')),
        'baseline_mbar': _num(r.get('chamber_baseline_mbar')),
        'closed_degC': _num(r.get('free_cooling_closed_degC')),
        'bottom_degC': _num(r.get('hold_degC')),
        'bottom_s': _num(r.get('hold_s')),
        'deep': 1 if (gap is not None and gap >= config.MAP_DEEP_GAP_K) else 0,
        'refills': _num(r.get('refills')) or 0,
        'detect_rule': rule,
        'creep_degC_per_min': _num(r.get('creep_degC_per_min')),
        'source': f"batch {name}/{r.get('run')}",
        'note': note,
    }


def import_batches(folder=None, path=None, sheet=True):
    """Read every batch folder not yet in the table. Returns a line for the
    event log (blank if there was nothing new)."""
    folder = folder or config.BATCH_DIR
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return ""
    done = {r.get('source', '').split('/')[0] for r in load(path)}
    new, batches = [], 0
    for n in names:
        f = os.path.join(folder, n)
        if not os.path.isdir(f) or f"batch {n}" in done:
            continue
        rows = rows_from_batch(f)
        if rows:
            new += rows
            batches += 1
    if not new:
        return ""
    try:
        _write_rows(new, path)
    except OSError as e:
        return f"Openings: can't import the batch folders — {e}"
    if sheet:
        for row in new:
            workbook.append(config.BATCH_WORKBOOK, 'Openings', schema.OPENINGS,
                            {c: _cell(row.get(c, '')) for c in schema.OPENINGS})
    return (f"Openings: imported {len(new)} opening{'s' * (len(new) > 1)} from "
            f"{batches} batch folder{'s' * (batches > 1)}")


# ── least squares, small and pure ───────────────────────────────────────────

_T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
         2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
         2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def t975(dof):
    """Student t, two-sided 95 %."""
    if dof < 1:
        return None
    if dof <= len(_T975):
        return _T975[dof - 1]
    z = 1.959964
    return z + (z ** 3 + z) / (4 * dof) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * dof * dof)


def _invert(a):
    """Inverse of a small symmetric matrix (Gauss-Jordan with pivoting), or
    None if singular."""
    n = len(a)
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    scale = max((abs(v) for row in a for v in row), default=1.0) or 1.0
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-10 * scale:
            return None
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        m[c] = [v / piv for v in m[c]]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c]
                m[r] = [vr - f * vc for vr, vc in zip(m[r], m[c])]
    return [row[n:] for row in m]


def lstsq(x, y):
    """(coefficients, covariance, residuals, residual std, dof) or None
    (singular). x: rows of regressors."""
    p = len(x[0]) if x else 0
    n = len(y)
    if p == 0 or n < p:
        return None
    xtx = [[sum(r[i] * r[j] for r in x) for j in range(p)] for i in range(p)]
    inv = _invert(xtx)
    if inv is None:
        return None
    xty = [sum(r[i] * v for r, v in zip(x, y)) for i in range(p)]
    beta = [sum(inv[i][j] * xty[j] for j in range(p)) for i in range(p)]
    res = [v - sum(b * xi for b, xi in zip(beta, r)) for r, v in zip(x, y)]
    dof = n - p
    sd = math.sqrt(sum(e * e for e in res) / dof) if dof > 0 else None
    cov = [[(sd ** 2 if sd is not None else float('nan')) * v for v in row] for row in inv]
    return beta, cov, res, sd, dof


# ── the fit ─────────────────────────────────────────────────────────────────

def _tkey(torque):
    return f"{torque:.2f}"


def parse_time(s):
    """Epoch seconds of an ISO local time, or None."""
    return _time(s)


def _time(s):
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return None


OPTIONAL = ('warm-start', 'drift', 'background')
_UNITS = {'warm-start': 'K (deep − shallow)', 'drift': 'K/h', 'background': 'K/decade'}


class Fit:
    """The result of fit(rows). Attributes: n, sd (residual std, K), dof,
    offsets {setting: (°C at MAP_REF_BAR, ±95 %)}, slopes {torque key:
    (K/bar, ±95 % or None, 'fit'|'assumed')}, terms {name: (value, ±95 %,
    kept)}, torque_of {setting: torque key}, ranges {torque key: (min bar,
    max bar)}, points [(row index, setting, bar, T_open, residual, hours,
    deep)], excluded (openings without an upstream reading)."""

    def __init__(self):
        self.n, self.sd, self.dof = 0, None, 0
        self.offsets, self.slopes, self.terms = {}, {}, {}
        self.torque_of, self.ranges, self.counts = {}, {}, {}
        self.points, self.excluded = [], 0
        self.confounded = []             # (term, term, correlation) that move together
        self.spread = {}                 # torque key: the widest upstream span of one setting

    # prediction ---------------------------------------------------------
    def slope(self, tkey):
        s = self.slopes.get(tkey)
        return s[0] if s else -config.PRESSURE_UP_K_PER_BAR

    def predict(self, setting, torque, bar, deep=False):
        """(T_open °C, how) at this upstream pressure, or None: from the
        setting's offset, else the mean offset of its torque's other
        settings. Drift and background terms are left out (they describe
        the past); the warm-start term applies to deep cycles."""
        tk = _tkey(torque)
        if setting in self.offsets:
            off, how = self.offsets[setting][0], "this setting's fit"
        else:
            others = [o for s, (o, _) in self.offsets.items() if self.torque_of.get(s) == tk]
            if not others:
                return None
            off = statistics.mean(others)
            how = f"the mean of {len(others)} other setting{'s' * (len(others) > 1)} at " \
                  f"{float(tk):.2f} N·m"
        t = off + self.slope(tk) * ((bar if bar is not None else config.MAP_REF_BAR)
                                    - config.MAP_REF_BAR)
        w = self.terms.get('warm-start')
        if deep and w and w[2]:
            t += w[0]
        return t, how

    # status -------------------------------------------------------------
    def status(self, setting):
        tk = self.torque_of.get(setting)
        off = self.offsets.get(setting)
        slope = self.slopes.get(tk) if tk else None
        rng = self.ranges.get(tk) if tk else None
        done = bool(off and off[1] is not None and off[1] <= config.MAP_DONE_OFFSET_K
                    and slope and slope[2] == 'fit' and slope[1] is not None
                    and slope[1] <= config.MAP_DONE_SLOPE_K_BAR)
        hint = ""
        if not done:
            if not (slope and slope[2] == 'fit' and slope[1] is not None
                    and slope[1] <= config.MAP_DONE_SLOPE_K_BAR):
                own = [p['bar'] for p in self.points if p['setting'] == setting]
                hint = _pressure_hint((min(own), max(own)) if own else rng)
            else:
                hint = "more openings at any pressure"
        return dict(setting=setting, torque=tk, n=self.counts.get(setting, 0),
                    offset=off, slope=slope, range=rng, done=done, hint=hint)

    def status_text(self, setting):
        s = self.status(setting)
        if s['offset'] is None:
            return f"map: {setting} — no openings yet"
        o, oh = s['offset']
        parts = [f"{setting}", f"{s['n']} opening{'s' * (s['n'] != 1)}",
                 f"T_open {o:.1f} °C" + (f" ± {oh:.1f} K" if oh is not None else "")
                 + f" at {config.MAP_REF_BAR:g} bar"]
        if s['slope']:
            k, kh, how = s['slope']
            parts.append(f"{k:+.1f}" + (f" ± {kh:.1f}" if kh is not None else "")
                         + " K/bar" + (" (assumed)" if how == 'assumed' else ""))
        if s['range']:
            parts.append(f"{s['range'][0]:.2f}-{s['range'][1]:.2f} bar")
        parts.append("DONE" if s['done'] else (s['hint'] or "not done"))
        return "map: " + " · ".join(parts)

    def _tangled(self):
        return {n for a, b, _ in self.confounded for n in (a, b)}

    def terms_text(self):
        out = []
        for name in OPTIONAL:
            v = self.terms.get(name)
            if v is None:
                continue
            val, half, kept = v
            out.append(f"{name} {val:+.2f}" + (f" ± {half:.2f}" if half is not None else "")
                       + f" {_UNITS[name]}" + ("" if kept else
                                               " (not used)" if name in self._tangled()
                                               else " (not significant)"))
        for a, b, r in self.confounded:
            out.append(f"{a} and {b} can't be told apart (r = {r:+.2f}): neither used")
        return "; ".join(out)


def _pressure_hint(rng):
    """Where to take the upstream pressure next to pin the slope: beyond
    the end of this setting's range with more room, within
    MAP_P_MIN/MAX_BAR. Within the setting: the offsets absorb pressure
    changes between settings."""
    lo_lim, hi_lim, step = config.MAP_P_MIN_BAR, config.MAP_P_MAX_BAR, config.MAP_HINT_STEP_BAR
    if rng is None:
        return "take upstream pressure readings (Keller)"
    lo, hi = rng
    down, up = max(lo_lim, lo - step), min(hi_lim, hi + step)
    gain_down, gain_up = lo - down, up - hi
    if max(gain_down, gain_up) < 0.1:
        return "more openings (the pressure range is already wide)"
    target = down if gain_down > gain_up else up
    return f"slope needs a wider range: try ~{target:.1f} bar (without re-torquing)"


def _design(pts, settings, torques_fit, extra):
    """Rows of regressors: one column per setting (its offset), one per
    fitted torque (its slope × (bar − ref)), then the extra columns."""
    si = {s: i for i, s in enumerate(settings)}
    ti = {t: len(settings) + i for i, t in enumerate(torques_fit)}
    p = len(settings) + len(torques_fit) + len(extra)
    x = []
    for q in pts:
        r = [0.0] * p
        r[si[q['setting']]] = 1.0
        if q['tk'] in ti:
            r[ti[q['tk']]] = q['bar'] - config.MAP_REF_BAR
        for j, name in enumerate(extra):
            r[len(settings) + len(torques_fit) + j] = q[name]
        x.append(r)
    return x


def fit(rows):
    """The opening-point fit over the openings table (rows as load() gives,
    or dicts with numbers)."""
    f = Fit()
    pts = []
    for i, r in enumerate(rows):
        T, bar, tq = _num(r.get('t_open_degC')), _num(r.get('upstream_bar')), \
            _num(r.get('torque_Nm'))
        s = r.get('setting') or ''
        if T is None or tq is None or not s:
            continue
        if bar is None:
            f.excluded += 1
            continue
        base = _num(r.get('baseline_mbar'))
        pts.append(dict(i=i, setting=s, tk=_tkey(tq), bar=bar, T=T,
                        t=_time(r.get('time')), when=r.get('time') or '', deep=1.0 if _num(r.get('deep')) else 0.0,
                        lgb=math.log10(base) if base and base > 0 else None))
    if not pts:
        return f
    settings = sorted({q['setting'] for q in pts})
    for q in pts:
        f.torque_of[q['setting']] = q['tk']
        f.counts[q['setting']] = f.counts.get(q['setting'], 0) + 1
    torques = sorted({q['tk'] for q in pts})
    for tk in torques:
        bars = [q['bar'] for q in pts if q['tk'] == tk]
        f.ranges[tk] = (min(bars), max(bars))
    # Each setting has its own offset, so a slope can only be learned from
    # pressure changes WITHIN a setting (between settings the offsets absorb
    # it): a torque's slope is fitted once one of its settings spans
    # MAP_MIN_SPREAD_BAR, else assumed.
    spread = {}
    for s in settings:
        bars = [q['bar'] for q in pts if q['setting'] == s]
        tk = f.torque_of[s]
        spread[tk] = max(spread.get(tk, 0.0), max(bars) - min(bars))
    torques_fit = [tk for tk in torques if spread.get(tk, 0.0) >= config.MAP_MIN_SPREAD_BAR]
    f.spread = spread
    # the assumed slope moves to the left-hand side
    for q in pts:
        q['y'] = q['T'] - (0.0 if q['tk'] in torques_fit else
                           -config.PRESSURE_UP_K_PER_BAR * (q['bar'] - config.MAP_REF_BAR))
    # candidate regressors, centred
    first = {}
    for q in pts:
        if q['t'] is not None:
            first[q['setting']] = min(first.get(q['setting'], q['t']), q['t'])
    for q in pts:
        q['drift'] = ((q['t'] - first[q['setting']]) / 3600.0) if q['t'] is not None else 0.0
        q['hours'] = q['drift']
        q['warm-start'] = q['deep']
    # drift centred within each setting, so an offset stays the setting's
    # average, not its value at the first opening
    by_s = {}
    for q in pts:
        by_s.setdefault(q['setting'], []).append(q['drift'])
    for q in pts:
        q['drift'] -= statistics.mean(by_s[q['setting']])
    lgbs = [q['lgb'] for q in pts if q['lgb'] is not None]
    mlg = statistics.mean(lgbs) if lgbs else 0.0
    for q in pts:
        q['background'] = (q['lgb'] - mlg) if q['lgb'] is not None else 0.0

    def run(extra):
        x = _design(pts, settings, torques_fit, extra)
        return lstsq(x, [q['y'] for q in pts])

    # each optional term on its own: estimable, and significant?
    kept, tried = [], {}
    for name in OPTIONAL:
        if not _varies_within_setting(pts, name):
            continue
        out = run([name])
        if out is None:
            continue
        beta, cov, _, sd, dof = out
        j = len(settings) + len(torques_fit)
        tq_ = t975(dof)
        half = tq_ * math.sqrt(cov[j][j]) if (tq_ and sd is not None) else None
        sig = half is not None and abs(beta[j]) > half
        tried[name] = (beta[j], half, sig)
        if sig:
            kept.append(name)
    # Terms that move together within the settings (time and a background
    # still pumping down) can't be told apart: neither is used to correct —
    # attributing the trend to one of them would be arbitrary — and it's said.
    names = list(tried)
    for i, a in enumerate(names):
        for b_ in names[i + 1:]:
            r = _within_corr(pts, a, b_)
            if r is not None and abs(r) >= config.MAP_CONFOUND_R:
                f.confounded.append((a, b_, r))
    tangled = {n for a, b_, _ in f.confounded for n in (a, b_)}
    kept = [n for n in kept if n not in tangled]
    out = run(kept)
    if out is None:                                 # shouldn't happen: fall back
        kept, out = [], run([])
    if out is None:                                 # still singular: assume every slope
        for q in pts:
            q['y'] = q['T'] + config.PRESSURE_UP_K_PER_BAR * (q['bar'] - config.MAP_REF_BAR)
        torques_fit = []
        out = run([])
        if out is None:
            return f
    beta, cov, res, sd, dof = out
    f.n, f.sd, f.dof = len(pts), sd, dof
    tq_ = t975(dof)

    def half(j):
        return tq_ * math.sqrt(cov[j][j]) if (tq_ and sd is not None) else None

    for i, s in enumerate(settings):
        f.offsets[s] = (beta[i], half(i))
    for i, tk in enumerate(torques_fit):
        j = len(settings) + i
        f.slopes[tk] = (beta[j], half(j), 'fit')
    for tk in torques:
        if tk not in f.slopes:
            f.slopes[tk] = (-config.PRESSURE_UP_K_PER_BAR, None, 'assumed')
    for j, name in enumerate(kept):
        k = len(settings) + len(torques_fit) + j
        f.terms[name] = (beta[k], half(k), True)
    for name, v in tried.items():
        if name not in f.terms:
            f.terms[name] = (v[0], v[1], False)
    for q, e in zip(pts, res):
        f.points.append(dict(i=q['i'], setting=q['setting'], tk=q['tk'], bar=q['bar'],
                             T=q['T'], residual=e, t=q['t'], when=q['when'],
                             deep=q['deep'],
                             hours=q['hours']))
    return f


def _within_corr(pts, a, b):
    """How far two regressors move together where they vary: the
    correlation (after removing each setting's mean) within the settings
    that carry most of their variation. Pooling alone would dilute it: a
    batch where time and background fell together, fitted with sessions
    whose background was steady, still can't tell them apart. Returns the
    correlation of the settings carrying ≥ half of either term's variation
    (the strongest such), or None."""
    by = {}
    for q in pts:
        by.setdefault(q['setting'], []).append((q[a], q[b]))
    per = []                           # (sxx, syy, sxy) per setting
    for v in by.values():
        ma = statistics.mean(x for x, _ in v)
        mb = statistics.mean(y for _, y in v)
        sxx = sum((x - ma) ** 2 for x, _ in v)
        syy = sum((y - mb) ** 2 for _, y in v)
        sxy = sum((x - ma) * (y - mb) for x, y in v)
        per.append((sxx, syy, sxy))
    tx, ty = sum(p[0] for p in per), sum(p[1] for p in per)
    if tx <= 0 or ty <= 0:
        return None
    best = None
    for share_of in (0, 1):            # the settings carrying a's (then b's) variation
        tot = (tx, ty)[share_of]
        ranked = sorted(per, key=lambda p: -p[share_of])
        acc, sxx, syy, sxy = 0.0, 0.0, 0.0, 0.0
        for p in ranked:
            if acc >= 0.5 * tot:
                break
            acc += p[share_of]
            sxx, syy, sxy = sxx + p[0], syy + p[1], sxy + p[2]
        if sxx > 0 and syy > 0:
            r = sxy / math.sqrt(sxx * syy)
            if best is None or abs(r) > abs(best):
                best = r
    return best


def latest(rows):
    """The setting of the newest opening (by time), or None."""
    best = None
    for r in rows:
        t = _time(r.get('time'))
        if r.get('setting') and t is not None and (best is None or t > best[0]):
            best = (t, r['setting'])
    return best[1] if best else None


def summary(path=None):
    """The newest setting's status line and the optional terms, for the
    event log; '' with an empty table."""
    rows = load(path)
    if not rows:
        return ""
    f = fit(rows)
    s = latest(rows)
    line = f.status_text(s) if s else ""
    terms = f.terms_text()
    extra = f" ({f.excluded} without an upstream reading, not fitted)" if f.excluded else ""
    return (f"Opening map: {f.n} openings, {len(f.offsets)} setting"
            f"{'s' * (len(f.offsets) != 1)}{extra}; {line}" + (f"; {terms}" if terms else ""))


def _varies_within_setting(pts, name):
    """A term is only estimable if it varies within at least one setting
    (between settings the offsets absorb it) — and with at least 3 openings."""
    by = {}
    for q in pts:
        by.setdefault(q['setting'], []).append(q[name])
    return any(len(v) >= 3 and max(v) - min(v) > 1e-9 for v in by.values())
