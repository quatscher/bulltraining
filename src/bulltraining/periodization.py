"""Periodisierung und Wochenziele.

Die Wochenziele entstehen aus drei Quellen:
1. Vorlage zur Zielart (Phasen, Verhältnis der Disziplinen, Taper) – `plan_templates/*.json`
2. aktuellem Trainingsumfang (tatsächlich absolvierte Wochen, sonst `training_baseline`)
3. Progressionsgrenzen aus `settings` (Last- und CTL-Anstieg, Unterlastgrenze, Entlastungswochen)

So startet ein Plan dort, wo der Athlet gerade steht, statt beim Wunschumfang.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from .db import get_float
from .metrics import activities_frame, form_state, load_corridor, training_baseline
from .util import ENDURANCE_SPORTS, monday_of, to_date

TEMPLATE_DIR = Path(__file__).parent / "plan_templates"
FALLBACK_LOAD_PER_HOUR = 50.0


@lru_cache(maxsize=32)
def load_template(goal_kind: str | None) -> dict[str, Any]:
    path = TEMPLATE_DIR / f"{goal_kind}.json"
    if not goal_kind or not path.exists():
        path = TEMPLATE_DIR / "generic.json"
    return json.loads(path.read_text(encoding="utf-8"))


def available_templates() -> list[dict[str, str]]:
    return [{"goal_kind": p.stem, "label": json.loads(p.read_text(encoding="utf-8")).get("label", p.stem)}
            for p in sorted(TEMPLATE_DIR.glob("*.json"))]


def plan_sports(plan: dict[str, Any]) -> list[str]:
    sports = plan["sports"]
    return json.loads(sports) if isinstance(sports, str) else list(sports)


INTRO_FRACTION = 0.4  # neue Sportart startet mit 40 % ihres Spitzenumfangs laut Vorlage


def sport_shares(plan: dict[str, Any], baseline: dict[str, Any] | None = None,
                 total_hours: float | None = None, reduction: float | None = None) -> dict[str, float]:
    """Anteile der Sportarten an der Wochenzeit.

    Ohne Kontext: Verhältnis der Vorlage. Mit aktuellem Umfang und Wochenziel:
    1. Was der Athlet schon macht, bleibt als Untergrenze – höchstens bis zum Spitzenumfang, den der Plan für die
       Sportart vorsieht (Vorlagenanteil × Zielstunden). Ein Läufer läuft also weiter, statt auf den Vorlagenanteil
       gekürzt zu werden, solange der Gesamtumfang noch klein ist.
    2. Neue Sportarten starten mit INTRO_FRACTION ihres Spitzenumfangs.
    3. Weitere Zeit geht an die Sportarten, die am weitesten unter ihrem Vorlagenanteil liegen.
    In Entlastungs- und Taperwochen (`reduction` = Anteil der Referenzwoche) schrumpfen alle Untergrenzen im
    selben Verhältnis.
    """
    tpl = load_template(plan.get("goal_kind"))
    sports = [s for s in plan_sports(plan) if s in ENDURANCE_SPORTS or s == "other"]
    raw = {s: tpl["sports"].get(s, 1.0 / max(1, len(sports))) for s in sports}
    norm = sum(raw.values()) or 1.0
    target = {s: v / norm for s, v in raw.items()}
    if not baseline or not total_hours:
        return target
    ceiling = float(plan.get("weekly_hours") or total_hours)
    cur = {s: float(baseline.get("by_sport", {}).get(s, {}).get("hours_per_week") or 0) for s in sports}
    if sum(cur.values()) <= 0:
        return target
    floor = {s: min(cur[s], target[s] * ceiling) if cur[s] > 0 else INTRO_FRACTION * target[s] * ceiling
             for s in sports}
    f = min(1.0, total_hours / (sum(floor.values()) or 1.0))
    if reduction is not None:
        f = min(f, reduction)  # Entlastung/Taper: jede Sportart anteilig reduzieren, nicht nur die "Überschüsse"
    alloc = {s: v * f for s, v in floor.items()}
    rest = total_hours - sum(alloc.values())
    if rest > 1e-6:
        deficit = {s: max(0.0, target[s] * total_hours - alloc[s]) for s in sports}
        weights = deficit if sum(deficit.values()) > 0 else target
        wsum = sum(weights.values()) or 1.0
        for s in sports:
            alloc[s] += rest * weights[s] / wsum
    return {s: alloc[s] / total_hours for s in sports}


def week_context(plan: dict[str, Any], monday: date) -> dict[str, Any]:
    """Phase und Wochentyp: load | recovery | taper | race | post | pre."""
    tpl = load_template(plan.get("goal_kind"))
    start = monday_of(plan["start_date"])
    idx = (monday - start).days // 7
    if idx < 0:
        return {"phase": "pre", "week_type": "pre", "week_index": idx, "weeks_to_goal": None}
    if plan["goal_type"] == "continuous":
        wt = "recovery" if idx % 4 == 3 else "load"
        return {"phase": "continuous", "week_type": wt, "week_index": idx, "weeks_to_goal": None,
                "block_week": idx % 4 + 1}
    goal_monday = monday_of(plan["goal_date"])
    to_goal = (goal_monday - monday).days // 7
    taper = int(tpl.get("taper_weeks", 1))
    if to_goal < 0:
        return {"phase": "post", "week_type": "post", "week_index": idx, "weeks_to_goal": to_goal}
    if to_goal == 0:
        return {"phase": "taper", "week_type": "race", "week_index": idx, "weeks_to_goal": 0}
    if to_goal <= taper:
        return {"phase": "taper", "week_type": "taper", "week_index": idx, "weeks_to_goal": to_goal}
    # Aufbauwochen rückwärts vom Taper: der letzte Block endet direkt vor dem Taper.
    build_weeks = (goal_monday - start).days // 7 - taper
    pos = idx / max(1, build_weeks)
    split = tpl["phase_split"]
    phase = "base" if pos < split["base"] else ("build" if pos < split["base"] + split["build"] else "specific")
    weeks_before_taper = to_goal - taper  # 1 = letzte Woche vor dem Taper
    # Blöcke enden am Taper; die erste Entlastung frühestens nach drei Belastungswochen ab Planstart
    wt = "recovery" if weeks_before_taper % 4 == 0 and weeks_before_taper > 0 and idx >= 3 else "load"
    return {"phase": phase, "week_type": wt, "week_index": idx, "weeks_to_goal": to_goal,
            "block_week": 4 - (weeks_before_taper % 4) if weeks_before_taper % 4 else 4}


def taper_start(plan: dict[str, Any]) -> date | None:
    if plan["goal_type"] != "event" or not plan.get("goal_date"):
        return None
    taper = int(load_template(plan.get("goal_kind")).get("taper_weeks", 1))
    return monday_of(plan["goal_date"]) - timedelta(weeks=taper)


def week_effective(conn: sqlite3.Connection, monday: date, sessions: list[dict[str, Any]],
                   today: date) -> dict[str, float]:
    """Last/Stunden einer Woche: vergangene Tage aus Aktivitäten, heutige und künftige aus dem Plan."""
    sunday = monday + timedelta(days=6)
    done_load = done_h = 0.0
    if monday < today:
        df = activities_frame(conn, start=monday, end=min(sunday, today - timedelta(days=1)))
        df = df[df["is_endurance"] == 1]
        done_load, done_h = float(df["eff_load"].sum()), float(df["duration_s"].sum()) / 3600
    plan_load = plan_h = 0.0
    for s in sessions:
        d = to_date(s["date"])
        if monday <= d <= sunday and d >= today and s.get("status") not in ("deleted", "skipped") \
                and s["sport"] in ENDURANCE_SPORTS:
            plan_load += float(s.get("target_load") or 0)
            plan_h += s["duration_s"] / 3600
    return {"load": round(done_load + plan_load, 1), "hours": round(done_h + plan_h, 2),
            "done_load": round(done_load, 1), "planned_load": round(plan_load, 1)}


def reference_week(conn: sqlite3.Connection, plan: dict[str, Any], monday: date,
                   sessions: list[dict[str, Any]], today: date, baseline: dict[str, Any]) -> dict[str, Any]:
    """Referenz für die Woche ab `monday`: akute Last (Vorwoche) gegen chronische Last (Schnitt 4 Wochen).

    Vergangene Tage zählen mit tatsächlich Absolviertem, künftige mit dem Plan. Referenz ist das Maximum aus
    beiden – eine Entlastungs- oder Krankheitswoche zieht den Plan nicht nach unten, eine Spitzenwoche zählt.
    """
    weeks = int(baseline.get("weeks") or 4)
    effs = [week_effective(conn, monday - timedelta(weeks=k), sessions, today) for k in range(1, weeks + 1)]
    from .metrics import manual_baseline
    manual = manual_baseline(conn)
    if manual and manual.get("as_of"):
        # Wochen vor der Selbstauskunft zählen mindestens mit dem angegebenen Umfang – auch wenn einzelne
        # Aktivitäten (z. B. eine Krafteinheit) aus dieser Zeit vorliegen
        from .metrics import _baseline_from_manual
        mb = _baseline_from_manual(manual, today, weeks, {"ctl_endurance": 0, "form_endurance": 0})
        manual_until = to_date(manual["as_of"])
        for k, e in enumerate(effs, start=1):
            if monday - timedelta(weeks=k) + timedelta(days=6) <= manual_until and e["load"] < mb["avg_endurance_load"]:
                effs[k - 1] = {**e, "load": mb["avg_endurance_load"], "hours": mb["avg_endurance_hours"]}
    chronic_load = sum(e["load"] for e in effs) / weeks
    chronic_hours = sum(e["hours"] for e in effs) / weeks
    if chronic_load <= 0:
        hours = (plan.get("weekly_hours") or 5) * 0.6
        return {"load": hours * FALLBACK_LOAD_PER_HOUR, "hours": hours, "chronic_load": 0.0, "prev_load": 0.0,
                "after_recovery": False,
                "source": "keine Trainingsdaten – konservativer Start mit 60 % der Zielstunden"}
    # letzte Belastungswoche: Entlastung/Taper überspringen, damit der nächste Block dort anknüpft
    prev, prev_k = effs[0], 1
    for k in range(1, min(3, weeks) + 1):
        if week_context(plan, monday - timedelta(weeks=k))["week_type"] not in ("recovery", "taper", "race"):
            prev, prev_k = effs[k - 1], k
            break
    use_prev = prev["load"] > chronic_load
    return {"load": round(max(prev["load"], chronic_load), 1), "hours": round(max(prev["hours"], chronic_hours), 2),
            "chronic_load": round(chronic_load, 1), "prev_load": prev["load"], "after_recovery": prev_k > 1,
            "source": (f"Belastungswoche ab {(monday - timedelta(weeks=prev_k)).isoformat()}" if use_prev
                       else f"Schnitt der {weeks} Wochen vor {monday.isoformat()}")}


PHASE_CEILING = {"base": 0.8, "build": 0.95, "specific": 1.0, "taper": 1.0, "continuous": 1.0, "pre": 0.8}
PHASE_INTENSITY = {"base": 1.0, "build": 1.05, "specific": 1.05, "continuous": 1.0, "taper": 0.95, "pre": 1.0}


def week_targets(conn: sqlite3.Connection, plan: dict[str, Any], monday: date,
                 sessions: list[dict[str, Any]] | None = None, today: date | None = None) -> dict[str, Any]:
    """Ziel-Stunden, Ziel-Last und zulässiger Lastkorridor für eine Woche."""
    today = today or date.today()
    if sessions is None:
        sessions = [dict(r) for r in conn.execute(
            "SELECT * FROM plan_sessions WHERE plan_id = ? AND status != 'deleted'", (plan["id"],))]
    baseline = training_baseline(conn, as_of=min(monday, today))
    ctx = week_context(plan, monday)
    ref = reference_week(conn, plan, monday, sessions, today, baseline)
    inc = get_float(conn, "max_weekly_load_increase_pct", 10) / 100
    rec = get_float(conn, "recovery_week_pct", 65) / 100
    # Spitzenumfang erst in der wettkampfspezifischen Phase; Grundlage und Aufbau bleiben darunter
    ceiling = float(plan.get("weekly_hours") or ref["hours"] or 5) * PHASE_CEILING.get(ctx["phase"], 1.0)
    wt = ctx["week_type"]
    notes = []
    if wt == "recovery":
        hours = ref["hours"] * rec
    elif wt == "taper":
        hours = ref["hours"] * (0.75 if ctx["weeks_to_goal"] >= 2 else 0.6)
    elif wt == "race":
        hours = ref["hours"] * 0.35  # ohne das Rennen selbst
    elif ref["hours"] <= ceiling:
        # nach einer Entlastungswoche auf dem Niveau der letzten Belastungswoche wieder einsteigen
        step = inc / 2 if ref.get("after_recovery") else inc
        hours = min(ceiling, ref["hours"] * (1 + step))
        if hours < ceiling:
            notes.append(f"Aufbau Richtung {ceiling:g} h/Woche, +{step:.0%} gegenüber Referenz")
    else:
        hours = max(ceiling, ref["hours"] * 0.9)
        notes.append("aktueller Umfang über Planziel – schrittweise Absenkung statt Sprung")
    lph = baseline.get("load_per_hour") or (ref["load"] / ref["hours"] if ref["hours"] else FALLBACK_LOAD_PER_HOUR)
    target_load = hours * lph * PHASE_INTENSITY.get(ctx["phase"], 1.0)
    ctl = form_state(conn, min(monday, today))["ctl_endurance"]
    corridor = load_corridor(conn, ref["load"], ctl, recovery=wt in ("recovery", "taper", "race", "post"),
                             chronic_load=ref["chronic_load"])
    if target_load > corridor["upper"]:
        notes.append(f"Ziellast auf Korridor-Obergrenze {corridor['upper']:.0f} gekappt")
        target_load = corridor["upper"]
        hours = min(hours, target_load / lph) if lph else hours
    if wt == "load" and target_load < corridor["lower"]:
        target_load = corridor["lower"]
    long_inc = get_float(conn, "long_session_max_increase_pct", 15) / 100
    tpl = load_template(plan.get("goal_kind"))
    long_caps, recent_longest = {}, {}
    for s in plan_sports(plan):
        if s not in ENDURANCE_SPORTS:
            continue
        recent = baseline["longest_min_by_sport"].get(s, 0)
        planned_long = max((x["duration_s"] / 60 for x in sessions
                            if x["sport"] == s and x.get("status") != "deleted"
                            and monday - timedelta(weeks=2) <= to_date(x["date"]) < monday), default=0)
        base_long = max(recent, planned_long) or {"run": 50, "ride": 75, "swim": 40}[s]
        recent_longest[s] = int(max(recent, planned_long))
        long_caps[s] = int(min(tpl["long_session_max_min"].get(s, 999), base_long * (1 + long_inc)))
    return {
        "week_start": monday.isoformat(), "context": ctx, "reference": ref,
        "target_hours": round(hours, 2), "target_load": round(target_load, 1), "load_corridor": corridor,
        "long_session_cap_min": long_caps, "recent_longest_min": recent_longest, "ctl_endurance": ctl, "notes": notes,
        "baseline_quality": baseline["data_quality"],
    }


def weekly_series(conn: sqlite3.Connection, plan_id: int | None, weeks_back: int = 8, weeks_ahead: int = 4,
                  today: date | None = None) -> list[dict[str, Any]]:
    """Wochenweise Stunden/Last: absolviert (Vergangenheit) und geplant (Zukunft) – für das Frontend."""
    today = today or date.today()
    this_monday = monday_of(today)
    df = activities_frame(conn, start=this_monday - timedelta(weeks=weeks_back), end=today)
    df = df[df["is_endurance"] == 1]
    planned = pd.read_sql_query("SELECT date, duration_s, target_load, sport FROM plan_sessions WHERE plan_id = ? "
                                "AND status != 'deleted'", conn, params=[plan_id or -1])
    planned = planned[planned["sport"].isin(ENDURANCE_SPORTS)]
    out = []
    for k in range(-weeks_back, weeks_ahead + 1):
        m = this_monday + timedelta(weeks=k)
        e = m + timedelta(days=6)
        w = df[(df["date"] >= pd.Timestamp(m)) & (df["date"] <= pd.Timestamp(e))]
        p = planned[(planned["date"] >= m.isoformat()) & (planned["date"] <= e.isoformat())]
        out.append({"week_start": m.isoformat(), "done_hours": round(w["duration_s"].sum() / 3600, 2),
                    "done_load": round(float(w["eff_load"].sum()), 1),
                    "planned_hours": round(p["duration_s"].sum() / 3600, 2),
                    "planned_load": round(float(p["target_load"].fillna(0).sum()), 1)})
    return out
