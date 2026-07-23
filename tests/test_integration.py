"""Integrationstest: echter Browser gegen einen lokalen Fake-Server.

Verifiziert die komplette Browser-Mechanik OHNE Hotels.com und ohne echten
Login: ein lokaler HTTP-Server liefert eine „Meine Reisen“-Seite, die – wie
die echte Seite – die Buchungen per GraphQL-XHR nachlädt, sowie eine
Hotel-Seite mit Preisen. Getestet werden die echten Funktionen
``fetch_account_bookings`` und ``fetch_price``.

Übersprungen, wenn Playwright/Chromium nicht verfügbar ist. Ein bereits
installiertes Chrome kann über ``REBOOKING_TEST_CHROME`` angegeben werden::

    REBOOKING_TEST_CHROME=/opt/pw-browsers/chromium python -m pytest -q tests/test_integration.py
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("playwright")

from rebooking.account import fetch_account_bookings
from rebooking.config import AccountConfig, ScraperConfig
from rebooking.models import Booking
from rebooking.scraper import fetch_price


def _resolve_chrome() -> str:
    env = os.environ.get("REBOOKING_TEST_CHROME")
    if env and os.path.exists(env):
        return env
    if os.path.exists("/opt/pw-browsers/chromium"):
        return "/opt/pw-browsers/chromium"
    return ""  # Playwright-Standardbrowser verwenden (falls installiert)


CHROME = _resolve_chrome()

# Die echte Kontoansicht ist ein EGDS-Komponentenbaum: die Übersicht liefert
# Buchungskarten mit Detail-Link, die Preise stehen erst auf der Detailseite.
# Texte bewusst auf Deutsch – so deckt der Test die deutsche Locale mit ab.
def _trips_json(base: str) -> dict:
    def card(pid: str, name: str, ort: str, zeitraum: str, detail: str, prop_num: str) -> dict:
        return {
            "__typename": "TripsUIBookedItemCard",
            "identifier": pid,
            "primary": name,
            "cardAction": {"resource": {"__typename": "HttpURI", "value": f"{base}{detail}"}},
            "enrichedSecondaries": [
                {"text": zeitraum, "__typename": "EGDSPlainText"},
                {"text": ort, "graphic": {"id": "place", "description": "Ort"},
                 "__typename": "EGDSGraphicText"},
            ],
            "media": {"url": f"https://images.trvl-media.com/lodging/1000/2000/3000/{prop_num}/x.jpg"},
        }

    return {"data": {"tripsView": {"sections": [{"items": [
        card("eg:property:v2:AAA", "Hotel Adlon Berlin", "Mitte",
             "von 15. September, 15:00 Uhr bis 18. September, 11:00 Uhr",
             "/trips/details/adlon", "123456"),
        card("eg:property:v2:BBB", "Strandhotel Sylt", "Westerland",
             "von 25. Sept., 16:00 Uhr bis 27. Sept., 11:00 Uhr",
             "/trips/details/sylt", "999888"),
        # Zeitraum unlesbar: darf NICHT stillschweigend den einer anderen
        # Buchung erben (genau dieser Fehler trat live auf).
        card("eg:property:v2:CCC", "Pension Ohne Datum", "Nirgendwo",
             "Zeitraum wird nachgereicht", "/trips/details/ohnedatum", "555000"),
    ]}]}}}


# Detailseite: Gesamtpreis ist BOLD, die Anzahlung REGULAR (darf nicht gewinnen).
_DETAIL_JSON = {
    "adlon": {"total": "540,00 €", "due": "54,00 €",
              "storno": "Kostenlose Stornierung bis 13. September"},
    "sylt": {"total": "820,00 €", "due": "82,00 €",
             "storno": "Diese Rate ist nicht erstattbar"},
    "ohnedatum": {"total": "300,00 €", "due": "30,00 €",
                  "storno": "Stornierungen und Änderungen"},
}


def _detail_json(key: str) -> dict:
    d = _DETAIL_JSON[key]
    return {"data": {"tripDetails": {"elements": [
        {"__typename": "TripDetailsUIPricingSummary",
         "label": {"stylizedText": "Gesamtpreis"},
         "value": {"stylizedText": d["total"], "weight": "BOLD"}},
        {"__typename": "TripDetailsUIPricingSummary",
         "label": {"stylizedText": "Jetzt fällig"},
         "value": {"stylizedText": d["due"], "weight": "REGULAR"}},
        {"__typename": "EGDSPlainText", "text": d["storno"]},
        {"__typename": "EGDSPlainText", "text": "Bezahlt am 17. Juni 2026"},
    ]}}}


TRIPS_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Meine Reisen</title></head>
<body><div id="app">…</div>
<script>fetch('/graphql',{method:'POST'}).then(r=>r.json()).then(d=>{
document.getElementById('app').textContent='ok '+JSON.stringify(d).length;});</script>
</body></html>"""

