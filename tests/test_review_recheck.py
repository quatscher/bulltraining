"""Regressionstests zur Nachprüfung vom 27.09.2026 (review/FIX_REVIEW_2026-09-27.md, review/recheck_fixes.py)."""
import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from bulltraining import activities, db, performance, plans, publisher, workout_syntax
from bulltraining.db import connect, set_setting
from bulltraining.workout_view import steps_as_text, workout_steps

from .conftest import TODAY, seed_history
from .test_review_regressions import MONDAY, Remote, custom, propose, session


@pytest.fixture
def planned(tmp_path):
    c = connect(tmp_path / "r.db")
    seed_history(c)
    p = plans.create_plan(c, name="Review", goal_type="continuous", sports=["run", "ride", "swim"],
                          weekly_hours=8, today=TODAY)["plan"]
    yield c, p
    c.close()


# --- 1: Publisher schreibt nur gegen seine Revision ---------------------------------------------

def test_change_during_upload_is_synced_in_same_run(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session(minutes=45)]), today=TODAY)
    sid = plans.get_sessions(c, p["id"])[0]["id"]
    change = propose(c, [dict(op="change_duration", session_id=sid, duration_min=30)])

    class EditDuringUpload(Remote):
        def create_event(self, payload):
            response = super().create_event(payload)
            plans.apply_change(c, change, today=TODAY)  # zweite Anfrage, während POST unterwegs ist
            return response

    remote = EditDuringUpload()
    result = publisher.publish(c, remote, today=TODAY)
    s = plans._current(c, sid)
    assert len(remote.events) == 1 and remote.events[s["external_event_id"]]["moving_time"] == 1800
    assert s["status"] == "published" and result["requeued"] == 1


def test_undo_during_remote_delete_keeps_session(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session()]), today=TODAY)
    sid = plans.get_sessions(c, p["id"])[0]["id"]
    remote = Remote()
    publisher.publish(c, remote, today=TODAY)
    deletion = propose(c, [dict(op="delete_session", session_id=sid)])
    plans.apply_change(c, deletion, today=TODAY)

    class UndoDuringDelete(Remote):
        def delete_event(self, eid):
            super().delete_event(eid)
            plans.revert_change(c, deletion, today=TODAY)

    remote.__class__ = UndoDuringDelete
    publisher.publish(c, remote, today=TODAY)
    s = plans._current(c, sid)
    assert s is not None and s["status"] == "published"
    assert list(remote.events) == [s["external_event_id"]]  # neu angelegt, altes Event gelöscht


def test_session_deleted_during_first_upload_leaves_no_orphan(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session()]), today=TODAY)
    sid = plans.get_sessions(c, p["id"])[0]["id"]
    deletion = propose(c, [dict(op="delete_session", session_id=sid)])

    class DeleteDuringUpload(Remote):
        def create_event(self, payload):
            response = super().create_event(payload)
            plans.apply_change(c, deletion, today=TODAY)  # unveröffentlicht -> physisch gelöscht
            return response

    remote = DeleteDuringUpload()
    publisher.publish(c, remote, today=TODAY)
    assert plans._current(c, sid) is None and not remote.events


# --- 3: unklare POST-Antwort ------------------------------------------------------------------

class LostResponse(Remote):
    def create_event(self, payload):
        response = super().create_event(payload)
        if len(self.events) == 1:
            raise TimeoutError("Antwort nach dem Anlegen verloren")
        return response


def test_lost_post_response_is_held_without_lookup(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session()]), today=TODAY)
    remote = LostResponse()
    first = publisher.publish(c, remote, today=TODAY)
    second = publisher.publish(c, remote, today=TODAY)
    assert first["errors"] and second["held"] == 1 and len(remote.events) == 1
    s = plans.get_sessions(c, p["id"])[0]
    assert s["publish_unknown"] == 1 and s["status"] == "planned"


def test_lost_post_response_is_matched_with_lookup(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session()]), today=TODAY)

    class Searchable(LostResponse):
        def find_event(self, day, name, category):
            return next(({"id": k, **v} for k, v in self.events.items()
                         if v["name"] == name and v["start_date_local"].startswith(day)), None)

    remote = Searchable()
    publisher.publish(c, remote, today=TODAY)
    second = publisher.publish(c, remote, today=TODAY)
    s = plans.get_sessions(c, p["id"])[0]
    assert len(remote.events) == 1 and s["status"] == "published" and s["external_event_id"] == "1"
    assert second["created"] == 0 and second["updated"] == 1


