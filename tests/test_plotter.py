"""TE_PLOTTER reads what the driver writes."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("matplotlib")

from driver import schema  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def plotter():
    spec = importlib.util.spec_from_file_location("te_plotter", REPO / "TE_PLOTTER.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def synthetic_log(n=600, columns=schema.MAIN):
    t = np.arange(n) * 0.5
    duty = np.where((t > 20) & (t < 40), 1.0, np.where(t > 120, 0.15, 0.0))
    data = {c: [""] * n for c in columns}
    data.update({
        "timestamp": (pd.Timestamp("2026-09-17 09:00")
                      + pd.to_timedelta(t, unit="s")).strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "keller_pressure_bar": 1.19 - 0.00025 * t,
        "keller_temperature_degC": 22.5,
        "vacuum_chamber_mbar": 1.5e-7 * np.where(t > 200, 4, 1),
        "te_temperature_degC": 25 + 15 * (1 - np.exp(-t / 60)),
        "tc_fault": 0,
        "heater_duty": duty,
        "heater_mode": "manual",
        "heater_I_mean_calc": duty * 24 / 88,
        "heater_P_mean_calc": duty * 24 ** 2 / 88,
        "heater_on_s": duty * 0.5,
    })
    return pd.DataFrame({c: data[c] for c in columns})


def test_every_role_maps_to_a_schema_column(plotter):
    assert set(plotter.ROLE_COLUMNS.values()) <= set(schema.MAIN)


def test_current_logs_use_exact_names(plotter):
    cols = plotter.resolve_columns(synthetic_log(5))
    for role, name in plotter.ROLE_COLUMNS.items():
        assert cols[role] == name, role


def test_old_logs_without_power_still_plot(plotter, tmp_path):
    old = [c for c in schema.MAIN if not c.startswith("heater_P_") and c != "heater_on_s"]
    path = tmp_path / "old.csv"
    synthetic_log(columns=old).to_csv(path, index=False)
    cols = plotter.resolve_columns(pd.read_csv(path, nrows=5))
    df = plotter.load(str(path), cols)
    assert cols["power_src"] == "derived: mean current × rail"
    assert df[cols["power"]].max() == pytest.approx(24 ** 2 / 88)


def test_full_run_writes_a_figure(plotter, tmp_path, monkeypatch):
    import matplotlib
    matplotlib.use("Agg")
    path = tmp_path / "te-sensor_test.csv"
    synthetic_log().to_csv(path, index=False)
    monkeypatch.setattr("sys.argv", ["TE_PLOTTER.py", str(path), "--no-show"])
    plotter.main()
    assert (tmp_path / "te-sensor_test.png").stat().st_size > 10_000


# ── seat screw torque ───────────────────────────────────────────────────────

def with_seat_screw(values):
    df = synthetic_log(len(values))
    df["seat_screw_torque_Nm"] = values
    return df


def loaded(plotter, tmp_path, df):
    path = tmp_path / "log.csv"
    df.to_csv(path, index=False)
    cols = plotter.resolve_columns(pd.read_csv(path, nrows=5))
    return plotter.load(str(path), cols), cols


def test_seat_screw_not_entered(plotter, tmp_path):
    df, cols = loaded(plotter, tmp_path, with_seat_screw([np.nan] * 6))
    history = plotter.seat_screw_history(df, cols)
    assert plotter.seat_screw_text(history) == "seat screw torque not recorded"


def test_seat_screw_constant(plotter, tmp_path):
    df, cols = loaded(plotter, tmp_path, with_seat_screw([0.4] * 6))
    history = plotter.seat_screw_history(df, cols)
    assert plotter.seat_screw_text(history) == "seat screw torque 0.40 N·m"


def test_seat_screw_entered_and_changed_mid_run(plotter, tmp_path):
    df, cols = loaded(plotter, tmp_path,
                      with_seat_screw([np.nan, np.nan, 0.4, 0.4, 0.45, 0.45]))
    history = plotter.seat_screw_history(df, cols)
    assert [v for _, v in history] == [None, 0.4, 0.45]
    assert [t for t, _ in history] == [0.0, 1.0, 2.0]        # 0.5 s rows
    text = plotter.seat_screw_text(history)
    assert text == ("seat screw torque not recorded → 0.40 N·m at 1.0 s "
                    "→ 0.45 N·m at 2.0 s")


def test_old_logs_have_no_seat_screw_column(plotter, tmp_path):
    old = [c for c in schema.MAIN if c != "seat_screw_torque_Nm"]
    df, cols = loaded(plotter, tmp_path, synthetic_log(6, columns=old))
    history = plotter.seat_screw_history(df, cols)
    assert plotter.seat_screw_text(history) == \
        "seat screw torque not recorded (log predates the column)"


def test_the_figure_title_and_markers_show_it(plotter, tmp_path):
    import matplotlib
    matplotlib.use("Agg")
    df, cols = loaded(plotter, tmp_path,
                      with_seat_screw([0.4] * 300 + [0.45] * 300))
    history = plotter.seat_screw_history(df, cols)
    fig = plotter.make_figure(df, cols, [], [], None, "log.csv",
                              seat_screw=history)
    assert "seat screw torque 0.40 N·m → 0.45 N·m at 150.0 s" in fig._suptitle.get_text()
    labels = [t.get_text() for ax in fig.axes for t in ax.texts]
    assert any("seat screw 0.45 N·m" in s for s in labels)
