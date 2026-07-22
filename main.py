#!/usr/bin/env python3
"""CLI-Einstiegspunkt für den Hotels.com Preis-Monitor.

Beispiele:
    python main.py check                 # einmal alle Buchungen prüfen
    python main.py check --once-per-day  # nur prüfen, wenn heute noch nicht geprüft
    python main.py list                  # konfigurierte Buchungen anzeigen
    python main.py check --mock          # ohne echten Browser (Testpreise)
    python main.py web                   # Web-UI starten
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from rebooking.config import Config
from rebooking.models import Booking, PriceResult
from rebooking.monitor import run
from rebooking.storage import PriceHistory


def _mock_fetch(booking: Booking, scraper) -> PriceResult:
    """Testabfrage ohne Browser: erzeugt einen Preis leicht unter dem bezahlten."""
    import hashlib

    seed = int(hashlib.sha1(booking.id.encode()).hexdigest(), 16) % 20
    # Immer etwas günstiger (0.80–0.99 des bezahlten Preises), um den Alert-Pfad zu zeigen.
    price = round(booking.paid_price * (0.80 + seed / 100.0), 2)
    return PriceResult(
        booking=booking,
        checked_at=datetime.now(),
        current_price=price,
        currency=booking.currency,
        ok=True,
        source_url=booking.url,
    )


def cmd_check(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    fetch = _mock_fetch if args.mock else None
    if args.once_per_day and _already_checked_today(config):
        print("Heute wurde bereits geprüft – überspringe (--once-per-day).")
        return 0
    if fetch:
        run(config, fetch=fetch)
    else:
        run(config)
    return 0


def _already_checked_today(config: Config) -> bool:
    history = PriceHistory(config.data_dir)
    today = date.today().isoformat()
    for booking in config.bookings:
        for entry in history.history(booking.id):
            if entry.get("checked_at", "").startswith(today):
                return True
    return False


def cmd_list(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    if not config.bookings:
        print("Keine Buchungen konfiguriert. Trage sie in config.yaml ein.")
        return 0
    print(f"{len(config.bookings)} Buchung(en):\n")
    for b in config.bookings:
        status = "aktiv" if b.is_active else "vergangen"
        print(f"• {b.name} [{status}]")
        print(f"    {b.checkin} → {b.checkout} ({b.nights} Nächte)")
        print(f"    {b.adults} Erw., {b.children} Kinder, {b.rooms} Zimmer")
        print(f"    bezahlt: {b.paid_price} {b.currency}")
        print(f"    id: {b.id}\n")
    return 0


def cmd_account(args: argparse.Namespace) -> int:
    from pathlib import Path

    import yaml

    from rebooking.account import fetch_account_bookings, login

    config = Config.load(args.config)
    if getattr(args, "headed", False):
        config.account.headless = False

    if args.action == "login":
        login(config.account)
        return 0

    # action == "import"
    capture = str(Path(config.data_dir) / "capture") if args.capture else None
    print("Lese Buchungen aus deinem Hotels.com-Konto ...")
    try:
        found = fetch_account_bookings(config.account, capture_dir=capture)
    except Exception as exc:  # noqa: BLE001
        print(f"\nFehler beim Laden der Reisen-Seite: {exc}\n")
        print("Versuche es mit sichtbarem Browser (oft robuster):")
        print("    python main.py account import --headed")
        print("Falls die Seite lädt, aber nichts erkannt wird:")
        print("    python main.py account import --headed --capture")
        return 1

    if not found:
        print(
            "Keine Buchungen erkannt.\n"
            "Tipp: Mit '--capture' erneut ausführen; die Rohantworten liegen dann in "
            f"{config.data_dir}/capture/ – damit lässt sich das Feld-Mapping justieren."
        )
        return 1

    print(f"\n{len(found)} Buchung(en) gefunden:\n")
    for b in found:
        price = b.get("paid_price")
        print(f"• {b['name']}  {b['checkin']} → {b['checkout']}  "
              f"{'Preis '+str(price)+' '+b.get('currency','') if price else '(Preis unbekannt)'}")

    if not args.merge:
        print("\n(Nur Anzeige. Mit '--merge' in config.yaml übernehmen.)")
        return 0

    cfg_path = Path(args.config)
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    raw = raw or {}
    existing = raw.setdefault("bookings", [])
    have = {(e.get("name"), str(e.get("checkin")), str(e.get("checkout"))) for e in existing}

    added = 0
    for b in found:
        key = (b["name"], b["checkin"], b["checkout"])
        if key in have:
            continue
        entry = {
            "name": b["name"],
            "url": b["url"],
            "checkin": b["checkin"],
            "checkout": b["checkout"],
            "adults": 2,
            "children": 0,
            "rooms": 1,
            "paid_price": b.get("paid_price") or 0,
            "currency": b.get("currency", "EUR"),
            "notes": "importiert – bitte Preis/Belegung prüfen",
        }
        existing.append(entry)
        have.add(key)
        added += 1

    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"\n{added} neue Buchung(en) in {cfg_path} übernommen.")
    if any(not b.get("paid_price") for b in found):
        print("Achtung: Bei einigen fehlt der Preis – bitte in der Web-UI ergänzen.")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from rebooking.webapp import create_app

    app = create_app(args.config)
    print(f"Web-UI läuft auf http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hotels.com Preis-Monitor")
    parser.add_argument("--config", default="config.yaml", help="Pfad zur Konfig (Standard: config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Preise aller Buchungen prüfen")
    p_check.add_argument("--mock", action="store_true", help="ohne echten Browser (Testdaten)")
    p_check.add_argument("--once-per-day", action="store_true", help="nur prüfen, wenn heute noch nicht geprüft")
    p_check.set_defaults(func=cmd_check)

    p_list = sub.add_parser("list", help="konfigurierte Buchungen anzeigen")
    p_list.set_defaults(func=cmd_list)

    p_acc = sub.add_parser("account", help="Buchungen aus dem Hotels.com-Konto importieren")
    p_acc.add_argument("action", choices=["login", "import"],
                       help="login: einmaliger Browser-Login | import: Buchungen einlesen")
    p_acc.add_argument("--capture", action="store_true", help="Rohantworten zum Justieren speichern")
    p_acc.add_argument("--merge", action="store_true", help="gefundene Buchungen in config.yaml übernehmen")
    p_acc.add_argument("--headed", action="store_true", help="Import im sichtbaren Browser (robuster gegen Bot-Schutz)")
    p_acc.set_defaults(func=cmd_account)

    p_web = sub.add_parser("web", help="Web-UI starten")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8000)
    p_web.add_argument("--debug", action="store_true")
    p_web.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
