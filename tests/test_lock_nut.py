"""Lock nut torque, and the "update" buttons on both torque lines (history
43); it pools results and a different one is a new seating (history 44).

Agreed behaviour (1 Oct 2026):
  * the lock nut torque is entered like the seat screw torque (N·m, point or
    comma, 0 … LOCK_NUT_TORQUE_MAX_NM) on a LOCK NUT line under SEAT SCREW;
    optional: blank means not recorded, and it locks nothing
  * once a torque is entered its line shows the value and a small "update"
    button; "update" puts the box (holding the value) where the value was;
    Return or "set" saves, Escape goes back unchanged
  * both lines are locked while t-min-tune runs
  * every main-log row carries it (lock_nut_torque_Nm, the last column);
    every t-min.csv row and session.json record it; the start dialog shows
    it, and the latest seating's when that was recorded
  * (history 44) the estimate from other seatings, and a scout's memory of
    openings at the start, count only seatings at the same seat screw and
    lock nut torques (blank = not recorded counts as a value); a lock nut
    torque different from the latest seating's at this seat screw torque
    starts a new seating without the re-torque question; the seating name
    ends _nut<torque>Nm when it is entered
"""
import csv
import json
import threading
import time
from pathlib import Path

import pytest

from driver import config, control, logfile, readout, schema, shared, tmin, tminlog, tminrun


def events():
    return [text for _, text in shared.recent_events()]


# ── the value ───────────────────────────────────────────────────────────────

def test_blank_at_the_start_and_entries_are_logged():
    assert shared.lock_nut_torque() is None
    shared.set_lock_nut_torque(0.1)
    assert shared.lock_nut_torque() == 0.1 and events()[-1] == "Lock nut torque set to 0.10 N·m"
    shared.set_lock_nut_torque(0.15)
    assert events()[-1] == "Lock nut torque 0.10 → 0.15 N·m"


@pytest.mark.parametrize("bad", [-0.1, config.LOCK_NUT_TORQUE_MAX_NM + 0.1, float("nan")])
def test_values_outside_the_range_are_refused(bad):
    with pytest.raises(ValueError):
        shared.set_lock_nut_torque(bad)
    assert shared.lock_nut_torque() is None


def test_typed_numbers_are_understood_and_others_refused():
    assert readout.parse_lock_nut_torque(" 0,1 ") == 0.1
    with pytest.raises(ValueError, match="Lock nut torque: 'abc' is not a number"):
        readout.parse_lock_nut_torque("abc")
    with pytest.raises(ValueError, match="outside"):
        readout.parse_lock_nut_torque("9")


def test_the_panel_text_has_a_lock_nut_line():
    text = "".join(t for t, _ in readout.lock_nut_segments(None))
    assert text.startswith("LOCK NUT     ---")
    assert "LOCK NUT     0.10 N·m" in "".join(t for t, _ in readout.lock_nut_segments(0.1))


# ── in the logs ─────────────────────────────────────────────────────────────

def test_the_main_log_column_is_appended_at_the_end():
    assert schema.MAIN[-1] == "lock_nut_torque_Nm"
    assert schema.MAIN.index("batch_phase") == 24          # nothing before it moved


def test_every_row_carries_it_blank_until_entered(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "clock", time.time)
    monkeypatch.setattr(logfile, "LOG_FILE", str(tmp_path / "t.csv"))
    monkeypatch.setattr(logfile, "PWM_LOG_FILE", str(tmp_path / "t_pwm.csv"))
    monkeypatch.setattr(logfile, "LOG_INTERVAL_S", 0.1)
    th = threading.Thread(target=logfile.logger_thread, daemon=True)
    th.start()
    time.sleep(0.35)
    shared.set_lock_nut_torque(0.1)
    time.sleep(0.35)
    shared.stop.set()
    th.join(timeout=3)
    rows = list(csv.DictReader(open(tmp_path / "t.csv", encoding="utf-8")))
    values = [r["lock_nut_torque_Nm"] for r in rows]
    assert values[0] == "" and values[-1] == "0.1"


