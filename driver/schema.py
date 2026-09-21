"""CSV log schema — the one place the column names are defined.

Imported by logfile.py (to write the logs) and by TE_PLOTTER.py (to read
them). Rules:
  * New columns are appended at the END, so older readers keep working.
  * Never rename or reorder a column; add a new one and leave the old one.
  * Units are in the name where they apply (_bar, _mbar, _degC, _V, _A, _W, _s).
This module imports nothing, so anything can import it.
"""

# Main log: te-sensor_<timestamp>.csv, one row every LOG_INTERVAL_S
MAIN_COLUMNS = (
    ("timestamp",               "ISO local time, ms"),
    ("keller_pressure_bar",     "upstream pressure, absolute, mean over the interval (blank if no samples)"),
    ("keller_temperature_degC", "Keller sensor-chip temperature, mean over the interval (not the gas temperature)"),
    ("n_keller_samples",        "number of Keller samples averaged"),
    ("vacuum_chamber_mbar",     "chamber pressure, latest reading (blank if invalid)"),
    ("te_temperature_degC",     "valve thermocouple, latest reading"),
    ("tc_fault",                "MAX31856 fault register (0 = OK)"),
    ("heater_duty",             "applied duty, 0-1"),
    ("heater_mode",             "off / manual / auto-t / auto-p (before 17 Sept 2026: auto / pressure)"),
    ("heater_setpoint_degC",    "temperature setpoint (auto: operator's; pressure: outer loop's)"),
    ("heater_V_mean_calc",      "V, duty x rail"),
    ("heater_I_mean_calc",      "A, duty x rail / R"),
    ("heater_V_mean_meas",      "V, measured, blank unless HEATER_V_AIN is set"),
    ("heater_I_mean_meas",      "A, measured, blank unless HEATER_I_AIN is set"),
    ("pressure_target_mbar",    "blank unless in pressure mode"),
    ("vacuum_status",           "blank = valid reading, else the gauge status"),
    ("heater_duty_cmd",         "operator's manual duty, 0-1 (blank unless manual)"),
    ("events",                  "event-log lines since the previous row, ' | '-joined, ASCII"),
    ("pressure_baseline_mbar",  "pressure mode: chamber baseline, frozen once the valve opens"),
    ("heater_P_mean_calc",      "W, duty x rail^2 / R (switching-period mean)"),
    ("heater_P_mean_meas",      "W, mean of per-tick V x I, blank unless sensing is wired"),
    ("heater_on_s",             "s, gate ON time since the previous row (from edge times)"),
    ("seat_screw_torque_Nm",    "N·m, TE-Valve seat screw torque as entered; blank = not recorded"),
)

# Switching log: te-sensor_<timestamp>_pwm.csv, one row per heater gate edge
PWM_COLUMNS = (
    ("timestamp", "ISO local time, ms, taken as the FIO0 write returns"),
    ("gate",      "1 = switched ON, 0 = switched OFF, blank = write failed"),
    ("duty",      "applied duty when the edge happened, 0-1"),
    ("on_s",      "OFF rows: how long the gate had been ON, s"),
    ("note",      "blank for normal switching; otherwise the reason"),
)

MAIN = tuple(name for name, _ in MAIN_COLUMNS)
PWM = tuple(name for name, _ in PWM_COLUMNS)
PWM_SUFFIX = "_pwm.csv"
