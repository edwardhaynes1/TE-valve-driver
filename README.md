# TE-Valve driver

Live display, logging and heater control for the TE-Valve (Thermally
Enabled Valve) test setup, plus a plotter for the logs.

Terms are defined in [prog-documentation/context.md](prog-documentation/context.md);
the reasons behind the design are in
[prog-documentation/software-history-log.md](prog-documentation/software-history-log.md).

## What it does

The driver reads three sensors, controls the valve heater, and logs
everything:

| Reading | Sensor | Connection |
|---|---|---|
| Upstream pressure (and the sensor's chip temperature) | Keller PAA-23SX-H2 | RS485 via the K-114 USB adapter |
| Chamber pressure | Pfeiffer IKR 270 cold-cathode gauge | LabJack U3, FIO2, through a voltage divider |
| Valve temperature | Type-K thermocouple on a MAX31856 | LabJack U3, FIO4–7 (SPI) |

Each device has its own thread. If one disconnects mid-session, its reading
shows `---` straight away (never a stale value) and the thread looks for the
device again; the rest carries on unaffected.

The **seat screw torque** (N·m) is entered by hand in the window. It starts
blank every session and is logged with every row from the moment it is set.
The **lock nut torque** (N·m) goes on the line below it in the same way, but
is optional: blank means not recorded, and it locks nothing. Once a torque
is entered its line shows the value and a small **update** button, which
puts the box back in its place to enter a new value (Return or **set**
saves, Escape keeps the old one). Both are locked while t-min-tune runs.
Results pool only at the same seat screw *and* lock nut torque (blank
counts as its own value), and a lock nut torque different from the latest
seating's starts a new seating without the re-torque question.

### The window

One "Live Log" window: device status, live readings, heater voltage /
current / power, the heater controls, the seat screw and lock nut torque inputs, four strip
charts (chamber pressure, valve temperature, upstream pressure, heater power
with the 1 W flight budget dashed) and a scrolling event log.

### Heater modes

| Mode | What it does |
|---|---|
| **manual** | A fixed duty. |
| **auto-t** | Holds a valve temperature setpoint: the measured hold power for that temperature (`HEATER_HOLD_*`), plus a PID that only trims. For a step up of `TEMP_BURST_MIN_STEP_K` or more it bursts at full power, cuts when the TC is predicted to coast to just short of the setpoint (T + tau × rate of rise ≥ setpoint − max(1.5 K, 15 % of the step), tau learned from every coast), and hands over at the peak; the PID lands it within 0.5 °C (history 35). |
| **auto-p** | A cascade holding a chamber pressure target, set relative to the valve's *opening point*: from the seat screw torque (`SEAT_SCREW_VALVE`), shifted for upstream pressure, and replaced by the point actually seen once the valve opens. It *seeks* first: burst (if well below), coast, then a setpoint creeping up with the valve shut, measuring the chamber baseline. When the pressure rises (the valve has opened) it *tracks* the target with a PI on log10(pressure), moving the auto-t setpoint; the gains and creep scale with the torque's e-fold, and upstream pressure changes are fed forward. The baseline also shows which targets the valve can't hold. |

The heater can only add heat: auto-p can't cool the valve, so a target that
would need that gets a warning and the minimum setpoint.

### t-min-tune: the lowest opening temperature

To find the lowest valve temperature at which the valve opens (T_min) for a
seat screw torque and an upstream pressure: set the torque, enter it,
choose **t-min-tune** (the fourth mode), enter the **upstream target** (bar)
and its **±** band (default 0.05 bar) and press **start t-min**. If the
torque has been measured before it asks **has the seat screw been
re-torqued (or the valve disturbed) since?** *No* continues that seating;
*yes* starts a new one. The optional **estimate °C** box sets the first
estimate of a seating with no result yet (e.g. after changing the lock nut):
its first test starts 10 K below it, instead of from the other seatings or
a scout; it is cleared once used. Hold the upstream inside the band by topping up; the
upstream chart is taller in this mode, with the band dotted and the trace
amber outside it. Then, test after test:

1. hold a start temperature below the estimate until the chamber is
   settled — 10 K below while the seating has no result, 5 K with one, then
   2 K below the lowest of its latest 3 results (kept 3-10 K below the
   estimate);
2. step the setpoint up 1 K every 5 min (from when the TC is within 0.5 K
   of it) until the valve opens: that step is T_min (the TC at the onset);
3. heater off; wait until the valve has closed — the chamber within +5 %
   (0.02 decades) of its baseline before the opening and settled (< 0.01
   decades/min); the TC then is T_close;
4. again, from the new estimate.

The upstream leaving the band during the hold or a step cuts the heater and
abandons the test; the next starts afresh once it is back inside. An
opening during the hold (the start was too high) moves the next start 5 K
lower. The estimate is this seating's latest results (corrected to the
target along the opening map's pressure slope), else your estimate, else
the other seatings at this torque and lock nut torque (each one's latest 3
results), else the opening map; with nothing at all, the first test
scouts (3 °C/min from 10 K below the torque table). It stops when the last
3 results lie within ±1 K of their mean, or when you press **stop t-min**
(or DISARM). When it converges — the valve has closed by then and the
heater is disarmed — the driver closes itself 60 s later, saving its logs as
when you close the window; starting t-min-tune again within that minute
cancels it (`TMIN_QUIT_WHEN_CONVERGED`, `TMIN_QUIT_DELAY_S`).

Every test is a row of **`logs/t-min.csv`**: time, seating, torque, target
and band, outcome (t_min, opened at start, aborted: out of band, opened out
of band, no opening, scout, stopped), start, step, T_min (and corrected to
the target), T_detect, T_close, upstream at the opening, in band, baseline,
the estimate and margin it started from, counted, converged, the path of
its trace, and — on the row that converged — the converged T_min
(`t_min_converged_degC`, the mean of the last 3; filled in for rows written
before it existed, the next time t-min-tune starts or a row is added). The estimates are recomputed from that file, so it is the one
record of what has been learned; the code only appends to it, and `logs/`
is not in git, so no commit or update touches it. A copy goes to the
*T_min* sheet of the opening-map workbook. Traces:
`logs/t-min/<seating>/<session>/test001.csv` … and `session.json`.
Settings: `TMIN_*` in `driver/config.py`.

### Cycling (history 34-35)

Replaced in the window by t-min-tune; `driver/cycle.py` and `cyclerun.py`
remain (with their tests), and its openings stay in `logs/openings.csv`,
which the first estimate at a new torque can come from.

### Batches (until history 34)

Replaced by cycling; `driver/batch.py` and `batchrun.py` remain, and their
folders are imported into the openings table. What they did:

To measure the valve's *opening point* (the valve temperature where flow
starts) with a mean and spread, set the seat screw torque and the upstream
pressure by hand, enter the torque, choose the most **test runs** it may do
(default 5), and press **start batch**. It shows the measured upstream pressure and asks you
to confirm the torque, or, if an opening point is remembered for it, **has
the seat screw been re-torqued (or the valve disturbed) since?** *No* skips
the scouts. Then it runs by itself, and stops as soon as the mean T_open is
known to **±1 K** (95 %, at least 3 test runs).

