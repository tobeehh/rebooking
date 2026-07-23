"""Tests für die reinen Logik-Bausteine (ohne Browser/Netzwerk)."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import pytest

from rebooking.config import Config, _interpolate_env
from rebooking.models import Booking, PriceResult
from rebooking.notifier import build_message, should_alert
from rebooking.config import NotificationConfig, ScraperConfig
from rebooking.scraper import _to_float, build_url, extract_prices_from_text
from rebooking.storage import PriceHistory


# --- models ---------------------------------------------------------------

def _booking(**kw):
    base = dict(
        name="Hotel Test",
        url="https://www.hotels.com/ho1/",
        checkin="2099-01-01",
        checkout="2099-01-04",
        paid_price=300.0,
    )
    base.update(kw)
    return Booking(**base)


def test_booking_nights_and_id():
    b = _booking()
    assert b.nights == 3
    assert len(b.id) == 12
    # ID stabil bei gleichen Eckdaten
    assert b.id == _booking().id
    # ID ändert sich bei anderem Zeitraum
    assert b.id != _booking(checkout="2099-01-05").id


def test_booking_checkout_must_be_after_checkin():
    with pytest.raises(ValueError):
        _booking(checkin="2099-01-04", checkout="2099-01-01")


def test_booking_active_flag():
    past = _booking(checkin="2000-01-01", checkout="2000-01-02")
    assert not past.is_active
    future = _booking()
    assert future.is_active


def test_price_result_savings():
    b = _booking(paid_price=300.0)
    r = PriceResult(booking=b, checked_at=datetime.now(), current_price=250.0, currency="EUR")
    assert r.savings == 50.0
    assert r.savings_percent == 16.7
    assert r.is_cheaper
    r2 = PriceResult(booking=b, checked_at=datetime.now(), current_price=320.0, currency="EUR")
    assert not r2.is_cheaper


# --- config ---------------------------------------------------------------

def test_env_interpolation(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "s3cr3t")
    data = {"password": "${MY_SECRET}", "nested": {"x": ["${MY_SECRET}"]}}
    out = _interpolate_env(data)
    assert out["password"] == "s3cr3t"
    assert out["nested"]["x"][0] == "s3cr3t"


def test_config_load(tmp_path, monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "pw123")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
currency: EUR
notifications:
  channel: email
  email:
    password: ${SMTP_PASSWORD}
bookings:
  - name: A
    url: https://www.hotels.com/ho1/
    checkin: "2099-05-01"
    checkout: "2099-05-03"
    paid_price: 200
""",
        encoding="utf-8",
    )
    cfg = Config.load(cfg_file)
    assert cfg.currency == "EUR"
    assert cfg.notifications.email["password"] == "pw123"
    assert len(cfg.bookings) == 1
    assert cfg.bookings[0].nights == 2


# --- scraper helpers ------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("540", 540.0),
        ("540,00", 540.0),
        ("2.000", 2000.0),
        ("99,90", 99.90),
    ],
)
def test_to_float(raw, expected):
    assert _to_float(raw) == expected


def test_extract_prices_from_text():
    text = "Ab 540 € pro Nacht. Gesamt 1.620,00 € für 3 Nächte. Gebühren 3 €."
    prices = extract_prices_from_text(text)
    assert 540.0 in prices
    assert 1620.0 in prices
    # 3 € liegt unter der Plausibilitätsschwelle (>=5)
    assert 3.0 not in prices


def test_build_url_sets_dates():
    b = _booking(url="https://www.hotels.com/ho9/?foo=bar", adults=2, children=1, child_ages=[7])
    url = build_url(b, ScraperConfig())
    assert "chkin=2099-01-01" in url
    assert "chkout=2099-01-04" in url
    assert "rm1=a2%3A7" in url or "rm1=a2:7" in url
    assert "foo=bar" in url


# --- notifier -------------------------------------------------------------

