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
from .zonemodel import ZONE_COLORS, ZONES, _HRZ, _PCT, _REPEAT, _STEP, _duration_s, _target_zone

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


def step_detail(th: Thresholds, sport: str, dur: str, target: str) -> dict[str, Any]:
    zone, frac = _target_zone(sport, target)
    t = target.strip().lower()
    targets: list[tuple[str, str]] = []
    word = ZONE_WORD[zone]
    z_lo = z_hi = zone
    pct = _PCT.search(t)
    hrz = _HRZ.search(t)
    is_hr = bool(hrz) and "hr" in t.split()
    if not t or t.startswith("rest"):
        word = "Pause" if sport == "swim" or t.startswith("rest") else "locker"
    elif "max" in t.split():
        word = "maximal"
        targets.append(("Intensität", "so schnell wie möglich, gleichmäßig"))
    elif pct:
        lo = float(pct.group(1))
        hi = float(pct.group(2) or pct.group(1))
        if "ramp" in t:
            word = "Rampe"
            if sport == "ride" and th.ftp:
                targets.append(("Leistung", f"{round(th.ftp * lo / 100)} W → {round(th.ftp * hi / 100)} W, bis zum Abbruch"))
        elif sport == "ride" or "pace" not in t:
            power = _power_range(th, lo, hi)
            targets.append(("Leistung", power or f"{_pct(lo, hi)} FTP"))
        else:
            pace = _pace_range(th, sport, lo, hi)
            targets.append(("Pace", pace or f"{_pct(lo, hi)} der Schwellenpace"))
    elif hrz:
        z_lo = max(0, min(4, int(hrz.group(1)) - 1))
        z_hi = max(0, min(4, int(hrz.group(2) or hrz.group(1)) - 1))
        word = ZONE_WORD[z_hi] if z_lo == z_hi else f"{ZONE_WORD[z_lo]} bis {ZONE_WORD[z_hi]}"
        # Pulsvorgabe: Pace/Leistung nur als Orientierung dazu
        if sport in SPEED_BAND:
            approx = _pace_range(th, sport, SPEED_BAND[sport][z_lo][0], SPEED_BAND[sport][z_hi][1])
            if approx:
                targets.append(("Pace ca.", approx))
        elif sport == "ride":
            approx = _power_range(th, POWER_BAND[z_lo][0], POWER_BAND[z_hi][1])
            if approx:
                targets.append(("Leistung ca.", approx))
    hr = _hr_range(th, sport, z_lo, z_hi)
    if hr and word not in ("maximal", "Rampe"):
        targets.append(("Puls" if is_hr else "Puls ca.", hr))
    elif is_hr and not hr:
        targets.append(("Puls", f"Zone {hrz.group(0).upper()}"))
    est = _duration_s(dur, sport, frac, th.pace.get(sport))
    is_distance = dur.lower().endswith(("mtr", "km"))
    return {"type": "step", "duration": _duration_text(dur), "duration_s": round(est),
            "estimated": is_distance, "zone": zone, "zone_label": ZONES[zone], "color": ZONE_COLORS[zone],
            "word": word, "targets": targets, "raw": f"- {dur} {target}".strip()}


def workout_steps(conn: sqlite3.Connection | Thresholds, sport: str, description: str | None) -> list[dict[str, Any]]:
    """Schritte, Wiederholungsblöcke und Zwischenüberschriften einer Beschreibung."""
    th = conn if isinstance(conn, Thresholds) else Thresholds(conn)
    items: list[dict[str, Any]] = []
    block: dict[str, Any] | None = None
    for raw in (description or "").splitlines():
        line = raw.strip()
        if not line:
            block = None
            continue
        rep = _REPEAT.match(line)
        if rep:
            block = {"type": "repeat", "count": int(rep.group(1)), "steps": []}
            items.append(block)
            continue
        m = _STEP.match(line)
        if m:
            step = step_detail(th, sport, m.group("dur"), m.group("target"))
            (block["steps"] if block else items).append(step)
            continue
        block = None
        if _GENERATED_NOTE.match(line):
            continue  # vom Generator gespeicherte Zielwerte veralten mit jedem Test – hier wird live gerechnet
        items.append({"type": "text", "text": line})
    # "1x" ist kein echter Block
    flat: list[dict[str, Any]] = []
    for it in items:
        if it["type"] == "repeat" and it["count"] == 1:
            flat.extend(it["steps"])
        else:
            flat.append(it)
    items = flat
    for it in items:
        if it["type"] == "repeat":
            it["duration_s"] = it["count"] * sum(s["duration_s"] for s in it["steps"])
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
