"""The control law must reproduce the recorded behaviour exactly.

tests/golden/control_trace.json.gz was recorded from the single-file driver
(17 Sept 2026) before it was split into the tevalve package. Every duty,
setpoint, phase, trip and event line must match, step by step.

Each scenario runs twice: through control.py (locks, shared state, event
log, as the device thread uses it) and through controller.py on its own.

If you change the control law ON PURPOSE, re-record with
    python tests/record_golden.py
review the differences it prints, and commit the new file with the change.
"""
import json
from pathlib import Path

import pytest

import scenarios

GOLDEN = Path(__file__).parent / "golden" / "control_trace.json.gz"


@pytest.fixture(scope="module")
def golden():
    return {r["name"]: r["trace"] for r in scenarios.load(GOLDEN)}


_cache = {}


def _results(adapter):
    """Run every scenario once per adapter per test session."""
    key = type(adapter).__name__
    if key not in _cache:
        _cache[key] = {r["name"]: r["trace"] for r in scenarios.all_scenarios(adapter)}
    return _cache[key]


def test_same_scenarios(golden, adapter):
    assert list(_results(adapter)) == list(golden)


@pytest.mark.parametrize("name", [
    "manual_duty_and_clamp", "auto_burst_from_cold",
    "auto_small_step_then_big_step", "pressure_from_cold_then_retarget",
    "pressure_warm_start_low_upstream", "pressure_target_below_baseline",
    "mode_switching", "trip_thermocouple", "trip_overtemperature",
    "trip_max_run_time", "trip_overpressure_pressure_mode",
    "overpressure_ignored_in_manual", "trip_gauge_saturated",
    "trip_gauge_dead_and_missed_reads", "pressure_no_reading_before_init",
])
def test_matches_golden(name, golden, adapter):
    ours = _results(adapter)[name]
    ref = golden[name]
    assert len(ours) == len(ref)
    for mine, theirs in zip(ours, ref):
        mine = json.loads(json.dumps(mine))   # same tuple → list conversion as the file
        assert mine == theirs, f"{name}: first difference at t = {mine['k'] * scenarios.DT:.2f} s"
