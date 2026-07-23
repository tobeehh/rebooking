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

### Welcher Preis verglichen wird

Auf einer Hotelseite stehen mehrere Zahlen nebeneinander, und nur eine ist
vergleichbar:

```
Der vorherige Preis war 759 €.      <- durchgestrichen, irrelevant
Der aktuelle Preis beträgt 391 €.   <- DAS ist der Vergleichswert
für 1 Apartment, 2 Nächte
195 € pro Nacht                     <- abgeleitet, NICHT vergleichbar
```

Verglichen wird der **günstigste aktuelle Gesamtpreis aus der Zimmerliste
dieser Unterkunft** (`[data-stid="section-room-list"]`). Zwei Fallen werden
dabei bewusst umgangen:

- **Nachtpreis statt Gesamtpreis.** Er ist immer kleiner – wer einfach das
  Minimum aller Zahlen nimmt, meldet dauerhaft falsche Ersparnisse.
- **Fremde Hotels.** Hotels.com blendet auf derselben Seite Vorschläge für
  ähnliche Unterkünfte ein. Deren Preise liegen außerhalb der Zimmerliste und
  werden nicht berücksichtigt.

Ist die Unterkunft für den Zeitraum **ausgebucht**, zeigt Hotels.com nur noch
Preise anderer Hotels. Das wird erkannt und als Fehler gemeldet – nicht als
günstiger Preis.

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

### Bot-Schutz umgehen: an den eigenen Chrome anbinden (CDP)

Hotels.com blockiert automatisierte Browser (auch sichtbare) mit hoher
Wahrscheinlichkeit. Der robusteste **legitime** Weg ist, keinen eigenen
Browser zu starten, sondern sich an deinen **echten, laufenden Chrome**
anzubinden – für die Bot-Erkennung sieht das aus wie du, nicht wie ein Bot.

```bash
# 1. Deinen Chrome mit Debug-Port + eigenem Profil starten:
python main.py chrome
#    -> im geöffneten Chrome bei Hotels.com einloggen (bleibt gespeichert)

# 2. In config.yaml eintragen:
#    account:
#      cdp_url: "http://127.0.0.1:9222"
#    scraper:
#      cdp_url: "http://127.0.0.1:9222"

# 3. In einem zweiten Terminal – Chrome offen lassen:
python main.py account import          # nutzt deinen echten Chrome
python main.py check                    # auch der Preis-Check läuft dann darüber
```

Der `chrome`-Befehl findet Chrome automatisch (macOS/Windows/Linux); sonst per
`--chrome-path` angeben. Das Profil liegt unter `data/chrome_profile` und ist
von deinem normalen Chrome-Profil getrennt – dein Alltags-Chrome bleibt
unberührt. Lässt du dieses Chrome-Fenster geöffnet, funktionieren Import und
täglicher Check ohne weiteres Zutun.

> Kein „Stealth"-Trick: es ist schlicht dein Browser mit deiner Session. Damit
> entfällt der Umweg über `account login`/`--headed`.

### Sprache der Kontoansicht

Hotels.com liefert „Meine Reisen“ je nach Session auf Deutsch oder Englisch –
mit unterschiedlichen Datums- und Preisformaten (`194,64 €` vs. `€194.64`) und
teilweise sogar anderer Währung (USD statt EUR). Damit die Extraktion
reproduzierbar ist, setzt der Import die Sprache explizit:

```yaml
account:
  locale: de-DE      # Standard
```

Entscheidend ist der **`?locale=de_DE`-Parameter in der URL** – er wird an alle
Navigationen angehängt. Die Domain allein genügt nicht: `de.hotels.com` liefert
ohne den Parameter weiterhin die englische Seite. Der Parameter wirkt auch im
**CDP-Modus**, wo sich weder Browser-Locale noch `Accept-Language` setzen
lassen. Ob es geklappt hat, zeigt die Zeile `Titel:` am Ende des Imports
(„Preise und Treuevorteile“ = deutsch, „Pricing and rewards“ = englisch).

Der Parser versteht **beide** Sprachen; die Einstellung sorgt nur dafür, dass
nicht mal so, mal so geparst wird.

### Stornierbarkeit

Ob eine Buchung kostenlos stornierbar ist, steht **weder** in der Übersicht
**noch** in den abgefangenen JSON-Antworten – sondern nur im gerenderten HTML
der Buchungsverwaltung (`/booking-servicing/lodging`). Deren URL baut der Import
aus Daten, die er ohnehin hat: `tripViewId` und `encodedTripItemId` aus dem
Detail-Link, `tripId` aus der „Reiseplan“-Nummer der Karte.

