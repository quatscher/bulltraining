"""Regressionstests zum Code-Review vom 27.09.2026 (review/REVIEW_2026-09-27.md).

Jeder Test entspricht einem Szenario aus review/reproduce_review.py – dort ist das fehlerhafte Verhalten
dokumentiert, hier das gewünschte.
"""
import json
from datetime import date, timedelta
from threading import Barrier, Event, Lock, Thread

import pytest
from fastapi.testclient import TestClient

from bulltraining import activities, metrics, performance, plans, rules, workouts
from bulltraining.db import SettingError, connect, get_float, get_setting, set_setting, thread_connection, transaction
from bulltraining.publisher import publish, reconcile
from bulltraining.sync import run_sync
from bulltraining.zonemodel import description_zone_secs

from .conftest import TODAY, seed_history

MONDAY = TODAY + timedelta(days=1)


@pytest.fixture
def planned(tmp_path):
    c = connect(tmp_path / "r.db")
    seed_history(c)
    p = plans.create_plan(c, name="Review", goal_type="continuous", sports=["run", "ride", "swim"],
                          weekly_hours=8, today=TODAY)["plan"]
    yield c, p
    c.close()


def propose(c, ops):
    return plans.propose_plan_change(c, ops, "Reproduktion im Code Review", today=TODAY)["change_id"]


def custom(c, items, week=MONDAY):
    return propose(c, [dict(op="regenerate_week", week=week.isoformat(), sessions=items)])


def session(day=MONDAY, intensity="easy", minutes=45, **kw):
    return dict(date=day.isoformat(), sport="run", intensity=intensity, duration_min=minutes, **kw)


class Remote:
    def __init__(self):
        self.events, self.calls, self.seq = {}, [], 0

    def create_event(self, payload):
        self.seq += 1
        eid = str(self.seq)
        self.events[eid] = payload
        self.calls.append(("POST", eid))
        return dict(id=eid)

    def update_event(self, eid, payload):
        self.calls.append(("PUT", eid))
        if eid not in self.events:
            raise RuntimeError("404: event no longer exists")
        self.events[eid] = payload

    def delete_event(self, eid):
        self.calls.append(("DELETE", eid))
        self.events.pop(eid, None)


def web_client(c):
    import bulltraining.web.app as web
    web._conn = c
    return web, TestClient(web.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_web():
    yield
    import bulltraining.web.app as web
    web._conn = None


# --- 1: parallele Anfragen ---------------------------------------------------------------

def test_parallel_requests_have_separate_transactions(tmp_path):
    path = tmp_path / "t.db"
    connect(path)
    entered, saved = Event(), Event()
    errors = []

    def failing_request():
        c = thread_connection(path)
        try:
            with transaction(c):
                set_setting(c, "strength_minutes", 60)
                entered.set()
                saved.wait(1)  # B wartet auf die Schreibsperre von A – kein gemeinsamer Zustand
                raise ValueError("A scheitert")
        except ValueError:
            pass

    def successful_request():
        c = thread_connection(path)
        assert entered.wait(5)
        try:
            with transaction(c):
                set_setting(c, "strength_description", "B")
            saved.set()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    a, b = Thread(target=failing_request), Thread(target=successful_request)
    a.start(); b.start(); a.join(15); b.join(15)
    assert not errors
    check = connect(path)
    assert get_setting(check, "strength_description") == "B"   # B bleibt gespeichert
    assert get_setting(check, "strength_minutes") == "45"      # A ist vollständig zurückgerollt


def test_shared_connection_serializes_transactions(tmp_path):
    c = connect(tmp_path / "s.db")
    entered = Event()

    def failing():
        try:
            with transaction(c):
                set_setting(c, "strength_minutes", 60)
                entered.set()
                raise ValueError("A scheitert")
        except ValueError:
            pass

    def succeeding():
        assert entered.wait(5)
        with transaction(c):  # wartet, bis A zurückgerollt hat
            set_setting(c, "strength_description", "B")

    a, b = Thread(target=failing), Thread(target=succeeding)
    a.start(); b.start(); a.join(10); b.join(10)
    assert get_setting(c, "strength_description") == "B" and get_setting(c, "strength_minutes") == "45"


# --- 2: Bestätigen prüft erneut ----------------------------------------------------------

def test_second_overlapping_proposal_is_rejected_on_apply(planned):
    c, p = planned
    a = custom(c, [session(MONDAY, "threshold")])
    b = custom(c, [session(MONDAY + timedelta(days=1), "threshold")])
    plans.apply_change(c, a, today=TODAY)
    with pytest.raises(plans.PlanError, match="Planregeln"):
        plans.apply_change(c, b, today=TODAY)
    assert plans.get_change(c, b)["status"] == "pending"
    errors, _ = rules.validate_weeks(c, p, plans.get_sessions(c, p["id"]), {MONDAY}, TODAY)
    assert not errors


def test_pending_delete_does_not_remove_completed_session(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session()]), today=TODAY)
    sid = plans.get_sessions(c, p["id"])[0]["id"]
    change = propose(c, [dict(op="delete_session", session_id=sid)])
    c.execute("UPDATE plan_sessions SET status='done' WHERE id=?", (sid,))
    with pytest.raises(plans.PlanError, match="passt nicht mehr"):
        plans.apply_change(c, change, today=MONDAY)
    assert plans._current(c, sid)["status"] == "done"


