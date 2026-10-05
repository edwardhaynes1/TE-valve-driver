"""Configuration: hardware wiring, calibration, heater limits, control
tuning, and logging settings. Every tunable number lives here.
"""


from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
# LabJack vacuum gauge (FIO2, analogue) — Pfeiffer IKR 270 cold cathode gauge
# behind a voltage divider. Gauge characteristic (manual, Appendix A):
#   p [mbar] = 10 ** (1.25 * U_gauge - 12.75)   valid for U_gauge 1.96 … 8.6 V
# The LabJack sees V = U_gauge / VACUUM_DIVIDER_RATIO, so the slope per
# LabJack volt is 1.25 × ratio. The divider does not change the intercept.
LABJACK_FIO2_CHANNEL  = 2          # AIN2 (analogue input 2) = FIO2
VACUUM_DIVIDER_RATIO  = 3.235      # U_gauge / V_LabJack — calibrated 1.713 V ↔ 1.5e-6 mbar
VACUUM_GAUGE_SLOPE    = 1.25       # decades per gauge volt (IKR 270)
VACUUM_SLOPE          = VACUUM_GAUGE_SLOPE * VACUUM_DIVIDER_RATIO   # ≈ 4.044 decades per LabJack volt
VACUUM_INTERCEPT      = -12.75     # log10(p / mbar) at 0 V (IKR 270, mbar)
VACUUM_GAUGE_ERROR_V  = 0.5        # gauge side: below = sensor error / no supply
VACUUM_GAUGE_MIN_V    = 1.96       # gauge side: below = underrange (<5e-11 mbar) or not ignited
VACUUM_GAUGE_MAX_V    = 8.6        # gauge side: above = overrange (>1e-2 mbar)
VACUUM_AIN_SPECIAL    = True       # read FIO2 on the U3-LV "special" 0-3.6 V range
                                   # (negative channel 32) instead of the normal
                                   # 0-2.44 V. With the 3.235 divider the gauge's full
                                   # 8.6 V maps to 2.66 V: the normal range clips it at
                                   # ≈ 1-2e-3 mbar, the special range doesn't.
                                   # Cost: ~2× coarser steps (≈ 1 % in pressure).
LABJACK_AIN_SAT_V     = 3.55 if VACUUM_AIN_SPECIAL else 2.40
                                   # at or above this the reading is CLIPPED, not a
                                   # pressure. None = don't check.

# MAX31856 thermocouple (SPI on FIO4-7)
TC_FIO_SDO = 4    # MAX31856 SDO  → LabJack MISO
TC_FIO_SDI = 5    # LabJack MOSI  → MAX31856 SDI
TC_FIO_SCK = 6    # clock
TC_FIO_CS  = 7    # chip select, active low

# ─── Heater (FIO0 → R171 → gate of Q171) ─────────────────────────────────────
HEATER_FIO            = 0          # FIO0 = Heater_PWM on the Ariel control PCB
HEATER_PWM_PERIOD_S   = 2.0        # software time-proportioning period
HEATER_TICK_HZ        = 20         # device-thread tick → 2.5 % duty resolution
HEATER_MAX_DUTY       = 1.00       # hard ceiling on commanded duty (0-1)
HEATER_R_OHM          = 88.0       # element resistance, for the power readout
FLIGHT_POWER_BUDGET_W = 1.0        # drawn dashed on the heater power chart
HEATER_V_RAIL         = 24.0       # switched rail voltage

# ─── Heater voltage / current sensing (optional) ─────────────────────────────
# None = not wired → values are CALCULATED from gate state, rail and R only.
# Spare analogue-capable pins: FIO1, FIO3 (FIO0 heater, FIO2 gauge, FIO4-7 SPI).
# U3-LV inputs are 0-2.44 V: a 24 V signal needs a divider, a shunt needs an
# amplifier. Reading = getAIN() × SCALE + OFFSET. Sampled every heater tick.
#
# HEATER_V_AIN senses the heater SUPPLY (tap it after SW171 if you can, then an
# open switch shows up as 0 V). Element voltage = that rail while the gate is
# on, 0 V while off (Q171 R_DS(on) neglected). Don't use a single divider on
# the MOSFET drain: with a low-side switch, "gate on" and "24 V missing" both
# read 0 V there and are indistinguishable.
HEATER_V_AIN          = None       # e.g. 1 → FIO1
HEATER_V_SCALE        = 11.0       # supply volts per LabJack volt (divider ratio)
HEATER_V_OFFSET       = 0.0
HEATER_I_AIN          = None       # e.g. 3 → FIO3, current-sense amplifier output
HEATER_I_SCALE        = 0.5        # amps per LabJack volt
HEATER_I_OFFSET       = 0.0
HEATER_SENSE_TICKS    = 20         # consecutive implausible ticks before acting (1 s)

# ─── Heater electrical arithmetic — the only place these formulas live ───────
# The heater is switched fully on or off within a PWM period, so the mean over
# a period is the full-power value times the duty.

def heater_voltage_v(duty=1.0):
    """Mean element voltage at this duty, V."""
    return duty * HEATER_V_RAIL


def heater_current_a(duty=1.0):
    """Mean element current at this duty, A."""
    return duty * HEATER_V_RAIL / HEATER_R_OHM