# Die echte Hotel-URL steht NUR als Link im DOM der Detailseite – nicht in den
# JSON-Antworten und nicht im Bildpfad. Neben Ablenkungs-Links (co…, App-Store).
DETAIL_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Buchung</title></head>
<body><div id="app">…</div>
<a href="https://hotelsapp.onelink.me/fSyN?pid=BRAND">App holen</a>
<a href="https://de.hotels.com/co10233060/hotels-deutschland/">Hotels in Deutschland</a>
<a href="https://de.hotels.com/ho__PROPID__/hotel-slug/">Unterkunft ansehen</a>
<script>fetch('/graphql-details'+location.pathname.replace('/trips/details','')).then(r=>r.json())
.then(d=>{document.getElementById('app').textContent='ok';});</script>
</body></html>"""

# propertyId je Buchung – bewusst anders als die Zahl im Bildpfad.
_PROP_IDS = {"adlon": "1347489024", "sylt": "2255667788", "ohnedatum": "9988776655"}

# Ausgebuchte Unterkunft: Hotels.com zeigt trotzdem Preise – die gehören aber
# zu vorgeschlagenen ANDEREN Hotels und dürfen nie verglichen werden.
SOLDOUT_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Hotel</title></head>
<body><h2>Bei uns ausgebucht</h2>
<p>Ähnliche Unterkünfte wie Testhotel</p>
<div data-stid="section-room-list">
  <div data-stid="price-summary">
    <span>Der aktuelle Preis beträgt 149 €.</span><span>149 €</span>
    <span>für 1 Zimmer, 2 Nächte</span>
  </div>
</div>
</body></html>"""

# Seite mit App-Hinweis-Overlay, wie es Hotels.com beim ersten Aufruf zeigt.
POPUP_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Reisen</title></head>
<body><h1 id="inhalt">Meine Reisen</h1>
<div role="dialog" aria-modal="true" id="appsheet"
     style="position:fixed;inset:0;background:#fff;z-index:99">
  <p>Hol dir die App!</p>
  <button onclick="alert('gebucht')">Buchung stornieren</button>
  <button aria-label="Nicht jetzt" onclick="document.getElementById('appsheet').remove()">
    Nicht jetzt</button>
