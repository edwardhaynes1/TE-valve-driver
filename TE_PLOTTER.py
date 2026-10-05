#!/usr/bin/env python3
"""
TE_PLOTTER.py

Plot and characterise a TE-Valve sensor log.

Panels (only those whose data is present are drawn):
  A  Supporting traces vs time, each on its own y-axis:
       raw upstream pressure (muted green, thin) and heater power (orange).
       Heater power is the switching-period mean: measured if the log has
       it, else calculated (duty × V²/R, or mean current × rail for older
       logs). P20 is no longer used: the Keller temperature is the sensor
       chip's, not the gas's, so the correction added artefacts.
  B  Main plot, larger, same time axis: chamber pressure (blue, log,
     left axis) and valve temperature (red, right axis). In auto-p
     runs the driver's pressure target is drawn dashed blue; while the
     heater is in auto-t, the temperature setpoint is
     drawn dotted red.

Valve open/close times are detected from the chamber pressure: the valve
counts as open while the pressure sits clearly above its fitted baseline
(dotted blue). Both plots get dashed vertical lines at those times.

The figure title gives the seat screw torque and the run's median upstream
pressure, e.g. "M_screw = 0.40 N·m, P_up ≈ 0.96 bar (abs)"; the log's
name and start date sit below it in small grey (history 49). If the torque
changed during the run, the title lists each value and both plots get a
dotted vertical line at each change. Logs without it say "M_screw not
recorded". The lock nut torque (history 44) is left out of the title but
still goes in the terminal summary, with its own dotted lines.

Step-response fits, upstream decay rates (raw pressure), heater energy and
the outgassing fit are printed to the terminal.

Column names come from driver/schema.py, the same definition the driver
writes with. Logs from older driver versions, whose names differ, fall back
to loose matching (COLUMN_ALIASES). Whatever it matched is printed at the
top of every run; anything it cannot match is skipped rather than fatal.

Batches: give a batch folder (or pick its summary.csv) for the batch view:
every run's chamber pressure against valve temperature (heating) and against
time since its onset, each run light grey (dashed: scouts and runs not
averaged) and their average in blue; and T_open against the measured
upstream pressure, with a straight-line fit (the rig leaks, so each run
opens at a different upstream pressure). The batch's T_open is given with
its 95 % interval after correcting for upstream pressure. The driver draws
this into the folder as batch.png when a batch ends.

Usage:
    python TE_PLOTTER.py                     # opens a file picker
    python TE_PLOTTER.py LOGFILE.csv         # skips the picker
    python TE_PLOTTER.py LOGFILE.csv -o OUT.png [--no-show]
    python TE_PLOTTER.py BATCH_FOLDER [-o OUT.png] [--no-show]
"""
import sys
sys.dont_write_bytecode = True   # keep __pycache__ folders out of the project

import argparse
import datetime
import re
import sys
import traceback

try:
    import matplotlib
    import matplotlib.pyplot as plt
    import matplotlib.transforms
    import numpy as np
    import pandas as pd
except ImportError as _exc:
    _missing = getattr(_exc, "name", None) or str(_exc)
    print(f"\nTE_PLOTTER needs the Python package '{_missing}', which is not "
          "installed for this Python.\nInstall the requirements with:\n\n"
          f'    "{sys.executable}" -m pip install numpy pandas matplotlib\n',
          file=sys.stderr)
    if len(sys.argv) == 1:               # double-clicked: keep the window open
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
    sys.exit(1)

# ---------------------------------------------------------------------------
# Column names written by the current driver (driver/schema.py). If the
# package isn't next to this file, the plotter still works on the aliases.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
try:
    from driver import schema as _schema
    SCHEMA_COLUMNS = set(_schema.MAIN)
except ImportError:
    SCHEMA_COLUMNS = set()

# Role -> column name in the current schema
ROLE_COLUMNS = {
    "time": "timestamp",
    "temp": "te_temperature_degC",
    "chamber": "vacuum_chamber_mbar",
    "upstream": "keller_pressure_bar",
    "p_target": "pressure_target_mbar",
    "t_setpoint": "heater_setpoint_degC",
    "keller_temp": "keller_temperature_degC",
    "duty": "heater_duty",
    "current_meas": "heater_I_mean_meas",
    "current_calc": "heater_I_mean_calc",
    "power_meas": "heater_P_mean_meas",
    "power_calc": "heater_P_mean_calc",
    "fault": "tc_fault",
    "mode": "heater_mode",
    "seat_screw": "seat_screw_torque_Nm",
    "lock_nut": "lock_nut_torque_Nm",
}
assert not SCHEMA_COLUMNS or set(ROLE_COLUMNS.values()) <= SCHEMA_COLUMNS, \
    "TE_PLOTTER.ROLE_COLUMNS names a column that driver/schema.py doesn't define"

# ---------------------------------------------------------------------------
# Fallback for older logs. Each role lists candidate name fragments, best first.
# Matching is case-insensitive and ignores spaces, hyphens and underscores.
# Add a fragment here if a future driver renames something.
# ---------------------------------------------------------------------------
COLUMN_ALIASES = {
    "time":     ["timestamp", "time", "datetime"],
    "temp":     ["tetemperature", "valvetemperature", "temperaturete",
                 "tctemperature", "thermocouple", "tetemp", "tempdegc"],
    "chamber":  ["vacuumchamber", "chamberpressure", "chambermbar",
                 "chamber", "vacuummbar", "ionguage", "iongauge"],
    "upstream": ["kellerpressure", "upstreampressure", "upstreambar",
                 "keller", "upstream", "inletpressure"],
    "p_target": ["pressuretarget", "targetmbar", "ptarget"],
    "t_setpoint": ["heatersetpoint", "setpointdegc", "tsetpoint"],
    "keller_temp": ["kellertemperature", "upstreamtemperature",
                    "inlettemperature", "kellertemp"],
    "duty":     ["heaterduty", "duty", "pwm", "heateroutput"],
    # current: measured is preferred, calculated is the fallback (see load)
    "current_meas": ["heaterimeanmeas", "currentmeas", "imeas"],
    "current_calc": ["heaterimeancalc", "currentcalc", "icalc",
                     "heatercurrent", "current"],
    # power: measured preferred, then calculated, then derived (see load)
    "power_meas": ["heaterpmeanmeas", "powermeas", "pmeas"],
    "power_calc": ["heaterpmeancalc", "powercalc", "pcalc", "heaterpower"],
    "fault":    ["tcfault", "fault"],
    "mode":     ["heatermode", "mode"],
    "seat_screw": ["seatscrewtorque", "seatscrew"],
    "lock_nut": ["locknuttorque", "locknut"],
}

# Fragments that disqualify a column for a given role, so that e.g.
# "n_keller_samples" is never mistaken for the Keller pressure trace.
COLUMN_EXCLUDE = {
    "temp":     ["keller", "setpoint", "samples", "count", "ambient"],
    "chamber":  ["temperature", "samples", "count", "setpoint"],
    "upstream": ["temperature", "samples", "count", "setpoint"],
    "keller_temp": ["samples", "count", "setpoint"],
    "duty":     ["setpoint", "samples", "count", "cmd"],
    "current_calc": ["meas"],
    "power_calc": ["meas"],
}

# Trace colours for the combined chart. Yellow and light green are the
# darker ends of those shades so they stay readable on a white background.
COLOURS = {
    "temp":     "#e01b1b",   # red
    "chamber":  "#1f4fe0",   # blue
    "current":  "#ff8c00",   # orange
    "duty":     "#e6c000",   # yellow
    "upstream": "#8cbf8c",   # muted green, secondary (whitish green would
                             # vanish on white; the driver's dark UI uses #cfe6cf)
    "power":    "#ff8c00",   # orange
}
LINE_WIDTH = {"upstream": 1.0}   # thinner than the default 1.4: secondary trace
HEATER_V_RAIL = 24.0       # for logs without a power column (match the driver)
HEATER_R_OHM = 88.0
FLIGHT_POWER_BUDGET_W = 1.0  # drawn dashed on the power axis
AXIS_OFFSET_PT = 62        # spacing between stacked y-axes on the same side

