"""Wochengenerator: füllt eine Woche innerhalb der Wochenziele, Leistungstests und verfügbaren Tage.

Deterministisch. Das LLM ruft ihn über `propose_plan_change(regenerate_week)` auf oder übergibt eigene
Einheiten – beides läuft durch dieselben Regeln.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import workouts
from .db import get_float
from .metrics import training_baseline
from .performance import DEFAULT_PROTOCOL, test_status
from .periodization import load_template, plan_sports, sport_shares, week_effective, week_targets
from .util import ENDURANCE_SPORTS, WEEKDAYS, to_date, week_days, weekday_key

MIN_SESSION = {"swim": 30, "run": 30, "ride": 45, "other": 30}
KEY_MIN = {"swim": 50, "run": 60, "ride": 75, "other": 45}
RACE_MIN = {"triathlon_sprint": 80, "triathlon_olympic": 150, "triathlon_70.3": 330, "triathlon_full": 720,
            "marathon": 240, "half_marathon": 120, "10k": 55, "gran_fondo": 360, "swim_distance": 90}
LONG_DAY_PREF = {"ride": ["sat", "sun"], "run": ["sun", "sat"], "swim": ["sat", "sun", "wed"], "other": ["sat", "sun"]}


def availability(plan: dict[str, Any]) -> dict[str, dict[str, int | None]]:
    """Normalisiert available_days zu {sport: {tag: max_minuten|None}}.

    Akzeptiert {"run": ["tue","sat"]}, {"run": {"tue": 60}} oder tagweise {"tue": ["run","swim"]}.
    """
    sports = plan_sports(plan)
    raw = plan.get("available_days")
    raw = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    if raw and set(raw) <= set(WEEKDAYS):
        inverted: dict[str, dict[str, int | None]] = {}
        for day, entry in raw.items():
            items = entry.items() if isinstance(entry, dict) else ((s, None) for s in entry)
            for s, cap in items:
                inverted.setdefault(s, {})[day] = cap
        raw = inverted
    out: dict[str, dict[str, int | None]] = {}
    for s in sports:
        entry = raw.get(s)
        if entry is None:
            out[s] = {d: None for d in WEEKDAYS if d != "mon"}
        elif isinstance(entry, dict):
            out[s] = {d: (int(v) if v else None) for d, v in entry.items()}
        else:
            out[s] = {d: None for d in entry}
    return out


class _Week:
    """Belegung einer Woche während der Platzierung."""

    def __init__(self, monday: date, fixed: list[dict[str, Any]], today: date, max_hard: int):
        self.days = week_days(monday)
        self.today = today
        self.max_hard = max_hard
        self.slots: dict[date, list[dict[str, Any]]] = {d: [] for d in self.days}
        for s in fixed:
            self.slots[to_date(s["date"])].append(s)
        self.rest_day: date | None = None

    def is_hard(self, d: date) -> bool:
        return any(s.get("intensity") in workouts.HARD for s in self.slots.get(d, []))

    def hard_count(self) -> int:
        return sum(1 for d in self.days if self.is_hard(d))

    def minutes(self, d: date) -> float:
        return sum(s["duration_s"] for s in self.slots[d]) / 60

    def open_days(self) -> list[date]:
        return [d for d in self.days if d >= self.today and d != self.rest_day]

    def neighbours_hard(self, d: date) -> bool:
        return self.is_hard(d - timedelta(days=1)) or self.is_hard(d + timedelta(days=1))


def _choose_rest_day(week: _Week, avail: dict[str, dict[str, int | None]]) -> None:
    free_past = [d for d in week.days if d < week.today and not week.slots[d]]
    if free_past:
        return  # Ruhetag hat diese Woche schon stattgefunden
    candidates = [d for d in week.days if d >= week.today and not week.slots[d]]
    if not candidates:
        return
    def usage(d: date) -> tuple[int, int]:
        pref = {"mon": 0, "fri": 1}.get(weekday_key(d), 2)
        return (sum(1 for s in avail.values() if weekday_key(d) in s), pref)
    week.rest_day = min(candidates, key=usage)


def _place(week: _Week, req: dict[str, Any], avail: dict[str, dict[str, int | None]],
           prefer: list[str] | None = None) -> date | None:
    sport, hard = req["sport"], req["intensity"] in workouts.HARD
    options = []
    for d in week.open_days():
        key = weekday_key(d)
        sport_days = avail.get(sport, {})
        if key not in sport_days:
            continue
        if any(s["sport"] == sport for s in week.slots[d]) or len(week.slots[d]) >= 2:
            continue
        cap = sport_days[key]
        if cap is not None and cap < req["duration_s"] / 60 * 0.7:
            continue
        if hard and (week.is_hard(d) or week.neighbours_hard(d) or week.hard_count() >= week.max_hard):
            continue
        score = week.minutes(d)
        if prefer and key in prefer:
            score -= 1000 * (len(prefer) - prefer.index(key))
        if not hard and week.is_hard(d):
            score += 60  # harte Tage hart, lockere Tage locker
        if req["intensity"] == "tempo" and any(s.get("intensity") == "tempo" for s in week.slots[d]):
            score += 2500  # zwei Qualitätseinheiten nicht auf denselben Tag stapeln
        if req["intensity"] in ("long",) and week.is_hard(d - timedelta(days=1)):
            score += 30
        options.append((score, d, cap))
    if not options:
        return None
    _, d, cap = min(options, key=lambda o: (o[0], o[1]))
    if cap is not None and req["duration_s"] / 60 > cap:
        req["duration_s"] = cap * 60
    return d


def _needs_test(conn: sqlite3.Connection, sport: str, monday: date, ctx: dict[str, Any],
                state: list[dict[str, Any]], today: date) -> str | None:
    """Liefert das Protokoll, falls in dieser Woche ein Test für die Sportart fällig ist."""
    if ctx["week_type"] in ("taper", "race"):
        return None
    status = test_status(conn, sport, today=monday)
    scheduled = [s for s in state if s["sport"] == sport and s.get("category") == "TEST"
                 and s.get("status") not in ("deleted", "skipped")
                 and max(today, monday - timedelta(days=28)) <= to_date(s["date"]) < monday]
    if scheduled:
        return None
    protocol = status["protocol"] or DEFAULT_PROTOCOL[sport]
    if status["status"] in ("missing", "stale"):
        return protocol
    valid_until = date.fromisoformat(status["valid_until"])
    if ctx["week_type"] == "recovery" and valid_until < monday + timedelta(days=35):
        return protocol  # Retest am Ende der Entlastungswoche, bevor der Test abläuft
    return None


def _key_intensity(plan: dict[str, Any], ctx: dict[str, Any]) -> str | None:
    tpl = load_template(plan.get("goal_kind"))
    if ctx["week_type"] in ("recovery", "post", "race"):
        return None
    if ctx["week_type"] == "taper":
        return "threshold"
    if ctx["phase"] == "continuous":
        return tpl["continuous_focus_intensity"].get(plan.get("focus") or "aerobic", "tempo")
    return tpl["key_intensity"].get(ctx["phase"], "tempo")


def generate_week(conn: sqlite3.Connection, plan: dict[str, Any], monday: date,
                  state: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
    """Erzeugt Einheiten für die Woche ab `monday`. `state` = aktuelle Einheiten des Plans (ohne diese Woche
    zu ersetzen, das macht der Aufrufer). Gibt Einheiten, Wochenziele und Hinweise zurück."""
    today = today or date.today()
    targets = week_targets(conn, plan, monday, state, today)
    ctx = targets["context"]
    warnings: list[str] = list(targets["notes"])
    sunday = monday + timedelta(days=6)
    # Fix bleiben: vergangene/erledigte Einheiten und Nebenwettkämpfe dieser Woche
    fixed = [s for s in state if monday <= to_date(s["date"]) <= sunday and s.get("status") != "deleted"
             and (to_date(s["date"]) < today or s.get("status") in ("done", "skipped") or s.get("category") == "RACE")]
    week = _Week(monday, fixed, today, int(get_float(conn, "max_hard_days", 3)))
    avail = availability(plan)
    _choose_rest_day(week, avail)
    shares = sport_shares(plan)
    # Anteil der Woche, der noch planbar ist (bei laufender Woche)
    open_fraction = len([d for d in week.days if d >= today]) / 7
    fixed_hours = sum(s["duration_s"] for s in fixed if s["sport"] in ENDURANCE_SPORTS and to_date(s["date"]) >= today) / 3600
    plan_hours = max(0.0, targets["target_hours"] * open_fraction - fixed_hours)
    requests: list[dict[str, Any]] = []

    if ctx["week_type"] == "race":
        goal = to_date(plan["goal_date"])
        if not any(s.get("category") == "RACE" and to_date(s["date"]) == goal for s in fixed):
            main = max(shares, key=shares.get) if shares else "run"
            race_min = RACE_MIN.get(plan.get("goal_kind") or "", 120)
            requests.append({"sport": main, "intensity": "race", "duration_s": race_min * 60, "fixed_date": goal,
                             "title": f"Wettkampf: {plan['name']}", "category": "RACE", "race_priority": plan.get("priority") or "A"})
    key_intensity = _key_intensity(plan, ctx)
    baseline = training_baseline(conn, as_of=min(monday, today))
    for sport, share in shares.items():
        requests += _sport_requests(conn, plan, sport, plan_hours * 60 * share, ctx, targets, baseline,
                                    key_intensity, avail, state, monday, today, warnings)

    if "strength" in plan_sports(plan) and ctx["week_type"] != "race":
        habit = baseline["strength_sessions_per_week"]
        count = 1 if ctx["week_type"] in ("recovery", "taper") else max(1, min(2, round(habit) or 1))
        for _ in range(count):
            requests.append({"sport": "strength", "intensity": "easy", "duration_s": 45 * 60})

    # Platzierung: Wettkampf, Tests, lange Einheiten, Schlüsseleinheiten, locker, Kraft
    order = {"race": 0, "test": 1, "long": 2, "threshold": 3, "vo2": 3, "tempo": 4, "easy": 5, "recovery": 5}
    requests.sort(key=lambda r: (r["sport"] == "strength", order.get(r["intensity"], 6), -r["duration_s"]))
    placed: list[dict[str, Any]] = []
    for req in requests:
        if req.get("fixed_date"):
            d = req["fixed_date"]
        elif req["sport"] == "strength":
            prefer = [weekday_key(x) for x in week.days if week.is_hard(x)]
            d = _place(week, req, {**avail, "strength": avail.get("strength") or {k: None for k in WEEKDAYS}}, prefer)
        else:
            prefer = LONG_DAY_PREF.get(req["sport"]) if req["intensity"] == "long" else (
                ["tue", "wed", "thu"] if req["intensity"] in ("test", "threshold", "vo2", "tempo") else None)
            d = _place(week, req, avail, prefer)
            if d is None and req["intensity"] in workouts.HARD - {"test"}:
                req["intensity"] = "tempo"
                warnings.append(f"{req['sport']}: harte Einheit ohne regelkonformen Tag – zu Tempo abgeschwächt")
                d = _place(week, req, avail, ["tue", "wed", "thu"])
        if d is None:
            warnings.append(f"{req['sport']} {req['intensity']} ({req['duration_s'] / 60:.0f} min) fand keinen verfügbaren Tag")
            continue
        minutes = max(10, int(round(req["duration_s"] / 60 / 5) * 5)) if req["intensity"] not in ("race", "test") else int(req["duration_s"] / 60)
        built = workouts.build(conn, req["sport"], req["intensity"], minutes, req.get("protocol"))
        session = {"date": d.isoformat(), "sport": req["sport"], **built, "status": "planned"}
        if req.get("category") == "RACE":
            session.update(category="RACE", race_priority=req["race_priority"], title=req["title"],
                           description="Wettkampf. Vortag kurz locker mit 3–4 Steigerungen.")
        week.slots[d].append(session)
        placed.append(session)

    fixed_load = week_effective(conn, monday, fixed, today)["load"]
    _fit_load(conn, placed, targets, warnings, avail, fixed_load)
    placed.sort(key=lambda s: (s["date"], s["sport"]))
    planned_load = fixed_load + sum(s["target_load"] or 0 for s in placed if s["sport"] in ENDURANCE_SPORTS)
    return {"sessions": placed, "targets": targets, "fixed_load": fixed_load, "rest_day": week.rest_day.isoformat() if week.rest_day else None,
            "planned_endurance_load": round(planned_load, 1),
            "planned_endurance_hours": round(sum(s["duration_s"] for s in placed if s["sport"] in ENDURANCE_SPORTS) / 3600, 2),
            "warnings": warnings}


def _sport_requests(conn: sqlite3.Connection, plan: dict[str, Any], sport: str, sport_min: float,
                    ctx: dict[str, Any], targets: dict[str, Any], baseline: dict[str, Any], key_intensity: str | None,
                    avail: dict[str, dict[str, int | None]], state: list[dict[str, Any]], monday: date,
                    today: date, warnings: list[str]) -> list[dict[str, Any]]:
    """Einheiten einer Sportart: Test, lange Einheit, Schlüsseleinheit, lockere Einheiten.

    Häufigkeit orientiert sich an der aktuellen Gewohnheit (Einheiten/Woche der letzten Wochen), höchstens +1,
    damit weder Umfang noch Frequenz sprunghaft steigen.
    """
    tpl = load_template(plan.get("goal_kind"))
    min_session = MIN_SESSION.get(sport, 30)
    by_volume = int(sport_min / tpl["typical_session_min"].get(sport, 50) + 0.5)
    habit = round(baseline["by_sport"].get(sport, {}).get("sessions_per_week", 0))
    n = min(by_volume, habit + 1) if habit else by_volume
    n = max(1, min(tpl["max_sessions_per_week"].get(sport, 3), len(avail.get(sport, {})), n))
    reqs: list[dict[str, Any]] = []
    protocol = _needs_test(conn, sport, monday, ctx, state, today) if sport in ENDURANCE_SPORTS else None
    if protocol:
        built = workouts.build(conn, sport, "test", 0, protocol)
        reqs.append({"sport": sport, "intensity": "test", "duration_s": built["duration_s"], "protocol": protocol})
        sport_min -= built["duration_s"] / 60
        n -= 1
        reason = ("kein gültiger Test vorhanden" if test_status(conn, sport, monday)["status"] in ("missing", "stale")
                  else "Retest vor Ablauf")
        warnings.append(f"{sport}: Leistungstest ({built['title']}) eingeplant – {reason}")
    total = max(0.0, sport_min)
    cap = targets["long_session_cap_min"].get(sport, 90)
    recovery = ctx["week_type"] == "recovery"
    if recovery:
        cap = int(cap * 0.7)
    long_min = key_min = 0.0
    if n >= 2 and sport in tpl.get("long_share", {}) and ctx["week_type"] != "race":
        # an der gewohnten Länge orientieren, aber höchstens die Hälfte der Wochenzeit der Sportart
        habitual = targets["recent_longest_min"].get(sport, 0) * (0.7 if recovery else 1.0)
        long_min = min(cap, max(total * tpl["long_share"][sport], min(habitual, total * 0.5)))
        if long_min < min_session:
            long_min = 0.0
    slots_left = n - (1 if long_min else 0)
    if key_intensity and slots_left >= 2 and not protocol and sport in ENDURANCE_SPORTS:
        status = test_status(conn, sport, monday)["status"]
        if status != "missing":
            if status == "stale":
                warnings.append(f"{sport}: Test veraltet – Zielbereiche beruhen auf altem Wert, Retest ausstehend")
            key_min = min(KEY_MIN.get(sport, 60), max(total * 0.3, 35))
            if ctx["week_type"] == "taper":
                key_min = min(key_min, 45)
        else:
            warnings.append(f"{sport}: keine harte Einheit ohne Leistungstest – erst testen")
    if long_min:
        reqs.append({"sport": sport, "intensity": "long", "duration_s": long_min * 60})
    if key_min:
        reqs.append({"sport": sport, "intensity": key_intensity, "duration_s": key_min * 60})
        slots_left -= 1
    rest = total - long_min - key_min
    easy_n = max(0, slots_left)
    while easy_n > 0 and rest / easy_n < min_session:
        easy_n -= 1
    if easy_n and rest / easy_n > cap:
        # eine lockere Einheit darf nicht länger werden als die Grenze für lange Einheiten -> Frequenz erhöhen
        free = min(tpl["max_sessions_per_week"].get(sport, 3), len(avail.get(sport, {}))) - len(reqs)
        easy_n = max(easy_n, min(free, math.ceil(rest / cap)))
        if rest / easy_n > cap:
            warnings.append(f"{sport}: {rest - easy_n * cap:.0f} min nicht planbar ohne die Längengrenze zu überschreiten")
    for _ in range(easy_n):
        reqs.append({"sport": sport, "intensity": "recovery" if recovery else "easy",
                     "duration_s": min(cap, rest / easy_n) * 60})
    if easy_n == 0 and rest >= 10:
        target = next((r for r in reqs if r["intensity"] == "long"), None)
        if target:
            target["duration_s"] = min(cap * 60, target["duration_s"] + rest * 60)
        elif not reqs and rest >= min_session * 0.7:
            reqs.append({"sport": sport, "intensity": "recovery" if recovery else "easy", "duration_s": rest * 60})
    return reqs


def _fit_load(conn: sqlite3.Connection, placed: list[dict[str, Any]], targets: dict[str, Any],
              warnings: list[str], avail: dict[str, dict[str, int | None]], fixed_load: float) -> None:
    """Hält die Wochenlast im Korridor: erst lockere, dann lange Einheiten anpassen."""
    corridor = targets["load_corridor"]
    ref = targets["reference"]
    def load() -> float:
        return fixed_load + sum(s["target_load"] or 0 for s in placed
                                if s["sport"] in ENDURANCE_SPORTS and s.get("race_priority") != "A")
    adjustable = [s for s in placed if s["intensity"] in ("easy", "recovery", "long") and s["sport"] in ENDURANCE_SPORTS]
    for _ in range(20):
        if load() <= corridor["upper"] or not adjustable:
            break
        for s in adjustable:
            new_min = max(MIN_SESSION.get(s["sport"], 30), int(s["duration_s"] / 60 * 0.9 / 5) * 5)
            s.update(workouts.build(conn, s["sport"], s["intensity"], new_min))
    if load() > corridor["upper"] * 1.02:
        warnings.append(f"Wochenlast {load():.0f} über Korridor {corridor['upper']:.0f} trotz Kürzung")
    if targets["context"]["week_type"] == "load" and load() < corridor["lower"]:
        for _ in range(10):
            if load() >= corridor["lower"]:
                break
            for s in adjustable:
                cap = avail.get(s["sport"], {}).get(weekday_key(s["date"]))
                limit = min(targets["long_session_cap_min"].get(s["sport"], 120), cap or 999)
                new_min = min(limit, int(s["duration_s"] / 60 * 1.1 / 5) * 5 + 5)
                if new_min > s["duration_s"] / 60:
                    s.update(workouts.build(conn, s["sport"], s["intensity"], new_min))
        if load() < corridor["lower"] * 0.98:
            warnings.append(f"Wochenlast {load():.0f} unter Untergrenze {corridor['lower']:.0f} "
                            f"(Referenz {ref['load']:.0f}) – zu wenige verfügbare Tage/Zeitfenster?")
