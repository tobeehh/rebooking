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

# Die Zimmerliste DIESER Unterkunft. Alles außerhalb davon sind Vorschläge für
# ANDERE Hotels – deren Preise dürfen nie in den Vergleich einfließen.
_ROOM_LIST_SELECTOR = '[data-stid="section-room-list"]'
_OFFER_PRICE_SELECTOR = '[data-stid="price-summary"]'

# Ein Angebotsblock nennt drei Zahlen; nur die mittlere ist der Vergleichswert:
#   "Der vorherige Preis war 759 €."      <- durchgestrichen, irrelevant
#   "Der aktuelle Preis beträgt 391 €."   <- Gesamtpreis für den Aufenthalt
#   "195 € pro Nacht"                     <- abgeleitet, NICHT vergleichbar
# min() über alle Zahlen würde also systematisch den Nachtpreis wählen und
# damit dauerhaft falsche „günstiger“-Meldungen erzeugen.
_CURRENT_PRICE_RES = (
    re.compile(r"aktuelle[rn]?\s+Preis\s+betr[äa]gt\s*([0-9][0-9.,\s]*[0-9])\s*(?:€|EUR)", re.I),
    re.compile(r"\bPreis\s+betr[äa]gt\s*([0-9][0-9.,\s]*[0-9])\s*(?:€|EUR)", re.I),
    re.compile(r"current\s+price\s+is\s*(?:€|EUR|\$|£)?\s*([0-9][0-9.,\s]*[0-9])", re.I),
    re.compile(r"\bprice\s+is\s*(?:€|EUR|\$|£)?\s*([0-9][0-9.,\s]*[0-9])", re.I),
)
_PREVIOUS_MARKERS = ("vorherige", "vorheriger", "alte preis", "alter preis",
                     "previous price", "old price", "standardpreis")
_PERNIGHT_MARKERS = ("pro nacht", "je nacht", "per night", "/nacht", "/night")


def extract_current_total(text: str) -> float | None:
    """Liest den aktuellen Gesamtpreis eines Angebotsblocks.

    Durchgestrichene Vorher-Preise und Nacht-Preise werden bewusst ignoriert –
    beide sind mit dem bezahlten Gesamtpreis nicht vergleichbar.
    """
    if not text:
        return None
    # Anker ist die Formulierung selbst ("… Preis beträgt X €"), nicht die Zeile:
    # je nach Markup liefert inner_text() den ganzen Block als eine Zeile, und
    # ein zeilenweiser Filter würde ihn dann komplett verwerfen.
    for rx in _CURRENT_PRICE_RES:
        for m in rx.finditer(text):
            # "war 759 €" / "195 € pro Nacht" matchen hier gar nicht erst –
            # beide stehen nie hinter "Preis beträgt".
            val = _to_float(m.group(1))
            if val is not None and 5 <= val <= 100000:
                return val
    return None

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


# Ist die Unterkunft für den Zeitraum ausgebucht, zeigt Hotels.com trotzdem
# Preise – die gehören dann aber zu VORGESCHLAGENEN ANDEREN Hotels. Wer das
# nicht erkennt, vergleicht den bezahlten Preis mit dem einer fremden Unterkunft
# und meldet fälschlich „günstiger“ für ein nicht buchbares Zimmer.
_SOLDOUT_MARKERS = (
    "bei uns ausgebucht", "ausgebucht", "ausverkauft",
    "wir sind ausgebucht", "sold out", "we are sold out",
    "keine verfügbarkeit", "no availability", "not available for these dates",
)
# Überschrift der Alternativvorschläge – zusätzlicher Beleg.
_ALTERNATIVES_MARKERS = ("ähnliche unterkünfte", "similar properties", "ähnliche hotels")


# Ohne Login zeigt Hotels.com die Listenpreise statt der Mitgliederpreise –
# gemessen 759 € statt 391 € für dieselbe Buchung. Eine abgelaufene Session
# würde den Monitor also lautlos wertlos machen: er meldete nie mehr eine
# Ersparnis, ohne dass ein Fehler sichtbar wird.
_SIGNEDOUT_MARKERS = (
    "anmelden",
    "sichere dir sofortrabatte",
    "entdecke hotels.com rewards",
    "sign in",
    "unlock instant savings",
    "members get",
)


def is_signed_out(page_text: str) -> bool:
    """Erkennt, ob die Seite abgemeldet ausgeliefert wurde."""
    low = (page_text or "").lower()
    return any(m in low for m in _SIGNEDOUT_MARKERS)


def is_sold_out(page_text: str) -> bool:
    """Erkennt, ob die Unterkunft für den Zeitraum nicht buchbar ist."""
    low = (page_text or "").lower()
    if any(m in low for m in _SOLDOUT_MARKERS):
        return True
    # Alternativen allein genügen nicht – nur zusammen mit fehlendem Preis
    # wäre das ein Indiz; hier bewusst konservativ.
    return False


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


