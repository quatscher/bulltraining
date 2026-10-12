"""Ausfallzeiten (OP, Krankheit, Reise): je Sportart ein Datum, ab dem sie wieder erlaubt ist.

Der Generator plant gesperrte Sportarten an diesen Tagen nicht ein (auch keine festen Einheiten wie Pendeln), die
Regelprüfung lehnt Vorschläge dorthin ab. Den behutsamen Wiedereinstieg regelt der Lastkorridor von selbst: die
Wochen der Pause zählen mit 0 und senken die chronische Last.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from .db import now_iso, transaction
from .util import to_date

SPORT_LABEL = {"run": "Laufen", "ride": "Rad", "swim": "Schwimmen", "strength": "Kraft", "other": "Sonstiges"}


class AbsenceError(ValueError):
    pass


def create(conn: sqlite3.Connection, reason: str, start: str, resume: dict[str, str]) -> int:
    """resume: {sport: erster wieder erlaubter Tag}. Ende der Ausfallzeit = Tag vor dem spätesten Wiedereinstieg."""
    reason = (reason or "").strip()[:120]
    if not reason:
        raise AbsenceError("Grund fehlt (z. B. „OP“).")
    try:
        start_d = to_date(start)
        res = {s: to_date(d).isoformat() for s, d in resume.items() if d}
    except (TypeError, ValueError) as exc:
        raise AbsenceError("Datum ungültig.") from exc
    if not res:
        raise AbsenceError("Für mindestens eine Sportart ein Datum für den Wiedereinstieg angeben.")
    if any(to_date(d) <= start_d for d in res.values()):
        raise AbsenceError("Wiedereinstieg muss nach dem Beginn der Ausfallzeit liegen.")
    end = max(to_date(d) for d in res.values()) - timedelta(days=1)
    with transaction(conn):
        cur = conn.execute("INSERT INTO absences(reason, start_date, end_date, resume_json, created_at) "
                           "VALUES (?, ?, ?, ?, ?)", (reason, start_d.isoformat(), end.isoformat(),
                                                      json.dumps(res), now_iso()))
    return int(cur.lastrowid)


def delete(conn: sqlite3.Connection, absence_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM absences WHERE id = ?", (absence_id,))


def list_all(conn: sqlite3.Connection, include_past: bool = False, today: date | None = None) -> list[dict[str, Any]]:
    today = today or date.today()
    rows = conn.execute("SELECT * FROM absences ORDER BY start_date").fetchall()
    out = []
    for r in rows:
        if not include_past and to_date(r["end_date"]) < today:
            continue
        resume = json.loads(r["resume_json"])
        out.append({"id": r["id"], "reason": r["reason"], "start_date": r["start_date"], "end_date": r["end_date"],
                    "resume": resume,
                    "resume_text": ", ".join(f"{SPORT_LABEL.get(s, s)} ab {to_date(d).strftime('%d.%m.')}"
                                             for s, d in sorted(resume.items(), key=lambda kv: kv[1]))})
    return out


def blocked(conn: sqlite3.Connection, sport: str, day: date | str) -> dict[str, Any] | None:
    """Die Ausfallzeit, die `sport` an `day` sperrt – sonst None. Sportarten ohne eigenes Datum sind bis zum Ende
    der Ausfallzeit gesperrt."""
    d = to_date(day)
    for a in list_all(conn, include_past=True):
        start = to_date(a["start_date"])
        resume = to_date(a["resume"].get(sport) or (to_date(a["end_date"]) + timedelta(days=1)).isoformat())
        if start <= d < resume:
            return a
    return None


def blocked_pairs(conn: sqlite3.Connection, sports: list[str], days: list[date]) -> set[tuple[str, date]]:
    return {(s, d) for s in sports for d in days if blocked(conn, s, d)}


def overlaps(conn: sqlite3.Connection, first: date, last: date) -> list[dict[str, Any]]:
    return [a for a in list_all(conn, include_past=True)
            if to_date(a["start_date"]) <= last and to_date(a["end_date"]) >= first]
