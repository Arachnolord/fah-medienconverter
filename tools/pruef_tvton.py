#!/usr/bin/env python3
"""Echt-Probe der TV-Reparatur mit ffmpeg (im Konverter-Container ausführen).

    MC_CONFIG_DIR=/tmp/tvprobe/cfg MC_PROGRESS_DIR=/tmp/tvprobe/prog \\
        python3 pruef_tvton.py <ordner-mit-server.py> [echte-datei.mkv <ausgabeordner>]

Erzeugt synthetische MKVs (H.264 + DTS + AAC-Zweitspur + SRT + Text-Anhang, optional
Cover), lässt cmd_tvton laufen und prüft Spuren + Lippensynchronität. Gegenproben,
die ABGELEHNT werden müssen: konstanter Versatz (60/−40 ms), Teil-Versatz und
Teil-Stille ab Filmmitte. Dazu eine kurze Datei (< 60 s), eine Datei mit Cover und
einmal der komplette Ablauf `convert_one` MIT Ersetzen an einer Testkopie.
Optional zusätzlich eine echte Datei als Trockenlauf (Ausgabe in <ausgabeordner>,
das Original wird NIE angefasst). Rückgabecode 0 = alle Proben bestanden.
"""
import os
import shutil
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


def fmt_mess(mess):
    return [(round(l, 1), round(k, 2), round(pr), round(pn)) for l, k, pr, pn, _ in mess]


def testfilm(pfad, codec, kanaele, dauer=90, cover=False):
    """Pink-Rauschen mit unregelmäßiger Lautstärke-Taktung = gut korrelierbar."""
    srt = pfad + ".srt"
    with open(srt, "w") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\nHallo\n")
    anhang = pfad + ".txt"
    with open(anhang, "w") as f:
        f.write("Anhang")
    ton = (f"anoisesrc=d={dauer}:c=pink:r=48000:a=0.6,"
           "volume='if(lt(mod(t*3.7\\,1.3)\\,0.45)\\,1\\,0.08)':eval=frame")
    args = ["-f", "lavfi", "-i", f"testsrc2=d={dauer}:s=640x360:r=25",
            "-f", "lavfi", "-i", ton, "-f", "lavfi", "-i", ton, "-i", srt,
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
            "-c:a:0", codec, "-ac:a:0", str(kanaele), "-strict", "-2",
            "-c:a:1", "aac", "-ac:a:1", "2", "-c:s", "srt",
            "-metadata:s:a:0", "language=ger", "-metadata:s:a:1", "language=eng",
            "-metadata:s:a:0", "BPS-eng=1509000", "-metadata:s:a:0", "NUMBER_OF_BYTES-eng=123",
            "-disposition:a:0", "default", "-disposition:a:1", "comment",
            "-attach", anhang, "-metadata:s:t:0", "mimetype=text/plain"]
    if cover:
        bild = pfad + ".cover.jpg"
        ff("-f", "lavfi", "-i", "color=c=red:s=300x450", "-frames:v", "1", bild)
        args += ["-attach", bild, "-metadata:s:t:1", "mimetype=image/jpeg", "-metadata:s:t:1", "filename=cover.jpg"]
    ff(*args, pfad)


