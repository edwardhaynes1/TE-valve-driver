"""Batches of opening-point runs (context.md, "Batches"; history entry 27).

Agreed behaviour (24 Sept 2026):
  * a batch needs the seat screw torque; upstream pressure is measured only
  * scout 1: baseline wait, then heat towards the ceiling until the valve opens
  * scout 2: creep at 3 °C/min from 10 K below scout 1's T_open
    (repeated 10 K lower if it opens before it could creep)
  * test runs: creep from 5 K below scout 2's T_open; only they are averaged
  * T_open is backdated to the onset; T_detect is logged beside it
  * the heater is disarmed at detection and re-armed for each run
  * cooldown: 20 K below T_open (not below 28 °C) and the chamber back at baseline
  * no opening by the ceiling → scouts stop the batch; two failed test runs
    in a row stop it; disarm, trip, lost gauge or abort end it
  * every run → its CSV, summary.csv and the Runs sheet; the batch → the
    Batches sheet, batch.json and batch.png
"""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

import batch_sim
from batch_sim import Rig
from driver import batch, batchrun, config, control, controller, schema, shared, workbook

REPO = Path(__file__).resolve().parent.parent


def summaries(rig, b):
    return [batch.run_summary(b, r, rig.rows_of(r['name'])) for r in b['runs']]


# ── the sequence, on the simulated rig ─────────────────────────────────────

def test_a_complete_batch():
    rig = Rig(open_c=60.0)
    b, msgs = rig.run_batch(n_tests=4)
    assert b['state'] == batch.COMPLETE
    names = [r['name'] for r in b['runs']]
    assert names == ['scout1', 'scout2', 'testrun01', 'testrun02', 'testrun03', 'testrun04']
    s = summaries(rig, b)
    tests = [x for x in s if x['in_average'] == 1]
    assert len(tests) == 4 and all(x['opened_during'] == 'creep' for x in tests)
    t = [x['t_open_degC'] for x in tests]
    # The TC leads the seat by ~5 s × 0.05 °C/s: T_open reads a little high, repeatably
    assert all(60.0 <= v <= 61.5 for v in t) and max(t) - min(t) < 0.3
    # scout 1 heats fast, so it reads high; scouts are never averaged
    assert s[0]['t_open_degC'] > max(t) + 5 and s[0]['in_average'] == 0 == s[1]['in_average']
    row = batch.batch_summary(b, s)
    assert row['test_runs_opened'] == 4 and abs(row['t_open_mean_degC'] - sum(t) / 4) < 0.01
    assert row['t_open_std_K'] != ''


def test_starts_follow_the_scouts():
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=2)
    s1, s2, t1, t2 = b['runs']
    assert s2['start_c'] == pytest.approx(s1['T_onset'] - config.BATCH_SCOUT2_BELOW_K)
    assert t1['start_c'] == t2['start_c'] == pytest.approx(s2['T_onset'] - config.BATCH_TEST_BELOW_K)


def test_heater_disarmed_at_detection_and_rearmed_per_run():
    seen = []

    def hook(rig, b):
        seen.append((b['run']['name'], b['phase'], rig.h['armed'], rig.h['armed_at']))
    rig = Rig(open_c=60.0)
    rig.run_batch(n_tests=2, hook=hook)
    assert not any(armed for _, phase, armed, _ in seen if phase in ('settle', 'cooldown'))
    arm_times = {name: at for name, phase, armed, at in seen if armed}
    assert len(arm_times) == 4 and len(set(arm_times.values())) == 4   # a fresh 60 min each


def test_creep_starts_only_after_the_burst():
    starts = []

    def hook(rig, b):
        if b['phase'] == batch.CREEP and b['creep_t0'] == rig.now:
            starts.append(rig.h['t_burst'])
    rig = Rig(open_c=60.0)
    rig.run_batch(n_tests=2, hook=hook)
    assert starts and all(s is None for s in starts)


