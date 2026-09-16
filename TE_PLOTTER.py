#!/usr/bin/env python3
"""
TE_PLOTTER.py

Plot and characterise a TE-Valve sensor log.

Panels (only those whose data is present are drawn):
  A  Supporting traces vs time, each on its own y-axis:
       upstream P20 (light green) and heater current (orange)
       upstream P20 is the upstream pressure referred to 20 °C,
       P20 = P * 293.15 K / T_Keller, which is proportional to the amount
       of gas. Falls back to raw upstream pressure if no Keller temperature.
  B  Main plot, larger, same time axis: chamber pressure (blue, log,
     left axis) and TE temperature (red, right axis)

Valve open/close times are detected from the chamber pressure: the valve
counts as open while the pressure sits clearly above its fitted baseline
(dotted blue). Both plots get dashed vertical lines at those times.

Step-response fits, upstream decay rates (of P20 when available) and the
outgassing fit are printed to the terminal.

Column names are matched loosely, so logs from different driver versions
work without editing this file. Whatever it matched is printed at the top
of every run; anything it cannot match is skipped rather than fatal.

Usage:
    python TE_PLOTTER.py                     # opens a file picker
    python TE_PLOTTER.py LOGFILE.csv         # skips the picker
    python TE_PLOTTER.py LOGFILE.csv -o OUT.png [--no-show]
"""

import argparse
import sys
import traceback

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.transforms
import numpy as np
import pandas as pd
from scipy.optimize import curve_fit

# ---------------------------------------------------------------------------
# Column matching. Each role lists candidate name fragments, best first.
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
    "keller_temp": ["kellertemperature", "upstreamtemperature",
                    "inlettemperature", "kellertemp"],
    "duty":     ["heaterduty", "duty", "pwm", "heateroutput"],
    # current: measured is preferred, calculated is the fallback (see load)
    "current_meas": ["heaterimeanmeas", "currentmeas", "imeas"],
    "current_calc": ["heaterimeancalc", "currentcalc", "icalc",
                     "heatercurrent", "current"],
    "fault":    ["tcfault", "fault"],
    "mode":     ["heatermode", "mode"],
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
}

# Trace colours for the combined chart. Yellow and light green are the
# darker ends of those shades so they stay readable on a white background.
COLOURS = {
    "temp":     "#e01b1b",   # red
    "chamber":  "#1f4fe0",   # blue
    "current":  "#ff8c00",   # orange
    "duty":     "#e6c000",   # yellow
    "upstream": "#6fd04a",   # light green
    "p20":      "#6fd04a",   # light green (replaces raw upstream when available)
}
P20_REF_K = 293.15         # reference temperature for P20
KELVIN = 273.15
AXIS_OFFSET_PT = 62        # spacing between stacked y-axes on the same side

# Valve open/close detection on chamber pressure (all in log10 decades)
VALVE_MIN_RISE_DEC = 0.03  # smallest rise above baseline counted as open (≈ 7 %)
VALVE_NOISE_SIGMAS = 6.0   # ...or this many noise sigmas, whichever is larger
VALVE_MIN_OPEN_S = 3.0     # ignore excursions shorter than this
VALVE_MERGE_GAP_S = 3.0    # join excursions separated by a shorter dip
VALVE_BASELINE_DEG = 1     # polynomial order of the baseline drift (1 = linear)
VALVE_LINE_COLOUR = "0.15"

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
                 "current_meas", "current_calc"):
        if cols[role]:
            df[cols[role]] = numeric(df, cols[role])

    # The driver leaves the measured current blank unless a sense input is
    # wired, so use it only if it actually holds data.
    meas, calc = cols["current_meas"], cols["current_calc"]
    cols["current"] = meas if meas and df[meas].notna().any() else calc

    # Temperature-corrected upstream pressure (ideal gas, fixed volume).
    cols["p20"] = None
    kt = cols["keller_temp"]
    if cols["upstream"] and kt and df[kt].notna().any():
        df["p20_bar"] = df[cols["upstream"]] * P20_REF_K / (df[kt] + KELVIN)
        cols["p20"] = "p20_bar"

    if cols["temp"]:
        df = df.dropna(subset=[cols["temp"]])
    return df.reset_index(drop=True)


