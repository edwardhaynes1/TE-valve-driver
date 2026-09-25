"""Batches of opening-point runs (context.md, "Batches"; history 27-30).

Agreed behaviour (24 Sept 2026):
  * a batch needs the seat screw torque; upstream pressure is measured only
  * every run: cool (heater off) to the hold temperature — 35 °C, or 20 K
    below the opening point if lower, not below 28 °C — hold it 4 min with
    auto-t, approach once the chamber is settled, creep at 3 °C/min
  * scout 1 heats towards the ceiling; scout 2 creeps from 10 K below scout
    1's T_open (repeated 10 K lower if it opens before it could creep)
  * a remembered opening point (operator: not re-torqued) replaces the scouts
  * test runs start a margin below the reference: 3 × scatter + 0.5 K,
    2-5 K (5 K while unknown), +2 K if upstream moved > 1 bar
  * a test run that opens while approaching isn't averaged; a scout 2
    follows (find again)
  * the batch stops once the mean T_open is within ±1 K (95 %) with ≥ 3
    test runs; N is the most
  * detection: +1e-7 mbar or +12 %, whichever first, at least 0.02 decades
  * T_open is backdated to the onset; T_detect is logged beside it
  * the heater is disarmed at detection and re-armed for each run
  * no opening by the ceiling → scouts stop the batch; two failed test runs
    in a row stop it; disarm, trip, lost gauge or abort end it; so does the
    valve opening while holding
  * every run → its CSV, summary.csv and the Runs sheet; the batch → the
    Batches sheet, batch.json, batch.png and (maybe) the remembered point
"""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

import batch_sim
from batch_sim import Rig
from driver import (batch, batchrun, config, control, controller, openings, schema, shared,
                    workbook)

REPO = Path(__file__).resolve().parent.parent


def summaries(rig, b):
    return [batch.run_summary(b, r, rig.rows_of(r['name'])) for r in b['runs']]


# ── the sequence, on the simulated rig ─────────────────────────────────────

def test_a_complete_batch():
    # A repeatable valve: precise enough after the minimum 3 test runs, not N.
    rig = Rig(open_c=60.0)
    b, msgs = rig.run_batch(n_tests=8)
    assert b['state'] == batch.COMPLETE and "precise enough" in b['note']
    names = [r['name'] for r in b['runs']]
    assert names == ['scout1', 'scout2', 'testrun01', 'testrun02', 'testrun03']
    s = summaries(rig, b)
    tests = [x for x in s if x['in_average'] == 1]
    assert len(tests) == 3 and all(x['opened_during'] == 'creep' for x in tests)
    t = [x['t_open_degC'] for x in tests]
    # The TC leads the seat by ~5 s × 0.05 °C/s: T_open reads a little high, repeatably
    assert all(60.0 <= v <= 61.5 for v in t) and max(t) - min(t) < 0.3
    # scout 1 heats fast, so it reads high; scouts are never averaged
    assert s[0]['t_open_degC'] > max(t) + 5 and s[0]['in_average'] == 0 == s[1]['in_average']
    row = batch.batch_summary(b, s)
    assert row['test_runs_opened'] == 3 and abs(row['t_open_mean_degC'] - sum(t) / 3) < 0.01
    assert row['t_open_std_K'] != '' and row['t_open_ci95_K'] <= config.BATCH_PRECISION_K
    assert row['started_from'] == 'scouts'


def test_starts_follow_the_scouts_then_the_margin_narrows():
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=3)
    s1, s2, t1, t2, t3 = b['runs']
    assert s2['start_c'] == pytest.approx(s1['T_onset'] - config.BATCH_SCOUT2_BELOW_K)
    # no scatter known yet: the widest margin, from scout 2, then from test run 1
    assert t1['margin'] == t2['margin'] == config.BATCH_MARGIN_MAX_K
    assert t1['ref_c'] == pytest.approx(s2['T_onset'])
    assert t2['ref_c'] == pytest.approx(t1['T_onset'])
    # two test runs give a scatter: 3 × it + 0.5 K, at least 2 K
    sd = __import__('statistics').stdev([t1['T_onset'], t2['T_onset']])
    assert t3['margin'] == pytest.approx(batch.margin(sd)) == pytest.approx(config.BATCH_MARGIN_MIN_K)
    for r in (t1, t2, t3):
        assert r['start_c'] == pytest.approx(r['ref_c'] - r['margin'])


