# Software history log

Short records of choices that shaped the code, newest first. Each says what
was decided and why, so nobody has to rediscover the reason. Add one when a
change would otherwise puzzle someone reading the code later.

## 24. Seat screw torque must be entered before anything else — 21 Sept 2026
At start-up the seat screw torque input is the only working control: ARM,
the mode buttons, the duty / setpoint / target boxes and "update" are locked
until a valid torque is entered. While it waits the torque input is orange
(`PROMPT` in palette.py) and the rest white; once entered it turns white and
the rest unlocks. Why: every run needs its torque recorded, and auto-p's
seek reference depends on it, so a run without one is not worth starting.

## 23. Seek reference calibrated by seat screw torque — 21 Sept 2026
At 0.30 N·m, `te-sensor_20260921_142905.csv` (an auto-t run at a fixed
110 °C, used to find the cracking point directly) showed the valve opening
at 92.67 °C, upstream 4.49 bar — over 50 K above the 16 Sept reference
(40.5 °C, at an unrecorded torque, since the input didn't exist then).
Separately, `te-sensor_20260921_144015.csv` shows auto-p itself computing a
seek goal of only 31.8 °C under the old model and creeping from there —
Edward disarmed it after 68 s rather than let it run. Two compounding
causes, both now fixed: (1) nothing in the seek/goal logic used seat screw
torque at all; (2) `PRESSURE_FF_MAX_C`, an absolute 55 °C ceiling, would
have capped the goal there regardless, since it didn't move with the
upstream/torque shift the way every other pressure-loop temperature does —
so even a correct torque model would have been clamped uselessly low.

`SEAT_SCREW_CRACKING_C` (config.py) now holds real cracking points by
torque — one entry so far, `{0.30: (92.7, 4.49)}`. When the entered torque
matches an entry within `SEAT_SCREW_CRACKING_TOL_NM`, it replaces
`PRESSURE_SEEK_START_C` as the seek/goal reference, and the upstream shift
applies relative to *that entry's own* upstream pressure, not
`PRESSURE_UP_REF_BAR` — the calibration already includes whatever upstream
effect was present when it was measured, so shifting from the historical
2.76 bar reference as well would double-count it. `PRESSURE_FF_MAX_C` now
moves with the same combined shift, so a calibrated reference above the old
55 °C ceiling is no longer clamped. An entered torque matching nothing logs
a one-time "UNVERIFIED for this torque" warning and falls back to the
historical reference — unchanged from today's behaviour, since we still
know nothing about any other torque. Replayed against the real upstream
pressure from the second file (4.60 bar), the new seek starts at 91.4 °C —
1.3 °C from the measured cracking point, versus the old model's 30.5 °C.

The golden record needed no changes: with no torque entered, the new code
path is bit-for-bit the old one (proven — all 15 scenarios still match
exactly), including a floating-point trap found along the way (adding a
zero-valued torque offset flipped a logged `-0.0 K` to `+0.0 K`). 8 new
tests cover the calibrated and uncalibrated paths, tolerance matching, the
upstream shift anchored to the calibration's own reference, and the
feedforward-ceiling fix specifically — each planted back as a bug to
confirm the tests catch it, including a repeat of today's actual fault.

Still open: only one torque is calibrated. A different, uncalibrated torque
falls back to the 16 Sept reference exactly as before, which we now know
can be wrong by 50+ K — the warning makes that visible, but doesn't fix it.
Calibrating more torques needs more auto-t characterisation runs like the
first file here.

## 22. Valve-opening marker could go missing — 21 Sept 2026
`TE_PLOTTER` seeded its chamber-pressure baseline from the globally lowest
2% of readings. If the chamber kept pumping down over a run, so the pressure
after the valve closed sat lower than before it opened, that seed grabbed
almost entirely from the low end, spanned too little time to fit a drift
line, and the whole pre-open period then read as "above baseline" — merging
with the real opening into one run with no recorded start, so no opening
marker was drawn (closing still was). Nothing tested this function.

The seed level is now taken from each time bin's own lowest point (so both
ends of the log contribute, whichever sits lower), using the 40th percentile
of the bins' minima as the level rather than seeding from the minima
directly — a bin fully inside an open period has no genuinely quiet sample,
and the existing refinement only ever excludes points *above* the fit, so
seeding from such a bin's minimum directly would anchor the baseline to it
permanently. 12 tests were added (there were none before): no event, drift
alone, a clean open/close, both baseline directions, open before the log,
still open at the end, a short blip ignored, two openings merged or kept
separate, and a missing chamber column. 3 of the 12 fail against the old
code; the other 9 already worked and still do.

