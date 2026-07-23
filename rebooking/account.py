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
# Erkennt Datums-artige Strings in vielen Formaten (nur zum Anzeigen in inspect).
_DATEISH_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{2,4}|(19|20)\d{2}"
    r"|jan|feb|mar|mär|apr|may|mai|jun|jul|aug|sep|oct|okt|nov|dec|dez",
    re.IGNORECASE,
)


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


def _redact(obj: Any, depth: int = 0, max_depth: int = 8) -> Any:
    """Schwärzt freie Texte (Namen/Adressen), behält Struktur, Datum, Zahlen, Enums."""
    if depth > max_depth:
        return "…"
    if isinstance(obj, dict):
        return {k: _redact(v, depth + 1, max_depth) for k, v in obj.items()}
    if isinstance(obj, list):
        red = [_redact(v, depth + 1, max_depth) for v in obj[:3]]
        if len(obj) > 3:
            red.append(f"…(+{len(obj) - 3} weitere)")
        return red
    if isinstance(obj, str):
        if len(obj) <= 30 and _DATEISH_RE.search(obj):
            return obj  # Datum ist nicht sensibel und hilft beim Mapping
        if len(obj) <= 18 and " " not in obj:
            return obj  # kurze Enums/Codes/Währungen behalten
        return f"<text len={len(obj)}>"
    return obj  # Zahlen, bool, None behalten


# Feldnamen-Hinweise, die auf eine echte Buchung deuten (unabhängig vom Datum).
_BOOKING_KEY_HINTS = (
    "propertyname", "hotelname", "property_name", "checkin", "check_in", "checkindate",
    "checkout", "check_out", "checkoutdate", "reservation", "confirmation", "roomtype",
    "numberofnights", "nights", "propertyid", "staydate", "arrival", "departure",
    "leadprice", "totalprice", "pricedetails", "itinerary", "itineraryitemid",
    # Preis-Felder (verschiedene Schreibweisen)
    "price", "amount", "paid", "payment", "subtotal", "grandtotal", "total",
    "fare", "charge", "formattedprice", "pricesummary", "tripprice", "ordertotal",
)

# Reine Tracking-/Analytics-Knoten, die wir NICHT sehen wollen.
_NOISE_KEY_HINTS = ("tc_vars", "point_of_sale", "clickstream", "identifiers_session_id")


_MONEY_RE = re.compile(r"[€$£]|\bEUR\b|\bUSD\b|\bGBP\b")


def _booking_like_nodes(data: Any) -> list[dict]:
    """Findet Knoten, die auf eine Buchung hindeuten (per Feldname oder Text-Inhalt)."""
    hits: list[dict] = []
    for node in _walk(data):
        if not isinstance(node, dict):
            continue
        keys_join = " ".join(node.keys()).lower()
        if any(n in keys_join for n in _NOISE_KEY_HINTS):
            continue
        if any(h in keys_join for h in _BOOKING_KEY_HINTS) and 2 <= len(node) <= 80:
            hits.append(node)
            continue
        # EGDS-Textbausteine mit Datum oder Geldbetrag (dort stehen oft Datum/Preis).
        t = node.get("text")
        if isinstance(t, str) and len(node) <= 12 and (_MONEY_RE.search(t) or _DATEISH_RE.search(t)):
            hits.append(node)
    return hits


def inspect_detail(capture_dir: str | Path, raw: bool = False, max_chars: int = 11000) -> str:
    """Zeigt die vollständige(n) Detail-Antwort(en) mit dem Preis (Kontext für Mapping)."""
    cap = Path(capture_dir)
    files = sorted(cap.glob("*.json")) if cap.exists() else []
    out: list[str] = []
    shown = 0
    for f in files:
        txt = f.read_text(encoding="utf-8")
        if "pricingSummaries" not in txt and "Total price" not in txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:  # noqa: BLE001
            continue
        red = data if raw else _redact(data)
        blob = json.dumps(red, indent=2, ensure_ascii=False)
        if len(blob) > max_chars:
            blob = blob[:max_chars] + "\n…(gekürzt)"
        out.append(f"### {f.name}\n{blob}")
        shown += 1
        if shown >= 2:
            break
    if not out:
        return "Keine Datei mit 'pricingSummaries'/'Total price' gefunden."
    return "\n\n".join(out)


