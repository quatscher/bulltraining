"""Räder und ihr Sitz-/Cockpit-Setup. Jede Änderung legt eine neue Version an – so bleibt nachvollziehbar, wann
was verstellt wurde (z. B. wenn nach einer Änderung Knie oder Nacken zwicken)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from .db import now_iso, transaction

# Feste Felder; Werte sind Text, weil viele Einstellungen Positionen statt Zahlen sind („mittleres Loch“)
SETUP_FIELDS: list[tuple[str, str, str]] = [
    ("saddle_height", "Sattelhöhe", "Skala / mm"),
    ("saddle_tilt", "Sattelneigung", "°"),
    ("saddle_position", "Sattelposition", "Klemmung / Setback"),
    ("saddle_model", "Sattel", "Modell"),
    ("cockpit_height", "Cockpit-Höhe", "mm"),
    ("pad_reach", "Pad-Reach", "Position"),
    ("pad_width", "Pad-Breite", "Position"),
    ("ext_length", "Extensions – Länge", "mm / Range"),
    ("ext_tilt", "Extensions – Neigung", "°"),
    ("crank_length", "Kurbellänge", "mm"),
    ("tires", "Reifen / Druck", "Breite, bar"),
]
FIELD_KEYS = {k for k, _, _ in SETUP_FIELDS}
MAX_PHOTO_BYTES = 8 * 1024 * 1024
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


class BikeError(ValueError):
    pass


def _clean_values(values: dict[str, Any]) -> dict[str, str]:
    """Feste Felder plus frei benannte Zusatzfelder; leere Werte fallen weg."""
    out: dict[str, str] = {}
    for k, v in values.items():
        k, v = str(k).strip(), str(v or "").strip()
        if not k or not v:
            continue
        if len(k) > 60 or len(v) > 300:
            raise BikeError(f"„{k[:30]}“: Feldname höchstens 60, Wert höchstens 300 Zeichen.")
        out[k] = v
    return out


def create_bike(conn: sqlite3.Connection, name: str, kind: str = "") -> int:
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise BikeError("Name des Rads fehlt oder ist zu lang.")
    with transaction(conn):
        cur = conn.execute("INSERT INTO bikes(name, kind, created_at) VALUES (?, ?, ?)",
                           (name, (kind or "").strip()[:40], now_iso()))
    return int(cur.lastrowid)


def save_setup(conn: sqlite3.Connection, bike_id: int, values: dict[str, Any], note: str = "",
               valid_from: str | None = None) -> int | None:
    """Neue Version, wenn sich etwas geändert hat; sonst None."""
    vals = _clean_values(values)
    valid_from = valid_from or date.today().isoformat()
    try:
        date.fromisoformat(valid_from)
    except ValueError as exc:
        raise BikeError("Datum ungültig.") from exc
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM bikes WHERE id = ?", (bike_id,)).fetchone():
            raise BikeError(f"Rad {bike_id} nicht gefunden.")
        cur = current_setup(conn, bike_id)
        if cur and cur["values"] == vals and not note.strip():
            return None
        r = conn.execute("INSERT INTO bike_setups(bike_id, valid_from, values_json, note, created_at) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (bike_id, valid_from, json.dumps(vals, ensure_ascii=False), note.strip()[:500], now_iso()))
    return int(r.lastrowid)


def _setup_row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "valid_from": r["valid_from"], "values": json.loads(r["values_json"]),
            "note": r["note"] or "", "created_at": r["created_at"]}


def current_setup(conn: sqlite3.Connection, bike_id: int) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM bike_setups WHERE bike_id = ? ORDER BY valid_from DESC, id DESC LIMIT 1",
                     (bike_id,)).fetchone()
    return _setup_row(r) if r else None


def setup_history(conn: sqlite3.Connection, bike_id: int) -> list[dict[str, Any]]:
    """Neueste zuerst, jede Version mit den Änderungen gegenüber der vorherigen."""
    rows = [_setup_row(r) for r in conn.execute(
        "SELECT * FROM bike_setups WHERE bike_id = ? ORDER BY valid_from, id", (bike_id,))]
    prev: dict[str, str] = {}
    for s in rows:
        v = s["values"]
        s["changes"] = [{"field": label(k), "old": prev.get(k), "new": v.get(k)}
                        for k in list(v) + [k for k in prev if k not in v] if prev.get(k) != v.get(k)]
        prev = v
    return rows[::-1]


def label(key: str) -> str:
    return next((lbl for k, lbl, _ in SETUP_FIELDS if k == key), key)


def list_bikes(conn: sqlite3.Connection, include_archived: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM bikes" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY id"
    out = []
    for b in conn.execute(sql):
        d = dict(b)
        d["setup"] = current_setup(conn, b["id"])
        d["photos"] = [dict(p) for p in conn.execute(
            "SELECT id, caption, created_at FROM bike_photos WHERE bike_id = ? ORDER BY id", (b["id"],))]
        out.append(d)
    return out


def add_photo(conn: sqlite3.Connection, bike_id: int, data: bytes, mime: str, caption: str = "") -> int:
    if mime not in PHOTO_TYPES:
        raise BikeError("Nur JPEG, PNG, WebP oder HEIC.")
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise BikeError("Foto leer oder größer als 8 MB.")
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM bikes WHERE id = ?", (bike_id,)).fetchone():
            raise BikeError(f"Rad {bike_id} nicht gefunden.")
        r = conn.execute("INSERT INTO bike_photos(bike_id, caption, mime, data, created_at) VALUES (?, ?, ?, ?, ?)",
                         (bike_id, caption.strip()[:120], mime, data, now_iso()))
    return int(r.lastrowid)


def get_photo(conn: sqlite3.Connection, photo_id: int) -> tuple[bytes, str] | None:
    r = conn.execute("SELECT data, mime FROM bike_photos WHERE id = ?", (photo_id,)).fetchone()
    return (r["data"], r["mime"]) if r else None


def delete_photo(conn: sqlite3.Connection, photo_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM bike_photos WHERE id = ?", (photo_id,))


def archive_bike(conn: sqlite3.Connection, bike_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE bikes SET archived = 1 WHERE id = ?", (bike_id,))


def bikes_for_llm(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Kompakt für MCP: aktuelles Setup mit lesbaren Feldnamen und die letzten Änderungen."""
    out = []
    for b in list_bikes(conn):
        s = b["setup"] or {"values": {}, "valid_from": None}
        out.append({"id": b["id"], "name": b["name"], "kind": b["kind"], "setup_since": s["valid_from"],
                    "setup": {label(k): v for k, v in s["values"].items()}, "note": (b["setup"] or {}).get("note", ""),
                    "recent_changes": [{"date": h["valid_from"], "changes": h["changes"], "note": h["note"]}
                                       for h in setup_history(conn, b["id"])[:3]],
                    "photos": len(b["photos"])})
    return out
