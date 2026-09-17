# Decisions

Short records of choices that shaped the code, newest first. Each says what
was decided and why, so nobody has to rediscover the reason. Add one when a
change would otherwise puzzle someone reading the code later.

## 10. The controller takes everything as arguments — 17 Sept 2026
`controller.step(h, now, dt, readings…)` returns `(duty, messages)` and
touches nothing else: no lock, clock, event log or shared readings. That
makes every rule testable with a plain dict and a number for the time.
`control.py` is the only place that adds threads: it reads the upstream
pressure, holds `heater_lock` for the whole step (so a GUI command can no
longer land halfway through one), and logs the messages afterwards. The
state stays a dict, changed in place, because the GUI and logger read it.
Behaviour is unchanged: the golden record matches through both paths.

## 9. Split the driver into the `tevalve` package — 17 Sept 2026
The single file had grown to ~2,550 lines mixing GUI, threads, control law,
interlocks and logging, so any change risked the rest. It is now a package
with one job per module (see README). Before splitting, the control law's
behaviour was recorded over 15 scenarios (`tests/golden/`); the package
reproduces it exactly, and the GUI renders pixel-identically. Behaviour did
not change. Importing the package no longer creates log files; `app.main()`
does that at start-up.

## 8. CSV columns are append-only and defined once — 17 Sept 2026
`tevalve/schema.py` defines both logs' columns; the driver writes with it and
the plotter reads with it. Columns are only ever appended, never renamed or
reordered, so old logs and old readers keep working. The plotter keeps its
loose name matching only for logs from before the schema existed.

## 7. Log every heater gate edge — 17 Sept 2026
A separate `_pwm.csv` records each switch with its time, because the 0.5 s
main log cannot place edges that come up to several times a second. The main
log gets `heater_on_s` from the same edge times, so energy per row is exact.
The log records what the software commanded, not a measured voltage.

## 6. Chart heater power, not current — 17 Sept 2026
Under time-proportioning, mean power = duty × V²/R. Mean V × mean I would
understate it (duty² × V²/R). With calculated values, current and power have
the same shape, so the chart shows power (the physical quantity, and what
the 1 W flight budget is about). Duty and current stay in the log: duty is
what the controller did and does not depend on the assumed 24 V / 88 Ω.

## 5. Chart raw upstream pressure, not P20 — 17 Sept 2026
P20 divided by the Keller's temperature, which is the sensor chip's, not the
gas's. Gripping the sensor head raised that temperature without changing
the gas, and P20 dropped accordingly. The raw absolute pressure is charted
instead. A proper correction needs a temperature sensor on the upstream
tubing.

## 4. Pressure mode is a cascade with seek and track phases — Sept 2026
The valve snaps open near 40 °C rather than throttling, and chamber pressure
spans decades. So the outer loop sets a temperature setpoint for the
proven temperature PI: it seeks (burst, coast, creep) with the valve shut
while measuring the baseline, detects the opening, then tracks log10(p).

## 3. Software time-proportioning on FIO0, no LabJack timers
Heater duty is produced by toggling FIO0 on the device thread's 50 ms tick
over a 2 s period. LabJack timers are not used because a timer may claim
FIO4, which is the MAX31856 SDO line. The U3 firmware watchdog pulls FIO0
low if the program dies.

## 2. MAX31856 set to the 50 Hz mains filter
CR0 = 0x91. The previous value left the notch at 60 Hz, which is wrong in
Switzerland.

## 1. Only one driver instance at a time
An OS file lock (`.te-valve-driver.lock`) stops a second copy starting,
because a forgotten instance may still be driving the heater.