def inspect_capture(capture_dir: str | Path, max_chars: int = 3000, raw: bool = False) -> str:
    """Struktur-Übersicht der Buchungs-Knoten (per Feldname), dedupliziert.

    raw=True zeigt echte Werte (zum Mapping); sonst werden freie Texte geschwärzt.
    """
    cap = Path(capture_dir)
    files = sorted(cap.glob("*.json")) if cap.exists() else []
    if not files:
        return f"Keine Mitschnitte in {cap}. Erst 'account import --cdp --capture' ausführen."

    shapes: dict[tuple, list] = {}  # key-tuple -> [example, count]
    url_list: list[str] = []
    for f in files:
        # Dateiname kodiert die URL – hilft zu sehen, welche Endpunkte Daten liefern.
        url_list.append(f.name)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for n in _booking_like_nodes(data):
            key = tuple(sorted(n.keys()))
            if key not in shapes:
                shapes[key] = [n if raw else _redact(n), 0]
            shapes[key][1] += 1

    out: list[str] = [f"{len(files)} Mitschnitte. Endpunkte:"]
    out += [f"  - {u}" for u in url_list[:40]]

    if not shapes:
        out.append(
            "\nKeine Knoten mit Buchungs-Feldnamen gefunden. Die Reservierungsdaten "
            "liegen evtl. in einem anderen Endpunkt oder als HTML vor.\n"
            "Bitte schick mir zusätzlich die obige Endpunkt-Liste – daran erkenne ich, "
            "welche Antwort die Buchung enthält."
        )
        return "\n".join(out)

    out.append(f"\n{len(shapes)} Knoten-Formen mit Buchungs-Feldern:")
    _prio = ("price", "amount", "paid", "total", "checkin", "checkout", "date", "text")

    def _score(kv):
        keys_join = " ".join(kv[0]).lower()
        return sum(1 for p in _prio if p in keys_join)

    for i, (key, (example, cnt)) in enumerate(
        sorted(shapes.items(), key=lambda kv: (_score(kv), kv[1][1]), reverse=True)[:15], 1
    ):
        blob = json.dumps(example, indent=2, ensure_ascii=False)
        if len(blob) > max_chars:
            blob = blob[:max_chars] + "\n…(gekürzt)"
        out.append(f"\n### Form {i} — {cnt}× — Felder: {list(key)}\n{blob}")
    return "\n".join(out)


