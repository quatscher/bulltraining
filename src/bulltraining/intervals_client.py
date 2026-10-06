"""Dünner Client für die intervals.icu-REST-API. Kapselt Auth, User-Agent und Rate Limits."""
from __future__ import annotations

import time
from typing import Any

import httpx

from . import config


class IntervalsError(RuntimeError):
    pass


def _stored(key: str) -> str:
    """Wert aus den Einstellungen der Datenbank (Weboberfläche); leer, wenn nicht gesetzt oder nicht lesbar."""
    try:
        from .db import get_setting, thread_connection
        return get_setting(thread_connection(), key) or ""
    except Exception:  # noqa: BLE001 – ohne DB (Tests, CLI ohne Datenbank) gilt die Konfiguration
        return ""


def effective_api_key() -> str:
    """API-Key: Weboberfläche vor Add-on-Option bzw. Umgebungsvariable."""
    return _stored("intervals_api_key") or config.INTERVALS_API_KEY


def effective_athlete_id() -> str:
    return _stored("intervals_athlete_id") or config.INTERVALS_ATHLETE_ID


def masked(key: str) -> str:
    return "••••" + key[-4:] if key else ""


class IntervalsClient:
    MIN_INTERVAL_S = 0.11  # höchstens ~9 Anfragen pro Sekunde, Limit ist 10

    def __init__(self, api_key: str | None = None, athlete_id: str | None = None,
                 base_url: str | None = None, transport: httpx.BaseTransport | None = None,
                 max_retries: int = 3):
        api_key = api_key if api_key is not None else effective_api_key()
        if not api_key and transport is None:
            raise IntervalsError("Kein intervals.icu-API-Key: in den Einstellungen eintragen "
                                 "(intervals.icu → Settings → Developer Settings)")
        self.athlete_id = athlete_id or effective_athlete_id()
        self.max_retries = max_retries
        self._last_request = 0.0
        self.rate_remaining: int | None = None
        self.http = httpx.Client(
            base_url=base_url or config.INTERVALS_BASE_URL,
            auth=("API_KEY", api_key),
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "IntervalsClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        for attempt in range(self.max_retries + 1):
            wait = self.MIN_INTERVAL_S - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            resp = self.http.request(method, url, **kwargs)
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None and remaining.isdigit():
                self.rate_remaining = int(remaining)
            if resp.status_code == 429 and attempt < self.max_retries:
                time.sleep(float(resp.headers.get("Retry-After", "5")))
                continue
            if resp.status_code >= 400:
                raise IntervalsError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
            if not resp.content:
                return None
            return resp.json()
        raise IntervalsError(f"{method} {url}: Rate Limit nach {self.max_retries} Versuchen")

    # --- lesend ---------------------------------------------------------------
    def athlete(self) -> dict:
        return self._request("GET", f"/api/v1/athlete/{self.athlete_id}")

    def sport_settings(self) -> list[dict]:
        return self.athlete().get("sportSettings") or []

    def activities(self, oldest: str, newest: str) -> list[dict]:
        return self._request("GET", f"/api/v1/athlete/{self.athlete_id}/activities",
                             params={"oldest": oldest, "newest": newest}) or []

    def activity(self, activity_id: str) -> dict:
        return self._request("GET", f"/api/v1/activity/{activity_id}", params={"intervals": "true"})

    def wellness(self, oldest: str, newest: str) -> list[dict]:
        return self._request("GET", f"/api/v1/athlete/{self.athlete_id}/wellness",
                             params={"oldest": oldest, "newest": newest}) or []

    def events(self, oldest: str, newest: str) -> list[dict]:
        return self._request("GET", f"/api/v1/athlete/{self.athlete_id}/events",
                             params={"oldest": oldest, "newest": newest}) or []

    def find_event(self, day: str, name: str, category: str) -> dict | None:
        """Event am Tag mit gleichem Namen und gleicher Kategorie – Abgleich nach unklarer POST-Antwort."""
        for e in self.events(day, day):
            if e.get("name") == name and e.get("category") == category \
                    and str(e.get("start_date_local", ""))[:10] == day:
                return e
        return None

    # --- schreibend (nur Publisher) -------------------------------------------
    def create_event(self, payload: dict) -> dict:
        return self._request("POST", f"/api/v1/athlete/{self.athlete_id}/events", json=payload)

    def update_event(self, event_id: str, payload: dict) -> dict:
        return self._request("PUT", f"/api/v1/athlete/{self.athlete_id}/events/{event_id}", json=payload)

    def update_sport_settings(self, settings_id: int | str, payload: dict) -> dict:
        """Sport-Einstellungen (Schwellen, Pulszonen). threshold_pace in m/s."""
        return self._request("PUT", f"/api/v1/athlete/{self.athlete_id}/sport-settings/{settings_id}", json=payload)

    def delete_event(self, event_id: str) -> None:
        self._request("DELETE", f"/api/v1/athlete/{self.athlete_id}/events/{event_id}")