def test_heater_disarmed_at_detection_and_rearmed_per_run():
    seen = []

    def hook(rig, b):
        if b['state'] == batch.RUNNING:
            seen.append((b['run']['name'], b['phase'], rig.h['armed'], rig.h['armed_at']))
    rig = Rig(open_c=60.0)
    rig.run_batch(n_tests=2, hook=hook)
    # heater off while cooling; on (auto-t) from the hold to detection
    assert not any(armed for _, phase, armed, _ in seen if phase == 'cooldown')
    assert all(armed for _, phase, armed, _ in seen if phase == 'creep')
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
    assert ('scout1', 'cooldown') in phases and ('scout1', 'hold') in phases
    assert ('scout2', 'hold') in phases and ('testrun01', 'creep') in phases
    assert ('scout2', 'cooldown') in phases
    assert not any(ph in ('settle', 'baseline') for _, ph in phases)
    assert {r['batch_run'] for _, r in rig.rows} >= {'scout1', 'scout2', 'testrun01'}


def test_upstream_is_measured_per_run():
    rig = Rig(open_c=60.0, upstream_leak_bar_s=0.0005)          # the rig leaks
    b, _ = rig.run_batch(n_tests=3)
    s = summaries(rig, b)
    ups = [x['upstream_at_open_bar'] for x in s if x['in_average']]
    assert ups == sorted(ups, reverse=True) and ups[0] > ups[-1]
    row = batch.batch_summary(b, s)
    assert row['upstream_at_open_min_bar'] < row['upstream_at_open_max_bar']


def test_scout2_repeats_lower_if_it_opens_before_creeping():
    def hook(rig, b):
        if b['run']['name'] == 'scout2' and b['phase'] == batch.HOLD:
            rig.open_c = 45.0               # well below scout 2's start (scout 1 − 10 K)
    rig = Rig(open_c=60.0)
    b, msgs = rig.run_batch(n_tests=1, hook=hook)
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
    assert 0 < config.BATCH_RECOVER_FRACTION < 1


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
    ok, name = batchrun.start(3, main_log="te-sensor_x.csv", start_thread=False)
    assert ok and name.endswith("_0.30Nm_3.10bar")
    drive(clock, Rig(open_c=60.0))
    folder = Path(batchrun.folder())
    assert {p.name for p in folder.iterdir()} >= {
        "scout1.csv", "scout2.csv", "testrun01.csv", "testrun02.csv", "testrun03.csv",
        "summary.csv", "batch.json"}
    rows = list(csv.DictReader(open(folder / "testrun01.csv", encoding="utf-8")))
    assert rows and tuple(rows[0]) == schema.MAIN
    assert {r['batch_phase'] for r in rows} == {'hold', 'approach', 'creep', 'cooldown'}
    summary = list(csv.DictReader(open(folder / "summary.csv", encoding="utf-8")))
    assert [r['run'] for r in summary] == ['scout1', 'scout2', 'testrun01', 'testrun02',
                                           'testrun03']
    assert all(float(r['energy_at_open_J']) > 0 for r in summary[2:])
    # test runs hold the target; scout 1 found the valve colder and held it there
    assert all(float(r['hold_degC']) == config.BATCH_COOL_TO_C for r in summary[2:])
    assert float(summary[0]['hold_degC']) < config.BATCH_COOL_TO_C
    assert [r['cooldown_end'] for r in summary[2:]] == ['target', 'fixed', 'fixed']
    assert all(float(r['hold_gap_K']) > 20 for r in summary[2:])
    assert all(float(r['hold_s']) >= config.BATCH_HOLD_S for r in summary)
    info = json.load(open(folder / "batch.json", encoding="utf-8"))
    assert info['seat_screw_torque_Nm'] == 0.3 and info['results']['status'] == 'complete'
    assert info['settings']['BATCH_CREEP_C_MIN'] == config.BATCH_CREEP_C_MIN
    assert info['settings']['BATCH_HOLD_S'] == config.BATCH_HOLD_S
    # the result is now the remembered opening point
    assert info['results']['remembered_after'] == 'yes' and info['remembered']['n'] == 3
    saved = json.load(open(config.OPENINGS_FILE, encoding="utf-8"))['points']['0.30']
    assert saved['t_open_C'] == pytest.approx(info['results']['t_open_mean_degC'], abs=0.01)
    assert saved['upstream_bar'] == pytest.approx(3.0, abs=0.01)
    wb = openpyxl.load_workbook(config.BATCH_WORKBOOK)
    assert wb['Runs'].max_row == 6 and wb['Batches'].max_row == 2
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
    assert len(x) > 5 and list(x) == sorted(x)          # drawn left to right
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
    assert main[1]['batch_run'] == 'scout1' and main[1]['batch_phase'] in ('cooldown', 'hold')
    run = list(csv.DictReader(open(Path(batchrun.folder()) / "scout1.csv", encoding="utf-8")))
    assert len(run) >= 3 and run[0]['batch_run'] == 'scout1'