def test_apply_requires_active_plan(planned):
    c, p = planned
    change = custom(c, [session()])
    c.execute("UPDATE plans SET status = 'archived' WHERE id = ?", (p["id"],))
    with pytest.raises(plans.PlanError, match="nicht mehr aktiv"):
        plans.apply_change(c, change, today=TODAY)


# --- 3/4: Undo ---------------------------------------------------------------------------

def test_undo_refuses_to_overwrite_later_change(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session(minutes=60)]), today=TODAY)
    sid = plans.get_sessions(c, p["id"])[0]["id"]
    a = propose(c, [dict(op="change_duration", session_id=sid, duration_min=45)])
    plans.apply_change(c, a, today=TODAY)
    b = propose(c, [dict(op="change_duration", session_id=sid, duration_min=30)])
    plans.apply_change(c, b, today=TODAY)
    with pytest.raises(plans.PlanError, match=f"#{b}"):
        plans.revert_change(c, a, today=TODAY)
    assert plans._current(c, sid)["duration_s"] == 30 * 60
    plans.revert_change(c, b, today=TODAY)  # in umgekehrter Reihenfolge geht es
    plans.revert_change(c, a, today=TODAY)
    assert plans._current(c, sid)["duration_s"] == 60 * 60


def test_session_ids_are_never_reused(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session()]), today=TODAY)
    sid = plans.get_sessions(c, p["id"])[0]["id"]
    deletion = propose(c, [dict(op="delete_session", session_id=sid)])
    plans.apply_change(c, deletion, today=TODAY)
    plans.apply_change(c, custom(c, [session(MONDAY + timedelta(days=2), title="UNRELATED")]), today=TODAY)
    other = [s for s in plans.get_sessions(c, p["id"]) if s["title"] == "UNRELATED"][0]
    assert other["id"] != sid
    plans.revert_change(c, deletion, today=TODAY)
    assert plans._current(c, other["id"])["title"] == "UNRELATED"
    assert plans._current(c, sid) is not None


def test_existing_db_is_migrated_to_autoincrement(tmp_path):
    import sqlite3
    from bulltraining import db
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT,  -- ids nie wiederverwenden: "
                                        "Diffs/Undo referenzieren sie", "INTEGER PRIMARY KEY,"))
    raw.close()
    c = connect(path)
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name='plan_sessions'").fetchone()[0]
    assert "AUTOINCREMENT" in sql


# --- 5: freie Beschreibungen ----------------------------------------------------------------

def test_custom_description_determines_duration_and_load(planned):
    c, p = planned
    with pytest.raises(plans.PlanError, match="180 min"):
        custom(c, [session(minutes=30, description="- 180m 120% Pace")])
    ch = custom(c, [session(minutes=40, intensity="easy",
                            description="- 10m 75% Pace\n3x\n- 5m 100% Pace\n- 2m 70% Pace\n\n- 9m 75% Pace")])
    after = plans.get_change(c, ch)
    s = after["diff"]["after"][0]
    assert s["duration_s"] == 40 * 60 and s["intensity"] == "threshold"
    assert s["target_load"] > workouts.estimate_load(40, "easy")
    assert any("härter" in w for w in after["warnings"])
    with pytest.raises(plans.PlanError, match="keine auswertbaren"):
        custom(c, [session(description="einfach locker laufen")])


