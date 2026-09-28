import pytest
from fastapi.testclient import TestClient

from bulltraining import bikes


@pytest.fixture
def web(conn):
    import bulltraining.web.app as app_module
    app_module._conn = conn
    yield TestClient(app_module.app), conn
    app_module._conn = None


def test_setup_versions_and_diff(conn):
    b = bikes.create_bike(conn, "Canyon Speedmax", "Zeitfahrrad")
    assert bikes.save_setup(conn, b, {"saddle_tilt": "4°", "cockpit_height": "50 mm"}, valid_from="2026-09-01")
    assert bikes.save_setup(conn, b, {"saddle_tilt": "4°", "cockpit_height": "50 mm"}) is None  # unverändert
    bikes.save_setup(conn, b, {"saddle_tilt": "3°", "Vorbau": "90 mm"}, note="Nacken", valid_from="2026-09-20")
    assert bikes.current_setup(conn, b)["values"] == {"saddle_tilt": "3°", "Vorbau": "90 mm"}
    newest = bikes.setup_history(conn, b)[0]
    assert {"field": "Sattelneigung", "old": "4°", "new": "3°"} in newest["changes"]
    assert {"field": "Cockpit-Höhe", "old": "50 mm", "new": None} in newest["changes"]
    assert bikes.bikes_for_llm(conn)[0]["setup"]["Sattelneigung"] == "3°"


def test_web_page_setup_and_photo(web):
    client, conn = web
    client.post("/bike", data={"name": "Canyon Speedmax", "kind": "Zeitfahrrad"}, follow_redirects=False)
    bid = bikes.list_bikes(conn)[0]["id"]
    r = client.post(f"/bike/{bid}/setup", data={"f__ext_tilt": "14°", "extra_name": ["Pedale", ""],
                                                  "extra_value": ["Look Keo", ""], "note": ""},
                    follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert bikes.current_setup(conn, bid)["values"] == {"ext_tilt": "14°", "Pedale": "Look Keo"}
    r = client.post(f"/bike/{bid}/photo", files={"photo": ("a.png", b"\x89PNG....", "image/png")},
                    data={"caption": "Cockpit"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    pid = bikes.list_bikes(conn)[0]["photos"][0]["id"]
    assert client.get(f"/bike/photo/{pid}").content == b"\x89PNG...."
    r = client.post(f"/bike/{bid}/photo", files={"photo": ("a.exe", b"MZ", "application/x-msdownload")},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]
    html = client.get("/bike").text
    assert "14°" in html and "Look Keo" in html and "Cockpit" in html
