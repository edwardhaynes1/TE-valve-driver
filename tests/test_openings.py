"""Remembered opening points (context.md, "Remembered opening point"; history 30).

Agreed behaviour (24 Sept 2026):
  * a batch's result is kept per torque in logs/opening-points.json
  * batches use it to skip the scouts — only if the operator says the seat
    screw wasn't re-torqued, and only if it was measured at the same creep rate
  * auto-p uses it ahead of the SEAT_SCREW_VALVE table (this session's learned
    point still comes first), and the event log names where it came from
  * no expiry by age
"""
import json
import math

import pytest

from driver import batchrun, config, control, controller, openings, shared

ENTRY = dict(torque_Nm=0.30, t_open_C=61.2, upstream_bar=2.8, k_per_bar=-11.0,
             k_per_bar_from='batch fit', scatter_K=0.2, n=4, ci95_K=0.3, **{'from': 'test runs'},
             creep_C_min=3.0, settings={}, batch='20260924_1612_0.30Nm_2.80bar',
             date='24 Sep 2026')


# ── the store ──────────────────────────────────────────────────────────────

def test_nothing_remembered_without_a_file():
    assert "none yet" in openings.load()
    assert openings.lookup(0.30) is None and openings.for_controller(0.30) is None


def test_remember_writes_the_file_and_loads_back():
    ok, msg = openings.remember(dict(ENTRY))
    assert ok and "0.30 N·m remembered: 61.2 °C at 2.80 bar" in msg
    data = json.load(open(config.OPENINGS_FILE, encoding="utf-8"))
    assert data['points']['0.30']['t_open_C'] == 61.2
    openings.reset()
    assert openings.lookup(0.30) is None
    assert "0.30 N·m 61.2 °C" in openings.load()
    assert openings.lookup(0.30)['n'] == 4


def test_lookup_matches_within_the_torque_tolerance_only():
    openings.remember(dict(ENTRY))
    assert openings.lookup(0.30 + config.SEAT_SCREW_TOL_NM)['t_open_C'] == 61.2
    assert openings.lookup(0.30 + config.SEAT_SCREW_TOL_NM + 0.01) is None
    assert openings.lookup(None) is None


def test_a_new_result_replaces_the_old_for_its_torque_only():
    openings.remember(dict(ENTRY))
    openings.remember(dict(ENTRY, torque_Nm=0.45, t_open_C=146.0))
    openings.remember(dict(ENTRY, t_open_C=58.0))
    assert openings.lookup(0.30)['t_open_C'] == 58.0
    assert openings.lookup(0.45)['t_open_C'] == 146.0


def test_a_corrupt_file_is_reported_not_fatal():
    open(config.OPENINGS_FILE, "w").write("{ not json")
    assert "can't read" in openings.load() and openings.lookup(0.30) is None


