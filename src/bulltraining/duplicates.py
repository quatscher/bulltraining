"""Dublettenerkennung zwischen lokal erfassten und gespiegelten Aktivitäten. Nie automatisch mergen."""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import now_iso, transaction

TOLERANCE_S = 15 * 60


def mark_candidates(conn: sqlite3.Connection) -> int:
    """Markiert lokale Zeilen, zu denen es eine externe mit gleichem Tag, Sportart und ±15 min Dauer gibt."""
    rows = conn.execute(
        """SELECT l.id AS local_id, e.id AS ext_id
           FROM activities l
           JOIN activities e
             ON e.source = 'intervals'
            AND substr(e.start_date, 1, 10) = substr(l.start_date, 1, 10)
            AND e.sport = l.sport
            AND abs(e.duration_s - l.duration_s) <= ?
            AND e.excluded = 0
           WHERE l.source = 'local'
             AND l.possible_duplicate_of IS NULL
             AND l.excluded = 0
             AND NOT EXISTS (SELECT 1 FROM duplicate_exceptions x
                             WHERE x.local_id = l.id AND x.external_id = e.external_id)
           ORDER BY l.id, abs(e.duration_s - l.duration_s)""",
        (TOLERANCE_S,),
    ).fetchall()
    marked: set[int] = set()
    for r in rows:
        if r["local_id"] in marked:
            continue
        conn.execute("UPDATE activities SET possible_duplicate_of = ?, updated_at = ? WHERE id = ?",
                     (r["ext_id"], now_iso(), r["local_id"]))
        marked.add(r["local_id"])
    return len(marked)


def open_pairs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT l.id AS local_id, l.start_date AS local_date, l.name AS local_name, l.duration_s AS local_duration_s,
                  l.rpe AS local_rpe, l.load AS local_load,
                  e.id AS ext_id, e.start_date AS ext_date, e.name AS ext_name, e.duration_s AS ext_duration_s,
                  e.hr_avg AS ext_hr_avg, e.load AS ext_load, l.sport AS sport
           FROM activities l JOIN activities e ON e.id = l.possible_duplicate_of
           WHERE l.source = 'local' ORDER BY l.start_date DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def resolve(conn: sqlite3.Connection, local_id: int, action: str) -> None:
    """action: 'keep_external' | 'keep_local' | 'not_duplicate'."""
    local = conn.execute("SELECT * FROM activities WHERE id = ? AND source = 'local'", (local_id,)).fetchone()
    if local is None or local["possible_duplicate_of"] is None:
        raise ValueError(f"Kein offenes Dublettenpaar für lokale Aktivität {local_id}.")
    ext = conn.execute("SELECT * FROM activities WHERE id = ?", (local["possible_duplicate_of"],)).fetchone()
    with transaction(conn):
        if action == "keep_external":
            # RPE der lokalen Erfassung ist oft das einzige subjektive Maß – übernehmen, falls extern leer.
            if ext["rpe"] is None and local["rpe"] is not None:
                from .activities import update_activity  # berechnet die Last aus der RPE mit
                update_activity(conn, ext["id"], rpe=local["rpe"])
            conn.execute("UPDATE plan_sessions SET activity_id = ? WHERE activity_id = ?", (ext["id"], local_id))
            conn.execute("UPDATE performance_tests SET activity_id = ? WHERE activity_id = ?", (ext["id"], local_id))
            conn.execute("DELETE FROM activities WHERE id = ?", (local_id,))
        elif action == "keep_local":
            # Abweichung zur ursprünglichen Architektur: is_endurance=0 allein nähme die Einheit nur aus
            # ctl_endurance, in ctl_total wäre sie doppelt. Deshalb zusätzlich excluded=1.
            conn.execute("UPDATE activities SET is_endurance = 0, excluded = 1, user_locked = 1, updated_at = ? "
                         "WHERE id = ?", (now_iso(), ext["id"]))
            conn.execute("UPDATE activities SET possible_duplicate_of = NULL, updated_at = ? WHERE id = ?",
                         (now_iso(), local_id))
        elif action == "not_duplicate":
            conn.execute("INSERT OR IGNORE INTO duplicate_exceptions(local_id, external_id, created_at) VALUES (?,?,?)",
                         (local_id, ext["external_id"], now_iso()))
            conn.execute("UPDATE activities SET possible_duplicate_of = NULL, updated_at = ? WHERE id = ?",
                         (now_iso(), local_id))
        else:
            raise ValueError(f"Unbekannte Auflösung '{action}'.")
