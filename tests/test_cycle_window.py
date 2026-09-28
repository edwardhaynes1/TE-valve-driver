"""The cycling controls in the window (history 34; they replaced the batch
controls): start needs a torque and a disarmed heater and asks for
confirmation — and, if the torque has a setting already, whether the
screw was re-torqued; while cycling runs it owns the heater (the heater
controls and the torque are locked); stop and DISARM end it."""
import time

import pytest

from driver import config, control, cyclerun, openmap, shared


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
    monkeypatch.setattr(config, "CYCLE_DIR", str(tmp_path / "cycles"))
    monkeypatch.setattr(cyclerun, "PLOT", False)
    real_start = cyclerun.start
    monkeypatch.setattr(cyclerun, "start",
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
    g.mode_var.set("cycle")
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
    assert not cyclerun.running()


def test_the_confirmation_shows_torque_and_measured_upstream(gui):
    enter_torque(gui, "0.45")
    shared.store_keller(3.21, None, control.clock())
    seen = []
    gui._confirm = lambda title, text: seen.append(text) or False
    gui._start_batch()
    assert "0.45 N·m" in seen[0] and "3.210 bar (measured;" in seen[0]
    assert "refill by hand" in seen[0] and "Every 5 cycles, one deep" in seen[0]
    assert "first cycle scouts" in seen[0]


def test_the_confirmation_warns_when_the_keller_is_not_read(gui):
    enter_torque(gui, "0.45")
    shared.clear_keller()
    seen = []
    gui._confirm = lambda title, text: seen.append(text) or False
    gui._start_batch()
    assert "NOT READ" in seen[0]


def test_deep_every_must_be_a_whole_number(gui):
    enter_torque(gui)
    gui._set_entry(gui.runs_entry, "x")
    gui._start_batch()
    assert not cyclerun.running() and "not a whole number" in shared.recent_events()[-1][1]


def test_running_locks_the_heater_and_torque(gui):
    enter_torque(gui)
    gui._set_entry(gui.runs_entry, "3")
    gui._start_batch()
    gui._poll()
    assert cyclerun.running() and cyclerun._c['deep_every'] == 3
    for w in (*gui.mode_buttons, gui.update_btn, gui.sp_entry, gui.seat_entry,
              gui.seat_btn, gui.runs_entry, gui.batch_btn):
        assert state(w) == "disabled"
    assert state(gui.abort_btn) == "normal" and state(gui.arm_btn) == "normal"
    assert "cycle001 (deep)" in gui.batch_status.cget("text")
    gui._abort_batch()
    gui._poll()
    assert not cyclerun.running() and state(gui.seat_entry) == "normal"
    assert state(gui.batch_btn) == "normal"
    assert "stopped" in gui.batch_status.cget("text")


def test_disarm_stops_and_arm_is_refused_while_cycling(gui):
    enter_torque(gui)
    gui._start_batch()
    gui._toggle_arm()                              # heater disarmed: ARM refused
    assert not control.snapshot()['armed'] and cyclerun.running()
    control.heater_command(armed=True)             # as cycling does
    gui._toggle_arm()                              # DISARM
    assert not control.snapshot()['armed'] and not cyclerun.running()


# ── the re-torque question (the torque has a setting already) ──────────────

OLD = "20260928_130630_0.30Nm"


def asked(gui, answer):
    """Start with the re-torque dialog answering `answer`; returns its text."""
    openmap.append(dict(time="2026-09-28T13:10:49", setting=OLD, torque_Nm=0.30,
                        upstream_bar=3.0, t_open_degC=61.2, deep=1), sheet=False)
    enter_torque(gui)
    seen = []
    gui._ask = lambda title, text: seen.append(text) or answer
    gui._confirm = lambda *a: pytest.fail("the plain confirmation shouldn't be shown")
    gui._start_batch()
    return seen[0] if seen else ""


def test_not_retorqued_continues_the_setting(gui):
    text = asked(gui, False)
    assert "Has the seat screw been re-torqued" in text and OLD in text
    assert cyclerun.running() and cyclerun.setting() == OLD


def test_retorqued_starts_a_new_setting(gui):
    asked(gui, True)
    assert cyclerun.running() and cyclerun.setting() != OLD
    assert cyclerun.setting().endswith("_0.30Nm")


def test_cancel_at_the_question_starts_nothing(gui):
    asked(gui, None)
    assert not cyclerun.running()


def test_no_question_without_a_setting_at_that_torque(gui):
    enter_torque(gui)
    seen = []
    gui._ask = lambda *a: pytest.fail("no re-torque question without a setting")
    gui._confirm = lambda title, text: seen.append(text) or True
    gui._start_batch()
    assert "Nothing measured at this torque yet" in seen[0] and cyclerun.running()


# ── cycle is the fourth mode ────────────────────────────────────────────────
# Its input (deep every) is greyed out unless cycle is selected; in cycle
# mode the duty / setpoint / target boxes and "update" are, and ARM is left
# to cycling.

def test_the_cycle_row_is_greyed_out_in_the_other_modes(gui):
    readings()
    gui._set_entry(gui.seat_entry, "0.3")
    gui._set_seat_screw()
    gui._poll()
    assert gui.mode_var.get() == "manual"
    assert state(gui.runs_entry) == "disabled" and state(gui.batch_btn) == "disabled"
    assert state(gui.duty_entry) == "normal"


def test_cycle_mode_enables_only_its_row(gui):
    enter_torque(gui)
    assert state(gui.runs_entry) == "normal" and state(gui.batch_btn) == "normal"
    assert gui.batch_btn.cget("text") == "start cycling"
    for w in (gui.duty_entry, gui.sp_entry, gui.p_entry, gui.update_btn):
        assert state(w) == "disabled"


def test_selecting_cycle_sends_nothing_to_the_heater(gui):
    enter_torque(gui)
    assert control.snapshot()['mode'] == "manual"


def test_arm_is_left_to_cycling_in_cycle_mode(gui):
    enter_torque(gui)
    gui._toggle_arm()
    assert not control.snapshot()['armed']
    assert "start cycling' arms the heater itself" in shared.recent_events()[-1][1]


def test_back_to_a_heater_mode_greys_the_cycle_row_again(gui):
    enter_torque(gui)
    gui.mode_var.set("auto-t")
    gui._on_mode()
    assert state(gui.runs_entry) == "disabled" and state(gui.batch_btn) == "disabled"
    assert state(gui.sp_entry) == "normal" and state(gui.update_btn) == "normal"
    assert control.snapshot()['mode'] == "auto-t"


def test_the_deep_every_input_sits_with_the_other_modes_inputs(gui):
    assert gui.runs_entry.master is gui.p_entry.master
    assert gui.runs_entry.label.cget("text") == "deep every"
    packed = lambda w: w.winfo_manager() == "pack"
    assert packed(gui.update_btn) and not packed(gui.batch_btn) and not packed(gui.abort_btn)
    enter_torque(gui)
    assert not packed(gui.update_btn) and packed(gui.batch_btn) and packed(gui.abort_btn)
    gui.mode_var.set("manual")
    gui._on_mode()
    assert packed(gui.update_btn) and not packed(gui.batch_btn)