def heater_power_w(duty=1.0):
    """Mean heater power at this duty, W (duty x V^2 / R)."""
    return duty * HEATER_V_RAIL ** 2 / HEATER_R_OHM


def power_from_voltage_w(volts):
    """Power for a measured element voltage, W."""
    return volts ** 2 / HEATER_R_OHM


# ─── TE-Valve settings the operator enters ───────────────────────────────────
SEAT_SCREW_TORQUE_MAX_NM = 5.0     # highest seat screw torque the driver accepts, N·m.
                                   # Blank at start-up until entered (see context.md).
LOCK_NUT_TORQUE_MAX_NM  = 5.0      # highest lock nut torque it accepts, N·m (history 43).
                                   # Optional: blank = not recorded; locks nothing.

# ─── Heater interlocks ───────────────────────────────────────────────────────
TEMP_TRIP_C           = 160.0      # latch off above this valve temperature
HEATER_MAX_RUN_S      = 3600       # auto-disarm after this long armed (s)
TC_BAD_READS_TO_TRIP  = 3          # consecutive bad TC reads before tripping
TC_RETRY_S            = 1.0        # re-initialise a missing / reset MAX31856 this often. Was 5 s,
                                   # when every retry blocked the device thread 0.3 s; a retry
                                   # that finds no chip now returns at once
LJ_WATCHDOG_S         = 10         # U3 firmware watchdog → FIO0 low if we die
# Thermocouple plausibility. On 21 Sept 2026 (17:20-17:31, logs _172024,
# _172612, _172933, _173050) the TC gave nonsense with no MAX31856 fault bit:
# stuck near 72 °C, then FALLING to -70 °C while the heater ran at full power
# for ~40 s; rising 1 °C/s with the heater off; jumping 26 → 51 → 27 → 77 °C
# within 3 s. The controller kept bursting on those readings. Two checks now
# trip the heater instead. Real behaviour the same day: at most +2.5 °C/s at
# full power and -1.6 °C/s cooling; at full power the TC never rose less than
# 0.7 °C/s below 160 °C (full power only balances the losses near ~210 °C).
TC_MAX_RATE_K_S       = 20.0       # a reading changing faster than this is not the valve
TC_RESPONSE_DUTY      = 0.9        # duty counted as "full power" for the response check
TC_RESPONSE_S         = 15.0       # after this long at full power… (None = no check. Needed
                                   # where full power cannot reach the setpoint — e.g. a cold
                                   # thermal-vacuum test — since a TC levelling off at full
                                   # power looks the same as a dead heater)
TC_RESPONSE_MIN_K     = 1.0        # …the TC must have risen at least this much (smallest real
                                   # rise in 15 s at full power, 21 Sept: 3.5 K, at 151 °C just
                                   # after a coast; the faulty TC fell 50 K)

# ─── Closed-loop temperature control (mode auto-t) ───────────────────────────
# Tuned 15 Sept 2026 from bench data. The valve fits a first-order plant with
# gain ≈ 105 °C per unit duty and time constant ≈ 80 s. The previous gains
# (Kp 0.020, Ki 0.0015 → integral time 13 s, far shorter than the plant's
# 80 s) gave damping ≈ 0.44: ~20 % overshoot and a ~2.5 min oscillation.
# These gains were chosen by simulation across ±30-40 % plant variation,
# thermocouple lag up to 20 s and 2.5× sensor noise: overshoot ≤ 0.4 °C,
# 30 °C step settled to ±1 °C in ~1.5 min nominal, ≤ 3 min worst case.
#
# Retuned 16 Sept 2026 from log te-sensor_20260916_100306 (11 °C step to
# ~43 °C). A two-node model (element 45 s + body 150 s, ~50/50, thermocouple
# lag ~4 s) matched that run within ~0.7 K. Kp and Ki raised by 30 %, Ti kept
# at 50 s. Simulated: within 1 K in 23 s (was 33 s), within ±0.3 K in 45 s
# (was 62 s), overshoot ~0.05 K nominal, ~0.7 K if the thermocouple is twice
# as laggy. Kp 0.08 was faster still but reached ~1.1 K overshoot in that
# case — too much when the setpoint sits near the valve's cracking point.
# Previous values: Kp 0.050, Ki 0.0010. Verify with a test step.
#
# REVERTED later on 16 Sept 2026. On real hardware the 0.065 / 0.0013 gains
# overshot by 0.7-1.1 K on ~8-11 K steps that don't saturate the heater
# (11:08 run 31.5 → 39 °C: +1.1 K; 12:38 and 12:58 pressure-mode runs,
# jump to 39 °C: +0.7 and +1.0 K), where the model had predicted ~0.05 K.
# The old gains showed no overshoot on comparable steps (10:03, 10:11).
# Near the valve's cracking point (~40-40.5 °C) a 1 K overshoot can open
# the valve by accident, so the ~10 s speed gain is not worth it.
#
# RETUNED 30 Sept 2026 for t-min-tune (≤ 0.5 °C overshoot, faster): with the
# D term raised and the burst aimed short of the setpoint (TEMP_BURST_SHORT_*),
# the three 21 Sept models (tests/plant_21sept.py), settled starts and
# ambient starts, 5-35 K steps and 1 K staircases: worst overshoot 0.24 K
# (0.26 K with 0.05 K TC noise), slowest settling to ±0.2 K 118 s (was
# 487 s at 0.05 / 0.001 / 0.10); 1 K steps settle in 10-12 s with ≤ 0.2 K.
# The 16 Sept lesson above still stands — the models underpredicted the
# rig's overshoot then — so check the first steps on the rig; the old
# values are PID_KP 0.050, PID_KI 0.0010, PID_KD 0.10.
PID_KP                = 0.20       # duty per °C of error
PID_KI                = 0.0040     # duty per °C·s of accumulated error (Ti = 50 s)
# Hold-power feedforward. auto-t (and auto-p's inner loop) applies
#     duty = hold(T_sp) + PI(error)
# so the PI only trims. Measured 21 Sept 2026 from 9 steady holds at
# 40-146 °C (three runs, lab ~23 °C), within ±14 %:
#     P_hold ≈ HOLD_W_PER_K·(T − HOLD_AMBIENT_C) + HOLD_W_PER_K2·(T − HOLD_AMBIENT_C)²
# = 0.47 W at 40 °C, 1.98 W at 90 °C, 4.07 W at 150 °C. The rule it
# replaces (15 % duty at 40 °C, linear from 26 °C) was about twice that at
# every temperature, and full power above ~120 °C: every burst ended with a
# 5-8 K overshoot as the PI started from it (90 → 94 °C; 155 → 159.5 °C,
# 0.5 K below the trip).
HEATER_HOLD_AMBIENT_C = 23.0       # °C — the lab, 21 Sept 2026
HEATER_HOLD_W_PER_K   = 0.0269     # W per K above ambient
HEATER_HOLD_W_PER_K2  = 4.05e-5    # W per K² (losses grow a little faster when hot)

