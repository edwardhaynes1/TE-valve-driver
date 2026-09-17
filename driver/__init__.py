"""driver — the parts of the TE-Valve driver program.

Run it with `python TE-VALVE-DRIVER.py`. The overview below is the
original driver's header, kept as the package documentation.

TE-VALVE-DRIVER.py
==================
Live display and logging for the thermally-enabled (TE) valve test setup.

Logs three sensor channels with a robust, self-reconnecting architecture:
  • Upstream pressure + temperature  — Keller PAA-23SX-H2 (RS485/USB, K-114)
  • Vacuum chamber pressure          — Pfeiffer IKR 270 cold cathode gauge,
                                       LabJack U3 FIO2 via voltage divider
  • Valve temperature                — MAX31856 Type-K thermocouple, LabJack SPI

Each sensor runs on its own thread. If any device disconnects mid-session the
affected reading goes to '---' immediately (never a stale value) and the thread
scans for the device again before resuming — the rest of the system is
unaffected.

HEATER CONTROL is included (FIO0). See the safety notes below. Three modes:
  manual    fixed duty cycle
  auto-t    PI loop (optional D) holding the valve at a temperature setpoint
  auto-p    cascade: an outer loop on chamber pressure moves the temperature
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

HEATER POWER is charted as the mean over one switching period. The heater is
time-proportioned (fully on or off), so mean power = duty × V²/R. Multiplying
mean V by mean I would understate it (duty² × V²/R). With sensing wired, the
measured power is the per-tick V × I averaged over the period.

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
  heater_duty_cmd, events, pressure_baseline_mbar,
  heater_P_mean_calc, heater_P_mean_meas, heater_on_s
  (new columns are appended at the end, so existing parsers keep working)

Heater switching log: te-sensor_<ts>_pwm.csv (one row per gate edge)
  timestamp   ISO, ms — taken as the FIO0 write returns (tick resolution
              1/HEATER_TICK_HZ; USB write latency a few ms)
  gate        1 = switched ON, 0 = switched OFF
  duty        applied duty (0-1) when the edge happened
  on_s        OFF rows: how long the gate had been ON
  note        blank for normal switching; otherwise why (connect,
              shutdown/reconnect forced low, write error)
  The gate state is what the software commanded, not a measured voltage.
  heater_on_s in the main CSV is the ON time since the previous row, from
  the same edge times, so energy = heater_on_s × V²/R exactly.

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
