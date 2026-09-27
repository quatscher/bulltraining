"""SQLite-Schema und Verbindungsaufbau. Einzige Lesequelle für alle weiteren Komponenten."""
from __future__ import annotations

import itertools
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
  id            INTEGER PRIMARY KEY,
  source        TEXT NOT NULL CHECK (source IN ('intervals','local')),
  external_id   TEXT UNIQUE,
  start_date    TEXT NOT NULL,
  sport         TEXT NOT NULL,
  name          TEXT,
  duration_s    INTEGER NOT NULL,
  distance_m    REAL,
  hr_avg        INTEGER,
  hr_max        INTEGER,
  power_avg     INTEGER,
  rpe           INTEGER CHECK (rpe IS NULL OR rpe BETWEEN 1 AND 10),
  load          REAL,                 -- bei srpe: Rohwert RPE*min, Faktor wird erst in der Auswertung angewendet
  load_method   TEXT,                 -- 'power'|'hr'|'icu'|'srpe'
  is_endurance  INTEGER NOT NULL,
  excluded      INTEGER NOT NULL DEFAULT 0,   -- 1 = zählt in keiner Lastkurve (Dublette aufgelöst)
  user_locked   INTEGER NOT NULL DEFAULT 0,   -- 1 = is_endurance/excluded von Hand gesetzt, Sync überschreibt nicht
  zone_times    TEXT,                 -- JSON {"kind":"power"|"hr","secs":[z1..zn]}
  possible_duplicate_of INTEGER REFERENCES activities(id) ON DELETE SET NULL,
  notes         TEXT,
  raw           TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_act_date ON activities(start_date);
CREATE INDEX IF NOT EXISTS idx_act_source ON activities(source);

CREATE TABLE IF NOT EXISTS duplicate_exceptions (
  local_id      INTEGER NOT NULL,
  external_id   TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (local_id, external_id)
);

CREATE TABLE IF NOT EXISTS wellness (
  date          TEXT PRIMARY KEY,
  hrv           REAL,
  resting_hr    INTEGER,
  sleep_h       REAL,
  weight_kg     REAL,
  ctl_icu       REAL,
  atl_icu       REAL,
  raw           TEXT
);

CREATE TABLE IF NOT EXISTS plans (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  goal_type     TEXT NOT NULL CHECK (goal_type IN ('event','continuous')),
  goal_date     TEXT,
  goal_kind     TEXT,
  sports        TEXT NOT NULL,
  focus         TEXT,
  priority      TEXT,
  weekly_hours  REAL,
  available_days TEXT,
  recurring     TEXT,                 -- JSON: feste Einheiten je Woche, z. B. Pendeln
  start_date    TEXT NOT NULL,
  baseline      TEXT,                 -- JSON: Trainingsumfang und Tests bei Planerstellung
  status        TEXT NOT NULL CHECK (status IN ('active','archived')),
  created_at    TEXT NOT NULL,
  CHECK (goal_type = 'continuous' OR goal_date IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS plan_sessions (
  id            INTEGER PRIMARY KEY,
  plan_id       INTEGER NOT NULL REFERENCES plans(id),
  date          TEXT NOT NULL,
  sport         TEXT NOT NULL,
  category      TEXT NOT NULL DEFAULT 'WORKOUT',  -- 'WORKOUT'|'RACE'|'TEST'
  race_priority TEXT,                             -- 'B'|'C' bei category='RACE'
  test_protocol TEXT,                             -- bei category='TEST'
  title         TEXT NOT NULL,
  description   TEXT,
  duration_s    INTEGER NOT NULL,
  target_load   REAL,
  intensity     TEXT,                 -- 'recovery'|'easy'|'long'|'tempo'|'threshold'|'vo2'|'test'|'race'
  status        TEXT NOT NULL,        -- 'planned'|'published'|'done'|'skipped'|'deleted'
  external_event_id TEXT,
  publish_error TEXT,
  activity_id   INTEGER REFERENCES activities(id) ON DELETE SET NULL,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ps_plan_date ON plan_sessions(plan_id, date);

CREATE TABLE IF NOT EXISTS plan_changes (
  id            INTEGER PRIMARY KEY,
  plan_id       INTEGER REFERENCES plans(id),
  created_at    TEXT NOT NULL,
  actor         TEXT NOT NULL,        -- 'llm'|'human'|'system'
  tool          TEXT,
  reason        TEXT NOT NULL,
  ops           TEXT NOT NULL,        -- JSON: Operationsliste wie übergeben
  diff          TEXT NOT NULL,        -- JSON: {"before":[...], "after":[...]}
  warnings      TEXT,                 -- JSON-Liste, nicht blockierend
  status        TEXT NOT NULL DEFAULT 'pending',  -- 'pending'|'applied'|'rejected'|'reverted'
  applied_at    TEXT,
  resolved_at   TEXT,
  reverts_change_id INTEGER REFERENCES plan_changes(id)
);

CREATE TABLE IF NOT EXISTS performance_tests (
  id            INTEGER PRIMARY KEY,
  date          TEXT NOT NULL,
  sport         TEXT NOT NULL,
  protocol      TEXT NOT NULL,
  inputs        TEXT NOT NULL,        -- JSON: Messwerte wie erfasst
  results       TEXT NOT NULL,        -- JSON: abgeleitete Schwellenwerte
  activity_id   INTEGER REFERENCES activities(id) ON DELETE SET NULL,
  plan_session_id INTEGER REFERENCES plan_sessions(id) ON DELETE SET NULL,
  notes         TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pt_sport_date ON performance_tests(sport, date);

CREATE TABLE IF NOT EXISTS sync_runs (
  id            INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  provider      TEXT NOT NULL,
  from_date     TEXT,
  n_created     INTEGER,
  n_updated     INTEGER,
  error         TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key           TEXT PRIMARY KEY,
  value         TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))


def _migrate(conn: sqlite3.Connection) -> None:
    """Spalten, die nach der ersten Version dazugekommen sind, in bestehenden DBs ergänzen."""
    added = {"plans": {"recurring": "TEXT"}}
    for table, cols in added.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


_SAVEPOINTS = itertools.count(1)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Alles oder nichts. Verschachtelte Aufrufe laufen über Savepoints."""
    if conn.in_transaction:
        name = f"sp_{next(_SAVEPOINTS)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --- Einstellungen -----------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] == "":
        return default
    return row["value"]


def get_float(conn: sqlite3.Connection, key: str, default: float | None = None) -> float | None:
    value = get_setting(conn, key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, "" if value is None else str(value)),
    )


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings ORDER BY key")}


# --- Hilfen ------------------------------------------------------------------

def row_to_dict(row: sqlite3.Row | None, json_fields: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for f in json_fields:
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except (TypeError, ValueError):
                pass
    return d
