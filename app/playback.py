# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Ansehen im Browser -- auch fuer Container, die kein Browser abspielt.

Das Problem: MKV ist der robusteste Container und darum die uebliche Wahl beim
Konvertieren, aber KEIN verbreiteter Browser spielt ihn ab. Ein <video>-Element
bekommt die Datei, laedt sie stumpf herunter und zeigt nie ein Bild.

Gleiche Mechanik wie im FAH Media Downloader -- die Datei ist bewusst nahezu
identisch, damit eine Korrektur an einer Stelle auch fuer die andere gilt.

Die Loesung ist ein Umpacken, keine Neukodierung: Bild und meistens auch Ton
werden Byte fuer Byte uebernommen (`-c copy`), nur die Huelle wechselt nach
MP4 bzw. WebM. Das dauert Sekunden statt Stunden und kostet keine Qualitaet.
Nur wenn die Tonspur in der Zielhuelle nicht erlaubt ist (AC3, DTS, TrueHD),
wird sie -- und ausschliesslich sie -- nach AAC bzw. Opus umgewandelt.

Das Ergebnis landet als Zwischendatei in CONFIG_DIR/playcache. Erst dadurch
laesst sich im Player spulen: ein direkt durchgereichter ffmpeg-Strom kann
keine Range-Anfragen beantworten. Der Zwischenspeicher raeumt sich selbst auf,
aelteste Datei zuerst.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("MC_CONFIG_DIR", "/config"))
CACHE_DIR = CONFIG_DIR / "playcache"

# Huellen, die jeder verbreitete Browser oeffnet -- die gehen unveraendert raus.
DIREKT_CONTAINER = {".mp4", ".m4v", ".webm", ".ogv", ".ogg", ".mov",
                    ".mp3", ".m4a", ".opus", ".wav", ".flac", ".aac"}

# Bildspuren, die ein Browser darstellen KANN -- je Codec die passende
# Zielhuelle und der MIME-Typ, mit dem der Browser selbst sagen kann, ob er sie
# beherrscht (canPlayType). HEVC ist bewusst dabei: es ist die Standardausgabe
# des Konverters, Safari spielt es seit je, Chrome/Edge seit 107 und Firefox
# seit 137 -- jeweils mit Hardware-Decoder. Ob es im Einzelfall klappt,
# beantwortet der Browser vor Ort besser als eine Liste hier.
_VIDEO_ZIEL = {
    "h264": (".mp4", 'video/mp4; codecs="avc1.42E01E"'),
    "avc1": (".mp4", 'video/mp4; codecs="avc1.42E01E"'),
    "hevc": (".mp4", 'video/mp4; codecs="hvc1.1.6.L93.B0"'),
    "h265": (".mp4", 'video/mp4; codecs="hvc1.1.6.L93.B0"'),
    "av1": (".mp4", 'video/mp4; codecs="av01.0.05M.08"'),
    "av01": (".mp4", 'video/mp4; codecs="av01.0.05M.08"'),
    "vp9": (".webm", 'video/webm; codecs="vp9"'),
    "vp09": (".webm", 'video/webm; codecs="vp9"'),
    "vp8": (".webm", 'video/webm; codecs="vp8"'),
}
# HEVC in einer MP4-Huelle braucht den Tag "hvc1". ffmpeg schreibt sonst
# "hev1" -- und damit weigert sich Safari, die Datei ueberhaupt anzufassen.
_HEVC = ("hevc", "h265")
# Tonspuren, die in der jeweiligen Zielhuelle unveraendert bleiben duerfen.
_AUDIO_OK = {".mp4": {"aac", "mp3", "alac"}, ".m4a": {"aac", "alac"},
             ".webm": {"opus", "vorbis"}, ".opus": {"opus"}}
_AUDIO_ERSATZ = {".mp4": ("aac", "192k"), ".m4a": ("aac", "192k"),
                 ".webm": ("libopus", "160k"), ".opus": ("libopus", "160k")}
# ffmpeg leitet das Ausgabeformat sonst aus der Dateiendung ab -- und die
# Zwischendatei heisst waehrend der Arbeit ".teil". Ohne diese Angabe bricht
# der Lauf mit "use a standard extension" ab.
_FFMPEG_FORMAT = {".mp4": "mp4", ".m4a": "mp4", ".webm": "webm", ".opus": "opus"}