# Valve open/close detection on chamber pressure (all in log10 decades)
VALVE_MIN_RISE_DEC = 0.03  # smallest rise above baseline counted as open (≈ 7 %)
VALVE_NOISE_SIGMAS = 6.0   # ...or this many noise sigmas, whichever is larger
VALVE_MIN_OPEN_S = 3.0     # ignore excursions shorter than this
VALVE_MERGE_GAP_S = 3.0    # join excursions separated by a shorter dip
VALVE_BASELINE_DEG = 1     # polynomial order of the baseline drift (1 = linear)
VALVE_SEED_BIN_PERCENTILE = 40  # percentile of time-bin minima used to seed the baseline
VALVE_LINE_COLOUR = "0.15"
SEAT_SCREW_COLOUR = "#7b3fa0"      # seat screw torque changes (purple, dotted)
LOCK_NUT_COLOUR = "#1b7f6b"        # lock nut torque changes (teal, dotted)

MIN_STEP_SAMPLES = 120     # ignore duty segments shorter than this
JUMP_BAR = 0.02            # upstream step that marks a refill/adjustment
AMBIENT_WINDOW_S = 10.0    # window at start of run used for ambient temperature

INTERACTIVE = False        # set True when launched with no command-line args


def norm(name):
    return name.lower().replace("_", "").replace("-", "").replace(" ", "")


def resolve_columns(df):
    """Map each role to an actual column name, or None if nothing matches."""
    normed = {norm(c): c for c in df.columns}
    found = {}
    for role, aliases in COLUMN_ALIASES.items():
        if ROLE_COLUMNS.get(role) in df.columns:    # current schema: exact name
            found[role] = ROLE_COLUMNS[role]
            continue
        banned = COLUMN_EXCLUDE.get(role, [])
        allowed = {n: c for n, c in normed.items()
                   if not any(b in n for b in banned)}
        hit = None
        for alias in aliases:                       # exact normalised match first
            if alias in allowed:
                hit = allowed[alias]
                break
        if hit is None:
            for alias in aliases:                   # then substring match
                for n, original in allowed.items():
                    if alias in n:
                        hit = original
                        break
                if hit:
                    break
        found[role] = hit
    return found


def numeric(df, col):
    """Coerce a column to float, turning junk into NaN."""
    return pd.to_numeric(df[col], errors="coerce")