def lauf(quelle, ausgabe_ordner, name):
    info = server.get_info(quelle)
    plan = server.tvton_plan(info["audio_streams"])
    pruef(f"{name}: Plan vorhanden", plan is not None, server.tv_ton_beschreibung(info["audio_streams"]))
    if plan is None:
        return None, None, None
    tmp = os.path.join(ausgabe_ordner, os.path.basename(quelle) + ".converting.mkv")
    prog = os.path.join(ausgabe_ordner, "prog.log")
    bilder, err = server.tv_bilder_herausloesen(quelle, info.get("covers") or [], tmp)
    pruef(f"{name}: Cover gesichert", not err, f"{len(bilder)} Bild(er)")
    t0 = time.time()
    r = subprocess.run(server.cmd_tvton(quelle, tmp, prog, plan, bilder, info.get("n_att", 0)),
                       capture_output=True, text=True)
    for b in bilder:
        os.remove(b["pfad"])
    pruef(f"{name}: ffmpeg ok", r.returncode == 0, r.stderr[-300:] if r.returncode else f"{time.time() - t0:.0f} s")
    if r.returncode:
        return None, None, None
    pruef(f"{name}: keine Dekodierfehler", server.tv_dekodierfehler(r.stderr) == 0)
    out = server.info_from_probe(server.probe(tmp))
    ok, err = server.tvton_spuren_ok(info, out, plan)
    pruef(f"{name}: Spuren", ok, err or f"{out['n_audio']} Ton, {out['n_sub']} UT, {out['n_att']} Anhänge, "
                                        f"{out['n_cover']} Cover")
    erste = out["audio_streams"][0]
    pruef(f"{name}: erste Spur AC3 ≤5.1 + Standard", erste["codec"] == "ac3" and erste["channels"] <= 6
          and erste["disp"].get("default") == 1, f"{erste['codec']} {erste['channels']}ch")
    geerbt = [k for k in erste.get("tags", []) if k.upper().startswith(("BPS", "NUMBER_OF", "_STATISTICS"))]
    pruef(f"{name}: neue Spur ohne geerbte Statistik-Tags", not geerbt, str(erste.get("tags")))
    orig = out["audio_streams"][1 + plan["haupt_nr"]]
    pruef(f"{name}: Originalspur behält Sprache", orig["lang"] == plan["haupt"]["lang"],
          f"{orig['codec']} {orig['lang'] or '-'}")
    t0 = time.time()
    ok, text, mess = server.tvton_sync_pruefen(tmp, info["dur"], plan, quelle)
    pruef(f"{name}: lippensynchron", ok, f"{text} {fmt_mess(mess)} ({time.time() - t0:.1f} s)")
    return tmp, info, plan


def verfaelscht(tmp, ziel, filter_a0):
    """Kopie von tmp, deren neue erste Spur per Filter verfälscht wurde."""
    ff("-i", tmp, "-map", "0:V", "-map", "0:a", "-map", "0:s?", "-c", "copy",
       "-filter:a:0", filter_a0, "-c:a:0", "ac3", "-b:a:0", "640k", ziel)


def gegenproben(tmp, info, plan, d, name):
    mitte = info["dur"] / 2
    faelle = [
        ("60 ms Versatz", "adelay=60:all=1"),
        ("Teil-Versatz 300 ms ab Filmmitte",
         f"asplit[a][b];[a]atrim=0:{mitte},asetpts=PTS-STARTPTS[a1];"
         f"[b]atrim={mitte},asetpts=PTS-STARTPTS,adelay=300:all=1[b1];[a1][b1]concat=n=2:v=0:a=1"),
        ("Teil-Stille ab Filmmitte", f"volume=enable='gte(t,{mitte})':volume=0"),
    ]
    for titel, filt in faelle:
        schief = os.path.join(d, "schief.mkv")
        verfaelscht(tmp, schief, filt)
        ok, text, mess = server.tvton_sync_pruefen(schief, info["dur"], plan, tmp)
        pruef(f"{name}: {titel} wird ABGELEHNT", not ok, f"{text} {fmt_mess(mess)}")
        os.remove(schief)
    # Vorzeichen andersherum (Ton zu früh)
    schief = os.path.join(d, "schief.mkv")
    ff("-i", tmp, "-itsoffset", "-0.040", "-i", tmp, "-map", "0:V", "-map", "1:a:0", "-map", "0:a:1",
       "-map", "0:a:2", "-map", "0:s?", "-c", "copy", schief)
    ok, text, mess = server.tvton_sync_pruefen(schief, info["dur"], plan, tmp)
    pruef(f"{name}: −40 ms Versatz wird ABGELEHNT", not ok, f"{text} {fmt_mess(mess)}")
    os.remove(schief)


