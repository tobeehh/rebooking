"""Web-UI: Buchungen/Einstellungen verwalten und Preis-Historie visualisieren.

Leichtgewichtige Flask-App. Charts werden mit Chart.js gerendert (per CDN).
Die App liest berechnete Werte über :class:`Config`, schreibt Änderungen aber
direkt in die YAML-Datei, damit ``${ENV}``-Platzhalter erhalten bleiben.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

from .config import Config
from .models import Booking
from .storage import PriceHistory

# ---------------------------------------------------------------------------
# YAML-Helfer (roh, ohne Env-Interpolation) für das Speichern von Änderungen.
# ---------------------------------------------------------------------------


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"currency": "EUR", "notifications": {}, "scraper": {}, "bookings": []}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _save_raw(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

BASE = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} · Rebooking</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root { --bg:#0f172a; --card:#1e293b; --fg:#e2e8f0; --muted:#94a3b8;
            --accent:#38bdf8; --good:#34d399; --bad:#f87171; --border:#334155; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
           background:var(--bg); color:var(--fg); }
    header { padding:16px 24px; border-bottom:1px solid var(--border);
             display:flex; align-items:center; gap:24px; }
    header h1 { font-size:18px; margin:0; }
    header a { color:var(--muted); text-decoration:none; font-size:14px; }
    header a:hover, header a.active { color:var(--accent); }
    main { max-width:1000px; margin:0 auto; padding:24px; }
    .card { background:var(--card); border:1px solid var(--border); border-radius:12px;
            padding:20px; margin-bottom:20px; }
    table { width:100%; border-collapse:collapse; }
    th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--border); font-size:14px; }
    th { color:var(--muted); font-weight:600; }
    .good { color:var(--good); } .bad { color:var(--bad); } .muted { color:var(--muted); }
    .badge { font-size:12px; padding:2px 8px; border-radius:999px; background:#0b213a; color:var(--accent); }
    a.btn, button.btn { display:inline-block; background:var(--accent); color:#04283a;
            border:none; padding:9px 16px; border-radius:8px; font-weight:600; cursor:pointer;
            text-decoration:none; font-size:14px; }
    a.btn.secondary, button.btn.secondary { background:transparent; color:var(--accent);
            border:1px solid var(--accent); }
    input,select,textarea { width:100%; padding:9px 11px; border-radius:8px;
            border:1px solid var(--border); background:#0b1220; color:var(--fg); font-size:14px; }
    label { display:block; font-size:13px; color:var(--muted); margin:12px 0 4px; }
    .row { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
    .row3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }
    .flash { background:#064e3b; color:#d1fae5; padding:10px 14px; border-radius:8px; margin-bottom:16px; }
    h2 { font-size:16px; }
    canvas { max-height:320px; }
  </style>
</head>
<body>
  <header>
    <h1>🏨 Rebooking</h1>
    <nav>
      <a href="{{ url_for('index') }}" class="{{ 'active' if active=='index' else '' }}">Übersicht</a>
      <a href="{{ url_for('settings') }}" class="{{ 'active' if active=='settings' else '' }}">Einstellungen</a>
    </nav>
  </header>
  <main>
    {% if flash %}<div class="flash">{{ flash }}</div>{% endif %}
    {{ body|safe }}
  </main>
</body>
</html>
"""

INDEX = """
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <h2 style="margin:0;">Beobachtete Buchungen</h2>
    <form method="post" action="{{ url_for('check_now') }}" style="margin:0;">
      <label style="display:inline;margin:0;">
        <input type="checkbox" name="mock" style="width:auto;"> Test (ohne Browser)
      </label>
      <button class="btn" type="submit">Jetzt prüfen</button>
    </form>
  </div>
</div>

{% if rows %}
<div class="card">
  <table>
    <tr><th>Hotel</th><th>Zeitraum</th><th>Bezahlt</th><th>Aktuell</th><th>Differenz</th><th></th></tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.name }} {% if not r.active %}<span class="badge">vergangen</span>{% endif %}</td>
      <td class="muted">{{ r.checkin }} → {{ r.checkout }}</td>
      <td>{{ r.paid }}</td>
      <td>{{ r.current }}</td>
      <td class="{{ 'good' if r.diff_val and r.diff_val > 0 else ('bad' if r.diff_val and r.diff_val < 0 else 'muted') }}">
        {{ r.diff }}</td>
      <td><a href="{{ url_for('booking_detail', booking_id=r.id) }}">Verlauf →</a></td>
    </tr>
    {% endfor %}
  </table>
</div>
{% else %}
<div class="card muted">Noch keine Buchungen. Lege welche unter „Einstellungen“ an.</div>
{% endif %}
"""

