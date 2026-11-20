"""TV-Reparatur „TV-Ton ergänzen“ (24.09.2026).

Befund: Samsung-Fernseher ab 2023 (Tizen 7) spielen kein DTS/TrueHD. Die
Plex-App schickt DTS trotzdem per Direct Play → Film ohne Ton; nur den Ton
umrechnen lassen friert auf Tizen 7+ häufig ein. Lösung: eine AC3-5.1-Spur
aus der Haupttonspur erzeugen und als ERSTE + Standard-Spur einsetzen; Bild,
alle übrigen Spuren und Untertitel bleiben unverändert (nur umverpackt).
Pflicht des Betreibers: Ton und Bild bleiben synchron — geprüft über den
Inhalt (Lautstärke-Hüllkurve) beider Spuren an mehreren Stellen.
"""
import math

import pytest


def _a(index, codec, ch=6, lang="ger", default=False, **disp):
    d = {"default": 1 if default else 0}
    d.update(disp)
    return {"index": index, "codec": codec, "channels": ch, "lang": lang, "disp": d, "title": ""}


# ─── Plan: braucht die Datei eine TV-Tonspur? ────────────────────────────────
def test_dts_als_erste_deutsche_spur_wird_kodiert(server):
    p = server.tvton_plan([_a(1, "dts", 6, "ger", True), _a(2, "ac3", 6, "eng")])
    assert p["kodieren"] is True
    assert p["haupt"]["index"] == 1
    assert p["kanaele"] == 6
    assert [s["index"] for s in p["rest"]] == [1, 2], "Original-DTS bleibt dahinter erhalten"


def test_truehd_und_mlp_sind_untauglich(server):
    assert server.tvton_plan([_a(1, "truehd", 8)])["kodieren"] is True
    assert server.tvton_plan([_a(1, "mlp", 6)])["kodieren"] is True


def test_eac3_71_wird_auf_51_gebracht(server):
    p = server.tvton_plan([_a(1, "eac3", 8)])
    assert p["kodieren"] is True and p["kanaele"] == 6


def test_stereo_dts_bleibt_stereo(server):
    assert server.tvton_plan([_a(1, "dts", 2)])["kanaele"] == 2


def test_bereits_tv_taugliche_erste_spur_braucht_nichts(server):
    assert server.tvton_plan([_a(1, "ac3", 6, "ger", True), _a(2, "dts", 6, "ger")]) is None
    assert server.tvton_plan([_a(1, "eac3", 6)]) is None
    assert server.tvton_plan([_a(1, "aac", 2)]) is None


def test_keine_tonspur_braucht_nichts(server):
    assert server.tvton_plan([]) is None
    assert server.tvton_plan(None) is None


def test_deutsche_taugliche_spur_hinter_englischer_dts_wird_nur_vorgezogen(server):
    p = server.tvton_plan([_a(1, "dts", 6, "eng", True), _a(2, "ac3", 6, "ger")])
    assert p["kodieren"] is False
    assert p["haupt"]["index"] == 2
    assert [s["index"] for s in p["rest"]] == [1], "vorgezogene Spur nicht doppelt"


def test_ohne_deutsche_spur_zaehlt_die_erste(server):
    p = server.tvton_plan([_a(1, "dts", 6, "eng"), _a(2, "dts", 6, "fre")])
    assert p["haupt"]["index"] == 1 and p["kodieren"] is True


@pytest.mark.parametrize("lang", ["ger", "deu", "de", "GER"])
def test_sprachkennungen_deutsch(server, lang):
    p = server.tvton_plan([_a(1, "ac3", 6, "eng"), _a(2, "dts", 6, lang)])
    assert p["haupt"]["index"] == 2


def test_kommentar_und_audiodeskription_werden_nie_haupttonspur(server):
    """Prüfbefund W3."""
    p = server.tvton_plan([_a(1, "dts", 6, "eng", True), _a(2, "ac3", 6, "ger", False, visual_impaired=1),
                           _a(3, "dts", 6, "ger")])
    assert p["haupt"]["index"] == 3 and p["kodieren"] is True
    kommentar = _a(1, "ac3", 2, "ger", True, comment=1)
    p = server.tvton_plan([kommentar, _a(2, "dts", 6, "ger")])
    assert p is not None and p["haupt"]["index"] == 2, "Kommentar vorn darf nicht als tauglich gelten"
    titel = _a(1, "ac3", 2, "ger", True)
    titel["title"] = "Audiodeskription"
    assert server.tvton_plan([titel, _a(2, "dts", 6, "ger")])["haupt"]["index"] == 2


