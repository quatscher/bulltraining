"""Aktivitäten: Normalisierung aus intervals.icu, lokale Erfassung (Chat), Korrekturen."""
from __future__ import annotations

import json
from datetime import datetime
import sqlite3
from typing import Any

from .db import now_iso, row_to_dict, transaction
from .util import ENDURANCE_SPORTS, SPORTS, map_icu_type, to_date

# Spalten, die der Sync besitzt und bei jedem Durchlauf überschreiben darf.
SYNC_COLUMNS = ("start_date", "sport", "name", "duration_s", "distance_m", "hr_avg", "hr_max",
                "power_avg", "load", "load_method", "zone_times", "raw")


class ActivityError(ValueError):
    pass


def srpe_load(rpe: int | None, duration_s: int) -> float | None:
    """Session-RPE nach Foster: RPE mal Dauer in Minuten (Rohwert, ohne Kalibrierfaktor)."""
    if rpe is None:
        return None
    return round(rpe * duration_s / 60.0, 1)


def _zone_times(a: dict[str, Any], sport: str) -> str | None:
    power = a.get("icu_zone_times")
    if sport == "ride" and isinstance(power, list) and power:
        secs = [int(z.get("secs") or 0) for z in power
                if isinstance(z, dict) and str(z.get("id", "")).startswith("Z")]
        if secs:
            return json.dumps({"kind": "power", "secs": secs})
    hr = a.get("icu_hr_zone_times")
    if isinstance(hr, list) and hr:
        return json.dumps({"kind": "hr", "secs": [int(s or 0) for s in hr]})
    return None


def normalize_intervals_activity(a: dict[str, Any]) -> dict[str, Any]:
    sport, is_endurance = map_icu_type(a.get("type"))
    duration_s = int(a.get("moving_time") or a.get("elapsed_time") or 0)
    power = a.get("icu_average_watts") or a.get("average_watts")
    hr_avg = a.get("average_heartrate")
    rpe = a.get("icu_rpe") or a.get("perceived_exertion")
    rpe = int(round(rpe)) if isinstance(rpe, (int, float)) and 1 <= rpe <= 10 else None
    load = a.get("icu_training_load")
    if load:
        method = "power" if sport == "ride" and power else ("hr" if hr_avg else "icu")
    elif rpe is not None:
        # Fehlt jede Messung (z. B. Schwimmen ohne Puls), fällt die Einheit auf sRPE zurück.
        load, method = srpe_load(rpe, duration_s), "srpe"
    else:
        load, method = None, None
    return {
        "source": "intervals",
        "external_id": str(a["id"]),
        "start_date": str(a.get("start_date_local") or a.get("start_date") or "")[:19],
        "sport": sport,
        "name": a.get("name"),
        "duration_s": duration_s,
        "distance_m": a.get("distance"),
        "hr_avg": int(hr_avg) if hr_avg else None,
        "hr_max": int(a["max_heartrate"]) if a.get("max_heartrate") else None,
        "power_avg": int(power) if power else None,
        "rpe": rpe,
        "load": float(load) if load is not None else None,
        "load_method": method,
        "is_endurance": is_endurance,
        "zone_times": _zone_times(a, sport),
        "raw": json.dumps(a, ensure_ascii=False),
    }