# ── the real rig: settling, top-ups, the leak, the ceiling ─────────────────

def approach_times(rig, **kw):
    """Run a batch; [(when the hold time was reached, when the approach
    began)] for every run."""
    seen, out = [], []

    def hook(r, b):
        seen.append((r.now, b['run']['name'], b['phase'], b['hold_since']))
    b, msgs = rig.run_batch(hook=lambda r, b_: hook(r, b_), **kw)
    for i in range(1, len(seen)):
        t, name, ph, since = seen[i]
        if ph == batch.APPROACH and seen[i - 1][2] == batch.HOLD:
            out.append((seen[i - 1][3] + config.BATCH_HOLD_S, t))
    return b, out, msgs


def rising(rig, t0, over_s):
    """The chamber doubles over over_s from t0 (outgassing, say), then stays."""
    rig.base = 5e-7 * (1 + min(max(rig.now - t0, 0.0), over_s) / over_s)


def test_waits_for_a_rising_chamber_before_heating():
    # Rising (0.03 dec/min) from 2 min before the start until 10 min: longer
    # than the hold. It was rising before the heater came on, so it's the
    # chamber's own rise: the batch waits it out.
    rig = Rig(open_c=60.0)
    t0 = rig.now
    history = []
    for _ in range(480):
        rising(rig, t0, 600)
        rig.advance(0.25)
        history.append((rig.now, rig.vac()[0]))
    approach = []
    b, _ = rig.run_batch(n_tests=1, history=history, hook=lambda r, b: (
        rising(r, t0, 600), b['phase'] == batch.APPROACH and approach.append(r.now)))
    assert approach[0] - t0 >= 600                     # not while it was rising
    assert b['state'] == batch.COMPLETE and b['runs'][0]['opened_during'] is not None


def test_a_rise_that_starts_with_the_hold_looks_like_the_valve():
    # Flat before; rising from the moment the heater warms the valve, then
    # steady higher: indistinguishable from the valve opening below the hold
    # temperature — the batch stops and says so rather than measure from it.
    rig = Rig(open_c=60.0)
    history = rig.idle(120)
    t0 = rig.now
    b, _ = rig.run_batch(n_tests=1, history=history, hook=lambda r, b: rising(r, t0, 120))
    assert b['state'] == batch.STOPPED and "hold temperature" in b['note']


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
    assert not any(armed for phase, armed in phases if phase in (batch.TOPUP, batch.COOLDOWN))
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


# ── no waiting beyond the hold when there is nothing to wait for ──────────

