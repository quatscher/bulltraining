"""Frontend: FastAPI + Jinja2 + HTMX, serverseitig gerendert, kein Build-Schritt."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .. import activities, duplicates, metrics, performance, plans
from ..db import all_settings, connect, set_setting
from ..intervals_client import IntervalsClient, IntervalsError
from ..periodization import available_templates, week_context, week_targets, weekly_series
from ..util import SPORTS, WEEKDAYS, fmt_pace, iso_week, monday_of, parse_week, week_days
from ..zonemodel import ZONE_COLORS, ZONES, activity_zone_secs, reference_paces, session_zone_secs
from ..workout_view import Thresholds, workout_steps

app = FastAPI(title="bulltraining")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["pace"] = lambda v, unit="/km": fmt_pace(v, unit) or "–"
templates.env.filters["minutes"] = lambda s: f"{int(s or 0) // 60} min"
templates.env.filters["hm"] = lambda s: f"{int(s or 0) // 3600}:{int(s or 0) % 3600 // 60:02d} h"


def _script_json(value: Any) -> Markup:
    """JSON für <script>-Blöcke: nicht HTML-escapen (sonst &#34; statt "), aber <, >, & und ' maskieren."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("'", "\\u0027")):
        text = text.replace(ch, esc)
    return Markup(text)


templates.env.filters["tojson_safe"] = _script_json
templates.env.filters["weekday_index"] = lambda s: date.fromisoformat(str(s)[:10]).weekday()
_conn = None

SPORT_COLOR = {"run": "#e0703a", "ride": "#3a7be0", "swim": "#2bb3a8", "strength": "#8a63d2", "other": "#888"}
WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def conn():
    global _conn
    if _conn is None:
        _conn = connect()
    return _conn


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    c = conn()
    pending = c.execute("SELECT count(*) AS n FROM plan_changes WHERE status = 'pending'").fetchone()["n"]
    dupes = c.execute("SELECT count(*) AS n FROM activities WHERE possible_duplicate_of IS NOT NULL").fetchone()["n"]
    return templates.TemplateResponse(request, name, {
        "inbox_count": pending + dupes, "sport_color": SPORT_COLOR, "zone_names": ZONES, "zone_colors": ZONE_COLORS, "weekday_de": WEEKDAY_DE, "today": date.today(),
        "flash": request.query_params.get("msg"), "error": request.query_params.get("err"), **ctx})


def back(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    from urllib.parse import urlencode
    params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return RedirectResponse(url + ("?" + urlencode(params) if params else ""), status_code=303)


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse("/calendar", status_code=303)


# --- Kalender --------------------------------------------------------------------

@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, week: str | None = None) -> HTMLResponse:
    c = conn()
    monday = parse_week(week) if week else monday_of(date.today())
    plan = plans.get_active_plan(c)
    weeks = []
    for k in range(2):
        m = monday + timedelta(weeks=k)
        sessions = plans.get_sessions(c, plan["id"] if plan else None, m, m + timedelta(days=6))
        acts = [dict(r) for r in c.execute(
            "SELECT id, start_date, sport, name, duration_s, load, load_method, rpe, excluded, possible_duplicate_of, "
            "is_endurance, zone_times "
            "FROM activities WHERE start_date >= ? AND start_date < ? ORDER BY start_date",
            (m.isoformat(), (m + timedelta(days=7)).isoformat()))]
        paces = reference_paces(c)
        for s in sessions:
            s["zones"] = session_zone_secs(s, paces) if s["sport"] in ("run", "ride", "swim") else None
        for a in acts:
            a["zones"] = activity_zone_secs(a.pop("zone_times")) if a["is_endurance"] else None
        timeline = metrics.zone_timeline(c, m, m + timedelta(days=6), plan["id"] if plan else None)
        days = []
        for d in week_days(m):
            days.append({"date": d, "sessions": [s for s in sessions if s["date"] == d.isoformat()],
                         "activities": [a for a in acts if a["start_date"][:10] == d.isoformat()]})
        info = None
        if plan:
            ctx = week_context(plan, m)
            info = {"ctx": ctx, "targets": week_targets(c, plan, m) if m >= monday_of(date.today()) else None}
        weeks.append({"monday": m, "iso": iso_week(m), "days": days, "summary": metrics.week_summary(c, m),
                      "info": info, "zone_days": timeline})
    return render(request, "calendar.html", weeks=weeks, plan=plan,
                  prev=iso_week(monday - timedelta(weeks=1)), next=iso_week(monday + timedelta(weeks=1)))


@app.get("/session/{session_id}", response_class=HTMLResponse)
def session_view(request: Request, session_id: int) -> HTMLResponse:
    c = conn()
    row = c.execute("SELECT * FROM plan_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return back("/calendar", err=f"Einheit {session_id} nicht gefunden")
    s = dict(row)
    endurance = s["sport"] in ("run", "ride", "swim")
    th = Thresholds(c)
    basis = []
    if s["sport"] == "ride" and th.ftp:
        basis.append(f"FTP {th.ftp:.0f} W")
    if s["sport"] in ("run", "swim") and th.pace.get(s["sport"]):
        label = "Schwellenpace" if s["sport"] == "run" else "CSS"
        basis.append(f"{label} {fmt_pace(th.pace[s['sport']], '/km' if s['sport'] == 'run' else '/100 m')}")
    if th.lthr.get(s["sport"]):
        basis.append(f"Schwellenpuls {th.lthr[s['sport']]:.0f}")
    elif th.max_hr and s["sport"] != "swim":
        basis.append(f"HFmax {th.max_hr:.0f} (Puls grob, kein Schwellenpuls getestet)")
    if endurance:
        t = performance.test_status(c, s["sport"])
        if t["last_test"]:
            basis.append(f"Test vom {t['last_test']}" + (" – veraltet" if t["status"] == "stale" else ""))
        else:
            basis.append("kein Test – Vorgaben nach Pulszone")
    return render(request, "session.html", s=s, steps=workout_steps(th, s["sport"], s["description"]),
                  zones=session_zone_secs(s, reference_paces(c)) if endurance else None, basis=basis,
                  week=iso_week(s["date"]), weekday=WEEKDAY_DE[date.fromisoformat(s["date"]).weekday()])


@app.post("/plan/generate")
def plan_generate() -> RedirectResponse:
    try:
        res = plans.propose_next_week(conn(), actor="human")
    except plans.PlanError as exc:
        return back("/inbox", err=str(exc))
    if res is None:
        return back("/calendar", msg="Keine leere Woche im Planungshorizont.")
    return back("/inbox", msg=f"Vorschlag {res['change_id']} erzeugt – bitte prüfen.")


# --- Form und Leistung ------------------------------------------------------------

@app.get("/form", response_class=HTMLResponse)
def form_view(request: Request, days: int = 180) -> HTMLResponse:
    c = conn()
    curves = metrics.form_curves(c).tail(days)
    series = {
        "dates": [d.strftime("%Y-%m-%d") for d in curves.index],
        **{k: [None if v is None or v != v else round(float(v), 1) for v in curves[k]]
           for k in ("ctl_endurance", "ctl_total", "atl_endurance", "form_endurance", "hrv", "ctl_icu")},
    }
    return render(request, "form.html", state=metrics.form_state(c), series=series,
                  wellness=metrics.wellness_trend(c, 14), zones=metrics.zone_distribution(c, 28), days=days,
                  zone_weeks=metrics.zone_weeks(c, weeks_back=12, weeks_ahead=4))


@app.get("/performance", response_class=HTMLResponse)
def performance_view(request: Request) -> HTMLResponse:
    c = conn()
    plan = plans.get_active_plan(c)
    next_monday = monday_of(date.today()) + timedelta(weeks=1)
    return render(request, "performance.html",
                  state=performance.performance_state(c), tests=performance.list_tests(c),
                  protocols=performance.protocols_overview(), baseline=metrics.training_baseline(c),
                  series=weekly_series(c, plan["id"] if plan else None),
                  next_targets=week_targets(c, plan, next_monday) if plan else None,
                  readiness=plans.plan_readiness(c, plan) if plan else None, plan=plan)


@app.post("/performance/test")
async def performance_test(request: Request) -> RedirectResponse:
    form = await request.form()
    protocol = str(form.get("protocol"))
    spec = performance.PROTOCOLS.get(protocol, {"inputs": {}})
    inputs = {k: str(form.get(f"{protocol}__{k}")).strip() for k in spec["inputs"] if form.get(f"{protocol}__{k}")}
    for k, v in list(inputs.items()):
        if ":" not in v:
            inputs[k] = float(v.replace(",", "."))
    try:
        res = performance.record_test(conn(), date=str(form.get("date")), protocol=protocol, inputs=inputs,
                                      notes=str(form.get("notes") or "") or None)
    except (performance.TestError, ValueError) as exc:
        return back("/performance", err=str(exc))
    parts = [f"{k}: {v['before'] or '–'} → {v['after']}" for k, v in res["changes"].items()]
    return back("/performance", msg="Test gespeichert. " + "; ".join(parts))


@app.post("/performance/test/{test_id}/delete")
def performance_test_delete(test_id: int) -> RedirectResponse:
    conn().execute("DELETE FROM performance_tests WHERE id = ?", (test_id,))
    return back("/performance", msg=f"Test {test_id} gelöscht. Schwellenwerte bleiben bis zum nächsten Test bestehen.")


# --- Posteingang ------------------------------------------------------------------

@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request) -> HTMLResponse:
    c = conn()
    runs = [dict(r) for r in c.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 5")]
    errors = [dict(r) for r in c.execute("SELECT id, date, title, publish_error FROM plan_sessions "
                                         "WHERE publish_error IS NOT NULL AND status = 'planned'")]
    unpublished = c.execute("SELECT count(*) AS n FROM plan_sessions ps JOIN plans p ON p.id = ps.plan_id AND p.status='active' "
                            "WHERE ps.status IN ('planned','deleted') AND ps.date >= ?", (date.today().isoformat(),)).fetchone()["n"]
    pending = plans.list_changes(c, "pending")
    th = Thresholds(c)
    paces = reference_paces(c)
    for ch in pending:
        ch["weeks"] = _proposal_weeks(c, ch, th, paces)
    return render(request, "inbox.html", pending=pending,
                  log=[x for x in plans.list_changes(c, limit=30) if x["status"] != "pending"],
                  pairs=duplicates.open_pairs(c), runs=runs, publish_errors=errors, unpublished=unpublished)


ENDURANCE = ("run", "ride", "swim")


def _change_note(b: dict[str, Any], a: dict[str, Any]) -> str:
    parts = []
    if a["date"] != b["date"]:
        parts.append(f"von {WEEKDAY_DE[date.fromisoformat(b['date']).weekday()]} {b['date'][8:10]}.{b['date'][5:7]}.")
    if a["duration_s"] != b["duration_s"]:
        parts.append(f"{b['duration_s'] // 60} → {a['duration_s'] // 60} min")
    if a.get("intensity") != b.get("intensity"):
        parts.append(f"{b.get('intensity')} → {a.get('intensity')}")
    return ", ".join(parts) or "Beschreibung geändert"


def _proposal_weeks(c: Any, ch: dict[str, Any], th: Thresholds, paces: dict[str, Any]) -> list[dict[str, Any]]:
    """Wochenraster eines offenen Vorschlags: aktueller Plan mit markierten Änderungen (neu/geändert/entfällt)."""
    diff = ch["diff"]
    before = {b["id"]: b for b in diff["before"]}
    dates = [date.fromisoformat(x["date"]) for x in diff["before"] + diff["after"] if x.get("date")]
    plan = plans.get_plan_by_id(c, ch["plan_id"]) if ch.get("plan_id") else None
    weeks = []
    n = 0
    for m in sorted({monday_of(d) for d in dates}):
        sunday = m + timedelta(days=6)
        entries: list[dict[str, Any]] = []
        after_by_id = {a["id"]: a for a in diff["after"]}
        current = plans.get_sessions(c, ch["plan_id"], m, sunday)
        for s in current:
            a = after_by_id.get(s["id"])
            if a is None:
                entries.append({"s": s, "mark": "same"})
            elif a.get("_deleted"):
                entries.append({"s": s, "mark": "removed", "note": "entfällt"})
            elif a["date"] != s["date"]:
                entries.append({"s": s, "mark": "removed",
                                "note": f"verschoben auf {WEEKDAY_DE[date.fromisoformat(a['date']).weekday()]}"})
        for a in diff["after"]:
            if a.get("_deleted") or not (m <= date.fromisoformat(a["date"]) <= sunday):
                continue
            b = before.get(a["id"])
            if b is None:
                entries.append({"s": a, "mark": "new", "note": "neu"})
            else:
                entries.append({"s": a, "mark": "changed", "note": _change_note(b, a)})
        for e in entries:
            s = e["s"]
            n += 1
            e["anchor"] = f"pc{ch['id']}-{n}"
            e["zones"] = session_zone_secs(s, paces) if s["sport"] in ENDURANCE else None
            e["steps"] = workout_steps(th, s["sport"], s.get("description")) if e["mark"] != "removed" else None
        def endu(sel):
            items = [e["s"] for e in entries if sel(e) and e["s"]["sport"] in ENDURANCE]
            return round(sum(x["duration_s"] for x in items) / 3600, 1), round(sum(x.get("target_load") or 0 for x in items))
        # vorher = aktueller Plan dieser Woche, nachher = mit angewendetem Vorschlag
        cur_endu = [s for s in current if s["sport"] in ENDURANCE]
        before_h = round(sum(s["duration_s"] for s in cur_endu) / 3600, 1)
        before_l = round(sum(s.get("target_load") or 0 for s in cur_endu))
        after_h, after_l = endu(lambda e: e["mark"] != "removed")
        days = []
        for d in week_days(m):
            day_entries = [e for e in entries if e["s"]["date"] == d.isoformat()]
            day_entries.sort(key=lambda e: (e["mark"] == "removed", e["s"]["sport"]))
            days.append({"date": d, "entries": day_entries})
        info = None
        if plan:
            ctx = week_context(plan, m)
            corridor = week_targets(c, plan, m)["load_corridor"] if m >= monday_of(date.today()) else None
            info = {"ctx": ctx, "corridor": corridor}
        weeks.append({"monday": m, "iso": iso_week(m), "days": days, "entries": entries, "info": info,
                      "before_h": before_h, "before_l": before_l, "after_h": after_h, "after_l": after_l})
    return weeks


@app.post("/changes/{change_id}/apply")
def change_apply(change_id: int) -> RedirectResponse:
    try:
        plans.apply_change(conn(), change_id)
    except plans.PlanError as exc:
        return back("/inbox", err=str(exc))
    return back("/inbox", msg=f"Änderung {change_id} angewendet.")


@app.post("/changes/{change_id}/reject")
def change_reject(change_id: int) -> RedirectResponse:
    try:
        plans.reject_change(conn(), change_id)
    except plans.PlanError as exc:
        return back("/inbox", err=str(exc))
    return back("/inbox", msg=f"Änderung {change_id} verworfen.")


@app.post("/changes/{change_id}/revert")
def change_revert(change_id: int) -> RedirectResponse:
    try:
        plans.revert_change(conn(), change_id)
    except plans.PlanError as exc:
        return back("/inbox", err=str(exc))
    return back("/inbox", msg=f"Änderung {change_id} rückgängig gemacht.")


@app.post("/duplicates/{local_id}/{action}")
def duplicate_resolve(local_id: int, action: str) -> RedirectResponse:
    try:
        duplicates.resolve(conn(), local_id, action)
    except ValueError as exc:
        return back("/inbox", err=str(exc))
    return back("/inbox", msg="Dublette aufgelöst.")


@app.post("/sync")
def sync_now() -> RedirectResponse:
    from ..sync import run_sync
    try:
        with IntervalsClient() as client:
            res = run_sync(conn(), client)
    except (IntervalsError, Exception) as exc:  # noqa: BLE001
        return back("/inbox", err=f"Sync fehlgeschlagen: {exc}")
    return back("/inbox", msg=f"Sync: {res['created']} neu, {res['updated']} aktualisiert, "
                              f"{res['duplicate_candidates']} Dublettenkandidaten.")


@app.post("/publish")
def publish_now() -> RedirectResponse:
    from ..publisher import publish
    try:
        with IntervalsClient() as client:
            res = publish(conn(), client)
    except IntervalsError as exc:
        return back("/inbox", err=str(exc))
    msg = f"Veröffentlicht: {res['created']} neu, {res['updated']} aktualisiert, {res['deleted']} gelöscht"
    return back("/inbox", msg=msg, err=f"{len(res['errors'])} Fehler, siehe unten" if res["errors"] else None)


# --- Aktivität --------------------------------------------------------------------

@app.get("/activity/{activity_id}", response_class=HTMLResponse)
def activity_view(request: Request, activity_id: int) -> HTMLResponse:
    act = activities.get_activity(conn(), activity_id)
    if not act:
        return back("/calendar", err=f"Aktivität {activity_id} nicht gefunden")
    return render(request, "activity.html", a=act, sports=SPORTS)


@app.post("/activity/{activity_id}")
async def activity_update(request: Request, activity_id: int) -> RedirectResponse:
    form = await request.form()
    fields: dict[str, Any] = {}
    for key in ("name", "notes", "sport", "start_date"):
        if key in form:
            fields[key] = str(form[key])
    if form.get("rpe"):
        fields["rpe"] = int(form["rpe"])
    if form.get("duration_min"):
        fields["duration_s"] = int(float(form["duration_min"]) * 60)
    fields["is_endurance"] = 1 if form.get("is_endurance") else 0
    fields["excluded"] = 1 if form.get("excluded") else 0
    act = activities.get_activity(conn(), activity_id)
    if act and act["source"] == "intervals":
        fields = {k: v for k, v in fields.items() if k in ("notes", "rpe", "is_endurance", "excluded")}
        fields = {k: v for k, v in fields.items() if act.get(k) != v}
    try:
        activities.update_activity(conn(), activity_id, **fields)
    except activities.ActivityError as exc:
        return back(f"/activity/{activity_id}", err=str(exc))
    return back(f"/activity/{activity_id}", msg="Gespeichert.")


@app.post("/activity")
async def activity_create(request: Request) -> RedirectResponse:
    form = await request.form()
    try:
        act = activities.log_activity(conn(), date=str(form["date"]), sport=str(form["sport"]),
                                      duration_min=float(form["duration_min"]),
                                      rpe=int(form["rpe"]) if form.get("rpe") else None,
                                      name=str(form.get("name") or "") or None, notes=str(form.get("notes") or "") or None)
    except (activities.ActivityError, ValueError, KeyError) as exc:
        return back("/calendar", err=str(exc))
    return back(f"/activity/{act['id']}", msg="Aktivität erfasst.")


# --- Pläne und Einstellungen --------------------------------------------------------

@app.get("/plans", response_class=HTMLResponse)
def plans_view(request: Request) -> HTMLResponse:
    c = conn()
    plan = plans.get_active_plan(c)
    return render(request, "plans.html", plans=plans.list_plans(c), active=plan,
                  readiness=plans.plan_readiness(c, plan) if plan else None,
                  templates_list=available_templates(), sports=SPORTS, weekdays=WEEKDAYS)


@app.post("/plans")
async def plans_create(request: Request) -> RedirectResponse:
    form = await request.form()
    sports = form.getlist("sports")
    avail: dict[str, dict[str, int | None]] = {}
    for s in sports:
        for d in WEEKDAYS:
            if form.get(f"avail__{s}__{d}"):
                cap = form.get(f"cap__{s}__{d}")
                avail.setdefault(s, {})[d] = int(cap) if cap else None
    try:
        res = plans.create_plan(
            conn(), name=str(form["name"]), goal_type=str(form["goal_type"]), sports=list(sports),
            weekly_hours=float(str(form["weekly_hours"]).replace(",", ".")),
            goal_date=str(form.get("goal_date") or "") or None, goal_kind=str(form.get("goal_kind") or "") or None,
            focus=str(form.get("focus") or "") or None, priority=str(form.get("priority") or "") or None,
            available_days=avail or None, start_date=str(form.get("start_date") or "") or None)
    except (plans.PlanError, ValueError, KeyError) as exc:
        return back("/plans", err=str(exc))
    return back("/plans", msg=f"Plan '{res['plan']['name']}' angelegt. " + " ".join(res["readiness"]["notes"]))


@app.post("/plans/{plan_id}/archive")
def plans_archive(plan_id: int) -> RedirectResponse:
    plans.archive_plan(conn(), plan_id)
    return back("/plans", msg="Plan archiviert.")


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request) -> HTMLResponse:
    return render(request, "settings.html", settings=all_settings(conn()))


@app.post("/settings")
async def settings_save(request: Request) -> RedirectResponse:
    form = await request.form()
    for k, v in form.items():
        set_setting(conn(), k, str(v).strip())
    return back("/settings", msg="Einstellungen gespeichert.")
