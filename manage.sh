#!/bin/sh
# ============================================================================
# Media Converter – manage.sh – (c) 2026 Fabian Hammer
# ============================================================================
# Deploy-Werkzeug fuer das NAS. Kein 'unzip' noetig, python3 reicht.
#
#   ./manage.sh update            – Update aus fah-converter.zip (hoechste (N) gewinnt)
#   ./manage.sh update <datei>    – Update aus einer bestimmten ZIP
#   ./manage.sh update --no-cache – vollstaendiger Neubau des Images
#   ./manage.sh watch             – auf neue ZIP warten und automatisch deployen
#   ./manage.sh start|stop|restart|status|logs|url
#
# Prinzip:
#   config/                bleibt IMMER unberuehrt (Zugangsdaten, Verlauf, Statistik)
#   docker-compose.yaml    bleibt unberuehrt -- nur die .example kommt aus der ZIP
#   Container              wird bei jedem Update mit --force-recreate ersetzt
#
# Die Release-Datei heisst fah-converter.zip. Browser haengen beim erneuten
# Herunterladen " (1)", " (2)" an -- die HOECHSTE Nummer ist der juengste
# Download und gewinnt, unabhaengig von der Aenderungszeit. Die Auswahl
# steckt in tools/release_zip.py (gemeinsam mit dem FAH Media Downloader).
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AUTO_ZIP_BASENAME="fah-converter.zip"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yaml"
COMPOSE_EXAMPLE="$SCRIPT_DIR/docker-compose.yaml.example"
CONTAINER="media-converter"
PORT_IM_CONTAINER=8686
WATCH_INTERVAL=20
NO_CACHE="${NO_CACHE:-0}"

export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        DC="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        DC="docker-compose"
    else
        echo "Fehler: weder 'docker compose' noch 'docker-compose' gefunden."
        exit 1
    fi
}

# Der Containername steht in der aktiven compose-Datei -- die kann der Nutzer
# angepasst haben (auf dem NAS heisst er "media-converter", die .example sagt
# "fah-mediaconverter").
container_name() {
    N="$(grep -oE 'container_name:[[:space:]]*[A-Za-z0-9_.-]+' "$COMPOSE_FILE" 2>/dev/null \
        | head -1 | awk '{print $2}')"
    [ -n "$N" ] && echo "$N" || echo "$CONTAINER"
}

# ── ZIP-Auswahl: hoechste (N) gewinnt, dann juengste Zeit ────────────────────
# Beim ALLERERSTEN Aufruf liegt tools/ noch nicht neben manage.sh -- dann wird
# das Modul direkt aus der ZIP gelesen, damit die Auswahl von Anfang an gilt.
_release_zip_py() {
    if [ -f "$SCRIPT_DIR/tools/release_zip.py" ]; then
        echo "$SCRIPT_DIR/tools/release_zip.py"; return 0
    fi
    for z in "$SCRIPT_DIR"/*.zip; do
        [ -f "$z" ] || continue
        python3 - "$z" "$SCRIPT_DIR/.release_zip.py" <<'PYEOF' && { echo "$SCRIPT_DIR/.release_zip.py"; return 0; }
import sys, zipfile
z, out = sys.argv[1], sys.argv[2]
try:
    with zipfile.ZipFile(z) as f:
        for n in f.namelist():
            if n.endswith("tools/release_zip.py"):
                open(out, "wb").write(f.read(n)); sys.exit(0)
except Exception:
    pass
sys.exit(1)
PYEOF
    done
    return 1
}

find_latest_zip() {
    RZ="$(_release_zip_py)" || return 0
    python3 "$RZ" latest "$SCRIPT_DIR" "$AUTO_ZIP_BASENAME" 2>/dev/null
}

# ── manage.sh selbst aus der ZIP holen (Self-Update) ─────────────────────────
_extract_manage_from_zip() {
    python3 - "$1" "$2" <<'PYEOF'
import sys, zipfile
zip_path, out = sys.argv[1], sys.argv[2]
try:
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            if n.rsplit("/", 1)[-1] == "manage.sh":
                open(out, "wb").write(z.read(n)); break
except Exception:
    pass
PYEOF
}

# ── Laeuft gerade eine Konvertierung? ────────────────────────────────────────
# Der Neustart wuerde sie abbrechen -- lieber warten als Arbeit vernichten.
# `ps` gibt es im Image nicht, deshalb der Blick nach /proc.
konvertierung_laeuft() {
    docker exec "$(container_name)" sh -c \
        'for p in /proc/[0-9]*/cmdline; do tr "\0" " " < $p; echo; done' 2>/dev/null \
        | grep -qi ffmpeg
}