def test_should_alert_thresholds():
    b = _booking(paid_price=300.0)
    cfg = NotificationConfig(min_drop_absolute=10.0, min_drop_percent=2.0)
    # 5 EUR Ersparnis -> unter absolutem Schwellwert
    r = PriceResult(booking=b, checked_at=datetime.now(), current_price=295.0, currency="EUR")
    assert not should_alert(r, cfg)
    # 30 EUR (10 %) -> über beiden Schwellen
    r2 = PriceResult(booking=b, checked_at=datetime.now(), current_price=270.0, currency="EUR")
    assert should_alert(r2, cfg)
    # teurer -> nie
    r3 = PriceResult(booking=b, checked_at=datetime.now(), current_price=350.0, currency="EUR")
    assert not should_alert(r3, cfg)


def test_build_message():
    b = _booking(paid_price=300.0, name="Hotel X")
    r = PriceResult(booking=b, checked_at=datetime.now(), current_price=250.0, currency="EUR")
    subject, body = build_message([r])
    assert "Hotel X" in subject
    assert "Hotel X" in body
    assert "250" in body


# --- storage --------------------------------------------------------------

# --- account / trips parser ----------------------------------------------

from rebooking.account import _coerce_date, parse_trips


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-09-15", "2026-09-15"),
        ("2026-09-15T14:00:00Z", "2026-09-15"),
        ({"isoDate": "2026-09-15"}, "2026-09-15"),
        ({"year": 2026, "month": 9, "day": 15}, "2026-09-15"),
        ({"epochSeconds": 1789000000}, "2026-09-10"),
        (None, None),
        ("kein datum", None),
    ],
)
def test_coerce_date(value, expected):
    assert _coerce_date(value) == expected


def test_parse_trips_nested_graphql_like():
    # Nachgebildete, verschachtelte Antwort wie sie eine GraphQL-API liefern könnte.
    data = {
        "data": {
            "trips": {
                "items": [
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
                ]
            }
        }
    }
    trips = parse_trips(data)
    names = {t["name"] for t in trips}
    assert names == {"Hotel Adlon Berlin", "Strandhotel Sylt"}

    adlon = next(t for t in trips if t["name"] == "Hotel Adlon Berlin")
    assert adlon["checkin"] == "2026-09-15"
    assert adlon["checkout"] == "2026-09-18"
    assert adlon["paid_price"] == 540.0
    assert adlon["url"] == "https://www.hotels.com/ho123456/"

    sylt = next(t for t in trips if t["name"] == "Strandhotel Sylt")
    assert sylt["paid_price"] == 820.0
    assert sylt["url"] == "https://www.hotels.com/ho999/"


def test_egds_extractors():
    from rebooking.account import (
        _extract_cards, _find_total_price, _find_date_range, _find_paid_on,
        _find_cancellation, _parse_money, _parse_date_range,
    )

    card = {
        "__typename": "TripsUIBookedItemCard", "identifier": "eg:property:v2:HASH",
        "primary": "Die Sonne Nollingen",
        "cardAction": {"resource": {"value": "https://www.hotels.com/trips/egti-0CJ/details/ABC"}},
        "enrichedSecondaries": [{"text": "Rheinfelden", "graphic": {"description": "Location"}}],
        "media": {"url": "https://images.trvl-media.com/lodging/91000000/90830000/90827800/90827734/x.jpg"},
    }
    cards = _extract_cards([{"x": card}])
    assert cards[0]["name"] == "Die Sonne Nollingen"
    assert cards[0]["property_num"] == "90827734"
    assert cards[0]["detail_url"].endswith("/details/ABC")

    pricing = {"__typename": "TripDetailsUIPricingSummary",
               "accessibility": "Total price is €194.64",
               "label": {"stylizedText": "Total price"}, "value": {"stylizedText": "€194.64"}}
    assert _find_total_price([{"a": pricing}]) == (194.64, "EUR")

    assert _find_paid_on([{"t": {"text": "Paid on Jun 17, 2026"}}]) == "2026-06-17"
    assert _find_date_range([{"t": {"text": "Jul 20 at 2:00pm - Jul 22 at 10:00am"}}]) == ("2026-07-20", "2026-07-22")

    assert _find_cancellation([{"t": {"text": "Free cancellation until Jul 18"}}])["free"] is True
    assert _find_cancellation([{"t": {"text": "This rate is non-refundable"}}])["free"] is False

    assert _parse_money("€1.234,56") == (1234.56, "EUR")
    assert _parse_money("$180.00") == (180.0, "USD")
    # Jahresübergang Dez -> Jan
    assert _parse_date_range("Dec 30 at 3pm - Jan 2 at 11am")[0].endswith("-12-30")


