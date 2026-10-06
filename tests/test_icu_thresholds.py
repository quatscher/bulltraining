import pytest
from fastapi.testclient import TestClient

from bulltraining import icu_thresholds
from bulltraining.performance import record_test

from .conftest import TODAY

# Stand des echten Kontos am 2026-10-06 (Ausschnitt)
SETTINGS = [
    {"id": 11, "types": ["Ride", "VirtualRide"], "lthr": 168, "max_hr": 185, "ftp": 250,
     "hr_zones": [135, 150, 156, 167, 172, 177, 185]},
    {"id": 12, "types": ["Run", "VirtualRun"], "lthr": 168, "max_hr": 185, "threshold_pace": None,
     "hr_zones": [141, 150, 158, 167, 172, 177, 185]},
    {"id": 13, "types": ["Swim", "OpenWaterSwim"], "lthr": 168, "max_hr": 185, "threshold_pace": 0.8333333},
]


class FakeClient:
    def __init__(self):
        self.puts = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sport_settings(self):
        return SETTINGS

    def update_sport_settings(self, sid, payload):
        self.puts.append((sid, payload))
        return {}


def _run_test(conn):
    record_test(conn, date=TODAY.isoformat(), protocol="run_30min_tt",
                inputs={"distance_m": 6763, "avg_hr_last_20min": 179, "max_hr": 185})


def test_diff_after_run_test(conn):
    _run_test(conn)
    d = icu_thresholds.diff(conn, SETTINGS)
    assert [x["sport"] for x in d] == ["run"]  # Rad/Schwimmen ohne eigenen Test: nichts überschreiben
    p = d[0]["payload"]
    assert p["lthr"] == 179 and p["threshold_pace"] == pytest.approx(1000 / 266.2, abs=1e-3)  # 4:26/km als m/s
    assert "max_hr" not in p  # 185 = 185
    assert p["hr_zones"][:4] == [150, 160, 168, 178] and p["hr_zones"][-1] == 185
    shown = {c["label"]: (c["old"], c["new"]) for c in d[0]["changes"]}
    assert shown["Schwellenpace"] == ("–", "4:26/km") and shown["Schwellenpuls"] == ("168", "179")


def test_nothing_to_push_without_tests_or_when_equal(conn):
    assert icu_thresholds.diff(conn, SETTINGS) == []
    _run_test(conn)
    same = [dict(s, lthr=179, threshold_pace=1000 / 266.2) if "Run" in s["types"] else s for s in SETTINGS]
    assert icu_thresholds.diff(conn, same) == []


def test_web_shows_diff_and_pushes(conn, monkeypatch):
    import bulltraining.web.app as app_module
    app_module._conn = conn
    fake = FakeClient()
    monkeypatch.setattr(app_module, "IntervalsClient", lambda: fake)
    try:
        client = TestClient(app_module.app)
        assert "nichts zu übertragen" in client.get("/performance/icu").text
        _run_test(conn)
        html = client.get("/performance/icu").text
        assert "Nach intervals.icu übernehmen" in html and "4:26/km" in html
        r = client.post("/performance/icu", follow_redirects=False)
        assert "msg=" in r.headers["location"] and fake.puts[0][0] == 12 and fake.puts[0][1]["lthr"] == 179
    finally:
        app_module._conn = None
