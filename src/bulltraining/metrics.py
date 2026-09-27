"""Kennzahlen-Layer, rein deterministisch: Formkurven, Wochenlast, Zonen, Wellness, Trainingsumfang."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

import pandas as pd

from .db import get_float, get_setting
from .util import ENDURANCE_SPORTS, monday_of, parse_week, to_date, week_days
from .zonemodel import ZONES, activity_zone_secs, reference_paces, session_zone_secs

CTL_DAYS = 42
ATL_DAYS = 7
# Anteil, um den sich CTL in 7 Tagen einer konstanten Tageslast annähert: 1 - (41/42)^7
CTL_WEEK_RESPONSE = 1 - (1 - 1 / CTL_DAYS) ** 7


def activities_frame(conn: sqlite3.Connection, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Alle nicht ausgeschlossenen Aktivitäten mit effektiver Last (sRPE mit Kalibrierfaktor)."""
    factor = get_float(conn, "srpe_factor", 1.0)
    sql = ("SELECT id, source, start_date, sport, name, duration_s, distance_m, load, load_method, is_endurance, "
           "rpe, zone_times FROM activities WHERE excluded = 0")
    params: list[Any] = []
    if start:
        sql += " AND start_date >= ?"
        params.append(start.isoformat())
    if end:
        sql += " AND start_date < ?"
        params.append((end + timedelta(days=1)).isoformat())
    df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        df["date"] = pd.Series(dtype="datetime64[ns]")
        df["eff_load"] = pd.Series(dtype=float)
        return df
    df["date"] = pd.to_datetime(df["start_date"].str[:10])
    df["eff_load"] = df["load"].fillna(0.0).astype(float)
    df.loc[df["load_method"] == "srpe", "eff_load"] *= factor
    return df


def _ewm(series: pd.Series, days: int) -> pd.Series:
    # Klassisches Impulse-Response-Modell: CTL_t = CTL_{t-1} + (L_t - CTL_{t-1}) / 42.
    # Das entspricht alpha = 1/42 – NICHT halflife=42, das ergäbe eine Zeitkonstante von ~61 Tagen.
    return series.ewm(alpha=1 / days, adjust=False).mean()


def form_curves(conn: sqlite3.Connection, end: date | None = None) -> pd.DataFrame:
    end = end or date.today()
    df = activities_frame(conn, end=end)
    first = df["date"].min() if not df.empty else pd.Timestamp(end)
    # Einen Tag vor der ersten Aktivität mit Last 0 beginnen: ewm(adjust=False) startet sonst beim ersten Wert.
    idx = pd.date_range(min(first, pd.Timestamp(end)) - pd.Timedelta(days=1), pd.Timestamp(end), freq="D")
    total = df.groupby("date")["eff_load"].sum().reindex(idx, fill_value=0.0)
    endu = df[df["is_endurance"] == 1].groupby("date")["eff_load"].sum().reindex(idx, fill_value=0.0)
    out = pd.DataFrame(index=idx)
    out["load_total"] = total
    out["load_endurance"] = endu
    for name, s in (("endurance", endu), ("total", total)):
        out[f"ctl_{name}"] = _ewm(s, CTL_DAYS)
        out[f"atl_{name}"] = _ewm(s, ATL_DAYS)
        # Form = Vortageswerte, damit die heutige Einheit die heutige Form nicht verschlechtert.
        out[f"form_{name}"] = (out[f"ctl_{name}"] - out[f"atl_{name}"]).shift(1).fillna(0.0)
    well = pd.read_sql_query("SELECT date, hrv, resting_hr, ctl_icu FROM wellness", conn)
    if not well.empty:
        well.index = pd.to_datetime(well.pop("date"))
        out = out.join(well, how="left")
    else:
        out["hrv"] = out["resting_hr"] = out["ctl_icu"] = float("nan")
    return out


def _r(x: Any, nd: int = 1) -> float | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    return round(float(x), nd)


