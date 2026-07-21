#!/usr/bin/env python3
"""
TE-VALVE-DRIVER.py
==================
Live display and logging for the thermally-enabled (TE) valve test setup.

Logs three sensor channels with a robust, self-reconnecting architecture:
  • Upstream pressure + temperature  — Keller PAA-23SX-H2 (RS485/USB, K-114)
  • Vacuum chamber pressure          — LabJack U3, FIO2 (log-linear gauge)
  • TE valve temperature             — MAX31856 Type-K thermocouple, LabJack SPI

Each sensor runs on its own thread. If any device disconnects mid-session the
affected reading goes to '---' immediately (never a stale value) and the thread
scans for the device again before resuming — the rest of the system is
unaffected.

The heating element is NOT controlled by this program yet — that is a separate
step. This driver is monitoring + logging only.

GUI (Tkinter, standard library): a single "Live Log" window with device status,
live readouts, a pressure strip chart, and a scrolling event log.

Hardware
--------
  Keller PAA-23SX-H2   RS485/USB (K-114 adapter) — upstream P + T
  LabJack U3           FIO2 — vacuum gauge (analogue)
                       FIO4-7 — MAX31856 thermocouple (SPI)

MAX31856 SPI pins (from Ariel_Control_PCB schematic):
  FIO4 → SDO   FIO5 → SDI   FIO6 → SCK   FIO7 → !CS

CSV columns
-----------
  timestamp, keller_pressure_bar, keller_temperature_degC,
  n_keller_samples, vacuum_chamber_mbar, te_temperature_degC, tc_fault

Logging cadence
---------------
  One CSV row on a drift-free wall-clock cadence of LOG_INTERVAL_S seconds
  (first row written immediately at start).

Usage
-----
  python TE-VALVE-DRIVER.py
"""

import serial
import serial.tools.list_ports
import threading
import time
import csv
import os
import math
from collections import deque
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

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

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
# LabJack vacuum gauge (FIO2, analogue)
LABJACK_FIO2_CHANNEL  = 2          # AIN2 = FIO2
VACUUM_SLOPE          = 5.389      # log-linear calibration slope
VACUUM_INTERCEPT      = -11.329    # log-linear calibration intercept

# MAX31856 thermocouple (SPI on FIO4-7)
TC_FIO_SDO = 4    # MAX31856 SDO  → LabJack MISO
TC_FIO_SDI = 5    # LabJack MOSI  → MAX31856 SDI
TC_FIO_SCK = 6    # clock
TC_FIO_CS  = 7    # chip select, active low

# LabJack combined sample rate (both vacuum + thermocouple read here)
LABJACK_SAMPLE_HZ     = 4          # Hz — reads vacuum and TC each cycle

# Keller
KELLER_BAUD        = 9600
KELLER_ADDRESS     = 250        # bus address 0xFA — confirmed for this unit
KELLER_PORT        = None       # None = auto-detect
KELLER_TIMEOUT     = 0.3
KELLER_ECHO        = True       # K-114 adapter echoes TX
KELLER_POLL_HZ     = 4          # Keller read rate

LOG_INTERVAL_S     = 0.5        # seconds between logged rows (drift-free)
CHART_SECONDS      = 120        # pressure strip-chart window (s)
LOG_DIR            = str(Path(__file__).parent / "logs")
# ═══════════════════════════════════════════════════════════════════════════════

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

os.makedirs(LOG_DIR, exist_ok=True)


def _make_log_path():
    """Return a writable CSV path, retrying with a unique suffix if locked."""
    base = datetime.now().strftime('%Y%m%d_%H%M%S')
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"_{attempt}"
        path = os.path.join(LOG_DIR, f"te-sensor_{base}{suffix}.csv")
        try:
            fh = open(path, 'x', newline='')
            fh.close()
            return path
        except (PermissionError, FileExistsError):
            continue
    return f"te-sensor_{base}.csv"