def test_no_creep_while_a_burst_or_coast_is_running():
    b = batch.new_batch(1, 0.3, 0.0)
    b['run']['start_c'] = 50.0
    b['phase'] = batch.APPROACH
    heater = dict(armed=True, trip_reason=None, t_burst='coast')
    batch.step(b, 1.0, 50.2, 5e-7, None, heater)            # at the start, still coasting
    assert b['phase'] == batch.APPROACH
    heater['t_burst'] = None
    batch.step(b, 1.25, 50.2, 5e-7, None, heater)
    assert b['phase'] == batch.CREEP


def test_labels_for_the_log():
    assert batch.labels(None) == ('', '')
    rig = Rig(open_c=60.0)
    phases = set()
    rig.run_batch(n_tests=1, hook=lambda r, b: phases.add(batch.labels(b)))
    assert ('scout1', 'settle') in phases and ('scout2', 'settle') in phases
    assert ('scout1', 'baseline') not in phases and ('testrun01', 'creep') in phases
    assert ('scout2', 'cooldown') in phases
    assert {r['batch_run'] for _, r in rig.rows} >= {'scout1', 'scout2', 'testrun01'}


def test_upstream_is_measured_per_run():
    rig = Rig(open_c=60.0, upstream_leak_bar_s=0.0005)          # the rig leaks
    b, _ = rig.run_batch(n_tests=3)
    s = summaries(rig, b)
    ups = [x['upstream_at_open_bar'] for x in s if x['in_average']]
    assert ups == sorted(ups, reverse=True) and ups[0] > ups[-1]
    row = batch.batch_summary(b, s)
    assert row['upstream_at_open_min_bar'] < row['upstream_at_open_max_bar']


def test_scout2_repeats_lower_if_it_opens_before_creeping(monkeypatch):
    monkeypatch.setattr(batch_sim, "TAU_SEAT", 20.0)            # a laggier seat: scout 1 reads ~35 K high
    rig = Rig(open_c=60.0)
    b, msgs = rig.run_batch(n_tests=1)
    names = [r['name'] for r in b['runs']]
    assert 'scout2b' in names
    first = b['runs'][1]
    assert first['opened_during'] == batch.APPROACH
    assert b['runs'][2]['start_c'] <= first['start_c'] - config.BATCH_SCOUT2_BELOW_K
    if b['state'] == batch.COMPLETE:
        assert b['runs'][-1]['opened_during'] == batch.CREEP


def test_no_opening_in_scout1_stops_the_batch():
    rig = Rig(open_c=500.0)                                     # never opens
    b, msgs = rig.run_batch(n_tests=3)
    assert b['state'] == batch.STOPPED and [r['name'] for r in b['runs']] == ['scout1']
    assert b['runs'][0]['status'] == batch.NO_OPENING and not rig.h['armed']
    assert "no opening" in b['note']


def test_two_failed_test_runs_in_a_row_stop_it():
    def hook(rig, b):
        if b['run']['name'] == 'testrun01':
            rig.open_c = 500.0                                  # stops opening
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=5, hook=hook)
    assert b['state'] == batch.STOPPED
    assert [r['status'] for r in b['runs'][2:]] == [batch.NO_OPENING] * 2
    assert 'in a row' in b['note']
    assert batch.batch_summary(b, summaries(rig, b))['test_runs_opened'] == 0


def test_operator_disarm_aborts():
    def hook(rig, b):
        if b['run']['name'] == 'testrun01' and b['phase'] == batch.CREEP:
            controller.command(rig.h, rig.now, armed=False)
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=3, hook=hook)
    assert b['state'] == batch.ABORTED and "operator" in b['note']
    assert b['runs'][-1]['name'] == 'testrun01' and b['runs'][-1]['status'] == batch.RUN_ABORTED


def test_a_trip_aborts_with_its_reason():
    def hook(rig, b):
        if b['run']['name'] == 'scout2' and b['phase'] == batch.CREEP:
            controller.trip(rig.h, "over-temperature (test)", [])
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=2, hook=hook)
    assert b['state'] == batch.ABORTED and "over-temperature" in b['note']


def test_a_lost_gauge_aborts_while_heating():
    def hook(rig, b):
        if b['run']['name'] == 'scout2' and b['phase'] == batch.CREEP:
            rig.vac_override = (None, config.VAC_ERROR)
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=2, hook=hook)
    assert b['state'] == batch.ABORTED and "chamber pressure" in b['note']
    assert not rig.h['armed']


