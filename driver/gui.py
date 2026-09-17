"""Tkinter 'Live Log' window: status, readouts, heater controls, charts.
"""


import math
import re
import time
import tkinter as tk
from tkinter import font as tkfont

from . import logfile
from . import shared
from .config import (
    CHART_SECONDS, FLIGHT_POWER_BUDGET_W, HEATER_I_AIN, HEATER_MAX_DUTY,
    HEATER_MAX_RUN_S, HEATER_PWM_PERIOD_S, HEATER_R_OHM, HEATER_V_AIN,
    HEATER_V_RAIL, P20_REF_K, PID_SETPOINT_DEFAULT, PRESSURE_BURST_BRAKE_K,
    PRESSURE_MIN_STEP_MBAR, PRESSURE_OPEN_FLOOR_C, PRESSURE_SEEK_RATE_C_MIN,
    PRESSURE_SEEK_START_C, PRESSURE_TARGET_DEFAULT, PRESSURE_TARGET_MIN,
    PRESSURE_TRIP_MBAR, PRESSURE_TSP_MAX_C, PRESSURE_TSP_MIN_C, TEMP_TRIP_C,
)
from .control import AUTO_P, AUTO_T, MANUAL, MODES, heater_command, snapshot
from .devices import FAULT_BITS, LABJACK_AVAILABLE
from .shared import log_event


# ═══════════════════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════════════════
# All GUI colours are defined here and used by name below. Change them here.

# Base palette (window, text, controls)
BG        = "#0c0c0c"   # window background
TEXT      = "#d0d0d0"   # normal text
BRIGHT    = "#ffffff"   # highlighted values
DIM       = "#505050"   # labels, inactive items
BORDER    = "#2a2a2a"   # chart and entry borders
WARN      = "#ff4040"   # errors, trips, warnings
FIELD     = "#1a1a1a"   # entry boxes and buttons
FIELD_HOT = "#303030"   # button while pressed

# Charts
GRID      = "#1a1a1a"   # horizontal grid lines
REF       = "#8a8a8a"   # dashed target / setpoint / budget lines

# Chart traces
TEMP_LINE = "#ff2a2a"   # valve temperature (red)
VAC_LINE  = "#2f8cff"   # vacuum chamber pressure (blue)
UP_LINE   = "#cfe6cf"   # upstream pressure (whitish green, secondary)
PWR_LINE  = "#ffffff"   # heater power (white)



