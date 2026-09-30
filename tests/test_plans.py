from datetime import date, timedelta

import pytest

from bulltraining import generator, performance, plans, workouts
from bulltraining.periodization import week_context, week_effective, week_targets
from bulltraining.util import monday_of

from .conftest import TODAY, seed_history

MONDAY = date(2026, 9, 28)


def _all_tests_valid(conn):
    d = (TODAY - timedelta(days=7)).isoformat()
    performance.record_test(conn, date=d, protocol="ride_ftp20", inputs={"avg_power_20min_w": 260})
    performance.record_test(conn, date=d, protocol="run_30min_tt", inputs={"distance_m": 6600, "avg_hr_last_20min": 168})
    performance.record_test(conn, date=d, protocol="swim_css", inputs={"t400": "7:00", "t200": "3:20"})


def _plan(conn, **kw):
    args = dict(name="Test 70.3", goal_type="event", goal_kind="triathlon_70.3",
                goal_date=(TODAY + timedelta(weeks=20)).isoformat(), sports=["swim", "ride", "run"], weekly_hours=9,
                today=TODAY)
    args.update(kw)
    return plans.create_plan(conn, **args)["plan"]


def _week_load(sessions):
    return sum(s["target_load"] for s in sessions if s["sport"] in ("run", "ride", "swim"))


def test_missing_tests_are_scheduled_first_without_hard_sessions(conn):
    seed_history(conn)
    plan = _plan(conn)
    g = generator.generate_week(conn, plan, MONDAY, [], TODAY)
    tests = {s["sport"] for s in g["sessions"] if s["category"] == "TEST"}
    assert "ride" in tests and len(tests) <= 2  # größte Sportart zuerst, gestaffelt
    assert any("folgt nächste Woche" in w for w in g["warnings"])
    assert not [s for s in g["sessions"] if s["intensity"] in ("threshold", "vo2")]
    state = list(g["sessions"])
    for k in (1, 2):  # spätestens nach drei Wochen sind alle Tests eingeplant
        nxt = generator.generate_week(conn, plan, MONDAY + timedelta(weeks=k), state, TODAY)
        state += nxt["sessions"]
    assert {s["sport"] for s in state if s["category"] == "TEST"} == {"swim", "ride", "run"}


def test_generated_week_stays_in_corridor_and_follows_rules(conn):
    seed_history(conn)
    _all_tests_valid(conn)
    plan = _plan(conn)
    g = generator.generate_week(conn, plan, MONDAY, [], TODAY)
    corridor = g["targets"]["load_corridor"]
    load = _week_load(g["sessions"])
    assert corridor["lower"] * 0.97 <= load <= corridor["upper"] * 1.03
    days = {s["date"] for s in g["sessions"]}
    assert len(days) < 7  # Ruhetag
    hard = sorted(date.fromisoformat(s["date"]) for s in g["sessions"] if s["intensity"] in workouts.HARD)
    assert all((b - a).days > 1 for a, b in zip(hard, hard[1:]))
    assert any(s["intensity"] in ("tempo", "threshold") for s in g["sessions"])  # mit gültigem Test


def test_plan_starts_at_current_volume_not_at_target(conn):
    seed_history(conn, hours_scale=0.5)  # ~3 h/Woche
    _all_tests_valid(conn)
    plan = _plan(conn, weekly_hours=12)
    t = week_targets(conn, plan, MONDAY, [], TODAY)
    assert t["target_hours"] < 3.0 * 1.15
    readiness = plans.plan_readiness(conn, plan, TODAY)
    assert readiness["ramp_weeks"] and readiness["ramp_weeks"] > 5


def test_plan_steps_down_gently_when_above_target(conn):
    seed_history(conn, hours_scale=1.6)  # ~9.5 h/Woche
    _all_tests_valid(conn)
    plan = _plan(conn, weekly_hours=5)
    t = week_targets(conn, plan, MONDAY, [], TODAY)
    assert t["target_hours"] >= 0.85 * t["reference"]["hours"]  # kein Sprung nach unten
    assert t["load_corridor"]["lower"] > 0


