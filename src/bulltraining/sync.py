"""Sync-Worker: intervals.icu -> SQLite, inkrementell, ohne Teilcommit. Lokale Zeilen bleiben unberührt."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import duplicates
from .activities import upsert_intervals_activity
from .db import now_iso, transaction
from .intervals_client import IntervalsClient

OVERLAP_DAYS = 7
INITIAL_DAYS = 365


def _from_date(conn: sqlite3.Connection, full: bool) -> date:
    row = conn.execute("SELECT max(start_date) AS d FROM activities WHERE source = 'intervals'").fetchone()
    if full or row["d"] is None:
        return date.today() - timedelta(days=INITIAL_DAYS)
    return date.fromisoformat(row["d"][:10]) - timedelta(days=OVERLAP_DAYS)


def _upsert_wellness(conn: sqlite3.Connection, w: dict[str, Any]) -> None:
    sleep = w.get("sleepSecs")
    conn.execute(
        """INSERT INTO wellness(date, hrv, resting_hr, sleep_h, weight_kg, ctl_icu, atl_icu, raw)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET hrv=excluded.hrv, resting_hr=excluded.resting_hr,
             sleep_h=excluded.sleep_h, weight_kg=excluded.weight_kg, ctl_icu=excluded.ctl_icu,
             atl_icu=excluded.atl_icu, raw=excluded.raw""",
        (str(w["id"])[:10], w.get("hrv"), w.get("restingHR"), round(sleep / 3600, 2) if sleep else w.get("sleepHours"),
         w.get("weight"), w.get("ctl"), w.get("atl"), json.dumps(w, ensure_ascii=False)),
    )


def _remove_deleted(conn: sqlite3.Connection, listed: set[str], oldest: str, newest: str) -> int:
    """Gespiegelte Aktivitäten im abgerufenen Zeitraum, die es an der Quelle nicht mehr gibt, entfernen.

    Nur nach vollständigem, erfolgreichem Abruf (sonst hätte run_sync vorher abgebrochen). Lokale Zeilen bleiben
    unberührt. Schutz gegen eine leere Antwort durch einen API-Fehler: dann nichts löschen.
    """
    rows = conn.execute("SELECT id, external_id FROM activities WHERE source = 'intervals' "
                        "AND start_date >= ? AND start_date < ?",
                        (oldest, (date.fromisoformat(newest) + timedelta(days=1)).isoformat())).fetchall()
    gone = [r["id"] for r in rows if r["external_id"] not in listed]
    if not listed and len(gone) >= 3:
        return 0
    for aid in gone:
        conn.execute("DELETE FROM activities WHERE id = ?", (aid,))
    return len(gone)


def run_sync(conn: sqlite3.Connection, client: IntervalsClient, full: bool = False,
             today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    from_date = _from_date(conn, full)
    run_id = conn.execute("INSERT INTO sync_runs(started_at, provider, from_date) VALUES (?, 'intervals', ?)",
                          (now_iso(), from_date.isoformat())).lastrowid
    created = updated = n_dupes = 0
    try:
        oldest, newest = from_date.isoformat(), today.isoformat()
        listing = client.activities(oldest, newest)
        known = {r["external_id"] for r in conn.execute(
            "SELECT external_id FROM activities WHERE source = 'intervals' AND external_id IS NOT NULL")}
        details = []
        for a in listing:
            if not a.get("id"):
                continue
            # Nur unbekannte Aktivitäten im Detail nachladen, spart Rate Limit.
            details.append(client.activity(str(a["id"])) if str(a["id"]) not in known else a)
        wellness = client.wellness(oldest, newest)
        with transaction(conn):
            for a in details:
                if upsert_intervals_activity(conn, a) == "created":
                    created += 1
                else:
                    updated += 1
            for w in wellness:
                if w.get("id"):
                    _upsert_wellness(conn, w)
            removed = _remove_deleted(conn, {str(a["id"]) for a in listing if a.get("id")}, oldest, newest)
            n_dupes = duplicates.mark_candidates(conn)
        from .publisher import reconcile  # spät importiert, vermeidet Zyklus
        reconciled = reconcile(conn, today=today)
    except Exception as exc:  # noqa: BLE001 – Fehler wird protokolliert und weitergereicht
        conn.execute("UPDATE sync_runs SET finished_at = ?, error = ? WHERE id = ?", (now_iso(), str(exc), run_id))
        raise
    conn.execute("UPDATE sync_runs SET finished_at = ?, n_created = ?, n_updated = ? WHERE id = ?",
                 (now_iso(), created, updated, run_id))
    return {"run_id": run_id, "from_date": from_date.isoformat(), "created": created, "updated": updated,
            "removed": removed,
            "wellness_days": len(wellness), "duplicate_candidates": n_dupes, "reconciled": reconciled,
            "rate_limit_remaining": client.rate_remaining}