def pick_logfile():
    """Open a file dialog and return the chosen path, or None if cancelled."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        fail("No log file given and tkinter is not available on this machine.\n"
             "Pass the file on the command line instead:\n"
             "    python TE_PLOTTER.py te-sensor_YYYYMMDD_HHMMSS.csv")

    root = tk.Tk()
    root.withdraw()
    root.update()
    try:
        root.call("wm", "attributes", ".", "-topmost", True)
    except tk.TclError:
        pass
    path = filedialog.askopenfilename(
        title="Select a TE-Valve sensor log",
        initialdir=".",
        filetypes=[("TE-Valve logs", "te-sensor_*.csv"),
                   ("CSV files", "*.csv"),
                   ("All files", "*.*")],
    )
    root.destroy()
    return path or None


def fail(message):
    """Report a fatal problem without the window vanishing, then exit."""
    print("\n" + message + "\n", file=sys.stderr)
    if INTERACTIVE:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("TE_PLOTTER", message)
            root.destroy()
        except Exception:
            try:
                input("Press Enter to close...")
            except EOFError:
                pass
    sys.exit(1)


def load(path, cols):
    """Read the log, add elapsed time in seconds, drop rows with no temperature."""
    df = pd.read_csv(path)

    if cols["time"]:
        ts = pd.to_datetime(df[cols["time"]], errors="coerce")
        if ts.notna().sum() > 1:
            df["t"] = (ts - ts.dropna().iloc[0]).dt.total_seconds()
    if "t" not in df:                    # no usable timestamp: assume even spacing
        df["t"] = np.arange(len(df), dtype=float)
        print("  note: no usable timestamp column, using sample index as time")

    for role in ("temp", "chamber", "upstream", "duty", "keller_temp",
                 "current_meas", "current_calc", "power_meas", "power_calc",
                 "p_target", "t_setpoint", "seat_screw", "lock_nut"):
        if cols[role]:
            df[cols[role]] = numeric(df, cols[role])

    # The driver leaves the measured current blank unless a sense input is
    # wired, so use it only if it actually holds data.
    meas, calc = cols["current_meas"], cols["current_calc"]
    cols["current"] = meas if meas and df[meas].notna().any() else calc

    # Heater power (switching-period mean). The heater is fully on or off,
    # so mean power = duty × V²/R = mean current × rail — NOT mean V × mean I.
    cols["power"], cols["power_src"] = None, None
    pm, pc = cols["power_meas"], cols["power_calc"]
    if pm and df[pm].notna().any():
        cols["power"], cols["power_src"] = pm, "measured"
    elif pc and df[pc].notna().any():
        cols["power"], cols["power_src"] = pc, "calculated (logged)"
    elif cols["current"] and df[cols["current"]].notna().any():
        df["power_W"] = df[cols["current"]] * HEATER_V_RAIL
        cols["power"], cols["power_src"] = "power_W", "derived: mean current × rail"
    elif cols["duty"] and df[cols["duty"]].notna().any():
        df["power_W"] = df[cols["duty"]] * HEATER_V_RAIL ** 2 / HEATER_R_OHM
        cols["power"], cols["power_src"] = "power_W", "derived: duty × V²/R"

    if cols["temp"]:
        df = df.dropna(subset=[cols["temp"]])
    return df.reset_index(drop=True)


def fit_first_order(t, T):
    """Fit T = T_inf + (T_0 - T_inf)·exp(-t/tau), NumPy only.

    For a fixed tau the model is linear in T_inf and T_0, so this scans tau
    on a log grid, solves each case by least squares, then refines around
    the best value. Returns (T_inf, T_0, tau), or NaNs if it cannot fit."""
    t = np.asarray(t, float)
    T = np.asarray(T, float)
    ok = np.isfinite(t) & np.isfinite(T)
    t, T = t[ok], T[ok]
    if len(t) < 5 or t[-1] <= 0:
        return np.nan, np.nan, np.nan

    def solve(tau):
        e = np.exp(-t / tau)
        A = np.column_stack([1.0 - e, e])          # T = T_inf·(1-e) + T_0·e
        coef, *_ = np.linalg.lstsq(A, T, rcond=None)
        return float(np.sum((A @ coef - T) ** 2)), coef

    span = t[-1]
    grid = np.geomspace(max(span / 1000, 1e-3), span * 20, 120)
    for _ in range(3):                             # coarse scan, then zoom in twice
        sse = [solve(tau)[0] for tau in grid]
        i = int(np.argmin(sse))
        lo, hi = grid[max(i - 1, 0)], grid[min(i + 1, len(grid) - 1)]
        grid = np.geomspace(lo, hi, 40)
    tau = float(grid[int(np.argmin([solve(x)[0] for x in grid]))])
    (T_inf, T_0) = solve(tau)[1]
    return float(T_inf), float(T_0), tau


def fit_steps(df, cols, ambient):
    """Fit a first-order response to each constant-duty segment."""
    if not cols["duty"] or not cols["temp"]:
        return pd.DataFrame()
    duty = df[cols["duty"]].round(3)
    groups = (duty != duty.shift()).cumsum()
    steps = []
    for _, seg in df.groupby(groups):
        if len(seg) < MIN_STEP_SAMPLES:
            continue
        t = seg.t.values - seg.t.values[0]
        T = seg[cols["temp"]].values
        try:
            T_inf, _T0, tau = fit_first_order(t, T)
        except (np.linalg.LinAlgError, ValueError):
            T_inf, tau = np.nan, np.nan
        # Reject runaway fits: a segment shorter than its own time constant
        # cannot constrain the asymptote.
        if not np.isfinite(tau) or tau <= 0 or tau > t[-1] or not (
                ambient - 50 < T_inf < T.max() + 100):
            T_inf, tau = np.nan, np.nan
        d = float(seg[cols["duty"]].iloc[0])
        steps.append(dict(
            duty=d, t0=seg.t.iloc[0], t1=seg.t.iloc[-1], T_inf=T_inf, tau=tau,
            dT=T_inf - ambient,
            K_per_duty=(T_inf - ambient) / d if d > 0 else np.nan,
            drift=np.polyfit(seg.t.values[-120:], T[-120:], 1)[0] * 60,
        ))
    return pd.DataFrame(steps)


def upstream_segments(df, cols):
    """Split the upstream trace at refills and fit a decay rate to each piece."""
    col = cols["upstream"]
    if not col:
        return []
    p = df[col]
    segs = []
    for _, seg in df.groupby((p.diff().abs() > JUMP_BAR).cumsum()):
        seg = seg.dropna(subset=[col])
        if len(seg) < 120:
            continue
        slope = np.polyfit(seg.t, seg[col], 1)[0]
        segs.append(dict(
            t0=seg.t.iloc[0], t1=seg.t.iloc[-1],
            rate_mbar_min=slope * 60 * 1000,
            mean_T=seg[cols["temp"]].mean() if cols["temp"] else np.nan,
            data=seg))
    return segs


def fit_outgassing(df, cols, baseline_T=70.0):
    """Fit chamber pressure excess above the cold baseline to exp(T/T_scale)."""
    if not cols["chamber"] or not cols["temp"]:
        return None, None, None, None
    C, T = df[cols["chamber"]], df[cols["temp"]]
    base = C[T < baseline_T].median()
    if not np.isfinite(base):
        base = C.median()
    hot = df[T > baseline_T + 15]
    if len(hot) < 20:
        return base, None, None, None
    excess = hot[cols["chamber"]] - base
    good = excess > 0.1 * base
    if good.sum() < 20:
        return base, None, None, None
    slope, intercept = np.polyfit(hot[cols["temp"]][good], np.log(excess[good]), 1)
    if slope <= 0:
        return base, None, None, None
    return base, np.exp(intercept), 1.0 / slope, np.log(2) / slope


def detect_valve_events(df, cols):
    """Find when the chamber pressure leaves and returns to its baseline.

    The baseline is a low-order fit of log10(p) against time. It starts from
    the samples near the lowest pressure and is refitted with the elevated points
    excluded until it settles, so a slow pump-down drift is followed even if
    the valve is open for most of the log. Noise comes from sample-to-sample
    differences. The valve counts as open while log10(p) is more than
    max(VALVE_MIN_RISE_DEC, VALVE_NOISE_SIGMAS * noise) above the baseline.

    Returns (baseline_mbar as a Series aligned to df, or None,
             list of dicts with t_open / t_close; None = outside the log)."""
    col = cols["chamber"]
    if not col:
        return None, []
    p = df[col]
    ok = (p > 0).to_numpy() & p.notna().to_numpy()
    if ok.sum() < 20:
        return None, []
    t = df.t.to_numpy()[ok]
    y = np.log10(p.to_numpy()[ok])

    # Noise from sample-to-sample differences: a valve opening is a handful
    # of large steps among many small ones, so it barely moves this estimate
    # even when the valve is open for most of the log.
    d = np.diff(y)
    sigma = 1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2)
    thresh = max(VALVE_MIN_RISE_DEC, VALVE_NOISE_SIGMAS * sigma)

    # Find the seed level from each time bin's own lowest point, not from
    # an overall percentile of all points: if the chamber keeps pumping down
    # over the run, the end of the log can sit below the start, and whichever
    # side happens to hold more samples then dominates an overall percentile,
    # hiding a real opening on the other side (bug: 21 Sept 2026 — see
    # software-history-log.md).
    #
    # A bin fully inside an open period has no genuinely quiet sample, so its
    # minimum is not trustworthy on its own; VALVE_SEED_BIN_PERCENTILE (40)
    # takes the level below which a bin's minimum typically falls, which is
    # only close to the true baseline for the bins that actually reach it, at
    # any level of imbalance up to the valve being open in most of the log.
    # Seeding straight from these bin minima, rather than from this derived
    # level, would instead let an all-open bin's minimum anchor the baseline:
    # the refinement below only excludes points *above* the fit, so a run of
    # such points, once seeded, never gets removed.
    span = t[-1] - t[0]
    n_bins = min(30, max(6, len(t) // 20))
    bin_of = np.clip(np.searchsorted(
        np.linspace(t[0], t[-1], n_bins + 1)[1:-1], t), 0, n_bins - 1)
    bin_mins = [y[bin_of == b].min() for b in range(n_bins) if (bin_of == b).any()]
    seed_level = np.percentile(bin_mins, VALVE_SEED_BIN_PERCENTILE)
    quiet = y <= seed_level + thresh / 2
    for _ in range(20):
        deg = VALVE_BASELINE_DEG if np.ptp(t[quiet]) >= 0.4 * span else 0
        coef = np.polyfit(t[quiet], y[quiet], deg)
        resid = y - np.polyval(coef, t)
        new_quiet = resid < thresh / 2      # keep the tails out of the fit too
        if new_quiet.sum() < 10 or np.array_equal(new_quiet, quiet):
            break
        quiet = new_quiet

    baseline = pd.Series(10 ** np.polyval(coef, df.t.to_numpy()), index=df.index)

    # Runs of samples above threshold -> (start index, end index exclusive)
    above = resid > thresh
    edges = np.flatnonzero(np.diff(np.r_[0, above.astype(int), 0]))
    runs = [[a, b] for a, b in zip(edges[::2], edges[1::2])]
    merged = []
    for a, b in runs:
        if merged and t[a] - t[merged[-1][1] - 1] < VALVE_MERGE_GAP_S:
            merged[-1][1] = b
        else:
            merged.append([a, b])

    events = []
    for a, b in merged:
        if t[b - 1] - t[a] < VALVE_MIN_OPEN_S:
            continue
        events.append(dict(
            t_open=None if a == 0 else float(t[a]),
            t_close=None if b >= len(t) else float(t[b]),
            peak=float(10 ** y[a:b].max()),
            base=float(10 ** np.polyval(coef, t[a])),
            thresh_pct=100 * (10 ** thresh - 1)))
    return baseline, events


def seat_screw_history(df, cols, role="seat_screw"):
    """[(t, N·m or None), …]: the value at the start and at each change.
    None means not recorded. Returns None if the log has no such column."""
    col = cols.get(role)
    if not col:
        return None
    history, last = [], object()
    for t, v in zip(df.t, df[col]):
        v = None if pd.isna(v) else float(v)
        if v != last:
            history.append((float(t), v))
            last = v
    return history


def lock_nut_history(df, cols):
    """As seat_screw_history, for the lock nut torque (history 44)."""
    return seat_screw_history(df, cols, role="lock_nut")


def lock_nut_text(history):
    """'lock nut torque 0.10 N·m' (or its changes); '' for a log from
    before the column, which then says nothing about it."""
    return "" if history is None else seat_screw_text(history, what="lock nut")


def torques_text(seat_screw, lock_nut=None):
    """The seat screw text, and the lock nut's after it when there is one."""
    nut = lock_nut_text(lock_nut)
    return seat_screw_text(seat_screw) + (f"  ·  {nut}" if nut else "")


def seat_screw_text(history, what="seat screw"):
    """'seat screw torque 0.40 N·m', or the sequence of values if it changed."""
    if history is None:
        return f"{what} torque not recorded (log predates the column)"
    def fmt(v):
        return "not recorded" if v is None else f"{v:.2f} N·m"
    if not history:
        return f"{what} torque not recorded"
    text = f"{what} torque " + fmt(history[0][1])
    for t, v in history[1:]:
        text += f" → {fmt(v)} at {t:.1f} s"
    return text


def m_screw_text(history):
    """'M_screw = 0.40 N·m', or '0.40 → 0.45 N·m' if it changed. Blanks are
    skipped (the torque is often typed in a few seconds after the start);
    'M_screw not recorded' if it never was."""
    values = []
    for _, v in history or []:
        if v is not None and (not values or v != values[-1]):
            values.append(v)
    if not values:
        return "M_screw not recorded"
    return "M_screw = " + " → ".join(f"{v:.2f}" for v in values) + " N·m"