def _has_date_descendant(node: Any, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(node, str):
        return bool(_DATE_RE.search(node))
    if isinstance(node, dict):
        return any(_has_date_descendant(v, depth + 1) for v in node.values())
    if isinstance(node, list):
        return any(_has_date_descendant(v, depth + 1) for v in node[:20])
    return False


def _trip_like_nodes(data: Any) -> list[dict]:
    """Findet die kleinsten Dicts, die ein Datum enthalten (Buchungskandidaten)."""
    hits: list[dict] = []
    for node in _walk(data):
        if not isinstance(node, dict):
            continue
        # Direktes Datum in einem Feld dieses Dicts (nicht zu tief) -> Kandidat.
        if any(_has_date_descendant(v, 3) for v in node.values()):
            # Knoten mit etwas Inhalt (nicht die Riesen-Wurzel, nicht der
            # reine {isoDate:…}-Wrapper)
            if 3 <= len(node) <= 60:
                hits.append(node)
    return hits


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


_TRIPID_RE = re.compile(r"egti-[A-Za-z0-9]{2,4}-[A-Za-z0-9]{2,4}-[A-Za-z0-9]{2,6}")
# "Reiseplan: 72077068384394" (DE) / "Itinerary: 72077068384394" (EN)
_TRIPID_NUM_RE = re.compile(r"(?:Reiseplan|Itinerary)\s*:?\s*(\d{8,})", re.IGNORECASE)
# Detail-Link: /trips/<tripViewId>/details/<encodedTripItemId>
_DETAIL_PARTS_RE = re.compile(r"/trips/([^/]+)/details/([^/?#]+)")


# Echte Hotelseite: /ho<propertyId>/<slug>/ – die propertyId hat NICHTS mit der
# Zahl im Bildpfad zu tun (die führt zu 404). Sie steht nur als Link im DOM der
# Buchungs-Detailseite.
_PROPERTY_HREF_RE = re.compile(r"^https?://[^/]*hotels\.com/ho(\d{4,})/", re.IGNORECASE)


def _extract_property_url(page) -> str:
    """Liest den Link zur Hotelseite aus der Buchungs-Detailseite."""
    try:
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    except Exception:
        return ""
    for h in hrefs or []:
        if _PROPERTY_HREF_RE.match(h or ""):
            return h.split("?")[0]
    return ""


def _servicing_url(detail_url: str, trip_num: str, locale: str = "") -> str | None:
    """Baut die URL der Buchungsverwaltung, wo die Stornobedingungen stehen.

    Alle drei Bestandteile stecken bereits in den Kartendaten:
    tripViewId und encodedTripItemId im Detail-Link, die tripId als
    „Reiseplan“-Nummer. Diese Seite wird ausschließlich GELESEN.
    """
    m = _DETAIL_PARTS_RE.search(detail_url or "")
    if not m or not trip_num:
        return None
    url = ("https://de.hotels.com/booking-servicing/lodging"
           f"?tripViewId={m.group(1)}&encodedTripItemId={m.group(2)}&tripId={trip_num}")
    return _localized_url(url, locale)


def _extract_trip_ids(captured: list[Any]) -> list[str]:
    """Zieht tripIds (egti-…) aus den abgefangenen Antworten."""
    ids: list[str] = []
    seen: set[str] = set()
    for body in captured:
        # 1) gezielt über den Schlüssel tripId
        for node in _walk(body):
            if isinstance(node, dict):
                low = _lower_keys(node)
                tid = low.get("tripid")
                if isinstance(tid, str) and tid and tid not in seen:
                    seen.add(tid)
                    ids.append(tid)
        # 2) zusätzlich per Regex über den Rohtext
        try:
            for m in _TRIPID_RE.findall(json.dumps(body)):
                if m not in seen:
                    seen.add(m)
                    ids.append(m)
        except Exception:
            pass
    return ids


def _extract_detail_urls(captured: list[Any]) -> list[str]:
    """Findet die Buchungs-Detail-Links (cardAction.resource.value, .../details/…)."""
    urls: list[str] = []
    seen: set[str] = set()
    for body in captured:
        for node in _walk(body):
            if isinstance(node, dict) and node.get("__typename") == "HttpURI":
                v = node.get("value")
                if isinstance(v, str) and "/details/" in v and v not in seen:
                    seen.add(v)
                    urls.append(v)
    return urls


# ---------------------------------------------------------------------------
# EGDS-Extraktion (Hotels.com „Trips“ ist ein UI-Komponentenbaum)
# ---------------------------------------------------------------------------

_MONTHS = {
    # Englisch (3-Buchstaben)
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    # Deutsch (voll + Kurzformen). Die deutsche Seite kürzt anders ab als die
    # englische – u.a. "Sept." (statt "Sep"), "Mrz.", "Okt.", "Dez.".
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "mär": 3, "mrz": 3,
    "april": 4, "mai": 5, "juni": 6, "juli": 7, "august": 8,
    "september": 9, "sept": 9, "oktober": 10, "okt": 10,
    "november": 11, "dezember": 12, "dez": 12,
}
# EN: "Jul 20 at 2:00pm - Jul 22 at 10:00am"
_RANGE_EN = re.compile(
    r"([A-Za-z]{3})[a-z]*\s+(\d{1,2})\b.{0,20}?[-–]\s*([A-Za-z]{3})[a-z]*\s+(\d{1,2})\b"
)
# DE: "von 20. Juli, 14:00 Uhr bis 22. Juli, 10:00 Uhr" / "20. Juli 2026 – 22. Juli 2026"
# Trennung wahlweise per "bis" oder Bindestrich/Gedankenstrich.
_RANGE_DE = re.compile(
    r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\.?(?:\s+(\d{4}))?.{0,25}?(?:\bbis\b|[-–]).{0,12}?"
    r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\.?(?:\s+(\d{4}))?"
)
# "Paid on Jun 17, 2026"
_PAIDON_RE = re.compile(r"([A-Za-z]{3})[a-z]*\s+(\d{1,2}),\s*(\d{4})")
# Geldbetrag: Symbol davor ("€194.64") ODER dahinter ("194,64 €" / "194,64 EUR")
_MONEY_BEFORE = re.compile(r"([€$£])\s*([0-9][0-9.,]*[0-9]|[0-9])")
_MONEY_AFTER = re.compile(r"([0-9][0-9.,]*[0-9]|[0-9])\s*(€|EUR|\$|USD|£|GBP)", re.IGNORECASE)
_CUR = {"€": "EUR", "$": "USD", "£": "GBP", "eur": "EUR", "usd": "USD", "gbp": "GBP"}


def _infer_year(month: int, day: int, ref: date | None = None) -> date:
    ref = ref or date.today()
    d = date(ref.year, month, day)
    from datetime import timedelta

    if d < ref - timedelta(days=120):
        d = date(ref.year + 1, month, day)
    return d


def _parse_date_range(text: str) -> tuple[str, str] | None:
    # Englisch: Monat Tag … – Monat Tag
    m = _RANGE_EN.search(text)
    if m:
        m1 = _MONTHS.get(m.group(1).lower())
        m2 = _MONTHS.get(m.group(3).lower())
        if m1 and m2:
            ci = _infer_year(m1, int(m.group(2)))
            co = date(ci.year, m2, int(m.group(4)))
            if co <= ci:
                co = date(ci.year + 1, m2, int(m.group(4)))
            return ci.isoformat(), co.isoformat()
    # Deutsch: Tag. Monat [Jahr] … – Tag. Monat [Jahr]
    m = _RANGE_DE.search(text)
    if m:
        m1 = _MONTHS.get(m.group(2).lower())
        m2 = _MONTHS.get(m.group(5).lower())
        if m1 and m2:
            y1, y2 = m.group(3), m.group(6)
            ci = date(int(y1), m1, int(m.group(1))) if y1 else _infer_year(m1, int(m.group(1)))
            co = date(int(y2), m2, int(m.group(4))) if y2 else date(ci.year, m2, int(m.group(4)))
            if co <= ci:
                co = date(ci.year + 1, m2, int(m.group(4)))
            return ci.isoformat(), co.isoformat()
    return None


def _parse_money(s: str) -> tuple[float | None, str]:
    m = _MONEY_BEFORE.search(s)
    if m:
        sym, raw = m.group(1), m.group(2)
    else:
        m = _MONEY_AFTER.search(s)
        if not m:
            return None, "EUR"
        raw, sym = m.group(1), m.group(2)
    cur = _CUR.get(sym.lower(), "EUR")
    # "194.64" (US) oder "1.234,56" / "194,64" (DE)
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".") if len(raw.split(",")[-1]) == 2 else raw.replace(",", "")
    try:
        return float(raw), cur
    except ValueError:
        return None, cur