def test_egds_extractors_german_locale():
    """Deutsche Kontoansicht: Datum mit 'bis', Preis mit Symbol dahinter."""
    from rebooking.account import _find_cancellation, _parse_date_range, _parse_money

    # Zeitraum, wie ihn de.hotels.com auf der Buchungskarte schreibt.
    assert _parse_date_range("von 20. Juli, 14:00 Uhr bis 22. Juli, 10:00 Uhr") == (
        "2026-07-20", "2026-07-22")
    assert _parse_date_range("20. Juli 2026 – 22. Juli 2026") == ("2026-07-20", "2026-07-22")
    # Jahresübergang: Rückgabedatum muss ins Folgejahr rutschen.
    assert _parse_date_range("28. Dezember 2026 bis 2. Januar 2027") == (
        "2026-12-28", "2027-01-02")

    # Deutsche Monatsabkürzungen aus der echten Kontoansicht. "Sept." ist der
    # Sonderfall: englisch wäre "Sep", die deutsche Seite schreibt vier Zeichen.
    assert _parse_date_range("von 25. Sept., 16:00 Uhr bis 27. Sept., 11:00 Uhr") == (
        "2026-09-25", "2026-09-27")
    assert _parse_date_range("von 31. Juli, 15:00 Uhr bis 2. Aug., 12:00 Uhr") == (
        "2026-07-31", "2026-08-02")
    assert _parse_date_range("von 28. Juli, 15:00 Uhr bis 30. Juli, 12:00 Uhr") == (
        "2026-07-28", "2026-07-30")

    # Währungssymbol steht im Deutschen hinter dem Betrag.
    assert _parse_money("194,64 €") == (194.64, "EUR")
    assert _parse_money("1.234,56 €") == (1234.56, "EUR")
    assert _parse_money("194,64 EUR") == (194.64, "EUR")
    assert _parse_money("89 €") == (89.0, "EUR")
    # Englische Schreibweise darf dadurch nicht kaputtgehen.
    assert _parse_money("€194.64") == (194.64, "EUR")

    assert _find_cancellation([{"t": {"text": "Kostenlose Stornierung bis 18. Juli"}}])["free"] is True
    assert _find_cancellation([{"t": {"text": "Diese Rate ist nicht erstattbar"}}])["free"] is False
    # Ohne klare Aussage bleibt die Stornierbarkeit unbekannt (nicht True raten).
    assert _find_cancellation([{"t": {"text": "Stornierungen und Änderungen"}}])["free"] is None


