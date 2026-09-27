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

# Erlaubte Werte je Einstellung: (Typ, min, max, leer erlaubt). Ungültiges wird beim Speichern abgewiesen und
# beim Lesen durch den Standard ersetzt – eine Tippfehler-Einstellung darf keine Seite lahmlegen.
SETTINGS_SCHEMA: dict[str, tuple] = {
    "srpe_factor": ("float", 0.01, 5, False),
    "max_weekly_load_increase_pct": ("float", 0, 50, False),
    "max_hard_days": ("int", 0, 7, False),
    "max_ctl_ramp_per_week": ("float", 0.5, 20, False),
    "underload_min_pct": ("float", 0, 100, False),
    "long_session_max_increase_pct": ("float", 0, 100, False),
    "recovery_week_pct": ("float", 20, 100, False),
    "baseline_weeks": ("int", 1, 12, False),
    "test_validity_days": ("int", 7, 365, False),
    "retest_lead_days": ("int", 0, 60, False),
    "ftp_w": ("float", 50, 600, True),
    "lthr_run": ("float", 100, 210, True),
    "lthr_ride": ("float", 100, 210, True),
    "threshold_pace_run_s_per_km": ("float", 150, 600, True),
    "css_s_per_100m": ("float", 55, 240, True),
    "max_hr": ("float", 120, 230, True),
    "manual_baseline": ("baseline_json", None, None, True),
    "strength_minutes": ("int", 10, 180, False),
    "strength_description": ("text", None, None, True),
    "publish_lock": ("text", None, None, True),
}

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
    # Ausgangsumfang aus Selbstauskunft, solange keine synchronisierten Daten vorliegen (JSON, siehe README)
    "manual_baseline": "",
    # Krafttraining: Dauer und Inhalt der geplanten Einheiten
    "strength_minutes": "45",
    "strength_description": "",
    "publish_lock": "",
}