def test_standardmarkierung_auf_anderer_spur_wird_korrigiert(server):
    """Prüfbefund W4: vorn taugliches AC3 ohne Standard, dahinter DTS mit Standard."""
    p = server.tvton_plan([_a(1, "ac3", 6, "ger", False), _a(2, "dts", 6, "ger", True)])
    assert p is not None and p["kodieren"] is False and p["haupt"]["index"] == 1
    assert [s["index"] for s in p["rest"]] == [2]


def test_ohne_jede_standardmarkierung_bleibt_alles(server):
    assert server.tvton_plan([_a(1, "ac3", 6, "ger", False), _a(2, "dts", 6, "ger", False)]) is None


def test_vorhandene_taugliche_spur_derselben_sprache_wird_vorgezogen_statt_kodiert(server):
    """Prüfbefund H2."""
    p = server.tvton_plan([_a(1, "dts", 6, "ger", True), _a(2, "ac3", 6, "ger")])
    assert p["kodieren"] is False and p["haupt"]["index"] == 2


def test_bcp47_sprachcode(server):
    """Prüfbefund H3."""
    p = server.tvton_plan([_a(1, "dts", 6, "eng"), _a(2, "dts", 6, "de-DE")])
    assert p["haupt"]["index"] == 2


# ─── Befehl ─────────────────────────────────────────────────────────────────
def _cmd(server, plan):
    return server.cmd_tvton("/m/f.mkv", "/m/f.converting.mkv", "/tmp/p.log", plan)


def _nach(cmd, flag):
    return [cmd[i + 1] for i, x in enumerate(cmd) if x == flag]


def test_befehl_kodiert_neue_spur_zuerst_und_kopiert_alles_andere(server):
    plan = server.tvton_plan([_a(1, "dts", 8, "ger", True), _a(2, "ac3", 2, "eng", False, comment=1)])
    cmd = _cmd(server, plan)
    maps = _nach(cmd, "-map")
    assert maps[:4] == ["0:V", "0:1", "0:1", "0:2"], "neu (aus 0:1), dann alle Originale in Reihenfolge"
    assert "0:s?" in maps and "0:t?" in maps
    assert cmd[cmd.index("-c") + 1] == "copy", "Bild und Rest nur kopiert"
    assert cmd[cmd.index("-c:a:0") + 1] == "ac3"
    assert cmd[cmd.index("-b:a:0") + 1] == "640k"
    assert cmd[cmd.index("-ac:a:0") + 1] == "6", "7.1 → 5.1"
    assert "aresample=async=1" in cmd[cmd.index("-filter:a:0") + 1], "Lücken mit Stille füllen statt zusammenschieben"
    assert "-c:v" not in cmd, "Video wird NIE neu kodiert"
    # Standard-Flag nur auf der neuen Spur, andere Flags der Originale bleiben
    assert _nach(cmd, "-disposition:a:0") == ["default"]
    assert _nach(cmd, "-disposition:a:1") == ["0"], "Original-DTS verliert nur das Standard-Flag"
    assert _nach(cmd, "-disposition:a:2") == ["comment"]
    assert "language=ger" in _nach(cmd, "-metadata:s:a:0")
    assert "-map_metadata:s:a:0" not in cmd, "riss ffmpeg ab / löschte Sprachkennungen"
    assert cmd[-1] == "/m/f.converting.mkv"


def test_geerbte_statistik_tags_der_vorlage_werden_nur_auf_der_neuen_spur_geloescht(server):
    dts = _a(1, "dts", 6, "ger", True)
    dts["tags"] = ["BPS-eng", "DURATION-eng", "NUMBER_OF_BYTES", "_STATISTICS_TAGS", "language", "title"]
    cmd = _cmd(server, server.tvton_plan([dts]))
    werte = _nach(cmd, "-metadata:s:a:0")
    for k in ("BPS-eng=", "DURATION-eng=", "NUMBER_OF_BYTES=", "_STATISTICS_TAGS="):
        assert k in werte
    assert "language=" not in werte and "title=" not in werte
    assert not _nach(cmd, "-metadata:s:a:1"), "Original-DTS behält alle Tags"


