"""The t-min-tune controls in the window (history 36; they replaced the
cycling controls): its row (upstream target and ± band) shows only in
t-min-tune; start needs a torque, a target and a disarmed heater and asks
for confirmation — and, if the torque has a seating already, whether the
screw was re-torqued; while it runs it owns the heater (the heater controls
and the torque are locked, the band stays editable); stop and DISARM end it."""
import time

import pytest

from driver import config, control, openmap, shared, tminlog, tminrun


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
    real_start = tminrun.start
    monkeypatch.setattr(tminrun, "start",
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


def enter_torque(g, nm="0.3", target="3.0"):
    readings()
    g.seat_entry.delete(0, "end")
    g.seat_entry.insert(0, nm)
    g._set_seat_screw()
    g.mode_var.set("t-min-tune")
    g._on_mode()
    g._set_entry(g.up_entry, target)
    g._poll()


def test_start_needs_the_torque(gui):
    assert state(gui.batch_btn) == "disabled" and state(gui.abort_btn) == "disabled"
    enter_torque(gui)
    assert state(gui.batch_btn) == "normal"


def test_start_needs_the_upstream_target(gui):
    enter_torque(gui, target="")
    gui._start_batch()
    assert not tminrun.running()
    assert "upstream target" in shared.recent_events()[-1][1]


def test_start_needs_a_disarmed_heater(gui):
    enter_torque(gui)
    control.heater_command(armed=True)
    gui._poll()
    assert state(gui.batch_btn) == "disabled"


def test_cancel_does_not_start(gui):
    enter_torque(gui)
    gui._confirm = lambda *a: False
    gui._start_batch()
    assert not tminrun.running()


def test_the_confirmation_shows_torque_band_and_upstream(gui):
    enter_torque(gui, "0.45")
    shared.store_keller(3.21, None, control.clock())
    seen = []
    gui._confirm = lambda title, text: seen.append(text) or False
    gui._start_batch()
    assert "0.45 N·m" in seen[0] and "hold 3 ± 0.05 bar" in seen[0] and "3.210 bar now" in seen[0]
    assert "+1 K every 5 min" in seen[0] and "first test scouts" in seen[0]
    assert "logs/t-min.csv" in seen[0]


def test_the_confirmation_warns_when_the_keller_is_not_read(gui):
    enter_torque(gui, "0.45")
    shared.clear_keller()
    seen = []
    gui._confirm = lambda title, text: seen.append(text) or False
    gui._start_batch()
    assert "NOT READ" in seen[0]


def test_running_locks_the_heater_and_torque_but_not_the_band(gui):
    enter_torque(gui)
    gui._set_entry(gui.band_entry, "0.1")
    gui._start_batch()
    gui._poll()
    assert tminrun.running() and tminrun._s['band'] == 0.1 and tminrun._s['target'] == 3.0
    for w in (*gui.mode_buttons, gui.update_btn, gui.sp_entry, gui.seat_entry,
              gui.seat_btn, gui.batch_btn):
        assert state(w) == "disabled"
    assert state(gui.up_entry) == "normal" and state(gui.band_entry) == "normal"
    assert state(gui.abort_btn) == "normal" and state(gui.arm_btn) == "normal"
    assert "t-min-tune running" in gui.batch_status.cget("text")
    # a new target, typed while it runs
    gui._set_entry(gui.up_entry, "2.5")
    gui._send_band()
    assert tminrun._s['target'] == 2.5
    gui._abort_batch()
    gui._poll()
    assert not tminrun.running() and state(gui.seat_entry) == "normal"
    assert state(gui.batch_btn) == "normal"
    assert "stopped" in gui.batch_status.cget("text")


def test_disarm_stops_and_arm_is_refused_while_running(gui):
    enter_torque(gui)
    gui._start_batch()
    gui._toggle_arm()                              # heater disarmed: ARM refused
    assert not control.snapshot()['armed'] and tminrun.running()
    control.heater_command(armed=True)             # as t-min-tune does
    gui._toggle_arm()                              # DISARM
    assert not control.snapshot()['armed'] and not tminrun.running()


# ── the re-torque question (the torque has a seating already) ──────────────

OLD = "20260930_110000_0.30Nm"


def asked(gui, answer):
    tminlog.append(dict(time="2026-09-30T11:30:00", seating=OLD, torque_Nm=0.30,
                        upstream_target_bar=3.0, outcome="t_min", t_min_degC=61.2,
                        upstream_at_open_bar=3.0, counted=1), sheet=False)
    enter_torque(gui)
    seen = []
    gui._ask = lambda title, text: seen.append(text) or answer
    gui._confirm = lambda *a: pytest.fail("the plain confirmation shouldn't be shown")
    gui._start_batch()
    return seen[0] if seen else ""


def test_not_retorqued_continues_the_seating(gui):
    text = asked(gui, False)
    assert "Has the seat screw been re-torqued" in text and OLD in text
    assert tminrun.running() and tminrun.seating() == OLD


def test_retorqued_starts_a_new_seating(gui):
    asked(gui, True)
    assert tminrun.running() and tminrun.seating() != OLD
    assert tminrun.seating().endswith("_0.30Nm")


def test_cancel_at_the_question_starts_nothing(gui):
    asked(gui, None)
    assert not tminrun.running()


def test_a_seating_in_the_openings_table_also_asks(gui):
    openmap.append(dict(time="2026-09-28T13:10:49", seating="old_cycling", torque_Nm=0.30,
                        upstream_bar=3.0, t_open_degC=61.2, deep=1), sheet=False)
    enter_torque(gui)
    seen = []
    gui._ask = lambda title, text: seen.append(text) or None
    gui._start_batch()
    assert seen and "old_cycling" in seen[0]


def test_no_question_without_a_seating_at_that_torque(gui):
    enter_torque(gui)
    seen = []
    gui._ask = lambda *a: pytest.fail("no re-torque question without a seating")
    gui._confirm = lambda title, text: seen.append(text) or True
    gui._start_batch()
    assert "Nothing measured at this torque yet" in seen[0] and tminrun.running()


# ── t-min-tune is the fourth mode ──────────────────────────────────────────

def packed(w):
    return w.winfo_manager() == "pack"


def test_the_upstream_row_shows_only_in_t_min_tune(gui):
    readings()
    gui._set_entry(gui.seat_entry, "0.3")
    gui._set_seat_screw()
    gui._poll()
    assert gui.mode_var.get() == "manual"
    assert not packed(gui._up_row_frame) and state(gui.batch_btn) == "disabled"
    assert state(gui.duty_entry) == "normal"
    enter_torque(gui)
    assert packed(gui._up_row_frame) and state(gui.up_entry) == "normal"
    assert gui.band_entry.get() == f"{config.TMIN_BAND_BAR:g}"
    assert packed(gui.batch_btn) and not packed(gui.update_btn)
    assert gui.batch_btn.cget("text") == "start t-min"
    for w in (gui.duty_entry, gui.sp_entry, gui.p_entry, gui.update_btn):
        assert state(w) == "disabled"
    gui.mode_var.set("auto-t")
    gui._on_mode()
    assert not packed(gui._up_row_frame) and packed(gui.update_btn)
    assert state(gui.sp_entry) == "normal" and control.snapshot()['mode'] == "auto-t"


def test_there_is_no_deep_every_any_more(gui):
    assert not hasattr(gui, "runs_entry")
    assert "cycle" not in [b.cget("text") for b in gui.mode_buttons]
    assert "t-min-tune" in [b.cget("text") for b in gui.mode_buttons]


def test_selecting_t_min_tune_sends_nothing_to_the_heater(gui):
    enter_torque(gui)
    assert control.snapshot()['mode'] == "manual"


def test_arm_is_left_to_t_min_tune(gui):
    enter_torque(gui)
    gui._toggle_arm()
    assert not control.snapshot()['armed']
    assert "'start t-min' arms the heater itself" in shared.recent_events()[-1][1]


def test_the_band_is_drawn_and_the_upstream_chart_is_taller(gui):
    enter_torque(gui)
    for i in range(40):
        shared.store_keller(3.0 + (0.2 if i > 30 else 0.0), None, control.clock() + i)
    gui._poll()
    assert gui._up_weight == config.TMIN_UP_CHART_WEIGHT
    assert "target 3 ± 0.05" in gui.up_canvas.title
    gui.mode_var.set("manual")
    gui._on_mode()
    gui._poll()
    assert gui._up_weight == 1 and "target" not in gui.up_canvas.title


# ── closing the driver once t-min-tune has converged (history 39) ───────────

def _converged(monkeypatch, gui, running=False):
    import driver.gui as gm
    monkeypatch.setattr(gm, "TMIN_QUIT_DELAY_S", 0.2)
    monkeypatch.setattr(tminrun, "converged_session", lambda: 1234.0)
    monkeypatch.setattr(tminrun, "running", lambda: running)
    closed = []
    gui.shutdown = lambda: closed.append(True)
    return closed


def test_the_driver_closes_after_t_min_tune_converged(gui, monkeypatch):
    closed = _converged(monkeypatch, gui)
    assert gui._auto_close(False) is False                  # the countdown starts
    assert gui._quit_at is not None
    assert any("closes in" in t for _, t in shared.recent_events())
    time.sleep(0.25)
    assert gui._auto_close(False) is True and closed == [True]
    assert gui._auto_close(False) is False and closed == [True]   # once per session


def test_starting_again_cancels_the_close(gui, monkeypatch):
    closed = _converged(monkeypatch, gui)
    gui._auto_close(False)
    time.sleep(0.25)
    assert gui._auto_close(True) is False and gui._quit_at is None and not closed
    assert any("cancelled" in t for _, t in shared.recent_events())


def test_it_can_be_switched_off(gui, monkeypatch):
    import driver.gui as gm
    closed = _converged(monkeypatch, gui)
    monkeypatch.setattr(gm, "TMIN_QUIT_WHEN_CONVERGED", False)
    assert gui._auto_close(False) is False and gui._quit_at is None and not closed
