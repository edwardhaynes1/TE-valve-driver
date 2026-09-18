"""readout.py: what the window says, checked without opening a window."""
import pytest

from driver import controller, readout, shared

FULL_W = 24.0 ** 2 / 88.0


@pytest.fixture
def h():
    return controller.new_state()


@pytest.fixture
def out():
    return shared.heater_output()          # a fresh, all-calculated readout


def text_of(segments):
    return "".join(t for t, _ in segments)


def readings(**kw):
    r = dict(keller_pressure_samples=[1.19], keller_temperature_samples=[22.5],
             keller_pressure_bar=1.19, keller_pressure_t=0.0,
             vacuum_chamber_mbar=1.5e-7, vacuum_status=None, vacuum_gauge_V=5.7,
             te_temperature_degC=41.9, tc_fault=0)
    r.update(kw)
    return r


HEALTHY = dict(keller=True, labjack=True, tc=True, csv=True)


# ── status panel ────────────────────────────────────────────────────────────

def test_status_panel_shows_the_readings(h, out):
    text = text_of(readout.status_segments(readings(), HEALTHY, h, out, True))
    assert "[KELLER:OK]" in text and "[VALVE-T:OK]" in text
    assert "UPSTREAM P   1.1900 bar" in text
    assert "KELLER T     22.5 °C" in text
    assert "VACUUM       1.50e-07 mbar" in text
    assert "(5.70 V at gauge)" in text
    assert "VALVE T      41.90 °C" in text


def test_missing_readings_show_dashes_not_stale_values(h, out):
    r = readings(keller_pressure_samples=[], keller_temperature_samples=[],
                 vacuum_chamber_mbar=None, te_temperature_degC=None, tc_fault=None)
    segs = readout.status_segments(r, dict(HEALTHY, keller=False), h, out, True)
    text = text_of(segs)
    assert "[KELLER:--]" in text
    for line in ("UPSTREAM P   ---", "KELLER T     ---", "UPSTREAM P20 ---",
                 "VACUUM       ---", "VALVE T      ---"):
        assert line in text, line


def test_gauge_and_thermocouple_faults_are_shown_in_red(h, out):
    r = readings(vacuum_chamber_mbar=None, vacuum_status="overrange (>1e-2 mbar)",
                 te_temperature_degC=None, tc_fault=0x01)
    segs = readout.status_segments(r, HEALTHY, h, out, True)
    assert "[overrange (>1e-2 mbar)]" in text_of(segs)
    assert "[open circuit]" in text_of(segs)
    assert any(tag == "err" and "open circuit" in t for t, tag in segs)


def test_p20_is_computed_from_the_keller_chip_temperature(h, out):
    r = readings(keller_pressure_samples=[1.2], keller_temperature_samples=[20.0])
    assert "UPSTREAM P20 1.2000 bar" in text_of(
        readout.status_segments(r, HEALTHY, h, out, True))


# ── heater V / I / P lines ──────────────────────────────────────────────────

def test_calculated_heater_lines(h, out):
    h['duty_actual'] = 0.5
    lines = readout.heater_vi_lines(h, out, labjack_ok=True)
    assert [line[0].strip() for line in lines] == ["HEATER V", "HEATER I", "HEATER P"]
    assert "12.00 V mean" in lines[0][1] and "assumes SW171 on" in lines[0][2]
    assert f"{0.5 * FULL_W:6.3f} W mean" == lines[2][1]


def test_measured_heater_lines_take_over_when_wired(h, out):
    h['duty_actual'] = 0.5
    out.update(out_high=True, v_meas=24.0, v_meas_mean=12.0, rail_meas=24.1,
               i_meas=0.27, i_meas_mean=0.14, p_meas_mean=3.2)
    lines = readout.heater_vi_lines(h, out, labjack_ok=True)
    assert "24.00 V ON" in lines[0][1] and "rail 24.10 V" in lines[0][2]
    assert "0.270 A ON" in lines[1][1]
    assert "3.200 W mean" in lines[2][1] and "calc" in lines[2][2]


