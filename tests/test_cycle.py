"""Cycling: openings one after another (context.md, "Cycling"; history 34).

Agreed behaviour (28 Sept 2026):
  * creep from the margin below the fit's prediction at the upstream
    pressure now; detection (heater off); cool until the valve has closed and
    is margin + 2 K below the prediction; again — until stopped
  * every 5th cycle (the first included) is deep: 35 °C (or T_open − 20 K)
    and a 4 min hold, so the fit measures the warm-start effect
  * no prediction yet: the first cycle scouts from the torque table
  * every creeping opening is a row of the openings table; the fit is redone
  * opened while approaching: not a row; the next start is 5 K lower
  * a refill while heating pauses detection; the cycle carries on
  * two cycles in a row without an opening stop it; DISARM or stop end it
"""
import csv
import json
from pathlib import Path

import pytest

from batch_sim import Rig
from driver import batch, config, controller, cycle, cyclerun, openmap, shared

VALVE = dict(open_c=128.0, k_up=-12.0, open_scatter=0.5, hysteresis=20.0, seed=3)


def session(max_s=3600, **kw):
    rig = Rig(**{**VALVE, **kw.pop('rig', {})})
    return (rig, *rig.run_cycling(max_s=max_s, **kw))


def test_a_session_of_openings():
    rig, c, rows, f, msgs = session(max_s=3600)
    assert c['state'] == cycle.RUNNING and len(rows) >= 12            # ~3 min per cycle
    assert rows[0]['note'].startswith("scout") and rows[0]['deep'] == 1
    assert [r['deep'] for r in rows[:11]] == [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    assert all(127.0 <= r['t_open_degC'] <= 131.0 for r in rows)
    assert all(r['upstream_bar'] == pytest.approx(3.0) for r in rows)
    assert all(r['detect_rule'] == "sim" and r['seating'] == "s1" for r in rows)
    s = f.status("s1")
    assert s['offset'][0] == pytest.approx(128.8, abs=1.0) and s['n'] == len(rows)


def test_deep_cycles_hold_and_shallow_ones_dont():
    rig, c, rows, f, msgs = session(max_s=2400)
    for r in rows:
        if r['deep']:
            assert r['bottom_s'] >= config.BATCH_HOLD_S and r['bottom_degC'] <= 35.0
        else:
            assert r['bottom_s'] < 90                      # only until the chamber settles
            # the margin + 2 K below the opening point, not the 35 °C hold
            assert 3.5 <= r['t_open_degC'] - r['bottom_degC'] <= 12.0


def test_a_shallow_cooldown_waits_for_the_valve_to_close():
    # A valve that keeps flowing until 25 K below where it opened (a = 1: a
    # strong tail): the cooldown goes on until the chamber is back.
    rig, c, rows, f, msgs = session(max_s=2400, rig=dict(a=1.0, efold=10.0))
    shallow = [r for r in rows if not r['deep']]
    assert shallow and all(r['t_open_degC'] - r['bottom_degC'] > 10.0 for r in shallow)


def test_deep_every_is_settable():
    rig, c, rows, f, msgs = session(max_s=2400, deep_every=2)
    assert [r['deep'] for r in rows[:5]] == [1, 0, 1, 0, 1]


def test_with_a_prediction_it_doesnt_scout():
    rows0 = [dict(time=f"2026-09-28T13:{10 + i}:00", seating="s1", torque_Nm=0.45,
                  upstream_bar=u, t_open_degC=128.0 - 12.0 * (u - 3.0), deep=1)
             for i, u in enumerate((2.0, 2.5, 3.0, 3.5))]
    rig, c, rows, f, msgs = session(max_s=900, rows=list(rows0))
    new = rows[len(rows0):]
    assert new and not new[0]['note']
    assert any("this seating's fit" in m for m in msgs)


def test_a_valve_that_moved_down_is_caught_in_the_hold_then_followed():
    # It moves 16 K down during a hold: the shallow hold (~120 °C) is
    # above its new opening point, so it opens there — caught, not a row —
    # and the cycles after it creep onto the new opening point.
    def hook(rig, c):
        if c['openings'] == 4 and c['phase'] == batch.HOLD:
            rig.open_c = 112.0
    rig, c, rows, f, msgs = session(max_s=3000, hook=hook, deep_every=100)  # no deep cycle
    assert any("while still holding" in m for m in msgs)
    assert all(r['t_open_degC'] > 125.0 for r in rows[:4])
    assert len(rows) > 6 and all(111.0 <= r['t_open_degC'] <= 115.0 for r in rows[5:])
    assert c['state'] == cycle.RUNNING


def test_a_valve_that_moved_down_mid_creep_is_followed():
    def hook(rig, c):
        if c['openings'] == 4:
            rig.open_c = 118.0
    rig, c, rows, f, msgs = session(max_s=3000, hook=hook)
    assert all(117.0 <= r['t_open_degC'] <= 121.0 for r in rows[6:])
    assert c['state'] == cycle.RUNNING


def test_an_opening_during_the_burst_is_not_a_creep_reading():
    # The valve moved 10 K down while cooling after a deep cycle's hold at
    # 35 °C: the approach bursts through 118 °C and the chamber lags, so
    # detection lands after the creep began. Judged by the onset, it opened
    # while approaching — no row reads several K high.
    def hook(rig, c):
        if c['openings'] == 5 and c['cyc']['deep'] and c['phase'] == batch.HOLD:
            rig.open_c = 118.0
    rig, c, rows, f, msgs = session(max_s=3000, hook=hook)
    moved = [r for r in rows[5:]]
    assert moved and all(r['t_open_degC'] <= 121.0 for r in moved)
    assert any("while still approaching" in m for m in msgs)


def test_a_refill_while_creeping_keeps_the_cycle():
    seen = set()

    def hook(rig, c):
        seen.add(cycle.labels(c)[1])
        # during a deep cycle's approach, well below the opening point
        if c['cyc']['deep'] and c['openings'] >= 1 and c['phase'] == batch.APPROACH \
                and rig.fills == 0 and rig.T < rig.opening() - 15.0:
            rig.up_full = rig.upstream + 0.3
            rig.fill()
    rig, c, rows, f, msgs = session(max_s=2400, hook=hook, rig=dict(fill_jump=0.7))
    assert rig.fills == 1 and cycle.REFILL in seen
    after = [r for r in rows if r['refills']]
    assert len(after) == 1 and after[0]['upstream_bar'] == pytest.approx(3.3, abs=0.02)
    # the opening point fell 12 K/bar × 0.3 bar, and the reading follows
    assert after[0]['t_open_degC'] == pytest.approx(128.0 - 3.6 + 0.5, abs=1.5)


def test_two_cycles_without_an_opening_stop_it():
    def hook(rig, c):
        if c['openings'] == 2:
            rig.open_c = 500.0
    rig, c, rows, f, msgs = session(max_s=4 * 3600, hook=hook)
    assert c['state'] == cycle.STOPPED and "2 cycles in a row" in c['note']
    assert not rig.h['armed'] and len(rows) == 2


def test_stop_and_disarm():
    rig, c, rows, f, msgs = session(max_s=4 * 3600, stop_after=3)
    assert c['state'] == cycle.STOPPED and not rig.h['armed'] and len(rows) == 3

    def hook(rig, c):
        if c['openings'] == 1 and c['phase'] == batch.CREEP:
            controller.command(rig.h, rig.now, armed=False)
    rig, c, rows, f, msgs = session(max_s=3600, hook=hook)
    assert c['state'] == cycle.ABORTED and "operator" in c['note']


def test_labels_and_status():
    assert cycle.labels(None) == ('', '')
    seen = []
    rig, c, rows, f, msgs = session(max_s=900, hook=lambda r, c: seen.append(
        (cycle.labels(c), cycle.status_text(c))))
    names = {lab[0] for lab, _ in seen}
    assert {'cycle001', 'cycle002'} <= names
    assert any("(deep)" in s and "(scout)" in s for _, s in seen)


# ── cyclerun: files, the table, the fit after every opening ────────────────

def drive(clock, rig, max_s=3600):
    """Run the current cycling through cyclerun / control.py on the rig."""
    import test_batch as tb
    from driver import control
    dt, next_row, on = 0.25, clock.t, 0.0
    rig.now = clock.t
    t0 = clock.t
    while cyclerun.running() and clock.t - t0 < max_s:
        vac, status = rig.vac()
        shared.store_valve_temp(rig.T, 0)
        shared.store_vacuum(vac, status, None)
        shared.store_keller(rig.upstream, None, clock.t)
        cyclerun.tick()
        rig.duty = control.compute_duty(rig.T, True, dt, vac=vac, vac_status=status)
        on += rig.duty * dt
        rig.advance(dt)
        clock.advance(dt)
        if clock.t >= next_row:
            name, phase = cyclerun.labels()
            row = {c: '' for c in tb.schema.MAIN}
            row.update(timestamp=__import__('datetime').datetime.fromtimestamp(clock.t)
                       .isoformat(timespec='milliseconds'),
                       te_temperature_degC=round(rig.T, 3), vacuum_chamber_mbar=vac,
                       keller_pressure_bar=rig.upstream, batch_run=name, batch_phase=phase)
            cyclerun.record_row(row)
            on, next_row = 0.0, next_row + config.LOG_INTERVAL_S


def test_cyclerun_writes_its_files_and_the_openings(clock, monkeypatch):
    monkeypatch.setattr(cyclerun, "PLOT", False)
    shared.set_seat_screw_torque(0.45)
    shared.store_valve_temp(25.0, 0)
    shared.store_vacuum(4e-7, None, 1.7)
    shared.store_keller(3.0, None, clock.t)
    ok, name = cyclerun.start(start_thread=False)
    assert ok and name.endswith("_0.45Nm") and cyclerun.last_seating(0.45) is None
    drive(clock, Rig(**VALVE), max_s=1800)
    assert cyclerun.running()
    status = cyclerun.status()
    assert status.startswith("cycling running") and "\nmap: " + name in status
    cyclerun.stop("test")
    folder = Path(cyclerun.folder())
    info = json.load(open(folder / "session.json", encoding="utf-8"))
    assert info['seating'] == name and info['deep_every'] == config.CYCLE_DEEP_EVERY
    rows = openmap.load()
    assert len(rows) >= 6 and {r['seating'] for r in rows} == {name}
    assert all(r['detect_rule'] == "+5e-08 mbar" for r in rows)
    trace = Path(config.OPENINGS_CSV).parent / rows[1]['source']   # (absolute stays absolute)
    lines = list(csv.DictReader(open(trace, encoding="utf-8")))
    assert lines and {'approach', 'creep', 'cooldown'} <= {r['batch_phase'] for r in lines}
    events = " | ".join(t for _, t in shared.recent_events())
    assert "map: " + name in events and "Cycling stopped: test" in events
    assert "file error" not in events
    # the next start at this torque can continue the seating
    assert cyclerun.last_seating(0.45) == name
    ok, again = cyclerun.start(retorqued=False, start_thread=False)
    assert ok and again == name
    cyclerun.stop("test")
    ok, new = cyclerun.start(retorqued=True, start_thread=False)
    assert ok and new != name


def test_the_trace_path_survives_another_drive(monkeypatch):
    # Windows: the repo on a network share, the table on C: (28 Sept, the
    # lab laptop) — relpath raises ValueError; the full path is stored instead.
    import os
    def other_drive(path, start):
        raise ValueError("path is on mount '\\\\titania\\Space$', start on mount 'C:'")
    monkeypatch.setattr(os.path, "relpath", other_drive)
    p = os.path.join("S:", "logs", "cycles", "x", "cycle001.csv")
    assert cyclerun._source(p) == p


def test_start_is_refused_without_the_torque_or_armed(clock):
    ok, msg = cyclerun.start(start_thread=False)
    assert not ok and "torque" in msg
    shared.set_seat_screw_torque(0.45)
    shared.store_valve_temp(25.0, 0)
    shared.store_vacuum(4e-7, None, 1.7)
    from driver import control
    control.heater_command(armed=True)
    ok, msg = cyclerun.start(start_thread=False)
    assert not ok and "disarm" in msg
