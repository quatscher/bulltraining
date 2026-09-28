import pytest
from fastapi.testclient import TestClient

from bulltraining import config, intervals_client
from bulltraining.db import SettingError, get_setting, set_setting

KEY = "41qbb0bjs58xfuhf8bc4cx9zz"


@pytest.fixture
def web(conn, monkeypatch):
    import bulltraining.web.app as app_module
    app_module._conn = conn
    monkeypatch.setattr(intervals_client, "_stored", lambda key: get_setting(conn, key) or "")
    yield TestClient(app_module.app), conn
    app_module._conn = None


def test_web_key_takes_precedence_over_addon_option(conn, monkeypatch):
    monkeypatch.setattr(config, "INTERVALS_API_KEY", "addonoptionkey123")
    monkeypatch.setattr(intervals_client, "_stored", lambda key: get_setting(conn, key) or "")
    assert intervals_client.effective_api_key() == "addonoptionkey123"
    set_setting(conn, "intervals_api_key", KEY)
    assert intervals_client.effective_api_key() == KEY
    assert intervals_client.masked(KEY) == "••••x9zz"


def test_key_is_validated():
    from bulltraining.db import validate_setting
    with pytest.raises(SettingError):
        validate_setting("intervals_api_key", "zu kurz")
    with pytest.raises(SettingError):
        validate_setting("intervals_athlete_id", "abc")
    assert validate_setting("intervals_athlete_id", "i727740") == "i727740"


def test_settings_page_never_shows_the_key(web):
    client, conn = web
    set_setting(conn, "intervals_api_key", KEY)
    html = client.get("/settings").text
    assert KEY not in html and "••••x9zz" in html and "Weboberfläche" in html
    assert 'name="intervals_api_key"' not in html  # nicht in der allgemeinen Tabelle


def test_save_empty_field_keeps_key_and_remove_clears_it(web):
    client, conn = web
    client.post("/settings/intervals", data={"api_key": KEY, "athlete_id": "0"}, follow_redirects=False)
    assert get_setting(conn, "intervals_api_key") == KEY
    client.post("/settings/intervals", data={"api_key": "", "athlete_id": "0"}, follow_redirects=False)
    assert get_setting(conn, "intervals_api_key") == KEY  # leeres Feld = unverändert
    r = client.post("/settings/intervals", data={"api_key": "x!", "athlete_id": "0"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and get_setting(conn, "intervals_api_key") == KEY
    client.post("/settings/intervals", data={"remove_key": "1", "athlete_id": "0"}, follow_redirects=False)
    assert get_setting(conn, "intervals_api_key") is None


class _FakeClient:
    def __init__(self, activities):
        self._acts = activities

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def athlete(self):
        return {"id": "i727740", "name": "quatscher", "icu_garmin_sync_activities": True}

    def activities(self, *a):
        return self._acts

    def wellness(self, *a):
        return []


def test_connection_test_reports_empty_account(web, monkeypatch):
    client, _ = web
    import bulltraining.web.app as app_module
    monkeypatch.setattr(app_module, "IntervalsClient", lambda: _FakeClient([]))
    loc = client.post("/settings/intervals/test", follow_redirects=False).headers["location"]
    assert "err=" in loc and "Garmin" in loc
    assert get_setting(_, "intervals_athlete_id") == "i727740"  # „0“ durch die echte ID ersetzt
    monkeypatch.setattr(app_module, "IntervalsClient",
                        lambda: _FakeClient([{"start_date_local": "2026-09-29T07:00:00"}]))
    loc = client.post("/settings/intervals/test", follow_redirects=False).headers["location"]
    assert "msg=" in loc and "2026-09-29" in loc
