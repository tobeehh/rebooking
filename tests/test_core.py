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