Every run starts cold: heater off until the valve is at the **hold
temperature** — 35 °C, or 20 K below the opening point if lower, or, near
room temperature, wherever it stops cooling faster than 1 K/min once it is
at least 5 K below the opening point (**adaptive cooling**, for low
opening points such as 0.25 N·m) — and the chamber is back at its baseline;
then auto-t **holds** it for 4 min, so every test run opens from the same
thermal state (scouts aren't averaged, so they don't hold). The first test run fixes the hold temperature for the batch,
and it is remembered with the result, so the next batch at that torque
starts from the same state; then it heats to the
run's start and **creeps** at 3 °C/min until the valve opens. The approach
waits until the chamber is neither rising nor falling faster than ~2 %/min
(a fill's jump or outgassing would look like an opening; a falling chamber
hides it). There is no pause for the leak: refill upstream by hand when you
like, best while it cools or holds (the approach waits for the chamber to
settle). A refill while heating is seen on the Keller (upstream up
≥ 0.05 bar): that run is discarded and repeated (`testrun02b`).

| Run | What it does |
|---|---|
| **scout 1** | Heats to 10 K below the torque table's guess (auto-p's opening point, shifted for upstream) and creeps. If it opens while creeping, the test runs follow directly. If it hasn't opened 20 K above the guess, it heats fast to 155 °C instead (and reads high). Skipped with a remembered opening point. |
| **scout 2** | Only if scout 1 opened while approaching or after heating fast: creeps from 10 K below scout 1's reading. If it opens before it could creep, it repeats 10 K lower (`scout2b`, …). |
| **test runs 1 … N** | Creep from a margin below the best estimate so far (3 × the scatter + 0.5 K, 2-5 K). Only these are averaged. One that opens while still approaching means the valve moved: it isn't averaged, and a scout 2 finds the opening point again. |

