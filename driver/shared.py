"""What the device, logger and GUI threads share: sensor readings, chart
history, the event log, gate edges waiting to be logged, and health flags.
(The heater state is not here: control.py owns it.)

Everything goes through the functions below. The containers and the lock
are private, so the locking rules live in this file only.

    stop                          shutdown signal (a threading.Event)

    device threads write:         store_keller, clear_keller, store_vacuum,
                                  store_valve_temp, clear_valve_temp,
                                  clear_labjack, push_power, set_health
    anyone reads:                 latest, upstream, charts, recent_events,
                                  health
    the logger takes:             take_log_readings, take_events,
                                  put_back_events, take_edges, put_back_edges
    control.py queues:            queue_edge
    events:                       log_event, ascii_text
    tests:                        reset, pending_edges
"""

import threading
from collections import deque
from datetime import datetime

from .config import CHART_SECONDS, KELLER_POLL_HZ, LABJACK_SAMPLE_HZ

stop = threading.Event()

_lock = threading.Lock()          # guards everything below


def _fresh_readings():
    return dict(
        keller_pressure_samples     = [],    # accumulated between log rows, then averaged
        keller_temperature_samples  = [],    # accumulated between log rows, then averaged
        keller_pressure_bar         = None,  # latest single upstream reading (absolute)
        keller_pressure_t           = None,  # time of that reading
        vacuum_chamber_mbar         = None,  # latest reading, None if invalid
        vacuum_status               = None,  # None = valid, else a VAC_* reason string
        vacuum_gauge_V              = None,  # gauge-side signal voltage, V
        te_temperature_degC         = None,  # latest valve temperature, None on a fault
        tc_fault                    = None,  # MAX31856 fault register (0 = OK, None = no read)
    )


def _fresh_charts():
    return dict(
        upstream   = deque(maxlen=CHART_SECONDS * KELLER_POLL_HZ),     # bar abs, raw
        vacuum     = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ),  # mbar
        valve_temp = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ),  # °C
        power      = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ),  # W, period mean
    )


_readings = _fresh_readings()
_charts = _fresh_charts()
_events = deque(maxlen=200)       # (timestamp, text) for the GUI
_events_pending = []              # texts not yet written to the CSV
_edges_pending = []               # gate-edge rows not yet written to the _pwm CSV
_health = dict(keller=False,      # Keller answering
               labjack=False,     # vacuum gauge channel healthy
               tc=False,          # thermocouple channel healthy
               csv=None)          # logger: None = starting, True = writing, False = failing


# ─── device threads write ───────────────────────────────────────────────────

def store_keller(p_bar, t_chip, when):
    """One Keller poll. Either value may be None (not read)."""
    with _lock:
        if p_bar is not None:
            _readings['keller_pressure_samples'].append(round(p_bar, 4))
            _readings['keller_pressure_bar'] = p_bar
            _readings['keller_pressure_t'] = when
            _charts['upstream'].append(p_bar)
        if t_chip is not None:
            _readings['keller_temperature_samples'].append(round(t_chip, 2))


def clear_keller():
    """Keller lost: show '---' rather than the last value."""
    with _lock:
        _readings['keller_pressure_samples'] = []
        _readings['keller_temperature_samples'] = []
        _readings['keller_pressure_bar'] = None


def store_vacuum(mbar, status, gauge_v):
    with _lock:
        _readings['vacuum_chamber_mbar'] = mbar
        _readings['vacuum_status'] = status
        _readings['vacuum_gauge_V'] = gauge_v
        _charts['vacuum'].append(mbar)


def store_valve_temp(temp, fault):
    """One thermocouple read. On any fault the temperature is not trusted."""
    with _lock:
        _readings['tc_fault'] = fault
        _readings['te_temperature_degC'] = temp if fault == 0 else None
        if fault == 0:
            _charts['valve_temp'].append(temp)


def clear_valve_temp():
    with _lock:
        _readings['te_temperature_degC'] = None
        _readings['tc_fault'] = None


def clear_labjack():
    """LabJack lost: every reading that comes through it goes to '---'."""
    with _lock:
        for k in ('vacuum_chamber_mbar', 'te_temperature_degC', 'tc_fault',
                  'vacuum_status', 'vacuum_gauge_V'):
            _readings[k] = None


