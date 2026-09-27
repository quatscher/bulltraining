"""Betrieb als Home-Assistant-Add-on.

Ein Prozess, drei Aufgaben:
- Weboberfläche auf Port 8099, nur über Home-Assistant-Ingress erreichbar (HA-Login schützt den Zugriff)
- MCP-Server über HTTP auf Port 8765 für Claude, nur mit Bearer-Token
- Sync mit intervals.icu im festen Intervall (Veröffentlichen bleibt ein bewusster Knopfdruck)

Daten liegen in /data (lokal auf dem Pi, in HA-Backups enthalten). Eine vorhandene Datenbank kann einmalig über
die Freigabe \\\\homeassistant\\share\\bulltraining\\import.db übernommen werden.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config

WEB_PORT = 8099
MCP_PORT = 8765


def log(msg: str) -> None:
    print(f"[bulltraining {datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_options(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_token(options: dict[str, Any], data_dir: Path) -> str:
    """Token aus den Add-on-Optionen, sonst ein einmal erzeugtes und in /data gespeichertes."""
    if options.get("mcp_token"):
        return str(options["mcp_token"])
    path = data_dir / "mcp_token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


def import_database(db_path: Path, import_path: Path) -> bool:
    """Einmaliger Import: nur wenn es noch keine Datenbank gibt. Die Importdatei wird danach umbenannt."""
    if db_path.exists() or not import_path.exists():
        return False
    src = sqlite3.connect(f"file:{import_path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"plans", "plan_sessions", "activities"} <= tables:
            log(f"{import_path} ist keine bulltraining-Datenbank – Import übersprungen.")
            return False
        dst = sqlite3.connect(db_path)
        src.backup(dst)  # konsistente Kopie, auch aus einer WAL-Datenbank
        dst.close()
    finally:
        src.close()
    done = import_path.with_name(import_path.name + f".importiert-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.move(str(import_path), done)
    for suffix in ("-wal", "-shm"):  # Reste der schreibgeschützten Leseverbindung
        leftover = import_path.with_name(import_path.name + suffix)
        if leftover.exists():
            leftover.unlink()
    log(f"Datenbank aus {import_path} übernommen (Original umbenannt in {done.name}).")
    return True


class BearerAuth:
    """ASGI-Hülle: HTTP-Anfragen nur mit 'Authorization: Bearer <token>'. Lifespan wird durchgereicht."""

    def __init__(self, app: Any, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            header = dict(scope.get("headers") or []).get(b"authorization", b"")
            given = header[7:] if header.lower().startswith(b"bearer ") else b""
            if not given or not hmac.compare_digest(given, self.token):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": "Token fehlt oder ist falsch.".encode()})
                return
        await self.app(scope, receive, send)


def _sync_once() -> None:
    from .db import thread_connection
    from .intervals_client import IntervalsClient
    from .sync import run_sync
    try:
        with IntervalsClient() as client:
            res = run_sync(thread_connection(), client)
        log(f"Sync: {res['created']} neu, {res['updated']} aktualisiert, {res.get('removed', 0)} entfernt, "
            f"{res['duplicate_candidates']} Dublettenkandidaten.")
    except Exception as exc:  # noqa: BLE001 – im Log sichtbar, nächster Versuch im nächsten Intervall
        log(f"Sync fehlgeschlagen: {exc}")


async def sync_loop(interval_min: int) -> None:
    if interval_min <= 0 or not config.INTERVALS_API_KEY:
        log("Automatischer Sync aus (kein API-Key oder Intervall 0).")
        return
    await asyncio.sleep(20)  # Start nicht mit dem Sync blockieren
    while True:
        await asyncio.to_thread(_sync_once)
        await asyncio.sleep(interval_min * 60)


def configure(options: dict[str, Any], data_dir: Path) -> str:
    config.DB_PATH = data_dir / "bulltraining.db"
    config.INTERVALS_API_KEY = str(options.get("intervals_api_key") or "")
    config.INTERVALS_ATHLETE_ID = str(options.get("intervals_athlete_id") or "0")
    token = ensure_token(options, data_dir)
    config.MCP_INFO = {"port": MCP_PORT, "path": "/mcp", "token": token}
    return token


async def serve(options_path: Path, data_dir: Path, import_path: Path) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    options = load_options(options_path)
    data_dir.mkdir(parents=True, exist_ok=True)
    import_database(data_dir / "bulltraining.db", import_path)
    token = configure(options, data_dir)
    from .db import connect
    connect().close()  # Schema anlegen bzw. migrieren, bevor Anfragen kommen

    from .mcp_server import mcp
    from .web.app import app as web_app
    # Kein DNS-Rebinding-Schutz über Hostnamen (Zugriff per IP oder homeassistant.local): das Token schützt, und
    # ein Browser kann den Authorization-Header ohne CORS-Freigabe nicht fremd setzen.
    mcp_app = BearerAuth(mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False), host="0.0.0.0"), token)
    web = uvicorn.Server(uvicorn.Config(web_app, host="0.0.0.0", port=WEB_PORT, log_level="warning",
                                        proxy_headers=True, forwarded_allow_ips="*"))
    mcp_server = uvicorn.Server(uvicorn.Config(mcp_app, host="0.0.0.0", port=MCP_PORT, log_level="warning"))
    log(f"Weboberfläche auf Port {WEB_PORT} (Ingress), MCP auf Port {MCP_PORT}/mcp, Daten in {data_dir}.")
    log(f"MCP-Token: {token}")
    await asyncio.gather(web.serve(), mcp_server.serve(),
                         sync_loop(int(options.get("sync_interval_minutes", 60))))


def main() -> None:
    asyncio.run(serve(Path("/data/options.json"), Path("/data"), Path("/share/bulltraining/import.db")))
