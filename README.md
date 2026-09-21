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

### The window

One "Live Log" window: device status, live readings, heater voltage /
current / power, the heater controls, the seat screw torque input, four strip
charts (chamber pressure, valve temperature, upstream pressure, heater power
with the 1 W flight budget dashed) and a scrolling event log.

### Heater modes

| Mode | What it does |
|---|---|
| **manual** | A fixed duty. |
| **auto-t** | A PI loop holds a valve temperature setpoint. From a cool start it bursts at full power, coasts with the heater off to the peak, then hands over to the PI. |
| **auto-p** | A cascade holding a chamber pressure target. It *seeks* first: burst, coast, then a setpoint of `PRESSURE_SEEK_START_C` creeping up at `PRESSURE_SEEK_RATE_C_MIN` with the valve shut, measuring the chamber baseline. When the pressure snaps up (the valve has opened) it *tracks* the target with a PI on log10(pressure), moving the auto-t setpoint. The baseline also shows which targets the valve can't hold. |

The heater can only add heat: auto-p can't cool the valve, so a target that
would need that gets a warning and the minimum setpoint.

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
- **auto-p adds:** no valid gauge reading for `PRESSURE_BAD_READS_TO_TRIP`
  reads, gauge over range or LabJack input saturated, or chamber pressure
  above `PRESSURE_TRIP_MBAR` (5e-4 mbar). Its temperature setpoint is limited
  to `PRESSURE_TSP_MIN_C` … `PRESSURE_TSP_MAX_C`.
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
```

Requirements: `py -m pip install -r requirements.txt`

The plotter draws the supporting traces (upstream pressure, heater power) on
top and chamber pressure with valve temperature below, marks valve openings
and closings, puts the seat screw torque in the title (with a dotted line at
each change), and prints fits and a summary to the terminal.

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
`gui` ← `app` (with `palette` ← `charts` alongside). `controller` imports
nothing but `config` and pure standard modules; `test_architecture.py`
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
