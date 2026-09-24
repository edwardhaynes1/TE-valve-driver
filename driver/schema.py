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
    ("batch_run",               "scout1 / scout2 / testrun01…, blank outside a batch"),
    ("batch_phase",             "baseline / approach / creep / cooldown, blank outside a batch"),
)

# Switching log: te-sensor_<timestamp>_pwm.csv, one row per heater gate edge
PWM_COLUMNS = (
    ("timestamp", "ISO local time, ms, taken as the FIO0 write returns"),
    ("gate",      "1 = switched ON, 0 = switched OFF, blank = write failed"),
    ("duty",      "applied duty when the edge happened, 0-1"),
    ("on_s",      "OFF rows: how long the gate had been ON, s"),
    ("note",      "blank for normal switching; otherwise the reason"),
)

# Batch summaries: <batch folder>/summary.csv and the opening map's Runs
# sheet (one row per run), and the Batches sheet (one row per batch).
RUN_SUMMARY_COLUMNS = (
    ("batch",                        "batch name (its folder)"),
    ("run",                          "scout1 / scout2 / testrun01…"),
    ("in_average",                   "1 = a test run that opened (averaged), else 0"),
    ("status",                       "opened / no opening / aborted"),
    ("start_time",                   "ISO local time the run started"),
    ("seat_screw_torque_Nm",         "N·m, as entered"),
    ("start_degC",                   "creep start temperature (scout 1: heats towards the ceiling)"),
    ("creep_degC_per_min",           "creep rate (blank for scout 1)"),
    ("opened_during",                "approach / creep"),
    ("onset_time",                   "ISO local time of the onset (ms)"),
    ("detect_time",                  "ISO local time of detection (ms)"),
    ("t_open_degC",                  "valve temperature at the onset (backdated): the opening point"),
    ("t_detect_degC",                "valve temperature at detection"),
    ("open_to_detect_s",             "s from onset to detection"),
    ("chamber_baseline_mbar",        "chamber baseline before opening"),
    ("chamber_at_open_mbar",         "chamber pressure at the onset"),
    ("chamber_at_detect_mbar",       "chamber pressure at detection"),
    ("dlog10p_dT_dec_per_K",         "rise of log10(chamber pressure) per K, onset to detection"),
    ("efold_K",                      "K per e-fold of the rise above baseline, fitted onset to detection (blank if too few points)"),
    ("energy_at_open_J",             "heater energy from the approach start to the onset, J"),
    ("energy_at_detect_J",           "…to detection, J"),
    ("heater_power_at_open_W",       "heater power (period mean, calc) at the onset"),
    ("upstream_at_open_bar",         "upstream pressure at the onset (measured)"),
    ("upstream_at_detect_bar",       "upstream pressure at detection (measured)"),
    ("free_cooling_closed_degC",     "valve temperature where the chamber fell back below the opening threshold (indicative)"),
    ("free_cooling_closed_s",        "s after detection when it did"),
    ("cooldown_s",                   "s from detection (or failure) to the end of cooldown"),
    ("note",                         "why a run failed or was cut short"),
    ("file",                         "the run's trace, relative to the batch folder"),
)

BATCH_SUMMARY_COLUMNS = (
    ("batch",                        "batch name (its folder)"),
    ("start_time",                   "ISO local time"),
    ("end_time",                     "ISO local time"),
    ("status",                       "complete / stopped / aborted"),
    ("note",                         "why it stopped or was aborted"),
    ("seat_screw_torque_Nm",         "N·m, as entered"),
    ("test_runs_requested",          "N"),
    ("test_runs_opened",             "n: test runs in the averages"),
    ("scout1_t_open_degC",           "rough opening point"),
    ("scout2_t_open_degC",           "fine opening point; test runs start 5 K below it"),
    ("creep_degC_per_min",           "creep rate"),
    ("t_open_mean_degC",             "mean T_open of the averaged test runs"),
    ("t_open_std_K",                 "sample standard deviation (blank if n < 2)"),
    ("t_open_min_degC",              ""),
    ("t_open_max_degC",              ""),
    ("t_detect_mean_degC",           ""),
    ("open_to_detect_mean_s",        ""),
    ("efold_mean_K",                 "mean of the runs' e-folds that could be measured"),
    ("energy_at_open_mean_J",        ""),
    ("energy_at_open_std_J",         ""),
    ("upstream_at_open_mean_bar",    "measured, averaged test runs"),
    ("upstream_at_open_min_bar",     ""),
    ("upstream_at_open_max_bar",     ""),
    ("chamber_baseline_mean_mbar",   ""),
    ("free_cooling_closed_mean_degC", "indicative"),
    ("folder",                       "the batch folder"),
)

MAIN = tuple(name for name, _ in MAIN_COLUMNS)
PWM = tuple(name for name, _ in PWM_COLUMNS)
RUN_SUMMARY = tuple(name for name, _ in RUN_SUMMARY_COLUMNS)
BATCH_SUMMARY = tuple(name for name, _ in BATCH_SUMMARY_COLUMNS)
PWM_SUFFIX = "_pwm.csv"
