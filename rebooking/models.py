"""Datenmodelle für Buchungen und Preis-Ergebnisse."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


@dataclass
class Booking:
    """Eine einzelne beobachtete Buchung."""

    name: str
    url: str
    checkin: date
    checkout: date
    paid_price: float
    adults: int = 2
    children: int = 0
    child_ages: list[int] = field(default_factory=list)
    rooms: int = 1
    currency: str = "EUR"
    notes: str = ""

    def __post_init__(self) -> None:
        self.checkin = _parse_date(self.checkin)
        self.checkout = _parse_date(self.checkout)
        if self.checkout <= self.checkin:
            raise ValueError(
                f"checkout ({self.checkout}) muss nach checkin ({self.checkin}) liegen "
                f"(Buchung: {self.name})"
            )
        self.paid_price = float(self.paid_price)

    @property
    def nights(self) -> int:
        return (self.checkout - self.checkin).days

    @property
    def id(self) -> str:
        """Stabile ID einer Buchung (Hotel + Zeitraum + Belegung)."""
        raw = f"{self.url}|{self.checkin}|{self.checkout}|{self.adults}|{self.children}|{self.rooms}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    @property
    def is_active(self) -> bool:
        """Buchungen, deren Check-in in der Vergangenheit liegt, werden ignoriert."""
        return self.checkin > date.today()


@dataclass
class PriceResult:
    """Ergebnis einer Preisabfrage."""

    booking: Booking
    checked_at: datetime
    current_price: Optional[float]
    currency: str
    ok: bool = True
    error: Optional[str] = None
    source_url: str = ""

    @property
    def savings(self) -> Optional[float]:
        if self.current_price is None:
            return None
        return round(self.booking.paid_price - self.current_price, 2)

    @property
    def savings_percent(self) -> Optional[float]:
        if self.current_price is None or self.booking.paid_price == 0:
            return None
        return round((self.booking.paid_price - self.current_price) / self.booking.paid_price * 100, 1)

    @property
    def is_cheaper(self) -> bool:
        return self.savings is not None and self.savings > 0
