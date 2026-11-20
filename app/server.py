#!/usr/bin/env python3
"""
Media Converter Server
- Pure Flask + threading (no eventlet, no socketio, no monkey-patching)
- Client gets live data via /api/events (SSE) or polls /api/status
- Job queue: one job at a time, 2 worker threads per job via ThreadPoolExecutor
- GPU VAAPI with 3-stage fallback to CPU
- Progress via ffmpeg -progress file polling
- Track verification after encode, ownership/mtime transfer, JSONL history
"""

import os
import re
import sys
import json
import math
import time
import array
import uuid
import stat
import shutil
import hmac
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta

import bcrypt
from flask import (Flask, request, jsonify, send_file,
                   send_from_directory, session, Response)

import playback

# ─── Config ──────────────────────────────────────────────────────────────────
MEDIA_ROOT = os.environ.get("MC_MEDIA_ROOT", "/media")
# MC_ROOTS: kommagetrennte Ordnernamen direkt unter MEDIA_ROOT, die der Converter sehen
# und benutzen darf (z. B. "volume1"). Leer = alles. Wer "/" nach /media einhängt, sieht
# sonst jedes Volume des NAS in der Baumansicht (Betreiber 17.09.2026).
MC_ROOTS = tuple(r.strip().strip("/") for r in os.environ.get("MC_ROOTS", "").split(",") if r.strip().strip("/"))
CONFIG_DIR = os.environ.get("MC_CONFIG_DIR", "/config")
PROGRESS_DIR = os.environ.get("MC_PROGRESS_DIR", "/tmp/mc_progress")
PORT = 8686
WORKERS = 2
PRESCAN_KEEP_S = 600          # Prescan-Ergebnisse 10 min aufbewahren
MAX_ORDNER = 50               # Obergrenze für einen Sammelauftrag
SSE_MAX = 6                   # gleichzeitige /api/events-Verbindungen (waitress hat 16 Threads)
# Nur setzen, wenn ein vertrauenswürdiger Reverse-Proxy davor steht: dann zählt die
# RECHTESTE X-Forwarded-For-IP (die der Proxy angehängt hat), sonst immer remote_addr.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(PROGRESS_DIR, exist_ok=True)

# ─── Auth ────────────────────────────────────────────────────────────────────
AUTH_HASH_FILE = os.path.join(CONFIG_DIR, "auth.hash")
AUTH_USER_FILE = os.path.join(CONFIG_DIR, "auth.user")
SECRET_KEY_FILE = os.path.join(CONFIG_DIR, "secret.key")

def _nur_eigentuemer(path):
    """Rechte auf 0600 zwingen – auch bei einer bestehenden Datei (wie S17 bei secret.key)."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            print(f"[WARN] {path} war für Gruppe/Welt lesbar ({oct(mode)}) – setze 0600", flush=True)
            os.chmod(path, 0o600)
    except OSError as e:
        print(f"[WARN] chmod {path}: {e}", flush=True)

def schreibe_geheim(path, text):
    """Datei mit 0600 anlegen bzw. überschreiben – für den Passwort-Hash, genau wie
    secret.key. Sonst könnte jeder, der config/ lesen darf, den Hash offline angreifen."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    _nur_eigentuemer(path)

def init_auth():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # Username
    if os.path.exists(AUTH_USER_FILE):
        with open(AUTH_USER_FILE, "r") as f:
            user = f.read().strip()
    else:
        user = "admin"
        with open(AUTH_USER_FILE, "w") as f:
            f.write(user)
    # Password hash
    if os.path.exists(AUTH_HASH_FILE):
        with open(AUTH_HASH_FILE, "r") as f:
            pw_hash = f.read().strip().encode()
        _nur_eigentuemer(AUTH_HASH_FILE)
    else:
        pw = "admin"
        pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12))
        schreibe_geheim(AUTH_HASH_FILE, pw_hash.decode())
    return user, pw_hash