def figure_title(df, cols, seat_screw):
    """'M_screw = 0.40 N·m, P_up ≈ 0.96 bar (abs)'. P_up is the run's median
    upstream pressure, so a refill spike does not move it; left out if the
    log has none."""
    parts = [m_screw_text(seat_screw)]
    col = cols.get("upstream")
    if col and df[col].notna().any():
        parts.append(f"P_up ≈ {df[col].median():.2f} bar (abs)")
    return ", ".join(parts)


def figure_subtitle(filename):
    """The log's name and, if the name carries one, its start date:
    'te-sensor_20261001_144601.csv  ·  1 Oct 2026 14:46'."""
    m = re.search(r"(\d{8})_(\d{6})", filename)
    if not m:
        return filename
    try:
        d = datetime.datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S")
    except ValueError:
        return filename
    return f"{filename}  ·  {d.day} {d:%b %Y %H:%M}"


def mark_seat_screw(axes, history, label_ax, what="seat screw", colour=SEAT_SCREW_COLOUR):
    """Dotted vertical lines where the seat screw (or lock nut) torque
    changed mid-run."""
    trans = matplotlib.transforms.blended_transform_factory(
        label_ax.transData, label_ax.transAxes)
    for t, v in (history or [])[1:]:
        for ax in axes:
            ax.axvline(t, ls=":", lw=1.4, color=colour, zorder=5)
        label = f"{what} not recorded" if v is None else f"{what} {v:.2f} N·m"
        label_ax.text(t, 0.02, f" {label} ", transform=trans, rotation=90,
                      ha="right", va="bottom", fontsize=8, color=colour,
                      zorder=6, bbox=dict(fc="white", ec="none", alpha=0.8, pad=1))


def mark_valve_events(axes, events, label_ax):
    """Dashed vertical lines at valve open/close; labels on label_ax."""
    trans = matplotlib.transforms.blended_transform_factory(
        label_ax.transData, label_ax.transAxes)
    for ev in events:
        for key, word in (("t_open", "valve open"), ("t_close", "valve closed")):
            x = ev[key]
            if x is None:
                continue
            for ax in axes:
                ax.axvline(x, ls="--", lw=1.2, color=VALVE_LINE_COLOUR, zorder=5)
            label_ax.text(x, 0.98, f" {word} {x:.1f} s ", transform=trans,
                          rotation=90, ha="right", va="top", fontsize=8,
                          color=VALVE_LINE_COLOUR, zorder=6,
                          bbox=dict(fc="white", ec="none", alpha=0.8, pad=1))


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------
def panel_timeseries(ax, df, cols, roles=None, sides=None):
    """Time series on one chart, each on its own colour-coded y-axis.
    roles: optional trace roles to draw, in this order (default: all present).
    sides: optional {role: "left"/"right"} overriding the default axis side.

    The first trace present uses the host axis on the left; the rest get
    twin axes, stacked outward on the left or right side."""
    series = [  # role, axis label, scale, side, style
        ("temp",     "valve temperature (°C)",       1,   "left",  "line"),
        ("upstream", "upstream pressure (bar abs)",  1,   "left",  "line"),
        ("chamber",  "chamber pressure (mbar)",      1,   "right", "log"),
        ("power",    "heater power (W)",             1,   "right", "line"),
        ("current",  "heater current (A)",           1,   "right", "line"),
        ("duty",     "heater duty (%)",              100, "right", "step"),
    ]
    if roles is not None:
        by_role = {s[0]: s for s in series}
        series = [by_role[r] for r in roles if r in by_role]
    sides = sides or {}
    series = [(r, lab, k, sides.get(r, side), sty)
              for r, lab, k, side, sty in series]
    present = [s for s in series if cols.get(s[0])]
    used = {"left": 0, "right": 0}
    handles = []
    axes_by_role = {}

    for i, (role, label, scale, side, style) in enumerate(present):
        colour = COLOURS[role]
        if i == 0:
            a, side = ax, "left"
        else:
            a = ax.twinx()
            if side == "left":
                a.yaxis.tick_left()
                a.yaxis.set_label_position("left")
                a.spines["right"].set_visible(False)
            a.spines[side].set_position(
                ("outward", AXIS_OFFSET_PT * used[side]))
        used[side] += 1

        y = df[cols[role]] * scale
        if style == "log":
            y = y.where(y > 0)
            a.set_yscale("log")
        if style == "step":
            (h,) = a.step(df.t, y, where="post", lw=1.4, color=colour)
            a.set_ylim(0, 105)
        else:
            (h,) = a.plot(df.t, y, lw=LINE_WIDTH.get(role, 1.4), color=colour)
        h.set_label(label.split(" (")[0])
        handles.append(h)
        axes_by_role[role] = a

        if role == "power":
            hb = a.axhline(FLIGHT_POWER_BUDGET_W, ls="--", lw=1.0, color=colour,
                           alpha=0.7, label=f"{FLIGHT_POWER_BUDGET_W:g} W flight budget")
            handles.append(hb)
            a.set_ylim(bottom=0)
        a.set_ylabel(label, color=colour)
        a.tick_params(axis="y", colors=colour)
        a.spines[side].set_color(colour)
        a.spines[side].set_linewidth(1.5)

    ax.set_xlabel("time (s)")
    ax.set_xlim(df.t.iloc[0], df.t.iloc[-1])
    ax.grid(alpha=0.3)
    ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.01),
              ncol=len(handles), frameon=False, fontsize=9,
              handlelength=1.8, borderaxespad=0)
    return axes_by_role


MAIN_ROLES = ("chamber", "temp")                      # lower, larger plot
MAIN_SIDES = {"chamber": "left", "temp": "right"}
TOP_ROLES = ("upstream", "power")                      # upper overview (duty and
                                                       # current omitted: power tracks them)


def add_to_legend(ax, handle):
    """Append one line to the legend drawn above ax by panel_timeseries."""
    leg = ax.get_legend()
    old = getattr(leg, "legend_handles", None) or leg.legendHandles  # mpl < 3.7
    handles = list(old) + [handle]
    labels = [x.get_text() for x in leg.get_texts()] + [handle.get_label()]
    ax.legend(handles=handles, labels=labels, loc="lower left",
              bbox_to_anchor=(0, 1.01), ncol=len(handles), frameon=False,
              fontsize=9, handlelength=1.8, borderaxespad=0)


