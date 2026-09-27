from datetime import timedelta

import pytest

from bulltraining import performance
from bulltraining.db import get_float

from .conftest import TODAY


def test_protocol_formulas():
    assert performance.evaluate("ride_ftp20", {"avg_power_20min_w": 300})["ftp_w"] == 285
    assert performance.evaluate("ride_ramp", {"best_1min_power_w": 400})["ftp_w"] == 300
    r = performance.evaluate("run_30min_tt", {"distance_m": 7200, "avg_hr_last_20min": 170})
    assert r["threshold_pace_run_s_per_km"] == 250.0 and r["lthr_run"] == 170
    assert performance.evaluate("swim_css", {"t400": "6:40", "t200": "3:10"})["css_s_per_100m"] == 105.0
    five = performance.evaluate("run_5k_tt", {"time_5k": "20:00"})["threshold_pace_run_s_per_km"]
    assert 240 < five < 265  # 60-min-Pace etwas langsamer als 5-km-Pace (240 s/km)


def test_missing_and_implausible_inputs():
    with pytest.raises(performance.TestError, match="Fehlende"):
        performance.evaluate("swim_css", {"t400": "6:40"})
    with pytest.raises(performance.TestError, match="Unplausibles"):
        performance.evaluate("ride_ftp20", {"avg_power_20min_w": 2000})


def test_record_applies_only_newest(conn):
    performance.record_test(conn, date=TODAY.isoformat(), protocol="ride_ftp20", inputs={"avg_power_20min_w": 280})
    old = performance.record_test(conn, date=(TODAY - timedelta(days=60)).isoformat(), protocol="ride_ftp20",
                                  inputs={"avg_power_20min_w": 250})
    assert get_float(conn, "ftp_w") == 266
    assert old["changes"]["ftp_w"]["applied"] is False


def test_state_status_trend_and_zones(conn):
    performance.record_test(conn, date=(TODAY - timedelta(days=90)).isoformat(), protocol="ride_ftp20",
                            inputs={"avg_power_20min_w": 250})
    performance.record_test(conn, date=(TODAY - timedelta(days=10)).isoformat(), protocol="ride_ftp20",
                            inputs={"avg_power_20min_w": 263})
    state = performance.performance_state(conn, TODAY)
    ftp = state["metrics"]["ftp_w"]
    assert ftp["value"] == 250 and ftp["previous"] == 238 and ftp["improved"] is True
    assert state["tests"]["ride"]["status"] == "valid"
    assert set(state["needs_test"]) == {"run", "swim"}
    z2 = state["zones"]["ride_power"][1]
    assert (z2["from"], z2["to"]) == (140, 188)


def test_status_due_soon_and_stale(conn):
    performance.record_test(conn, date=(TODAY - timedelta(days=50)).isoformat(), protocol="swim_css",
                            inputs={"t400": "7:00", "t200": "3:20"})
    assert performance.test_status(conn, "swim", TODAY)["status"] == "due_soon"
    assert performance.test_status(conn, "swim", TODAY + timedelta(days=10))["status"] == "stale"


def test_lower_is_better_for_pace(conn):
    performance.record_test(conn, date=(TODAY - timedelta(days=40)).isoformat(), protocol="run_30min_tt",
                            inputs={"distance_m": 6800})
    performance.record_test(conn, date=TODAY.isoformat(), protocol="run_30min_tt", inputs={"distance_m": 7000})
    m = performance.performance_state(conn, TODAY)["metrics"]["threshold_pace_run_s_per_km"]
    assert m["change_pct"] < 0 and m["improved"] is True