def test_an_unwritable_file_keeps_it_for_the_session(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OPENINGS_FILE", str(tmp_path))      # a folder: can't write
    ok, msg = openings.remember(dict(ENTRY))
    assert not ok and "this session only" in msg
    assert openings.lookup(0.30)['t_open_C'] == 61.2


def test_describe():
    assert openings.describe(ENTRY) == ("61.2 °C at 2.80 bar (±0.3 K, n = 4; batch "
                                        "20260924_1612_0.30Nm_2.80bar, 24 Sep 2026)")
    assert "upstream not read" in openings.describe(dict(ENTRY, upstream_bar=None))


# ── auto-p uses it ─────────────────────────────────────────────────────────

REM = (61.2, 2.8, "remembered: 61.2 °C at 2.80 bar")


def test_remembered_comes_before_the_table():
    c, bar, efold, how, detail = controller.opening_point(0.30, None, REM)
    assert (c, bar, how) == (61.2, 2.8, 'remembered') and detail.startswith("remembered")
    table = controller.opening_point(0.30)
    assert efold == table[2]                         # the gains keep the table's e-fold
    assert table[3] == 'calibrated'


def test_this_sessions_learned_point_comes_first():
    c, _, _, how, _ = controller.opening_point(0.30, (95.0, 4.49, 0.30), REM)
    assert (c, how) == (95.0, 'learned')


def test_no_upstream_reading_in_the_remembered_point_means_no_shift():
    assert controller._upstream_shift(3.5, None) == 0.0
    h = controller.new_state()
    controller.command(h, 0.0, armed=True, mode=controller.AUTO_P, p_target_mbar=2e-6)
    _, msgs = controller.step(h, 0.0, 0.25, 30.0, True, vac=1e-6, p_up=3.5, p_up_t=0.0,
                              seat_nm=0.30, remembered=(61.2, None, "remembered: x"))
    assert h['p_ref'] == pytest.approx(61.2) and h['p_ref_how'] == 'remembered'
    assert any("upstream not read" in m for m in msgs)


def test_auto_p_seeks_from_the_remembered_point_through_control(clock):
    openings.remember(dict(ENTRY))
    shared.set_seat_screw_torque(0.30)
    shared.store_keller(2.8, None, clock.t)
    control.heater_command(armed=True, mode=control.AUTO_P, p_target_mbar=2e-6)
    control.compute_duty(30.0, True, 0.25, vac=1e-6)
    h = control.snapshot()
    assert h['p_ref'] == pytest.approx(61.2) and h['p_ref_how'] == 'remembered'
    assert any("remembered: 61.2 °C at 2.80 bar" in t for _, t in shared.recent_events())


def test_shifted_for_the_upstream_pressure_now(clock):
    openings.remember(dict(ENTRY))
    shared.set_seat_screw_torque(0.30)
    shared.store_keller(3.3, None, clock.t)               # 0.5 bar above the remembered 2.8
    control.heater_command(armed=True, mode=control.AUTO_P, p_target_mbar=2e-6)
    control.compute_duty(30.0, True, 0.25, vac=1e-6)
    assert control.snapshot()['p_ref'] == pytest.approx(61.2 - config.PRESSURE_UP_K_PER_BAR * 0.5)


def test_without_a_remembered_point_auto_p_is_unchanged(clock):
    shared.set_seat_screw_torque(0.30)
    control.heater_command(armed=True, mode=control.AUTO_P, p_target_mbar=2e-6)
    control.compute_duty(30.0, True, 0.25, vac=1e-6)
    assert control.snapshot()['p_ref_how'] == 'calibrated'


# ── the batch's side ───────────────────────────────────────────────────────

def test_usable_only_at_the_same_creep_rate():
    assert batchrun.remembered(0.30) == (None, False, "none remembered for this torque")
    openings.remember(dict(ENTRY))
    entry, usable, why = batchrun.remembered(0.30)
    assert usable and entry['t_open_C'] == 61.2
    openings.remember(dict(ENTRY, creep_C_min=6.0))
    entry, usable, why = batchrun.remembered(0.30)
    assert not usable and "6.0 °C/min" in why


@pytest.fixture
def ready(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(config, "BATCH_DIR", str(tmp_path / "batches"))
    monkeypatch.setattr(config, "BATCH_WORKBOOK", str(tmp_path / "map.xlsx"))
    monkeypatch.setattr(batchrun, "PLOT", False)
    shared.set_seat_screw_torque(0.30)
    shared.store_valve_temp(25.0, 0)
    shared.store_vacuum(1e-6, None, 1.7)
    shared.store_keller(2.8, None, clock.t)
    openings.remember(dict(ENTRY))


@pytest.mark.parametrize("retorqued, first", [(False, 'testrun01'), (True, 'scout1'),
                                              (None, 'scout1')])
def test_the_answer_decides_the_first_run(ready, retorqued, first):
    ok, _ = batchrun.start(3, retorqued=retorqued, start_thread=False)
    assert ok
    batchrun.tick()
    assert batchrun.labels()[0] == first
    events = " | ".join(t for _, t in shared.recent_events())
    if retorqued is False:
        assert "from the remembered opening point 61.2 °C" in events
    elif retorqued:
        assert "valve disturbed" in events
    batchrun.abort("test")
    info = json.load(open(__import__('pathlib').Path(batchrun.folder()) / "batch.json"))
    assert info['retorqued'] == retorqued
    assert (info['started_from_remembered'] is not None) == (retorqued is False)


def test_a_different_creep_rate_scouts_even_if_not_retorqued(ready):
    openings.remember(dict(ENTRY, creep_C_min=6.0))
    ok, _ = batchrun.start(3, retorqued=False, start_thread=False)
    batchrun.tick()
    assert ok and batchrun.labels()[0] == 'scout1'
