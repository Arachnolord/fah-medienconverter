# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Prueft den Player-Remux gegen ECHTES ffmpeg -- laeuft IM Container.

Auf einem Entwicklungsrechner ohne ffmpeg laesst sich nur pruefen, WAS mit
einer Datei geschehen soll. Ob der ffmpeg-Aufruf danach wirklich durchlaeuft,
zeigt erst dieser Test. Genau so wurde gefunden, dass ffmpeg das Ausgabeformat
aus der Endung ableitet und an der Zwischendatei "....teil" scheitert.

    cat tools/pruef_player_container.py | ssh <nas> \
        "cat > /volume3/docker/medienconverter/config/pp.py"
    ssh <nas> docker exec media-converter python /config/pp.py
"""
import os, pathlib, subprocess, sys, tempfile, time
sys.path.insert(0, "/app")
import playback

R = tempfile.mkdtemp()
quelle = os.path.join(R, "probe.mkv")
# h264 + AC3 in MKV: genau der Fall, der im Browser scheitert -- MKV kann er
# nicht, und AC3 darf auch in einer MP4-Huelle nicht bleiben.
subprocess.run(["ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3",
                quelle], check=True)
print("Testdatei:", os.path.getsize(quelle), "Bytes")

vorher = playback.pruefe_datei(quelle)
print("Spuren vorher:", vorher)
plan = playback.plan_fuer(quelle)
print("Plan:", {k: v for k, v in plan.items() if k != "spuren"})
assert plan["modus"] == "umpacken" and plan["ziel"] == ".mp4", plan

playback.CACHE_DIR = pathlib.Path(R) / "cache"
playback.vorbereiten(quelle, 4 * 1024 ** 3)
st = {}
for _ in range(180):
    st = playback.status(quelle, ".mp4")
    if st["bereit"] or st["fehler"]:
        break
    time.sleep(0.5)
print("Status:", st)
assert st.get("bereit"), st
ziel = playback.cache_pfad(quelle, ".mp4")
print("Ergebnis:", ziel.name, os.path.getsize(ziel), "Bytes")

nach = playback.pruefe_datei(str(ziel))
print("Spuren nachher:", nach)
assert nach["video"] == "h264", nach          # Bild unveraendert kopiert
assert nach["audio"] == "aac", nach           # nur der Ton gewandelt
assert abs(nach["dauer"] - 4) < 1, nach
kopf = open(ziel, "rb").read(64)
print("moov liegt vorne (Spulen moeglich):", b"moov" in kopf)
assert b"moov" in kopf

# Zweiter Aufruf darf nicht erneut rechnen
t0 = time.time()
r2 = playback.vorbereiten(quelle, 4 * 1024 ** 3)
print("Zweiter Aufruf:", r2.get("bereit"), f"{time.time() - t0:.3f}s")
assert r2.get("bereit") is True

# HEVC ist die Standardausgabe des Konverters -- es MUSS umgepackt werden,
# und die MP4 braucht den Tag "hvc1", sonst spielt Safari sie nicht ab.
hevc = os.path.join(R, "probe_hevc.mkv")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", quelle,
                "-c:v", "libx265", "-preset", "ultrafast", "-c:a", "copy",
                hevc], check=True)
plan2 = playback.plan_fuer(hevc)
print("HEVC-Plan:", plan2["modus"], plan2["ziel"], "|", plan2.get("mime", ""))
assert plan2["modus"] == "umpacken" and plan2["ziel"] == ".mp4", plan2
assert "hvc1" in plan2.get("mime", ""), plan2

playback.vorbereiten(hevc, 4 * 1024 ** 3)
for _ in range(180):
    st2 = playback.status(hevc, ".mp4")
    if st2["bereit"] or st2["fehler"]:
        break
    time.sleep(0.5)
print("HEVC umgepackt:", st2)
assert st2.get("bereit"), st2
hziel = playback.cache_pfad(hevc, ".mp4")
hnach = playback.pruefe_datei(str(hziel))
print("HEVC-Spuren danach:", hnach)
assert hnach["video"] == "hevc", hnach          # Bild unveraendert kopiert
assert hnach["audio"] == "aac", hnach           # AC3 -> AAC, nur der Ton
marke = subprocess.run(
    ["ffprobe", "-v", "error", "-select_streams", "v:0",
     "-show_entries", "stream=codec_tag_string", "-of",
     "default=nw=1:nk=1", str(hziel)],
    capture_output=True, text=True).stdout.strip()
print("Codec-Tag der MP4:", marke, "(hvc1 = Safari spielt sie ab)")
assert marke == "hvc1", marke

# Eine Bildspur, die WIRKLICH kein Browser darstellt -> ehrlicher Hinweis
alt_codec = os.path.join(R, "probe_mpeg4.avi")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", quelle,
                "-c:v", "mpeg4", "-c:a", "mp3", alt_codec], check=True)
plan4 = playback.plan_fuer(alt_codec)
print("MPEG-4-Plan:", plan4["modus"], "|", plan4["grund"][:70])
assert plan4["modus"] == "nein", plan4

# VP9 + Opus gehoert nach WebM, nicht nach MP4
webm_quelle = os.path.join(R, "probe_vp9.mkv")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", quelle,
                "-c:v", "libvpx-vp9", "-b:v", "200k", "-cpu-used", "8",
                "-c:a", "libopus", webm_quelle], check=True)
plan3 = playback.plan_fuer(webm_quelle)
print("VP9-Plan:", plan3["modus"], plan3["ziel"])
assert plan3["modus"] == "umpacken" and plan3["ziel"] == ".webm", plan3

print("ALLE PRUEFUNGEN BESTANDEN")