def test_a_cooldown_that_never_ends_stops_the_batch():
    def hook(rig, b):
        if b['phase'] == batch.COOLDOWN:
            rig.vac_override = (2e-6, None)                     # chamber never recovers
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=2, hook=hook)
    assert b['state'] == batch.STOPPED and "cooldown" in b['note']
    assert b['ended'] - b['runs'][0]['t_detect'] >= config.BATCH_COOL_MAX_S


def test_abort_function():
    b = batch.new_batch(2, 0.3, 0.0)
    cmds, msgs, events = batch.abort(b, 1.0, "aborted by the operator")
    assert cmds == [dict(armed=False)] and b['state'] == batch.ABORTED
    assert [k for k, _ in events] == ['run_end', 'batch_end']
    assert batch.abort(b, 2.0, "again") == ([], [], [])


def test_needs_a_torque():
    with pytest.raises(ValueError):
        batch.new_batch(3, None, 0.0)


def test_recovery_is_tighter_than_detection():
    # Otherwise the next run would start "already open" (found in simulation).
    assert config.BATCH_RECOVER_DEC < config.PRESSURE_OPEN_DEC


# ── onset, e-fold, energy ──────────────────────────────────────────────────

def test_onset_is_where_the_rise_began():
    base = -6.3
    samples = [(i * 0.25, 50 + i * 0.0125, base) for i in range(40)]
    samples += [(10 + i * 0.25, 50.5 + i * 0.0125, base + 0.004 * (i + 1) ** 1.5)
                for i in range(40)]
    i_on = batch.find_onset(samples, base, len(samples) - 1)
    assert 39 <= i_on <= 43                     # the last sample still at the baseline


def test_flow_efold_of_the_rise_above_baseline():
    import math
    base = math.log10(5e-7)
    samples = [(i, 60 + 0.1 * i, math.log10(5e-7 * (1 + 0.05 * math.exp(0.1 * i / 3.0))))
               for i in range(60)]
    assert batch.flow_efold(samples, base, 0, 59) == pytest.approx(3.0, rel=0.01)
    assert batch.flow_efold(samples[:3], base, 0, 2) is None


def test_energy_counts_from_the_approach():
    b = batch.new_batch(1, 0.3, 0.0)
    run = b['run']
    run.update(status=batch.OPENED, T_onset=60.0, T_detect=62.0, t_onset=12.0,
               t_detect=20.0, y_onset=-6.3, y_detect=-6.2, base=-6.3)
    rows = [(t, dict(batch_phase='baseline' if t < 5 else 'creep', heater_on_s=0.5,
                     heater_P_mean_calc=1.0, keller_pressure_bar=3.0))
            for t in [i * 0.5 for i in range(50)]]
    s = batch.run_summary(b, run, rows)
    full = config.heater_power_w()
    assert s['energy_at_open_J'] == pytest.approx(round(15 * 0.5 * full, 1))   # rows 5.0 … 12.0
    assert s['energy_at_detect_J'] == pytest.approx(round(31 * 0.5 * full, 1))
    assert s['upstream_at_open_bar'] == 3.0


# ── the workbook ───────────────────────────────────────────────────────────

