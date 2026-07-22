"""Benachrichtigungen: Konsole, E-Mail (SMTP), Telegram."""

from __future__ import annotations

import json
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage

from .config import NotificationConfig
from .models import PriceResult


def _fmt(value: float | None, currency: str) -> str:
    if value is None:
        return "–"
    return f"{value:,.2f} {currency}".replace(",", "X").replace(".", ",").replace("X", ".")


def should_alert(result: PriceResult, cfg: NotificationConfig) -> bool:
    """Alert nur, wenn günstiger UND über den konfigurierten Schwellen."""
    if not result.is_cheaper:
        return False
    savings = result.savings or 0
    if savings < cfg.min_drop_absolute:
        return False
    pct = result.savings_percent or 0
    if pct < cfg.min_drop_percent:
        return False
    return True


def build_message(results: list[PriceResult]) -> tuple[str, str]:
    """Erzeugt (Betreff, Text) für eine Liste günstiger Ergebnisse."""
    if len(results) == 1:
        r = results[0]
        subject = (
            f"💰 Günstiger: {r.booking.name} – "
            f"{_fmt(r.savings, r.currency)} sparen"
        )
    else:
        total = sum(r.savings or 0 for r in results)
        subject = f"💰 {len(results)} Buchungen günstiger – bis zu {_fmt(total, results[0].currency)} sparen"

    lines: list[str] = []
    for r in results:
        lines.append(f"🏨 {r.booking.name}")
        lines.append(
            f"   {r.booking.checkin} → {r.booking.checkout} "
            f"({r.booking.nights} Nächte, {r.booking.adults} Erw."
            + (f", {r.booking.children} Kinder" if r.booking.children else "")
            + ")"
        )
        lines.append(f"   Bezahlt:  {_fmt(r.booking.paid_price, r.currency)}")
        lines.append(f"   Aktuell:  {_fmt(r.current_price, r.currency)}")
        lines.append(
            f"   Ersparnis: {_fmt(r.savings, r.currency)} ({r.savings_percent} %)"
        )
        lines.append(f"   Link: {r.source_url}")
        if r.booking.notes:
            lines.append(f"   Notiz: {r.booking.notes}")
        lines.append("")

    lines.append(
        "Tipp: Bei Hotels.com kannst du oft kostenlos stornieren und neu buchen, "
        "wenn deine Rate 'kostenlose Stornierung' erlaubt. Prüfe die Bedingungen, "
        "bevor du umbuchst."
    )
    return subject, "\n".join(lines)


class Notifier:
    def __init__(self, cfg: NotificationConfig) -> None:
        self.cfg = cfg

    def send(self, subject: str, body: str) -> None:
        channel = (self.cfg.channel or "console").lower()
        if channel == "email":
            self._send_email(subject, body)
        elif channel == "telegram":
            self._send_telegram(subject, body)
        else:
            self._send_console(subject, body)

    # -- Kanäle ---------------------------------------------------------------

    def _send_console(self, subject: str, body: str) -> None:
        print("=" * 60)
        print(subject)
        print("=" * 60)
        print(body)

    def _send_email(self, subject: str, body: str) -> None:
        e = self.cfg.email
        required = ("smtp_host", "smtp_port", "username", "password", "to_addr")
        missing = [k for k in required if not e.get(k)]
        if missing:
            raise ValueError(
                f"E-Mail-Konfiguration unvollständig, fehlt: {', '.join(missing)}"
            )
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = e.get("from_addr") or e["username"]
        msg["To"] = e["to_addr"]
        msg.set_content(body)

        port = int(e["smtp_port"])
        host = e["smtp_host"]
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(e["username"], e["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(e["username"], e["password"])
                s.send_message(msg)

    def _send_telegram(self, subject: str, body: str) -> None:
        t = self.cfg.telegram
        token = t.get("bot_token")
        chat_id = t.get("chat_id")
        if not token or not chat_id:
            raise ValueError("Telegram-Konfiguration unvollständig (bot_token/chat_id).")
        text = f"*{subject}*\n\n{body}"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        ).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode())
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram-Fehler: {payload}")