def hrv_status(conn: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    """7-Tage-Mittel gegen 60-Tage-Baseline, in Standardabweichungen."""
    today = today or date.today()
    rows = pd.read_sql_query("SELECT date, hrv, resting_hr FROM wellness WHERE date > ? AND date <= ? ORDER BY date",
                             conn, params=[(today - timedelta(days=60)).isoformat(), today.isoformat()])
    hrv = rows["hrv"].dropna()
    if len(hrv) < 14:
        return {"available": False, "reason": "weniger als 14 HRV-Werte in 60 Tagen"}
    base_mean, base_sd = hrv.mean(), hrv.std() or 1.0
    recent = rows[rows["date"] > (today - timedelta(days=7)).isoformat()]["hrv"].dropna()
    if recent.empty:
        # Baseline vorhanden, aber keine aktuelle Messung: keine Abweichung behaupten
        return {"available": False, "reason": "keine HRV-Messung in den letzten 7 Tagen",
                "baseline_60d": _r(base_mean), "sd": _r(base_sd), "mean_7d": None, "deviation_sd": None,
                "days_below_baseline_in_row": 0}
    last7 = recent.mean()
    by_day = {r["date"]: r["hrv"] for _, r in rows.iterrows() if pd.notna(r["hrv"])}
    below = 0
    day = today
    if day.isoformat() not in by_day:
        day -= timedelta(days=1)  # heutiger Wert fehlt oft noch morgens
    while by_day.get(day.isoformat()) is not None and by_day[day.isoformat()] < base_mean - 0.5 * base_sd:
        below += 1  # nur lückenlose Kalendertage zählen
        day -= timedelta(days=1)
    return {"available": True, "baseline_60d": _r(base_mean), "sd": _r(base_sd), "mean_7d": _r(last7),
            "deviation_sd": _r((last7 - base_mean) / base_sd, 2), "days_below_baseline_in_row": below}


def form_state(conn: sqlite3.Connection, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    curves = form_curves(conn, end=today)
    last = curves.iloc[-1]
    week_ago = curves.iloc[-8] if len(curves) >= 8 else curves.iloc[0]
    return {
        "date": today.isoformat(),
        "ctl_endurance": _r(last["ctl_endurance"]),
        "atl_endurance": _r(last["atl_endurance"]),
        "form_endurance": _r(last["form_endurance"]),
        "ctl_total": _r(last["ctl_total"]),
        "atl_total": _r(last["atl_total"]),
        "form_total": _r(last["form_total"]),
        "ctl_icu": _r(last.get("ctl_icu")),
        "ctl_ramp_7d": _r(last["ctl_endurance"] - week_ago["ctl_endurance"]),
        "hrv": hrv_status(conn, today),
    }


def _week_actuals(df: pd.DataFrame, monday: date) -> pd.DataFrame:
    end = pd.Timestamp(monday + timedelta(days=7))
    return df[(df["date"] >= pd.Timestamp(monday)) & (df["date"] < end)]


def week_summary(conn: sqlite3.Connection, week: str | date | None = None, plan_id: int | None = None) -> dict[str, Any]:
    monday = parse_week(week)
    sunday = monday + timedelta(days=6)
    acts = activities_frame(conn, start=monday, end=sunday)
    if plan_id is None:
        row = conn.execute("SELECT id FROM plans WHERE status = 'active'").fetchone()
        plan_id = row["id"] if row else None
    planned = pd.read_sql_query(
        "SELECT sport, duration_s, target_load, status FROM plan_sessions "
        "WHERE plan_id = ? AND date BETWEEN ? AND ? AND status != 'deleted'",
        conn, params=[plan_id or -1, monday.isoformat(), sunday.isoformat()])
    sports = sorted(set(acts["sport"]).union(planned["sport"]))
    by_sport = {}
    for s in sports:
        a = acts[acts["sport"] == s]
        p = planned[planned["sport"] == s]
        by_sport[s] = {
            "planned_min": int(p["duration_s"].sum() / 60), "planned_load": _r(p["target_load"].sum()),
            "done_min": int(a["duration_s"].sum() / 60), "done_load": _r(a["eff_load"].sum()),
            "sessions_planned": int(len(p)), "sessions_done": int(len(a)),
        }
    return {
        "week": f"{monday.isocalendar()[0]}-W{monday.isocalendar()[1]:02d}",
        "from": monday.isoformat(), "to": sunday.isoformat(),
        "by_sport": by_sport,
        "total": {
            "planned_min": int(planned["duration_s"].sum() / 60), "planned_load": _r(planned["target_load"].sum()),
            "done_min": int(acts["duration_s"].sum() / 60), "done_load": _r(acts["eff_load"].sum()),
            "done_endurance_load": _r(acts[acts["is_endurance"] == 1]["eff_load"].sum()),
        },
        "zones_min": _minutes(zone_totals(zone_timeline(conn, monday, sunday, plan_id))),
    }


def _minutes(t: dict[str, Any]) -> dict[str, Any]:
    return {"zones": ZONES, "planned": [round(s / 60) for s in t["planned"]], "done": [round(s / 60) for s in t["done"]],
            "done_without_zones": round(t["done_no_zones"] / 60)}


def zone_timeline(conn: sqlite3.Connection, start: date, end: date, plan_id: int | None = None) -> list[dict[str, Any]]:
    """Je Tag: Sekunden je Zone (5-Zonen-Modell) geplant und absolviert, plus Ausdauerzeit ohne Zonendaten."""
    if plan_id is None:
        row = conn.execute("SELECT id FROM plans WHERE status = 'active'").fetchone()
        plan_id = row["id"] if row else None
    days = {(start + timedelta(days=i)).isoformat(): {"planned": [0.0] * 5, "done": [0.0] * 5, "done_no_zones": 0.0}
            for i in range((end - start).days + 1)}
    paces = reference_paces(conn)
    for s in conn.execute("SELECT date, sport, description, duration_s, intensity FROM plan_sessions "
                          "WHERE plan_id = ? AND date BETWEEN ? AND ? AND status != 'deleted'",
                          (plan_id or -1, start.isoformat(), end.isoformat())):
        if s["sport"] not in ENDURANCE_SPORTS:
            continue
        for i, v in enumerate(session_zone_secs(dict(s), paces)):
            days[s["date"]]["planned"][i] += v
    for a in conn.execute("SELECT start_date, duration_s, zone_times FROM activities WHERE excluded = 0 "
                          "AND is_endurance = 1 AND start_date >= ? AND start_date < ?",
                          (start.isoformat(), (end + timedelta(days=1)).isoformat())):
        day = days[a["start_date"][:10]]
        secs = activity_zone_secs(a["zone_times"])
        if secs:
            for i, v in enumerate(secs):
                day["done"][i] += v
        else:
            day["done_no_zones"] += a["duration_s"] or 0
    return [{"date": d, **v} for d, v in days.items()]


def zone_totals(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    out = {"planned": [0.0] * 5, "done": [0.0] * 5, "done_no_zones": 0.0}
    for d in timeline:
        for i in range(5):
            out["planned"][i] += d["planned"][i]
            out["done"][i] += d["done"][i]
        out["done_no_zones"] += d["done_no_zones"]
    return out


def zone_weeks(conn: sqlite3.Connection, weeks_back: int = 8, weeks_ahead: int = 4,
               today: date | None = None) -> list[dict[str, Any]]:
    """Wochenweise Minuten je Zone, geplant und absolviert, von -weeks_back bis +weeks_ahead."""
    first = monday_of(today or date.today()) - timedelta(weeks=weeks_back)
    timeline = zone_timeline(conn, first, first + timedelta(weeks=weeks_back + weeks_ahead + 1, days=-1))
    return [{"week_start": timeline[i]["date"], **_minutes(zone_totals(timeline[i:i + 7]))}
            for i in range(0, len(timeline), 7)]


def zone_distribution(conn: sqlite3.Connection, days: int = 28, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    df = activities_frame(conn, start=today - timedelta(days=days - 1), end=today)
    result: dict[str, Any] = {"days": days, "by_sport": {}, "polarization": None}
    low = mid = high = 0
    for sport, group in df[df["zone_times"].notna()].groupby("sport"):
        kinds: dict[str, list[int]] = {}
        for zt in group["zone_times"]:
            z = json.loads(zt)
            acc = kinds.setdefault(z["kind"], [0] * len(z["secs"]))
            if len(acc) < len(z["secs"]):
                acc.extend([0] * (len(z["secs"]) - len(acc)))
            for i, s in enumerate(z["secs"]):
                acc[i] += s
        result["by_sport"][sport] = {
            kind: {f"Z{i + 1}": round(s / 60) for i, s in enumerate(secs)} for kind, secs in kinds.items()
        }
        # Drei-Zonen-Modell: Z1–2 niedrig, Z3 mittel, Z4+ hoch (je Quelle eine Art zählen, Leistung bevorzugt)
        secs = kinds.get("power") or kinds.get("hr") or []
        low += sum(secs[:2])
        mid += sum(secs[2:3])
        high += sum(secs[3:])
    total = low + mid + high
    if total:
        result["polarization"] = {"low_pct": round(100 * low / total), "mid_pct": round(100 * mid / total),
                                  "high_pct": round(100 * high / total), "minutes_with_zones": round(total / 60)}
    no_zones = df[df["zone_times"].isna() & (df["is_endurance"] == 1)]
    result["endurance_minutes_without_zones"] = int(no_zones["duration_s"].sum() / 60)
    return result


def wellness_trend(conn: sqlite3.Connection, days: int = 14, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    rows = conn.execute("SELECT date, hrv, resting_hr, sleep_h, weight_kg FROM wellness WHERE date > ? AND date <= ? "
                        "ORDER BY date", ((today - timedelta(days=days)).isoformat(), today.isoformat())).fetchall()
    base = pd.read_sql_query("SELECT hrv, resting_hr, sleep_h FROM wellness WHERE date > ? AND date <= ?", conn,
                             params=[(today - timedelta(days=60)).isoformat(), today.isoformat()])
    return {
        "days": days,
        "series": [dict(r) for r in rows],
        "baseline_60d": {c: _r(base[c].mean()) for c in ("hrv", "resting_hr", "sleep_h")} if not base.empty else None,
        "hrv_status": hrv_status(conn, today),
    }


def training_baseline(conn: sqlite3.Connection, as_of: date | None = None, weeks: int | None = None) -> dict[str, Any]:
    """Aktueller Trainingsumfang: Grundlage dafür, dass ein Plan weder über- noch unterfordert.

    Betrachtet die letzten `weeks` abgeschlossenen Kalenderwochen vor `as_of`.
    """
    as_of = to_date(as_of)
    weeks = int(weeks or get_float(conn, "baseline_weeks", 4))
    this_monday = monday_of(as_of)
    start = this_monday - timedelta(weeks=weeks)
    df = activities_frame(conn, start=start, end=this_monday - timedelta(days=1))
    weekly = []
    for i in range(weeks):
        m = start + timedelta(weeks=i)
        w = _week_actuals(df, m)
        we = w[w["is_endurance"] == 1]
        weekly.append({
            "week": f"{m.isocalendar()[0]}-W{m.isocalendar()[1]:02d}",
            "endurance_hours": round(we["duration_s"].sum() / 3600, 2),
            "total_hours": round(w["duration_s"].sum() / 3600, 2),
            "endurance_load": round(float(we["eff_load"].sum()), 1),
            "total_load": round(float(w["eff_load"].sum()), 1),
            "sessions": int(len(w)),
        })
    active_weeks = [w for w in weekly if w["sessions"] > 0]
    avg_hours = sum(w["endurance_hours"] for w in weekly) / weeks
    avg_load = sum(w["endurance_load"] for w in weekly) / weeks
    by_sport = {}
    for sport, g in df.groupby("sport"):
        by_sport[sport] = {
            "hours_per_week": round(g["duration_s"].sum() / 3600 / weeks, 2),
            "sessions_per_week": round(len(g) / weeks, 1),
            "longest_min": int(g["duration_s"].max() / 60),
            "load_per_week": round(float(g["eff_load"].sum()) / weeks, 1),
        }
    last_week = weekly[-1] if weekly else None
    acwr = round(last_week["endurance_load"] / avg_load, 2) if last_week and avg_load > 0 else None
    state = form_state(conn, this_monday - timedelta(days=1))
    if not active_weeks:
        quality = "no_data"
    elif len(active_weeks) < max(2, weeks // 2):
        quality = "sparse"
    else:
        quality = "ok"
    manual = manual_baseline(conn)
    if quality != "ok" and manual:
        return _baseline_from_manual(manual, as_of, weeks, state)
    return {
        "as_of": as_of.isoformat(), "weeks": weeks, "window": [start.isoformat(), (this_monday - timedelta(days=1)).isoformat()],
        "data_quality": quality,
        "avg_endurance_hours": round(avg_hours, 2),
        "avg_endurance_load": round(avg_load, 1),
        "max_week_endurance_hours": max((w["endurance_hours"] for w in weekly), default=0.0),
        "max_week_endurance_load": max((w["endurance_load"] for w in weekly), default=0.0),
        "load_per_hour": round(avg_load / avg_hours, 1) if avg_hours > 0 else None,
        "strength_sessions_per_week": by_sport.get("strength", {}).get("sessions_per_week", 0.0),
        "longest_min_by_sport": {s: v["longest_min"] for s, v in by_sport.items() if s in ENDURANCE_SPORTS},
        "by_sport": by_sport,
        "weekly": weekly,
        "acwr_last_week": acwr,
        "ctl_endurance": state["ctl_endurance"],
        "form_endurance": state["form_endurance"],
    }


ACWR_MAX = 1.3


def manual_baseline(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Selbstauskunft zum aktuellen Umfang (settings.manual_baseline), z. B. nach längerer Sync-Pause."""
    raw = get_setting(conn, "manual_baseline")
    if not raw:
        return None
    from .db import SettingError, _validate_manual_baseline
    try:
        _validate_manual_baseline(raw)
        return json.loads(raw)
    except (ValueError, SettingError):
        return None  # fehlerhafte Altwerte: so, als gäbe es keine Selbstauskunft


def _baseline_from_manual(m: dict[str, Any], as_of: date, weeks: int, state: dict[str, Any]) -> dict[str, Any]:
    lph = float(m.get("load_per_hour") or 50)
    by_sport = {}
    for sport, v in (m.get("sports") or {}).items():
        hours = float(v.get("hours_per_week") or 0)
        if not hours and v.get("minutes") and v.get("sessions_per_week"):
            hours = float(v["minutes"]) * float(v["sessions_per_week"]) / 60
        by_sport[sport] = {"hours_per_week": round(hours, 2),
                           "sessions_per_week": float(v.get("sessions_per_week") or 0),
                           "longest_min": int(v.get("longest_min") or 0),
                           "load_per_week": round(hours * lph, 1) if sport in ENDURANCE_SPORTS else None}
    endu_h = sum(v["hours_per_week"] for s, v in by_sport.items() if s in ENDURANCE_SPORTS)
    load = endu_h * lph
    return {
        "as_of": as_of.isoformat(), "weeks": weeks, "window": None, "data_quality": "manual",
        "manual_as_of": m.get("as_of"),
        "avg_endurance_hours": round(endu_h, 2), "avg_endurance_load": round(load, 1),
        "max_week_endurance_hours": round(endu_h, 2), "max_week_endurance_load": round(load, 1),
        "load_per_hour": lph,
        "strength_sessions_per_week": by_sport.get("strength", {}).get("sessions_per_week", 0.0),
        "longest_min_by_sport": {s: v["longest_min"] for s, v in by_sport.items() if s in ENDURANCE_SPORTS},
        "by_sport": by_sport,
        "weekly": [{"week": "Selbstauskunft", "endurance_hours": round(endu_h, 2), "total_hours": round(endu_h, 2),
                    "endurance_load": round(load, 1), "total_load": round(load, 1), "sessions": 0}] * weeks,
        "acwr_last_week": None,
        # ohne Historie: CTL als Tagesmittel der gewohnten Wochenlast schätzen
        "ctl_endurance": state["ctl_endurance"] or round(load / 7, 1),
        "form_endurance": state["form_endurance"],
    }


def load_corridor(conn: sqlite3.Connection, reference_load: float, ctl: float | None,
                  recovery: bool = False, chronic_load: float | None = None) -> dict[str, float]:
    """Zulässiger Bereich der Wochenlast.

    Obergrenze: min(Referenz + x %, 1,3 × chronische Wochenlast, Last, die CTL um höchstens den Ramp-Grenzwert hebt).
    Untergrenze: Belastungswochen nicht unter underload_min_pct der chronischen Last – sonst Formverlust.
    """
    inc = get_float(conn, "max_weekly_load_increase_pct", 10) / 100
    ramp = get_float(conn, "max_ctl_ramp_per_week", 6)
    under = get_float(conn, "underload_min_pct", 85) / 100
    chronic = chronic_load if chronic_load else reference_load
    upper = min(reference_load * (1 + inc), chronic * ACWR_MAX)
    if ctl is not None and ctl > 0:
        # konstante Tageslast L hebt CTL in 7 Tagen um (L - CTL) * CTL_WEEK_RESPONSE
        upper = min(upper, 7 * (ctl + ramp / CTL_WEEK_RESPONSE))
    lower = 0.0 if recovery else chronic * under
    return {"lower": round(min(lower, upper), 1), "upper": round(upper, 1)}


def week_planned_load(conn: sqlite3.Connection, plan_id: int, monday: date) -> float:
    row = conn.execute("SELECT coalesce(sum(target_load), 0) AS l FROM plan_sessions WHERE plan_id = ? "
                       "AND date BETWEEN ? AND ? AND status != 'deleted'",
                       (plan_id, monday.isoformat(), (monday + timedelta(days=6)).isoformat())).fetchone()
    return float(row["l"])


def week_done_load(conn: sqlite3.Connection, monday: date) -> float:
    df = activities_frame(conn, start=monday, end=monday + timedelta(days=6))
    return float(df[df["is_endurance"] == 1]["eff_load"].sum())


__all__ = ["form_curves", "form_state", "week_summary", "zone_distribution", "wellness_trend",
           "zone_timeline", "zone_weeks",
           "training_baseline", "load_corridor", "week_days"]
