# Software history log

Short records of choices that shaped the code, newest first. Each says what
was decided and why, so nobody has to rediscover the reason. Add one when a
change would otherwise puzzle someone reading the code later.

## 28. Batches made for the real rig — 24 Sept 2026
Two logs from 24 Sept 2026 (0.45 N·m) showed what a batch has to live with:
te-sensor_20260924_155503 (an opening at ~146 °C, 2.7 bar) and
te-sensor_20260924_161238 (cooling, heater off).
* After the opening the chamber was back within 6 % of its pre-opening
  level ~90 s after disarming (at 73 °C: the valve closed ~70 K below where
  it opened). So entry 27's cooldown rule holds.
* The chamber keeps pumping down slowly (1.5 %/min), noise ~0.003 decades.
* Filling upstream (0.96 → 3.16 bar) with the valve cold at 29 °C made the
  chamber jump 1.4 → 2.35e-6 mbar, falling back over ~4 min.
* Upstream leaked 0.05-0.07 bar/min (0.015 on 14 Sept): over an hour's
  batch, more than 1 bar, i.e. ~12 K or more of opening point.
* 0.45 N·m opened at ~146 °C: 4 K below the 150 °C ceiling, and detection
  needs a few K above the opening.

So:
1. **Settle before every run** (heater off): the chamber must be neither
   rising (a fill's jump or outgassing looks like an opening) nor falling
   fast (the median baseline lags it). Replaces scout 1's fixed baseline wait.
   After a top-up only readings from after the fill count — without that,
   the flat minute before the fill made it look settled at once and the jump
   was detected as an opening at 36 °C (in simulation, true opening 60 °C).
   The baseline is measured fresh while settling, and no longer carried from
   the previous run.
2. **Upstream is a measured variable.** Each batch fits T_open against the
   upstream pressure at each opening; the scatter left about that fit is
   what the leak doesn't explain. In simulation with −12 K/bar and the
   0.07 bar/min leak: slope recovered within 1.5 K/bar, scatter 0.05 K
   against 1.5 K about the plain mean.
3. **Optional top-up pause** (default 0.3 bar below the batch's start),
   heater off, until the operator presses continue.
4. **Ceiling 155 °C** (as `PRESSURE_TSP_MAX_C`, 5 K below the trip).
5. **Start checks**: no valve temperature, no chamber reading, or (with the
   top-up pause) no upstream reading refuses the start. A rising chamber
   doesn't: the batch waits for it.

Tried and dropped: a baseline extrapolated along the fitted trend, to
follow a falling chamber. With the never-rise rule its noise walked it
down, and it produced false openings (52.8 °C for a 60 °C valve). The
limit on how fast the chamber may fall before heating does the same job
robustly.

Found on the way: a high background hides the first flow, so T_open reads
high early in a steep pump-down (60.5-61.7 °C for 60.4 °C in simulation,
converging as it falls). It never reads early.

## 27. Batches: repeated opening-point runs — 24 Sept 2026
Goal: characterise the opening point against seat screw torque and upstream
pressure, averaged over several runs. Torque is set by hand; upstream
pressure is measured, not controlled (the rig leaks). Flow after opening and
closing were deliberately left out: map the opening point first, then
characterise around it. The run is a sequence of phases, so later phases
(settle, step-down) can be added without a rewrite.

A run: approach (auto-t) → creep at 3 °C/min → detection → heater disarmed →
cooldown. Choices, with the reason for each:
* **Two scouts, not one.** A fast scout reads high (the TC lags the seat by
  roughly rate × tau: ~8.5 K at 3 °C/min). Starting the test runs from it
  risked starting above the opening point. Scout 2 creeps from 10 K below
  scout 1; test runs start 5 K below scout 2. Scout 2 also gives the test
  runs an identical predecessor (creep, heater off at detection), which is
  why there is no soak before a run.
* **Same creep rate every run**, so the TC lag is a repeatable offset, not
  scatter.
* **T_open backdated to the onset**, because detection confirms a few
  seconds after the rise begins. The onset is the last sample (3-point
  median) within 0.015 decades of the baseline; this is a best guess, and
  T_detect is logged beside it. No extra lag correction is applied.
* **Heater energy from the approach start** is logged at onset and
  detection, since the opening depends on the temperature distribution
  (Invar piece against stainless sleeve), not the TC alone. The heater is
  off before every approach, so energy since the run's start would be the
  same number.
* **Heater disarmed at detection**: shortest time open, smallest pressure
  excursion, cooldown starts at once. Each run re-arms, so the 60-minute
  armed limit applies per run, not per batch. All interlocks stay active.
* **The batch adds its own chamber checks** while heating (auto-t alone has
  none): no valid reading for 2 s, or above 5e-4 mbar, aborts the batch.
* **Cooldown floor 28 °C**: T_open − 20 K can be below what the lab lets the
  valve cool to (0.25 N·m opens near 40 °C).
* Operator disarm or a trip during a batch aborts it; a batch cannot resume.
* Averages are over test runs that opened; scouts and failed runs are in the
  Runs sheet but not in the averages.

The opening map is an Excel workbook because that is where the results are
used. If Excel has it open (Windows locks it), rows go to side files next to
it and are merged the next time it can be saved.

**Changed after simulation** (tests/batch_sim.py: the real controller on a
thermal model whose seat lags the TC by 5 s, throttles with a 4 K e-fold
and closes with hysteresis):
* *Recovery threshold.* "Chamber back within 20 % of baseline" is looser
  than the opening threshold (+12 %), so the next run was detected as open
  within a second of starting. Recovery is now within 0.025 decades (≈ 6 %),
  and a test checks it stays below the detection threshold.
* *Scout 2 repeats.* With a 20 s seat lag, scout 1 read 35 K high and
  scout 2 opened while still heating to its start. A scout 2 that opens
  before it creeps is repeated 10 K lower (and below where it opened), up
  to three tries; then the batch stops.
* *e-fold* is fitted on the rise above baseline (as in the 16 Sept flow
  map), not on log(total pressure), which is almost flat near baseline.
On the 5 s model the test runs read 60.4 ± 0.04 °C for a seat opening at
60 °C: the TC leads the seat by about rate × lag, the same every run.

## 26. Thermocouple found again after it is unplugged — 22 Sept 2026
Unplugging and re-plugging the thermocouple while the driver ran left it
without a valve temperature until restarted. The MAX31856 was set up only at
connect, or after an SPI call raised — and a bit-banged SPI read never
raises: an unplugged chip reads all ones (fault 0xFF), and a re-powered one
reads its factory settings, which don't convert, so the temperature reads
0.000 °C with no fault bit (te-sensor_20260921_172933, after 17:30). Now
every read is one 16-register transfer that also reads back CR0/CR1; if
they aren't ours the reading is dropped, the loss logged once, and the chip
set up again as soon as it answers. A retry that finds nothing returns at
once instead of waiting 0.3 s, so `TC_RETRY_S` went from 5 s to 1 s. Fault
bits (e.g. the thermocouple wire unplugged: open circuit) are logged when
they appear and clear.

## 25. Temperature and pressure controllers redesigned from the 21 Sept 2026 runs — 22 Sept 2026
Data: te-sensor_20260921_150128 (0.25 N·m), _152054 (0.40), _173217 (0.45);
the thermocouple-fault logs _172024, _172612, _172933, _173050.

**auto-t.** The PI started every hold from a "hold power" of 15 % duty at
40 °C scaled linearly from 26 °C — about twice the measured hold power at
every temperature, and full power above ~120 °C. So every burst ended with a
5-8 K overshoot (90 → 94 °C; 155 → 159.5 °C, 0.5 K below the trip). Now the
duty is the measured hold power for the setpoint (`HEATER_HOLD_*`, 9 steady
holds, ±14 %) plus a PID trim (`PID_KD` 0 → 0.10). The burst is cut when
T + tau × rate reaches the setpoint (tau learned per coast) instead of a
fixed brake, which overshot small low-temperature steps by 3-4 K; the rate
already reflects the starting temperature. On three-node thermal models
fitted to each run (0.09-0.34 K rms), overshoot after bursts from 90 to
155 °C went from +3.4…+8.5 K to +0.2…+0.5 K. Near 40 °C, on the model of
the remounted TC (_173217), a session's first burst still overshoots ~4.5 K
until tau is learned (+1.6 K on the next).

**auto-p.** Everything is relative to the valve's *opening point* instead
of fixed temperatures near 40 °C: `SEAT_SCREW_VALVE` gives it (and the
flow e-fold) per torque — interpolated between entries, nearest entry
outside — shifted for upstream pressure (asymmetric limits), and the point
actually seen replaces the table for the session. Gains and creep scale
with √(e-fold / 3.2 K). The burst is decided by the distance below the
goal, not an absolute 35 °C. Once open, upstream-pressure changes are fed
forward on the setpoint. `PRESSURE_TSP_MAX_C` 140 → 155 °C (the 0.45 N·m
valve opens at ~150 °C). Simulated on valve models with the logs' shape
(throttling, soak, hysteresis 1-10 K, opening point ±8 K off the table):
all 48 cases at 0.25 and 0.40 N·m reached target (median 3.7-4.7 min,
overshoot ≤ +20 %); at 0.45 N·m every target the valve can give below
155 °C was reached, with no hunting.

**Thermocouple plausibility.** 17:20-17:31 the TC read nonsense with no
fault bit — falling 72 → -30 °C at full power for ~40 s, jumping 26 → 77 °C
— and the controller kept heating. New interlocks: a reading changing faster
than `TC_MAX_RATE_K_S`, or rising less than `TC_RESPONSE_MIN_K` in
`TC_RESPONSE_S` at full power (tests replay both logs).

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
