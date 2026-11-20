# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Konvertierte Dateien im Browser ansehen und anhoeren.

Der eigentliche ffmpeg-Lauf braucht ffmpeg und wird deshalb im Container
geprueft (tools/pruef_player_container.py). Hier geht es um die Entscheidung,
was mit einer Datei geschehen soll, und vor allem um die Absicherung: die
Routen bekommen einen ABSOLUTEN Pfad vom Client und muessen alles abweisen,
was ausserhalb von MEDIA_ROOT liegt.
"""
import os
import threading
import time

import pytest


@pytest.fixture
def medien(server, dirs):
    """Testdateien im Medienordner -- und hinterher restlos wieder weg.

    Der Medienordner gehoert der ganzen Testsitzung. Bleibt hier etwas liegen,
    zaehlt es ein anderer Test mit (test_folders_endpoint erwartet in der
    Wurzel ausdruecklich null Mediendateien).
    """
    wurzel = dirs["media"]
    pfade, angelegt = {}, []
    for name, inhalt in (("clip.mp4", b"\x00" * 4096),
                         ("lied.mp3", b"\x00" * 2048),
                         ("film.mkv", b"\x00" * 8192),
                         ("notiz.txt", b"kein Medium")):
        p = os.path.join(wurzel, name)
        with open(p, "wb") as f:
            f.write(inhalt)
        pfade[name] = p
        angelegt.append(p)
    yield pfade
    for p in angelegt + [os.path.join(wurzel, "verweis.mp4")]:
        try:
            if os.path.islink(p) or os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass


# ── Absicherung ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("route", ["/api/play", "/api/play/stream",
                                   "/api/play/download", "/api/playcache"])
def test_ohne_anmeldung_abgewiesen(client, route):
    assert client.get(route).status_code == 401


def test_pfad_ausserhalb_media_root(logged_in):
    r = logged_in.get("/api/play", query_string={"path": "/etc/passwd"})
    assert r.status_code == 400
    assert "Pfad" in r.get_json()["error"]


def test_traversal_wird_abgewiesen(logged_in, dirs):
    ziel = os.path.join(dirs["media"], "..", "..", "etc", "passwd")
    assert logged_in.get("/api/play", query_string={"path": ziel}).status_code == 400


def test_nullbyte_gibt_400_statt_500(logged_in, dirs):
    """realpath wirft bei einem Nullbyte ValueError -- das darf nicht durchschlagen."""
    r = logged_in.get("/api/play",
                      query_string={"path": os.path.join(dirs["media"], "a\x00b.mp4")})
    assert r.status_code == 400


def test_symlink_aus_dem_medienordner_heraus(logged_in, dirs, medien):
    aussen = os.path.join(dirs["config"], "geheim.mp4")
    with open(aussen, "wb") as f:
        f.write(b"nicht fuer den Player")
    link = os.path.join(dirs["media"], "verweis.mp4")
    if os.path.exists(link):
        os.unlink(link)
    os.symlink(aussen, link)
    # realpath loest den Link auf -- das Ziel liegt ausserhalb, also 400.
    assert logged_in.get("/api/play",
                         query_string={"path": link}).status_code == 400


def test_nur_mediendateien(logged_in, medien):
    r = logged_in.get("/api/play", query_string={"path": medien["notiz.txt"]})
    assert r.status_code == 400
    assert "Mediendatei" in r.get_json()["error"]


def test_fehlende_datei_gibt_404(logged_in, dirs):
    r = logged_in.get("/api/play",
                      query_string={"path": os.path.join(dirs["media"], "weg.mp4")})
    assert r.status_code == 404


def test_leerer_pfad(logged_in):
    assert logged_in.get("/api/play", query_string={"path": ""}).status_code == 400


# ── Entscheidung ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["clip.mp4", "lied.mp3"])
def test_browsertaugliche_datei_geht_direkt(logged_in, medien, name):
    d = logged_in.get("/api/play", query_string={"path": medien[name]}).get_json()
    assert d["modus"] == "direkt"
    assert d["bereit"] is True
    assert d["name"] == name


def test_mkv_geht_nicht_direkt(logged_in, medien):
    d = logged_in.get("/api/play", query_string={"path": medien["film.mkv"]}).get_json()
    assert d["modus"] in ("umpacken", "nein")
    if d["modus"] == "nein":              # ohne ffmpeg: dann aber MIT Begruendung
        assert d["grund"]


# ── Ausliefern ──────────────────────────────────────────────────────────────
def test_stream_liefert_die_datei(logged_in, medien):
    r = logged_in.get("/api/play/stream", query_string={"path": medien["clip.mp4"]})
    assert r.status_code == 200
    assert len(r.data) == 4096


def test_stream_beantwortet_range_anfragen(logged_in, medien):
    """Ohne 206 laedt ein <video> die ganze Datei und laesst sich nicht spulen."""
    r = logged_in.get("/api/play/stream", query_string={"path": medien["clip.mp4"]},
                      headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert len(r.data) == 100
    assert r.headers["Content-Range"] == "bytes 100-199/4096"


def test_stream_meldet_range_bereitschaft(logged_in, medien):
    r = logged_in.get("/api/play/stream", query_string={"path": medien["clip.mp4"]})
    assert r.headers.get("Accept-Ranges") == "bytes"


def test_download_kommt_als_anhang(logged_in, medien):
    r = logged_in.get("/api/play/download", query_string={"path": medien["clip.mp4"]})
    assert r.status_code == 200
    assert "attachment" in r.headers.get("Content-Disposition", "")


def test_stream_ohne_aufbereitung_gibt_409(logged_in, medien, server):
    """MKV ist nicht direkt abspielbar -- solange nichts umgepackt wurde, 409."""
    plan = server.playback.plan_fuer(medien["film.mkv"])
    r = logged_in.get("/api/play/stream", query_string={"path": medien["film.mkv"]})
    assert r.status_code == (409 if plan["modus"] == "umpacken" else 415)


# ── Zwischenspeicher ────────────────────────────────────────────────────────
def test_playcache_meldet_groesse_und_grenze(logged_in):
    d = logged_in.get("/api/playcache").get_json()
    assert d["bytes"] >= 0
    assert d["grenze"] > 0


def test_playcache_leeren(logged_in, server):
    server.playback.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (server.playback.CACHE_DIR / "alt.mp4").write_bytes(b"x" * 512)
    d = logged_in.post("/api/playcache/clear").get_json()
    assert d["ok"] is True
    assert d["freigegeben"] >= 512
    assert server.playback.cache_groesse() == 0


def test_zwischenspeicher_liegt_im_config_ordner(server, dirs):
    """Er muss auf dem persistenten Volume liegen, nicht im Medienordner."""
    assert str(server.playback.CACHE_DIR).startswith(dirs["config"])


def test_cache_name_haengt_an_datei_und_zielformat(server, medien):
    a = server.playback.cache_pfad(medien["film.mkv"], ".mp4")
    b = server.playback.cache_pfad(medien["film.mkv"], ".webm")
    assert a != b
    vorher = server.playback.cache_pfad(medien["film.mkv"], ".mp4")
    with open(medien["film.mkv"], "ab") as f:      # Quelle aendert sich
        f.write(b"mehr")
    os.utime(medien["film.mkv"], (0, 0))
    assert server.playback.cache_pfad(medien["film.mkv"], ".mp4") != vorher


# ── Der ffmpeg-Aufruf (ohne laufendes ffmpeg pruefbar) ──────────────────────
def _plan(video, audio, ziel=".mp4", nur_ton=False):
    return {"modus": "umpacken", "ziel": ziel, "nur_ton": nur_ton,
            "spuren": {"video": video, "audio": audio, "dauer": 10.0}}


def test_bild_wird_immer_nur_kopiert(server):
    """Das ist der Kern: umpacken, NICHT neu kodieren."""
    b = server.playback.ffmpeg_befehl("/x/a.mkv", _plan("hevc", "ac3"), "/t/o.teil")
    assert "-c:v" in b and b[b.index("-c:v") + 1] == "copy"
    assert "libx264" not in b and "libx265" not in b


def test_hevc_bekommt_den_hvc1_tag(server):
    """Ohne 'hvc1' schreibt ffmpeg 'hev1' -- und Safari spielt gar nichts ab."""
    b = server.playback.ffmpeg_befehl("/x/a.mkv", _plan("hevc", "ac3"), "/t/o.teil")
    assert "-tag:v" in b and b[b.index("-tag:v") + 1] == "hvc1"


def test_h264_braucht_keinen_tag(server):
    b = server.playback.ffmpeg_befehl("/x/a.mkv", _plan("h264", "aac"), "/t/o.teil")
    assert "-tag:v" not in b


def test_webm_bekommt_keinen_mp4_tag(server):
    b = server.playback.ffmpeg_befehl("/x/a.mkv", _plan("vp9", "opus", ".webm"),
                                      "/t/o.teil")
    assert "-tag:v" not in b and "-movflags" not in b


def test_erlaubte_tonspur_bleibt_unangetastet(server):
    b = server.playback.ffmpeg_befehl("/x/a.mkv", _plan("h264", "aac"), "/t/o.teil")
    assert b[b.index("-c:a") + 1] == "copy"


def test_ac3_wird_nach_aac_gewandelt(server):
    """AC3 darf in einer MP4-Huelle nicht bleiben -- nur der Ton wird angefasst."""
    b = server.playback.ffmpeg_befehl("/x/a.mkv", _plan("hevc", "ac3"), "/t/o.teil")
    assert b[b.index("-c:a") + 1] == "aac"


def test_dts_wird_ebenfalls_gewandelt(server):
    b = server.playback.ffmpeg_befehl("/x/a.mkv", _plan("h264", "dts"), "/t/o.teil")
    assert b[b.index("-c:a") + 1] == "aac"


def test_ausgabeformat_wird_ausdruecklich_gesetzt(server):
    """Die Zwischendatei heisst '....teil' -- ffmpeg kennt die Endung nicht."""
    b = server.playback.ffmpeg_befehl("/x/a.mkv", _plan("h264", "aac"), "/t/o.teil")
    assert "-f" in b and b[b.index("-f") + 1] == "mp4"
    b = server.playback.ffmpeg_befehl("/x/a.mkv", _plan("vp9", "opus", ".webm"),
                                      "/t/o.teil")
    assert b[b.index("-f") + 1] == "webm"


def test_faststart_fuer_mp4(server):
    """Ohne faststart liegt der Index hinten und der Player kann nicht spulen."""
    b = server.playback.ffmpeg_befehl("/x/a.mkv", _plan("h264", "aac"), "/t/o.teil")
    assert "-movflags" in b and b[b.index("-movflags") + 1] == "+faststart"


def test_nur_ton_laesst_das_bild_weg(server):
    b = server.playback.ffmpeg_befehl("/x/a.mka", _plan("", "flac", ".m4a", True),
                                      "/t/o.teil")
    assert "-vn" in b and "-c:v" not in b


# ── Welche Codecs gelten als browsertauglich ───────────────────────────────
@pytest.mark.parametrize("codec,huelle,marke", [
    ("h264", ".mp4", "avc1"),
    ("hevc", ".mp4", "hvc1"),     # Standardausgabe des Konverters!
    ("h265", ".mp4", "hvc1"),
    ("av1", ".mp4", "av01"),
    ("vp9", ".webm", "vp9"),
    ("vp8", ".webm", "vp8"),
])
def test_zielhuelle_und_mime_je_codec(server, codec, huelle, marke):
    ziel, mime = server.playback._VIDEO_ZIEL[codec]
    assert ziel == huelle
    assert marke in mime


def test_unbekannter_codec_wird_ehrlich_abgelehnt(server):
    assert "wmv3" not in server.playback._VIDEO_ZIEL


# ── Der echte Lauf mit einem nachgebauten ffmpeg (Pruefung 17.09.2026) ──────
def _fake_ffmpeg(tmp_path, monkeypatch, skript):
    """Ein 'ffmpeg' vorn im PATH -- ein Shellskript, das tut, was der Test braucht.
    Das letzte Argument ist immer die Zieldatei."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    f = bin_dir / "ffmpeg"
    f.write_text("#!/bin/sh\nfor a; do ziel=$a; done\n" + skript, encoding="utf-8")
    f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def _lauf(server, ablage, plan, quelle, timeout=20):
    """_umpacken in einem Thread -- wie im Betrieb -- und hoechstens `timeout` s warten."""
    server.playback.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with server.playback._lock:
        server.playback._laeuft[ablage.name] = {"fortschritt": 0, "fehler": ""}
    th = threading.Thread(target=server.playback._umpacken,
                          args=(str(quelle), plan, ablage, 10 ** 9), daemon=True)
    th.start()
    th.join(timeout)
    return th