def test_definite_rejection_is_not_marked_unclear(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session()]), today=TODAY)

    class Rejecting(Remote):
        def create_event(self, payload):
            raise RuntimeError("POST /events -> HTTP 422: invalid")

    publisher.publish(c, Rejecting(), today=TODAY)
    assert plans.get_sessions(c, p["id"])[0]["publish_unknown"] == 0


# --- 2: strikter Parser -------------------------------------------------------------------------

def test_absolute_watts_are_rated_against_ftp(planned):
    c, _ = planned
    set_setting(c, "ftp_w", 250)
    ch = custom(c, [dict(date=MONDAY.isoformat(), sport="ride", intensity="easy", duration_min=30,
                         description="- 30m 400w")])
    after = plans.get_change(c, ch)
    s = after["diff"]["after"][0]
    assert s["intensity"] == "vo2" and s["target_load"] > 50
    assert any("härter" in w for w in after["warnings"])


def test_watts_without_ftp_are_rejected(planned):
    c, _ = planned
    with pytest.raises(plans.PlanError, match="nicht unterstützte Vorgabe"):
        custom(c, [dict(date=MONDAY.isoformat(), sport="ride", duration_min=30, description="- 30m 200w")])


def test_labeled_repeat_counts_all_repetitions(planned):
    c, _ = planned
    desc = "Main set 6x\n- 5m 100%\n- 5m 50%"
    with pytest.raises(plans.PlanError, match="60 min"):
        custom(c, [dict(date=MONDAY.isoformat(), sport="ride", intensity="threshold", duration_min=10, description=desc)])
    ch = custom(c, [dict(date=MONDAY.isoformat(), sport="ride", intensity="threshold", duration_min=60, description=desc)])
    assert plans.get_change(c, ch)["diff"]["after"][0]["duration_s"] == 3600


@pytest.mark.parametrize("desc,sport", [
    ("- 20m sweetspot", "ride"),
    ("- 20m 80%", "run"),               # unklar: Pace, Puls oder Leistung?
    ("- zwanzig Minuten locker", "run"),
    ("3x\n\n- 5m Z2", "run"),            # Wiederholung ohne Schritte
])
def test_unsupported_syntax_is_rejected(planned, desc, sport):
    c, _ = planned
    with pytest.raises(plans.PlanError):
        custom(c, [dict(date=MONDAY.isoformat(), sport=sport, duration_min=20, description=desc)])


def test_supported_syntax_variants():
    items = workout_syntax.parse(
        "Warm-up\n- Einrollen 10m 55-65% 90rpm\nMain set 3x\n- 5m 250w\n- 2m rest\n\n- 5m Z2 Power",
        "ride", ftp=250, strict=True)
    secs = workout_syntax.zone_seconds(items)
    assert sum(secs) == (10 + 3 * 7 + 5) * 60
    assert secs[3] == 15 * 60  # 250 W bei FTP 250 -> Schwelle
    run = workout_syntax.parse("- 10m 4:30/km Pace\n- 5m 90% HR\n- 1km 5:00-5:10/km", "run", pace=270, strict=True)
    assert [s["target"]["kind"] for s in run] == ["pace_abs", "hr_pct", "pace_abs"]
    assert run[0]["target"]["zone"] == 4 - 1  # Schwellenpace = Z4


def test_view_shows_labels_and_absolute_targets(conn):
    set_setting(conn, "ftp_w", 250)
    text = steps_as_text(workout_steps(conn, "ride", "Main set 2x\n- Hart 5m 260-280w\n- 3m rest"))
    assert text[0].startswith("2× [5 min Hart · ")
    assert "Leistung 260–280 W" in text[0]


# --- 4: manuelle RPE gewinnt auch gegen Quell-RPE --------------------------------------------------

def test_manual_rpe_overrides_source_rpe_on_resync(conn):
    source = dict(id="rpe", type="Swim", start_date_local=TODAY.isoformat() + "T10:00:00", moving_time=3600, icu_rpe=4)
    activities.upsert_intervals_activity(conn, source)
    aid = conn.execute("SELECT id FROM activities").fetchone()["id"]
    assert activities.get_activity(conn, aid)["load"] == 240
    assert activities.update_activity(conn, aid, rpe=8)["load"] == 480
    activities.upsert_intervals_activity(conn, source)
    after = activities.get_activity(conn, aid)
    assert after["rpe"] == 8 and after["load"] == 480


