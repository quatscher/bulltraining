"""Gemeinsames 5-Zonen-Modell für geplante und absolvierte Einheiten.

Geplant: Zeit je Zone wird aus der Workout-Beschreibung (Intervals-Syntax) gelesen – dieselbe Quelle, die auf
die Uhr geht, also auch für vom LLM geschriebene oder von Hand geänderte Beschreibungen korrekt.
Absolviert: Zonenzeiten aus intervals.icu (Leistung beim Rad, sonst Puls), 7 Zonen auf 5 zusammengefasst.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .db import get_float

ZONES = ["Z1 Regeneration", "Z2 Grundlage", "Z3 Tempo", "Z4 Schwelle", "Z5 VO2max+"]
ZONE_COLORS = ["#9fb7c9", "#4fa36b", "#e0b43a", "#e0703a", "#c8363a"]

# Obergrenzen in % der Schwelle (Rad: % FTP; Lauf/Schwimmen: % Schwellengeschwindigkeit) für Z1..Z4
_UPPER = {
    "ride": (55, 75, 90, 105),
    "run": (78, 88, 95, 102),
    "swim": (80, 90, 97, 102),
}
_DEFAULT_PACE = {"run": 330.0, "swim": 120.0}  # s/km bzw. s/100 m, falls kein Test vorliegt

_STEP = re.compile(r"^-\s*(?P<dur>(?:\d+(?:\.\d+)?(?:h|m|s))+|\d+(?:\.\d+)?(?:mtr|km))\b\s*(?P<target>.*)$", re.I)
_REPEAT = re.compile(r"^(\d+)\s*x\s*$", re.I)
_PCT = re.compile(r"(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?\s*%")
_HRZ = re.compile(r"Z(\d)(?:\s*-\s*Z(\d))?", re.I)


def zone_for_pct(sport: str, pct: float) -> int:
    for i, upper in enumerate(_UPPER.get(sport, _UPPER["ride"])):
        if pct <= upper:
            return i
    return 4


def _target_zone(sport: str, target: str) -> tuple[int, float]:
    """Zone (0..4) und Anteil der Schwellengeschwindigkeit (für Distanzschritte)."""
    t = target.strip().lower()
    if not t or t.startswith("rest"):
        return 0, 0.65
    if "max" in t.split():
        return 4, 1.05
    m = _PCT.search(t)
    if m:
        lo = float(m.group(1))
        hi = float(m.group(2) or m.group(1))
        if "ramp" in t:  # Rampe: mittlere Intensität, Schwerpunkt oben
            mid = lo + (hi - lo) * 0.6
        else:
            mid = (lo + hi) / 2
        return zone_for_pct(sport, mid), mid / 100
    m = _HRZ.search(t)
    if m:
        z = (int(m.group(1)) + int(m.group(2) or m.group(1))) / 2
        z = int(z)  # Z1-Z2 -> Z1, Z2 -> Z2 usw.
        return max(0, min(4, z - 1)), {0: 0.72, 1: 0.83, 2: 0.9, 3: 0.98, 4: 1.05}[max(0, min(4, z - 1))]
    return 1, 0.83  # ohne Vorgabe: Grundlage


def _duration_s(dur: str, sport: str, speed_frac: float, pace: float | None) -> float:
    dur = dur.lower()
    if dur.endswith("mtr") or dur.endswith("km"):
        meters = float(dur[:-3]) if dur.endswith("mtr") else float(dur[:-2]) * 1000
        base = pace or _DEFAULT_PACE.get(sport, 330.0)
        per_m = base / (100 if sport == "swim" else 1000)
        return meters * per_m / max(speed_frac, 0.3)
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", dur):
        total += float(num) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def description_zone_secs(description: str | None, sport: str, pace: float | None = None) -> list[float] | None:
    """Sekunden je Zone aus einer Intervals-Beschreibung. None, wenn kein Schritt erkannt wurde."""
    if not description:
        return None
    secs = [0.0] * 5
    found = False
    repeat = 1
    for raw in description.splitlines():
        line = raw.strip()
        if not line:
            repeat = 1  # Wiederholungsblock endet an einer Leerzeile
            continue
        rep = _REPEAT.match(line)
        if rep:
            repeat = int(rep.group(1))
            continue
        step = _STEP.match(line)
        if not step:
            repeat = 1  # Textzeile beendet ebenfalls einen Block
            continue
        zone, frac = _target_zone(sport, step.group("target"))
        secs[zone] += _duration_s(step.group("dur"), sport, frac, pace) * repeat
        found = True
    return [round(s) for s in secs] if found else None


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
