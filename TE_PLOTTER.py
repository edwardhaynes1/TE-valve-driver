#!/usr/bin/env python3
"""
TE_PLOTTER.py

Plot and characterise a TE-Valve sensor log.

Panels (only those whose data is present are drawn):
  A  Temperature and heater duty vs time, with fitted step responses
  B  Chamber pressure vs time (log)
  C  Upstream pressure vs time, with decay rate per segment
  D  Chamber pressure vs temperature (log y), with exponential fit

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
    "duty":     ["heaterduty", "duty", "pwm", "heateroutput"],
    "fault":    ["tcfault", "fault"],
    "mode":     ["heatermode", "mode"],
}

# Fragments that disqualify a column for a given role, so that e.g.
# "n_keller_samples" is never mistaken for the Keller pressure trace.
COLUMN_EXCLUDE = {
    "temp":     ["keller", "setpoint", "samples", "count", "ambient"],
    "chamber":  ["temperature", "samples", "count", "setpoint"],
    "upstream": ["temperature", "samples", "count", "setpoint"],
    "duty":     ["setpoint", "samples", "count"],
}

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

    for role in ("temp", "chamber", "upstream", "duty"):
        if cols[role]:
            df[cols[role]] = numeric(df, cols[role])

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
    """Split the upstream trace at refills and fit a decay rate to each piece."""
    if not cols["upstream"]:
        return []
    p = df[cols["upstream"]]
    segs = []
    for _, seg in df.groupby((p.diff().abs() > JUMP_BAR).cumsum()):
        seg = seg.dropna(subset=[cols["upstream"]])
        if len(seg) < 120:
            continue
        slope = np.polyfit(seg.t, seg[cols["upstream"]], 1)[0]
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


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------
def panel_temperature(ax, df, cols, steps):
    ax.plot(df.t, df[cols["temp"]], lw=1.2, color="#c1272d")
    for s in steps.itertuples():
        if np.isfinite(s.tau):
            ax.hlines(s.T_inf, s.t0, s.t1, ls="--", lw=1, color="0.35")
            ax.annotate(f"{s.T_inf:.1f} °C\nτ={s.tau:.0f} s",
                        xy=((s.t0 + s.t1) / 2, s.T_inf), xytext=(0, 6),
                        textcoords="offset points", ha="center",
                        fontsize=8, color="0.25")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("TE temperature (°C)")
    ax.set_title("A — Temperature and heater duty", fontsize=10, loc="left")
    ax.grid(alpha=0.3)
    if cols["duty"]:
        ax_d = ax.twinx()
        ax_d.step(df.t, df[cols["duty"]] * 100, where="post",
                  lw=1, color="#1f6feb", alpha=0.6)
        ax_d.set_ylabel("heater duty (%)", color="#1f6feb")
        ax_d.tick_params(axis="y", colors="#1f6feb")
        ax_d.set_ylim(0, 105)


def panel_chamber(ax, df, cols, base):
    ax.semilogy(df.t, df[cols["chamber"]], lw=1.2, color="#2e7d32")
    if base is not None and np.isfinite(base):
        ax.axhline(base, ls=":", lw=1, color="0.4")
        ax.annotate(f"cold baseline {base:.2e} mbar",
                    xy=(df.t.iloc[-1], base), xytext=(-4, 5),
                    textcoords="offset points", ha="right",
                    fontsize=8, color="0.35")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("chamber pressure (mbar)")
    ax.set_title("B — Chamber pressure", fontsize=10, loc="left")
    ax.grid(alpha=0.3, which="both")


def panel_upstream(ax, df, cols, segs):
    ax.plot(df.t, df[cols["upstream"]], lw=1.2, color="#6a1b9a")
    for s in segs:
        seg = s["data"]
        fit = np.poly1d(np.polyfit(seg.t, seg[cols["upstream"]], 1))
        ax.plot(seg.t, fit(seg.t), ls="--", lw=1.2, color="k", alpha=0.8)
        mid = seg.t.iloc[len(seg) // 2]
        label = f"{s['rate_mbar_min']:+.2f} mbar/min"
        if np.isfinite(s["mean_T"]):
            label += f"\n(mean T {s['mean_T']:.0f} °C)"
        ax.annotate(label, xy=(mid, fit(mid)), xytext=(0, -28),
                    textcoords="offset points", ha="center", fontsize=8)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("upstream pressure (bar)")
    ax.set_title("C — Upstream pressure (steps = refill/adjustment)",
                 fontsize=10, loc="left")
    ax.grid(alpha=0.3)


def panel_chamber_vs_temp(fig, ax, df, cols, outgas):
    base, pre, T_scale, doubling = outgas
    sc = ax.scatter(df[cols["temp"]], df[cols["chamber"]], s=5,
                    c=df.t, cmap="viridis", alpha=0.75)
    ax.set_yscale("log")
    if base is not None and np.isfinite(base):
        ax.axhline(base, ls=":", lw=1, color="0.4")
    if pre is not None:
        Tg = np.linspace(df[cols["temp"]].min(), df[cols["temp"]].max(), 200)
        ax.plot(Tg, base + pre * np.exp(Tg / T_scale), "r--", lw=1.4,
                label=f"baseline + exp(T/{T_scale:.1f} K)\n"
                      f"doubles every {doubling:.1f} K")
        ax.legend(fontsize=8, loc="upper left")
    ax.set_xlabel("TE temperature (°C)")
    ax.set_ylabel("chamber pressure (mbar)")
    ax.set_title("D — Chamber pressure vs temperature", fontsize=10, loc="left")
    ax.grid(alpha=0.3, which="both")
    fig.colorbar(sc, ax=ax, pad=0.02).set_label("time (s)", fontsize=8)


def make_figure(df, cols, steps, segs, outgas, title):
    """Build a figure from whichever panels the log can support."""
    wanted = []
    if cols["temp"]:
        wanted.append("A")
    if cols["chamber"]:
        wanted.append("B")
    if cols["upstream"]:
        wanted.append("C")
    if cols["temp"] and cols["chamber"]:
        wanted.append("D")

    n = len(wanted)
    rows, ncols = (1, 1) if n == 1 else (1, 2) if n == 2 else (2, 2)
    fig, axes = plt.subplots(rows, ncols,
                             figsize=(6.8 * ncols, 4.3 * rows), squeeze=False)
    flat = axes.ravel()
    fig.suptitle(title, fontsize=12, y=0.98)

    for ax, which in zip(flat, wanted):
        if which == "A":
            panel_temperature(ax, df, cols, steps)
        elif which == "B":
            panel_chamber(ax, df, cols, outgas[0])
        elif which == "C":
            panel_upstream(ax, df, cols, segs)
        elif which == "D":
            panel_chamber_vs_temp(fig, ax, df, cols, outgas)
    for ax in flat[n:]:
        ax.axis("off")

    fig.tight_layout(rect=(0, 0, 1, 0.95))
    return fig


def report(path, df, cols, steps, segs, outgas, ambient):
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
        print("\nupstream segments")
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
    print("\ncolumns matched")
    for role in ("time", "temp", "chamber", "upstream", "duty"):
        print(f"  {role:<9} -> {cols[role] or '(not found — panel skipped)'}")

    if not any(cols[r] for r in ("temp", "chamber", "upstream")):
        fail("None of the expected data columns were found in this file.\n\n"
             f"Columns present:\n  {', '.join(raw.columns)}\n\n"
             "If a column simply has a new name, add a fragment of it to "
             "COLUMN_ALIASES near the top of TE_PLOTTER.py.")

    df = load(args.logfile, cols)
    if df.empty:
        fail("The file parsed but contains no usable rows.")

    ambient = (df.loc[df.t < AMBIENT_WINDOW_S, cols["temp"]].mean()
               if cols["temp"] else np.nan)
    steps = fit_steps(df, cols, ambient)
    segs = upstream_segments(df, cols)
    outgas = fit_outgassing(df, cols)

    report(args.logfile, df, cols, steps, segs, outgas, ambient)

    fig = make_figure(df, cols, steps, segs, outgas,
                      args.logfile.replace("\\", "/").split("/")[-1])
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