def first_order(t, T_inf, T_0, tau):
    return T_inf + (T_0 - T_inf) * np.exp(-t / tau)


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
            (T_inf, _T0, tau), _ = curve_fit(
                first_order, t, T, p0=[T[-1], T[0], 150.0], maxfev=20000)
        except (RuntimeError, TypeError):
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
    """Split the upstream trace at refills and fit a decay rate to each piece.
    Uses P20 when available, so rates are free of gas-temperature drift."""
    col = cols["p20"] or cols["upstream"]
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
    the lowest quarter of the trace and is refitted with the elevated points
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

    # Start from the lowest part of the trace (the valve only adds gas),
    # then refit with everything clearly above the baseline left out.
    quiet = y <= np.percentile(y, 25)
    for _ in range(20):
        coef = np.polyfit(t[quiet], y[quiet], VALVE_BASELINE_DEG)
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
    if cols.get("p20"):
        upstream = ("p20", "upstream P20 (bar, at 20 °C)", 1, "left", "line")
    else:
        upstream = ("upstream", "upstream pressure (bar)", 1, "left", "line")
    series = [  # role, axis label, scale, side, style
        ("temp",     "TE temperature (°C)",          1,   "left",  "line"),
        upstream,
        ("chamber",  "chamber pressure (mbar)",      1,   "right", "log"),
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
            (h,) = a.plot(df.t, y, lw=1.4, color=colour)
        h.set_label(label.split(" (")[0])
        handles.append(h)
        axes_by_role[role] = a

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
TOP_ROLES = ("p20", "upstream", "current")              # upper overview (duty
                                                       # omitted: current tracks it)


def make_figure(df, cols, steps, segs, outgas, title, valve=(None, [])):
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
    fig.suptitle(title, fontsize=12, y=0.99)

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
    if events:
        hosts = [a for a in (ax_t, ax_m) if a is not None]
        mark_valve_events(hosts, events, ax_m if ax_m is not None else ax_t)

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return fig


def report(path, df, cols, steps, segs, outgas, ambient, valve=(None, [])):
    print(f"\nfile      : {path}")
    print(f"duration  : {df.t.iloc[-1]:.0f} s   samples: {len(df)}")
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

    if segs:
        which = "P20, referred to 20 °C" if cols["p20"] else "raw pressure"
        print(f"\nupstream segments ({which})")
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
                                 "current_meas", "current_calc")):
        fail("None of the expected data columns were found in this file.\n\n"
             f"Columns present:\n  {', '.join(raw.columns)}\n\n"
             "If a column simply has a new name, add a fragment of it to "
             "COLUMN_ALIASES near the top of TE_PLOTTER.py.")

    df = load(args.logfile, cols)
    if df.empty:
        fail("The file parsed but contains no usable rows.")

    print("\ncolumns matched")
    for role in ("time", "temp", "chamber", "upstream", "keller_temp",
                 "current", "duty"):
        print(f"  {role:<11} -> {cols[role] or '(not found — trace skipped)'}")
    print(f"  {'p20':<11} -> " + ("computed from upstream / keller_temp"
          if cols["p20"] else "(not available — raw upstream plotted)"))

    ambient = (df.loc[df.t < AMBIENT_WINDOW_S, cols["temp"]].mean()
               if cols["temp"] else np.nan)
    steps = fit_steps(df, cols, ambient)
    segs = upstream_segments(df, cols)
    outgas = fit_outgassing(df, cols)

    valve = detect_valve_events(df, cols)

    report(args.logfile, df, cols, steps, segs, outgas, ambient, valve)

    fig = make_figure(df, cols, steps, segs, outgas,
                      args.logfile.replace("\\", "/").split("/")[-1], valve)
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