# --- 6/7/8: Veröffentlichen ------------------------------------------------------------------

def test_moved_published_session_is_updated_beyond_horizon(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session()]), today=TODAY)
    sid = plans.get_sessions(c, p["id"])[0]["id"]
    remote = Remote()
    publish(c, remote, today=TODAY)
    new_day = MONDAY + timedelta(days=28)
    plans.apply_change(c, propose(c, [dict(op="move_session", session_id=sid, to_date=new_day.isoformat())]), today=TODAY)
    assert publish(c, remote, today=TODAY)["updated"] == 1
    assert next(iter(remote.events.values()))["start_date_local"].startswith(new_day.isoformat())


def test_undo_of_published_deletion_recreates_event(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session()]), today=TODAY)
    sid = plans.get_sessions(c, p["id"])[0]["id"]
    remote = Remote()
    publish(c, remote, today=TODAY)
    change = propose(c, [dict(op="delete_session", session_id=sid)])
    plans.apply_change(c, change, today=TODAY)
    publish(c, remote, today=TODAY)
    assert not remote.events
    plans.revert_change(c, change, today=TODAY)
    result = publish(c, remote, today=TODAY)
    assert not result["errors"] and result["created"] == 1 and len(remote.events) == 1


def test_parallel_publish_creates_one_event(tmp_path):
    path = tmp_path / "p.db"
    c = connect(path)
    seed_history(c)
    p = plans.create_plan(c, name="P", goal_type="continuous", sports=["run"], weekly_hours=4, today=TODAY)["plan"]
    plans.apply_change(c, custom(c, [session()]), today=TODAY)
    barrier, lock = Barrier(2), Lock()

    class SlowRemote(Remote):
        def create_event(self, payload):
            with lock:
                return super().create_event(payload)

    remote, results = SlowRemote(), []

    def run():
        barrier.wait(5)
        results.append(publish(thread_connection(path), remote, today=TODAY))

    threads = [Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert len(remote.events) == 1
    assert sum(r["created"] for r in results) == 1
    assert len(plans.get_sessions(c, p["id"])) == 1


def test_archiving_cancels_future_published_sessions(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session(), session(MONDAY + timedelta(days=2))]), today=TODAY)
    remote = Remote()
    publish(c, remote, today=TODAY)
    assert len(remote.events) == 2
    res = plans.archive_plan(c, p["id"], today=TODAY)
    assert res["cancelled_published"] == 2
    publish(c, remote, today=TODAY)
    assert not remote.events


# --- 9/10/11: Sync ----------------------------------------------------------------------------

def test_resync_keeps_load_from_manual_rpe(conn):
    a = dict(id="rpe", type="Swim", start_date_local=TODAY.isoformat() + "T10:00:00", moving_time=3600)
    activities.upsert_intervals_activity(conn, a)
    sid = conn.execute("SELECT id FROM activities").fetchone()["id"]
    assert activities.update_activity(conn, sid, rpe=6)["load"] == 360
    activities.upsert_intervals_activity(conn, a)
    after = activities.get_activity(conn, sid)
    assert after["load"] == 360 and after["load_method"] == "srpe" and after["rpe"] == 6


class _Source:
    rate_remaining = None

    def __init__(self, rows, detail=None):
        self.rows, self.detail = rows, detail

    def activities(self, *args):
        return list(self.rows)

    def activity(self, aid):
        return self.detail(aid) if self.detail else next(r for r in self.rows if r["id"] == aid)

    def wellness(self, *args):
        return []


def test_second_sync_keeps_interval_details(conn):
    summary = dict(id="detail", type="Run", start_date_local=TODAY.isoformat() + "T10:00:00", moving_time=3600)
    source = _Source([summary], lambda aid: dict(summary, icu_intervals=[dict(label="Interval", moving_time=300)]))
    run_sync(conn, source, today=TODAY)
    aid = conn.execute("SELECT id FROM activities").fetchone()["id"]
    run_sync(conn, source, today=TODAY)
    assert activities.get_activity(conn, aid)["intervals"]


