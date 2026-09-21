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
# Burst — for upward steps of at least TEMP_BURST_MIN_STEP_K (on arming or
# when the setpoint is raised), heat at full power, cut early, coast with
# the heater off until the TC peaks, then hand back to the PI preloaded near
# holding power. Same idea as the pressure-mode burst. The heater keeps
# ~HEATER_LAG_S worth of heat when cut, which carried the TC on by
# 2.4-3 K in the 16 Sept 2026 runs (12 s and 21 s bursts), so the cut comes
# brake × (1 − e^(−t/HEATER_LAG_S)) below the setpoint. The brake starts at
# TEMP_BURST_BRAKE_K and is re-estimated from each coast's measured rise
# (for this session), so a wrong starting value only costs the first step.
TEMP_BURST_ENABLE     = True
TEMP_BURST_MIN_STEP_K = 4.0        # smaller steps are left to the PI
TEMP_BURST_BRAKE_K    = 3.0        # rise after the cut, fully heated heater (starting value)
TEMP_BURST_LEARN      = 0.5        # weight of each new measurement in the brake estimate
TEMP_BURST_MAX_S      = 90.0       # never burst longer than this
TEMP_COAST_MAX_S      = 60.0
HEATER_LAG_S          = 5.0        # heater → TC lag (fitted to the 13:36 run)
HEATER_HOLD_DUTY_40C  = 0.15       # duty that holds ~40 °C (16 Sept 2026 holds)…
HEATER_HOLD_AMBIENT_C = 26.0       # …scaled with (T − this) for other temperatures
PID_KD                = 0.0        # duty per °C/s, acts on the MEASUREMENT (no
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
PRESSURE_DEADBAND_DEC   = 0.01     # ±decades (≈ ±2.3 %) in which the integrator rests — was 0.02,
                                   # which left the rise above baseline up to ~10 % off
PRESSURE_FILTER_S       = 2.0      # EMA time constant on log10(p), s
PRESSURE_TSP_MIN_C      = 20.0     # lowest temperature setpoint the loop may request
PRESSURE_TSP_MAX_C      = 140.0    # highest (also capped at TEMP_TRIP_C − 5 °C)
PRESSURE_ERR_CLAMP_DEC  = 1.0      # integrator sees at most ±this error (decades), so the
                                   # integral path moves the setpoint ≤ KI × clamp
                                   # (0.2 × 1 × 60 = 12 °C/min) however far off target
PRESSURE_TRIP_MBAR      = 5e-4     # latch heater off above this chamber pressure
PRESSURE_TRIP_ALL_MODES = False    # True: apply the over-pressure trip in manual/auto too
PRESSURE_BAD_READS_TO_TRIP = 8     # consecutive invalid gauge reads (2 s at 4 Hz)
PRESSURE_NO_AUTHORITY_S = 600      # warn if the setpoint sits on a limit this long

# Seek phase — valve still shut. From 16 Sept 2026 runs: the valve stayed shut
# for 18 min at a 39 °C setpoint, opened near 40.1–40.6 °C, snapped open
# rather than throttling, and closed again ~1 K below where it opened.
# Assumes PRESSURE_HEAT_OPENS = True.
PRESSURE_SEEK_START_C   = 40.5     # lowest seek goal: at the estimated cracking point
                                   # (40.1-40.6 °C). Was 39 then 40. Opening early is harmless:
                                   # it only gives the minimum flow, which any target above
                                   # baseline + PRESSURE_MIN_STEP_MBAR exceeds anyway

# Feedforward — for bigger targets, aim the seek straight at the temperature
# the flow map predicts, minus a margin, and let the PI finish the approach.
# Map, from the 16 Sept 2026 16:46 staircase (steady plateaus at 41, 42, 43,
# 44, 45 and 47 °C, upstream ~2.76 bar): the rise above baseline grows
# exponentially, doubling every ~2.2 K:
#     rise ≈ PRESSURE_FF_REF_RISE_MBAR · exp((T − PRESSURE_FF_REF_C) / PRESSURE_FF_EFOLD_K)
# so the goal is T = REF_C + EFOLD · ln(FRACTION · rise / REF_RISE).
# (Replaces a straight-line map, 3e-7 mbar/K above 40.3 °C, which aimed too
# high for mid-size targets.) Flow scales with upstream pressure, so re-fit
# if that changes much.
PRESSURE_FF_ENABLE      = True
PRESSURE_FF_REF_C       = 41.0     # °C
PRESSURE_FF_REF_RISE_MBAR = 7.1e-7 # steady rise above baseline at REF_C
PRESSURE_FF_EFOLD_K     = 3.2      # K per e-fold (×2 every 2.2 K)
PRESSURE_FF_FRACTION    = 0.8      # aim for this share of the rise
PRESSURE_FF_MAX_C       = 55.0     # never aim the seek higher than this

# Upstream-pressure shift. Upstream pressure pushes the seat open, so every
# temperature pressure mode uses (seek start and goal, the parking and
# floor temperatures) moves down as upstream pressure rises:
#     shift = −PRESSURE_UP_K_PER_BAR · (P_upstream − PRESSURE_UP_REF_BAR)
# Evidence: the 6 May 2026 test (Thermo-LeakvalveV2, fixed flow) fitted
# T ≈ 136 °C − 60 K·ln(P/bar), i.e. −21 K/bar near 2.8 bar; on 16 Sept 2026
# the opening temperature rose from ~40.3 °C (2.87 bar, morning) to
# ~41.3 °C (2.77 bar, afternoon), i.e. ~−10 K/bar on this setup. 12 K/bar is
# used until a test at deliberately different upstream pressures pins it
# down. Uses the raw Keller pressure (absolute), not P20.
PRESSURE_UP_ENABLE      = True
PRESSURE_UP_REF_BAR     = 2.76     # upstream pressure the map and temperatures above refer to
PRESSURE_UP_K_PER_BAR   = 12.0     # K of shift per bar
PRESSURE_UP_MAX_SHIFT_K = 10.0     # clamp: the correction is only a local fit
PRESSURE_UP_MAX_AGE_S   = 5.0      # ignore Keller readings older than this