## 21. Measured heater means are time-weighted — 21 Sept 2026
GitHub's test run failed: on its busy shared machine, ticks came irregularly,
and the mean over the last period (entry 15) gave every sample equal weight,
so a stall during one gate state over-counted the other. Each sample now
counts for the time since the previous one, which is what mean power means.
The end-to-end test compared the reading with an ideal 50 %, but a stalled
tick really does keep the heater on (or off) longer; it now compares with
the fake gate's own record, and its tolerance comes from the sample spacing
it actually observes. Tested under full CPU load: 8 of 8 passes. The exact
weighting is pinned by a timing-free unit test
(`test_period_mean_weights_samples_by_time`), which the old equal weights
fail; the end-to-end test still catches the count-of-samples bug of entry 15.

## 20. The README is the front page again — 18 Sept 2026
The old single-file driver opened with a ~125-line description (sensors,
modes, wiring, why the heater is switched in software, safety, logs). The
split moved it into `driver/__init__.py`, where nobody reads it, and it went
out of date there. It is now in README.md, rewritten with current names and
features; the CSV column list is replaced by a pointer to `driver/schema.py`
so it can't go stale again, and `driver/__init__.py` just points to the
README.

## 19. Seat screw torque — 18 Sept 2026
The operator can now record the torque on the TE-Valve's seat screw, in N·m
(see context.md). Agreed before any code: the name (seat screw torque, the
shortest unambiguous one), and that it starts blank every session, because
a remembered value could silently outlive a re-torque. It is entered in its
own row under the heater controls (a decimal point or comma both work;
0–5 N·m, `SEAT_SCREW_TORQUE_MAX_NM`), shown as `SEAT SCREW` in the readout,
written to every CSV row (`seat_screw_torque_Nm`, appended; blank = not
recorded, never 0) and logged as an event on every entry. A refused entry
leaves the box showing the value actually in use. TE_PLOTTER puts the value
in the figure title and summary and marks mid-run changes with dotted
purple lines; older logs say "not recorded". The tests were written first
(`test_seat_screw.py`, plus five in `test_plotter.py`).

## 18. gui.py split; the window's text is now testable — 17 Sept 2026
`gui.py` was 628 lines doing layout, formatting, chart drawing and input
handling. The text is now `readout.py` (pure functions: readings in, strings
and tags out), the charts are `charts.py`, the colours are `palette.py`, and
`gui.py` (376 lines) builds the window and feeds them. `readout.py` needs no
Tk, so `test_readout.py` checks every displayed line directly; deliberately
broken versions (swapped labels, a trip shown as armed, a missing fault note,
a stale reading) all make it fail. The window renders pixel-identically to
before the split.

## 17. The measured readout belongs to the device thread — 17 Sept 2026
The heater state held three different things: the operator's commands, the
controller's internals, and the measured voltage / current the device thread
writes. The measurements now live in `shared.py`
(`store_heater_output` / `heater_output`), so the controller's state is only
what the controller decides. The GUI and logger read the two separately.

## 16. Heater electrical arithmetic in one place — 17 Sept 2026
`duty x V^2 / R` and its relatives appeared seven times across four files.
They are now `config.heater_power_w()`, `heater_current_a()`,
`heater_voltage_v()` and `power_from_voltage_w()`, and a test fails if the
formulas reappear anywhere else.

## 15. Measured heater means cover one period of time — 17 Sept 2026
The measured V, I and power means averaged the last 20 samples, assuming
50 ms ticks. On Windows the waits are rounded up to the ~15.6 ms timer steps,
so ticks run long; 20 samples then spanned more than one PWM period and the
mean swung with the cycle (a test on the lab laptop read 40 % instead of
50 %). The samples are now kept with their times and averaged over the last
`HEATER_PWM_PERIOD_S` seconds. This only matters once the sense inputs are
wired. The test now slows the ticks to 75 ms and watches the readout for two
periods; the old code strays by 20 % of full power there.

## 14. devices.py split; the LabJack tick in named steps — 17 Sept 2026
`devices.py` mixed the Keller, the LabJack, the thermocouple chip and a
290-line loop. It is now `keller.py`, `thermocouple.py` and `labjack.py`.
In `labjack.py` one connection is a `_Session`, and each tick reads as
`_sense_heater → [_read_vacuum → _service_thermocouple → _run_controller] →
_drive_gate`; a device error raises `_DeviceLost`, which ends the session
through `_release` (gate low, watchdog released) and reconnects. The
statements inside each step are unchanged. Before the split, 9 tests were
added for the loop's error and sensing paths (`test_device_faults.py`);
they, the existing device tests and the golden record all pass on the new
code, and deliberately broken versions of the new code make them fail.

## 13. Heater state fields are fixed — 17 Sept 2026
The heater state is a dict with text keys, so a misspelt key on assignment
used to create a new field silently while the real one stayed unchanged.
`controller.HeaterState` now refuses unknown or removed fields. A dataclass
with named attributes would catch typos even earlier (in the editor), but
means rewriting every access in the controller and GUI; this gets most of
the benefit with no change to the code that uses the state.

