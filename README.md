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

## Ablauf

1. **Sync** (`bulltraining sync`, per Aufgabenplanung/Cron, oder Button im Posteingang): Aktivitäten und Wellness spiegeln, Dubletten markieren, geplante Einheiten mit absolvierten abgleichen.
2. **Leistungstests** (Ansicht *Leistung* oder `record_performance_test` im Chat): Das Ergebnis setzt die Schwellen (FTP, Schwellenpace, CSS, Schwellenpuls) und damit Zonen und Zielbereiche.
3. **Plan anlegen** (Ansicht *Pläne*): Zieltyp, Zielart, Wochenstunden, verfügbare Tage. Die Planübersicht zeigt, welche Tests fehlen und wie lange der Aufbau vom aktuellen Umfang bis zum Ziel dauert.
4. **Woche generieren** (Kalender-Button, `bulltraining next-week` oder im Chat `regenerate_week`): Das ergibt einen Vorschlag im Posteingang. Der Vorschlag hält den Lastkorridor ein, plant fehlende Tests zuerst ein und orientiert sich an Häufigkeit und Länge der aktuellen Einheiten.
5. **Bestätigen** im Posteingang; Änderungen lassen sich dort auch rückgängig machen.
6. **Veröffentlichen** (`bulltraining publish` oder Button): bestätigte Einheiten der nächsten 14 Tage gehen nach intervals.icu und von dort auf die Uhr.

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

Werkzeuge: `get_form_state`, `get_performance_state`, `get_training_load_context`, `get_week_summary`, `get_zone_distribution`, `get_wellness_trend`, `get_plan`, `get_activity`, `get_pending_changes`, `log_activity`, `update_activity`, `record_performance_test`, `propose_plan_change`.

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