def load_secret_key(path=SECRET_KEY_FILE):
    """Persistent 32-byte secret key (file mode 0600); created on first start."""
    try:
        with open(path, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            # Rechte auch bei bestehender Datei erzwingen (S17)
            try:
                mode = stat.S_IMODE(os.stat(path).st_mode)
                if mode & 0o077:
                    print(f"[WARN] {path} war für Gruppe/Welt lesbar ({oct(mode)}) – setze 0600", flush=True)
                    os.chmod(path, 0o600)
            except OSError as e:
                print(f"[WARN] chmod {path}: {e}", flush=True)
            return key
    except OSError:
        pass
    key = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key

AUTH_USER, PW_HASH = init_auth()
auth_lock = threading.Lock()

class LoginGuard:
    """Rate limiter for login attempts. Pure, testable, injectable clock.

    Per key: after `max_fail` failures within `window` seconds → lock `base_lock` s,
    every further lock of the same key doubles (capped at `max_lock`). Success resets.
    Global: more than `global_max` failures within `window` across all keys → `base_lock` s for everyone.
    """
    def __init__(self, now=time.time, max_fail=5, window=600, base_lock=60, max_lock=900, global_max=30):
        self.now = now
        self.max_fail = max_fail
        self.window = window
        self.base_lock = base_lock
        self.max_lock = max_lock
        self.global_max = global_max
        self._fails = {}        # key -> [timestamps]
        self._locks = {}        # key -> (until, length)
        self._global_fails = []
        self._global_until = 0.0
        self._lock = threading.Lock()

    def _prune(self, lst, now):
        cutoff = now - self.window
        return [t for t in lst if t > cutoff]

    def retry_after(self, key):
        """Seconds the key must still wait; 0 if allowed."""
        now = self.now()
        with self._lock:
            wait = 0
            if self._global_until > now:
                wait = max(wait, self._global_until - now)
            lk = self._locks.get(key)
            if lk and lk[0] > now:
                wait = max(wait, lk[0] - now)
            return int(-(-wait // 1)) if wait > 0 else 0   # ceil

    def fail(self, key):
        now = self.now()
        with self._lock:
            fails = self._prune(self._fails.get(key, []), now)
            fails.append(now)
            self._fails[key] = fails
            self._global_fails = self._prune(self._global_fails, now)
            self._global_fails.append(now)
            if len(fails) >= self.max_fail:
                prev = self._locks.get(key)
                length = self.base_lock
                if prev:
                    length = min(prev[1] * 2, self.max_lock)
                self._locks[key] = (now + length, length)
                self._fails[key] = []
            if len(self._global_fails) > self.global_max:
                self._global_until = now + self.base_lock
                self._global_fails = []

    def success(self, key):
        with self._lock:
            self._fails.pop(key, None)
            self._locks.pop(key, None)

login_guard = LoginGuard()

def client_key():
    """Rate-Limit-Schlüssel. X-Forwarded-For ist frei fälschbar – nur mit TRUST_PROXY=1
    wird die rechteste (vom eigenen Proxy angehängte) IP benutzt."""
    if TRUST_PROXY:
        parts = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.remote_addr or "?"

# ─── Stats (cumulative, persisted across runs) ──────────────────────────────
STATS_FILE = os.path.join(CONFIG_DIR, "stats.json")
stats_lock = threading.Lock()

def load_stats(path=None):
    try:
        with open(path or STATS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"runs": 0, "files_converted": 0, "files_skipped": 0,
                "bytes_before": 0, "bytes_after": 0, "last_run": None}

def save_stats(s, path=None):
    """Atomar: erst .tmp schreiben, dann umbenennen. Ein Absturz mitten im Schreiben
    hinterließ sonst eine halbe stats.json – und damit eine Bilanz von null."""
    tgt = path or STATS_FILE
    tmp = tgt + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, tgt)

def stats_lesbar(path=None):
    """True, wenn stats.json existiert und ein JSON-Objekt enthält."""
    try:
        with open(path or STATS_FILE, "r") as f:
            return isinstance(json.load(f), dict)
    except (OSError, ValueError):
        return False

def update_cumulative_stats(t):
    """Fold a finished task's results into the persistent cumulative stats file.
    Reine Skip-Läufe (nichts konvertiert) zählen NICHT – genau wie in record_run.
    Vorher zählte stats.json sie als Lauf, runs.jsonl nicht, und Startseite und
    Verlauf nannten verschiedene Laufzahlen."""
    converted = t["completed"] - t["skipped"]
    if converted <= 0:
        return
    with stats_lock:
        s = load_stats()
        s["runs"] += 1
        s["files_converted"] += max(0, converted)
        s["files_skipped"] += t["skipped"]
        # TV-Reparatur macht Dateien größer (zusätzliche Tonspur) – das zählt
        # ehrlich mit, genau wie in runs.jsonl, sonst zeigten Startseite und
        # Verlauf verschiedene Bilanzen.
        s["bytes_before"] += t.get("bytes_before_done", 0)
        s["bytes_after"] += t.get("bytes_after_done", 0)
        s["last_run"] = time.time()
        save_stats(s)

# ─── History (JSONL, one line per converted file) ───────────────────────────
HISTORY_FILE = os.path.join(CONFIG_DIR, "history.jsonl")
HISTORY_LEGACY_FILE = os.path.join(CONFIG_DIR, "history.json")
history_lock = threading.Lock()

def legacy_run_id(ts):
    try:
        return "legacy-" + datetime.fromtimestamp(float(ts)).strftime("%Y%m%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return "legacy-00000000"

def migrate_history(legacy_path=HISTORY_LEGACY_FILE, jsonl_path=HISTORY_FILE):
    """history.json (list) → history.jsonl, once. Old entries get run_id 'legacy-<YYYYMMDD>'.
    Returns number of migrated entries (0 if nothing to do)."""
    if os.path.exists(jsonl_path) or not os.path.exists(legacy_path):
        return 0
    try:
        with open(legacy_path, "r") as f:
            old = json.load(f)
    except Exception as e:
        print(f"[HISTORY] Migration fehlgeschlagen: {e}", flush=True)
        return 0
    if not isinstance(old, list):
        old = []
    n = 0
    with open(jsonl_path, "w") as out:
        for e in old:
            if not isinstance(e, dict):
                continue
            entry = {
                "run_id": e.get("run_id") or legacy_run_id(e.get("timestamp", 0)),
                "name": e.get("name", ""),
                "path": e.get("path", ""),
                "src": e.get("src", ""),
                "rel_dir": e.get("rel_dir", ""),
                "size_before": e.get("size_before", 0) or 0,
                "size_after": e.get("size_after", 0) or 0,
                "method": e.get("method", ""),
                "target": e.get("target", ""),
                "quality": e.get("quality", ""),
                "timestamp": e.get("timestamp", 0) or 0,
                "duration_s": e.get("duration_s", 0) or 0,
                "speed": e.get("speed", 0) or 0,
            }
            out.write(json.dumps(entry) + "\n")
            n += 1
    try:
        os.rename(legacy_path, legacy_path + ".migrated")
    except OSError as e:
        print(f"[HISTORY] Altdatei konnte nicht umbenannt werden: {e}", flush=True)
    print(f"[HISTORY] {n} Einträge nach JSONL migriert", flush=True)
    return n

def load_history(path=None):
    entries = []
    try:
        with open(path or HISTORY_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return entries

def append_history(entry):
    """Record one actually-converted file. Skipped files are intentionally excluded."""
    with history_lock:
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")

def clear_history():
    """Leert nur die Dateiliste. Der letzte nicht leere Stand bleibt als
    history.jsonl.geleert liegen: der Verlauf wurde zweimal versehentlich geleert,
    und die jüngsten Läufe steckten in keiner Update-Sicherung."""
    with history_lock:
        try:
            with open(HISTORY_FILE, "rb") as f:
                alt = f.read()
            if alt.strip():
                with open(HISTORY_FILE + ".geleert", "wb") as f:
                    f.write(alt)
        except OSError:
            pass
        with open(HISTORY_FILE, "w"):
            pass

def history_runs(entries):
    """Group entries by run_id → list sorted newest first."""
    runs = {}
    for e in entries:
        rid = e.get("run_id") or legacy_run_id(e.get("timestamp", 0))
        r = runs.get(rid)
        ts = e.get("timestamp", 0) or 0
        if r is None:
            r = {"run_id": rid, "timestamp": ts, "target": e.get("target", ""),
                 "count": 0, "bytes_before": 0, "bytes_after": 0}
            runs[rid] = r
        r["timestamp"] = min(r["timestamp"], ts) if r["count"] else ts
        if not r["target"] and e.get("target"):
            r["target"] = e["target"]
        r["count"] += 1
        r["bytes_before"] += e.get("size_before", 0) or 0
        r["bytes_after"] += e.get("size_after", 0) or 0
    return sorted(runs.values(), key=lambda r: r["timestamp"], reverse=True)

# ─── Runs (JSONL, one line per finished run – survives clearing the history) ─
# Kacheln, Rückgewinn-Leiste und Diagramm speisen sich hieraus statt aus den
# Einzeleinträgen. „Verlauf löschen" räumt nur history.jsonl, die Bilanz bleibt.
RUNS_FILE = os.path.join(CONFIG_DIR, "runs.jsonl")
GAP_RUN_ID = "vor-verlauf"
runs_lock = threading.Lock()

def load_runs(path=None):
    """All recorded run summaries, newest first."""
    runs = []
    try:
        with open(path or RUNS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("run_id"):
                    runs.append(r)
    except OSError:
        pass
    return sorted(runs, key=lambda r: r.get("timestamp", 0) or 0, reverse=True)

def append_run(summary, path=None):
    with runs_lock:
        with open(path or RUNS_FILE, "a") as f:
            f.write(json.dumps(summary) + "\n")

def save_runs(runs, path=None):
    """Rewrite the whole file, oldest first (append order)."""
    tgt = path or RUNS_FILE
    tmp = tgt + ".tmp"
    with open(tmp, "w") as f:
        for r in sorted(runs, key=lambda r: r.get("timestamp", 0) or 0):
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, tgt)

def migrate_runs(history_path=None, runs_path=None, stats_path=None):
    """Build runs.jsonl once: from the existing history, plus one collective entry
    for whatever the cumulative stats know beyond it (a cleared history keeps its
    balance that way). Returns the number of written runs (0 if nothing to do)."""
    runs_path = runs_path or RUNS_FILE
    if os.path.exists(runs_path):
        return 0
    runs = history_runs(load_history(history_path))
    s = load_stats(stats_path)
    d_count = (s.get("files_converted", 0) or 0) - sum(r["count"] for r in runs)
    d_before = (s.get("bytes_before", 0) or 0) - sum(r["bytes_before"] for r in runs)
    d_after = (s.get("bytes_after", 0) or 0) - sum(r["bytes_after"] for r in runs)
    d_runs = (s.get("runs", 0) or 0) - len(runs)
    if d_count > 0 or d_before > 0:
        oldest = min((r["timestamp"] for r in runs), default=None)
        ts = oldest - 1 if oldest is not None else (s.get("last_run") or 0)
        runs.append({"run_id": GAP_RUN_ID, "timestamp": ts, "target": "",
                     "count": max(0, d_count), "bytes_before": max(0, d_before),
                     "bytes_after": max(0, d_after), "runs": max(1, d_runs)})
    if not runs:
        return 0
    save_runs(runs, runs_path)
    print(f"[RUNS] {len(runs)} Laufzusammenfassungen angelegt", flush=True)
    return len(runs)

def stats_aus_runs(runs):
    """Pure: Bilanz aus den Laufzusammenfassungen nachrechnen. files_skipped kennt
    runs.jsonl nicht – bleibt 0."""
    return {"runs": sum(int(r.get("runs") or 1) for r in runs),
            "files_converted": sum(int(r.get("count") or 0) for r in runs),
            "files_skipped": 0,
            "bytes_before": sum(int(r.get("bytes_before") or 0) for r in runs),
            "bytes_after": sum(int(r.get("bytes_after") or 0) for r in runs),
            "last_run": max((r.get("timestamp") or 0) for r in runs) or None}

def stats_reparieren(stats_path=None, runs_path=None):
    """Beim Start: fehlt stats.json oder ist sie leer/kaputt, aus runs.jsonl
    nachrechnen – die Bilanz steckt dort je Lauf. True, wenn geschrieben wurde."""
    stats_path = stats_path or STATS_FILE
    if stats_lesbar(stats_path):
        return False
    runs = load_runs(runs_path)
    if not runs:
        return False
    with stats_lock:
        save_stats(stats_aus_runs(runs), stats_path)
    print(f"[STATS] stats.json aus {len(runs)} Laufzusammenfassungen nachgerechnet", flush=True)
    return True

def record_run(t):
    """Fold a finished task into the persistent run log. Mirrors update_cumulative_stats
    (reine Skip-Läufe zählen in beiden nicht)."""
    converted = t["completed"] - t["skipped"]
    if converted <= 0:
        return
    append_run({"run_id": t["id"], "timestamp": t.get("finished_at") or time.time(),
                "target": t.get("target", ""), "count": max(0, converted),
                "bytes_before": t.get("bytes_before_done", 0),
                "bytes_after": t.get("bytes_after_done", 0)})

def _rewrite_history(keep):
    """Rewrite history.jsonl with only the entries `keep` accepts. Returns removed count."""
    with history_lock:
        entries = load_history()
        left = [e for e in entries if keep(e)]
        removed = len(entries) - len(left)
        if removed:
            tmp = HISTORY_FILE + ".tmp"
            with open(tmp, "w") as f:
                for e in left:
                    f.write(json.dumps(e) + "\n")
            os.replace(tmp, HISTORY_FILE)
    return removed

def delete_history_entry(run_id, path, timestamp):
    """Remove one file entry. All three keys must match – names repeat across runs."""
    def keep(e):
        return not (e.get("run_id") == run_id
                    and e.get("path") == path
                    and float(e.get("timestamp", 0) or 0) == float(timestamp))
    return _rewrite_history(keep)

def delete_history_run(run_id):
    """Remove every file entry of one run. The run summary itself is kept on purpose."""
    return _rewrite_history(lambda e: e.get("run_id") != run_id)

migrate_history()
migrate_runs()
stats_reparieren()

# ─── Formats ─────────────────────────────────────────────────────────────────
VID_EXT = {".mp4",".mkv",".avi",".mov",".wmv",".flv",".webm",".m4v",
           ".mpg",".mpeg",".ts",".vob",".3gp",".ogv"}
AUD_EXT = {".mp3",".flac",".aac",".ogg",".wav",".wma",".m4a",".opus",
           ".ac3",".dts",".aiff",".alac"}

# Verfügbare Zielcontainer (Dateiendung + Anzeigename). MKV ist der Standard:
# nimmt jeden Video-/Audio-/Untertitel-Codec auf. Die anderen sind je Codec
# über VID_CODECS[...]["containers"] freigegeben (Muxer-Kompatibilität).
CONTAINERS = {
    "mkv":  {"label":"MKV",  "ext":".mkv",  "hint":"Matroska – nimmt alles auf (Untertitel, mehrere Tonspuren)."},
    "mp4":  {"label":"MP4",  "ext":".mp4",  "hint":"Maximale Kompatibilität, läuft auf praktisch jedem Gerät."},
    "mov":  {"label":"MOV",  "ext":".mov",  "hint":"QuickTime – für Apple-Umfeld und Videoschnitt."},
    "webm": {"label":"WebM", "ext":".webm", "hint":"Für Web/Browser (nur VP9/AV1 + Opus/Vorbis)."},
    "avi":  {"label":"AVI",  "ext":".avi",  "hint":"Sehr altes Format für alte Abspielgeräte."},
}

VID_CODECS = {
    "h265":  {"label":"H.265 (HEVC)", "probe":"hevc", "vaapi":"hevc_vaapi", "cpu":"libx265",   "cpu_opts":["-preset","medium"], "crf":24,  "containers":["mkv","mp4","mov"],
              "hint":"Beste Wahl für Filme und Serien – etwa halb so groß wie H.264, läuft auf jedem aktuellen Gerät.", "saving":"−50 bis −75 %"},
    "h264":  {"label":"H.264 (AVC)",  "probe":"h264", "vaapi":"h264_vaapi", "cpu":"libx264",   "cpu_opts":["-preset","medium"], "crf":22,  "containers":["mkv","mp4","mov"],
              "hint":"Maximale Kompatibilität, auch für alte Geräte – spart aber nur wenig Platz.", "saving":"−0 bis −30 %"},
    "av1":   {"label":"AV1",          "probe":"av1",  "vaapi":"av1_vaapi",  "cpu":"libsvtav1",  "cpu_opts":["-preset","6"],      "crf":30,  "containers":["mkv","mp4","webm"],
              "hint":"Kleinste Dateien, aber ohne GPU sehr langsam – nur mit viel Geduld oder AV1-fähiger Grafikkarte.", "saving":"−60 bis −80 %"},
    "vp9":   {"label":"VP9",          "probe":"vp9",  "vaapi":"vp9_vaapi",  "cpu":"libvpx-vp9", "cpu_opts":["-b:v","0"],         "crf":30,  "containers":["mkv","webm"],
              "hint":"Freier Codec aus dem Web-Umfeld, ähnlich effizient wie H.265 – für Browser-Wiedergabe.", "saving":"−40 bis −65 %"},
    "mpeg4": {"label":"MPEG-4",       "probe":"mpeg4","vaapi":None,         "cpu":"mpeg4",      "cpu_opts":[],                   "crf":None,"containers":["mkv","mp4","avi"],
              "hint":"Altes Format für sehr alte Abspielgeräte – macht Dateien meist größer, nicht kleiner.", "saving":"±0 %"},
}

def codec_containers(target):
    """Freigegebene Container eines Video-Codecs (erster = Standard)."""
    return VID_CODECS.get(target, {}).get("containers") or ["mkv"]

def out_ext(is_video, target, container):
    """Zielendung: Video → gewählter Container, Audio → Codec-Endung."""
    if is_video:
        cont = container if container in codec_containers(target) else codec_containers(target)[0]
        return CONTAINERS[cont]["ext"]
    return AUD_CODECS[target]["ext"]

def ton_herausloesen(src, is_video):
    """Pure: Musik-Modus mit einer VIDEO-Quelle → der Ton wird als eigene Datei neben
    das Video gelegt, das Video bleibt (27.09.2026). Bei reinen Audiodateien ersetzt
    das Ergebnis wie bisher die Quelle."""
    return (not is_video) and Path(src).suffix.lower() in VID_EXT

def quell_endungen(ctype):
    """Pure: welche Dateiendungen ein Auftragstyp als Quelle annimmt. Der Musik-Modus
    nimmt auch Videos – sonst ließe sich aus keinem Video Musik machen."""
    if ctype == "audio":
        return VID_EXT | AUD_EXT
    return VID_EXT
AUD_CODECS = {
    "mp3":  {"label":"MP3",        "probe":"mp3",      "enc":"libmp3lame","opts":["-q:a","2"],    "ext":".mp3",  "hint":"Läuft überall, gute Qualität bei kleiner Größe."},
    "aac":  {"label":"AAC",        "probe":"aac",      "enc":"aac",       "opts":["-b:a","192k"], "ext":".m4a",  "hint":"Effizienter als MP3, Standard auf Apple- und Android-Geräten."},
    "flac": {"label":"FLAC",       "probe":"flac",     "enc":"flac",      "opts":[],              "ext":".flac", "hint":"Verlustfrei – spart nur bei WAV/AIFF-Quellen Platz."},
    "opus": {"label":"Opus",       "probe":"opus",     "enc":"libopus",   "opts":["-b:a","128k"], "ext":".ogg",  "hint":"Kleinste Dateien bei bester Qualität, aber nicht jedes Gerät kann es."},
    "wav":  {"label":"WAV (PCM)",  "probe":"pcm_s16le","enc":"pcm_s16le", "opts":[],              "ext":".wav",  "hint":"Unkomprimiert – sehr groß, nur für Bearbeitung sinnvoll."},
    "ac3":  {"label":"AC3 (Dolby)","probe":"ac3",      "enc":"ac3",       "opts":[],              "ext":".ac3",  "hint":"Für Heimkino-Receiver mit Dolby-Digital-Eingang."},
    "ogg":  {"label":"Vorbis",     "probe":"vorbis",   "enc":"libvorbis", "opts":["-q:a","5"],    "ext":".ogg",  "hint":"Freier Vorgänger von Opus, gute Qualität, breite Unterstützung."},
}

QUALITIES = [
    {"key":"fast",     "label":"Schnell",    "hint":"Größte Ersparnis, sichtbar weicher"},
    {"key":"balanced", "label":"Ausgewogen", "hint":"Kaum vom Original zu unterscheiden"},
    {"key":"high",     "label":"Hoch",       "hint":"Archivqualität, größere Dateien"},
]
QUALITY_KEYS = {q["key"] for q in QUALITIES}
VAAPI_QP = {"fast": 26, "balanced": 22, "high": 18}
# vp9_vaapi/av1_vaapi kennen kein -qp (gemessen: wirkungslos) – dort steuert -global_quality
# zusammen mit -rc_mode CQP. Höherer Wert = kleinere Datei.
VAAPI_GQ = {
    "vp9": {"fast": 150, "balanced": 110, "high": 80},
    "av1": {"fast": 160, "balanced": 130, "high": 100},
}
CRF_DELTA = {"fast": 4, "balanced": 0, "high": -4}
MPEG4_QV = {"fast": 6, "balanced": 4, "high": 2}

# Untertitel-Codecs, die Matroska direkt aufnehmen kann
SUB_COPY = {"subrip","srt","ass","ssa","webvtt","hdmv_pgs_subtitle","dvd_subtitle","dvb_subtitle","kate"}
# Textformate, die MKV nicht direkt kann, die aber verlustarm nach SubRip wandern
SUB_TO_SRT = {"mov_text","text","tx3g","subviewer","subviewer1","microdvd","mpl2","pjs",
              "realtext","sami","stl","jacosub","vplayer"}
# Bild-basierte Untertitel (Bitmap) – MP4/WebM können sie nicht aufnehmen, nur MKV.
SUB_IMAGE = {"hdmv_pgs_subtitle","dvd_subtitle","dvb_subtitle","xsub","dvbsub"}
# Alle text-basierten Untertitel (kopierbar ODER nach Text wandelbar).
SUB_TEXT = (SUB_COPY - SUB_IMAGE) | SUB_TO_SRT

# Untertitel-Strategie je Container. mode = wohin Textspuren wandern; image = ob
# Bitmap-Untertitel muxbar sind. Nur MKV kann beliebige Untertitel + Bitmaps.
CONTAINER_SUBS = {
    "mkv":  {"text": None,      "image": True},   # None = MKV-Sonderweg (copy/srt)
    "mp4":  {"text": "mov_text","image": False},
    "mov":  {"text": "mov_text","image": False},
    "webm": {"text": "webvtt",  "image": False},
    "avi":  {"text": None,      "image": False, "drop_all": True},
}

# Audio-Codecs, die der jeweilige Container per Stream-Copy aufnimmt; alles andere
# wird in den Fallback-Encoder umgewandelt. copy=None → Container nimmt alles (MKV).
CONTAINER_AUDIO = {
    "mkv":  {"copy": None, "fallback": None},
    "mp4":  {"copy": {"aac","ac3","eac3","mp3","alac","mp2","dts"}, "fallback": ("aac", ["-b:a","192k"])},
    "mov":  {"copy": {"aac","ac3","eac3","mp3","alac","mp2"},       "fallback": ("aac", ["-b:a","192k"])},
    "webm": {"copy": {"opus","vorbis"},                            "fallback": ("libopus", ["-b:a","128k"])},
    "avi":  {"copy": {"mp3","ac3","aac"},                          "fallback": ("libmp3lame", ["-q:a","2"])},
}

# Namensteil der eigenen Zwischen-Datei (X.converting.mkv). Sie trägt eine Medien-
# endung und darf deshalb NIE als Quelle eingesammelt werden: sonst wäre eine Leiche
# aus einem abgestürzten Lauf beim nächsten Scan plötzlich ein Konvertier-Auftrag und
# die Zielkollisions-Prüfung (S3) hätte ein Loch (R1).
TMP_MARK = ".converting"

SYSTEM_DIRS = {"bin","boot","dev","etc","lib","lib32","lib64","libx32","sbin","usr","proc","sys","run",
               "var","tmp","root","opt","srv","overlay","rootfs","mnt","lost+found","@eaDir","#recycle",
               "snapshot",".snapshot"}

def split_dirs(names, is_root):
    """Split directory names into (visible, system). SYSTEM_DIRS only on root level,
    dot-prefixed names on every level. Pure helper."""
    folders, system = [], []
    for n in sorted(names):
        if n.startswith(".") or (is_root and n in SYSTEM_DIRS) or (is_root and MC_ROOTS and n not in MC_ROOTS):
            system.append(n)
        else:
            folders.append(n)
    return folders, system

# ─── Flask ───────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static")
app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# ─── State (thread-safe via lock) ────────────────────────────────────────────
lock = threading.Lock()
queue_cv = threading.Condition(lock)
state = {
    "current": None,     # running task (full dict incl. _procs/t0)
    "queue": [],         # waiting tasks
    "last": None,        # last finished task
    "cancel": False,     # cancel flag for the current task
    "discard_id": None,  # job id that /api/clear dropped – must not resurface as `last`
}

def _task_copy(t):
    if not t:
        return None
    copy = {k: v for k, v in t.items() if not k.startswith("_") and k != "t0"}
    copy["files"] = [dict(f) for f in t["files"]]
    return copy

def _queue_brief(t):
    return {"id": t["id"], "folder": t["folder"], "folders": list(t.get("folders") or []),
            "target_label": t["target_label"], "container": t.get("container"), "total": t["total"],
            "bytes_total": t["bytes_total"], "quality": t["quality"]}

def get_status():
    """Snapshot as served by /api/status and /api/events."""
    with lock:
        cur = _task_copy(state["current"])
        return {
            "active": cur is not None,
            "task": cur,
            "queue": [_queue_brief(t) for t in state["queue"]],
            "last": _task_copy(state["last"]),
        }

# ─── GPU Check ───────────────────────────────────────────────────────────────
def parse_vainfo(output):
    """Pure: vainfo text → {"h264","hevc","hevc10","vp9","av1"} encode capability dict.
    `hevc10` (Main10-Profil) wird für 10-bit-Quellen gebraucht."""
    enc = [l for l in output.split("\n") if "VAProfile" in l and "Enc" in l]
    return {
        "h264":   any("H264" in l for l in enc),
        "hevc":   any("HEVC" in l for l in enc),
        "hevc10": any("HEVCMain10" in l for l in enc),
        "vp9":    any("VP9" in l for l in enc),
        "av1":    any("AV1" in l for l in enc),
    }

def check_vaapi():
    caps = {"h264": False, "hevc": False, "hevc10": False, "vp9": False, "av1": False}
    if not os.path.exists("/dev/dri/renderD128"):
        print("[GPU] /dev/dri/renderD128 not found", flush=True)
        return caps
    try:
        r = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=10,
                           env={**os.environ, "DISPLAY":"", "XDG_RUNTIME_DIR":"/tmp"})
        output = r.stdout + r.stderr  # vainfo prints to both
        if "VAProfile" in output:
            caps = parse_vainfo(output)
            print(f"[GPU] VAAPI encode: {caps}", flush=True)
    except Exception as e:   # FileNotFoundError (no vainfo), timeout, ...
        print(f"[GPU] vainfo error: {e}", flush=True)
    return caps

GPU_CAPS = check_vaapi()
GPU = any(GPU_CAPS.values())
print(f"[INIT] GPU VAAPI: {'YES' if GPU else 'NO - using CPU'}", flush=True)

def codec_gpu(target):
    c = VID_CODECS.get(target)
    return bool(c and c.get("vaapi") and GPU_CAPS.get(c["probe"]))

# ─── ffprobe helpers ─────────────────────────────────────────────────────────
def probe(fp):
    try:
        r = subprocess.run(
            ["ffprobe","-v","quiet","-print_format","json","-show_streams","-show_format",str(fp)],
            capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None

def is_10bit(pix_fmt, bits=None):
    """Pure: erkennt >8-bit-Videoformate. Bewusst NICHT über '10 in pix_fmt' –
    das trifft sonst yuv410p (8 bit)."""
    pf = (pix_fmt or "").lower()
    if pf.endswith(("10le", "10be", "12le", "12be")) or "p010" in pf or "p012" in pf:
        return True
    try:
        if bits is not None and int(bits) > 8:
            return True
    except (TypeError, ValueError):
        pass
    return False

def info_from_probe(info):
    """Pure: ffprobe JSON → info dict (see get_info)."""
    out = {"vc": None, "ac": None, "dur": 0.0, "pix_fmt": "", "is10": False, "bits": None,
           "n_video": 0, "n_audio": 0, "n_sub": 0, "n_att": 0, "n_cover": 0, "size": 0,
           "sub_streams": [], "audio_streams": [], "covers": []}
    if not info:
        return out
    for s in info.get("streams", []):
        t = s.get("codec_type", "")
        n = (s.get("codec_name") or "").lower()
        if t == "video":
            # Cover art is flagged as attached_pic – doesn't count as a video track.
            # Die TV-Reparatur muss sie aber mitnehmen (MKV-Bild-Anhänge zeigt ffmpeg
            # als solche Spuren, nicht als „attachment“) – deshalb getrennt zählen.
            if (s.get("disposition") or {}).get("attached_pic"):
                tags = s.get("tags") or {}
                out["n_cover"] += 1
                out["covers"].append({"index": s.get("index"), "codec": n,
                                      "filename": tags.get("filename") or "",
                                      "mimetype": tags.get("mimetype") or ""})
                continue
            out["n_video"] += 1
            if not out["vc"]:
                out["vc"] = n
                out["pix_fmt"] = s.get("pix_fmt") or ""
                out["bits"] = s.get("bits_per_raw_sample")
        elif t == "audio":
            out["n_audio"] += 1
            tags = s.get("tags") or {}
            # Kanäle, Sprache und Markierungen braucht die TV-Reparatur (Haupttonspur
            # finden, Standard-Flag umsetzen, andere Flags wie „comment“ erhalten).
            out["audio_streams"].append({"index": s.get("index"), "codec": n,
                                         "channels": s.get("channels"),
                                         "lang": (tags.get("language") or "").lower(),
                                         "title": tags.get("title") or "",
                                         "tags": sorted(tags),
                                         "dauer": mkv_dauer(tags),
                                         "disp": dict(s.get("disposition") or {})})
            if not out["ac"]:
                out["ac"] = n
        elif t == "subtitle":
            out["n_sub"] += 1
            out["sub_streams"].append({"index": s.get("index"), "codec": n})
        elif t == "attachment":
            out["n_att"] += 1
    out["is10"] = is_10bit(out["pix_fmt"], out["bits"])
    fmt = info.get("format") or {}
    try:
        out["dur"] = float(fmt.get("duration"))
    except (TypeError, ValueError):
        out["dur"] = 0.0
    try:
        out["size"] = int(fmt.get("size") or 0)
    except (TypeError, ValueError):
        out["size"] = 0
    return out

def mkv_dauer(tags):
    """Pure: DURATION-Tag einer MKV-Spur ("01:52:03.456000000", auch "DURATION-eng")
    → Sekunden oder None."""
    for k, v in (tags or {}).items():
        if k.upper().split("-")[0] == "DURATION":
            m = re.match(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$", str(v).strip())
            if m:
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return None

def get_info(fp):
    """Single ffprobe call → dict {vc, ac, dur, pix_fmt, is10, bits, n_video, n_audio,
    n_sub, size, sub_streams} – oder None, wenn ffprobe die Datei nicht lesen konnte.
    None NIE als "leere" Info behandeln: sonst wären alle Erwartungswerte 0 und die
    Spuren-Verifikation nach dem Encode wäre vakuum-wahr (S5)."""
    raw = probe(fp)
    return info_from_probe(raw) if raw is not None else None

def _filesize(fp):
    try:
        return os.path.getsize(fp)
    except OSError:
        return 0

CODEC_ALIASES = {"h264":{"h264","avc"}, "hevc":{"hevc","h265"}, "av1":{"av1"},
     "vp9":{"vp9"}, "mpeg4":{"mpeg4"}, "mp3":{"mp3"}, "aac":{"aac"},
     "flac":{"flac"}, "opus":{"opus"}, "vorbis":{"vorbis"}, "ac3":{"ac3"},
     "pcm_s16le":{"pcm_s16le","pcm_s24le","pcm_s32le"}}

def codec_matches(current, target, is_video):
    """Compare an already-known codec name against a target codec key (no ffprobe call)."""
    if not current:
        return False
    tgt = (VID_CODECS if is_video else AUD_CODECS).get(target,{}).get("probe","")
    c = current.lower()
    for k, names in CODEC_ALIASES.items():
        if (tgt in names or tgt == k) and (c in names or c == k):
            return True
    return c == tgt

def is_temp_name(path):
    """Pure: True für unsere eigenen Zwischen-Dateien (X.converting.mkv)."""
    return Path(path).stem.lower().endswith(TMP_MARK)

def is_media_file(path):
    """Pure: Mediendatei nach Endung – ohne die eigenen .converting-Reste (R1)."""
    return Path(path).suffix.lower() in (VID_EXT | AUD_EXT) and not is_temp_name(path)

def scan(folder):
    root = Path(folder)
    if not root.exists():
        return []
    return sorted(str(f) for f in root.rglob("*")
                  if f.is_file() and is_media_file(f))

def rel_dir_of(path, folder):
    """Directory of `path` relative to `folder` ("" if directly inside)."""
    try:
        r = os.path.relpath(os.path.dirname(path), folder)
    except ValueError:
        return ""
    return "" if r in (".", "") else r

def folder_label(real):
    """Pure: Anzeigename des Auftragsordners, IMMER aus dem bereits geprüften Pfad
    abgeleitet – nie aus dem Rohtext des Clients. Sonst landet beliebiger Text
    (inklusive Markup) dauerhaft in der Warteschlangen-Anzeige aller Betrachter."""
    try:
        rel = os.path.relpath(real, os.path.realpath(MEDIA_ROOT))
    except (ValueError, OSError):
        return ""
    return "" if rel in (".", "") else rel.replace(os.sep, "/")

def fmt_dur(sec):
    sec = int(max(0, round(sec or 0)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ─── Progress file parser ────────────────────────────────────────────────────
_SPEED_RE = re.compile(r"^speed=\s*([0-9.]+)x?")

def parse_progress(data):
    """Pure: ffmpeg -progress text → {"us": int, "fps": float, "speed": float} (last values win)."""
    us, fps, speed = 0, 0.0, 0.0
    for line in data.split("\n"):
        line = line.strip()
        if line.startswith("out_time_us="):
            try:
                v = int(line.split("=", 1)[1].strip())
                if v > 0:
                    us = v
            except ValueError:
                pass
        elif line.startswith("fps="):
            try:
                fps = float(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("speed="):
            m = _SPEED_RE.match(line)
            if m:
                try:
                    speed = float(m.group(1))
                except ValueError:
                    pass
    return {"us": us, "fps": fps, "speed": speed}

def read_progress(path):
    """Read last out_time_us / fps / speed from ffmpeg progress file."""
    try:
        with open(path, "r") as f:
            data = f.read()
    except OSError:
        return {"us": 0, "fps": 0.0, "speed": 0.0}
    return parse_progress(data)

# ─── ffmpeg command builders ─────────────────────────────────────────────────
def subtitle_args(sub_streams, container="mkv"):
    """Pure: Untertitel-Spuren der Quelle → (ffmpeg-Argumente, gemappt, verworfene Codecs).

    Je Container unterschiedlich: MKV nimmt fast alles (Text kopieren / nach SubRip
    wandeln, Bitmap kopieren), MP4/MOV nur Text → mov_text (Bitmap verworfen), WebM
    nur Text → WebVTT, AVI gar keine Untertitel. `-c:s copy` mit einem unpassenden
    Untertitel-Codec lässt den Muxer abbrechen und die ganze Datei scheitern (R4).
    """
    conf = CONTAINER_SUBS.get(container, CONTAINER_SUBS["mkv"])
    args, mapped, dropped = [], 0, []
    for s in sub_streams or []:
        codec = (s.get("codec") or "").lower()
        idx = s.get("index")
        if idx is None:
            dropped.append(codec or "unknown")
            continue
        if conf.get("drop_all"):
            dropped.append(codec or "unknown")
            continue
        is_image = codec in SUB_IMAGE
        if conf["text"] is None:
            # MKV-Sonderweg: kopieren was geht, Text sonst nach SubRip.
            if codec in SUB_COPY and (conf["image"] or not is_image):
                mode = "copy"
            elif codec in SUB_TO_SRT:
                mode = "srt"
            else:
                dropped.append(codec or "unknown")
                continue
        else:
            # MP4/MOV/WebM: nur Text, in das Container-Textformat gewandelt.
            if codec in SUB_TEXT and not is_image:
                mode = conf["text"]
            else:
                dropped.append(codec or "unknown")
                continue
        args += ["-map", f"0:{idx}", f"-c:s:{mapped}", mode]
        mapped += 1
    return args, mapped, dropped

def audio_args(audio_streams, container="mkv"):
    """Pure: Audiospuren → ffmpeg-Argumente. MKV kopiert alles; andere Container
    kopieren nur die von ihnen unterstützten Codecs und wandeln den Rest in den
    Container-Fallback (z. B. DTS → AAC für MP4), damit der Muxer nicht abbricht."""
    conf = CONTAINER_AUDIO.get(container, CONTAINER_AUDIO["mkv"])
    copy_set = conf["copy"]
    streams = audio_streams or []
    if not streams:
        return ["-map", "0:a?", "-c:a", "copy"]   # Quelle ohne bekannte Audiospuren
    args = []
    for i, s in enumerate(streams):
        idx = s.get("index")
        if idx is None:
            continue
        codec = (s.get("codec") or "").lower()
        args += ["-map", f"0:{idx}"]
        if copy_set is None or codec in copy_set:
            args += [f"-c:a:{i}", "copy"]
        else:
            enc, opts = conf["fallback"]
            args += [f"-c:a:{i}", enc] + opts
    return args

def _map_args(sub_streams, audio_streams=None, container="mkv"):
    """Alle Spuren übernehmen: Video (0:V schließt Cover-Art aus), Audiospuren (je
    Container kopiert oder umgewandelt), die muxbaren Untertitel, Attachments/Fonts
    (nur MKV), Metadaten und Kapitel."""
    subs, _, _ = subtitle_args(sub_streams, container)
    aud = audio_args(audio_streams, container)
    att = ["-map","0:t?","-c:t","copy"] if container == "mkv" else []
    return (["-map","0:V:0"] + aud + subs + att +
            ["-map_metadata","0","-map_chapters","0"])

def _qp(quality):
    return str(VAAPI_QP.get(quality, VAAPI_QP["balanced"]))

def _vaapi_quality(target, quality):
    """Qualitätsoption je VAAPI-Encoder: h264/hevc verstehen -qp, vp9/av1 nicht (gemessen)."""
    gq = VAAPI_GQ.get(target)
    if gq:
        return ["-rc_mode","CQP","-global_quality", str(gq.get(quality, gq["balanced"]))]
    return ["-qp", _qp(quality)]

def _cpu_opts(target, quality):
    c = VID_CODECS[target]
    opts = list(c["cpu_opts"])
    if c.get("crf") is not None:
        opts += ["-crf", str(c["crf"] + CRF_DELTA.get(quality, 0))]
    elif target == "mpeg4":
        opts += ["-q:v", str(MPEG4_QV.get(quality, MPEG4_QV["balanced"]))]
    return opts

def cmd_vaapi_hwaccel(src, dst, target, progfile, quality="balanced", pix10=False, sub_streams=None, audio_streams=None, container="mkv"):
    """Full hardware: VAAPI decode + encode."""
    c = VID_CODECS[target]
    return ["ffmpeg","-y","-hide_banner","-nostats","-progress",progfile,
            "-hwaccel","vaapi","-hwaccel_device","/dev/dri/renderD128",
            "-hwaccel_output_format","vaapi",
            "-i",str(src)] + _map_args(sub_streams, audio_streams, container) + \
           ["-c:v",c["vaapi"]] + _vaapi_quality(target, quality) + [str(dst)]

def cmd_vaapi_upload(src, dst, target, progfile, quality="balanced", pix10=False, sub_streams=None, audio_streams=None, container="mkv"):
    """Software decode + VAAPI encode via hwupload filter (p010 for 10-bit sources)."""
    c = VID_CODECS[target]
    ten = bool(pix10) and target in ("h265", "vp9", "av1")
    vf = "format=p010,hwupload" if ten else "format=nv12,hwupload"
    extra = ["-profile:v","main10"] if (ten and target == "h265") else []
    return ["ffmpeg","-y","-hide_banner","-nostats","-progress",progfile,
            "-vaapi_device","/dev/dri/renderD128",
            "-i",str(src)] + _map_args(sub_streams, audio_streams, container) + ["-vf",vf,
            "-c:v",c["vaapi"]] + extra + _vaapi_quality(target, quality) + [str(dst)]

def cmd_cpu_video(src, dst, target, progfile, quality="balanced", pix10=False, sub_streams=None, audio_streams=None, container="mkv"):
    """Pure CPU encode."""
    c = VID_CODECS[target]
    return ["ffmpeg","-y","-hide_banner","-nostats","-progress",progfile,
            "-i",str(src)] + _map_args(sub_streams, audio_streams, container) + \
           ["-c:v",c["cpu"]] + _cpu_opts(target, quality) + [str(dst)]

def gpu_attempts(src, dst, target, progfile, quality, info, sub_streams, caps, container="mkv"):
    """Welche VAAPI-Versuche für diese Quelle überhaupt Sinn haben → [(Methode, cmd), …].

    Gemessen: 10-bit → H.264 scheitert im vollen Hardware-Pfad ("No usable encoding
    profile"), läuft aber über den Upload-Pfad mit nv12. 10-bit → HEVC braucht das
    Main10-Encode-Profil der GPU; fehlt es, ist jeder GPU-Versuch verlorene Zeit.
    """
    ten = bool(info.get("is10"))
    aud = info.get("audio_streams")
    if ten and target == "h265" and not caps.get("hevc10"):
        return []
    out = []
    if not (ten and target == "h264"):
        out.append(("GPU", cmd_vaapi_hwaccel(src, dst, target, progfile, quality, ten, sub_streams, aud, container)))
    out.append(("GPU*", cmd_vaapi_upload(src, dst, target, progfile, quality, ten, sub_streams, aud, container)))
    return out

def cmd_audio(src, dst, target, progfile, quality="balanced", pix10=False, kopie=False, spur=None):
    """`spur` = Nummer der Tonspur (0-basiert) bei Videoquellen – ffmpeg nähme sonst die
    Spur mit den meisten Kanälen, nicht die als Standard markierte. `kopie` übernimmt
    die Spur unverändert (gleicher Codec wie das Ziel: nie schlechter als neu kodiert)."""
    c = AUD_CODECS[target]
    cmd = ["ffmpeg","-y","-hide_banner","-nostats","-progress",progfile,
           "-i",str(src),"-map_metadata","0"]
    if spur is not None:
        cmd += ["-map", f"0:a:{int(spur)}"]
    if kopie:
        cmd += ["-c:a","copy"]
    else:
        cmd += ["-c:a",c["enc"]] + c["opts"]
    # Kein Bild, keine Untertitel, keine Datenspuren – bei Videoquellen würde ffmpeg
    # sonst z. B. Untertitel nach mov_text zwängen und an Bitmap-Untertiteln scheitern.
    return cmd + ["-vn","-sn","-dn", str(dst)]

def standard_tonspur(audio_streams):
    """Pure: Nummer (0-basiert) der als Standard markierten Tonspur, sonst 0."""
    for i, s in enumerate(audio_streams or []):
        if (s.get("disp") or {}).get("default"):
            return i
    return 0

def tonspur_codec(info):
    """Pure: Codec der Tonspur, die beim Herauslösen genommen wird (die Standard-Spur –
    nicht zwingend die erste, deren Codec in info["ac"] steht)."""
    spuren = info.get("audio_streams") or []
    if spuren:
        return spuren[standard_tonspur(spuren)].get("codec") or info.get("ac")
    return info.get("ac")

# ─── Zielpfade ───────────────────────────────────────────────────────────────
def target_path(src, ext):
    """Pure: Quellpfad + Zielendung → Zielpfad (gleicher Ordner, gleicher Stamm)."""
    return str(Path(src).with_suffix(ext))

def tmp_path(src, ext):
    """Pure: Quellpfad + Zielendung → Pfad der Zwischen-Datei (X.converting.mkv)."""
    return str(Path(src).with_suffix(TMP_MARK + ext))

class ErsetzenAbgelehnt(Exception):
    """Das geprüfte Ergebnis wird bewusst NICHT ans Ziel geschoben (Quelle hat sich
    verändert, Zieldatei inzwischen entstanden). Die Meldung ist für die Oberfläche."""

def quell_signatur(st):
    """Pure: Fingerabdruck einer Quelle – Inode, Größe, mtime."""
    return (st.st_ino, st.st_size, st.st_mtime)

def ziel_anlegen_ohne_ueberschreiben(tmp, dst):
    """Zwischen-Datei unter dem Zielnamen sichtbar machen, ohne eine Zieldatei zu
    überschreiben, die WÄHREND des Encodes entstanden ist.

    os.link legt den zweiten Namen atomar an und scheitert mit EEXIST, wenn dst
    schon da ist – os.replace hätte stumm überschrieben. Danach ist tmp nur noch
    ein zweiter Name derselben Datei und wird gelöst. Dateisysteme ohne Hardlinks
    (FAT/exFAT, manche SMB-Mounts) fallen auf Prüfung + os.replace zurück –
    nicht atomar, aber nicht schlechter als bisher."""
    try:
        os.link(tmp, dst)
    except FileExistsError:
        raise ErsetzenAbgelehnt(f"Zieldatei ist inzwischen entstanden: {os.path.basename(dst)}")
    except OSError as e:
        if os.path.lexists(dst):
            raise ErsetzenAbgelehnt(f"Zieldatei ist inzwischen entstanden: {os.path.basename(dst)}")
        print(f"[WARN] Hardlink nicht möglich ({e}) – ersetze per os.replace", flush=True)
        os.replace(tmp, dst)
        return
    try:
        os.unlink(tmp)
    except OSError as e:
        print(f"[WARN] Zwischen-Datei blieb als zweiter Name liegen {tmp}: {e}", flush=True)

def find_target_collisions(paths, ext):
    """Pure: {Quellpfad: Name der belegenden Quelle} für alle Quellen eines Jobs, die
    auf denselben Zielpfad zeigen (z. B. a.mkv und a.mp4 → a.mkv). Die Datei, die an
    Ort und Stelle konvertiert wird, behält den Zielpfad; alle weiteren werden abgelehnt."""
    inplace = [p for p in paths if target_path(p, ext) == p]
    others = [p for p in paths if target_path(p, ext) != p]
    seen, conflicts = {}, {}
    for p in inplace + others:
        d = target_path(p, ext)
        if d in seen:
            conflicts[p] = os.path.basename(seen[d])
        else:
            seen[d] = p
    return conflicts

# ─── Verification ────────────────────────────────────────────────────────────
def streams_ok(before, after, dur_in, dur_out, is_video=True):
    """Pure. before/after = {"v","a","s"} stream counts (+ optional "size" in after).
    Returns (True, "") or (False, german error text)."""
    if after.get("size", 1) == 0:
        return False, "Ausgabedatei ist leer"
    if is_video:
        missing = []
        if after.get("v", 0) < 1:
            missing.append(f"Video {before.get('v', 0)}→{after.get('v', 0)}")
        if after.get("a", 0) < before.get("a", 0) or after.get("s", 0) < before.get("s", 0):
            missing.append(f"Audio {before.get('a', 0)}→{after.get('a', 0)}")
            missing.append(f"Untertitel {before.get('s', 0)}→{after.get('s', 0)}")
        if missing:
            return False, "Spuren fehlen nach Encode (" + ", ".join(missing) + ")"
    else:
        if after.get("a", 0) < 1:
            return False, "Spuren fehlen nach Encode (Audio 1→0)"
    if dur_in and dur_in > 0:
        if not dur_out or dur_out < 0.98 * dur_in:
            return False, f"Ausgabe zu kurz ({fmt_dur(dur_out)} statt {fmt_dur(dur_in)})"
    return True, ""

# ─── TV-Reparatur: „TV-Ton ergänzen“ ─────────────────────────────────────────
# Befund 24.09.2026: Samsung-Fernseher ab 2023 (Tizen 7) spielen weder DTS noch
# TrueHD (Samsung-Datenblätter 2023–2025), E-AC3 nur bis 5.1. Die Plex-App schickt
# DTS trotzdem per Direct Play → Film ohne Ton; nur den Ton umrechnen lassen friert
# auf Tizen 7+ häufig ein. Abhilfe: aus der Haupttonspur eine AC3-Spur (Dolby
# Digital, max. 5.1) erzeugen und als ERSTE + Standard-Spur einsetzen – die
# Samsung-App spielt nur die erste/Standard-Spur direkt ab, Plex wählt die erste
# Spur der bevorzugten Sprache. Bild, Originalton und Untertitel werden nur
# umverpackt, nie neu kodiert. Vorgabe des Betreibers: Ton bleibt lippensynchron –
# deshalb vergleicht tvton_sync_pruefen() den INHALT der neuen Spur mit der Vorlage.
TV_TON_UNTAUGLICH = {"dts", "truehd", "mlp"}
TV_TON_MAX_KANAELE = 6
TV_SPRACHEN = {"ger", "deu", "de"}          # Haupttonspur: erste deutsche, sonst die erste
TV_SYNC_MAX_MS = 20.0                       # mehr Versatz → Ergebnis verworfen, Original bleibt
TV_SYNC_STELLEN = (0.05, 0.22, 0.39, 0.56, 0.73, 0.90)  # Anteile der Laufzeit für die Inhaltsprobe
TV_SYNC_MIN_KORR = 0.75                     # echte Paare 0,87–1,0; Teil-Versatz ~0,1
TV_SYNC_SEKUNDEN = 8
TV_SYNC_BLOCK_MS = 2.5
TV_SYNC_RATE = 8000
TV_SYNC_LAUT = 40.0                         # mittlerer Betrag (s16) darunter = Stille
TVFIX = {
    "tvton": {"label": "TV-Ton ergänzen (Dolby Digital)",
              "hint": ("Für Fernseher ohne DTS/TrueHD (z. B. Samsung ab 2023, sonst bleibt der Film "
                       "stumm): legt aus der Haupttonspur eine Dolby-Digital-Spur (max. 5.1) als erste "
                       "Spur an. Bild, Originalton und Untertitel bleiben unverändert; die neue Spur "
                       "wird auf Lippensynchronität geprüft. Nur MKV."),
              "ext": ".mkv", "saving": "+ ca. 5 MB je Minute"},
}


def tv_ton_tauglich(s):
    """Pure: Kann eine Fernseher-App diese Tonspur selbst abspielen?"""
    codec = (s.get("codec") or "").lower()
    if codec in TV_TON_UNTAUGLICH:
        return False
    if codec in ("ac3", "eac3") and (s.get("channels") or 0) > TV_TON_MAX_KANAELE:
        return False
    return True


# Spuren, die NIE Haupttonspur werden: Kommentar, Audiodeskription, Hörgeschädigte.
TV_NEBENSPUR_FLAGS = ("comment", "visual_impaired", "hearing_impaired", "descriptions")
TV_NEBENSPUR_TITEL = re.compile(r"kommentar|commentary|audiodeskription|audio.?description|"
                                r"h[öo]rfilm|\bAD\b", re.I)


def _sprache(s):
    """Pure: Sprachcode normalisiert ("de-DE" → "de", "GER" → "ger")."""
    return (s.get("lang") or "").lower().split("-")[0]


def _nebenspur(s):
    d = s.get("disp") or {}
    return any(d.get(f) for f in TV_NEBENSPUR_FLAGS) or bool(TV_NEBENSPUR_TITEL.search(s.get("title") or ""))


def tvton_plan(audio_streams):
    """Pure: Was muss an den Tonspuren geschehen? None = nichts (schon TV-tauglich).

    Haupttonspur: nur „normale“ Spuren (keine Kommentar-/AD-/HI-Spur); Sprache =
    Deutsch, falls vorhanden, sonst die der ersten normalen Spur. Hat diese Sprache
    eine TV-taugliche Spur, wird die erste davon verlustfrei nach vorn gezogen
    (kodieren=False) – sonst entsteht aus der ersten Spur der Sprache eine neue
    AC3-Spur an erster Stelle, und ALLE Originalspuren folgen unverändert.
    Steht die taugliche Hauptspur schon vorn, trägt aber eine ANDERE Spur die
    Standard-Markierung, werden nur die Markierungen umgesetzt (kodieren=False).
    `rest` = die Originalspuren hinter der ersten, in Originalreihenfolge."""
    spuren = [s for s in (audio_streams or []) if s.get("index") is not None]
    if not spuren:
        return None
    normal = [s for s in spuren if not _nebenspur(s)] or spuren
    deutsch = [s for s in normal if _sprache(s) in TV_SPRACHEN]
    gruppe = deutsch or [s for s in normal if _sprache(s) == _sprache(normal[0])]
    tauglich = [s for s in gruppe if tv_ton_tauglich(s)]
    haupt = tauglich[0] if tauglich else gruppe[0]
    haupt_nr = spuren.index(haupt)
    if tauglich:
        andere_standard = any((s.get("disp") or {}).get("default") for s in spuren if s is not haupt)
        if haupt_nr == 0 and not andere_standard:
            return None
        return {"haupt": haupt, "haupt_nr": haupt_nr, "kodieren": False,
                "kanaele": haupt.get("channels"), "rest": [s for s in spuren if s is not haupt]}
    kanaele = min(haupt.get("channels") or TV_TON_MAX_KANAELE, TV_TON_MAX_KANAELE)
    return {"haupt": haupt, "haupt_nr": haupt_nr, "kodieren": True, "kanaele": kanaele,
            "rest": list(spuren)}


def _disp_ohne_default(s):
    """Pure: Markierungen einer Spur ohne „default“ (für -disposition), sonst "0"."""
    flags = [k for k, v in sorted((s.get("disp") or {}).items()) if v and k != "default"]
    return "+".join(flags) if flags else "0"


BILD_ENDUNG = {"mjpeg": (".jpg", "image/jpeg"), "png": (".png", "image/png"),
               "bmp": (".bmp", "image/bmp"), "gif": (".gif", "image/gif"), "webp": (".webp", "image/webp")}


def tv_bilder_herausloesen(src, covers, tmp):
    """Cover-Bildspuren als Dateien neben die Zwischen-Datei legen (für -attach).
    → ([{pfad, mimetype, filename}], Fehlertext oder "")."""
    bilder = []
    for i, c in enumerate(covers):
        endung, mime = BILD_ENDUNG.get(c.get("codec") or "", (".bin", "application/octet-stream"))
        pfad = f"{tmp}.cover{i}{endung}"
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(src), "-map", f"0:{c['index']}",
                            "-c", "copy", "-frames:v", "1", "-f", "image2", pfad],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(pfad) or os.path.getsize(pfad) == 0:
            return bilder, f"Cover-Bild {i + 1} ließ sich nicht sichern – nichts geändert"
        bilder.append({"pfad": pfad, "mimetype": c.get("mimetype") or mime,
                       "filename": c.get("filename") or f"cover{i}{endung}"})
    return bilder, ""


def cmd_tvton(src, dst, progfile, plan, bilder=(), n_att=0):
    """Pure: ffmpeg-Befehl zum Umverpacken. Video/Untertitel/Anhänge und alle
    Originaltonspuren werden KOPIERT; nur die neue erste Spur wird kodiert.
    aresample=async=1 füllt Lücken der Vorlage mit Stille statt die Folgesamples
    nach vorn zu schieben – sonst liefe der Ton nach jeder Lücke zu früh.
    `bilder` = vorab herausgelöste Cover [{pfad, mimetype, filename}]: ffmpeg führt
    sie als Bild-Spuren, die ein Kopieren nicht wieder als MKV-Anhang schreibt –
    sie kommen per -attach zurück (Anhang-Nummern hinter den `n_att` vorhandenen)."""
    haupt = plan["haupt"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", progfile, "-i", str(src),
           "-map", "0:V", "-map", f"0:{haupt['index']}"]
    for s in plan["rest"]:
        cmd += ["-map", f"0:{s['index']}"]
    cmd += ["-map", "0:s?", "-map", "0:t?", "-c", "copy"]
    if plan["kodieren"]:
        ch = plan["kanaele"] or TV_TON_MAX_KANAELE
        cmd += ["-c:a:0", "ac3", "-b:a:0", "640k" if ch > 2 else "256k"]
        if (haupt.get("channels") or 0) > TV_TON_MAX_KANAELE:
            cmd += ["-ac:a:0", str(TV_TON_MAX_KANAELE)]
        cmd += ["-filter:a:0", "aresample=async=1"]
        # Die neue Spur erbte sonst die MKV-Statistik-Tags der Vorlage (BPS,
        # NUMBER_OF_BYTES … der DTS-Spur) – Plex zeigte dann falsche Werte an.
        # Leerer Wert löscht den Schlüssel NUR auf dieser Spur. NICHT
        # „-map_metadata:s:a:0 -1“: das riss ffmpeg ab (-22) bzw. nahm den
        # Originalspuren die Sprachkennung (Echt-Probe 24.09.2026).
        for k in haupt.get("tags") or []:
            if k.lower() not in ("language", "title"):
                cmd += ["-metadata:s:a:0", f"{k}="]
        art = "5.1" if ch >= 6 else ("Stereo" if ch == 2 else ("Mono" if ch == 1 else f"{ch}.0"))
        cmd += ["-metadata:s:a:0", f"title=Dolby Digital {art} (TV)"]
    lang = (haupt.get("lang") or "").lower()
    if lang:
        cmd += ["-metadata:s:a:0", f"language={lang}"]
    # Vorgezogene Spur behält ihre übrigen Markierungen (z. B. „original“).
    eigene = "" if plan["kodieren"] else _disp_ohne_default(haupt)
    cmd += ["-disposition:a:0", "default" if eigene in ("", "0") else f"default+{eigene}"]
    for i, s in enumerate(plan["rest"], start=1):
        cmd += [f"-disposition:a:{i}", _disp_ohne_default(s)]
    for i, b in enumerate(bilder):
        cmd += ["-attach", str(b["pfad"]),
                f"-metadata:s:t:{n_att + i}", f"mimetype={b['mimetype']}",
                f"-metadata:s:t:{n_att + i}", f"filename={b['filename']}"]
    cmd += ["-map_metadata", "0", "-map_chapters", "0", "-max_muxing_queue_size", "4096", str(dst)]
    return cmd


def tvton_spuren_ok(vorher, nachher, plan):
    """Pure: Stimmen die Spuren nach dem Umverpacken? (True, "") oder (False, Grund)."""
    probleme = []
    if nachher.get("n_video", 0) < 1 or nachher.get("n_video", 0) != vorher.get("n_video", 0):
        probleme.append(f"Video {vorher.get('n_video', 0)}→{nachher.get('n_video', 0)}")
    soll = vorher.get("n_audio", 0) + (1 if plan["kodieren"] else 0)
    if nachher.get("n_audio", 0) != soll:
        probleme.append(f"Tonspuren {nachher.get('n_audio', 0)} statt {soll}")
    if nachher.get("n_sub", 0) != vorher.get("n_sub", 0):
        probleme.append(f"Untertitel {vorher.get('n_sub', 0)}→{nachher.get('n_sub', 0)}")
    if nachher.get("n_att", 0) != vorher.get("n_att", 0):
        probleme.append(f"Anhänge {vorher.get('n_att', 0)}→{nachher.get('n_att', 0)}")
    if nachher.get("n_cover", 0) != vorher.get("n_cover", 0):
        probleme.append(f"Cover-Bilder {vorher.get('n_cover', 0)}→{nachher.get('n_cover', 0)}")
    if probleme:
        return False, "Spuren passen nicht (" + ", ".join(probleme) + ")"
    din, dout = vorher.get("dur") or 0, nachher.get("dur") or 0
    if din > 0 and dout < din - 1.0:
        return False, f"Ausgabe zu kurz ({fmt_dur(dout)} statt {fmt_dur(din)})"
    ton = nachher.get("audio_streams") or []
    erste = ton[0] if ton else {}
    soll_codec = "ac3" if plan["kodieren"] else (plan["haupt"].get("codec") or "")
    if (erste.get("codec") or "") != soll_codec:
        return False, f"erste Tonspur ist {erste.get('codec') or '?'} statt {soll_codec}"
    standard = [s for s in ton if (s.get("disp") or {}).get("default")]
    if len(standard) != 1 or standard[0] is not erste:
        return False, "Standard-Markierung liegt nicht allein auf der ersten Tonspur"
    # Endet die neue Spur früher/später als ihre Vorlage (die kopiert dahinter liegt)?
    if plan["kodieren"] and len(ton) > 1 + plan["haupt_nr"]:
        d_neu, d_ref = erste.get("dauer"), ton[1 + plan["haupt_nr"]].get("dauer")
        if d_neu and d_ref and abs(d_neu - d_ref) > 0.5:
            return False, (f"neue Tonspur ist {fmt_dur(d_neu)} lang, die Vorlage "
                           f"{fmt_dur(d_ref)} – verworfen")
    return True, ""


def versatz_ms(ref, neu, block_ms=TV_SYNC_BLOCK_MS, max_ms=100.0):
    """Pure: Versatz zweier Lautstärke-Hüllkurven → (ms, Korrelation 0..1).

    Gesucht wird die Verschiebung L (in Blöcken), bei der ref[i+L] und neu[i] am
    besten übereinstimmen (Pearson auf der Überlappung). Stille/keine Varianz →
    (0.0, 0.0), die Stelle gilt dann als nicht bewertbar."""
    n = min(len(ref), len(neu))
    if n < 10:
        return 0.0, 0.0
    max_lag = int(round(max_ms / block_ms))
    best_lag, best_k = 0, 0.0
    for lag in range(-max_lag, max_lag + 1):
        a = ref[max(lag, 0):min(n + lag, n)]
        b = neu[max(-lag, 0):min(n, n - lag)]
        m = len(a)
        if m < 10:
            continue
        ma, mb = sum(a) / m, sum(b) / m
        sab = saa = sbb = 0.0
        for x, y in zip(a, b):
            dx, dy = x - ma, y - mb
            sab += dx * dy
            saa += dx * dx
            sbb += dy * dy
        if saa <= 0 or sbb <= 0:
            continue
        k = sab / math.sqrt(saa * sbb)
        if k > best_k:
            best_lag, best_k = lag, k
    return best_lag * block_ms, best_k


def sync_urteil(messungen, max_ms=TV_SYNC_MAX_MS, min_korr=TV_SYNC_MIN_KORR, min_stellen=2):
    """Pure: [(versatz_ms, korrelation, pegel_vorlage, pegel_neu, gemessen)] → (ok, Text).

    Jede auffällige Stelle führt zur Ablehnung – sie wird NICHT übergangen: Messung
    fehlgeschlagen, Vorlage laut aber neue Spur stumm (oder umgekehrt), beide laut
    aber schlecht übereinstimmend (Teil-Versatz!), oder Versatz > max_ms. Nur Stellen,
    an denen beide leise sind, zählen nicht; es müssen `min_stellen` bewertbar sein."""
    bewertet, groesster = 0, 0.0
    for nr, (lag, k, p_ref, p_neu, gemessen) in enumerate(messungen, 1):
        if not gemessen:
            return False, f"Stelle {nr}: Messung fehlgeschlagen – verworfen"
        laut_ref, laut_neu = p_ref > TV_SYNC_LAUT, p_neu > TV_SYNC_LAUT
        if (p_ref > 3 * TV_SYNC_LAUT and not laut_neu) or (p_neu > 3 * TV_SYNC_LAUT and not laut_ref):
            was = "neue Spur stumm" if laut_ref else "neue Spur laut, Vorlage still"
            return False, f"Stelle {nr}: {was} – verworfen"
        if not (laut_ref and laut_neu):
            continue
        if k < min_korr:
            return False, (f"Stelle {nr}: neue Spur passt nicht zur Vorlage "
                           f"(Übereinstimmung {k:.2f}) – verworfen")
        if abs(lag) > max_ms:
            return False, f"Ton weicht an Stelle {nr} um {abs(lag):.0f} ms vom Original ab – verworfen"
        bewertet += 1
        groesster = max(groesster, abs(lag))
    if bewertet < min_stellen:
        return False, (f"Synchronität nicht prüfbar (nur {bewertet} von {len(messungen)} "
                       f"Stellen auswertbar) – verworfen")
    return True, f"synchron (max. {groesster:.1f} ms an {bewertet} Stellen)"


def tv_huellkurve(pfad, audio_nr, start_s, dauer_s=TV_SYNC_SEKUNDEN):
    """Lautstärke-Hüllkurve (mittlerer Betrag je TV_SYNC_BLOCK_MS) einer Tonspur
    ab start_s → (Liste, mittlerer Pegel) oder (None, 0.0). Mono, 8 kHz – der
    Versatz, nicht der Klang zählt; so vergleichen sich DTS 7.1 und AC3 5.1 direkt."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start_s:.3f}", "-i", str(pfad),
           "-map", f"0:a:{audio_nr}", "-t", f"{dauer_s:.3f}", "-ac", "1", "-ar", str(TV_SYNC_RATE),
           "-f", "s16le", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
    except Exception:
        return None, 0.0
    if r.returncode != 0 or not r.stdout:
        return None, 0.0
    werte = array.array("h")
    werte.frombytes(r.stdout[:len(r.stdout) // 2 * 2])
    if sys.byteorder == "big":
        werte.byteswap()
    blk = max(1, int(TV_SYNC_RATE * TV_SYNC_BLOCK_MS / 1000))
    huelle = [sum(abs(v) for v in werte[i:i + blk]) / blk for i in range(0, len(werte) - blk + 1, blk)]
    if not huelle:
        return None, 0.0
    return huelle, sum(huelle) / len(huelle)


def tv_sync_stellen(dauer):
    """Pure: (Startzeiten, Fensterlänge, min_stellen). Nie direkt am Dateianfang –
    dort misst der Vergleich einen Schein-Versatz (Encoder-Vorlauf, ~27 ms)."""
    if dauer and dauer >= 60:
        return [dauer * f for f in TV_SYNC_STELLEN], TV_SYNC_SEKUNDEN, 2
    d = dauer or 0
    fenster = max(1.0, min(float(TV_SYNC_SEKUNDEN), d * 0.3))
    return [max(1.0, d * 0.2), max(1.0, d * 0.55)], fenster, 1


def tvton_sync_pruefen(ausgabe, dauer, plan, quelle):
    """Neue erste Tonspur gegen ihre Vorlage an mehreren Stellen vergleichen.
    Beim Kodieren steckt die Vorlage kopiert in derselben Ausgabe (gleiche
    Zeitbasis); beim Vorziehen wird gegen die Quelldatei verglichen."""
    if plan["kodieren"]:
        ref_datei, ref_nr = ausgabe, 1 + plan["haupt_nr"]
    else:
        ref_datei, ref_nr = quelle, plan["haupt_nr"]
    stellen, fenster, min_stellen = tv_sync_stellen(dauer)
    messungen = []
    for start in stellen:
        ref, p_ref = tv_huellkurve(ref_datei, ref_nr, start, fenster)
        neu, p_neu = tv_huellkurve(ausgabe, 0, start, fenster)
        if ref is None or neu is None:
            messungen.append((0.0, 0.0, 0.0, 0.0, False))
            continue
        lag, k = versatz_ms(ref, neu)
        messungen.append((lag, k, p_ref, p_neu, True))
    ok, text = sync_urteil(messungen, min_stellen=min_stellen)
    return ok, text, messungen


TV_DEKODIERFEHLER = re.compile(r"error while decoding|error decoding|decoding error|invalid frame", re.I)


def tv_dekodierfehler(stderr_text):
    """Pure: Zählt Dekodierfehler im ffmpeg-Protokoll. Lücken füllt aresample mit
    Stille – das wäre hörbar, deshalb wird ein solches Ergebnis verworfen."""
    return sum(1 for z in (stderr_text or "").splitlines() if TV_DEKODIERFEHLER.search(z))


def tv_platz_ok(ordner, groesse, reserve=5 * 1024 ** 3):
    """Die Ausgabe entsteht NEBEN dem Original und ist größer – vorher prüfen, dass
    sie samt Reserve passt, statt das Volume für alle anderen Dienste vollzuschreiben."""
    try:
        frei = shutil.disk_usage(ordner).free
    except OSError:
        return True, ""
    noetig = int(groesse * 1.1) + reserve
    if frei < noetig:
        return False, (f"Zu wenig Platz: {frei / 1e9:.1f} GB frei, {noetig / 1e9:.1f} GB nötig "
                       f"(Datei + 10 % + 5 GB Reserve)")
    return True, ""


def transfer_ownership(final, st):
    """Give the new file owner/group/mode/mtime of the source. Errors only logged."""
    try:
        os.chown(final, st.st_uid, st.st_gid)
    except Exception as e:
        print(f"[WARN] chown {final}: {e}", flush=True)
    try:
        os.chmod(final, stat.S_IMODE(st.st_mode))
    except Exception as e:
        print(f"[WARN] chmod {final}: {e}", flush=True)
    try:
        os.utime(final, (st.st_atime, st.st_mtime))
    except Exception as e:
        print(f"[WARN] utime {final}: {e}", flush=True)

# ─── Progress calculation (pure) ─────────────────────────────────────────────
def calc_progress(files, bytes_total, elapsed):
    """Byte-weighted progress. Returns dict {progress, eta, bytes_done,
    bytes_before_done, bytes_after_done, bytes_saved}."""
    bytes_done = 0.0
    before = after = 0
    for f in files:
        sb = f.get("size_before") or 0
        st_ = f.get("status")
        if st_ in ("done", "skipped", "error"):
            bytes_done += sb
        elif st_ == "converting":
            bytes_done += sb * min(max(f.get("progress") or 0, 0), 100) / 100.0
        # Ton aus Video: die Quelle bleibt, es wird nichts gespart – aus der Bilanz raus.
        if st_ in ("done", "skipped") and not f.get("quelle_bleibt"):
            before += sb
            after += f.get("size_after") or 0
    if bytes_total > 0:
        progress = round(min(bytes_done / bytes_total * 100, 100), 1)
    else:
        n = len(files)
        d = sum(1 for f in files if f.get("status") in ("done", "skipped", "error"))
        progress = round(d / n * 100, 1) if n else 0
    eta = 0
    if bytes_done > 0 and elapsed > 0 and bytes_total > bytes_done:
        rate = bytes_done / elapsed
        eta = int(round((bytes_total - bytes_done) / rate))
    return {"progress": progress, "eta": max(0, eta), "bytes_done": int(bytes_done),
            "bytes_before_done": before, "bytes_after_done": after, "bytes_saved": before - after}

def _recalc(t):
    """Recalculate total progress of task t. Must hold lock."""
    t.update(calc_progress(t["files"], t["bytes_total"], time.time() - t["t0"]))

# ─── Convert one file ────────────────────────────────────────────────────────
def convert_one(t, idx, filepath, target, is_video, quality):
    """Convert a single file with fallback chain. Updates task t directly."""
    src = Path(filepath)
    fi = t["files"][idx]
    container = t.get("container") or "mkv"

    if state.get("cancel"):
        return

    if not src.exists():
        with lock:
            fi["status"] = "error"
            fi["error"] = "Datei nicht gefunden"
            t["failed"] += 1
            _recalc(t)
        return

    info = get_info(filepath)
    if info is None:
        # Ohne gelesene Quelle gibt es keine Erwartungswerte – nicht konvertieren (S5)
        with lock:
            fi["status"] = "error"
            fi["error"] = "ffprobe konnte die Datei nicht lesen"
            t["failed"] += 1
            _recalc(t)
        return

    vc, ac, dur = info["vc"], info["ac"], info["dur"]
    is_tv = t.get("type") == "tvfix"
    plan = None
    # Ton aus Video: Ergebnis kommt neben das Video, das Video bleibt liegen und
    # zählt nicht als „ersetzt“ – weder beim Löschen noch in der Bilanz.
    herauslösen = ton_herausloesen(src, is_video)
    with lock:
        fi["streams_before"] = {"a": info["n_audio"], "s": info["n_sub"]}
        fi["quelle_bleibt"] = herauslösen

    skip_grund = None
    if is_tv:
        if src.suffix.lower() != ".mkv":
            skip_grund = "nur MKV"
        else:
            plan = tvton_plan(info.get("audio_streams"))
            if plan is None:
                skip_grund = "=" + tv_ton_beschreibung(info.get("audio_streams"))
    elif codec_matches(vc if is_video else ac, target, is_video) and not herauslösen:
        skip_grund = f"={vc or ac}"
    if skip_grund:
        with lock:
            fi["status"] = "skipped"
            fi["progress"] = 100
            fi["method"] = skip_grund
            fi["size_after"] = fi.get("size_before", 0)
            fi["streams_after"] = dict(fi["streams_before"])
            t["skipped"] += 1
            t["completed"] += 1
            _recalc(t)
        return

    # TV-Reparatur behält die Endung EXAKT (auch „.MKV“) – sonst wäre es für Plex
    # eine neue Datei (Gesehen-Status, Metadaten).
    ext = src.suffix if is_tv else out_ext(is_video, target, container)
    dst = Path(target_path(src, ext))
    inplace = str(dst) == str(src)

    # Zielpfad-Kollisionen abfangen, BEVOR etwas geschrieben wird (S3)
    blocker = (t.get("_collisions") or {}).get(str(src))
    if blocker:
        with lock:
            fi["status"] = "error"
            fi["error"] = f"Zielname doppelt belegt: {dst.name} gehört zu {blocker}"
            t["failed"] += 1
            _recalc(t)
        return
    if not inplace and dst.exists():
        with lock:
            fi["status"] = "error"
            fi["error"] = f"Zieldatei existiert bereits: {dst.name}"
            t["failed"] += 1
            _recalc(t)
        return

    # Fingerabdruck der Quelle VOR dem Encode. Ändert sie sich währenddessen (neu
    # heruntergeladen, ersetzt, angehängt), wird am Ende nicht ersetzt – sonst
    # überschriebe das Ergebnis der alten Fassung die neue. `st` dient danach auch
    # der Rechte-/Zeitübernahme.
    try:
        st = src.stat()
    except OSError as e:
        with lock:
            fi["status"] = "error"
            fi["error"] = f"Quelle nicht lesbar: {e}"
            t["failed"] += 1
            _recalc(t)
        return
    quelle_sig = quell_signatur(st)
    if is_tv:
        platz_ok, platz_err = tv_platz_ok(str(src.parent), st.st_size)
        if not platz_ok:
            with lock:
                fi["status"] = "error"
                fi["error"] = platz_err
                t["failed"] += 1
                _recalc(t)
            return

    # IMMER in eine .converting-Datei encodieren und erst nach der Prüfung
    # atomar ans Ziel schieben (S3/S4) – nie direkt auf dst schreiben.
    tmp = Path(tmp_path(src, ext))
    if tmp.exists():
        # Rest eines abgestürzten Laufs: ffmpeg würde ihn mit -y ohnehin überschreiben,
        # aber er soll auch dann verschwinden, wenn dieser Versuch scheitert (R1).
        print(f"[TMP] Rest entfernt: {tmp.name}", flush=True)
        try: tmp.unlink()
        except OSError: pass
    progfile = os.path.join(PROGRESS_DIR, f"p{t['id']}_{idx}.log")

    # Erwartungswert für die Verifikation: nur die tatsächlich gemappten Untertitel (S1)
    subs = info.get("sub_streams") or []
    # TV-Reparatur kopiert ALLE Untertitel unverändert (-map 0:s?), es fällt keiner weg.
    n_sub_mapped, sub_dropped = (info["n_sub"], []) if is_tv else subtitle_args(subs, container)[1:]
    before = {"v": info["n_video"], "a": info["n_audio"], "s": n_sub_mapped}
    with lock:
        fi["subs_dropped"] = len(sub_dropped)
        # Vorher/Nachher müssen dieselbe Zählweise benutzen: Untertitel, die MKV nicht
        # aufnehmen kann, werden bewusst nicht gemappt und dürfen in der Anzeige nicht
        # als "verloren" erscheinen. Wie viele es waren, steht in subs_dropped (R4).
        fi["streams_before"] = {"a": info["n_audio"], "s": n_sub_mapped}
    if sub_dropped:
        print(f"[SUBS] {src.name}: {len(sub_dropped)} Untertitel weggelassen "
              f"({', '.join(sorted(set(sub_dropped)))})", flush=True)

    with lock:
        fi["status"] = "converting"
        fi["progress"] = 0

    # Build attempt list
    attempts = []
    bilder = []
    if is_tv:
        bilder, bild_err = tv_bilder_herausloesen(src, info.get("covers") or [], tmp)
        if bild_err:
            for b in bilder:
                try: os.remove(b["pfad"])
                except OSError: pass
            with lock:
                fi["status"] = "error"
                fi["error"] = bild_err
                t["failed"] += 1
                _recalc(t)
            return
        attempts.append(("Remux", cmd_tvton(src, tmp, progfile, plan, bilder, info.get("n_att", 0))))
    elif is_video:
        if codec_gpu(target):
            attempts += gpu_attempts(src, tmp, target, progfile, quality, info, subs, GPU_CAPS, container)
        attempts.append(("CPU", cmd_cpu_video(src, tmp, target, progfile, quality, info["is10"], subs, info.get("audio_streams"), container)))
    elif herauslösen:
        spur = standard_tonspur(info.get("audio_streams"))
        if codec_matches(tonspur_codec(info), target, False):
            # Gleicher Codec: Spur unverändert übernehmen; scheitert der Muxer
            # (z. B. ADTS-Sonderfall), wird als Rückfall neu kodiert.
            attempts.append(("Kopie", cmd_audio(src, tmp, target, progfile, quality, kopie=True, spur=spur)))
        attempts.append(("CPU", cmd_audio(src, tmp, target, progfile, quality, spur=spur)))
    else:
        attempts.append(("CPU", cmd_audio(src, tmp, target, progfile, quality)))

    success = False
    verify_err = ""
    last_err = ""   # last ffmpeg stderr across all attempts
    t_file0 = time.time()
    for method, cmd in attempts:
        if state.get("cancel"):
            return

        # Reset progress file
        try:
            with open(progfile, "w") as pf:
                pf.write("")
        except OSError:
            pass

        with lock:
            fi["method"] = method
            fi["progress"] = 0
        print(f"[{method}] {src.name}", flush=True)

        errfile = os.path.join(PROGRESS_DIR, f"err_{t['id']}_{idx}.log")
        err_fh = None
        try:
            err_fh = open(errfile, "w")
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err_fh)

            # Store for cancel
            with lock:
                t["_procs"][idx] = proc

            t0 = time.time()

            # Poll loop
            while proc.poll() is None:
                if state.get("cancel"):
                    proc.kill()
                    proc.wait()
                    break

                pr = read_progress(progfile)
                us = pr["us"]
                if us > 0 and dur > 0:
                    pct = min(us / 1_000_000 / dur * 100, 99.9)
                    elapsed = time.time() - t0
                    eta = (elapsed / pct * (100 - pct)) if pct > 0.5 else 0
                    with lock:
                        fi["progress"] = round(pct, 1)
                        fi["eta"] = max(0, round(eta))
                        fi["speed"] = pr["speed"]
                        fi["fps"] = pr["fps"]
                        _recalc(t)

                time.sleep(0.8)

            err_fh.close()
            err_fh = None
            with lock:
                t["_procs"].pop(idx, None)

            # Cleanup progress file
            try: os.remove(progfile)
            except OSError: pass

            if state.get("cancel"):
                try: tmp.unlink()
                except OSError: pass
                try: os.remove(errfile)
                except OSError: pass
                return

            if proc.returncode == 0:
                # Verify tracks + duration + size before touching the source
                out = info_from_probe(probe(tmp))
                after = {"v": out["n_video"], "a": out["n_audio"], "s": out["n_sub"],
                         "size": _filesize(tmp)}
                if is_tv:
                    ok, verify_err = tvton_spuren_ok(info, out, plan)
                    try:
                        with open(errfile, "r", errors="replace") as ef:
                            n_fehler = tv_dekodierfehler(ef.read())
                    except OSError:
                        n_fehler = 0
                    if ok and n_fehler:
                        ok, verify_err = False, (f"{n_fehler} Dekodierfehler in der Vorlage – die "
                                                 f"Lücken wären hörbar, verworfen")
                    if ok:
                        # Lippensynchron? Inhalt der neuen Spur gegen die Vorlage.
                        with lock:
                            fi["method"] = "Sync-Prüfung"
                        ok, text, messungen = tvton_sync_pruefen(tmp, dur, plan, src)
                        print(f"[SYNC] {src.name}: {text} "
                              f"{[(round(l, 1), round(k, 2), round(pr), round(pn)) for l, k, pr, pn, _ in messungen]}",
                              flush=True)
                        with lock:
                            fi["method"] = method
                            fi["sync"] = text
                        verify_err = "" if ok else text
                else:
                    ok, verify_err = streams_ok(before, after, dur, out["dur"], is_video)
                try: os.remove(errfile)
                except OSError: pass
                if ok:
                    success = True
                    with lock:
                        fi["streams_after"] = {"a": after["a"], "s": after["s"]}
                    print(f"[OK] {method} {src.name}", flush=True)
                else:
                    print(f"[VERIFY-FAIL] {method} {src.name}: {verify_err}", flush=True)
                    try:
                        if tmp.exists(): tmp.unlink()
                    except OSError: pass
                # No further fallback after a verification failure (mapping would be identical)
                break
            else:
                try:
                    with open(errfile, "r") as ef:
                        last_err = ef.read()[-1000:]
                except OSError: pass
                print(f"[FAIL] {method} rc={proc.returncode} {src.name}", flush=True)
                print(f"  stderr: {last_err[:200]}", flush=True)
                try:
                    if tmp.exists(): tmp.unlink()
                except OSError: pass
                try: os.remove(errfile)
                except OSError: pass
                # Continue to next attempt

        except Exception as e:
            print(f"[ERR] {method} {src.name}: {e}", flush=True)
            last_err = f"Fehler bei {method}: {e}"
            try:
                if err_fh: err_fh.close()
            except OSError: pass
            with lock:
                t["_procs"].pop(idx, None)
            try:
                if tmp.exists(): tmp.unlink()
            except OSError: pass
            # auch hier keine Leichen in /tmp/mc_progress hinterlassen (R3)
            for junk in (progfile, errfile):
                try: os.remove(junk)
                except OSError: pass

    for b in bilder:                      # herausgelöste Cover wieder wegräumen
        try: os.remove(b["pfad"])
        except OSError: pass
    if state.get("cancel"):
        # Abbruch WÄHREND der (langen) Prüfung: Original bleibt, wie der
        # Abbrechen-Dialog verspricht – nichts ersetzen.
        try:
            if tmp.exists(): tmp.unlink()
        except OSError: pass
        return

    if success:
        # Erst jetzt die Quelle anfassen. In-place: os.replace ist atomar, es gibt
        # keinen Moment ohne Datei. Nicht in-place: Hardlink statt replace, damit
        # eine Zieldatei, die WÄHREND des Encodes entstanden ist, nie überschrieben
        # wird. Vorher: hat sich die Quelle verändert, wird gar nichts ersetzt.
        # Scheitert es, bleibt die Quelle liegen und der Versuch gilt als Fehler –
        # kein History-Eintrag, keine gemeldete Ersparnis (S4).
        final_path = dst
        try:
            if quell_signatur(src.stat()) != quelle_sig:
                raise ErsetzenAbgelehnt("Quelle hat sich während der Konvertierung geändert – nicht ersetzt")
            if inplace:
                os.replace(str(tmp), str(dst))
            else:
                ziel_anlegen_ohne_ueberschreiben(str(tmp), str(dst))
        except Exception as e:
            success = False
            verify_err = str(e) if isinstance(e, ErsetzenAbgelehnt) else f"Ersetzen fehlgeschlagen: {e}"
            print(f"[ERR] replace {src.name}: {e}", flush=True)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        else:
            # Ab hier liegt das geprüfte Ergebnis am Ziel. Ein Fehler beim Aufräumen der
            # alten Quelle oder beim Übernehmen der Rechte darf den Erfolg NICHT mehr
            # zurücknehmen – sonst stünde eine korrekt konvertierte Datei als Fehler da
            # und der Verlauf verlöre den Eintrag (R2).
            if not inplace and not herauslösen and src.exists():
                try:
                    src.unlink()
                except OSError as e:
                    print(f"[WARN] Quelle blieb liegen {src}: {e}", flush=True)
            transfer_ownership(final_path, st)

    if success:
        duration_s = time.time() - t_file0
        with lock:
            fi["status"] = "done"
            fi["progress"] = 100
            fi["eta"] = 0
            fi["size_after"] = _filesize(final_path)
            t["completed"] += 1
            _recalc(t)
            speed = fi.get("speed") or 0
        append_history({
            "run_id": t["id"],
            "name": fi["name"],
            "path": str(final_path),
            "src": fi.get("src", ""),
            "rel_dir": fi.get("rel_dir", ""),
            # Ton aus Video: das Video bleibt – die Videogröße als „vorher“ wäre eine
            # Schein-Ersparnis. Der Eintrag beschreibt nur die neue Tondatei.
            "size_before": fi["size_after"] if herauslösen else fi.get("size_before", 0),
            "size_after": fi["size_after"],
            "quelle_bleibt": herauslösen,
            "method": fi.get("method", ""),
            "target": target,
            "quality": quality,
            "timestamp": time.time(),
            "duration_s": round(duration_s, 1),
            "speed": speed,
            **({"sync": fi.get("sync", "")} if is_tv else {}),
        })
    else:
        err_summary = verify_err or "Alle Methoden fehlgeschlagen"
        if not verify_err and last_err:
            # Extract the most useful line from ffmpeg stderr
            useful = [l.strip() for l in last_err.splitlines()
                      if any(k in l for k in ("Error", "error", "Invalid", "No such",
                                               "Permission", "failed", "Could not",
                                               "moov atom", "codec", "Unrecognized"))]
            if useful:
                err_summary = useful[-1][:200]
            elif last_err.strip():
                err_summary = last_err.strip().splitlines()[-1][:200]
        with lock:
            fi["status"] = "error"
            fi["error"] = err_summary
            fi["progress"] = 0
            fi["streams_after"] = None
            t["failed"] += 1
            _recalc(t)

# ─── Task runner / queue worker ──────────────────────────────────────────────
def run_task(t):
    """Runs one task (called by the queue worker). Uses ThreadPoolExecutor for 2 workers."""
    is_video = t["type"] == "video"
    ext = ".mkv" if t["type"] == "tvfix" else out_ext(is_video, t["target"], t.get("container") or "mkv")
    # Zwei Quellen desselben Jobs mit gleichem Zielnamen vorab erkennen (S3c)
    # TV-Reparatur schreibt jede Datei an ihren eigenen Platz zurück – keine Kollisionen.
    t["_collisions"] = {} if t["type"] == "tvfix" else find_target_collisions([f["path"] for f in t["files"]], ext)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(convert_one, t, idx, f["path"], t["target"], is_video, t["quality"])
                   for idx, f in enumerate(t["files"])]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"[ERR] Worker exception: {e}", flush=True)

    with lock:
        if state.get("cancel"):
            t["status"] = "cancelled"
            for fi in t["files"]:
                if fi["status"] in ("pending", "converting"):
                    fi["status"] = "cancelled"
        else:
            t["status"] = "done"
            t["progress"] = 100
        t["eta"] = 0
        t["finished_at"] = time.time()
    # Datei-I/O bewusst AUSSERHALB des globalen Locks: ein hängender /config-Mount
    # darf weder /api/status noch SSE noch Abbrechen blockieren (S13).
    try:
        update_cumulative_stats(t)
    except Exception as e:
        print(f"[WARN] stats: {e}", flush=True)
    try:
        record_run(t)
    except Exception as e:
        print(f"[WARN] runs: {e}", flush=True)

    print(f"[DONE] {t['id']} completed={t['completed']} skipped={t['skipped']} failed={t['failed']}", flush=True)

def queue_worker():
    while True:
        with queue_cv:
            while not state["queue"]:
                queue_cv.wait()
            t = state["queue"].pop(0)
            t["status"] = "running"
            t["started_at"] = time.time()
            t["t0"] = t["started_at"]
            state["current"] = t
            state["cancel"] = False
        try:
            run_task(t)
        except Exception as e:
            print(f"[ERR] run_task: {e}", flush=True)
            with lock:
                t["status"] = "cancelled" if state.get("cancel") else "done"
                t["finished_at"] = time.time()
        with queue_cv:
            # Wurde der Job per /api/clear ausgeblendet, darf er nicht als `last`
            # zurückkommen, nur weil der Worker noch lief (S12).
            discarded = state.get("discard_id") == t["id"]
            state["last"] = None if discarded else t
            if discarded:
                state["discard_id"] = None
            state["current"] = None
            state["cancel"] = False
            queue_cv.notify_all()

def _kill_current_locked():
    """Must hold lock: set cancel flag and kill running ffmpeg processes."""
    state["cancel"] = True
    cur = state["current"]
    if cur:
        for p in list(cur.get("_procs", {}).values()):
            try: p.kill()
            except Exception: pass

STOP_WARTE_S = 8          # `docker stop` schickt nach 10 s SIGKILL – vorher fertig sein
_START_TS = time.time()   # Reste, die älter sind, gehören keinem laufenden Encode

def laufenden_auftrag_abbrechen(warte_s=STOP_WARTE_S):
    """Beim Herunterfahren (SIGTERM = `docker stop`, Update): laufende ffmpeg-Prozesse
    töten und warten, bis der Worker den Lauf als abgebrochen verbucht hat – er
    schreibt dabei wie immer update_cumulative_stats/record_run. Ohne das starb
    server.py als PID 1 sofort, ffmpeg mit ihm, und der halbe Lauf fehlte in der
    Bilanz. True = Lauf verbucht (oder es lief nichts), False = Zeit abgelaufen."""
    with queue_cv:
        cur = state["current"]
        if cur is None:
            return True
        tid = cur["id"]
        _kill_current_locked()
        deadline = time.time() + warte_s
        while state["current"] is not None and state["current"]["id"] == tid:
            rest = deadline - time.time()
            if rest <= 0:
                return False
            queue_cv.wait(rest)
    return True

def _sigterm(signum, frame):
    """Signal-Handler (nur im Hauptthread, nur unter __main__ registriert). SystemExit
    lässt waitress sauber schließen."""
    print("[STOP] SIGTERM – laufenden Auftrag abbrechen und beenden", flush=True)
    if laufenden_auftrag_abbrechen():
        print("[STOP] Lauf verbucht, beende", flush=True)
    else:
        print(f"[STOP] Lauf nach {STOP_WARTE_S} s nicht verbucht – beende trotzdem", flush=True)
    raise SystemExit(0)

def converting_reste_aufraeumen(root=None, aelter_als=None):
    """Beim Start: liegen gebliebene X.converting.<ext> unter MEDIA_ROOT löschen –
    Reste eines Absturzes oder SIGKILL. Nur Dateien, die älter als der Serverstart
    sind: eine, die gerade ein neuer Encode schreibt, hat eine jüngere mtime.
    Symlinks werden nicht verfolgt, Systemordner auf der Wurzel und Punkt-Ordner
    übersprungen (wie im Ordner-Browser). Rückgabe: Liste der entfernten Pfade."""
    root = root or MEDIA_ROOT
    grenze = _START_TS if aelter_als is None else aelter_als
    weg = []
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath == root:
            dirnames[:] = [d for d in dirnames if d not in SYSTEM_DIRS]
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = os.path.join(dirpath, name)
            if not is_temp_name(p) or Path(p).suffix.lower() not in (VID_EXT | AUD_EXT):
                continue
            try:
                if os.path.islink(p) or os.stat(p).st_mtime >= grenze:
                    continue
                os.remove(p)
                weg.append(p)
                print(f"[TMP] Rest nach Neustart entfernt: {p}", flush=True)
            except OSError as e:
                print(f"[WARN] Rest nicht löschbar {p}: {e}", flush=True)
    if weg:
        print(f"[TMP] {len(weg)} .converting-Rest(e) entfernt", flush=True)
    return weg

def _start_aufraeumen():
    """Im Hintergrund, damit ein großer Medienbaum den Start nicht verzögert."""
    try:
        converting_reste_aufraeumen()
    except Exception as e:
        print(f"[WARN] Aufräumen der .converting-Reste: {e}", flush=True)

_worker_thread = threading.Thread(target=queue_worker, name="queue_worker", daemon=True)
_worker_thread.start()

# ─── Prescan (asynchron) ─────────────────────────────────────────────────────
prescan_lock = threading.Lock()
prescans = {}   # scan_id -> {status, done, total, result, error, created}

def _prescan_gc():
    """Must hold prescan_lock: drop results older than PRESCAN_KEEP_S.
    Gemessen wird ab ENDE der Analyse – sonst ist das Ergebnis einer langen Analyse
    schon beim ersten Abruf verfallen (S8)."""
    cutoff = time.time() - PRESCAN_KEEP_S
    for k in [k for k, v in prescans.items()
              if v["status"] != "running" and v.get("finished", v["created"]) < cutoff]:
        prescans.pop(k, None)

def tv_ton_beschreibung(audio_streams):
    """Pure: Kurztext der Haupttonspur für Analyse/Anzeige, z. B. "dts 6ch"."""
    plan = tvton_plan(audio_streams)
    s = plan["haupt"] if plan else next(iter(audio_streams or []), None)
    if not s:
        return "kein Ton"
    return f"{s.get('codec') or '?'} {s.get('channels') or '?'}ch"


def prescan_check(f, folder, target, is_video, ctype=None):
    """Runs in a worker thread: corrupt-check + single ffprobe call per file.
    `folder` ist der jeweilige Quellordner – `rel_dir` bezieht sich immer auf ihn.
    ctype "tvfix": entschieden wird nach der Haupttonspur, nicht nach dem Codec."""
    name = os.path.basename(f)
    item = {"name": name, "path": f, "src": folder_label(folder),
            "rel_dir": rel_dir_of(f, folder), "codec": "?", "size": _filesize(f)}
    try:
        with open(f, "rb") as fh:
            header = fh.read(4)
        if header == b"\x00\x00\x00\x00" or len(header) == 0:
            return ("corrupt", item)
    except Exception:
        item["codec"] = "error"
        return ("corrupt", item)
    info = get_info(f)
    if info is None:
        # ffprobe kann die Datei nicht lesen → nicht zur Konvertierung anbieten (S5)
        item["codec"] = "error"
        return ("corrupt", item)
    if ctype == "tvfix":
        if Path(f).suffix.lower() != ".mkv":
            item["codec"] = "nur MKV"
            return ("skip", item)
        item["codec"] = tv_ton_beschreibung(info.get("audio_streams"))
        return ("skip", item) if tvton_plan(info.get("audio_streams")) is None else ("convert", item)
    # Ein Video ist nie „schon im Zielformat“ – der Ton soll ja herausgelöst werden;
    # angezeigt wird der Codec der Spur, die dabei genommen wird.
    if ton_herausloesen(f, is_video):
        item["codec"] = tonspur_codec(info) or "?"
        return ("convert", item)
    current_codec = info["vc"] if is_video else info["ac"]
    item["codec"] = current_codec or "?"
    if codec_matches(current_codec, target, is_video):
        return ("skip", item)
    return ("convert", item)

def run_prescan(scan_id, paare, target, is_video, uebersicht=None, ctype=None):
    """`paare` = [(pfad, quellordner)] aus `dateien_sammeln` – jede Datei kennt so
    ihren eigenen Quellordner (A4)."""
    ps = prescans[scan_id]
    to_convert, to_skip, corrupt = [], [], []
    try:
        with ThreadPoolExecutor(max_workers=min(16, max(4, WORKERS * 4))) as pool:
            for kind, item in pool.map(lambda p: prescan_check(p[0], p[1], target, is_video, ctype), paare):
                if kind == "corrupt":
                    corrupt.append(item)
                elif kind == "skip":
                    to_skip.append(item)
                else:
                    to_convert.append(item)
                with prescan_lock:
                    ps["done"] += 1
        with prescan_lock:
            ps["result"] = {
                "to_convert": to_convert, "to_skip": to_skip, "corrupt": corrupt,
                "total": len(paare),
                "bytes_convert": sum(i["size"] for i in to_convert),
                "bytes_skip": sum(i["size"] for i in to_skip),
                "ordner": uebersicht or [],
            }
            ps["status"] = "done"
            ps["finished"] = time.time()
    except Exception as e:
        with prescan_lock:
            ps["status"] = "error"
            ps["error"] = str(e)
            ps["finished"] = time.time()

# ─── Auth ────────────────────────────────────────────────────────────────────
def auth(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("ok"):
            return jsonify({"error":"Unauthorized"}), 401
        return f(*a, **kw)
    return w

def _safe_media_path(rel):
    """Join rel to MEDIA_ROOT and verify it stays inside. Returns real path or None.

    `realpath` wirft bei einem Nullbyte im Pfad `ValueError` – ein einzelner fauler
    Eintrag darf den ganzen Aufruf (bis zu 50 Ordner) nicht mit 500 abreissen."""
    try:
        full = os.path.join(MEDIA_ROOT, rel or "")
        real = os.path.realpath(full)
        root = os.path.realpath(MEDIA_ROOT)
    except (ValueError, OSError, TypeError):
        return None
    if real != root and not real.startswith(root.rstrip(os.sep) + os.sep):
        return None
    if MC_ROOTS and real != root:
        erster = os.path.relpath(real, root).split(os.sep, 1)[0]
        if erster not in MC_ROOTS:
            return None
    return real

def is_media_root(real):
    """True, wenn der Pfad die gemountete Wurzel selbst ist. Scannen/Konvertieren auf
    der Wurzel würde das ganze NAS durchlaufen – nur der Browser darf sie listen (S9)."""
    return os.path.realpath(real) == os.path.realpath(MEDIA_ROOT)

ROOT_ERR = {"error": "Bitte einen Unterordner wählen"}

# ─── Ordner-Auswahl (Sammelauftrag über mehrere Ordner) ──────────────────────
LIMIT_ERR = f"Höchstens {MAX_ORDNER} Ordner pro Auftrag"
KEIN_ORDNER_ERR = "Kein gültiger Ordner ausgewählt"
PFAD_ERR = "Ungültiger Pfad"
NICHT_GEFUNDEN_ERR = "Ordner nicht gefunden"
KEIN_VERZEICHNIS_ERR = "Kein Ordner"


# Zu jedem Grund eine Handlungsanweisung (B7) – sonst steht der Nutzer vor einer Sackgasse.
HINWEISE = {
    NICHT_GEFUNDEN_ERR: "prüfe, ob das Volume noch eingebunden ist",
    KEIN_VERZEICHNIS_ERR: "das ist eine Datei – wähle den Ordner darüber",
    PFAD_ERR: "wähle den Ordner über den Browser aus",
}


def mit_hinweis(grund):
    """Pure: Grund → Grund + Handlungsanweisung (falls es eine gibt)."""
    hinweis = HINWEISE.get(grund)
    return f"{grund} – {hinweis}" if hinweis else grund


class OrdnerAuswahlFehler(ValueError):
    """Die Auswahl als Ganzes ist unbrauchbar (zu viele Einträge) – der Aufruf endet mit 400."""


def _kurz(wert, n=120):
    """Rohtext des Clients für eine Fehlermeldung kürzen. Der Wert geht nur in die
    Antwort dieses einen Aufrufs, nie in Task/Verlauf (I1b)."""
    s = wert if isinstance(wert, str) else repr(wert)
    return s[:n]


def pruefe_ordner(eintrag):
    """Pure: ein Rohpfad → (geprüfter Realpfad, Grund). Genau eines von beiden ist None."""
    if not isinstance(eintrag, str):
        return None, PFAD_ERR
    real = _safe_media_path(eintrag)
    if real is None:
        return None, PFAD_ERR
    if is_media_root(real):
        return None, ROOT_ERR["error"]
    if not os.path.exists(real):
        return None, NICHT_GEFUNDEN_ERR
    if not os.path.isdir(real):
        return None, KEIN_VERZEICHNIS_ERR
    return real, None


def _liegt_unter(pfad, ordner):
    """Pure: liegt `pfad` echt unterhalb von `ordner`?"""
    return pfad.startswith(ordner.rstrip(os.sep) + os.sep)


def sammle_ordner(rohliste):
    """Pure (bis auf die Dateisystem-Prüfung): Rohliste → (ordner, fehler).

    `ordner` sind geprüfte Realpfade, sortiert und **entschachtelt**: sind A und A/B
    gewählt, bleibt nur A – sonst liefen im selben Auftrag zwei Quellen auf dieselbe
    Datei. Fehlerhafte Einzeleinträge scheitern nicht den ganzen Aufruf, sie landen
    als Klartext in `fehler`.
    """
    if rohliste is None:
        rohliste = []
    elif isinstance(rohliste, str) or not isinstance(rohliste, (list, tuple)):
        rohliste = [rohliste]
    if len(rohliste) > MAX_ORDNER:
        raise OrdnerAuswahlFehler(LIMIT_ERR)
    gueltig, fehler, gesehen = [], [], set()
    for eintrag in rohliste:
        real, grund = pruefe_ordner(eintrag)
        if grund:
            hinweis = HINWEISE.get(grund)
            fehler.append(f"{grund}: {_kurz(eintrag)}" + (f" – {hinweis}" if hinweis else ""))
            continue
        if real in gesehen:
            continue          # Duplikat (auch anders geschrieben) – stillschweigend
        gesehen.add(real)
        gueltig.append(real)
    ordner = [p for p in gueltig if not any(_liegt_unter(p, q) for q in gueltig if q != p)]
    return sorted(ordner), fehler


def ordner_eingabe(d):
    """Pure: Request-Body → Rohliste. `folders` (neu) gewinnt vor `folder` (bisher)."""
    if not isinstance(d, dict):
        return [""]
    roh = d.get("folders")
    if isinstance(roh, (list, tuple)):
        return list(roh)
    if isinstance(roh, str):
        return [roh]
    return [d.get("folder", "")]


def dateien_sammeln(ordner_liste, exts=None):
    """[(pfad, quellordner)] – jeder Ordner rekursiv, Reihenfolge: Ordner wie übergeben,
    darin sortiert. Über `os.path.realpath` dedupliziert, weil ein Symlink dieselbe
    Datei in zwei gewählten Ordnern auftauchen lassen kann."""
    if exts is None:
        exts = VID_EXT | AUD_EXT
    treffer, gesehen = [], set()
    wurzel = os.path.realpath(MEDIA_ROOT)
    for ordner in ordner_liste:
        for f in scan(ordner):
            if Path(f).suffix.lower() not in exts:
                continue
            try:
                echt = os.path.realpath(f)
            except (ValueError, OSError):
                continue
            # Ein Datei-Symlink kann aus der Wurzel zeigen – die Zieldatei ersetzt die
            # Quelle, also würde ffmpeg als root ausserhalb von /media schreiben.
            if echt != wurzel and not echt.startswith(wurzel.rstrip(os.sep) + os.sep):
                continue
            if echt in gesehen:
                continue
            gesehen.add(echt)
            treffer.append((f, ordner))
    return treffer


def quellordner_von(pfad, ordner_liste):
    """Pure: der gewählte Ordner, unter dem `pfad` liegt (längste Übereinstimmung), sonst None."""
    treffer = None
    for o in ordner_liste:
        if _liegt_unter(pfad, o) and (treffer is None or len(o) > len(treffer)):
            treffer = o
    return treffer


def dateien_aus_auswahl(rohdateien, ordner_liste, exts):
    """Client-Dateiliste → [(pfad, quellordner)].

    Jede Datei muss unterhalb eines GEWÄHLTEN Ordners liegen (damit hat sie einen
    Quellordner und ffmpeg als root sieht nichts anderes), Endungsfilter,
    `.converting`-Ausschluss und Nullbyte-Prüfung wie bisher (S6/R1)."""
    treffer, gesehen = [], set()
    for f in rohdateien:
        if not isinstance(f, str):
            continue
        try:
            rf = os.path.realpath(f)
        except (ValueError, OSError):          # Nullbyte im Pfad → realpath wirft (S6)
            continue
        if rf in gesehen:
            continue
        quelle = quellordner_von(rf, ordner_liste)
        if quelle is None or not os.path.isfile(rf):
            continue
        if os.path.splitext(rf)[1].lower() not in exts or is_temp_name(rf):
            continue
        try:
            with open(rf, "rb") as fh:
                h = fh.read(4)
            if h == b"\x00\x00\x00\x00" or len(h) == 0:
                continue
        except OSError:
            continue
        gesehen.add(rf)
        treffer.append((rf, quelle))
    return treffer


def ordner_uebersicht(ordner_liste, paare):
    """Pure bis auf die Dateigrößen: Aufschlüsselung je Quellordner (leere Ordner
    erscheinen mit Nullen), Reihenfolge wie `ordner_liste`."""
    per = {o: {"folder": folder_label(o), "video": 0, "audio": 0, "total": 0, "bytes": 0}
           for o in ordner_liste}
    for pfad, quelle in paare:
        e = per.get(quelle)
        if e is None:
            continue
        suffix = Path(pfad).suffix.lower()
        if suffix in VID_EXT:
            e["video"] += 1
        elif suffix in AUD_EXT:
            e["audio"] += 1
        e["total"] += 1
        e["bytes"] += _filesize(pfad)
    return [per[o] for o in ordner_liste]


def ordner_anzeige(labels):
    """Pure: Anzeigename des Auftrags. Ein Ordner wie bisher, mehrere als
    "<erster> + N weitere" – damit brauchen Warteschlange und Verlauf keinen Sonderfall."""
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    return f"{labels[0]} + {len(labels) - 1} weitere"


def keine_dateien_err(n_ordner):
    return ("Keine passenden Dateien in diesem Ordner" if n_ordner <= 1
            else "Keine passenden Dateien in den gewählten Ordnern")


def auswahl_aus_request(d):
    """(ordner, fehler, antwort). `antwort` ist gesetzt, wenn der Aufruf mit 400 endet:
    kein einziger gültiger Ordner. Ein einzelner Eintrag behält dabei seine bisherige,
    genaue Meldung – der Einzelordner-Weg bleibt unverändert."""
    roh = ordner_eingabe(d)
    try:
        ordner, fehler = sammle_ordner(roh)
    except OrdnerAuswahlFehler as e:
        return [], [], (jsonify({"error": str(e)}), 400)
    if not ordner:
        meldung = KEIN_ORDNER_ERR
        if len(roh) == 1:
            meldung = mit_hinweis(pruefe_ordner(roh[0])[1] or KEIN_ORDNER_ERR)
        return [], fehler, (jsonify({"error": meldung, "fehler": fehler}), 400)
    return ordner, fehler, None


# ─── Routes ──────────────────────────────────────────────────────────────────
def _seite(name):
    """Eine Seite ausliefern, ohne dass der Browser sie zwischenspeichert.

    Die Oberfläche steckt komplett in diesen beiden Dateien. Ohne diesen Header
    zeigt ein Browser nach einem Update tagelang die alte Seite und redet mit
    einem neueren Server – genau das ist nach der Umstellung passiert.
    ETag und Last-Modified bleiben erhalten, ein unveränderter Stand kostet also
    weiterhin nur eine 304-Antwort.
    """
    antwort = send_from_directory("static", name, max_age=0)
    antwort.headers["Cache-Control"] = "no-cache, must-revalidate"
    return antwort

@app.route("/")
def index():
    return _seite("index.html")

@app.route("/history")
def history_page():
    return _seite("history.html")

@app.route("/api/login", methods=["POST"])
def login():
    key = client_key()
    wait = login_guard.retry_after(key)
    if wait > 0:
        return jsonify({"error": f"Zu viele Versuche – warte {wait} s", "retry_after": wait}), 429
    d = request.get_json(silent=True) if request.is_json else None
    if not isinstance(d, dict):
        d = {}
    username = str(d.get("username") or "")
    password = str(d.get("password") or "")
    with auth_lock:
        user_ok = hmac.compare_digest(username.encode(), AUTH_USER.encode())
        try:
            pw_ok = bcrypt.checkpw(password.encode(), PW_HASH)   # always run (timing)
        except ValueError:
            pw_ok = False
    if user_ok and pw_ok:
        login_guard.success(key)
        session.permanent = True
        session["ok"] = True
        return jsonify({"ok":True})
    login_guard.fail(key)
    print(f"[AUTH] fail ip={key} user={username[:32]!r}", flush=True)
    wait = login_guard.retry_after(key)
    if wait > 0:
        return jsonify({"error": f"Zu viele Versuche – warte {wait} s", "retry_after": wait}), 429
    return jsonify({"error":"Benutzername oder Passwort falsch"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok":True})

@app.route("/api/auth-check")
def auth_check():
    return jsonify({"authenticated": bool(session.get("ok"))})

@app.route("/api/folders")
@auth
def folders():
    """List subfolders of a given path (relative to MEDIA_ROOT). Supports browsing any depth."""
    rel = request.args.get("path", "").strip("/")
    real = _safe_media_path(rel)
    if real is None:
        return jsonify({"error":"Ungültiger Pfad"}), 400
    root = Path(real)
    if not root.exists() or not root.is_dir():
        return jsonify({"folders":[], "system":[], "current": folder_label(real), "media_count": 0, "media_bytes": 0})
    names, media_here, media_bytes = [], 0, 0
    try:
        for d in root.iterdir():
            try:
                if d.is_dir():
                    names.append(d.name)
                elif d.is_file() and is_media_file(d):
                    media_here += 1
                    media_bytes += _filesize(d)
            except OSError:
                continue
    except OSError as e:
        return jsonify({"error": f"Ordner nicht lesbar: {e}"}), 400
    dirs, system = split_dirs(names, rel == "")
    return jsonify({"folders": dirs, "system": system, "current": folder_label(real),
                    "media_count": media_here, "media_bytes": media_bytes})

@app.route("/api/scan", methods=["POST"])
@auth
def scan_folder():
    """Summen über alle gewählten Ordner plus Aufschlüsselung je Ordner."""
    d = request.get_json(silent=True) or {}
    ordner, fehler, antwort = auswahl_aus_request(d)
    if antwort:
        return antwort
    uebersicht = ordner_uebersicht(ordner, dateien_sammeln(ordner))
    return jsonify({"video_count": sum(e["video"] for e in uebersicht),
                    "audio_count": sum(e["audio"] for e in uebersicht),
                    "total": sum(e["total"] for e in uebersicht),
                    "bytes": sum(e["bytes"] for e in uebersicht),
                    "ordner": uebersicht, "fehler": fehler})

@app.route("/api/codecs")
@auth
def codecs():
    return jsonify({
        "video": {k: {"label": v["label"], "gpu": codec_gpu(k), "hint": v["hint"], "saving": v["saving"],
                      "containers": v["containers"]}
                  for k, v in VID_CODECS.items()},
        "audio": {k: {"label": v["label"], "hint": v["hint"], "ext": v["ext"]}
                  for k, v in AUD_CODECS.items()},
        "tvfix": {k: {"label": v["label"], "hint": v["hint"], "ext": v["ext"], "saving": v["saving"]}
                  for k, v in TVFIX.items()},
        "containers": {k: {"label": v["label"], "ext": v["ext"], "hint": v["hint"]}
                       for k, v in CONTAINERS.items()},
        "gpu": GPU,
        "gpu_caps": GPU_CAPS,
        "qualities": QUALITIES,
    })

@app.route("/api/prescan", methods=["POST"])
@auth
def prescan_start():
    """Start background scan: which files already match the target codec."""
    d = request.get_json(silent=True) or {}
    target = d.get("codec", "")
    ctype = d.get("type", "video")

    is_video = ctype == "video"
    is_tv = ctype == "tvfix"
    if target not in (TVFIX if is_tv else VID_CODECS if is_video else AUD_CODECS):
        return jsonify({"error": "Unbekannter Codec"}), 400
    ordner, fehler, antwort = auswahl_aus_request(d)
    if antwort:
        return antwort

    exts = quell_endungen(ctype)
    paare = dateien_sammeln(ordner, exts)
    if not paare:
        return jsonify({"error": keine_dateien_err(len(ordner))}), 404
    uebersicht = ordner_uebersicht(ordner, paare)

    with prescan_lock:
        _prescan_gc()
        if any(p["status"] == "running" for p in prescans.values()):
            return jsonify({"error": "Es läuft bereits eine Analyse"}), 409
        scan_id = uuid.uuid4().hex[:8]
        prescans[scan_id] = {"status": "running", "done": 0, "total": len(paare),
                             "result": None, "error": None,
                             "created": time.time(), "finished": None}
    threading.Thread(target=run_prescan, args=(scan_id, paare, target, is_video, uebersicht,
                                               ctype if is_tv else None),
                     daemon=True).start()
    return jsonify({"scan_id": scan_id, "total": len(paare), "fehler": fehler}), 202

@app.route("/api/prescan/<scan_id>")
@auth
def prescan_status(scan_id):
    with prescan_lock:
        _prescan_gc()
        ps = prescans.get(scan_id)
        if not ps:
            return jsonify({"error": "Analyse nicht gefunden (abgelaufen?)"}), 404
        return jsonify({"status": ps["status"], "done": ps["done"], "total": ps["total"],
                        "result": ps["result"], "error": ps["error"]})

@app.route("/api/convert", methods=["POST"])
@auth
def convert():
    d = request.get_json(silent=True) or {}
    target = d.get("codec","")
    ctype = d.get("type","video")
    quality = d.get("quality", "balanced") or "balanced"

    is_video = ctype == "video"
    is_tv = ctype == "tvfix"
    codecs_map = TVFIX if is_tv else VID_CODECS if is_video else AUD_CODECS
    if target not in codecs_map:
        return jsonify({"error":"Unbekannter Codec"}), 400
    if quality not in QUALITY_KEYS:
        return jsonify({"error":"Unbekannte Qualitätsstufe"}), 400
    # Container nur bei Video wählbar; leer/unbekannt → Standard des Codecs (mkv).
    # TV-Reparatur arbeitet nur an MKV und bleibt dabei (Umverpacken an Ort und Stelle).
    container = (d.get("container") or "").lower()
    if is_video:
        if container and container not in codec_containers(target):
            return jsonify({"error":"Container passt nicht zum gewählten Codec"}), 400
        if not container:
            container = codec_containers(target)[0]
    elif is_tv:
        container = "mkv"
    else:
        container = None
    ordner, fehler, antwort = auswahl_aus_request(d)
    if antwort:
        return antwort

    exts = quell_endungen(ctype)
    # If frontend sends prescan file list, use it directly (skip re-scanning all files)
    prescan_files = d.get("files")
    if prescan_files and isinstance(prescan_files, list):
        paare = dateien_aus_auswahl(prescan_files, ordner, exts)
        if not paare:
            return jsonify({"error":"Keine gültigen Dateien in der Auswahl"}), 404
    else:
        paare = dateien_sammeln(ordner, exts)
        if not paare:
            return jsonify({"error": keine_dateien_err(len(ordner))}), 404

    tid = uuid.uuid4().hex[:8]
    # Anzeigename und Ordnerliste IMMER aus dem geprüften Pfad, nie aus dem Rohtext (I1b)
    labels = {o: folder_label(o) for o in ordner}
    file_items = [
        {"name": os.path.basename(f), "path": f, "src": labels[q],
         "rel_dir": rel_dir_of(f, q), "status": "pending",
         "progress": 0, "eta": 0, "method": "", "error": "",
         "size_before": _filesize(f), "size_after": None, "speed": 0.0, "fps": 0.0,
         "streams_before": None, "streams_after": None, "subs_dropped": 0}
        for f, q in paare
    ]
    files = [f for f, _ in paare]
    ordner_labels = [labels[o] for o in ordner]
    bytes_total = sum(fi["size_before"] for fi in file_items)
    task = {
        "id": tid, "status": "queued", "folder": ordner_anzeige(ordner_labels),
        "folders": ordner_labels, "target": target, "container": container,
        "target_label": codecs_map[target]["label"], "type": ctype, "quality": quality,
        "total": len(files), "completed": 0, "skipped": 0, "failed": 0,
        "progress": 0, "eta": 0, "bytes_total": bytes_total, "bytes_done": 0,
        "bytes_before_done": 0, "bytes_after_done": 0, "bytes_saved": 0,
        "started_at": None, "finished_at": None, "t0": None,
        "_procs": {},
        "files": file_items,
    }
    with queue_cv:
        position = len(state["queue"]) + (1 if state["current"] else 0)
        state["queue"].append(task)
        queue_cv.notify_all()

    return jsonify({"ok": True, "job_id": tid, "position": position,
                    "total": len(files), "fehler": fehler})

@app.route("/api/status")
@auth
def status():
    return jsonify(get_status())

SSE_SLOTS = threading.BoundedSemaphore(SSE_MAX)

class _Slot:
    """Einmalige Freigabe eines Semaphor-Platzes (Generator-Ende ODER Response-Close)."""
    def __init__(self, sem):
        self.sem = sem
        self.done = False
        self._l = threading.Lock()

    def release(self):
        with self._l:
            if self.done:
                return
            self.done = True
        self.sem.release()

@app.route("/api/events")
@auth
def events():
    """Server-Sent Events: status every 1 s while a job runs, else every 5 s; `done` event at job end.

    Jede offene Verbindung belegt dauerhaft einen der 16 waitress-Threads – deshalb
    gedeckelt. Das Frontend fällt bei 503 auf Polling im Sekundentakt zurück (S7)."""
    if not SSE_SLOTS.acquire(blocking=False):
        return jsonify({"error": "Zu viele Live-Verbindungen – die Seite aktualisiert im Sekundentakt weiter"}), 503
    slot = _Slot(SSE_SLOTS)

    def gen():
        last_id = None
        last_beat = time.time()
        try:
            while True:
                snap = get_status()
                cur_id = snap["task"]["id"] if snap["task"] else None
                if last_id and cur_id != last_id:
                    fin = snap["last"] if (snap["last"] and snap["last"]["id"] == last_id) else None
                    if fin:
                        yield "event: done\ndata: " + json.dumps(fin) + "\n\n"
                last_id = cur_id
                yield "data: " + json.dumps(snap) + "\n\n"
                if time.time() - last_beat >= 15:
                    yield ": ping\n\n"
                    last_beat = time.time()
                interval = 1.0 if snap["active"] else 5.0
                slept = 0.0
                while slept < interval:
                    time.sleep(0.25)
                    slept += 0.25
                    if not snap["active"]:
                        with lock:
                            if state["current"] is not None:
                                break   # job started – report immediately
        finally:
            slot.release()   # GeneratorExit bei Client-Abbruch landet auch hier
    resp = Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    # Falls der Generator nie startet (Client bricht sofort ab), gibt der Close den Platz frei
    resp.call_on_close(slot.release)
    return resp

@app.route("/api/stats")
@auth
def stats_route():
    """Cumulative before/after size stats across all completed runs."""
    with stats_lock:
        s = load_stats()
    saved = s.get("bytes_before", 0) - s.get("bytes_after", 0)
    pct = round(saved / s["bytes_before"] * 100, 1) if s.get("bytes_before") else 0
    return jsonify({**s, "bytes_saved": saved, "percent_saved": pct})

def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default

@app.route("/api/history")
@auth
def history_route():
    """Paginated per-file history with run grouping. limit=all returns everything on one page."""
    with history_lock:
        h = load_history()
    for e in h:
        if not e.get("run_id"):
            e["run_id"] = legacy_run_id(e.get("timestamp", 0))
        e.setdefault("src", "")   # Einträge aus der Zeit vor dem Sammelauftrag
    runs = load_runs()

    q = (request.args.get("q") or "").strip().lower()
    method = (request.args.get("method") or "").strip()
    run = (request.args.get("run") or "").strip()
    filtered = [e for e in h
                if (not q or q in (e.get("name") or "").lower())
                and (not method or e.get("method") == method)
                and (not run or e.get("run_id") == run)]
    h_sorted = sorted(filtered, key=lambda e: e.get("timestamp", 0), reverse=True)
    total = len(h_sorted)

    limit_param = request.args.get("limit", "20")
    if limit_param == "all":
        entries = h_sorted
        page, pages, limit = 1, 1, total
    else:
        limit = max(1, _int_arg("limit", 20))
        pages = max(1, -(-total // limit))  # ceil div
        page = min(max(1, _int_arg("page", 1)), pages)
        start = (page - 1) * limit
        entries = h_sorted[start:start + limit]

    # totals beschreiben IMMER die Gesamtbilanz – sie kommen aus dem Lauf-Speicher und
    # nicht aus den Einzeleinträgen. Nur so überstehen Kacheln, Rückgewinn-Leiste und
    # Diagramm sowohl jeden Filter (S11) als auch das Löschen von Einträgen.
    bytes_before = sum(r.get("bytes_before", 0) or 0 for r in runs)
    bytes_after = sum(r.get("bytes_after", 0) or 0 for r in runs)
    files_total = sum(r.get("count", 0) or 0 for r in runs)
    # "zuletzt" kommt aus der Gesamtbilanz: nach einer Teilwiederherstellung ist der
    # jüngste bekannte Lauf älter als der letzte echte – sonst widerspräche der
    # Verlauf der Startseite.
    with stats_lock:
        last_run = load_stats().get("last_run")
    if not last_run and runs:
        last_run = runs[0].get("timestamp")
    return jsonify({
        "entries": entries,
        "total": total,
        "filtered_total": total,
        # Zählt die Dateiliste, nicht die Bilanz: nach „Verlauf löschen" stand sonst
        # „87 Dateien im Verlauf" über einer leeren Liste.
        "list_total": len(h),
        "page": page,
        "pages": pages,
        "limit": limit,
        "totals": {"bytes_before": bytes_before, "bytes_after": bytes_after, "count": files_total,
                   "last_run": last_run},
        "runs": runs,
    })

@app.route("/api/history/clear", methods=["POST"])
@auth
def history_clear():
    """Clear only the per-file history. Neither stats.json nor runs.jsonl are touched."""
    clear_history()
    return jsonify({"ok": True})


@app.route("/api/history/delete", methods=["POST"])
@auth
def history_delete():
    """Remove a single file entry. The run summary behind it stays – deleting tidies
    the list, never the balance."""
    d = request.get_json(silent=True) or {}
    run_id, path, ts = d.get("run_id"), d.get("path"), d.get("timestamp")
    if not run_id or not path or ts is None:
        return jsonify({"error": "run_id, path und timestamp sind nötig"}), 400
    try:
        removed = delete_history_entry(run_id, path, float(ts))
    except (TypeError, ValueError):
        return jsonify({"error": "Ungültiger Zeitstempel"}), 400
    return jsonify({"ok": True, "removed": removed})

@app.route("/api/history/delete-run", methods=["POST"])
@auth
def history_delete_run():
    """Remove every file entry of one run. The run stays in the balance and the chart."""
    d = request.get_json(silent=True) or {}
    run_id = d.get("run_id")
    if not run_id:
        return jsonify({"error": "run_id ist nötig"}), 400
    return jsonify({"ok": True, "removed": delete_history_run(run_id)})

@app.route("/api/cancel", methods=["POST"])
@auth
def cancel():
    """Cancel only the running job; the queue continues."""
    with lock:
        if state["current"]:
            _kill_current_locked()
    return jsonify({"ok":True})

@app.route("/api/clear", methods=["POST"])
@auth
def clear():
    """Cancel running job, drop the queue, forget current/last."""
    with lock:
        state["queue"].clear()
        had_current = state["current"] is not None
        if had_current:
            # Merken, damit ein noch laufender Worker den Job nicht als `last`
            # zurückschreibt, nachdem er ausgeblendet wurde (S12)
            state["discard_id"] = state["current"]["id"]
            _kill_current_locked()
    if had_current:
        deadline = time.time() + 5
        while time.time() < deadline:
            with lock:
                if state["current"] is None:
                    break
            time.sleep(0.05)
    with lock:
        state["queue"].clear()
        state["last"] = None
        if state["current"] is None:
            state["cancel"] = False
            state["discard_id"] = None   # Worker ist durch, die Merkung wird nicht mehr gebraucht
    return jsonify({"ok":True})

@app.route("/api/queue/<job_id>", methods=["DELETE"])
@auth
def queue_remove(job_id):
    with lock:
        before = len(state["queue"])
        state["queue"][:] = [t for t in state["queue"] if t["id"] != job_id]
        removed = before - len(state["queue"])
    if not removed:
        return jsonify({"error": "Auftrag nicht in der Warteschlange"}), 404
    return jsonify({"ok": True})

@app.route("/api/delete-file", methods=["POST"])
@auth
def delete_file():
    """Delete a single media file (for corrupt file cleanup)."""
    d = request.get_json(silent=True) or {}
    path = d.get("path", "")
    if not path or not isinstance(path, str):
        return jsonify({"error": "Kein Pfad angegeben"}), 400
    if os.path.islink(path):
        return jsonify({"error": "Symbolische Links werden nicht gelöscht"}), 400
    try:
        real = os.path.realpath(path)
        root = os.path.realpath(MEDIA_ROOT)
    except (ValueError, OSError):              # Nullbyte im Pfad → 400 statt 500
        return jsonify({"error": "Ungültiger Pfad"}), 400
    if not real.startswith(root.rstrip(os.sep) + os.sep):
        return jsonify({"error": "Ungültiger Pfad"}), 400
    if os.path.splitext(real)[1].lower() not in (VID_EXT | AUD_EXT):
        return jsonify({"error": "Nur Mediendateien können gelöscht werden"}), 400
    if not os.path.isfile(real):
        return jsonify({"error": "Datei nicht gefunden"}), 404
    try:
        os.remove(real)
        print(f"[DELETE] {real}", flush=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Ansehen im Browser ──────────────────────────────────────────────────────
# Der Verlauf merkt sich den vollen Pfad jeder konvertierten Datei. Damit sie
# sich direkt anhoeren/ansehen laesst, ohne sie erst herunterzuladen, liefert
# der Server sie hier aus -- MKV & Co. vorher verlustfrei umgepackt, weil kein
# verbreiteter Browser sie nativ abspielt.
PLAY_CACHE_GB = int(os.environ.get("MC_PLAY_CACHE_GB", "4") or 4)


def _play_path(roh):
    """Absoluten Pfad einer Mediendatei pruefen: unter MEDIA_ROOT und wirklich da.

    Kein Symlink-Schlupfloch (realpath), kein Nullbyte (ValueError), und nur
    Endungen, die der Konverter selbst als Medien behandelt.
    """
    if not roh or not isinstance(roh, str):
        return None, ("Kein Pfad angegeben", 400)
    try:
        real = os.path.realpath(roh)
        root = os.path.realpath(MEDIA_ROOT)
    except (ValueError, OSError, TypeError):
        return None, ("Ungueltiger Pfad", 400)
    if not real.startswith(root.rstrip(os.sep) + os.sep):
        return None, ("Ungueltiger Pfad", 400)
    if os.path.splitext(real)[1].lower() not in (VID_EXT | AUD_EXT):
        return None, ("Keine Mediendatei", 400)
    if not os.path.isfile(real):
        return None, ("Datei nicht gefunden", 404)
    return real, None


@app.route("/api/play")
@auth
def play_info():
    """Direkt abspielbar, erst umpacken, oder gar nicht? Plus Fortschritt."""
    real, err = _play_path(request.args.get("path", ""))
    if err:
        return jsonify({"error": err[0]}), err[1]
    plan = playback.plan_fuer(real)
    antwort = {"modus": plan["modus"], "grund": plan.get("grund", ""),
               "ziel": plan.get("ziel", ""), "mime": plan.get("mime", ""),
               "name": os.path.basename(real)}
    if plan["modus"] == "umpacken":
        antwort.update(playback.status(real, plan["ziel"]))
    else:
        antwort.update({"bereit": plan["modus"] == "direkt", "fortschritt": 100})
    return jsonify(antwort)


@app.route("/api/play", methods=["POST"])
@auth
def play_prepare():
    """Das Umpacken anstossen. Antwortet sofort, ffmpeg laeuft im Hintergrund."""
    d = request.get_json(silent=True) or {}
    real, err = _play_path(d.get("path", ""))
    if err:
        return jsonify({"error": err[0]}), err[1]
    plan = playback.vorbereiten(real, PLAY_CACHE_GB * 1024 ** 3)
    return jsonify({"modus": plan["modus"], "grund": plan.get("grund", ""),
                    "bereit": bool(plan.get("bereit")),
                    "fortschritt": plan.get("fortschritt", 0)})


@app.route("/api/play/stream")
@auth
def play_stream():
    """Die abspielbare Fassung ausliefern -- mit Range, damit Spulen geht."""
    real, err = _play_path(request.args.get("path", ""))
    if err:
        return jsonify({"error": err[0]}), err[1]
    plan = playback.plan_fuer(real)
    if plan["modus"] == "direkt":
        # conditional=True laesst Werkzeug die Range-Anfragen beantworten --
        # ohne 206 laedt ein <video> die ganze Datei und spult nicht.
        return send_file(real, conditional=True,
                         download_name=os.path.basename(real))
    if plan["modus"] != "umpacken":
        return jsonify({"error": plan.get("grund", "Nicht abspielbar")}), 415
    ablage = playback.cache_pfad(real, plan["ziel"])
    if not (ablage.is_file() and ablage.stat().st_size > 0):
        return jsonify({"error": "Noch nicht fertig aufbereitet"}), 409
    os.utime(ablage, None)          # zuletzt benutzt -> faellt spaeter heraus
    return send_file(str(ablage), conditional=True,
                     download_name=os.path.splitext(os.path.basename(real))[0]
                     + plan["ziel"])


@app.route("/api/play/download")
@auth
def play_download():
    """Die Originaldatei zum Herunterladen -- unveraendert, ohne Umpacken."""
    real, err = _play_path(request.args.get("path", ""))
    if err:
        return jsonify({"error": err[0]}), err[1]
    return send_file(real, conditional=True, as_attachment=True,
                     download_name=os.path.basename(real))


@app.route("/api/playcache")
@auth
def playcache_info():
    return jsonify({"bytes": playback.cache_groesse(),
                    "grenze": PLAY_CACHE_GB * 1024 ** 3})


@app.route("/api/playcache/clear", methods=["POST"])
@auth
def playcache_clear():
    return jsonify({"ok": True, "freigegeben": playback.cache_leeren()})


@app.route("/api/change-credentials", methods=["POST"])
@auth
def change_credentials():
    """Change username and/or password."""
    global AUTH_USER, PW_HASH
    d = request.get_json(silent=True) or {}
    new_user = (d.get("username") or "").strip()
    new_pw = d.get("password") or ""
    current_pw = d.get("current_password") or ""

    with auth_lock:
        # Verify current password
        if not bcrypt.checkpw(current_pw.encode(), PW_HASH):
            return jsonify({"error": "Aktuelles Passwort falsch"}), 401

        if new_pw and len(new_pw) < 6:
            return jsonify({"error": "Passwort muss mindestens 6 Zeichen haben"}), 400

        if new_user:
            AUTH_USER = new_user
            with open(AUTH_USER_FILE, "w") as f:
                f.write(new_user)

        if new_pw:
            h = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt(12))
            PW_HASH = h
            schreibe_geheim(AUTH_HASH_FILE, h.decode())

    return jsonify({"ok": True})


@app.route("/api/auth-info")
@auth
def auth_info():
    """Return current username (for settings form)."""
    with auth_lock:
        return jsonify({"username": AUTH_USER})


# ─── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from waitress import serve
    print(f"[INIT] Media Converter :{PORT}", flush=True)
    print(f"[INIT] Media: {MEDIA_ROOT}", flush=True)
    print(f"[INIT] Workers: {WORKERS}", flush=True)
    print(f"[INIT] GPU VAAPI: {'ON' if GPU else 'OFF (CPU only)'} {GPU_CAPS}", flush=True)
    # server.py ist PID 1 im Container: ohne Handler stirbt es bei `docker stop`
    # sofort und ffmpeg mit ihm – der Lauf fehlte dann in der Bilanz.
    signal.signal(signal.SIGTERM, _sigterm)
    threading.Thread(target=_start_aufraeumen, name="tmp_cleanup", daemon=True).start()
    n_teil = playback.verwaiste_teile_aufraeumen()
    if n_teil:
        print(f"[PLAY] {n_teil} verwaiste .teil-Datei(en) im Zwischenspeicher entfernt", flush=True)
    serve(app, host="0.0.0.0", port=PORT, threads=16)