## 12. Shared state only through functions — 17 Sept 2026
`shared.py` used to hand out its dictionaries, lists and locks, and the GUI,
logger and device thread reached straight in: the logger reset the heater's
on-time bookkeeping itself, and the device thread disarmed the heater by
writing its fields. Every module had to know the key names and locking rules.
Now `shared.py` keeps readings, charts, events and health flags private
behind named functions (`store_keller`, `latest`, `take_log_readings`, …),
and `control.py` alone owns the heater state (`snapshot`, `take_on_time`,
`force_off`, …). The locking lives in those two files only.
`test_architecture.py` fails if any module touches another's private names
or if either file exposes a raw container. Behaviour is unchanged: the golden
record still matches, and the GUI and logs were checked end to end.

## 11. One set of names for modes and the valve temperature — 17 Sept 2026
The modes were "auto (T)" / "auto (P)" on screen but `auto` / `pressure` in
the code and CSV. They are now `manual`, `auto-t` and `auto-p` everywhere,
defined once in `controller.MODES`; an unknown name is refused rather than
silently running the temperature loop. The CSV `heater_mode` column carries
the new names from 17 Sept 2026; the plotter accepts both. The thermocouple
reading is "valve temperature" on screen and in the plotter (the CSV column
keeps its name, `te_temperature_degC`). Control behaviour is unchanged.

## 10. The controller takes everything as arguments — 17 Sept 2026
`controller.step(h, now, dt, readings…)` returns `(duty, messages)` and
touches nothing else: no lock, clock, event log or shared readings. That
makes every rule testable with a plain dict and a number for the time.
`control.py` is the only place that adds threads: it reads the upstream
pressure, holds `heater_lock` for the whole step (so a GUI command can no
longer land halfway through one), and logs the messages afterwards. The
state stays a dict, changed in place, because the GUI and logger read it.
Behaviour is unchanged: the golden record matches through both paths.

## 9. Split the driver into the `driver` package — 17 Sept 2026
The single file had grown to ~2,550 lines mixing GUI, threads, control law,
interlocks and logging, so any change risked the rest. It is now a package
with one job per module (see README). Before splitting, the control law's
behaviour was recorded over 15 scenarios (`tests/golden/`); the package
reproduces it exactly, and the GUI renders pixel-identically. Behaviour did
not change. Importing the package no longer creates log files; `app.main()`
does that at start-up.

## 8. CSV columns are append-only and defined once — 17 Sept 2026
`driver/schema.py` defines both logs' columns; the driver writes with it and
the plotter reads with it. Columns are only ever appended, never renamed or
reordered, so old logs and old readers keep working. The plotter keeps its
loose name matching only for logs from before the schema existed.

## 7. Log every heater gate edge — 17 Sept 2026
A separate `_pwm.csv` records each switch with its time, because the 0.5 s
main log cannot place edges that come up to several times a second. The main
log gets `heater_on_s` from the same edge times, so energy per row is exact.
The log records what the software commanded, not a measured voltage.

## 6. Chart heater power, not current — 17 Sept 2026
Under time-proportioning, mean power = duty × V²/R. Mean V × mean I would
understate it (duty² × V²/R). With calculated values, current and power have
the same shape, so the chart shows power (the physical quantity, and what
the 1 W flight budget is about). Duty and current stay in the log: duty is
what the controller did and does not depend on the assumed 24 V / 88 Ω.

## 5. Chart raw upstream pressure, not P20 — 17 Sept 2026
P20 divided by the Keller's temperature, which is the sensor chip's, not the
gas's. Gripping the sensor head raised that temperature without changing
the gas, and P20 dropped accordingly. The raw absolute pressure is charted
instead. A proper correction needs a temperature sensor on the upstream
tubing.

## 4. auto-p is a cascade with seek and track phases — Sept 2026
The valve snaps open near 40 °C rather than throttling, and chamber pressure
spans decades. So the outer loop sets a temperature setpoint for the
proven temperature PI: it seeks (burst, coast, creep) with the valve shut
while measuring the baseline, detects the opening, then tracks log10(p).

## 3. Software time-proportioning on FIO0, no LabJack timers
Heater duty is produced by toggling FIO0 on the device thread's 50 ms tick
over a 2 s period. LabJack timers are not used because a timer may claim
FIO4, which is the MAX31856 SDO line. The U3 firmware watchdog pulls FIO0
low if the program dies.

## 2. MAX31856 set to the 50 Hz mains filter
CR0 = 0x91. The previous value left the notch at 60 Hz, which is wrong in
Switzerland.

## 1. Only one driver instance at a time
An OS file lock (`.te-valve-driver.lock`) stops a second copy starting,
because a forgotten instance may still be driving the heater.
