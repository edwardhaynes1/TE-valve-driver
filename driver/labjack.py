"""LabJack U3: the heater gate (FIO0), the IKR 270 vacuum gauge (FIO2), the
MAX31856 thermocouple (FIO4-7, see thermocouple.py) and the optional heater
voltage / current sense inputs — all on one thread, because the U3 handle
can't be shared.

    labjack_thread()                 connect, run, reconnect — until stop
    voltage_to_vacuum_mbar(volts)    gauge conversion (None if invalid)
    vacuum_gauge_status(volts)       None, or why the reading is invalid
    check_sense_pins()               None, or what's wrong with the pin settings
    LABJACK_AVAILABLE                is LabJackPython installed?

One tick (every 1/HEATER_TICK_HZ s) of a connected session:
    sense the heater → [every 1/LABJACK_SAMPLE_HZ s: read the vacuum gauge →
    service the thermocouple → run the controller] → drive the gate
Any device error ends the session; the gate is forced low and the thread
reconnects.
"""

import math
import time
from collections import deque
from datetime import datetime

try:
    import u3
    LABJACK_AVAILABLE = True
except ImportError:
    LABJACK_AVAILABLE = False
    print("Warning: LabJackPython not installed — vacuum gauge and thermocouple disabled.")
    print("Install: pip install LabJackPython")

from . import shared, thermocouple
from .config import (
    HEATER_FIO, HEATER_I_AIN, HEATER_I_OFFSET, HEATER_I_SCALE,
    HEATER_PWM_PERIOD_S, HEATER_SENSE_TICKS, HEATER_TICK_HZ, HEATER_V_AIN,
    HEATER_V_OFFSET, HEATER_V_SCALE, LABJACK_AIN_SAT_V, LABJACK_FIO2_CHANNEL,
    LABJACK_SAMPLE_HZ, LJ_WATCHDOG_S, PRESSURE_BAD_READS_TO_TRIP,
    TC_BAD_READS_TO_TRIP, TC_FIO_CS, TC_FIO_SCK, TC_FIO_SDI, TC_FIO_SDO,
    TC_RETRY_S, VACUUM_AIN_SPECIAL, VACUUM_DIVIDER_RATIO,
    VACUUM_GAUGE_ERROR_V, VACUUM_GAUGE_MAX_V, VACUUM_GAUGE_MIN_V,
    VACUUM_INTERCEPT, VACUUM_SLOPE, VAC_ERROR, VAC_OVER, VAC_SATURATED,
    VAC_UNDER, heater_current_a, heater_power_w, heater_voltage_v,
    power_from_voltage_w,
)
from .control import (
    apply_duty, compute_duty, force_off, gate_on_recorded, heater_trip,
    record_gate_edge,
)
from .shared import log_event


# ═══════════════════════════════════════════════════════════════════════════════
# LABJACK U3 — VACUUM PRESSURE (FIO2) + THERMOCOUPLE (FIO4-7)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Both LabJack-attached sensors are serviced by a single thread, because they
# share one U3 device handle. FIO2 is analogue (vacuum gauge) and FIO4-7 are
# digital SPI (thermocouple); configIO sets the analogue/digital split once.
#
# The labjack health flag tracks the vacuum channel, tc the thermocouple. They
# are reported independently in the GUI so a fault on one doesn't mask the other.

def vacuum_gauge_status(volts: float):
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


def voltage_to_vacuum_mbar(volts: float):
    """LabJack voltage → chamber pressure in mbar, or None if the gauge
    signal is outside its valid measuring range (never a made-up number)."""
    if vacuum_gauge_status(volts) is not None:
        return None
    return 10.0 ** (VACUUM_SLOPE * volts + VACUUM_INTERCEPT)


def check_sense_pins():
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


def labjack_thread():
    """Owns the U3 handle. Does everything on the device: heater output,
    vacuum gauge, thermocouple. Single-threaded by design — the U3 handle is
    not safe to share, and the heater must never wait behind another thread.

    Runs at HEATER_TICK_HZ so the software time-proportioning output has
    reasonable resolution; sensors are sub-sampled to LABJACK_SAMPLE_HZ."""
    if not LABJACK_AVAILABLE:
        return
    while not shared.stop.is_set():
        lj = _connect()
        if lj is not None:
            _Session(lj).run()