</div></body></html>"""

# Echte Struktur: die Zimmer DIESER Unterkunft stehen in section-room-list,
# darunter folgen Vorschläge fremder Hotels. Jeder Block nennt Vorher-Preis,
# aktuellen Gesamtpreis und Nachtpreis – nur der mittlere ist vergleichbar.
HOTEL_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Hotel</title></head>
<body>
<div data-stid="content-hotel-lead-price"><span>ab 244,00 €</span></div>
<div data-stid="section-room-list">
  <div data-stid="price-summary">
    <span>Der vorherige Preis war 759 €.</span><span>759 €</span>
    <span>Der aktuelle Preis beträgt 489 €.</span><span>489 €</span>
    <span>für 1 Zimmer, 2 Nächte</span><span>244 € pro Nacht</span>
  </div>
  <div data-stid="price-summary">
    <span>Der aktuelle Preis beträgt 512 €.</span><span>512 €</span>
    <span>für 1 Zimmer, 2 Nächte</span><span>256 € pro Nacht</span>
  </div>
</div>
<div data-stid="section-similar-properties">
  <div data-stid="price-summary">
    <span>Der Preis beträgt 399 €</span><span>399 €</span>
    <span>für 1 Zimmer, 2 Nächte</span><span>PLAZA Fremdhotel</span>
  </div>
</div>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: ANN002
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def _base(self) -> str:
        return f"http://{self.headers.get('Host', '127.0.0.1')}"

    def do_GET(self):  # noqa: N802
        # Query abschneiden – die URLs tragen ?locale=de_DE.
        pfad = self.path.split("?", 1)[0]
        if pfad.startswith("/graphql-details/"):
            key = pfad.rsplit("/", 1)[-1]
            if key in _DETAIL_JSON:
                self._send(200, "application/json", json.dumps(_detail_json(key)))
            else:
                self._send(404, "text/plain", "nope")
        elif pfad.startswith("/trips/details/"):
            key = pfad.rsplit("/", 1)[-1]
            self._send(200, "text/html; charset=utf-8",
                       DETAIL_HTML.replace("__PROPID__", _PROP_IDS.get(key, "0")))
        elif pfad.startswith("/trips"):
            self._send(200, "text/html; charset=utf-8", TRIPS_HTML)
        elif self.path.startswith("/popup"):
            self._send(200, "text/html; charset=utf-8", POPUP_HTML)
        elif pfad.startswith("/hotel-ausgebucht"):
            self._send(200, "text/html; charset=utf-8", SOLDOUT_HTML)
        elif pfad.startswith("/hotel"):
            self._send(200, "text/html; charset=utf-8", HOTEL_HTML)
        else:
            self._send(404, "text/plain", "nope")

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/graphql"):
            self._send(200, "application/json", json.dumps(_trips_json(self._base())))
        else:
            self._send(404, "text/plain", "nope")


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _skip_if_no_browser(exc: Exception):
    msg = str(exc)
    if "Executable doesn't exist" in msg or "playwright install" in msg:
        pytest.skip("Kein Chromium verfügbar – Integrationstest übersprungen.")
    raise exc


def test_account_import_end_to_end(server):
    profile = tempfile.mkdtemp(prefix="pwprofile_")
    acc = AccountConfig(
        profile_dir=profile,
        trips_url=f"{server}/trips",
        login_url=f"{server}/login",
        headless=True,
        executable_path=CHROME,
    )
    try:
        bookings = fetch_account_bookings(acc)
    except Exception as exc:  # noqa: BLE001
        _skip_if_no_browser(exc)

    names = {b["name"] for b in bookings}
    assert names == {"Hotel Adlon Berlin", "Strandhotel Sylt", "Pension Ohne Datum"}

    adlon = next(b for b in bookings if b["name"] == "Hotel Adlon Berlin")
    assert adlon["checkin"] == "2026-09-15"
    assert adlon["checkout"] == "2026-09-18"
    # Gesamtpreis (BOLD), nicht die Anzahlung von 54,00 €.
    assert adlon["paid_price"] == 540.0
    assert adlon["currency"] == "EUR"
    # Echter Link aus dem DOM – nicht die Zahl aus dem Bildpfad (die ergab 404),
    # und nicht der co…-Länderlink daneben.
    assert adlon["url"] == "https://de.hotels.com/ho1347489024/hotel-slug/"
    assert adlon["location"] == "Mitte"
    assert adlon["paid_on"] == "2026-06-17"
    assert adlon["free_cancellation"] is True

    # Jede Buchung muss ihren EIGENEN Zeitraum bekommen, nicht den der ersten.
    sylt = next(b for b in bookings if b["name"] == "Strandhotel Sylt")
    assert sylt["checkin"] == "2026-09-25"
    assert sylt["checkout"] == "2026-09-27"
    assert sylt["paid_price"] == 820.0
    assert sylt["location"] == "Westerland"
    assert sylt["free_cancellation"] is False

    # Unlesbarer Zeitraum -> leer lassen, NICHT den einer anderen Buchung erben.
    ohne = next(b for b in bookings if b["name"] == "Pension Ohne Datum")
    assert ohne["checkin"] == ""
    assert ohne["checkout"] == ""
    assert ohne["checkin"] != adlon["checkin"]
    # Preis wird trotzdem gelesen; nur der Zeitraum fehlt.
    assert ohne["paid_price"] == 300.0
    # Ohne klare Aussage bleibt die Stornierbarkeit unbekannt.
    assert ohne["free_cancellation"] is None


def test_overlay_is_dismissed_without_cancelling_booking(server):
    """Der App-Hinweis muss unbeaufsichtigt verschwinden.

    Wichtig: der Dialog enthält absichtlich auch „Buchung stornieren“ – dieser
    Button darf unter keinen Umständen geklickt werden.
    """
    from playwright.sync_api import sync_playwright

    from rebooking.account import _dismiss_overlays

    alerts: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=CHROME or None)
            page = browser.new_page()
            page.on("dialog", lambda d: (alerts.append(d.message), d.dismiss()))
            page.goto(f"{server}/popup", wait_until="domcontentloaded")
            assert page.locator("#appsheet").is_visible()

            dismissed = _dismiss_overlays(page, verbose=True)
            page.wait_for_timeout(500)

            assert dismissed is not None, "Overlay wurde nicht erkannt"
            assert page.locator("#appsheet").count() == 0, "Overlay noch da"
            assert page.locator("#inhalt").is_visible(), "Seiteninhalt muss erreichbar sein"
            assert alerts == [], f"Es wurde ein gefährlicher Button geklickt: {alerts}"

            # Ohne Overlay darf die Funktion nichts tun und nicht scheitern.
            assert _dismiss_overlays(page) is None
            browser.close()
    except Exception as exc:  # noqa: BLE001
        _skip_if_no_browser(exc)


def test_price_scraper_end_to_end(server):
    booking = Booking(
        name="Hotel Adlon", url=f"{server}/hotel",
        checkin="2099-09-15", checkout="2099-09-18", paid_price=600.0,
    )
    sc = ScraperConfig(headless=True, override_url_dates=False,
                       timeout_ms=30000, executable_path=CHROME)
    try:
        result = fetch_price(booking, sc)
    except Exception as exc:  # noqa: BLE001
        _skip_if_no_browser(exc)

    if not result.ok:
        _skip_if_no_browser(RuntimeError(result.error or ""))
    # 489 = günstigster aktueller GESAMTpreis der eigenen Zimmerliste.
    # NICHT 244 (Nachtpreis), nicht 759 (durchgestrichen), nicht 399
    # (vorgeschlagenes Fremdhotel außerhalb der Zimmerliste).
    assert result.current_price == 489.0
    assert result.is_cheaper
    assert result.savings == 111.0


def test_price_scraper_erkennt_ausgebucht(server):
    """Bei ausgebuchter Unterkunft darf kein Preis gemeldet werden."""
    booking = Booking(
        name="Ausgebucht", url=f"{server}/hotel-ausgebucht",
        checkin="2099-09-15", checkout="2099-09-18", paid_price=600.0,
    )
    sc = ScraperConfig(headless=True, override_url_dates=False,
                       timeout_ms=30000, executable_path=CHROME)
    try:
        result = fetch_price(booking, sc)
    except Exception as exc:  # noqa: BLE001
        _skip_if_no_browser(exc)

    assert result.ok is False
    assert "ausgebucht" in (result.error or "").lower()
    assert result.current_price is None
