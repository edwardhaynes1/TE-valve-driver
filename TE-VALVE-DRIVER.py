#!/usr/bin/env python3
"""
TE-VALVE-DRIVER.py
==================
Live display and logging for the thermally-enabled (TE) valve test setup.

Logs three sensor channels with a robust, self-reconnecting architecture:
  • Upstream pressure + temperature  — Keller PAA-23SX-H2 (RS485/USB, K-114)
  • Vacuum chamber pressure          — Pfeiffer IKR 270 cold cathode gauge,
                                       LabJack U3 FIO2 via voltage divider
  • TE valve temperature             — MAX31856 Type-K thermocouple, LabJack SPI

Each sensor runs on its own thread. If any device disconnects mid-session the
affected reading goes to '---' immediately (never a stale value) and the thread
scans for the device again before resuming — the rest of the system is
unaffected.

HEATER CONTROL is included (FIO0). See the safety notes below. Three modes:
  manual    fixed duty cycle
  auto (T)  PI loop (optional D) holding the valve at a temperature setpoint
  auto (P)  cascade: an outer loop on chamber pressure moves the temperature
            setpoint, and the auto-mode PI holds the valve there. It first
            SEEKS (from a cool start a full-power BURST, then a coast with the
            heater off; then setpoint 40 °C and a slow creep upwards while
            the valve is shut, measuring the chamber baseline), and once the
            pressure snaps up it TRACKS the absolute pressure target with a
            PI on log10(pressure). The baseline is used to detect the opening
            and to warn about targets the valve cannot hold

HEATER VOLTAGE / CURRENT are shown live (instantaneous and mean over one
switching period). By default they are CALCULATED from the gate state, rail
voltage and element resistance. That assumes SW171 is on and 24 V is present,
because the software cannot see either. If a supply-sense divider and/or a
current-sense amplifier is wired to a spare analogue input, set HEATER_V_AIN /
HEATER_I_AIN and the MEASURED values are shown and logged as well, with a
plausibility check of current against gate state.

GUI (Tkinter, standard library): a single "Live Log" window with device status,
live readouts, strip charts, heater controls, and a scrolling event log.

Hardware
--------
  Keller PAA-23SX-H2   RS485/USB (K-114 adapter) — upstream P + T
  LabJack U3           FIO0 — heater gate drive (digital out)
                       FIO2 — vacuum gauge (analogue)
                       FIO4-7 — MAX31856 thermocouple (SPI)

MAX31856 SPI pins (from Ariel_Control_PCB schematic):
  FIO4 → SDO   FIO5 → SDI   FIO6 → SCK   FIO7 → !CS

Heater chain (from Ariel_Control_PCB schematic):
  FIO0 (Heater_PWM) → R171 (270 R) → gate of Q171 (DMN3150L-7, N-ch MOSFET)
  Heater+ sits on the CONSTANT +24 V rail; Q171 low-side switches the return.
  SW171 (SW_Heater) is a physical enable in series — the software cannot
  override it, which is exactly the point.
  Element: ~88 R  →  24 V / 0.27 A / 6.5 W at 100 % duty.

Why software time-proportioning and NOT hardware PWM
----------------------------------------------------
  On U3 hardware revision 1.30+ (which includes every U3-HV), timers cannot be
  assigned to FIO0-FIO3: TimerCounterPinOffset must be 4-8. A value of 0-3
  either errors, or — if the error is suppressed in LJControlPanel — is
  silently promoted to 4, which would place Timer0 on FIO4, i.e. directly on
  top of the MAX31856 SDO line. So hardware PWM on FIO0 is not available here.

  Instead FIO0 is toggled as a plain digital output on a slow duty cycle
  (HEATER_PWM_PERIOD_S). The valve's thermal time constant is tens of seconds
  to minutes, so a 1-2 s switching period is thermally invisible while keeping
  the SPI bus untouched.

Safety
------
  * Heater always starts DISARMED, and is forced low on connect and on exit.
  * LabJack firmware watchdog: if this program stops talking to the U3 for
    LJ_WATCHDOG_S seconds, the U3 itself drives FIO0 low. This is the
    protection against a crashed or killed Python process.
  * Interlocks (any one forces duty to 0): thermocouple unavailable or
    faulted, temperature above TEMP_TRIP_C, run time above HEATER_MAX_RUN_S,
    heater current flowing with the gate OFF (only if current sense is wired).
  * Pressure mode adds: no valid gauge reading for PRESSURE_BAD_READS_TO_TRIP
    reads, gauge overrange or LabJack input saturated, or chamber pressure
    above PRESSURE_TRIP_MBAR. The temperature setpoint it generates is clamped
    to PRESSURE_TSP_MIN_C … PRESSURE_TSP_MAX_C and rate-limited.
  * The heater can only ADD heat. In pressure mode the loop cannot cool the
    valve below ambient; if the target needs that, it warns and sits at the
    minimum setpoint.
  * A trip LATCHES. You must press DISARM then ARM again to clear it.

CSV columns
-----------
  timestamp, keller_pressure_bar, keller_temperature_degC,
  n_keller_samples, vacuum_chamber_mbar, te_temperature_degC, tc_fault,
  heater_duty, heater_mode, heater_setpoint_degC,
  heater_V_mean_calc, heater_I_mean_calc, heater_V_mean_meas,
  heater_I_mean_meas, pressure_target_mbar, vacuum_status,
  heater_duty_cmd, events, pressure_baseline_mbar
  (new columns are appended at the end, so existing parsers keep working)

Logging cadence
---------------
  One CSV row on a drift-free wall-clock cadence of LOG_INTERVAL_S seconds
  (first row written immediately at start, final row written on exit).
  Every event-log line (operator changes to mode / duty / setpoint / target,
  arm / disarm, trips, gauge status, reconnects) is also written to the
  'events' column of the next row, so the CSV is a complete record.

Usage
-----
  python TE-VALVE-DRIVER.py
"""

import serial
import serial.tools.list_ports
import threading
import time
import csv
import os
import math
import re
from collections import deque
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

try:
    import u3
    LABJACK_AVAILABLE = True
except ImportError:
    LABJACK_AVAILABLE = False
    print("Warning: LabJackPython not installed — vacuum gauge and thermocouple disabled.")
    print("Install: pip install LabJackPython")

try:
    from keller_protocol import keller_protocol as kp
    KELLER_LIB_AVAILABLE = True
except ImportError:
    KELLER_LIB_AVAILABLE = False
    print("Warning: keller-protocol not installed — Keller sensor disabled.")
    print("Install: pip install keller-protocol")

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

# ─── Heater interlocks ───────────────────────────────────────────────────────
TEMP_TRIP_C           = 160.0      # latch off above this valve temperature
HEATER_MAX_RUN_S      = 3600       # auto-disarm after this long armed (s)
TC_BAD_READS_TO_TRIP  = 3          # consecutive bad TC reads before tripping
TC_RETRY_S            = 5.0        # re-initialise a non-responding MAX31856 this often
LJ_WATCHDOG_S         = 10         # U3 firmware watchdog → FIO0 low if we die

# ─── Closed-loop temperature control (mode 'auto') ───────────────────────────
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

# ─── Closed-loop PRESSURE control (mode 'pressure') ──────────────────────────
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
P20_REF_K          = 293.15     # P20 = P * P20_REF_K / T_keller — upstream pressure
                                # referred to 20 °C, proportional to the amount of gas

LOG_INTERVAL_S     = 0.5        # seconds between logged rows (drift-free)
CHART_SECONDS      = 300        # strip-chart window for all live graphs (s)
LOG_DIR            = str(Path(__file__).parent / "logs")
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

# MAX31856 register map
REG_CR0     = 0x00
REG_CR1     = 0x01
REG_LTCBH   = 0x0C   # linearized TC temp, high byte
REG_FAULTSR = 0x0F   # fault status register

FAULT_BITS = {
    0x01: "open circuit",
    0x02: "over/under-voltage",
    0x04: "TC low threshold",
    0x08: "TC high threshold",
    0x10: "CJ low threshold",
    0x20: "CJ high threshold",
    0x40: "CJ out of range",
    0x80: "TC out of range",
}

os.makedirs(LOG_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLE-INSTANCE LOCK
# ═══════════════════════════════════════════════════════════════════════════════
# Two copies of this program cannot share the LabJack, and — more importantly —
# a forgotten instance may still be driving FIO0. So the second copy refuses to
# start rather than failing halfway through connecting.
#
# The lock is an OS-level file lock held open for the life of the process. The
# operating system releases it automatically when the process dies, however it
# dies, so there is no stale-lock file to clean up by hand.

_LOCK_PATH = str(Path(__file__).parent / ".te-valve-driver.lock")
_lock_fh = None


def acquire_single_instance_lock():
    """Return True if we got the lock, False if another instance holds it."""
    global _lock_fh
    try:
        _lock_fh = open(_LOCK_PATH, 'a+')
    except Exception:
        return True          # can't create a lock file — don't block the user

    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _lock_fh.close()
        _lock_fh = None
        return False
    except Exception:
        return True          # unsupported platform — fail open, not closed

    _lock_fh.seek(0)
    _lock_fh.truncate()
    _lock_fh.write(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}\n")
    _lock_fh.flush()
    return True


def _make_log_path():
    """Return a writable CSV path, retrying with a unique suffix if locked."""
    base = datetime.now().strftime('%Y%m%d_%H%M%S')
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"_{attempt}"
        path = os.path.join(LOG_DIR, f"te-sensor_{base}{suffix}.csv")
        try:
            fh = open(path, 'x', newline='')
            fh.close()
            return path
        except (PermissionError, FileExistsError):
            continue
    return f"te-sensor_{base}.csv"


LOG_FILE = _make_log_path()

# ─── Shared state ─────────────────────────────────────────────────────────────
_lock        = threading.Lock()        # protects _state / _events / chart buffers
_stop        = threading.Event()
_state       = dict(
    keller_pressure_samples     = [],    # accumulated between log rows, then averaged
    keller_temperature_samples  = [],    # accumulated between log rows, then averaged
    vacuum_chamber_mbar         = None,  # latest single reading
    vacuum_status               = None,  # None = valid, else VAC_* reason string
    vacuum_gauge_V              = None,  # gauge-side signal voltage, V
    te_temperature_degC         = None,  # latest MAX31856 reading
    tc_fault                    = None,  # latest fault register (0 = OK, None = no read)
    keller_pressure_bar         = None,  # latest single upstream reading (absolute)
    keller_pressure_t           = None,  # time of that reading
)

# ─── Heater command / status (protected by _heater_lock) ──────────────────────
_heater_lock = threading.Lock()
_heater = dict(
    armed        = False,      # operator has armed the heater
    mode         = 'manual',   # 'manual' (fixed duty) or 'auto' (PI on temp)
    duty_cmd     = 0.0,        # commanded duty in manual mode, 0-1
    setpoint_C   = PID_SETPOINT_DEFAULT,
    duty_actual  = 0.0,        # what the output is actually doing right now
    armed_at     = None,       # time.time() when armed
    trip_reason  = None,       # non-None = latched trip, needs re-arm
    integral     = 0.0,        # PI integral term
    d_prev       = None,       # last valve temperature seen by the D term
    d_filt       = 0.0,        # filtered dT/dt, °C/s
    # ── live electrical readout (written by the device thread) ────────────
    out_high     = False,      # FIO0 state right now
    v_now        = 0.0,        # calculated element voltage right now, V
    i_now        = 0.0,        # calculated element current right now, A
    rail_meas    = None,       # measured supply (HEATER_V_AIN), V
    v_meas       = None,       # measured element voltage right now, V
    i_meas       = None,       # measured element current right now, A
    v_meas_mean  = None,       # ... averaged over one switching period
    i_meas_mean  = None,
    # ── pressure (outer) loop ─────────────────────────────────────────────
    p_target_mbar   = PRESSURE_TARGET_DEFAULT,
    p_init          = True,    # re-initialise bumplessly on the next valid read
    p_filt          = None,    # filtered log10(p / mbar)
    p_err           = None,    # last error, decades (target − filtered)
    p_y0            = None,    # filtered log10(p) at loop start (P-on-measurement reference)
    p_t0            = 0.0,     # setpoint at loop start, °C
    p_integral      = 0.0,     # decade·s
    p_pinned_since  = None,
    p_pinned_warned = False,
    p_phase         = 'seek',  # 'seek' (valve shut) or 'track' (PI on pressure)
    p_ramping       = False,   # seek: creeping upwards
    p_band_since    = None,    # seek: when the TC first reached the seek band
    p_creep         = 0.0,     # seek: °C added by the creep on top of the goal
    p_goal          = None,    # seek: current goal temperature (for display/logging)
    p_shift         = 0.0,     # upstream-pressure shift applied to all temperatures, K
    p_shift_logged  = None,    # last shift reported in the event log
    p_up_bar        = None,    # upstream pressure the shift was computed from
    p_burst         = None,    # seek: None, 'burst', 'coast'
    t_check         = False,   # auto: re-evaluate a burst (armed / setpoint changed)
    t_burst         = None,    # auto: None, 'burst', 'coast'
    t_burst_t0      = None,
    t_burst_peak    = None,
    t_burst_cut     = None,    # auto: (TC at cut, heater charge fraction at cut)
    t_brake         = TEMP_BURST_BRAKE_K,
    p_burst_t0      = None,    # when the current burst/coast stage began
    p_burst_peak    = None,    # coast: highest TC seen
    p_override      = None,    # duty the outer loop imposes (burst/coast), else None
    p_seek_capped   = False,   # seek: reached PRESSURE_SEEK_MAX_C
    p_hist          = deque(maxlen=int((PRESSURE_BASE_WINDOW_S + 5) * LABJACK_SAMPLE_HZ * 2)),
    p_base          = None,    # baseline log10(p / mbar), frozen when the valve opens
    p_warned_target = None,    # last target warned about (below what the valve can hold)
)
_events      = deque(maxlen=200)                            # (timestamp, text)
_p20_chart   = deque(maxlen=CHART_SECONDS * KELLER_POLL_HZ)     # recent upstream P20 (bar at 20 °C)
_vac_chart   = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ)  # recent vacuum readings
_te_chart    = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ)  # recent TE temperatures
_heat_chart  = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ)  # recent heater current (period mean)
_keller_ok   = False
_labjack_ok  = False   # vacuum gauge channel healthy
_tc_ok       = False   # thermocouple channel healthy
_csv_ok      = None    # CSV logger: None = starting, True = writing, False = failing


