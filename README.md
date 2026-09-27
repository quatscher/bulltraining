# bulltraining

Persönliches Trainingssystem für Ausdauersport: spiegelt intervals.icu in eine lokale SQLite-DB, berechnet Form und Trainingsumfang, misst den Leistungszustand über Leistungstests und pflegt einen Trainingsplan, den ein LLM über MCP nur vorschlagen und nie selbst anwenden kann. Konzept und Begründungen stehen in [architecture.md](architecture.md).

## Einrichtung

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
$env:INTERVALS_API_KEY = "…"          # intervals.icu/settings -> Developer Settings
.venv\Scripts\bulltraining init
.venv\Scripts\bulltraining sync --full   # 365 Tage, danach inkrementell ohne --full
.venv\Scripts\bulltraining serve         # http://127.0.0.1:8000
```

Ohne API-Key ausprobieren: `bulltraining demo` legt 16 Wochen synthetisches Training, drei Tests und einen 70.3-Plan an (Schwimmen bewusst ohne Test). Mit `$env:BULLTRAINING_DB = "data\demo.db"` bleibt die Demo von der echten DB getrennt.

## Ohne Trainingshistorie starten

Liegen (noch) keine synchronisierten Aktivitäten vor, nimmt der Plan den Ausgangsumfang aus der Einstellung
`manual_baseline` (Selbstauskunft), bis echte Daten da sind:

```json
{"as_of": "2026-09-27", "load_per_hour": 50,
 "sports": {"run": {"hours_per_week": 3.4, "sessions_per_week": 3.5, "longest_min": 120},
            "ride": {"hours_per_week": 2.0, "sessions_per_week": 2, "longest_min": 60},
            "strength": {"sessions_per_week": 2, "minutes": 50}}}
