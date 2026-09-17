"""CSV logs: column layout, PWM switching log, ON-time accounting."""
import csv
import json
import threading
import time
from pathlib import Path

from driver import control, logfile, schema, shared

GOLDEN_HEADERS = json.loads(
    (Path(__file__).parent / "golden" / "csv_headers.json").read_text())


def test_schema_keeps_every_original_column_in_order():
    # Columns may be appended, never renamed, removed or reordered.
    assert schema.MAIN[:len(GOLDEN_HEADERS["main"])] == tuple(GOLDEN_HEADERS["main"])
    assert schema.PWM[:len(GOLDEN_HEADERS["pwm"])] == tuple(GOLDEN_HEADERS["pwm"])


def test_column_names_are_unique():
    assert len(set(schema.MAIN)) == len(schema.MAIN)
    assert len(set(schema.PWM)) == len(schema.PWM)


def test_on_time_accounting(clock):
    control.record_gate_edge(True, 0.25)
    clock.advance(0.5)
    control.record_gate_edge(False, 0.25)
    clock.advance(1.5)
    control.record_gate_edge(True, 0.25)
    clock.advance(0.2)
    control.record_gate_edge(False, 0.25, "shutdown: forced low")
    assert abs(shared.heater['on_time_acc'] - 0.7) < 1e-9
    edges = list(shared.pwm_edges)
    assert [e['gate'] for e in edges] == [1, 0, 1, 0]
    assert [e['on_s'] for e in edges] == ['', 0.5, '', 0.2]
    assert edges[-1]['note'] == "shutdown: forced low"
    assert all(set(e) == set(schema.PWM) for e in edges)


def test_repeated_on_edge_does_not_restart_the_on_period(clock):
    control.record_gate_edge(True, 1.0)
    clock.advance(0.3)
    control.record_gate_edge(True, 1.0)        # e.g. a reconnect re-asserting ON
    clock.advance(0.3)
    control.record_gate_edge(False, 1.0)
    assert shared.pwm_edges[-1]['on_s'] == 0.6


def test_logger_writes_both_files(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "clock", time.time)   # logger runs in real time
    monkeypatch.setattr(logfile, "LOG_FILE", str(tmp_path / "t.csv"))
    monkeypatch.setattr(logfile, "PWM_LOG_FILE", str(tmp_path / "t_pwm.csv"))
    shared.log_event("héllo → wörld 40 °C")
    th = threading.Thread(target=logfile.logger_thread, daemon=True)
    th.start()
    t0 = time.time()
    while time.time() - t0 < 1.2:                      # 30 % duty, 0.4 s period
        control.record_gate_edge(True, 0.3)
        time.sleep(0.12)
        control.record_gate_edge(False, 0.3)
        time.sleep(0.28)
    shared.stop.set()
    th.join(timeout=3)
    assert not th.is_alive()

    rows = list(csv.DictReader(open(tmp_path / "t.csv", encoding="utf-8")))
    edges = list(csv.DictReader(open(tmp_path / "t_pwm.csv", encoding="utf-8")))
    assert tuple(rows[0].keys()) == schema.MAIN
    assert tuple(edges[0].keys()) == schema.PWM
    assert len(rows) >= 3
    assert "h?llo -> w?rld 40 degC" in rows[0]["events"]   # ASCII only in the CSV
    on_rows = sum(float(r["heater_on_s"]) for r in rows)
    on_edges = sum(float(e["on_s"]) for e in edges if e["on_s"])
    # each value is rounded to 1 ms, so allow 0.5 ms per rounded value
    assert abs(on_rows - on_edges) <= 0.0005 * (len(rows) + len(edges)) + 1e-9
    assert shared.csv_ok is True


def test_importing_the_package_creates_no_files(tmp_path):
    import subprocess
    import sys
    repo = Path(__file__).resolve().parent.parent
    logs = repo / "logs"
    before = sorted(logs.iterdir()) if logs.exists() else None
    code = ("import driver.config, driver.shared, driver.control, "
            "driver.schema, driver.logfile; "
            "assert driver.logfile.LOG_FILE is None")
    subprocess.run([sys.executable, "-c", code], cwd=repo, check=True)
    after = sorted(logs.iterdir()) if logs.exists() else None
    assert before == after