The opening is detected when the chamber rises 0.5 × 10⁻⁷ mbar above its
baseline (a fixed flow, at least 4.7 %), and **T_open** is backdated to where the
rise began; the valve temperature at detection is logged too, with the
heater energy since the approach started and the upstream pressure. The
heater is **disarmed at detection**. At the end the result becomes the
**remembered opening point** for the torque (`logs/opening-points.json`) —
after ≥ 3 test runs, or if it shows the valve moved — which auto-p also
uses instead of the `SEAT_SCREW_VALVE` guesses.

The rig leaks, so every run opens at a different upstream pressure: each
batch fits T_open against upstream pressure (K/bar), and judges the scatter
after correcting for it. A batch stops by itself if a scout doesn't open by
155 °C, if two test runs in a row don't, if the valve opens while warming to
the hold temperature, if the opening point is less than 5 K above the hold
temperature (too close to room temperature to start cold), or if a cooldown
or the chamber settling takes over 30 min. Start is refused without a valve temperature or chamber reading;
without an upstream reading it starts, but can't correct for upstream or
see a refill (the start dialog says so). **abort batch**, DISARM, any
trip, the chamber gauge failing while heating, or closing the driver ends
it at once. While it runs, the heater controls and the torque are locked.

Results: a folder per batch in `logs/batches/` (each run's trace, a
`summary.csv`, `batch.json` with the settings and results, and `batch.png`),
and a row per run and per batch in `logs/TE-valve-opening-map.xlsx`. If the
workbook is open in Excel, rows wait in a side file and are moved in next
time. Settings: `BATCH_*` in `driver/config.py` (hold depth and time, margin,
precision, detection). Terms: [context.md](prog-documentation/context.md),
"Batches".

### The opening map: every opening, one fit

Every opening that crept onto the valve goes into one table,
`logs/openings.csv` (copied to the *Openings* sheet of the opening map), and
one fit over all of it is redone after every opening:

    T_open = offset (per seating) + slope (per torque) × (upstream − 3 bar)

A *seating* is one tightening of the seat screw (a batch, until cycling
replaces batches), so a re-torque to the same value may sit higher or lower;
all seatings at one torque share the pressure slope. A torque whose openings
span under 0.3 bar of upstream within one seating uses −12 K/bar (*assumed*):
the offsets absorb pressure differences *between* seatings, so the slope
comes from changing the pressure *without* re-torquing. The fit also
tries a warm-start term (deep against shallow cycles), drift in time and the
chamber background, keeps each only if significant at 95 %, and says when
two can't be told apart (28 Sept: time and a background pumping down moved
together). After each opening the event log gets the seating's status line —
T_open ± at 3 bar, the slope ±, the pressure range, and **DONE** (±1 K, ±2
K/bar) or where to take the pressure next — and `logs/opening-map.png` is
redrawn: T_open against upstream per seating, the map against torque, and
the residuals over time — and `logs/opening-map-3d.png`, torque × upstream ×
T_open in 3D. Old batch folders are imported at start-up, once each. Draw
them by hand with `py TE_PLOTTER.py --map` (add `--3d` to rotate the 3D
one). Settings: `MAP_*` in
`driver/config.py`.

Detection is on a fixed flow: the chamber 0.5 × 10⁻⁷ mbar above its
baseline (at least 4.7 %), so a background still pumping down doesn't move
what counts as open.

### Heater voltage, current and power

By default these are *calculated* from the gate state, the 24 V rail and the
88 Ω element, which assumes SW171 is on and 24 V is present; the software
can't see either. Wire a supply divider and/or a current-sense amplifier to
a spare analogue input, set `HEATER_V_AIN` / `HEATER_I_AIN` in
`driver/config.py`, and the *measured* values are shown and logged too, with
a check of current against the gate state.

Power is the mean over one switching period: duty × V²/R. The heater is
fully on or off, so mean V × mean I would understate it.

## Hardware

```
Keller PAA-23SX-H2   RS485 / USB (K-114)   upstream pressure + chip temperature
LabJack U3           FIO0     heater gate drive (digital out)
                     FIO2     vacuum gauge (analogue in)
                     FIO4-7   MAX31856 thermocouple (SPI):
                              FIO4 SDO · FIO5 SDI · FIO6 SCK · FIO7 !CS
```

