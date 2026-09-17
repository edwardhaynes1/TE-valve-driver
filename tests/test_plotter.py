"""TE_PLOTTER reads what the driver writes."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("matplotlib")

from tevalve import schema  # noqa: E402

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
