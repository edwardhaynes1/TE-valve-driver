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
    ("batch_run",               "test001… in t-min-tune (from history 36); cycle001… while cycling (history 34-35); scout1 / scout2 / testrun01… in a batch; blank otherwise"),
    ("batch_phase",             "t-min-tune: wait / hold / step / scout / cool; cycling and batches: cooldown / hold / approach / creep (top-up: until history 33; settle: 24 Sept 2026 only); blank otherwise"),
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
    ("in_average",                   "1 = a test run that opened while creeping (averaged), else 0"),
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
    ("reference_degC",               "test runs: the reference T_open, shifted to the upstream pressure at the start"),
    ("margin_K",                     "test runs: how far below the reference it started"),
    ("hold_degC",                    "the hold temperature before the approach"),
    ("hold_s",                       "s held (within BATCH_HOLD_BAND_K) before the approach"),
    ("hold_gap_K",                   "T_open − the hold temperature: how cold a start it was"),
    ("cooldown_end",                 "target / slowed (adaptive cooling) / fixed (the batch's hold temperature)"),
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
    ("t_open_vs_upstream_K_per_bar", "slope of a straight-line fit of T_open against upstream pressure (n ≥ 3, spread ≥ 0.05 bar)"),
    ("t_open_vs_upstream_se_K_per_bar", "its standard error"),
    ("t_open_resid_std_K",           "scatter of T_open about that fit: the scatter the leak doesn't explain"),
    ("top_ups",                      "how many times the batch paused for a top-up (blank since history 33: no pauses)"),
    ("free_cooling_closed_mean_degC", "indicative"),
    ("folder",                       "the batch folder"),
    ("started_from",                 "scouts, or the remembered opening point (and its batch)"),
    ("t_open_ci95_K",                "± of the mean T_open, 95 % (Student t × scatter ÷ √n): the stopping rule"),
    ("t_open_corrected_std_K",       "scatter of T_open corrected to the mean upstream pressure"),
    ("slope_used_K_per_bar",         "K/bar used for that correction"),
    ("slope_from",                   "batch fit / remembered / default"),
    ("find_agains",                  "test runs that opened while still approaching (the valve had moved)"),
    ("hold_degC",                    "hold temperature of the averaged test runs"),
    ("hold_s",                       "BATCH_HOLD_S"),
    ("detect_abs_mbar",              "BATCH_DETECT_ABS_MBAR (blank = off)"),
    ("detect_rel_dec",               "BATCH_DETECT_REL_DEC (blank = off, from history 34)"),
    ("remembered_after",             "yes = this result is now the remembered opening point"),
    ("hold_gap_min_K",               "the smallest T_open − hold temperature of the averaged test runs"),
)

# The openings table: logs/openings.csv and the opening map's Openings sheet,
# a row per opening (history 34). openmap.py fits over it.
OPENINGS_COLUMNS = (
    ("time",                  "ISO local time of the onset"),
    ("seating",               "one seating of the valve: from tightening the seat screw until it is re-torqued; <date>_<time>_<torque>Nm, or the batch name for an imported batch (column 'setting' until 1 Oct 2026)"),
    ("torque_Nm",             "N·m, as entered"),
    ("upstream_bar",          "upstream pressure at the onset (measured)"),
    ("t_open_degC",           "valve temperature at the onset: the opening point"),
    ("t_detect_degC",         "valve temperature at detection"),
    ("baseline_mbar",         "chamber background before it opened"),
    ("closed_degC",           "valve temperature where the chamber fell back after it (the valve closed)"),
    ("bottom_degC",           "the temperature the cycle cooled to (held at) before its creep"),
    ("bottom_s",              "s it was held there"),
    ("deep",                  "1 = a deep (cold) cycle, 0 = shallow"),
    ("refills",               "refills while heating before it opened"),
    ("detect_rule",           "the detection rule that found it"),
    ("creep_degC_per_min",    "creep rate"),
    ("source",                "where it came from: the cycle's file, or 'batch <folder>/<run>'"),
    ("note",                  ""),
)

# The t-min-tune results: logs/t-min.csv, a row per test (history 36). New
# columns are only ever added at the end; old files gain them when written.
TMIN_COLUMNS = (
    ("time",                  "ISO local time the test ended (the opening's onset, if it opened)"),
    ("seating",               "one seating of the valve: from tightening the seat screw until it is re-torqued; <date>_<time>_<torque>Nm (column 'setting' until 1 Oct 2026)"),
    ("torque_Nm",             "seat screw torque M_A, N·m, as entered"),
    ("upstream_target_bar",   "the upstream pressure the operator held (bar abs)"),
    ("band_bar",              "± bar around the target"),
    ("outcome",               "t_min / opened at start / aborted: out of band / opened out of band / "
                              "no opening / scout / stopped"),
    ("start_degC",            "the temperature held before stepping"),
    ("step_degC",             "the last step's setpoint (where it opened, for t_min)"),
    ("t_min_degC",            "TC at the onset of the opening"),
    ("t_min_at_target_degC",  "t_min corrected to the target: − k × (target − upstream at the opening)"),
    ("t_detect_degC",         "TC at detection"),
    ("t_close_degC",          "TC when the chamber was back at its baseline (closed)"),
    ("upstream_at_open_bar",  "upstream at the onset (bar abs)"),
    ("in_band",               "1 = the upstream was inside the band at the onset"),
    ("baseline_mbar",         "chamber baseline before the opening"),
    ("estimate_degC",         "the estimate the test started from"),
    ("margin_K",              "how far below the estimate it started"),
    ("estimate_from",         "this seating / other seatings at this torque / the opening map / scout"),
    ("counted",               "1 = counts towards the estimate and convergence"),
    ("converged",             "1 = the last results were within ± TMIN_CONVERGE_K after this test"),
    ("trace",                 "the test's rows (main-log columns), relative to logs/"),
    ("note",                  ""),
    ("t_min_converged_degC",  "on the row that converged: the converged T_min, the mean of the last "
                              "TMIN_CONVERGE_N counted results at the target (history 38; filled in "
                              "for rows written before)"),
)

# Columns renamed since they were first written: old name → new. Files
# written before are read under the new name and rewritten with it the next
# time a row is added (history 37: "setting" became "seating", 1 Oct 2026).
RENAMED_COLUMNS = {"setting": "seating"}


def upgrade_row(row):
    """A row read from an older file, with its columns under their new names."""
    for old, new in RENAMED_COLUMNS.items():
        if old in row and new not in row:
            row[new] = row.pop(old)
    return row


def upgrade_header(header):
    """(header with the new names, whether anything was renamed)."""
    new = [RENAMED_COLUMNS.get(c, c) if RENAMED_COLUMNS.get(c) not in header else c
           for c in header]
    return new, new != list(header)


MAIN = tuple(name for name, _ in MAIN_COLUMNS)
PWM = tuple(name for name, _ in PWM_COLUMNS)
RUN_SUMMARY = tuple(name for name, _ in RUN_SUMMARY_COLUMNS)
BATCH_SUMMARY = tuple(name for name, _ in BATCH_SUMMARY_COLUMNS)
OPENINGS = tuple(name for name, _ in OPENINGS_COLUMNS)
TMIN = tuple(name for name, _ in TMIN_COLUMNS)
PWM_SUFFIX = "_pwm.csv"
