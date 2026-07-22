"""Automatischer Import der Buchungen aus dem Hotels.com-Konto.

Ansatz (bewusst KEIN reiner HTTP-Client):

Hotels.com schützt Login und interne API mit Bot-Erkennung (HUMAN/PerimeterX)
und Geräteverifikation. Ein nachgebauter ``requests``-Client würde ständig
brechen. Stattdessen nutzen wir einen **echten Browser mit persistenter
Session**:

1. ``login()`` – einmalig, sichtbarer Browser. Du loggst dich manuell ein
   (inkl. 2FA). Cookies/Storage landen im Profilordner und werden
   wiederverwendet.
2. ``fetch_account_bookings()`` – lädt „Meine Reisen“ im eingeloggten Kontext,
   **fängt die GraphQL-/JSON-Antworten ab** und parst daraus die Buchungen.
   Zusätzlich ``--capture``, um die Rohantworten zu speichern und das
   Feld-Mapping bei Bedarf einmal nachzujustieren.

Die interne JSON-Struktur von Hotels.com ist nicht öffentlich dokumentiert und
ändert sich. Der Parser ``parse_trips`` ist deshalb heuristisch: er sucht
rekursiv nach Objekten, die sowohl einen Hotelnamen als auch ein Reisedatum
enthalten, und ist über eine Kandidatenliste von Feldnamen tolerant.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Kandidaten-Feldnamen (case-insensitive) für die heuristische Extraktion.
_NAME_KEYS = ("propertyname", "hotelname", "name", "title", "displayname")
_CHECKIN_KEYS = ("checkin", "checkindate", "startdate", "arrivaldate", "fromdate", "begindate")
_CHECKOUT_KEYS = ("checkout", "checkoutdate", "enddate", "departuredate", "todate")
_URL_KEYS = ("url", "propertyurl", "detailsurl", "infositeurl", "link", "href")
_PRICE_KEYS = ("total", "totalprice", "amount", "grandtotal", "price")
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


# ---------------------------------------------------------------------------
# Parsing (rein, testbar ohne Browser)
# ---------------------------------------------------------------------------


def _lower_keys(d: dict) -> dict:
    return {str(k).lower(): v for k, v in d.items()}


def _coerce_date(value: Any) -> str | None:
    """Extrahiert ein ISO-Datum (YYYY-MM-DD) aus String/Dict/Epoch."""
    if value is None:
        return None
    if isinstance(value, dict):
        low = _lower_keys(value)
        for k in ("isodate", "date", "value", "raw"):
            if k in low:
                got = _coerce_date(low[k])
                if got:
                    return got
        # Manche APIs liefern {year, month, day}
        if {"year", "month", "day"} <= set(low):
            try:
                return date(int(low["year"]), int(low["month"]), int(low["day"])).isoformat()
            except (ValueError, TypeError):
                return None
        if "epochseconds" in low:
            try:
                return datetime.fromtimestamp(int(low["epochseconds"]), tz=timezone.utc).date().isoformat()
            except (ValueError, TypeError, OSError):
                return None
        return None
    if isinstance(value, (int, float)) and value > 10_000_000:
        # Vermutlich Epoch (Sekunden oder Millisekunden).
        secs = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc).date().isoformat()
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        m = _DATE_RE.search(value)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _find_first(node: dict, keys: tuple[str, ...]) -> Any:
    low = _lower_keys(node)
    for k in keys:
        if k in low and low[k] not in (None, "", []):
            return low[k]
    return None


def _find_price(node: Any) -> float | None:
    """Sucht rekursiv einen plausiblen Gesamtpreis (Zahl)."""
    if isinstance(node, dict):
        low = _lower_keys(node)
        for k in _PRICE_KEYS:
            if k in low:
                val = low[k]
                if isinstance(val, (int, float)) and 5 <= val <= 100000:
                    return float(val)
                if isinstance(val, dict):
                    got = _find_price(val)
                    if got is not None:
                        return got
                if isinstance(val, str):
                    m = re.search(r"[0-9]+(?:[.,][0-9]+)?", val)
                    if m:
                        num = float(m.group(0).replace(",", "."))
                        if 5 <= num <= 100000:
                            return num
        # "amount" ist oft direkt ein Zahlenfeld irgendwo tiefer.
        for v in low.values():
            got = _find_price(v)
            if got is not None:
                return got
    elif isinstance(node, list):
        for v in node:
            got = _find_price(v)
            if got is not None:
                return got
    return None


def _walk(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def parse_trips(data: Any) -> list[dict]:
    """Extrahiert Buchungen aus einer (unbekannt strukturierten) JSON-Antwort.

    Rückgabe: Liste von Dicts mit den Feldern
    ``name, url, checkin, checkout, paid_price, currency`` (soweit auffindbar).
    Nur Objekte mit Name + Check-in + Check-out werden übernommen.
    """
    found: list[dict] = []
    seen: set[tuple] = set()

    for node in _walk(data):
        if not isinstance(node, dict):
            continue
        name = _find_first(node, _NAME_KEYS)
        checkin_raw = _find_first(node, _CHECKIN_KEYS)
        checkout_raw = _find_first(node, _CHECKOUT_KEYS)
        if not (name and checkin_raw and checkout_raw):
            continue
        checkin = _coerce_date(checkin_raw)
        checkout = _coerce_date(checkout_raw)
        if not (checkin and checkout) or checkout <= checkin:
            continue

        key = (str(name), checkin, checkout)
        if key in seen:
            continue
        seen.add(key)

        url = _find_first(node, _URL_KEYS)
        if isinstance(url, str) and url and not url.startswith("http"):
            url = "https://www.hotels.com" + url
        price = _find_price(node)
        currency = _find_first(node, ("currency", "currencycode")) or "EUR"

        found.append(
            {
                "name": str(name),
                "url": url or "",
                "checkin": checkin,
                "checkout": checkout,
                "paid_price": price,
                "currency": str(currency),
            }
        )
    return found


# ---------------------------------------------------------------------------
# Browser-Session (benötigt Playwright; lazy import)
# ---------------------------------------------------------------------------


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# --disable-http2 umgeht ERR_HTTP2_PROTOCOL_ERROR, das bei manchen Seiten im
# automatisierten Chromium auftritt.
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-http2",
]


def _looks_interesting(url: str) -> bool:
    url = url.lower()
    return "graphql" in url or "trips" in url or "itinerar" in url or "booking" in url


def _goto_with_retry(page, url: str, attempts: int = 3) -> None:
    """Navigiert mit Wiederholungen (fängt transiente Protokoll-/Netzfehler ab)."""
    last_exc = None
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            page.wait_for_timeout(2000 * (i + 1))
    if last_exc:
        raise last_exc


def login(account) -> None:
    """Öffnet einen sichtbaren Browser für den einmaligen manuellen Login."""
    from playwright.sync_api import sync_playwright

    profile = Path(account.profile_dir)
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            executable_path=account.executable_path or None,
            user_agent=_USER_AGENT,
            args=_LAUNCH_ARGS,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(account.login_url, wait_until="domcontentloaded")
        print(
            "\n>>> Bitte im geöffneten Browser einloggen (inkl. 2FA).\n"
            ">>> Wenn du 'Meine Reisen' sehen kannst, hier ENTER drücken."
        )
        try:
            input()
        except EOFError:
            page.wait_for_timeout(120_000)
        ctx.close()
    print("Session gespeichert in", profile)


def fetch_account_bookings(account, capture_dir: str | Path | None = None) -> list[dict]:
    """Lädt „Meine Reisen“ im eingeloggten Kontext und liefert Buchungen.

    Fängt GraphQL-/JSON-Antworten ab und parst sie. Optional werden die
    Rohantworten in ``capture_dir`` gespeichert (zum Justieren des Mappings).
    """
    from playwright.sync_api import sync_playwright

    use_cdp = bool(account.cdp_url)
    profile = Path(account.profile_dir)
    if not use_cdp and not profile.exists():
        raise RuntimeError(
            "Kein Browser-Profil gefunden. Bitte zuerst 'python main.py account login' ausführen "
            "oder cdp_url setzen und dich in deinem eigenen Chrome einloggen."
        )

    captured: list[Any] = []
    cap_path = Path(capture_dir) if capture_dir else None
    if cap_path:
        cap_path.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = None
        if use_cdp:
            # An bereits laufenden, eingeloggten Chrome anbinden (kein Bot-Browser).
            browser = p.chromium.connect_over_cdp(account.cdp_url)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        else:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=account.headless,
                executable_path=account.executable_path or None,
                user_agent=_USER_AGENT,
                args=_LAUNCH_ARGS,
            )

        def on_response(resp):  # noqa: ANN001
            try:
                if not _looks_interesting(resp.url):
                    return
                ctype = resp.headers.get("content-type", "")
                if "json" not in ctype:
                    return
                body = resp.json()
                captured.append(body)
                if cap_path:
                    idx = len(captured)
                    safe = re.sub(r"[^a-z0-9]+", "_", resp.url.lower())[:60]
                    (cap_path / f"{idx:02d}_{safe}.json").write_text(
                        json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
            except Exception:
                pass  # nicht-JSON oder abgebrochene Antworten ignorieren

        # Bei CDP eine eigene Seite öffnen (bestehende Tabs des Nutzers nicht stören).
        page = ctx.new_page() if use_cdp else (ctx.pages[0] if ctx.pages else ctx.new_page())
        page.on("response", on_response)
        _goto_with_retry(page, account.trips_url)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(4000)

        if use_cdp:
            page.close()          # nur unseren Tab schließen, Chrome des Nutzers bleibt offen
        else:
            ctx.close()

    bookings: list[dict] = []
    seen: set[tuple] = set()
    for body in captured:
        for b in parse_trips(body):
            key = (b["name"], b["checkin"], b["checkout"])
            if key not in seen:
                seen.add(key)
                bookings.append(b)
    return bookings
