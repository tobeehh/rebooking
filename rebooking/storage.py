"""Persistente Preis-Historie (einfache JSON-Datei)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import PriceResult


class PriceHistory:
    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / "price_history.json"
        self._data: dict[str, list[dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def record(self, result: PriceResult) -> None:
        entry = {
            "checked_at": result.checked_at.isoformat(timespec="seconds"),
            "price": result.current_price,
            "currency": result.currency,
            "ok": result.ok,
            "error": result.error,
        }
        self._data.setdefault(result.booking.id, []).append(entry)

    def lowest_seen(self, booking_id: str) -> float | None:
        prices = [
            e["price"]
            for e in self._data.get(booking_id, [])
            if e.get("ok") and e.get("price") is not None
        ]
        return min(prices) if prices else None

    def last_price(self, booking_id: str) -> float | None:
        for entry in reversed(self._data.get(booking_id, [])):
            if entry.get("ok") and entry.get("price") is not None:
                return entry["price"]
        return None

    def history(self, booking_id: str) -> list[dict[str, Any]]:
        return list(self._data.get(booking_id, []))