def make_figure(df, cols, steps, segs, outgas, title, valve=(None, []),
                seat_screw=None, lock_nut=None):
    """Supporting traces on top; larger chamber + temperature plot below.
    Both share one time axis."""
    has_main = any(cols.get(r) for r in MAIN_ROLES)
    has_top = any(cols.get(r) for r in TOP_ROLES)
    if has_main and has_top:
        fig, (ax_t, ax_m) = plt.subplots(
            2, 1, figsize=(14, 12), sharex=True,
            gridspec_kw={"height_ratios": [1, 1.7]})
    else:
        fig, ax = plt.subplots(figsize=(14, 7 if has_main else 5.5))
        ax_t, ax_m = (None, ax) if has_main else (ax, None)
    fig.suptitle(figure_title(df, cols, seat_screw), fontsize=13, y=0.995)
    fig.text(0.5, 0.968, figure_subtitle(title), ha="center", va="top",
             fontsize=9, color="0.45")

    if ax_t is not None:
        panel_timeseries(ax_t, df, cols, roles=TOP_ROLES)
        if ax_m is not None:
            ax_t.set_xlabel("")          # shared axis: label the bottom plot only
    main_axes = {}
    if ax_m is not None:
        main_axes = panel_timeseries(ax_m, df, cols, roles=MAIN_ROLES,
                                     sides=MAIN_SIDES)

    baseline, events = valve
    if baseline is not None and "chamber" in main_axes:
        main_axes["chamber"].plot(df.t, baseline, ls=":", lw=1.2,
                                  color=COLOURS["chamber"], alpha=0.7)
    tcol = cols.get("p_target")
    if tcol and "chamber" in main_axes and df[tcol].notna().any():
        (h,) = main_axes["chamber"].step(
            df.t, df[tcol].where(df[tcol] > 0), where="post", ls="--", lw=1.4,
            color=COLOURS["chamber"], alpha=0.8, label="pressure target")
        add_to_legend(ax_m, h)
        last = df[tcol].dropna()
        print(f"\npressure target (from log): {last.iloc[0]:.2e} → {last.iloc[-1]:.2e} mbar")

    scol = cols.get("t_setpoint")
    if scol and "temp" in main_axes:
        sp = df[scol]
        if cols.get("mode"):             # temperature-control mode only
            # temperature mode is 'auto-t' (named 'auto' in logs before 17 Sept 2026)
            sp = sp.where(df[cols["mode"]].astype(str).str.strip().str.lower()
                          .isin(["auto-t", "auto"]))
        if sp.notna().any():
            (h,) = main_axes["temp"].step(df.t, sp, where="post", ls=":", lw=1.8,
                                          color=COLOURS["temp"], label="temperature setpoint")
            add_to_legend(ax_m, h)

    hosts = [a for a in (ax_t, ax_m) if a is not None]
    if events:
        mark_valve_events(hosts, events, ax_m if ax_m is not None else ax_t)
    if seat_screw and len(seat_screw) > 1:
        mark_seat_screw(hosts, seat_screw, ax_m if ax_m is not None else ax_t)
    if lock_nut and len(lock_nut) > 1:
        mark_seat_screw(hosts, lock_nut, ax_m if ax_m is not None else ax_t,
                        what="lock nut", colour=LOCK_NUT_COLOUR)

    fig.tight_layout(rect=(0, 0, 1, 0.955))
    return fig


def report(path, df, cols, steps, segs, outgas, ambient, valve=(None, []),
           seat_screw=None, lock_nut=None):
    print(f"\nfile      : {path}")
    print(f"duration  : {df.t.iloc[-1]:.0f} s   samples: {len(df)}")
    print(f"TE-Valve  : {torques_text(seat_screw, lock_nut)}")
    if cols["temp"]:
        print(f"ambient   : {ambient:.1f} °C")
        print(f"max |ΔT| between samples: "
              f"{df[cols['temp']].diff().abs().max():.2f} K", end="")
        if cols["fault"]:
            print(f"   tc faults: {int((df[cols['fault']] != 0).sum())}", end="")
        print()

    if len(steps):
        print("\nduty steps")
        print(f"{'duty':>6} {'window (s)':>14} {'T_inf':>8} {'dT':>7} {'tau':>7} "
              f"{'K/duty':>8} {'drift':>12}")
        for s in steps.itertuples():
            print(f"{s.duty*100:5.0f}% {s.t0:6.0f}-{s.t1:<7.0f} {s.T_inf:7.1f}  "
                  f"{s.dT:6.1f}  {s.tau:6.0f}  {s.K_per_duty:7.1f}  "
                  f"{s.drift:+7.2f} K/min")
        valid = steps[steps.K_per_duty.notna()]
        if len(valid) > 1:
            droop = 100 * (1 - valid.K_per_duty.iloc[-1] / valid.K_per_duty.iloc[0])
            print(f"\nK-per-duty droop across the staircase: {droop:.1f}% "
                  "(thermal resistance falling as the element gets hotter)")

    if cols.get("power"):
        pw = df[cols["power"]]
        ok = pw.notna()
        if ok.sum() > 1:
            energy = np.trapezoid(pw[ok], df.t[ok]) if hasattr(np, "trapezoid") \
                else np.trapz(pw[ok], df.t[ok])
            print(f"\nheater power ({cols['power_src']})")
            print(f"  mean {pw.mean():.3f} W   peak {pw.max():.3f} W   "
                  f"energy {energy:.0f} J over the log")
            above = (pw > FLIGHT_POWER_BUDGET_W).mean() * 100
            print(f"  above the {FLIGHT_POWER_BUDGET_W:g} W flight budget "
                  f"{above:.0f} % of the time")

    if segs:
        print("\nupstream segments (raw pressure, bar abs)")
        for s in segs:
            T = f"{s['mean_T']:5.1f} °C" if np.isfinite(s["mean_T"]) else "  n/a"
            print(f"  t {s['t0']:6.0f}-{s['t1']:<6.0f}s  "
                  f"{s['rate_mbar_min']:+7.2f} mbar/min   mean T {T}")
        decaying = [s["rate_mbar_min"] for s in segs if s["rate_mbar_min"] < -0.5]
        if len(decaying) > 1:
            print(f"  spread across the {len(decaying)} decaying segments: "
                  f"{max(decaying) - min(decaying):.2f} mbar/min")
            print("  near zero => the decay is independent of valve state, "
                  "i.e. a leak rather than throughput")

    base, pre, T_scale, doubling = outgas
    if base is not None and np.isfinite(base):
        print(f"\nchamber cold baseline: {base:.3e} mbar")
    if pre is not None:
        print(f"excess fits exp(T/{T_scale:.1f} K), doubling every {doubling:.1f} K")
        print("  exponential in T => thermal desorption; a conductance gap "
              "opening would give a power law in (T - T_onset)")

    _, events = valve
    if cols["chamber"]:
        print("\nvalve events (chamber pressure above fitted baseline)")
        if not events:
            print("  none detected")
        for ev in events:
            o = f"{ev['t_open']:7.1f} s" if ev["t_open"] is not None else "  (before log)"
            c = f"{ev['t_close']:7.1f} s" if ev["t_close"] is not None else "  (after log)"
            dur = (f"{ev['t_close'] - ev['t_open']:6.1f} s"
                   if ev["t_open"] is not None and ev["t_close"] is not None else "    n/a")
            print(f"  open {o}   closed {c}   open for {dur}   "
                  f"peak {ev['peak']:.2e} mbar (baseline {ev['base']:.2e}, "
                  f"threshold +{ev['thresh_pct']:.0f} %)")
            if cols["temp"]:
                temps = []
                for key, word in (("t_open", "opened"), ("t_close", "closed")):
                    if ev[key] is not None:
                        i = (df.t - ev[key]).abs().idxmin()
                        temps.append(f"{word} at {df[cols['temp']][i]:.1f} °C")
                if temps:
                    print("    valve temperature: " + ", ".join(temps))


# ---------------------------------------------------------------------------
# Batch view
# ---------------------------------------------------------------------------
BATCH_BIN_K = 0.5            # temperature bins for the averaged curve
BATCH_ALIGN_S = (-60, 180)   # time window around each run's onset


def load_batch(folder):
    """(summary DataFrame, {run: trace DataFrame with t, T, p, phase})."""
    from pathlib import Path
    folder = Path(folder)
    summary = pd.read_csv(folder / "summary.csv")
    traces = {}
    for _, r in summary.iterrows():
        f = folder / str(r["file"])
        if not f.exists():
            continue
        d = pd.read_csv(f)
        if d.empty:
            continue
        t = pd.to_datetime(d["timestamp"])
        traces[r["run"]] = pd.DataFrame({
            "time": t,
            "T": pd.to_numeric(d["te_temperature_degC"], errors="coerce"),
            "p": pd.to_numeric(d["vacuum_chamber_mbar"], errors="coerce"),
            "phase": d["batch_phase"].fillna("").astype(str),
        })
    return summary, traces


