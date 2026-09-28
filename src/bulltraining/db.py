"""SQLite-Schema und Verbindungsaufbau. Einzige Lesequelle für alle weiteren Komponenten."""
from __future__ import annotations

import itertools
import json
import math
import re
import sqlite3
import threading
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
  id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- ids nie wiederverwenden: Diffs/Undo referenzieren sie
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
  revision      INTEGER NOT NULL DEFAULT 0,  -- steigt mit jeder Planänderung; Publisher schreibt nur gegen seine Revision
  publish_unknown INTEGER NOT NULL DEFAULT 0, -- 1 = POST ohne klare Antwort, erst abgleichen, dann ggf. erneut senden
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

CREATE TABLE IF NOT EXISTS bikes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT '',
  archived      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bike_setups (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  bike_id       INTEGER NOT NULL REFERENCES bikes(id),
  valid_from    TEXT NOT NULL,
  values_json   TEXT NOT NULL,
  note          TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bike_setups ON bike_setups(bike_id, valid_from);

CREATE TABLE IF NOT EXISTS bike_photos (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  bike_id       INTEGER NOT NULL REFERENCES bikes(id),
  caption       TEXT,
  mime          TEXT NOT NULL,
  data          BLOB NOT NULL,
  created_at    TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


_INITIALIZED: set[str] = set()
_INIT_LOCK = threading.Lock()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    memory = str(path) == ":memory:"
    if not memory:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    if not memory:
        conn.execute("PRAGMA journal_mode = WAL")
    key = str(path.resolve()) if not memory else None
    with _INIT_LOCK:
        if memory or key not in _INITIALIZED:
            init_schema(conn)
            if key:
                _INITIALIZED.add(key)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_LOCAL = threading.local()


def thread_connection(path: Path | str | None = None) -> sqlite3.Connection:
    """Eine Verbindung je Thread. Eine gemeinsame Verbindung für parallele Anfragen würde deren Transaktionen
    vermischen: der Rollback der einen Anfrage löschte dann auch die bestätigte Änderung der anderen."""
    key = str(path or config.DB_PATH)
    conns = getattr(_LOCAL, "conns", None)
    if conns is None:
        conns = _LOCAL.conns = {}
    if key not in conns:
        conns[key] = connect(path)
    return conns[key]


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))


def _migrate(conn: sqlite3.Connection) -> None:
    """Schemaänderungen nach der ersten Version in bestehenden DBs nachziehen."""
    added = {"plans": {"recurring": "TEXT"},
             "plan_sessions": {"revision": "INTEGER NOT NULL DEFAULT 0",
                               "publish_unknown": "INTEGER NOT NULL DEFAULT 0"}}
    for table, cols in added.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='plan_sessions'").fetchone()[0]
    if "AUTOINCREMENT" not in sql.upper():
        # Tabelle neu aufbauen: ohne AUTOINCREMENT vergibt SQLite die id einer gelöschten letzten Zeile erneut,
        # und ein Undo überschriebe dann eine fremde Einheit.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            new_sql = sql.replace("plan_sessions", "plan_sessions_new", 1).replace(
                "id            INTEGER PRIMARY KEY,", "id            INTEGER PRIMARY KEY AUTOINCREMENT,", 1)
            assert "AUTOINCREMENT" in new_sql
            conn.execute(new_sql)
            conn.execute("INSERT INTO plan_sessions_new SELECT * FROM plan_sessions")
            conn.execute("DROP TABLE plan_sessions")
            conn.execute("ALTER TABLE plan_sessions_new RENAME TO plan_sessions")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_plan_date ON plan_sessions(plan_id, date)")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    _protect_historical_ids(conn)


def _protect_historical_ids(conn: sqlite3.Connection) -> None:
    """Die ID-Sequenz über alle je vergebenen Session-IDs heben – auch über die, die nur noch in den Diffs der
    Änderungshistorie stehen (gelöschte Einheiten). Sonst vergibt eine migrierte DB eine solche ID neu."""
    ids = [r[0] for r in conn.execute("SELECT id FROM plan_sessions")]
    for (diff,) in conn.execute("SELECT diff FROM plan_changes"):
        try:
            data = json.loads(diff)
        except (TypeError, ValueError):
            continue
        ids += [x.get("id") for x in data.get("before", []) + data.get("after", [])
                if isinstance(x.get("id"), int) and x["id"] > 0]
    highest = max(ids, default=0)
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'plan_sessions'").fetchone()
    if row is None:
        if highest:
            conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('plan_sessions', ?)", (highest,))
    elif row[0] < highest:
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'plan_sessions'", (highest,))


