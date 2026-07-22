# 🏨 Rebooking – Hotels.com Preis-Monitor

Beobachtet deine **Hotels.com-Buchungen** und prüft **einmal täglich**, ob
dasselbe Hotel für denselben Zeitraum inzwischen **günstiger** buchbar ist.
Wird ein niedrigerer Preis gefunden, bekommst du eine Benachrichtigung
(E-Mail, Telegram oder Konsole) – oft lässt sich dann bei einer Rate mit
kostenloser Stornierung günstiger umbuchen.

Enthält eine **Web-UI** zum Verwalten der Buchungen/Einstellungen und zur
**Visualisierung der Preis-Historie**.

---

## ⚠️ Wichtig zu wissen

Hotels.com bietet **keine öffentliche API** und setzt Bot-Schutz ein. Der
Monitor automatisiert daher einen echten Browser (Playwright/Chromium) und
liest den angezeigten Preis aus. Das bedeutet:

- **Nur für privaten Gebrauch**, eine Abfrage pro Buchung und Tag – bewusst
  sparsam, um nicht aufzufallen.
- **Wartung nötig:** Ändert Hotels.com sein Seitenlayout, muss ggf. der
  Selektor angepasst werden (`scraper.price_selectors` in der Konfig).
- Findet der Bot keinen Preis, meldet er einen **Fehler** statt eines
  falschen Werts – so gibt es keine Fehlalarme.

---

## Schnellstart

```bash
# 1. Abhängigkeiten
pip install -r requirements.txt
python -m playwright install chromium

# 2. Konfiguration anlegen
cp config.example.yaml config.yaml
cp .env.example .env          # E-Mail-/Telegram-Zugangsdaten eintragen

# 3. Buchungen eintragen – am einfachsten über die Web-UI:
python main.py web            # -> http://127.0.0.1:8000

# 4. Einmal prüfen
set -a; source .env; set +a
python main.py check
```

### Ohne echten Browser testen

```bash
python main.py check --mock   # erzeugt Testpreise, gut zum Ausprobieren
```

---

## Buchung anlegen

Am komfortabelsten über die **Web-UI → Einstellungen**. Du brauchst:

| Feld            | Beschreibung                                                        |
|-----------------|---------------------------------------------------------------------|
| Name            | frei wählbar, z. B. „Hotel Adlon Berlin“                            |
| Hotels.com-Link | Hotelseite auf hotels.com öffnen, **komplette URL** kopieren        |
| Check-in/-out   | dein Reisezeitraum                                                   |
| Belegung        | Erwachsene / Kinder / Zimmer                                         |
| Bezahlter Preis | der **Gesamtpreis**, den du bezahlt hast (Vergleichsbasis)          |

Datum und Belegung setzt der Bot selbst in die URL – die kopierte Hotel-URL
darf also generisch sein.

---

## Buchungen automatisch aus dem Konto importieren (optional)

Statt Buchungen manuell einzutragen, kannst du sie aus deinem Hotels.com-Konto
importieren. Hotels.com hat **keine offene API** und schützt Login + interne
API mit Bot-Erkennung und 2FA – ein reiner HTTP-Client wäre nicht dauerhaft
stabil. Deshalb nutzt der Import einen **echten Browser mit einmalig
gespeicherter Session**:

```bash
# 1. Einmalig einloggen (Browser öffnet sich sichtbar, inkl. 2FA)
python main.py account login

# 2. Buchungen einlesen (nur anzeigen)
python main.py account import

# 3. In config.yaml übernehmen
python main.py account import --merge
```

Der Import liest „Meine Reisen“ im eingeloggten Kontext und fängt die internen
GraphQL-/JSON-Antworten ab. Die JSON-Struktur ist nicht dokumentiert und kann
sich ändern; der Parser ist deshalb tolerant. Falls nichts erkannt wird:

```bash
python main.py account import --capture   # Rohantworten -> data/capture/
```

Damit lässt sich das Feld-Mapping in `rebooking/account.py`
(`_NAME_KEYS`, `_CHECKIN_KEYS`, …) einmal an die echten Daten anpassen.