# ── Starten -- bei Updates mit erzwungenem Neuaufbau ─────────────────────────
# "up -d" allein laesst einen laufenden Container stehen, wenn compose keine
# Aenderung an Image oder Konfiguration erkennt -- neue Features aus der ZIP
# kaemen dann erst beim naechsten Zufall an. --force-recreate ersetzt ihn
# in jedem Fall.
compose_up() {
    cd "$SCRIPT_DIR" || return 1
    UP_ARGS="-d"
    [ "${FORCE_RECREATE:-0}" = "1" ] && UP_ARGS="-d --force-recreate"
    # shellcheck disable=SC2086
    $DC up $UP_ARGS
}

# ── Warten, bis die Oberflaeche antwortet ────────────────────────────────────
warte_auf_start() {
    i=0
    # Anzahl der 2-s-Versuche; die Tests setzen sie auf 1, sonst warten sie
    # 60 s auf ein docker, das es dort gar nicht gibt.
    while [ $i -lt "${MC_START_VERSUCHE:-30}" ]; do
        if docker exec "$(container_name)" python3 -c \
            "import urllib.request;urllib.request.urlopen('http://127.0.0.1:$PORT_IM_CONTAINER/api/auth-check',timeout=3)" \
            >/dev/null 2>&1; then
            return 0
        fi
        i=$((i + 1))
        sleep 2
    done
    return 1
}

print_url() {
    P="$(docker port "$(container_name)" "$PORT_IM_CONTAINER" 2>/dev/null | head -1 | sed 's/.*://')"
    [ -n "$P" ] || P="$PORT_IM_CONTAINER"
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "$IP" ] || IP="localhost"
    echo "  WebGUI:  http://$IP:$P    (lokal: http://localhost:$P)"
}

# ── Das Update ───────────────────────────────────────────────────────────────
do_update() {
    ZIP="$1"
    [ -n "$ZIP" ] || ZIP="$(find_latest_zip)"
    if [ -z "$ZIP" ] || [ ! -f "$ZIP" ]; then
        echo "Fehler: keine $AUTO_ZIP_BASENAME (oder ' (N)'-Variante) gefunden."
        echo "Lege die ZIP in: $SCRIPT_DIR"
        exit 1
    fi
    echo "=== Update aus: $ZIP ==="
    cd "$SCRIPT_DIR"
    detect_compose

    # Schritt 1: Self-Update von manage.sh AUS DER ZIP, dann neu starten.
    if [ -z "${MC_SELFUPDATED:-}" ]; then
        _extract_manage_from_zip "$ZIP" "$SCRIPT_DIR/manage.sh.new"
        if [ -f "$SCRIPT_DIR/manage.sh.new" ]; then
            if ! cmp -s "$SCRIPT_DIR/manage.sh.new" "$SCRIPT_DIR/manage.sh"; then
                chmod +x "$SCRIPT_DIR/manage.sh.new" 2>/dev/null || true
                mv "$SCRIPT_DIR/manage.sh.new" "$SCRIPT_DIR/manage.sh"
                echo "  [SELF-UPDATE] manage.sh aktualisiert -- starte neu ..."
                export MC_SELFUPDATED=1
                [ "$NO_CACHE" = "1" ] && exec "$SCRIPT_DIR/manage.sh" update --no-cache "$ZIP"
                exec "$SCRIPT_DIR/manage.sh" update "$ZIP"
            else
                rm -f "$SCRIPT_DIR/manage.sh.new"
                echo "  [SELF-UPDATE] manage.sh bereits aktuell."
            fi
        fi
    fi

    # Schritt 2: nicht mitten in eine Konvertierung hineinfahren.
    if konvertierung_laeuft; then
        echo "ABBRUCH: Es laeuft gerade eine Konvertierung."
        echo "Warte, bis sie fertig ist, oder brich sie in der Oberflaeche ab."
        exit 3
    fi

    # Schritt 3: Sicherung. config gehoert teils root (secret.key 0600),
    # deshalb aus dem Container heraus.
    STAMP="$(date +%Y%m%d-%H%M%S)"
    BACKUP="$SCRIPT_DIR/_backup_$STAMP"
    mkdir -p "$BACKUP"
    for f in app README.md Dockerfile docker-compose.yaml docker-compose.yaml.example tests tools; do
        [ -e "$SCRIPT_DIR/$f" ] && cp -R "$SCRIPT_DIR/$f" "$BACKUP/" 2>/dev/null || true
    done
    docker exec "$(container_name)" sh -c 'cd /config && tar czf - .' > "$BACKUP/config.tar.gz" 2>/dev/null || \
        echo "  (Hinweis: config konnte nicht gesichert werden -- Container gestoppt?)"
    echo "  [BACKUP] $BACKUP"

    # Schritt 4: entpacken, Nutzerdaten schuetzen.
    python3 - "$ZIP" "$SCRIPT_DIR" <<'PYEOF'
import os, sys, zipfile
zip_pfad, ziel = sys.argv[1], sys.argv[2]
TABU = ("config/",)
TABU_DATEIEN = ("docker-compose.yaml", "manage.sh")   # manage.sh: Self-Update oben
geschrieben, uebersprungen = 0, []
with zipfile.ZipFile(zip_pfad) as z:
    for eintrag in z.infolist():
        name = eintrag.filename.lstrip("/")
        if not name or name.endswith("/"):
            continue
        if name.startswith(TABU) or name in TABU_DATEIEN:
            uebersprungen.append(name)
            continue
        pfad = os.path.realpath(os.path.join(ziel, name))
        if not pfad.startswith(os.path.realpath(ziel) + os.sep):
            uebersprungen.append(name + " (Pfad ausserhalb!)")
            continue
        os.makedirs(os.path.dirname(pfad), exist_ok=True)
        with z.open(eintrag) as q, open(pfad, "wb") as f:
            f.write(q.read())
        modus = eintrag.external_attr >> 16
        if modus & 0o111:
            os.chmod(pfad, (modus & 0o777) or 0o755)
        geschrieben += 1
print(f"  [CODE] {geschrieben} Datei(en) aktualisiert, {len(uebersprungen)} geschuetzt.")
PYEOF
    [ -f "$COMPOSE_FILE" ] || { cp "$COMPOSE_EXAMPLE" "$COMPOSE_FILE"; echo "  [COMPOSE] docker-compose.yaml aus .example erzeugt."; }
    mkdir -p "$SCRIPT_DIR/config"

    # Schritt 5: Build + erzwungener Neuaufbau.
    echo "=== Baue & starte Container (Neuaufbau erzwungen) ==="
    BUILD_ARGS=""
    [ "$NO_CACHE" = "1" ] && BUILD_ARGS="--no-cache"
    # shellcheck disable=SC2086
    $DC build $BUILD_ARGS
    FORCE_RECREATE=1 compose_up

    # Schritt 6: aeltere Fassungen der Release-ZIP wegraeumen. Sonst gewinnt
    # beim naechsten Aufruf ohne Argument dieselbe "(N)"-Datei wieder, obwohl
    # laengst eine neuere ohne Klammer daneben liegt.
    RZ="$(_release_zip_py)" && python3 "$RZ" cleanup "$SCRIPT_DIR" "$AUTO_ZIP_BASENAME" "$ZIP" 2>/dev/null
    rm -f "$SCRIPT_DIR/.release_zip.py"

    if warte_auf_start; then
        echo ""
        print_url
        echo "=== Update abgeschlossen ==="
        echo "  Rueckweg: cp -R $BACKUP/app $SCRIPT_DIR/ && $DC build && $DC up -d --force-recreate"
    else
        echo "WARNUNG: Die Oberflaeche antwortet nach 60 s noch nicht."
        echo "  Logs:     docker logs --tail 50 $(container_name)"
        echo "  Rueckweg: cp -R $BACKUP/app $SCRIPT_DIR/ && $DC build && $DC up -d --force-recreate"
        exit 1
    fi
}