def test_a_settled_chamber_approaches_as_soon_as_the_hold_is_done():
    for history in (True, False):
        rig = Rig(open_c=60.0)
        hist = rig.idle(120) if history else ()
        b, times, _ = approach_times(rig, n_tests=3, history=hist)
        assert b['state'] == batch.COMPLETE and len(times) == 5
        assert all(0 <= began - done < 1.0 for done, began in times)


def test_continue_long_after_the_fill_needs_no_extra_wait():
    # topped up 20 s into the pause, continue pressed 5 min later: settled by then
    rig = Rig(open_c=60.0, upstream_leak_bar_s=0.07 / 60, fill_jump=0.7)
    b, times, _ = approach_times(rig, n_tests=4, topup_drop=0.3, continue_after_s=300)
    assert b['top_ups'] >= 1 and b['state'] == batch.COMPLETE
    assert all(0 <= began - done < 1.0 for done, began in times)


def test_a_fill_that_outlasts_the_hold_delays_the_approach(monkeypatch):
    # A slow fill tail (τ 400 s, falling faster than the settle limit when
    # the hold ends): the approach waits for it; the jump isn't an opening.
    monkeypatch.setattr(batch_sim, "TAU_FILL", 400.0)
    rig = Rig(open_c=60.0, upstream_leak_bar_s=0.07 / 60, fill_jump=3.0)
    b, times, msgs = approach_times(rig, n_tests=4, topup_drop=0.3)
    assert b['top_ups'] >= 1 and b['state'] == batch.COMPLETE, b['note']
    assert max(began - done for done, began in times) > 30
    tests = [r for r in b['runs'] if r['counts']]
    assert all(r['opened_during'] == batch.CREEP and 60.0 <= r['T_onset'] <= 61.5 for r in tests)


def test_the_driver_keeps_timed_chamber_readings():
    shared.store_vacuum(1e-6, None, 1.7, 100.0)
    shared.store_vacuum(None, config.VAC_ERROR, 0.0, 100.25)     # invalid: not kept
    shared.store_vacuum(1.1e-6, None, 1.7)                       # no time: not kept
    assert shared.vacuum_history() == [(100.0, 1e-6)]
    shared.reset()
    assert shared.vacuum_history() == []


# ── the hold ───────────────────────────────────────────────────────────────

def test_hold_target_rule():
    b = batch.new_batch(3, 0.3, 0.0)
    assert batch.hold_target(b) == config.BATCH_COOL_TO_C             # nothing known
    for t_open, hold in ((146.0, 35.0), (60.0, 35.0), (50.0, 30.0), (40.0, 20.0)):
        k = dict(t_open_C=t_open, upstream_bar=3.0, scatter_K=0.3, creep_C_min=3.0)
        assert batch.hold_target(batch.new_batch(3, 0.3, 0.0, known=k)) == hold   # no floor


def test_every_run_holds_first():
    seen = []
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=3, hook=lambda r, b: seen.append((b['run']['name'], b['phase'],
                                                                  r.T, r.h['setpoint_C'])))
    for r in b['runs']:
        assert r['held_s'] >= config.BATCH_HOLD_S
        # the test runs hold the target; scout 1 found the valve colder and held it there
        assert r['hold_c'] == config.BATCH_COOL_TO_C if r['counts'] else r['hold_c'] <= 35.0
        # it approached from the hold temperature: the TC was within the band
        at_start = [T for name, ph, T, _ in seen if name == r['name'] and ph == batch.HOLD]
        assert abs(at_start[-1] - r['hold_c']) <= config.BATCH_HOLD_BAND_K


def test_a_cold_valve_isnt_warmed_before_scout_1():
    # Nothing known yet: the valve at 23 °C is held at 23 °C, not warmed to 35 —
    # so a valve opening at 30 °C (lab 23 °C, a 7 K gap) can still be measured.
    rig = Rig(open_c=30.0)
    b, _ = rig.run_batch(n_tests=3, history=rig.idle(120))
    assert b['runs'][0]['hold_c'] == 23.0
    assert b['state'] == batch.COMPLETE, b['note']
    assert all(r['T_onset'] - r['hold_c'] >= config.BATCH_MIN_GAP_K
               for r in b['runs'] if r['averaged'])


