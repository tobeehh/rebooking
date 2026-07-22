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

TRIPS_JSON = {
    "data": {"tripsView": {"sections": [{"items": [
        {
            "propertyName": "Hotel Adlon Berlin",
            "detailsUrl": "/ho123456/",
            "checkInDate": {"isoDate": "2026-09-15"},
            "checkOutDate": {"isoDate": "2026-09-18"},
            "price": {"total": {"amount": 540.0, "currency": "EUR"}},
        },
        {
            "propertyName": "Strandhotel Sylt",
            "propertyUrl": "https://www.hotels.com/ho999/",
            "startDate": "2026-10-01",
            "endDate": "2026-10-04",
            "grandTotal": {"amount": 820, "currency": "EUR"},
        },
    ]}]}}
}

TRIPS_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Meine Reisen</title></head>
<body><div id="app">…</div>
<script>fetch('/graphql',{method:'POST'}).then(r=>r.json()).then(d=>{
document.getElementById('app').textContent='ok '+JSON.stringify(d).length;});</script>
</body></html>"""

HOTEL_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Hotel</title></head>
<body><div data-stid="content-hotel-lead-price"><span>ab 489,00 €</span></div>
<div data-stid="price-summary">Standard 512,00 €</div></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: ANN002
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/trips"):
            self._send(200, "text/html; charset=utf-8", TRIPS_HTML)
        elif self.path.startswith("/hotel"):
            self._send(200, "text/html; charset=utf-8", HOTEL_HTML)
        else:
            self._send(404, "text/plain", "nope")

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/graphql"):
            self._send(200, "application/json", json.dumps(TRIPS_JSON))
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
    assert names == {"Hotel Adlon Berlin", "Strandhotel Sylt"}
    adlon = next(b for b in bookings if b["name"] == "Hotel Adlon Berlin")
    assert adlon["checkin"] == "2026-09-15"
    assert adlon["checkout"] == "2026-09-18"
    assert adlon["paid_price"] == 540.0
    assert adlon["url"] == "https://www.hotels.com/ho123456/"


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
    assert result.current_price == 489.0
    assert result.is_cheaper
    assert result.savings == 111.0