def batch_average_vs_temperature(summary, traces):
    """Geometric-mean chamber pressure per BATCH_BIN_K bin of valve
    temperature, heating branch of the averaged test runs, where at least
    two runs have data. Returns (bin centres, pressures)."""
    per_run = []
    for _, r in summary[summary["in_average"] == 1].iterrows():
        d = traces.get(r["run"])
        if d is None:
            continue
        d = d[d["phase"].isin(["approach", "creep"]) & d["p"].gt(0) & d["T"].notna()]
        if d.empty:
            continue
        b = np.floor(d["T"] / BATCH_BIN_K) * BATCH_BIN_K + BATCH_BIN_K / 2
        per_run.append(np.log10(d["p"]).groupby(b).mean())
    if len(per_run) < 2:
        return np.array([]), np.array([])
    table = pd.concat(per_run, axis=1).sort_index()   # bins in temperature order
    # only where most runs have data: where runs join or leave, the average
    # would jump by the difference in their baselines
    table = table[table.notna().sum(axis=1) >= max(2, -(-len(per_run) // 2))]
    return table.index.to_numpy(), 10 ** table.mean(axis=1).to_numpy()


def batch_average_vs_time(summary, traces, step=0.5):
    """Geometric-mean chamber pressure against time since onset, over the
    averaged test runs. Returns (seconds, pressures)."""
    grid = np.arange(BATCH_ALIGN_S[0], BATCH_ALIGN_S[1] + step, step)
    curves = []
    for _, r in summary[summary["in_average"] == 1].iterrows():
        d = traces.get(r["run"])
        if d is None or pd.isna(r.get("onset_time")):
            continue
        s = (d["time"] - pd.to_datetime(r["onset_time"])).dt.total_seconds()
        ok = d["p"].gt(0)
        if ok.sum() < 2:
            continue
        y = np.interp(grid, s[ok], np.log10(d["p"][ok]), left=np.nan, right=np.nan)
        curves.append(y)
    if len(curves) < 2:
        return grid[:0], grid[:0]
    c = np.vstack(curves)
    n = np.sum(~np.isnan(c), axis=0)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)      # columns no run covers
        mean = np.where(n >= 2, np.nanmean(c, axis=0), np.nan)
    return grid, 10 ** mean


def upstream_fit(up, t_open):
    """(slope K/bar, intercept) of T_open against upstream, or None (fewer
    than 3 runs, or under 0.05 bar of spread). driver/batch.py fits the same
    for the Batches sheet."""
    ok = up.notna() & t_open.notna()
    u, t = up[ok].to_numpy(float), t_open[ok].to_numpy(float)
    if len(u) < 3 or u.max() - u.min() < 0.05:
        return None
    slope, intercept = np.polyfit(u, t, 1)
    return slope, intercept


def _t975(dof):
    try:
        from driver.batch import t975
        return t975(dof)
    except Exception:                                   # the driver isn't importable
        return 2.0 if dof > 30 else None


def batch_result(summary):
    """The batch's T_open, corrected for upstream pressure, from the
    averaged test runs: dict(mean, up, half, sd, n, slope, slope_half), or
    None. With a fit (n ≥ 3, ≥ 0.05 bar spread) the ± is the 95 % interval
    of the fit at the runs' mean upstream pressure (t × residual scatter ÷
    √n), so the spread the leak causes isn't counted as scatter; the mean
    itself is the plain mean (a straight-line fit passes through it). Else
    the plain mean ± t × sd ÷ √n."""
    avg = summary[summary["in_average"] == 1]
    up = pd.to_numeric(avg["upstream_at_open_bar"], errors="coerce")
    to = pd.to_numeric(avg["t_open_degC"], errors="coerce")
    ok = to.notna()
    n = int(ok.sum())
    if not n:
        return None
    t = to[ok].to_numpy(float)
    res = dict(mean=float(t.mean()), up=None, half=None, sd=None, n=n, slope=None,
               slope_half=None)
    u = up[ok]
    if u.notna().all():
        res["up"] = float(u.mean())
    fit = upstream_fit(up, to)
    if fit and u.notna().all():
        uu = u.to_numpy(float)
        r = t - (fit[1] + fit[0] * uu)
        dof = n - 2
        sd = float(np.sqrt((r @ r) / dof)) if dof > 0 else None
        tq = _t975(dof) if dof > 0 else None
        res.update(sd=sd, slope=float(fit[0]))
        if sd is not None and tq is not None:
            res["half"] = tq * sd / np.sqrt(n)
            sxx = float(((uu - uu.mean()) ** 2).sum())
            res["slope_half"] = tq * sd / np.sqrt(sxx) if sxx > 0 else None
    elif n > 1:
        res["sd"] = float(t.std(ddof=1))
        tq = _t975(n - 1)
        res["half"] = tq * res["sd"] / np.sqrt(n) if tq is not None else None
    return res


def _result_text(res):
    txt = f"T_open {res['mean']:.2f} °C"
    if res["half"] is not None:
        txt += f" ± {res['half']:.2f} K"
    if res["up"] is not None:
        txt += f" at {res['up']:.2f} bar"
    return txt


def _fit_y_to_x(ax):
    """Scale the y axis to the data inside the current x limits."""
    x0, x1 = ax.get_xlim()
    ys = []
    for line in ax.get_lines():
        x, y = np.asarray(line.get_xdata(), float), np.asarray(line.get_ydata(), float)
        if x.size != y.size or x.size < 3:          # (axvline: 2 points, axes coords)
            continue
        m = (x >= x0) & (x <= x1) & np.isfinite(y) & (y > 0)
        ys.append(y[m])
    ys = np.concatenate(ys) if ys else np.array([])
    if ys.size:
        ax.set_ylim(ys.min() / 1.03, ys.max() * 1.03)


TRACE_GREY = "0.72"          # individual runs
SCOUT_GREY = "0.85"          # scouts and runs left out of the average
AVERAGE_BLUE = "#1f5fbf"


def make_batch_figure(summary, traces, title):
    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(19, 6),
                                        gridspec_kw={"width_ratios": [1.2, 1.2, 0.8]})
    res = batch_result(summary)
    shown = set()
    for _, r in summary.iterrows():
        d = traces.get(r["run"])
        if d is None:
            continue
        averaged = r["in_average"] == 1
        colour = TRACE_GREY if averaged else SCOUT_GREY
        ls = "-" if averaged else "--"
        key = "test runs (averaged)" if averaged else "scouts / not averaged"
        label = None if key in shown else key
        shown.add(key)
        heat = d[d["phase"].isin(["baseline", "settle", "hold", "approach", "creep"])]
        ax1.plot(heat["T"], heat["p"], color=colour, lw=0.9, ls=ls, label=label, zorder=1)
        if pd.notna(r.get("onset_time")):
            s = (d["time"] - pd.to_datetime(r["onset_time"])).dt.total_seconds()
            w = s.between(*BATCH_ALIGN_S)
            ax2.plot(s[w], d["p"][w], color=colour, lw=0.9, ls=ls, label=label, zorder=1)
    x, y = batch_average_vs_temperature(summary, traces)
    if len(x):
        ax1.plot(x, y, color=AVERAGE_BLUE, lw=2.2, label="average (geometric mean)", zorder=3)
    x, y = batch_average_vs_time(summary, traces)
    if len(x):
        ax2.plot(x, y, color=AVERAGE_BLUE, lw=2.2, label="average (geometric mean)", zorder=3)
    if res is not None:
        m, half = res["mean"], res["half"]
        ax1.axvline(m, color=AVERAGE_BLUE, ls="--", lw=1.2, label=_result_text(res), zorder=2)
        if half:
            ax1.axvspan(m - half, m + half, color=AVERAGE_BLUE, alpha=0.10, zorder=0)
    ax2.axvline(0, color="black", ls="--", lw=1.0, label="onset")
    # Zoom on the creep towards the opening, where the runs say something;
    # the fast approach from the hold temperature is off to the left.
    avg = summary[summary["in_average"] == 1]
    lo = pd.to_numeric(avg.get("start_degC"), errors="coerce").min()
    hi = pd.to_numeric(avg.get("t_detect_degC"), errors="coerce").max()
    if pd.notna(lo) and pd.notna(hi) and hi > lo:
        ax1.set_xlim(lo - 3.0, hi + 3.0)
        ax1.autoscale(axis="y")
        _fit_y_to_x(ax1)

    # T_open against the measured upstream pressure
    up = pd.to_numeric(summary["upstream_at_open_bar"], errors="coerce")
    to = pd.to_numeric(summary["t_open_degC"], errors="coerce")
    test = summary["in_average"] == 1
    ax3.scatter(up[~test], to[~test], color=SCOUT_GREY, edgecolors="0.5", s=28,
                label="scouts / not averaged")
    ax3.scatter(up[test], to[test], color="0.35", s=32, zorder=3, label="test runs")
    fit = upstream_fit(up[test], to[test])
    if fit:
        xs = np.linspace(up[test].min(), up[test].max(), 2)
        sl = f"{fit[0]:+.1f}"
        if res is not None and res["slope_half"] is not None:
            sl += f" ± {res['slope_half']:.1f}"
        ax3.plot(xs, fit[1] + fit[0] * xs, color=AVERAGE_BLUE, lw=1.6,
                 label=f"fit: {sl} K/bar (95 %)")
        if res is not None and res["up"] is not None:
            ax3.errorbar([res["up"]], [res["mean"]], yerr=[[res["half"] or 0]],
                         fmt="D", color=AVERAGE_BLUE, ms=6, capsize=4, zorder=4,
                         label=_result_text(res))
    ax3.set_xlabel("upstream pressure at the onset (bar abs)")
    ax3.set_ylabel("T_open (°C)")
    ax3.set_title("opening point vs upstream")
    ax3.grid(True, alpha=0.3)
    ax3.legend(fontsize=8)
    for ax in (ax1, ax2):
        ax.set_yscale("log")
        ax.set_ylabel("chamber pressure (mbar)")
        ax.grid(True, which="both", alpha=0.3)
        ax.legend(fontsize=8, loc="upper left")
    ax1.set_xlabel("valve temperature (°C), creep towards the opening")
    ax2.set_xlabel("time since onset (s)")
    ax1.set_title("chamber pressure vs valve temperature")
    ax2.set_title("aligned at each run's onset")
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    return fig


def batch_title(folder, summary):
    from pathlib import Path
    avg = summary[summary["in_average"] == 1]
    up = pd.to_numeric(avg["upstream_at_open_bar"], errors="coerce").dropna()
    torque = summary["seat_screw_torque_Nm"].iloc[0] if len(summary) else float("nan")
    parts = [Path(folder).name, f"{torque:g} N·m"]
    if len(up):
        parts.append(f"upstream {up.min():.2f}-{up.max():.2f} bar")
    res = batch_result(summary)
    if res is not None:
        parts.append(_result_text(res) + (" (95 %, corrected for upstream)"
                                          if res["slope"] is not None else " (95 %)")
                     + f", n = {res['n']}")
    return "   ·   ".join(parts)


def plot_batch(folder, output=None, show=True):
    from pathlib import Path
    summary, traces = load_batch(folder)
    print(f"\nbatch {Path(folder).name}")
    cols = ["run", "status", "t_open_degC", "t_detect_degC", "open_to_detect_s",
            "energy_at_open_J", "upstream_at_open_bar", "efold_K"]
    print(summary[[c for c in cols if c in summary]].to_string(index=False))
    fig = make_batch_figure(summary, traces, batch_title(folder, summary))
    out = output or str(Path(folder) / "batch.png")
    fig.savefig(out, dpi=150)
    print(f"\nfigure written to {out}\n")
    if show:
        try:
            plt.show()
        except Exception:
            pass
    return out


# ── the opening map: every opening, one fit (driver/openmap.py; history 34) ──

def make_map_figure(rows, torque=None):
    """Three panels from the openings table: T_open against upstream for
    one torque (each seating in its colour, with its fitted line; hollow =
    deep cycle), the map (each seating's T_open at MAP_REF_BAR against
    torque, ± 95 %), and the residuals over time. torque: which one the
    first panel shows (default: the newest opening's)."""
    from driver import config, openmap
    f = openmap.fit(rows)
    newest = openmap.latest(rows)
    tk = (f"{torque:.2f}" if torque is not None else f.torque_of.get(newest))
    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(19, 6),
                                        gridspec_kw={"width_ratios": [1.2, 1.0, 1.2]})
    seatings = sorted(f.offsets, key=lambda s: min((p["t"] or 0) for p in f.points
                                                   if p["seating"] == s))
    cyc = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    colour = {s: cyc[i % len(cyc)] for i, s in enumerate(seatings)}
    ref = config.MAP_REF_BAR

    # 1: T_open against upstream, this torque
    if tk is not None:
        k = f.slope(tk)
        lo, hi = f.ranges.get(tk, (ref, ref))
        for s in seatings:
            if f.torque_of.get(s) != tk:
                continue
            pts = [p for p in f.points if p["seating"] == s]
            b = np.array([p["bar"] for p in pts])
            t = np.array([p["T"] for p in pts])
            deep = np.array([bool(p["deep"]) for p in pts])
            off, oh = f.offsets[s]
            lab = f"{s}: {off:.1f}" + (f" ± {oh:.1f}" if oh is not None else "") \
                + f" °C at {ref:g} bar, n = {len(pts)}"
            ax1.scatter(b[~deep], t[~deep], color=colour[s], s=30, zorder=3)
            ax1.scatter(b[deep], t[deep], facecolors="none", edgecolors=colour[s], s=40,
                        zorder=3)
            xs = np.linspace(min(lo, ref), max(hi, ref), 2)
            ax1.plot(xs, off + k * (xs - ref), color=colour[s], lw=1.3, label=lab)
        kk = f.slopes.get(tk)
        if kk:
            ax1.set_title(f"{float(tk):.2f} N·m: slope {kk[0]:+.1f}"
                          + (f" ± {kk[1]:.1f}" if kk[1] is not None else "")
                          + " K/bar" + (" (assumed)" if kk[2] == "assumed" else ""))
        ax1.axvline(ref, color="0.6", ls=":", lw=1)
    ax1.set_xlabel("upstream pressure at the onset (bar abs)   hollow = deep cycle")
    ax1.set_ylabel("T_open (°C)")
    ax1.grid(True, alpha=0.3)
    if ax1.get_legend_handles_labels()[0]:
        ax1.legend(fontsize=7, loc="best")

    # 2: the map — each seating's T_open at the reference pressure
    for s in seatings:
        off, oh = f.offsets[s]
        x = float(f.torque_of[s])
        ax2.errorbar([x], [off], yerr=[[oh or 0]], fmt="o", color=colour[s], capsize=3)
    ax2.set_xlabel("seat screw torque (N·m)")
    ax2.set_ylabel(f"T_open at {ref:g} bar (°C)")
    ax2.set_title("the map: each seating, ± 95 %")
    ax2.grid(True, alpha=0.3)

    # 3: residuals over time
    for s in seatings:
        pts = [p for p in f.points if p["seating"] == s and p["t"] is not None]
        if not pts:
            continue
        when = pd.to_datetime([p["when"] for p in pts])          # local time, as logged
        r = np.array([p["residual"] for p in pts])
        deep = np.array([bool(p["deep"]) for p in pts])
        ax3.scatter(when[~deep], r[~deep], color=colour[s], s=22)
        ax3.scatter(when[deep], r[deep], facecolors="none", edgecolors=colour[s], s=30)
    ax3.axhline(0, color="black", lw=1)
    if f.sd:
        ax3.axhspan(-f.sd, f.sd, color=AVERAGE_BLUE, alpha=0.08)
    ax3.set_ylabel("residual: measured − fit (K)")
    ax3.set_xlabel("time   band = ±1 scatter   hollow = deep cycle")
    ax3.set_title("residuals: a trend here is something the fit misses")
    ax3.grid(True, alpha=0.3)
    fig.autofmt_xdate()

    top = f.status_text(newest) if newest else "no openings yet"
    terms = f.terms_text()
    import textwrap
    fig.suptitle("\n".join(textwrap.wrap(top, 170) + textwrap.wrap(terms, 170)), fontsize=9)
    fig.tight_layout()
    return fig