# Seat screw torque changes the seat preload, and so the cracking point —
# far more than upstream pressure does (see below). PRESSURE_SEEK_START_C is
# from 16 Sept 2026, at whatever torque was on the screw then; unrecorded,
# since this input didn't exist yet. Every torque below has a real
# measurement (open temperature, at the upstream pressure recorded with it)
# and REPLACES PRESSURE_SEEK_START_C as the seek/goal reference when the
# entered torque matches within SEAT_SCREW_CRACKING_TOL_NM; the upstream
# shift then applies relative to that entry's own upstream pressure, not
# PRESSURE_UP_REF_BAR — the calibration already includes whatever upstream
# effect existed when it was measured, so shifting from PRESSURE_UP_REF_BAR
# as well would double-count it. An entered torque that matches nothing
# here falls back to PRESSURE_SEEK_START_C, logged as unverified for that
# torque: with only one torque calibrated, nothing is known about any other.
#
# 0.30 N·m: opened at 92.7 °C, upstream 4.49 bar (21 Sept 2026 14:29,
# te-sensor_20260921_142905.csv — an auto-t run at a fixed 110 °C setpoint,
# used to find the cracking point directly; auto-p's own seek, still using
# the 16 Sept reference, was watched separately over the same torque
# (te-sensor_20260921_144015.csv, 14:40) creeping from a seek goal of only
# 31.8 °C — a ~61 K gap at PRESSURE_SEEK_RATE_C_MIN, and also above
# PRESSURE_FF_MAX_C's old absolute ceiling — so it was disarmed well short
# of opening rather than run for the hour that would have taken).
SEAT_SCREW_CRACKING_C = {
    0.30: (92.7, 4.49),    # torque_Nm: (cracking_C, upstream_bar_at_measurement)
}
SEAT_SCREW_CRACKING_TOL_NM = 0.02  # entered torque must be at least this close to reuse a point

# Burst — from a cool start, heat at full power until the TC reaches
# PRESSURE_BURST_BRAKE_K below the seek goal, then coast (heater off) until
# the TC peaks, then seek.
# 16 Sept 2026 13:36 run: 100 % from 29.5 °C opened the valve after 21 s with
# the TC at 45 °C; after cutting the heater the TC kept rising ~3 K for ~8 s
# (heat still in the heater), and chamber pressure followed the TC within
# seconds, so the valve's actuating part sits thermally close to the TC.
# Aiming only for 40 °C, the burst saved almost nothing in the fitted model
# (the PI already heats the small local mass in ~30 s). Enabled with the
# feedforward: the goal is now often well above 40 °C,
# where full power saves more time. The burst starts at once (the valve
# can't open for ≥ ~15 s from a start below PRESSURE_BURST_MAX_START_C, and
# the baseline needs only PRESSURE_BASE_MIN_S of samples).
PRESSURE_BURST_ENABLE   = True
PRESSURE_BURST_BRAKE_K  = 3.5      # cut full power this far below the seek goal
                                   # (the TC then rises ~3 K more)
PRESSURE_BURST_MAX_START_C = 35.0  # warmer starts skip the burst
PRESSURE_BURST_MAX_S    = 60.0     # never burst longer than this (13:36: 21 s for +15.5 K)
PRESSURE_COAST_MAX_S    = 60.0     # coast ends at the TC peak, or after this long
                                   # (holding power for the handover: HEATER_HOLD_DUTY_40C)
PRESSURE_HOLD_SHUT_C    = 38.5     # setpoint while the target is at/below baseline — safely
                                   # below the cracking point, so the valve stays shut
PRESSURE_SEEK_BAND_C    = 0.3      # creep starts once the TC is within this of the setpoint…
PRESSURE_SEEK_HOLD_S    = 0.0      # …and has stayed there this long (lets the valve body
                                   # soak; try 30-60 s if openings overshoot)
PRESSURE_SEEK_RATE_C_MIN = 1.0     # creep rate while the valve is shut, °C per minute.
                                   # Was 0.3 while the valve was thought to lag the TC by
                                   # minutes; the 13:36 run shows it follows within seconds.
                                   # Fitted model: opens after ~2 min (median; 0.3 → 3.5 min),
                                   # overshoot p90 +8 % (0.3: +5 %)
PRESSURE_SEEK_MAX_C     = 46.0     # creep stops here (with a warning)
PRESSURE_OPEN_DEC       = 0.05     # rise above baseline that counts as open (≈ +12 %;
                                   # the snap is +25–40 %)
PRESSURE_BASE_WINDOW_S  = 30.0     # baseline = median log10(p) over this window…
PRESSURE_BASE_GUARD_S   = 5.0      # …ignoring the most recent seconds…
PRESSURE_BASE_MIN_S     = 5.0      # …and available once this much has been recorded
PRESSURE_OPEN_FLOOR_C   = 39.5     # once open, the setpoint never goes below this, so the
                                   # loop trims flow instead of shutting the valve

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
