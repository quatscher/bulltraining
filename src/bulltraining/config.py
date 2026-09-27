"""Konfiguration aus Umgebungsvariablen und Standardwerte für die Tabelle `settings`."""
from __future__ import annotations

import os
from pathlib import Path

DB_PATH = Path(os.environ.get("BULLTRAINING_DB", "data/bulltraining.db"))
INTERVALS_API_KEY = os.environ.get("INTERVALS_API_KEY", "")
INTERVALS_ATHLETE_ID = os.environ.get("INTERVALS_ATHLETE_ID", "0")
INTERVALS_BASE_URL = os.environ.get("INTERVALS_BASE_URL", "https://intervals.icu")

# Browserähnlicher User-Agent wegen Cloudflare (siehe architecture.md, "Sync").
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 bulltraining/0.1"
)

# Alle Grenzwerte sind Einstellungen, kein Code. Werte als Strings, wie sie in `settings` liegen.
DEFAULT_SETTINGS: dict[str, str] = {
    # Belastung
    # sRPE-Punkte -> Ausdauerskala. 1 h an der Schwelle = 100 Lastpunkte, als sRPE aber RPE 7–8 × 60 = 420–480.
    # Daher Startwert ~0,2 statt 1,0; nach vier Wochen an vergleichbaren Einheiten nachjustieren.
    "srpe_factor": "0.2",
    # Progressionsregeln (Validierung von Planänderungen)
    "max_weekly_load_increase_pct": "10",  # Wochenlast vs. Vorwoche
    "max_hard_days": "3",
    "max_ctl_ramp_per_week": "6",          # CTL-Anstieg pro Woche, Obergrenze
    "underload_min_pct": "85",             # Belastungswoche nicht unter x % der Referenz
    "long_session_max_increase_pct": "15", # längste Einheit vs. längste der letzten 4 Wochen
    "recovery_week_pct": "65",             # Entlastungswoche in % der Vorwoche
    "baseline_weeks": "4",                 # Fenster für den aktuellen Trainingsumfang
    # Leistungstests
    "test_validity_days": "56",            # älter = veraltet, Plan fordert neuen Test
    "retest_lead_days": "14",              # so früh vor Ablauf in Entlastungswoche testen
    # Schwellenwerte (werden durch Leistungstests gesetzt, hier nur leer angelegt)
    "ftp_w": "",
    "lthr_run": "",
    "lthr_ride": "",
    "threshold_pace_run_s_per_km": "",
    "css_s_per_100m": "",
    "max_hr": "",
}
