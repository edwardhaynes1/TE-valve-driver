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

# ─── Heater interlocks ───────────────────────────────────────────────────────
TEMP_TRIP_C           = 160.0      # latch off above this valve temperature
HEATER_MAX_RUN_S      = 3600       # auto-disarm after this long armed (s)
TC_BAD_READS_TO_TRIP  = 3          # consecutive bad TC reads before tripping
TC_RETRY_S            = 5.0        # re-initialise a non-responding MAX31856 this often
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
PID_KP                = 0.050      # duty per °C of error
PID_KI                = 0.0010     # duty per °C·s of accumulated error (Ti = 50 s)
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
TEMP_BURST_MAX_S      = 150.0      # never burst longer than this (21 Sept: 25 → 155 °C ≈ 95 s;
                                   # the response check catches a dead TC much sooner)
TEMP_COAST_MAX_S      = 60.0
TEMP_RATE_FILTER_S    = 1.0        # low-pass on the TC rate of change used for the cut, s
PID_KD                = 0.10       # duty per °C/s, acts on the MEASUREMENT (no
                                   # setpoint kick). 0 = PI, recommended here: in
                                   # simulation D bought a few seconds of settling
                                   # at the cost of amplified sensor noise. Try
                                   # ≤ 0.2 if the thermocouple ever gets laggier.
PID_D_FILTER_S        = 5.0        # low-pass on the derivative, s
PID_SETPOINT_DEFAULT  = 60.0       # °C

# ─── Closed-loop PRESSURE control (mode auto-p) ──────────────────────────────
# Cascade: outer PI on log10(chamber pressure) → valve temperature setpoint →
# the inner PI above → duty. Gains are STARTING VALUES — tune on the bench.
# The operator's target is ABSOLUTE chamber pressure. The baseline (valve
# shut) drifts through the day as the chamber pumps down (0.56–1.43e-6 mbar
# on 16 Sept 2026), so the lowest target the valve can hold moves too: the
# driver measures the baseline and warns when a target is below it.
PRESSURE_TARGET_DEFAULT = 1.5e-6   # mbar
PRESSURE_TARGET_MIN     = 5e-11    # mbar, IKR 270 lower measuring limit
PRESSURE_MIN_STEP_MBAR  = 2.5e-7   # lowest rise above baseline the valve holds once open.
                                   # 16 Sept 2026: it snaps open by 0.27-0.34e-6, then
                                   # settled at +0.26e-6 with the setpoint at the
                                   # 39.5 °C floor (12:58 run). Targets below
                                   # baseline + this cannot be held
PRESSURE_HEAT_OPENS     = True     # True: hotter valve → more flow → higher chamber p
# Gains set 16 Sept 2026 by simulating this code against a valve model built
# from that day's runs (snap-open at ~40.3 °C, ~1 K closing hysteresis, body
# lag 150-250 s, flow gain 0.2-0.8e-6 mbar/K, flow lag 5-60 s; 72 cases).
# KI 0.05 → 0.1 halved the settling time after opening (median 430 → 215 s)
# and stopped a slow ±0.4 K setpoint wander; overshoot ≤ +9 % of the rise
# above baseline.
#
# Retuned again 16 Sept 2026 after the 13:36 full-power run, with a lumped
# model fitted to it (heater lag ~5 s, ~6 J/K local, ~80 s to ~27 °C ambient;
# valve follows the TC within ~2-6 s, plus some slow soak). 240 cases (valve
# lag/soak, start temperature, target, flow slope 0.3-2.5e-7 mbar/K, cracking
# point 40.0-40.6 °C): KI 0.1 → 0.2 cut the median time to target from
# ~420 s to ~270 s with no hunting (smoothed ripple ≤ 4 %); 0.3-0.4 were
# faster still but with less margin.
PRESSURE_KP             = 10.0     # °C per decade — acts on the MEASUREMENT (damping), not the error
# With the feedforward goal (below) doing most of the heating, KI 0.2 → 0.3
# cut the median time to target from ~210 to ~170 s (p90 530 → 360 s) with
# the same overshoot; smoothed steady ripple ≤ 12 % (≤ 8 % at 0.2). KP 20
# hunted in the same tests, so KP stays at 10.
#
# Back to 0.2 after the 16:46 staircase: the valve keeps "soaking" for
# 1-2 min after it opens (pressure doubled at constant TC), much more than
# assumed above. On valve models fitted to that (324 cases, exponential flow
# map ±50 %), KI 0.3 overshot by up to +77 % and 0.45 hunted; 0.2 kept
# overshoot ≤ +10 % (p90 +7 %), median time to target ~3 min.
PRESSURE_KI             = 0.20     # °C per (decade·s) — history: 0.05, 0.10, 0.20, 0.30
# Both gains are for a valve whose flow e-folds every PRESSURE_EFOLD_REF_K
# (3.2 K, 16 Sept 2026). auto-p multiplies them by (this torque's e-fold ÷
# PRESSURE_EFOLD_REF_K): the 0.45 N·m valve needs ~4× the temperature change
# for the same change in flow, so it gets ~4× the gain and the loop behaves
# the same in decades of pressure.
PRESSURE_GAIN_EXP       = 0.5      # gains × (e-fold ÷ ref) ^ this. 1.0 (fully proportional)
                                   # made a 1 K-hysteresis valve hunt ±0.5 decade in simulation;
                                   # 0.5 kept 28 reachable cases of 32 within +19 % (the other
                                   # 4 targets were above what 155 °C can give)
