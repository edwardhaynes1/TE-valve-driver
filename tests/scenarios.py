"""Scripted control scenarios on a fake clock, with a simple plant.

Used two ways:
  * once, against the original single-file driver, to record what the
    control law did (tests/golden/control_trace.json);
  * by test_control_equivalence.py, against the driver package, which must
    reproduce that record exactly.

The plant is deliberately simple. It is not a model of the real valve; it
only has to exercise every branch (burst, coast, seek, creep, opening,
tracking, trips) in a repeatable way.

An adapter hides where the code lives. It must provide:
    command(**kw)                  heater_command
    compute(temp, healthy, dt, vac, vac_status, vac_healthy) -> duty
    heater                         the heater state dict
    set_upstream(bar, t)           Keller reading as seen by the pressure loop
    set_clock(fn)                  replace the control law's time source
    VAC_OVER, VAC_SATURATED, VAC_ERROR   the gauge status strings
    reset()                        fresh heater state, events cleared
    events()                       list of event texts logged so far
"""

import math
import random

DT = 0.25                     # control period (LABJACK_SAMPLE_HZ = 4)
P_FULL = 24.0 ** 2 / 88.0     # W at 100 % duty

# Heater state keys recorded at every step
TRACE_KEYS = ("setpoint_C", "p_phase", "p_burst", "t_burst", "trip_reason",
              "armed", "p_override", "p_ramping", "p_goal", "t_brake")


class Plant:
    """Two thermal nodes plus a valve that snaps open with hysteresis."""

    def __init__(self, t0=25.0, amb=25.0, seed=1):
        self.th = t0          # heater / thermocouple node
        self.tb = t0          # valve body
        self.amb = amb
        self.open = False
        self.base = 1.5e-7
        self.rng = random.Random(seed)

    def step(self, duty, dt):
        c_h, c_b, r_amb, r_hb = 6.0, 10.0, 16.0, 4.0
        q_hb = (self.th - self.tb) / r_hb
        self.th += dt * (P_FULL * duty - (self.th - self.amb) / r_amb - q_hb) / c_h
        self.tb += dt * q_hb / c_b
        if not self.open and self.tb > 40.0:
            self.open = True
        elif self.open and self.tb < 39.0:
            self.open = False
        self.base *= 1 - 1e-5 * dt

    def temp(self):
        return round(self.th + self.rng.gauss(0, 0.03), 3)

    def vac(self):
        p = self.base
        if self.open:
            p += 6e-7 * math.exp((self.tb - 40.0) / 3.0)
        return p * (1 + self.rng.gauss(0, 0.004))


class Clock:
    def __init__(self, t0=1_000_000.0):
        self.t = t0

    def __call__(self):
        return self.t


def save(results, path):
    import gzip, json
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(results, f, separators=(",", ":"), ensure_ascii=False)


def load(path):
    import gzip, json
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _snap(h):
    out = {}
    for k in TRACE_KEYS:
        v = h[k]
        out[k] = round(v, 9) if isinstance(v, float) else v
    return out


def run(adapter, name, script, seconds, plant=None, upstream=2.76,
        vac_fn=None, temp_fn=None, healthy_fn=None):
    """Run one scenario. script: {step_index: callable(adapter, clock)}."""
    adapter.reset()
    clock = Clock()
    adapter.set_clock(clock)
    plant = plant or Plant()
    trace = []
    n_events = 0
    for k in range(int(seconds / DT)):
        if k in script:
            script[k](adapter, clock)
        adapter.set_upstream(upstream, clock.t)
        temp = temp_fn(k, plant) if temp_fn else plant.temp()
        vac = vac_fn(k, plant, adapter) if vac_fn else plant.vac()
        vac_status, vac_ok = None, True
        if isinstance(vac, tuple):
            vac, vac_status, vac_ok = vac
        healthy = healthy_fn(k) if healthy_fn else True
        duty = adapter.compute(temp, healthy, DT, vac, vac_status, vac_ok)
        plant.step(duty, DT)
        ev = adapter.events()
        row = dict(k=k, duty=round(duty, 9), **_snap(adapter.heater),
                   events=ev[n_events:])
        n_events = len(ev)
        trace.append(row)
        clock.t += DT
    return {"name": name, "trace": trace}


def cmd(**kw):
    return lambda a, c: a.command(**kw)


def at(seconds):
    return int(seconds / DT)


