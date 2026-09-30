"""Einzelne Einheiten: Struktur in Intervals-Syntax, Titel und geschätzte Belastung.

Zielvorgaben sind %-Angaben der Schwelle aus dem letzten Leistungstest (Rad: % FTP, Laufen/Schwimmen: % Pace).
Ohne gültigen Test fällt die Einheit auf Pulszonen zurück. Absolute Zielwerte stehen als Kommentarzeile dabei.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import get_float, get_setting
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


ZONE_IF = (0.55, 0.68, 0.80, 0.92, 1.05)  # Intensitätsfaktor je Zone des 5-Zonen-Modells


def from_description(conn: sqlite3.Connection, sport: str, description: str) -> dict[str, Any] | None:
    """Dauer, Last und Intensität aus einer Beschreibung in Intervals-Syntax; None, wenn nichts auswertbar ist.

    Strikt: jede steuernde Zeile muss verstanden sein, sonst WorkoutSyntaxError – die Beschreibung geht so auf
    die Uhr, eine still übersprungene Zeile würde Dauer oder Belastung verfälschen."""
    from . import workout_syntax as syntax
    from .zonemodel import reference_paces
    items = syntax.parse(description, sport, ftp=get_float(conn, "ftp_w"),
                         pace=reference_paces(conn).get(sport), strict=True)
    secs = syntax.zone_seconds(items)
    if sum(secs) <= 0:
        return None
    total = sum(secs)
    load = sum(s / 3600 * f ** 2 * 100 for s, f in zip(secs, ZONE_IF))
    share = [s / total for s in secs]
    if share[4] >= 0.08:
        intensity = "vo2"
    elif share[3] + share[4] >= 0.10:
        intensity = "threshold"
    elif share[2] >= 0.15:
        intensity = "tempo"
    elif share[0] >= 0.9:
        intensity = "recovery"
    else:
        intensity = "long" if total >= 90 * 60 else "easy"
    return {"duration_s": int(round(total)), "target_load": round(load, 1), "intensity": intensity}


def estimate_load(duration_min: float, intensity: str) -> float:
    return round(duration_min / 60 * IF.get(intensity, 0.65) ** 2 * 100, 1)


class _Targets:
    def __init__(self, conn: sqlite3.Connection, sport: str):
        from .workout_syntax import DEFAULT_PACE
        from .zonemodel import swim_reference_pace
        self.sport = sport
        # Schwimmen wird in Metern geplant: im Becken zählt die Uhr Bahnen und schaltet Streckenschritte selbst weiter
        self.swim_pace = (swim_reference_pace(conn) or DEFAULT_PACE["swim"]) if sport == "swim" else None
        self.threshold = get_float(conn, THRESHOLD_KEY[sport]) if sport in THRESHOLD_KEY else None
        # Jeder vorhandene Test ist Grundlage für %-Vorgaben – auch ein veralteter (der Generator setzt dann einen
        # Retest an). Ohne jeden Test wird nach Pulszone vorgegeben.
        self.valid = self.threshold is not None and test_status(conn, sport)["status"] != "missing"

    def target(self, zone: str) -> str:
        if self.valid and self.sport in PCT:
            lo, hi = PCT[self.sport][zone]
            return f"{lo}-{hi}" + ("%" if self.sport == "ride" else "% Pace")
        return f"{HR_ZONE[zone]} HR"

    def step(self, minutes: float, zone: str, label: str = "") -> str:
        if minutes <= 0:
            return ""
        prefix = f"- {label} " if label else "- "
        if self.sport == "swim":
            return f"{prefix}{self.meters(minutes, zone)}mtr {self.target(zone)}"
        return f"{prefix}{round(minutes)}m {self.target(zone)}"

    def meters(self, minutes: float, zone: str) -> int:
        """Strecke, die in `minutes` bei dieser Zone geschwommen wird – auf 50 m gerundet (gerade Bahnzahl im
        25-m-Becken). Dieselbe Pace-Annahme wie das Zonenmodell, damit Dauer und Last übereinstimmen."""
        from .workout_syntax import parse_target
        frac = parse_target("swim", self.target(zone).split(), pace=self.swim_pace)["frac"]
        return max(50, int(round(minutes * 60 * frac * 100 / self.swim_pace / 50)) * 50)

    @staticmethod
    def pause(seconds: int) -> str:
        """Echte Pause: intervals.icu/Garmin zeigen „Erholung“ und schalten nach Ablauf selbst weiter."""
        return f"- {int(seconds)}s intensity=rest"

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
                "duration_s": spec["duration_min"] * 60,
                "target_load": round(spec["duration_min"] / 60 * spec.get("load_if", IF["test"]) ** 2 * 100, 1),
                "intensity": "test", "category": "TEST", "test_protocol": protocol}
    if sport not in PCT:
        rpe = {"recovery": 4, "easy": 5, "long": 6, "tempo": 7}.get(intensity, 7)
        if sport == "strength":
            rpe = 5 if intensity == "recovery" else 7  # schwere Grundübungen mit langen Pausen
        factor = get_float(conn, "srpe_factor", 1.0)
        custom = get_setting(conn, "strength_description") if sport == "strength" else None
        return {"title": f"{SPORT_LABEL.get(sport, sport)} {duration_min} min",
                "description": (custom + "\n\n" if custom else "")
                + f"{SPORT_LABEL.get(sport, sport)}, Ziel-RPE {rpe}. Nach der Einheit RPE melden.",
                "duration_s": duration_min * 60, "target_load": round(rpe * duration_min * factor, 1),
                "intensity": intensity, "category": "WORKOUT", "test_protocol": None}

    t = _Targets(conn, sport)
    lines: list[str] = []
    title = f"{SPORT_LABEL[sport]} {INTENSITY_LABEL.get(intensity, intensity)}"
    if intensity in WORK_REST[sport] and duration_min >= 35:
        work, rest = WORK_REST[sport][intensity]
        wu, cd = (15, 10) if sport != "swim" else (10, 5)
        main = duration_min - wu - cd
        reps = min(6, main // (work + rest))
        if reps < 2 and intensity != "tempo" and main >= 2 * (rest + 3):
            reps, work = 2, main // 2 - rest  # lieber zwei kürzere Wiederholungen als eine lange
        elif reps < 1:
            reps, work = 1, max(2, main - rest)
            rest = min(rest, main - work)
        filler = main - reps * (work + rest)  # Rest locker, damit die Summe exakt der Dauer entspricht
        title += f" {reps}x{work}"
        note = t.note(intensity)
        lines += ["Einlaufen" if sport == "run" else "Aufwärmen", t.step(wu, "wu"), ""]
        if note:
            lines.append(note)
        pause = t.pause(rest * 30) if sport == "swim" else t.step(rest, "rest")  # im Wasser: am Rand stehen
        lines += [f"{reps}x", t.step(work, intensity), pause, ""]
        if filler > 0:
            lines += [t.step(filler, "easy"), ""]
        lines += ["Ausklang", t.step(cd, "rest")]
    elif sport == "swim" and intensity in ("easy", "long", "recovery") and duration_min >= 30:
        # Schwimmen ist bei den meisten Triathleten Technik-limitiert: jede lockere Einheit mit Technikblock.
        # Alles in Metern mit echten Pausen: die Uhr schaltet im Becken selbst weiter.
        reps = 6 if duration_min >= 40 else 4
        wu, cd = 10, 5
        zone = intensity if intensity in PCT[sport] else "easy"
        drill = f"- Drill 50mtr {t.target('recovery')}"
        drill_min = 50 / 100 * t.swim_pace / 0.72 / 60 + 20 / 60
        main = max(4.0, duration_min - wu - cd - reps * drill_min)
        per_rep = 200 / 100 * t.swim_pace / 0.83 / 60 + 20 / 60  # 200 m locker + 20 s Pause
        main_reps = max(1, round(main / per_rep))
        title = f"Schwimmen Technik + {INTENSITY_LABEL.get(intensity, intensity)} {duration_min} min"
        note = t.note(zone)
        lines += ["Einschwimmen", t.step(wu, "wu"), "",
                  "Technik: Drills im Wechsel – Abschlagschwimmen, Faustschwimmen, Züge pro Bahn zählen",
                  f"{reps}x", drill, t.pause(20), ""]
        if note:
            lines.append(note)
        lines += ["Grundlage: lang gleiten, Wasserlage halten, unter Wasser ausatmen", f"{main_reps}x",
                  f"- 200mtr {t.target(zone)}", t.pause(20), "",
                  "Ausschwimmen", t.step(cd, "rest")]
    else:
        zone = intensity if intensity in PCT[sport] else "easy"
        if intensity in WORK_REST[sport]:  # kurze harte Einheit ohne Platz für Struktur -> Tempo-Dauer
            zone = "tempo"
        note = t.note(zone)
        if note:
            lines.append(note)
        lines.append(t.step(duration_min, zone))
        title += f" {duration_min} min"
    description = "\n".join(l for l in lines if l is not None).strip()
    from .zonemodel import description_zone_secs
    steps = description_zone_secs(description, sport, pace=t.swim_pace if sport == "swim" else None)
    duration_s = duration_min * 60
    if steps and sum(steps) > 0 and abs(sum(steps) - duration_s) > 1:
        # Planung, Last und Export rechnen mit derselben Dauer (Strecken im 50-m-Raster treffen sie nur ungefähr)
        duration_s = int(round(sum(steps)))
    return {"title": title, "description": description,
            "duration_s": duration_s, "target_load": estimate_load(duration_s / 60, intensity),
            "intensity": intensity, "category": "WORKOUT", "test_protocol": None}