PRESSURE_DEADBAND_DEC   = 0.01     # ±decades (≈ ±2.3 %) in which the integrator rests — was 0.02,
                                   # which left the rise above baseline up to ~10 % off
PRESSURE_FILTER_S       = 2.0      # EMA time constant on log10(p), s
PRESSURE_TSP_MIN_C      = 20.0     # lowest temperature setpoint the loop may request
PRESSURE_TSP_MAX_C      = 155.0    # highest (also capped at TEMP_TRIP_C − 5 °C); was 140,
                                   # below the 0.45 N·m valve's ~150 °C opening point
PRESSURE_ERR_CLAMP_DEC  = 1.0      # integrator sees at most ±this error (decades), so the
                                   # integral path moves the setpoint ≤ KI × clamp
                                   # (0.2 × 1 × 60 = 12 °C/min) however far off target
PRESSURE_TRIP_MBAR      = 5e-4     # latch heater off above this chamber pressure
PRESSURE_TRIP_ALL_MODES = False    # True: apply the over-pressure trip in manual/auto too
PRESSURE_BAD_READS_TO_TRIP = 8     # consecutive invalid gauge reads (2 s at 4 Hz)
PRESSURE_NO_AUTHORITY_S = 600      # warn if the setpoint sits on a limit this long

# ─── Where the valve opens: seat screw torque and upstream pressure ─────────
# Every temperature auto-p uses (seek goal, creep limit, parking, open floor)
# is set RELATIVE to the valve's opening point: the valve temperature (TC)
# at which flow starts. That point depends above all on the seat screw
# torque, then on upstream pressure.
#
# 21 Sept 2026 (logs te-sensor_20260921_150128, _152054, _173217): flow is
# continuous in temperature — the valve throttles — with wide hysteresis
# (it stays open 10-35 K below where it opened) and a soak (at a constant
# 145 °C, flow kept rising for ~3 min). Each entry below:
#     torque_Nm: (opening point °C, upstream bar when measured, e-fold K)
# e-fold: temperature rise that multiplies the flow by e (2.7×), measured on
# the throttling branch — the loop gains scale with it (see PRESSURE_KP).
#   0.25 N·m  flow starts at TC 38-40 °C, 2.0-2.2 bar; e-fold ~4 K (35.5 →
#             40.7 °C at 4 bar). At 5 bar it stayed open down to 27 °C.
#   0.30 N·m  opened at 92.7 °C, 4.49 bar (te-sensor_20260921_142905); no e-fold.
#   0.40 N·m  flow starts at TC 85-89 °C, 2.4 bar; e-fold ~7 K (65 → 52 °C,
#             4.4-4.9 bar). At 5.2 bar it opened by itself at 62 °C.
#   0.45 N·m  flow starts at TC ~150 °C, 1.02 bar (158 °C on a slow first
#             heat-up, 140-148 °C on re-heats); e-fold 10 K (114 → 90 °C) to
#             29 K (150 → 125 °C) at 4-5 bar.
# NOT monotonic in torque (0.30 above 0.40): torque alone does not fix the
# opening point (seating, friction). So these are first guesses. Once the
# valve opens, the point actually seen replaces the table for the rest of
# the session (while the entered torque stays the same).
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
PRESSURE_EFOLD_REF_K    = 3.2      # e-fold of the 16 Sept 2026 valve, which the gains were
                                   # tuned on; also used with no torque entered

# Upstream pressure. It pushes the seat open, lowering the opening point:
#     shift = −PRESSURE_UP_K_PER_BAR · (P_upstream − P_at_calibration)
# (6 May 2026: −21 K/bar near 2.8 bar; 16 Sept 2026: ~−10 K/bar; 21 Sept 2026:
# 0.40 N·m shut at 60-67 °C at 2.2 bar, open at 50 °C at 5 bar.) The shift is
# limited asymmetrically: a goal too LOW only costs creep time, a goal too
# high can overshoot the opening, so upward shifts are kept small.
# It also raises the flow through a given opening: at 0.45 N·m, 1.0 → 5.2 bar
# multiplied the flow by 11 (≈ pressure^1.5). Once open, auto-p feeds that
# forward — when upstream pressure changes (a refill, or the upstream leak),
# the setpoint moves by −exponent × e-fold × ln(P / P_at_opening) at once,
# instead of waiting for the chamber pressure to drift and the PI to catch it.
PRESSURE_UP_ENABLE      = True
PRESSURE_UP_REF_BAR     = 2.76     # upstream pressure of PRESSURE_SEEK_START_C
PRESSURE_UP_K_PER_BAR   = 12.0     # K of shift per bar
PRESSURE_UP_MAX_SHIFT_K = 10.0     # largest upward shift (upstream below calibration)
PRESSURE_UP_MAX_DOWN_K  = 40.0     # largest downward shift (upstream above calibration)
PRESSURE_UP_MAX_AGE_S   = 5.0      # ignore Keller readings older than this
PRESSURE_UP_FLOW_EXP    = 1.5      # flow ∝ upstream pressure ^ this, at a fixed opening
PRESSURE_UP_FF_MAX_K    = 30.0     # the track feedforward moves the setpoint at most this far

