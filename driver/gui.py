"""Tkinter 'Live Log' window: status, readouts, heater controls, charts.
"""


import math
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

from . import batchrun
from . import logfile
from . import readout
from . import shared
from .config import (
    BATCH_MIN_TESTS, BATCH_PRECISION_K, BATCH_TEST_RUNS_DEFAULT, BATCH_TEST_RUNS_MAX,
    BATCH_TOPUP_DROP_DEFAULT_BAR,
    FLIGHT_POWER_BUDGET_W, HEATER_I_AIN, HEATER_MAX_DUTY, HEATER_PWM_PERIOD_S,
    HEATER_R_OHM, HEATER_V_AIN, PID_SETPOINT_DEFAULT, PRESSURE_TARGET_DEFAULT,
    PRESSURE_TARGET_MIN, PRESSURE_TRIP_MBAR, TEMP_TRIP_C, heater_current_a,
    heater_power_w, heater_voltage_v,
)
from .control import AUTO_P, AUTO_T, MANUAL, MODES, heater_command, snapshot
from .charts import draw_chart, make_chart
from .labjack import LABJACK_AVAILABLE
from .openings import describe as openings_text
from .palette import (
    BG, BORDER, BRIGHT, DIM, FIELD, FIELD_HOT, PROMPT, PWR_LINE, TEMP_LINE, TEXT, UP_LINE,
    VAC_LINE, WARN,
)

# readout.py returns a tag per line; the window turns it into a colour.
TAG_COLOUR = {'bright': BRIGHT, 'dim': DIM, 'warn': WARN, 'err': WARN, 'ok': BRIGHT,
              'prompt': PROMPT}
from .shared import log_event


# ═══════════════════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════════════════