def _iter_texts(responses: list[Any]):
    """Alle String-Werte in den Antworten (für Textsuche)."""
    for body in responses:
        for node in _walk(body):
            if isinstance(node, dict):
                for v in node.values():
                    if isinstance(v, str):
                        yield v


def _find_total_price(responses: list[Any]) -> tuple[float | None, str]:
    """Sprachunabhängig: Gesamtpreis = Geldbetrag im value der Preissumme.

    Bevorzugt die fett gesetzte Summe (Gesamtpreis), sonst den größten Betrag.
    """
    candidates: list[tuple[float, str, bool]] = []
    for body in responses:
        for node in _walk(body):
            if not isinstance(node, dict) or node.get("__typename") != "TripDetailsUIPricingSummary":
                continue
            val = node.get("value")
            if not isinstance(val, dict):
                continue
            amount, cur = _parse_money(val.get("stylizedText", "") or "")
            if amount is None:
                continue
            is_bold = (val.get("weight") == "BOLD")
            candidates.append((amount, cur, is_bold))
    if not candidates:
        return None, "EUR"
    bold = [c for c in candidates if c[2]]
    pick = bold[0] if bold else max(candidates, key=lambda c: c[0])
    return pick[0], pick[1]


# "Paid on Jun 17, 2026" (EN) / "Bezahlt am 17. Juni 2026" (DE)
_PAIDON_DE_RE = re.compile(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\.?\s+(\d{4})")


def _find_paid_on(responses: list[Any]) -> str | None:
    for txt in _iter_texts(responses):
        low = txt.lower()
        if low.startswith("paid on"):
            m = _PAIDON_RE.search(txt)
            if m:
                mon = _MONTHS.get(m.group(1).lower())
                if mon:
                    return date(int(m.group(3)), mon, int(m.group(2))).isoformat()
        if low.startswith("bezahlt am") or low.startswith("gezahlt am"):
            m = _PAIDON_DE_RE.search(txt)
            if m:
                mon = _MONTHS.get(m.group(2).lower())
                if mon:
                    return date(int(m.group(3)), mon, int(m.group(1))).isoformat()
    return None


def _find_date_range(responses: list[Any]) -> tuple[str, str] | None:
    for txt in _iter_texts(responses):
        if len(txt) > 200 or not any(c.isdigit() for c in txt):
            continue
        got = _parse_date_range(txt)
        if got:
            return got
    return None


def _find_cancellation(responses: list[Any]) -> dict:
    """Stornierbarkeit (DE+EN). free: True/False/None (unbekannt)."""
    free: bool | None = None
    detail = ""
    for txt in _iter_texts(responses):
        low = txt.lower()
        if ("non-refundable" in low or "nonrefundable" in low or "non refundable" in low
                or "nicht erstattbar" in low or "nicht stornierbar" in low
                or "keine erstattung" in low):
            return {"free": False, "text": txt.strip()[:120]}
        if ("free cancellation" in low or "fully refundable" in low or "free cancelation" in low
                or "kostenlose stornierung" in low or "kostenlos stornier" in low
                or "gratis stornier" in low or "kostenfrei stornier" in low):
            free, detail = True, txt.strip()[:120]
    return {"free": free, "text": detail}


# Die Stornobedingungen stehen NUR im gerenderten HTML der Seite
# /booking-servicing/lodging – nicht in den abgefangenen JSON-Antworten.
# Maßgeblich ist die Zeile zur aktuell geltenden Phase:
#   "Vollständige Rückerstattung ab heute bis zum 26. Juli."
#   "Keine Rückerstattung ab heute bis zum Check-in."
# Die Datumsangabe enthält selbst einen Punkt ("bis zum 26. Juli."), deshalb
# bis zum Zeilenende lesen statt bis zum ersten Punkt.
_PHASE_DE = re.compile(r"([^.\n]{3,60}?)\s+ab heute bis\s+([^\n]{1,40})", re.IGNORECASE)
_PHASE_EN = re.compile(r"([^.\n]{3,60}?)\s+from today until\s+([^\n]{1,40})", re.IGNORECASE)
# "… wenn du vor dem 26. Juli 2026, 18:00 Uhr (Ortszeit), stornierst."
_DEADLINE_DE = re.compile(r"vor dem\s+(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\.?\s+(\d{4})", re.IGNORECASE)

_FULL_WORDS = ("vollständige rückerstattung", "volle rückerstattung", "full refund",
               "fully refundable", "vollständig erstattbar")
_NONE_WORDS = ("keine rückerstattung", "nicht erstattbar", "no refund", "non-refundable",
               "nonrefundable")
_PART_WORDS = ("teilerstattung", "teilweise rückerstattung", "partial refund")


def parse_cancellation_page(text: str) -> dict:
    """Liest die Stornierbarkeit aus dem Text der Buchungsverwaltungs-Seite.

    Rückgabe: ``free`` True/False/None (unbekannt), ``text`` als Beleg und
    ``free_until`` (ISO-Datum), bis wann kostenlos storniert werden kann.

    Entscheidend ist die **aktuell geltende** Phase ("ab heute bis …"): eine
    Buchung, die erst in Zukunft teilerstattbar wird, ist heute noch voll
    erstattbar – und nur das zählt fürs Umbuchen.
    """
    if not text:
        return {"free": None, "text": "", "free_until": None}

    phase_txt = ""
    for rx in (_PHASE_DE, _PHASE_EN):
        m = rx.search(text)
        if m:
            phase_txt = m.group(0).strip()
            break

    quelle = phase_txt or text
    low = quelle.lower()
    free: bool | None = None
    if any(w in low for w in _NONE_WORDS):
        free = False
    elif any(w in low for w in _FULL_WORDS):
        free = True
    elif any(w in low for w in _PART_WORDS):
        # Teilerstattung reicht zum kostenlosen Umbuchen nicht.
        free = False

    # Ohne Phasen-Zeile den zusammenfassenden Satz auswerten.
    if free is None:
        low_all = text.lower()
        if any(w in low_all for w in _NONE_WORDS):
            free = False
        elif any(w in low_all for w in _FULL_WORDS):
            free = True

    free_until = None
    if free:
        m = _DEADLINE_DE.search(text)
        if m:
            mon = _MONTHS.get(m.group(2).lower())
            if mon:
                try:
                    free_until = date(int(m.group(3)), mon, int(m.group(1))).isoformat()
                except ValueError:
                    free_until = None

    return {"free": free, "text": (phase_txt or "")[:160], "free_until": free_until}


def _extract_cards(captured: list[Any]) -> list[dict]:
    """Buchungskarten (TripsUIBookedItemCard): Hotelname, Property-ID, Detail-Link.

    Dieselbe Buchung taucht in mehreren Kartenvarianten auf: eine trägt den
    Detail-Link und den Reisezeitraum als Fließtext, eine andere Ort und
    Nächtezahl als Icon-Text. Die Felder werden deshalb über alle Varianten
    hinweg zusammengeführt – wer nur die erste nimmt, verliert den Rest.
    """
    cards: dict[str, dict] = {}
    for body in captured:
        for node in _walk(body):
            if not isinstance(node, dict) or node.get("__typename") != "TripsUIBookedItemCard":
                continue
            pid = node.get("identifier") or ""
            name = node.get("primary")
            if not name:
                continue
            action = node.get("cardAction") or {}
            res = (action.get("resource") or {}) if isinstance(action, dict) else {}
            detail_url = res.get("value", "") if isinstance(res, dict) else ""
            location = ""
            trip_num = ""
            dates: tuple[str, str] | None = None
            for sec in node.get("enrichedSecondaries") or []:
                if not isinstance(sec, dict):
                    continue
                text = sec.get("text") or ""
                g = sec.get("graphic") or {}
                # Icon-ID statt Beschreibung: "place" ist sprachunabhängig,
                # "Location" heißt auf der deutschen Seite anders.
                if isinstance(g, dict) and (g.get("id") == "place" or g.get("description") == "Location"):
                    location = location or text
                elif text and _TRIPID_NUM_RE.search(text):
                    # "Reiseplan: 72077068384394" / "Itinerary: 72077068384394"
                    trip_num = trip_num or _TRIPID_NUM_RE.search(text).group(1)
                elif text and not dates:
                    # "Jul 20 at 2:00pm - Jul 22 at 10:00am" bzw.
                    # "von 20. Juli, 14:00 Uhr bis 22. Juli, 10:00 Uhr"
                    dates = _parse_date_range(text)
            # Property-Bild-URL enthält oft die Property-ID (letzter Zahlenordner).
            prop_num = ""
            media = node.get("media") or {}
            murl = media.get("url", "") if isinstance(media, dict) else ""
            mnum = re.search(r"/lodging/(?:\d+/){3}(\d+)/", murl)
            if mnum:
                prop_num = mnum.group(1)
            key = pid or name
            cur = cards.setdefault(key, {
                "property_id": pid,
                "name": name,
                "location": "",
                "detail_url": "",
                "property_num": "",
                "trip_num": "",
                "dates": None,
            })
            # Leere Felder aus späteren Varianten nachtragen.
            cur["location"] = cur["location"] or location
            cur["detail_url"] = cur["detail_url"] or detail_url
            cur["property_num"] = cur["property_num"] or prop_num
            cur["trip_num"] = cur["trip_num"] or trip_num
            cur["dates"] = cur["dates"] or dates
    return list(cards.values())


def _auth_state(captured: list[Any]) -> str | None:
    """Ermittelt grob den Login-Status aus den Antworten (ANONYMOUS/AUTHENTICATED)."""
    for body in captured:
        for node in _walk(body):
            if isinstance(node, dict):
                low = _lower_keys(node)
                for k in ("usertype", "user_authentication_state", "userauthenticationstate"):
                    if isinstance(low.get(k), str):
                        return low[k]
                if low.get("registered") is True:
                    return "AUTHENTICATED"
    return None


def _locale_kwargs(account) -> dict:
    """Locale + Accept-Language für den eigenen Browser.

    Ohne das richtet sich die Sprache nach dem System und die Kontoansicht
    kommt mal auf Deutsch, mal auf Englisch – die Extraktion wäre nicht
    reproduzierbar. Im CDP-Modus greift das nicht (dort gilt dein Chrome).
    """
    loc = (getattr(account, "locale", "") or "").strip()
    if not loc:
        return {}
    primary = loc.split("-")[0]
    return {
        "locale": loc,
        "extra_http_headers": {"Accept-Language": f"{loc},{primary};q=0.9"},
    }


def _localized_url(url: str, locale: str) -> str:
    """Hängt ?locale=de_DE an, falls noch keine Sprache in der URL steht.

    Die Domain allein genügt nicht: de.hotels.com liefert ohne den Parameter
    weiterhin die englische Seite (und teils USD als Währung). Erst der
    locale-Parameter schaltet die Sprache um – danach behält die Session sie
    auch auf den Detailseiten bei.
    """
    loc = (locale or "").strip().replace("-", "_")
    if not loc or "locale=" in url:
        return url
    return url + ("&" if "?" in url else "?") + f"locale={loc}"


# Nur eindeutige „Wegklicken"-Beschriftungen. Ein blankes „Cancel"/„Stornieren"
# ist bewusst NICHT dabei – das könnte eine Buchung stornieren statt ein Popup
# zu schließen.
_DISMISS_PATTERNS = (
    "schließen", "schliessen", "close", "dismiss",
    "nicht jetzt", "not now", "später", "maybe later",
    "nein danke", "no thanks", "no, thanks",
    "im browser fortfahren", "continue in browser", "weiter im browser",
    "zur website", "continue to site", "ablehnen",
)


def _dismiss_overlays(page, verbose: bool = False) -> str | None:
    """Schließt Overlays (App-Hinweis, Cookie-Banner), damit der Lauf
    unbeaufsichtigt durchläuft.

    Klickt ausschließlich innerhalb eines erkannten Dialogs und nur auf
    eindeutige Schließen-Beschriftungen; sonst Escape. Findet sich nichts,
    passiert nichts – die Funktion ist absichtlich geräuschlos.
    """
    containers = ('[role="dialog"]', '[aria-modal="true"]', "dialog[open]",
                  '[data-stid*="sheet"]', '[class*="uitk-sheet"]')
    # Auf der Buchungsverwaltung wird GRUNDSÄTZLICH nicht geklickt – dort liegt
    # der Knopf zum Stornieren. Dort nur Escape.
    nur_escape = "booking-servicing" in (getattr(page, "url", "") or "")
    try:
        for sel in containers:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 3)):
                box = loc.nth(i)
                if not box.is_visible():
                    continue
                buttons = box.locator("button, a[role='button']") if not nur_escape else None
                for j in range(min(buttons.count(), 12) if buttons else 0):
                    btn = buttons.nth(j)
                    try:
                        if not btn.is_visible():
                            continue
                        label = ((btn.get_attribute("aria-label") or "")
                                 + " " + (btn.inner_text() or "")).strip().lower()
                    except Exception:
                        continue
                    if any(pat in label for pat in _DISMISS_PATTERNS):
                        try:
                            btn.click(timeout=3000)
                            page.wait_for_timeout(600)
                            if verbose:
                                print(f"    Overlay geschlossen: {label[:50]!r}")
                            return label[:50]
                        except Exception:
                            pass
                # Dialog sichtbar, aber kein passender Button -> Escape.
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(400)
                    if verbose:
                        print(f"    Overlay per Escape geschlossen ({sel})")
                    return "escape"
                except Exception:
                    pass
    except Exception:
        pass  # Overlay-Behandlung darf den Import nie zum Scheitern bringen
    return None