def test_workbook_rows_and_locked_file(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    path = str(tmp_path / "map.xlsx")
    ok, _ = workbook.append(path, 'Runs', ('a', 'b'), {'a': 1, 'b': 'x'})
    assert ok
    real_save = openpyxl.Workbook.save

    def locked(self, *a, **k):
        raise PermissionError("[Errno 13] Permission denied")
    monkeypatch.setattr(openpyxl.Workbook, "save", locked)
    ok, msg = workbook.append(path, 'Runs', ('a', 'b'), {'a': 2, 'b': 'y'})
    assert not ok and "Excel" in msg and Path(workbook.pending_path(path, 'Runs')).exists()
    monkeypatch.setattr(openpyxl.Workbook, "save", real_save)
    ok, msg = workbook.append(path, 'Runs', ('a', 'b'), {'a': 3, 'b': 'z'})
    assert ok and "moved in" in msg and not Path(workbook.pending_path(path, 'Runs')).exists()
    ws = openpyxl.load_workbook(path)['Runs']
    assert [[c.value for c in r] for r in ws.iter_rows()] == [['a', 'b'], [1, 'x'], [2, 'y'], [3, 'z']]


# ── through batchrun: files, workbook, plot ────────────────────────────────

def drive(clock, rig, max_s=3 * 3600):
    """Run the current batch through batchrun / control.py on the rig."""
    dt, next_row, on = 0.25, clock.t, 0.0
    rig.now = clock.t
    while batchrun.running() and clock.t - rig.now < max_s:
        vac, status = rig.vac()
        shared.store_valve_temp(rig.T, 0)
        shared.store_vacuum(vac, status, None)
        shared.store_keller(rig.upstream, None, clock.t)
        batchrun.tick()
        rig.duty = control.compute_duty(rig.T, True, dt, vac=vac, vac_status=status)
        on += rig.duty * dt
        rig.advance(dt)
        rig.now -= dt                      # rig.now is only the start here
        clock.advance(dt)
        if clock.t >= next_row:
            name, phase = batchrun.labels()
            row = {c: '' for c in schema.MAIN}
            row.update(timestamp=__import__('datetime').datetime.fromtimestamp(clock.t)
                       .isoformat(timespec='milliseconds'),
                       te_temperature_degC=round(rig.T, 3), vacuum_chamber_mbar=vac,
                       keller_pressure_bar=rig.upstream, heater_on_s=round(on, 3),
                       heater_P_mean_calc=config.heater_power_w(rig.duty),
                       batch_run=name, batch_phase=phase)
            batchrun.record_row(row)
            on, next_row = 0.0, next_row + config.LOG_INTERVAL_S


@pytest.fixture
def batch_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BATCH_DIR", str(tmp_path / "batches"))
    monkeypatch.setattr(config, "BATCH_WORKBOOK", str(tmp_path / "map.xlsx"))
    monkeypatch.setattr(batchrun, "PLOT", False)
    return tmp_path


def sensors():
    shared.store_valve_temp(25.0, 0)
    shared.store_vacuum(1e-6, None, 1.7)


def test_start_is_refused_without_a_torque_or_while_armed(batch_dirs):
    sensors()
    ok, msg = batchrun.start(3, start_thread=False)
    assert not ok and "torque" in msg
    shared.set_seat_screw_torque(0.3)
    control.heater_command(armed=True)
    ok, msg = batchrun.start(3, start_thread=False)
    assert not ok and "disarm" in msg


def test_files_workbook_and_plot(batch_dirs, clock):
    openpyxl = pytest.importorskip("openpyxl")
    shared.set_seat_screw_torque(0.3)
    shared.store_keller(3.1, None, clock.t)
    sensors()
    ok, name = batchrun.start(2, main_log="te-sensor_x.csv", start_thread=False)
    assert ok and name.endswith("_0.30Nm_3.10bar")
    drive(clock, Rig(open_c=60.0))
    folder = Path(batchrun.folder())
    assert {p.name for p in folder.iterdir()} >= {
        "scout1.csv", "scout2.csv", "testrun01.csv", "testrun02.csv", "summary.csv", "batch.json"}
    rows = list(csv.DictReader(open(folder / "testrun01.csv", encoding="utf-8")))
    assert rows and tuple(rows[0]) == schema.MAIN
    assert {r['batch_phase'] for r in rows} == {'settle', 'approach', 'creep', 'cooldown'}
    summary = list(csv.DictReader(open(folder / "summary.csv", encoding="utf-8")))
    assert [r['run'] for r in summary] == ['scout1', 'scout2', 'testrun01', 'testrun02']
    assert all(float(r['energy_at_open_J']) > 0 for r in summary[2:])
    info = json.load(open(folder / "batch.json", encoding="utf-8"))
    assert info['seat_screw_torque_Nm'] == 0.3 and info['results']['status'] == 'complete'
    assert info['settings']['BATCH_CREEP_C_MIN'] == config.BATCH_CREEP_C_MIN
    wb = openpyxl.load_workbook(config.BATCH_WORKBOOK)
    assert wb['Runs'].max_row == 5 and wb['Batches'].max_row == 2
    assert [c.value for c in wb['Batches'][1]] == list(schema.BATCH_SUMMARY)
    assert not control.snapshot()['armed']
    assert batchrun.labels() == ('', '')

    # the plotter's batch view
    pytest.importorskip("matplotlib")
    import matplotlib
    matplotlib.use("Agg")
    spec = importlib.util.spec_from_file_location("te_plotter", REPO / "TE_PLOTTER.py")
    plotter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plotter)
    s, traces = plotter.load_batch(folder)
    x, y = plotter.batch_average_vs_temperature(s, traces)
    assert len(x) > 5
    out = plotter.plot_batch(folder, str(folder / "batch.png"), show=False)
    assert Path(out).stat().st_size > 10_000


