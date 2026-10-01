"""A simulated rig for batch tests: the real controller heating a thermal
model of the valve, whose seat opens at a set temperature and throttles.

    TC      dT/dt  = (G·P − (T − ambient)) / TAU_TC     (P = duty × full power)
    seat    dTs/dt = (T − Ts) / TAU_SEAT                 (the seat lags the TC)
    valve   opens when Ts ≥ open_c + k_up·(P_up − 3 bar), shuts hysteresis below
    chamber p → base · (1 + A · exp((Ts − opening) / efold)) + fill jump, lag TAU_P

Realities from the 24 Sept 2026 logs, all optional: the upstream leak (bar/s),
the opening point moving with upstream (k_up, K/bar; −12 in the fit so far),
and filling upstream making the chamber jump (+fill_jump × base) and decay
over TAU_FILL.

G and TAU_TC roughly match the 21 Sept holds (2 W ≈ 90 °C) and bursts
(~2.5 °C/s at full power from cold).

body_tau (optional) makes it two-stage, as the 24 Sept 2026 cooling log
shows (the TC falls fast to the body temperature, then the body cools
slowly towards the lab):
    C1 dT/dt  = P − (T − Tb)/R1          C1 = 3 J/K, R1 = 20 K/W (60 s)
    C2 dTb/dt = (T − Tb)/R1 − (Tb − ambient)/R2    R2 = G − R1, C2 = body_tau/R2
Same steady state (G K/W); ambient is settable (a warmer lab).
"""
import math
import random

from driver import batch, config, controller, cycle, openmap, tmin, tminlog

G_K_PER_W = 33.0
TAU_TC = 80.0
TAU_SEAT = 5.0         # config: the valve follows the TC within ~2-6 s
TAU_P = 3.0
TAU_FILL = 60.0
AMBIENT = 23.0


