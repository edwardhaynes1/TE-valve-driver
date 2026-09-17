#!/usr/bin/env python3
"""TE-VALVE-DRIVER — live display, logging and heater control for the
TE-Valve test setup.

Run:   python TE-VALVE-DRIVER.py      (or double-click)

The code lives in the driver/ package:
    config.py    every tunable number (wiring, calibration, limits, tuning)
    controller.py  heater control law and interlocks (no threads or hardware)
    control.py   thread-safe wrapper around it
    devices.py   Keller, LabJack, thermocouple, heater gate
    logfile.py   CSV logs (main + PWM switching)
    schema.py    CSV column names, shared with TE_PLOTTER.py
    gui.py       the Live Log window
    app.py       start-up
See README.md and prog-documentation/context.md.
"""
from driver.app import run

if __name__ == "__main__":
    run()