LOG_FILE = _make_log_path()

# ─── Shared state ─────────────────────────────────────────────────────────────
_lock        = threading.Lock()        # protects _state / _events / _chart
_stop        = threading.Event()
_state       = dict(
    keller_pressure_samples     = [],    # accumulated between log rows, then averaged
    keller_temperature_samples  = [],    # accumulated between log rows, then averaged
    vacuum_chamber_mbar         = None,  # latest single reading
    te_temperature_degC         = None,  # latest MAX31856 reading
    tc_fault                    = None,  # latest fault register (0 = OK, None = no read)
)
_events      = deque(maxlen=200)                            # (timestamp, text)
_chart       = deque(maxlen=CHART_SECONDS * KELLER_POLL_HZ)     # recent upstream pressures
_kt_chart    = deque(maxlen=CHART_SECONDS * KELLER_POLL_HZ)     # recent Keller (upstream) temperatures
_vac_chart   = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ)  # recent vacuum readings
_te_chart    = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ)  # recent TE temperatures
_keller_ok   = False
_labjack_ok  = False   # vacuum gauge channel healthy
_tc_ok       = False   # thermocouple channel healthy


def log_event(text: str):
    """Record a timestamped event for the GUI log (and echo to console)."""
    stamp = datetime.now().strftime('%H:%M:%S')
    with _lock:
        _events.append((stamp, text))
    print(f"[{stamp}] {text}")


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


def _detect_keller_bus():
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
    global _keller_ok
    interval = 1.0 / KELLER_POLL_HZ
    port, bus = initial_port, initial_bus

    while not _stop.is_set():
        # ── read loop — exits on error ─────────────────────────────────────
        try:
            while not _stop.is_set():
                t0 = time.time()
                p1   = bus.f73(KELLER_ADDRESS, 1)
                tob1 = bus.f73(KELLER_ADDRESS, 4)
                with _lock:
                    if p1 is not None:
                        _state['keller_pressure_samples'].append(round(p1, 4))
                        _chart.append(p1)
                    if tob1 is not None:
                        _state['keller_temperature_samples'].append(round(tob1, 2))
                        _kt_chart.append(tob1)
                _stop.wait(timeout=max(0.0, interval - (time.time() - t0)))

        except Exception as e:
            log_event(f"Keller read error: {e} — reconnecting…")

        # ── null state immediately so GUI shows '---' ──────────────────────
        _keller_ok = False
        with _lock:
            _state['keller_pressure_samples']    = []
            _state['keller_temperature_samples'] = []

        if _stop.is_set():
            break

        # ── scan for the sensor again ──────────────────────────────────────
        _stop.wait(timeout=5.0)
        new_port, new_bus = _detect_keller_bus()
        if new_bus is not None:
            port, bus = new_port, new_bus
            _keller_ok = True
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


def _tc_init(lj):
    """Configure MAX31856 for automatic conversion, Type-K. Returns True if the
    register readback confirms SPI communication is working."""
    _tc_write_reg(lj, REG_CR0, 0x80)   # automatic conversion mode
    _tc_write_reg(lj, REG_CR1, 0x03)   # Type-K, single-sample averaging
    time.sleep(0.2)                    # allow first conversion (~155 ms)
    cr0 = _tc_read_reg(lj, REG_CR0)
    cr1 = _tc_read_reg(lj, REG_CR1)
    return (cr0 == 0x80 and cr1 == 0x03)


# ═══════════════════════════════════════════════════════════════════════════════
# LABJACK U3 — VACUUM PRESSURE (FIO2) + THERMOCOUPLE (FIO4-7)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Both LabJack-attached sensors are serviced by a single thread, because they
# share one U3 device handle. FIO2 is analogue (vacuum gauge) and FIO4-7 are
# digital SPI (thermocouple); configIO sets the analogue/digital split once.
#
# _labjack_ok tracks the vacuum channel, _tc_ok tracks the thermocouple. They
# are reported independently in the GUI so a fault on one doesn't mask the other.

