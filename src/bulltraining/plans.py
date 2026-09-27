"""Pläne und Planänderungen: Vorschlag -> Bestätigung -> Anwendung, mit Protokoll und Undo."""
from __future__ import annotations

import copy
import json
import math
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import generator, rules, workouts
from .db import get_float, now_iso, row_to_dict, transaction
from .metrics import training_baseline
from .performance import PROTOCOLS, DEFAULT_PROTOCOL, test_status
from .periodization import available_templates, load_template, week_context, week_targets
from .util import ENDURANCE_SPORTS, SPORTS, WEEKDAYS, monday_of, parse_week, to_date

SESSION_FIELDS = ("id", "plan_id", "date", "sport", "category", "race_priority", "test_protocol", "title",
                  "description", "duration_s", "target_load", "intensity", "status", "external_event_id")
OPS = ("move_session", "swap_days", "change_duration", "change_intensity", "insert_recovery_day",
       "regenerate_week", "delete_session", "schedule_test", "add_race")
CHANGEABLE = ("planned", "published")


class PlanError(ValueError):
    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message if not errors else message + "\n- " + "\n- ".join(errors))
        self.errors = errors or [message]


# --- Pläne ------------------------------------------------------------------------

def get_active_plan(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return row_to_dict(conn.execute("SELECT * FROM plans WHERE status = 'active' ORDER BY id DESC LIMIT 1").fetchone(),
                       ("sports", "available_days", "baseline"))


def get_plan_by_id(conn: sqlite3.Connection, plan_id: int) -> dict[str, Any] | None:
    return row_to_dict(conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone(),
                       ("sports", "available_days", "baseline"))


def list_plans(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [row_to_dict(r, ("sports", "available_days")) for r in conn.execute("SELECT * FROM plans ORDER BY id DESC")]


def create_plan(conn: sqlite3.Connection, *, name: str, goal_type: str, sports: list[str],
                weekly_hours: float, goal_date: str | None = None, goal_kind: str | None = None,
                focus: str | None = None, priority: str | None = None,
                available_days: dict[str, Any] | None = None, start_date: str | None = None,
                today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    if goal_type not in ("event", "continuous"):
        raise PlanError("goal_type muss 'event' oder 'continuous' sein.")
    if goal_type == "event":
        if not goal_date:
            raise PlanError("Ein Event-Plan braucht ein Zieldatum.")
        if to_date(goal_date) <= today:
            raise PlanError("Das Zieldatum muss in der Zukunft liegen.")
    else:
        goal_date = None
    bad = [s for s in sports if s not in SPORTS]
    if not sports or bad:
        raise PlanError(f"Sportarten ungültig: {bad or 'leer'}. Erlaubt: {', '.join(SPORTS)}")
    if available_days:
        for key, entry in available_days.items():
            days = [key] if key in WEEKDAYS else (list(entry) if not isinstance(entry, str) else [entry])
            if any(d not in WEEKDAYS and d not in SPORTS for d in days):
                raise PlanError(f"available_days: unbekannte Tage in {key}: {days}")
    start = monday_of(start_date or today + timedelta(days=(7 - today.weekday()) % 7 or 7))
    baseline = training_baseline(conn, as_of=today)
    snapshot = {"training": {k: baseline[k] for k in ("avg_endurance_hours", "avg_endurance_load", "by_sport",
                                                     "ctl_endurance", "data_quality", "weeks")},
                "tests": {s: test_status(conn, s, today) for s in sports if s in ENDURANCE_SPORTS},
                "thresholds": {k: get_float(conn, k) for k in ("ftp_w", "threshold_pace_run_s_per_km", "css_s_per_100m", "lthr_run")}}
    with transaction(conn):
        conn.execute("UPDATE plans SET status = 'archived' WHERE status = 'active'")
        cur = conn.execute(
            "INSERT INTO plans(name, goal_type, goal_date, goal_kind, sports, focus, priority, weekly_hours, "
            "available_days, start_date, baseline, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'active', ?)",
            (name, goal_type, goal_date, goal_kind, json.dumps(sports), focus,
             (priority or "A") if goal_type == "event" else None, weekly_hours,
             json.dumps(available_days) if available_days else None, start.isoformat(),
             json.dumps(snapshot), now_iso()))
    plan = get_plan_by_id(conn, cur.lastrowid)
    return {"plan": plan, "readiness": plan_readiness(conn, plan, today)}


def archive_plan(conn: sqlite3.Connection, plan_id: int) -> None:
    conn.execute("UPDATE plans SET status = 'archived' WHERE id = ?", (plan_id,))


def plan_readiness(conn: sqlite3.Connection, plan: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Ist der Plan startklar? Tests vorhanden, Umfangsdaten vorhanden, Zielumfang realistisch."""
    today = today or date.today()
    baseline = training_baseline(conn, as_of=today)
    sports = [s for s in (plan["sports"] if isinstance(plan["sports"], list) else json.loads(plan["sports"]))
              if s in ENDURANCE_SPORTS]
    tests = {s: test_status(conn, s, today) for s in sports}
    notes = []
    missing = [s for s, t in tests.items() if t["status"] in ("missing", "stale")]
    if missing:
        notes.append("Erste Planwoche enthält Leistungstests für: " + ", ".join(
            f"{s} ({PROTOCOLS[tests[s]['protocol'] or DEFAULT_PROTOCOL[s]]['name']})" for s in missing)
            + ". Harte Einheiten dieser Sportarten folgen erst nach dem Test.")
    if baseline["data_quality"] != "ok":
        notes.append("Wenig Trainingsdaten in den letzten Wochen – Start konservativ. Vorher synchronisieren.")
    current = baseline["avg_endurance_hours"]
    target = float(plan.get("weekly_hours") or 0)
    inc = get_float(conn, "max_weekly_load_increase_pct", 10) / 100
    ramp_weeks = None
    if current > 0 and target > current:
        # 3 Aufbauwochen je Block, Entlastung zählt nicht zum Aufbau
        load_weeks = math.ceil(math.log(target / current) / math.log(1 + inc))
        ramp_weeks = load_weeks + load_weeks // 3
        notes.append(f"Aktuell {current:.1f} h/Woche, Ziel {target:g} h: etwa {ramp_weeks} Wochen bis zum Zielumfang.")
        if plan["goal_type"] == "event" and plan.get("goal_date"):
            weeks_left = (monday_of(plan["goal_date"]) - monday_of(today)).days // 7
            if ramp_weeks > weeks_left * 0.7:
                notes.append("Zielumfang ist vor dem Wettkampf kaum sicher erreichbar – Wochenstunden realistischer ansetzen.")
    elif current > target * 1.15 and target > 0:
        notes.append(f"Aktuell {current:.1f} h/Woche, Ziel nur {target:g} h – Umfang wird schrittweise (−10 %/Woche) gesenkt.")
    tpl = load_template(plan.get("goal_kind"))
    if target and target < tpl.get("min_weekly_hours", 0):
        notes.append(f"Vorlage '{tpl['label']}' empfiehlt mindestens {tpl['min_weekly_hours']} h/Woche.")
    return {"tests": tests, "needs_test": missing, "current_hours": current, "target_hours": target,
            "ramp_weeks": ramp_weeks, "baseline_quality": baseline["data_quality"], "notes": notes,
            "template": tpl["goal_kind"]}


def get_sessions(conn: sqlite3.Connection, plan_id: int | None, date_from: str | date | None = None,
                 date_to: str | date | None = None, include_deleted: bool = False) -> list[dict[str, Any]]:
    if plan_id is None:
        return []
    sql = "SELECT * FROM plan_sessions WHERE plan_id = ?"
    params: list[Any] = [plan_id]
    if date_from:
        sql += " AND date >= ?"
        params.append(to_date(date_from).isoformat())
    if date_to:
        sql += " AND date <= ?"
        params.append(to_date(date_to).isoformat())
    if not include_deleted:
        sql += " AND status != 'deleted'"
    return [dict(r) for r in conn.execute(sql + " ORDER BY date, id", params)]


def get_plan_view(conn: sqlite3.Connection, date_from: str | None = None, date_to: str | None = None,
                  today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    plan = get_active_plan(conn)
    if not plan:
        return {"plan": None, "sessions": [], "hint": "Kein aktiver Plan. Aufzeichnung und Kennzahlen laufen trotzdem."}
    f = to_date(date_from) if date_from else monday_of(today)
    t = to_date(date_to) if date_to else f + timedelta(days=13)
    goal_days = (to_date(plan["goal_date"]) - today).days if plan.get("goal_date") else None
    weeks = []
    m = monday_of(f)
    while m <= t:
        ctx = week_context(plan, m)
        weeks.append({"week_start": m.isoformat(), "phase": ctx["phase"], "week_type": ctx["week_type"]})
        m += timedelta(weeks=1)
    return {"plan": {k: plan[k] for k in ("id", "name", "goal_type", "goal_date", "goal_kind", "sports", "focus",
                                           "priority", "weekly_hours", "start_date")},
            "days_to_goal": goal_days, "weeks": weeks,
            "sessions": [{k: s[k] for k in SESSION_FIELDS if k != "plan_id"} for s in get_sessions(conn, plan["id"], f, t)]}


# --- Änderungen: Simulation ---------------------------------------------------------

def _snap(s: dict[str, Any]) -> dict[str, Any]:
    return {k: s.get(k) for k in SESSION_FIELDS}


class _Sim:
    def __init__(self, conn: sqlite3.Connection, plan: dict[str, Any], today: date):
        self.conn, self.plan, self.today = conn, plan, today
        self.state: dict[int, dict[str, Any]] = {s["id"]: _snap(s) for s in get_sessions(conn, plan["id"])}
        self.original = copy.deepcopy(self.state)
        self.touched: list[tuple[str, dict | None, dict | None]] = []
        self.mondays: set[date] = set()
        self.warnings: list[str] = []
        self._next_tmp = -1

    def session(self, sid: Any) -> dict[str, Any]:
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            raise PlanError(f"session_id '{sid}' ist keine Zahl.")
        s = self.state.get(sid)
        if s is None or s["status"] == "deleted":
            raise PlanError(f"Einheit {sid} existiert im aktiven Plan nicht.")
        if s["status"] not in CHANGEABLE:
            raise PlanError(f"Einheit {sid} hat Status '{s['status']}' und ist nicht mehr änderbar.")
        return s

    def _mark(self, op: str, before: dict | None, after: dict | None) -> None:
        self.touched.append((op, copy.deepcopy(before) if before else None, copy.deepcopy(after) if after else None))
        for s in (before, after):
            if s:
                self.mondays.add(monday_of(s["date"]))

    def update(self, op: str, s: dict[str, Any], **changes: Any) -> None:
        before = copy.deepcopy(s)
        s.update(changes)
        self._mark(op, before, s)

    def delete(self, op: str, s: dict[str, Any]) -> None:
        before = copy.deepcopy(s)
        s["status"] = "deleted"
        self._mark(op, before, None)

    def add(self, op: str, s: dict[str, Any]) -> None:
        s = {**{k: None for k in SESSION_FIELDS}, **s, "id": self._next_tmp, "plan_id": self.plan["id"],
             "status": "planned"}
        s.setdefault("category", "WORKOUT")
        s["category"] = s["category"] or "WORKOUT"
        self.state[self._next_tmp] = s
        self._next_tmp -= 1
        self._mark(op, None, s)

    def rebuild(self, s: dict[str, Any], intensity: str, minutes: int) -> dict[str, Any]:
        if s.get("category") in ("RACE", "TEST"):
            factor = minutes * 60 / s["duration_s"] if s["duration_s"] else 1
            return {"duration_s": minutes * 60, "intensity": intensity,
                    "target_load": round((s.get("target_load") or 0) * factor, 1)}
        built = workouts.build(self.conn, s["sport"], intensity, minutes)
        return {k: built[k] for k in ("title", "description", "duration_s", "target_load", "intensity")}

    def active_list(self) -> list[dict[str, Any]]:
        return [s for s in self.state.values() if s["status"] != "deleted"]


def _parse_date(value: Any, field: str) -> date:
    try:
        return to_date(value)
    except (TypeError, ValueError):
        raise PlanError(f"{field}: '{value}' ist kein Datum (YYYY-MM-DD).")


def _apply_op(sim: _Sim, op: dict[str, Any]) -> None:
    name = op.get("op")
    if name not in OPS:
        raise PlanError(f"Unbekannte Operation '{name}'. Erlaubt: {', '.join(OPS)}")
    if name == "move_session":
        s = sim.session(op.get("session_id"))
        sim.update(name, s, date=_parse_date(op.get("to_date"), "to_date").isoformat())
    elif name == "swap_days":
        a, b = _parse_date(op.get("date_a"), "date_a").isoformat(), _parse_date(op.get("date_b"), "date_b").isoformat()
        on_a = [s for s in sim.active_list() if s["date"] == a]
        on_b = [s for s in sim.active_list() if s["date"] == b]
        for s in on_a + on_b:
            if s["status"] not in CHANGEABLE:
                raise PlanError(f"Einheit {s['id']} am {s['date']} ist '{s['status']}' – Tausch nicht möglich.")
        for s in on_a:
            sim.update(name, s, date=b)
        for s in on_b:
            sim.update(name, s, date=a)
    elif name == "change_duration":
        s = sim.session(op.get("session_id"))
        minutes = int(op.get("duration_min") or 0)
        if not 10 <= minutes <= 12 * 60:
            raise PlanError("duration_min muss zwischen 10 und 720 liegen.")
        sim.update(name, s, **sim.rebuild(s, s["intensity"] or "easy", minutes))
    elif name == "change_intensity":
        s = sim.session(op.get("session_id"))
        intensity = op.get("intensity")
        if intensity not in workouts.INTENSITY_ORDER or intensity in ("test", "race"):
            raise PlanError("intensity muss recovery|easy|long|tempo|threshold|vo2 sein.")
        if s.get("category") != "WORKOUT":
            raise PlanError("Intensität von Tests und Wettkämpfen ist nicht änderbar.")
        sim.update(name, s, **sim.rebuild(s, intensity, s["duration_s"] // 60))
    elif name == "insert_recovery_day":
        d = _parse_date(op.get("date"), "date").isoformat()
        victims = [s for s in sim.active_list() if s["date"] == d and s.get("category") != "RACE"]
        if not victims:
            sim.warnings.append(f"insert_recovery_day: am {d} war nichts geplant.")
        for s in victims:
            if s["status"] not in CHANGEABLE:
                raise PlanError(f"Einheit {s['id']} am {d} ist '{s['status']}'.")
            sim.delete(name, s)
    elif name == "delete_session":
        sim.delete(name, sim.session(op.get("session_id")))
    elif name == "schedule_test":
        sport = op.get("sport")
        protocol = op.get("protocol") or DEFAULT_PROTOCOL.get(sport or "")
        if protocol not in PROTOCOLS or PROTOCOLS[protocol]["sport"] != sport:
            raise PlanError(f"Kein passendes Testprotokoll für {sport}. Verfügbar: "
                            + ", ".join(k for k, v in PROTOCOLS.items() if v["sport"] == sport))
        d = _parse_date(op.get("date"), "date")
        sim.add(name, {"date": d.isoformat(), "sport": sport, **workouts.build(sim.conn, sport, "test", 0, protocol)})
    elif name == "add_race":
        priority = op.get("priority", "B")
        if priority not in ("B", "C"):
            raise PlanError("Nebenwettkämpfe haben Priorität B oder C; das A-Ziel ist der Plan selbst.")
        sport = op.get("sport")
        if sport not in ENDURANCE_SPORTS:
            raise PlanError("add_race: sport muss run, ride oder swim sein.")
        minutes = int(op.get("duration_min") or 60)
        sim.add(name, {"date": _parse_date(op.get("date"), "date").isoformat(), "sport": sport, "category": "RACE",
                       "race_priority": priority, "title": op.get("name") or f"Wettkampf ({priority})",
                       "description": "Nebenwettkampf", "duration_s": minutes * 60,
                       "target_load": workouts.estimate_load(minutes, "race"), "intensity": "race"})
    elif name == "regenerate_week":
        monday = parse_week(op.get("week"))
        sunday = monday + timedelta(days=6)
        custom = op.get("sessions")
        tool_name = "regenerate_week" if custom else "regenerate_week_template"
        for s in list(sim.active_list()):
            d = to_date(s["date"])
            if monday <= d <= sunday and d >= sim.today and s["status"] in CHANGEABLE and s.get("category") != "RACE":
                sim.delete(tool_name, s)
        if custom:
            for i, c in enumerate(custom):
                sport, intensity = c.get("sport"), c.get("intensity", "easy")
                if sport not in SPORTS:
                    raise PlanError(f"sessions[{i}]: unbekannte Sportart '{sport}'.")
                if intensity not in workouts.INTENSITY_ORDER or intensity == "race":
                    raise PlanError(f"sessions[{i}]: intensity ungültig.")
                d = _parse_date(c.get("date"), f"sessions[{i}].date")
                if not monday <= d <= sunday:
                    raise PlanError(f"sessions[{i}]: {d} liegt nicht in der Woche ab {monday}.")
                built = workouts.build(sim.conn, sport, intensity, int(c.get("duration_min") or 45),
                                       c.get("protocol") or (DEFAULT_PROTOCOL.get(sport) if intensity == "test" else None))
                if c.get("title"):
                    built["title"] = c["title"]
                if c.get("description"):
                    built["description"] = c["description"]
                sim.add(tool_name, {"date": d.isoformat(), "sport": sport, **built})
        else:
            gen = generator.generate_week(sim.conn, sim.plan, monday, sim.active_list(), sim.today)
            for s in gen["sessions"]:
                sim.add(tool_name, s)
            sim.warnings += gen["warnings"]
        sim.mondays.add(monday)


def _diff(sim: _Sim) -> dict[str, list[dict[str, Any]]]:
    ids = []
    for _, before, after in sim.touched:
        for s in (before, after):
            if s and s["id"] not in ids:
                ids.append(s["id"])
    before = [copy.deepcopy(sim.original[i]) for i in ids if i in sim.original]
    after = []
    for i in ids:
        s = sim.state[i]
        if i < 0 and s["status"] == "deleted":
            continue  # im selben Vorschlag angelegt und wieder entfernt
        after.append({**copy.deepcopy(s), "_deleted": s["status"] == "deleted"})
    return {"before": before, "after": after}


def _summary(diff: dict[str, list[dict[str, Any]]]) -> list[str]:
    before = {s["id"]: s for s in diff["before"]}
    lines = []
    for a in diff["after"]:
        b = before.get(a["id"])
        if a.get("_deleted"):
            lines.append(f"entfernt: {b['date']} {b['title']}")
        elif b is None:
            lines.append(f"neu: {a['date']} {a['title']} ({a['duration_s'] // 60} min)")
        else:
            parts = []
            if a["date"] != b["date"]:
                parts.append(f"{b['date']} → {a['date']}")
            if a["duration_s"] != b["duration_s"]:
                parts.append(f"{b['duration_s'] // 60} → {a['duration_s'] // 60} min")
            if a["intensity"] != b["intensity"]:
                parts.append(f"{b['intensity']} → {a['intensity']}")
            lines.append(f"geändert: {b['title']}: " + ", ".join(parts or ["Beschreibung"]))
    return lines


def propose_plan_change(conn: sqlite3.Connection, ops: list[dict[str, Any]], reason: str, actor: str = "llm",
                        tool: str = "propose_plan_change", today: date | None = None) -> dict[str, Any]:
    """Validiert die Operationen gegen die Regeln und speichert sie als unbestätigten Vorschlag."""
    today = today or date.today()
    if not reason or len(reason.strip()) < 10:
        raise PlanError("Begründung fehlt oder ist zu kurz. Jede Änderung braucht einen nachvollziehbaren Grund.")
    if not ops:
        raise PlanError("Keine Operationen übergeben.")
    plan = get_active_plan(conn)
    if not plan:
        raise PlanError("Kein aktiver Plan.")
    sim = _Sim(conn, plan, today)
    for op in ops:
        _apply_op(sim, op)
    if not sim.touched:
        raise PlanError("Die Operationen ändern nichts.", sim.warnings or None)
    errors = rules.check_op_dates(plan, sim.touched, today)
    rule_errors, rule_warnings = rules.validate_weeks(conn, plan, sim.active_list(), sim.mondays, today)
    errors += rule_errors
    if errors:
        raise PlanError("Vorschlag verletzt Planregeln", errors)
    diff = _diff(sim)
    warnings = sim.warnings + rule_warnings
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO plan_changes(plan_id, created_at, actor, tool, reason, ops, diff, warnings, status) "
            "VALUES (?,?,?,?,?,?,?,?, 'pending')",
            (plan["id"], now_iso(), actor, tool, reason.strip(), json.dumps(ops, ensure_ascii=False),
             json.dumps(diff, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False)))
    return {"change_id": cur.lastrowid, "status": "pending",
            "summary": _summary(diff), "warnings": warnings,
            "weeks": {m.isoformat(): _week_brief(conn, plan, m, sim.active_list(), today) for m in sorted(sim.mondays)},
            "hint": "Vorschlag gespeichert, noch nicht angewendet. Bestätigung im Posteingang des Frontends."}


def _week_brief(conn: sqlite3.Connection, plan: dict[str, Any], monday: date, state: list[dict[str, Any]],
                today: date) -> dict[str, Any]:
    from .periodization import week_effective
    t = week_targets(conn, plan, monday, state, today)
    eff = week_effective(conn, monday, state, today)
    return {"week_type": t["context"]["week_type"], "phase": t["context"]["phase"], "load": eff["load"],
            "hours": eff["hours"], "corridor": t["load_corridor"], "target_hours": t["target_hours"],
            "reference": t["reference"]}


# --- Änderungen: Anwenden, Verwerfen, Rückgängig --------------------------------------

def _write_session(conn: sqlite3.Connection, target: dict[str, Any], current: dict[str, Any] | None) -> int:
    """Schreibt den Zielzustand einer Einheit. Gibt die (ggf. neue) id zurück."""
    ts = now_iso()
    fields = {k: target.get(k) for k in SESSION_FIELDS if k not in ("id", "status", "external_event_id")}
    if target.get("_deleted"):
        if current is None:
            return target["id"]
        if current.get("external_event_id"):
            conn.execute("UPDATE plan_sessions SET status = 'deleted', updated_at = ? WHERE id = ?", (ts, current["id"]))
        else:
            conn.execute("DELETE FROM plan_sessions WHERE id = ?", (current["id"],))
        return current["id"]
    if current is None:
        ext = target.get("external_event_id")
        values = {**fields, "status": "planned", "external_event_id": ext, "created_at": ts, "updated_at": ts}
        if target.get("id") and target["id"] > 0:
            values["id"] = target["id"]  # Wiederherstellen einer gelöschten Einheit mit alter id
        cols = list(values)
        cur = conn.execute(f"INSERT INTO plan_sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                           list(values.values()))
        return cur.lastrowid
    # Bereits veröffentlicht: zurück auf 'planned', external_event_id bleibt -> Publisher aktualisiert per PUT
    status = "planned" if current["status"] in ("published", "planned", "deleted") else current["status"]
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE plan_sessions SET {assignments}, status = ?, updated_at = ? WHERE id = ?",
                 [*fields.values(), status, ts, current["id"]])
    return current["id"]


def _current(conn: sqlite3.Connection, sid: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM plan_sessions WHERE id = ?", (sid,)).fetchone()
    return dict(row) if row else None


def get_change(conn: sqlite3.Connection, change_id: int) -> dict[str, Any] | None:
    return row_to_dict(conn.execute("SELECT * FROM plan_changes WHERE id = ?", (change_id,)).fetchone(),
                       ("ops", "diff", "warnings"))


def apply_change(conn: sqlite3.Connection, change_id: int, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    ch = get_change(conn, change_id)
    if ch is None or ch["status"] != "pending":
        raise PlanError(f"Änderung {change_id} ist nicht offen.")
    diff = ch["diff"]
    stale = []
    for b in diff["before"]:
        cur = _current(conn, b["id"])
        if cur is None or any(cur.get(k) != b.get(k) for k in SESSION_FIELDS if k not in ("status", "external_event_id")):
            stale.append(b["id"])
        if to_date(b["date"]) < today:
            stale.append(b["id"])
    if any(to_date(a["date"]) < today for a in diff["after"] if not a.get("_deleted")):
        stale.append("Datum")
    if stale:
        raise PlanError("Plan hat sich seit dem Vorschlag geändert oder betroffene Tage liegen jetzt in der "
                        "Vergangenheit. Vorschlag verwerfen und neu erstellen.")
    with transaction(conn):
        for a in diff["after"]:
            cur = _current(conn, a["id"]) if a["id"] > 0 else None
            new_id = _write_session(conn, a, cur)
            a["id"] = new_id  # temporäre ids durch echte ersetzen, damit Undo sie findet
        conn.execute("UPDATE plan_changes SET status = 'applied', applied_at = ?, resolved_at = ?, diff = ? WHERE id = ?",
                     (now_iso(), now_iso(), json.dumps(diff, ensure_ascii=False), change_id))
    return get_change(conn, change_id)


def reject_change(conn: sqlite3.Connection, change_id: int) -> None:
    ch = get_change(conn, change_id)
    if ch is None or ch["status"] != "pending":
        raise PlanError(f"Änderung {change_id} ist nicht offen.")
    conn.execute("UPDATE plan_changes SET status = 'rejected', resolved_at = ? WHERE id = ?", (now_iso(), change_id))


def revert_change(conn: sqlite3.Connection, change_id: int, today: date | None = None) -> dict[str, Any]:
    """Macht eine angewendete Änderung rückgängig – als eigene, protokollierte Änderung."""
    today = today or date.today()
    ch = get_change(conn, change_id)
    if ch is None or ch["status"] != "applied":
        raise PlanError(f"Änderung {change_id} ist nicht angewendet.")
    diff = ch["diff"]
    before_ids = {b["id"] for b in diff["before"]}
    for s in diff["before"] + [a for a in diff["after"] if not a.get("_deleted")]:
        cur = _current(conn, s["id"])
        if to_date(s["date"]) < today or (cur and cur["status"] in ("done", "skipped")):
            raise PlanError(f"Einheit {s['id']} liegt in der Vergangenheit oder ist erledigt – Undo nicht möglich.")
    new_before, new_after = [], []
    with transaction(conn):
        for a in diff["after"]:
            cur = _current(conn, a["id"])
            if a["id"] not in before_ids and cur is not None:  # von der Änderung angelegt -> entfernen
                new_before.append(_snap(cur))
                new_after.append({**_snap(cur), "_deleted": True})
                _write_session(conn, {**_snap(cur), "_deleted": True}, cur)
        for b in diff["before"]:
            cur = _current(conn, b["id"])
            if cur:
                new_before.append(_snap(cur))
            _write_session(conn, {**b, "_deleted": False}, cur)
            new_after.append(b)
        conn.execute("UPDATE plan_changes SET status = 'reverted', resolved_at = ? WHERE id = ?", (now_iso(), change_id))
        cur = conn.execute(
            "INSERT INTO plan_changes(plan_id, created_at, actor, tool, reason, ops, diff, warnings, status, applied_at, "
            "resolved_at, reverts_change_id) VALUES (?,?,'human','revert',?,?,?, '[]', 'applied', ?, ?, ?)",
            (ch["plan_id"], now_iso(), f"Rückgängig: {ch['reason']}", json.dumps([{"op": "revert", "change_id": change_id}]),
             json.dumps({"before": new_before, "after": new_after}, ensure_ascii=False), now_iso(), now_iso(), change_id))
    return get_change(conn, cur.lastrowid)


def list_changes(conn: sqlite3.Connection, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM plan_changes"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    rows = [row_to_dict(r, ("ops", "diff", "warnings"))
            for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])]
    for r in rows:
        r["summary"] = _summary(r["diff"])
    return rows


def propose_next_week(conn: sqlite3.Connection, today: date | None = None,
                      actor: str = "system") -> dict[str, Any] | None:
    """Schlägt die nächste noch leere Woche vor. Immer nur eine offene Generierung: die Folgewoche baut auf
    der bestätigten Vorwoche auf (Referenzlast), deshalb wird wochenweise statt blockweise generiert."""
    today = today or date.today()
    plan = get_active_plan(conn)
    if not plan:
        raise PlanError("Kein aktiver Plan.")
    m = max(monday_of(today), monday_of(plan["start_date"]))
    pending_weeks = set()
    for ch in list_changes(conn, "pending"):
        for op in ch["ops"]:
            if op.get("op") == "regenerate_week":
                pending_weeks.add(parse_week(op.get("week")))
    if pending_weeks:
        raise PlanError("Es gibt bereits eine offene Wochengenerierung im Posteingang. Erst bestätigen oder verwerfen.")
    while m < monday_of(today) + timedelta(weeks=26):
        has = conn.execute("SELECT 1 FROM plan_sessions WHERE plan_id = ? AND date BETWEEN ? AND ? AND status != 'deleted'",
                           (plan["id"], m.isoformat(), (m + timedelta(days=6)).isoformat())).fetchone()
        if plan.get("goal_date") and m > to_date(plan["goal_date"]):
            break
        if not has:
            return propose_plan_change(conn, [{"op": "regenerate_week", "week": m.isoformat()}],
                                       f"Woche ab {m.isoformat()} aus Vorlage, Leistungstests und aktuellem "
                                       f"Trainingsumfang erzeugt", actor=actor, tool="generate_week", today=today)
        m += timedelta(weeks=1)
    return None


__all__ = ["create_plan", "get_active_plan", "propose_plan_change", "apply_change", "reject_change",
           "revert_change", "list_changes", "plan_readiness", "available_templates", "week_targets"]
