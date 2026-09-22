"""Models of the TE-Valve rig fitted to the 21 Sept 2026 logs, for testing
the control law in closed loop (tests/test_closed_loop.py).

Thermal: three nodes — heater element, thermocouple, valve body — with
losses to ambient. Fitted separately to each run (least squares on the TC,
0.5 s rows, heater power from the logged ON time):

    run                        torque   rms error   worst
    te-sensor_20260921_150128  0.25     0.09 K      0.41 K
    te-sensor_20260921_152054  0.40     0.32 K      0.90 K
    te-sensor_20260921_173217  0.45     0.34 K      1.17 K

The three fits differ (the TC was remounted after the 17:20 fault), which
makes them a useful spread for robustness tests.

Valve: flow starts when the fast-lagged valve temperature reaches the
opening point, then grows exponentially with temperature (the e-fold), with
a slow soak (flow keeps rising for minutes at a constant TC, 173217 at
145 °C), hysteresis (closes well below where it opened), and upstream
pressure both lowering the opening point and scaling the flow. The shape is
from the logs; the spread of each parameter is what the tests vary.
"""

import math
import random

P_FULL = 24.0 ** 2 / 88.0      # W at 100 % duty

# (Ch J/K, Rh K/W, C J/K, k1 W/K, k2 W/K², ambient °C, Cb J/K, Rb K/W, kb W/K)
THERMAL_FITS = {
    "150128": (0.04984, 50, 1.7829, 0.012514, 0.00022574, 25.544, 4.5572, 26.852, 0.033706),
    "152054": (0.053567, 50, 1.7095, 0.0, 0.0003037, 27.943, 1.3337, 15.329, 0.04399),
    "173217": (0.73857, 36.328, 0.54823, 0.0, 6.1087e-05, 19.622, 22.3, 26.375, 0.057988),
}


class Thermal:
    """Heater → TC → body. step(duty, dt) applies the duty's mean power."""

    def __init__(self, fit="152054", t0=None, noise=0.05, seed=1):
        (self.ch, self.rh, self.c, self.k1, self.k2, self.amb,
         self.cb, self.rb, self.kb) = THERMAL_FITS[fit]
        t0 = self.amb if t0 is None else t0
        self.th = self.t = self.tb = t0
        self.noise = noise
        self.rng = random.Random(seed)
        self.powered = True        # False: heater supply off (SW171)

    def step(self, duty, dt, sub=5):
        p = P_FULL * duty if self.powered else 0.0
        h = dt / sub
        for _ in range(sub):
            q = (self.th - self.t) / self.rh
            qb = (self.t - self.tb) / self.rb
            d = self.t - self.amb
            self.th += h * (p - q) / self.ch
            self.t += h * (q - qb - self.k1 * d - self.k2 * d * abs(d)) / self.c
            self.tb += h * (qb - self.kb * (self.tb - self.amb)) / self.cb

    def temp(self):
        return self.t + self.rng.gauss(0, self.noise)


class Valve:
    """Chamber pressure for a valve temperature history.

    open_c     opening point, fast valve temperature, at upstream p0_bar
    efold      K per e-fold of flow
    rise_open  steady rise above baseline at the opening point, mbar, at p0_bar
    soak       share of the slow (150 s) temperature in the flow exponent
    hyst       closes this far below the opening point
    k_per_bar  opening-point shift per bar of upstream pressure
    flow_exp   flow ∝ upstream ^ this
    """

    def __init__(self, open_c, p0_bar, efold, rise_open=3e-7, soak=0.3, hyst=10.0,
                 k_per_bar=12.0, flow_exp=1.5, base=3e-7, seed=2):
        self.open_c, self.p0, self.efold = open_c, p0_bar, efold
        self.rise_open, self.soak, self.hyst = rise_open, soak, hyst
        self.k, self.n, self.base = k_per_bar, flow_exp, base
        self.tf = self.ts = None
        self.open = False
        self.rise = 0.0
        self.rng = random.Random(seed)

    def on_point(self, p_up):
        return self.open_c - self.k * (p_up - self.p0)

    def step(self, tc, p_up, dt):
        if self.tf is None:
            self.tf = self.ts = tc
        self.tf += dt / 5.0 * (tc - self.tf)     # follows the TC within ~5 s (16 Sept)
        self.ts += dt / 150.0 * (tc - self.ts)
        on = self.on_point(p_up)
        if not self.open and self.tf >= on:
            self.open = True
        elif self.open and self.tf < on - self.hyst:
            self.open = False
        target = 0.0
        if self.open:
            x = (self.tf - on + self.soak * (self.ts - self.tf)) / self.efold
            target = self.rise_open * (p_up / self.p0) ** self.n * math.exp(min(x, 12.0))
        self.rise += dt / 3.0 * (target - self.rise)       # chamber, ~3 s
        self.base *= 1 - 2e-6 * dt                          # slow pump-down

    def vac(self):
        p = (self.base + self.rise) * (1 + self.rng.gauss(0, 0.004))
        step = 0.0049                                       # IKR 270 via LabJack, decades
        return 10 ** (round(math.log10(p) / step) * step)
