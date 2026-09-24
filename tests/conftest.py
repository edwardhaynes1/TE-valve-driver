"""Shared test set-up: make the repo importable and give each test a fresh
heater state and a fake clock."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from driver import batchrun, config, control, controller, openings, shared  # noqa: E402


def reset_shared():
    shared.reset()
    control.reset()
    batchrun.reset()
    openings.reset()


class PackageAdapter:
    """Scenario adapter (see scenarios.py) for the driver package."""
    VAC_OVER = config.VAC_OVER
    VAC_SATURATED = config.VAC_SATURATED
    VAC_ERROR = config.VAC_ERROR

    @property
    def heater(self):
        return control.snapshot()

    def command(self, **kw):
        control.heater_command(**kw)

    def compute(self, *a):
        return control.compute_duty(*a)

    def set_upstream(self, bar, t):
        if bar is None:
            shared.clear_keller()
        else:
            shared.store_keller(bar, None, t)

    def set_clock(self, fn):
        control.clock = fn

    def reset(self):
        reset_shared()
        self._log = []

    def events(self):
        self._log.extend(shared.take_events())
        return list(self._log)


class ControllerAdapter:
    """Scenario adapter that drives controller.py directly: no shared state,
    no locks, no event log. Proves the core alone reproduces the record."""
    VAC_OVER = config.VAC_OVER
    VAC_SATURATED = config.VAC_SATURATED
    VAC_ERROR = config.VAC_ERROR

    def reset(self):
        self.heater = controller.new_state()
        self._msgs, self._up, self._clock = [], (None, None), None

    def set_clock(self, fn):
        self._clock = fn

    def set_upstream(self, bar, t):
        self._up = (bar, t)

    def command(self, **kw):
        controller.command(self.heater, self._clock(), **kw)

    def compute(self, temp, healthy, dt, vac, vac_status, vac_ok):
        duty, msgs = controller.step(
            self.heater, self._clock(), dt, temp, healthy, vac=vac,
            vac_status=vac_status, vac_healthy=vac_ok,
            p_up=self._up[0], p_up_t=self._up[1])
        self._msgs.extend(msgs)
        return duty

    def events(self):
        return list(self._msgs)


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch, capsys, tmp_path):
    # never read or write the real logs/opening-points.json
    monkeypatch.setattr(config, "OPENINGS_FILE", str(tmp_path / "opening-points.json"))
    reset_shared()
    clock = FakeClock()
    monkeypatch.setattr(control, "clock", clock)
    yield clock
    reset_shared()


@pytest.fixture
def clock(fresh_state):
    return fresh_state


@pytest.fixture(params=["threaded", "core"])
def adapter(request):
    """Every scenario runs twice: through control.py (as the device thread
    calls it) and through controller.py alone."""
    return PackageAdapter() if request.param == "threaded" else ControllerAdapter()