def _voltage_to_vacuum_mbar(volts: float) -> float:
    return 10.0 ** (VACUUM_SLOPE * volts + VACUUM_INTERCEPT)


def labjack_thread():
    global _labjack_ok, _tc_ok
    if not LABJACK_AVAILABLE:
        return
    interval = 1.0 / LABJACK_SAMPLE_HZ

    while not _stop.is_set():
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
            # FIO2 analogue (vacuum), FIO4-7 digital (SPI). Mask 0x04 = FIO2 only.
            lj.configIO(FIOAnalog=0x04)
            _labjack_ok = True
            log_event("LabJack connected  FIO2 analog + FIO4-7 SPI")
        except Exception as e:
            _labjack_ok = False
            _tc_ok = False
            log_event(f"LabJack connect failed: {e} — retry in 5 s")
            _stop.wait(timeout=5)
            continue

        # ── initialise the thermocouple IC ─────────────────────────────────
        try:
            if _tc_init(lj):
                _tc_ok = True
                log_event("MAX31856 thermocouple init OK")
            else:
                _tc_ok = False
                log_event("MAX31856 did not respond — check FIO4-7 wiring")
        except Exception as e:
            _tc_ok = False
            log_event(f"MAX31856 init error: {e}")

        # ── read loop — breaks on error to trigger reconnect ───────────────
        try:
            while not _stop.is_set():
                t0 = time.time()

                # vacuum gauge (analogue)
                try:
                    raw  = lj.getAIN(LABJACK_FIO2_CHANNEL)
                    mbar = _voltage_to_vacuum_mbar(raw)
                    with _lock:
                        _state['vacuum_chamber_mbar'] = mbar
                        _vac_chart.append(mbar)
                    _labjack_ok = True
                except Exception as e:
                    log_event(f"LabJack vacuum read error: {e}")
                    break   # reconnect the whole device

                # thermocouple (SPI) — only if init succeeded
                if _tc_ok:
                    try:
                        fault = _tc_read_reg(lj, REG_FAULTSR)
                        temp  = _tc_decode_temp(_tc_read_regs(lj, REG_LTCBH, 3))
                        with _lock:
                            _state['tc_fault'] = fault
                            # On any active fault, don't trust the temperature
                            _state['te_temperature_degC'] = temp if fault == 0 else None
                            if fault == 0:
                                _te_chart.append(temp)
                    except Exception as e:
                        _tc_ok = False
                        with _lock:
                            _state['te_temperature_degC'] = None
                            _state['tc_fault'] = None
                        log_event(f"MAX31856 read error: {e}")

                _stop.wait(timeout=max(0.0, interval - (time.time() - t0)))
        finally:
            _labjack_ok = False
            _tc_ok = False
            with _lock:
                _state['vacuum_chamber_mbar']   = None
                _state['te_temperature_degC']   = None
                _state['tc_fault']              = None
            try:
                lj.close()
            except Exception:
                pass
            if not _stop.is_set():
                log_event("LabJack disconnected — attempting reconnect")


# ═══════════════════════════════════════════════════════════════════════════════
# CSV LOGGING THREAD  — drift-free cadence, immediate first row
#
# One output file per session: te-sensor_<ts>.csv
#
# Columns:
#   timestamp, keller_pressure_bar (mean), keller_temperature_degC (mean),
#   n_keller_samples, vacuum_chamber_mbar, te_temperature_degC, tc_fault
# ═══════════════════════════════════════════════════════════════════════════════

