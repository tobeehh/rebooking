"""Orchestrierung: alle Buchungen prüfen, Historie speichern, ggf. melden."""

from __future__ import annotations

import time
from typing import Callable

from .config import Config
from .models import Booking, PriceResult
from .notifier import Notifier, build_message, should_alert
from .scraper import fetch_price
from .storage import PriceHistory

# Signatur einer Preis-Abfragefunktion (für Tests injizierbar).
FetchFn = Callable[[Booking, object], PriceResult]


def run(config: Config, fetch: FetchFn = fetch_price, verbose: bool = True) -> list[PriceResult]:
    history = PriceHistory(config.data_dir)
    notifier = Notifier(config.notifications)

    results: list[PriceResult] = []
    active = [b for b in config.bookings if b.is_active]
    skipped = len(config.bookings) - len(active)

    if verbose:
        print(f"Prüfe {len(active)} aktive Buchung(en)" + (f" ({skipped} vergangene übersprungen)" if skipped else ""))

    for i, booking in enumerate(active):
        if verbose:
            print(f"  → {booking.name} ({booking.checkin} → {booking.checkout}) ...", flush=True)
        result = fetch(booking, config.scraper)
        results.append(result)
        history.record(result)

        if verbose:
            if result.ok:
                cmp = "günstiger" if result.is_cheaper else "nicht günstiger"
                print(
                    f"     aktuell {result.current_price} {result.currency} "
                    f"(bezahlt {booking.paid_price}) → {cmp}"
                )
            else:
                print(f"     Fehler: {result.error}")

        # Höflichkeitsverzögerung zwischen Abfragen.
        if i < len(active) - 1 and config.scraper.delay_between_seconds > 0:
            time.sleep(config.scraper.delay_between_seconds)

    history.save()

    alerts = [r for r in results if r.ok and should_alert(r, config.notifications)]
    if alerts:
        subject, body = build_message(alerts)
        notifier.send(subject, body)
        if verbose:
            print(f"\n✅ Benachrichtigung gesendet ({len(alerts)} günstigere Buchung(en)).")
    elif verbose:
        print("\nKeine günstigere Option gefunden – keine Benachrichtigung.")

    return results