def ready(clock, nut=None):
    shared.set_seat_screw_torque(0.4)
    if nut is not None:
        shared.set_lock_nut_torque(nut)
    shared.store_valve_temp(25.0, 0)
    shared.store_vacuum(4e-7, None, 1.7)
    shared.store_keller(0.955, None, clock.t)


def test_t_min_tune_records_it(clock):
    ready(clock, nut=0.1)
    ok, name = tminrun.start(target=0.955, band=0.05, start_thread=False)
    assert ok
    info = json.load(open(Path(tminrun.folder()) / "session.json", encoding="utf-8"))
    assert info["lock_nut_torque_Nm"] == 0.1
    assert "(lock nut 0.10 N·m)" in " | ".join(events())
    test = tmin._new_test(tminrun._s, clock.t)        # each test's row carries it
    test['outcome'] = tmin.STOPPED_TEST
    assert tmin.result_row(tminrun._s, test, -12.0, "t")["lock_nut_torque_Nm"] == 0.1
    tminrun.stop("test")
    tminlog.append(dict(time="t", seating=name, torque_Nm=0.4, outcome="stopped",
                        lock_nut_torque_Nm=0.1), sheet=False)
    assert tminrun.seating_lock_nut(name) == 0.1
    assert tminrun.seating_lock_nut("no such seating") is None


def test_an_older_t_min_file_gains_the_column(tmp_path):
    p = tmp_path / "t-min.csv"
    cols = list(schema.TMIN)[:-1]
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(cols)
        csv.writer(f).writerow(["2026-10-01T10:00:00", "a", "0.4"] + [""] * (len(cols) - 3))
    tminlog.append(dict(time="2026-10-01T11:00:00", seating="a", torque_Nm=0.4,
                        outcome="stopped", lock_nut_torque_Nm=0.2), path=str(p), sheet=False)
    rows = tminlog.load(str(p))
    assert [r["lock_nut_torque_Nm"] for r in rows] == ["", "0.2"]