def _connect():
    """Open and configure the U3 with the gate low. Returns the handle, or
    None after logging the failure and waiting 5 s."""
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
        record_gate_edge(False, 0.0, "connect: forced low")
        force_off()

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

        shared.set_health(labjack=True)
        sense = "".join(f" · FIO{ch} {nm}" for nm, ch in
                        (("V-sense", HEATER_V_AIN), ("I-sense", HEATER_I_AIN))
                        if ch is not None)
        rng = "0-3.6 V" if VACUUM_AIN_SPECIAL else "0-2.44 V"
        log_event(f"LabJack connected  FIO0 heater · FIO2 analog ({rng}){sense} · FIO4-7 SPI")
        return lj
    except Exception as e:
        shared.set_health(labjack=False, tc=False)
        log_event(f"LabJack connect failed: {e} — retry in 5 s")
        shared.stop.wait(timeout=5)
        return None


def _period_mean(samples, now):
    """Time-weighted mean of the (time, value) samples over the last PWM
    period, ending at `now` (the newest sample's time); older samples are
    dropped. None if there are none.

    Each sample describes the heater from the previous sample's time up to
    its own — it is read before the tick switches the gate — so it is
    weighted by that interval. Equal weights would let an irregular tick
    (a busy machine stalling for a moment) tip the mean towards whichever
    state happened to be sampled more often."""
    start = now - HEATER_PWM_PERIOD_S
    while samples and samples[0][0] <= start:
        samples.popleft()
    if not samples:
        return None
    total = span = 0.0
    prev = start                  # the first interval is clipped to the window
    for t, value in samples:
        total += value * (t - prev)
        span += t - prev
        prev = t
    return total / span if span > 0 else samples[-1][1]


class _DeviceLost(Exception):
    """A read or write failed: end the session and reconnect."""