DETAIL = """
<div class="card">
  <h2 style="margin-top:0;">{{ name }}</h2>
  <p class="muted">{{ checkin }} → {{ checkout }} · bezahlt {{ paid }}</p>
  <canvas id="chart"></canvas>
</div>
<a class="btn secondary" href="{{ url_for('index') }}">← Zurück</a>
<script>
const labels = {{ labels|tojson }};
const prices = {{ prices|tojson }};
const paid = {{ paid_val }};
new Chart(document.getElementById('chart'), {
  type: 'line',
  data: { labels, datasets: [
    { label:'Preis', data:prices, borderColor:'#38bdf8', backgroundColor:'rgba(56,189,248,.15)',
      tension:.25, spanGaps:true, fill:true },
    { label:'Bezahlt', data:labels.map(()=>paid), borderColor:'#f87171', borderDash:[6,6],
      pointRadius:0, fill:false }
  ]},
  options: { plugins:{legend:{labels:{color:'#e2e8f0'}}},
    scales:{ x:{ticks:{color:'#94a3b8'}}, y:{ticks:{color:'#94a3b8'}} } }
});
</script>
"""

SETTINGS = """
<div class="card">
  <h2 style="margin-top:0;">Benachrichtigungen</h2>
  <form method="post" action="{{ url_for('save_settings') }}">
    <div class="row3">
      <div>
        <label>Kanal</label>
        <select name="channel">
          {% for c in ['console','email','telegram'] %}
          <option value="{{ c }}" {{ 'selected' if n.channel==c else '' }}>{{ c }}</option>
          {% endfor %}
        </select>
      </div>
      <div><label>Min. Ersparnis (Betrag)</label>
        <input name="min_drop_absolute" value="{{ n.min_drop_absolute }}"></div>
      <div><label>Min. Ersparnis (%)</label>
        <input name="min_drop_percent" value="{{ n.min_drop_percent }}"></div>
    </div>
    <p class="muted" style="font-size:12px;margin-top:14px;">
      E-Mail-/Telegram-Zugangsdaten werden aus Umgebungsvariablen gelesen
      (${SMTP_PASSWORD} usw.) und hier nicht angezeigt.</p>
    <div style="margin-top:12px;"><button class="btn" type="submit">Speichern</button></div>
  </form>
</div>

<div class="card">
  <h2 style="margin-top:0;">Buchungen</h2>
  <table>
    <tr><th>Hotel</th><th>Zeitraum</th><th>Bezahlt</th><th></th></tr>
    {% for b in bookings %}
    <tr>
      <td>{{ b.name }}</td>
      <td class="muted">{{ b.checkin }} → {{ b.checkout }}</td>
      <td>{{ b.paid_price }} {{ b.currency }}</td>
      <td><form method="post" action="{{ url_for('delete_booking', idx=loop.index0) }}" style="margin:0;">
        <button class="btn secondary" type="submit">Löschen</button></form></td>
    </tr>
    {% endfor %}
  </table>
</div>

<div class="card">
  <h2 style="margin-top:0;">Buchung hinzufügen</h2>
  <form method="post" action="{{ url_for('add_booking') }}">
    <label>Name</label><input name="name" required placeholder="Hotel Berlin Mitte">
    <label>Hotels.com-Link</label>
    <input name="url" required placeholder="https://www.hotels.com/ho123456/">
    <div class="row">
      <div><label>Check-in</label><input name="checkin" type="date" required></div>
      <div><label>Check-out</label><input name="checkout" type="date" required></div>
    </div>
    <div class="row3">
      <div><label>Erwachsene</label><input name="adults" type="number" value="2" min="1"></div>
      <div><label>Kinder</label><input name="children" type="number" value="0" min="0"></div>
      <div><label>Zimmer</label><input name="rooms" type="number" value="1" min="1"></div>
    </div>
    <div class="row">
      <div><label>Bezahlter Gesamtpreis</label><input name="paid_price" type="number" step="0.01" required></div>
      <div><label>Währung</label><input name="currency" value="EUR"></div>
    </div>
    <label>Notiz (optional)</label><input name="notes" placeholder="Doppelzimmer, inkl. Frühstück">
    <div style="margin-top:14px;"><button class="btn" type="submit">Hinzufügen</button></div>
  </form>
</div>
"""


def _fmt(value: float | None, currency: str) -> str:
    if value is None:
        return "–"
    return f"{value:,.2f} {currency}".replace(",", "X").replace(".", ",").replace("X", ".")