def test_umpacken_haengt_nicht_bei_viel_stderr(server, tmp_path, monkeypatch, medien):
    """stderr war eine Pipe, die erst NACH proc.wait gelesen wurde: ab 64 KB wartete
    ffmpeg auf den Leser und der Leser auf ffmpeg -- fuer immer."""
    _fake_ffmpeg(tmp_path, monkeypatch,
                 'printf "out_time_ms=5000000\\nprogress=end\\n"\n'
                 'dd if=/dev/zero bs=1000 count=300 2>/dev/null | tr "\\0" "E" >&2\n'
                 'printf "daten" > "$ziel"\n')
    ablage = server.playback.CACHE_DIR / "stderrflut.mp4"
    th = _lauf(server, ablage, _plan("h264", "aac"), medien["film.mkv"])
    try:
        assert not th.is_alive(), "Umpacken haengt -- stderr-Pipe voll"
        assert ablage.read_bytes() == b"daten"
        assert ablage.name not in server.playback._laeuft
        assert not ablage.with_suffix(".mp4.teil").exists()
    finally:
        ablage.unlink(missing_ok=True)
        server.playback._laeuft.pop(ablage.name, None)


def test_umpacken_zeitwaechter_toetet_haengendes_ffmpeg(server, tmp_path, monkeypatch, medien):
    """Ein ffmpeg, das nie fertig wird, wird nach UMPACK_TIMEOUT_S getoetet -- auch
    wenn der Thread gerade in der stdout-Schleife haengt."""
    _fake_ffmpeg(tmp_path, monkeypatch, "exec sleep 30\n")
    monkeypatch.setattr(server.playback, "UMPACK_TIMEOUT_S", 1)
    ablage = server.playback.CACHE_DIR / "haenger.mp4"
    t0 = time.time()
    th = _lauf(server, ablage, _plan("h264", "aac"), medien["film.mkv"])
    try:
        assert not th.is_alive() and time.time() - t0 < 15
        assert "abgebrochen" in server.playback._laeuft[ablage.name]["fehler"]
        assert not ablage.exists()
        assert not ablage.with_suffix(".mp4.teil").exists()
    finally:
        server.playback._laeuft.pop(ablage.name, None)