def all_scenarios(adapter):
    """Every scenario, in a fixed order."""
    out = []

    out.append(run(adapter, "manual_duty_and_clamp", {
        0: cmd(mode='manual', duty_cmd=0.3),
        at(1): cmd(armed=True),
        at(30): cmd(duty_cmd=1.2),
        at(45): cmd(duty_cmd=-0.1),
        at(50): cmd(armed=False),
    }, 60))

    out.append(run(adapter, "auto_burst_from_cold", {
        0: cmd(mode='auto-t', setpoint_C=45.0),
        at(1): cmd(armed=True),
    }, 900))

    out.append(run(adapter, "auto_small_step_then_big_step", {
        0: cmd(mode='auto-t', setpoint_C=27.0),
        at(1): cmd(armed=True),
        at(300): cmd(setpoint_C=50.0),
        at(700): cmd(setpoint_C=48.0),
    }, 1000))

    out.append(run(adapter, "pressure_from_cold_then_retarget", {
        0: cmd(mode='auto-p', p_target_mbar=1.5e-6),
        at(1): cmd(armed=True),
        at(900): cmd(p_target_mbar=5e-7),
        at(1300): cmd(p_target_mbar=1e-7),
        at(1600): cmd(p_target_mbar=2e-6),
    }, 2000))

    out.append(run(adapter, "pressure_warm_start_low_upstream", {
        0: cmd(mode='auto-p', p_target_mbar=1e-6),
        at(1): cmd(armed=True),
    }, 900, plant=Plant(t0=37.0, amb=30.0, seed=2), upstream=2.0))

    out.append(run(adapter, "pressure_target_below_baseline", {
        0: cmd(mode='auto-p', p_target_mbar=1e-7),
        at(1): cmd(armed=True),
        at(400): cmd(p_target_mbar=1e-6),
    }, 900, upstream=None))

    out.append(run(adapter, "mode_switching", {
        0: cmd(mode='auto-t', setpoint_C=42.0),
        at(1): cmd(armed=True),
        at(200): cmd(mode='auto-p', p_target_mbar=1e-6),
        at(600): cmd(mode='manual', duty_cmd=0.1),
        at(700): cmd(mode='auto-t', setpoint_C=38.0),
        at(800): cmd(armed=False),
        at(810): cmd(armed=True),
    }, 1000))

    out.append(run(adapter, "trip_thermocouple", {
        0: cmd(mode='auto-t', setpoint_C=40.0),
        at(1): cmd(armed=True),
    }, 30, healthy_fn=lambda k: k < at(10)))

    out.append(run(adapter, "trip_overtemperature", {
        0: cmd(mode='manual', duty_cmd=0.2),
        at(1): cmd(armed=True),
        at(20): cmd(armed=False),
        at(21): cmd(armed=True),
    }, 30, temp_fn=lambda k, p: 161.0 if at(10) <= k < at(12) else 30.0))

    def jump_clock(a, c):
        c.t += 3600.0
    out.append(run(adapter, "trip_max_run_time", {
        0: cmd(mode='manual', duty_cmd=0.2),
        at(1): cmd(armed=True),
        at(5): jump_clock,
    }, 10))

    out.append(run(adapter, "trip_overpressure_pressure_mode", {
        0: cmd(mode='auto-p', p_target_mbar=1e-6),
        at(1): cmd(armed=True),
    }, 40, vac_fn=lambda k, p, a: 1e-3 if k >= at(20) else p.vac()))

    out.append(run(adapter, "overpressure_ignored_in_manual", {
        0: cmd(mode='manual', duty_cmd=0.1),
        at(1): cmd(armed=True),
    }, 20, vac_fn=lambda k, p, a: (None, a.VAC_OVER, True)))

    out.append(run(adapter, "trip_gauge_saturated", {
        0: cmd(mode='auto-p', p_target_mbar=1e-6),
        at(1): cmd(armed=True),
    }, 20, vac_fn=lambda k, p, a: ((None, a.VAC_SATURATED, True)
                                   if k >= at(10) else p.vac())))

    out.append(run(adapter, "trip_gauge_dead_and_missed_reads", {
        0: cmd(mode='auto-p', p_target_mbar=1e-6),
        at(1): cmd(armed=True),
    }, 40, vac_fn=lambda k, p, a: ((None, a.VAC_ERROR, k < at(30))
                                   if k >= at(20) and k % 7 else p.vac())))

    out.append(run(adapter, "pressure_no_reading_before_init", {
        0: cmd(mode='auto-p', p_target_mbar=1e-6),
        at(1): cmd(armed=True),
    }, 10, vac_fn=lambda k, p, a: (None, None, True) if k < at(5) else p.vac()))

    return out