def test_parse_cancellation_page_echte_texte():
    """Wortlaut 1:1 von de.hotels.com/booking-servicing/lodging."""
    from rebooking.account import parse_cancellation_page

    erstattbar = (
        "Stornierungen\n"
        "Du erhältst eine volle Rückerstattung, wenn du vor dem 26. Juli 2026, "
        "18:00 Uhr (Ortszeit), stornierst.\n"
        "Vollständige Rückerstattung\nTeilerstattung\n"
        "Vollständige Rückerstattung ab heute bis zum 26. Juli.\n"
        "Teilerstattung vom 26. Juli bis zum Check-in.\n"
    )
    got = parse_cancellation_page(erstattbar)
    assert got["free"] is True
    assert got["free_until"] == "2026-07-26"

    nicht = (
        "Stornierungen\n"
        "Wenn du deinen Aufenthalt stornierst, erhältst du keine Rückerstattung.\n"
        "Keine Rückerstattung\n"
        "Keine Rückerstattung ab heute bis zum Check-in.\n"
    )
    got = parse_cancellation_page(nicht)
    assert got["free"] is False
    assert got["free_until"] is None

    # Nur noch Teilerstattung -> zum kostenlosen Umbuchen unbrauchbar.
    teil = "Teilerstattung ab heute bis zum Check-in.\n"
    assert parse_cancellation_page(teil)["free"] is False

    # Leerer/unbekannter Text darf nichts behaupten.
    assert parse_cancellation_page("")["free"] is None
    assert parse_cancellation_page("Verwalte deine Unterkunft")["free"] is None


def test_parse_cancellation_prefers_current_phase():
    """Die JETZT geltende Phase zählt, nicht eine spätere.

    Kommt „Teilerstattung“ im Text vor, die Buchung ist heute aber noch voll
    erstattbar, muss True herauskommen – sonst würde eine umbuchbare Buchung
    fälschlich aussortiert.
    """
    from rebooking.account import parse_cancellation_page

    text = ("Vollständige Rückerstattung\nTeilerstattung\n"
            "Vollständige Rückerstattung ab heute bis zum 26. Juli.\n"
            "Teilerstattung vom 26. Juli bis zum Check-in.\n")
    assert parse_cancellation_page(text)["free"] is True


def test_extract_current_total_echter_angebotsblock():
    """Wortlaut 1:1 von de.hotels.com (Mimosa Suites, Sylt).

    Der Block nennt drei Zahlen; nur 391 € ist der Vergleichswert. 195 € ist
    der Nachtpreis – den würde ein naives min() wählen und dauerhaft falsche
    „günstiger“-Meldungen erzeugen.
    """
    from rebooking.scraper import extract_current_total

    block = (
        "5% Rabatt\n"
        "Der vorherige Preis war 759 €.\n"
        "759 €\n"
        "Der aktuelle Preis beträgt 391 €.\n"
        "391 €\n"
        "für 1 Apartment, 2 Nächte\n"
        "195 € pro Nacht\n"
        "inkl. Steuern & Gebühren\n"
        "Reservieren"
    )
    assert extract_current_total(block) == 391.0

    # Ohne Rabatt-Vorpreis.
    assert extract_current_total("Der Preis beträgt 625 €\n625 €\nfür 1 Zimmer, 2 Nächte") == 625.0

    # Nur durchgestrichener Preis und Nachtpreis -> kein Vergleichswert.
    assert extract_current_total("Der vorherige Preis war 759 €.\n195 € pro Nacht") is None
    assert extract_current_total("") is None

    # Tausendertrennzeichen.
    assert extract_current_total("Der aktuelle Preis beträgt 1.234,56 €") == 1234.56


def test_is_signed_out():
    """Abgemeldete Seiten müssen erkannt werden.

    Ohne Login liefert Hotels.com Listen- statt Mitgliederpreise (gemessen:
    759 € statt 391 €). Unerkannt würde der Monitor nie mehr eine Ersparnis
    melden – ohne dass ein Fehler sichtbar wird.
    """
    from rebooking.scraper import is_signed_out

    # Kopfbereich abgemeldet (Wortlaut von de.hotels.com).
    assert is_signed_out("Hilfe\nMeine Reisen\nAnmelden\n"
                         "Sichere dir Sofortrabatte dank Preisen für Mitglieder")
    assert is_signed_out("Help\nMy Trips\nSign in")
    # Eingeloggt: Kontoname statt Anmelde-Aufforderung.
    assert not is_signed_out("Hilfe\nMeine Reisen\nTobias\n4 von 10 Nächten")
    assert not is_signed_out("")