def _extract_room_offer_totals(page) -> list[float]:
    """Gesamtpreise der Zimmerangebote DIESER Unterkunft.

    Bewusst eng auf die Zimmerliste begrenzt: Hotels.com blendet auf derselben
    Seite Preise ähnlicher Hotels ein, die nicht zur Buchung gehören.
    """
    totals: list[float] = []
    try:
        liste = page.query_selector(_ROOM_LIST_SELECTOR)
        if not liste:
            return []
        for el in liste.query_selector_all(_OFFER_PRICE_SELECTOR):
            wert = extract_current_total((el.inner_text() or "").strip())
            if wert is not None:
                totals.append(wert)
    except Exception:
        return []
    return totals


def _extract_prices(page, scraper: ScraperConfig) -> list[float]:
    """Vergleichbare Gesamtpreise der Buchung.

    Ohne eigene Selektoren wird AUSSCHLIESSLICH die Zimmerliste ausgewertet.
    Der frühere Rückfall auf den gesamten Seitentext ist entfallen: er sammelte
    Nachtpreise und Preise vorgeschlagener Fremdhotels ein und lieferte damit
    still falsche Vergleichswerte. Findet sich nichts, ist ein Fehler das
    ehrlichere Ergebnis – dann muss der Selektor nachgezogen werden.
    """
    if not scraper.price_selectors:
        return _extract_room_offer_totals(page)

    # Ausdrücklich konfigurierte Selektoren: Nutzerentscheidung, wie gehabt.
    prices: list[float] = []
    for sel in scraper.price_selectors:
        try:
            for el in page.query_selector_all(sel):
                txt = (el.inner_text() or "").strip()
                prices.extend(extract_prices_from_text(txt))
        except Exception:
            continue
    return prices


def fetch_price(booking: Booking, scraper: ScraperConfig) -> PriceResult:
    """Ruft den aktuell niedrigsten Preis für eine Buchung ab."""
    # Import hier, damit Tests/Config ohne installiertes Playwright laufen.
    from playwright.sync_api import sync_playwright

    url = build_url(booking, scraper)
    now = datetime.now()

    use_cdp = bool(scraper.cdp_url)
    try:
        with sync_playwright() as p:
            if use_cdp:
                # An laufenden, echten Chrome anbinden (robuster gegen Bot-Schutz).
                browser = p.chromium.connect_over_cdp(scraper.cdp_url)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
            else:
                browser = p.chromium.launch(
                    headless=scraper.headless,
                    executable_path=scraper.executable_path or None,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-http2",
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

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=scraper.timeout_ms)
                _dismiss_cookie_banner(page)
            # Gezielt auf die Zimmerliste warten statt blind auf networkidle:
            # letzteres wird durch Tracking-Skripte oft nie erreicht, und ohne
            # gerenderte Liste liefert die Extraktion grundlos nichts.
                try:
                    page.wait_for_selector(_ROOM_LIST_SELECTOR,
                                           timeout=min(30000, scraper.timeout_ms))
                except Exception:
                    try:
                        page.wait_for_load_state("networkidle", timeout=scraper.timeout_ms)
                    except Exception:
                        pass
                page.wait_for_timeout(2500)

                # Zuerst Verfügbarkeit prüfen: bei „ausgebucht" stehen auf der
                # Seite ausschließlich Preise anderer, vorgeschlagener Hotels.
                try:
                    seitentext = page.inner_text("body")
                except Exception:
                    seitentext = ""
                ausgebucht = is_sold_out(seitentext)
                abgemeldet = scraper.require_login and is_signed_out(seitentext)

                prices = [] if (ausgebucht or abgemeldet) else _extract_prices(page, scraper)
            finally:
                # Tab immer schließen – auch im Fehlerfall. Sonst sammeln sich im
                # Chrome des Nutzers mit jedem Lauf verwaiste Tabs an.
                try:
                    if use_cdp:
                        page.close()
                    else:
                        browser.close()
                except Exception:
                    pass

        if abgemeldet:
            return PriceResult(
                booking=booking,
                checked_at=now,
                current_price=None,
                currency=booking.currency,
                ok=False,
                error="Nicht eingeloggt – es kämen nur Listenpreise statt Mitgliederpreisen. "
                      "Session erneuern (siehe 'account login').",
                source_url=url,
            )

        if ausgebucht:
            return PriceResult(
                booking=booking,
                checked_at=now,
                current_price=None,
                currency=booking.currency,
                ok=False,
                error="Unterkunft für diesen Zeitraum ausgebucht – kein vergleichbarer Preis.",
                source_url=url,
            )

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