do_watch() {
    echo "=== Watch-Modus: warte auf neue $AUTO_ZIP_BASENAME (Intervall ${WATCH_INTERVAL}s) ==="
    LAST=""
    while true; do
        Z="$(find_latest_zip)"
        if [ -n "$Z" ] && [ -f "$Z" ]; then
            SIG="$(python3 -c "import os,sys;print(sys.argv[1],os.path.getmtime(sys.argv[1]),os.path.getsize(sys.argv[1]))" "$Z" 2>/dev/null)"
            if [ "$SIG" != "$LAST" ]; then
                echo "[watch] neue ZIP erkannt: $Z"
                LAST="$SIG"
                "$SCRIPT_DIR/manage.sh" update "$Z" || echo "[watch] Update fehlgeschlagen."
            fi
        fi
        sleep "$WATCH_INTERVAL"
    done
}

CMD="${1:-}"
case "$CMD" in
    update)
        shift
        ZIP=""
        for a in "$@"; do
            case "$a" in
                --no-cache) NO_CACHE=1 ;;
                *) ZIP="$a" ;;
            esac
        done
        do_update "$ZIP"
        ;;
    watch)   do_watch ;;
    start)   detect_compose; compose_up; print_url ;;
    stop)    detect_compose; cd "$SCRIPT_DIR"; $DC down ;;
    restart) detect_compose; cd "$SCRIPT_DIR"; $DC down; compose_up; print_url ;;
    status)  detect_compose; cd "$SCRIPT_DIR"; $DC ps ;;
    logs)    detect_compose; cd "$SCRIPT_DIR"; shift; $DC logs -f "$@" ;;
    url)     print_url ;;
    *)
        echo "Media Converter – manage.sh"
        echo "Kommandos: update [<zip>] [--no-cache] | watch | start | stop |"
        echo "           restart | status | logs | url"
        ;;
esac