def gesamtablauf(d):
    """convert_one mit ECHTEM Ersetzen an einer Testkopie (inkl. Cover)."""
    print("== Gesamtablauf convert_one (ersetzt die Testkopie)", flush=True)
    film = os.path.join(d, "Gesamt.mkv")
    testfilm(film, "dca", 6, dauer=75, cover=True)
    for rest in (film + ".srt", film + ".txt", film + ".cover.jpg"):
        os.remove(rest)
    ino_vorher = os.stat(film).st_ino
    fi = {"name": "Gesamt.mkv", "path": film, "src": "t", "rel_dir": "", "status": "pending",
          "progress": 0, "eta": 0, "method": "", "error": "", "size_before": os.path.getsize(film),
          "size_after": None, "speed": 0.0, "fps": 0.0, "streams_before": None, "streams_after": None,
          "subs_dropped": 0}
    t = {"id": "probe", "type": "tvfix", "container": "mkv", "target": "tvton", "quality": "balanced",
         "files": [fi], "_procs": {}, "completed": 0, "skipped": 0, "failed": 0, "total": 1,
         "bytes_total": fi["size_before"], "bytes_done": 0, "bytes_before_done": 0, "bytes_after_done": 0,
         "bytes_saved": 0, "progress": 0, "eta": 0, "_collisions": {}, "t0": time.time(),
         "status": "running", "folder": "t", "folders": ["t"], "target_label": "TV-Ton",
         "started_at": time.time(), "finished_at": None}
    server.state["cancel"] = False
    server.convert_one(t, 0, film, "tvton", False, "balanced")
    pruef("Gesamt: Status fertig", fi["status"] == "done", fi.get("error", "") + " " + fi.get("sync", ""))
    pruef("Gesamt: Original ersetzt (neue Datei am selben Pfad)", os.stat(film).st_ino != ino_vorher)
    out = server.info_from_probe(server.probe(film))
    pruef("Gesamt: Ergebnis AC3 vorn, Cover + Anhang da",
          out["audio_streams"][0]["codec"] == "ac3" and out["n_cover"] == 1 and out["n_att"] == 1,
          f"{[a['codec'] for a in out['audio_streams']]} Cover={out['n_cover']} Anhänge={out['n_att']}")
    reste = [f for f in os.listdir(d) if ".converting" in f]
    pruef("Gesamt: keine Reste im Ordner", not reste, str(reste))
    # zweiter Lauf: nichts mehr zu tun
    fi2 = dict(fi, status="pending")
    t["files"] = [fi2]
    server.convert_one(t, 0, film, "tvton", False, "balanced")
    pruef("Gesamt: zweiter Lauf überspringt", fi2["status"] == "skipped", fi2.get("method", ""))


def main():
    with tempfile.TemporaryDirectory(dir=os.environ.get("TVPROBE_TMP")) as d:
        # TrueHD/7.1 lassen sich mit ffmpeg nicht synthetisch ERZEUGEN (Encoder
        # verweigert) – der 7.1-Downmix wird deshalb an echten Dateien geprüft.
        for name, codec, ch, dauer, cover in (("DTS 5.1", "dca", 6, 90, False), ("DTS Stereo", "dca", 2, 90, False),
                                              ("DTS kurz 30 s", "dca", 6, 30, False),
                                              ("DTS mit Cover", "dca", 6, 70, True)):
            print(f"== {name}", flush=True)
            quelle = os.path.join(d, f"test_{codec}_{ch}_{dauer}{'_c' if cover else ''}.mkv")
            testfilm(quelle, codec, ch, dauer, cover)
            tmp, info, plan = lauf(quelle, d, name)
            if tmp and name == "DTS 5.1":
                gegenproben(tmp, info, plan, d, name)
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        gesamt = os.path.join(d, "gesamt")
        os.makedirs(gesamt)
        gesamtablauf(gesamt)
    if len(sys.argv) >= 4:
        echt, ziel = sys.argv[2], sys.argv[3]
        print(f"== Echte Datei (Trockenlauf, Original bleibt): {os.path.basename(echt)}", flush=True)
        os.makedirs(ziel, exist_ok=True)
        tmp, _, _ = lauf(echt, ziel, "Echt")
        if tmp and os.path.exists(tmp):
            print(f"  Größe: {os.path.getsize(echt) / 1e9:.2f} GB → {os.path.getsize(tmp) / 1e9:.2f} GB", flush=True)
            os.remove(tmp)
        shutil.rmtree(ziel, ignore_errors=True)
    print("ERGEBNIS:", "alle Proben bestanden" if not FEHLER else f"{len(FEHLER)} FEHLER: {FEHLER}")
    return 1 if FEHLER else 0


if __name__ == "__main__":
    sys.exit(main())