def test_umpacken_meldet_stderr_als_fehler(server, tmp_path, monkeypatch, medien):
    _fake_ffmpeg(tmp_path, monkeypatch, 'echo "Fehler: Datei kaputt" >&2\nexit 1\n')
    ablage = server.playback.CACHE_DIR / "kaputt.mp4"
    th = _lauf(server, ablage, _plan("h264", "aac"), medien["film.mkv"])
    try:
        assert not th.is_alive()
        assert "Datei kaputt" in server.playback._laeuft[ablage.name]["fehler"]
        assert not ablage.exists()
    finally:
        server.playback._laeuft.pop(ablage.name, None)


# ── Verwaiste .teil-Dateien ─────────────────────────────────────────────────
def test_verwaiste_teil_dateien_werden_geloescht(server):
    """Nach Absturz/Neustart mitten im Umpacken blieb die .teil-Datei ewig liegen --
    cache_leeren machte einen Bogen um jede .teil."""
    cd = server.playback.CACHE_DIR
    cd.mkdir(parents=True, exist_ok=True)
    verwaist = cd / "alt.mp4.teil"
    aktiv = cd / "laeuft.mp4.teil"
    verwaist.write_bytes(b"halb")
    aktiv.write_bytes(b"halb")
    with server.playback._lock:
        server.playback._laeuft["laeuft.mp4"] = {"fortschritt": 3, "fehler": ""}
    try:
        assert server.playback.verwaiste_teile_aufraeumen() == 1
        assert not verwaist.exists() and aktiv.exists()

        verwaist.write_bytes(b"halb")
        assert server.playback.cache_leeren() >= 4
        assert not verwaist.exists() and aktiv.exists()
    finally:
        server.playback._laeuft.pop("laeuft.mp4", None)
        aktiv.unlink(missing_ok=True)
