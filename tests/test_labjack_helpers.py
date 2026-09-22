"""Pure helpers: vacuum gauge conversion and pin checks (labjack.py),
thermocouple decoding (thermocouple.py)."""
import pytest


from driver import config, labjack, thermocouple  # noqa: E402


def gauge_to_labjack(u):
    return u / config.VACUUM_DIVIDER_RATIO


@pytest.mark.parametrize("u_gauge", [1.96, 2.6, 3.8, 5.69, 7.0, 8.6])
def test_ikr270_characteristic(u_gauge):
    # Pfeiffer IKR 270 manual: p [mbar] = 10 ** (1.25 * U - 12.75)
    expected = 10 ** (1.25 * u_gauge - 12.75)
    got = labjack.voltage_to_vacuum_mbar(gauge_to_labjack(u_gauge))
    assert got == pytest.approx(expected, rel=1e-9)


def test_calibration_point():
    # 1.713 V at the LabJack read 1.5e-6 mbar on the gauge display
    assert labjack.voltage_to_vacuum_mbar(1.713) == pytest.approx(1.5e-6, rel=0.01)


@pytest.mark.parametrize("u_gauge, status", [
    (0.3, config.VAC_ERROR),
    (1.9, config.VAC_UNDER),
    (8.7, config.VAC_OVER),
    (5.0, None),
])
def test_gauge_status(u_gauge, status):
    v = gauge_to_labjack(u_gauge)
    assert labjack.vacuum_gauge_status(v) == status
    if status is not None:
        assert labjack.voltage_to_vacuum_mbar(v) is None   # never a made-up number


def test_saturated_input():
    assert labjack.vacuum_gauge_status(config.LABJACK_AIN_SAT_V) == config.VAC_SATURATED


def test_sense_pins_default_ok():
    assert labjack.check_sense_pins() is None


@pytest.mark.parametrize("v_ain, i_ain, fragment", [
    (0, None, "heater gate"),
    (2, None, "vacuum gauge"),
    (5, None, "MAX31856"),
    (1, 1, "both point at FIO1"),
    (9, None, "must be an FIO number"),
])
def test_sense_pin_clashes(monkeypatch, v_ain, i_ain, fragment):
    monkeypatch.setattr(labjack, "HEATER_V_AIN", v_ain)
    monkeypatch.setattr(labjack, "HEATER_I_AIN", i_ain)
    assert fragment in labjack.check_sense_pins()


def test_analog_mask(monkeypatch):
    assert labjack._fio_analog_mask() == 1 << config.LABJACK_FIO2_CHANNEL
    monkeypatch.setattr(labjack, "HEATER_V_AIN", 1)
    monkeypatch.setattr(labjack, "HEATER_I_AIN", 3)
    assert labjack._fio_analog_mask() == (1 << 2) | (1 << 1) | (1 << 3)


@pytest.mark.parametrize("celsius", [-200.0, -0.5, 0.0, 25.0, 41.25, 160.0, 1372.0])
def test_thermocouple_temperature_decoding(celsius):
    raw = int(round(celsius / 0.0078125)) & 0x7FFFF         # 19-bit two's complement
    raw <<= 5
    bytes3 = [(raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF]
    assert thermocouple._tc_decode_temp(bytes3) == pytest.approx(celsius, abs=0.0079)


@pytest.mark.parametrize("cr0, cr1, cause", [
    (0x00, 0x00, "all-zero"), (0xFF, 0xFF, "all-ones"), (0x12, 0x34, "garbage"),
    (0x00, 0x03, "power-on settings"),
])
def test_thermocouple_readback_hints(cr0, cr1, cause):
    assert cause in thermocouple.lost_hint(cr0, cr1)


def test_period_mean_keeps_only_the_last_period(monkeypatch):
    from collections import deque
    monkeypatch.setattr(labjack, "HEATER_PWM_PERIOD_S", 1.0)
    samples = deque((t / 10, 1.0 if t % 10 < 5 else 0.0) for t in range(0, 31))
    # 3.1 s of a 50 % square wave; only (2.0, 3.0] is kept
    assert labjack._period_mean(samples, 3.0) == pytest.approx(0.5)
    assert samples[0][0] == pytest.approx(2.1)
    assert labjack._period_mean(deque(), 3.0) is None
    old = deque([(0.0, 5.0)])
    assert labjack._period_mean(old, 3.0) is None and not old


def test_period_mean_weights_samples_by_time(monkeypatch):
    # Irregular ticks: the heater is ON from 0 to 0.5 s (two samples, the
    # second after a long stall) and OFF from 0.5 to 1.0 s (five quick
    # samples). Each sample covers the time since the previous one, so the
    # true mean is 50 %; counting samples would say 2 of 7.
    from collections import deque
    monkeypatch.setattr(labjack, "HEATER_PWM_PERIOD_S", 1.0)
    samples = deque([(0.10, 1.0), (0.50, 1.0),
                     (0.60, 0.0), (0.70, 0.0), (0.80, 0.0), (0.90, 0.0), (1.00, 0.0)])
    assert labjack._period_mean(samples, 1.0) == pytest.approx(0.5)
