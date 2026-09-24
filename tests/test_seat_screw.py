"""Seat screw torque: the torque on the TE-Valve's seat screw, in N·m, as the
operator enters it (see prog-documentation/context.md).

Agreed behaviour:
  * blank at the start of every session — blank means "not recorded", never 0
  * entered in the driver; numbers from 0 to SEAT_SCREW_TORQUE_MAX_NM only,
    with a decimal point or a comma
  * every CSV row carries the current value (seat_screw_torque_Nm)
  * each entry goes in the event log, saying what changed
  * the plotter reports it, and marks the moment it changed mid-run
"""
import csv
import threading
import time

import pytest

from driver import config, control, logfile, readout, schema, shared


def events():
    return [text for _, text in shared.recent_events()]


# ── the value ───────────────────────────────────────────────────────────────

def test_blank_at_the_start():
    assert shared.seat_screw_torque() is None


def test_first_entry_is_logged():
    shared.set_seat_screw_torque(0.4)
    assert shared.seat_screw_torque() == 0.4
    assert events()[-1] == "Seat screw torque set to 0.40 N·m"


def test_a_change_is_logged_with_old_and_new_values():
    shared.set_seat_screw_torque(0.4)
    shared.set_seat_screw_torque(0.45)
    assert shared.seat_screw_torque() == 0.45
    assert events()[-1] == "Seat screw torque 0.40 → 0.45 N·m"


def test_re_entering_the_same_value_says_so():
    shared.set_seat_screw_torque(0.4)
    shared.set_seat_screw_torque(0.4)
    assert events()[-1] == "Seat screw torque unchanged (0.40 N·m)"


@pytest.mark.parametrize("bad", [-0.1, config.SEAT_SCREW_TORQUE_MAX_NM + 0.01,
                                 float("nan"), float("inf")])
def test_values_outside_the_range_are_refused(bad):
    shared.set_seat_screw_torque(0.4)
    with pytest.raises(ValueError):
        shared.set_seat_screw_torque(bad)
    assert shared.seat_screw_torque() == 0.4          # unchanged


def test_zero_and_the_maximum_are_allowed():
    shared.set_seat_screw_torque(0.0)
    assert shared.seat_screw_torque() == 0.0          # 0 is a value; blank is None
    shared.set_seat_screw_torque(config.SEAT_SCREW_TORQUE_MAX_NM)


def test_a_new_session_starts_blank_again():
    shared.set_seat_screw_torque(0.4)
    shared.reset()
    assert shared.seat_screw_torque() is None


# ── what the operator types ────────────────────────────────────────────────

@pytest.mark.parametrize("text, value", [("0.4", 0.4), ("0,45", 0.45), (" 1 ", 1.0),
                                         ("2.25", 2.25), ("0", 0.0)])
def test_typed_numbers_are_understood(text, value):
    assert readout.parse_seat_screw_torque(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ["", "   ", "abc", "0.4 Nm", "1e", "-0.2",
                                  f"{config.SEAT_SCREW_TORQUE_MAX_NM + 1:g}"])
def test_anything_else_is_refused_with_a_reason(text):
    with pytest.raises(ValueError) as err:
        readout.parse_seat_screw_torque(text)
    assert "seat screw torque" in str(err.value).lower()


# ── on screen ───────────────────────────────────────────────────────────────

def screen_text(torque):
    r = shared.latest()
    segs = readout.status_segments(r, shared.health(), control.snapshot(),
                                   shared.heater_output(), True, torque)
    return "".join(t for t, _ in segs)


def test_the_screen_says_when_it_has_not_been_entered():
    assert "SEAT SCREW   ---  (not entered)" in screen_text(None)


def test_the_screen_shows_the_entered_value():
    assert "SEAT SCREW   0.40 N·m" in screen_text(0.4)


# ── in the log ──────────────────────────────────────────────────────────────

def test_the_column_is_appended_at_the_end():
    # Appended after the 22 older columns; later columns (the batch labels)
    # come after it, never before (schema.py: append-only).
    assert schema.MAIN.index("seat_screw_torque_Nm") == 22
    assert schema.MAIN[23:] == ("batch_run", "batch_phase")


def test_every_row_carries_the_value_blank_until_entered(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "clock", time.time)
    monkeypatch.setattr(logfile, "LOG_FILE", str(tmp_path / "t.csv"))
    monkeypatch.setattr(logfile, "PWM_LOG_FILE", str(tmp_path / "t_pwm.csv"))
    monkeypatch.setattr(logfile, "LOG_INTERVAL_S", 0.1)
    th = threading.Thread(target=logfile.logger_thread, daemon=True)
    th.start()
    time.sleep(0.35)
    shared.set_seat_screw_torque(0.4)
    time.sleep(0.35)
    shared.stop.set()
    th.join(timeout=3)
    rows = list(csv.DictReader(open(tmp_path / "t.csv", encoding="utf-8")))
    values = [r["seat_screw_torque_Nm"] for r in rows]
    assert values[0] == ""                            # not entered yet: blank, not 0
    assert values[-1] == "0.4"
    changed = [r for r in rows if "Seat screw torque set to 0.40" in r["events"]]
    assert len(changed) == 1