# Burst — for upward steps of at least TEMP_BURST_MIN_STEP_K (on arming or
# when the setpoint is raised), heat at full power, cut early, coast with
# the heater off until the TC peaks, then hand back to feedforward + PI.
# The cut is predicted from how fast the TC is rising: cut when
#     T + tau · dT/dt ≥ setpoint
# because the heat still in the element carries the TC on by about
# tau × the rate at the cut. That rate already reflects the starting
# temperature (losses at 150 °C leave 0.7-1 °C/s at full power, 1.9-2.3 °C/s
# near 30 °C), which a fixed brake did not. 21 Sept 2026 coasts: tau
# 0.8-2.8 s (12 bursts). tau starts at TEMP_BURST_TAU_S and is re-estimated
# from every coast (for this session). The old fixed brake overshot small
# low-temperature steps by 3-4 K (35 → 38.6 °C).
TEMP_BURST_ENABLE     = True
TEMP_BURST_MIN_STEP_K = 4.0        # smaller steps are left to the PI
TEMP_BURST_TAU_S      = 2.5        # coast rise ÷ rate at the cut, s (starting value; errs
                                   # towards landing short, which the PI finishes)
TEMP_BURST_TAU_MIN_S  = 0.5        # learning limits
TEMP_BURST_TAU_MAX_S  = 6.0
TEMP_BURST_LEARN      = 0.5        # weight of each new measurement in the tau estimate
TEMP_BURST_SHORT_FRAC = 0.15       # aim the burst this share of the step short of the setpoint …
TEMP_BURST_SHORT_MIN_K = 1.5       # … and at least this far short; the PID does the rest. The
                                   # rig's coasts ran 0.6-2.5 K past the setpoint on 16-38 K
                                   # steps (28-30 Sept: ≤ 10 % of the step), which the fitted
                                   # models underpredict, so the burst is kept clear of it.
TEMP_BURST_MAX_S      = 150.0      # never burst longer than this (21 Sept: 25 → 155 °C ≈ 95 s;
                                   # the response check catches a dead TC much sooner)
TEMP_COAST_MAX_S      = 60.0
TEMP_RATE_FILTER_S    = 1.0        # low-pass on the TC rate of change used for the cut, s
PID_KD                = 0.80       # duty per °C/s, acts on the MEASUREMENT (no
                                   # setpoint kick), filtered over PID_D_FILTER_S. It
                                   # damps the stronger P and I (30 Sept retune); with
                                   # 0.05 K TC noise it moves the duty by ~2 % (sd),
                                   # the held temperature by 0.02 K.
PID_D_FILTER_S        = 5.0        # low-pass on the derivative, s
PID_SETPOINT_DEFAULT  = 60.0       # °C