def test_befehl_stereo_ohne_downmix_und_kleinere_bitrate(server):
    cmd = _cmd(server, server.tvton_plan([_a(1, "dts", 2)]))
    assert "-ac:a:0" not in cmd
    assert cmd[cmd.index("-b:a:0") + 1] == "256k"


def test_befehl_vorziehen_kodiert_nicht(server):
    plan = server.tvton_plan([_a(1, "dts", 6, "eng", True), _a(2, "ac3", 6, "ger")])
    cmd = _cmd(server, plan)
    assert _nach(cmd, "-map")[:3] == ["0:V", "0:2", "0:1"]
    assert "-c:a:0" not in cmd and "-filter:a:0" not in cmd
    assert "-map_metadata:s:a:0" not in cmd, "vorgezogene Spur behält ihre Tags"
    assert _nach(cmd, "-disposition:a:0") == ["default"]
    assert _nach(cmd, "-disposition:a:1") == ["0"]


def test_vorgezogene_spur_behaelt_ihre_uebrigen_markierungen(server):
    plan = server.tvton_plan([_a(1, "dts", 6, "eng", True), _a(2, "ac3", 6, "ger", False, original=1)])
    assert _nach(_cmd(server, plan), "-disposition:a:0") == ["default+original"]


def test_cover_werden_wieder_angehaengt(server):
    """Prüfbefund K2: Cover (attached_pic) gingen verloren."""
    plan = server.tvton_plan([_a(1, "dts", 6)])
    bilder = [{"pfad": "/m/f.converting.mkv.cover0.jpg", "mimetype": "image/jpeg", "filename": "cover.jpg"}]
    cmd = server.cmd_tvton("/m/f.mkv", "/m/f.converting.mkv", "/tmp/p.log", plan, bilder, n_att=2)
    i = cmd.index("-attach")
    assert cmd[i + 1] == "/m/f.converting.mkv.cover0.jpg"
    assert _nach(cmd, "-metadata:s:t:2") == ["mimetype=image/jpeg", "filename=cover.jpg"], "hinter 2 vorhandenen Anhängen"
    assert cmd.index("-attach") < len(cmd) - 1


def test_info_zaehlt_cover_getrennt(server):
    i = server.info_from_probe({"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc"},
        {"index": 1, "codec_type": "audio", "codec_name": "dts", "channels": 6,
         "tags": {"language": "ger", "DURATION": "01:30:00.500000000"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1},
         "tags": {"filename": "cover.jpg", "mimetype": "image/jpeg"}},
        {"index": 3, "codec_type": "attachment", "codec_name": "ttf"},
    ], "format": {}})
    assert i["n_video"] == 1 and i["n_cover"] == 1 and i["n_att"] == 1
    assert i["covers"] == [{"index": 2, "codec": "mjpeg", "filename": "cover.jpg", "mimetype": "image/jpeg"}]
    assert i["audio_streams"][0]["dauer"] == pytest.approx(5400.5)


# ─── Spuren-Prüfung nach dem Umverpacken ────────────────────────────────────
def _info(v=1, audio=(), s=2, att=1, dur=6000.0):
    return {"n_video": v, "n_audio": len(audio), "n_sub": s, "n_att": att, "dur": dur,
            "audio_streams": list(audio)}


def test_pruefung_ok(server):
    plan = server.tvton_plan([_a(1, "dts", 6, "ger", True)])
    vorher = _info(audio=[_a(1, "dts", 6, "ger", True)])
    nachher = _info(audio=[_a(1, "ac3", 6, "ger", True), _a(2, "dts", 6, "ger")], dur=6000.1)
    assert server.tvton_spuren_ok(vorher, nachher, plan) == (True, "")


def test_pruefung_findet_verlorenes_cover_und_kurze_neue_spur(server):
    plan = server.tvton_plan([_a(1, "dts", 6, "ger", True)])
    vorher = _info(audio=[_a(1, "dts", 6, "ger", True)])
    vorher["n_cover"] = 1
    nachher = _info(audio=[_a(1, "ac3", 6, "ger", True), _a(2, "dts", 6, "ger")])
    ok, err = server.tvton_spuren_ok(vorher, nachher, plan)
    assert not ok and "Cover" in err
    nachher["n_cover"] = 1
    nachher["audio_streams"][0]["dauer"], nachher["audio_streams"][1]["dauer"] = 5200.0, 6000.0
    ok, err = server.tvton_spuren_ok(vorher, nachher, plan)
    assert not ok and "lang" in err