def test_abort_keeps_what_was_done(batch_dirs, clock):
    shared.set_seat_screw_torque(0.3)
    sensors()
    ok, _ = batchrun.start(3, start_thread=False)
    rig = Rig(open_c=60.0)
    for _ in range(2000):                   # into scout 1's heating
        vac, status = rig.vac()
        shared.store_valve_temp(rig.T, 0)
        shared.store_vacuum(vac, status, None)
        batchrun.tick()
        rig.duty = control.compute_duty(rig.T, True, 0.25, vac=vac, vac_status=status)
        rig.advance(0.25)
        clock.advance(0.25)
        if batchrun.labels()[1] == 'approach' and rig.T > 40:
            break
    batchrun.abort("aborted by the operator")
    assert not batchrun.running() and not control.snapshot()['armed']
    folder = Path(batchrun.folder())
    summary = list(csv.DictReader(open(folder / "summary.csv", encoding="utf-8")))
    assert summary[0]['status'] == 'aborted'
    assert json.load(open(folder / "batch.json"))['results']['status'] == 'aborted'


def test_the_logger_labels_rows_and_copies_them(batch_dirs, monkeypatch):
    import threading
    import time
    from driver import logfile
    monkeypatch.setattr(control, "clock", time.time)
    monkeypatch.setattr(logfile, "LOG_FILE", str(batch_dirs / "t.csv"))
    monkeypatch.setattr(logfile, "PWM_LOG_FILE", str(batch_dirs / "t_pwm.csv"))
    monkeypatch.setattr(logfile, "LOG_INTERVAL_S", 0.1)
    shared.set_seat_screw_torque(0.3)
    sensors()
    assert batchrun.start(2, start_thread=False)[0]
    batchrun.tick()                          # scout1 begins: its file opens
    th = threading.Thread(target=logfile.logger_thread, daemon=True)
    th.start()
    time.sleep(0.45)
    shared.stop.set()
    th.join(timeout=3)
    main = list(csv.DictReader(open(batch_dirs / "t.csv", encoding="utf-8")))
    assert main[1]['batch_run'] == 'scout1' and main[1]['batch_phase'] == 'settle'
    run = list(csv.DictReader(open(Path(batchrun.folder()) / "scout1.csv", encoding="utf-8")))
    assert len(run) >= 3 and run[0]['batch_run'] == 'scout1'


# ── the real rig: settling, top-ups, the leak, the ceiling ─────────────────

def test_waits_for_a_rising_chamber_before_heating():
    # The chamber rises for the first 3 min (say, after filling upstream).
    def hook(rig, b):
        rig.base = 5e-7 * (1 + min(rig.now - rig.t_start, 180) / 180)
    rig = Rig(open_c=60.0)
    rig.t_start = rig.now
    armed = []
    b, _ = rig.run_batch(n_tests=1, hook=lambda r, b: (hook(r, b), armed.append((r.now, r.h['armed'])))[0])
    first = next(t for t, a in armed if a) - rig.t_start
    assert first >= 180                                # not while it was rising
    assert b['state'] == batch.COMPLETE and b['runs'][0]['opened_during'] is not None


