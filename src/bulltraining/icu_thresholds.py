"""Schwellenwerte aus den eigenen Leistungstests nach intervals.icu übertragen (nur auf ausdrücklichen Klick).

intervals.icu und die Uhr berechnen Puls-, Pace- und Leistungszonen aus den Sport-Einstellungen dort. Stimmen die
nicht mit dem Test überein, zeigen Uhr und Aktivitätsauswertung andere Zonen als bulltraining.
Pace speichert intervals.icu als Geschwindigkeit in m/s; Pulszonen als absolute Obergrenzen in bpm.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import get_float
from .util import fmt_pace

# Sportart -> Typ in den Sport-Einstellungen von intervals.icu
ICU_TYPE = {"run": "Run", "ride": "Ride", "swim": "Swim"}
SPORT_LABEL = {"run": "Laufen", "ride": "Rad", "swim": "Schwimmen"}
# Obergrenzen in % LTHR (Friel), falls intervals.icu noch keine Pulszonen hat
DEFAULT_HR_ZONE_PCT = (84, 89, 94, 99, 102, 106)


def local_thresholds(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Werte aus den Tests im Format von intervals.icu (Pace als m/s)."""
    run_pace, css = get_float(conn, "threshold_pace_run_s_per_km"), get_float(conn, "css_s_per_100m")
    out: dict[str, dict[str, float]] = {
        "run": {"lthr": get_float(conn, "lthr_run"), "threshold_pace": 1000 / run_pace if run_pace else None},
        "ride": {"lthr": get_float(conn, "lthr_ride"), "ftp": get_float(conn, "ftp_w")},
        "swim": {"threshold_pace": 100 / css if css else None},
    }
    max_hr = get_float(conn, "max_hr")
    for sport in ("run", "ride"):
        out[sport]["max_hr"] = max_hr
    return {s: {k: v for k, v in vals.items() if v} for s, vals in out.items()}


def _show(field: str, sport: str, value: float | None) -> str:
    if value in (None, 0):
        return "–"
    if field == "threshold_pace":
        return fmt_pace(1000 / value if sport == "run" else 100 / value, "/km" if sport == "run" else "/100m")
    if field == "ftp":
        return f"{value:.0f} W"
    return f"{value:.0f}"


FIELD_LABEL = {"lthr": "Schwellenpuls", "max_hr": "Maximalpuls", "threshold_pace": "Schwellenpace", "ftp": "FTP"}


def _scaled_hr_zones(icu: dict[str, Any], lthr: float, max_hr: float | None) -> list[int]:
    """Pulszonen zur neuen Schwelle: bestehende Zonen proportional verschieben (ihr Modell bleibt erhalten),
    sonst Friel-Prozente. Die oberste Grenze ist der Maximalpuls."""
    old, old_lthr = icu.get("hr_zones") or [], icu.get("lthr")
    if old and old_lthr:
        zones = [round(z * lthr / old_lthr) for z in old[:-1]]
    else:
        zones = [round(lthr * p / 100) for p in DEFAULT_HR_ZONE_PCT]
    top = max_hr or icu.get("max_hr") or (old[-1] if old else round(lthr * 1.1))
    zones = [min(z, int(top) - 1) for z in zones]
    return zones + [int(top)]


def diff(conn: sqlite3.Connection, sport_settings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Je Sportart die Felder, die sich unterscheiden, mit Anzeige vorher/nachher und dem Schreib-Payload."""
    local = local_thresholds(conn)
    out = []
    for sport, vals in local.items():
        icu = next((s for s in sport_settings if ICU_TYPE[sport] in (s.get("types") or [])), None)
        if icu is None or not vals:
            continue
        changes, payload = [], {}
        for field, new in vals.items():
            old = icu.get(field)
            same = old is not None and abs(float(old) - float(new)) <= (0.005 * float(new) if field == "threshold_pace"
                                                                        else 0.5)
            if same:
                continue
            changes.append({"field": field, "label": FIELD_LABEL[field],
                            "old": _show(field, sport, old), "new": _show(field, sport, new)})
            payload[field] = round(float(new), 4) if field == "threshold_pace" else round(float(new))
        if "lthr" in payload or "max_hr" in payload:
            lthr = payload.get("lthr") or icu.get("lthr")
            if lthr:
                payload["hr_zones"] = _scaled_hr_zones(icu, float(lthr), payload.get("max_hr"))
                changes.append({"field": "hr_zones", "label": "Pulszonen (Obergrenzen)",
                                "old": ", ".join(str(z) for z in icu.get("hr_zones") or []) or "–",
                                "new": ", ".join(str(z) for z in payload["hr_zones"])})
        if changes:
            out.append({"sport": sport, "label": SPORT_LABEL[sport], "settings_id": icu.get("id"),
                        "changes": changes, "payload": payload})
    return out


def push(conn: sqlite3.Connection, client: Any) -> list[dict[str, Any]]:
    """Schreibt die Unterschiede; gibt die übertragenen Sportarten zurück."""
    pending = diff(conn, client.sport_settings())
    for d in pending:
        client.update_sport_settings(d["settings_id"], d["payload"])
    return pending
