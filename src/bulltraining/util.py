"""Datums- und Sportart-Hilfen."""
from __future__ import annotations

from datetime import date, datetime, timedelta

SPORTS = ("run", "ride", "swim", "strength", "other")
ENDURANCE_SPORTS = ("run", "ride", "swim")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# intervals.icu-Typ -> (sport, is_endurance)
_ICU_TYPES = {
    "Run": ("run", 1), "VirtualRun": ("run", 1), "TrailRun": ("run", 1),
    "Ride": ("ride", 1), "VirtualRide": ("ride", 1), "GravelRide": ("ride", 1),
    "MountainBikeRide": ("ride", 1), "EBikeRide": ("ride", 0), "TrackRide": ("ride", 1),
    "Swim": ("swim", 1), "OpenWaterSwim": ("swim", 1),
    "WeightTraining": ("strength", 0), "Crossfit": ("strength", 0), "Workout": ("other", 0),
    "Rowing": ("other", 1), "VirtualRow": ("other", 1), "NordicSki": ("other", 1),
    "Elliptical": ("other", 1), "Walk": ("other", 0), "Hike": ("other", 0), "Yoga": ("other", 0),
}

SPORT_TO_ICU = {"run": "Run", "ride": "Ride", "swim": "Swim", "strength": "WeightTraining", "other": "Workout"}


def map_icu_type(icu_type: str | None) -> tuple[str, int]:
    return _ICU_TYPES.get(icu_type or "", ("other", 0))


def to_date(value: str | date | datetime | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def monday_of(d: str | date) -> date:
    d = to_date(d)
    return d - timedelta(days=d.weekday())


def parse_week(week: str | date | None) -> date:
    """'2026-W40', ein beliebiges Datum der Woche oder None (= aktuelle Woche) -> Montag."""
    if week is None or week == "":
        return monday_of(date.today())
    if isinstance(week, str) and "-W" in week:
        year, w = week.split("-W")
        return date.fromisocalendar(int(year), int(w), 1)
    return monday_of(week)


def iso_week(d: str | date) -> str:
    y, w, _ = to_date(d).isocalendar()
    return f"{y}-W{w:02d}"


def week_days(monday: date) -> list[date]:
    return [monday + timedelta(days=i) for i in range(7)]


def weekday_key(d: str | date) -> str:
    return WEEKDAYS[to_date(d).weekday()]


def fmt_pace(seconds: float | None, unit: str = "/km") -> str | None:
    if seconds is None:
        return None
    seconds = round(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}{unit}"


def parse_duration(value: str | int | float) -> int:
    """'1:02:30', '4:35', '95' (Sekunden) -> Sekunden."""
    if isinstance(value, (int, float)):
        return int(round(value))
    parts = [float(p) for p in str(value).strip().split(":")]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return int(round(seconds))
