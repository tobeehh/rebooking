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
        if _DATE_RE.search(obj):
            return obj  # Datum ist nicht sensibel und hilft beim Mapping
        if len(obj) <= 18 and " " not in obj:
            return obj  # kurze Enums/Codes/Währungen behalten
        return f"<text len={len(obj)}>"
    return obj  # Zahlen, bool, None behalten


def inspect_capture(capture_dir: str | Path, max_chars: int = 6000) -> str:
    """Gibt eine geschwärzte Struktur-Übersicht der Mitschnitte zurück (Feldnamen)."""
    cap = Path(capture_dir)
    files = sorted(cap.glob("*.json")) if cap.exists() else []
    if not files:
        return f"Keine Mitschnitte in {cap}. Erst 'account import --cdp --capture' ausführen."

    out: list[str] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            out.append(f"### {f.name}: nicht lesbar ({exc})")
            continue
        # Auf trip-ähnliche Knoten fokussieren (mit Datum-Deszendent), sonst ganzes Objekt.
        nodes = _trip_like_nodes(data)
        if nodes:
            red = _redact(nodes[:5])
            head = f"### {f.name}  ({len(nodes)} datums-tragende Knoten)"
        else:
            red = _redact(data)
            head = f"### {f.name}  (kein Datum gefunden – ganze Struktur)"
        blob = json.dumps(red, indent=2, ensure_ascii=False)
        if len(blob) > max_chars:
            blob = blob[:max_chars] + "\n…(gekürzt)"
        out.append(head + "\n" + blob)
    return "\n\n".join(out)


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
            # schlanke Knoten mit etwas Inhalt (nicht die Riesen-Wurzel, nicht der
            # reine {isoDate:…}-Wrapper)
            if 3 <= len(node) <= 40:
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

        if verbose:
            print(f"  Modus: {'CDP (eigener Chrome)' if use_cdp else 'eigener Browser'} · Ziel: {account.trips_url}")

        # Bei CDP eine eigene Seite öffnen (bestehende Tabs des Nutzers nicht stören).
        page = ctx.new_page() if use_cdp else (ctx.pages[0] if ctx.pages else ctx.new_page())
        page.on("response", on_response)
        _goto_with_retry(page, account.trips_url)
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
        low = (final_url + " " + final_title).lower()
        if any(w in low for w in ("sign in", "log in", "anmelden", "login")):
            print("  ⚠ Sieht nach Login-Seite aus – bist du im geöffneten Chrome eingeloggt?")

    bookings: list[dict] = []
    seen: set[tuple] = set()
    for body in captured + embedded:
        for b in parse_trips(body):
            key = (b["name"], b["checkin"], b["checkout"])
            if key not in seen:
                seen.add(key)
                bookings.append(b)
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