_laeuft = {}                 # Cache-Schluessel -> Fortschritts-Dikt
_lock = threading.Lock()
UMPACK_TIMEOUT_S = 3600      # laenger dauert kein Umpacken -- danach gilt ffmpeg als haengend
FEHLER_SICHTBAR_S = 300      # so lange bleibt eine Fehlermeldung abrufbar


def _ffmpeg_da() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def pruefe_datei(pfad: str) -> dict:
    """Spuren einer Datei ermitteln (leeres Dikt, wenn ffprobe nicht kann)."""
    if not _ffmpeg_da():
        return {}
    try:
        roh = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format",
             "-of", "json", pfad],
            capture_output=True, text=True, timeout=60)
        daten = json.loads(roh.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    video = audio = ""
    for s in daten.get("streams", []):
        art, name = s.get("codec_type"), (s.get("codec_name") or "").lower()
        if art == "video" and not video and not s.get("disposition", {}).get("attached_pic"):
            video = name
        elif art == "audio" and not audio:
            audio = name
    try:
        dauer = float(daten.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        dauer = 0.0
    return {"video": video, "audio": audio, "dauer": dauer}


def plan_fuer(pfad: str) -> dict:
    """Wie kommt diese Datei in den Browser?

    modus "direkt"    -- unveraendert ausliefern (wie bisher)
    modus "umpacken"  -- vorher in eine browsertaugliche Huelle bringen
    modus "nein"      -- ginge nur mit echter Neukodierung; ehrlich sagen
    """
    endung = os.path.splitext(pfad)[1].lower()
    if endung in DIREKT_CONTAINER:
        return {"modus": "direkt", "ziel": endung, "grund": ""}
    if not _ffmpeg_da():
        return {"modus": "nein", "ziel": "",
                "grund": "ffmpeg steht in diesem Container nicht bereit."}
    spuren = pruefe_datei(pfad)
    if not spuren:
        return {"modus": "nein", "ziel": "",
                "grund": "Die Datei liess sich nicht lesen."}
    video = spuren.get("video", "")
    if not video:                       # reine Tonspur in fremder Huelle
        ziel = ".m4a" if spuren.get("audio") in ("aac", "alac") else ".opus"
        mime = "audio/mp4" if ziel == ".m4a" else "audio/ogg; codecs=opus"
        return {"modus": "umpacken", "ziel": ziel, "grund": "", "mime": mime,
                "spuren": spuren, "nur_ton": True}
    treffer = _VIDEO_ZIEL.get(video)
    if not treffer:
        return {"modus": "nein", "ziel": "", "spuren": spuren, "mime": "",
                "grund": f"Die Bildspur ({video or 'unbekannt'}) kann kein "
                         f"Browser darstellen. Neu kodieren waere kein "
                         f"Umpacken mehr -- lade die Datei besser herunter."}
    ziel, mime = treffer
    # Der MIME-Typ geht mit an die Oberflaeche: dort fragt canPlayType() den
    # Browser, ob er den Codec beherrscht -- zuverlaessiger als jede Liste.
    return {"modus": "umpacken", "ziel": ziel, "grund": "", "spuren": spuren,
            "mime": mime}


def _schluessel(pfad: str, ziel: str) -> str:
    """Eindeutig je Datei UND Fassung: aendert sich die Quelle, faellt der
    Zwischenspeicher automatisch aus dem Rennen."""
    try:
        st = os.stat(pfad)
        stempel = f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        stempel = "0:0"
    roh = f"{os.path.realpath(pfad)}|{stempel}|{ziel}".encode()
    return hashlib.sha256(roh).hexdigest()[:32] + ziel


def cache_pfad(pfad: str, ziel: str) -> Path:
    return CACHE_DIR / _schluessel(pfad, ziel)


def _aufraeumen(max_bytes: int, schonen: Path = None):
    """Aelteste Zwischendateien werfen, bis die Obergrenze wieder passt."""
    try:
        dateien = [p for p in CACHE_DIR.glob("*") if p.is_file()
                   and not p.name.endswith(".teil")]
    except OSError:
        return
    gesamt = 0
    with_stat = []
    for p in dateien:
        try:
            st = p.stat()
        except OSError:
            continue
        gesamt += st.st_size
        with_stat.append((st.st_mtime, st.st_size, p))
    with_stat.sort()
    for _, groesse, p in with_stat:
        if gesamt <= max_bytes:
            break
        if schonen and p == schonen:
            continue
        try:
            p.unlink()
            gesamt -= groesse
        except OSError:
            pass


def status(pfad: str, ziel: str) -> dict:
    """Wo steht die Aufbereitung dieser Datei gerade?"""
    ablage = cache_pfad(pfad, ziel)
    if ablage.is_file() and ablage.stat().st_size > 0:
        return {"bereit": True, "fortschritt": 100, "fehler": ""}
    with _lock:
        lauf = _laeuft.get(ablage.name)
    if lauf:
        return {"bereit": False, "fortschritt": lauf.get("fortschritt", 0),
                "fehler": lauf.get("fehler", "")}
    return {"bereit": False, "fortschritt": 0, "fehler": ""}


def vorbereiten(pfad: str, max_cache_bytes: int) -> dict:
    """Das Umpacken anstossen (oder melden, dass es schon laeuft/fertig ist)."""
    plan = plan_fuer(pfad)
    if plan["modus"] != "umpacken":
        return plan
    ablage = cache_pfad(pfad, plan["ziel"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if ablage.is_file() and ablage.stat().st_size > 0:
        os.utime(ablage, None)                  # als zuletzt benutzt merken
        return {**plan, "bereit": True, "fortschritt": 100}
    with _lock:
        if ablage.name in _laeuft:
            return {**plan, "bereit": False,
                    "fortschritt": _laeuft[ablage.name].get("fortschritt", 0)}
        _laeuft[ablage.name] = {"fortschritt": 0, "fehler": ""}
    t = threading.Thread(target=_umpacken, name="remux",
                         args=(pfad, plan, ablage, max_cache_bytes), daemon=True)
    t.start()
    return {**plan, "bereit": False, "fortschritt": 0}


def ffmpeg_befehl(pfad: str, plan: dict, ziel_datei) -> list:
    """Den ffmpeg-Aufruf zusammenstellen -- absichtlich als eigene Funktion,
    damit er ohne laufendes ffmpeg geprueft werden kann."""
    spuren = plan.get("spuren") or {}
    ziel = plan["ziel"]
    audio = (spuren.get("audio") or "").lower()
    video = (spuren.get("video") or "").lower()

    befehl = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", pfad]
    if plan.get("nur_ton"):
        befehl += ["-vn", "-map", "0:a:0"]
    else:
        # Nur die erste Bild- und Tonspur: mehr braucht ein Browser-Player
        # nicht, und jede weitere Spur waere unnoetige Schreibarbeit.
        befehl += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy"]
    if audio and audio in _AUDIO_OK.get(ziel, set()):
        befehl += ["-c:a", "copy"]
    else:
        codec, rate = _AUDIO_ERSATZ[ziel]
        befehl += ["-c:a", codec, "-b:a", rate]
    if ziel in (".mp4", ".m4a"):
        # Der Index gehoert an den Anfang, sonst kann der Player erst spulen,
        # wenn die ganze Datei geladen ist.
        befehl += ["-movflags", "+faststart"]
    if ziel == ".mp4" and video in _HEVC:
        # Ohne diesen Tag schreibt ffmpeg "hev1" -- und Safari weigert sich,
        # die Datei ueberhaupt anzufassen.
        befehl += ["-tag:v", "hvc1"]
    befehl += ["-sn", "-dn", "-f", _FFMPEG_FORMAT.get(ziel, "mp4"),
               "-progress", "pipe:1", "-nostats", str(ziel_datei)]
    return befehl


def _umpacken(pfad: str, plan: dict, ablage: Path, max_cache_bytes: int):
    """Der eigentliche ffmpeg-Lauf. Laeuft in einem eigenen Thread.

    stderr geht in eine anonyme Temporaerdatei, nicht in eine Pipe: eine Pipe
    fasst 64 KB, und wer sie erst NACH dem Ende liest, wartet bei einer
    geschwaetzigen Quelle ewig auf ein ffmpeg, das seinerseits auf den Leser
    wartet. Ein Zeitwaechter toetet ffmpeg nach UMPACK_TIMEOUT_S -- egal, wo
    dieser Thread gerade blockiert (ein proc.wait(timeout=...) haette nichts
    genuetzt, solange die Schleife ueber stdout noch auf Zeilen wartet).
    """
    spuren = plan.get("spuren") or {}
    teil = ablage.with_suffix(ablage.suffix + ".teil")
    befehl = ffmpeg_befehl(pfad, plan, teil)

    dauer = float(spuren.get("dauer") or 0)
    proc, waechter = None, None
    zu_langsam = threading.Event()
    try:
        with tempfile.TemporaryFile() as err_fh:
            proc = subprocess.Popen(befehl, stdout=subprocess.PIPE,
                                    stderr=err_fh, text=True)

            def _abbrechen():
                zu_langsam.set()
                try:
                    proc.kill()
                except OSError:
                    pass
            waechter = threading.Timer(UMPACK_TIMEOUT_S, _abbrechen)
            waechter.daemon = True
            waechter.start()
            for zeile in proc.stdout:
                if zeile.startswith("out_time_ms=") and dauer > 0:
                    try:
                        sek = int(zeile.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    with _lock:
                        if ablage.name in _laeuft:
                            _laeuft[ablage.name]["fortschritt"] = \
                                max(1, min(99, int(sek / dauer * 100)))
            proc.wait()
            waechter.cancel()
            err_fh.seek(0)
            fehlertext = err_fh.read(4096).decode("utf-8", "replace").strip()
        if len(fehlertext) > 400:          # der Anfang benennt die Ursache
            fehlertext = fehlertext[:400] + " …"
        if zu_langsam.is_set():
            raise RuntimeError(f"ffmpeg nach {UMPACK_TIMEOUT_S} s abgebrochen")
        if proc.returncode == 0 and teil.is_file() and teil.stat().st_size > 0:
            os.replace(teil, ablage)
            _aufraeumen(max_cache_bytes, schonen=ablage)
            with _lock:
                _laeuft.pop(ablage.name, None)
            return
        raise RuntimeError(fehlertext or f"ffmpeg endete mit {proc.returncode}")
    except Exception as e:                       # noqa: BLE001 - alles melden
        with _lock:
            if ablage.name in _laeuft:
                _laeuft[ablage.name]["fehler"] = str(e)[:400]
        # Die Fehlermeldung soll der Nutzer sehen; damit sie nicht ewig haengen
        # bleibt, verfaellt sie nach ein paar Minuten von selbst. Daemon, sonst
        # haelt der Timer beim Beenden den Prozess fuenf Minuten auf.
        aufraeumer = threading.Timer(FEHLER_SICHTBAR_S,
                                     lambda: _laeuft.pop(ablage.name, None))
        aufraeumer.daemon = True
        aufraeumer.start()
    finally:
        if waechter is not None:
            waechter.cancel()
        if proc is not None:
            if proc.poll() is None:              # Ausnahme mitten im Lauf
                try:
                    proc.kill()
                    proc.wait(timeout=10)
                except (OSError, subprocess.SubprocessError):
                    pass
            if proc.stdout:
                proc.stdout.close()
        try:
            if teil.exists():
                teil.unlink()
        except OSError:
            pass


def cache_groesse() -> int:
    try:
        return sum(p.stat().st_size for p in CACHE_DIR.glob("*") if p.is_file())
    except OSError:
        return 0


def _teil_verwaist(p: Path, aktiv) -> bool:
    """Eine .teil-Datei, zu der kein laufendes Umpacken mehr gehoert (Absturz,
    Neustart mitten im Lauf) -- sie wuerde sonst ewig Platz belegen."""
    return p.name.endswith(".teil") and p.name[:-len(".teil")] not in aktiv


def verwaiste_teile_aufraeumen() -> int:
    """Beim Start: verwaiste .teil-Dateien loeschen. Rueckgabe: Anzahl."""
    with _lock:
        aktiv = set(_laeuft)
    weg = 0
    for p in list(CACHE_DIR.glob("*.teil")) if CACHE_DIR.exists() else []:
        if p.is_file() and _teil_verwaist(p, aktiv):
            try:
                p.unlink()
                weg += 1
            except OSError:
                pass
    return weg


def cache_leeren() -> int:
    """Alles wegwerfen, was nicht gerade geschrieben wird -- verwaiste .teil-
    Dateien eingeschlossen, die laufende bleibt."""
    weg = 0
    with _lock:
        aktiv = set(_laeuft)
    for p in list(CACHE_DIR.glob("*")) if CACHE_DIR.exists() else []:
        if not p.is_file() or p.name in aktiv:
            continue
        if p.name.endswith(".teil") and not _teil_verwaist(p, aktiv):
            continue
        try:
            weg += p.stat().st_size
            p.unlink()
        except OSError:
            pass
    return weg
