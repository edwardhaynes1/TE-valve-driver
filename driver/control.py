"""The heater's owner. The device thread, logger and GUI use the heater only
through these functions; the state and its lock are private.

    heater_command(**kw)     operator commands (arm, mode, duty, setpoints)
    compute_duty(temp, tc_healthy, dt, vac, vac_status, vac_healthy)
                             one control step on the live state; returns duty
    heater_trip(reason)      latch the heater off
    record_gate_edge(state, duty, note)
                             ON-time accounting; queues the PWM log row
    snapshot()               copy of the heater state, for display and logging
    take_on_time()           gate ON seconds since the previous call (logger)
    apply_duty(duty)         device thread: the duty now applied
    gate_on_recorded()       is an ON edge recorded without its OFF?
    force_off(device_lost)   device thread: disarm on connect / on loss
    MANUAL, AUTO_T, AUTO_P, MODES   the mode names

Each call holds the heater lock for its whole duration, so a GUI command can
never land halfway through a control step. Event messages are logged after
the lock is released. The logic itself is in controller.py.
"""

import threading
import time

from . import controller, shared
from .controller import AUTO_P, AUTO_T, MANUAL, MODES
from .shared import log_event

__all__ = ['MANUAL', 'AUTO_T', 'AUTO_P', 'MODES', 'clock', 'heater_command',
           'compute_duty', 'heater_trip', 'record_gate_edge', 'snapshot',
           'take_on_time', 'apply_duty', 'gate_on_recorded', 'force_off',
           'reset']

# Time source. Tests replace it with a fake clock.
clock = time.time

_lock = threading.Lock()
_heater = controller.new_state()


def heater_command(**kwargs):
    now = clock()
    with _lock:
        controller.command(_heater, now, **kwargs)


def compute_duty(temp, tc_healthy, dt, vac=None, vac_status=None, vac_healthy=True):
    now = clock()
    p_up, p_up_t = shared.upstream()          # never while holding _lock
    with _lock:
        duty, msgs = controller.step(
            _heater, now, dt, temp, tc_healthy, vac=vac,
            vac_status=vac_status, vac_healthy=vac_healthy,
            p_up=p_up, p_up_t=p_up_t)
    for m in msgs:
        log_event(m)
    return duty


def heater_trip(reason: str):
    msgs = []
    with _lock:
        controller.trip(_heater, reason, msgs)
    for m in msgs:
        log_event(m)


def record_gate_edge(state, duty, note=""):
    """Call right after the FIO0 write succeeds. File I/O stays on the
    logger thread."""
    now = clock()
    with _lock:
        row = controller.record_edge(_heater, state, duty, now, note)
    shared.queue_edge(row)


def snapshot():
    """A copy of the heater state (without the pressure history)."""
    with _lock:
        return {k: v for k, v in _heater.items() if k != 'p_hist'}


def take_on_time():
    now = clock()
    with _lock:
        return controller.take_on_time(_heater, now)


def apply_duty(duty):
    """The device thread: this duty is now being applied."""
    with _lock:
        _heater['duty_actual'] = duty


def gate_on_recorded():
    with _lock:
        return _heater['on_since'] is not None


def force_off(device_lost=False):
    with _lock:
        controller.force_off(_heater, device_lost)


def reset():
    """Back to the start-up state (tests)."""
    global _heater
    with _lock:
        _heater = controller.new_state()