Heater chain (Ariel control PCB schematic): FIO0 → R171 (270 Ω) → gate of
Q171 (DMN3150L-7, N-channel MOSFET). The element sits on the constant 24 V
rail and Q171 switches its return. SW171 is a physical enable in series,
which software cannot override, deliberately. Element ≈ 88 Ω: 0.27 A and
6.5 W at full duty.

### Why the heater is switched in software, not by hardware PWM

On U3 hardware revision 1.30 and later (every U3-HV), timers can't be
assigned to FIO0–3: the timer pin offset must be 4–8, and a lower value is
either refused or silently moved to FIO4, which is the MAX31856's SDO line.
So FIO0 is switched as a plain digital output on a 2 s period
(`HEATER_PWM_PERIOD_S`) in 50 ms steps. The valve's thermal time constant is
tens of seconds to minutes, so the switching is invisible thermally, and the
SPI lines stay free.

## Safety

- The heater always starts **disarmed**, and the gate is forced low on
  connecting and on exit.
- **LabJack watchdog:** if the program stops talking to the U3 for
  `LJ_WATCHDOG_S` (10 s), the U3 itself drives FIO0 low. This covers a
  crashed or killed program.
- **Interlocks** (any one turns the heater off): thermocouple missing or
  faulted, valve temperature above `TEMP_TRIP_C` (160 °C), armed longer than
  `HEATER_MAX_RUN_S` (60 min), or current flowing with the gate off (only
  with current sensing wired).
- **Thermocouple plausibility** (added after the 21 Sept 2026 17:20 fault,
  when a bad reading raised no fault bit): a reading changing faster than
  `TC_MAX_RATE_K_S`, or one that rises less than `TC_RESPONSE_MIN_K` in
  `TC_RESPONSE_S` at full power (a dead TC, or SW171 off). Set
  `TC_RESPONSE_S = None` where full power can't reach the setpoint (a cold
  test), since a TC levelling off at full power looks the same.
- **auto-p adds:** no valid gauge reading for `PRESSURE_BAD_READS_TO_TRIP`
  reads, gauge over range or LabJack input saturated, or chamber pressure
  above `PRESSURE_TRIP_MBAR` (5e-4 mbar). Its temperature setpoint is limited
  to `PRESSURE_TSP_MIN_C` … `PRESSURE_TSP_MAX_C`.
- **Thermocouple unplugged and plugged back in** while running: the driver
  notices within one read (every read also checks the MAX31856's settings,
  since an unplugged chip reads all ones and a re-powered one reads 0 °C
  with no fault), shows the valve temperature as unavailable, and sets the
  chip up again within `TC_RETRY_S` (1 s) of it answering. A heater that
  tripped meanwhile stays off until re-armed.
- **A batch** arms the heater itself, once per run (so the 60 min limit
  applies per run, hold included), and disarms it the moment the valve opens. While heating
  it also stops on no valid gauge reading for 2 s or chamber pressure above
  5e-4 mbar; DISARM or a trip aborts it.
- A **trip latches**: press DISARM, then ARM, to clear it.
- Disarming takes effect at the next control step, within 0.25 s.

## Logs

Logs go to `logs/`, named by start time:

- **Main log** `te-sensor_<time>.csv`: one row every 0.5 s (`LOG_INTERVAL_S`)
  on a drift-free clock, the first at start and the last on exit. The columns
  and their units are listed in `driver/schema.py`, the one definition the
  driver writes with and the plotter reads with. New columns are only ever
  added at the end. Every event-log line (mode, duty, setpoint or target
  changes, arming, trips, gauge status, reconnects, the seat screw torque)
  also goes in the `events` column of the next row, so the CSV is a complete
  record.
- **Switching log** `te-sensor_<time>_pwm.csv`: one row per heater gate
  switch, with its time, the duty, and for each switch-off how long the gate
  was on. It records what the software commanded, not a measured voltage.
  The main log's `heater_on_s` comes from the same switch times, so energy
  per row is exactly `heater_on_s` × V²/R.

A blank cell means *not recorded* (for example a sensor missing, or the seat
screw torque not yet entered), never zero.

## Run

```
py TE-VALVE-DRIVER.py              # the driver (or double-click it)
py TE_PLOTTER.py                   # plot a log (file picker)
py TE_PLOTTER.py logs/te-sensor_<time>.csv --no-show
py TE_PLOTTER.py logs/batches/<batch folder>        # a batch (or pick its summary.csv)
```

