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
                        lambda *a, **k: real_start(*a, **{**k, 'start_thread': False}))
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
    shared.store_keller(3.0, None, control.clock())


def enter_torque(g, nm="0.3"):
    readings()
    g.seat_entry.delete(0, "end")
    g.seat_entry.insert(0, nm)
    g._set_seat_screw()
    g.mode_var.set("batch")
    g._on_mode()
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
    assert "0.45 N·m" in seen[0] and "3.210 bar (measured;" in seen[0]
    assert "Refill upstream by hand" in seen[0]


def test_the_confirmation_warns_when_the_keller_is_not_read(gui):
    enter_torque(gui, "0.45")
    shared.clear_keller()
    seen = []
    gui._confirm = lambda title, text: seen.append(text) or False
    gui._start_batch()
    assert "NOT READ" in seen[0]


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


# ── the re-torque question (a remembered opening point exists) ─────────────

REMEMBERED = dict(torque_Nm=0.30, t_open_C=61.2, upstream_bar=3.0, k_per_bar=-12.0,
                  scatter_K=0.2, n=4, ci95_K=0.3, creep_C_min=config.BATCH_CREEP_C_MIN,
                  batch='20260924_1612', date='24 Sep 2026')


def asked(gui, answer):
    """Start with the re-torque dialog answering `answer`; returns its text."""
    from driver import openings
    openings.remember(dict(REMEMBERED))
    enter_torque(gui)
    seen = []
    gui._ask = lambda title, text: seen.append(text) or answer
    gui._confirm = lambda *a: pytest.fail("the plain confirmation shouldn't be shown")
    gui._start_batch()
    batchrun.tick()
    return seen[0] if seen else ""


def test_not_retorqued_starts_from_the_remembered_point(gui):
    text = asked(gui, False)
    assert "Has the seat screw been re-torqued" in text and "61.2 °C at 3.00 bar" in text
    assert batchrun.running() and batchrun.labels()[0] == 'testrun01'


def test_retorqued_scouts(gui):
    asked(gui, True)
    assert batchrun.running() and batchrun.labels()[0] == 'scout1'


def test_cancel_at_the_question_starts_nothing(gui):
    asked(gui, None)
    assert not batchrun.running()


def test_no_question_without_a_remembered_point(gui):
    enter_torque(gui)
    seen = []
    gui._ask = lambda *a: pytest.fail("no re-torque question without a remembered point")
    gui._confirm = lambda title, text: seen.append(text) or True
    gui._start_batch()
    assert "No remembered opening point" in seen[0] and batchrun.running()


# ── batch is the fourth mode (28 Sept 2026) ─────────────────────────────────
# The test runs row is greyed out unless batch is selected; in batch mode the
# duty / setpoint / target boxes and "update" are, and ARM is left to the batch.

def test_the_batch_row_is_greyed_out_in_the_other_modes(gui):
    readings()
    gui._set_entry(gui.seat_entry, "0.3")
    gui._set_seat_screw()
    gui._poll()
    assert gui.mode_var.get() == "manual"
    assert state(gui.runs_entry) == "disabled" and state(gui.batch_btn) == "disabled"
    assert state(gui.duty_entry) == "normal"


def test_batch_mode_enables_only_the_batch_row(gui):
    enter_torque(gui)
    assert state(gui.runs_entry) == "normal" and state(gui.batch_btn) == "normal"
    for w in (gui.duty_entry, gui.sp_entry, gui.p_entry, gui.update_btn):
        assert state(w) == "disabled"


def test_selecting_batch_sends_nothing_to_the_heater(gui):
    enter_torque(gui)
    assert control.snapshot()['mode'] == "manual"


def test_arm_is_left_to_the_batch_in_batch_mode(gui):
    enter_torque(gui)
    gui._toggle_arm()
    assert not control.snapshot()['armed']
    assert "start batch' arms the heater itself" in shared.recent_events()[-1][1]


def test_back_to_a_heater_mode_greys_the_batch_row_again(gui):
    enter_torque(gui)
    gui.mode_var.set("auto-t")
    gui._on_mode()
    assert state(gui.runs_entry) == "disabled" and state(gui.batch_btn) == "disabled"
    assert state(gui.sp_entry) == "normal" and state(gui.update_btn) == "normal"
    assert control.snapshot()['mode'] == "auto-t"
