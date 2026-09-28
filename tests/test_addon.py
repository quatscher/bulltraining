import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from bulltraining import addon, config, plans
from bulltraining.db import connect

from .conftest import TODAY, seed_history


def test_token_from_options_or_generated_once(tmp_path):
    assert addon.ensure_token({"mcp_token": "abc"}, tmp_path) == "abc"
    first = addon.ensure_token({}, tmp_path)
    assert len(first) > 30 and addon.ensure_token({"mcp_token": ""}, tmp_path) == first


def test_import_only_into_empty_data_dir(tmp_path):
    src = tmp_path / "share" / "import.db"
    src.parent.mkdir()
    c = connect(src)
    seed_history(c)
    plans.create_plan(c, name="Import", goal_type="continuous", sports=["run"], weekly_hours=4, today=TODAY)
    c.close()
    db_path = tmp_path / "data" / "bulltraining.db"
    db_path.parent.mkdir()
    assert addon.import_database(db_path, src) is True
    assert not src.exists() and not list(src.parent.glob("import.db-*"))  # umbenannt, keine WAL-Reste
    assert plans.get_active_plan(connect(db_path))["name"] == "Import"
    other = tmp_path / "share" / "import.db"
    connect(other).close()
    assert addon.import_database(db_path, other) is False  # vorhandene Daten werden nie überschrieben


def test_import_rejects_foreign_database(tmp_path):
    import sqlite3
    src = tmp_path / "import.db"
    sqlite3.connect(src).execute("CREATE TABLE x (a)").connection.close()
    assert addon.import_database(tmp_path / "bulltraining.db", src) is False
    assert not (tmp_path / "bulltraining.db").exists()


def _call(app, headers):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


def test_dual_stack_socket_accepts_ipv4_and_ipv6():
    import socket
    sock = addon.dual_stack_socket(0)
    port = sock.getsockname()[1]
    try:
        for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            if family == socket.AF_INET6 and sock.family != socket.AF_INET6:
                continue
            c = socket.socket(family, socket.SOCK_STREAM)
            c.settimeout(3)
            c.connect((host, port))
            c.close()
    finally:
        sock.close()


def test_bearer_auth():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = addon.BearerAuth(inner, "geheim")
    assert _call(app, []) == 401
    assert _call(app, [(b"authorization", b"Bearer falsch")]) == 401
    assert _call(app, [(b"authorization", b"Bearer geheim")]) == 200


@pytest.fixture
def ingress_client(conn):
    import bulltraining.web.app as web
    web._conn = conn
    yield TestClient(web.app)
    web._conn = None


def test_ingress_prefix_in_links_and_redirects(ingress_client):
    h = {"X-Ingress-Path": "/api/hassio_ingress/Tok_en-1"}
    r = ingress_client.get("/", headers=h, follow_redirects=False)
    assert r.headers["location"] == "/api/hassio_ingress/Tok_en-1/calendar"
    html = ingress_client.get("/calendar", headers=h).text
    assert 'href="/api/hassio_ingress/Tok_en-1/performance"' in html and 'href="/performance"' not in html
    evil = {"X-Ingress-Path": "https://evil.example"}  # nur echte Ingress-Pfade werden übernommen
    assert ingress_client.get("/", headers=evil, follow_redirects=False).headers["location"] == "/calendar"


def test_same_origin_behind_proxy(ingress_client):
    h = {"X-Ingress-Path": "/api/hassio_ingress/abc", "Origin": "http://homeassistant.local:8123",
         "X-Forwarded-Host": "homeassistant.local:8123"}
    assert ingress_client.post("/settings", data={"strength_minutes": "50"}, headers=h,
                               follow_redirects=False).status_code == 303
    assert ingress_client.post("/settings", data={"strength_minutes": "50"},
                               headers={**h, "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_settings_page_shows_mcp_access(ingress_client, monkeypatch):
    monkeypatch.setattr(config, "MCP_INFO", {"port": 8765, "path": "/mcp", "token": "t0k"})
    html = ingress_client.get("/settings", headers={"X-Forwarded-Host": "homeassistant.local:8123"}).text
    assert "http://homeassistant.local:8765/mcp" in html and "Bearer t0k" in html


def test_addon_config_matches_code():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    cfg = (root / "bulltraining" / "config.yaml").read_text(encoding="utf-8")
    assert f"ingress_port: {addon.WEB_PORT}" in cfg and f"{addon.MCP_PORT}/tcp: null" in cfg  # MCP-Port standardmäßig aus
    assert "host_network" not in cfg  # Weboberfläche nur über Ingress
    version = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    assert f'version: "{version}"' in cfg  # CI prüft dasselbe vor dem Image-Build
    assert json.dumps("share:rw")[1:-1] in cfg
