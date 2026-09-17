# TE-Valve driver

Live display, logging and heater control for the TE-Valve test setup, plus
a plotter for the logs. Terms used here are defined in [prog-documentation/context.md](prog-documentation/context.md);
the reasons behind the design are in [prog-documentation/software-history-log.md](prog-documentation/software-history-log.md).

## Run

```
python TE-VALVE-DRIVER.py          # the driver (or double-click it)
python TE_PLOTTER.py               # plot a log (file picker)
python TE_PLOTTER.py logs/te-sensor_<time>.csv --no-show
```

Logs go to `logs/`: a main log every 0.5 s and a `_pwm.csv` switching log.

Requirements: `pip install -r requirements.txt`

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
  gui.py              the Live Log window
  app.py              start-up
tests/                pytest suite, scenarios and golden record
```

Dependencies only point one way: `config` ← `controller`, `shared` ←
`control`, `thermocouple` ← `keller`, `labjack`, `logfile` ← `gui` ← `app`. `controller` imports
nothing but `config` and pure standard modules; `test_architecture.py`
enforces this, and that no module reaches into another's private names.

## Test

```
pip install pytest
python -m pytest
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
- **`test_logfile.py`, `test_labjack_helpers.py`, `test_plotter.py`** cover the logs,
  the gauge conversion, pin checks, and that the plotter reads what the
  driver writes.

## Changing things

- **Tuning or control changes.** Change `driver/config.py` or
  `driver/controller.py`, then run `python tests/record_golden.py`. It lists
  which scenarios changed, where, and whether any trip changed. If every
  change is one you meant, save with `--write` and commit the new golden
  file together with the change.
- **New log column.** Append it to `MAIN_COLUMNS` in `driver/schema.py`,
  fill it in `logfile.py`. Never rename or reorder existing columns.
- **New term.** Add it to prog-documentation/context.md.
- **A choice someone might question later.** Add a line to prog-documentation/software-history-log.md.