class TEGui:
    """Single 'Live Log' window: device status, live readouts (including heater
    voltage and current), heater controls, strip charts, and an event log."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TE Valve — Live Log")
        self.root.configure(bg=BG)
        screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"800x{max(600, min(1100, screen_h - 90))}+20+10")
        self.root.minsize(700, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

        M = ("Consolas", "Menlo", "Courier New", "DejaVu Sans Mono", "monospace")
        self.f = self._pick_font(M, 11)

        self._build_log_window()
        self.root.after(150, self._poll)

    def _pick_font(self, families, size, weight="normal"):
        available = set(tkfont.families())
        fam = next((f for f in families if f in available), families[-1])
        return tkfont.Font(family=fam, size=size, weight=weight)

    def _chart(self, parent, title):
        # Title is drawn inside the graph (saves a text row per graph). Small
        # requested height + expand: the graphs share whatever height the
        # window has left, instead of pushing the event log off-screen.
        row = len(parent.grid_slaves())
        c = tk.Canvas(parent, bg=BG, height=30,
                      highlightbackground=BORDER, highlightthickness=1)
        c.grid(row=row, column=0, sticky="nsew", pady=(0, 3))
        parent.rowconfigure(row, weight=1, uniform="charts")   # equal share, shrink evenly
        c.title = f"{title}  ·  last {CHART_SECONDS}s"
        return c

    def _build_log_window(self):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=8, pady=8)

        self.status_text = tk.Text(outer, bg=BG, fg=TEXT, font=self.f,
                                   height=13, bd=0, highlightthickness=0,
                                   state="disabled", wrap="none", cursor="arrow")
        self.status_text.pack(fill="x")
        self.status_text.tag_config("bright", foreground=BRIGHT)
        self.status_text.tag_config("dim",    foreground=DIM)
        self.status_text.tag_config("ok",     foreground=BRIGHT)
        self.status_text.tag_config("err",    foreground=WARN)

        self._build_heater_panel(outer)

        # Bottom block is packed BEFORE the graphs so it always keeps its space.
        tk.Label(outer, text=f"─── log: {logfile.LOG_FILE}",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(side="bottom", fill="x")
        self.logtext = tk.Text(outer, bg=BG, fg=TEXT, font=self.f,
                               height=7, bd=0, highlightthickness=0,
                               state="disabled", wrap="word", cursor="arrow")
        self.logtext.pack(side="bottom", fill="x")
        tk.Label(outer, text="─── event log",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(side="bottom", fill="x")

        charts = tk.Frame(outer, bg=BG)
        charts.pack(fill="both", expand=True)
        charts.columnconfigure(0, weight=1)
        p_src = ("measured" if (HEATER_I_AIN is not None or HEATER_V_AIN is not None)
                 else "calculated")
        self.vac_canvas = self._chart(charts, "vacuum chamber (mbar, log)   dashed = target")
        self.te_canvas  = self._chart(charts, "valve temperature (°C)   dashed = setpoint")
        self.up_canvas  = self._chart(charts, "upstream pressure (bar abs, Keller raw)")
        self.heat_canvas = self._chart(
            charts, f"heater power (W, {p_src}, {HEATER_PWM_PERIOD_S:g} s mean)   "
                    f"dashed = {FLIGHT_POWER_BUDGET_W:g} W flight budget")

    # ── heater panel ──────────────────────────────────────────────────────
    def _entry(self, parent, label, initial, width=8):
        lbl = tk.Label(parent, text=label, font=self.f, fg=DIM, bg=BG)
        lbl.pack(side="left")
        e = tk.Entry(parent, width=width, font=self.f, bg=FIELD,
                     fg=BRIGHT, insertbackground=BRIGHT, bd=0,
                     highlightthickness=1, highlightbackground=BORDER,
                     disabledbackground=BG, disabledforeground=DIM)
        e.insert(0, initial)
        e.pack(side="left", padx=(2, 12))
        e.bind("<Return>", lambda _ev: self._send_update())
        e.label = lbl
        return e

    def _on_mode(self):
        self._update_inputs()
        self._send_update()

    def _update_inputs(self):
        """Enable only the input that belongs to the selected mode."""
        active = {MANUAL: self.duty_entry,
                  AUTO_T: self.sp_entry,
                  AUTO_P: self.p_entry}[self.mode_var.get()]
        for e in (self.duty_entry, self.sp_entry, self.p_entry):
            on = e is active
            e.configure(state="normal" if on else "disabled",
                        highlightbackground=BRIGHT if on else BORDER)
            e.label.configure(fg=TEXT if on else DIM)

    def _build_heater_panel(self, parent):
        tk.Label(parent, text="─── heater  (FIO0 → Q171)   SW171 must be enabled",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(fill="x")

        btn = dict(bg=FIELD, fg=TEXT, activebackground=FIELD_HOT,
                   activeforeground=BRIGHT, font=self.f, bd=0,
                   highlightthickness=1, highlightbackground=BORDER,
                   padx=8, pady=2)

        row1 = tk.Frame(parent, bg=BG)
        row1.pack(fill="x", pady=(2, 2))
        self.arm_btn = tk.Button(row1, text="ARM", width=7,
                                 command=self._toggle_arm, **btn)
        self.arm_btn.pack(side="left", padx=(0, 10))

        self.mode_var = tk.StringVar(value=MANUAL)
        for mode in MODES:
            tk.Radiobutton(row1, text=mode, value=mode, variable=self.mode_var,
                           command=self._on_mode, font=self.f, fg=TEXT, bg=BG,
                           selectcolor=BG, activebackground=BG,
                           activeforeground=BRIGHT, bd=0,
                           highlightthickness=0).pack(side="left", padx=(0, 6))

        row2 = tk.Frame(parent, bg=BG)
        row2.pack(fill="x", pady=(0, 4))
        self.duty_entry = self._entry(row2, "duty %", "0", width=6)
        self.sp_entry   = self._entry(row2, "setpoint °C", f"{PID_SETPOINT_DEFAULT:g}", width=6)
        self.p_entry    = self._entry(row2, "target mbar", f"{PRESSURE_TARGET_DEFAULT:.1e}", width=9)
        tk.Button(row2, text="update", command=self._send_update, **btn).pack(side="left")

        self.heater_status = tk.Label(parent, text="", font=self.f, fg=DIM,
                                      bg=BG, anchor="w")
        self.heater_status.pack(fill="x")
        self.loop_status = tk.Label(parent, text="", font=self.f, fg=DIM,
                                    bg=BG, anchor="w")
        self.loop_status.pack(fill="x")
        self._update_inputs()
        self._send_update()

    def _toggle_arm(self):
        if snapshot()['armed']:
            heater_command(armed=False)
            log_event("Heater DISARMED by operator")
        else:
            self._send_update(quiet_if_unchanged=True)   # picks up un-sent edits to the active value, logged
            heater_command(armed=True)
            log_event(f"Heater ARMED by operator · {self._active_summary()}")

    def _active_summary(self):
        """'auto-t · setpoint 60.0 °C' — the mode and the value it uses."""
        a, mode = self._applied, self.mode_var.get()
        value = {'manual':   f"duty {a['duty']*100:g} %",
                 AUTO_T:   f"setpoint {a['sp']:g} °C",
                 AUTO_P:   f"target {a['tgt']:.2e} mbar"}[mode]
        return f"{mode} · {value}"

    @staticmethod
    def _set_entry(entry, text):
        """Write into an entry even while it is disabled."""
        state = entry.cget("state")
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, text)
        entry.configure(state=state)

    def _read_entry(self, entry, name, previous, lo, hi, scale=1.0, fmt="{:g}", unit=""):
        """Parse, validate and clamp one input. Invalid text is rejected (the
        previous value is restored and the rejection logged) rather than being
        silently replaced by a default. Clamping is logged too."""
        text = entry.get().strip()
        try:
            v = float(text) * scale
            if not math.isfinite(v):
                raise ValueError
        except ValueError:
            if previous is not None:
                self._set_entry(entry, fmt.format(previous / scale))
                self._input_noted = True
                log_event(f"Heater {name} entry '{text}' rejected — kept "
                          f"{fmt.format(previous / scale)}{unit}")
            return previous
        c = max(lo, min(hi, v))
        if c != v:
            self._input_noted = True
            log_event(f"Heater {name} {fmt.format(v / scale)}{unit} out of range — "
                      f"clamped to {fmt.format(c / scale)}{unit}")
            self._set_entry(entry, fmt.format(c / scale))
        return c

    def _send_update(self, quiet_if_unchanged=False):
        """Send the ACTIVE mode's value to the heater and log any change.

        Only the input belonging to the selected mode is read, validated and
        sent. The other two boxes are ignored entirely — whatever they
        contain has no effect until their own mode is selected, at which
        point their value is read and sent (and logged) like any update."""
        a = getattr(self, "_applied", None)
        first = a is None
        if first:
            a = self._applied = dict(mode=None, duty=0.0,
                                     sp=PID_SETPOINT_DEFAULT,
                                     tgt=PRESSURE_TARGET_DEFAULT)
        self._input_noted = False
        mode = self.mode_var.get()

        if mode == 'manual':
            key = 'duty'
            val = self._read_entry(self.duty_entry, "duty", a['duty'],
                                   0.0, HEATER_MAX_DUTY, scale=0.01, unit=" %")
            heater_command(mode=mode, duty_cmd=val)
            change = f"duty {a['duty']*100:g} → {val*100:g} %"
        elif mode == AUTO_T:
            key = 'sp'
            val = self._read_entry(self.sp_entry, "setpoint", a['sp'],
                                   0.0, TEMP_TRIP_C - 5.0, unit=" °C")
            heater_command(mode=mode, setpoint_C=val)
            change = f"setpoint {a['sp']:g} → {val:g} °C"
        else:   # auto-p — the outer loop owns the temperature setpoint
            key = 'tgt'
            val = self._read_entry(self.p_entry, "target", a['tgt'],
                                   PRESSURE_TARGET_MIN, PRESSURE_TRIP_MBAR / 2.0,
                                   fmt="{:.2e}", unit=" mbar")
            heater_command(mode=mode, p_target_mbar=val)
            change = f"target {a['tgt']:.2e} → {val:.2e} mbar"

        changes = []
        if not first and mode != a['mode']:
            changes.append(f"mode {a['mode']} → {mode}")
        if val != a[key]:
            changes.append(change)
        a['mode'], a[key] = mode, val

        if first:
            log_event(f"Heater settings · {self._active_summary()}")
        elif changes:
            log_event(f"Heater {' · '.join(changes)}   (now {self._active_summary()})")
        elif not (quiet_if_unchanged or self._input_noted):
            log_event(f"Heater update — no change ({self._active_summary()})")

    def _draw_chart(self, canvas, data, fmt="{:.3f}", log=False,
                    ref=None, floor=None, min_span=None,
                    color=BRIGHT, width=1):
        """Draw a strip chart on *canvas*.

        data  : sequence of values (oldest → newest)
        fmt   : format string for the y-axis tick labels
        log   : if True, plot log10 of the data (for vacuum pressure, which
                spans orders of magnitude). Non-positive values are skipped.
        ref   : optional reference value (target / setpoint), drawn dashed and
                always kept inside the y-range
        floor : optional (lo, hi) the y-range will always include
        min_span : smallest y-range shown (plot units; decades if log), so
                sensor quantisation isn't magnified into apparent swings.
                Tick labels gain decimals automatically if they'd repeat.
        color : line colour of the data curve
        width : line width of the data curve, px
        """
        c = canvas
        c.delete("all")
        w = c.winfo_width() or 580
        h = c.winfo_height() or 90
        pad_l, pad_r, pad_y = 74, 8, 8
        n_div = 4 if h >= 110 else (2 if h >= 55 else 1)   # label rows that fit

        for i in range(1, n_div):
            y = pad_y + (h - 2 * pad_y) * i / n_div
            c.create_line(pad_l, y, w - pad_r, y, fill=GRID)
        title = getattr(c, "title", "")
        if title:
            c.create_text(pad_l + 6, 2, text=title, fill=DIM, font=self.f,
                          anchor="nw", tags="title")

        if log:
            plot_vals = [math.log10(v) for v in data if v is not None and v > 0]
        else:
            plot_vals = [v for v in data if v is not None]
        if len(plot_vals) < 2:
            c.create_text(w / 2, h / 2, text="waiting for data",
                          fill=DIM, font=self.f)
            return

        ref_p = None
        if ref is not None and (not log or ref > 0):
            ref_p = math.log10(ref) if log else ref

        # Decimate to ~one min/max pair per pixel column. A 300 s window is
        # 1200 points per chart; min/max (not every-Nth) keeps short spikes.
        cols = max(2, int((w - pad_l - pad_r) / 2))
        if len(plot_vals) > 2 * cols:
            step, dec = math.ceil(len(plot_vals) / cols), []
            for k in range(0, len(plot_vals), step):
                b = plot_vals[k:k + step]
                i_lo = min(range(len(b)), key=b.__getitem__)
                i_hi = max(range(len(b)), key=b.__getitem__)
                dec.extend(b[i] for i in sorted({i_lo, i_hi}))
            plot_vals = dec

        lo, hi = min(plot_vals), max(plot_vals)
        if ref_p is not None:
            lo, hi = min(lo, ref_p), max(hi, ref_p)
        if floor is not None:
            lo, hi = min(lo, floor[0]), max(hi, floor[1])
        need = max(min_span or 0.0, 1e-9)
        if hi - lo < need:
            mid = (hi + lo) / 2
            lo, hi = mid - need / 2, mid + need / 2
        span = hi - lo
        n = len(plot_vals)

        def ypix(v):
            return (h - pad_y) - (h - 2 * pad_y) * (v - lo) / span

        ticks = [lo + span * i / n_div for i in range(n_div + 1)]
        reals = [(10 ** v) if log else v for v in ticks]
        m = re.search(r"\.(\d+)([fe])", fmt)
        labels = [fmt.format(v) for v in reals]
        if m:
            for extra in range(1, 4):              # add digits until labels differ
                if len(set(labels)) == len(labels):
                    break
                f2 = "{:." + str(int(m.group(1)) + extra) + m.group(2) + "}"
                labels = [f2.format(v) for v in reals]
        for v, text in zip(ticks, labels):
            c.create_text(pad_l - 4, ypix(v), text=text,
                          fill=DIM, font=self.f, anchor="e")

        if ref_p is not None:
            y = ypix(ref_p)
            c.create_line(pad_l, y, w - pad_r, y, fill=REF, dash=(4, 3))

        pts = []
        for i, v in enumerate(plot_vals):
            pts.extend((pad_l + (w - pad_l - pad_r) * i / (n - 1), ypix(v)))
        c.create_line(*pts, fill=color, width=width)
        c.tag_raise("title")

    def _heater_vi_lines(self, h):
        """Return [(label, value, note, value_tag)] for the V and I readouts."""
        if not shared.health()['labjack']:
            return [("HEATER V     ", "---", "", "dim"),
                    ("HEATER I     ", "---", "", "dim"),
                    ("HEATER P     ", "---", "", "dim")]
        d      = h['duty_actual']
        v_mean = d * HEATER_V_RAIL
        i_mean = v_mean / HEATER_R_OHM
        state  = "ON " if h['out_high'] else "off"
        lines  = []

        if h['v_meas'] is not None and h['v_meas_mean'] is not None:
            rail = h['rail_meas']
            lines.append(("HEATER V     ",
                          f"{h['v_meas']:6.2f} V {state} · {h['v_meas_mean']:6.2f} V mean",
                          f"  meas · rail {rail:.2f} V · calc {v_mean:.2f} V", "bright"))
        else:
            lines.append(("HEATER V     ",
                          f"{h['v_now']:6.2f} V {state} · {v_mean:6.2f} V mean",
                          "  calc — assumes SW171 on, 24 V present", "bright"))

        if h['i_meas'] is not None and h['i_meas_mean'] is not None:
            lines.append(("HEATER I     ",
                          f"{h['i_meas']:6.3f} A {state} · {h['i_meas_mean']:6.3f} A mean",
                          f"  meas · calc {i_mean:.3f} A", "bright"))
        else:
            lines.append(("HEATER I     ",
                          f"{h['i_now']:6.3f} A {state} · {i_mean:6.3f} A mean",
                          f"  calc ({HEATER_R_OHM:g} Ω element)", "bright"))

        p_full = HEATER_V_RAIL ** 2 / HEATER_R_OHM
        p_calc = d * p_full
        if h['p_meas_mean'] is not None:
            lines.append(("HEATER P     ",
                          f"{h['p_meas_mean']:6.3f} W mean",
                          f"  meas · calc {p_calc:.3f} W", "bright"))
        else:
            lines.append(("HEATER P     ",
                          f"{p_calc:6.3f} W mean",
                          f"  calc (duty × {p_full:.2f} W full)", "bright"))
        return lines

    def _poll(self):
        r = shared.latest()
        hist = shared.charts()
        ok = shared.health()
        events = shared.recent_events()
        h = snapshot()
        p_samp = r['keller_pressure_samples']
        t_samp = r['keller_temperature_samples']
        p       = (sum(p_samp) / len(p_samp)) if p_samp else None
        t       = (sum(t_samp) / len(t_samp)) if t_samp else None
        vac     = r['vacuum_chamber_mbar']
        vac_st  = r['vacuum_status']
        vac_u   = r['vacuum_gauge_V']
        te_temp = r['te_temperature_degC']
        fault   = r['tc_fault']
        up_chart   = hist['upstream']
        vac_chart  = hist['vacuum']
        te_chart   = hist['valve_temp']
        heat_chart = hist['power']

        p_s  = f"{p:.4f} bar"      if p       is not None else "---"
        t_s  = f"{t:.1f} °C"       if t       is not None else "---"
        v_s  = f"{vac:.2e} mbar"   if vac     is not None else "---"
        te_s = f"{te_temp:.2f} °C" if te_temp is not None else "---"
        p20  = p * P20_REF_K / (t + 273.15) if (p is not None and t is not None) else None
        p20_s = f"{p20:.4f} bar" if p20 is not None else "---"
        vac_note = f"  [{vac_st}]" if (vac is None and vac_st) else ""
        vac_volt = f"  ({vac_u:.2f} V at gauge)" if vac_u is not None else ""

        # thermocouple fault annotation
        fault_note = ""
        if fault:
            names = [d for b, d in FAULT_BITS.items() if fault & b]
            fault_note = "  [" + ", ".join(names) + "]"

        st = self.status_text
        st.configure(state="normal")
        st.delete("1.0", "end")
        st.insert("end", "TE-VALVE-DRIVER\n", "bright")
        for lbl, ok, avail in (
            (f"[KELLER:{'OK' if ok['keller'] else '--'}]",   ok['keller'],  True),
            (f"[VACUUM:{'OK' if ok['labjack'] else '--'}]",  ok['labjack'], LABJACK_AVAILABLE),
            (f"[VALVE-T:{'OK' if ok['tc'] else '--'}]",      ok['tc'],      LABJACK_AVAILABLE),
            (f"[CSV:{'OK' if ok['csv'] else ('ERR' if ok['csv'] is False else '--')}]",
             bool(ok['csv']), True),
        ):
            tag = "ok" if ok else ("dim" if not avail else "err")
            st.insert("end", lbl + "  ", tag)
        st.insert("end", "\n\n")
        st.insert("end", "UPSTREAM P   ", "dim") ; st.insert("end", p_s + "\n",
                  "bright" if p is not None else "dim")
        st.insert("end", "KELLER T     ", "dim") ; st.insert("end", t_s + "\n",
                  "bright" if t is not None else "dim")
        st.insert("end", "UPSTREAM P20 ", "dim") ; st.insert("end", p20_s, "bright" if p20 is not None else "dim")
        st.insert("end", "  (at 20 °C, uses Keller chip T — not the gas T)\n", "dim")
        st.insert("end", "VACUUM       ", "dim")
        st.insert("end", v_s, "bright" if vac is not None else "dim")
        st.insert("end", vac_note, "err")
        st.insert("end", vac_volt + "\n", "dim")
        st.insert("end", "VALVE T      ", "dim")
        st.insert("end", te_s, "bright" if te_temp is not None else "dim")
        st.insert("end", fault_note + "\n", "err" if fault_note else "dim")
        st.insert("end", "\n")
        for lbl, val, note, tag in self._heater_vi_lines(h):
            st.insert("end", lbl, "dim")
            st.insert("end", val, tag)
            st.insert("end", note + "\n", "dim")
        st.configure(state="disabled")

        # ── heater status lines ───────────────────────────────────────────
        mode = h['mode']
        self.arm_btn.configure(text="DISARM" if h['armed'] else "ARM",
                               fg=WARN if h['armed'] else TEXT)
        power = h['duty_actual'] * HEATER_V_RAIL ** 2 / HEATER_R_OHM
        if h['trip_reason']:
            self.heater_status.configure(
                text=f"TRIPPED — {h['trip_reason']}   (disarm, then arm to clear)",
                fg=WARN)
        elif h['armed']:
            left = "" if h['armed_at'] is None else \
                f" · off in {max(0, HEATER_MAX_RUN_S - (time.time() - h['armed_at']))/60:.0f} min"
            tgt = {'manual': "",
                   AUTO_T: f" · T_sp {h['setpoint_C']:.1f} °C",
                   AUTO_P: f" · T_sp {h['setpoint_C']:.1f} °C (from pressure)"}[mode]
            self.heater_status.configure(
                text=f"ARMED · {mode} · duty {h['duty_actual']*100:4.1f} % · "
                     f"{power:.2f} W{tgt}{left}", fg=BRIGHT)
        else:
            self.heater_status.configure(text="disarmed · output low", fg=DIM)

        if mode == AUTO_T and h['armed'] and h['t_burst'] == 'burst':
            self.loop_status.configure(
                text=f"auto-t · BURST full power, cut ~{h['t_brake']:.1f} K below "
                     f"{h['setpoint_C']:.1f} °C", fg=BRIGHT)
        elif mode == AUTO_T and h['armed'] and h['t_burst'] == 'coast':
            self.loop_status.configure(
                text=f"auto-t · coasting, heater off (peak {h['t_burst_peak']:.1f} °C) — "
                     f"PI resumes at the peak", fg=BRIGHT)
        elif mode != AUTO_P:
            self.loop_status.configure(text="")
        elif not h['armed'] or h['p_filt'] is None or h['p_init']:
            self.loop_status.configure(
                text=f"auto-p idle · target {h['p_target_mbar']:.2e} mbar · "
                     f"on arm: T_sp → {PRESSURE_SEEK_START_C:g} °C, then "
                     f"+{PRESSURE_SEEK_RATE_C_MIN:g} °C/min until the valve opens", fg=DIM)
        elif h['p_phase'] == 'seek':
            base = (f"{10 ** h['p_base']:.2e} mbar" if h['p_base'] is not None
                    else "measuring…")
            low = ""
            if h['p_base'] is not None:
                lowest = 10 ** h['p_base'] + PRESSURE_MIN_STEP_MBAR
                if 10 ** h['p_base'] < h['p_target_mbar'] < lowest:
                    low = (f" — BELOW lowest holdable ≈ {lowest:.1e}, "
                           f"will hold minimum flow")
            if h['p_burst'] == 'burst':
                step = f"BURST full power to {h['setpoint_C'] - PRESSURE_BURST_BRAKE_K:.1f} °C"
            elif h['p_burst'] == 'coast':
                step = f"coasting (peak {h['p_burst_peak']:.1f} °C)"
            elif h['p_base'] is not None and h['p_target_mbar'] <= 10 ** h['p_base']:
                step = "holding shut (target ≤ baseline)"
            elif h['p_ramping']:
                step = f"creeping +{PRESSURE_SEEK_RATE_C_MIN:g} °C/min"
            else:
                step = "heating"
            self.loop_status.configure(
                text=f"auto-p seeking · valve shut · {step} · goal {h['p_goal']:.1f} "
                     f"(upstream shift {h['p_shift']:+.1f} K) · "
                     f"T_sp {h['setpoint_C']:.2f} °C · "
                     f"baseline {base} · target {h['p_target_mbar']:.2e} mbar{low}",
                fg=WARN if (h['p_seek_capped'] or low) else BRIGHT)
        else:
            tsp_hi = min(PRESSURE_TSP_MAX_C, TEMP_TRIP_C - 5.0)
            floor  = max(PRESSURE_TSP_MIN_C, PRESSURE_OPEN_FLOOR_C + h['p_shift'])
            lowest = 10 ** h['p_base'] + PRESSURE_MIN_STEP_MBAR
            if h['p_target_mbar'] < lowest:
                self.loop_status.configure(
                    text=f"auto-p · target {h['p_target_mbar']:.2e} is BELOW the lowest "
                         f"holdable ≈ {lowest:.1e} (baseline {10 ** h['p_base']:.2e} + "
                         f"min. flow) — holding minimum flow at the {floor:.1f} °C floor · "
                         f"now {10 ** h['p_filt']:.2e}",
                    fg=WARN)
            else:
                self.loop_status.configure(
                    text=f"auto-p · target {h['p_target_mbar']:.2e} · "
                         f"baseline {10 ** h['p_base']:.2e} · "
                         f"filt {10 ** h['p_filt']:.2e} · err {h['p_err']:+.2f} dec · "
                         f"T_sp {floor:.1f}-{tsp_hi:g} °C · upstream shift {h['p_shift']:+.1f} K",
                    fg=WARN if h['p_pinned_since'] else BRIGHT)

        vac_ref = h['p_target_mbar'] if mode == AUTO_P else None
        te_ref  = h['setpoint_C'] if (h['armed'] and mode != 'manual') else None
        p_full  = HEATER_V_RAIL ** 2 / HEATER_R_OHM

        self._draw_chart(self.vac_canvas,  vac_chart,  fmt="{:.1e}", log=True, ref=vac_ref,
                         min_span=0.05, color=VAC_LINE, width=2)
        self._draw_chart(self.te_canvas,   te_chart,   fmt="{:.1f}", ref=te_ref, min_span=0.5,
                         color=TEMP_LINE, width=2)
        self._draw_chart(self.up_canvas,   up_chart,   fmt="{:.3f}", min_span=0.005,
                         color=UP_LINE, width=1)
        self._draw_chart(self.heat_canvas, heat_chart, fmt="{:.2f}",
                         ref=FLIGHT_POWER_BUDGET_W, floor=(0.0, 1.05 * p_full),
                         color=PWR_LINE, width=1)

        text = "\n".join(f"> {s}  {m}" for s, m in events[-200:])
        if getattr(self, "_last_log_text", None) != text:
            self._last_log_text = text
            self.logtext.configure(state="normal")
            self.logtext.delete("1.0", "end")
            self.logtext.insert("1.0", text)
            self.logtext.see("end")
            self.logtext.configure(state="disabled")

        if not shared.stop.is_set():
            self.root.after(150, self._poll)

    def shutdown(self):
        heater_command(armed=False)
        log_event("Shutdown — heater disarmed")
        shared.stop.set()
        # Give the device thread a moment to drive FIO0 low and release the
        # watchdog before the process exits.
        time.sleep(0.5)
        print(f"Log saved: {logfile.LOG_FILE}")
        print(f"Heater switching log: {logfile.PWM_LOG_FILE}")
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()
