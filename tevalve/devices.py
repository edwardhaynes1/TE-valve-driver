"""Hardware: Keller (RS485), LabJack U3 (vacuum gauge, MAX31856
thermocouple over SPI, heater gate), and their reconnecting threads.
"""


import math
import serial
import serial.tools.list_ports
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

try:
    from keller_protocol import keller_protocol as kp
    KELLER_LIB_AVAILABLE = True
except ImportError:
    KELLER_LIB_AVAILABLE = False
    print("Warning: keller-protocol not installed — Keller sensor disabled.")
    print("Install: pip install keller-protocol")

from . import shared
from .config import (
    HEATER_FIO, HEATER_I_AIN, HEATER_I_OFFSET, HEATER_I_SCALE,
    HEATER_PWM_PERIOD_S, HEATER_R_OHM, HEATER_SENSE_TICKS, HEATER_TICK_HZ,
    HEATER_V_AIN, HEATER_V_OFFSET, HEATER_V_RAIL, HEATER_V_SCALE,
    KELLER_ADDRESS, KELLER_BAUD, KELLER_ECHO, KELLER_POLL_HZ, KELLER_PORT,
    KELLER_TIMEOUT, LABJACK_AIN_SAT_V, LABJACK_FIO2_CHANNEL,
    LABJACK_SAMPLE_HZ, LJ_WATCHDOG_S, PRESSURE_BAD_READS_TO_TRIP,
    TC_BAD_READS_TO_TRIP, TC_FIO_CS, TC_FIO_SCK, TC_FIO_SDI, TC_FIO_SDO,
    TC_RETRY_S, VACUUM_AIN_SPECIAL, VACUUM_DIVIDER_RATIO,
    VACUUM_GAUGE_ERROR_V, VACUUM_GAUGE_MAX_V, VACUUM_GAUGE_MIN_V,
    VACUUM_INTERCEPT, VACUUM_SLOPE, VAC_ERROR, VAC_OVER, VAC_SATURATED,
    VAC_UNDER,
)
from .control import compute_duty, heater_trip, record_gate_edge
from .shared import log_event


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