class Rig:
    def __init__(self, open_c=60.0, hysteresis=5.0, efold=4.0, base=5e-7, a=0.05,
                 noise_dec=0.002, t0=1_000_000.0, seed=1, upstream_bar=3.0,
                 upstream_leak_bar_s=0.0, k_up=0.0, fill_jump=0.0, pump_tail=0.0,
                 open_scatter=0.0, body_tau=None, ambient=AMBIENT, start_c=None):
        self.open_c, self.hyst, self.efold, self.base, self.a = open_c, hysteresis, efold, base, a
        self.noise = noise_dec
        self.rng = random.Random(seed)
        self.now = t0
        self.ambient = ambient
        self.body_tau = body_tau
        self.T = self.Ts = self.Tb = start_c if start_c is not None else ambient
        self.p = base
        self.is_open = False
        self.h = controller.new_state()
        self.duty = 0.0
        self.upstream = upstream_bar
        self.leak = upstream_leak_bar_s
        self.k_up, self.fill_jump = k_up, fill_jump
        self.up_full = upstream_bar
        self.extra = pump_tail * base      # a slowly falling tail, like the pump-down
        self.tau_extra = 600.0 if pump_tail else TAU_FILL
        self.fills = 0
        self.open_scatter = open_scatter     # K, drawn afresh each time the valve shuts
        self.jitter = self.rng.gauss(0.0, open_scatter) if open_scatter else 0.0
        self.rows = []                 # (t, row) as the logger would record them
        self._row_on = 0.0
        self._row_t = t0
        self.vac_override = None       # (vac, status) to fake a gauge problem

    # the plant
    def advance(self, dt):
        P = config.heater_power_w(self.duty)
        if self.body_tau is None:
            self.T += dt * (G_K_PER_W * P - (self.T - self.ambient)) / TAU_TC
        else:
            c1, r1 = 3.0, 20.0
            r2 = G_K_PER_W - r1
            c2 = self.body_tau / r2
            q12 = (self.T - self.Tb) / r1
            self.T += dt * (P - q12) / c1
            self.Tb += dt * (q12 - (self.Tb - self.ambient) / r2) / c2
        self.Ts += dt * (self.T - self.Ts) / TAU_SEAT
        opening = self.opening()
        if not self.is_open and self.Ts >= opening:
            self.is_open = True
        elif self.is_open and self.Ts < opening - self.hyst:
            self.is_open = False
            if self.open_scatter:
                self.jitter = self.rng.gauss(0.0, self.open_scatter)
        target = self.base
        if self.is_open:
            target *= 1 + self.a * math.exp((self.Ts - opening) / self.efold)
        self.extra -= dt * self.extra / self.tau_extra
        target += self.extra
        self.p += dt * (target - self.p) / TAU_P
        self.upstream -= self.leak * dt
        self.now += dt

    def opening(self):
        return self.open_c + self.jitter + self.k_up * (self.upstream - 3.0)

    def fill(self):
        """Refill upstream by hand: the chamber jumps, then decays over TAU_FILL."""
        self.upstream = self.up_full
        self.extra = self.fill_jump * self.base
        self.tau_extra = TAU_FILL
        self.fills += 1

    def vac(self):
        if self.vac_override is not None:
            return self.vac_override
        return self.p * 10 ** self.rng.gauss(0.0, self.noise), None

    def heater(self):
        return dict(armed=self.h['armed'], trip_reason=self.h['trip_reason'],
                    t_burst=self.h['t_burst'])

    def command(self, cmd):
        controller.command(self.h, self.now, **cmd)

    def idle(self, seconds, dt=0.25):
        """Heater off for a while (as before a batch); returns the chamber
        readings as the driver keeps them: [(time, mbar)]."""
        hist = []
        for _ in range(int(seconds / dt)):
            self.duty = 0.0
            self.advance(dt)
            hist.append((self.now, self.vac()[0]))
        return hist

    def run_batch(self, n_tests=3, torque=0.3, max_s=6 * 3600, dt=0.25, hook=None,
                  history=(), known=None, old=None, retorqued=None, p_up=True):
        """Run a whole batch; returns (b, all messages). history: readings
        from before. A hand refill is rig.fill() from a hook. p_up=False: the
        Keller isn't read."""
        b = batch.new_batch(n_tests, torque, self.now, history, known=known,
                            old=old if old is not None else known, retorqued=retorqued)
        msgs_all, self.events = [], []
        while b['state'] == batch.RUNNING and self.now - b['started'] < max_s:
            vac, status = self.vac()
            up = self.upstream if p_up else None
            cmds, msgs, events = batch.step(b, self.now, self.T, vac, status, self.heater(),
                                            p_up=up, p_up_t=self.now if p_up else None)
            for c in cmds:
                self.command(c)
            msgs_all += msgs
            self.events += events
            if hook:
                hook(self, b)
            self.duty, cmsgs = controller.step(self.h, self.now, dt, self.T, True,
                                               vac=vac, vac_status=status,
                                               p_up=self.upstream, p_up_t=self.now,
                                               seat_nm=torque)
            msgs_all += cmsgs
            self._row_on += self.duty * dt
            self.advance(dt)
            if self.now - self._row_t >= config.LOG_INTERVAL_S - 1e-9:
                name, phase = batch.labels(b)
                self.rows.append((self.now, dict(
                    batch_run=name, batch_phase=phase, heater_on_s=self._row_on,
                    heater_P_mean_calc=config.heater_power_w(self.duty),
                    keller_pressure_bar=self.upstream, te_temperature_degC=self.T,
                    vacuum_chamber_mbar=vac)))
                self._row_on, self._row_t = 0.0, self.now
        return b, msgs_all

    def rows_of(self, run_name):
        return [(t, r) for t, r in self.rows if r['batch_run'] == run_name]

    def run_cycling(self, torque=0.45, seating="s1", max_s=3600, rows=None, hook=None,
                    deep_every=5, dt=0.25, stop_after=None, idle_s=60):
        """Cycle until max_s (or stop_after openings); the fit is redone
        after every opening, as cyclerun does. Returns (c, rows, fit, msgs).
        rows: openings already in the table (they are appended to)."""
        rows = [] if rows is None else rows
        c = cycle.new_cycling(torque, seating, self.now, self.idle(idle_s),
                              deep_every=deep_every)
        fit = [openmap.fit(rows)]

        def predict(bar, deep):
            p = fit[0].predict(seating, torque, bar, deep)
            return None if p is None else (p[0], p[1], fit[0].sd)
        msgs, self.events = [], []
        while c['state'] == cycle.RUNNING and self.now - c['started'] < max_s:
            vac, st = self.vac()
            cmds, m, ev = cycle.step(c, self.now, self.T, vac, st, self.heater(),
                                     self.upstream, self.now, predict)
            for x in cmds:
                self.command(x)
            msgs += m
            self.events += ev
            for kind, obj in ev:
                if kind == 'opening':
                    rows.append(cycle.opening_row(c, obj, "sim", obj['name']))
                    fit[0] = openmap.fit(rows)
            if hook:
                hook(self, c)
            if stop_after is not None and c['openings'] >= stop_after:
                for x in cycle.stop(c, self.now, "enough")[0]:
                    self.command(x)
                break
            self.duty, _ = controller.step(self.h, self.now, dt, self.T, True, vac=vac,
                                           vac_status=st, p_up=self.upstream,
                                           p_up_t=self.now, seat_nm=torque)
            self.advance(dt)
        return c, rows, fit[0], msgs

    def run_tmin(self, torque=0.45, seating="s1", target=3.0, band=0.05, max_s=6 * 3600,
                 rows=None, fit=None, hook=None, dt=0.25, idle_s=60, stop_after=None,
                 operator=None):
        """t-min-tune until it ends or max_s; every finished test is a row of
        `rows` (the t-min.csv stand-in) and the estimate is recomputed from
        them, as tminrun does. Returns (s, rows, msgs)."""
        rows = [] if rows is None else rows
        s = tmin.new_session(torque, seating, target, band, self.now, self.idle(idle_s),
                             operator_est=operator)

        def estimate(tgt):
            return tminlog.estimate(rows, seating, torque, tgt, fit, operator=operator)
        msgs, self.events = [], []
        while s['state'] == tmin.RUNNING and self.now - s['started'] < max_s:
            vac, st = self.vac()
            cmds, m, ev = tmin.step(s, self.now, self.T, vac, st, self.heater(),
                                    self.upstream, self.now, estimate)
            for x in cmds:
                self.command(x)
            msgs += m
            self.events += ev
            for kind, obj in ev:
                if kind == 'test_end':
                    slope = tminlog.slope_for(torque, fit)
                    rows.append({k: ('' if v is None else str(v)) for k, v in
                                 tmin.result_row(s, obj, slope, str(self.now)).items()})
            if hook:
                hook(self, s)
            if stop_after is not None and s['results'] >= stop_after:
                for x in tmin.stop(s, self.now, "enough")[0]:
                    self.command(x)
                break
            self.duty, cm = controller.step(self.h, self.now, dt, self.T, True, vac=vac,
                                            vac_status=st, p_up=self.upstream,
                                            p_up_t=self.now, seat_nm=torque)
            msgs += cm
            self.advance(dt)
        return s, rows, msgs