def test_the_valve_opening_while_holding_stops_the_batch():
    # Remembered from a hold at 35 °C, but the valve has moved: it now opens
    # at 31 °C, so warming to the remembered hold temperature opens it.
    known = dict(t_open_C=50.0, upstream_bar=3.0, scatter_K=0.3, creep_C_min=3.0,
                 settings=dict(hold_C=35.0))
    rig = Rig(open_c=31.0)
    b, _ = rig.run_batch(n_tests=3, history=rig.idle(120), known=known, retorqued=False)
    assert b['state'] == batch.STOPPED and "hold temperature" in b['note']
    assert [r['name'] for r in b['runs']] == ['testrun01'] and not rig.h['armed']


# ── adaptive cooling (0.25 N·m: the valve opens near room temperature) ────

def low(**kw):
    """A 0.25 N·m-like valve: opens near 40 °C, two-stage cooling (the TC
    falls fast to the body, the body cools slowly to the lab)."""
    return Rig(**{**dict(open_c=40.0, body_tau=1500.0, open_scatter=0.3), **kw})


def test_low_opening_point_completes_with_an_adaptive_hold():
    rig = low()
    b, _ = rig.run_batch(n_tests=8, history=rig.idle(120))
    assert b['state'] == batch.COMPLETE, b['note']
    tests = [r for r in b['runs'] if r['averaged']]
    # the first test run's cooldown ended by slowing down, and fixed the hold
    assert tests[0]['cool_end'] == 'slowed' and all(r['cool_end'] == 'fixed' for r in tests[1:])
    assert {r['hold_c'] for r in tests} == {b['hold_fixed']}
    assert b['hold_fixed'] > config.HEATER_HOLD_AMBIENT_C          # not all the way to the lab
    assert b['hold_fixed'] * 2 == int(b['hold_fixed'] * 2)         # rounded to 0.5 K
    assert all(r['T_onset'] - r['hold_c'] >= config.BATCH_MIN_GAP_K for r in tests)
    row = batch.batch_summary(b, [])
    assert row['hold_gap_min_K'] >= config.BATCH_MIN_GAP_K


def test_the_next_batch_starts_from_the_same_hold_temperature():
    rig = low()
    b, _ = rig.run_batch(n_tests=8, history=rig.idle(120))
    e = batch.remembered_entry(b, "first")
    assert e['settings']['hold_C'] == b['hold_fixed']
    rig = low(seed=7)
    b2, _ = rig.run_batch(n_tests=8, history=rig.idle(120), known=e, retorqued=False)
    assert b2['state'] == batch.COMPLETE and b2['hold_fixed'] == b['hold_fixed']
    assert {r['hold_c'] for r in b2['runs']} == {b['hold_fixed']}
    assert b2['ended'] - b2['started'] < b['ended'] - b['started']


def test_it_keeps_cooling_until_the_gap_is_secured():
    # A warm valve body (38 °C, cooling ~0.3 K/min): it cools slower than
    # 1 K/min from the start, only 2 K below the opening point. The cooldown
    # carries on until the gap is secured, rather than hold there and then
    # stop on the gap guard.
    known = dict(t_open_C=40.5, upstream_bar=3.0, scatter_K=0.3, creep_C_min=3.0)
    rig = low(start_c=38.0, body_tau=3000.0, open_scatter=0.0)
    b, _ = rig.run_batch(n_tests=4, known=known, retorqued=False)
    assert b['state'] == batch.COMPLETE, b['note']
    assert b['runs'][0]['cool_end'] == 'slowed'
    assert all(r['T_onset'] - r['hold_c'] >= config.BATCH_MIN_GAP_K
               for r in b['runs'] if r['averaged'])


