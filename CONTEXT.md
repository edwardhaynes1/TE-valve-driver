# CONTEXT — shared vocabulary for the TE-Valve driver

One meaning per word. If code, logs, plots or conversation use a term
differently from this page, fix one of them. Add terms as they appear.

## The hardware

| Term | Meaning |
|---|---|
| **TE-Valve** | Thermally Enabled Valve: the Max Planck prototype under test. Heating it opens it. Final name (ARIEL or TERP) still to be decided. |
| **ARIEL PCB** | The heater control board (FIO0 drives the MOSFET gate Q171; SW171 enables the 24 V rail). Not the valve, whatever the valve ends up being called. |
| **Heater** | The 88 Ω element on the valve, 24 V rail: 6.55 W at full duty. |
| **Upstream pressure** | Gas pressure before the valve, from the Keller PAA-23SX-H2. Absolute, in bar. |
| **Keller chip temperature** | The Keller's temperature reading. It is the sensor chip's temperature, *not* the gas temperature (grip test, 17 Sept 2026). |
| **Chamber pressure** | Vacuum chamber pressure from the Pfeiffer IKR 270 cold-cathode gauge, in mbar. |
| **TC** | The valve's type-K thermocouple, read by the MAX31856. "Valve temperature" in the GUI means the TC reading, which leads the valve body. |

## Valve behaviour

| Term | Meaning |
|---|---|
| **Cracking point** | TC temperature at which the valve opens: about 40.1–40.6 °C in the 16 Sept runs (valve body ≈ 39.3 °C). |
| **Snap open** | The valve goes from shut to open in one step rather than throttling gradually (16 Sept runs). Whether it throttles at all above the cracking point is still open. |
| **Closing hysteresis** | The valve closes about 1 K below where it opened. |
| **Baseline** | Chamber pressure with the valve shut. Measured during seek, frozen once the valve opens. |

## Heater control

| Term | Meaning |
|---|---|
| **Armed / disarmed** | Armed = the software may switch the heater on. Disarming always wins. |
| **Trip** | A latched heater-off caused by an interlock. Cleared only by disarm, then arm. |
| **Interlock** | A check before any heating: TC healthy, below 160 °C, armed less than 60 min, chamber pressure readable and below 5e-4 mbar (pressure mode). |
| **Duty** | Fraction of each PWM period the heater is on, 0–1. What the controller commands. |
| **PWM period** | 2 s. The heater is switched fully on or off within it (time-proportioning, done in software on the 50 ms tick). |
| **Gate / edge** | The FIO0 output to the MOSFET gate. An edge is one on→off or off→on switch; every edge goes in the `_pwm.csv` log. |
| **Heater power** | Mean power over one PWM period = duty × V²/R. *Not* mean V × mean I. |
| **Flight power budget** | 1 W for the valve heater in flight. Drawn dashed on the power chart. |
| **Mode: manual** | Fixed duty. Internal name `manual`. |
| **Mode: auto (T)** | PI loop holding a TC setpoint. Internal name `auto`. |
| **Mode: auto (P)** | Cascade: an outer loop on chamber pressure moves the auto (T) setpoint. Internal name `pressure`. |
| **Burst** | Full power from a cool start, cut early (the *brake*), to reach a temperature fast. |
| **Coast** | Heater off after a burst until the TC peaks; then the PI takes over. |
| **Seek** | Auto (P) phase with the valve shut: heat to the *goal*, then *creep* up at 1 °C/min until the valve opens. |
| **Goal** | Seek target temperature: the cracking point, raised by the *feedforward map* for larger pressure targets and moved by the *upstream shift*. |
| **Upstream shift** | Temperature offset applied for upstream pressure (12 K per bar relative to 2.76 bar). |
| **Hold shut** | If the pressure target is at or below the baseline, the setpoint parks at 38.5 °C. |
| **Track** | Auto (P) phase with the valve open: PI on log10(chamber pressure). |
| **Open floor** | In track, the setpoint never drops below 39.5 °C, so the loop trims flow rather than shutting the valve. |

## Logs

| Term | Meaning |
|---|---|
| **Main log** | `logs/te-sensor_<time>.csv`, one row every 0.5 s. Columns in `tevalve/schema.py`. |
| **Switching log** | `logs/te-sensor_<time>_pwm.csv`, one row per gate edge. |
| **P20** | Upstream pressure referred to 20 °C using the Keller chip temperature. Not charted or used by the plotter any more (see entry 5 in docs/software-history-log.md); shown in the text readout with a warning. |
| **Golden record** | `tests/golden/control_trace.json.gz`: the control law's recorded behaviour. Tests require an exact match. |
| **Heater state** | The dict `h` from `controller.new_state()`: operator commands, loop internals and the live electrical readout. At run time it is `shared.heater`. |
| **Step** | One call of `controller.step`: interlocks, then the control law, every 0.25 s. |

## Mission context

| Term | Meaning |
|---|---|
| **Descent requirement** | During the Uranus descent the valve needs *more* conductance early (low upstream pressure, cold atmosphere) and *less* at depth (high upstream pressure, warm atmosphere). |

## Open questions

- Does the valve throttle above the cracking point, or is it purely on/off? This decides what the pressure controller should be.
- Does the cracking point move with upstream pressure, and by how much? (The 12 K/bar shift is a local fit.)
- Final valve name: ARIEL or TERP.