# ─── Closed-loop PRESSURE control (mode auto-p; history 50) ──────────────────
# Conservative, and assumes no opening point: the seat screw torque is not
# used (the opening point moved by tens of K between two seatings, KW40 →
# KW41 2026). auto-p holds the start temperature while it measures the
# baseline P_vacuum (valve shut), creeps up slowly, and backs right off as
# soon as P_vacuum moves. It aims for P_vacuum_target and cuts the heater well
# before P_vacuum_max. Only the upstream pressure P_up is trusted: it sets the
# creep rate. Decisions use the RAW gauge reading, sample by sample (4 Hz).
#
# Lag, from 33 t-min-tune openings (logs 1-5 Oct 2026, 0.20-0.50 N·m, heater
# cut at detection): the TC rose a further ~0.1 K (≤ 1.4 K) for ~0.5 s
# (≤ 4.5 s); P_vacuum peaked ~1 s later (79 % within 3 s), but in 3 of 33 it
# crept up for 5 s-2 min by ≤ 0.15 decades (×1.4: soak); the valve closed
# ~10 K below where it was cut (0-27 K).
PRESSURE_TARGET_DEFAULT = 5e-7     # mbar, P_vacuum_target: where to aim. Any flow near
                                   # it will do (5 Oct 2026); what matters is that
                                   # gas flows and P_vacuum never exceeds PRESSURE_MAX_MBAR
PRESSURE_MAX_MBAR       = 9e-7     # mbar, P_vacuum_max: never to be exceeded
PRESSURE_TARGET_MIN     = 5e-11    # mbar, IKR 270 lower measuring limit
PRESSURE_FILTER_S       = 2.0      # EMA time constant on log10(p), s — display and
                                   # batches / t-min-tune; auto-p decides on the raw reading
PRESSURE_TSP_MAX_C      = 155.0    # highest setpoint auto-p may request (also capped at
                                   # TEMP_TRIP_C − 5 °C)
PRESSURE_TRIP_MBAR      = 5e-4     # latch heater off above this chamber pressure
PRESSURE_TRIP_ALL_MODES = False    # True: apply the over-pressure trip in manual/auto too
PRESSURE_BAD_READS_TO_TRIP = 8     # consecutive invalid gauge reads (2 s at 4 Hz)
PRESSURE_UP_MAX_AGE_S   = 5.0      # ignore Keller readings older than this

# Baseline — P_vacuum with the valve shut, measured before the creep starts
# and while it runs. Batches and t-min-tune use the same window.
PRESSURE_BASE_WINDOW_S  = 30.0     # baseline = median log10(p) over this window…
PRESSURE_BASE_GUARD_S   = 5.0      # …ignoring the most recent seconds…
PRESSURE_BASE_MIN_S     = 5.0      # …and available once this much has been recorded

# Creep — valve shut. Flow through a given opening grows as P_up^1.5
# (0.45 N·m: 1.0 → 5.2 bar gave ×11), so the creep slows by the same factor:
#     rate = PRESSURE_CREEP_C_MIN × (PRESSURE_CREEP_REF_BAR / P_up)^1.5
# 1.5 °C/min (history 53): at 0.5 °C/min a 0.50 N·m valve went 30 → 56 °C in
# the 60 min arming limit (5 Oct 2026, 16:21) and opens at ~92 °C.
PRESSURE_CREEP_C_MIN    = 1.5      # °C/min at PRESSURE_CREEP_REF_BAR
PRESSURE_CREEP_REF_BAR  = 1.0
PRESSURE_CREEP_UP_EXP   = 1.5
PRESSURE_CREEP_MIN_C_MIN = 0.3     # slowest; also used while P_up is not read
PRESSURE_CREEP_MAX_C_MIN = 2.0     # fastest

# Movement — the first sign that the valve is opening: two raw readings in a
# row above baseline + margin (one alone was a spike four times in an hour on
# 5 Oct 2026, 16:21, each costing a back-off). The margin is 4 × the gauge's
# scatter over the baseline window, so it triggers as early as the noise
# allows. The rate of rise for the cut prediction uses the same pairs (the
# lower of each two readings), so a spike can't fake a fast rise; a single
# reading over the cut line still cuts at once.
PRESSURE_MOVE_SIGMAS    = 4.0
PRESSURE_MOVE_MIN_DEC   = 0.02     # decades (+4.7 %): never a smaller margin
# On movement the setpoint freezes below the TC by what the valve body lags
# it at the creep rate (TC lags the body ~8.5 K at 3 °C/min: ~170 s), at
# least PRESSURE_FREEZE_BELOW_K (the TC rose ≤ 1.4 K after a cut).
PRESSURE_FREEZE_BELOW_K = 1.0
PRESSURE_BODY_LAG_S     = 170.0
PRESSURE_STEADY_S       = 30.0     # no further creep until P_vacuum has not risen for this long
PRESSURE_APPROACH_FRACTION = 0.25  # …then creep at this share of the rate within
PRESSURE_NEAR_AIM_DEC   = 0.1      # this many decades (×1.26) of the aim; the full rate
                                   # further below it

# Aim — P_vacuum_target, but at least PRESSURE_FLOW_MARGINS movement margins
# above the baseline, so gas is seen to flow even when the baseline is high.
# Above the aim (and below the cut line) the setpoint eases down at the
# approach rate, at most PRESSURE_TRIM_BELOW_K below the TC.
PRESSURE_FLOW_MARGINS   = 2.0
PRESSURE_TRIM_BELOW_K   = 3.0
PRESSURE_AIM_BAND_DEC   = 0.02     # easing down starts this far (×1.05) above the aim and
                                   # ends below it, so the gauge's 0.005-decade steps don't
                                   # flick it on and off

