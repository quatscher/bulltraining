"""Kommandozeile: init, sync, publish, serve, mcp, plan, test, demo."""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta

from . import config
from .db import connect, now_iso


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_init(args) -> None:
    connect()
    print(f"Datenbank bereit: {config.DB_PATH.resolve()}")


def cmd_sync(args) -> None:
    from .intervals_client import IntervalsClient
    from .sync import run_sync
    with IntervalsClient() as client:
        _print(run_sync(connect(), client, full=args.full))


def cmd_publish(args) -> None:
    from .intervals_client import IntervalsClient
    from .publisher import publish
    with IntervalsClient() as client:
        _print(publish(connect(), client, horizon_days=args.days))


def cmd_serve(args) -> None:
    import uvicorn
    uvicorn.run("bulltraining.web.app:app", host=args.host, port=args.port, reload=False)


def cmd_mcp(args) -> None:
    from .mcp_server import main
    main()


def cmd_state(args) -> None:
    from . import metrics, performance
    c = connect()
    _print({"form": metrics.form_state(c), "baseline": metrics.training_baseline(c),
            "performance": performance.performance_state(c)})


def cmd_test(args) -> None:
    from .performance import record_test
    inputs = dict(kv.split("=", 1) for kv in args.inputs)
    inputs = {k: (float(v) if ":" not in v else v) for k, v in inputs.items()}
    _print(record_test(connect(), date=args.date, protocol=args.protocol, inputs=inputs))


def cmd_next_week(args) -> None:
    from .plans import propose_next_week, apply_change
    c = connect()
    res = propose_next_week(c, actor="human")
    if res and args.apply:
        apply_change(c, res["change_id"])
        res["status"] = "applied"
    _print(res)


def cmd_demo(args) -> None:
    """Synthetische 16 Wochen Training + Wellness + Tests, um das System ohne intervals.icu zu erkunden."""
    from .performance import record_test
    from .plans import create_plan
    c = connect()
    if c.execute("SELECT count(*) AS n FROM activities").fetchone()["n"] and not args.force:
        sys.exit("DB enthält bereits Aktivitäten. Mit --force trotzdem Demo-Daten ergänzen.")
    rnd = random.Random(42)
    today = date.today()
    start = today - timedelta(weeks=16)
    ts = now_iso()
    # Wochenmuster: Di Lauf-Intervalle, Mi Rad, Do Schwimmen, Sa Rad lang, So Lauf lang, Mo/Fr Kraft/Pause
    pattern = {1: ("run", 50, 55), 2: ("ride", 60, 50), 3: ("swim", 45, 45), 5: ("ride", 150, 48), 6: ("run", 80, 52)}
    n = 0
    d = start
    while d < today:
        week_idx = (d - start).days // 7
        factor = 0.7 if week_idx % 4 == 3 else 1.0 + 0.03 * (week_idx % 4)
        wd = d.weekday()
        if wd in pattern and rnd.random() > 0.08:
            sport, minutes, lph = pattern[wd]
            minutes = int(minutes * factor * rnd.uniform(0.9, 1.1))
            load = round(minutes / 60 * lph * rnd.uniform(0.9, 1.1), 1)
            ext = f"demo{d.isoformat()}{sport}"
            zone_kind = "power" if sport == "ride" else "hr"
            secs = [int(minutes * 60 * p) for p in (0.25, 0.5, 0.12, 0.09, 0.04)]
            c.execute("INSERT OR IGNORE INTO activities(source, external_id, start_date, sport, name, duration_s, distance_m, "
                      "hr_avg, load, load_method, is_endurance, zone_times, raw, created_at, updated_at) "
                      "VALUES ('intervals',?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
                      (ext, f"{d.isoformat()}T07:00:00", sport, f"Demo {sport}", minutes * 60,
                       {"run": 11.5, "ride": 28, "swim": 2.4}[sport] * minutes / 60 * 1000, rnd.randint(130, 150), load,
                       "power" if sport == "ride" else "hr", json.dumps({"kind": zone_kind, "secs": secs}),
                       json.dumps({"id": ext, "icu_ftp": 245 if sport == "ride" else None}), ts, ts))
            n += 1
        if wd == 0 and rnd.random() > 0.3:
            c.execute("INSERT INTO activities(source, start_date, sport, name, duration_s, rpe, load, load_method, "
                      "is_endurance, created_at, updated_at) VALUES ('local',?, 'strength', 'Kraft Ganzkörper', 2700, 6, 270, 'srpe', 0, ?, ?)",
                      (f"{d.isoformat()}T18:00:00", ts, ts))
        c.execute("INSERT OR REPLACE INTO wellness(date, hrv, resting_hr, sleep_h, weight_kg) VALUES (?,?,?,?,?)",
                  (d.isoformat(), round(rnd.gauss(62, 5), 1), rnd.randint(46, 52), round(rnd.uniform(6.3, 8.2), 1), 74.5))
        d += timedelta(days=1)
    record_test(c, date=(today - timedelta(weeks=12)).isoformat(), protocol="ride_ftp20", inputs={"avg_power_20min_w": 255})
    record_test(c, date=(today - timedelta(weeks=3)).isoformat(), protocol="ride_ftp20", inputs={"avg_power_20min_w": 266})
    record_test(c, date=(today - timedelta(weeks=10)).isoformat(), protocol="run_30min_tt",
                inputs={"distance_m": 6450, "avg_hr_last_20min": 171})
    # Schwimmen bewusst ohne Test: zeigt, wie der Plan zuerst einen Test ansetzt.
    res = create_plan(c, name="Demo 70.3", goal_type="event", goal_kind="triathlon_70.3",
                      goal_date=(today + timedelta(weeks=20)).isoformat(), sports=["swim", "ride", "run", "strength"],
                      weekly_hours=9, available_days={"swim": ["tue", "thu", "sat"], "ride": ["wed", "sat", "sun"],
                                                      "run": ["tue", "thu", "fri", "sun"], "strength": ["mon", "thu"]})
    print(f"Demo: {n} Ausdauereinheiten, 3 Tests, Plan '{res['plan']['name']}' angelegt.")
    for note in res["readiness"]["notes"]:
        print(" -", note)


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="bulltraining")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="Datenbank anlegen").set_defaults(func=cmd_init)
    s = sub.add_parser("sync", help="intervals.icu -> lokale DB")
    s.add_argument("--full", action="store_true", help="365 Tage statt inkrementell")
    s.set_defaults(func=cmd_sync)
    s = sub.add_parser("publish", help="bestätigte Einheiten nach intervals.icu schreiben")
    s.add_argument("--days", type=int, default=14)
    s.set_defaults(func=cmd_publish)
    s = sub.add_parser("serve", help="Frontend starten")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)
    sub.add_parser("mcp", help="MCP-Server über stdio").set_defaults(func=cmd_mcp)
    sub.add_parser("state", help="Form, Trainingsumfang und Leistungszustand ausgeben").set_defaults(func=cmd_state)
    s = sub.add_parser("test", help="Leistungstest erfassen: test ride_ftp20 avg_power_20min_w=265")
    s.add_argument("protocol")
    s.add_argument("inputs", nargs="+")
    s.add_argument("--date", default=date.today().isoformat())
    s.set_defaults(func=cmd_test)
    s = sub.add_parser("next-week", help="nächste leere Planwoche als Vorschlag erzeugen")
    s.add_argument("--apply", action="store_true", help="sofort bestätigen")
    s.set_defaults(func=cmd_next_week)
    s = sub.add_parser("demo", help="Demo-Daten erzeugen")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_demo)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