def test_the_rounded_hold_keeps_the_gap():
    # A 33.5 °C lab: cooling slows right at 5 K below the opening point;
    # rounding the hold up must not undo the gap.
    rig = low(ambient=33.5, open_scatter=0.0)
    b, _ = rig.run_batch(n_tests=4, history=rig.idle(120))
    if b['state'] == batch.STOPPED:                    # it may honestly not make it…
        assert "too close to room temperature" in b['note'] and "cooldown" in b['note']
    else:                                              # …but never by rounding
        assert all(r['T_onset'] - r['hold_c'] >= config.BATCH_MIN_GAP_K
                   for r in b['runs'] if r['averaged'])


def test_too_close_to_room_temperature_stops_it():
    # 0.25 N·m at 5 bar: opens ~29 °C in a 27 °C lab — can't get 5 K below
    rig = low(open_c=29.0, ambient=27.0, open_scatter=0.0)
    b, _ = rig.run_batch(n_tests=3, history=rig.idle(120))
    assert b['state'] == batch.STOPPED and "too close to room temperature" in b['note']
    assert not rig.h['armed']


def test_a_reference_too_close_to_the_hold_stops_before_heating():
    # remembered hold 37 °C but the opening point 40 °C: a 3 K gap
    known = dict(t_open_C=40.0, upstream_bar=3.0, scatter_K=0.3, creep_C_min=3.0,
                 settings=dict(hold_C=37.0))
    rig = low(open_c=40.0, open_scatter=0.0)
    b, _ = rig.run_batch(n_tests=3, history=rig.idle(120), known=known, retorqued=False)
    assert b['state'] == batch.STOPPED and "minimum 5 K" in b['note']
    assert b['runs'][0]['start_c'] is not None and b['runs'][0]['T_onset'] is None


def test_a_hold_that_cant_be_reached_again_stops_it():
    def hook(rig, b):
        if b['hold_fixed'] is not None and b['phase'] == batch.CREEP:
            rig.ambient = 38.0                           # the lab (or rig) warms up
    rig = low()
    b, _ = rig.run_batch(n_tests=8, history=rig.idle(120), hook=hook)
    assert b['state'] == batch.STOPPED and "lab warmer" in b['note']


def test_a_find_again_frees_the_hold():
    known = dict(t_open_C=60.3, upstream_bar=3.0, scatter_K=0.15, creep_C_min=3.0,
                 k_per_bar=-12.0, settings=dict(hold_C=35.0))
    rig = Rig(open_c=45.0)                               # moved down: target 25 °C now
    b, _ = rig.run_batch(n_tests=6, history=rig.idle(120), known=known, retorqued=False)
    assert b['finds'] == 1 and b['state'] == batch.COMPLETE
    assert b['hold_fixed'] < 35.0
    assert all(r['T_onset'] - r['hold_c'] >= config.BATCH_MIN_GAP_K
               for r in b['runs'] if r['averaged'])


def test_the_approach_message_says_why():
    rig = Rig(open_c=60.0)
    b, msgs = rig.run_batch(n_tests=3)
    starts = [m for m in msgs if "testrun" in m and "heating to" in m]
    assert starts and all("K below" in m and "()" not in m for m in starts)


# ── the stopping rule ──────────────────────────────────────────────────────

def test_student_t():
    assert batch.t975(1) == 12.706 and batch.t975(2) == 4.303 and batch.t975(30) == 2.042
    assert batch.t975(31) == pytest.approx(2.040, abs=0.002)
    assert batch.t975(120) == pytest.approx(1.980, abs=0.002)
    assert batch.t975(0) is None


def test_more_scatter_needs_more_runs_and_N_is_the_most():
    rig = Rig(open_c=60.0, open_scatter=1.5, seed=4)
    b, _ = rig.run_batch(n_tests=10)
    mean, sd, n, half = batch.precision(b)
    assert b['state'] == batch.COMPLETE and n > config.BATCH_MIN_TESTS
    assert half <= config.BATCH_PRECISION_K and "precise enough" in b['note']
    # before the last run it wasn't precise enough yet
    rig = Rig(open_c=60.0, open_scatter=1.5, seed=4)
    b, _ = rig.run_batch(n_tests=4)
    assert b['state'] == batch.COMPLETE and "all 4 test runs done" in b['note']
    assert sum(r['counts'] for r in b['runs']) == 4