# Cut — heater OFF when P_vacuum is above the cut line, P_vacuum_max ÷
# PRESSURE_SOAK_FACTOR (9e-7 / 1.4 = 6.4e-7 mbar), or a straight-line fit of
# the last PRESSURE_SLOPE_WINDOW_S predicts it will be within
# PRESSURE_PREDICT_S. 1.4 is the most P_vacuum rose after a cut (1 of 33; the
# others ≤ ×1.12). Heating resumes, holding the TC of that moment, as soon as
# P_vacuum is below PRESSURE_RESUME_FRACTION × the cut line and no longer
# predicted to cross it: the TC falls fast with the heater off (~0.4 K/s
# after t-min-tune cuts), and once the valve shuts it takes long to reopen.
# Above P_vacuum_max itself the valve opens too abruptly for the heater to
# stop it: auto-p stops heating until it is restarted (re-arm, or mode).
PRESSURE_SOAK_FACTOR    = 1.4
PRESSURE_RESUME_FRACTION = 0.95
PRESSURE_PREDICT_S      = 3.0      # s: P_vacuum peaked within 3 s of a cut in 79 % of cases
PRESSURE_SLOPE_WINDOW_S = 2.0      # s of raw readings (8 at 4 Hz) for the rate of rise

# ─── Where the valve opens: seat screw torque and upstream pressure ─────────
# The torque table's guess at the opening point, the valve temperature (TC)
# at which flow starts. Batches and t-min-tune start from it; auto-p does
# not use it (history 50). It depends above all on the seat screw torque,
# then on upstream pressure.
#
# 21 Sept 2026 (logs te-sensor_20260921_150128, _152054, _173217): flow is
# continuous in temperature — the valve throttles — with wide hysteresis
# (it stays open 10-35 K below where it opened) and a soak (at a constant
# 145 °C, flow kept rising for ~3 min). Each entry below:
#     torque_Nm: (opening point °C, upstream bar when measured, e-fold K)
# e-fold: temperature rise that multiplies the flow by e (2.7×), measured on
# the throttling branch.
#   0.25 N·m  flow starts at TC 38-40 °C, 2.0-2.2 bar; e-fold ~4 K (35.5 →
#             40.7 °C at 4 bar). At 5 bar it stayed open down to 27 °C.
#   0.30 N·m  opened at 92.7 °C, 4.49 bar (te-sensor_20260921_142905); no e-fold.
#   0.40 N·m  flow starts at TC 85-89 °C, 2.4 bar; e-fold ~7 K (65 → 52 °C,
#             4.4-4.9 bar). At 5.2 bar it opened by itself at 62 °C.
#   0.45 N·m  flow starts at TC ~150 °C, 1.02 bar (158 °C on a slow first
#             heat-up, 140-148 °C on re-heats); e-fold 10 K (114 → 90 °C) to
#             29 K (150 → 125 °C) at 4-5 bar.
# NOT monotonic in torque (0.30 above 0.40): torque alone does not fix the
# opening point (seating, friction). So these are first guesses only.
# Torques between entries are interpolated; outside the range the nearest
# entry is used, flagged as such. With no torque entered, PRESSURE_SEEK_START_C
# at PRESSURE_UP_REF_BAR is used (16 Sept 2026, torque unrecorded).
SEAT_SCREW_VALVE = {
    0.25: (40.0, 2.10, 3.5),
    0.30: (92.7, 4.49, None),      # e-fold interpolated from its neighbours
    0.40: (88.0, 2.38, 7.5),
    0.45: (150.0, 1.02, 12.0),
}
SEAT_SCREW_TOL_NM       = 0.02     # an entered torque this close counts as that entry
PRESSURE_SEEK_START_C   = 40.5     # opening point with no torque entered (16 Sept 2026:
                                   # 40.1-40.6 °C at ~2.76 bar)
PRESSURE_EFOLD_REF_K    = 3.2      # e-fold of the 16 Sept 2026 valve; used with no torque
                                   # entered

# Upstream pressure pushes the seat open, lowering the opening point:
#     shift = −PRESSURE_UP_K_PER_BAR · (P_upstream − P_at_calibration)
# (6 May 2026: −21 K/bar near 2.8 bar; 16 Sept 2026: ~−10 K/bar; 21 Sept 2026:
# 0.40 N·m shut at 60-67 °C at 2.2 bar, open at 50 °C at 5 bar.) The shift is
# limited asymmetrically: a guess too LOW only costs creep time, a guess too
# high can overshoot the opening, so upward shifts are kept small.
PRESSURE_UP_REF_BAR     = 2.76     # upstream pressure of PRESSURE_SEEK_START_C
PRESSURE_UP_K_PER_BAR   = 12.0     # K of shift per bar
PRESSURE_UP_MAX_SHIFT_K = 10.0     # largest upward shift (upstream below calibration)
PRESSURE_UP_MAX_DOWN_K  = 40.0     # largest downward shift (upstream above calibration)

# ─── Batches — repeated opening-point runs (see context.md; history 27-30) ───
# Every run: cool to the hold temperature, hold it, then approach and creep
# until the valve opens. Scout 1 heats towards the ceiling; scout 2 creeps
# from 10 K below scout 1's opening; test runs creep from a margin below the
# reference. A remembered opening point (OPENINGS_FILE) replaces the scouts.
BATCH_TEST_RUNS_DEFAULT = 5        # most test runs per batch (scouts not counted); it stops
                                   # earlier once the mean is precise enough