def test_is_sold_out():
    """Ausgebucht muss erkannt werden – sonst werden Fremdpreise verglichen.

    Bei ausgebuchter Unterkunft zeigt Hotels.com die Preise ÄHNLICHER Hotels.
    Ohne diese Erkennung meldet der Monitor „günstiger“ für ein Zimmer, das
    gar nicht buchbar ist.
    """
    from rebooking.scraper import is_sold_out

    assert is_sold_out("Bei uns ausgebucht\nÄhnliche Unterkünfte wie Holiday Inn")
    assert is_sold_out("We are sold out")
    assert is_sold_out("Keine Verfügbarkeit für diese Daten")
    # Normale Hotelseite mit Preisen darf NICHT als ausgebucht gelten.
    assert not is_sold_out("Standardzimmer, 1 Queen-Bett\n107 € pro Nacht\ninkl. Steuern")
    assert not is_sold_out("")


def test_property_href_erkennung():
    """Nur echte /ho<id>/-Hotelseiten gelten als Hotel-URL.

    Die früher aus dem Bildpfad geratene Nummer ergab durchweg 404 – deshalb
    darf ausschließlich der echte Link aus dem DOM verwendet werden.
    """
    from rebooking.account import _PROPERTY_HREF_RE

    assert _PROPERTY_HREF_RE.match(
        "https://de.hotels.com/ho1347489024/the-niu-loco-munchen-deutschland/")
    assert _PROPERTY_HREF_RE.match("https://www.hotels.com/ho123456/")
    # Länder-/Städteseiten (co…) und Fremdlinks dürfen NICHT greifen.
    assert not _PROPERTY_HREF_RE.match("https://de.hotels.com/co10233060/hotels-deutschland/")
    assert not _PROPERTY_HREF_RE.match("https://de.hotels.com/account")
    assert not _PROPERTY_HREF_RE.match("https://hotelsapp.onelink.me/fSyN?pid=BRAND")
    assert not _PROPERTY_HREF_RE.match("https://de.hotels.com/trips/egti-TJW/details/ABC")


def test_servicing_url_aus_kartendaten():
    """Die Verwaltungs-URL wird aus Detail-Link + Reiseplan-Nummer gebaut."""
    from rebooking.account import _servicing_url

    detail = ("https://www.hotels.com/trips/egti-TJW-XGD-4W9W/details/ZGUzNjZmMDgtZmZhMw")
    url = _servicing_url(detail, "72077068384394", "de-DE")
    assert "booking-servicing/lodging" in url
    assert "tripViewId=egti-TJW-XGD-4W9W" in url
    assert "encodedTripItemId=ZGUzNjZmMDgtZmZhMw" in url
    assert "tripId=72077068384394" in url
    assert "locale=de_DE" in url

    # Ohne Reiseplan-Nummer oder unpassenden Link: keine URL raten.
    assert _servicing_url(detail, "") is None
    assert _servicing_url("https://www.hotels.com/ho123/", "72077068384394") is None


def test_localized_url():
    """Die Sprache muss über die URL erzwingbar sein.

    Die Domain allein genügt nicht: de.hotels.com liefert ohne locale-Parameter
    weiterhin Englisch (und teils USD). Im CDP-Modus ist die URL zudem der
    einzige Hebel, weil dort kein Accept-Language gesetzt werden kann.
    """
    from rebooking.account import _localized_url

    assert _localized_url("https://www.hotels.com/trips", "de-DE") == \
        "https://www.hotels.com/trips?locale=de_DE"
    # Bestehende Query-Parameter bleiben erhalten.
    assert _localized_url("https://www.hotels.com/trips?a=1", "de-DE") == \
        "https://www.hotels.com/trips?a=1&locale=de_DE"
    # Schon vorhandene Sprache wird nicht doppelt angehängt.
    unchanged = "https://de.hotels.com/trips?locale=de_DE&siteid=300000752"
    assert _localized_url(unchanged, "de-DE") == unchanged
    # Ohne konfigurierte Locale bleibt die URL unangetastet.
    assert _localized_url("https://www.hotels.com/trips", "") == "https://www.hotels.com/trips"


