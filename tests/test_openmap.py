"""The opening map: the openings table, the import of old batch folders,
and the fit over all of it (context.md, "Mapping the opening point";
history 34)."""
import csv
import importlib.util
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from batch_sim import Rig
from driver import batchrun, config, openmap, schema, shared

REPO = Path(__file__).resolve().parent.parent

# The 28 Sept 2026 0.45 N·m batch, as its summary.csv has it (the columns
# the import reads).
REAL = [
    # run, status, opened_during, creep, onset, T_open, T_detect, upstream, base, closed, hold, hold_s, gap
    ("scout1", "opened", "creep", 3.0, "2026-09-28T13:10:49.721", 125.55, 125.65, 2.6511,
     4.136e-07, 104.42, 30.5, 8.0, 95.05),
    ("testrun01", "opened", "creep", 3.0, "2026-09-28T13:27:24.429", 131.3, 131.33, 2.1029,
     4.044e-07, 109.84, 38.0, 240.0, 93.3),
    ("testrun02", "opened", "creep", 3.0, "2026-09-28T13:43:20.623", 126.02, 126.08, 2.5768,
     3.909e-07, 102.7, 38.0, 240.0, 88.02),
    ("testrun03", "aborted", "", 3.0, "", None, None, None, 3.808e-07, None, 38.0, 240.0, None),
    ("testrun03b", "opened", "creep", 3.0, "2026-09-28T14:12:29.630", 125.35, 125.48, 2.811,
     3.695e-07, 99.54, 38.0, 240.0, 87.35),
    ("testrun04", "opened", "creep", 3.0, "2026-09-28T14:27:49.896", 127.48, 127.52, 2.5519,
     3.653e-07, 109.56, 38.0, 240.0, 89.48),
    ("testrun05", "opened", "creep", 3.0, "2026-09-28T14:43:23.124", 137.05, 137.16, 1.8393,
     3.572e-07, 111.32, 38.0, 240.0, 99.05),
    ("testrun06", "opened", "creep", 3.0, "2026-09-28T14:59:26.835", 128.49, 128.57, 2.5097,
     3.492e-07, None, 38.0, 240.0, 90.49),
]
REAL_NAME = "20260928_130630_0.45Nm_2.95bar"