BATCH_TEST_RUNS_MAX     = 50
BATCH_MIN_TESTS         = 3        # never stop on precision before this many test runs
BATCH_PRECISION_K       = 1.0      # stop when the mean T_open's 95 % interval is within ±this
BATCH_CREEP_C_MIN       = 3.0      # creep rate, °C per minute (TC lags ~8.5 K at this rate)
BATCH_SCOUT2_BELOW_K    = 10.0     # scout 1 starts this far below the table guess; scout 2
                                   # this far below scout 1's T_open
BATCH_GUESS_ABOVE_K     = 20.0     # scout 1 not open this far above the table guess: it heats
                                   # fast to the ceiling instead of creeping on (history 32)
BATCH_SCOUT_HOLD_S      = 0.0      # scouts aren't averaged: no hold, just a settled chamber
# Margin — how far below the reference a test run starts: each K costs 20 s of
# creep, too little and the valve opens while still approaching.
BATCH_MARGIN_SCATTER_X  = 3.0      # × the scatter of T_open…
BATCH_MARGIN_ADD_K      = 0.5      # …+ this (the approach lands up to ~0.5 K high)…
BATCH_MARGIN_MIN_K      = 2.0      # …kept between these;
BATCH_MARGIN_MAX_K      = 5.0      # also used while no scatter is known (was the fixed margin)
BATCH_MARGIN_UP_BAR     = 1.0      # upstream moved more than this from the reference's…
BATCH_MARGIN_UP_ADD_K   = 2.0      # …adds this
BATCH_SLOPE_MIN_TESTS   = 4        # the batch's own K/bar slope is used from this many test runs…
BATCH_SLOPE_MAX_SE      = 4.0      # …if its standard error is below this, K/bar; else the
                                   # remembered slope, else −PRESSURE_UP_K_PER_BAR
BATCH_CEILING_C         = 155.0    # no opening by here → the run failed. 5 K below TEMP_TRIP_C,
                                   # like PRESSURE_TSP_MAX_C: 0.45 N·m opened at ~146 °C at 2.7 bar
                                   # (24 Sept 2026) and 158 °C at 1 bar (21 Sept) — was 150
BATCH_CEILING_HOLD_S    = 60.0     # …after holding at the ceiling this long
BATCH_START_BAND_K      = 0.5      # creep starts once the TC is within this of the start
# Settle — before every run, heater off: wait until the chamber pressure is
# neither rising (a refill, or outgassing, would look like an opening) nor
# falling fast (the baseline would lag it and make detection late). 24 Sept 2026: after filling upstream the chamber jumped
# 1.4 → 2.35e-6 mbar with the valve cold, then fell back over ~4 min.
BATCH_SETTLE_WINDOW_S   = 60.0     # trend = straight-line fit of log10(p) over this long
BATCH_SETTLE_MAX_RISE_DEC_MIN = 0.01   # settled when rising slower than this (≈ +2.3 %/min)
BATCH_SETTLE_MAX_FALL_DEC_MIN = 0.01   # …and falling slower than this (≈ −2.3 %/min). A
                                   # falling chamber hides the valve's first flow for the
                                   # whole approach and creep (1-2 min): at the earlier
                                   # 0.03 dec/min that was as much as the detection
                                   # threshold, and a slow fill tail read T_open ~7 K high in
                                   # simulation (history 30); at 0.01, < 1 K. 24 Sept 2026:
                                   # the pump-down tail fell 1.5 %/min, so it passes
BATCH_SETTLE_MAX_S      = 1800.0   # still rising after this long: the batch stops
BATCH_UP_MAX_AGE_S      = 5.0      # upstream readings older than this are ignored
BATCH_REFILL_RISE_BAR   = 0.05     # upstream up this much while heating = refilled by hand:
                                   # the fill's chamber jump would read as an opening, so
                                   # the run is discarded and repeated (history 33)
BATCH_UP_FIT_MIN_SPREAD_BAR = 0.05 # fit T_open against upstream only over at least this range
BATCH_ONSET_DEC         = 0.015    # onset = last sample within this of the baseline (best guess)
# Cold start — every run starts from the hold temperature, so every T_open is
# the opening point of a cold, closed valve in the same state (history 30).
# The valve has memory (it opened at 158 °C on a first heat-up, 140-148 °C on
# re-heats, 0.45 N·m); a shallow cooldown gives a precise number for another
# state. Change these for a deep-vs-shallow check; they are stored with results.
BATCH_COOL_TO_C         = 35.0     # hold target…
BATCH_COOL_BELOW_K      = 20.0     # …or this far below the opening point, if lower. No floor:
# Adaptive cooling (history 31) — near room temperature the valve cools ever
# more slowly (24 Sept 2026: the TC falls with a ~150 s time constant to the
# body, ~35 °C, then the body cools slowly), so a cooldown also ends once the
# valve cools slower than this, if it is already the minimum gap below the
# opening point. The first test run's cooldown fixes the hold for the batch.
BATCH_COOL_SLOW_K       = 1.0      # cooled less than this…
BATCH_COOL_SLOW_S       = 60.0     # …over this long: it has (nearly) stopped cooling
BATCH_MIN_GAP_K         = 5.0      # a test run's reference must be this far above the hold
                                   # temperature, or the batch stops: too close to room
                                   # temperature to start cold (0.25 N·m at 5 bar: ~27 °C)