def upsert_intervals_activity(conn: sqlite3.Connection, a: dict[str, Any]) -> str:
    """Upsert auf external_id. Gibt 'created' oder 'updated' zurück.

    Von Hand getroffene Entscheidungen (is_endurance, excluded, rpe, notes) bleiben erhalten,
    sobald user_locked gesetzt ist – sonst würde der nächste Resync jede Dublettenauflösung zurückdrehen.
    """
    existing = conn.execute("SELECT id, user_locked, rpe, raw FROM activities WHERE external_id = ? "
                            "AND source = 'intervals'", (str(a["id"]),)).fetchone()
    if existing is not None and existing["raw"]:
        # Die Aktivitätsliste liefert nur eine Zusammenfassung; Detailfelder (Intervalle, Zonen) aus dem früheren
        # Detailabruf bleiben erhalten, die Zusammenfassung aktualisiert den Rest.
        try:
            a = {**json.loads(existing["raw"]), **a}
        except ValueError:
            pass
    row = normalize_intervals_activity(a)
    ts = now_iso()
    if existing is not None:
        # Last aus dem wirksamen Zustand bestimmen: von Hand eingetragene RPE trägt die Last, wenn keine Messung da ist
        rpe_eff = existing["rpe"] if existing["user_locked"] else (row["rpe"] if row["rpe"] is not None else existing["rpe"])
        # Keine Messlast (oder nur eine aus der Quell-RPE abgeleitete): Last aus der wirksamen RPE. Echte
        # Messlast (Leistung/Puls) bleibt unberührt.
        if row["load_method"] in (None, "srpe") and rpe_eff is not None:
            row["rpe"] = rpe_eff
            row["load"], row["load_method"] = srpe_load(rpe_eff, row["duration_s"]), "srpe"
    if existing is None:
        cols = list(row) + ["created_at", "updated_at"]
        conn.execute(f"INSERT INTO activities ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [*row.values(), ts, ts])
        return "created"
    cols = list(SYNC_COLUMNS)
    values = [row[c] for c in cols]
    if not existing["user_locked"]:
        cols += ["is_endurance", "rpe"]
        values += [row["is_endurance"], row["rpe"] if row["rpe"] is not None else existing["rpe"]]
    assignments = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(f"UPDATE activities SET {assignments}, updated_at = ? WHERE id = ?",
                 [*values, ts, existing["id"]])
    return "updated"


def get_activity(conn: sqlite3.Connection, activity_id: int, include_raw: bool = False) -> dict[str, Any] | None:
    d = row_to_dict(conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone(),
                    ("zone_times", "raw"))
    if d is None:
        return None
    raw = d.pop("raw", None)
    if include_raw:
        d["raw"] = raw
    elif isinstance(raw, dict) and raw.get("icu_intervals"):
        d["intervals"] = [
            {k: iv.get(k) for k in ("label", "type", "moving_time", "distance", "average_watts",
                                     "average_heartrate", "average_speed") if iv.get(k) is not None}
            for iv in raw["icu_intervals"][:30]
        ]
    return d


def log_activity(conn: sqlite3.Connection, *, date: str, sport: str, duration_min: float,
                 rpe: int | None, hr_avg: int | None = None, name: str | None = None,
                 notes: str | None = None, time: str = "12:00", is_endurance: bool | None = None,
                 distance_km: float | None = None) -> dict[str, Any]:
    """Lokale Aktivität erfassen (Krafttraining u. ä.). RPE ist Pflicht – geschätzt wird nicht."""
    if rpe is None:
        raise ActivityError("RPE fehlt. Bitte beim Athleten nachfragen (1 = sehr leicht, 10 = maximal), nicht schätzen.")
    if not 1 <= int(rpe) <= 10:
        raise ActivityError("RPE muss zwischen 1 und 10 liegen.")
    if sport not in SPORTS:
        raise ActivityError(f"Unbekannte Sportart '{sport}'. Erlaubt: {', '.join(SPORTS)}")
    if duration_min <= 0 or duration_min > 24 * 60:
        raise ActivityError("Dauer muss zwischen 1 Minute und 24 Stunden liegen.")
    d = to_date(date)
    duration_s = int(round(duration_min * 60))
    endurance = int(is_endurance if is_endurance is not None else sport in ENDURANCE_SPORTS)
    ts = now_iso()
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO activities (source, external_id, start_date, sport, name, duration_s, distance_m,
                   hr_avg, rpe, load, load_method, is_endurance, notes, created_at, updated_at)
               VALUES ('local', NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'srpe', ?, ?, ?, ?)""",
            (f"{d.isoformat()}T{time}:00"[:19], sport, name or sport.capitalize(), duration_s,
             distance_km * 1000 if distance_km else None, hr_avg, int(rpe), srpe_load(int(rpe), duration_s),
             endurance, notes, ts, ts),
        )
    return get_activity(conn, cur.lastrowid)


_EDITABLE_LOCAL = {"name", "notes", "rpe", "duration_s", "sport", "is_endurance", "excluded", "hr_avg", "start_date", "distance_m"}
_EDITABLE_EXTERNAL = {"notes", "rpe", "is_endurance", "excluded"}


def update_activity(conn: sqlite3.Connection, activity_id: int, **fields: Any) -> dict[str, Any]:
    act = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if act is None:
        raise ActivityError(f"Aktivität {activity_id} existiert nicht.")
    fields = {k: v for k, v in fields.items() if v is not None}
    allowed = _EDITABLE_LOCAL if act["source"] == "local" else _EDITABLE_EXTERNAL
    illegal = set(fields) - allowed
    if illegal:
        raise ActivityError(f"Nicht änderbar bei source='{act['source']}': {', '.join(sorted(illegal))}. "
                            "Gespiegelte Messwerte werden in intervals.icu korrigiert.")
    if "rpe" in fields and not 1 <= int(fields["rpe"]) <= 10:
        raise ActivityError("RPE muss zwischen 1 und 10 liegen.")
    if "sport" in fields and fields["sport"] not in SPORTS:
        raise ActivityError(f"Unbekannte Sportart '{fields['sport']}'.")
    if "duration_s" in fields:
        fields["duration_s"] = int(fields["duration_s"])
        if not 60 <= fields["duration_s"] <= 24 * 3600:
            raise ActivityError("Dauer muss zwischen 1 Minute und 24 Stunden liegen.")
    if "start_date" in fields:
        try:
            start = datetime.fromisoformat(str(fields["start_date"]).strip())
        except ValueError:
            raise ActivityError(f"Startzeit '{fields['start_date']}' ist ungültig (YYYY-MM-DDTHH:MM).")
        fields["start_date"] = start.replace(microsecond=0).isoformat()[:19]
    if "hr_avg" in fields and not 30 <= int(fields["hr_avg"]) <= 250:
        raise ActivityError("Puls muss zwischen 30 und 250 liegen.")
    if "distance_m" in fields and float(fields["distance_m"]) < 0:
        raise ActivityError("Distanz darf nicht negativ sein.")
    merged = {**dict(act), **fields}
    if merged["load_method"] == "srpe" or (act["load"] is None and merged.get("rpe")):
        fields["load"] = srpe_load(merged["rpe"], merged["duration_s"])
        fields["load_method"] = "srpe"
    if act["source"] == "intervals" and ({"is_endurance", "excluded", "rpe"} & set(fields)):
        fields["user_locked"] = 1
    if fields:
        with transaction(conn):
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE activities SET {assignments}, updated_at = ? WHERE id = ?",
                         [*fields.values(), now_iso(), activity_id])
    return get_activity(conn, activity_id)
