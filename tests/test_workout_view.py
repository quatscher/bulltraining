from bulltraining import workouts
from bulltraining.db import set_setting
from bulltraining.performance import PROTOCOLS, evaluate, record_test
from bulltraining.workout_view import steps_as_text, workout_steps

from .conftest import TODAY


def _thresholds(conn):
    set_setting(conn, "ftp_w", 250)
    set_setting(conn, "threshold_pace_run_s_per_km", 270)  # 4:30/km
    set_setting(conn, "lthr_run", 170)
    set_setting(conn, "css_s_per_100m", 100)


def test_run_step_has_pace_and_hr(conn):
    _thresholds(conn)
    items = workout_steps(conn, "run", "- 10m 78-86% Pace")
    step = items[0]
    assert step["duration"] == "10 min" and step["word"] == "Grundlage"
    assert ("Pace", "5:14–5:46/km") in step["targets"]
    assert ("Puls ca.", "144–151 bpm") in step["targets"]  # Z2: 85–89 % von 170


def test_ride_repeat_block_in_watts(conn):
    _thresholds(conn)
    items = workout_steps(conn, "ride", "Aufwärmen\n- 15m 50-65%\n\n4x\n- 10m 95-102%\n- 4m 45-55%\n\n- 10m 45-55%")
    assert [i["type"] for i in items] == ["text", "step", "repeat", "step"]
    rep = items[2]
    assert rep["count"] == 4 and rep["duration_s"] == 4 * 14 * 60
    assert rep["steps"][0]["targets"][0] == ("Leistung", "238–255 W")


def test_hr_step_gets_approximate_pace(conn):
    _thresholds(conn)
    step = workout_steps(conn, "run", "- 15m Z1-Z2 HR")[0]
    labels = dict(step["targets"])
    assert labels["Puls"] == "128–151 bpm" and labels["Pace ca."].endswith("/km")


def test_without_tests_falls_back_to_percent_and_zone(conn):
    step = workout_steps(conn, "ride", "- 20m 95-102%")[0]
    assert step["targets"] == [("Leistung", "95–102 % FTP")]
    step = workout_steps(conn, "run", "- 20m Z2 HR")[0]
    assert ("Puls", "Zone Z2") in step["targets"]


def test_swim_distance_and_max(conn):
    _thresholds(conn)
    items = workout_steps(conn, "swim", "- 400mtr max\n- 5m rest")
    assert items[0]["duration"] == "400 m" and items[0]["word"] == "maximal" and items[0]["estimated"]
    assert items[1]["word"] == "Pause"


def test_generated_note_hidden_and_1x_flattened(conn):
    record_test(conn, date=TODAY.isoformat(), protocol="ride_ftp20", inputs={"avg_power_20min_w": 263, "avg_hr_20min": 165})
    built = workouts.build(conn, "ride", "tempo", 45)  # 1x-Block mit "Ziel ... (FTP ...)"-Zeile
    items = workout_steps(conn, "ride", built["description"])
    assert not any(i["type"] == "text" and i["text"].startswith("Ziel") for i in items)
    assert not any(i["type"] == "repeat" for i in items)
    assert any(k == "Puls ca." for s in items if s["type"] == "step" for k, _ in s["targets"])  # lthr_ride aus Test


def test_ftp_protocol_repeats_only_openers():
    desc = PROTOCOLS["ride_ftp20"]["description"]
    block = desc.split("3x\n", 1)[1].split("\n\n", 1)[0]
    assert "5m 105%" not in block  # nur 3× (1 min hart / 1 min locker), nicht die 5-min-Vorbelastung
    assert evaluate("ride_ftp20", {"avg_power_20min_w": 300, "avg_hr_20min": 170})["lthr_ride"] == 162


def test_text_form_for_llm(conn):
    _thresholds(conn)
    text = steps_as_text(workout_steps(conn, "run", "Einlaufen\n- 10m 78-86% Pace\n\n3x\n- 5m 97-102% Pace\n- 2m Z1 HR"))
    assert text[0] == "# Einlaufen"
    assert text[1].startswith("10 min Grundlage – Pace 5:14–5:46/km")
    assert text[2].startswith("3× [5 min Schwelle")