@pytest.mark.parametrize("aendern,fehlerteil", [
    (dict(s=1), "Untertitel"),
    (dict(att=0), "Anhänge"),
    (dict(v=0), "Video"),
    (dict(dur=5000.0), "zu kurz"),
    (dict(audio=[_a(1, "dts", 6, "ger", True), _a(2, "ac3", 6, "ger")]), "erste Tonspur"),
    (dict(audio=[_a(1, "ac3", 6, "ger", True), _a(2, "dts", 6, "ger", True)]), "Standard"),
    (dict(audio=[_a(1, "ac3", 6, "ger", True)]), "Tonspuren"),
])
def test_pruefung_findet_fehler(server, aendern, fehlerteil):
    plan = server.tvton_plan([_a(1, "dts", 6, "ger", True)])
    vorher = _info(audio=[_a(1, "dts", 6, "ger", True)])
    basis = dict(audio=[_a(1, "ac3", 6, "ger", True), _a(2, "dts", 6, "ger")])
    basis.update(aendern)
    ok, err = server.tvton_spuren_ok(vorher, _info(**basis), plan)
    assert not ok and fehlerteil in err


# ─── Synchron-Prüfung: Versatz aus zwei Hüllkurven ──────────────────────────
def _huelle(n=3200, verschiebung=0, seed=7):
    """Sprachähnliche Hüllkurve: unregelmäßige Silben, deterministisch."""
    werte, x = [], seed
    for i in range(n + 200):
        x = (x * 1103515245 + 12345) % 2 ** 31
        werte.append(abs(math.sin(i / 9.0)) * (x % 1000) / 1000.0)
    return werte[100 + verschiebung:100 + verschiebung + n]


@pytest.mark.parametrize("verschiebung", [0, 3, -5, 8, -12])
def test_versatz_wird_blockgenau_erkannt(server, verschiebung):
    ref = _huelle()
    neu = _huelle(verschiebung=verschiebung)
    lag_ms, korr = server.versatz_ms(ref, neu, block_ms=2.5, max_ms=100)
    assert lag_ms == pytest.approx(verschiebung * 2.5)
    assert korr > 0.9


def test_versatz_bei_stille_nicht_bewertbar(server):
    lag_ms, korr = server.versatz_ms([0.0] * 3200, [0.0] * 3200, block_ms=2.5, max_ms=100)
    assert korr == 0.0


L, S = 500.0, 5.0     # Pegel: deutlich laut / still


def test_sync_urteil_gut(server):
    m = [(0.0, 0.95, L, L, True), (2.5, 0.9, L, L, True), (0.0, 0.0, S, S, True), (-2.5, 0.97, L, L, True)]
    ok, text = server.sync_urteil(m)
    assert ok is True and "3 Stellen" in text


def test_sync_urteil_versatz(server):
    ok, text = server.sync_urteil([(0.0, 0.95, L, L, True), (25.0, 0.9, L, L, True), (0.0, 0.9, L, L, True)])
    assert ok is False and "25" in text


def test_sync_urteil_teilversatz_ab_filmmitte_wird_nicht_uebergangen(server):
    """Prüfbefund K3: laute Stellen mit schlechter Übereinstimmung fielen früher heraus."""
    m = [(15.0, 1.0, L, L, True), (15.0, 0.99, L, L, True), (-87.5, 0.09, L, L, True), (-50.0, 0.09, L, L, True)]
    ok, text = server.sync_urteil(m)
    assert ok is False and "passt nicht" in text


def test_sync_urteil_neue_spur_stumm_ab_filmmitte(server):
    m = [(0.0, 1.0, L, L, True), (0.0, 1.0, L, L, True), (0.0, 0.0, L, S, True), (0.0, 0.0, L, S, True)]
    ok, text = server.sync_urteil(m)
    assert ok is False and "stumm" in text


def test_sync_urteil_messung_fehlgeschlagen(server):
    ok, text = server.sync_urteil([(0.0, 1.0, L, L, True), (0.0, 0.0, 0.0, 0.0, False), (0.0, 1.0, L, L, True)])
    assert ok is False and "fehlgeschlagen" in text