class TEGui:
    """Single 'Live Log' window: device status, live readouts (including heater
    voltage and current), heater controls, strip charts, and an event log."""

    def __init__(self, root=None):
        # root: the window to build in. None = a new Tk interpreter (the
        # program). Tests pass a Toplevel of one shared interpreter instead,
        # since starting a fresh Tcl for every test intermittently fails to
        # load init.tcl on Windows.
        self.root = root if root is not None else tk.Tk()
        self.root.title("TE Valve — Live Log")
        self.root.configure(bg=BG)
        screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"800x{max(600, min(1100, screen_h - 90))}+20+10")
        self.root.minsize(700, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

        M = ("Consolas", "Menlo", "Courier New", "DejaVu Sans Mono", "monospace")
        self.f = self._pick_font(M, 11)

        self._build_log_window()
        self._poll_job = self.root.after(150, self._poll)

    def _pick_font(self, families, size, weight="normal"):
        available = set(tkfont.families())
        fam = next((f for f in families if f in available), families[-1])
        return tkfont.Font(family=fam, size=size, weight=weight)

    def _build_log_window(self):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=8, pady=8)

        self.status_text = tk.Text(outer, bg=BG, fg=TEXT, font=self.f,
                                   height=14, bd=0, highlightthickness=0,
                                   state="disabled", wrap="none", cursor="arrow")
        self.status_text.pack(fill="x")
        self.status_text.tag_config("bright", foreground=BRIGHT)
        self.status_text.tag_config("dim",    foreground=DIM)
        self.status_text.tag_config("ok",     foreground=BRIGHT)
        self.status_text.tag_config("err",    foreground=WARN)
        self.status_text.tag_config("prompt", foreground=PROMPT)

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
        self.vac_canvas = make_chart(charts, "vacuum chamber (mbar, log)   dashed = target")
        self.te_canvas  = make_chart(charts, "valve temperature (°C)   dashed = setpoint")
        self.up_canvas  = make_chart(charts, "upstream pressure (bar abs, Keller raw)")
        self.heat_canvas = make_chart(
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
        self.mode_buttons = []
        for mode in MODES:
            rb = tk.Radiobutton(row1, text=mode, value=mode, variable=self.mode_var,
                                command=self._on_mode, font=self.f, fg=TEXT, bg=BG,
                                selectcolor=BG, activebackground=BG,
                                activeforeground=BRIGHT, bd=0,
                                highlightthickness=0)
            rb.pack(side="left", padx=(0, 6))
            self.mode_buttons.append(rb)

        row2 = tk.Frame(parent, bg=BG)
        row2.pack(fill="x", pady=(0, 4))
        self.duty_entry = self._entry(row2, "duty %", "0", width=6)
        self.sp_entry   = self._entry(row2, "setpoint °C", f"{PID_SETPOINT_DEFAULT:g}", width=6)
        self.p_entry    = self._entry(row2, "target mbar", f"{PRESSURE_TARGET_DEFAULT:.1e}", width=9)
        self.update_btn = tk.Button(row2, text="update", command=self._send_update, **btn)
        self.update_btn.pack(side="left")

        # Seat screw torque: a TE-Valve setting, not a heater setting, so it
        # has its own row and "set" button. Blank until the operator enters it,
        # and until then it is the only control that works (_apply_gate).
        row3 = tk.Frame(parent, bg=BG)
        row3.pack(fill="x", pady=(0, 4))
        self.seat_entry = self._entry(row3, "seat screw N·m", "", width=6)
        self.seat_entry.bind("<Return>", lambda _ev: self._set_seat_screw())
        self.seat_btn = tk.Button(row3, text="set", command=self._set_seat_screw, **btn)
        self.seat_btn.pack(side="left")

        # Batch of opening-point runs (context.md, "Batches"). While one runs
        # it owns the heater: the heater controls and the torque are locked,
        # and DISARM aborts it.
        row4 = tk.Frame(parent, bg=BG)
        row4.pack(fill="x", pady=(0, 4))
        self.runs_entry = self._entry(row4, "test runs (max)", f"{BATCH_TEST_RUNS_DEFAULT}", width=4)
        self.runs_entry.unbind("<Return>")
        self.batch_btn = tk.Button(row4, text="start batch", command=self._start_batch, **btn)
        self.batch_btn.pack(side="left", padx=(0, 6))
        self.abort_btn = tk.Button(row4, text="abort batch", command=self._abort_batch, **btn)
        self.abort_btn.pack(side="left")
        row5 = tk.Frame(parent, bg=BG)
        row5.pack(fill="x", pady=(0, 4))
        self.topup_entry = self._entry(row5, "pause for top-up if upstream falls (bar, blank = never)",
                                       f"{BATCH_TOPUP_DROP_DEFAULT_BAR:g}", width=5)
        self.topup_entry.unbind("<Return>")
        self.continue_btn = tk.Button(row5, text="continue", command=self._continue_batch, **btn)
        self.continue_btn.pack(side="left")
        self.batch_status = tk.Label(parent, text="", font=self.f, fg=DIM, bg=BG, anchor="w")
        self.batch_status.pack(fill="x")
        self._batch_running = False
        self._confirm = messagebox.askokcancel      # tests replace these two
        self._ask = messagebox.askyesnocancel

        self.heater_status = tk.Label(parent, text="", font=self.f, fg=DIM,
                                      bg=BG, anchor="w")
        self.heater_status.pack(fill="x")
        self.loop_status = tk.Label(parent, text="", font=self.f, fg=DIM,
                                    bg=BG, anchor="w")
        self.loop_status.pack(fill="x")
        self._update_inputs()
        self._send_update()
        self._apply_gate()

    def _apply_gate(self):
        """Lock every other control until the seat screw torque is entered.
        While locked the torque input is orange and the rest white; once a
        torque is in, the torque input turns white and the rest work as usual
        (readout.torque_gate)."""
        locked, tag = readout.torque_gate(shared.seat_screw_torque())
        self._locked = locked
        colour = TAG_COLOUR[tag]
        # The focus ring (highlightcolor) matches the border, or the box would
        # lose its colour the moment the cursor is in it.
        self.seat_entry.configure(fg=colour, insertbackground=colour,
                                  highlightbackground=colour, highlightcolor=colour,
                                  highlightthickness=2 if locked else 1)
        self.seat_entry.label.configure(fg=colour)
        self.seat_btn.configure(fg=colour,
                                highlightbackground=PROMPT if locked else BORDER)
        buttons = [self.arm_btn, *self.mode_buttons, self.update_btn]
        entries = [self.duty_entry, self.sp_entry, self.p_entry]
        self._apply_batch_lock()
        if locked:
            for w in buttons + entries:
                w.configure(state="disabled", disabledforeground=BRIGHT)
            for e in entries:
                e.configure(highlightbackground=BORDER)
                e.label.configure(fg=BRIGHT)
            self.seat_entry.focus_set()
        else:
            for w in buttons:
                w.configure(state="normal", disabledforeground=DIM)
            for e in entries:
                e.configure(disabledforeground=DIM)
            self._update_inputs()          # the selected mode's box only, as before

    def _apply_batch_lock(self):
        """While a batch runs it owns the heater: mode, duty, setpoint,
        target, update and the torque are locked; ARM stays (as DISARM,
        which aborts the batch). Start batch needs a torque, no batch
        running and the heater disarmed; abort only works during one."""
        busy = batchrun.running()
        self._batch_running = busy
        heater = [*self.mode_buttons, self.update_btn, self.duty_entry, self.sp_entry,
                  self.p_entry, self.seat_entry, self.seat_btn, self.runs_entry,
                  self.topup_entry]
        if busy:
            for w in heater:
                w.configure(state="disabled")
            for w in (*self.mode_buttons, self.update_btn, self.seat_btn):
                w.configure(disabledforeground=DIM)
            for e in (self.duty_entry, self.sp_entry, self.p_entry, self.runs_entry,
                      self.topup_entry):
                e.configure(highlightbackground=BORDER)
                e.label.configure(fg=DIM)
        elif not getattr(self, "_locked", True):
            for w in (*self.mode_buttons, self.update_btn, self.seat_btn):
                w.configure(state="normal")
            for e in (self.seat_entry, self.runs_entry, self.topup_entry):
                e.configure(state="normal")
            self._update_inputs()
        else:
            for e in (self.seat_entry, self.runs_entry, self.topup_entry):
                e.configure(state="normal")
            self.seat_btn.configure(state="normal")
        can_start = (not busy and shared.seat_screw_torque() is not None
                     and not snapshot()['armed'])
        self.batch_btn.configure(state="normal" if can_start else "disabled",
                                 disabledforeground=DIM)
        self.runs_entry.label.configure(fg=DIM if busy else TEXT)
        self.topup_entry.label.configure(fg=DIM if busy else TEXT)
        waiting = batchrun.paused()
        self.continue_btn.configure(state="normal" if waiting else "disabled",
                                    fg=PROMPT if waiting else TEXT, disabledforeground=DIM,
                                    highlightbackground=PROMPT if waiting else BORDER)
        self.abort_btn.configure(state="normal" if busy else "disabled",
                                 fg=WARN if busy else TEXT, disabledforeground=DIM)

    def _start_batch(self):
        """Confirm the torque (and show the measured upstream pressure),
        then start. The batch arms the heater itself, run by run."""
        torque = shared.seat_screw_torque()
        try:
            n = int(self.runs_entry.get().strip())
        except ValueError:
            log_event(f"Batch: test runs '{self.runs_entry.get()}' is not a whole number")
            return
        if not 1 <= n <= BATCH_TEST_RUNS_MAX:
            log_event(f"Batch: test runs must be 1 to {BATCH_TEST_RUNS_MAX}")
            return
        text = self.topup_entry.get().strip().replace(",", ".")
        try:
            topup = float(text) if text else None
            if topup is not None and not 0.0 < topup < 10.0:
                raise ValueError
        except ValueError:
            log_event(f"Batch: top-up '{text}' should be a pressure drop in bar "
                      f"(e.g. 0.3), or blank for never")
            return
        if torque is None:
            log_event("Batch not started — enter the seat screw torque first")
            return
        problem = batchrun.start_problem(n, topup)
        if problem:
            log_event(f"Batch not started — {problem}")
            return
        up, _ = shared.upstream()
        up_txt = f"{up:.3f} bar (measured)" if up is not None else "not read (Keller offline)"
        entry, usable, why_not = batchrun.remembered(torque)
        common = (f"Upstream pressure: {up_txt}\n"
                  f"Test runs: up to {n}; it stops once the mean T_open is known "
                  f"to ±{BATCH_PRECISION_K:g} K (at least {BATCH_MIN_TESTS})\n"
                  + (f"Pause for a top-up if upstream falls {topup:g} bar\n" if topup
                     else "No top-up pauses\n") +
                  "\nThe batch arms the heater itself for each run. "
                  "SW171 must be on. DISARM or 'abort batch' stops it.")
        retorqued = None
        if usable:
            answer = self._ask(
                "Start batch",
                f"Seat screw torque: {torque:.2f} N·m\n"
                f"Remembered opening point: {openings_text(entry)}\n\n"
                f"Has the seat screw been re-torqued (or the valve disturbed) since "
                f"the last batch at {torque:.2f} N·m?\n\n"
                f"No: start from the remembered opening point (no scouts).\n"
                f"Yes: find it again with scout 1 and scout 2.\n\n" + common)
            if answer is None:
                log_event("Batch not started (cancelled)")
                return
            retorqued = bool(answer)
        else:
            note = (f"Remembered opening point not used ({why_not}): scout 1 and "
                    f"scout 2 first.\n" if entry is not None else
                    "No remembered opening point: scout 1 and scout 2 first.\n")
            if not self._confirm(
                    "Start batch",
                    f"Seat screw torque: {torque:.2f} N·m\n"
                    f"Is that the torque on the valve now?\n\n" + note + common):
                log_event("Batch not started (cancelled)")
                return
        ok, msg = batchrun.start(n, logfile.LOG_FILE, topup, retorqued=retorqued)
        if not ok:
            log_event(msg)
        self._apply_gate()

    def _continue_batch(self):
        batchrun.resume()
        self._apply_gate()

    def _abort_batch(self):
        batchrun.abort("aborted by the operator")
        self._apply_gate()

    def _set_seat_screw(self):
        """Record the seat screw torque typed in the box."""
        try:
            nm = readout.parse_seat_screw_torque(self.seat_entry.get())
        except ValueError as err:
            log_event(str(err))
            current = shared.seat_screw_torque()      # show what is really in use
            self._set_entry(self.seat_entry, "" if current is None else f"{current:g}")
            self._apply_gate()
            return
        shared.set_seat_screw_torque(nm)
        self._set_entry(self.seat_entry, f"{nm:g}")
        self._apply_gate()

    def _toggle_arm(self):
        if self._locked and not snapshot()['armed']:
            log_event("Enter the seat screw torque first — ARM is locked until then")
            return
        if snapshot()['armed']:
            heater_command(armed=False)
            log_event("Heater DISARMED by operator")
            if batchrun.running():
                batchrun.abort("heater disarmed by the operator")
        elif batchrun.running():
            log_event("A batch is running and arms the heater itself — "
                      "'abort batch' to stop it")
        else:
            self._send_update(quiet_if_unchanged=True)   # picks up un-sent edits to the active value, logged
            heater_command(armed=True)
            log_event(f"Heater ARMED by operator · {self._active_summary()}")

    def _active_summary(self):
        a = self._applied
        return readout.mode_summary(self.mode_var.get(), a['duty'], a['sp'], a['tgt'])

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

    def _heater_vi_lines(self, h, out):
        """Return [(label, value, note, value_tag)] for the V and I readouts.
        h: heater state (control.snapshot()); out: what the device
        thread measures (shared.heater_output())."""
        if not shared.health()['labjack']:
            return [("HEATER V     ", "---", "", "dim"),
                    ("HEATER I     ", "---", "", "dim"),
                    ("HEATER P     ", "---", "", "dim")]
        d      = h['duty_actual']
        v_mean = heater_voltage_v(d)
        i_mean = heater_current_a(d)
        state  = "ON " if out['out_high'] else "off"
        lines  = []

        if out['v_meas'] is not None and out['v_meas_mean'] is not None:
            rail = out['rail_meas']
            lines.append(("HEATER V     ",
                          f"{out['v_meas']:6.2f} V {state} · {out['v_meas_mean']:6.2f} V mean",
                          f"  meas · rail {rail:.2f} V · calc {v_mean:.2f} V", "bright"))
        else:
            lines.append(("HEATER V     ",
                          f"{out['v_now']:6.2f} V {state} · {v_mean:6.2f} V mean",
                          "  calc — assumes SW171 on, 24 V present", "bright"))

        if out['i_meas'] is not None and out['i_meas_mean'] is not None:
            lines.append(("HEATER I     ",
                          f"{out['i_meas']:6.3f} A {state} · {out['i_meas_mean']:6.3f} A mean",
                          f"  meas · calc {i_mean:.3f} A", "bright"))
        else:
            lines.append(("HEATER I     ",
                          f"{out['i_now']:6.3f} A {state} · {i_mean:6.3f} A mean",
                          f"  calc ({HEATER_R_OHM:g} Ω element)", "bright"))

        p_full = heater_power_w()
        p_calc = heater_power_w(d)
        if out['p_meas_mean'] is not None:
            lines.append(("HEATER P     ",
                          f"{out['p_meas_mean']:6.3f} W mean",
                          f"  meas · calc {p_calc:.3f} W", "bright"))
        else:
            lines.append(("HEATER P     ",
                          f"{p_calc:6.3f} W mean",
                          f"  calc (duty × {p_full:.2f} W full)", "bright"))
        return lines

    def _poll(self):
        """Redraw everything from the current readings. Runs every
        GUI_REFRESH_MS on the Tk thread."""
        r = shared.latest()
        hist = shared.charts()
        h = snapshot()
        mode = h['mode']

        st = self.status_text
        st.configure(state="normal")
        st.delete("1.0", "end")
        for text, tag in readout.status_segments(
                r, shared.health(), h, shared.heater_output(), LABJACK_AVAILABLE,
                shared.seat_screw_torque()):
            st.insert("end", text, tag)
        st.configure(state="disabled")

        self.arm_btn.configure(text="DISARM" if h['armed'] else "ARM",
                               fg=WARN if h['armed'] else TEXT)
        if self._locked != (shared.seat_screw_torque() is None):
            self._apply_gate()
        if self._locked:
            text, tag = ("Enter the seat screw torque (N·m) to begin — "
                         "everything else is locked until then", 'prompt')
        else:
            text, tag = readout.heater_status(h)
        self.heater_status.configure(text=text, fg=TAG_COLOUR[tag])
        text, tag = readout.loop_status(h)
        self.loop_status.configure(text=text, fg=TAG_COLOUR[tag])
        busy = batchrun.running()
        line = batchrun.status()
        self.batch_status.configure(text=line or "", fg=BRIGHT if busy else DIM)
        if busy != self._batch_running:
            if not busy:
                # The batch drove the heater in auto-t; back to the window's settings.
                self._send_update(quiet_if_unchanged=True)
            self._apply_gate()
        else:
            self._apply_batch_lock()

        events = shared.recent_events()
        up_chart   = hist['upstream']
        vac_chart  = hist['vacuum']
        te_chart   = hist['valve_temp']
        heat_chart = hist['power']
        vac_ref = h['p_target_mbar'] if mode == AUTO_P else None
        te_ref  = h['setpoint_C'] if (h['armed'] and mode != 'manual') else None
        p_full  = heater_power_w()

        draw_chart(self.vac_canvas,  vac_chart, self.f,  fmt="{:.1e}", log=True, ref=vac_ref,
                         min_span=0.05, color=VAC_LINE, width=2)
        draw_chart(self.te_canvas,   te_chart, self.f,   fmt="{:.1f}", ref=te_ref, min_span=0.5,
                         color=TEMP_LINE, width=2)
        draw_chart(self.up_canvas,   up_chart, self.f,   fmt="{:.3f}", min_span=0.005,
                         color=UP_LINE, width=1)
        draw_chart(self.heat_canvas, heat_chart, self.f, fmt="{:.2f}",
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
            self._poll_job = self.root.after(150, self._poll)

    def shutdown(self):
        batchrun.abort("driver closed")          # writes the batch's files first
        heater_command(armed=False)
        log_event("Shutdown — heater disarmed")
        shared.stop.set()
        # Give the device thread a moment to drive FIO0 low and release the
        # watchdog before the process exits.
        time.sleep(0.5)
        print(f"Log saved: {logfile.LOG_FILE}")
        print(f"Heater switching log: {logfile.PWM_LOG_FILE}")
        self.destroy()

    def destroy(self):
        """Close the window, cancelling the pending redraw first so it can't
        fire on a window that no longer exists."""
        try:
            self.root.after_cancel(self._poll_job)
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()
