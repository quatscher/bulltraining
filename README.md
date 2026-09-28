# bulltraining

[![CI](https://github.com/quatscher/bulltraining/actions/workflows/ci.yml/badge.svg)](https://github.com/quatscher/bulltraining/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

Endurance training planning (running, cycling, swimming, triathlon) that runs next to your data – as a Home Assistant
add-on or locally. It mirrors your activities from [intervals.icu](https://intervals.icu), measures your current
performance with field tests, builds week-by-week plans from your tests and your *actual* training volume, and lets
an LLM such as Claude propose plan changes through [MCP](https://modelcontextprotocol.io) – but never apply them:
every change is a proposal with a reason, a diff and an undo.

**Status:** early version, user interface in German. Not medical advice.

## Features

- **Performance tests as the basis:** FTP (20-min / ramp), run threshold (30-min TT / 5 km), swim CSS. Tests set
  zones and targets; missing or stale tests are scheduled first, hard sessions follow after the result.
- **Load corridor:** progression limited against acute and chronic load, CTL ramp cap, underload warnings, 3:1
  blocks, taper; long sessions grow from what you currently do.
- **Plans as data, not prose:** typed operations (move, shorten, regenerate week, …), validated against rules before
  they become a proposal, re-validated when you confirm; conflict-checked undo.
- **Strict workout parser:** workouts in intervals.icu syntax; everything that is exported must be understood.
- **Zones and details:** time in zone planned vs. done, each session step by step with pace/power/heart rate.
- **Publishing:** confirmed workouts go to intervals.icu (and from there to Garmin etc.), with revision tracking.
- **Self-reported baseline and fixed sessions** (e.g. a bike commute) when you start without history.

## Home Assistant add-on

1. Settings → Add-ons → Add-on store → ⋮ → **Repositories** → add `https://github.com/quatscher/bulltraining`
2. Install **bulltraining**, enter your intervals.icu API key in the configuration, start.
3. Open **Training** in the sidebar.
4. For Claude: enable port `8765/tcp` in the add-on's *Network* section; the settings page shows the commands for
   Claude Code and Claude Desktop (URL and token).

Details: [bulltraining/DOCS.md](bulltraining/DOCS.md).

## Local use

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"            # Windows: .venv\Scripts\pip
export INTERVALS_API_KEY=...                  # intervals.icu → Settings → Developer Settings
bulltraining init
bulltraining sync --full                      # 365 days, afterwards incremental
bulltraining serve                            # http://127.0.0.1:8000
bulltraining demo                             # synthetic data to try it without an API key
```

Claude Desktop via stdio:

```json
{"mcpServers": {"bulltraining": {"command": "/path/to/.venv/bin/python", "args": ["-m", "bulltraining.mcp_server"],
  "env": {"BULLTRAINING_DB": "/path/to/data/bulltraining.db"}}}}
```

MCP tools: `get_form_state`, `get_performance_state`, `get_training_load_context`, `get_week_summary`,
`get_zone_distribution`, `get_wellness_trend`, `get_plan`, `get_session`, `get_activity`, `get_pending_changes`,
`log_activity`, `update_activity`, `record_performance_test`, `propose_plan_change`.

## Development

```bash
python -m pytest                              # tests
python tools/build_addon.py <dir>             # add-on for a local build, e.g. into /addons on the HA host
```

Images are built by GitHub Actions on version tags (`v0.3.0`); `pyproject.toml` and `bulltraining/config.yaml` must
carry the same version. Design and rationale (German): [architecture.md](architecture.md).

| Module | Purpose |
| --- | --- |
| `db.py` | schema, migrations, settings, transactions |
| `intervals_client.py`, `sync.py` | REST client (rate limits, 429), incremental sync |
| `activities.py`, `duplicates.py` | normalisation, manual entries with RPE, duplicate handling |
| `metrics.py` | CTL/ATL/form, weeks, zones, wellness, training baseline, load corridor |
| `performance.py` | test protocols, thresholds, validity, performance state, zones |
| `periodization.py`, `plan_templates/` | phases, week type, reference load, week targets; templates per goal |
| `workouts.py`, `workout_syntax.py`, `generator.py` | workouts, strict parser, week generator |
| `rules.py`, `plans.py` | rules, proposal → confirmation → apply, undo |
| `publisher.py` | events to intervals.icu with revisions, reconcile planned vs. done |
| `mcp_server.py`, `web/`, `addon.py` | MCP tools, web UI (FastAPI + Jinja2 + HTMX), add-on runtime |

## Deutsch (Kurzfassung)

Trainingsplanung für Ausdauersport als Home-Assistant-Add-on: Daten aus intervals.icu, Pläne auf Basis von
Leistungstests und tatsächlichem Umfang, Planänderungen per Claude nur als Vorschlag mit Bestätigung und Undo.
Installation: Add-on-Store → ⋮ → Repositories → `https://github.com/quatscher/bulltraining` hinzufügen.

## License

[AGPL-3.0-or-later](LICENSE). Bundled libraries: see
[src/bulltraining/web/static/THIRD_PARTY.md](src/bulltraining/web/static/THIRD_PARTY.md).