_events_pending = []    # events not yet written to the CSV (protected by _lock)


def log_event(text: str):
    """Record a timestamped event for the GUI log, the CSV 'events' column,
    and the console."""
    stamp = datetime.now().strftime('%H:%M:%S')
    with _lock:
        _events.append((stamp, text))
        _events_pending.append(text)
        if len(_events_pending) > 1000:          # CSV failing for a long time
            del _events_pending[:-1000]
    try:
        print(f"[{stamp}] {text}")
    except Exception:
        # Console that can't show a character (e.g. cp1252) must not break
        # the calling thread — which may be the heater/device thread.
        try:
            print(f"[{stamp}] {_ascii(text)}")
        except Exception:
            pass


_ASCII_MAP = str.maketrans({'→': '->', '·': ';', '—': '-', '–': '-',
                            '°': 'deg', '…': '...', 'Ω': 'ohm', '±': '+/-',
                            '≈': '~', 'µ': 'u', '×': 'x'})


def _ascii(text: str) -> str:
    """Plain-ASCII version of an event line, for the CSV and odd consoles."""
    return text.translate(_ASCII_MAP).encode('ascii', 'replace').decode('ascii')


# ═══════════════════════════════════════════════════════════════════════════════
# PORT FILTERING
# ═══════════════════════════════════════════════════════════════════════════════

def _usb_serial_ports():
    """Return non-Bluetooth COM ports, FTDI/USB-serial first."""
    real, other = [], []
    for p in serial.tools.list_ports.comports():
        desc = (p.description or '').upper()
        hwid = (p.hwid or '').upper()
        if 'BLUETOOTH' in desc or 'BLUETOOTH' in hwid or 'BTH' in hwid:
            continue
        if not hwid or hwid == 'N/A':
            continue
        if 'FTDI' in desc or 'FTDI' in hwid or 'USB SERIAL' in desc or 'VID_0403' in hwid:
            real.append(p)
        else:
            other.append(p)
    return real + other


# ═══════════════════════════════════════════════════════════════════════════════
# KELLER  — upstream pressure / temperature
# ═══════════════════════════════════════════════════════════════════════════════

def _probe_keller(port: str):
    if not KELLER_LIB_AVAILABLE:
        return None
    try:
        bus = kp.KellerProtocol(
            port=port, baud_rate=KELLER_BAUD,
            timeout=KELLER_TIMEOUT, echo=KELLER_ECHO,
        )
        fw = bus.f48(KELLER_ADDRESS)
        p1 = bus.f73(KELLER_ADDRESS, 1)
        if p1 is not None and -2.0 < p1 < 50.0:
            print(f"  [Keller] {port}: init OK (fw {fw}), P1={p1:.3f} bar")
            return bus
    except Exception:
        pass
    return None


def _detect_keller_bus():
    if KELLER_PORT is not None:
        bus = _probe_keller(KELLER_PORT)
        if bus is not None:
            print(f"  [Keller] connected on {KELLER_PORT}")
            return KELLER_PORT, bus
        print(f"  [Keller] no response on configured port {KELLER_PORT}")
        return None, None
    for port_info in _usb_serial_ports():
        port = port_info.device
        bus = _probe_keller(port)
        if bus is not None:
            print(f"  [Keller] detected on {port}")
            return port, bus
    return None, None


def keller_thread(initial_port: str, initial_bus):
    """Keller read loop with automatic reconnection.

    On any read error the state is immediately nulled (so the GUI shows '---'
    rather than silently repeating the last good value) and the thread scans
    for the sensor again before resuming.
    """
    global _keller_ok
    interval = 1.0 / KELLER_POLL_HZ
    port, bus = initial_port, initial_bus

    while not _stop.is_set():
        # ── read loop — exits on error ─────────────────────────────────────
        try:
            while not _stop.is_set():
                t0 = time.time()
                p1   = bus.f73(KELLER_ADDRESS, 1)
                tob1 = bus.f73(KELLER_ADDRESS, 4)
                with _lock:
                    if p1 is not None:
                        _state['keller_pressure_samples'].append(round(p1, 4))
                        _state['keller_pressure_bar'] = p1
                        _state['keller_pressure_t']   = t0
                    if tob1 is not None:
                        _state['keller_temperature_samples'].append(round(tob1, 2))
                    if p1 is not None and tob1 is not None:
                        _p20_chart.append(p1 * P20_REF_K / (tob1 + 273.15))
                _stop.wait(timeout=max(0.0, interval - (time.time() - t0)))

        except Exception as e:
            log_event(f"Keller read error: {e} — reconnecting…")

        # ── null state immediately so GUI shows '---' ──────────────────────
        _keller_ok = False
        with _lock:
            _state['keller_pressure_samples']    = []
            _state['keller_temperature_samples'] = []
            _state['keller_pressure_bar']        = None

        if _stop.is_set():
            break

        # ── scan for the sensor again ──────────────────────────────────────
        _stop.wait(timeout=5.0)
        new_port, new_bus = _detect_keller_bus()
        if new_bus is not None:
            port, bus = new_port, new_bus
            _keller_ok = True
            log_event(f"Keller reconnected · {port}")
        else:
            log_event("Keller not found — will retry")


# ═══════════════════════════════════════════════════════════════════════════════
# MAX31856 THERMOCOUPLE  — SPI helpers (LabJack U3 bit-banged SPI)
# ═══════════════════════════════════════════════════════════════════════════════
#
# The U3's SPI command configures pins and transfers in a single low-level
# operation: every transfer must pass the full pin assignment and mode. Passing
# an empty byte list triggers an internal LabJackPython indexing bug, so we
# only ever call _tc_transfer with at least one byte.

def _tc_transfer(lj, spi_bytes):
    """One SPI transfer with the MAX31856 pin configuration."""
    result = lj.spi(
        SPIBytes=spi_bytes,
        AutoCS=True,
        DisableDirConfig=False,
        SPIMode='A',            # CPOL0, CPHA0 (standard for MAX31856)
        SPIClockFactor=0,
        CSPINNum=TC_FIO_CS,
        CLKPinNum=TC_FIO_SCK,
        MISOPinNum=TC_FIO_SDO,
        MOSIPinNum=TC_FIO_SDI,
    )
    return result['SPIBytes']


def _tc_read_reg(lj, reg):
    return _tc_transfer(lj, [reg & 0x7F, 0x00])[1]


def _tc_read_regs(lj, reg, n):
    return _tc_transfer(lj, [reg & 0x7F] + [0x00] * n)[1:]


def _tc_write_reg(lj, reg, value):
    _tc_transfer(lj, [(reg & 0x7F) | 0x80, value & 0xFF])


def _tc_decode_temp(bytes3):
    """Decode the 19-bit signed linearized TC temperature (0.0078125 °C/LSB)."""
    raw = (bytes3[0] << 16) | (bytes3[1] << 8) | bytes3[2]
    raw >>= 5
    if raw & 0x40000:
        raw -= 0x80000
    return raw * 0.0078125


# CR0 bit map (MAX31856 datasheet):
#   bit7 CMODE      1 = automatic conversion
#   bit5:4 OCFAULT  01 = open-circuit detection enabled, TC resistance < 5 kR
#   bit0 50/60 Hz   1 = 50 Hz mains notch filter  (Bern — the old 0x80 value
#                       left this at 60 Hz, which is wrong for Europe)
CR0_VALUE = 0x80 | 0x10 | 0x01      # = 0x91
CR1_VALUE = 0x03                     # AVGSEL = 1 sample, TC type = K


def _tc_readback_hint(cr0, cr1):
    """Turn a failed CR0/CR1 readback into a likely cause."""
    if cr0 == 0x00 and cr1 == 0x00:
        return "reads all-zero: MAX31856 unpowered, or SDO (FIO4) not connected / shorted to GND"
    if cr0 == 0xFF and cr1 == 0xFF:
        return "reads all-ones: SDO (FIO4) floating or pulled high — chip absent or unpowered"
    return "reads garbage: check SCK/SDI/CS (FIO5-7) wiring, connector seating and ground"


def _tc_init(lj):
    """Configure MAX31856 for automatic conversion, Type-K.

    Returns (ok, cr0, cr1): ok is True if the register readback confirms SPI
    communication is working; cr0/cr1 are what was read back.

    Open-circuit fault detection is deliberately ENABLED here. Without it a
    detached thermocouple returns a plausible-looking number rather than a
    fault — which, now that the heater is under software control, would let
    the interlock be satisfied by a sensor that is no longer attached."""
    _tc_write_reg(lj, REG_CR0, CR0_VALUE)
    _tc_write_reg(lj, REG_CR1, CR1_VALUE)
    time.sleep(0.3)                    # allow first conversion + OC check
    cr0 = _tc_read_reg(lj, REG_CR0)
    cr1 = _tc_read_reg(lj, REG_CR1)
    return (cr0 == CR0_VALUE and cr1 == CR1_VALUE), cr0, cr1


def _tc_try_init(lj, announce_failure):
    """Initialise the MAX31856, logging the outcome. Returns True on success.
    Failures are logged only if announce_failure, so retries don't spam."""
    try:
        ok, cr0, cr1 = _tc_init(lj)
    except Exception as e:
        if announce_failure:
            log_event(f"MAX31856 init error: {e} — retrying every {TC_RETRY_S:g} s")
        return False
    if ok:
        log_event("MAX31856 thermocouple init OK")
        return True
    if announce_failure:
        log_event(f"MAX31856 not responding: CR0=0x{cr0:02X} CR1=0x{cr1:02X} "
                  f"(expected 0x{CR0_VALUE:02X}/0x{CR1_VALUE:02X}) — "
                  f"{_tc_readback_hint(cr0, cr1)}. Retrying every {TC_RETRY_S:g} s")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# LABJACK U3 — VACUUM PRESSURE (FIO2) + THERMOCOUPLE (FIO4-7)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Both LabJack-attached sensors are serviced by a single thread, because they
# share one U3 device handle. FIO2 is analogue (vacuum gauge) and FIO4-7 are
# digital SPI (thermocouple); configIO sets the analogue/digital split once.
#
# _labjack_ok tracks the vacuum channel, _tc_ok tracks the thermocouple. They
# are reported independently in the GUI so a fault on one doesn't mask the other.

