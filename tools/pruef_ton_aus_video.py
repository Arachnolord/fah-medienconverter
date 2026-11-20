#!/usr/bin/env python3
"""Echt-Probe „Ton aus Video“ mit ffmpeg (im Konverter-Container ausführen).

    MC_CONFIG_DIR=/tmp/tonprobe/cfg MC_PROGRESS_DIR=/tmp/tonprobe/prog MC_MEDIA_ROOT=/tmp/tonprobe \\
        python3 pruef_ton_aus_video.py <ordner-mit-server.py>

Erzeugt ein synthetisches MKV (H.264 + AC3 + AAC-Zweitspur als Standard + SRT), eine
MP4 mit AAC und eine MPEG-TS mit AAC (ADTS) und lässt `convert_one` im Musik-Modus
laufen: MP3 (neu kodiert, Standard-Spur), AAC (Spur kopiert), Opus, TS→M4A (Kopie
oder Rückfall). Geprüft wird: Video bleibt liegen, Tondatei liegt daneben, genau eine
Tonspur mit dem Zielcodec, Dauer stimmt, keine Bilanz-Ersparnis, keine Reste.
Rückgabecode 0 = alle Proben bestanden.
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, sys.argv[1])
import server  # noqa: E402

FEHLER = []


def pruef(name, bedingung, info=""):
    print(f"  [{'OK ' if bedingung else 'FEHLER'}] {name} {info}", flush=True)
    if not bedingung:
        FEHLER.append(name)


def ff(*args):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])


def testvideo(pfad, dauer=12):
    """MKV: H.264 + AC3 (ger, comment) + AAC (eng, DEFAULT) + SRT. Die Standard-Spur ist
    absichtlich die ZWEITE, damit die Spurwahl und nicht ffmpegs Automatik geprüft wird."""
    srt = pfad + ".srt"
    with open(srt, "w") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\nHallo\n")
    ton = f"sine=f=440:d={dauer}:r=48000"
    ton2 = f"sine=f=880:d={dauer}:r=48000"
    ff("-f", "lavfi", "-i", f"testsrc2=d={dauer}:s=320x180:r=25",
       "-f", "lavfi", "-i", ton, "-f", "lavfi", "-i", ton2, "-i", srt,
       "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a:0", "ac3", "-c:a:1", "aac", "-c:s", "srt",
       "-metadata:s:a:0", "language=ger", "-metadata:s:a:1", "language=eng",
       "-disposition:a:0", "comment", "-disposition:a:1", "default", pfad)
    os.remove(srt)


def testclip(pfad, dauer=8):
    """MP4 bzw. TS mit H.264 + AAC (eine Spur)."""
    ff("-f", "lavfi", "-i", f"testsrc2=d={dauer}:s=320x180:r=25",
       "-f", "lavfi", "-i", f"sine=f=660:d={dauer}:r=48000",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", pfad)


def lauf(quelle, target, name, erwartet_codec, erwartet_methode=None, erwartet_freq=None):
    fi = {"name": os.path.basename(quelle), "path": quelle, "src": "t", "rel_dir": "", "status": "pending",
          "progress": 0, "eta": 0, "method": "", "error": "", "size_before": os.path.getsize(quelle),
          "size_after": None, "speed": 0.0, "fps": 0.0, "streams_before": None, "streams_after": None,
          "subs_dropped": 0}
    t = {"id": "probe", "type": "audio", "container": None, "target": target, "quality": "balanced",
         "files": [fi], "_procs": {}, "completed": 0, "skipped": 0, "failed": 0, "total": 1,
         "bytes_total": fi["size_before"], "bytes_done": 0, "bytes_before_done": 0, "bytes_after_done": 0,
         "bytes_saved": 0, "progress": 0, "eta": 0, "_collisions": {}, "t0": time.time(),
         "status": "running", "folder": "t", "folders": ["t"], "target_label": target,
         "started_at": time.time(), "finished_at": None}
    server.state["cancel"] = False
    ino = os.stat(quelle).st_ino
    quelle_info = server.get_info(quelle)
    t0 = time.time()
    server.convert_one(t, 0, quelle, target, False, "balanced")
    ziel = server.target_path(quelle, server.AUD_CODECS[target]["ext"])
    pruef(f"{name}: Status fertig", fi["status"] == "done", fi.get("error", "") + f" ({time.time() - t0:.1f} s)")
    pruef(f"{name}: Video bleibt unverändert", os.path.exists(quelle) and os.stat(quelle).st_ino == ino)
    pruef(f"{name}: Tondatei liegt daneben", os.path.exists(ziel), os.path.basename(ziel))
    if erwartet_methode:
        pruef(f"{name}: Verfahren {erwartet_methode}", fi.get("method") == erwartet_methode, fi.get("method", ""))
    pruef(f"{name}: keine Schein-Ersparnis", t["bytes_saved"] == 0 and t["bytes_before_done"] == 0,
          f"saved={t['bytes_saved']} before={t['bytes_before_done']}")
    pruef(f"{name}: Fortschritt 100", t["progress"] == 100, str(t["progress"]))
    if os.path.exists(ziel):
        out = server.info_from_probe(server.probe(ziel))
        pruef(f"{name}: genau eine Tonspur, kein Bild, keine Untertitel",
              out["n_audio"] == 1 and out["n_video"] == 0 and out["n_sub"] == 0,
              f"a={out['n_audio']} v={out['n_video']} s={out['n_sub']}")
        pruef(f"{name}: Codec {erwartet_codec}", out["ac"] == erwartet_codec, str(out["ac"]))
        pruef(f"{name}: Dauer passt", abs((out["dur"] or 0) - (quelle_info["dur"] or 0)) < 0.5,
              f"{out['dur']:.2f} s statt {quelle_info['dur']:.2f} s")
        if erwartet_freq:
            # Welche Spur wurde genommen? Grundton per astats-Peak-Frequenz nicht verfügbar → per
            # Bandpass-Vergleich: Energie um erwartete Frequenz muss die andere klar übersteigen.
            def energie(f_hz):
                r = subprocess.run(["ffmpeg", "-v", "error", "-i", ziel, "-af",
                                    f"bandpass=f={f_hz}:w=60,astats=metadata=1:reset=0,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                                    "-f", "null", "-"], capture_output=True, text=True)
                werte = [float(l.split("=")[1]) for l in r.stdout.splitlines() if "RMS_level=" in l and "-inf" not in l]
                return werte[-1] if werte else -99.0
            e_soll, e_andere = energie(erwartet_freq), energie(440 if erwartet_freq == 880 else 880)
            pruef(f"{name}: Standard-Spur ({erwartet_freq} Hz) gewählt", e_soll > e_andere + 10,
                  f"{e_soll:.1f} dB gegen {e_andere:.1f} dB")
        os.remove(ziel)
    reste = [f for f in os.listdir(os.path.dirname(quelle)) if ".converting" in f]
    pruef(f"{name}: keine Reste", not reste, str(reste))
    return fi


def main():
    with tempfile.TemporaryDirectory(dir=os.environ.get("MC_MEDIA_ROOT")) as d:
        mkv = os.path.join(d, "Film.mkv")
        testvideo(mkv)
        info = server.get_info(mkv)
        pruef("Vorbereitung: MKV mit 2 Tonspuren, Standard = 2.", info["n_audio"] == 2
              and server.standard_tonspur(info["audio_streams"]) == 1)
        print("== MKV → MP3 (neu kodiert, Standard-Spur)", flush=True)
        lauf(mkv, "mp3", "MP3", "mp3", "CPU", erwartet_freq=880)
        print("== MKV → AAC (Standard-Spur ist AAC → Kopie)", flush=True)
        lauf(mkv, "aac", "AAC", "aac", "Kopie", erwartet_freq=880)
        print("== MKV → Opus", flush=True)
        lauf(mkv, "opus", "Opus", "opus", "CPU")
        print("== MKV → FLAC", flush=True)
        lauf(mkv, "flac", "FLAC", "flac", "CPU")
        print("== Zieldatei existiert schon → Fehler, nichts angefasst", flush=True)
        with open(os.path.join(d, "Film.mp3"), "wb") as f:
            f.write(b"alt")
        fi = {"name": "Film.mkv", "path": mkv, "src": "t", "rel_dir": "", "status": "pending",
              "progress": 0, "eta": 0, "method": "", "error": "", "size_before": os.path.getsize(mkv),
              "size_after": None, "speed": 0.0, "fps": 0.0, "streams_before": None, "streams_after": None,
              "subs_dropped": 0}
        t = {"id": "probe2", "type": "audio", "container": None, "target": "mp3", "quality": "balanced",
             "files": [fi], "_procs": {}, "completed": 0, "skipped": 0, "failed": 0, "total": 1,
             "bytes_total": fi["size_before"], "bytes_done": 0, "bytes_before_done": 0, "bytes_after_done": 0,
             "bytes_saved": 0, "progress": 0, "eta": 0, "_collisions": {}, "t0": time.time(),
             "status": "running", "folder": "t", "folders": ["t"], "target_label": "mp3",
             "started_at": time.time(), "finished_at": None}
        server.convert_one(t, 0, mkv, "mp3", False, "balanced")
        with open(os.path.join(d, "Film.mp3"), "rb") as f:
            unveraendert = f.read() == b"alt"
        pruef("Vorhandene Tondatei: Fehler gemeldet", fi["status"] == "error", fi.get("error", ""))
        pruef("Vorhandene Tondatei: nicht überschrieben", unveraendert)
        os.remove(os.path.join(d, "Film.mp3"))

        print("== MP4 (AAC) → AAC: Kopie", flush=True)
        mp4 = os.path.join(d, "Clip.mp4")
        testclip(mp4)
        lauf(mp4, "aac", "MP4→M4A", "aac", "Kopie")
        print("== MPEG-TS (AAC/ADTS) → AAC: Kopie oder Rückfall", flush=True)
        ts = os.path.join(d, "Aufnahme.ts")
        testclip(ts)
        fi = lauf(ts, "aac", "TS→M4A", "aac")
        print(f"     Verfahren: {fi.get('method')}", flush=True)
        print("== Prescan: Video zählt als Quelle, wird nie übersprungen", flush=True)
        art, item = server.prescan_check(mkv, d, "aac", False)
        pruef("Prescan MKV→AAC = convert", art == "convert", f"{art} codec={item['codec']}")
        art, item = server.prescan_check(mp4, d, "h264", True)
        pruef("Prescan MP4→H.264 (Video-Modus) = skip wie bisher", art == "skip", art)
    print("ERGEBNIS:", "alle Proben bestanden" if not FEHLER else f"{len(FEHLER)} FEHLER: {FEHLER}")
    return 1 if FEHLER else 0


if __name__ == "__main__":
    sys.exit(main())