def test_measured_load_is_not_replaced_by_rpe(conn):
    source = dict(id="hr", type="Run", start_date_local=TODAY.isoformat() + "T10:00:00", moving_time=3600,
                  icu_training_load=65, average_heartrate=150)
    activities.upsert_intervals_activity(conn, source)
    aid = conn.execute("SELECT id FROM activities").fetchone()["id"]
    activities.update_activity(conn, aid, rpe=9)
    activities.upsert_intervals_activity(conn, source)
    after = activities.get_activity(conn, aid)
    assert after["load"] == 65 and after["load_method"] == "hr"


# --- 5: 0 % Progression, Planerstellung atomar ------------------------------------------------------

def test_zero_progression_creates_plan_with_note(planned):
    c, old = planned
    import bulltraining.web.app as web
    web._conn = c
    try:
        client = TestClient(web.app, raise_server_exceptions=False)
        assert client.post("/settings", data={"max_weekly_load_increase_pct": "0"}, follow_redirects=False).status_code == 303
        r = client.post("/plans", data={"name": "Neu", "goal_type": "continuous", "sports": "run", "weekly_hours": "20"},
                        follow_redirects=False)
        assert r.status_code == 303 and "0 %" in r.headers["location"].replace("%25", "%").replace("+", " ")
        assert client.get("/plans").status_code == 200 and client.get("/performance").status_code == 200
    finally:
        web._conn = None


def test_failed_plan_creation_keeps_old_plan_active(planned, monkeypatch):
    c, old = planned
    monkeypatch.setattr(plans, "plan_readiness", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaputt")))
    with pytest.raises(RuntimeError):
        plans.create_plan(c, name="Neu", goal_type="continuous", sports=["run"], weekly_hours=5, today=TODAY)
    assert plans.get_active_plan(c)["id"] == old["id"]
    assert len(plans.list_plans(c)) == 1


# --- 6: Migration schützt historische IDs ---------------------------------------------------------------

def test_migration_protects_ids_from_change_history(tmp_path):
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT,", "INTEGER PRIMARY KEY,"))
    raw.close()
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    for k, v in db.config.DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    seed_history(c)
    p = plans.create_plan(c, name="Legacy", goal_type="continuous", sports=["run"], weekly_hours=4, today=TODAY)["plan"]
    plans.apply_change(c, custom(c, [session()]), today=TODAY)
    sid = plans.get_sessions(c, p["id"])[0]["id"]
    deletion = propose(c, [dict(op="delete_session", session_id=sid)])
    plans.apply_change(c, deletion, today=TODAY)
    c.close()
    c = connect(path)  # migriert
    plans.apply_change(c, custom(c, [session(MONDAY + timedelta(days=2), title="NEW")]), today=TODAY)
    new = [s for s in plans.get_sessions(c, p["id"]) if s["title"] == "NEW"][0]
    assert new["id"] > sid
    plans.revert_change(c, deletion, today=TODAY)
    assert plans._current(c, sid) is not None and plans._current(c, new["id"])["title"] == "NEW"


# --- 7: Stichtag ------------------------------------------------------------------------------------------

def test_historical_state_uses_tests_up_to_that_day(conn):
    performance.record_test(conn, date=(TODAY - timedelta(days=7)).isoformat(), protocol="ride_ftp20",
                            inputs={"avg_power_20min_w": 200}, today=TODAY)
    performance.record_test(conn, date=TODAY.isoformat(), protocol="ride_ftp20",
                            inputs={"avg_power_20min_w": 300}, today=TODAY)
    past = performance.performance_state(conn, TODAY - timedelta(days=1))["metrics"]["ftp_w"]
    assert past["value"] == 190 and past["manual_override"] is False


def test_invalid_stored_manual_baseline_is_ignored(conn):
    from bulltraining.metrics import training_baseline
    conn.execute("UPDATE settings SET value = ? WHERE key = 'manual_baseline'",
                 (json.dumps({"sports": {"run": {"hours_per_week": "viel"}}}),))
    assert training_baseline(conn, TODAY)["data_quality"] == "no_data"
