# Software history log

Short records of choices that shaped the code, newest first. Each says what
was decided and why, so nobody has to rediscover the reason. Add one when a
change would otherwise puzzle someone reading the code later.

## 14. devices.py split; the LabJack tick in named steps — 17 Sept 2026
`devices.py` mixed the Keller, the LabJack, the thermocouple chip and a
290-line loop. It is now `keller.py`, `thermocouple.py` and `labjack.py`.
In `labjack.py` one connection is a `_Session`, and each tick reads as
`_sense_heater → [_read_vacuum → _service_thermocouple → _run_controller] →
_drive_gate`; a device error raises `_DeviceLost`, which ends the session
through `_release` (gate low, watchdog released) and reconnects. The
statements inside each step are unchanged. Before the split, 9 tests were
added for the loop's error and sensing paths (`test_device_faults.py`);
they, the existing device tests and the golden record all pass on the new
code, and deliberately broken versions of the new code make them fail.

## 13. Heater state fields are fixed — 17 Sept 2026
The heater state is a dict with text keys, so a misspelt key on assignment
used to create a new field silently while the real one stayed unchanged.
`controller.HeaterState` now refuses unknown or removed fields. A dataclass
with named attributes would catch typos even earlier (in the editor), but
means rewriting every access in the controller and GUI; this gets most of
the benefit with no change to the code that uses the state.

## 12. Shared state only through functions — 17 Sept 2026
`shared.py` used to hand out its dictionaries, lists and locks, and the GUI,
logger and device thread reached straight in: the logger reset the heater's
on-time bookkeeping itself, and the device thread disarmed the heater by
writing its fields. Every module had to know the key names and locking rules.
Now `shared.py` keeps readings, charts, events and health flags private
behind named functions (`store_keller`, `latest`, `take_log_readings`, …),
and `control.py` alone owns the heater state (`snapshot`, `take_on_time`,
`force_off`, …). The locking lives in those two files only.
`test_architecture.py` fails if any module touches another's private names
or if either file exposes a raw container. Behaviour is unchanged: the golden
record still matches, and the GUI and logs were checked end to end.

## 11. One set of names for modes and the valve temperature — 17 Sept 2026
The modes were "auto (T)" / "auto (P)" on screen but `auto` / `pressure` in
the code and CSV. They are now `manual`, `auto-t` and `auto-p` everywhere,
defined once in `controller.MODES`; an unknown name is refused rather than
silently running the temperature loop. The CSV `heater_mode` column carries
the new names from 17 Sept 2026; the plotter accepts both. The thermocouple
reading is "valve temperature" on screen and in the plotter (the CSV column
keeps its name, `te_temperature_degC`). Control behaviour is unchanged.

## 10. The controller takes everything as arguments — 17 Sept 2026
`controller.step(h, now, dt, readings…)` returns `(duty, messages)` and
touches nothing else: no lock, clock, event log or shared readings. That
makes every rule testable with a plain dict and a number for the time.
`control.py` is the only place that adds threads: it reads the upstream
pressure, holds `heater_lock` for the whole step (so a GUI command can no
longer land halfway through one), and logs the messages afterwards. The
state stays a dict, changed in place, because the GUI and logger read it.
Behaviour is unchanged: the golden record matches through both paths.

## 9. Split the driver into the `driver` package — 17 Sept 2026
The single file had grown to ~2,550 lines mixing GUI, threads, control law,
interlocks and logging, so any change risked the rest. It is now a package
with one job per module (see README). Before splitting, the control law's
behaviour was recorded over 15 scenarios (`tests/golden/`); the package
reproduces it exactly, and the GUI renders pixel-identically. Behaviour did
not change. Importing the package no longer creates log files; `app.main()`
does that at start-up.

## 8. CSV columns are append-only and defined once — 17 Sept 2026
`driver/schema.py` defines both logs' columns; the driver writes with it and
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

## 4. auto-p is a cascade with seek and track phases — Sept 2026
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