def _vacuum_gauge_status(volts: float):
    """Classify a LabJack voltage against the IKR 270 signal ranges.
    Returns None if the reading is valid, otherwise one of the VAC_* strings."""
    if LABJACK_AIN_SAT_V is not None and volts >= LABJACK_AIN_SAT_V:
        return VAC_SATURATED                    # clipped at the LabJack input
    u = volts * VACUUM_DIVIDER_RATIO            # back to gauge-side volts
    if u < VACUUM_GAUGE_ERROR_V:
        return VAC_ERROR
    if u < VACUUM_GAUGE_MIN_V:
        return VAC_UNDER
    if u > VACUUM_GAUGE_MAX_V:
        return VAC_OVER
    return None


def _voltage_to_vacuum_mbar(volts: float):
    """LabJack voltage → chamber pressure in mbar, or None if the gauge
    signal is outside its valid measuring range (never a made-up number)."""
    if _vacuum_gauge_status(volts) is not None:
        return None
    return 10.0 ** (VACUUM_SLOPE * volts + VACUUM_INTERCEPT)


def _check_sense_pins():
    """Return an error string if the optional sense channels clash with
    anything, else None. Checked once at startup."""
    reserved = {HEATER_FIO: "heater gate", LABJACK_FIO2_CHANNEL: "vacuum gauge",
                TC_FIO_SDO: "MAX31856 SDO", TC_FIO_SDI: "MAX31856 SDI",
                TC_FIO_SCK: "MAX31856 SCK", TC_FIO_CS: "MAX31856 CS"}
    seen = {}
    for name, ch in (("HEATER_V_AIN", HEATER_V_AIN), ("HEATER_I_AIN", HEATER_I_AIN)):
        if ch is None:
            continue
        if not isinstance(ch, int) or not 0 <= ch <= 7:
            return f"{name}={ch!r}: must be an FIO number 0-7"
        if ch in reserved:
            return f"{name}=FIO{ch} is already used as the {reserved[ch]}"
        if ch in seen:
            return f"{name} and {seen[ch]} both point at FIO{ch}"
        seen[ch] = name
    return None


def _fio_analog_mask():
    """FIOAnalog bitmask: the vacuum gauge plus any configured sense inputs."""
    mask = 1 << LABJACK_FIO2_CHANNEL
    for ch in (HEATER_V_AIN, HEATER_I_AIN):
        if ch is not None:
            mask |= 1 << ch
    return mask


def heater_command(**kwargs):
    """Thread-safe update of the heater command block, called from the GUI.

    Arming clears the latched trip and resets both integrators. Disarming
    always succeeds and always wins. The inner (temperature) integrator is
    reset only when switching to or from manual; auto ↔ pressure keeps it,
    since both use the same inner loop and a reset would just cause a sag."""
    with _heater_lock:
        if 'armed' in kwargs:
            if kwargs['armed']:
                _heater['armed']       = True
                _heater['armed_at']    = time.time()
                _heater['trip_reason'] = None
                _heater['integral']    = 0.0
                _heater['p_init']      = True
                _heater['d_prev']      = None
                _heater['t_check']     = True
                _heater['t_burst']     = None
            else:
                _heater['armed']    = False
                _heater['armed_at'] = None
                _heater['duty_cmd'] = 0.0
                _heater['t_burst']  = None
        new_mode = kwargs.get('mode')
        if new_mode is not None and new_mode != _heater['mode']:
            if (new_mode == 'manual') != (_heater['mode'] == 'manual'):
                _heater['integral'] = 0.0
                _heater['d_prev']   = None
            if new_mode == 'pressure':
                _heater['p_init'] = True
            _heater['t_burst'] = None
            _heater['t_check'] = new_mode == 'auto'
        if ('setpoint_C' in kwargs and _heater['mode'] != 'pressure'
                and kwargs['setpoint_C'] != _heater['setpoint_C']):
            _heater['t_check'] = True
        for k in ('mode', 'duty_cmd', 'setpoint_C', 'p_target_mbar'):
            if k in kwargs:
                _heater[k] = kwargs[k]


def _hold_integral(t_sp):
    """Integrator value that makes the PI output roughly the duty that holds
    t_sp — used to hand over smoothly after a burst."""
    hold = HEATER_HOLD_DUTY_40C * max(0.0, t_sp - HEATER_HOLD_AMBIENT_C) / max(
        1.0, 40.0 - HEATER_HOLD_AMBIENT_C)
    return (min(HEATER_MAX_DUTY, hold) / PID_KI) if PID_KI > 0 else 0.0


def _temp_burst_step(temp, setpoint):
    """Auto mode: run the burst/coast sequence if one is due.
    Returns the duty to impose, or None to let the PI run."""
    now, msgs, duty = time.time(), [], None
    with _heater_lock:
        h = _heater
        if h['t_check']:
            h['t_check'] = False
            if TEMP_BURST_ENABLE and setpoint - temp >= TEMP_BURST_MIN_STEP_K:
                h.update(t_burst='burst', t_burst_t0=now, t_burst_peak=None)
                msgs.append(f"Temperature burst: full power from {temp:.1f} °C toward "
                            f"{setpoint:.1f} °C")
            elif h['t_burst']:
                h['t_burst'] = None
        stage = h['t_burst']
        if stage == 'burst':
            el     = now - h['t_burst_t0']
            charge = 1.0 - math.exp(-el / HEATER_LAG_S)
            if temp >= setpoint - h['t_brake'] * charge:
                msgs.append(f"Temperature burst: cut after {el:.1f} s at {temp:.1f} °C "
                            f"(brake {h['t_brake']:.1f} K) — coasting")
                h.update(t_burst='coast', t_burst_t0=now, t_burst_peak=temp,
                         t_burst_cut=(temp, charge))
                duty = 0.0
            elif el > TEMP_BURST_MAX_S:
                h.update(t_burst=None, d_prev=None, integral=_hold_integral(setpoint))
                msgs.append(f"Temperature burst: time limit ({TEMP_BURST_MAX_S:g} s) at "
                            f"{temp:.1f} °C — PI resumes; check the thermocouple")
            else:
                duty = HEATER_MAX_DUTY
        elif stage == 'coast':
            # Heater stays off until the TC peaks, even if it passes the
            # setpoint (the PI would be near zero there anyway), so the whole
            # rise is measured.
            h['t_burst_peak'] = max(h['t_burst_peak'], temp)
            past_peak = temp < h['t_burst_peak'] - 0.3
            if past_peak or now - h['t_burst_t0'] > TEMP_COAST_MAX_S:
                note = ""
                cut_T, charge = h['t_burst_cut']
                if past_peak and charge > 0.3:
                    measured = (h['t_burst_peak'] - cut_T) / charge
                    h['t_brake'] = min(10.0, max(1.0, (1 - TEMP_BURST_LEARN) * h['t_brake']
                                                 + TEMP_BURST_LEARN * measured))
                    note = f", brake now {h['t_brake']:.1f} K"
                h.update(t_burst=None, d_prev=None, integral=_hold_integral(setpoint))
                msgs.append(f"Temperature burst: coast done, peak {h['t_burst_peak']:.1f} °C "
                            f"vs setpoint {setpoint:.1f} °C{note} — PI resumes")
            else:
                duty = 0.0
    for m in msgs:
        log_event(m)
    return duty


def _heater_trip(reason: str):
    """Latch the heater off. Cleared only by an explicit disarm→arm cycle."""
    with _heater_lock:
        if _heater['trip_reason'] is None:
            _heater['trip_reason'] = reason
        _heater['armed']       = False
        _heater['armed_at']    = None
        _heater['duty_cmd']    = 0.0
        _heater['duty_actual'] = 0.0
    log_event(f"HEATER TRIP — {reason}")