Maßgeblich ist die **aktuell geltende** Phase:

```
Vollständige Rückerstattung ab heute bis zum 26. Juli.   -> kostenlos, Frist 26.07.
Keine Rückerstattung ab heute bis zum Check-in.          -> nicht erstattbar
Teilerstattung ab heute bis zum Check-in.                -> nicht kostenlos
```

Eine Buchung, die erst *später* nur noch teilerstattbar wird, gilt heute noch
als kostenlos stornierbar – das Datum landet als `free_until` in der Ausgabe.
Lässt sich nichts eindeutig lesen, bleibt der Status **unbekannt** statt geraten.

> **Diese Seite wird ausschließlich gelesen.** Sie enthält den Knopf „Unterkunft
> stornieren“; deshalb klickt der Import dort grundsätzlich nichts an (auch
> keine Overlay-Buttons – dort wird höchstens `Escape` gedrückt).

Vergangene Aufenthalte werden erkannt (`past`) und beim `--merge` übersprungen.

### Popups

Beim ersten Aufruf blendet Hotels.com einen App-Hinweis über die Seite. Der
Import klickt solche Overlays selbstständig weg, damit ein unbeaufsichtigter
Lauf (cron/GitHub Actions) nicht daran hängen bleibt. Geklickt wird
**ausschließlich** innerhalb eines erkannten Dialogs und nur auf eindeutige
Beschriftungen wie „Schließen“, „Nicht jetzt“ oder „Weiter im Browser“ – ein
blankes „Stornieren“/„Cancel“ ist bewusst ausgeschlossen, damit nie versehentlich
eine Buchung storniert wird. Findet sich kein passender Knopf, wird `Escape`
gedrückt.

### Ohne CDP (einfacher Automatik-Browser)

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

### Variante C: Synology Docker (empfohlen für Dauerbetrieb)

Enthalten sind `Dockerfile`, `docker-compose.yml` und `config.docker.yaml`.

**Warum der Container einen Bildschirm braucht.** Zwei gemessene Randbedingungen
bestimmen den Aufbau:

| Modus | Ergebnis |
|---|---|
| headless-Chromium | **blockiert** (`ERR_HTTP2_PROTOCOL_ERROR` / Timeout) |
| headful, aber abgemeldet | lädt, zeigt aber **Listenpreise**: 759 € statt 391 € |
| headful + eingeloggt | korrekte Mitgliederpreise |

Chromium läuft deshalb **headful unter Xvfb**, und die Session muss bestehen.

**Der kurze Weg: Image aus der Registry ziehen**

`.github/workflows/image.yml` baut das Image bei jedem Push für **amd64 und
arm64** und legt es in der GitHub Container Registry ab. Auf der NAS wird dann
nichts mehr gebaut – du brauchst dort nur drei Dateien:

```
/volume1/docker/rebooking/
├── docker-compose.yml          # zieht ghcr.io/tobeehh/rebooking:latest
├── .env                        # VNC_PASSWORD=…
└── data/config.yaml            # aus config.docker.yaml
```

Container Manager → **Registry** kann `ghcr.io` durchsuchen; einfacher ist,
direkt ein **Projekt** mit obiger `docker-compose.yml` anzulegen – der Pull
passiert dann automatisch. Aktualisieren später:

```bash
sudo docker compose -f /volume1/docker/rebooking/docker-compose.yml pull
sudo docker compose -f /volume1/docker/rebooking/docker-compose.yml up -d
```

> Nach dem allerersten Push muss das Paket einmal auf **öffentlich** gestellt
> werden: GitHub → Repo → *Packages* → `rebooking` → *Package settings* →
> *Change visibility* → **Public**. Sonst bräuchte die NAS Zugangsdaten
> (Container Manager → Registry → mit GitHub-Token `read:packages`).

Selbst bauen geht weiterhin – dann `docker-compose.build.yml` verwenden und
den kompletten Quellcode wie unten beschrieben übertragen.

**Schritt 1 – Dateien auf die NAS**

Ziel ist `/volume1/docker/rebooking/` (der Ordner `docker` entsteht mit der
Installation von Container Manager).

*Variante A – ohne SSH, über File Station:* Ordner `rebooking` unter `docker`
anlegen und diese Dateien hineinziehen:

```
Dockerfile   docker-compose.yml   requirements.txt   main.py
rebooking/   docker/
```

**Nicht** mitkopieren: `.venv/`, `data/`, `.git/`. Besonders `data/chrome_profile`
– das ist ein Zugang zu deinem Hotels.com-Konto, und die macOS-Cookies sind über
die Keychain verschlüsselt und unter Linux ohnehin wertlos.

*Variante B – per SSH* (DSM → Systemsteuerung → Terminal & SNMP → SSH aktivieren):

```bash
rsync -avz --delete \
  --exclude '.venv' --exclude 'data' --exclude '.git' --exclude '.github' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.DS_Store' \
  --exclude 'config.yaml' \
  ./ dein-user@NAS-IP:/volume1/docker/rebooking/
```

**Schritt 2 – Konfiguration und Passwort**

```bash
ssh dein-user@NAS-IP
cd /volume1/docker/rebooking
mkdir -p data
cp config.docker.yaml data/config.yaml     # to_addr anpassen
printf 'VNC_PASSWORD=EinGutesPasswort\n' > .env
```

Ohne `.env` verweigert die Compose-Datei absichtlich den Start – sonst stünde
die Fernsteuerung deines Kontos ungeschützt im Netz. Für E-Mail-Versand
zusätzlich `SMTP_USERNAME=` und `SMTP_PASSWORD=` in dieselbe `.env`.

**Schritt 3 – Projekt anlegen**

Container Manager → **Projekt** → **Erstellen** → Pfad
`/volume1/docker/rebooking`, Quelle „vorhandene docker-compose.yml verwenden“ →
**Erstellen**. Das Image wird auf der NAS gebaut, dadurch stimmt die
Architektur (amd64/arm64) automatisch. Der erste Bau dauert einige Minuten und
braucht rund 5 GB Platz.

**Schritt 4 – Anmelden und Buchungen holen**

```bash
# 1. Im Browser: http://NAS-IP:6080/vnc.html  -> bei Hotels.com anmelden (2FA)
# 2. Session prüfen:
sudo docker exec -i rebooking python /app/main.py --config /data/config.yaml account status
# 3. Buchungen importieren:
sudo docker exec -i rebooking python /app/main.py --config /data/config.yaml account import --merge
# 4. Einmal prüfen:
sudo docker exec -i rebooking python /app/main.py --config /data/config.yaml check
```

`docker exec` braucht **`-i`**, sonst kommt bei manchen Befehlen nichts an.

**Einmaliger Login mit 2FA**

```
http://<NAS-IP>:6080/vnc.html
```

Dort siehst du den Chromium des Containers. Melde dich ganz normal bei
Hotels.com an – inklusive 2FA-Code. Das Profil liegt unter
`/data/chrome_profile` im Volume und überlebt Neustarts und Updates.

Danach prüfen:

```bash
docker exec rebooking python /app/main.py account status --config /data/config.yaml
# -> ✅ Session aktiv als <Name>
```

> **Sicherheit:** Wer die noVNC-Oberfläche erreicht, bedient dein eingeloggtes
> Hotels.com-Konto vollständig – inklusive Stornierungen. Nur im LAN freigeben,
> **niemals** per Portweiterleitung ins Internet, und immer `VNC_PASSWORD`
> setzen. Wenn du den Port nach dem Login nicht mehr brauchst, nimm ihn aus der
> Compose-Datei heraus und öffne ihn erst wieder, wenn die Session abläuft.

**Betrieb.** Der Container prüft dann selbstständig einmal täglich
(`CHECK_INTERVAL_SECONDS`, Standard 86400). Manuell anstoßen:

```bash
docker exec rebooking python /app/main.py check --config /data/config.yaml
```

**Wenn die Session abläuft** (erfahrungsgemäß nach Wochen bis Monaten), meldet
der Check `Nicht eingeloggt – es kämen nur Listenpreise`. Dann noVNC öffnen und
neu anmelden. Ein Login ohne Browser ist nicht möglich: Hotels.com hat keine
öffentliche API und keine Token-Authentifizierung.

> **Kein GitHub Actions.** Variante A funktioniert für den Preis-Check *nicht*:
> Datacenter-IP, kein persistentes Profil, kein Login – es kämen bestenfalls
> Listenpreise. Eine NAS im Heimnetz ist deutlich unauffälliger.

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
pip install -r requirements-dev.txt   # installiert u.a. pytest
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