def test_dismiss_patterns_never_cancel_a_booking():
    """Das Popup-Wegklicken darf niemals eine Buchung stornieren."""
    from rebooking.account import _DISMISS_PATTERNS

    for gefaehrlich in ("stornieren", "buchung stornieren", "cancel booking", "cancel"):
        assert not any(p in gefaehrlich for p in _DISMISS_PATTERNS), \
            f"{gefaehrlich!r} würde angeklickt werden"
    # Harmlose Schließen-Beschriftungen müssen dagegen greifen.
    for harmlos in ("schließen", "close", "nicht jetzt", "no thanks", "weiter im browser"):
        assert any(p in harmlos for p in _DISMISS_PATTERNS)


def test_extract_cards_merges_variants_and_reads_dates():
    """Dieselbe Buchung kommt in mehreren Kartenvarianten – Felder zusammenführen."""
    from rebooking.account import _extract_cards

    # Variante 1: Detail-Link + Zeitraum als Fließtext, kein Ort.
    v1 = {
        "__typename": "TripsUIBookedItemCard", "identifier": "eg:property:v2:HASH",
        "primary": "Die Sonne Nollingen",
        "cardAction": {"resource": {"value": "https://www.hotels.com/trips/egti-0CJ/details/ABC"}},
        "enrichedSecondaries": [
            {"text": "von 20. Juli, 14:00 Uhr bis 22. Juli, 10:00 Uhr"},
            {"text": "Buchungsnummer: 72075653118313"},
        ],
        "media": {"url": "https://images.trvl-media.com/lodging/91000000/90830000/90827800/90827734/x.jpg"},
    }
    # Variante 2: Ort per Icon, aber ohne Link/Zeitraum. Icon-ID ist sprachneutral.
    v2 = {
        "__typename": "TripsUIBookedItemCard", "identifier": "eg:property:v2:HASH",
        "primary": "Die Sonne Nollingen",
        "enrichedSecondaries": [
            {"text": "2 Nächte", "graphic": {"id": "timelapse", "description": "Dauer"}},
            {"text": "Rheinfelden", "graphic": {"id": "place", "description": "Ort"}},
        ],
    }
    cards = _extract_cards([{"a": v1}, {"b": v2}])
    assert len(cards) == 1
    card = cards[0]
    assert card["detail_url"].endswith("/details/ABC")   # aus Variante 1
    assert card["location"] == "Rheinfelden"             # aus Variante 2
    assert card["property_num"] == "90827734"
    assert card["dates"] == ("2026-07-20", "2026-07-22")
    # Die Nächtezahl darf nicht als Zeitraum missverstanden werden.
    assert card["dates"] != ("2 Nächte", "")


def test_parse_trips_ignores_incomplete_and_dedupes():
    data = {
        "a": {"name": "Ohne Datum"},  # kein Datum -> ignoriert
        "b": {"name": "Doppelt", "checkin": "2026-01-01", "checkout": "2026-01-03"},
        "c": {"name": "Doppelt", "checkInDate": "2026-01-01", "checkOutDate": "2026-01-03"},
        "d": {"name": "Falschrum", "checkin": "2026-01-05", "checkout": "2026-01-01"},
    }
    trips = parse_trips(data)
    assert len(trips) == 1
    assert trips[0]["name"] == "Doppelt"


def test_storage_roundtrip(tmp_path):
    b = _booking()
    h = PriceHistory(tmp_path)
    r1 = PriceResult(booking=b, checked_at=datetime.now(), current_price=280.0, currency="EUR")
    r2 = PriceResult(booking=b, checked_at=datetime.now(), current_price=260.0, currency="EUR")
    h.record(r1)
    h.record(r2)
    h.save()

    h2 = PriceHistory(tmp_path)
    assert h2.last_price(b.id) == 260.0
    assert h2.lowest_seen(b.id) == 260.0
    assert len(h2.history(b.id)) == 2