def detect_keller_bus():
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
    interval = 1.0 / KELLER_POLL_HZ
    port, bus = initial_port, initial_bus

    while not shared.stop.is_set():
        # ── read loop — exits on error ─────────────────────────────────────
        try:
            while not shared.stop.is_set():
                t0 = time.time()
                p1   = bus.f73(KELLER_ADDRESS, 1)
                tob1 = bus.f73(KELLER_ADDRESS, 4)
                with shared.lock:
                    if p1 is not None:
                        shared.readings['keller_pressure_samples'].append(round(p1, 4))
                        shared.readings['keller_pressure_bar'] = p1
                        shared.readings['keller_pressure_t']   = t0
                        shared.up_chart.append(p1)
                    if tob1 is not None:
                        shared.readings['keller_temperature_samples'].append(round(tob1, 2))
                shared.stop.wait(timeout=max(0.0, interval - (time.time() - t0)))

        except Exception as e:
            log_event(f"Keller read error: {e} — reconnecting…")

        # ── null state immediately so GUI shows '---' ──────────────────────
        shared.keller_ok = False
        with shared.lock:
            shared.readings['keller_pressure_samples']    = []
            shared.readings['keller_temperature_samples'] = []
            shared.readings['keller_pressure_bar']        = None

        if shared.stop.is_set():
            break

        # ── scan for the sensor again ──────────────────────────────────────
        shared.stop.wait(timeout=5.0)
        new_port, new_bus = detect_keller_bus()
        if new_bus is not None:
            port, bus = new_port, new_bus
            shared.keller_ok = True
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
# shared.labjack_ok tracks the vacuum channel, shared.tc_ok tracks the thermocouple. They
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
    tick        = 1.0 / HEATER_TICK_HZ
    sensor_gap  = 1.0 / LABJACK_SAMPLE_HZ

    while not shared.stop.is_set():
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
            record_gate_edge(False, 0.0, "connect: forced low")
            with shared.heater_lock:
                shared.heater['armed']       = False
                shared.heater['duty_cmd']    = 0.0
                shared.heater['duty_actual'] = 0.0

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

            shared.labjack_ok = True
            sense = "".join(f" · FIO{ch} {nm}" for nm, ch in
                            (("V-sense", HEATER_V_AIN), ("I-sense", HEATER_I_AIN))
                            if ch is not None)
            rng = "0-3.6 V" if VACUUM_AIN_SPECIAL else "0-2.44 V"
            log_event(f"LabJack connected  FIO0 heater · FIO2 analog ({rng}){sense} · FIO4-7 SPI")
        except Exception as e:
            shared.labjack_ok = False
            shared.tc_ok = False
            log_event(f"LabJack connect failed: {e} — retry in 5 s")
            shared.stop.wait(timeout=5)
            continue

        # ── initialise the thermocouple IC ─────────────────────────────────
        shared.tc_ok       = _tc_try_init(lj, announce_failure=True)
        tc_announced = not shared.tc_ok      # failure already logged
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
        p_win        = deque(maxlen=win_len)   # measured V × I over one period
        low_i_ticks  = 0            # gate ON but little current
        stray_i_ticks = 0           # gate OFF but current flowing
        i_on_expect  = HEATER_V_RAIL / HEATER_R_OHM

        try:
            while not shared.stop.is_set():
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
                # Instantaneous power from whatever is measured: V × I if the
                # current is sensed (V measured, else the nominal rail while on),
                # V²/R if only the voltage is.
                if i_meas is not None:
                    v_use = v_meas if v_meas is not None else v_now
                    p_win.append(v_use * i_meas)
                elif v_meas is not None:
                    p_win.append(v_meas ** 2 / HEATER_R_OHM)

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
                        heater_trip("heater current with gate OFF")

                with shared.heater_lock:
                    shared.heater['out_high']    = out_high
                    shared.heater['v_now']       = v_now
                    shared.heater['i_now']       = i_now
                    shared.heater['rail_meas']   = rail
                    shared.heater['v_meas']      = v_meas
                    shared.heater['i_meas']      = i_meas
                    shared.heater['v_meas_mean'] = sum(v_win) / len(v_win) if v_win else None
                    shared.heater['i_meas_mean'] = sum(i_win) / len(i_win) if i_win else None
                    shared.heater['p_meas_mean'] = sum(p_win) / len(p_win) if p_win else None

                # ── sensors, sub-sampled ───────────────────────────────────
                if t0 >= next_sensor:
                    next_sensor = t0 + sensor_gap

                    # vacuum gauge (analogue)
                    try:
                        raw  = lj.getAIN(LABJACK_FIO2_CHANNEL,
                                         32 if VACUUM_AIN_SPECIAL else 31)
                        mbar = voltage_to_vacuum_mbar(raw)
                        status = vacuum_gauge_status(raw)
                        if status != vac_status:
                            if status is not None:
                                log_event(f"Vacuum gauge {status} — "
                                          f"{raw:.3f} V at LabJack, "
                                          f"{raw * VACUUM_DIVIDER_RATIO:.2f} V at gauge")
                            elif vac_status != "startup":
                                log_event("Vacuum gauge back in range")
                            vac_status = status
                        with shared.lock:
                            shared.readings['vacuum_chamber_mbar'] = mbar
                            shared.readings['vacuum_status']       = status
                            shared.readings['vacuum_gauge_V']      = raw * VACUUM_DIVIDER_RATIO
                            shared.vac_chart.append(mbar)
                        bad_vac_reads = 0 if mbar is not None else bad_vac_reads + 1
                        shared.labjack_ok = True
                    except Exception as e:
                        log_event(f"LabJack vacuum read error: {e}")
                        break   # reconnect the whole device

                    # thermocouple not talking: retry init periodically.
                    # (_tc_init blocks ~0.3 s; harmless, as the heater is
                    # already forced off whenever the thermocouple is down.)
                    if not shared.tc_ok and t0 >= tc_next_try:
                        tc_next_try = t0 + TC_RETRY_S
                        shared.tc_ok = _tc_try_init(lj, announce_failure=not tc_announced)
                        tc_announced = not shared.tc_ok
                        if shared.tc_ok:
                            bad_tc_reads = 0

                    # thermocouple (SPI) — only if init succeeded
                    if shared.tc_ok:
                        try:
                            fault = _tc_read_reg(lj, REG_FAULTSR)
                            temp  = _tc_decode_temp(_tc_read_regs(lj, REG_LTCBH, 3))
                            with shared.lock:
                                shared.readings['tc_fault'] = fault
                                # On any active fault, don't trust the temperature
                                shared.readings['te_temperature_degC'] = temp if fault == 0 else None
                                if fault == 0:
                                    shared.te_chart.append(temp)
                            bad_tc_reads = 0 if fault == 0 else bad_tc_reads + 1
                        except Exception as e:
                            shared.tc_ok = False
                            bad_tc_reads += 1
                            with shared.lock:
                                shared.readings['te_temperature_degC'] = None
                                shared.readings['tc_fault'] = None
                            log_event(f"MAX31856 read error: {e} — "
                                      f"re-initialising every {TC_RETRY_S:g} s")
                            tc_announced = True
                            tc_next_try  = t0 + TC_RETRY_S

                    # ── control law ────────────────────────────────────────
                    with shared.lock:
                        temp_now = shared.readings['te_temperature_degC']
                        vac_now  = shared.readings['vacuum_chamber_mbar']
                        vac_st   = shared.readings['vacuum_status']
                    healthy = shared.tc_ok and bad_tc_reads < TC_BAD_READS_TO_TRIP
                    dt      = max(1e-3, t0 - last_ctrl)
                    last_ctrl = t0
                    duty = compute_duty(
                        temp_now, healthy, dt,
                        vac=vac_now, vac_status=vac_st,
                        vac_healthy=bad_vac_reads < PRESSURE_BAD_READS_TO_TRIP)
                    with shared.heater_lock:
                        shared.heater['duty_actual'] = duty
                        p_mean = shared.heater['p_meas_mean']
                    if p_mean is None:
                        p_mean = duty * HEATER_V_RAIL ** 2 / HEATER_R_OHM
                    with shared.lock:
                        shared.heat_chart.append(p_mean)

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
                        with shared.lock:
                            shared.pwm_edges.append(dict(
                                timestamp=datetime.now().isoformat(timespec='milliseconds'),
                                gate='', duty=round(duty, 4), on_s='',
                                note=f"write error ({'ON' if want_high else 'OFF'} "
                                     f"requested), state unknown"))
                        break
                    record_gate_edge(out_high, duty)

                shared.stop.wait(timeout=max(0.0, tick - (time.time() - t0)))
        finally:
            shared.labjack_ok = False
            shared.tc_ok = False
            with shared.lock:
                shared.readings['vacuum_chamber_mbar']   = None
                shared.readings['te_temperature_degC']   = None
                shared.readings['tc_fault']              = None
                shared.readings['vacuum_status']         = None
                shared.readings['vacuum_gauge_V']        = None
            with shared.heater_lock:
                shared.heater['armed']       = False
                shared.heater['duty_cmd']    = 0.0
                shared.heater['duty_actual'] = 0.0
                shared.heater['p_init']      = True
                shared.heater.update(out_high=False, v_now=0.0, i_now=0.0,
                               rail_meas=None, v_meas=None, i_meas=None,
                               v_meas_mean=None, i_meas_mean=None,
                               p_meas_mean=None)
            # Belt and braces on the way out: force the gate low, then let go
            # of the watchdog so the device isn't left armed for the next user.
            why = "shutdown" if shared.stop.is_set() else "reconnect"
            try:
                lj.setDOState(HEATER_FIO, 0)
                record_gate_edge(False, 0.0, f"{why}: forced low")
            except Exception:
                with shared.heater_lock:
                    stuck_on = shared.heater['on_since'] is not None
                if stuck_on:
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
