"""MCP-Server (stdio). Grob geschnittene Werkzeuge; das LLM sieht keine Rohtabellen und kein SQL."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from mcp.server import MCPServer

from . import activities, metrics, performance, plans
from .db import connect
from .periodization import week_targets
from .util import monday_of, parse_week

INSTRUCTIONS = """Trainingssystem eines Ausdauersportlers. Regeln:
- Leistungszustand kommt aus Leistungstests (get_performance_state). Fehlt ein gültiger Test, zuerst einen Test
  vorschlagen (schedule_test), bevor harte Einheiten geplant werden.
- Planänderungen nur über propose_plan_change mit typisierten Operationen und Begründung. Nichts wird ohne
  Bestätigung im Frontend angewendet. Lehnt das Werkzeug ab, Fehler lesen und korrigieren.
- Wochenlast bleibt im Korridor aus aktuellem Trainingsumfang (get_training_load_context) – nicht über-, nicht unterfordern.
- log_activity: fehlt die RPE, beim Athleten nachfragen statt schätzen. Eingefügte Zeile zur Kontrolle zeigen.
- Kontext sparsam: Formzustand, 4 Wochenzusammenfassungen, Zonen 28 Tage, Wellness 14 Tage, Plan 2 Wochen."""

mcp = MCPServer(name="bulltraining", instructions=INSTRUCTIONS)
_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = connect()
    return _conn


def _err(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc), "errors": getattr(exc, "errors", [str(exc)])}


@mcp.tool()
def get_form_state() -> dict[str, Any]:
    """Beide CTL-Kurven (Ausdauer/Gesamt), ATL, Form, HRV-Abweichung, aktiver Plan mit Ziel und Tagen bis zum Ziel,
    dazu Kurzfassung des Leistungszustands (Testwerte und deren Gültigkeit)."""
    c = conn()
    state = metrics.form_state(c)
    plan = plans.get_active_plan(c)
    if plan:
        state["plan"] = {k: plan[k] for k in ("id", "name", "goal_type", "goal_kind", "goal_date", "focus", "weekly_hours")}
        if plan.get("goal_date"):
            state["plan"]["days_to_goal"] = (date.fromisoformat(plan["goal_date"]) - date.today()).days
    perf = performance.performance_state(c)
    state["performance"] = {k: {"value": v["display"], "tested_on": v["tested_on"], "change_pct": v["change_pct"]}
                            for k, v in perf["metrics"].items()}
    state["tests"] = {s: t["status"] for s, t in perf["tests"].items()}
    return state


@mcp.tool()
def get_performance_state() -> dict[str, Any]:
    """Aktueller Leistungszustand aus Leistungstests: FTP, Schwellenpace, CSS, Schwellenpuls mit Verlauf und
    Veränderung, Gültigkeit je Sportart (valid|due_soon|stale|missing), Zonen, Abgleich mit intervals.icu,
    verfügbare Testprotokolle."""
    c = conn()
    state = performance.performance_state(c)
    for m in state["metrics"].values():
        m["history"] = m["history"][-6:]
    state["protocols"] = performance.protocols_overview()
    return state


@mcp.tool()
def record_performance_test(date: str, protocol: str, inputs: dict[str, Any], activity_id: int | None = None,
                            notes: str | None = None) -> dict[str, Any]:
    """Ergebnis eines Leistungstests erfassen. protocol: ride_ftp20 | ride_ramp | run_30min_tt | run_5k_tt | swim_css.
    inputs je Protokoll, z. B. {"avg_power_20min_w": 265} oder {"t400": "6:40", "t200": "3:10"}.
    Übernimmt die neuen Schwellenwerte und gibt vorher/nachher zurück. Messwerte nie schätzen – nachfragen."""
    try:
        return performance.record_test(conn(), date=date, protocol=protocol, inputs=inputs,
                                       activity_id=activity_id, notes=notes)
    except (performance.TestError, ValueError) as exc:
        return _err(exc)


@mcp.tool()
def get_training_load_context(week: str | None = None) -> dict[str, Any]:
    """Aktueller Trainingsumfang (Ø Stunden/Last der letzten 4 Wochen, je Sportart, längste Einheiten,
    Acute:Chronic-Verhältnis) und für die angegebene Woche (ISO '2026-W41' oder Datum, Standard: nächste Woche)
    Wochentyp, Ziel-Stunden und zulässiger Lastkorridor. Grundlage, um weder zu über- noch zu unterfordern."""
    c = conn()
    baseline = metrics.training_baseline(c)
    baseline.pop("weekly", None)
    out: dict[str, Any] = {"baseline": baseline}
    plan = plans.get_active_plan(c)
    if plan:
        monday = parse_week(week) if week else monday_of(date.today()) + timedelta(weeks=1)
        out["week_targets"] = week_targets(c, plan, monday)
    return out


@mcp.tool()
def get_week_summary(week: str | None = None) -> dict[str, Any]:
    """Dauer und Last je Sportart, geplant gegen absolviert. week: ISO-Woche '2026-W40' oder Datum; leer = aktuelle."""
    return metrics.week_summary(conn(), week)


@mcp.tool()
def get_zone_distribution(days: int = 28) -> dict[str, Any]:
    """Zeit je Intensitätszone über die letzten `days` Tage, je Sportart, plus Drei-Zonen-Verteilung."""
    return metrics.zone_distribution(conn(), days)


@mcp.tool()
def get_wellness_trend(days: int = 14) -> dict[str, Any]:
    """HRV, Ruhepuls, Schlaf als Reihe plus 60-Tage-Baseline und HRV-Status."""
    return metrics.wellness_trend(conn(), days)


@mcp.tool()
def get_plan(date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
    """Geplante Einheiten des aktiven Plans mit Status, Phase und Wochentyp. Standard: zwei Wochen ab Montag."""
    return plans.get_plan_view(conn(), date_from, date_to)


@mcp.tool()
def get_activity(activity_id: int) -> dict[str, Any]:
    """Eine Aktivität im Detail, inklusive Intervallen, falls vorhanden."""
    act = activities.get_activity(conn(), activity_id)
    return act or {"ok": False, "error": f"Aktivität {activity_id} nicht gefunden"}


@mcp.tool()
def log_activity(date: str, sport: str, duration_min: float, rpe: int | None = None, hr_avg: int | None = None,
                 name: str | None = None, notes: str | None = None, time: str = "12:00") -> dict[str, Any]:
    """Lokale Aktivität erfassen (v. a. Krafttraining). sport: run|ride|swim|strength|other. RPE 1–10 ist Pflicht:
    fehlt sie, NICHT schätzen, sondern nachfragen. Gibt die eingefügte Zeile zur Kontrolle zurück."""
    try:
        row = activities.log_activity(conn(), date=date, sport=sport, duration_min=duration_min, rpe=rpe,
                                      hr_avg=hr_avg, name=name, notes=notes, time=time)
        return {"ok": True, "inserted": row}
    except activities.ActivityError as exc:
        return _err(exc)


@mcp.tool()
def update_activity(activity_id: int, rpe: int | None = None, notes: str | None = None,
                    duration_min: float | None = None, name: str | None = None,
                    is_endurance: bool | None = None) -> dict[str, Any]:
    """Aktivität korrigieren. Bei gespiegelten Aktivitäten nur rpe, notes, is_endurance."""
    try:
        row = activities.update_activity(
            conn(), activity_id, rpe=rpe, notes=notes, name=name,
            duration_s=int(duration_min * 60) if duration_min else None,
            is_endurance=int(is_endurance) if is_endurance is not None else None)
        return {"ok": True, "updated": row}
    except activities.ActivityError as exc:
        return _err(exc)


@mcp.tool()
def propose_plan_change(ops: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    """Planänderung vorschlagen (wird erst nach Bestätigung im Frontend angewendet). ops ist eine Liste aus:
    {"op":"move_session","session_id":1,"to_date":"2026-10-08"}
    {"op":"swap_days","date_a":"2026-10-06","date_b":"2026-10-08"}
    {"op":"change_duration","session_id":1,"duration_min":45}
    {"op":"change_intensity","session_id":1,"intensity":"easy"}   (recovery|easy|long|tempo|threshold|vo2)
    {"op":"insert_recovery_day","date":"2026-10-07"}
    {"op":"delete_session","session_id":1}
    {"op":"regenerate_week","week":"2026-W41"}   – aus Vorlage, Leistungstests und aktuellem Umfang
    {"op":"regenerate_week","week":"2026-W41","sessions":[{"date":"2026-10-06","sport":"run","intensity":"easy","duration_min":50}]}
    {"op":"schedule_test","date":"2026-10-07","sport":"ride","protocol":"ride_ftp20"}
    {"op":"add_race","date":"2026-10-18","sport":"run","name":"10k Stadtlauf","priority":"C","duration_min":45}
    Regeln (Lastkorridor, harte Tage, Ruhetag, Taper, Vergangenheit) werden geprüft; bei Verstoß kommt ein Fehler
    mit Begründung zurück. reason: nachvollziehbare Begründung mit Bezug auf Kennzahlen."""
    try:
        return {"ok": True, **plans.propose_plan_change(conn(), ops, reason, actor="llm")}
    except plans.PlanError as exc:
        return _err(exc)


@mcp.tool()
def get_pending_changes() -> list[dict[str, Any]]:
    """Offene, noch nicht bestätigte Vorschläge (nur lesend; bestätigt wird im Frontend)."""
    return [{"id": c["id"], "created_at": c["created_at"], "reason": c["reason"], "summary": c["summary"],
             "warnings": c["warnings"]} for c in plans.list_changes(conn(), "pending")]


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