def make_map3d_figure(rows):
    """The map in 3D: every opening at (torque, upstream, T_open), each
    seating's fitted line across the upstream range at its torque (the fit
    has a slope per torque and an offset per seating), and, where two or
    more torques are fitted, a surface through their lines at the mean
    offset of each torque (linear between torques: a guide, not a model)."""
    from driver import config, openmap
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401  (registers the 3d projection)
    f = openmap.fit(rows)
    fig = plt.figure(figsize=(11, 8))
    ax = fig.add_subplot(111, projection="3d")
    seatings = sorted(f.offsets, key=lambda s: min((p["t"] or 0) for p in f.points
                                                   if p["seating"] == s))
    cyc = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    colour = {s: cyc[i % len(cyc)] for i, s in enumerate(seatings)}
    ref = config.MAP_REF_BAR
    all_bars = [p["bar"] for p in f.points] or [ref]
    b_lo, b_hi = min(min(all_bars), ref), max(max(all_bars), ref)
    for s in seatings:
        pts = [p for p in f.points if p["seating"] == s]
        tq = float(f.torque_of[s])
        b = np.array([p["bar"] for p in pts])
        t = np.array([p["T"] for p in pts])
        deep = np.array([bool(p["deep"]) for p in pts])
        ax.scatter(np.full(b.shape, tq)[~deep], b[~deep], t[~deep], color=colour[s], s=22,
                   depthshade=False)
        ax.scatter(np.full(b.shape, tq)[deep], b[deep], t[deep], facecolors="none",
                   edgecolors=colour[s], s=30, depthshade=False)
        k = f.slope(f.torque_of[s])
        xs = np.linspace(b_lo, b_hi, 2)
        ax.plot([tq, tq], xs, f.offsets[s][0] + k * (xs - ref), color=colour[s], lw=1.6,
                label=f"{s} ({f.counts.get(s, 0)})")
    torques = sorted({float(tk) for tk in f.slopes})
    if len(torques) >= 2:
        mean_off = {}
        for tk in f.slopes:
            offs = [o for s_, (o, _) in f.offsets.items() if f.torque_of.get(s_) == tk]
            mean_off[float(tk)] = (np.mean(offs), f.slope(tk))
        tq_grid = np.array(torques)
        bars = np.linspace(b_lo, b_hi, 12)
        T = np.array([[mean_off[q][0] + mean_off[q][1] * (bb - ref) for bb in bars]
                      for q in tq_grid])
        TQ, BB = np.meshgrid(tq_grid, bars, indexing="ij")
        ax.plot_surface(TQ, BB, T, color=AVERAGE_BLUE, alpha=0.15, linewidth=0)
    ax.set_xlabel("seat screw torque (N·m)")
    ax.set_ylabel("upstream (bar abs)")
    ax.set_zlabel("T_open (°C)")
    # limits from the data (matplotlib's 3D autoscale counts empty point sets as 0)
    if torques:
        pad = max(0.02, 0.08 * (max(torques) - min(torques)))
        ax.set_xlim(min(torques) - pad, max(torques) + pad)
    ax.set_ylim(b_lo - 0.1, b_hi + 0.1)
    zs = [p["T"] for p in f.points] + [o + f.slope(f.torque_of[s]) * (bb - ref)
                                       for s, (o, _) in f.offsets.items() for bb in (b_lo, b_hi)]
    if zs:
        ax.set_zlim(min(zs) - 3.0, max(zs) + 3.0)
    ax.view_init(elev=22, azim=-128)
    ax.set_title(f"opening map: {f.n} openings, {len(seatings)} seating"
                 f"{'s' * (len(seatings) != 1)}, {len(torques)} torque"
                 f"{'s' * (len(torques) != 1)}   (hollow = deep cycle; lines: each "
                 f"seating's fit)", fontsize=10)
    ax.legend(fontsize=7, loc="upper left")
    fig.tight_layout()
    return fig


