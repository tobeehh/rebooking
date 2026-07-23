"""Laden und Validieren der Konfiguration (YAML + Umgebungsvariablen)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import Booking

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _interpolate_env(value: Any) -> Any:
    """Ersetzt ${VAR} in Strings durch Umgebungsvariablen (leer, falls nicht gesetzt)."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


@dataclass
class NotificationConfig:
    channel: str = "console"  # console | email | telegram
    min_drop_absolute: float = 1.0
    min_drop_percent: float = 0.0
    email: dict[str, Any] = field(default_factory=dict)
    telegram: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScraperConfig:
    headless: bool = True
    timeout_ms: int = 60000
    locale: str = "de-DE"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    override_url_dates: bool = True
    price_selectors: list[str] = field(default_factory=list)
    # Sekunden Verzögerung zwischen zwei Abfragen, um nicht aufzufallen.
    delay_between_seconds: float = 8.0
    # Optionaler Pfad zu einer Chrome/Chromium-Binary (statt Download).
    executable_path: str = ""
    # Optional: an laufenden Chrome per CDP anbinden (z.B. http://127.0.0.1:9222)
    # statt einen eigenen Browser zu starten – robuster gegen Bot-Schutz.
    cdp_url: str = ""
    # Abgemeldete Seiten als Fehler behandeln. Ohne Login liefert Hotels.com
    # Listen- statt Mitgliederpreise (gemessen: 759 € statt 391 €) – der
    # Vergleich wäre dann wertlos, ohne dass es auffällt.
    require_login: bool = True


@dataclass
class AccountConfig:
    """Automatischer Import der Buchungen aus dem Hotels.com-Konto."""

    # Persistentes Browser-Profil (Cookies/Session nach einmaligem Login).
    profile_dir: str = "data/browser_profile"
    # Seite mit der Reise-/Buchungsübersicht.
    trips_url: str = "https://www.hotels.com/trips"
    login_url: str = "https://www.hotels.com/login"
    # Detailseite je Buchung; {id} wird durch die tripId ersetzt.
    trip_detail_url_template: str = "https://www.hotels.com/trips/{id}"
    # Beim Import Login-Fenster sichtbar? (für den einmaligen Login nötig)
    headless: bool = True
    # Optionaler Pfad zu einer Chrome/Chromium-Binary (statt Download).
    executable_path: str = ""
    # Optional: an laufenden Chrome per CDP anbinden (z.B. http://127.0.0.1:9222).
    cdp_url: str = ""
    # Sprache der Kontoansicht. Wird im eigenen Browser als Locale und
    # Accept-Language gesetzt, damit die Texte reproduzierbar sind.
    # Im CDP-Modus nicht erzwingbar – dort gilt die Sprache deines Chrome.
    locale: str = "de-DE"
    # Nur frei stornierbare Buchungen überwachen (Umbuchen sonst nicht möglich).
    only_if_free_cancellation: bool = True


@dataclass
class Config:
    currency: str
    notifications: NotificationConfig
    scraper: ScraperConfig
    bookings: list[Booking]
    data_dir: Path
    account: AccountConfig = field(default_factory=AccountConfig)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Konfigurationsdatei nicht gefunden: {path}\n"
                f"Kopiere config.example.yaml nach config.yaml und trage deine Buchungen ein."
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = _interpolate_env(raw)

        currency = raw.get("currency", "EUR")

        n = raw.get("notifications", {}) or {}
        notifications = NotificationConfig(
            channel=n.get("channel", "console"),
            min_drop_absolute=float(n.get("min_drop_absolute", 1.0)),
            min_drop_percent=float(n.get("min_drop_percent", 0.0)),
            email=n.get("email", {}) or {},
            telegram=n.get("telegram", {}) or {},
        )

        s = raw.get("scraper", {}) or {}
        scraper = ScraperConfig(
            headless=bool(s.get("headless", True)),
            timeout_ms=int(s.get("timeout_ms", 60000)),
            locale=s.get("locale", "de-DE"),
            user_agent=s.get("user_agent", ScraperConfig.user_agent),
            override_url_dates=bool(s.get("override_url_dates", True)),
            price_selectors=list(s.get("price_selectors", []) or []),
            delay_between_seconds=float(s.get("delay_between_seconds", 8.0)),
            executable_path=s.get("executable_path", "") or "",
            cdp_url=s.get("cdp_url", "") or "",
            require_login=bool(s.get("require_login", True)),
        )

        bookings: list[Booking] = []
        for entry in raw.get("bookings", []) or []:
            bookings.append(
                Booking(
                    name=entry["name"],
                    url=entry["url"],
                    checkin=entry["checkin"],
                    checkout=entry["checkout"],
                    paid_price=entry["paid_price"],
                    adults=int(entry.get("adults", 2)),
                    children=int(entry.get("children", 0)),
                    child_ages=list(entry.get("child_ages", []) or []),
                    rooms=int(entry.get("rooms", 1)),
                    currency=entry.get("currency", currency),
                    notes=entry.get("notes", ""),
                )
            )

        data_dir = Path(raw.get("data_dir", "data"))

        a = raw.get("account", {}) or {}
        account = AccountConfig(
            profile_dir=a.get("profile_dir", "data/browser_profile"),
            trips_url=a.get("trips_url", "https://www.hotels.com/trips"),
            login_url=a.get("login_url", "https://www.hotels.com/login"),
            trip_detail_url_template=a.get("trip_detail_url_template", "https://www.hotels.com/trips/{id}"),
            headless=bool(a.get("headless", True)),
            executable_path=a.get("executable_path", "") or "",
            cdp_url=a.get("cdp_url", "") or "",
            locale=a.get("locale", "de-DE") or "de-DE",
            only_if_free_cancellation=bool(a.get("only_if_free_cancellation", True)),
        )

        return cls(
            currency=currency,
            notifications=notifications,
            scraper=scraper,
            bookings=bookings,
            data_dir=data_dir,
            account=account,
        )