Requirements: `py -m pip install -r requirements.txt`

The plotter draws the supporting traces (upstream pressure, heater power) on
top and chamber pressure with valve temperature below, marks valve openings
and closings, puts the seat screw and lock nut torques in the title (with a
dotted line at each change), and prints fits and a summary to the terminal.

## Layout

```
TE-VALVE-DRIVER.py    launcher (keep double-clicking this)
TE_PLOTTER.py         log plotter
driver/
  config.py           every tunable number: wiring, calibration, limits, tuning
  controller.py       heater control law and interlocks: state, time and readings
                      in; duty and messages out. No threads, clock, files or hardware
  control.py          owns the heater state; the only way to command or read it
  keller.py           Keller upstream pressure sensor and its thread
  labjack.py          LabJack thread: heater gate, vacuum gauge, sense inputs;
                      each tick is a few named steps (sense, read, control, drive)
  thermocouple.py     MAX31856 thermocouple chip (SPI)
  batch.py            batch sequence: phases, detection, onset, summaries. Like
                      controller.py: no threads, clock, files or hardware
  batchrun.py         runs a batch: its thread, heater commands and files
  openings.py         remembered opening points (logs/opening-points.json)
  workbook.py         the opening map (Excel), with a side file if it's locked
  logfile.py          the two CSV logs
  schema.py           CSV column names (shared with the plotter)
  shared.py           readings, charts, event log, health flags — through functions only
  gui.py              the Live Log window: widgets, input handling, polling
  readout.py          what the window says, as pure text functions (tested)
  charts.py           strip charts on a Tk canvas
  palette.py          the GUI's colours
  app.py              start-up
tests/                pytest suite, scenarios and golden record
```

Dependencies only point one way: `config` ← `controller`, `shared` ←
`control`, `thermocouple` ← `keller`, `labjack`, `logfile`, `readout` ←
`gui` ← `app` (with `palette` ← `charts` alongside; `batch` ← `batchrun`
← `logfile`, `gui`, with `workbook` alongside; `openings` ← `control`,
`batchrun`, `gui`, `app`). `controller` imports nothing but `config`, and
`batch` nothing but `config` and `controller`'s mode names, plus pure
standard modules; `test_architecture.py`
enforces this, and that no module reaches into another's private names.

## Test

```
py -m pip install pytest
py -m pytest
```

Run the tests before every commit. They take about half a minute and
need no hardware. GitHub also runs them on Windows after every push
(`.github/workflows/tests.yml`): see the repository's **Actions** tab, and
GitHub emails you if a run fails.

- **`test_control_equivalence.py`** replays 15 scripted scenarios (manual,
  bursts, seek/track, retargeting, mode changes, every interlock) against a
  simple plant and a fake clock, and requires the result to match the
  golden record step for step, both through `control.py` and through
  `controller.py` alone.
- **`test_controller.py`** states the interlock and command rules as short
  examples. Start here when changing the controller: write the new rule as
  a failing example first.
- **`test_device_thread.py`** runs the real LabJack thread against a fake
  U3 (`fake_u3.py`): duty becomes gate switching, disarm and trips drop the
  gate, a write error reconnects with the gate low, shutdown leaves it low.
- **`test_device_faults.py`** covers the thread's error and sensing paths:
  a lost vacuum reading, gauge range changes, thermocouple faults and
  recovery, measured heater voltage and current (including the no-current
  warning and the stray-current trip), and a failed force-low at shutdown.
- **`test_readout.py`** checks every line the window displays — readings,
  faults, the armed/tripped line and the loop's phases — without opening a
  window.
- **`test_shared.py`** covers the shared readings, the heater output record,
  health flags and the hand-over to the logger.
- **`test_logfile.py`, `test_labjack_helpers.py`, `test_plotter.py`** cover the logs,
  the gauge conversion, pin checks, and that the plotter reads what the
  driver writes.

## Changing things

- **Tuning or control changes.** Change `driver/config.py` or
  `driver/controller.py`, then run `py tests/record_golden.py`. It lists
  which scenarios changed, where, and whether any trip changed. If every
  change is one you meant, save with `--write` and commit the new golden
  file together with the change.
- **New log column.** Append it to `MAIN_COLUMNS` in `driver/schema.py`,
  fill it in `logfile.py`. Never rename or reorder existing columns.
- **New term.** Add it to prog-documentation/context.md.
- **A choice someone might question later.** Add a line to prog-documentation/software-history-log.md.
