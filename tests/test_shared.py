"""shared.py's own behaviour: readings, the heater output record, health
flags and the hand-over to the logger."""
import pytest

from driver import shared


def test_readings_are_copies_not_the_real_thing():
    shared.store_keller(1.2, 22.5, 100.0)
    first = shared.latest()
    first['keller_pressure_samples'].append(99.0)
    first['vacuum_chamber_mbar'] = 1.0
    assert shared.latest()['keller_pressure_samples'] == [1.2]
    assert shared.latest()['vacuum_chamber_mbar'] is None


def test_keller_samples_are_averaged_and_cleared_for_each_row():
    for p in (1.0, 1.2, 1.4):
        shared.store_keller(p, 22.0, 100.0)
    row = shared.take_log_readings()
    assert row['p_mean'] == pytest.approx(1.2) and row['n_keller'] == 3
    assert shared.take_log_readings()['n_keller'] == 0          # taken once
    assert shared.latest()['keller_pressure_bar'] == 1.4        # latest value stays


def test_heater_output_record():
    shared.store_heater_output(out_high=True, v_meas=24.0)
    out = shared.heater_output()
    assert out['out_high'] and out['v_meas'] == 24.0
    out['v_meas'] = 0.0                                          # a copy
    assert shared.heater_output()['v_meas'] == 24.0
    with pytest.raises(KeyError):
        shared.store_heater_output(v_mean=1.0)                   # misspelt field
    shared.clear_heater_output()
    assert shared.heater_output() == {'out_high': False, 'v_now': 0.0, 'i_now': 0.0,
                                      'rail_meas': None, 'v_meas': None, 'i_meas': None,
                                      'v_meas_mean': None, 'i_meas_mean': None,
                                      'p_meas_mean': None}


def test_health_flags():
    assert shared.health() == {'keller': False, 'labjack': False, 'tc': False, 'csv': None}
    shared.set_health(keller=True, csv=False)
    assert shared.health()['keller'] and shared.health()['csv'] is False
    with pytest.raises(KeyError):
        shared.set_health(labjackk=True)


def test_events_and_edges_are_handed_over_once():
    shared.log_event("first")
    shared.log_event("second")
    assert shared.take_events() == ["first", "second"]
    assert shared.take_events() == []
    assert [t for _, t in shared.recent_events()] == ["first", "second"]   # GUI keeps them
    shared.put_back_events(["first"])                                      # write failed
    assert shared.take_events() == ["first"]
    shared.queue_edge({'gate': 1})
    assert shared.pending_edges() == [{'gate': 1}]                         # peek
    assert shared.take_edges() == [{'gate': 1}] and shared.take_edges() == []


def test_charts_keep_the_recent_history():
    for k in range(5):
        shared.store_vacuum(1e-7 * (k + 1), None, 5.0)
        shared.push_power(k)
    charts = shared.charts()
    assert charts['vacuum'][-1] == pytest.approx(5e-7)
    assert charts['power'] == [0, 1, 2, 3, 4]
    assert set(charts) == {'upstream', 'vacuum', 'valve_temp', 'power'}