# ── the window ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tk_root():
    tk = pytest.importorskip("tkinter")
    err = None
    for _ in range(3):
        try:
            root = tk.Tk()
            break
        except tk.TclError as e:
            err = e
            time.sleep(0.5)
    else:
        pytest.skip(f"Tk could not start: {str(err).splitlines()[0]}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def gui(tk_root, monkeypatch):
    import tkinter as tk
    from driver.gui import TEGui
    real_start = tminrun.start
    monkeypatch.setattr(tminrun, "start",
                        lambda *a, **k: real_start(*a, **{**k, 'start_thread': False}))
    g = TEGui(root=tk.Toplevel(tk_root))
    g._confirm = lambda *a: True
    g.root.update()
    yield g
    g.destroy()


def shown(w):
    return w.winfo_manager() == "pack"


def state(w):
    return str(w.cget("state"))


def set_seat(g, text="0.4"):
    g._set_entry(g.seat_entry, text)
    g._set_seat_screw()


def set_nut(g, text):
    g._set_entry(g.nut_entry, text)
    g._set_lock_nut()


def test_at_start_the_lock_nut_line_holds_the_box_and_says_optional(gui):
    assert gui.nut_entry.master is gui.nut_row
    assert shown(gui.nut_entry) and shown(gui.nut_btn) and shown(gui.nut_hint)
    assert not shown(gui.nut_value) and not shown(gui.nut_upd_btn)
    assert state(gui.nut_entry) == "normal"          # usable before the seat screw


def test_it_locks_nothing(gui):
    set_seat(gui)
    assert str(gui.arm_btn.cget("state")) == "normal"   # no lock nut entered: still unlocked


def test_once_entered_each_line_shows_the_value_and_update(gui):
    set_seat(gui, "0.4")
    set_nut(gui, "0,1")
    assert shared.lock_nut_torque() == 0.1
    for value, upd, entry, btn, text in (
            (gui.seat_value, gui.seat_upd_btn, gui.seat_entry, gui.seat_btn, "0.40 N·m"),
            (gui.nut_value, gui.nut_upd_btn, gui.nut_entry, gui.nut_btn, "0.10 N·m")):
        assert shown(value) and shown(upd) and value.cget("text") == text
        assert not shown(entry) and not shown(btn)
        assert upd.cget("text") == "update"
    assert not shown(gui.nut_hint)


def test_update_puts_the_box_where_the_value_was(gui):
    set_seat(gui, "0.4")
    set_nut(gui, "0.1")
    gui.seat_upd_btn.invoke()
    assert shown(gui.seat_entry) and not shown(gui.seat_value) and not shown(gui.seat_upd_btn)
    assert gui.seat_entry.get() == "0.4"
    set_seat(gui, "0.45")
    assert shared.seat_screw_torque() == 0.45 and shown(gui.seat_value)
    gui.nut_upd_btn.invoke()
    assert shown(gui.nut_entry) and not shown(gui.nut_value) and gui.nut_entry.get() == "0.1"
    set_nut(gui, "0.2")
    assert shared.lock_nut_torque() == 0.2 and gui.nut_value.cget("text") == "0.20 N·m"
    assert "Lock nut torque 0.10 → 0.20 N·m" in events()


def test_escape_goes_back_unchanged(gui):
    set_seat(gui)
    set_nut(gui, "0.1")
    gui.nut_upd_btn.invoke()
    gui._set_entry(gui.nut_entry, "0.3")
    gui._cancel_nut_edit()
    assert shared.lock_nut_torque() == 0.1 and shown(gui.nut_value)


def test_a_bad_entry_changes_nothing(gui):
    set_seat(gui)
    set_nut(gui, "0.1")
    gui.nut_upd_btn.invoke()
    set_nut(gui, "abc")
    assert shared.lock_nut_torque() == 0.1 and "not a number" in events()[-1]


def start_tmin(g):
    shared.store_valve_temp(25.0, 0)
    shared.store_vacuum(1e-6, None, 1.7)
    shared.store_keller(0.955, None, control.clock())
    g.mode_var.set("t-min-tune")
    g._on_mode()
    g._set_entry(g.up_entry, "0.955")
    g._poll()
    seen = []
    g._confirm = lambda title, text: seen.append(text) or True
    g._ask = lambda title, text: seen.append(text) or True
    g._start_batch()
    return seen


def test_both_lines_are_locked_while_t_min_tune_runs(gui):
    set_seat(gui)
    set_nut(gui, "0.1")
    seen = start_tmin(gui)
    assert tminrun.running()
    assert "Lock nut torque: 0.10 N·m" in seen[0]
    for w in (gui.seat_upd_btn, gui.nut_upd_btn, gui.nut_btn, gui.nut_entry):
        assert state(w) == "disabled", w
    gui._edit_lock_nut()                              # even if called directly
    assert shown(gui.nut_value) and "lock nut torque is locked" in events()[-1]
    tminrun.stop("test")
    gui._poll()
    assert state(gui.seat_upd_btn) == "normal" and state(gui.nut_upd_btn) == "normal"


def test_the_dialog_says_when_it_is_not_entered_and_the_latest_seatings(gui):
    tminlog.append(dict(time="2026-10-01T11:00:00", seating="20261001_110000_0.40Nm",
                        torque_Nm=0.4, outcome="stopped", lock_nut_torque_Nm=0.2), sheet=False)
    set_seat(gui)
    seen = start_tmin(gui)
    assert "Lock nut torque: not entered" in seen[0]
    assert "20261001_110000_0.40Nm (lock nut 0.20 N·m)" in seen[0]


# ── history 44: the lock nut torque pools results and makes a new seating ──

def row(seating, T, nut, outcome=tminlog.T_MIN, torque="0.4"):
    return dict(time="2026-10-01T11:00:00", seating=seating, torque_Nm=torque, outcome=outcome, counted="1",
                t_min_degC=str(T), upstream_at_open_bar="0.955",
                lock_nut_torque_Nm="" if nut is None else str(nut))


def test_other_seatings_count_only_at_the_same_lock_nut_torque():
    rows = [row("a", 100.0, 0.1), row("b", 88.0, 0.2), row("c", 120.0, None)]
    est = tminlog.estimate(rows, "new", 0.4, 0.955, lock_nut=0.1)
    assert est['T'] == 100.0 and "lock nut 0.10 N·m" in est['how']
    assert tminlog.estimate(rows, "new", 0.4, 0.955, lock_nut=0.21)['T'] == 88.0  # within tol
    assert tminlog.estimate(rows, "new", 0.4, 0.955)['T'] == 120.0       # blank: blank only
    assert tminlog.estimate(rows, "new", 0.4, 0.955, lock_nut=0.3) is None


def test_openings_at_the_start_count_only_at_the_same_lock_nut_torque():
    rows = [row("a", 30.0, 0.1, tminlog.OPENED_AT_START), row("b", 25.0, None,
                                                               tminlog.OPENED_AT_START)]
    assert tminlog.opened_low(rows, 0.4, 0.1) == 30.0
    assert tminlog.opened_low(rows, 0.4) == 25.0
    assert tminlog.opened_low(rows, 0.4, 0.2) is None


def test_the_seating_name_carries_the_lock_nut_torque(clock):
    ready(clock, nut=0.1)
    ok, name = tminrun.start(target=0.955, band=0.05, start_thread=False)
    assert ok and name.endswith("_0.40Nm_nut0.10Nm")


@pytest.mark.parametrize("old_nut, now_nut, same", [
    (0.1, 0.1, True), (0.1, 0.2, False), (None, 0.1, False), (0.1, None, False),
    (None, None, True)])
def test_a_different_lock_nut_torque_is_a_new_seating(clock, old_nut, now_nut, same):
    old = "20261001_110000_0.40Nm"
    tminlog.append(row(old, 100.0, old_nut), sheet=False)
    ready(clock, nut=now_nut)
    assert tminrun.lock_nut_changed(old) is not same
    ok, name = tminrun.start(retorqued=False, target=0.955, band=0.05, start_thread=False)
    assert ok and (name == old) is same
    if not same:
        assert "(lock nut torque changed)" in " | ".join(events())


def test_the_window_does_not_ask_when_the_lock_nut_torque_changed(gui):
    tminlog.append(row("20261001_110000_0.40Nm", 100.0, 0.1), sheet=False)
    set_seat(gui)
    set_nut(gui, "0.2")
    gui._ask = lambda *a: pytest.fail("no re-torque question: the lock nut changed")
    seen = start_tmin(gui)
    assert "The lock nut torque has changed: this starts a new seating" in seen[0]
    assert "(lock nut 0.10 N·m)" in seen[0]
    assert tminrun.running() and tminrun.seating().endswith("_nut0.20Nm")


def test_the_window_asks_when_the_lock_nut_torque_is_the_same(gui):
    old = "20261001_110000_0.40Nm"
    tminlog.append(row(old, 100.0, 0.1), sheet=False)
    set_seat(gui)
    set_nut(gui, "0.1")
    asked = []
    gui._ask = lambda title, text: asked.append(text) or False
    gui.mode_var.set("t-min-tune")
    shared.store_valve_temp(25.0, 0)
    shared.store_vacuum(1e-6, None, 1.7)
    shared.store_keller(0.955, None, control.clock())
    gui._on_mode()
    gui._set_entry(gui.up_entry, "0.955")
    gui._poll()
    gui._start_batch()
    assert asked and "Has the seat screw been re-torqued" in asked[0]
    assert tminrun.seating() == old                      # No: continued
