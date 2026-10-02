from datetime import timedelta

import pytest

from bulltraining import metrics, plans, workouts
from bulltraining.performance import PROTOCOLS, record_test
from bulltraining.zonemodel import reference_paces, activity_zone_secs, description_zone_secs, session_zone_secs

from .conftest import TODAY, add_activity


def mins(secs):
    return [round(s / 60) for s in secs]


def test_ride_intervals_with_repeats():
    desc = "Aufwärmen\n- 15m 50-65%\n\nZiel 238–255 W\n4x\n- 10m 95-102%\n- 4m 45-55%\n\nAusklang\n- 10m 45-55%"
    assert mins(description_zone_secs(desc, "ride")) == [26, 15, 0, 40, 0]


def test_run_pace_and_hr_zones():
    desc = "- 15m Z1-Z2 HR\n4x\n- 20s 110% Pace\n- 40s Z1 HR\n\n- 30m 100% Pace\n- 10m Z1 HR"
    assert description_zone_secs(desc, "run") == [15 * 60 + 160 + 600, 0, 0, 1800, 80]


def test_swim_distances_use_css():
    # 400 m bei 100 % CSS (100 s/100 m) = 400 s; "max" zählt als Z5
    secs = description_zone_secs("- 400mtr 100% Pace\n- 200mtr max", "swim", pace=100)
    assert secs[3] == 400 and secs[4] == pytest.approx(200 / 1.05, abs=1)


def test_all_test_protocols_parse():
    for key, spec in PROTOCOLS.items():
        secs = description_zone_secs(spec["description"], spec["sport"])
        assert secs and sum(secs) > 0, key


def test_generated_workouts_match_intensity(conn):
    record_test(conn, date=TODAY.isoformat(), protocol="ride_ftp20", inputs={"avg_power_20min_w": 260})
    thr = workouts.build(conn, "ride", "threshold", 75)
    paces = reference_paces(conn)  # wie im Programm: Watt-Vorgaben brauchen die FTP zur Zonenzuordnung
    assert "w" in thr["description"] and "%" not in thr["description"]
    secs = session_zone_secs({**thr, "sport": "ride"}, paces)
    assert sum(secs) == pytest.approx(75 * 60, abs=5)
    assert secs[3] == max(secs)  # Schwellenanteil dominiert
    easy = workouts.build(conn, "ride", "easy", 60)
    assert mins(session_zone_secs({**easy, "sport": "ride"}, paces)) == [0, 60, 0, 0, 0]


def test_fallback_without_description():
    s = {"sport": "run", "description": "freier Text ohne Schritte", "duration_s": 3000, "intensity": "tempo"}
    assert session_zone_secs(s, {}) == [0, 0, 3000, 0, 0]


def test_activity_zones_collapse_to_five():
    assert activity_zone_secs('{"kind":"hr","secs":[10,20,30,40,5,6,7]}') == [10, 20, 30, 40, 18]
    assert activity_zone_secs(None) is None


def test_timeline_planned_and_done(conn):
    plan = plans.create_plan(conn, name="P", goal_type="continuous", sports=["run"], weekly_hours=4, today=TODAY)["plan"]
    conn.execute("INSERT INTO plan_sessions(plan_id, date, sport, title, description, duration_s, target_load, intensity, "
                 "status, created_at, updated_at) VALUES (?,?,'run','x','- 40m 78-86% Pace\n- 10m 97-102% Pace',3000,40,"
                 "'easy','planned','x','x')", (plan["id"], TODAY.isoformat()))
    aid = add_activity(conn, TODAY, "run", 50, 40, ext="z1")
    conn.execute("UPDATE activities SET zone_times = ? WHERE id = ?", ('{"kind":"hr","secs":[600,1800,300,300,0]}', aid))
    add_activity(conn, TODAY, "swim", 30, 20, ext="z2")  # ohne Zonendaten
    day = metrics.zone_timeline(conn, TODAY, TODAY)[0]
    assert mins(day["planned"]) == [0, 40, 0, 10, 0]
    assert mins(day["done"]) == [10, 30, 5, 5, 0] and day["done_no_zones"] == 1800
    week = metrics.zone_weeks(conn, 0, 0, today=TODAY)[0]
    assert week["planned"] == [0, 40, 0, 10, 0] and week["done_without_zones"] == 30
    assert metrics.week_summary(conn, TODAY)["zones_min"]["done"] == [10, 30, 5, 5, 0]
    assert metrics.zone_timeline(conn, TODAY - timedelta(days=1), TODAY)[0]["planned"] == [0] * 5


def test_no_percent_targets_on_the_watch(conn):
    """%-Vorgaben rechnen intervals.icu und die Uhr gegen ihre eigene Schwelle – nur absolute Werte, Puls-Zonen
    oder Schritt-Typen dürfen in Beschreibungen stehen, die veröffentlicht werden."""
    for spec in PROTOCOLS.values():
        assert "%" not in spec["description"], spec["name"]
    record_test(conn, date=TODAY.isoformat(), protocol="ride_ftp20", inputs={"avg_power_20min_w": 260})
    record_test(conn, date=TODAY.isoformat(), protocol="run_30min_tt", inputs={"distance_m": 7000})
    record_test(conn, date=TODAY.isoformat(), protocol="swim_css", inputs={"t400": "8:00", "t200": "3:50"})
    for sport in ("ride", "run", "swim"):
        for intensity in ("recovery", "easy", "long", "tempo", "threshold", "vo2"):
            b = workouts.build(conn, sport, intensity, 60)
            assert "%" not in b["description"], (sport, intensity, b["description"])