def _pressure_baseline(h, now):
    """Median log10(p) over the baseline window, skipping the newest seconds.
    None until PRESSURE_BASE_MIN_S of samples are available."""
    ys = sorted(y for t, y in h['p_hist']
                if now - PRESSURE_BASE_WINDOW_S <= t <= now - PRESSURE_BASE_GUARD_S)
    need = PRESSURE_BASE_MIN_S * LABJACK_SAMPLE_HZ
    if len(ys) < need:
        return None
    n = len(ys)
    return ys[n // 2] if n % 2 else 0.5 * (ys[n // 2 - 1] + ys[n // 2])


def _pressure_outer_loop(vac, temp, dt):
    """Outer loop of the cascade: chamber pressure → valve temperature setpoint.

    SEEK (valve shut). The setpoint jumps to PRESSURE_SEEK_START_C, at or just
    below the cracking point, so the inner loop heats at full speed. Once the
    TC is there (and, optionally, has stayed there PRESSURE_SEEK_HOLD_S), the
    setpoint creeps up at PRESSURE_SEEK_RATE_C_MIN. If the target is at or
    below the baseline, the setpoint waits at PRESSURE_HOLD_SHUT_C instead. Meanwhile the
    chamber baseline is measured (median over the last 30 s, allowed to fall
    with the pump-down but never to rise). When the
    filtered pressure rises PRESSURE_OPEN_DEC above that baseline, the valve
    has opened: the baseline is frozen and control passes to TRACK. Creeping
    slowly keeps the setpoint from running far ahead of the lagging valve
    body, which is what would otherwise cause an overshoot on opening.

    TRACK (valve open). PI on log10(p), because chamber pressure moves in
    decades, with target r = log10(target):

        T_sp = T_open − s·KP·(y − y_open) + s·KI·∫clamp(r − y) dt

    with y the filtered log10(p) and s = ±1 from PRESSURE_HEAT_OPENS.
      * Bumpless handover: T_open and y_open are the values at opening.
      * P acts on the measurement, not the error, so changing the target
        causes no setpoint kick — only the integrator responds to it.
      * The integrator input is clamped to ±PRESSURE_ERR_CLAMP_DEC, which caps
        how fast the setpoint can ramp without a separate slew limiter.
      * Output clamped to PRESSURE_OPEN_FLOOR_C … max, so the loop trims flow
        above the valve's minimum step instead of shutting it; the integrator
        stops only when it would push further into a clamp.

    Returns the temperature setpoint in °C (also written to _heater)."""
    y      = math.log10(vac)
    s      = 1.0 if PRESSURE_HEAT_OPENS else -1.0
    tsp_hi = min(PRESSURE_TSP_MAX_C, TEMP_TRIP_C - 5.0)
    now    = time.time()
    msgs   = []
    with _lock:                         # never held together with _heater_lock
        p_up, p_up_t = _state['keller_pressure_bar'], _state['keller_pressure_t']
    if p_up is not None and (p_up_t is None or now - p_up_t > PRESSURE_UP_MAX_AGE_S):
        p_up = None
    shift = _upstream_shift(p_up)

    with _heater_lock:
        h = _heater
        h['p_shift'], h['p_up_bar'] = shift, p_up
        starting = h['p_init'] or h['p_filt'] is None     # start-up logs it itself
        if PRESSURE_UP_ENABLE and not starting and (
                h['p_shift_logged'] is None or abs(shift - h['p_shift_logged']) >= 0.5):
            h['p_shift_logged'] = shift
            msgs.append(f"Pressure loop: upstream {p_up:.3f} bar → temperatures shifted "
                        f"{shift:+.1f} K" if p_up is not None else
                        "Pressure loop: no upstream pressure reading — no upstream shift")

        # ── (re)start: always begin by seeking ────────────────────────────
        if h['p_init'] or h['p_filt'] is None:
            warm  = temp > PRESSURE_SEEK_START_C + shift + PRESSURE_SEEK_BAND_C
            start = min(tsp_hi, max(PRESSURE_SEEK_START_C + shift, temp))
            h['p_hist'].clear()
            h['p_hist'].append((now, y))
            burst = PRESSURE_BURST_ENABLE and temp <= PRESSURE_BURST_MAX_START_C
            h.update(p_init=False, p_phase='seek', p_filt=y, p_y0=y, p_t0=start,
                     p_err=None, p_integral=0.0, setpoint_C=start,
                     p_ramping=warm, p_band_since=None, p_creep=0.0, p_goal=start,
                     p_burst='burst' if burst else None, p_burst_t0=now,
                     p_burst_peak=None,
                     p_override=HEATER_MAX_DUTY if burst else None,
                     p_seek_capped=False, p_base=None,
                     p_warned_target=None,
                     p_pinned_since=None, p_pinned_warned=False)
            if PRESSURE_UP_ENABLE:
                msgs.append("Pressure loop: upstream "
                            + (f"{p_up:.3f} bar → temperatures shifted {shift:+.1f} K"
                               if p_up is not None else "pressure unavailable — no shift"))
                h['p_shift_logged'] = shift
            if h['p_burst']:
                msgs.append(f"Pressure loop: full power until "
                            f"{PRESSURE_BURST_BRAKE_K:g} K below the goal (≥ {start:.1f} °C, "
                            f"set from the target once the baseline is known), coast, then "
                            f"+{PRESSURE_SEEK_RATE_C_MIN:g} °C/min until the valve opens")
            else:
                msgs.append(f"Pressure loop: seeking — T_sp {start:.1f} °C, then "
                            f"+{PRESSURE_SEEK_RATE_C_MIN:g} °C/min until the valve opens")
            if warm:
                msgs.append(f"Pressure loop: valve already at {temp:.1f} °C — if it is "
                            f"open, the baseline will include flow. Let it cool below "
                            f"{PRESSURE_SEEK_START_C + shift:.1f} °C for a clean baseline")
        else:
            h['p_filt'] += dt / (PRESSURE_FILTER_S + dt) * (y - h['p_filt'])

            if h['p_phase'] == 'seek':
                h['p_hist'].append((now, y))
                base = _pressure_baseline(h, now)
                tsp  = h['setpoint_C']
                if base is not None and h['p_base'] is not None:
                    # The chamber only pumps down, so the baseline may fall but
                    # never rise — otherwise a gradual opening drags it upwards
                    # and is never detected.
                    base = min(base, h['p_base'])
                if base is not None:
                    h['p_base'] = base
                    h['p_err']  = math.log10(h['p_target_mbar']) - h['p_filt']
                    _check_target(h, msgs)

                if base is not None and h['p_filt'] - base > PRESSURE_OPEN_DEC:
                    # Opened — freeze the baseline, hand over bumplessly.
                    if h['p_burst']:
                        _end_burst(h, tsp, msgs, f"valve opened during {h['p_burst']}")
                    h.update(p_phase='track', p_y0=h['p_filt'], p_t0=tsp,
                             p_integral=0.0, p_ramping=False)
                    msgs.append(f"Pressure loop: valve opened at T_sp {tsp:.2f} °C "
                                f"(TC {temp:.2f} °C), baseline {10 ** base:.2e} mbar — "
                                f"now controlling to {h['p_target_mbar']:.2e} mbar")
                elif base is not None and h['p_target_mbar'] <= 10 ** base:
                    # Target at/below baseline: opening would only move away from
                    # it, so park safely below the cracking point (resumes if raised).
                    if h['p_burst']:
                        h.update(p_burst=None, p_override=None)
                    h.update(setpoint_C=min(tsp, PRESSURE_HOLD_SHUT_C + shift), p_creep=0.0,
                             p_ramping=False, p_band_since=None)
                else:
                    goal = min(tsp_hi, _seek_goal(h))
                    if h['p_goal'] is None or abs(goal - h['p_goal']) > 0.05:
                        if base is not None:
                            msgs.append(f"Pressure loop: seek goal {goal:.1f} °C for target "
                                        f"{h['p_target_mbar']:.2e} mbar (baseline "
                                        f"{10 ** base:.2e})")
                        h['p_goal'] = goal
                    tsp = min(tsp_hi, goal + h['p_creep'])
                    if h['p_burst']:
                        _burst_step(h, temp, tsp, now, msgs)
                    else:
                        if not h['p_ramping'] and temp >= tsp - PRESSURE_SEEK_BAND_C:
                            if h['p_band_since'] is None:
                                h['p_band_since'] = now
                        if (not h['p_ramping'] and h['p_band_since'] is not None
                                and now - h['p_band_since'] >= PRESSURE_SEEK_HOLD_S):
                            h['p_ramping'] = True
                            msgs.append(f"Pressure loop: at {temp:.1f} °C, valve shut — "
                                        f"creeping +{PRESSURE_SEEK_RATE_C_MIN:g} °C/min")
                        if h['p_ramping']:
                            cap = min(max(PRESSURE_SEEK_MAX_C, goal), tsp_hi)
                            h['p_creep'] += PRESSURE_SEEK_RATE_C_MIN / 60.0 * dt
                            tsp = min(cap, goal + h['p_creep'])
                            if tsp >= cap and not h['p_seek_capped']:
                                h['p_seek_capped'] = True
                                msgs.append(f"Pressure loop: reached {cap:g} °C and the "
                                            f"valve has not opened — holding there")
                    h['setpoint_C'] = tsp

        if h['p_phase'] == 'seek':
            tsp = h['setpoint_C']
        else:
            tsp = _pressure_track(h, s, tsp_hi, now, dt, msgs)

    for m in msgs:
        log_event(m)
    return tsp


def _upstream_shift(p_up):
    """Temperature shift for upstream pressure p_up (bar abs.), K."""
    if not PRESSURE_UP_ENABLE or p_up is None:
        return 0.0
    shift = -PRESSURE_UP_K_PER_BAR * (p_up - PRESSURE_UP_REF_BAR)
    return max(-PRESSURE_UP_MAX_SHIFT_K, min(PRESSURE_UP_MAX_SHIFT_K, shift))


def _seek_goal(h):
    """Seek goal temperature: PRESSURE_SEEK_START_C, raised by the feedforward
    map for larger targets, plus the upstream-pressure shift
    (caller holds _heater_lock)."""
    goal = PRESSURE_SEEK_START_C
    if PRESSURE_FF_ENABLE and h['p_base'] is not None:
        rise = h['p_target_mbar'] - 10 ** h['p_base']
        if rise > 0:
            goal = max(goal, PRESSURE_FF_REF_C + PRESSURE_FF_EFOLD_K * math.log(
                PRESSURE_FF_FRACTION * rise / PRESSURE_FF_REF_RISE_MBAR))
    return min(goal + h['p_shift'], PRESSURE_FF_MAX_C)


def _burst_step(h, temp, tsp, now, msgs):
    """Advance the burst/coast sequence toward seek setpoint tsp
    (caller holds _heater_lock)."""
    stage = h['p_burst']
    if stage == 'burst':
        h['p_override'] = HEATER_MAX_DUTY
        if temp >= tsp - PRESSURE_BURST_BRAKE_K:
            msgs.append(f"Pressure loop: burst done after {now - h['p_burst_t0']:.1f} s "
                        f"at {temp:.1f} °C (goal {tsp:.1f} °C) — coasting")
            h.update(p_burst='coast', p_burst_t0=now, p_burst_peak=temp, p_override=0.0)
        elif now - h['p_burst_t0'] > PRESSURE_BURST_MAX_S:
            _end_burst(h, tsp, msgs, f"burst time limit ({PRESSURE_BURST_MAX_S:g} s) "
                                     f"reached at {temp:.1f} °C — check the thermocouple")
    elif stage == 'coast':
        h['p_override'] = 0.0
        h['p_burst_peak'] = max(h['p_burst_peak'], temp)
        if (temp < h['p_burst_peak'] - 0.3        # clearly past the peak (TC noise ~0.05 K)
                or temp >= tsp - PRESSURE_SEEK_BAND_C
                or now - h['p_burst_t0'] > PRESSURE_COAST_MAX_S):
            _end_burst(h, tsp, msgs, f"coast done, TC peaked at {h['p_burst_peak']:.1f} °C")


def _end_burst(h, tsp, msgs, why):
    """Hand heating back to the temperature PI, preloaded near holding power."""
    h.update(p_burst=None, p_override=None, d_prev=None, integral=_hold_integral(tsp))
    msgs.append(f"Pressure loop: {why} — temperature control resumes")


def _check_target(h, msgs):
    """Warn once per target value if the valve cannot hold it
    (caller holds _heater_lock; needs a measured baseline)."""
    tgt = h['p_target_mbar']
    if h['p_base'] is None or h['p_warned_target'] == tgt:
        return
    base   = 10 ** h['p_base']
    lowest = base + PRESSURE_MIN_STEP_MBAR
    if tgt < lowest:
        h['p_warned_target'] = tgt
        if tgt <= base and h['p_phase'] == 'seek':
            msgs.append(f"Pressure loop: target {tgt:.2e} mbar is at or below the "
                        f"chamber baseline {base:.2e} mbar — keeping the valve shut "
                        f"at {PRESSURE_HOLD_SHUT_C + h['p_shift']:.1f} °C. "
                        f"The lowest holdable pressure is ≈ {lowest:.1e} mbar")
        else:
            msgs.append(f"Pressure loop: target {tgt:.2e} mbar is below the lowest "
                        f"pressure the valve can hold once open (baseline {base:.2e} + "
                        f"step ~{PRESSURE_MIN_STEP_MBAR:.1e} ≈ {lowest:.1e} mbar) — "
                        f"expect it to sit above target at the "
                        f"{PRESSURE_OPEN_FLOOR_C + h['p_shift']:.1f} °C floor")


def _pressure_track(h, s, tsp_hi, now, dt, msgs):
    """TRACK step of the outer loop (caller holds _heater_lock)."""
    tsp_lo = max(PRESSURE_TSP_MIN_C, PRESSURE_OPEN_FLOOR_C + h['p_shift'])
    r   = math.log10(h['p_target_mbar'])
    err = r - h['p_filt']
    h['p_err'] = err
    _check_target(h, msgs)

    e_i = 0.0 if abs(err) < PRESSURE_DEADBAND_DEC else err
    e_i = max(-PRESSURE_ERR_CLAMP_DEC, min(PRESSURE_ERR_CLAMP_DEC, e_i))

    def raw_for(integral):
        return (h['p_t0'] - s * PRESSURE_KP * (h['p_filt'] - h['p_y0'])
                + s * PRESSURE_KI * integral)

    integral = h['p_integral'] + e_i * dt
    raw      = raw_for(integral)
    push     = s * e_i                         # >0: integrator raising T_sp
    if (raw > tsp_hi and push > 0) or (raw < tsp_lo and push < 0):
        integral = h['p_integral']             # don't wind further into a clamp
        raw      = raw_for(integral)
    h['p_integral'] = integral
    tsp = max(tsp_lo, min(tsp_hi, raw))
    h['setpoint_C'] = tsp

    # Authority check — is the loop asking for more than it can have?
    at_hi = tsp >= tsp_hi and push > 0
    at_lo = tsp <= tsp_lo and push < 0
    if at_hi or at_lo:
        if h['p_pinned_since'] is None:
            h['p_pinned_since'] = now
        elif (not h['p_pinned_warned']
              and now - h['p_pinned_since'] > PRESSURE_NO_AUTHORITY_S):
            h['p_pinned_warned'] = True
            where = "below" if err > 0 else "above"
            limit = (f"maximum ({tsp_hi:.0f} °C)" if at_hi else
                     f"floor ({tsp_lo:g} °C, kept to hold the valve open)")
            msgs.append(f"Pressure loop: setpoint held at {limit} for "
                        f"{PRESSURE_NO_AUTHORITY_S/60:.0f} min and pressure is still "
                        f"{where} target")
    else:
        h['p_pinned_since']  = None
        h['p_pinned_warned'] = False
    return tsp


def _heater_compute_duty(temp, tc_healthy, dt,
                         vac=None, vac_status=None, vac_healthy=True):
    """Evaluate all interlocks and return the duty cycle to apply (0-1).

    Returns 0.0 unless every interlock is satisfied. Interlocks are checked
    before the control law, never after."""
    with _heater_lock:
        armed    = _heater['armed']
        mode     = _heater['mode']
        duty_cmd = _heater['duty_cmd']
        setpoint = _heater['setpoint_C']
        armed_at = _heater['armed_at']
        p_init   = _heater['p_init']

    if not armed:
        return 0.0

    # Interlock 1 — no trustworthy temperature means no heat.
    if not tc_healthy or temp is None:
        _heater_trip("thermocouple unavailable or faulted")
        return 0.0

    # Interlock 2 — over-temperature.
    if temp > TEMP_TRIP_C:
        _heater_trip(f"over-temperature {temp:.1f} °C > {TEMP_TRIP_C:.0f} °C")
        return 0.0

    # Interlock 3 — maximum unattended run time.
    if armed_at is not None and (time.time() - armed_at) > HEATER_MAX_RUN_S:
        _heater_trip(f"maximum armed time ({HEATER_MAX_RUN_S/60:.0f} min) reached")
        return 0.0

    # Interlock 4 — chamber over-pressure.
    if mode == 'pressure' or PRESSURE_TRIP_ALL_MODES:
        if vac_status in VAC_HIGH_STATES:
            _heater_trip(f"chamber pressure too high to measure ({vac_status})")
            return 0.0
        if vac is not None and vac > PRESSURE_TRIP_MBAR:
            _heater_trip(f"chamber over-pressure {vac:.2e} mbar > "
                         f"{PRESSURE_TRIP_MBAR:.0e} mbar")
            return 0.0

    if mode == 'manual':
        return max(0.0, min(HEATER_MAX_DUTY, duty_cmd))

    if mode == 'pressure':
        # Interlock 5 — pressure mode is blind without a live gauge.
        if not vac_healthy:
            _heater_trip(f"no valid chamber pressure for {PRESSURE_BAD_READS_TO_TRIP} "
                         f"reads ({vac_status or 'no reading'}) — pressure mode "
                         f"needs a live gauge")
            return 0.0
        if vac is not None:
            setpoint = _pressure_outer_loop(vac, temp, dt)
        elif p_init:
            return 0.0      # not initialised yet — don't heat on a stale setpoint
        # else: a single missed read — hold the last setpoint / override
        with _heater_lock:
            override = _heater['p_override']
        if override is not None:                # burst / coast: duty set directly
            return max(0.0, min(HEATER_MAX_DUTY, override))

    if mode == 'auto':
        burst_duty = _temp_burst_step(temp, setpoint)
        if burst_duty is not None:
            return max(0.0, min(HEATER_MAX_DUTY, burst_duty))

    # ── 'auto' / 'pressure': PI(D) on valve temperature, with anti-windup ──
    error = setpoint - temp
    with _heater_lock:
        prev = _heater['d_prev']
        if prev is None or PID_KD == 0.0:
            _heater['d_filt'] = 0.0
        else:
            rate = (temp - prev) / dt
            _heater['d_filt'] += dt / (PID_D_FILTER_S + dt) * (rate - _heater['d_filt'])
        _heater['d_prev'] = temp
        integral = _heater['integral'] + error * dt
        duty     = (PID_KP * error + PID_KI * integral
                    - PID_KD * _heater['d_filt'])
        clamped  = max(0.0, min(HEATER_MAX_DUTY, duty))
        # Only accumulate when not saturated — keeps the integrator honest.
        if duty == clamped:
            _heater['integral'] = integral
    return clamped


def labjack_thread():
    """Owns the U3 handle. Does everything on the device: heater output,
    vacuum gauge, thermocouple. Single-threaded by design — the U3 handle is
    not safe to share, and the heater must never wait behind another thread.

    Runs at HEATER_TICK_HZ so the software time-proportioning output has
    reasonable resolution; sensors are sub-sampled to LABJACK_SAMPLE_HZ."""
    global _labjack_ok, _tc_ok
    if not LABJACK_AVAILABLE:
        return
    tick        = 1.0 / HEATER_TICK_HZ
    sensor_gap  = 1.0 / LABJACK_SAMPLE_HZ

    while not _stop.is_set():
        # ── connect / configure ────────────────────────────────────────────
        try:
            lj = u3.U3()
            # NOTE: getCalibrationData() is required for accurate getAIN()
            # voltage scaling on the vacuum channel. If a firmware/library
            # mismatch makes it raise, we fall back to uncalibrated reads.
            try:
                lj.getCalibrationData()
            except Exception as e:
                log_event(f"LabJack calibration read failed ({e}) — using nominal scaling")
            # FIO2 analogue (vacuum) plus any heater sense inputs; FIO0 and
            # FIO4-7 digital. NumberOfTimersEnabled=0 is important: no timer
            # may be allowed to claim FIO4 (MAX31856 SDO) — see the header.
            lj.configIO(FIOAnalog=_fio_analog_mask(), NumberOfTimersEnabled=0)

            # Heater OFF before anything else happens on this device.
            lj.setDOState(HEATER_FIO, 0)
            with _heater_lock:
                _heater['armed']       = False
                _heater['duty_cmd']    = 0.0
                _heater['duty_actual'] = 0.0

            # U3 firmware watchdog: if we stop talking to the device for
            # LJ_WATCHDOG_S, the U3 itself drives FIO0 low. This is what
            # protects the heater if this program crashes or is killed.
            try:
                lj.watchdog(ResetOnTimeout=False, SetDIOStateOnTimeout=True,
                            TimeoutPeriod=LJ_WATCHDOG_S,
                            DIOState=0, DIONumber=HEATER_FIO)
                log_event(f"U3 watchdog armed — FIO0 low after {LJ_WATCHDOG_S} s silence")
            except Exception as e:
                log_event(f"WARNING: U3 watchdog not armed ({e}) — do not leave heater unattended")

            _labjack_ok = True
            sense = "".join(f" · FIO{ch} {nm}" for nm, ch in
                            (("V-sense", HEATER_V_AIN), ("I-sense", HEATER_I_AIN))
                            if ch is not None)
            rng = "0-3.6 V" if VACUUM_AIN_SPECIAL else "0-2.44 V"
            log_event(f"LabJack connected  FIO0 heater · FIO2 analog ({rng}){sense} · FIO4-7 SPI")
        except Exception as e:
            _labjack_ok = False
            _tc_ok = False
            log_event(f"LabJack connect failed: {e} — retry in 5 s")
            _stop.wait(timeout=5)
            continue

        # ── initialise the thermocouple IC ─────────────────────────────────
        _tc_ok       = _tc_try_init(lj, announce_failure=True)
        tc_announced = not _tc_ok      # failure already logged
        tc_next_try  = time.time() + TC_RETRY_S

        # ── tick loop — breaks on error to trigger reconnect ───────────────
        next_sensor  = 0.0          # time.time() of the next sensor read
        cycle_start  = time.time()  # start of the current PWM period
        duty         = 0.0
        out_high     = False
        bad_tc_reads = 0
        last_ctrl    = time.time()
        vac_status   = "startup"    # last gauge status, so changes are logged once
        bad_vac_reads = 0           # consecutive invalid gauge readings
        win_len      = max(1, int(round(HEATER_PWM_PERIOD_S * HEATER_TICK_HZ)))
        v_win        = deque(maxlen=win_len)   # measured V over one period
        i_win        = deque(maxlen=win_len)   # measured I over one period
        low_i_ticks  = 0            # gate ON but little current
        stray_i_ticks = 0           # gate OFF but current flowing
        i_on_expect  = HEATER_V_RAIL / HEATER_R_OHM

        try:
            while not _stop.is_set():
                t0 = time.time()

                # ── heater V / I: calculated every tick, measured if wired ─
                # Read BEFORE this tick's output write, so the samples belong
                # to out_high as it is right now.
                v_now  = HEATER_V_RAIL if out_high else 0.0
                i_now  = v_now / HEATER_R_OHM
                rail = v_meas = i_meas = None
                try:
                    if HEATER_V_AIN is not None:
                        rail   = lj.getAIN(HEATER_V_AIN) * HEATER_V_SCALE + HEATER_V_OFFSET
                        v_meas = rail if out_high else 0.0
                        v_win.append(v_meas)
                    if HEATER_I_AIN is not None:
                        i_meas = lj.getAIN(HEATER_I_AIN) * HEATER_I_SCALE + HEATER_I_OFFSET
                        i_win.append(i_meas)
                except Exception as e:
                    log_event(f"Heater sense read error: {e}")
                    break

                if i_meas is not None:
                    if out_high:
                        low_i_ticks = low_i_ticks + 1 if i_meas < 0.5 * i_on_expect else 0
                    else:
                        stray_i_ticks = stray_i_ticks + 1 if i_meas > 0.5 * i_on_expect else 0
                    if low_i_ticks == HEATER_SENSE_TICKS:
                        log_event(f"WARNING: gate ON but heater current {i_meas:.3f} A "
                                  f"(expected ~{i_on_expect:.3f} A) — SW171 off, "
                                  f"24 V missing, or element open?")
                    if stray_i_ticks == HEATER_SENSE_TICKS:
                        log_event(f"WARNING: {i_meas:.3f} A flowing with gate OFF — "
                                  f"Q171 may be shorted. Software cannot stop this: "
                                  f"open SW171 / switch off 24 V.")
                        _heater_trip("heater current with gate OFF")

                with _heater_lock:
                    _heater['out_high']    = out_high
                    _heater['v_now']       = v_now
                    _heater['i_now']       = i_now
                    _heater['rail_meas']   = rail
                    _heater['v_meas']      = v_meas
                    _heater['i_meas']      = i_meas
                    _heater['v_meas_mean'] = sum(v_win) / len(v_win) if v_win else None
                    _heater['i_meas_mean'] = sum(i_win) / len(i_win) if i_win else None

                # ── sensors, sub-sampled ───────────────────────────────────
                if t0 >= next_sensor:
                    next_sensor = t0 + sensor_gap

                    # vacuum gauge (analogue)
                    try:
                        raw  = lj.getAIN(LABJACK_FIO2_CHANNEL,
                                         32 if VACUUM_AIN_SPECIAL else 31)
                        mbar = _voltage_to_vacuum_mbar(raw)
                        status = _vacuum_gauge_status(raw)
                        if status != vac_status:
                            if status is not None:
                                log_event(f"Vacuum gauge {status} — "
                                          f"{raw:.3f} V at LabJack, "
                                          f"{raw * VACUUM_DIVIDER_RATIO:.2f} V at gauge")
                            elif vac_status != "startup":
                                log_event("Vacuum gauge back in range")
                            vac_status = status
                        with _lock:
                            _state['vacuum_chamber_mbar'] = mbar
                            _state['vacuum_status']       = status
                            _state['vacuum_gauge_V']      = raw * VACUUM_DIVIDER_RATIO
                            _vac_chart.append(mbar)
                        bad_vac_reads = 0 if mbar is not None else bad_vac_reads + 1
                        _labjack_ok = True
                    except Exception as e:
                        log_event(f"LabJack vacuum read error: {e}")
                        break   # reconnect the whole device

                    # thermocouple not talking: retry init periodically.
                    # (_tc_init blocks ~0.3 s; harmless, as the heater is
                    # already forced off whenever the thermocouple is down.)
                    if not _tc_ok and t0 >= tc_next_try:
                        tc_next_try = t0 + TC_RETRY_S
                        _tc_ok = _tc_try_init(lj, announce_failure=not tc_announced)
                        tc_announced = not _tc_ok
                        if _tc_ok:
                            bad_tc_reads = 0

                    # thermocouple (SPI) — only if init succeeded
                    if _tc_ok:
                        try:
                            fault = _tc_read_reg(lj, REG_FAULTSR)
                            temp  = _tc_decode_temp(_tc_read_regs(lj, REG_LTCBH, 3))
                            with _lock:
                                _state['tc_fault'] = fault
                                # On any active fault, don't trust the temperature
                                _state['te_temperature_degC'] = temp if fault == 0 else None
                                if fault == 0:
                                    _te_chart.append(temp)
                            bad_tc_reads = 0 if fault == 0 else bad_tc_reads + 1
                        except Exception as e:
                            _tc_ok = False
                            bad_tc_reads += 1
                            with _lock:
                                _state['te_temperature_degC'] = None
                                _state['tc_fault'] = None
                            log_event(f"MAX31856 read error: {e} — "
                                      f"re-initialising every {TC_RETRY_S:g} s")
                            tc_announced = True
                            tc_next_try  = t0 + TC_RETRY_S

                    # ── control law ────────────────────────────────────────
                    with _lock:
                        temp_now = _state['te_temperature_degC']
                        vac_now  = _state['vacuum_chamber_mbar']
                        vac_st   = _state['vacuum_status']
                    healthy = _tc_ok and bad_tc_reads < TC_BAD_READS_TO_TRIP
                    dt      = max(1e-3, t0 - last_ctrl)
                    last_ctrl = t0
                    duty = _heater_compute_duty(
                        temp_now, healthy, dt,
                        vac=vac_now, vac_status=vac_st,
                        vac_healthy=bad_vac_reads < PRESSURE_BAD_READS_TO_TRIP)
                    with _heater_lock:
                        _heater['duty_actual'] = duty
                        i_mean = _heater['i_meas_mean']
                    if i_mean is None:
                        i_mean = duty * i_on_expect
                    with _lock:
                        _heat_chart.append(i_mean)

                # ── time-proportioning output on FIO0 ──────────────────────
                # Plain digital toggling, no timers: FIO4-7 stay free for SPI.
                phase = (t0 - cycle_start) % HEATER_PWM_PERIOD_S
                if t0 - cycle_start >= HEATER_PWM_PERIOD_S:
                    cycle_start += HEATER_PWM_PERIOD_S * \
                        math.floor((t0 - cycle_start) / HEATER_PWM_PERIOD_S)
                want_high = (duty > 0.0) and (phase < duty * HEATER_PWM_PERIOD_S)
                if want_high != out_high:
                    try:
                        lj.setDOState(HEATER_FIO, 1 if want_high else 0)
                        out_high = want_high
                    except Exception as e:
                        log_event(f"Heater output write error: {e}")
                        break

                _stop.wait(timeout=max(0.0, tick - (time.time() - t0)))
        finally:
            _labjack_ok = False
            _tc_ok = False
            with _lock:
                _state['vacuum_chamber_mbar']   = None
                _state['te_temperature_degC']   = None
                _state['tc_fault']              = None
                _state['vacuum_status']         = None
                _state['vacuum_gauge_V']        = None
            with _heater_lock:
                _heater['armed']       = False
                _heater['duty_cmd']    = 0.0
                _heater['duty_actual'] = 0.0
                _heater['p_init']      = True
                _heater.update(out_high=False, v_now=0.0, i_now=0.0,
                               rail_meas=None, v_meas=None, i_meas=None,
                               v_meas_mean=None, i_meas_mean=None)
            # Belt and braces on the way out: force the gate low, then let go
            # of the watchdog so the device isn't left armed for the next user.
            try:
                lj.setDOState(HEATER_FIO, 0)
            except Exception:
                pass
            try:
                lj.watchdog(ResetOnTimeout=False, SetDIOStateOnTimeout=False,
                            TimeoutPeriod=LJ_WATCHDOG_S,
                            DIOState=0, DIONumber=HEATER_FIO)
            except Exception:
                pass
            try:
                lj.close()
            except Exception:
                pass
            if not _stop.is_set():
                log_event("LabJack disconnected — heater forced off, reconnecting")


# ═══════════════════════════════════════════════════════════════════════════════
# CSV LOGGING THREAD  — drift-free cadence, immediate first row
#
# One output file per session: te-sensor_<ts>.csv
#
# Columns:
#   timestamp, keller_pressure_bar (mean), keller_temperature_degC (mean),
#   n_keller_samples, vacuum_chamber_mbar, te_temperature_degC, tc_fault,
#   heater_duty, heater_mode, heater_setpoint_degC, heater_V/I (calc + meas),
#   pressure_target_mbar, vacuum_status
# ═══════════════════════════════════════════════════════════════════════════════

def logger_thread():
    """CSV writer. Encoding is explicit UTF-8 — Windows would otherwise use
    cp1252 — and the events text is additionally reduced to ASCII, so any
    reader (Excel, LOG-PLOTTER.py, default-encoding open()) copes.

    A failed row never kills the thread: the error is shown in the GUI
    ([CSV:ERR] plus an event-log line), the row's events are kept for the
    next attempt, and logging resumes as soon as a write succeeds."""
    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'timestamp',
            'keller_pressure_bar',      # mean over interval (blank if no samples)
            'keller_temperature_degC',  # mean over interval (blank if no samples)
            'n_keller_samples',         # number of samples averaged
            'vacuum_chamber_mbar',
            'te_temperature_degC',
            'tc_fault',                 # MAX31856 fault register (0 = OK)
            'heater_duty',              # 0-1, actual applied duty
            'heater_mode',              # off / manual / auto
            'heater_setpoint_degC',     # auto: operator's; pressure: outer loop's
            'heater_V_mean_calc',       # V, duty × rail
            'heater_I_mean_calc',       # A, duty × rail / R
            'heater_V_mean_meas',       # V, blank unless HEATER_V_AIN set
            'heater_I_mean_meas',       # A, blank unless HEATER_I_AIN set
            'pressure_target_mbar',     # blank unless mode is pressure
            'vacuum_status',            # blank = valid reading
            'heater_duty_cmd',          # 0-1, operator's manual duty (blank unless manual)
            'events',                   # event-log lines since the previous row, ' | '-joined
            'pressure_baseline_mbar',   # pressure mode: chamber baseline, frozen once the valve opens
        ])
        f.flush()

        def write_row():
            global _csv_ok
            with _lock:
                p_samp = _state['keller_pressure_samples']
                t_samp = _state['keller_temperature_samples']
                p_mean = round(sum(p_samp) / len(p_samp), 4) if p_samp else None
                t_mean = round(sum(t_samp) / len(t_samp), 2) if t_samp else None
                n_k    = len(p_samp)
                _state['keller_pressure_samples']    = []
                _state['keller_temperature_samples'] = []

                vac     = _state['vacuum_chamber_mbar']
                te_temp = _state['te_temperature_degC']
                fault   = _state['tc_fault']
                vac_st  = _state['vacuum_status'] or ''
                taken   = list(_events_pending)
                _events_pending.clear()
            events = ' | '.join(_ascii(e) for e in taken)

            with _heater_lock:
                h_duty = round(_heater['duty_actual'], 4)
                h_mode = _heater['mode'] if _heater['armed'] else 'off'
                h_set  = (round(_heater['setpoint_C'], 2)
                          if _heater['mode'] in ('auto', 'pressure') else '')
                v_calc = round(_heater['duty_actual'] * HEATER_V_RAIL, 3)
                i_calc = round(_heater['duty_actual'] * HEATER_V_RAIL / HEATER_R_OHM, 4)
                v_m    = _heater['v_meas_mean']
                i_m    = _heater['i_meas_mean']
                v_m    = round(v_m, 3) if v_m is not None else ''
                i_m    = round(i_m, 4) if i_m is not None else ''
                in_p   = _heater['mode'] == 'pressure' and not _heater['p_init']
                p_tgt  = _heater['p_target_mbar'] if in_p else ''
                p_base = (10 ** _heater['p_base']
                          if in_p and _heater['p_base'] is not None else '')
                d_cmd  = (round(_heater['duty_cmd'], 4)
                          if _heater['mode'] == 'manual' else '')

            ts = datetime.now().isoformat(timespec='milliseconds')
            try:
                writer.writerow([
                    ts, p_mean, t_mean, n_k, vac, te_temp,
                    fault if fault is not None else '',
                    h_duty, h_mode, h_set,
                    v_calc, i_calc, v_m, i_m, p_tgt, vac_st,
                    d_cmd, events, p_base,
                ])
                f.flush()
            except Exception as e:
                with _lock:                       # keep the events for next time
                    _events_pending[:0] = taken
                if _csv_ok is not False:          # report once per outage
                    _csv_ok = False
                    log_event(f"CSV WRITE FAILED — {type(e).__name__}: {e} — "
                              f"data is NOT being logged, retrying every row")
                return
            if _csv_ok is False:
                log_event("CSV logging resumed")
            _csv_ok = True

        start = time.time()
        n = 0
        while not _stop.is_set():
            write_row()
            n += 1
            target = start + n * LOG_INTERVAL_S
            sleep_for = target - time.time()
            if sleep_for < 0:
                n = max(n, math.ceil((time.time() - start) / LOG_INTERVAL_S))
                target = start + n * LOG_INTERVAL_S
                sleep_for = max(0.0, target - time.time())
            _stop.wait(timeout=sleep_for)

        write_row()        # final row: captures the shutdown events