def test_no_labjack_means_no_heater_numbers(h, out):
    assert all(line[1] == "---" for line in readout.heater_vi_lines(h, out, False))


# ── the armed / tripped line ────────────────────────────────────────────────

def test_disarmed(h):
    assert readout.heater_status(h) == ("disarmed · output low", "dim")


def test_armed_shows_duty_power_and_time_left(h):
    h.update(armed=True, armed_at=1000.0, duty_actual=0.15, mode=controller.AUTO_T,
             setpoint_C=42.0)
    text, tag = readout.heater_status(h, now=1000.0 + 600)
    assert tag == "bright"
    assert "ARMED · auto-t · duty 15.0 %" in text
    assert f"{0.15 * FULL_W:.2f} W" in text
    assert "T_sp 42.0 °C" in text and "off in 50 min" in text


def test_a_trip_replaces_everything_with_the_reason(h):
    h.update(armed=False, trip_reason="over-temperature 161.0 °C")
    text, tag = readout.heater_status(h)
    assert tag == "warn"
    assert "TRIPPED — over-temperature 161.0 °C" in text and "disarm, then arm" in text


# ── what the loop is doing ──────────────────────────────────────────────────

def test_manual_says_nothing(h):
    h.update(armed=True, mode=controller.MANUAL)
    assert readout.loop_status(h) == ("", "dim")


@pytest.mark.parametrize("stage, expected", [('burst', "BURST full power"),
                                             ('coast', "coasting")])
def test_auto_t_burst_and_coast(h, stage, expected):
    h.update(armed=True, mode=controller.AUTO_T, t_burst=stage, setpoint_C=45.0,
             t_burst_peak=41.0, t_brake=6.0)
    text, tag = readout.loop_status(h)
    assert expected in text and tag == "bright"


def test_auto_p_idle_explains_what_arming_will_do(h):
    h.update(mode=controller.AUTO_P, p_target_mbar=1e-6)
    text, tag = readout.loop_status(h)
    assert "auto-p idle" in text and "until the valve opens" in text and tag == "dim"


def test_auto_p_seeking(h):
    h.update(armed=True, mode=controller.AUTO_P, p_init=False, p_filt=-6.8,
             p_phase='seek', p_base=-6.82, p_goal=40.3, p_shift=0.0, p_ramping=True,
             setpoint_C=40.1, p_target_mbar=1e-6)
    text, tag = readout.loop_status(h)
    assert "auto-p seeking · valve shut" in text and "creeping" in text
    assert "baseline 1.51e-07 mbar" in text and tag == "bright"


def test_auto_p_tracking(h):
    h.update(armed=True, mode=controller.AUTO_P, p_init=False, p_filt=-6.0,
             p_phase='track', p_base=-6.82, p_err=0.05, p_shift=1.2,
             setpoint_C=41.0, p_target_mbar=1e-6)
    text, tag = readout.loop_status(h)
    assert "auto-p · target 1.00e-06" in text and "err +0.05 dec" in text
    assert "upstream shift +1.2 K" in text and tag == "bright"


def test_a_target_below_the_baseline_warns(h):
    h.update(armed=True, mode=controller.AUTO_P, p_init=False, p_filt=-6.8,
             p_phase='track', p_base=-6.82, p_err=0.0, p_shift=0.0,
             setpoint_C=40.0, p_target_mbar=1.6e-7)
    text, tag = readout.loop_status(h)
    assert "BELOW the lowest holdable" in text and tag == "warn"


# ── the settings summary in the event log ───────────────────────────────────

@pytest.mark.parametrize("mode, expected", [
    (controller.MANUAL, "manual · duty 30 %"),
    (controller.AUTO_T, "auto-t · setpoint 42 °C"),
    (controller.AUTO_P, "auto-p · target 1.00e-06 mbar"),
])
def test_mode_summary(mode, expected):
    assert readout.mode_summary(mode, 0.3, 42.0, 1e-6) == expected
