import json
from datetime import timedelta

from bulltraining import generator, plans
from bulltraining.db import set_setting
from bulltraining.metrics import training_baseline
from bulltraining.periodization import sport_shares, week_targets

from .conftest import TODAY

MONDAY = TODAY + timedelta(days=1)
MANUAL = {"as_of": TODAY.isoformat(), "load_per_hour": 50,
          "sports": {"run": {"hours_per_week": 3.4, "sessions_per_week": 3.5, "longest_min": 120},
                     "ride": {"hours_per_week": 2.0, "sessions_per_week": 2, "longest_min": 60},
                     "strength": {"sessions_per_week": 2, "minutes": 50}}}
COMMUTE = [{"day": "thu", "sport": "ride", "duration_min": 60, "title": "Pendeln hin"},
           {"day": "thu", "sport": "ride", "duration_min": 60, "title": "Pendeln zurück"}]


def _plan(conn, **kw):
    set_setting(conn, "manual_baseline", json.dumps(MANUAL))
    args = dict(name="70.3", goal_type="event", goal_kind="triathlon_70.3", goal_date="2027-08-01",
                sports=["swim", "ride", "run", "strength"], weekly_hours=10, recurring=COMMUTE, today=TODAY)
    args.update(kw)
    return plans.create_plan(conn, **args)["plan"]


def test_manual_baseline_used_without_data(conn):
    set_setting(conn, "manual_baseline", json.dumps(MANUAL))
    b = training_baseline(conn, TODAY)
    assert b["data_quality"] == "manual"
    assert b["avg_endurance_hours"] == 5.4 and b["longest_min_by_sport"]["run"] == 120
    assert b["strength_sessions_per_week"] == 2


def test_reference_starts_at_stated_volume(conn):
    plan = _plan(conn)
    t = week_targets(conn, plan, MONDAY, [], TODAY)
    assert t["reference"]["load"] == 270  # 5.4 h * 50
    assert 5.4 < t["target_hours"] <= 5.4 * 1.1 + 0.01


def test_current_habit_kept_new_sport_introduced(conn):
    plan = _plan(conn)
    b = training_baseline(conn, TODAY)
    shares = sport_shares(plan, b, 5.9)
    hours = {s: v * 5.9 for s, v in shares.items()}
    assert hours["run"] > 3.0          # Läufer wird nicht auf 32 % gekürzt
    assert 0.5 < hours["swim"] < 1.0   # Schwimmen startet mit Einstiegsumfang
    peak = {s: v * 10 for s, v in sport_shares(plan, b, 10).items()}
    assert abs(peak["ride"] - 5.0) < 0.3 and abs(peak["run"] - 3.2) < 0.3  # Spitze = Vorlage 70.3


def test_recurring_commute_in_every_week(conn):
    plan = _plan(conn)
    g = generator.generate_week(conn, plan, MONDAY, [], TODAY)
    thu = [s for s in g["sessions"] if s["date"] == (MONDAY + timedelta(days=3)).isoformat()]
    assert [s["title"] for s in thu if s["sport"] == "ride"] == ["Pendeln hin", "Pendeln zurück"]
    assert g["rest_day"] != thu[0]["date"]
    res = plans.propose_plan_change(conn, [{"op": "regenerate_week", "week": MONDAY.isoformat()}],
                                    "erste Woche aus Selbstauskunft", today=TODAY)
    assert res["status"] == "pending"


def test_strength_uses_configured_routine(conn):
    set_setting(conn, "strength_description", "Klimmzüge 3x, Liegestütze 3x")
    set_setting(conn, "strength_minutes", 50)
    plan = _plan(conn)
    g = generator.generate_week(conn, plan, MONDAY, [], TODAY)
    strength = [s for s in g["sessions"] if s["sport"] == "strength"]
    assert len(strength) == 2 and strength[0]["duration_s"] == 3000
    assert strength[0]["description"].startswith("Klimmzüge")


def test_stated_weeks_still_count_after_first_real_week(conn):
    """Regression 2026-10-06: Nach dem Datenneustart lag eine Krafteinheit am Tag der Selbstauskunft vor, danach eine
    echte Woche. Der Code hielt die Daten für „ok“, zählte die Wochen davor als 0 h und kappte die Woche auf Last 91."""
    from .conftest import add_activity
    plan = _plan(conn)
    add_activity(conn, TODAY, "strength", 50, 40, source="local", is_endurance=0, method="srpe", rpe=6)
    for d, sport, mins, load in ((1, "run", 115, 115), (3, "swim", 44, 18), (4, "ride", 75, 55), (4, "ride", 75, 52),
                                 (5, "run", 35, 40)):
        add_activity(conn, MONDAY + timedelta(days=d - 1), sport, mins, load, ext=f"x{d}{sport}{mins}{load}")
    later = MONDAY + timedelta(weeks=1)
    b = training_baseline(conn, later + timedelta(days=1))
    assert b["data_quality"] == "mixed"
    assert 4.5 < b["avg_endurance_hours"] < 6.5  # drei Wochen Selbstauskunft + eine echte Woche
    assert b["acwr_last_week"] < 1.3
    t = week_targets(conn, plan, later, [], later + timedelta(days=1))
    assert t["load_corridor"]["upper"] > 250 and t["target_hours"] > 5
