from datetime import timedelta

import pytest

from bulltraining import metrics
from bulltraining.activities import log_activity
from bulltraining.db import set_setting

from .conftest import TODAY, add_activity, seed_history


def test_ctl_uses_42_day_time_constant(conn):
    # Konstante Tageslast 100 über 42 Tage: CTL = 100 * (1 - (41/42)^42) ≈ 63.6 (nicht halflife-Verhalten)
    for i in range(42):
        add_activity(conn, TODAY - timedelta(days=41 - i), "run", 60, 100, ext=f"c{i}")
    state = metrics.form_state(conn, TODAY)
    assert state["ctl_endurance"] == pytest.approx(100 * (1 - (41 / 42) ** 42), abs=0.2)
    assert state["atl_endurance"] == pytest.approx(100 * (1 - (6 / 7) ** 42), abs=0.2)


def test_strength_only_in_total_curve_and_srpe_factor(conn):
    log_activity(conn, date=TODAY.isoformat(), sport="strength", duration_min=60, rpe=5)
    set_setting(conn, "srpe_factor", "0.5")
    curves = metrics.form_curves(conn, TODAY)
    assert curves["load_total"].iloc[-1] == pytest.approx(150)  # 5 * 60 * 0.5
    assert curves["load_endurance"].iloc[-1] == 0


def test_excluded_activity_not_counted(conn):
    aid = add_activity(conn, TODAY, "run", 60, 80, ext="e1")
    conn.execute("UPDATE activities SET excluded = 1 WHERE id = ?", (aid,))
    assert metrics.form_curves(conn, TODAY)["load_total"].iloc[-1] == 0


def test_training_baseline(conn):
    seed_history(conn, weeks=6)
    b = metrics.training_baseline(conn, TODAY)
    assert b["data_quality"] == "ok"
    assert 5.5 < b["avg_endurance_hours"] < 7.5
    assert b["longest_min_by_sport"]["ride"] == 150
    assert 0.8 < b["acwr_last_week"] < 1.2


def test_load_corridor_respects_acwr_and_ctl_ramp(conn):
    c = metrics.load_corridor(conn, reference_load=400, ctl=40, chronic_load=300)
    assert c["upper"] == pytest.approx(390)  # 1.3 * chronic < 1.1 * reference
    assert c["lower"] == pytest.approx(255)  # 85 % der chronischen Last
    low_ctl = metrics.load_corridor(conn, reference_load=400, ctl=10, chronic_load=400)
    assert low_ctl["upper"] < 400 * 1.1  # CTL-Ramp begrenzt


def test_week_summary_planned_vs_done(conn):
    seed_history(conn, weeks=1)
    s = metrics.week_summary(conn, TODAY)
    assert s["total"]["done_min"] > 0
    assert "run" in s["by_sport"]