def _goto_with_retry(page, url: str, attempts: int = 3, verbose: bool = False) -> None:
    """Navigiert mit Wiederholungen (fängt transiente Protokoll-/Netzfehler ab)."""
    last_exc = None
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # App-Hinweis/Cookie-Banner wegklicken – sonst blockiert das Popup
            # den unbeaufsichtigten Lauf.
            page.wait_for_timeout(1200)
            _dismiss_overlays(page, verbose=verbose)
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
            **_locale_kwargs(account),
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        _goto_with_retry(page, _localized_url(account.login_url, account.locale), verbose=True)
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


def session_status(account) -> dict:
    """Prüft, ob die gespeicherte Session noch eingeloggt ist.

    Nötig, weil eine abgelaufene Session sonst unbemerkt bleibt: Hotels.com
    liefert dann klaglos die Listenpreise statt der Mitgliederpreise.
    """
    from playwright.sync_api import sync_playwright

    from .scraper import is_signed_out

    use_cdp = bool(account.cdp_url)
    ergebnis = {"logged_in": False, "name": "", "url": "", "error": ""}
    try:
        with sync_playwright() as p:
            if use_cdp:
                browser = p.chromium.connect_over_cdp(account.cdp_url)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
            else:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=str(Path(account.profile_dir)),
                    headless=account.headless,
                    executable_path=account.executable_path or None,
                    user_agent=_USER_AGENT,
                    args=_LAUNCH_ARGS,
                    **_locale_kwargs(account),
                )
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                _goto_with_retry(page, _localized_url(account.trips_url, account.locale))
                page.wait_for_timeout(5000)
                text = page.inner_text("body")
                ergebnis["url"] = page.url
                ergebnis["logged_in"] = not is_signed_out(text)
                # Der Kontoname steht direkt hinter „Meine Reisen“ im Kopfbereich.
                zeilen = [z.strip() for z in text.splitlines() if z.strip()]
                for i, z in enumerate(zeilen[:20]):
                    if z.lower() in ("meine reisen", "my trips") and i + 1 < len(zeilen):
                        kandidat = zeilen[i + 1]
                        if kandidat.lower() not in ("anmelden", "sign in"):
                            ergebnis["name"] = kandidat
                        break
            finally:
                try:
                    page.close() if use_cdp else ctx.close()
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        ergebnis["error"] = f"{type(exc).__name__}: {exc}"
    return ergebnis


