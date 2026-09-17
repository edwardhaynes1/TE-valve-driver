"""State shared between the device, logger and GUI threads, and the
event log. Locks: `lock` guards readings/events/charts, `heater_lock`
guards `heater`. Never hold both at once.
"""


import threading
from collections import deque
from datetime import datetime

from . import controller
from .config import CHART_SECONDS, KELLER_POLL_HZ, LABJACK_SAMPLE_HZ


# ─── Shared state ─────────────────────────────────────────────────────────────
lock        = threading.Lock()        # protects readings / events / chart buffers
stop        = threading.Event()
readings     = dict(
    keller_pressure_samples     = [],    # accumulated between log rows, then averaged
    keller_temperature_samples  = [],    # accumulated between log rows, then averaged
    vacuum_chamber_mbar         = None,  # latest single reading
    vacuum_status               = None,  # None = valid, else VAC_* reason string
    vacuum_gauge_V              = None,  # gauge-side signal voltage, V
    te_temperature_degC         = None,  # latest MAX31856 reading
    tc_fault                    = None,  # latest fault register (0 = OK, None = no read)
    keller_pressure_bar         = None,  # latest single upstream reading (absolute)
    keller_pressure_t           = None,  # time of that reading
)

# ─── Heater command / status (protected by heater_lock) ──────────────────────
heater_lock = threading.Lock()
heater = controller.new_state()
events      = deque(maxlen=200)                            # (timestamp, text)
up_chart    = deque(maxlen=CHART_SECONDS * KELLER_POLL_HZ)     # recent upstream pressure (bar abs, raw)
vac_chart   = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ)  # recent vacuum readings
te_chart    = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ)  # recent valve temperatures
heat_chart  = deque(maxlen=CHART_SECONDS * LABJACK_SAMPLE_HZ)  # recent heater power (period mean, W)


events_pending = []    # events not yet written to the CSV (protected by lock)
pwm_edges      = []    # gate edges not yet written to the _pwm CSV (protected by lock)


def log_event(text: str):
    """Record a timestamped event for the GUI log, the CSV 'events' column,
    and the console."""
    stamp = datetime.now().strftime('%H:%M:%S')
    with lock:
        events.append((stamp, text))
        events_pending.append(text)
        if len(events_pending) > 1000:          # CSV failing for a long time
            del events_pending[:-1000]
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


# ─── Device / logger health flags (assigned by the device and logger threads,
#     read by the GUI; single assignments, so no lock) ─────────────────────────
keller_ok   = False
labjack_ok  = False   # vacuum gauge channel healthy
tc_ok       = False   # thermocouple channel healthy
csv_ok      = None    # CSV logger: None = starting, True = writing, False = failing
