"""Parser für Workout-Beschreibungen in Intervals-Syntax (klar begrenzter Umfang).

Eine Beschreibung geht unverändert auf die Uhr. Deshalb muss jede Zeile, die das Training steuert, verstanden sein:
Im strikten Modus (Validierung von Vorschlägen) wird alles Unbekannte mit Zeilenangabe abgelehnt; im toleranten
Modus (Anzeige, Zonenbalken) werden unbekannte Zeilen übersprungen.

Unterstützt:
- Textzeilen (Überschriften, Hinweise)
- Wiederholungsblock: Zeile endet auf "<n>x" – optional mit Beschriftung ("Main set 6x"); Block endet an Leerzeile
  oder Textzeile
- Schritt: "- [Beschriftung] <Dauer> [Ziel] [Trittfrequenz]"
  Dauer: 10m, 30s, 1h, 1h30m, 1m30s | Distanz: 400mtr, 5km
  Ziel: leer | rest | max | 55-65% (Rad: % FTP) | 78-86% Pace | 90% HR / 95% LTHR | Z2 / Z1-Z2 [HR|Pace|Power]
        | 200w / 180-220w | ramp 50-150% | 4:30/km Pace / 1:50-2:00/100m Pace
  Trittfrequenz: 90rpm / 85-95rpm (wird ignoriert)
"""
from __future__ import annotations

import re
from typing import Any


class WorkoutSyntaxError(ValueError):
    pass


# Obergrenzen je Zone (Z1..Z4) in % der Schwelle; darüber Z5
UPPER = {"ride": (55, 75, 90, 105), "run": (78, 88, 95, 102), "swim": (80, 90, 97, 102)}
HR_UPPER = (84, 89, 94, 99)  # % Schwellenpuls
DEFAULT_PACE = {"run": 330.0, "swim": 120.0}
ZONE_FRAC = (0.72, 0.83, 0.90, 0.98, 1.05)  # Anteil Schwellengeschwindigkeit je Zone (für Distanzschritte)

_TIME = re.compile(r"^(?:\d+(?:\.\d+)?(?:h|m|s))+$", re.I)
_DIST = re.compile(r"^\d+(?:\.\d+)?(?:mtr|km)$", re.I)
_REPEAT = re.compile(r"^(?P<label>.*?)\s*\b(?P<n>\d+)\s*x$", re.I)
_RPM = re.compile(r"^\d+(?:-\d+)?rpm$", re.I)
_PCT = re.compile(r"^(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?%$")
_WATTS = re.compile(r"^(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?w$", re.I)
_ZONE = re.compile(r"^z(\d)(?:-z(\d))?$", re.I)
_PACE = re.compile(r"^(\d+:\d\d)(?:-(\d+:\d\d))?/(km|100m)$", re.I)


def zone_for_pct(sport: str, pct: float) -> int:
    for i, upper in enumerate(UPPER.get(sport, UPPER["ride"])):
        if pct <= upper:
            return i
    return 4


def _zone_for_hr_pct(pct: float) -> int:
    for i, upper in enumerate(HR_UPPER):
        if pct <= upper:
            return i
    return 4


def _mmss(text: str) -> float:
    m, s = text.split(":")
    return int(m) * 60 + int(s)


