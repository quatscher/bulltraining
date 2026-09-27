from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from bulltraining.db import connect, now_iso

TODAY = date(2026, 9, 27)  # Sonntag; nächste Planwoche beginnt am 28.09.


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


def add_activity(conn, day: date, sport: str, minutes: int, load: float, *, source: str = "intervals",
                 ext: str | None = None, rpe: int | None = None, is_endurance: int = 1, method: str = "hr"):
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO activities(source, external_id, start_date, sport, name, duration_s, load, load_method, rpe, "
        "is_endurance, raw, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, ext if source == "intervals" else None, f"{day.isoformat()}T08:00:00", sport, f"{sport} {minutes}",
         minutes * 60, load, method, rpe, is_endurance, json.dumps({}), ts, ts))
    return cur.lastrowid


def seed_history(conn, weeks: int = 8, hours_scale: float = 1.0, today: date = TODAY):
    """Regelmäßiges Training: ~6 h/Woche bei Skala 1.0."""
    start = today - timedelta(weeks=weeks)
    pattern = {1: ("run", 50, 55), 2: ("ride", 60, 50), 3: ("swim", 45, 45), 5: ("ride", 150, 48), 6: ("run", 80, 52)}
    d = start
    i = 0
    while d < today:
        if d.weekday() in pattern:
            sport, minutes, lph = pattern[d.weekday()]
            m = int(minutes * hours_scale)
            add_activity(conn, d, sport, m, round(m / 60 * lph, 1), ext=f"x{i}")
            i += 1
        d += timedelta(days=1)
