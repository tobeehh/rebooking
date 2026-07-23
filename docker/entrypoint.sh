#!/usr/bin/env bash
# Startet Xvfb + Chromium (headful, mit Debug-Port) + noVNC und fährt danach
# den gewählten Modus. Headless ist bewusst KEINE Option: Hotels.com blockiert
# headless-Chromium, siehe Dockerfile.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
PROFILE_DIR="$DATA_DIR/chrome_profile"
CONFIG="${REBOOKING_CONFIG:-$DATA_DIR/config.yaml}"
CDP_PORT="${CDP_PORT:-9222}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
SCREEN="${SCREEN_SIZE:-1440x900x24}"
CHECK_INTERVAL="${CHECK_INTERVAL_SECONDS:-86400}"

mkdir -p "$PROFILE_DIR" "$DATA_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

if [ ! -f "$CONFIG" ]; then
  log "FEHLER: Keine Konfiguration unter $CONFIG."
  log "        config.example.yaml nach $DATA_DIR/config.yaml kopieren und anpassen."
  exit 1
fi

# --- Xvfb ------------------------------------------------------------------
log "Starte Xvfb auf $DISPLAY ($SCREEN)"
Xvfb "$DISPLAY" -screen 0 "$SCREEN" -nolisten tcp &
for _ in $(seq 1 30); do
  xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
  sleep 0.5
done

# --- Chromium --------------------------------------------------------------
# Pfad der von Playwright mitgelieferten Chromium-Binary erfragen – so passt er
# unabhängig von Image-Version und Architektur.
CHROME_BIN="$(python - <<'PY'
from playwright.sync_api import sync_playwright
p = sync_playwright().start()
print(p.chromium.executable_path)
p.stop()
PY
)"
log "Chromium: $CHROME_BIN"

"$CHROME_BIN" \
  --remote-debugging-port="$CDP_PORT" \
  --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$PROFILE_DIR" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  --lang="${CHROME_LANG:-de-DE}" \
  --window-position=0,0 \
  --window-size="${WINDOW_SIZE:-1440,900}" \
  "https://de.hotels.com/trips?locale=de_DE" \
  >"$DATA_DIR/chrome.log" 2>&1 &

# Auf den Debug-Port warten – ohne ihn kann sich Playwright nicht verbinden.
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then
    log "Debug-Port $CDP_PORT bereit"
    break
  fi
  sleep 1
done

# --- VNC / noVNC -----------------------------------------------------------
# Über diese Oberfläche erledigst du den einmaligen Login samt 2FA.
if [ -n "${VNC_PASSWORD:-}" ]; then
  mkdir -p /root/.vnc
  x11vnc -storepasswd "$VNC_PASSWORD" /root/.vnc/passwd >/dev/null 2>&1
  x11vnc -display "$DISPLAY" -rfbauth /root/.vnc/passwd -rfbport 5900 \
         -forever -shared -localhost -quiet >/dev/null 2>&1 &
  log "VNC mit Passwort gestartet"
else
  # Ohne Passwort NUR an localhost binden – websockify erreicht es trotzdem.
  x11vnc -display "$DISPLAY" -nopw -rfbport 5900 -forever -shared -localhost -quiet \
         >/dev/null 2>&1 &
  log "WARNUNG: VNC OHNE Passwort. Setze VNC_PASSWORD – über diese Oberfläche"
  log "         ist dein eingeloggtes Hotels.com-Konto vollständig bedienbar."
fi

websockify --web=/usr/share/novnc "$NOVNC_PORT" localhost:5900 >/dev/null 2>&1 &
log "noVNC erreichbar auf Port $NOVNC_PORT  ->  http://<NAS-IP>:$NOVNC_PORT/vnc.html"

# --- Modus -----------------------------------------------------------------
run_check() {
  log "Preis-Check startet"
  python /app/main.py --config "$CONFIG" check "$@" || log "Preis-Check meldete einen Fehler"
}

case "${1:-serve}" in
  serve)
    # Erst den Session-Status melden, damit im Log sofort sichtbar ist, ob ein
    # Login nötig ist – sonst liefe der Check monatelang mit Listenpreisen.
    python /app/main.py --config "$CONFIG" account status || true
    log "Intervall: alle ${CHECK_INTERVAL}s"
    while true; do
      run_check --once-per-day
      sleep "$CHECK_INTERVAL"
    done
    ;;
  check)
    shift || true
    run_check "$@"
    ;;
  login)
    log "Bitte im Browser http://<NAS-IP>:$NOVNC_PORT/vnc.html öffnen und dort"
    log "bei Hotels.com anmelden (inkl. 2FA). Danach: docker exec ... account status"
    tail -f /dev/null
    ;;
  shell)
    exec /bin/bash
    ;;
  *)
    shift || true
    exec python /app/main.py --config "$CONFIG" "$@"
    ;;
esac