```

Feste Einheiten (z. B. Pendeln) stehen je Plan in `plans.recurring` und werden in jede Woche eingeplant; sie zählen
zur Last und zum Anteil ihrer Sportart. Inhalt und Dauer der Krafteinheiten kommen aus `strength_description` und
`strength_minutes`.

## Ablauf

1. **Sync** (`bulltraining sync`, per Aufgabenplanung/Cron, oder Button im Posteingang): Aktivitäten und Wellness spiegeln, Dubletten markieren, geplante Einheiten mit absolvierten abgleichen.
2. **Leistungstests** (Ansicht *Leistung* oder `record_performance_test` im Chat): Das Ergebnis setzt die Schwellen (FTP, Schwellenpace, CSS, Schwellenpuls) und damit Zonen und Zielbereiche.
3. **Plan anlegen** (Ansicht *Pläne*): Zieltyp, Zielart, Wochenstunden, verfügbare Tage. Die Planübersicht zeigt, welche Tests fehlen und wie lange der Aufbau vom aktuellen Umfang bis zum Ziel dauert.
4. **Woche generieren** (Kalender-Button, `bulltraining next-week` oder im Chat `regenerate_week`): Das ergibt einen Vorschlag im Posteingang. Der Vorschlag hält den Lastkorridor ein, plant fehlende Tests zuerst ein und orientiert sich an Häufigkeit und Länge der aktuellen Einheiten.
5. **Bestätigen** im Posteingang; Änderungen lassen sich dort auch rückgängig machen.
6. **Zonen im Blick:** Kalender (je Tag) und *Form* (je Woche, 12 zurück, 4 voraus) zeigen die Zeit je Zone, geplant (hell) gegen absolviert (kräftig); jede Einheit hat zusätzlich einen Mini-Zonenbalken. Geplante Zonen werden aus der Workout-Beschreibung gelesen, absolvierte kommen aus intervals.icu.
7. **Einheit im Detail:** Klick auf eine geplante Einheit im Kalender zeigt den Ablauf Schritt für Schritt mit konkreten Zielen aus den aktuellen Tests (Dauer, Pace bzw. Watt, Pulsbereich), Wiederholungsblöcke und Zeit je Zone. Im Chat liefert `get_session` dasselbe als Text.
8. **Veröffentlichen** (`bulltraining publish` oder Button): bestätigte Einheiten der nächsten 14 Tage gehen nach intervals.icu und von dort auf die Uhr.

## Betrieb auf Home Assistant (Raspberry Pi)

Als lokales Home-Assistant-Add-on (`homeassistant/bulltraining`): Weboberfläche in der HA-Seitenleiste (Ingress,
über den HA-Login geschützt), MCP für Claude über HTTP auf Port 8765 mit Token, Sync im festen Intervall. Die
Datenbank liegt in `/data` des Add-ons und ist in den HA-Backups enthalten.

1. Im Samba-Add-on die Freigabe `addons` aktivieren (Einstellungen → Add-ons → Samba share → Konfiguration →
   `enabled_shares` um `addons` ergänzen, Add-on neu starten).
2. Add-on bauen und kopieren: `python homeassistant/build_addon.py \\homeassistant\addons`
3. Bisherige Daten übernehmen (optional, vor dem ersten Start):
   `bulltraining export \\homeassistant\share\bulltraining\import.db`
4. HA: Einstellungen → Add-ons → Add-on-Store → ⋮ → *Nach Updates suchen* → unter *Lokale Add-ons*
   „bulltraining“ installieren, API-Key in der Konfiguration eintragen, starten.
5. Claude verbinden: die fertigen Befehle stehen in der Weboberfläche unter *Einstellungen* (URL und Token).

Updates: Version in `pyproject.toml` erhöhen, Schritt 2 wiederholen, in HA *Neu erstellen*.

Hinweis: SQLite gehört auf den lokalen Datenträger des Add-ons, nicht auf eine Netzwerkfreigabe – über SMB sind
Dateisperren und der WAL-Modus nicht zuverlässig.

## MCP (Claude Desktop / Claude Code)

```json
{
  "mcpServers": {
    "bulltraining": {
      "command": "C:\\Users\\chris\\IdeaProjects\\bulltraining\\.venv\\Scripts\\python.exe",
      "args": ["-m", "bulltraining.mcp_server"],
      "env": { "BULLTRAINING_DB": "C:\\Users\\chris\\IdeaProjects\\bulltraining\\data\\bulltraining.db" }
    }
  }
}
```

Werkzeuge: `get_form_state`, `get_performance_state`, `get_training_load_context`, `get_week_summary`, `get_zone_distribution`, `get_wellness_trend`, `get_plan`, `get_session`, `get_activity`, `get_pending_changes`, `log_activity`, `update_activity`, `record_performance_test`, `propose_plan_change`.

## Aufbau

| Modul | Aufgabe |
| --- | --- |
| `db.py` | Schema, Einstellungen, Transaktionen |
| `intervals_client.py`, `sync.py` | REST-Client (User-Agent, Rate Limits, 429), inkrementeller Sync ohne Teilcommit |
| `activities.py`, `duplicates.py` | Normalisierung, lokale Erfassung mit Pflicht-RPE, Dublettenmarkierung und -auflösung |
| `metrics.py` | CTL/ATL/Form (Ausdauer und gesamt), Wochen, Zonen, Wellness, Trainingsumfang, Lastkorridor |
| `performance.py` | Testprotokolle, Schwellen, Gültigkeit, Leistungszustand, Zonen |
| `periodization.py`, `plan_templates/` | Phasen, Wochentyp, Referenzlast, Wochenziele; Vorlagen je Zielart als JSON |
| `workouts.py`, `generator.py` | Einheiten in Intervals-Syntax, Wochengenerator |
| `rules.py`, `plans.py` | Regeln, Vorschlag → Bestätigung → Anwendung, Undo |
| `publisher.py` | Events nach intervals.icu, Abgleich geplant/absolviert |
| `mcp_server.py`, `web/` | MCP-Werkzeuge, Frontend (FastAPI + Jinja2 + HTMX) |

Grenzwerte (Laststeigerung, harte Tage, Testgültigkeit, sRPE-Faktor usw.) stehen in der Tabelle `settings` und sind unter *Einstellungen* änderbar.

## Tests

```powershell
.venv\Scripts\python -m pytest
```
