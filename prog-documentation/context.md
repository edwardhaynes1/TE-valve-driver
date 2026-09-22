# Context — shared vocabulary for the TE-Valve driver

One meaning per word. If code, logs, plots or conversation use a term
differently from this page, fix one of them. Add terms as they appear.

## Names

| Term | Meaning |
|---|---|
| **TE-VALVE-DRIVER** | The program: live display, logging and heater control. Started with `TE-VALVE-DRIVER.py`. |
| **`driver/`** | The folder holding the program's parts (a Python *package*: code refers to it by name, e.g. `from driver import controller`). |
| **TE_PLOTTER** | The separate program that plots the logs. |

## The hardware

| Term | Meaning |
|---|---|
| **TE-Valve** | Thermally Enabled Valve: the Max Planck prototype under test. Heating it opens it. Final name (ARIEL or TERP) still to be decided. |
| **Seat screw torque** | Torque applied to the TE-Valve's seat screw with the torque wrench, in N·m. Entered in the driver (`SEAT SCREW`) before anything else — every other control is locked, and the input shown orange, until it is — logged in every CSV row (`seat_screw_torque_Nm`), and noted in the event log when it changes. Blank at the start of every session until entered: blank means *not recorded*, never zero. It is the torque, not the clamping force on the seat, which also depends on thread friction. Sets the *opening point* far more than upstream pressure does: see `SEAT_SCREW_VALVE`. |
| **ARIEL PCB** | The heater control board (FIO0 drives the MOSFET gate Q171; SW171 enables the 24 V rail). Not the valve, whatever the valve ends up being called. |
| **Heater** | The 88 Ω element on the valve, 24 V rail: 6.55 W at full duty. |
| **Upstream pressure** | Gas pressure before the valve, from the Keller PAA-23SX-H2. Absolute, in bar. |
| **Keller chip temperature** | The Keller's temperature reading ("KELLER T" on screen). It is the sensor chip's temperature, *not* the gas temperature (grip test, 17 Sept 2026). |
| **Chamber pressure** | Vacuum chamber pressure from the Pfeiffer IKR 270 cold-cathode gauge, in mbar. |
| **TC** | The valve's type-K thermocouple, read by the MAX31856. It sits by the heater, so it leads the valve body. Can be unplugged and plugged back in while the driver runs: readings stop, then resume within about a second. |
| **Valve temperature** | The TC reading, in °C. The one name for it: not "TE temperature" or "TC temperature". (The CSV column keeps its original name, `te_temperature_degC`.) |

## Valve behaviour