def test_the_upstream_correction_removes_the_leak_scatter_not_the_mean():
    rig = Rig(open_c=60.0, upstream_leak_bar_s=0.07 / 60, k_up=-12.0)
    b, _ = rig.run_batch(n_tests=10)
    runs = [r for r in b['runs'] if r['averaged']]
    raw = [r['T_onset'] for r in runs]
    mean, sd, n, half = batch.precision(b)
    assert mean == pytest.approx(sum(raw) / len(raw))              # the mean is untouched
    assert sd < 0.5 < __import__('statistics').stdev(raw)          # the leak's scatter is gone
    assert b['state'] == batch.COMPLETE and "precise enough" in b['note']


def test_margin_rule():
    assert batch.margin(None) == config.BATCH_MARGIN_MAX_K
    assert batch.margin(0.1) == config.BATCH_MARGIN_MIN_K
    assert batch.margin(1.0) == pytest.approx(3.5)
    assert batch.margin(3.0) == config.BATCH_MARGIN_MAX_K
    assert batch.margin(1.0, up_moved_bar=-1.2) == pytest.approx(5.5)
    assert batch.margin(1.0, up_moved_bar=0.8) == pytest.approx(3.5)


# ── detection threshold ────────────────────────────────────────────────────

def test_absolute_or_relative_whichever_first_with_a_floor():
    import math
    lg = math.log10
    # low background: +12 % comes before +1e-7 mbar
    assert batch.open_threshold_dec(lg(5e-7)) == pytest.approx(0.05)
    # 1e-6 mbar: +1e-7 is +10 % — first
    assert batch.open_threshold_dec(lg(1e-6)) == pytest.approx(lg(1.1))
    # high background: 1e-7 would be under the noise floor
    assert batch.open_threshold_dec(lg(5e-6)) == pytest.approx(config.BATCH_DETECT_FLOOR_DEC)


def test_detection_at_a_higher_background_is_earlier_than_plus_12_percent(monkeypatch):
    rig = Rig(open_c=60.0, base=1.5e-6)
    b, _ = rig.run_batch(n_tests=3)
    t_abs = [r['T_detect'] for r in b['runs'] if r['averaged']]
    monkeypatch.setattr(batch, "BATCH_DETECT_ABS_MBAR", None)
    rig = Rig(open_c=60.0, base=1.5e-6)
    b, _ = rig.run_batch(n_tests=3)
    t_rel = [r['T_detect'] for r in b['runs'] if r['averaged']]
    assert max(t_abs) < min(t_rel)


# ── starting from a remembered opening point ───────────────────────────────

KNOWN = dict(torque_Nm=0.3, t_open_C=60.3, upstream_bar=3.0, k_per_bar=-12.0,
             k_per_bar_from='batch fit', scatter_K=0.15, n=3, ci95_K=0.4,
             creep_C_min=3.0, batch='20260924_120000_0.30Nm_3.00bar', date='24 Sep 2026')


def test_remembered_start_skips_the_scouts():
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=6, known=dict(KNOWN), retorqued=False)
    names = [r['name'] for r in b['runs']]
    assert names == ['testrun01', 'testrun02', 'testrun03']
    t1 = b['runs'][0]
    assert t1['ref_c'] == pytest.approx(KNOWN['t_open_C'])
    assert t1['margin'] == pytest.approx(batch.margin(KNOWN['scatter_K']))
    assert b['state'] == batch.COMPLETE
    assert batch.batch_summary(b, [])['started_from'].startswith('remembered')


def test_remembered_start_is_shifted_for_the_upstream_pressure_now():
    known = dict(KNOWN, upstream_bar=3.5)            # measured at 3.5 bar, now 3.0
    rig = Rig(open_c=60.0)
    b, _ = rig.run_batch(n_tests=3, known=known, retorqued=False)
    t1 = b['runs'][0]
    assert t1['ref_c'] == pytest.approx(60.3 + (-12.0) * (3.0 - 3.5), abs=0.01)


