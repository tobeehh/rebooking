# Rebooking – Hotels.com Preis-Monitor für Synology Docker (Container Manager)
#
# Warum so aufwendig und nicht einfach "headless"?
#   * Hotels.com blockiert headless-Chromium aktiv (gemessen: ERR_HTTP2_PROTOCOL_ERROR
#     bzw. Timeout schon beim Laden). Der Browser läuft deshalb HEADFUL unter Xvfb.
#   * Ohne Login liefert Hotels.com Listen- statt Mitgliederpreise (gemessen:
#     759 € statt 391 € für dieselbe Buchung). Die Session muss also bestehen –
#     und dafür braucht es einmalig einen echten Login inklusive 2FA.
#   * Den 2FA-Login erledigst du über noVNC im Browser: du siehst den Chromium
#     des Containers und meldest dich dort ganz normal an. Das Profil liegt im
#     Volume und überlebt Neustarts.
#
# Das Basis-Image bringt Chromium samt aller Systembibliotheken mit und gibt es
# für amd64 UND arm64 – baue es einfach auf der Synology selbst, dann passt die
# Architektur automatisch.
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Xvfb ist im Basis-Image enthalten; VNC/noVNC kommen dazu.
RUN apt-get update && apt-get install -y --no-install-recommends \
        x11vnc \
        novnc \
        websockify \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Ohne index.html zeigt der Aufruf von "/" nur die Dateiliste des noVNC-Ordners.
# Die Startseite leitet deshalb direkt auf den Client weiter – autoconnect spart
# einen Klick, resize=scale passt das Bild ans Browserfenster an.
RUN printf '%s\n' \
    '<!doctype html><html><head><meta charset="utf-8">' \
    '<title>Rebooking – Browser-Anmeldung</title>' \
    '<meta http-equiv="refresh" content="0; url=vnc.html?autoconnect=true&amp;resize=scale">' \
    '</head><body>Weiterleitung zu <a href="vnc.html?autoconnect=true&amp;resize=scale">vnc.html</a> …' \
    '</body></html>' > /usr/share/novnc/index.html

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY rebooking/ ./rebooking/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# /data hält Profil, Konfiguration und Preis-Historie – als Volume mounten.
VOLUME ["/data"]
# 6080 = noVNC (Anmeldung), 8000 = Web-UI (Uebersicht/Verlauf/Einstellungen)
EXPOSE 6080 8000

# tini als PID 1: sonst bleiben Chromium-/Xvfb-Zombies zurück.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
