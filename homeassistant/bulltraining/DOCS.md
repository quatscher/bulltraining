# bulltraining

Trainingsplanung mit intervals.icu, Leistungstests und Plan-Vorschlägen per Claude (MCP).

## Optionen

- **intervals_api_key** – API-Key aus intervals.icu → Settings → Developer Settings. Leer = kein automatischer Sync.
- **intervals_athlete_id** – `0` = Besitzer des Keys.
- **mcp_token** – Token für den Claude-Zugang. Leer lassen: beim ersten Start wird eines erzeugt; es steht im Log
  und in der Weboberfläche unter *Einstellungen*.
- **sync_interval_minutes** – Abstand der automatischen Syncs, `0` = aus.

## Zugriff

- Weboberfläche: Seitenleiste → *Training* (über Home-Assistant-Login geschützt).
- Claude: `http://<IPv4 des Pi>:8765/mcp` mit `Authorization: Bearer <mcp_token>`. Die Portfreigabe gilt nur für
  IPv4; löst `homeassistant.local` am Rechner zuerst per IPv6 auf, schlägt die Verbindung sonst fehl. Die genauen Befehle für
  Claude Code und Claude Desktop stehen in der Weboberfläche unter *Einstellungen*.

## Bestehende Daten übernehmen

Vor dem ersten Start die bisherige `bulltraining.db` als `\\homeassistant\share\bulltraining\import.db` ablegen.
Der Import passiert nur, solange das Add-on noch keine eigene Datenbank hat; die Datei wird danach umbenannt.

## Daten und Backup

Die Datenbank liegt in `/data/bulltraining.db` des Add-ons und ist in den Home-Assistant-Backups enthalten.
