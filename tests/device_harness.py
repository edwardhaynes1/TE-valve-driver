"""Helpers for running the real LabJack thread against FakeU3."""
import threading
import time
import types

from driver import config, control, shared
from driver import devices as DEVICE   # the module that holds labjack_thread

TICK = 1.0 / config.HEATER_TICK_HZ      # 50 ms: the gate can only switch on a tick
STEP = 1.0 / config.LABJACK_SAMPLE_HZ   # the controller runs this often (0.25 s)


def start(monkeypatch, fake, wait_for_tc=True, u3_factory=None, **settings):
    """Run DEVICE.labjack_thread on `fake`, with config overrides such as
    HEATER_PWM_PERIOD_S=1.0 applied to the device module."""
    monkeypatch.setattr(control, "clock", time.time)          # real time here
    monkeypatch.setattr(DEVICE, "LABJACK_AVAILABLE", True)
    monkeypatch.setattr(DEVICE, "u3", types.SimpleNamespace(
        U3=u3_factory or (lambda: fake)), raising=False)
    for name, value in settings.items():
        monkeypatch.setattr(DEVICE, name, value)
    th = threading.Thread(target=DEVICE.labjack_thread, daemon=True)
    th.start()
    if wait_for_tc:
        wait_for(lambda: shared.health()['tc']
                 and shared.latest()['te_temperature_degC'] is not None)
    return th


def stop(th):
    shared.stop.set()
    th.join(timeout=3)
    assert not th.is_alive(), "LabJack thread did not stop"


def wait_for(cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def events_with(text):
    return [m for _, m in shared.recent_events() if text in m]


def edges_with(note):
    return [e for e in shared.pending_edges() if note in e['note']]