class _Session:
    """One connection to the U3, from connect to disconnect."""

    def __init__(self, lj):
        self.lj = lj
        now = time.time()
        # thermocouple
        self.tc_ok = False
        self.tc_announced = False     # failure already logged
        self.tc_next_try = now
        self.bad_tc_reads = 0
        # vacuum gauge
        self.vac_status = "startup"   # last gauge status, so changes are logged once
        self.bad_vac_reads = 0        # consecutive invalid gauge readings
        # controller and gate
        self.next_sensor = 0.0        # time.time() of the next sensor read
        self.last_ctrl = now
        self.cycle_start = now        # start of the current PWM period
        self.duty = 0.0
        self.out_high = False
        # heater sense: (time, value) samples from the last PWM period. Kept
        # by time, not by count: ticks can run long (Windows rounds waits up
        # to its ~15.6 ms timer steps), and a fixed count would then span
        # more than one period and swing with the cycle.
        self.v_win = deque()          # measured V
        self.i_win = deque()          # measured I
        self.p_win = deque()          # measured V × I
        self.low_i_ticks = 0          # gate ON but little current
        self.stray_i_ticks = 0        # gate OFF but current flowing

    # ── the loop ────────────────────────────────────────────────────────────
    def run(self):
        self._set_tc(thermocouple.try_init(self.lj, announce_failure=True))
        self.tc_announced = not self.tc_ok
        self.tc_next_try = time.time() + TC_RETRY_S
        # timing starts after the thermocouple init (which takes ~0.3 s)
        self.last_ctrl = self.cycle_start = time.time()
        tick = 1.0 / HEATER_TICK_HZ
        try:
            while not shared.stop.is_set():
                t0 = time.time()
                self._sense_heater(t0)
                if t0 >= self.next_sensor:
                    self.next_sensor = t0 + 1.0 / LABJACK_SAMPLE_HZ
                    self._read_vacuum()
                    self._service_thermocouple(t0)
                    self._run_controller(t0)
                self._drive_gate(t0)
                shared.stop.wait(timeout=max(0.0, tick - (time.time() - t0)))
        except _DeviceLost:
            pass
        finally:
            self._release()

    def _set_tc(self, ok):
        self.tc_ok = ok
        shared.set_health(tc=ok)

    # ── heater V / I: calculated every tick, measured if wired ─────────────
    def _sense_heater(self, t0):
        # Read BEFORE this tick's output write, so the samples belong to
        # out_high as it is right now.
        lj, out_high = self.lj, self.out_high
        v_now  = heater_voltage_v() if out_high else 0.0
        i_now  = heater_current_a() if out_high else 0.0
        rail = v_meas = i_meas = None
        try:
            if HEATER_V_AIN is not None:
                rail   = lj.getAIN(HEATER_V_AIN) * HEATER_V_SCALE + HEATER_V_OFFSET
                v_meas = rail if out_high else 0.0
                self.v_win.append((t0, v_meas))
            if HEATER_I_AIN is not None:
                i_meas = lj.getAIN(HEATER_I_AIN) * HEATER_I_SCALE + HEATER_I_OFFSET
                self.i_win.append((t0, i_meas))
        except Exception as e:
            log_event(f"Heater sense read error: {e}")
            raise _DeviceLost from e
        # Instantaneous power from whatever is measured: V × I if the
        # current is sensed (V measured, else the nominal rail while on),
        # V²/R if only the voltage is.
        if i_meas is not None:
            v_use = v_meas if v_meas is not None else v_now
            self.p_win.append((t0, v_use * i_meas))
        elif v_meas is not None:
            self.p_win.append((t0, power_from_voltage_w(v_meas)))

        if i_meas is not None:
            self._check_current(i_meas)

        shared.store_heater_output(
            out_high=out_high, v_now=v_now, i_now=i_now, rail_meas=rail,
            v_meas=v_meas, i_meas=i_meas,
            v_meas_mean=_period_mean(self.v_win, t0),
            i_meas_mean=_period_mean(self.i_win, t0),
            p_meas_mean=_period_mean(self.p_win, t0))

    def _check_current(self, i_meas):
        """Plausibility of the measured current against the gate state."""
        i_on_expect = heater_current_a()
        if self.out_high:
            self.low_i_ticks = self.low_i_ticks + 1 if i_meas < 0.5 * i_on_expect else 0
        else:
            self.stray_i_ticks = self.stray_i_ticks + 1 if i_meas > 0.5 * i_on_expect else 0
        if self.low_i_ticks == HEATER_SENSE_TICKS:
            log_event(f"WARNING: gate ON but heater current {i_meas:.3f} A "
                      f"(expected ~{i_on_expect:.3f} A) — SW171 off, "
                      f"24 V missing, or element open?")
        if self.stray_i_ticks == HEATER_SENSE_TICKS:
            log_event(f"WARNING: {i_meas:.3f} A flowing with gate OFF — "
                      f"Q171 may be shorted. Software cannot stop this: "
                      f"open SW171 / switch off 24 V.")
            heater_trip("heater current with gate OFF")

    # ── vacuum gauge (analogue) ────────────────────────────────────────────
    def _read_vacuum(self):
        try:
            raw  = self.lj.getAIN(LABJACK_FIO2_CHANNEL,
                                  32 if VACUUM_AIN_SPECIAL else 31)
            mbar = voltage_to_vacuum_mbar(raw)
            status = vacuum_gauge_status(raw)
            if status != self.vac_status:
                if status is not None:
                    log_event(f"Vacuum gauge {status} — "
                              f"{raw:.3f} V at LabJack, "
                              f"{raw * VACUUM_DIVIDER_RATIO:.2f} V at gauge")
                elif self.vac_status != "startup":
                    log_event("Vacuum gauge back in range")
                self.vac_status = status
            shared.store_vacuum(mbar, status, raw * VACUUM_DIVIDER_RATIO)
            self.bad_vac_reads = 0 if mbar is not None else self.bad_vac_reads + 1
            shared.set_health(labjack=True)
        except Exception as e:
            log_event(f"LabJack vacuum read error: {e}")
            raise _DeviceLost from e    # reconnect the whole device

    # ── thermocouple (SPI) ─────────────────────────────────────────────────
    def _service_thermocouple(self, t0):
        # Not talking: retry init periodically. (try_init blocks ~0.3 s;
        # harmless, as the heater is already forced off whenever the
        # thermocouple is down.)
        if not self.tc_ok and t0 >= self.tc_next_try:
            self.tc_next_try = t0 + TC_RETRY_S
            self._set_tc(thermocouple.try_init(
                self.lj, announce_failure=not self.tc_announced))
            self.tc_announced = not self.tc_ok
            if self.tc_ok:
                self.bad_tc_reads = 0

        # Read — only if init succeeded.
        if self.tc_ok:
            try:
                fault = thermocouple.read_fault(self.lj)
                temp  = thermocouple.read_temperature(self.lj)
                shared.store_valve_temp(temp, fault)
                self.bad_tc_reads = 0 if fault == 0 else self.bad_tc_reads + 1
            except Exception as e:
                self._set_tc(False)
                self.bad_tc_reads += 1
                shared.clear_valve_temp()
                log_event(f"MAX31856 read error: {e} — "
                          f"re-initialising every {TC_RETRY_S:g} s")
                self.tc_announced = True
                self.tc_next_try  = t0 + TC_RETRY_S

    # ── control law ────────────────────────────────────────────────────────
    def _run_controller(self, t0):
        now_r    = shared.latest()
        healthy  = self.tc_ok and self.bad_tc_reads < TC_BAD_READS_TO_TRIP
        dt       = max(1e-3, t0 - self.last_ctrl)
        self.last_ctrl = t0
        self.duty = compute_duty(
            now_r['te_temperature_degC'], healthy, dt,
            vac=now_r['vacuum_chamber_mbar'], vac_status=now_r['vacuum_status'],
            vac_healthy=self.bad_vac_reads < PRESSURE_BAD_READS_TO_TRIP)
        apply_duty(self.duty)
        p_mean = shared.heater_output()['p_meas_mean']
        if p_mean is None:
            p_mean = heater_power_w(self.duty)
        shared.push_power(p_mean)

    # ── time-proportioning output on FIO0 ──────────────────────────────────
    def _drive_gate(self, t0):
        # Plain digital toggling, no timers: FIO4-7 stay free for SPI.
        period, duty = HEATER_PWM_PERIOD_S, self.duty
        phase = (t0 - self.cycle_start) % period
        if t0 - self.cycle_start >= period:
            self.cycle_start += period * math.floor((t0 - self.cycle_start) / period)
        want_high = (duty > 0.0) and (phase < duty * period)
        if want_high == self.out_high:
            return
        try:
            self.lj.setDOState(HEATER_FIO, 1 if want_high else 0)
            self.out_high = want_high
        except Exception as e:
            log_event(f"Heater output write error: {e}")
            shared.queue_edge(dict(
                timestamp=datetime.now().isoformat(timespec='milliseconds'),
                gate='', duty=round(duty, 4), on_s='',
                note=f"write error ({'ON' if want_high else 'OFF'} "
                     f"requested), state unknown"))
            raise _DeviceLost from e
        record_gate_edge(self.out_high, duty)

    # ── end of session ─────────────────────────────────────────────────────
    def _release(self):
        lj = self.lj
        shared.set_health(labjack=False)
        self._set_tc(False)
        shared.clear_labjack()
        shared.clear_heater_output()
        force_off(device_lost=True)
        # Belt and braces on the way out: force the gate low, then let go
        # of the watchdog so the device isn't left armed for the next user.
        why = "shutdown" if shared.stop.is_set() else "reconnect"
        try:
            lj.setDOState(HEATER_FIO, 0)
            record_gate_edge(False, 0.0, f"{why}: forced low")
        except Exception:
            if gate_on_recorded():
                record_gate_edge(False, 0.0,
                                 f"{why}: force-low write FAILED — gate state "
                                 f"unknown, LabJack watchdog should drop it")
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
        if not shared.stop.is_set():
            log_event("LabJack disconnected — heater forced off, reconnecting")