def test_sync_removes_activities_deleted_at_source(conn):
    rows = [dict(id="gone", type="Run", start_date_local=TODAY.isoformat() + "T10:00:00", moving_time=3600,
                 icu_training_load=80),
            dict(id="stays", type="Run", start_date_local=TODAY.isoformat() + "T18:00:00", moving_time=1800,
                 icu_training_load=30)]
    source = _Source(rows)
    run_sync(conn, source, today=TODAY)
    local = activities.log_activity(conn, date=TODAY.isoformat(), sport="strength", duration_min=45, rpe=6)
    rows.pop(0)
    res = run_sync(conn, source, full=True, today=TODAY)
    assert res["removed"] == 1
    assert metrics.activities_frame(conn)["eff_load"].sum() == pytest.approx(30 + 270 * 0.2)
    assert activities.get_activity(conn, local["id"])  # lokale Zeilen bleiben


def test_empty_listing_does_not_wipe_local_mirror(conn):
    rows = [dict(id=f"a{i}", type="Run", start_date_local=(TODAY - timedelta(days=i)).isoformat() + "T10:00:00",
                 moving_time=1800, icu_training_load=30) for i in range(4)]
    run_sync(conn, _Source(rows), today=TODAY)
    run_sync(conn, _Source([]), full=True, today=TODAY)
    assert conn.execute("SELECT count(*) FROM activities").fetchone()[0] == 4


# --- 12: Wochengrenze ----------------------------------------------------------------------------

def test_hard_sunday_then_hard_monday_is_rejected(planned):
    c, p = planned
    sunday = MONDAY + timedelta(days=6)
    plans.apply_change(c, custom(c, [session(sunday, "threshold")]), today=TODAY)
    with pytest.raises(plans.PlanError, match="in Folge"):
        custom(c, [session(sunday + timedelta(days=1), "threshold")], week=sunday + timedelta(days=1))


# --- 13/14: Eingaben ------------------------------------------------------------------------------

