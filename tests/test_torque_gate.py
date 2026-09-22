"""Seat screw torque gate: nothing else can be used until the torque is entered.

Agreed behaviour (21 Sept 2026):
  * at start-up the seat screw torque input is the only thing that works:
    ARM, the mode buttons, the duty / setpoint / target boxes and "update"
    are all locked
  * while it waits, the torque input is orange and everything else is white
  * once a valid torque is entered, the torque input turns white and the
    rest unlocks as normal
  * an invalid entry leaves it locked and orange
"""
import time

import pytest

from driver import control, palette, readout, shared


# ── the rule, without a window ───────────────────────────────────────────────

def test_locked_and_orange_until_entered():
    assert readout.torque_gate(None) == (True, "prompt")


@pytest.mark.parametrize("nm", [0.4, 0.0])           # 0 is a value; blank is None
def test_unlocked_and_white_once_entered(nm):
    assert readout.torque_gate(nm) == (False, "bright")


def test_status_panel_shows_not_entered_in_orange():
    segs = readout.status_segments(shared.latest(), shared.health(), control.snapshot(),
                                   shared.heater_output(), True, None)
    assert ("  (not entered)\n", "prompt") in segs


def test_the_prompt_colour_is_orange():
    r, g, b = (int(palette.PROMPT[i:i + 2], 16) for i in (1, 3, 5))
    assert r == 0xff and 0x80 <= g <= 0xb0 and b < 0x40


# ── the real window ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tk_root():
    """One Tk interpreter for all the window tests. Starting a fresh one per
    test intermittently failed to load init.tcl on the lab PC (Windows,
    Python 3.13). A few tries, then skip with the reason rather than error."""
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
def gui(tk_root):
    import tkinter as tk
    from driver.gui import TEGui
    g = TEGui(root=tk.Toplevel(tk_root))
    g.root.update()
    yield g
    g.destroy()


def others(g):
    """Every control other than the torque input."""
    return [g.arm_btn, *g.mode_buttons, g.update_btn,
            g.duty_entry, g.sp_entry, g.p_entry]


def events():
    return [text for _, text in shared.recent_events()]


def test_at_start_only_the_torque_input_works(gui):
    for w in others(gui):
        assert str(w.cget("state")) == "disabled", w
    assert str(gui.seat_entry.cget("state")) == "normal"
    assert str(gui.seat_btn.cget("state")) == "normal"


def test_at_start_the_torque_input_is_orange(gui):
    assert gui.seat_entry.cget("highlightbackground") == palette.PROMPT
    assert gui.seat_entry.cget("fg") == palette.PROMPT
    assert gui.seat_entry.label.cget("fg") == palette.PROMPT
    assert gui.seat_btn.cget("fg") == palette.PROMPT


def test_at_start_everything_else_is_white(gui):
    for w in others(gui):
        assert w.cget("disabledforeground") == palette.BRIGHT, w
    for e in (gui.duty_entry, gui.sp_entry, gui.p_entry):
        assert e.label.cget("fg") == palette.BRIGHT


def test_arming_is_refused_while_locked(gui):
    gui._toggle_arm()                          # even if called directly
    assert not control.snapshot()["armed"]
    assert "Enter the seat screw torque first" in events()[-1]


def test_entering_a_torque_unlocks_and_turns_it_white(gui):
    gui._set_entry(gui.seat_entry, "0,4")
    gui._set_seat_screw()
    gui.root.update()
    assert shared.seat_screw_torque() == 0.4
    for w in (gui.arm_btn, *gui.mode_buttons, gui.update_btn, gui.duty_entry):
        assert str(w.cget("state")) == "normal", w
    # only the selected mode's box is live, as before
    assert str(gui.sp_entry.cget("state")) == "disabled"
    assert gui.seat_entry.cget("fg") == palette.BRIGHT
    assert gui.seat_entry.cget("highlightbackground") == palette.BRIGHT
    assert gui.seat_entry.label.cget("fg") == palette.BRIGHT
    assert gui.seat_btn.cget("fg") == palette.BRIGHT
    gui._toggle_arm()
    assert control.snapshot()["armed"]


def test_an_invalid_entry_stays_locked_and_orange(gui):
    gui._set_entry(gui.seat_entry, "abc")
    gui._set_seat_screw()
    gui.root.update()
    assert shared.seat_screw_torque() is None
    assert str(gui.arm_btn.cget("state")) == "disabled"
    assert gui.seat_entry.cget("fg") == palette.PROMPT
