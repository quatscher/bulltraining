"""Publisher: bestätigte Einheiten nach intervals.icu schreiben; Abgleich geplant gegen absolviert."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

from .db import now_iso
from .intervals_client import IntervalsClient
from .util import SPORT_TO_ICU, to_date


def event_payload(s: dict[str, Any]) -> dict[str, Any]:
    category = f"RACE_{s['race_priority'] or 'A'}" if s["category"] == "RACE" else "WORKOUT"
    payload = {
        "start_date_local": f"{s['date']}T00:00:00",  # ohne Uhrzeit, muss auf T00:00:00 enden
        "category": category,
        "type": SPORT_TO_ICU.get(s["sport"], "Workout"),
        "name": s["title"],
        "description": s["description"] or "",
        "moving_time": int(s["duration_s"]),
    }
    if s.get("target_load"):
        payload["icu_training_load"] = round(float(s["target_load"]))
    return payload


def publish(conn: sqlite3.Connection, client: IntervalsClient, today: date | None = None,
            horizon_days: int = 14) -> dict[str, Any]:
    """Veröffentlicht Einheiten des aktiven Plans von heute bis `horizon_days`. Jede Einheit einzeln –
    ein Fehler blockiert die übrigen nicht, der Status bleibt dann 'planned' und der Fehler wird gespeichert."""
    today = today or date.today()
    plan = conn.execute("SELECT id FROM plans WHERE status = 'active'").fetchone()
    result = {"created": 0, "updated": 0, "deleted": 0, "errors": []}
    if not plan:
        return result
    rows = conn.execute(
        "SELECT * FROM plan_sessions WHERE plan_id = ? AND date >= ? AND date <= ? "
        "AND status IN ('planned', 'deleted') ORDER BY date",
        (plan["id"], today.isoformat(), (today + timedelta(days=horizon_days)).isoformat())).fetchall()
    for r in rows:
        s = dict(r)
        try:
            if s["status"] == "deleted":
                if s["external_event_id"]:
                    client.delete_event(s["external_event_id"])
                conn.execute("DELETE FROM plan_sessions WHERE id = ?", (s["id"],))
                result["deleted"] += 1
                continue
            if s["external_event_id"]:
                client.update_event(s["external_event_id"], event_payload(s))
                ext_id = s["external_event_id"]
                result["updated"] += 1
            else:
                resp = client.create_event(event_payload(s))
                ext_id = str(resp["id"])
                result["created"] += 1
            conn.execute("UPDATE plan_sessions SET status = 'published', external_event_id = ?, publish_error = NULL, "
                         "updated_at = ? WHERE id = ?", (ext_id, now_iso(), s["id"]))
        except Exception as exc:  # noqa: BLE001 – Fehler je Einheit sichtbar machen, Rest weiter veröffentlichen
            conn.execute("UPDATE plan_sessions SET publish_error = ?, updated_at = ? WHERE id = ?",
                         (str(exc)[:500], now_iso(), s["id"]))
            result["errors"].append({"session_id": s["id"], "error": str(exc)[:200]})
    return result


def reconcile(conn: sqlite3.Connection, today: date | None = None) -> dict[str, int]:
    """Geplante Einheiten mit absolvierten Aktivitäten verbinden: gleicher Tag und gleiche Sportart -> 'done';
    vergangene Einheiten ohne Aktivität -> 'skipped'. Wettkämpfe und Tests werden genauso behandelt."""
    today = today or date.today()
    done = skipped = 0
    rows = conn.execute(
        "SELECT ps.* FROM plan_sessions ps JOIN plans p ON p.id = ps.plan_id "
        "WHERE ps.status IN ('planned', 'published') AND ps.date < ? ORDER BY ps.date", (today.isoformat(),)).fetchall()
    used = {r["activity_id"] for r in conn.execute("SELECT activity_id FROM plan_sessions WHERE activity_id IS NOT NULL")}
    for s in rows:
        acts = conn.execute(
            "SELECT id, duration_s FROM activities WHERE excluded = 0 AND sport = ? AND substr(start_date,1,10) = ? "
            "ORDER BY abs(duration_s - ?)", (s["sport"], s["date"], s["duration_s"])).fetchall()
        match = next((a for a in acts if a["id"] not in used), None)
        if match:
            used.add(match["id"])
            conn.execute("UPDATE plan_sessions SET status = 'done', activity_id = ?, updated_at = ? WHERE id = ?",
                         (match["id"], now_iso(), s["id"]))
            done += 1
        elif to_date(s["date"]) < today - timedelta(days=1):
            # einen Tag Karenz: Uploads kommen oft verspätet
            conn.execute("UPDATE plan_sessions SET status = 'skipped', updated_at = ? WHERE id = ?", (now_iso(), s["id"]))
            skipped += 1
    return {"done": done, "skipped": skipped}
