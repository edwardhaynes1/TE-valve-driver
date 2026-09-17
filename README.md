# TE-Valve driver

Live display, logging and heater control for the TE-Valve test setup, plus
a plotter for the logs. Terms used here are defined in [CONTEXT.md](CONTEXT.md);
the reasons behind the design are in [docs/decisions.md](docs/decisions.md).

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
tevalve/
  config.py           every tunable number: wiring, calibration, limits, tuning
  controller.py       heater control law and interlocks: state, time and readings
                      in; duty and messages out. No threads, clock, files or hardware
  control.py          thread-safe wrapper the device thread and GUI call
  devices.py          Keller, LabJack, thermocouple, heater gate, their threads
  logfile.py          the two CSV logs
  schema.py           CSV column names (shared with the plotter)
  shared.py           state shared between threads, event log
  gui.py              the Live Log window
  app.py              start-up
tests/                pytest suite, scenarios and golden record
```

Dependencies only point one way: `config` ← `controller` ← `shared` ←
`control` ← `devices`, `logfile` ← `gui` ← `app`. `controller` imports
nothing but `config` and pure standard modules; `test_architecture.py`
enforces this.

## Test

```
pip install pytest
python -m pytest
```

Run the tests before every commit. They take about 10 seconds and need
no hardware.

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
- **`test_logfile.py`, `test_devices.py`, `test_plotter.py`** cover the logs,
  the gauge conversion, pin checks, and that the plotter reads what the
  driver writes.

## Changing things

- **Tuning or control changes.** Change `tevalve/config.py` or
  `tevalve/controller.py`, then run `python tests/record_golden.py`. It lists
  which scenarios changed, where, and whether any trip changed. If every
  change is one you meant, save with `--write` and commit the new golden
  file together with the change.
- **New log column.** Append it to `MAIN_COLUMNS` in `tevalve/schema.py`,
  fill it in `logfile.py`. Never rename or reorder existing columns.
- **New term.** Add it to CONTEXT.md.
- **A choice someone might question later.** Add a line to docs/decisions.md.