_SAVEPOINTS = itertools.count(1)
_CONN_LOCKS: dict[int, threading.RLock] = {}


def _lock_for(conn: sqlite3.Connection) -> threading.RLock:
    with _INIT_LOCK:
        return _CONN_LOCKS.setdefault(id(conn), threading.RLock())


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Siehe _transaction. Zusätzlich eine Sperre je Verbindung: teilen sich doch einmal zwei Threads eine
    Verbindung, wartet der zweite, statt die offene Transaktion des ersten für eine verschachtelte zu halten."""
    with _lock_for(conn):
        with _transaction(conn):
            yield conn


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Alles oder nichts. Verschachtelte Aufrufe laufen über Savepoints.

    BEGIN IMMEDIATE nimmt die Schreibsperre sofort: parallele Schreiber warten (busy_timeout), statt dass zwei
    Transaktionen denselben Stand lesen und beide darauf schreiben."""
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
    conn.execute("BEGIN IMMEDIATE")
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
    """Zahl aus den Einstellungen; ungültige oder außerhalb des erlaubten Bereichs liegende Werte -> default."""
    value = get_setting(conn, key)
    if value is None:
        return default
    try:
        number = float(value)
    except ValueError:
        return default
    rule = config.SETTINGS_SCHEMA.get(key)
    if not math.isfinite(number) or (rule and rule[0] in ("float", "int") and not rule[1] <= number <= rule[2]):
        return default
    return number


class SettingError(ValueError):
    pass


def validate_setting(key: str, value: Any) -> str:
    """Prüft einen Einstellungswert gegen config.SETTINGS_SCHEMA und gibt ihn normalisiert als Text zurück."""
    text = "" if value is None else str(value).strip()
    rule = config.SETTINGS_SCHEMA.get(key)
    if rule is None:
        raise SettingError(f"Unbekannte Einstellung '{key}'.")
    kind = rule[0]
    if text == "" and kind in ("float", "int") and rule[3]:
        return ""  # optionaler Wert, z. B. Schwelle noch nicht getestet
    if kind in ("float", "int"):
        try:
            number = float(text.replace(",", "."))
        except ValueError:
            raise SettingError(f"{key}: '{text}' ist keine Zahl.")
        if not math.isfinite(number) or not rule[1] <= number <= rule[2]:
            raise SettingError(f"{key}: {text} liegt außerhalb von {rule[1]:g}–{rule[2]:g}.")
        if kind == "int":
            if number != int(number):
                raise SettingError(f"{key}: ganze Zahl erwartet.")
            return str(int(number))
        return f"{number:g}"
    if kind == "secret":
        if text and not re.fullmatch(r"[A-Za-z0-9_\-]{10,128}", text):
            raise SettingError(f"{key}: ungültiges Format (10–128 Zeichen, Buchstaben/Ziffern).")
        return text
    if kind == "athlete_id":
        if text and not re.fullmatch(r"i?\d{1,12}", text):
            raise SettingError(f"{key}: erwartet 0 oder eine Athleten-ID wie i123456.")
        return text
    if kind == "baseline_json":
        if text == "":
            return ""
        _validate_manual_baseline(text)
        return text
    return text


def _validate_manual_baseline(text: str) -> None:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SettingError(f"manual_baseline: kein gültiges JSON ({exc}).")
    if not isinstance(data, dict) or not isinstance(data.get("sports"), dict):
        raise SettingError('manual_baseline: erwartet {"as_of": "...", "sports": {"run": {...}, ...}}.')
    if data.get("as_of"):
        try:
            datetime.fromisoformat(str(data["as_of"]))
        except ValueError:
            raise SettingError("manual_baseline.as_of: kein Datum.")
    lph = data.get("load_per_hour", 50)
    if not isinstance(lph, (int, float)) or not 10 <= lph <= 150:
        raise SettingError("manual_baseline.load_per_hour: 10–150 erwartet.")
    limits = {"hours_per_week": 40, "sessions_per_week": 21, "longest_min": 900, "minutes": 600}
    for sport, values in data["sports"].items():
        if not isinstance(values, dict):
            raise SettingError(f"manual_baseline.sports.{sport}: Objekt erwartet.")
        for field, v in values.items():
            if field in limits and (not isinstance(v, (int, float)) or not 0 <= v <= limits[field]):
                raise SettingError(f"manual_baseline.sports.{sport}.{field}: 0–{limits[field]} erwartet.")


def set_setting(conn: sqlite3.Connection, key: str, value: Any, validate: bool = True) -> None:
    if validate and key in config.SETTINGS_SCHEMA:
        value = validate_setting(key, value)
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
