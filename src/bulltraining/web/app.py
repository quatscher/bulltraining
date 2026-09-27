"""Frontend: FastAPI + Jinja2 + HTMX, serverseitig gerendert, kein Build-Schritt."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import activities, duplicates, metrics, performance, plans
from ..db import all_settings, connect, set_setting
from ..intervals_client import IntervalsClient, IntervalsError
from ..periodization import available_templates, week_context, week_targets, weekly_series
from ..util import SPORTS, WEEKDAYS, fmt_pace, iso_week, monday_of, parse_week, week_days

app = FastAPI(title="bulltraining")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["pace"] = lambda v, unit="/km": fmt_pace(v, unit) or "–"
templates.env.filters["minutes"] = lambda s: f"{int(s or 0) // 60} min"
templates.env.filters["hm"] = lambda s: f"{int(s or 0) // 3600}:{int(s or 0) % 3600 // 60:02d} h"
templates.env.filters["tojson_safe"] = lambda v: json.dumps(v, ensure_ascii=False)
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
        "inbox_count": pending + dupes, "sport_color": SPORT_COLOR, "weekday_de": WEEKDAY_DE, "today": date.today(),
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
            "SELECT id, start_date, sport, name, duration_s, load, load_method, rpe, excluded, possible_duplicate_of "
            "FROM activities WHERE start_date >= ? AND start_date < ? ORDER BY start_date",
            (m.isoformat(), (m + timedelta(days=7)).isoformat()))]
        days = []
        for d in week_days(m):
            days.append({"date": d, "sessions": [s for s in sessions if s["date"] == d.isoformat()],
                         "activities": [a for a in acts if a["start_date"][:10] == d.isoformat()]})
        info = None
        if plan:
            ctx = week_context(plan, m)
            info = {"ctx": ctx, "targets": week_targets(c, plan, m) if m >= monday_of(date.today()) else None}
        weeks.append({"monday": m, "iso": iso_week(m), "days": days, "summary": metrics.week_summary(c, m),
                      "info": info})
    return render(request, "calendar.html", weeks=weeks, plan=plan,
                  prev=iso_week(monday - timedelta(weeks=1)), next=iso_week(monday + timedelta(weeks=1)))


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
                  wellness=metrics.wellness_trend(c, 14), zones=metrics.zone_distribution(c, 28), days=days)


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
    return render(request, "inbox.html", pending=plans.list_changes(c, "pending"),
                  log=[x for x in plans.list_changes(c, limit=30) if x["status"] != "pending"],
                  pairs=duplicates.open_pairs(c), runs=runs, publish_errors=errors, unpublished=unpublished)


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
