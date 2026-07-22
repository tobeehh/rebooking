"""Preisabfrage bei Hotels.com via Playwright (echter Browser).

Hotels.com bietet keine öffentliche API und schützt sich gegen Bots. Wir
automatisieren daher einen echten Chromium-Browser, navigieren zur Hotelseite
mit den passenden Datums-/Belegungsparametern und lesen den niedrigsten
angezeigten Preis aus.

Die Extraktion ist bewusst mehrstufig und tolerant, weil Hotels.com sein
HTML regelmäßig ändert:

1. konfigurierbare CSS-Selektoren (``scraper.price_selectors``)
2. bekannte data-stid-Attribute
3. Regex-Fallback über den sichtbaren Seitentext (Währungsbeträge)

Hinweis: Findet die Extraktion nichts, liefert ``fetch_price`` einen Fehler
statt eines falschen Preises – lieber keine als eine falsche Benachrichtigung.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .config import ScraperConfig
from .models import Booking, PriceResult

# Standard-Selektoren (Stand 2026, können sich ändern – per Config überschreibbar).
_DEFAULT_SELECTORS = [
    '[data-stid="price-summary-message-line"]',
    '[data-stid="content-hotel-lead-price"]',
    'div[data-stid*="price"] .uitk-text',
    'span[aria-hidden="true"].uitk-text',
]

# Erkennt Beträge wie "1.234,56 €", "€ 1.234", "1,234.56", "123 EUR"
_PRICE_RE = re.compile(
    r"(?:€|EUR|\$|£|CHF)\s?([0-9][0-9\.\,   ]{1,12}[0-9])"
    r"|([0-9][0-9\.\,   ]{1,12}[0-9])\s?(?:€|EUR|\$|£|CHF)",
    re.IGNORECASE,
)


def _to_float(raw: str) -> float | None:
    """Wandelt einen lokalisierten Betragsstring in float um.

    Behandelt sowohl "1.234,56" (DE) als auch "1,234.56" (EN).
    """
    s = raw.strip().replace(" ", "").replace(" ", "").replace(" ", "")
    if not s:
        return None
    has_comma = "," in s
    has_dot = "." in s
    if has_comma and has_dot:
        # Das letzte Trennzeichen ist das Dezimaltrennzeichen.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        # Nur Komma: Dezimaltrennzeichen, wenn genau 2 Nachkommastellen, sonst Tausender.
        if re.match(r"^\d{1,3}(,\d{3})+$", s):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    else:
        # Nur Punkt(e): Tausenderpunkte entfernen, wenn Gruppen von 3 Stellen.
        if re.match(r"^\d{1,3}(\.\d{3})+$", s):
            s = s.replace(".", "")
    try:
        val = float(s)
    except ValueError:
        return None
    return val


def build_url(booking: Booking, scraper: ScraperConfig) -> str:
    """Setzt Datums- und Belegungsparameter in die Hotel-URL ein."""
    parsed = urlparse(booking.url)
    query = parse_qs(parsed.query)

    if scraper.override_url_dates:
        query["chkin"] = [booking.checkin.strftime("%Y-%m-%d")]
        query["chkout"] = [booking.checkout.strftime("%Y-%m-%d")]
        # Belegung: rm1=a{adults} optional mit Kinderaltern (rm1=a2:8,10)
        room = f"a{booking.adults}"
        if booking.children:
            ages = booking.child_ages or [8] * booking.children
            room += ":" + ",".join(str(a) for a in ages[: booking.children])
        for i in range(1, booking.rooms + 1):
            query[f"rm{i}"] = [room]

    new_query = urlencode({k: v[0] for k, v in query.items()})
    return urlunparse(parsed._replace(query=new_query))


def extract_prices_from_text(text: str) -> list[float]:
    """Findet alle plausiblen Preise in einem Text (Regex-Fallback)."""
    prices: list[float] = []
    for match in _PRICE_RE.finditer(text):
        raw = match.group(1) or match.group(2)
        if not raw:
            continue
        val = _to_float(raw)
        # Plausibilitätsfilter: Hotelnächte kosten üblicherweise >5 und <100000.
        if val is not None and 5 <= val <= 100000:
            prices.append(val)
    return prices


def _dismiss_cookie_banner(page) -> None:
    for sel in (
        'button:has-text("Accept")',
        'button:has-text("Akzeptieren")',
        'button:has-text("Alle akzeptieren")',
        '[data-stid="accept-cookie"]',
    ):
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _extract_prices(page, scraper: ScraperConfig) -> list[float]:
    prices: list[float] = []
    selectors = scraper.price_selectors or _DEFAULT_SELECTORS
    for sel in selectors:
        try:
            for el in page.query_selector_all(sel):
                txt = (el.inner_text() or "").strip()
                prices.extend(extract_prices_from_text(txt))
        except Exception:
            continue

    if not prices:
        # Fallback: gesamter sichtbarer Text.
        try:
            body = page.inner_text("body")
            prices.extend(extract_prices_from_text(body))
        except Exception:
            pass
    return prices


def fetch_price(booking: Booking, scraper: ScraperConfig) -> PriceResult:
    """Ruft den aktuell niedrigsten Preis für eine Buchung ab."""
    # Import hier, damit Tests/Config ohne installiertes Playwright laufen.
    from playwright.sync_api import sync_playwright

    url = build_url(booking, scraper)
    now = datetime.now()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=scraper.headless,
                executable_path=scraper.executable_path or None,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=scraper.user_agent,
                locale=scraper.locale,
                viewport={"width": 1366, "height": 900},
            )
            # Einfache Stealth-Anpassung: navigator.webdriver verstecken.
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=scraper.timeout_ms)
            _dismiss_cookie_banner(page)
            try:
                page.wait_for_load_state("networkidle", timeout=scraper.timeout_ms)
            except Exception:
                pass  # networkidle wird bei Tracking-Skripten oft nie erreicht.
            page.wait_for_timeout(2500)

            prices = _extract_prices(page, scraper)
            browser.close()

        if not prices:
            return PriceResult(
                booking=booking,
                checked_at=now,
                current_price=None,
                currency=booking.currency,
                ok=False,
                error="Kein Preis auf der Seite gefunden (evtl. Layout geändert oder blockiert).",
                source_url=url,
            )

        return PriceResult(
            booking=booking,
            checked_at=now,
            current_price=min(prices),
            currency=booking.currency,
            ok=True,
            source_url=url,
        )
    except Exception as exc:  # noqa: BLE001 – wir wollen jeden Fehler als Ergebnis melden.
        return PriceResult(
            booking=booking,
            checked_at=now,
            current_price=None,
            currency=booking.currency,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            source_url=url,
        )