BATCH_GAP_BUFFER_K      = 1.0      # adaptive cooling aims this much beyond the minimum gap, so
                                   # the scatter of later test runs' T_open can't undo it
BATCH_HOLD_S            = 240.0    # auto-t holds it this long (the body lags the TC 150-250 s)
BATCH_HOLD_BAND_K       = 1.0      # the hold time counts once the TC is within this of it
# Detection — open when the chamber rises above its baseline by the absolute
# rise or the relative one, whichever comes first, but not less than the floor.
BATCH_DETECT_ABS_MBAR   = 0.5e-7   # a fixed throughput (flow ≈ pumping speed × rise), so a
                                   # background still pumping down doesn't move what counts
                                   # as open (history 34). ≈ +12 % at the 28 Sept 2026
                                   # background (3.5-4.1e-7), ~15 × the gauge noise; the
                                   # openings jumped ~1e-7 in 2-3 s. Was 1e-7 (or +12 %)
BATCH_DETECT_REL_DEC    = None     # a relative rise too, whichever comes first (was 0.05,
                                   # +12 %, the auto-p rule). None = off
BATCH_DETECT_FLOOR_DEC  = 0.02     # +4.7 %: the gauge noise is ~0.003 decades (24 Sept 2026)
BATCH_RECOVER_FRACTION  = 0.5      # cooldown: chamber back within this × the threshold. Below
                                   # 1, or the next run would start "already open" (found in
                                   # simulation with a 20 % recovery)
BATCH_COOL_MAX_S        = 1800.0   # a cooldown longer than this stops the batch
BATCH_MAX_FAILS         = 2        # this many test runs in a row without opening stop it
BATCH_SCOUT2_TRIES      = 3        # scout 2 opening during its approach (not creeping) is
                                   # repeated this many times in all, each 10 K lower
BATCH_DIR               = str(Path(__file__).resolve().parent.parent / "logs" / "batches")
BATCH_WORKBOOK          = str(Path(__file__).resolve().parent.parent / "logs"
                              / "TE-valve-opening-map.xlsx")
OPENINGS_FILE           = str(Path(__file__).resolve().parent.parent / "logs"
                              / "opening-points.json")   # remembered opening points

# ─── Cycling — openings one after another (cycle.py; history 34) ───
CYCLE_DEEP_EVERY        = 5        # every 5th cycle (the first included) is deep: cooled to
                                   # the batch hold target and held BATCH_HOLD_S, so the fit
                                   # measures the warm-start effect. Set in the window
CYCLE_BELOW_START_K     = 2.0      # a shallow cooldown ends this far below the creep start
                                   # (and not before the valve has closed)
CYCLE_APPROACH_EXTRA_K  = 5.0      # opened while still approaching: the next start is this
                                   # much further below (until one creeps onto it)
CYCLE_MAX_FAILS         = 2        # this many cycles in a row without an opening stop it
CYCLE_CREEP_GUARD_S     = 10.0     # an onset before the creep began, or this soon after it,
                                   # opened during the approach: the chamber lags the valve by
                                   # a few s, and a burst heats 2-3 °C/s, so detection can land
                                   # just after the creep starts and read several K high
CYCLE_REFILL_MAX_S      = 600.0    # a refill while heating pauses detection (seen as upstream
                                   # up BATCH_REFILL_RISE_BAR); not back at its level before
                                   # in this long, the valve opened or the background rose:
                                   # the cycle is dropped
CYCLE_DIR               = str(Path(__file__).resolve().parent.parent / "logs" / "cycles")

# ─── t-min-tune — the lowest opening temperature, step by step (tmin.py; history 36) ───
# At an upstream target the operator holds by hand (± TMIN_BAND_BAR, by
# topping up), each test holds a start temperature below the estimate until
# the chamber is settled, then steps the setpoint up TMIN_STEP_K every
# TMIN_DWELL_S until the valve opens: that step is T_min. The upstream
# leaving the band cuts the heater and abandons the test. After an opening
# the valve must close (chamber back at its baseline, settled) before the
# next test. Every test is a row of TMIN_CSV, which the estimates are
# recomputed from; the logs folder is not in git, so no commit touches it.
TMIN_CSV                = str(Path(__file__).resolve().parent.parent / "logs" / "t-min.csv")
TMIN_DIR                = str(Path(__file__).resolve().parent.parent / "logs" / "t-min")
TMIN_BAND_BAR           = 0.05     # ± bar around the upstream target (≈ ±0.5 K of T_open
                                   # at ~10 K/bar; 30 Sept the operator held ±0.015 bar)