> Hinweis: Importierte Buchungen werden mit `paid_price` aus der Kontoansicht
> übernommen, wo verfügbar. Preis/Belegung bitte in der Web-UI kurz prüfen.
> Der Login läuft **lokal** (interaktiv) – in GitHub Actions ist der
> Konto-Import nicht sinnvoll; dort besser die manuelle/`--merge`-erzeugte
> `config.yaml` verwenden.

## Benachrichtigungen

In `config.yaml` unter `notifications`:

- `channel`: `console` | `email` | `telegram`
- `min_drop_absolute` / `min_drop_percent`: Es wird **nur** gemeldet, wenn die
  Ersparnis beide Schwellen überschreitet (z. B. mind. 5 € **und** 2 %).

Zugangsdaten kommen aus Umgebungsvariablen (`${SMTP_PASSWORD}` usw.) und
werden **nie** in die Konfig geschrieben. Gmail benötigt ein
[App-Passwort](https://myaccount.google.com/apppasswords).

---

## Täglich automatisch laufen lassen

### Variante A: GitHub Actions (kein eigener Server)

`.github/workflows/daily.yml` ist enthalten und läuft täglich ~09:00 Berlin.

1. Repo bei GitHub anlegen (privat).
2. Unter **Settings → Secrets and variables → Actions** anlegen:
   `SMTP_USERNAME`, `SMTP_PASSWORD` (und ggf. Telegram-Secrets).
3. Buchungen bereitstellen – entweder eine eingecheckte `config.ci.yaml`
   (ohne Secrets, nur `${...}`-Platzhalter) **oder** die komplette Konfig als
   Secret `REBOOKING_CONFIG`.

### Variante B: cron auf eigenem Rechner/Server

```cron
0 9 * * *  cd /pfad/zu/rebooking && set -a; . ./.env; set +a; \
           /usr/bin/python3 main.py check --once-per-day >> data/cron.log 2>&1
```

---

## Web-UI

```bash
python main.py web --host 127.0.0.1 --port 8000
```

- **Übersicht:** bezahlt vs. aktuell, Differenz, Button „Jetzt prüfen“.
- **Verlauf:** Preis-Historie je Buchung als Chart (mit Referenzlinie zum
  bezahlten Preis).
- **Einstellungen:** Buchungen anlegen/löschen, Benachrichtigungsschwellen.

---

## Projektstruktur

```
main.py                    CLI (check / list / web)
rebooking/
  models.py                Booking, PriceResult
  config.py                YAML + ${ENV}-Interpolation
  scraper.py               Playwright-Preisabfrage + Extraktion
  account.py               Konto-Import via persistente Browser-Session
  storage.py               Preis-Historie (JSON)
  notifier.py              Konsole / E-Mail / Telegram
  monitor.py               Orchestrierung
  webapp.py                Flask Web-UI
tests/                     pytest (ohne Netzwerk/Browser)
.github/workflows/daily.yml  täglicher Lauf
config.example.yaml        Vorlage (kopieren nach config.yaml)
```

## Tests

Reine Logik (ohne Netzwerk/Browser):

```bash
pip install pytest
python -m pytest -q tests/test_core.py
```

**Integrationstest mit echtem Browser** – verifiziert die komplette
Browser-Mechanik (Konto-Import via Response-Interception + Preis-Scraper)
gegen einen lokalen Fake-Server, **ohne Hotels.com und ohne Login**:

```bash
python -m pytest -q tests/test_integration.py
# vorhandenes Chrome nutzen statt Download:
REBOOKING_TEST_CHROME=/pfad/zu/chrome python -m pytest -q tests/test_integration.py
```

Der Test überspringt sich automatisch, wenn kein Chromium verfügbar ist.
Er beweist, dass Browserstart, das Abfangen der GraphQL-Antwort und das
Parsen der Buchungen real funktionieren – lediglich die Hotels.com-Gegenstelle
ist durch einen lokalen Server ersetzt (mangels echtem Login unvermeidbar).