def test_a_falling_chamber_does_not_hold_it_up():
    # A pump-down tail 50 % above base, τ 10 min (~7 %/min: far steeper than the
    # 1.5 %/min on 24 Sept). It completes; T_open is never read early; it reads
    # a little high while the background hides the first flow, converging as
    # the tail falls (the baseline is logged per run, so this shows in the data).
    rig = Rig(open_c=60.0, pump_tail=0.5)
    b, _ = rig.run_batch(n_tests=3)
    assert b['state'] == batch.COMPLETE
    t = [r['T_onset'] for r in b['runs'] if r['counts']]
    assert all(60.0 <= v <= 62.0 for v in t) and t[-1] <= t[0]


def test_a_chamber_that_never_settles_stops_it():
    def hook(rig, b):
        rig.base *= 1.0005                               # rising ~7 %/min, forever
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=1, hook=hook)
    assert b['state'] == batch.STOPPED and "not settled" in b['note']
    assert not rig.h['armed']


def test_top_up_pause_and_the_chamber_jump_after_filling():
    # Leaks 0.07 bar/min; the operator tops up when asked; filling makes the
    # chamber jump 70 % (as on 24 Sept) — the batch waits it out.
    rig = Rig(open_c=60.0, upstream_leak_bar_s=0.07 / 60, fill_jump=0.7)
    phases = []
    b, msgs = rig.run_batch(n_tests=4, topup_drop=0.3,
                            hook=lambda r, b: phases.append((b['phase'], r.h['armed'])))
    assert b['state'] == batch.COMPLETE and b['top_ups'] >= 1 and rig.fills == b['top_ups']
    assert not any(armed for phase, armed in phases if phase in (batch.TOPUP, batch.SETTLE))
    # no false openings from the jump: every test run opened while creeping, near 60 °C
    tests = [r for r in b['runs'] if r['counts']]
    assert all(r['opened_during'] == batch.CREEP and 60.0 <= r['T_onset'] <= 61.5 for r in tests)
    assert any("PAUSED for a top-up" in m for m in msgs)


def test_no_top_up_pause_unless_asked():
    rig = Rig(open_c=60.0, upstream_leak_bar_s=0.07 / 60)
    b, _ = rig.run_batch(n_tests=3)
    assert b['state'] == batch.COMPLETE and b['top_ups'] == 0


def test_the_leak_becomes_a_fit_against_upstream():
    # The opening point moves −12 K/bar; the leak spreads the runs over upstream.
    rig = Rig(open_c=60.0, upstream_leak_bar_s=0.07 / 60, k_up=-12.0)
    b, _ = rig.run_batch(n_tests=6)
    s = summaries(rig, b)
    row = batch.batch_summary(b, s)
    assert row['t_open_vs_upstream_K_per_bar'] == pytest.approx(-12.0, abs=1.5)
    assert row['t_open_resid_std_K'] < 0.5 < row['t_open_std_K']   # the leak explains the scatter


def test_upstream_fit_needs_three_runs_and_some_spread():
    assert batch.upstream_fit([(3.0, 60.0), (2.9, 61.0)]) is None
    assert batch.upstream_fit([(3.0, 60.0), (3.01, 61.0), (3.02, 60.5)]) is None
    slope, se, resid = batch.upstream_fit([(3.0, 60.0), (2.8, 62.4), (2.6, 64.8), (2.4, 67.2)])
    assert slope == pytest.approx(-12.0) and resid == pytest.approx(0.0, abs=1e-9)


def test_ceiling_is_5_K_below_the_trip():
    assert config.BATCH_CEILING_C == config.TEMP_TRIP_C - 5.0


def test_opening_at_150_is_reached():
    # 0.45 N·m at low upstream (21 Sept): ~150 °C. Detection needs ~12 % of
    # flow, a few K above the opening point, so the old 150 °C ceiling failed.
    rig = Rig(open_c=150.0)
    b, _ = rig.run_batch(n_tests=1)
    assert b['state'] == batch.COMPLETE
    assert max(r['T_onset'] for r in b['runs']) < config.TEMP_TRIP_C


