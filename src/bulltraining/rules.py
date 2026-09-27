"""Harte Regeln für Planänderungen. Validierung im Werkzeug, nicht im Prompt.

Fehler blockieren den Vorschlag; Hinweise werden mitgespeichert und im Posteingang angezeigt.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from . import workouts
from .db import get_float
from .performance import test_status
from .periodization import taper_start, week_effective, week_targets
from .util import ENDURANCE_SPORTS, monday_of, to_date, week_days

LOAD_TOLERANCE = 0.03
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _active(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in sessions if s.get("status") not in ("deleted",)]


def validate_weeks(conn: sqlite3.Connection, plan: dict[str, Any], state: list[dict[str, Any]],
                   mondays: set[date], today: date) -> tuple[list[str], list[str]]:
    """Prüft betroffene Wochen im (hypothetischen) Planzustand `state`."""
    errors: list[str] = []
    warnings: list[str] = []
    max_hard = int(get_float(conn, "max_hard_days", 3))
    active = _active(state)
    hard_days = {to_date(s["date"]) for s in active if s.get("intensity") in workouts.HARD}
    for monday in sorted(mondays):
        label = f"Woche ab {monday.isoformat()}"
        days = week_days(monday)
        week = [s for s in active if monday <= to_date(s["date"]) <= days[-1]]
        # 1) harte Tage: Anzahl und nie zwei in Folge (auch über die Wochengrenze)
        n_hard = sum(1 for d in days if d in hard_days)
        if n_hard > max_hard:
            errors.append(f"{label}: {n_hard} harte Tage, erlaubt sind höchstens {max_hard}.")
        for d in [monday - timedelta(days=1)] + days:  # Vortag einschließen: Sonntag -> Montag der Woche
            if d in hard_days and d + timedelta(days=1) in hard_days and d + timedelta(days=1) >= today:
                errors.append(f"{label}: harte Tage in Folge am {d.isoformat()} und {(d + timedelta(days=1)).isoformat()}.")
        # 2) mindestens ein vollständig freier Tag (vergangene Tage zählen nach tatsächlichen Aktivitäten)
        busy = {to_date(s["date"]) for s in week}
        for r in conn.execute("SELECT DISTINCT substr(start_date,1,10) AS d FROM activities WHERE excluded = 0 "
                              "AND start_date >= ? AND start_date < ?", (monday.isoformat(), today.isoformat())):
            busy.add(to_date(r["d"]))
        if all(d in busy for d in days):
            errors.append(f"{label}: kein vollständig freier Tag.")
        # 3) Lastkorridor gegenüber Referenzwoche und aktuellem Trainingsumfang
        targets = week_targets(conn, plan, monday, state, today)
        eff = week_effective(conn, monday, state, today)
        corridor = targets["load_corridor"]
        # Das A-Rennen ist das Ziel selbst und zählt nicht gegen die Obergrenze; B/C-Rennen schon.
        a_race = sum(float(s.get("target_load") or 0) for s in week
                     if s.get("category") == "RACE" and s.get("race_priority") == "A" and to_date(s["date"]) >= today)
        if eff["load"] - a_race > corridor["upper"] * (1 + LOAD_TOLERANCE):
            errors.append(f"{label}: Wochenlast {eff['load']:.0f} über Obergrenze {corridor['upper']:.0f} "
                          f"(Referenz {targets['reference']['load']:.0f}, {targets['reference']['source']}). "
                          "Überforderung – Umfang oder Intensität reduzieren.")
        if targets["context"]["week_type"] == "load" and eff["load"] < corridor["lower"] * (1 - LOAD_TOLERANCE):
            warnings.append(f"{label}: Wochenlast {eff['load']:.0f} unter Untergrenze {corridor['lower']:.0f} "
                            "– Unterforderung, sofern nicht bewusst (Krankheit, HRV) reduziert.")
        # 4) lange Einheiten nicht weit über das zuletzt Gelaufene/Gefahrene hinaus
        for s in week:
            if s["sport"] not in ENDURANCE_SPORTS or s.get("category") in ("RACE", "TEST") or to_date(s["date"]) < today:
                continue
            cap = targets["long_session_cap_min"].get(s["sport"])
            if cap and s["duration_s"] / 60 > cap * (1 + LOAD_TOLERANCE):
                errors.append(f"{label}: {s['title']} ({s['duration_s'] / 60:.0f} min) über der Grenze von {cap} min "
                              f"für {s['sport']} (längste Einheit der letzten Wochen + Aufschlag).")
        # 5) Verfügbarkeit: Sportart an diesem Tag erlaubt und Zeitfenster lang genug
        from .generator import availability  # spät importiert, vermeidet Zyklus
        avail = availability(plan)
        for s in week:
            if to_date(s["date"]) < today or s["sport"] not in avail or s.get("category") == "RACE":
                continue
            day = WEEKDAY_KEYS[to_date(s["date"]).weekday()]
            if day not in avail[s["sport"]]:
                errors.append(f"{label}: {s['title']} am {s['date']} – {s['sport']} ist an diesem Wochentag nicht verfügbar.")
            elif avail[s["sport"]][day] and s["duration_s"] / 60 > avail[s["sport"]][day] * (1 + LOAD_TOLERANCE):
                errors.append(f"{label}: {s['title']} ({s['duration_s'] // 60} min) passt nicht ins Zeitfenster "
                              f"von {avail[s['sport']][day]} min.")
        # 6) Intensität ohne Testgrundlage: nur Hinweis
        for s in week:
            if s.get("intensity") in ("threshold", "vo2") and s["sport"] in ENDURANCE_SPORTS \
                    and test_status(conn, s["sport"], monday)["status"] == "missing":
                warnings.append(f"{label}: {s['title']} ohne Leistungstest für {s['sport']} – Zielbereiche nur geschätzt.")
    return errors, warnings


def check_op_dates(plan: dict[str, Any], touched: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]],
                   today: date) -> list[str]:
    """Keine Änderung in der Vergangenheit; Taper-Fenster nur für entlastende Änderungen offen.

    touched: (op-name, vorher, nachher) je betroffener Einheit.
    """
    errors = []
    t_start = taper_start(plan)
    goal = to_date(plan["goal_date"]) if plan.get("goal_date") else None
    for op, before, after in touched:
        for s in (before, after):
            if s and to_date(s["date"]) < today:
                errors.append(f"{op}: Einheit am {s['date']} liegt in der Vergangenheit und bleibt unverändert.")
            if s and s.get("status") in ("done", "skipped"):
                errors.append(f"{op}: Einheit {s.get('id')} ist bereits '{s['status']}'.")
        if t_start and goal:
            in_taper = [s for s in (before, after) if s and t_start <= to_date(s["date"]) <= goal]
            if in_taper and not _is_reducing(op, before, after):
                errors.append(f"{op}: Taper-Fenster ab {t_start.isoformat()} ist gesperrt – "
                              "nur Kürzen, Entlasten oder Löschen erlaubt.")
    return sorted(set(errors))


def _is_reducing(op: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    if op in ("delete_session", "insert_recovery_day", "regenerate_week_template"):
        return True
    if before is None or after is None:
        return False
    order = workouts.INTENSITY_ORDER
    less_or_equal = (after["duration_s"] <= before["duration_s"]
                     and order.index(after.get("intensity") or "easy") <= order.index(before.get("intensity") or "easy"))
    return less_or_equal and (op != "move_session" or monday_of(after["date"]) == monday_of(before["date"]))
