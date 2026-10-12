from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from bulltraining import absences, generator, plans
from bulltraining.periodization import week_targets

from .conftest import TODAY, add_activity, seed_history

COMMUTE = [{"day": "thu", "sport": "ride", "duration_min": 60, "title": "Pendeln hin"},
           {"day": "thu", "sport": "ride", "duration_min": 60, "title": "Pendeln zurück"}]
OP = TODAY + timedelta(days=5)                     # Freitag
RUN_RIDE = OP + timedelta(days=14)                  # zwei Wochen danach
SWIM_STRENGTH = OP + timedelta(days=28)


def _setup(conn):
    seed_history(conn)
    plan = plans.create_plan(conn, name="70.3", goal_type="event", goal_kind="triathlon_70.3", goal_date="2027-08-01",
                             sports=["swim", "ride", "run", "strength"], weekly_hours=8, recurring=COMMUTE,
                             today=TODAY)["plan"]
    absences.create(conn, "OP Nase", OP.isoformat(),
                    {"run": RUN_RIDE.isoformat(), "ride": RUN_RIDE.isoformat(),
                     "swim": SWIM_STRENGTH.isoformat(), "strength": SWIM_STRENGTH.isoformat()})
    return plan


def _monday(d):
    return d - timedelta(days=d.weekday())


def test_nothing_planned_while_blocked(conn):
    plan = _setup(conn)
    op_week = generator.generate_week(conn, plan, _monday(OP), [], TODAY)
    assert all(date.fromisoformat(s["date"]) < OP for s in op_week["sessions"])  # ab OP-Tag nichts
    week_after = generator.generate_week(conn, plan, _monday(OP) + timedelta(weeks=1), [], TODAY)
    assert week_after["sessions"] == []  # auch kein Pendeln
    assert any("pausiert" in w for w in week_after["warnings"])


def test_comeback_only_allowed_sports_and_conservative(conn):
    plan = _setup(conn)
    before = week_targets(conn, plan, _monday(OP) - timedelta(weeks=1), [], TODAY)
    comeback = _monday(RUN_RIDE)
    # Pausenwochen zählen mit 0: simuliert, indem nach der OP keine Aktivitäten vorliegen
    gen = generator.generate_week(conn, plan, comeback, [], RUN_RIDE)
    sports = {s["sport"] for s in gen["sessions"]}
    assert sports and sports <= {"run", "ride"}
    assert gen["targets"]["load_corridor"]["upper"] < before["load_corridor"]["upper"] * 0.8


def test_rules_reject_session_in_absence(conn):
    plan = _setup(conn)
    res = plans.propose_plan_change(conn, [{"op": "regenerate_week", "week": _monday(OP).isoformat()}],
                                    "Woche der OP", today=TODAY)
    plans.apply_change(conn, res["change_id"], today=TODAY)
    s = next(x for x in plans.get_sessions(conn, plan["id"]) if date.fromisoformat(x["date"]) < OP
             and x["sport"] == "run")
    with pytest.raises(plans.PlanError, match="Ausfallzeit"):
        plans.propose_plan_change(conn, [{"op": "move_session", "session_id": s["id"],
                                          "to_date": (OP + timedelta(days=1)).isoformat()}],
                                  "Lauf in die Woche nach der OP schieben", today=TODAY)


def test_create_validates_and_web(conn):
    with pytest.raises(absences.AbsenceError):
        absences.create(conn, "OP", "2026-10-30", {"run": "2026-10-20"})
    import bulltraining.web.app as app_module
    app_module._conn = conn
    try:
        client = TestClient(app_module.app)
        r = client.post("/absences", data={"reason": "OP Nase", "start_date": "2026-10-30",
                                           "resume__run": "2026-11-13", "resume__swim": "2026-11-27"},
                        follow_redirects=False)
        assert "msg=" in r.headers["location"]
        html = client.get("/plans").text
        assert "OP Nase" in html and "Laufen ab 13.11." in html and "Schwimmen ab 27.11." in html
        assert absences.blocked(conn, "ride", "2026-11-20")  # ohne eigenes Datum: bis zum Ende gesperrt
        assert not absences.blocked(conn, "run", "2026-11-13")
    finally:
        app_module._conn = None