TMIN_UP_CHART_WEIGHT    = 2        # the upstream chart's share of height in t-min-tune
TMIN_STEP_K             = 1.0      # setpoint step
TMIN_DWELL_S            = 300.0    # time at each step (2-3 thermal time constants)
TMIN_STEP_BAND_K        = 0.5      # a step's dwell starts once the TC is this close to it
TMIN_MARGIN_NEW_K       = 10.0     # start this far below the estimate: no result yet at this seating
TMIN_MARGIN_ONE_K       = 5.0      # … one result (no scatter yet)
TMIN_START_BELOW_LOWEST_K = 2.0    # from two results: start this far below the lowest of the
                                   # latest TMIN_ESTIMATE_LAST_N (history 38) …
TMIN_MARGIN_MIN_K       = 3.0      # … but at least this far below the estimate, and at most
TMIN_MARGIN_MAX_K       = 10.0     # this far
TMIN_START_ABOVE_CLOSE_K = None   # K: start no higher than the previous test's T_close + this
                                   # (history 38); None = off. Off by default: on 1 Oct
                                   # (0.30 N·m) +3 K would have started tests at 58-59 °C
                                   # instead of 64-65, ~30 min more each
TMIN_ABOVE_EST_K        = 15.0     # no opening by this far above the estimate: the test ends
                                   # "no opening" and the session stops (the estimate is far off,
                                   # or the valve was already open at the start: no rise to see)
TMIN_OPENED_AT_START_K  = 5.0      # opened before stepping (the start was too high): the next
                                   # test starts this much lower again
TMIN_CLOSED_DEC         = 0.02     # closed: chamber within +5 % of the baseline before the
                                   # opening, and settled (BATCH_SETTLE_MAX_*_DEC_MIN)
TMIN_CLOSE_MAX_S        = 1800.0   # not closed within this long: stop
TMIN_CONVERGE_N         = 3        # stop when the last N counted results, each corrected to
TMIN_CONVERGE_K         = 1.0      # the target, all lie within ± this of their mean
TMIN_ESTIMATE_LAST_N    = 3        # the estimate: the mean of this seating's last N results
TMIN_OPERATOR_EST_MIN_C = 20.0     # the optional "estimate °C" at the start: 20 °C … BATCH_CEILING_C
TMIN_QUIT_WHEN_CONVERGED = True    # converged (the valve has closed by then): the driver closes
TMIN_QUIT_DELAY_S       = 60.0     # … after this long; starting t-min-tune again cancels it (history 39)

# ─── The opening map — every opening, one fit (openmap.py; history 34) ───
OPENINGS_CSV            = str(Path(__file__).resolve().parent.parent / "logs" / "openings.csv")
MAP_FIGURE              = str(Path(__file__).resolve().parent.parent / "logs"
                              / "opening-map.png")      # redrawn after every opening
MAP_REF_BAR             = 3.0      # offsets (a seating's T_open) are given at this upstream
MAP_MIN_SPREAD_BAR      = 0.3      # a torque's slope is fitted once its openings span this
                                   # much upstream; below, −PRESSURE_UP_K_PER_BAR (assumed)
MAP_DEEP_GAP_K          = 15.0     # an imported batch run that started this far below where
                                   # it opened counts as a deep (cold) start
MAP_DONE_OFFSET_K       = 1.0      # a seating is "done" at offset ± this (95 %)…
MAP_DONE_SLOPE_K_BAR    = 2.0      # …and its torque's slope ± this, K/bar (95 %)
MAP_P_MIN_BAR           = 1.0      # pressure hints stay within these
MAP_P_MAX_BAR           = 5.0
MAP_HINT_STEP_BAR       = 1.0      # …and suggest this far beyond the range covered
MAP_CONFOUND_R          = 0.8      # two optional terms correlated this much (within the
                                   # seatings) are reported as not separable

# LabJack combined sample rate (both vacuum + thermocouple read here)
LABJACK_SAMPLE_HZ     = 4          # Hz — reads vacuum and TC each cycle

# Keller
KELLER_BAUD        = 9600
KELLER_ADDRESS     = 250        # bus address 0xFA — confirmed for this unit
KELLER_PORT        = None       # None = auto-detect
KELLER_TIMEOUT     = 0.3
KELLER_ECHO        = True       # K-114 adapter echoes TX
KELLER_POLL_HZ     = 4          # Keller read rate

LOG_INTERVAL_S     = 0.5        # seconds between logged rows (drift-free)
CHART_SECONDS      = 300        # strip-chart window for all live graphs (s)
LOG_DIR            = str(Path(__file__).resolve().parent.parent / "logs")
# ═══════════════════════════════════════════════════════════════════════════════

# Vacuum gauge status strings (None = reading valid)
_SAT_MBAR = (10.0 ** (VACUUM_SLOPE * LABJACK_AIN_SAT_V + VACUUM_INTERCEPT)
             if LABJACK_AIN_SAT_V is not None else None)
VAC_ERROR       = "sensor error / no supply"
VAC_UNDER       = "underrange or not yet ignited"
VAC_OVER        = "overrange (>1e-2 mbar)"
VAC_SATURATED   = (f"LabJack input saturated (>~{_SAT_MBAR:.0e} mbar)"
                   if _SAT_MBAR and _SAT_MBAR < 1e-2 else "LabJack input saturated")
VAC_HIGH_STATES = (VAC_OVER, VAC_SATURATED)   # "pressure is too high to read"
