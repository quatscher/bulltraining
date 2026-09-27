import json
from datetime import date, timedelta

import httpx

from bulltraining import duplicates
from bulltraining.activities import log_activity, update_activity
from bulltraining.intervals_client import IntervalsClient
from bulltraining.publisher import event_payload, publish, reconcile
from bulltraining.sync import run_sync

from .conftest import TODAY


def _icu(day: date, i: int = 1, **kw):
    a = {"id": f"i{i}", "start_date_local": f"{day.isoformat()}T07:00:00", "type": "Run", "name": "Lauf",
         "moving_time": 3000, "distance": 10000, "average_heartrate": 140, "max_heartrate": 170,
         "icu_training_load": 55, "icu_hr_zone_times": [600, 1800, 400, 200, 0]}
    a.update(kw)
    return a


def _client(acts, wellness=(), calls=None):
    calls = calls if calls is not None else []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path, request.headers.get("user-agent")))
        path = request.url.path
        if path.endswith("/activities"):
            return httpx.Response(200, json=acts)
        if "/activity/" in path:
            aid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json=next(a for a in acts if a["id"] == aid))
        if path.endswith("/wellness"):
            return httpx.Response(200, json=list(wellness))
        if path.endswith("/events") and request.method == "POST":
            return httpx.Response(200, json={"id": 777, **json.loads(request.content)})
        return httpx.Response(404)
    return IntervalsClient(api_key="k", transport=httpx.MockTransport(handler)), calls


def test_sync_upsert_wellness_and_local_rows_untouched(conn):
    local = log_activity(conn, date=TODAY.isoformat(), sport="strength", duration_min=45, rpe=6)
    client, calls = _client([_icu(TODAY)], [{"id": TODAY.isoformat(), "hrv": 60, "restingHR": 48, "sleepSecs": 27000,
                                             "ctl": 40, "atl": 45}])
    res = run_sync(conn, client, today=TODAY)
    assert res["created"] == 1
    assert "Mozilla" in calls[0][2]  # Cloudflare: browserähnlicher User-Agent
    row = conn.execute("SELECT * FROM activities WHERE external_id='i1'").fetchone()
    assert row["load_method"] == "hr" and json.loads(row["zone_times"])["kind"] == "hr"
    w = conn.execute("SELECT * FROM wellness").fetchone()
    assert w["sleep_h"] == 7.5 and w["ctl_icu"] == 40
    res2 = run_sync(conn, _client([_icu(TODAY, moving_time=3100)])[0], today=TODAY)
    assert res2["updated"] == 1
    assert conn.execute("SELECT count(*) AS n FROM activities WHERE source='local'").fetchone()["n"] == 1
    assert conn.execute("SELECT id FROM activities WHERE id = ?", (local["id"],)).fetchone()


def test_swim_without_hr_falls_back_to_srpe(conn):
    run_sync(conn, _client([_icu(TODAY, type="Swim", icu_training_load=None, average_heartrate=None, icu_rpe=6)])[0],
             today=TODAY)
    row = conn.execute("SELECT load, load_method FROM activities").fetchone()
    assert row["load_method"] == "srpe" and row["load"] == 300.0


def test_duplicates_marked_and_resolution_survives_resync(conn):
    local = log_activity(conn, date=TODAY.isoformat(), sport="run", duration_min=55, rpe=5)
    acts = [_icu(TODAY)]
    run_sync(conn, _client(acts)[0], today=TODAY)
    pairs = duplicates.open_pairs(conn)
    assert len(pairs) == 1 and pairs[0]["local_id"] == local["id"]
    duplicates.resolve(conn, local["id"], "keep_local")
    ext = conn.execute("SELECT * FROM activities WHERE external_id='i1'").fetchone()
    assert ext["excluded"] == 1 and ext["is_endurance"] == 0
    run_sync(conn, _client(acts)[0], today=TODAY)
    ext = conn.execute("SELECT * FROM activities WHERE external_id='i1'").fetchone()
    assert ext["excluded"] == 1 and ext["is_endurance"] == 0  # Resync dreht die Entscheidung nicht zurück


def test_not_duplicate_is_remembered(conn):
    local = log_activity(conn, date=TODAY.isoformat(), sport="run", duration_min=50, rpe=5)
    run_sync(conn, _client([_icu(TODAY)])[0], today=TODAY)
    duplicates.resolve(conn, local["id"], "not_duplicate")
    run_sync(conn, _client([_icu(TODAY)])[0], today=TODAY)
    assert not duplicates.open_pairs(conn)


def test_rpe_is_mandatory_for_local(conn):
    import pytest
    from bulltraining.activities import ActivityError
    with pytest.raises(ActivityError, match="nachfragen"):
        log_activity(conn, date=TODAY.isoformat(), sport="strength", duration_min=45, rpe=None)


def test_external_measurements_not_editable(conn):
    import pytest
    from bulltraining.activities import ActivityError
    run_sync(conn, _client([_icu(TODAY)])[0], today=TODAY)
    aid = conn.execute("SELECT id FROM activities").fetchone()["id"]
    with pytest.raises(ActivityError):
        update_activity(conn, aid, duration_s=100)
    assert update_activity(conn, aid, rpe=7)["rpe"] == 7


def test_publish_and_reconcile(conn):
    from bulltraining import plans
    plan = plans.create_plan(conn, name="P", goal_type="continuous", sports=["run"], weekly_hours=4, today=TODAY)["plan"]
    tomorrow = TODAY + timedelta(days=1)
    conn.execute("INSERT INTO plan_sessions(plan_id, date, sport, title, description, duration_s, target_load, intensity, "
                 "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, 'planned', 'x', 'x')",
                 (plan["id"], tomorrow.isoformat(), "run", "Lauf", "- 50m Z2 HR", 3000, 35, "easy"))
    client, calls = _client([])
    res = publish(conn, client, today=TODAY)
    assert res["created"] == 1
    s = conn.execute("SELECT * FROM plan_sessions").fetchone()
    assert s["status"] == "published" and s["external_event_id"] == "777"
    payload = event_payload(dict(s))
    assert payload["start_date_local"].endswith("T00:00:00") and payload["type"] == "Run"
    run_sync(conn, _client([_icu(tomorrow, i=5)])[0], today=tomorrow)
    r = reconcile(conn, today=tomorrow + timedelta(days=1))
    assert r["done"] == 1
