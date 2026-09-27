"""Gemeinsames 5-Zonen-Modell für geplante und absolvierte Einheiten.

Geplant: Zeit je Zone wird aus der Workout-Beschreibung (Intervals-Syntax) gelesen – dieselbe Quelle, die auf
die Uhr geht, also auch für vom LLM geschriebene oder von Hand geänderte Beschreibungen korrekt.
Absolviert: Zonenzeiten aus intervals.icu (Leistung beim Rad, sonst Puls), 7 Zonen auf 5 zusammengefasst.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import workout_syntax as syntax
from .db import get_float

ZONES = ["Z1 Regeneration", "Z2 Grundlage", "Z3 Tempo", "Z4 Schwelle", "Z5 VO2max+"]
ZONE_COLORS = ["#9fb7c9", "#4fa36b", "#e0b43a", "#e0703a", "#c8363a"]

def zone_for_pct(sport: str, pct: float) -> int:
    return syntax.zone_for_pct(sport, pct)


def description_zone_secs(description: str | None, sport: str, pace: float | None = None,
                          ftp: float | None = None) -> list[float] | None:
    """Sekunden je Zone aus einer Intervals-Beschreibung (tolerant, für Anzeige). None, wenn kein Schritt erkannt."""
    if not description:
        return None
    items = syntax.parse(description, sport, ftp=ftp, pace=pace)
    if not any(it["type"] == "step" or (it["type"] == "repeat" and it["steps"]) for it in items):
        return None
    return [round(v) for v in syntax.zone_seconds(items)]


def session_zone_secs(session: dict[str, Any], paces: dict[str, float | None]) -> list[float]:
    """Zeit je Zone einer geplanten Einheit; ohne auswertbare Beschreibung nach Intensität geschätzt."""
    parsed = description_zone_secs(session.get("description"), session["sport"], paces.get(session["sport"]))
    total = float(session.get("duration_s") or 0)
    if parsed and sum(parsed) > 0:
        # auf die geplante Dauer skalieren (Distanzschritte sind nur geschätzt)
        factor = total / sum(parsed) if total else 1.0
        return [round(s * factor) for s in parsed]
    fallback = {"recovery": 0, "easy": 1, "long": 1, "tempo": 2, "threshold": 3, "vo2": 4, "test": 3, "race": 3}
    secs = [0.0] * 5
    secs[fallback.get(session.get("intensity") or "easy", 1)] = total
    return secs


def activity_zone_secs(zone_times: str | dict | None) -> list[float] | None:
    """intervals.icu-Zonen (meist 7) auf 5 zusammenfassen: Z1..Z4 bleiben, alles darüber wird Z5."""
    if not zone_times:
        return None
    z = json.loads(zone_times) if isinstance(zone_times, str) else zone_times
    secs = z.get("secs") or []
    if not secs:
        return None
    out = [float(s) for s in secs[:4]] + [float(sum(secs[4:]))]
    out += [0.0] * (5 - len(out))
    return out


def reference_paces(conn: sqlite3.Connection) -> dict[str, float | None]:
    return {"run": get_float(conn, "threshold_pace_run_s_per_km"), "swim": get_float(conn, "css_s_per_100m")}