def parse_target(sport: str, tokens: list[str], ftp: float | None = None,
                 pace: float | None = None) -> dict[str, Any] | None:
    """Ziel eines Schritts. None = nicht unterstützt."""
    toks = [t for t in tokens if not _RPM.match(t)]  # Trittfrequenz steuert keine Belastung
    low = [t.lower() for t in toks]
    if not low:
        return {"kind": "none", "zone": 1, "frac": ZONE_FRAC[1]}
    if low == ["rest"] or low == ["recovery"]:
        return {"kind": "rest", "zone": 0, "frac": 0.65}
    if low == ["max"]:
        return {"kind": "max", "zone": 4, "frac": 1.05}
    ramp = low[0] == "ramp"
    if ramp:
        low = low[1:]
    if not low:
        return None
    head, rest = low[0], low[1:]
    m = _PCT.match(head)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2) or m.group(1))
        mid = lo + (hi - lo) * (0.6 if ramp else 0.5)
        if rest in ([], ["power"]) and sport == "ride":
            return {"kind": "ramp" if ramp else "pct", "unit": "ftp", "lo": lo, "hi": hi,
                    "zone": zone_for_pct(sport, mid), "frac": mid / 100}
        if rest == ["pace"] and sport in ("run", "swim"):
            return {"kind": "ramp" if ramp else "pct", "unit": "pace", "lo": lo, "hi": hi,
                    "zone": zone_for_pct(sport, mid), "frac": mid / 100}
        if rest in (["hr"], ["lthr"]) and not ramp:
            z = _zone_for_hr_pct(mid)
            return {"kind": "hr_pct", "unit": "lthr", "lo": lo, "hi": hi, "zone": z, "frac": ZONE_FRAC[z]}
        return None  # z. B. "80%" beim Laufen: unklar, ob Pace, Puls oder Leistung
    if ramp:
        return None
    m = _WATTS.match(head)
    if m and not rest and sport == "ride":
        if not ftp:
            return None  # ohne FTP lässt sich die Belastung nicht bewerten
        lo, hi = float(m.group(1)), float(m.group(2) or m.group(1))
        mid = (lo + hi) / 2 / ftp * 100
        return {"kind": "watts", "lo": lo, "hi": hi, "zone": zone_for_pct(sport, mid), "frac": mid / 100}
    m = _ZONE.match(head)
    if m and rest in ([], ["hr"], ["pace"], ["power"]):
        z1, z2 = int(m.group(1)), int(m.group(2) or m.group(1))
        z = max(0, min(4, (z1 + z2) // 2 - 1))
        return {"kind": "zone", "unit": (rest or ["zone"])[0], "z_lo": max(0, min(4, z1 - 1)),
                "z_hi": max(0, min(4, z2 - 1)), "zone": z, "frac": ZONE_FRAC[z]}
    m = _PACE.match(head)
    if m and rest in ([], ["pace"]) and sport in ("run", "swim"):
        unit = m.group(3).lower()
        if (unit == "km") != (sport == "run") or not pace:
            return None
        slow = _mmss(m.group(1))
        fast = _mmss(m.group(2)) if m.group(2) else slow
        fast, slow = min(fast, slow), max(fast, slow)
        lo, hi = pace / slow * 100, pace / fast * 100  # % Schwellengeschwindigkeit
        mid = (lo + hi) / 2
        return {"kind": "pace_abs", "fast": fast, "slow": slow, "lo": lo, "hi": hi,
                "zone": zone_for_pct(sport, mid), "frac": mid / 100}
    return None


def duration_seconds(dur: str, sport: str, frac: float, pace: float | None) -> float:
    d = dur.lower()
    if _DIST.match(d):
        meters = float(d[:-3]) if d.endswith("mtr") else float(d[:-2]) * 1000
        base = pace or DEFAULT_PACE.get(sport, 330.0)
        per_m = base / (100 if sport == "swim" else 1000)
        return meters * per_m / max(frac, 0.3)
    return sum(float(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", d))


def parse(description: str | None, sport: str, ftp: float | None = None, pace: float | None = None,
          strict: bool = False) -> list[dict[str, Any]]:
    """Beschreibung -> Liste aus {"type": "text"|"step"|"repeat", ...}. Strikt: unbekannte Schritte/Ziele -> Fehler."""
    items: list[dict[str, Any]] = []
    block: dict[str, Any] | None = None
    for no, raw in enumerate((description or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            block = None
            continue
        if line.startswith("-"):
            tokens = line[1:].split()
            idx = next((i for i, t in enumerate(tokens) if _TIME.match(t) or _DIST.match(t)), None)
            if idx is None:
                if strict:
                    raise WorkoutSyntaxError(f"Zeile {no}: Schritt ohne erkennbare Dauer: '{line}'.")
                continue
            dur, label, target_tokens = tokens[idx], " ".join(tokens[:idx]), tokens[idx + 1:]
            target = parse_target(sport, target_tokens, ftp, pace)
            if target is None:
                if strict:
                    raise WorkoutSyntaxError(f"Zeile {no}: nicht unterstützte Vorgabe "
                                             f"'{' '.join(target_tokens)}' für {sport}.")
                target = {"kind": "unknown", "zone": 1, "frac": ZONE_FRAC[1]}
            step = {"type": "step", "line": no, "label": label, "dur": dur,
                    "distance": bool(_DIST.match(dur)), "target": target, "target_text": " ".join(target_tokens),
                    "seconds": duration_seconds(dur, sport, target["frac"], pace)}
            (block["steps"] if block is not None else items).append(step)
            continue
        rep = _REPEAT.match(line)
        if rep and 1 <= int(rep.group("n")) <= 50:
            block = {"type": "repeat", "count": int(rep.group("n")), "label": rep.group("label").strip(" :"),
                     "steps": [], "line": no}
            items.append(block)
            continue
        block = None
        items.append({"type": "text", "text": line, "line": no})
    if strict:
        for it in items:
            if it["type"] == "repeat" and not it["steps"]:
                raise WorkoutSyntaxError(f"Zeile {it['line']}: Wiederholung {it['count']}x ohne Schritte.")
    return items


def zone_seconds(items: list[dict[str, Any]]) -> list[float]:
    secs = [0.0] * 5
    for it in items:
        if it["type"] == "step":
            secs[it["target"]["zone"]] += it["seconds"]
        elif it["type"] == "repeat":
            for s in it["steps"]:
                secs[s["target"]["zone"]] += s["seconds"] * it["count"]
    return secs
