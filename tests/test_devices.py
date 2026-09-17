"""Pure helpers in devices.py: vacuum gauge conversion and pin checks."""
import pytest

pytest.importorskip("serial")   # devices.py needs pyserial, like the real driver

from driver import config, devices  # noqa: E402


def gauge_to_labjack(u):
    return u / config.VACUUM_DIVIDER_RATIO


@pytest.mark.parametrize("u_gauge", [1.96, 2.6, 3.8, 5.69, 7.0, 8.6])
def test_ikr270_characteristic(u_gauge):
    # Pfeiffer IKR 270 manual: p [mbar] = 10 ** (1.25 * U - 12.75)
    expected = 10 ** (1.25 * u_gauge - 12.75)
    got = devices.voltage_to_vacuum_mbar(gauge_to_labjack(u_gauge))
    assert got == pytest.approx(expected, rel=1e-9)


def test_calibration_point():
    # 1.713 V at the LabJack read 1.5e-6 mbar on the gauge display
    assert devices.voltage_to_vacuum_mbar(1.713) == pytest.approx(1.5e-6, rel=0.01)


@pytest.mark.parametrize("u_gauge, status", [
    (0.3, config.VAC_ERROR),
    (1.9, config.VAC_UNDER),
    (8.7, config.VAC_OVER),
    (5.0, None),
])
def test_gauge_status(u_gauge, status):
    v = gauge_to_labjack(u_gauge)
    assert devices.vacuum_gauge_status(v) == status
    if status is not None:
        assert devices.voltage_to_vacuum_mbar(v) is None   # never a made-up number


def test_saturated_input():
    assert devices.vacuum_gauge_status(config.LABJACK_AIN_SAT_V) == config.VAC_SATURATED


def test_sense_pins_default_ok():
    assert devices.check_sense_pins() is None


@pytest.mark.parametrize("v_ain, i_ain, fragment", [
    (0, None, "heater gate"),
    (2, None, "vacuum gauge"),
    (5, None, "MAX31856"),
    (1, 1, "both point at FIO1"),
    (9, None, "must be an FIO number"),
])
def test_sense_pin_clashes(monkeypatch, v_ain, i_ain, fragment):
    monkeypatch.setattr(devices, "HEATER_V_AIN", v_ain)
    monkeypatch.setattr(devices, "HEATER_I_AIN", i_ain)
    assert fragment in devices.check_sense_pins()


def test_analog_mask(monkeypatch):
    assert devices._fio_analog_mask() == 1 << config.LABJACK_FIO2_CHANNEL
    monkeypatch.setattr(devices, "HEATER_V_AIN", 1)
    monkeypatch.setattr(devices, "HEATER_I_AIN", 3)
    assert devices._fio_analog_mask() == (1 << 2) | (1 << 1) | (1 << 3)