def plot_map(path, output=None, show=True, torque=None, three_d=False):
    """The 2D figure (output) and the 3D one next to it (<output>-3d.png).
    three_d: show the 3D one (rotatable) instead of the 2D one."""
    from driver import openmap
    rows = openmap.load(path)
    if not rows:
        fail(f"{path} has no openings yet")
    out = output or str(path).rsplit(".", 1)[0] + ".png"
    fig = make_map_figure(rows, torque)
    fig.savefig(out, dpi=130)
    fig3 = make_map3d_figure(rows)
    out3 = out.rsplit(".", 1)[0] + "-3d." + out.rsplit(".", 1)[1]
    fig3.savefig(out3, dpi=130)
    print(f"figures written to {out} and {out3}")
    if show:
        plt.close(fig3 if not three_d else fig)
        try:
            plt.show()
        except Exception:
            pass
    return out


def main():
    global INTERACTIVE
    INTERACTIVE = len(sys.argv) == 1

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile", nargs="?", default=None,
                    help="te-sensor_*.csv log file (omit to open a file picker)")
    ap.add_argument("-o", "--output", default=None,
                    help="output image path (.png/.pdf)")
    ap.add_argument("--no-show", action="store_true",
                    help="save only, don't open a window")
    ap.add_argument("--map", action="store_true",
                    help="the opening map: give logs/openings.csv (the default)")
    ap.add_argument("--torque", type=float, default=None,
                    help="with --map: the torque the first panel shows")
    ap.add_argument("--3d", dest="three_d", action="store_true",
                    help="with --map: show the 3D map (rotatable) instead of the 2D one")
    args = ap.parse_args()

    if args.map:
        if args.no_show:
            matplotlib.use("Agg")
        from driver import config
        plot_map(args.logfile or config.OPENINGS_CSV, args.output, show=not args.no_show,
                 torque=args.torque, three_d=args.three_d)
        return

    if args.logfile is None:
        args.logfile = pick_logfile()
        if args.logfile is None:
            sys.exit("No file selected.")
    if args.no_show:
        matplotlib.use("Agg")

    from pathlib import Path
    target = Path(args.logfile)
    if target.is_dir() or target.name == "summary.csv":
        folder = target if target.is_dir() else target.parent
        if not (folder / "summary.csv").exists():
            fail(f"{folder} has no summary.csv — is it a batch folder?")
        plot_batch(folder, args.output, show=not args.no_show)
        return

    try:
        raw = pd.read_csv(args.logfile, nrows=5)
    except Exception as exc:
        fail(f"Could not read {args.logfile}:\n{exc}")

    cols = resolve_columns(raw)

    if not any(cols[r] for r in ("temp", "chamber", "upstream", "duty",
                                 "current_meas", "current_calc",
                                 "power_meas", "power_calc")):
        fail("None of the expected data columns were found in this file.\n\n"
             f"Columns present:\n  {', '.join(raw.columns)}\n\n"
             "If a column simply has a new name, add a fragment of it to "
             "COLUMN_ALIASES near the top of TE_PLOTTER.py.")

    df = load(args.logfile, cols)
    if df.empty:
        fail("The file parsed but contains no usable rows.")

    print("\ncolumns matched")
    for role in ("time", "temp", "chamber", "upstream", "current", "duty"):
        print(f"  {role:<11} -> {cols[role] or '(not found — trace skipped)'}")
    print(f"  {'power':<11} -> " + (f"{cols['power']}  [{cols['power_src']}]"
          if cols["power"] else "(not available — trace skipped)"))

    ambient = (df.loc[df.t < AMBIENT_WINDOW_S, cols["temp"]].mean()
               if cols["temp"] else np.nan)
    steps = fit_steps(df, cols, ambient)
    segs = upstream_segments(df, cols)
    outgas = fit_outgassing(df, cols)

    valve = detect_valve_events(df, cols)

    seat_screw = seat_screw_history(df, cols)
    lock_nut = lock_nut_history(df, cols)

    report(args.logfile, df, cols, steps, segs, outgas, ambient, valve, seat_screw, lock_nut)

    fig = make_figure(df, cols, steps, segs, outgas,
                      args.logfile.replace("\\", "/").split("/")[-1], valve,
                      seat_screw, lock_nut)
    out = args.output or args.logfile.rsplit(".", 1)[0] + ".png"
    fig.savefig(out, dpi=150)
    print(f"\nfigure written to {out}\n")

    if not args.no_show:
        try:
            plt.show()
        except Exception:
            pass   # headless machine: the file is already written


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        fail("TE_PLOTTER hit an unexpected error:\n\n" + traceback.format_exc())
