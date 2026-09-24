"""The batch controls in the window: start needs a torque and a disarmed
heater and asks for confirmation; while a batch runs it owns the heater
(the heater controls and the torque are locked); abort and DISARM end it."""
import time

import pytest

from driver import batchrun, config, control, shared


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
def gui(tk_root, tmp_path, monkeypatch):
    import tkinter as tk
    from driver.gui import TEGui
    monkeypatch.setattr(config, "BATCH_DIR", str(tmp_path / "batches"))
    monkeypatch.setattr(config, "BATCH_WORKBOOK", str(tmp_path / "map.xlsx"))
    monkeypatch.setattr(batchrun, "PLOT", False)
    real_start = batchrun.start
    monkeypatch.setattr(batchrun, "start",
                        lambda n, main_log="", topup=None, start_thread=True:
                        real_start(n, main_log, topup, False))
    g = TEGui(root=tk.Toplevel(tk_root))
    g._confirm = lambda *a: True
    g.root.update()
    yield g
    g.destroy()


def state(w):
    return str(w.cget("state"))


def readings():
    shared.store_valve_temp(25.0, 0)
    shared.store_vacuum(1e-6, None, 1.7)
    shared.store_keller(3.0, None, control.clock())      # top-up is on by default


def enter_torque(g, nm="0.3"):
    readings()
    g.seat_entry.delete(0, "end")
    g.seat_entry.insert(0, nm)
    g._set_seat_screw()
    g._poll()


def test_start_needs_the_torque(gui):
    assert state(gui.batch_btn) == "disabled" and state(gui.abort_btn) == "disabled"
    enter_torque(gui)
    assert state(gui.batch_btn) == "normal"


def test_start_needs_a_disarmed_heater(gui):
    enter_torque(gui)
    control.heater_command(armed=True)
    gui._poll()
    assert state(gui.batch_btn) == "disabled"


def test_cancel_does_not_start(gui):
    enter_torque(gui)
    gui._confirm = lambda *a: False
    gui._start_batch()
    assert not batchrun.running()


def test_the_confirmation_shows_torque_and_measured_upstream(gui):
    enter_torque(gui, "0.45")
    shared.store_keller(3.21, None, control.clock())
    seen = []
    gui._confirm = lambda title, text: seen.append(text) or False
    gui._start_batch()
    assert "0.45 N·m" in seen[0] and "3.210 bar (measured)" in seen[0]


def test_a_running_batch_locks_the_heater_and_torque(gui):
    enter_torque(gui)
    gui._start_batch()
    gui._poll()
    assert batchrun.running()
    for w in (*gui.mode_buttons, gui.update_btn, gui.sp_entry, gui.seat_entry,
              gui.seat_btn, gui.runs_entry, gui.batch_btn):
        assert state(w) == "disabled"
    assert state(gui.abort_btn) == "normal" and state(gui.arm_btn) == "normal"
    assert "scout1" in gui.batch_status.cget("text")
    gui._abort_batch()
    gui._poll()
    assert not batchrun.running() and state(gui.seat_entry) == "normal"
    assert state(gui.batch_btn) == "normal"
    assert "aborted" in gui.batch_status.cget("text")


def test_disarm_aborts_and_arm_is_refused_during_a_batch(gui):
    enter_torque(gui)
    gui._start_batch()
    gui._toggle_arm()                              # heater disarmed: ARM refused
    assert not control.snapshot()['armed'] and batchrun.running()
    control.heater_command(armed=True)             # as the batch does
    gui._toggle_arm()                              # DISARM
    assert not control.snapshot()['armed'] and not batchrun.running()