def fetch_account_bookings(account, capture_dir: str | Path | None = None, verbose: bool = True) -> list[dict]:
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
        # Alte Mitschnitte entfernen, damit 'inspect' nie veraltete Daten zeigt.
        for old in cap_path.glob("*.json"):
            try:
                old.unlink()
            except OSError:
                pass

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
                **_locale_kwargs(account),
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

        if verbose:
            print(f"  Modus: {'CDP (eigener Chrome)' if use_cdp else 'eigener Browser'} · Ziel: {account.trips_url}")

        # Bei CDP eine eigene Seite öffnen (bestehende Tabs des Nutzers nicht stören).
        page = ctx.new_page() if use_cdp else (ctx.pages[0] if ctx.pages else ctx.new_page())
        page.on("response", on_response)
        # Sprache über die URL erzwingen: im CDP-Modus ist das der EINZIGE Hebel,
        # weil dort weder locale noch Accept-Language gesetzt werden können.
        _goto_with_retry(page, _localized_url(account.trips_url, account.locale), verbose=verbose)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(4000)
        # Etwas scrollen, damit nachladende Inhalte (Lazy-Load) erscheinen.
        try:
            for _ in range(3):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(1000)
        except Exception:
            pass

        # Die Buchungsdetails werden pro Trip separat geladen -> tripIds folgen.
        trip_ids = _extract_trip_ids(captured)
        if verbose and trip_ids:
            print(f"  Gefundene Trip-IDs: {len(trip_ids)} – lade Detailseiten …")
        for tid in trip_ids[:25]:
            try:
                _goto_with_retry(page, _localized_url(
                    account.trip_detail_url_template.format(id=tid), account.locale))
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
            except Exception:
                continue

        # Buchungskarten (Hotelname + Detail-Link) aus den Übersichtsseiten.
        cards = _extract_cards(captured)
        if verbose:
            print(f"  Buchungskarten: {len(cards)}")

        bookings: list[dict] = []
        for card in cards[:30]:
            if not card.get("detail_url"):
                continue
            start = len(captured)
            prop_url = ""
            try:
                _goto_with_retry(page, _localized_url(card["detail_url"], account.locale))
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
                # Link zur echten Hotelseite einsammeln, solange wir dort sind.
                prop_url = _extract_property_url(page)
                # Zusätzlich der „Beleg/Details"-Unterseite folgen (Storno-Bedingungen).
                for sub in _extract_detail_urls(captured[start:])[:3]:
                    if sub != card["detail_url"]:
                        try:
                            _goto_with_retry(page, _localized_url(sub, account.locale))
                            page.wait_for_timeout(2000)
                        except Exception:
                            pass
            except Exception:
                pass

            page_slice = captured[start:]
            amount, cur = _find_total_price(page_slice)
            # Das Datum aus der Karte ist eindeutig dieser Buchung zugeordnet;
            # die Textsuche über den Slice kann Fremdtreffer liefern.
            # KEIN Rückgriff auf alle Antworten: der lieferte immer den Zeitraum
            # der ERSTEN Buchung und damit still ein falsches Datum.
            dates = card.get("dates") or _find_date_range(page_slice)

            # Stornobedingungen: nur auf der Verwaltungsseite zu finden, und dort
            # ausschließlich im HTML (nicht in den JSON-Antworten). Die Seite wird
            # NUR gelesen – es wird dort nichts angeklickt.
            cancel = {"free": None, "text": "", "free_until": None}
            svc = _servicing_url(card["detail_url"], card.get("trip_num", ""), account.locale)
            if svc:
                try:
                    _goto_with_retry(page, svc)
                    page.wait_for_timeout(2500)
                    cancel = parse_cancellation_page(page.inner_text("body"))
                except Exception:
                    pass
            if cancel.get("free") is None:
                # Rückfall auf die Textsuche in den JSON-Antworten.
                alt = _find_cancellation(page_slice)
                if alt.get("free") is not None:
                    cancel = {**alt, "free_until": None}
            if verbose and (amount is None or not dates):
                # Zeigt echte Kandidaten-Texte (Datum/Storno), um Muster zu justieren.
                samples: list[str] = []
                for t in _iter_texts(page_slice):
                    low = t.lower()
                    if (re.search(r"\d", t) and (_DATEISH_RE.search(t) or "uhr" in low or "–" in t)) \
                            or any(k in low for k in ("stornier", "erstattbar", "refund", "cancel")):
                        if t not in samples:
                            samples.append(t)
                    if len(samples) >= 6:
                        break
                print(f"    [debug {card['name'][:22]}] price={amount} dates={dates}")
                for s in samples:
                    print(f"        text: {s[:75]}")
            checkin, checkout = (dates or ("", ""))
            # NUR der echte Link aus dem DOM. Die früher aus dem Bildpfad geratene
            # ID ("ho42077782") lieferte durchweg 404 – lieber leer als falsch.
            url = prop_url
            vergangen = bool(checkout) and checkout < date.today().isoformat()
            bookings.append({
                "name": card["name"],
                "url": url,
                "location": card.get("location", ""),
                "checkin": checkin,
                "checkout": checkout,
                "paid_price": amount,
                "currency": cur,
                "paid_on": _find_paid_on(page_slice),
                "free_cancellation": cancel.get("free"),
                "cancellation_text": cancel.get("text", ""),
                "free_until": cancel.get("free_until"),
                "past": vergangen,
            })
            if verbose:
                fc = {True: "kostenlos stornierbar", False: "NICHT erstattbar", None: "Storno unbekannt"}[cancel.get("free")]
                if cancel.get("free_until"):
                    fc += f" bis {cancel['free_until']}"
                if vergangen:
                    fc += " · VERGANGEN"
                if not url:
                    fc += " · KEINE HOTEL-URL"
                print(f"    • {card['name']}: {checkin}→{checkout}  {amount} {cur}  [{fc}]")

        auth = _auth_state(captured)
        final_url = page.url
        final_title = ""
        try:
            final_title = page.title()
        except Exception:
            pass

        # Zweite Quelle: im HTML eingebettete JSON-Blobs (z.B. Next.js __NEXT_DATA__).
        embedded = _collect_embedded_json(page, cap_path)

        if use_cdp:
            page.close()          # nur unseren Tab schließen, Chrome des Nutzers bleibt offen
        else:
            ctx.close()

    if verbose:
        print(f"  Geladen: {final_url}")
        if final_title:
            print(f"  Titel: {final_title}")
        print(f"  Abgefangene JSON-Antworten: {len(captured)} · eingebettete JSON-Blobs: {len(embedded)}")
        if auth:
            print(f"  Login-Status: {auth}")
        low = (final_url + " " + final_title).lower()
        on_login_page = any(w in low for w in ("sign in", "log in", "anmelden", "/login"))
        if on_login_page:
            print(
                "  ⚠ Es wurde eine Login-Seite geladen. Bitte im Debug-Chrome-Fenster einloggen."
            )
        elif auth and "anon" in auth.lower():
            print(
                "  ℹ Analytics meldet ANONYMOUS – meist unkritisch, weil die Detailseiten über die\n"
                "    Trip-ID erreichbar sind. Wenn Buchungen erkannt werden, ist alles gut."
            )

    return bookings


def _collect_embedded_json(page, cap_path=None) -> list[Any]:
    """Extrahiert JSON aus <script type=application/json>-Blöcken (SSR-Daten)."""
    out: list[Any] = []
    try:
        handles = page.query_selector_all('script[type="application/json"]')
    except Exception:
        return out
    for i, el in enumerate(handles):
        try:
            txt = el.text_content() or ""
            if not txt.strip():
                continue
            data = json.loads(txt)
            out.append(data)
            if cap_path:
                (cap_path / f"embedded_{i:02d}.json").write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
        except Exception:
            continue
    return out