def test_find_again_when_the_valve_moved_down():
    rig = Rig(open_c=50.0)                           # remembered 60.3, now opens at 50
    b, _ = rig.run_batch(n_tests=6, known=dict(KNOWN), retorqued=False)
    names = [r['name'] for r in b['runs']]
    assert names[:2] == ['testrun01', 'scout2'] and b['finds'] == 1
    t1, s2 = b['runs'][:2]
    assert t1['opened_during'] == batch.APPROACH and not t1['averaged']
    assert s2['start_c'] == pytest.approx(min(t1['start_c'], t1['T_onset']) - 10.0)
    mean, _, n, _ = batch.precision(b)
    assert b['state'] == batch.COMPLETE and n >= 3 and 50.0 <= mean <= 51.0
    assert b['runs'][2]['ref_c'] == pytest.approx(s2['T_onset'])   # from the re-scout
    e = batch.remembered_entry(b, "x")
    assert e is not None and e['t_open_C'] == pytest.approx(mean, abs=0.01)


def test_a_valve_that_moved_up_just_creeps_longer():
    rig = Rig(open_c=72.0)
    b, _ = rig.run_batch(n_tests=6, known=dict(KNOWN), retorqued=False)
    assert b['finds'] == 0 and b['runs'][0]['opened_during'] == batch.CREEP
    assert b['state'] == batch.COMPLETE
    assert 72.0 <= batch.precision(b)[0] <= 73.0


# ── what gets remembered ───────────────────────────────────────────────────

def ended_batch(t_opens, ups=None, known=None, old=None, retorqued=None):
    """A finished batch with test runs at these T_opens (no simulation)."""
    b = batch.new_batch(10, 0.3, 1_000_000.0, known=known, old=old, retorqued=retorqued)
    b['runs'] = []
    for i, t in enumerate(t_opens):
        r = batch._new_run(b, i + 2, 0.0, None)
        r.update(status=batch.OPENED, opened_during=batch.CREEP, averaged=True, T_onset=t,
                 up_open=(ups[i] if ups else 3.0))
        b['runs'].append(r)
    b['run'] = b['runs'][-1] if b['runs'] else b['run']
    b['ended'] = 1_000_100.0
    return b


def test_remembered_when_nothing_was_before():
    e = batch.remembered_entry(ended_batch([60.1]), "b1")
    assert e['t_open_C'] == 60.1 and e['n'] == 1 and e['scatter_K'] is None
    assert e['creep_C_min'] == config.BATCH_CREEP_C_MIN and e['batch'] == "b1"
    assert e['settings']['hold_s'] == config.BATCH_HOLD_S


def test_replaced_after_three_test_runs():
    e = batch.remembered_entry(ended_batch([60.2, 60.4, 60.3], known=KNOWN, old=KNOWN,
                                           retorqued=False), "b2")
    assert e['n'] == 3 and e['t_open_C'] == pytest.approx(60.3)


def test_kept_after_a_short_batch_that_agrees():
    assert batch.remembered_entry(ended_batch([60.5], known=KNOWN, old=KNOWN,
                                              retorqued=False), "b3") is None


def test_replaced_after_a_short_batch_that_shows_it_moved():
    e = batch.remembered_entry(ended_batch([64.0], known=KNOWN, old=KNOWN,
                                           retorqued=False), "b4")
    assert e is not None and e['t_open_C'] == 64.0


def test_moved_is_judged_at_the_old_upstream_pressure():
    # 63.3 °C at 2.75 bar is 60.3 °C at 3.0 bar with −12 K/bar: not moved
    assert batch.remembered_entry(ended_batch([63.3], ups=[2.75], known=KNOWN, old=KNOWN,
                                              retorqued=False), "b5") is None


def test_replaced_whenever_the_valve_was_disturbed():
    e = batch.remembered_entry(ended_batch([60.5], old=KNOWN, retorqued=True), "b6")
    assert e is not None and e['n'] == 1


def test_nothing_to_remember_without_a_creeping_opening():
    assert batch.remembered_entry(ended_batch([]), "b7") is None