def test_invalid_settings_are_rejected_atomically(planned):
    c, _ = planned
    _, client = web_client(c)
    r = client.post("/settings", data={"strength_minutes": "60", "baseline_weeks": "0"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert get_setting(c, "strength_minutes") == "45"  # nichts gespeichert
    assert client.get("/performance").status_code == 200
    with pytest.raises(SettingError):
        set_setting(c, "manual_baseline", json.dumps({"sports": {"run": {"hours_per_week": -3}}}))
    c.execute("UPDATE settings SET value = 'nan' WHERE key = 'baseline_weeks'")  # Altlast in der DB
    assert get_float(c, "baseline_weeks", 4) == 4
    assert client.get("/performance").status_code == 200


def test_activity_update_validates(conn):
    a = activities.log_activity(conn, date=TODAY.isoformat(), sport="run", duration_min=45, rpe=5)
    with pytest.raises(activities.ActivityError):
        activities.update_activity(conn, a["id"], duration_s=-3600)
    with pytest.raises(activities.ActivityError):
        activities.update_activity(conn, a["id"], start_date="2026-09-00")
    assert activities.update_activity(conn, a["id"], start_date="2026-09-26T07:30")["start_date"] == "2026-09-26T07:30:00"
    metrics.form_state(conn, TODAY)


# --- 15/16: Tests -----------------------------------------------------------------------------------

def test_deleting_test_resets_effective_threshold(conn):
    performance.record_test(conn, date=(TODAY - timedelta(days=7)).isoformat(), protocol="ride_ftp20",
                            inputs={"avg_power_20min_w": 200}, today=TODAY)
    b = performance.record_test(conn, date=TODAY.isoformat(), protocol="ride_ftp20",
                                inputs={"avg_power_20min_w": 300}, today=TODAY)
    performance.delete_test(conn, b["test_id"], today=TODAY)
    shown = performance.performance_state(conn, TODAY)["metrics"]["ftp_w"]
    assert shown["value"] == get_float(conn, "ftp_w") == 190
    assert shown["manual_override"] is False


def test_future_test_results_are_rejected(conn):
    with pytest.raises(performance.TestError, match="Zukunft"):
        performance.record_test(conn, date="2030-01-01", protocol="ride_ftp20", inputs={"avg_power_20min_w": 400},
                                today=TODAY)
    assert get_float(conn, "ftp_w") is None


# --- 17: HRV-Lücke -------------------------------------------------------------------------------------

def test_hrv_gap_does_not_break_form_page(conn):
    today = date.today()
    for i in range(10, 30):
        conn.execute("INSERT INTO wellness(date,hrv) VALUES (?,?)", ((today - timedelta(days=i)).isoformat(), 60 + i % 3))
    state = metrics.hrv_status(conn)
    assert state["available"] is False and state["deviation_sd"] is None
    _, client = web_client(conn)
    assert client.get("/form").status_code == 200


def test_hrv_days_below_counts_consecutive_calendar_days(conn):
    for i in range(1, 40):
        conn.execute("INSERT INTO wellness(date,hrv) VALUES (?,?)", ((TODAY - timedelta(days=i)).isoformat(), 70))
    for i in (1, 2, 4):  # Tag 3 fehlt -> nur 2 Tage in Folge
        conn.execute("UPDATE wellness SET hrv = 40 WHERE date = ?", ((TODAY - timedelta(days=i)).isoformat(),))
    conn.execute("DELETE FROM wellness WHERE date = ?", ((TODAY - timedelta(days=3)).isoformat(),))
    assert metrics.hrv_status(conn, TODAY)["days_below_baseline_in_row"] == 2


# --- 18/19: Zeitfenster und Dauern ----------------------------------------------------------------------

def test_tests_only_in_fitting_time_windows(tmp_path):
    c = connect(tmp_path / "a.db")
    seed_history(c)
    plans.create_plan(c, name="A", goal_type="continuous", sports=["ride"], weekly_hours=6, today=TODAY,
                      available_days={"ride": {"tue": 50, "thu": 50, "sat": 120}})
    ch = propose(c, [dict(op="regenerate_week", week=MONDAY.isoformat())])
    tests = [s for s in plans.get_change(c, ch)["diff"]["after"] if s["category"] == "TEST"]
    assert tests and all(date.fromisoformat(t["date"]).weekday() == 5 for t in tests)


def test_moving_session_to_unavailable_day_is_rejected(tmp_path):
    c = connect(tmp_path / "b.db")
    seed_history(c)
    p = plans.create_plan(c, name="B", goal_type="continuous", sports=["run"], weekly_hours=4, today=TODAY,
                          available_days={"run": ["tue", "thu", "sat"]})["plan"]
    plans.apply_change(c, custom(c, [session(MONDAY + timedelta(days=1))]), today=TODAY)
    sid = plans.get_sessions(c, p["id"])[0]["id"]
    with pytest.raises(plans.PlanError, match="nicht verfügbar"):
        propose(c, [dict(op="move_session", session_id=sid, to_date=(MONDAY + timedelta(days=2)).isoformat())])


def test_structured_durations_match_declared(conn):
    for key, spec in performance.PROTOCOLS.items():
        if any(u in spec["description"] for u in ("mtr", "km")):
            continue  # Distanzschritte sind nur geschätzt
        assert sum(description_zone_secs(spec["description"], spec["sport"])) == spec["duration_min"] * 60, key
    for sport in ("ride", "run", "swim"):
        for intensity in ("easy", "long", "tempo", "threshold", "vo2", "recovery"):
            for minutes in (20, 30, 35, 40, 45, 60, 75, 90):
                b = workouts.build(conn, sport, intensity, minutes)
                steps = description_zone_secs(b["description"], sport)
                assert sum(steps) == b["duration_s"], (sport, intensity, minutes)


# --- 20: späte Uploads ----------------------------------------------------------------------------

def test_late_activity_turns_skipped_into_done(planned):
    c, p = planned
    plans.apply_change(c, custom(c, [session()]), today=TODAY)
    reconcile(c, MONDAY + timedelta(days=2))
    assert plans.get_sessions(c, p["id"])[0]["status"] == "skipped"
    activities.log_activity(c, date=MONDAY.isoformat(), sport="run", duration_min=45, rpe=5)
    assert reconcile(c, MONDAY + timedelta(days=3))["done"] == 1
    assert plans.get_sessions(c, p["id"])[0]["status"] == "done"


# --- 21: fremde Ursprünge ---------------------------------------------------------------------------

def test_cross_origin_writes_are_rejected(conn):
    _, client = web_client(conn)
    r = client.post("/settings", data={"ftp_w": "500"}, headers={"Origin": "https://untrusted.example"},
                    follow_redirects=False)
    assert r.status_code == 403 and get_float(conn, "ftp_w") is None
    r = client.post("/settings", data={"ftp_w": "250"}, headers={"Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303 and get_float(conn, "ftp_w") == 250
