"""Publisher: bestätigte Einheiten nach intervals.icu schreiben; Abgleich geplant gegen absolviert."""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
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


LOCK_STALE_MIN = 10
RECONCILE_LOOKBACK_DAYS = 14


def _acquire_lock(conn: sqlite3.Connection) -> bool:
    """Exklusive Veröffentlichung über alle Prozesse (Web, CLI): sonst sehen zwei Läufe dieselbe Einheit ohne
    Event-ID und legen sie zweimal an. Ein hängengebliebener Lauf gibt die Sperre nach LOCK_STALE_MIN frei."""
    stale = (datetime.now() - timedelta(minutes=LOCK_STALE_MIN)).replace(microsecond=0).isoformat()
    conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('publish_lock', '')")
    cur = conn.execute("UPDATE settings SET value = ? WHERE key = 'publish_lock' AND (value = '' OR value < ?)",
                       (now_iso(), stale))
    return cur.rowcount == 1


def _release_lock(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE settings SET value = '' WHERE key = 'publish_lock'")


def _is_not_found(exc: Exception) -> bool:
    text = str(exc)
    return "404" in text or "not found" in text.lower()


def publish(conn: sqlite3.Connection, client: IntervalsClient, today: date | None = None,
            horizon_days: int = 14) -> dict[str, Any]:
    """Gleicht den Kalender in intervals.icu mit dem lokalen Plan ab.

    - neue Einheiten des aktiven Plans: nur von heute bis `horizon_days` (was weiter weg liegt, ändert sich noch)
    - bereits veröffentlichte Einheiten: Änderungen und Löschungen immer, unabhängig von Datum und Planstatus
    - Rückmeldungen werden nur gegen die Revision geschrieben, die versendet wurde. Hat sich die Einheit während
      des HTTP-Aufrufs geändert (Bestätigen, Undo, Archivieren), bleibt sie zur Synchronisation vorgemerkt und wird
      im selben Lauf noch einmal abgeglichen.
    Jede Einheit einzeln: ein Fehler blockiert die übrigen nicht, er wird an der Einheit gespeichert.
    """
    today = today or date.today()
    result = {"created": 0, "updated": 0, "deleted": 0, "requeued": 0, "held": 0, "errors": []}
    if not _acquire_lock(conn):
        result["errors"].append({"session_id": None, "error": "Veröffentlichung läuft bereits"})
        return result
    try:
        for attempt in range(3):  # Nachzügler aus zwischenzeitlichen Änderungen im selben Lauf abgleichen
            requeued_before = result["requeued"]
            for s in _pending(conn, today, horizon_days):
                _publish_one(conn, client, s, result)
            if result["requeued"] == requeued_before:
                break
    finally:
        _release_lock(conn)
    return result


def _pending(conn: sqlite3.Connection, today: date, horizon_days: int) -> list[dict[str, Any]]:
    known = conn.execute(
        "SELECT * FROM plan_sessions WHERE status IN ('planned', 'deleted') "
        "AND (external_event_id IS NOT NULL OR status = 'deleted') ORDER BY date").fetchall()
    new = conn.execute(
        "SELECT ps.* FROM plan_sessions ps JOIN plans p ON p.id = ps.plan_id AND p.status = 'active' "
        "WHERE ps.status = 'planned' AND ps.external_event_id IS NULL AND ps.date >= ? AND ps.date <= ? "
        "ORDER BY ps.date",
        (today.isoformat(), (today + timedelta(days=horizon_days)).isoformat())).fetchall()
    return [dict(r) for r in list(known) + list(new)]


_DEFINITE_CLIENT_ERROR = re.compile(r"HTTP 4\d\d")


def _is_definite_failure(exc: Exception) -> bool:
    """Klare Ablehnung (HTTP 4xx): das Event wurde nicht angelegt. Alles andere (Timeout, Verbindungsabbruch,
    5xx) ist unklar – es kann extern trotzdem angelegt worden sein."""
    return bool(_DEFINITE_CLIENT_ERROR.search(str(exc)))


def _find_existing(client: IntervalsClient, s: dict[str, Any]) -> str | None | bool:
    """Nach unklarem POST: passendes Event suchen. Rückgabe: ID, None (sicher nicht vorhanden) oder False
    (kein Abgleich möglich)."""
    finder = getattr(client, "find_event", None)
    if not callable(finder):
        return False
    payload = event_payload(s)
    found = finder(payload["start_date_local"][:10], payload["name"], payload["category"])
    return str(found["id"]) if found else None


def _publish_one(conn: sqlite3.Connection, client: IntervalsClient, s: dict[str, Any], result: dict[str, Any]) -> None:
    sid, rev, status, ext_id = s["id"], s["revision"], s["status"], s["external_event_id"]
    try:
        if status == "deleted":
            if ext_id:
                try:
                    client.delete_event(ext_id)
                except Exception as exc:  # noqa: BLE001
                    if not _is_not_found(exc):
                        raise
            gone = conn.execute("DELETE FROM plan_sessions WHERE id = ? AND revision = ? AND status = 'deleted'",
                                (sid, rev)).rowcount
            if gone:
                result["deleted"] += 1
            else:
                # während des Löschens wiederhergestellt/geändert: das externe Event ist weg -> neu anlegen lassen
                conn.execute("UPDATE plan_sessions SET external_event_id = NULL WHERE id = ? AND external_event_id IS ?",
                             (sid, ext_id))
                result["requeued"] += 1
            return
        if ext_id:
            try:
                client.update_event(ext_id, event_payload(s))
                result["updated"] += 1
            except Exception as exc:  # noqa: BLE001
                if not _is_not_found(exc):
                    raise
                ext_id = None  # extern verschwunden (z. B. nach Undo einer Löschung) -> neu anlegen
        if not ext_id:
            if s.get("publish_unknown"):
                existing = _find_existing(client, s)
                if existing is False:
                    result["held"] += 1
                    result["errors"].append({"session_id": sid, "error": "Unklar, ob bereits in intervals.icu "
                                             "angelegt – bitte dort prüfen und im Posteingang freigeben."})
                    return
                if existing:
                    client.update_event(existing, event_payload(s))
                    ext_id = existing
                    result["updated"] += 1
            if not ext_id:
                try:
                    resp = client.create_event(event_payload(s))
                except Exception as exc:  # noqa: BLE001
                    if not _is_definite_failure(exc):
                        conn.execute("UPDATE plan_sessions SET publish_unknown = 1 WHERE id = ?", (sid,))
                    raise
                ext_id = str(resp["id"])
                result["created"] += 1
        done = conn.execute(
            "UPDATE plan_sessions SET status = 'published', external_event_id = ?, publish_error = NULL, "
            "publish_unknown = 0, updated_at = ? WHERE id = ? AND revision = ? AND status = ?",
            (ext_id, now_iso(), sid, rev, status)).rowcount
        if not done:
            _record_after_concurrent_change(conn, client, sid, ext_id, result)
    except Exception as exc:  # noqa: BLE001 – Fehler je Einheit sichtbar machen, Rest weiter veröffentlichen
        conn.execute("UPDATE plan_sessions SET publish_error = ?, updated_at = ? WHERE id = ?",
                     (str(exc)[:500], now_iso(), sid))
        result["errors"].append({"session_id": sid, "error": str(exc)[:200]})


def _record_after_concurrent_change(conn: sqlite3.Connection, client: IntervalsClient, sid: int, ext_id: str,
                                    result: dict[str, Any]) -> None:
    """Die Einheit hat sich während des HTTP-Aufrufs geändert. Externe ID trotzdem festhalten (sonst entstünde ein
    verwaistes Event), Status aber nicht auf 'published' setzen: der neuere Stand ist noch nicht übertragen."""
    row = conn.execute("SELECT status FROM plan_sessions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        # inzwischen physisch gelöscht (war noch unveröffentlicht) -> gerade angelegtes Event wieder entfernen
        try:
            client.delete_event(ext_id)
        except Exception as exc:  # noqa: BLE001
            if not _is_not_found(exc):
                raise
        return
    conn.execute("UPDATE plan_sessions SET external_event_id = ?, publish_unknown = 0 WHERE id = ?", (ext_id, sid))
    if row["status"] in ("planned", "deleted"):
        result["requeued"] += 1


def reconcile(conn: sqlite3.Connection, today: date | None = None) -> dict[str, int]:
    """Geplante Einheiten mit absolvierten Aktivitäten verbinden: gleicher Tag und gleiche Sportart -> 'done';
    vergangene Einheiten ohne Aktivität -> 'skipped'. Automatisch übersprungene Einheiten der letzten
    RECONCILE_LOOKBACK_DAYS Tage werden erneut geprüft – Uploads kommen manchmal Tage später."""
    today = today or date.today()
    done = skipped = 0
    rows = conn.execute(
        "SELECT ps.* FROM plan_sessions ps JOIN plans p ON p.id = ps.plan_id "
        "WHERE ps.date < ? AND (ps.status IN ('planned', 'published') OR (ps.status = 'skipped' AND ps.date >= ?)) "
        "ORDER BY ps.date",
        (today.isoformat(), (today - timedelta(days=RECONCILE_LOOKBACK_DAYS)).isoformat())).fetchall()
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
        elif s["status"] != "skipped" and to_date(s["date"]) < today - timedelta(days=1):
            # einen Tag Karenz: Uploads kommen oft verspätet
            conn.execute("UPDATE plan_sessions SET status = 'skipped', updated_at = ? WHERE id = ?", (now_iso(), s["id"]))
            skipped += 1
    return {"done": done, "skipped": skipped}
