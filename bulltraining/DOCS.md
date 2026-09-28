# bulltraining

Endurance training planning for Home Assistant: mirrors your activities from intervals.icu, derives fitness and
load metrics, bases plans on your own performance tests and current training volume, and lets Claude propose plan
changes through MCP – every change needs your confirmation. The user interface is currently **German**.

> Not medical advice. The plan follows common training rules (load progression limits, recovery weeks, tests),
> but you are responsible for your training. Listen to your body.

## Configuration

| Option | Description |
|---|---|
| `intervals_api_key` | intervals.icu → Settings → Developer Settings. Can also be set on the add-on's settings page (takes precedence). |
| `intervals_athlete_id` | `0` = owner of the key. |
| `mcp_token` | Bearer token for Claude. Empty = generated on first start (see log and settings page). |
| `sync_interval_minutes` | Sync interval, `0` = off. Publishing workouts to intervals.icu is always a manual step. |

## Access

- **Web UI:** sidebar → *Training* (Home Assistant ingress, protected by your HA login). The web port is not
  exposed; if you want a friendly local name, point a reverse proxy (e.g. Nginx Proxy Manager) at the add-on's
  hostname on port 8099.
- **Claude (MCP):** enable port `8765/tcp` in the add-on's *Network* section, then connect to
  `http://<home-assistant-host>:8765/mcp` with header `Authorization: Bearer <mcp_token>`. The settings page shows
  ready-to-copy commands for Claude Code and Claude Desktop. The MCP server always requires the token.
  Note: on Home Assistant OS, published add-on ports are IPv4 only – use the IPv4 address or an IPv4-only name if
  your computer resolves the HA hostname to IPv6 first.

## Importing an existing database

Before the first start, put a copy of an existing `bulltraining.db` at `/share/bulltraining/import.db`
(`bulltraining export <path>` creates a consistent copy). It is imported only while the add-on has no database
yet; the file is renamed afterwards.

## Data and backups

The database lives in the add-on's `/data` and is included in Home Assistant backups.

---

# bulltraining (Deutsch)

Trainingsplanung für Ausdauersport in Home Assistant: spiegelt Aktivitäten aus intervals.icu, berechnet Form und
Belastung, baut Pläne auf deinen Leistungstests und deinem aktuellen Umfang auf und lässt Claude über MCP
Planänderungen vorschlagen – jede Änderung bestätigst du selbst.

- **Weboberfläche:** Seitenleiste → *Training* (über den HA-Login geschützt).
- **Claude:** Port `8765/tcp` unter *Netzwerk* aktivieren, dann `http://<HA-Host>:8765/mcp` mit
  `Authorization: Bearer <mcp_token>`. Die fertigen Befehle stehen in der Oberfläche unter *Einstellungen*.
- **Bestehende Daten:** vor dem ersten Start als `/share/bulltraining/import.db` ablegen.
- Kein medizinischer Rat.