def push_power(watts):
    with _lock:
        _charts['power'].append(watts)


def set_health(**flags):
    """set_health(keller=True), set_health(labjack=False, tc=False), …"""
    unknown = set(flags) - set(_health)
    if unknown:
        raise KeyError(f"unknown health flag(s): {sorted(unknown)}")
    with _lock:
        _health.update(flags)


# ─── anyone reads ───────────────────────────────────────────────────────────

def latest():
    """A copy of the current readings (sample lists copied too)."""
    with _lock:
        out = dict(_readings)
        out['keller_pressure_samples'] = list(_readings['keller_pressure_samples'])
        out['keller_temperature_samples'] = list(_readings['keller_temperature_samples'])
    return out


def upstream():
    """(latest upstream pressure in bar, time it was read) — either may be None."""
    with _lock:
        return _readings['keller_pressure_bar'], _readings['keller_pressure_t']


def charts():
    """Copies of the chart histories: upstream, vacuum, valve_temp, power."""
    with _lock:
        return {k: list(v) for k, v in _charts.items()}


def recent_events():
    with _lock:
        return list(_events)


def health():
    with _lock:
        return dict(_health)


# ─── the logger takes ───────────────────────────────────────────────────────

def take_log_readings():
    """Readings for one log row. The Keller samples since the previous row
    are averaged and cleared."""
    with _lock:
        p_samp = _readings['keller_pressure_samples']
        t_samp = _readings['keller_temperature_samples']
        row = dict(
            p_mean=round(sum(p_samp) / len(p_samp), 4) if p_samp else None,
            t_mean=round(sum(t_samp) / len(t_samp), 2) if t_samp else None,
            n_keller=len(p_samp),
            vac=_readings['vacuum_chamber_mbar'],
            valve_temp=_readings['te_temperature_degC'],
            fault=_readings['tc_fault'],
            vac_status=_readings['vacuum_status'],
        )
        _readings['keller_pressure_samples'] = []
        _readings['keller_temperature_samples'] = []
    return row


def take_events():
    with _lock:
        taken = list(_events_pending)
        _events_pending.clear()
    return taken


def put_back_events(taken):
    """A write failed: keep these for the next row, ahead of newer ones."""
    with _lock:
        _events_pending[:0] = taken


def queue_edge(row):
    with _lock:
        _edges_pending.append(row)


def take_edges():
    with _lock:
        taken = list(_edges_pending)
        _edges_pending.clear()
    return taken


def put_back_edges(taken):
    with _lock:
        _edges_pending[:0] = taken


# ─── events ─────────────────────────────────────────────────────────────────

def log_event(text: str):
    """Record a timestamped event for the GUI log, the CSV 'events' column,
    and the console."""
    stamp = datetime.now().strftime('%H:%M:%S')
    with _lock:
        _events.append((stamp, text))
        _events_pending.append(text)
        if len(_events_pending) > 1000:          # CSV failing for a long time
            del _events_pending[:-1000]
    try:
        print(f"[{stamp}] {text}")
    except Exception:
        # Console that can't show a character (e.g. cp1252) must not break
        # the calling thread — which may be the heater/device thread.
        try:
            print(f"[{stamp}] {ascii_text(text)}")
        except Exception:
            pass


_ASCII_MAP = str.maketrans({'→': '->', '·': ';', '—': '-', '–': '-',
                            '°': 'deg', '…': '...', 'Ω': 'ohm', '±': '+/-',
                            '≈': '~', 'µ': 'u', '×': 'x'})


def ascii_text(text: str) -> str:
    """Plain-ASCII version of an event line, for the CSV and odd consoles."""
    return text.translate(_ASCII_MAP).encode('ascii', 'replace').decode('ascii')


# ─── tests ──────────────────────────────────────────────────────────────────

def pending_edges():
    """Copy of the edges not yet logged, without taking them."""
    with _lock:
        return list(_edges_pending)


def reset():
    """Back to the start-up state."""
    global _readings, _charts
    with _lock:
        _readings = _fresh_readings()
        _charts = _fresh_charts()
        _events.clear()
        _events_pending.clear()
        _edges_pending.clear()
        _health.update(keller=False, labjack=False, tc=False, csv=None)
    stop.clear()