def test_sync_urteil_zu_wenig_bewertbar(server):
    ok, text = server.sync_urteil([(0.0, 0.95, L, L, True), (0.0, 0.0, S, S, True), (0.0, 0.0, S, S, True)])
    assert ok is False and "nicht prüfbar" in text


def test_sync_stellen_nie_am_dateianfang(server):
    """Prüfbefund H1: bei 0,0 s maß der Vergleich ~27 ms Schein-Versatz."""
    stellen, fenster, mindest = server.tv_sync_stellen(40.0)
    assert min(stellen) >= 1.0 and fenster <= 12.0 and mindest == 1
    stellen, fenster, mindest = server.tv_sync_stellen(6000.0)
    assert len(stellen) == 6 and min(stellen) >= 1.0 and max(stellen) + fenster < 6000 and mindest == 2


def test_dekodierfehler_werden_gezaehlt(server):
    log = ("[dca @ 0x1] Invalid frame\nError while decoding stream #0:1: Invalid data found\n"
           "frame= 10 fps=0\n")
    assert server.tv_dekodierfehler(log) == 2
    assert server.tv_dekodierfehler("") == 0


def test_platzpruefung(server, tmp_path):
    assert server.tv_platz_ok(str(tmp_path), 1)[0] is True
    ok, text = server.tv_platz_ok(str(tmp_path), 10 ** 18)
    assert ok is False and "Platz" in text


# ─── API / Vorabanalyse ─────────────────────────────────────────────────────
def test_codecs_liefert_tv_reparatur_mit_erklaerung(logged_in):
    d = logged_in.get("/api/codecs").get_json()
    assert "tvton" in d["tvfix"]
    assert d["tvfix"]["tvton"]["hint"], "jede Einstellung braucht eine Erklärung"


def test_prescan_tvfix_entscheidet_nach_tonspur(server, monkeypatch, tmp_path):
    f = tmp_path / "film.mkv"
    f.write_bytes(b"\x1aE\xdf\xa3" + b"x" * 100)
    info = {"vc": "hevc", "ac": "dts", "dur": 10.0, "n_video": 1, "n_audio": 1, "n_sub": 0,
            "n_att": 0, "sub_streams": [], "audio_streams": [_a(1, "dts", 6, "ger", True)]}
    monkeypatch.setattr(server, "get_info", lambda p: info)
    kind, item = server.prescan_check(str(f), str(tmp_path), "tvton", False, ctype="tvfix")
    assert kind == "convert" and "dts" in item["codec"]
    info["audio_streams"] = [_a(1, "ac3", 6, "ger", True)]
    assert server.prescan_check(str(f), str(tmp_path), "tvton", False, ctype="tvfix")[0] == "skip"
    mp4 = tmp_path / "film.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x18ftyp" + b"x" * 100)
    info["audio_streams"] = [_a(1, "dts", 6, "ger", True)]
    kind, item = server.prescan_check(str(mp4), str(tmp_path), "tvton", False, ctype="tvfix")
    assert kind == "skip" and "MKV" in item["codec"]


def test_convert_nimmt_tvfix_an(logged_in, server, dirs, monkeypatch):
    import os
    import threading
    d = os.path.join(dirs["media"], "tvfix")
    os.makedirs(d, exist_ok=True)
    mkv = os.path.join(d, "a.mkv")
    open(mkv, "wb").write(b"\x1aE\xdf\xa3xx")
    open(os.path.join(d, "b.mp3"), "wb").write(b"ID3xx")
    gate, gesehen = threading.Event(), []
    monkeypatch.setattr(server, "run_task", lambda t: (gesehen.append(t), gate.wait(3)))
    r = logged_in.post("/api/convert", json={"folder": "tvfix", "codec": "tvton", "type": "tvfix"})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["total"] == 1, "nur Videodateien"
    try:
        import time
        frist = time.time() + 3
        while time.time() < frist and not gesehen:
            time.sleep(0.02)
        t = gesehen[0]
        assert t["type"] == "tvfix" and t["container"] == "mkv" and t["target"] == "tvton"
        assert t["target_label"].startswith("TV-Ton")
    finally:
        gate.set()
    assert logged_in.post("/api/convert", json={"folder": "tvfix", "codec": "h265",
                                                "type": "tvfix"}).status_code == 400
