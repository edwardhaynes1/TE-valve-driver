"""Thread-safe wrapper around the controller for the device thread and GUI.

    heater_command(**kw)     operator commands (arm, mode, duty, setpoints)
    compute_duty(temp, tc_healthy, dt, vac, vac_status, vac_healthy)
                             one control step on the live state; returns duty
    heater_trip(reason)      latch the heater off
    record_gate_edge(state, duty, note)
                             ON-time accounting and the PWM switching log

Each call takes shared.heater_lock for the whole controller call, so a GUI
command can never land halfway through a step. Messages go to the event log
after the lock is released. The logic itself is in controller.py.
"""

import time

from . import controller, shared
from .controller import AUTO_P, AUTO_T, MANUAL, MODES
from .shared import log_event

__all__ = ['MANUAL', 'AUTO_T', 'AUTO_P', 'MODES', 'clock', 'heater_command',
           'compute_duty', 'heater_trip', 'record_gate_edge']

# Time source. Tests replace it with a fake clock.
clock = time.time


def heater_command(**kwargs):
    now = clock()
    with shared.heater_lock:
        controller.command(shared.heater, now, **kwargs)


def compute_duty(temp, tc_healthy, dt, vac=None, vac_status=None, vac_healthy=True):
    now = clock()
    with shared.lock:                   # never held together with heater_lock
        p_up = shared.readings['keller_pressure_bar']
        p_up_t = shared.readings['keller_pressure_t']
    with shared.heater_lock:
        duty, msgs = controller.step(
            shared.heater, now, dt, temp, tc_healthy, vac=vac,
            vac_status=vac_status, vac_healthy=vac_healthy,
            p_up=p_up, p_up_t=p_up_t)
    for m in msgs:
        log_event(m)
    return duty


def heater_trip(reason: str):
    msgs = []
    with shared.heater_lock:
        controller.trip(shared.heater, reason, msgs)
    for m in msgs:
        log_event(m)


def record_gate_edge(state, duty, note=""):
    """Call right after the FIO0 write succeeds. File I/O stays on the
    logger thread."""
    now = clock()
    with shared.heater_lock:
        row = controller.record_edge(shared.heater, state, duty, now, note)
    with shared.lock:
        shared.pwm_edges.append(row)