| Term | Meaning |
|---|---|
| **Opening point** | Valve temperature (TC) at which flow starts. Depends mostly on seat screw torque (21 Sept 2026: 0.25 N·m ~40 °C, 0.40 N·m ~88 °C, 0.45 N·m ~150 °C; 0.30 N·m 92.7 °C — not monotonic, so torque alone doesn't fix it), then on upstream pressure (lower when it is higher), and on the valve's recent history (lower on re-heats). auto-p takes it from `SEAT_SCREW_VALVE`, then uses the point actually seen for the rest of the session (*learned*). Replaces "cracking point". |
| **Throttling** | Flow grows continuously with temperature above the opening point (21 Sept 2026). The 16 Sept "snap open" came from coarse setpoint steps. |
| **e-fold** | Temperature rise that multiplies the flow by e (2.7×): ~4 K at 0.25 N·m, ~7 K at 0.40, 10-29 K at 0.45. auto-p's gains and creep scale with it. |
| **Soak** | Flow keeps rising for minutes at a constant TC once the valve is hot (0.45 N·m, 145 °C: ×6 over 3 min). |
| **Closing hysteresis** | The valve closes below where it opened: ~1 K on 16 Sept, 10-35 K on 21 Sept. |
| **Baseline** | Chamber pressure with the valve shut. Measured during seek, frozen once the valve opens. |
| **Hold power** | Power that holds a valve temperature, lab ~23 °C (21 Sept 2026, 9 holds): 0.47 W at 40 °C, 2.0 W at 90 °C, 4.1 W at 150 °C (`HEATER_HOLD_*`). Only up to ~58 °C fits the 1 W flight budget (in the lab). |

## Known rig behaviour

| Term | Meaning |
|---|---|
| **Upstream leak** | The upstream connection at the valve leaks (17 Sept 2026), so upstream pressure falls even with the valve shut. Fix before measuring pressure dependence. |
| **Keller head sensitivity** | The Keller's chip temperature follows the air conditioning and a hand on the sensor head, not the gas. Insulate the head; don't use its temperature to correct pressure. |

## Heater control

| Term | Meaning |
|---|---|
| **Armed / disarmed** | Armed = the software may switch the heater on. Disarming always wins, and takes effect at the next control step (within 0.25 s). |
| **Trip** | A latched heater-off caused by an interlock. Cleared only by disarm, then arm. |
| **Interlock** | A check before any heating: TC healthy, below 160 °C, armed less than 60 min, TC *plausible*, chamber pressure readable and below 5e-4 mbar (auto-p only). |
| **Plausible (TC)** | The reading changes no faster than 20 °C/s and rises at least 1 K in 15 s at full power. Added after the 21 Sept 2026 17:20 fault, when the TC read nonsense with no fault bit. |
| **Duty** | Fraction of each PWM period the heater is on, 0–1. What the controller commands. |
| **PWM period** | 2 s. The heater is switched fully on or off within it (time-proportioning, done in software on the 50 ms tick). The cycle runs continuously, so the first pulse after arming can be shorter than the rest. |
| **Gate / edge** | The FIO0 output to the MOSFET gate. An edge is one on→off or off→on switch; every edge goes in the `_pwm.csv` log. |
| **Heater power** | Mean power over one PWM period = duty × V²/R. *Not* mean V × mean I. |
| **Flight power budget** | 1 W for the valve heater in flight. Drawn dashed on the power chart. |
| **Mode** | One of three, named identically on screen, in the code (`controller.MODES`) and in the CSV `heater_mode` column. Logs before 17 Sept 2026 say `auto` for auto-t and `pressure` for auto-p. |
| **manual** | Fixed duty. |
| **auto-t** | Holds a valve temperature setpoint: hold-power feedforward plus a PID trim. |
| **auto-p** | Cascade: an outer loop on chamber pressure moves the auto-t setpoint. Avoid "pressure mode". |
| **Burst** | Full power to reach a temperature fast, cut when the TC is predicted to coast onto the target: T + tau × rate of rise. *tau* (s) is learned from every coast. |
| **Coast** | Heater off after a burst until the valve temperature peaks; then hold feedforward + PID take over. |
| **Seek** | auto-p phase with the valve shut: heat to the *goal*, then *creep* up (1 °C/min × gain scale) until the valve opens, at most 20 K above the opening point. |
| **Goal** | Seek target temperature: the opening point (with the *upstream shift*), raised by at most 5 K by the feedforward for larger pressure targets. |
| **Upstream shift** | Opening-point offset for upstream pressure: −12 K per bar relative to the pressure the torque's point was measured at; at most +10 K, −40 K. |
| **Upstream feedforward** | Once open, the setpoint moves by −1.5 × e-fold × ln(P_up / P_up at opening) when upstream pressure changes (refill, leak). |
| **Hold shut** | If the pressure target is at or below the baseline, the setpoint parks 5 K below the opening point. |
| **Track** | auto-p phase with the valve open: PI on log10(chamber pressure). |
| **Open floor** | In track, the setpoint stays within 1 K of the opening point, so the loop trims flow rather than shutting the valve. |

## Logs

| Term | Meaning |
|---|---|
| **Main log** | `logs/te-sensor_<time>.csv`, one row every 0.5 s. Columns in `driver/schema.py`. |
| **Switching log** | `logs/te-sensor_<time>_pwm.csv`, one row per gate edge. |
| **P20** | Upstream pressure referred to 20 °C using the Keller chip temperature. Not charted or used by the plotter any more (see entry 5 in [software-history-log.md](software-history-log.md)); shown in the text readout with a warning. |
| **Golden record** | `tests/golden/control_trace.json.gz`: the control law's recorded behaviour. Tests require an exact match. |
| **Heater state** | The dict `h` from `controller.new_state()` (a `HeaterState`: its fields are fixed, so a misspelt field is an error): operator commands, loop internals and the duty being applied. At run time it is private to `control.py`; others see a copy via `control.snapshot()`. |
| **Readings** | The latest sensor values and chart history, private to `shared.py`; written with `store_…`, read with `latest()`, `charts()`, `health()`. |
| **Heater output** | What the device thread measures on the heater circuit (gate state, voltage, current, power). In `shared.py`, read with `heater_output()`. Not part of the heater state. |
| **Control step** | One call of `controller.step`: interlocks, then the control law, every 0.25 s. |

## Mission context

| Term | Meaning |
|---|---|
| **Descent requirement** | During the Uranus descent the valve needs *more* conductance early (low upstream pressure, cold atmosphere) and *less* at depth (high upstream pressure, warm atmosphere). |

## Open questions

- How far does upstream pressure move the opening point? (12 K/bar is a local fit; 21 Sept runs changed torque and pressure together.)
- Why is the torque → opening point relation not monotonic (0.30 N·m above 0.40)? Seating, friction, or the 17:20 TC remount?
- Could auto-p use the closing hysteresis to hold flows below what the valve gives at its opening point?
- Final valve name: ARIEL or TERP.
