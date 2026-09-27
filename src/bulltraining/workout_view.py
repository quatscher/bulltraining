"""Geplante Einheit als Schrittfolge mit konkreten Zielwerten ("10 min, Pace 5:40–6:05/km, Puls 138–146").

Quelle ist die Workout-Beschreibung in Intervals-Syntax – also genau das, was auf die Uhr geht. Prozentangaben
werden mit den aktuellen Schwellen aus den Leistungstests in Watt, Pace und Puls umgerechnet.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from .db import get_float
from .util import fmt_pace
from . import workout_syntax as syntax
from .zonemodel import ZONE_COLORS, ZONES

ZONE_WORD = ["locker", "Grundlage", "Tempo", "Schwelle", "hart"]
# Zielbänder je Zone (5-Zonen-Modell) für Näherungen, wenn die Vorgabe eine andere Größe nutzt
POWER_BAND = [(45, 55), (56, 75), (76, 90), (91, 105), (106, 120)]            # % FTP
SPEED_BAND = {"run": [(70, 78), (78, 88), (88, 95), (95, 102), (102, 110)],   # % Schwellengeschwindigkeit
              "swim": [(75, 80), (80, 90), (90, 97), (97, 102), (102, 108)]}
HR_BAND = {"run": [(75, 84), (85, 89), (90, 94), (95, 99), (100, 106)],        # % Schwellenpuls (Friel)
           "ride": [(70, 80), (81, 89), (90, 93), (94, 99), (100, 106)]}
_GENERATED_NOTE = re.compile(r"^Ziel .+\((FTP|Schwelle) [^)]*\)$")
HRMAX_BAND = [(50, 60), (60, 70), (70, 80), (80, 90), (90, 100)]               # % HFmax, falls keine Schwelle


class Thresholds:
    def __init__(self, conn: sqlite3.Connection):
        self.ftp = get_float(conn, "ftp_w")
        self.pace = {"run": get_float(conn, "threshold_pace_run_s_per_km"), "swim": get_float(conn, "css_s_per_100m")}
        self.lthr = {"run": get_float(conn, "lthr_run"), "ride": get_float(conn, "lthr_ride")}
        self.max_hr = get_float(conn, "max_hr")


def _unit(sport: str) -> str:
    return "/100 m" if sport == "swim" else "/km"


def _pace_range(th: Thresholds, sport: str, lo: float, hi: float) -> str | None:
    t = th.pace.get(sport)
    if not t:
        return None
    fast, slow = fmt_pace(t / (hi / 100), ""), fmt_pace(t / (lo / 100), "")
    return f"{fast}{_unit(sport)}" if fast == slow else f"{fast}–{slow}{_unit(sport)}"


def _power_range(th: Thresholds, lo: float, hi: float) -> str | None:
    if not th.ftp:
        return None
    a, b = round(th.ftp * lo / 100), round(th.ftp * hi / 100)
    return f"{a} W" if a == b else f"{a}–{b} W"


def _hr_range(th: Thresholds, sport: str, z_lo: int, z_hi: int) -> str | None:
    lthr = th.lthr.get(sport)
    if lthr and sport in HR_BAND:
        lo, hi = HR_BAND[sport][z_lo][0], HR_BAND[sport][z_hi][1]
        return f"{round(lthr * lo / 100)}–{round(lthr * hi / 100)} bpm"
    if th.max_hr and sport != "swim":
        lo, hi = HRMAX_BAND[z_lo][0], HRMAX_BAND[z_hi][1]
        return f"{round(th.max_hr * lo / 100)}–{round(th.max_hr * hi / 100)} bpm"
    return None


def _pct(lo: float, hi: float) -> str:
    return f"{lo:g} %" if lo == hi else f"{lo:g}–{hi:g} %"


def _duration_text(dur: str) -> str:
    d = dur.lower()
    if d.endswith("mtr"):
        return f"{d[:-3]} m"
    if d.endswith("km"):
        return f"{d[:-2]} km"
    parts = re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", d)
    return " ".join(f"{n} {'h' if u == 'h' else ('min' if u == 'm' else 's')}" for n, u in parts)


def step_detail(th: Thresholds, sport: str, step: dict[str, Any]) -> dict[str, Any]:
    """Anzeige eines geparsten Schritts: Wort, Zone und konkrete Ziele aus den aktuellen Schwellen."""
    tg = step["target"]
    kind, zone = tg["kind"], tg["zone"]
    targets: list[tuple[str, str]] = []
    word = ZONE_WORD[zone]
    z_lo = z_hi = zone
    hr_given = False
    if kind == "rest":
        word = "Pause"
    elif kind == "max":
        word = "maximal"
        targets.append(("Intensität", "so schnell wie möglich, gleichmäßig"))
    elif kind == "ramp":
        word = "Rampe"
        if sport == "ride" and th.ftp:
            targets.append(("Leistung", f"{round(th.ftp * tg['lo'] / 100)} W → {round(th.ftp * tg['hi'] / 100)} W, "
                                        "bis zum Abbruch"))
    elif kind == "pct" and tg["unit"] == "ftp":
        targets.append(("Leistung", _power_range(th, tg["lo"], tg["hi"]) or f"{_pct(tg['lo'], tg['hi'])} FTP"))
    elif kind == "pct":
        targets.append(("Pace", _pace_range(th, sport, tg["lo"], tg["hi"]) or f"{_pct(tg['lo'], tg['hi'])} der Schwellenpace"))
    elif kind == "watts":
        targets.append(("Leistung", f"{tg['lo']:.0f} W" if tg["lo"] == tg["hi"] else f"{tg['lo']:.0f}–{tg['hi']:.0f} W"))
    elif kind == "pace_abs":
        unit = _unit(sport)
        fast, slow = fmt_pace(tg["fast"], ""), fmt_pace(tg["slow"], "")
        targets.append(("Pace", f"{fast}{unit}" if fast == slow else f"{fast}–{slow}{unit}"))
    elif kind == "hr_pct":
        hr_given = True
        lthr = th.lthr.get(sport)
        targets.append(("Puls", f"{round(lthr * tg['lo'] / 100)}–{round(lthr * tg['hi'] / 100)} bpm" if lthr
                        else f"{_pct(tg['lo'], tg['hi'])} Schwellenpuls"))
    elif kind == "zone":
        z_lo, z_hi = tg["z_lo"], tg["z_hi"]
        hr_given = tg["unit"] == "hr"
        word = ZONE_WORD[z_hi] if z_lo == z_hi else f"{ZONE_WORD[z_lo]} bis {ZONE_WORD[z_hi]}"
        if sport in SPEED_BAND:
            approx = _pace_range(th, sport, SPEED_BAND[sport][z_lo][0], SPEED_BAND[sport][z_hi][1])
            if approx:
                targets.append(("Pace ca.", approx))
        elif sport == "ride":
            approx = _power_range(th, POWER_BAND[z_lo][0], POWER_BAND[z_hi][1])
            if approx:
                targets.append(("Leistung ca.", approx))
    elif kind == "unknown":
        targets.append(("Vorgabe", step["target_text"] or "–"))
    if kind not in ("max", "ramp", "rest", "hr_pct", "unknown"):
        hr = _hr_range(th, sport, z_lo, z_hi)
        if hr:
            targets.append(("Puls" if hr_given else "Puls ca.", hr))
        elif hr_given:
            label = f"Z{z_lo + 1}" if z_lo == z_hi else f"Z{z_lo + 1}-Z{z_hi + 1}"
            targets.append(("Puls", f"Zone {label}"))
    if step["label"]:
        word = f"{step['label']} · {word}"
    return {"type": "step", "duration": _duration_text(step["dur"]), "duration_s": round(step["seconds"]),
            "estimated": step["distance"], "zone": zone, "zone_label": ZONES[zone], "color": ZONE_COLORS[zone],
            "word": word, "targets": targets, "raw": f"- {step['dur']} {step['target_text']}".strip()}


def workout_steps(conn: sqlite3.Connection | Thresholds, sport: str, description: str | None) -> list[dict[str, Any]]:
    """Schritte, Wiederholungsblöcke und Zwischenüberschriften einer Beschreibung."""
    th = conn if isinstance(conn, Thresholds) else Thresholds(conn)
    parsed = syntax.parse(description, sport, ftp=th.ftp, pace=th.pace.get(sport))
    items: list[dict[str, Any]] = []
    for it in parsed:
        if it["type"] == "text":
            if not _GENERATED_NOTE.match(it["text"]):  # gespeicherte Zielwerte veralten – hier wird live gerechnet
                items.append({"type": "text", "text": it["text"]})
        elif it["type"] == "step":
            items.append(step_detail(th, sport, it))
        elif it["count"] == 1:  # "1x" ist kein echter Block
            items.extend(step_detail(th, sport, s) for s in it["steps"])
        else:
            steps = [step_detail(th, sport, s) for s in it["steps"]]
            items.append({"type": "repeat", "count": it["count"], "label": it["label"], "steps": steps,
                          "duration_s": it["count"] * sum(s["duration_s"] for s in steps)})
    return items


def steps_as_text(items: list[dict[str, Any]]) -> list[str]:
    """Kompakte Textform, z. B. für das LLM: '10 min Grundlage – Pace 5:40–6:05/km, Puls 138–146 bpm'."""
    def one(s: dict[str, Any]) -> str:
        tgt = ", ".join(f"{k} {v}" for k, v in s["targets"])
        return f"{s['duration']} {s['word']}" + (f" – {tgt}" if tgt else "")
    out = []
    for it in items:
        if it["type"] == "text":
            out.append(f"# {it['text']}")
        elif it["type"] == "step":
            out.append(one(it))
        else:
            out.append(f"{it['count']}× [" + " / ".join(one(s) for s in it["steps"]) + "]")
    return out