# ═══════════════════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════════════════
BG     = "#0c0c0c"
TEXT   = "#d0d0d0"
BRIGHT = "#ffffff"
DIM    = "#505050"
BORDER = "#2a2a2a"
WARN   = "#ff4040"
GRID   = "#1a1a1a"
REF    = "#8a8a8a"   # dashed target / setpoint lines
TEMP_LINE = "#ff2a2a"   # TE valve temperature curve (red)
VAC_LINE  = "#2f8cff"   # vacuum chamber pressure curve (blue)
P20_LINE  = "#7ed957"   # upstream P20 curve (light green)

# Operator-facing mode names. Internal values (and the CSV heater_mode
# column) stay 'manual' / 'auto' / 'pressure'.
MODE_LABELS = {'manual': "manual", 'auto': "auto (T)", 'pressure': "auto (P)"}


class TEGui:
    """Single 'Live Log' window: device status, live readouts (including heater
    voltage and current), heater controls, strip charts, and an event log."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TE Valve — Live Log")
        self.root.configure(bg=BG)
        screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"800x{max(600, min(1100, screen_h - 90))}+20+10")
        self.root.minsize(700, 600)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

        M = ("Consolas", "Menlo", "Courier New", "DejaVu Sans Mono", "monospace")
        self.f = self._pick_font(M, 11)

        self._build_log_window()
        self.root.after(150, self._poll)

    def _pick_font(self, families, size, weight="normal"):
        available = set(tkfont.families())
        fam = next((f for f in families if f in available), families[-1])
        return tkfont.Font(family=fam, size=size, weight=weight)

    def _chart(self, parent, title):
        # Title is drawn inside the graph (saves a text row per graph). Small
        # requested height + expand: the graphs share whatever height the
        # window has left, instead of pushing the event log off-screen.
        row = len(parent.grid_slaves())
        c = tk.Canvas(parent, bg=BG, height=30,
                      highlightbackground=BORDER, highlightthickness=1)
        c.grid(row=row, column=0, sticky="nsew", pady=(0, 3))
        parent.rowconfigure(row, weight=1, uniform="charts")   # equal share, shrink evenly
        c.title = f"{title}  ·  last {CHART_SECONDS}s"
        return c

    def _build_log_window(self):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=8, pady=8)

        self.status_text = tk.Text(outer, bg=BG, fg=TEXT, font=self.f,
                                   height=12, bd=0, highlightthickness=0,
                                   state="disabled", wrap="none", cursor="arrow")
        self.status_text.pack(fill="x")
        self.status_text.tag_config("bright", foreground=BRIGHT)
        self.status_text.tag_config("dim",    foreground=DIM)
        self.status_text.tag_config("ok",     foreground=BRIGHT)
        self.status_text.tag_config("err",    foreground=WARN)

        self._build_heater_panel(outer)

        # Bottom block is packed BEFORE the graphs so it always keeps its space.
        tk.Label(outer, text=f"─── log: {LOG_FILE}",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(side="bottom", fill="x")
        self.logtext = tk.Text(outer, bg=BG, fg=TEXT, font=self.f,
                               height=7, bd=0, highlightthickness=0,
                               state="disabled", wrap="word", cursor="arrow")
        self.logtext.pack(side="bottom", fill="x")
        tk.Label(outer, text="─── event log",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(side="bottom", fill="x")

        charts = tk.Frame(outer, bg=BG)
        charts.pack(fill="both", expand=True)
        charts.columnconfigure(0, weight=1)
        i_src = "measured" if HEATER_I_AIN is not None else "calculated"
        self.p20_canvas = self._chart(charts, "upstream P20 (bar, referred to 20 °C)")
        self.vac_canvas = self._chart(charts, "vacuum chamber (mbar, log)   dashed = target")
        self.te_canvas  = self._chart(charts, "TE valve temperature (°C)   dashed = setpoint")
        self.heat_canvas = self._chart(
            charts, f"heater current (A, {i_src}, {HEATER_PWM_PERIOD_S:g} s mean)")

    # ── heater panel ──────────────────────────────────────────────────────
    def _entry(self, parent, label, initial, width=8):
        lbl = tk.Label(parent, text=label, font=self.f, fg=DIM, bg=BG)
        lbl.pack(side="left")
        e = tk.Entry(parent, width=width, font=self.f, bg="#1a1a1a",
                     fg=BRIGHT, insertbackground=BRIGHT, bd=0,
                     highlightthickness=1, highlightbackground=BORDER,
                     disabledbackground=BG, disabledforeground=DIM)
        e.insert(0, initial)
        e.pack(side="left", padx=(2, 12))
        e.bind("<Return>", lambda _ev: self._send_update())
        e.label = lbl
        return e

    def _on_mode(self):
        self._update_inputs()
        self._send_update()

    def _update_inputs(self):
        """Enable only the input that belongs to the selected mode."""
        active = {'manual': self.duty_entry,
                  'auto': self.sp_entry,
                  'pressure': self.p_entry}[self.mode_var.get()]
        for e in (self.duty_entry, self.sp_entry, self.p_entry):
            on = e is active
            e.configure(state="normal" if on else "disabled",
                        highlightbackground=BRIGHT if on else BORDER)
            e.label.configure(fg=TEXT if on else DIM)

    def _build_heater_panel(self, parent):
        tk.Label(parent, text="─── heater  (FIO0 → Q171)   SW171 must be enabled",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(fill="x")

        btn = dict(bg="#1a1a1a", fg=TEXT, activebackground="#303030",
                   activeforeground=BRIGHT, font=self.f, bd=0,
                   highlightthickness=1, highlightbackground=BORDER,
                   padx=8, pady=2)

        row1 = tk.Frame(parent, bg=BG)
        row1.pack(fill="x", pady=(2, 2))
        self.arm_btn = tk.Button(row1, text="ARM", width=7,
                                 command=self._toggle_arm, **btn)
        self.arm_btn.pack(side="left", padx=(0, 10))

        self.mode_var = tk.StringVar(value="manual")
        for val, label in MODE_LABELS.items():
            tk.Radiobutton(row1, text=label, value=val, variable=self.mode_var,
                           command=self._on_mode, font=self.f, fg=TEXT, bg=BG,
                           selectcolor=BG, activebackground=BG,
                           activeforeground=BRIGHT, bd=0,
                           highlightthickness=0).pack(side="left", padx=(0, 6))

        row2 = tk.Frame(parent, bg=BG)
        row2.pack(fill="x", pady=(0, 4))
        self.duty_entry = self._entry(row2, "duty %", "0", width=6)
        self.sp_entry   = self._entry(row2, "setpoint °C", f"{PID_SETPOINT_DEFAULT:g}", width=6)
        self.p_entry    = self._entry(row2, "target mbar", f"{PRESSURE_TARGET_DEFAULT:.1e}", width=9)
        tk.Button(row2, text="update", command=self._send_update, **btn).pack(side="left")

        self.heater_status = tk.Label(parent, text="", font=self.f, fg=DIM,
                                      bg=BG, anchor="w")
        self.heater_status.pack(fill="x")
        self.loop_status = tk.Label(parent, text="", font=self.f, fg=DIM,
                                    bg=BG, anchor="w")
        self.loop_status.pack(fill="x")
        self._update_inputs()
        self._send_update()

    def _toggle_arm(self):
        with _heater_lock:
            armed = _heater['armed']
        if armed:
            heater_command(armed=False)
            log_event("Heater DISARMED by operator")
        else:
            self._send_update(quiet_if_unchanged=True)   # picks up un-sent edits to the active value, logged
            heater_command(armed=True)
            log_event(f"Heater ARMED by operator · {self._active_summary()}")

    def _active_summary(self):
        """'auto (T) · setpoint 60.0 °C' — the mode and the value it uses."""
        a, mode = self._applied, self.mode_var.get()
        value = {'manual':   f"duty {a['duty']*100:g} %",
                 'auto':     f"setpoint {a['sp']:g} °C",
                 'pressure': f"target {a['tgt']:.2e} mbar"}[mode]
        return f"{MODE_LABELS[mode]} · {value}"

    @staticmethod
    def _set_entry(entry, text):
        """Write into an entry even while it is disabled."""
        state = entry.cget("state")
        entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, text)
        entry.configure(state=state)

    def _read_entry(self, entry, name, previous, lo, hi, scale=1.0, fmt="{:g}", unit=""):
        """Parse, validate and clamp one input. Invalid text is rejected (the
        previous value is restored and the rejection logged) rather than being
        silently replaced by a default. Clamping is logged too."""
        text = entry.get().strip()
        try:
            v = float(text) * scale
            if not math.isfinite(v):
                raise ValueError
        except ValueError:
            if previous is not None:
                self._set_entry(entry, fmt.format(previous / scale))
                self._input_noted = True
                log_event(f"Heater {name} entry '{text}' rejected — kept "
                          f"{fmt.format(previous / scale)}{unit}")
            return previous
        c = max(lo, min(hi, v))
        if c != v:
            self._input_noted = True
            log_event(f"Heater {name} {fmt.format(v / scale)}{unit} out of range — "
                      f"clamped to {fmt.format(c / scale)}{unit}")
            self._set_entry(entry, fmt.format(c / scale))
        return c

    def _send_update(self, quiet_if_unchanged=False):
        """Send the ACTIVE mode's value to the heater and log any change.

        Only the input belonging to the selected mode is read, validated and
        sent. The other two boxes are ignored entirely — whatever they
        contain has no effect until their own mode is selected, at which
        point their value is read and sent (and logged) like any update."""
        a = getattr(self, "_applied", None)
        first = a is None
        if first:
            a = self._applied = dict(mode=None, duty=0.0,
                                     sp=PID_SETPOINT_DEFAULT,
                                     tgt=PRESSURE_TARGET_DEFAULT)
        self._input_noted = False
        mode = self.mode_var.get()

        if mode == 'manual':
            key = 'duty'
            val = self._read_entry(self.duty_entry, "duty", a['duty'],
                                   0.0, HEATER_MAX_DUTY, scale=0.01, unit=" %")
            heater_command(mode=mode, duty_cmd=val)
            change = f"duty {a['duty']*100:g} → {val*100:g} %"
        elif mode == 'auto':
            key = 'sp'
            val = self._read_entry(self.sp_entry, "setpoint", a['sp'],
                                   0.0, TEMP_TRIP_C - 5.0, unit=" °C")
            heater_command(mode=mode, setpoint_C=val)
            change = f"setpoint {a['sp']:g} → {val:g} °C"
        else:   # 'pressure' — the outer loop owns the temperature setpoint
            key = 'tgt'
            val = self._read_entry(self.p_entry, "target", a['tgt'],
                                   PRESSURE_TARGET_MIN, PRESSURE_TRIP_MBAR / 2.0,
                                   fmt="{:.2e}", unit=" mbar")
            heater_command(mode=mode, p_target_mbar=val)
            change = f"target {a['tgt']:.2e} → {val:.2e} mbar"

        changes = []
        if not first and mode != a['mode']:
            changes.append(f"mode {MODE_LABELS[a['mode']]} → {MODE_LABELS[mode]}")
        if val != a[key]:
            changes.append(change)
        a['mode'], a[key] = mode, val

        if first:
            log_event(f"Heater settings · {self._active_summary()}")
        elif changes:
            log_event(f"Heater {' · '.join(changes)}   (now {self._active_summary()})")
        elif not (quiet_if_unchanged or self._input_noted):
            log_event(f"Heater update — no change ({self._active_summary()})")

    def _draw_chart(self, canvas, data, fmt="{:.3f}", log=False,
                    ref=None, floor=None, min_span=None,
                    color=BRIGHT, width=1):
        """Draw a strip chart on *canvas*.

        data  : sequence of values (oldest → newest)
        fmt   : format string for the y-axis tick labels
        log   : if True, plot log10 of the data (for vacuum pressure, which
                spans orders of magnitude). Non-positive values are skipped.
        ref   : optional reference value (target / setpoint), drawn dashed and
                always kept inside the y-range
        floor : optional (lo, hi) the y-range will always include
        min_span : smallest y-range shown (plot units; decades if log), so
                sensor quantisation isn't magnified into apparent swings.
                Tick labels gain decimals automatically if they'd repeat.
        color : line colour of the data curve
        width : line width of the data curve, px
        """
        c = canvas
        c.delete("all")
        w = c.winfo_width() or 580
        h = c.winfo_height() or 90
        pad_l, pad_r, pad_y = 74, 8, 8
        n_div = 4 if h >= 110 else (2 if h >= 55 else 1)   # label rows that fit

        for i in range(1, n_div):
            y = pad_y + (h - 2 * pad_y) * i / n_div
            c.create_line(pad_l, y, w - pad_r, y, fill=GRID)
        title = getattr(c, "title", "")
        if title:
            c.create_text(pad_l + 6, 2, text=title, fill=DIM, font=self.f,
                          anchor="nw", tags="title")

        if log:
            plot_vals = [math.log10(v) for v in data if v is not None and v > 0]
        else:
            plot_vals = [v for v in data if v is not None]
        if len(plot_vals) < 2:
            c.create_text(w / 2, h / 2, text="waiting for data",
                          fill=DIM, font=self.f)
            return

        ref_p = None
        if ref is not None and (not log or ref > 0):
            ref_p = math.log10(ref) if log else ref

        # Decimate to ~one min/max pair per pixel column. A 300 s window is
        # 1200 points per chart; min/max (not every-Nth) keeps short spikes.
        cols = max(2, int((w - pad_l - pad_r) / 2))
        if len(plot_vals) > 2 * cols:
            step, dec = math.ceil(len(plot_vals) / cols), []
            for k in range(0, len(plot_vals), step):
                b = plot_vals[k:k + step]
                i_lo = min(range(len(b)), key=b.__getitem__)
                i_hi = max(range(len(b)), key=b.__getitem__)
                dec.extend(b[i] for i in sorted({i_lo, i_hi}))
            plot_vals = dec

        lo, hi = min(plot_vals), max(plot_vals)
        if ref_p is not None:
            lo, hi = min(lo, ref_p), max(hi, ref_p)
        if floor is not None:
            lo, hi = min(lo, floor[0]), max(hi, floor[1])
        need = max(min_span or 0.0, 1e-9)
        if hi - lo < need:
            mid = (hi + lo) / 2
            lo, hi = mid - need / 2, mid + need / 2
        span = hi - lo
        n = len(plot_vals)

        def ypix(v):
            return (h - pad_y) - (h - 2 * pad_y) * (v - lo) / span

        ticks = [lo + span * i / n_div for i in range(n_div + 1)]
        reals = [(10 ** v) if log else v for v in ticks]
        m = re.search(r"\.(\d+)([fe])", fmt)
        labels = [fmt.format(v) for v in reals]
        if m:
            for extra in range(1, 4):              # add digits until labels differ
                if len(set(labels)) == len(labels):
                    break
                f2 = "{:." + str(int(m.group(1)) + extra) + m.group(2) + "}"
                labels = [f2.format(v) for v in reals]
        for v, text in zip(ticks, labels):
            c.create_text(pad_l - 4, ypix(v), text=text,
                          fill=DIM, font=self.f, anchor="e")

        if ref_p is not None:
            y = ypix(ref_p)
            c.create_line(pad_l, y, w - pad_r, y, fill=REF, dash=(4, 3))

        pts = []
        for i, v in enumerate(plot_vals):
            pts.extend((pad_l + (w - pad_l - pad_r) * i / (n - 1), ypix(v)))
        c.create_line(*pts, fill=color, width=width)
        c.tag_raise("title")

    def _heater_vi_lines(self, h):
        """Return [(label, value, note, value_tag)] for the V and I readouts."""
        if not _labjack_ok:
            return [("HEATER V     ", "---", "", "dim"),
                    ("HEATER I     ", "---", "", "dim")]
        d      = h['duty_actual']
        v_mean = d * HEATER_V_RAIL
        i_mean = v_mean / HEATER_R_OHM
        state  = "ON " if h['out_high'] else "off"
        lines  = []

        if h['v_meas'] is not None and h['v_meas_mean'] is not None:
            rail = h['rail_meas']
            lines.append(("HEATER V     ",
                          f"{h['v_meas']:6.2f} V {state} · {h['v_meas_mean']:6.2f} V mean",
                          f"  meas · rail {rail:.2f} V · calc {v_mean:.2f} V", "bright"))
        else:
            lines.append(("HEATER V     ",
                          f"{h['v_now']:6.2f} V {state} · {v_mean:6.2f} V mean",
                          "  calc — assumes SW171 on, 24 V present", "bright"))

        if h['i_meas'] is not None and h['i_meas_mean'] is not None:
            lines.append(("HEATER I     ",
                          f"{h['i_meas']:6.3f} A {state} · {h['i_meas_mean']:6.3f} A mean",
                          f"  meas · calc {i_mean:.3f} A", "bright"))
        else:
            lines.append(("HEATER I     ",
                          f"{h['i_now']:6.3f} A {state} · {i_mean:6.3f} A mean",
                          f"  calc ({HEATER_R_OHM:g} Ω element)", "bright"))
        return lines

    def _poll(self):
        with _lock:
            p_samp = _state['keller_pressure_samples']
            t_samp = _state['keller_temperature_samples']
            p       = (sum(p_samp) / len(p_samp)) if p_samp else None
            t       = (sum(t_samp) / len(t_samp)) if t_samp else None
            vac     = _state['vacuum_chamber_mbar']
            vac_st  = _state['vacuum_status']
            vac_u   = _state['vacuum_gauge_V']
            te_temp = _state['te_temperature_degC']
            fault   = _state['tc_fault']
            p20_chart  = list(_p20_chart)
            vac_chart  = list(_vac_chart)
            te_chart   = list(_te_chart)
            heat_chart = list(_heat_chart)
            events     = list(_events)
        with _heater_lock:
            h = dict(_heater)

        p_s  = f"{p:.4f} bar"      if p       is not None else "---"
        t_s  = f"{t:.1f} °C"       if t       is not None else "---"
        v_s  = f"{vac:.2e} mbar"   if vac     is not None else "---"
        te_s = f"{te_temp:.2f} °C" if te_temp is not None else "---"
        p20  = p * P20_REF_K / (t + 273.15) if (p is not None and t is not None) else None
        p20_s = f"{p20:.4f} bar" if p20 is not None else "---"
        vac_note = f"  [{vac_st}]" if (vac is None and vac_st) else ""
        vac_volt = f"  ({vac_u:.2f} V at gauge)" if vac_u is not None else ""

        # thermocouple fault annotation
        fault_note = ""
        if fault:
            names = [d for b, d in FAULT_BITS.items() if fault & b]
            fault_note = "  [" + ", ".join(names) + "]"

        st = self.status_text
        st.configure(state="normal")
        st.delete("1.0", "end")
        st.insert("end", "TE-VALVE-DRIVER\n", "bright")
        for lbl, ok, avail in (
            (f"[KELLER:{'OK' if _keller_ok else '--'}]",  _keller_ok,  True),
            (f"[VACUUM:{'OK' if _labjack_ok else '--'}]", _labjack_ok, LABJACK_AVAILABLE),
            (f"[TE-TEMP:{'OK' if _tc_ok else '--'}]",     _tc_ok,      LABJACK_AVAILABLE),
            (f"[CSV:{'OK' if _csv_ok else ('ERR' if _csv_ok is False else '--')}]",
             bool(_csv_ok), True),
        ):
            tag = "ok" if ok else ("dim" if not avail else "err")
            st.insert("end", lbl + "  ", tag)
        st.insert("end", "\n\n")
        st.insert("end", "UPSTREAM P   ", "dim") ; st.insert("end", p_s + "\n",
                  "bright" if p is not None else "dim")
        st.insert("end", "UPSTREAM T   ", "dim") ; st.insert("end", t_s + "\n",
                  "bright" if t is not None else "dim")
        st.insert("end", "UPSTREAM P20 ", "dim") ; st.insert("end", p20_s, "bright" if p20 is not None else "dim")
        st.insert("end", "  (at 20 °C)\n", "dim")
        st.insert("end", "VACUUM       ", "dim")
        st.insert("end", v_s, "bright" if vac is not None else "dim")
        st.insert("end", vac_note, "err")
        st.insert("end", vac_volt + "\n", "dim")
        st.insert("end", "TE VALVE T   ", "dim")
        st.insert("end", te_s, "bright" if te_temp is not None else "dim")
        st.insert("end", fault_note + "\n", "err" if fault_note else "dim")
        st.insert("end", "\n")
        for lbl, val, note, tag in self._heater_vi_lines(h):
            st.insert("end", lbl, "dim")
            st.insert("end", val, tag)
            st.insert("end", note + "\n", "dim")
        st.configure(state="disabled")

        # ── heater status lines ───────────────────────────────────────────
        mode = h['mode']
        self.arm_btn.configure(text="DISARM" if h['armed'] else "ARM",
                               fg=WARN if h['armed'] else TEXT)
        power = h['duty_actual'] * HEATER_V_RAIL ** 2 / HEATER_R_OHM
        if h['trip_reason']:
            self.heater_status.configure(
                text=f"TRIPPED — {h['trip_reason']}   (disarm, then arm to clear)",
                fg=WARN)
        elif h['armed']:
            left = "" if h['armed_at'] is None else \
                f" · off in {max(0, HEATER_MAX_RUN_S - (time.time() - h['armed_at']))/60:.0f} min"
            tgt = {'manual': "",
                   'auto': f" · T_sp {h['setpoint_C']:.1f} °C",
                   'pressure': f" · T_sp {h['setpoint_C']:.1f} °C (from pressure)"}[mode]
            self.heater_status.configure(
                text=f"ARMED · {MODE_LABELS[mode]} · duty {h['duty_actual']*100:4.1f} % · "
                     f"{power:.2f} W{tgt}{left}", fg=BRIGHT)
        else:
            self.heater_status.configure(text="disarmed · output low", fg=DIM)

        if mode == 'auto' and h['armed'] and h['t_burst'] == 'burst':
            self.loop_status.configure(
                text=f"auto (T) · BURST full power, cut ~{h['t_brake']:.1f} K below "
                     f"{h['setpoint_C']:.1f} °C", fg=BRIGHT)
        elif mode == 'auto' and h['armed'] and h['t_burst'] == 'coast':
            self.loop_status.configure(
                text=f"auto (T) · coasting, heater off (peak {h['t_burst_peak']:.1f} °C) — "
                     f"PI resumes at the peak", fg=BRIGHT)
        elif mode != 'pressure':
            self.loop_status.configure(text="")
        elif not h['armed'] or h['p_filt'] is None or h['p_init']:
            self.loop_status.configure(
                text=f"auto (P) idle · target {h['p_target_mbar']:.2e} mbar · "
                     f"on arm: T_sp → {PRESSURE_SEEK_START_C:g} °C, then "
                     f"+{PRESSURE_SEEK_RATE_C_MIN:g} °C/min until the valve opens", fg=DIM)
        elif h['p_phase'] == 'seek':
            base = (f"{10 ** h['p_base']:.2e} mbar" if h['p_base'] is not None
                    else "measuring…")
            low = ""
            if h['p_base'] is not None:
                lowest = 10 ** h['p_base'] + PRESSURE_MIN_STEP_MBAR
                if 10 ** h['p_base'] < h['p_target_mbar'] < lowest:
                    low = (f" — BELOW lowest holdable ≈ {lowest:.1e}, "
                           f"will hold minimum flow")
            if h['p_burst'] == 'burst':
                step = f"BURST full power to {h['setpoint_C'] - PRESSURE_BURST_BRAKE_K:.1f} °C"
            elif h['p_burst'] == 'coast':
                step = f"coasting (peak {h['p_burst_peak']:.1f} °C)"
            elif h['p_base'] is not None and h['p_target_mbar'] <= 10 ** h['p_base']:
                step = "holding shut (target ≤ baseline)"
            elif h['p_ramping']:
                step = f"creeping +{PRESSURE_SEEK_RATE_C_MIN:g} °C/min"
            else:
                step = "heating"
            self.loop_status.configure(
                text=f"auto (P) seeking · valve shut · {step} · goal {h['p_goal']:.1f} "
                     f"(upstream shift {h['p_shift']:+.1f} K) · "
                     f"T_sp {h['setpoint_C']:.2f} °C · "
                     f"baseline {base} · target {h['p_target_mbar']:.2e} mbar{low}",
                fg=WARN if (h['p_seek_capped'] or low) else BRIGHT)
        else:
            tsp_hi = min(PRESSURE_TSP_MAX_C, TEMP_TRIP_C - 5.0)
            floor  = max(PRESSURE_TSP_MIN_C, PRESSURE_OPEN_FLOOR_C + h['p_shift'])
            lowest = 10 ** h['p_base'] + PRESSURE_MIN_STEP_MBAR
            if h['p_target_mbar'] < lowest:
                self.loop_status.configure(
                    text=f"auto (P) · target {h['p_target_mbar']:.2e} is BELOW the lowest "
                         f"holdable ≈ {lowest:.1e} (baseline {10 ** h['p_base']:.2e} + "
                         f"min. flow) — holding minimum flow at the {floor:.1f} °C floor · "
                         f"now {10 ** h['p_filt']:.2e}",
                    fg=WARN)
            else:
                self.loop_status.configure(
                    text=f"auto (P) · target {h['p_target_mbar']:.2e} · "
                         f"baseline {10 ** h['p_base']:.2e} · "
                         f"filt {10 ** h['p_filt']:.2e} · err {h['p_err']:+.2f} dec · "
                         f"T_sp {floor:.1f}-{tsp_hi:g} °C · upstream shift {h['p_shift']:+.1f} K",
                    fg=WARN if h['p_pinned_since'] else BRIGHT)

        vac_ref = h['p_target_mbar'] if mode == 'pressure' else None
        te_ref  = h['setpoint_C'] if (h['armed'] and mode != 'manual') else None
        i_full  = HEATER_V_RAIL / HEATER_R_OHM

        self._draw_chart(self.p20_canvas,  p20_chart,  fmt="{:.3f}", min_span=0.005,
                         color=P20_LINE, width=2)
        self._draw_chart(self.vac_canvas,  vac_chart,  fmt="{:.1e}", log=True, ref=vac_ref,
                         min_span=0.05, color=VAC_LINE, width=2)
        self._draw_chart(self.te_canvas,   te_chart,   fmt="{:.1f}", ref=te_ref, min_span=0.5,
                         color=TEMP_LINE, width=2)
        self._draw_chart(self.heat_canvas, heat_chart, fmt="{:.3f}",
                         floor=(0.0, 1.05 * i_full))

        text = "\n".join(f"> {s}  {m}" for s, m in events[-200:])
        if getattr(self, "_last_log_text", None) != text:
            self._last_log_text = text
            self.logtext.configure(state="normal")
            self.logtext.delete("1.0", "end")
            self.logtext.insert("1.0", text)
            self.logtext.see("end")
            self.logtext.configure(state="disabled")

        if not _stop.is_set():
            self.root.after(150, self._poll)

    def shutdown(self):
        heater_command(armed=False)
        log_event("Shutdown — heater disarmed")
        _stop.set()
        # Give the device thread a moment to drive FIO0 low and release the
        # watchdog before the process exits.
        time.sleep(0.5)
        print(f"Log saved: {LOG_FILE}")
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global _keller_ok

    print("\nTE-VALVE-DRIVER — startup")
    print("─" * 50)

    if not acquire_single_instance_lock():
        print("\n" + "!" * 60)
        print("  ANOTHER INSTANCE IS ALREADY RUNNING.")
        print()
        print("  That instance still owns the LabJack, and it may still be")
        print("  driving the heater on FIO0. Close its window, or end the")
        print("  python.exe task, then start this one again.")
        print()
        print("  If in doubt about the heater: SW171 off and 24 V off at")
        print("  the wall makes it safe regardless of what software does.")
        print("!" * 60 + "\n")
        return

    pin_err = _check_sense_pins()
    if pin_err:
        print(f"\nCONFIG ERROR: {pin_err}\nFix the heater sense settings and restart.\n")
        return

    print("Scanning for Keller sensor...")
    keller_port, keller_bus = _detect_keller_bus()
    _keller_ok = keller_bus is not None
    if not _keller_ok:
        print("  [Keller] NOT FOUND — upstream pressure/temperature will be blank.")

    if LABJACK_AVAILABLE:
        print("LabJack will connect on its own thread (vacuum + thermocouple).")
    else:
        print("LabJack not available — vacuum and TE temperature disabled.")

    print(f"\nLogging to: {LOG_FILE}")
    print(f"Log cadence: every {LOG_INTERVAL_S:g} s (drift-free)")
    print("─" * 50)

    # ── Start worker threads ───────────────────────────────────────────────
    if keller_bus is not None:
        threading.Thread(target=keller_thread, args=(keller_port, keller_bus),
                         daemon=True).start()
    if LABJACK_AVAILABLE:
        threading.Thread(target=labjack_thread, daemon=True).start()
    threading.Thread(target=logger_thread, daemon=True).start()

    log_event("System started")
    if _keller_ok:
        log_event(f"Keller online · {keller_port}")

    # ── GUI runs on the main thread ────────────────────────────────────────
    TEGui().run()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        print("\n" + "=" * 60)
        print("FATAL ERROR:")
        print("=" * 60)
        traceback.print_exc()
    finally:
        input("\nPress Enter to close...")