# Seek — valve shut: heat to the goal (the opening point, raised by the
# feedforward below for large targets), then creep up until the valve opens.
PRESSURE_SEEK_BAND_C    = 0.3      # creep starts once the TC is within this of the setpoint…
PRESSURE_SEEK_HOLD_S    = 0.0      # …and has stayed there this long
PRESSURE_SEEK_RATE_C_MIN = 1.0     # creep rate while the valve is shut, °C per minute, for
                                   # the 16 Sept valve; × the same gain scale as PRESSURE_KP
                                   # (0.45 N·m: 1.9 °C/min)
PRESSURE_SEEK_ABOVE_K   = 20.0     # creep stops this far above the opening point (with a
                                   # warning) — the table can be ~10 K off
PRESSURE_HOLD_SHUT_BELOW_K = 5.0   # target at/below baseline: park this far below the
                                   # opening point, so the valve stays shut
PRESSURE_OPEN_DEC       = 0.05     # rise above baseline that counts as open (≈ +12 %)
PRESSURE_BASE_WINDOW_S  = 30.0     # baseline = median log10(p) over this window…
PRESSURE_BASE_GUARD_S   = 5.0      # …ignoring the most recent seconds…
PRESSURE_BASE_MIN_S     = 5.0      # …and available once this much has been recorded
PRESSURE_OPEN_FLOOR_BELOW_K = 1.0  # once open, the setpoint stays at least this close to
                                   # the opening point, so the loop trims flow rather than
                                   # shutting the valve. 16 Sept: it closed 1 K below where
                                   # it opened. 21 Sept valves stayed open 10-35 K below, but
                                   # in simulation a 3 K floor made a 1 K-hysteresis valve
                                   # close and reopen every ~90 s (±1 decade)
PRESSURE_OPEN_LAG_S     = 5.0      # the opening point learned from a rising TC is taken this
                                   # many seconds' rise earlier: the valve follows the TC within
                                   # ~2-6 s (16 Sept). Kept short on purpose: learning it too
                                   # LOW also lowers the open floor, and in simulation 10 s put
                                   # the floor below where a 1 K-hysteresis valve shuts — it
                                   # then cycled open/shut. Too high only costs a little range.

# Feedforward — for bigger targets, aim the seek above the opening point,
# using the 16 Sept 2026 map (rise ≈ 7.1e-7 mbar 0.5 K above the opening,
# at 2.76 bar), scaled by this torque's e-fold and the upstream pressure.
# Kept small: on 21 Sept the valve's soak made higher aims overshoot.
PRESSURE_FF_ENABLE      = True
PRESSURE_FF_REF_ABOVE_K = 0.5      # K above the opening point where…
PRESSURE_FF_REF_RISE_MBAR = 7.1e-7 # …the rise above baseline was this, at PRESSURE_UP_REF_BAR
PRESSURE_FF_FRACTION    = 0.8      # aim for this share of the rise
PRESSURE_FF_MAX_ABOVE_K = 5.0      # never aim the seek more than this above the opening point

# Burst — from well below the goal, heat at full power, cut on the same
# rate prediction as auto-t (T + tau·dT/dt, tau shared and learned), aiming
# PRESSURE_BURST_MARGIN_K below the goal; coast to the peak; then seek.
# Decided by how far below the goal the TC starts, not by an absolute
# temperature (the old 35 °C rule never burst a 60 °C start toward 150 °C).
PRESSURE_BURST_ENABLE   = True
PRESSURE_BURST_MIN_STEP_K = 10.0   # burst only if the TC starts at least this far below the goal
PRESSURE_BURST_MARGIN_K = 2.0      # land this far below the goal; the creep does the rest
PRESSURE_BURST_MAX_S    = 120.0    # never burst longer (21 Sept: 25 → 150 °C ≈ 80 s)
PRESSURE_COAST_MAX_S    = 60.0     # coast ends at the TC peak, or after this long

# LabJack combined sample rate (both vacuum + thermocouple read here)
LABJACK_SAMPLE_HZ     = 4          # Hz — reads vacuum and TC each cycle

# Keller
KELLER_BAUD        = 9600
KELLER_ADDRESS     = 250        # bus address 0xFA — confirmed for this unit
KELLER_PORT        = None       # None = auto-detect
KELLER_TIMEOUT     = 0.3
KELLER_ECHO        = True       # K-114 adapter echoes TX
KELLER_POLL_HZ     = 4          # Keller read rate
P20_REF_K          = 293.15     # P20 = P * P20_REF_K / T_keller — text readout only.
                                # T_keller is the sensor-chip temperature, not the gas
                                # temperature, so P20 is not charted (17 Sept 2026).

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