def logger_thread():
    with open(LOG_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'timestamp',
            'keller_pressure_bar',      # mean over interval (blank if no samples)
            'keller_temperature_degC',  # mean over interval (blank if no samples)
            'n_keller_samples',         # number of samples averaged
            'vacuum_chamber_mbar',
            'te_temperature_degC',
            'tc_fault',                 # MAX31856 fault register (0 = OK)
        ])
        f.flush()

        start = time.time()
        n = 0
        while not _stop.is_set():
            with _lock:
                p_samp = _state['keller_pressure_samples']
                t_samp = _state['keller_temperature_samples']
                p_mean = round(sum(p_samp) / len(p_samp), 4) if p_samp else None
                t_mean = round(sum(t_samp) / len(t_samp), 2) if t_samp else None
                n_k    = len(p_samp)
                _state['keller_pressure_samples']    = []
                _state['keller_temperature_samples'] = []

                vac     = _state['vacuum_chamber_mbar']
                te_temp = _state['te_temperature_degC']
                fault   = _state['tc_fault']

            ts = datetime.now().isoformat(timespec='milliseconds')
            writer.writerow([
                ts, p_mean, t_mean, n_k, vac, te_temp,
                fault if fault is not None else '',
            ])
            f.flush()

            n += 1
            target = start + n * LOG_INTERVAL_S
            sleep_for = target - time.time()
            if sleep_for < 0:
                n = max(n, math.ceil((time.time() - start) / LOG_INTERVAL_S))
                target = start + n * LOG_INTERVAL_S
                sleep_for = max(0.0, target - time.time())
            _stop.wait(timeout=sleep_for)


# ═══════════════════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════════════════
BG     = "#0c0c0c"
TEXT   = "#d0d0d0"
BRIGHT = "#ffffff"
DIM    = "#505050"
BORDER = "#2a2a2a"
WARN   = "#ff4040"
GRID   = "#1a1a1a"


