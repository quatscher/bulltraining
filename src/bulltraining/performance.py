"""Leistungstests: Protokolle, Auswertung, Schwellenwerte, Leistungszustand und Zonen.

Tests sind die Grundlage des Plans. Ohne gültigen Test je Sportart plant der Generator zuerst den Test
und hält harte Einheiten dieser Sportart zurück, bis ein Ergebnis vorliegt.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from .db import get_float, get_setting, now_iso, row_to_dict, set_setting, transaction
from .util import ENDURANCE_SPORTS, fmt_pace, parse_duration, to_date


class TestError(ValueError):
    pass


# --- Protokolle ------------------------------------------------------------------
# inputs: name -> (Beschreibung, Pflicht). Formeln sind gängige Feldtest-Näherungen, keine eigene Wissenschaft.
PROTOCOLS: dict[str, dict[str, Any]] = {
    "ride_ftp20": {
        "sport": "ride", "name": "FTP-Test 20 min",
        "summary": "20 min maximal gleichmäßig nach Vorbelastung. FTP = 95 % der Durchschnittsleistung.",
        "inputs": {"avg_power_20min_w": ("Ø Leistung 20 min in Watt", True),
                   "avg_hr_20min": ("Ø Puls 20 min", False), "max_hr": ("höchster Puls", False)},
        "duration_min": 65,
        "description": ("Einfahren\n- 15m 55-65%\n3x\n- 1m 100%\n- 1m 55%\n- 5m 105%\n- 10m 50%\n\n"
                        "Test: 20 Minuten so hoch wie gleichmäßig haltbar\n- 20m 100%\n\nAusfahren\n- 10m 50%"),
    },
    "ride_ramp": {
        "sport": "ride", "name": "Rampentest",
        "summary": "Stufen je 1 min, +20 W bis zum Abbruch. FTP = 75 % der besten 1-min-Leistung.",
        "inputs": {"best_1min_power_w": ("beste 1-min-Leistung in Watt", True), "max_hr": ("höchster Puls", False)},
        "duration_min": 40,
        "description": "Einfahren\n- 10m 50%\n\nRampe bis zum Abbruch, jede Minute +20 W\n- 25m ramp 50-150%\n\nAusfahren\n- 10m 45%",
    },
    "run_30min_tt": {
        "sport": "run", "name": "Lauf-Schwellentest 30 min",
        "summary": "30 min allein maximal. Schwellenpace = Ø Pace, Schwellenpuls = Ø Puls der letzten 20 min.",
        "inputs": {"distance_m": ("Strecke in 30 min, Meter", True),
                   "avg_hr_last_20min": ("Ø Puls letzte 20 min", False), "max_hr": ("höchster Puls", False)},
        "duration_min": 60,
        "description": "Einlaufen\n- 15m Z1-Z2 HR\n4x\n- 20s 110% Pace\n- 40s Z1 HR\n\nTest: 30 Minuten maximal gleichmäßig\n- 30m 100% Pace\n\nAuslaufen\n- 10m Z1 HR",
    },
    "run_5k_tt": {
        "sport": "run", "name": "5-km-Zeitlauf",
        "summary": "5 km maximal. Schwellenpace über Riegel-Formel auf 60 min hochgerechnet (Schätzung).",
        "inputs": {"time_5k": ("Zeit 5 km, mm:ss", True), "avg_hr": ("Ø Puls", False), "max_hr": ("höchster Puls", False)},
        "duration_min": 50,
        "description": "Einlaufen\n- 15m Z1-Z2 HR\n4x\n- 20s 110% Pace\n- 40s Z1 HR\n\nTest: 5 km maximal\n- 5km 105% Pace\n\nAuslaufen\n- 10m Z1 HR",
    },
    "swim_css": {
        "sport": "swim", "name": "CSS-Test 400/200",
        "summary": "400 m und 200 m maximal mit Pause. CSS = (t400 − t200) / 2 je 100 m.",
        "inputs": {"t400": ("Zeit 400 m, mm:ss", True), "t200": ("Zeit 200 m, mm:ss", True)},
        "duration_min": 45,
        "description": "Einschwimmen\n- 400mtr Z1\n4x\n- 50mtr Z3\n- 15s rest\n\nTest\n- 400mtr max\n- 5m rest\n- 200mtr max\n\nAusschwimmen\n- 200mtr Z1",
    },
}

DEFAULT_PROTOCOL = {"ride": "ride_ftp20", "run": "run_30min_tt", "swim": "swim_css"}

# Kennwerte, die Tests liefern: key -> (Sportart, Beschriftung, kleiner ist besser)
METRICS: dict[str, tuple[str, str, bool]] = {
    "ftp_w": ("ride", "FTP", False),
    "threshold_pace_run_s_per_km": ("run", "Schwellenpace", True),
    "lthr_run": ("run", "Schwellenpuls Laufen", False),
    "css_s_per_100m": ("swim", "CSS", True),
}
PRIMARY_METRIC = {"ride": "ftp_w", "run": "threshold_pace_run_s_per_km", "swim": "css_s_per_100m"}

_PLAUSIBLE = {
    "ftp_w": (50, 600), "threshold_pace_run_s_per_km": (150, 600), "lthr_run": (100, 210),
    "css_s_per_100m": (55, 240), "max_hr": (120, 230),
}


def evaluate(protocol: str, inputs: dict[str, Any]) -> dict[str, float]:
    if protocol not in PROTOCOLS:
        raise TestError(f"Unbekanntes Protokoll '{protocol}'. Verfügbar: {', '.join(PROTOCOLS)}")
    spec = PROTOCOLS[protocol]
    missing = [k for k, (_, req) in spec["inputs"].items() if req and inputs.get(k) in (None, "")]
    if missing:
        raise TestError(f"Fehlende Messwerte für {spec['name']}: {', '.join(missing)}")
    r: dict[str, float] = {}
    if protocol == "ride_ftp20":
        r["ftp_w"] = round(0.95 * float(inputs["avg_power_20min_w"]))
    elif protocol == "ride_ramp":
        r["ftp_w"] = round(0.75 * float(inputs["best_1min_power_w"]))
    elif protocol == "run_30min_tt":
        r["threshold_pace_run_s_per_km"] = round(1800 / (float(inputs["distance_m"]) / 1000), 1)
        if inputs.get("avg_hr_last_20min"):
            r["lthr_run"] = round(float(inputs["avg_hr_last_20min"]))
    elif protocol == "run_5k_tt":
        t = parse_duration(inputs["time_5k"])
        # Riegel: T2 = T1 * (D2/D1)^1.06 -> Strecke, die in 60 min gelaufen würde
        d60 = 5000 * (3600 / t) ** (1 / 1.06)
        r["threshold_pace_run_s_per_km"] = round(3600 / (d60 / 1000), 1)
        if inputs.get("avg_hr"):
            r["lthr_run"] = round(0.98 * float(inputs["avg_hr"]))  # 5 km liegt knapp über der Schwelle
    elif protocol == "swim_css":
        t400, t200 = parse_duration(inputs["t400"]), parse_duration(inputs["t200"])
        if t400 <= t200:
            raise TestError("400-m-Zeit muss größer als die 200-m-Zeit sein.")
        r["css_s_per_100m"] = round((t400 - t200) / 2, 1)
    if inputs.get("max_hr"):
        r["max_hr"] = round(float(inputs["max_hr"]))
    for k, v in r.items():
        lo, hi = _PLAUSIBLE[k]
        if not lo <= v <= hi:
            raise TestError(f"Unplausibles Ergebnis {k}={v} (erwartet {lo}–{hi}). Messwerte prüfen.")
    return r


def record_test(conn: sqlite3.Connection, *, date: str, protocol: str, inputs: dict[str, Any],
                activity_id: int | None = None, notes: str | None = None) -> dict[str, Any]:
    """Test speichern und – wenn es der jüngste ist – die Schwellenwerte in `settings` übernehmen."""
    results = evaluate(protocol, inputs)
    sport = PROTOCOLS[protocol]["sport"]
    d = to_date(date).isoformat()
    before = {k: get_float(conn, k) for k in results}
    with transaction(conn):
        session = conn.execute(
            "SELECT id FROM plan_sessions WHERE date = ? AND category = 'TEST' AND sport = ? AND status != 'deleted' "
            "ORDER BY id LIMIT 1", (d, sport)).fetchone()
        cur = conn.execute(
            "INSERT INTO performance_tests(date, sport, protocol, inputs, results, activity_id, plan_session_id, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (d, sport, protocol, json.dumps(inputs), json.dumps(results), activity_id,
             session["id"] if session else None, notes, now_iso()))
        if session:
            conn.execute("UPDATE plan_sessions SET status = 'done', activity_id = coalesce(?, activity_id), updated_at = ? "
                         "WHERE id = ?", (activity_id, now_iso(), session["id"]))
        applied = {}
        for key, value in results.items():
            if key == "max_hr":
                if value > (get_float(conn, "max_hr") or 0):
                    set_setting(conn, key, value)
                    applied[key] = value
                continue
            newest = conn.execute(
                "SELECT max(date) AS d FROM performance_tests WHERE results LIKE ?", (f'%"{key}"%',)).fetchone()["d"]
            if newest == d:
                set_setting(conn, key, value)
                applied[key] = value
    changes = {}
    for k, v in results.items():
        old = before.get(k)
        changes[k] = {"before": old, "after": v,
                      "change_pct": round(100 * (v - old) / old, 1) if old else None,
                      "applied": k in applied}
    return {"test_id": cur.lastrowid, "date": d, "sport": sport, "protocol": protocol, "results": results,
            "changes": changes, "linked_plan_session": session["id"] if session else None,
            "hint": ("Schwellenwerte lokal übernommen. Beschreibungen geplanter Einheiten nutzen %-Angaben; "
                     "damit Uhr und intervals.icu dieselben Ziele zeigen, die Werte dort in den Sport-Einstellungen "
                     "angleichen.") if applied else None}


def list_tests(conn: sqlite3.Connection, sport: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM performance_tests"
    params: list[Any] = []
    if sport:
        sql += " WHERE sport = ?"
        params.append(sport)
    sql += " ORDER BY date DESC, id DESC LIMIT ?"
    params.append(limit)
    return [row_to_dict(r, ("inputs", "results")) for r in conn.execute(sql, params)]


def _display(key: str, value: float | None) -> str | None:
    if value is None:
        return None
    if key == "threshold_pace_run_s_per_km":
        return fmt_pace(value, "/km")
    if key == "css_s_per_100m":
        return fmt_pace(value, "/100m")
    if key == "ftp_w":
        return f"{value:.0f} W"
    return f"{value:.0f} bpm"


def _intervals_thresholds(conn: sqlite3.Connection) -> dict[str, Any]:
    """Schwellen, mit denen intervals.icu zuletzt gerechnet hat – zum Abgleich mit den lokalen Tests."""
    out: dict[str, Any] = {}
    for sport, keys in (("ride", ("icu_ftp",)), ("run", ("lthr", "threshold_pace"))):
        row = conn.execute("SELECT raw FROM activities WHERE source='intervals' AND sport = ? AND raw IS NOT NULL "
                           "ORDER BY start_date DESC LIMIT 1", (sport,)).fetchone()
        if not row:
            continue
        raw = json.loads(row["raw"])
        for k in keys:
            if raw.get(k):
                out[f"{sport}_{k}"] = raw[k]
    return out


def test_status(conn: sqlite3.Connection, sport: str, today: date | None = None) -> dict[str, Any]:
    """Gültigkeit des jüngsten Tests einer Sportart: valid | due_soon | stale | missing."""
    today = today or date.today()
    validity = int(get_float(conn, "test_validity_days", 56))
    lead = int(get_float(conn, "retest_lead_days", 14))
    row = conn.execute("SELECT * FROM performance_tests WHERE sport = ? ORDER BY date DESC, id DESC LIMIT 1",
                       (sport,)).fetchone()
    if row is None:
        return {"sport": sport, "status": "missing", "last_test": None, "age_days": None,
                "valid_until": None, "protocol": DEFAULT_PROTOCOL.get(sport)}
    age = (today - date.fromisoformat(row["date"])).days
    status = "stale" if age > validity else ("due_soon" if age > validity - lead else "valid")
    return {"sport": sport, "status": status, "last_test": row["date"], "age_days": age,
            "valid_until": (date.fromisoformat(row["date"]) + timedelta(days=validity)).isoformat(),
            "protocol": row["protocol"]}


def performance_state(conn: sqlite3.Connection, today: date | None = None,
                      sports: tuple[str, ...] = ENDURANCE_SPORTS) -> dict[str, Any]:
    """Aktueller Leistungszustand: Schwellenwerte aus Tests, Verlauf, Gültigkeit, Zonen, Abgleich."""
    from .metrics import form_state  # vermeidet Zyklus beim Import
    today = today or date.today()
    metrics = {}
    for key, (sport, label, lower_better) in METRICS.items():
        if sport not in sports:
            continue
        hist = [(r["date"], json.loads(r["results"])[key]) for r in conn.execute(
            "SELECT date, results FROM performance_tests WHERE sport = ? AND results LIKE ? ORDER BY date, id",
            (sport, f'%"{key}"%'))]
        current = hist[-1][1] if hist else get_float(conn, key)
        prev = hist[-2][1] if len(hist) > 1 else None
        change = round(100 * (current - prev) / prev, 1) if prev and current else None
        improved = None if change is None else (change < 0 if lower_better else change > 0)
        metrics[key] = {"sport": sport, "label": label, "value": current, "display": _display(key, current),
                        "tested_on": hist[-1][0] if hist else None, "previous": prev,
                        "previous_display": _display(key, prev), "change_pct": change, "improved": improved,
                        "history": [{"date": d, "value": v} for d, v in hist]}
    weight_row = conn.execute("SELECT weight_kg FROM wellness WHERE weight_kg IS NOT NULL ORDER BY date DESC LIMIT 1").fetchone()
    ftp = metrics.get("ftp_w", {}).get("value")
    tests = {s: test_status(conn, s, today) for s in sports}
    icu = _intervals_thresholds(conn)
    mismatches = []
    if ftp and icu.get("ride_icu_ftp") and abs(icu["ride_icu_ftp"] - ftp) / ftp > 0.03:
        mismatches.append(f"FTP lokal {ftp:.0f} W, intervals.icu rechnet mit {icu['ride_icu_ftp']} W")
    lthr = metrics.get("lthr_run", {}).get("value")
    if lthr and icu.get("run_lthr") and abs(icu["run_lthr"] - lthr) > 3:
        mismatches.append(f"Schwellenpuls Laufen lokal {lthr:.0f}, intervals.icu {icu['run_lthr']}")
    return {
        "date": today.isoformat(),
        "metrics": metrics,
        "tests": tests,
        "needs_test": [s for s, t in tests.items() if t["status"] in ("missing", "stale")],
        "ftp_w_per_kg": round(ftp / weight_row["weight_kg"], 2) if ftp and weight_row else None,
        "max_hr": get_float(conn, "max_hr"),
        "fitness": form_state(conn, today),
        "zones": zones(conn),
        "intervals_mismatch": mismatches,
    }


def zones(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Trainingszonen aus den aktuellen Testwerten."""
    out: dict[str, list[dict[str, Any]]] = {}
    ftp = get_float(conn, "ftp_w")
    if ftp:
        bands = [("Z1 Regeneration", 0, 55), ("Z2 Grundlage", 56, 75), ("Z3 Tempo", 76, 90), ("Z4 Schwelle", 91, 105),
                 ("Z5 VO2max", 106, 120), ("Z6 anaerob", 121, 150), ("Z7 neuromuskulär", 151, None)]
        out["ride_power"] = [{"zone": n, "from": round(ftp * lo / 100), "to": round(ftp * hi / 100) if hi else None}
                             for n, lo, hi in bands]
    pace = get_float(conn, "threshold_pace_run_s_per_km")
    if pace:
        # Prozent der Schwellengeschwindigkeit; Pace = Schwellenpace / Anteil
        bands = [("Z1 Regeneration", None, 78), ("Z2 Grundlage", 78, 88), ("Z3 Tempo", 88, 95),
                 ("Z4 Schwelle", 95, 102), ("Z5 VO2max", 102, None)]
        out["run_pace"] = [{"zone": n, "from": fmt_pace(pace / (lo / 100)) if lo else None,
                            "to": fmt_pace(pace / (hi / 100)) if hi else None} for n, lo, hi in bands]
    lthr = get_float(conn, "lthr_run")
    if lthr:
        bands = [("Z1", None, 85), ("Z2", 85, 89), ("Z3", 90, 94), ("Z4", 95, 99), ("Z5a", 100, 102),
                 ("Z5b", 103, 106), ("Z5c", 107, None)]
        out["run_hr"] = [{"zone": n, "from": round(lthr * lo / 100) if lo else None,
                          "to": round(lthr * hi / 100) if hi else None} for n, lo, hi in bands]
    css = get_float(conn, "css_s_per_100m")
    if css:
        bands = [("Z1 locker", 15, None), ("Z2 Grundlage", 8, 15), ("Z3 Tempo", 3, 8), ("Z4 CSS", -2, 3), ("Z5 schnell", None, -2)]
        out["swim_css"] = [{"zone": n, "from": fmt_pace(css + hi, "/100m") if hi is not None else None,
                            "to": fmt_pace(css + lo, "/100m") if lo is not None else None} for n, lo, hi in bands]
    return out


def protocols_overview() -> list[dict[str, Any]]:
    return [{"protocol": k, "sport": v["sport"], "name": v["name"], "summary": v["summary"],
             "inputs": {n: {"label": lbl, "required": req} for n, (lbl, req) in v["inputs"].items()}}
            for k, v in PROTOCOLS.items()]


def threshold_for(conn: sqlite3.Connection, sport: str) -> float | None:
    key = PRIMARY_METRIC.get(sport)
    return get_float(conn, key) if key else None


__all__ = ["PROTOCOLS", "record_test", "performance_state", "test_status", "zones", "evaluate", "get_setting"]
