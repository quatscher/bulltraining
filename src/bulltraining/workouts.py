"""Einzelne Einheiten: Struktur in Intervals-Syntax, Titel und geschätzte Belastung.

Zielvorgaben sind %-Angaben der Schwelle aus dem letzten Leistungstest (Rad: % FTP, Laufen/Schwimmen: % Pace).
Ohne gültigen Test fällt die Einheit auf Pulszonen zurück. Absolute Zielwerte stehen als Kommentarzeile dabei.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import get_float
from .performance import PROTOCOLS, test_status
from .util import fmt_pace

HARD = {"threshold", "vo2", "test", "race"}
INTENSITY_ORDER = ["recovery", "easy", "long", "tempo", "threshold", "vo2", "test", "race"]
# Intensitätsfaktor für die Lastschätzung (Last = h * IF² * 100, TSS-ähnlich, vergleichbar mit intervals.icu)
IF = {"recovery": 0.55, "easy": 0.65, "long": 0.68, "tempo": 0.78, "threshold": 0.85, "vo2": 0.83,
      "test": 0.88, "race": 0.90}
SPORT_LABEL = {"run": "Lauf", "ride": "Rad", "swim": "Schwimmen", "strength": "Kraft", "other": "Einheit"}
INTENSITY_LABEL = {"recovery": "Regeneration", "easy": "Grundlage", "long": "Lang", "tempo": "Tempo",
                   "threshold": "Schwelle", "vo2": "VO2max", "test": "Leistungstest", "race": "Wettkampf"}

# (untere, obere) Prozent der Schwelle; bei Pace: Prozent der Schwellengeschwindigkeit
PCT = {
    "ride": {"wu": (50, 65), "rest": (45, 55), "recovery": (45, 55), "easy": (56, 75), "long": (60, 72),
             "tempo": (76, 88), "threshold": (95, 102), "vo2": (106, 118)},
    "run": {"wu": (72, 80), "rest": (65, 75), "recovery": (70, 76), "easy": (78, 86), "long": (78, 85),
            "tempo": (88, 94), "threshold": (97, 102), "vo2": (104, 110)},
    "swim": {"wu": (78, 85), "rest": (60, 70), "recovery": (75, 82), "easy": (82, 88), "long": (84, 90),
             "tempo": (92, 96), "threshold": (98, 102), "vo2": (103, 108)},
}
HR_ZONE = {"wu": "Z1-Z2", "rest": "Z1", "recovery": "Z1", "easy": "Z2", "long": "Z2", "tempo": "Z3",
           "threshold": "Z4", "vo2": "Z5"}
# (Arbeit, Pause) in Minuten für strukturierte Einheiten
WORK_REST = {
    "ride": {"tempo": (15, 5), "threshold": (10, 4), "vo2": (4, 4)},
    "run": {"tempo": (12, 3), "threshold": (8, 3), "vo2": (3, 3)},
    "swim": {"tempo": (6, 1), "threshold": (4, 1), "vo2": (2, 1)},
}
THRESHOLD_KEY = {"ride": "ftp_w", "run": "threshold_pace_run_s_per_km", "swim": "css_s_per_100m"}


def estimate_load(duration_min: float, intensity: str) -> float:
    return round(duration_min / 60 * IF.get(intensity, 0.65) ** 2 * 100, 1)


class _Targets:
    def __init__(self, conn: sqlite3.Connection, sport: str):
        self.sport = sport
        self.threshold = get_float(conn, THRESHOLD_KEY[sport]) if sport in THRESHOLD_KEY else None
        # Nur ein gültiger (nicht veralteter) Test zählt als Grundlage für %-Vorgaben.
        self.valid = self.threshold is not None and test_status(conn, sport)["status"] != "missing"

    def step(self, minutes: int, zone: str) -> str:
        if minutes <= 0:
            return ""
        if self.valid and self.sport in PCT:
            lo, hi = PCT[self.sport][zone]
            suffix = "%" if self.sport == "ride" else "% Pace"
            return f"- {minutes}m {lo}-{hi}{suffix}"
        return f"- {minutes}m {HR_ZONE[zone]} HR"

    def note(self, zone: str) -> str | None:
        if not self.valid or self.sport not in PCT:
            return "Keine gültige Schwelle aus einem Leistungstest – Vorgabe nach Pulszone."
        lo, hi = PCT[self.sport][zone]
        t = self.threshold
        if self.sport == "ride":
            return f"Ziel {round(t * lo / 100)}–{round(t * hi / 100)} W (FTP {t:.0f} W)"
        unit = "/km" if self.sport == "run" else "/100m"
        return f"Ziel {fmt_pace(t / (hi / 100), unit)}–{fmt_pace(t / (lo / 100), unit)} (Schwelle {fmt_pace(t, unit)})"


def build(conn: sqlite3.Connection, sport: str, intensity: str, duration_min: int,
          protocol: str | None = None) -> dict[str, Any]:
    """Titel, Beschreibung, Dauer und geschätzte Last einer Einheit."""
    duration_min = int(round(duration_min))
    if intensity == "test":
        spec = PROTOCOLS[protocol]
        return {"title": spec["name"], "description": spec["summary"] + "\n\n" + spec["description"],
                "duration_s": spec["duration_min"] * 60, "target_load": estimate_load(spec["duration_min"], "test"),
                "intensity": "test", "category": "TEST", "test_protocol": protocol}
    if sport not in PCT:
        rpe = {"recovery": 4, "easy": 5, "long": 6, "tempo": 7}.get(intensity, 7)
        factor = get_float(conn, "srpe_factor", 1.0)
        return {"title": f"{SPORT_LABEL.get(sport, sport)} {duration_min} min",
                "description": f"{SPORT_LABEL.get(sport, sport)}, Ziel-RPE {rpe}. Nach der Einheit RPE melden.",
                "duration_s": duration_min * 60, "target_load": round(rpe * duration_min * factor, 1),
                "intensity": intensity, "category": "WORKOUT", "test_protocol": None}

    t = _Targets(conn, sport)
    lines: list[str] = []
    title = f"{SPORT_LABEL[sport]} {INTENSITY_LABEL.get(intensity, intensity)}"
    if intensity in WORK_REST[sport] and duration_min >= 35:
        work, rest = WORK_REST[sport][intensity]
        wu, cd = (15, 10) if sport != "swim" else (10, 5)
        main = duration_min - wu - cd
        reps = max(2 if intensity != "tempo" else 1, min(6, main // (work + rest)))
        if reps * (work + rest) > main:  # zu kurz für zwei Wiederholungen: Arbeitsphase kürzen
            work = max(2, main // reps - rest)
        filler = main - reps * (work + rest)
        title += f" {reps}x{work}"
        note = t.note(intensity)
        lines += ["Einlaufen" if sport == "run" else "Aufwärmen", t.step(wu, "wu"), ""]
        if note:
            lines.append(note)
        lines += [f"{reps}x", t.step(work, intensity), t.step(rest, "rest"), ""]
        if filler > 0:
            lines += [t.step(filler, "easy"), ""]
        lines += ["Ausklang", t.step(cd, "rest")]
    else:
        zone = intensity if intensity in PCT[sport] else "easy"
        if intensity in WORK_REST[sport]:  # kurze harte Einheit ohne Platz für Struktur -> Tempo-Dauer
            zone = "tempo"
        note = t.note(zone)
        if note:
            lines.append(note)
        lines.append(t.step(duration_min, zone))
        title += f" {duration_min} min"
    return {"title": title, "description": "\n".join(l for l in lines if l is not None).strip(),
            "duration_s": duration_min * 60, "target_load": estimate_load(duration_min, intensity),
            "intensity": intensity, "category": "WORKOUT", "test_protocol": None}