def create_app(config_path: str = "config.yaml") -> Flask:
    app = Flask(__name__)
    config_path = Path(config_path)
    # Für Flash-Meldungen ohne Session brauchen wir keinen Secret-Key; wir nutzen Query-Param.

    def render(tpl: str, title: str, active: str, **ctx) -> str:
        body = render_template_string(tpl, **ctx)
        return render_template_string(
            BASE, title=title, active=active, body=body, flash=request.args.get("msg")
        )

    @app.route("/")
    def index():  # noqa: ANN202
        config = Config.load(config_path)
        history = PriceHistory(config.data_dir)
        rows = []
        for b in config.bookings:
            last = history.last_price(b.id)
            diff_val = round(b.paid_price - last, 2) if last is not None else None
            rows.append(
                {
                    "id": b.id,
                    "name": b.name,
                    "active": b.is_active,
                    "checkin": b.checkin.isoformat(),
                    "checkout": b.checkout.isoformat(),
                    "paid": _fmt(b.paid_price, b.currency),
                    "current": _fmt(last, b.currency),
                    "diff": _fmt(diff_val, b.currency) if diff_val is not None else "–",
                    "diff_val": diff_val,
                }
            )
        return render(INDEX, "Übersicht", "index", rows=rows)

    @app.route("/booking/<booking_id>")
    def booking_detail(booking_id: str):  # noqa: ANN202
        config = Config.load(config_path)
        history = PriceHistory(config.data_dir)
        booking = next((b for b in config.bookings if b.id == booking_id), None)
        if booking is None:
            return redirect(url_for("index", msg="Buchung nicht gefunden"))
        entries = [e for e in history.history(booking_id) if e.get("ok")]
        labels = [e["checked_at"][:10] for e in entries]
        prices = [e["price"] for e in entries]
        return render(
            DETAIL,
            booking.name,
            "index",
            name=booking.name,
            checkin=booking.checkin.isoformat(),
            checkout=booking.checkout.isoformat(),
            paid=_fmt(booking.paid_price, booking.currency),
            paid_val=booking.paid_price,
            labels=labels,
            prices=prices,
        )

    @app.route("/settings")
    def settings():  # noqa: ANN202
        raw = _load_raw(config_path)
        n = raw.get("notifications", {}) or {}
        n.setdefault("channel", "console")
        n.setdefault("min_drop_absolute", 1.0)
        n.setdefault("min_drop_percent", 0.0)
        return render(
            SETTINGS, "Einstellungen", "settings",
            n=n, bookings=raw.get("bookings", []) or [],
        )

    @app.route("/settings/save", methods=["POST"])
    def save_settings():  # noqa: ANN202
        raw = _load_raw(config_path)
        n = raw.setdefault("notifications", {})
        n["channel"] = request.form.get("channel", "console")
        n["min_drop_absolute"] = float(request.form.get("min_drop_absolute", 1) or 1)
        n["min_drop_percent"] = float(request.form.get("min_drop_percent", 0) or 0)
        _save_raw(config_path, raw)
        return redirect(url_for("settings", msg="Einstellungen gespeichert."))

    @app.route("/booking/add", methods=["POST"])
    def add_booking():  # noqa: ANN202
        f = request.form
        entry = {
            "name": f["name"],
            "url": f["url"],
            "checkin": f["checkin"],
            "checkout": f["checkout"],
            "adults": int(f.get("adults", 2) or 2),
            "children": int(f.get("children", 0) or 0),
            "rooms": int(f.get("rooms", 1) or 1),
            "paid_price": float(f["paid_price"]),
            "currency": f.get("currency", "EUR") or "EUR",
            "notes": f.get("notes", ""),
        }
        # Validierung über das Datenmodell.
        try:
            Booking(**{k: v for k, v in entry.items()})
        except (ValueError, KeyError) as exc:
            return redirect(url_for("settings", msg=f"Ungültige Buchung: {exc}"))
        raw = _load_raw(config_path)
        raw.setdefault("bookings", []).append(entry)
        _save_raw(config_path, raw)
        return redirect(url_for("settings", msg="Buchung hinzugefügt."))

    @app.route("/booking/delete/<int:idx>", methods=["POST"])
    def delete_booking(idx: int):  # noqa: ANN202
        raw = _load_raw(config_path)
        bookings = raw.get("bookings", []) or []
        if 0 <= idx < len(bookings):
            removed = bookings.pop(idx)
            _save_raw(config_path, raw)
            return redirect(url_for("settings", msg=f"„{removed.get('name')}“ gelöscht."))
        return redirect(url_for("settings", msg="Buchung nicht gefunden."))

    @app.route("/check", methods=["POST"])
    def check_now():  # noqa: ANN202
        from .monitor import run as run_monitor

        mock = request.form.get("mock") == "on"
        config = Config.load(config_path)

        def _worker():
            if mock:
                import main as cli

                run_monitor(config, fetch=cli._mock_fetch, verbose=False)
            else:
                run_monitor(config, verbose=False)

        threading.Thread(target=_worker, daemon=True).start()
        return redirect(url_for("index", msg="Prüfung gestartet – lädt im Hintergrund. Seite in ~1 Min. neu laden."))

    @app.route("/api/history/<booking_id>")
    def api_history(booking_id: str):  # noqa: ANN202
        config = Config.load(config_path)
        history = PriceHistory(config.data_dir)
        return jsonify(history.history(booking_id))

    return app