@pytest.mark.parametrize("missing", ["tc", "gauge"])
def test_start_is_refused_without_the_sensors(batch_dirs, missing):
    shared.set_seat_screw_torque(0.3)
    if missing != "tc":
        shared.store_valve_temp(25.0, 0)
    if missing != "gauge":
        shared.store_vacuum(1e-6, None, 1.7)
    ok, msg = batchrun.start(3, start_thread=False)
    assert not ok and ("thermocouple" in msg if missing == "tc" else "chamber" in msg)


def test_top_up_needs_the_keller(batch_dirs):
    shared.set_seat_screw_torque(0.3)
    shared.store_valve_temp(25.0, 0)
    shared.store_vacuum(1e-6, None, 1.7)
    ok, msg = batchrun.start(3, topup_drop_bar=0.3, start_thread=False)
    assert not ok and "Keller" in msg
    ok, _ = batchrun.start(3, start_thread=False)          # without top-up: fine
    assert ok


# ── no waiting when there is nothing to wait for ───────────────────────────

def first_arm(rig, **kw):
    arms = []
    b, msgs = rig.run_batch(hook=lambda r, b: arms.append((r.now, r.h['armed'])), **kw)
    return b, next(t for t, a in arms if a), msgs


def test_a_settled_chamber_starts_at_once():
    rig = Rig(open_c=60.0)
    history = rig.idle(120)                     # the driver's readings before start
    t0 = rig.now
    b, armed_at, _ = first_arm(rig, n_tests=1, history=history)
    assert armed_at - t0 < 1.0 and b['state'] == batch.COMPLETE


def test_without_history_it_collects_a_minute_first():
    rig = Rig(open_c=60.0)
    t0 = rig.now
    _, armed_at, _ = first_arm(rig, n_tests=1)
    assert 0.8 * config.BATCH_SETTLE_WINDOW_S <= armed_at - t0 < config.BATCH_SETTLE_WINDOW_S + 5


def test_a_rising_history_still_waits():
    rig = Rig(open_c=60.0, fill_jump=0.7)
    rig.fill()                                  # just filled: the chamber jumps
    history = rig.idle(5)
    t0 = rig.now
    _, armed_at, _ = first_arm(rig, n_tests=1, history=history)
    assert armed_at - t0 > 30


def test_continue_long_after_the_fill_needs_no_wait():
    # topped up 20 s into the pause, continue pressed 5 min later: settled by then
    rig = Rig(open_c=60.0, upstream_leak_bar_s=0.07 / 60, fill_jump=0.7)
    events = []

    def hook(r, b):
        events.append((r.now, b['phase'], r.h['armed']))
    b, msgs = rig.run_batch(n_tests=3, topup_drop=0.3, continue_after_s=300, hook=hook)
    assert b['top_ups'] >= 1 and b['state'] == batch.COMPLETE
    resumed = next(i for i, (t, ph, a) in enumerate(events)
                   if ph == batch.SETTLE and events[i - 1][1] == batch.TOPUP)
    armed = next(t for t, ph, a in events[resumed:] if a)
    assert armed - events[resumed][0] < 1.0


def test_continue_right_after_the_fill_waits_for_the_jump():
    rig = Rig(open_c=60.0, upstream_leak_bar_s=0.07 / 60, fill_jump=0.7)
    events = []
    b, _ = rig.run_batch(n_tests=3, topup_drop=0.3,
                         hook=lambda r, b: events.append((r.now, b['phase'], r.h['armed'])))
    resumed = next(i for i, (t, ph, a) in enumerate(events)
                   if ph == batch.SETTLE and events[i - 1][1] == batch.TOPUP)
    armed = next(t for t, ph, a in events[resumed:] if a)
    assert armed - events[resumed][0] >= 0.8 * config.BATCH_SETTLE_WINDOW_S


def test_the_driver_keeps_timed_chamber_readings():
    shared.store_vacuum(1e-6, None, 1.7, 100.0)
    shared.store_vacuum(None, config.VAC_ERROR, 0.0, 100.25)     # invalid: not kept
    shared.store_vacuum(1.1e-6, None, 1.7)                       # no time: not kept
    assert shared.vacuum_history() == [(100.0, 1e-6)]
    shared.reset()
    assert shared.vacuum_history() == []