def test_propose_apply_and_revert(conn):
    seed_history(conn)
    _all_tests_valid(conn)
    _plan(conn)
    res = plans.propose_plan_change(conn, [{"op": "regenerate_week", "week": "2026-W40"}],
                                    "Erste Woche aus Vorlage erzeugen", today=TODAY)
    assert res["status"] == "pending"
    assert not plans.get_sessions(conn, plans.get_active_plan(conn)["id"])  # noch nichts angewendet
    plans.apply_change(conn, res["change_id"], today=TODAY)
    sessions = plans.get_sessions(conn, plans.get_active_plan(conn)["id"])
    assert sessions and all(s["status"] == "planned" for s in sessions)
    target = next(s for s in sessions if s["intensity"] == "easy")
    ch = plans.propose_plan_change(conn, [{"op": "change_duration", "session_id": target["id"], "duration_min": 30}],
                                   "HRV unter Baseline, Umfang reduzieren", today=TODAY)
    plans.apply_change(conn, ch["change_id"], today=TODAY)
    got = plans._current(conn, target["id"])["duration_s"]
    # Schwimmen wird in Metern (50-m-Raster) geplant und trifft die Dauer nur ungefähr
    assert got == 1800 if target["sport"] != "swim" else abs(got - 1800) <= 120
    plans.revert_change(conn, ch["change_id"], today=TODAY)
    assert plans._current(conn, target["id"])["duration_s"] == target["duration_s"]
    plans.revert_change(conn, res["change_id"], today=TODAY)
    assert not plans.get_sessions(conn, plans.get_active_plan(conn)["id"])


def test_overload_is_rejected_with_reason(conn):
    seed_history(conn)
    _all_tests_valid(conn)
    _plan(conn)
    heavy = [{"date": (MONDAY + timedelta(days=i)).isoformat(), "sport": "ride", "intensity": "easy", "duration_min": 200}
             for i in range(1, 7)]
    with pytest.raises(plans.PlanError) as exc:
        plans.propose_plan_change(conn, [{"op": "regenerate_week", "week": "2026-W40", "sessions": heavy}],
                                  "Viel hilft viel", today=TODAY)
    text = str(exc.value)
    assert "Obergrenze" in text and "Grenze von" in text  # Wochenlast und lange Einheit


def test_hard_days_and_rest_day_rules(conn):
    seed_history(conn)
    _all_tests_valid(conn)
    _plan(conn)
    week = [{"date": (MONDAY + timedelta(days=i)).isoformat(), "sport": "run", "intensity": "threshold" if i in (1, 2) else "easy",
             "duration_min": 30} for i in range(7)]
    with pytest.raises(plans.PlanError) as exc:
        plans.propose_plan_change(conn, [{"op": "regenerate_week", "week": "2026-W40", "sessions": week}],
                                  "Test der Regeln", today=TODAY)
    assert "in Folge" in str(exc.value) and "freier Tag" in str(exc.value)


def test_past_and_taper_locked(conn):
    seed_history(conn)
    _all_tests_valid(conn)
    plan = _plan(conn, goal_date=(TODAY + timedelta(weeks=3)).isoformat())
    with pytest.raises(plans.PlanError, match="Vergangenheit"):
        plans.propose_plan_change(conn, [{"op": "schedule_test", "date": (TODAY - timedelta(days=2)).isoformat(),
                                          "sport": "run"}], "Test nachtragen", today=TODAY)
    ctx = week_context(plan, monday_of(date.fromisoformat(plan["goal_date"])) - timedelta(weeks=1))
    assert ctx["week_type"] == "taper"
    with pytest.raises(plans.PlanError, match="Taper"):
        plans.propose_plan_change(conn, [{"op": "add_race", "date": (TODAY + timedelta(days=16)).isoformat(),
                                          "sport": "run", "priority": "C", "duration_min": 40}],
                                  "Stadtlauf als Tempoeinheit", today=TODAY)


def test_underload_warning(conn):
    seed_history(conn)
    _all_tests_valid(conn)
    _plan(conn)
    light = [{"date": (MONDAY + timedelta(days=2)).isoformat(), "sport": "run", "intensity": "easy", "duration_min": 30}]
    res = plans.propose_plan_change(conn, [{"op": "regenerate_week", "week": "2026-W40", "sessions": light}],
                                    "Bewusst locker nach Infekt", today=TODAY)
    assert any("Unterforderung" in w for w in res["warnings"])


def test_week_effective_uses_done_for_past_days(conn):
    seed_history(conn)
    eff = week_effective(conn, MONDAY - timedelta(weeks=1), [], TODAY)
    assert eff["done_load"] > 0 and eff["planned_load"] == 0


def test_propose_next_week_one_at_a_time(conn):
    seed_history(conn)
    _all_tests_valid(conn)
    _plan(conn)
    first = plans.propose_next_week(conn, today=TODAY)
    assert first["status"] == "pending"
    with pytest.raises(plans.PlanError, match="offene"):
        plans.propose_next_week(conn, today=TODAY)