def write_batch(folder, name, runs, torque=0.45, settings=None):
    d = Path(folder) / name
    d.mkdir(parents=True)
    with open(d / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(schema.RUN_SUMMARY)
        for (run, status, during, creep, onset, T, Td, up, base, closed, hold, hold_s,
             gap) in runs:
            row = {c: '' for c in schema.RUN_SUMMARY}
            row.update(batch=name, run=run, status=status, opened_during=during,
                       creep_degC_per_min=creep, onset_time=onset, t_open_degC=T,
                       t_detect_degC=Td, upstream_at_open_bar=up,
                       chamber_baseline_mbar=base, free_cooling_closed_degC=closed,
                       hold_degC=hold, hold_s=hold_s, hold_gap_K=gap,
                       seat_screw_torque_Nm=torque)
            w.writerow(['' if row[c] is None else row[c] for c in schema.RUN_SUMMARY])
    if settings is not None:
        (d / "batch.json").write_text(json.dumps(dict(settings=settings)), encoding="utf-8")
    return d


# ── the import ─────────────────────────────────────────────────────────────

def test_the_28_sept_batch_imports_its_creeping_openings(tmp_path):
    write_batch(tmp_path / "batches", REAL_NAME, REAL)
    msg = openmap.import_batches(tmp_path / "batches", tmp_path / "o.csv", sheet=False)
    assert "imported 7 openings from 1 batch folder" in msg
    rows = openmap.load(tmp_path / "o.csv")
    assert [r['source'].split('/')[-1] for r in rows] == [
        'scout1', 'testrun01', 'testrun02', 'testrun03b', 'testrun04', 'testrun05', 'testrun06']
    assert {r['setting'] for r in rows} == {REAL_NAME}
    assert all(r['deep'] == '1' for r in rows)              # every batch run started cold
    assert rows[0]['detect_rule'].startswith("old")         # no batch.json: the old rule
    # once only
    assert openmap.import_batches(tmp_path / "batches", tmp_path / "o.csv", sheet=False) == ""
    assert len(openmap.load(tmp_path / "o.csv")) == 7


def test_fast_scouts_and_approach_openings_are_not_imported(tmp_path):
    runs = [("scout1", "opened", "creep", "", "2026-09-28T13:00:00", 140.0, 145.0, 2.5,
             4e-7, None, 30.0, 0.0, 95.0),                   # heated fast: no creep rate
            ("testrun01", "opened", "approach", 3.0, "2026-09-28T13:10:00", 120.0, 121.0,
             2.5, 4e-7, None, 38.0, 240.0, 80.0),            # opened before creeping
            ("testrun02", "no opening", "", 3.0, "", None, None, None, 4e-7, None, 38.0,
             240.0, None)]
    write_batch(tmp_path / "b", "x", runs)
    assert openmap.rows_from_batch(tmp_path / "b" / "x") == []


def test_the_detection_rule_is_read_from_the_batch(tmp_path):
    d = write_batch(tmp_path / "b", "y", REAL[:2],
                    settings=dict(BATCH_DETECT_ABS_MBAR=1e-7, BATCH_DETECT_REL_DEC=0.05))
    assert openmap.rows_from_batch(d)[0]['detect_rule'] == "+1e-07 mbar or +12 %"
    assert openmap.current_rule() == "+5e-08 mbar"


# ── the fit ────────────────────────────────────────────────────────────────

def real_fit(tmp_path):
    write_batch(tmp_path / "batches", REAL_NAME, REAL)
    openmap.import_batches(tmp_path / "batches", tmp_path / "o.csv", sheet=False)
    return openmap.fit(openmap.load(tmp_path / "o.csv"))


def test_the_28_sept_fit(tmp_path):
    # as worked out by hand on 28 Sept: −12.0 ± 3.5 K/bar, scatter 1.1 K,
    # 128.0 °C at 2.5 bar
    f = real_fit(tmp_path)
    k, kh, how = f.slopes['0.45']
    assert how == 'fit' and k == pytest.approx(-12.01, abs=0.01) and kh == pytest.approx(3.51, abs=0.01)
    assert f.sd == pytest.approx(1.14, abs=0.01) and f.n == 7
    t, how = f.predict(REAL_NAME, 0.45, 2.5)
    assert t == pytest.approx(127.96, abs=0.01) and how == "this setting's fit"
    # drift and background moved together (pump-down over the batch): not
    # separable, so neither corrects the fit — and it says so
    assert f.confounded and {f.confounded[0][0], f.confounded[0][1]} == {'drift', 'background'}
    assert not any(v[2] for v in f.terms.values())
    assert "can't be told apart" in f.terms_text()
    # not done: the slope needs a wider pressure range; above 2.81 bar has room
    st = f.status(REAL_NAME)
    assert not st['done'] and "try ~3.8 bar" in st['hint']
    assert "7 openings" in f.status_text(REAL_NAME)


def synthetic(settings, n=12, noise=0.3, seed=1, deep_every=0, drift_k_h=0.0,
              warm=0.0, bars=(1.5, 4.5), bg_k_dec=0.0, bg=(4e-7, 4e-7)):
    """Openings from known truths: settings = [(name, torque, T at 3 bar,
    K/bar)]."""
    rng = random.Random(seed)
    t0 = datetime(2026, 10, 1, 9, 0, 0)
    rows = []
    for si, (name, tq, off, k) in enumerate(settings):
        for i in range(n):
            bar = bars[0] + (bars[1] - bars[0]) * rng.random()
            deep = 1 if deep_every and i % deep_every == 0 else 0
            hours = i * 4 / 60.0
            base = bg[0] * (bg[1] / bg[0]) ** (rng.random())
            T = (off + k * (bar - 3.0) + warm * deep + drift_k_h * (hours - (n - 1) * 2 / 60)
                 + bg_k_dec * (__import__('math').log10(base) - __import__('math').log10(4e-7))
                 + rng.gauss(0, noise))
            rows.append(dict(time=(t0 + timedelta(days=si, hours=hours)).isoformat(),
                             setting=name, torque_Nm=tq, upstream_bar=bar, t_open_degC=T,
                             baseline_mbar=base, deep=deep))
    return rows


def test_offsets_per_setting_and_slope_per_torque_are_recovered():
    truth = [("a1", 0.45, 128.0, -12.0), ("a2", 0.45, 131.0, -12.0),      # a re-torque: +3 K
             ("b1", 0.30, 92.0, -6.0)]
    f = openmap.fit(synthetic(truth, n=15))
    assert f.offsets['a1'][0] == pytest.approx(128.0, abs=0.3)
    assert f.offsets['a2'][0] - f.offsets['a1'][0] == pytest.approx(3.0, abs=0.4)
    assert f.slopes['0.45'][0] == pytest.approx(-12.0, abs=0.3)
    assert f.slopes['0.30'][0] == pytest.approx(-6.0, abs=0.4)
    assert f.sd == pytest.approx(0.3, abs=0.1)
    for s in ('a1', 'a2', 'b1'):
        assert f.status(s)['done']
    assert "DONE" in f.status_text('a1')


def test_a_new_setting_is_predicted_from_its_torques_others():
    f = openmap.fit(synthetic([("a1", 0.45, 128.0, -12.0), ("a2", 0.45, 130.0, -12.0)]))
    t, how = f.predict("new", 0.45, 2.0)
    assert t == pytest.approx(129.0 + 12.0, abs=0.5) and "2 other settings" in how
    assert f.predict("new", 0.30, 2.0) is None                # nothing at that torque


def test_too_little_pressure_spread_assumes_the_slope():
    f = openmap.fit(synthetic([("a1", 0.45, 128.0, -12.0)], bars=(2.9, 3.1)))
    k, kh, how = f.slopes['0.45']
    assert how == 'assumed' and k == -config.PRESSURE_UP_K_PER_BAR and kh is None
    st = f.status('a1')
    assert not st['done'] and "try ~" in st['hint'] and "(assumed)" in f.status_text('a1')


def test_the_slope_is_learned_only_within_a_setting():
    # Two settings at 0.40 N·m, each held at one pressure (2 and 4 bar): the
    # offsets absorb the difference, so there's nothing to fit a slope from
    # (it was unsolvable before) — assumed, and the hint says: don't re-torque.
    rows = (synthetic([("a", 0.40, 90.0, -8.0)], bars=(2.0, 2.0), seed=1)
            + synthetic([("b", 0.40, 90.0, -8.0)], bars=(4.0, 4.0), seed=2))
    f = openmap.fit(rows)
    assert f.n == len(rows) and f.slopes['0.40'][2] == 'assumed'
    assert "without re-torquing" in f.status('a')['hint']
    # one of them swept 1 bar: now it's fitted, from that setting
    rows += synthetic([("a", 0.40, 90.0, -8.0)], bars=(2.0, 3.0), seed=3)
    k, kh, how = openmap.fit(rows).slopes['0.40']
    assert how == 'fit' and k == pytest.approx(-8.0, abs=1.0)


def test_the_warm_start_effect_is_measured_from_deep_cycles():
    f = openmap.fit(synthetic([("a1", 0.45, 128.0, -12.0)], n=30, deep_every=5, warm=2.0))
    v, half, kept = f.terms['warm-start']
    assert kept and v == pytest.approx(2.0, abs=0.5)
    # the offset is a shallow cycle's; a deep one is predicted the effect higher
    assert f.predict('a1', 0.45, 3.0, deep=True)[0] - f.predict('a1', 0.45, 3.0)[0] \
        == pytest.approx(v)


def test_no_warm_start_effect_is_reported_as_not_significant():
    f = openmap.fit(synthetic([("a1", 0.45, 128.0, -12.0)], n=30, deep_every=5, warm=0.0))
    v, half, kept = f.terms['warm-start']
    assert not kept and abs(v) < half


def test_drift_is_found_and_the_offset_stays_the_settings_average():
    f = openmap.fit(synthetic([("a1", 0.45, 128.0, -12.0)], n=30, drift_k_h=1.5))
    v, half, kept = f.terms['drift']
    assert kept and v == pytest.approx(1.5, abs=0.4)
    assert f.offsets['a1'][0] == pytest.approx(128.0, abs=0.3)


def test_a_background_effect_is_found_when_it_varies_on_its_own():
    f = openmap.fit(synthetic([("a1", 0.45, 128.0, -12.0)], n=30, bg_k_dec=-6.0,
                              bg=(2e-7, 8e-7)))
    v, half, kept = f.terms['background']
    assert kept and v == pytest.approx(-6.0, abs=1.5)
    assert not f.confounded


def test_openings_without_upstream_are_counted_but_not_fitted():
    rows = synthetic([("a1", 0.45, 128.0, -12.0)])
    rows[0]['upstream_bar'] = ''
    f = openmap.fit(rows)
    assert f.excluded == 1 and f.n == len(rows) - 1


def test_pressure_hint_goes_where_there_is_room():
    assert "try ~1.0 bar" in openmap._pressure_hint((1.8, 4.6))
    assert "try ~3.8 bar" in openmap._pressure_hint((1.84, 2.81))
    assert "already wide" in openmap._pressure_hint((1.0, 5.0))


def test_an_empty_table():
    f = openmap.fit([])
    assert f.n == 0 and f.offsets == {} and openmap.summary("nope.csv") == ""


# ── the driver: startup import, batches feed the table, the figure ─────────

def test_the_startup_summary_names_the_newest_setting(tmp_path):
    write_batch(Path(config.BATCH_DIR).parent / "batches", REAL_NAME, REAL)
    assert "imported 7" in openmap.import_batches()
    line = openmap.summary()
    assert line.startswith("Opening map: 7 openings, 1 setting;") and REAL_NAME in line


def test_a_batch_feeds_the_openings_table(clock, monkeypatch):
    import test_batch as tb
    monkeypatch.setattr(batchrun, "PLOT", False)
    shared.set_seat_screw_torque(0.3)
    shared.store_keller(3.0, None, clock.t)
    tb.sensors()
    ok, name = batchrun.start(4, start_thread=False)
    tb.drive(clock, Rig(open_c=60.0))
    rows = openmap.load()
    creeping = [s for s in batchrun._summaries
                if s['status'] == 'opened' and s['opened_during'] == 'creep']
    assert len(rows) == len(creeping) >= 4 and {r['setting'] for r in rows} == {name}
    assert all(r['detect_rule'] == "+5e-08 mbar" for r in rows)
    events = " | ".join(t for _, t in shared.recent_events())
    assert "map: " in events
    # a restart doesn't import the same batch again
    assert openmap.import_batches() == ""


def test_the_map_figures_draw_2d_and_3d(tmp_path):
    pytest.importorskip("matplotlib")
    import matplotlib
    matplotlib.use("Agg")
    rows = synthetic([("a1", 0.45, 128.0, -12.0), ("a2", 0.45, 131.0, -12.0),
                      ("b1", 0.30, 92.0, -6.0)], deep_every=5)
    path = tmp_path / "o.csv"
    for r in rows:
        openmap.append(r, path, sheet=False)
    spec = importlib.util.spec_from_file_location("te_plotter", REPO / "TE_PLOTTER.py")
    plotter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plotter)
    out = plotter.plot_map(path, str(tmp_path / "map.png"), show=False)
    assert Path(out).stat().st_size > 20_000
    assert (tmp_path / "map-3d.png").stat().st_size > 20_000
