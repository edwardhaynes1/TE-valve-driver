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

The seat screw torque (N·m, as entered in the driver) goes in the figure
title and the terminal summary; if it changed during the run, both plots
get a dotted vertical line at each change. Logs from before the column
existed say "not recorded".

Step-response fits, upstream decay rates (raw pressure), heater energy and
the outgassing fit are printed to the terminal.

Column names come from driver/schema.py, the same definition the driver
writes with. Logs from older driver versions, whose names differ, fall back
to loose matching (COLUMN_ALIASES). Whatever it matched is printed at the
top of every run; anything it cannot match is skipped rather than fatal.

Usage:
    python TE_PLOTTER.py                     # opens a file picker
    python TE_PLOTTER.py LOGFILE.csv         # skips the picker
    python TE_PLOTTER.py LOGFILE.csv -o OUT.png [--no-show]
"""
import sys
sys.dont_write_bytecode = True   # keep __pycache__ folders out of the project

import argparse
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
                 "p_target", "t_setpoint", "seat_screw"):
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


def seat_screw_history(df, cols):
    """[(t, N·m or None), …]: the value at the start and at each change.
    None means not recorded. Returns None if the log has no such column."""
    col = cols.get("seat_screw")
    if not col:
        return None
    history, last = [], object()
    for t, v in zip(df.t, df[col]):
        v = None if pd.isna(v) else float(v)
        if v != last:
            history.append((float(t), v))
            last = v
    return history


def seat_screw_text(history):
    """'seat screw torque 0.40 N·m', or the sequence of values if it changed."""
    if history is None:
        return "seat screw torque not recorded (log predates the column)"
    def fmt(v):
        return "not recorded" if v is None else f"{v:.2f} N·m"
    if not history:
        return "seat screw torque not recorded"
    text = "seat screw torque " + fmt(history[0][1])
    for t, v in history[1:]:
        text += f" → {fmt(v)} at {t:.1f} s"
    return text


def mark_seat_screw(axes, history, label_ax):
    """Dotted vertical lines where the seat screw torque changed mid-run."""
    trans = matplotlib.transforms.blended_transform_factory(
        label_ax.transData, label_ax.transAxes)
    for t, v in (history or [])[1:]:
        for ax in axes:
            ax.axvline(t, ls=":", lw=1.4, color=SEAT_SCREW_COLOUR, zorder=5)
        label = "seat screw not recorded" if v is None else f"seat screw {v:.2f} N·m"
        label_ax.text(t, 0.02, f" {label} ", transform=trans, rotation=90,
                      ha="right", va="bottom", fontsize=8, color=SEAT_SCREW_COLOUR,
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
                seat_screw=None):
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
    fig.suptitle(f"{title}  ·  {seat_screw_text(seat_screw)}", fontsize=12, y=0.99)

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

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


def report(path, df, cols, steps, segs, outgas, ambient, valve=(None, []),
           seat_screw=None):
    print(f"\nfile      : {path}")
    print(f"duration  : {df.t.iloc[-1]:.0f} s   samples: {len(df)}")
    print(f"TE-Valve  : {seat_screw_text(seat_screw)}")
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
    args = ap.parse_args()

    if args.logfile is None:
        args.logfile = pick_logfile()
        if args.logfile is None:
            sys.exit("No file selected.")
    if args.no_show:
        matplotlib.use("Agg")

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

    report(args.logfile, df, cols, steps, segs, outgas, ambient, valve, seat_screw)

    fig = make_figure(df, cols, steps, segs, outgas,
                      args.logfile.replace("\\", "/").split("/")[-1], valve,
                      seat_screw)
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