class TEGui:
    """Single 'Live Log' window: device status, live readouts, pressure strip
    chart, and a scrolling event log. Monitoring only — no valve control."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TE Valve — Live Log")
        self.root.configure(bg=BG)
        self.root.geometry("640x900")
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)

        M = ("Consolas", "Menlo", "Courier New", "DejaVu Sans Mono", "monospace")
        self.f = self._pick_font(M, 11)

        self._build_log_window()
        self.root.after(150, self._poll)

    def _pick_font(self, families, size, weight="normal"):
        available = set(tkfont.families())
        fam = next((f for f in families if f in available), families[-1])
        return tkfont.Font(family=fam, size=size, weight=weight)

    def _build_log_window(self):
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=8, pady=8)

        self.status_text = tk.Text(outer, bg=BG, fg=TEXT, font=self.f,
                                   height=9, bd=0, highlightthickness=0,
                                   state="disabled", wrap="none", cursor="arrow")
        self.status_text.pack(fill="x")
        self.status_text.tag_config("bright", foreground=BRIGHT)
        self.status_text.tag_config("dim",    foreground=DIM)
        self.status_text.tag_config("ok",     foreground=BRIGHT)
        self.status_text.tag_config("err",    foreground=WARN)

        tk.Label(outer, text=f"─── upstream pressure (bar)  last {CHART_SECONDS}s",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(fill="x")
        self.canvas = tk.Canvas(outer, bg=BG, height=100,
                                highlightbackground=BORDER, highlightthickness=1)
        self.canvas.pack(fill="both", expand=True)

        tk.Label(outer, text=f"─── upstream temperature (Keller, °C)  last {CHART_SECONDS}s",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(fill="x")
        self.kt_canvas = tk.Canvas(outer, bg=BG, height=100,
                                   highlightbackground=BORDER, highlightthickness=1)
        self.kt_canvas.pack(fill="both", expand=True)

        tk.Label(outer, text=f"─── vacuum chamber (mbar, log)  last {CHART_SECONDS}s",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(fill="x")
        self.vac_canvas = tk.Canvas(outer, bg=BG, height=100,
                                    highlightbackground=BORDER, highlightthickness=1)
        self.vac_canvas.pack(fill="both", expand=True)

        tk.Label(outer, text=f"─── TE valve temperature (°C)  last {CHART_SECONDS}s",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(fill="x")
        self.te_canvas = tk.Canvas(outer, bg=BG, height=100,
                                   highlightbackground=BORDER, highlightthickness=1)
        self.te_canvas.pack(fill="both", expand=True)

        tk.Label(outer, text="─── event log",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(fill="x")
        self.logtext = tk.Text(outer, bg=BG, fg=TEXT, font=self.f,
                               height=6, bd=0, highlightthickness=0,
                               state="disabled", wrap="none", cursor="arrow")
        self.logtext.pack(fill="both", expand=True)

        tk.Label(outer, text=f"─── log: {LOG_FILE}",
                 font=self.f, fg=DIM, bg=BG, anchor="w").pack(fill="x")

    def _draw_chart(self, canvas, data, fmt="{:.3f}", log=False):
        """Draw a strip chart on *canvas*.

        data : sequence of values (oldest → newest)
        fmt  : format string for the y-axis tick labels
        log  : if True, plot log10 of the data (for vacuum pressure, which
               spans orders of magnitude). Non-positive values are skipped.
        """
        c = canvas
        c.delete("all")
        w = c.winfo_width() or 580
        h = c.winfo_height() or 100
        pad_l, pad_r, pad_y = 62, 8, 6

        for i in range(1, 4):
            y = pad_y + (h - 2 * pad_y) * i / 4
            c.create_line(pad_l, y, w - pad_r, y, fill=GRID)

        if len(data) < 2:
            c.create_text(w / 2, h / 2, text="waiting for data",
                          fill=DIM, font=self.f)
            return

        # Transform to plot space (log or linear)
        if log:
            plot_vals = [math.log10(v) for v in data if v is not None and v > 0]
        else:
            plot_vals = [v for v in data if v is not None]
        if len(plot_vals) < 2:
            c.create_text(w / 2, h / 2, text="waiting for data",
                          fill=DIM, font=self.f)
            return

        lo, hi = min(plot_vals), max(plot_vals)
        if hi - lo < 1e-9:
            lo -= 0.5
            hi += 0.5
        span = hi - lo
        n = len(plot_vals)

        # y-axis tick labels (convert back from log space if needed)
        for i in range(5):
            frac = i / 4
            y = (h - pad_y) - (h - 2 * pad_y) * frac
            plot_val = lo + span * frac
            real_val = (10 ** plot_val) if log else plot_val
            label = fmt.format(real_val)
            c.create_text(pad_l - 4, y, text=label,
                          fill=DIM, font=self.f, anchor="e")

        pts = []
        for i, v in enumerate(plot_vals):
            x = pad_l + (w - pad_l - pad_r) * i / (n - 1)
            y = (h - pad_y) - (h - 2 * pad_y) * (v - lo) / span
            pts.extend((x, y))
        c.create_line(*pts, fill=BRIGHT, width=1)

    def _poll(self):
        with _lock:
            p_samp = _state['keller_pressure_samples']
            t_samp = _state['keller_temperature_samples']
            p       = (sum(p_samp) / len(p_samp)) if p_samp else None
            t       = (sum(t_samp) / len(t_samp)) if t_samp else None
            vac     = _state['vacuum_chamber_mbar']
            te_temp = _state['te_temperature_degC']
            fault   = _state['tc_fault']
            chart     = list(_chart)
            kt_chart  = list(_kt_chart)
            vac_chart = list(_vac_chart)
            te_chart  = list(_te_chart)
            events    = list(_events)

        p_s  = f"{p:.4f} bar"     if p       is not None else "---"
        t_s  = f"{t:.1f} °C"      if t       is not None else "---"
        v_s  = f"{vac:.2e} mbar"  if vac     is not None else "---"
        te_s = f"{te_temp:.2f} °C" if te_temp is not None else "---"

        # thermocouple fault annotation
        fault_note = ""
        if fault:
            names = [d for b, d in FAULT_BITS.items() if fault & b]
            fault_note = "  [" + ", ".join(names) + "]"

        st = self.status_text
        st.configure(state="normal")
        st.delete("1.0", "end")
        st.insert("end", "TE-VALVE-DRIVER\n", "bright")
        for lbl, ok, avail in (
            (f"[KELLER:{'OK' if _keller_ok else '--'}]",  _keller_ok,  True),
            (f"[VACUUM:{'OK' if _labjack_ok else '--'}]", _labjack_ok, LABJACK_AVAILABLE),
            (f"[TE-TEMP:{'OK' if _tc_ok else '--'}]",     _tc_ok,      LABJACK_AVAILABLE),
        ):
            tag = "ok" if ok else ("dim" if not avail else "err")
            st.insert("end", lbl + "  ", tag)
        st.insert("end", "\n\n")
        st.insert("end", "UPSTREAM P   ", "dim") ; st.insert("end", p_s + "\n",
                  "bright" if p is not None else "dim")
        st.insert("end", "UPSTREAM T   ", "dim") ; st.insert("end", t_s + "\n",
                  "bright" if t is not None else "dim")
        st.insert("end", "VACUUM       ", "dim") ; st.insert("end", v_s + "\n",
                  "bright" if vac is not None else "dim")
        st.insert("end", "TE VALVE T   ", "dim")
        st.insert("end", te_s, "bright" if te_temp is not None else "dim")
        st.insert("end", fault_note + "\n", "err" if fault_note else "dim")
        st.configure(state="disabled")

        self._draw_chart(self.canvas,     chart,     fmt="{:.3f}")
        self._draw_chart(self.kt_canvas,  kt_chart,  fmt="{:.1f}")
        self._draw_chart(self.vac_canvas, vac_chart, fmt="{:.1e}", log=True)
        self._draw_chart(self.te_canvas,  te_chart,  fmt="{:.1f}")

        text = "\n".join(f"> {s}  {m}" for s, m in events[-200:])
        if getattr(self, "_last_log_text", None) != text:
            self._last_log_text = text
            self.logtext.configure(state="normal")
            self.logtext.delete("1.0", "end")
            self.logtext.insert("1.0", text)
            self.logtext.see("end")
            self.logtext.configure(state="disabled")

        if not _stop.is_set():
            self.root.after(150, self._poll)

    def shutdown(self):
        _stop.set()
        print(f"Log saved: {LOG_FILE}")
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global _keller_ok

    print("\nTE-VALVE-DRIVER — startup")
    print("─" * 50)

    print("Scanning for Keller sensor...")
    keller_port, keller_bus = _detect_keller_bus()
    _keller_ok = keller_bus is not None
    if not _keller_ok:
        print("  [Keller] NOT FOUND — upstream pressure/temperature will be blank.")

    if LABJACK_AVAILABLE:
        print("LabJack will connect on its own thread (vacuum + thermocouple).")
    else:
        print("LabJack not available — vacuum and TE temperature disabled.")

    print(f"\nLogging to: {LOG_FILE}")
    print(f"Log cadence: every {LOG_INTERVAL_S:g} s (drift-free)")
    print("─" * 50)

    # ── Start worker threads ───────────────────────────────────────────────
    if keller_bus is not None:
        threading.Thread(target=keller_thread, args=(keller_port, keller_bus),
                         daemon=True).start()
    if LABJACK_AVAILABLE:
        threading.Thread(target=labjack_thread, daemon=True).start()
    threading.Thread(target=logger_thread, daemon=True).start()

    log_event("System started")
    if _keller_ok:
        log_event(f"Keller online · {keller_port}")

    # ── GUI runs on the main thread ────────────────────────────────────────
    TEGui().run()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        print("\n" + "=" * 60)
        print("FATAL ERROR:")
        print("=" * 60)
        traceback.print_exc()
    finally:
        input("\nPress Enter to close...")
