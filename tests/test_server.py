import os
import json
import time

import pytest


# ─── Befehls-Builder ─────────────────────────────────────────────────────────
def _after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


SUBS_MIX = [{"index": 2, "codec": "subrip"}, {"index": 3, "codec": "mov_text"},
            {"index": 4, "codec": "eia_608"}]


@pytest.mark.parametrize("builder", ["cmd_vaapi_hwaccel", "cmd_vaapi_upload", "cmd_cpu_video"])
def test_video_builders_map_all_tracks(server, builder):
    cmd = getattr(server, builder)("/m/a.mp4", "/m/a.converting.mkv", "h265", "/tmp/p.log",
                                   sub_streams=[{"index": 2, "codec": "subrip"}])
    joined = " ".join(cmd)
    assert "-map 0:V:0" in joined          # großes V: Cover-Art bleibt außen vor (S16)
    assert "-map 0:v:0" not in joined
    assert "-map 0:a?" in joined
    assert "-map 0:t?" in joined
    assert "-map 0:s?" not in joined       # Untertitel werden einzeln gemappt (S1)
    assert "-map 0:2 -c:s:0 copy" in joined
    assert "-c:a copy" in joined and "-c:t copy" in joined
    assert "-map_metadata 0" in joined and "-map_chapters 0" in joined
    assert cmd.index("-i") < cmd.index("-map")
    assert cmd[-1] == "/m/a.converting.mkv"


@pytest.mark.parametrize("builder", ["cmd_vaapi_hwaccel", "cmd_vaapi_upload", "cmd_cpu_video"])
def test_video_builders_without_subtitles(server, builder):
    """Ohne Untertitel darf gar keine -c:s-Option auftauchen (leerer Ausgabestream)."""
    cmd = getattr(server, builder)("/m/a.mp4", "/m/a.mkv", "h265", "/tmp/p.log")
    joined = " ".join(cmd)
    assert "-c:s" not in joined and "-map 0:s" not in joined
    assert "-map 0:V:0" in joined and "-map 0:a?" in joined


@pytest.mark.parametrize("quality,qp", [("fast", "26"), ("balanced", "22"), ("high", "18")])
def test_vaapi_qp_per_quality(server, quality, qp):
    for b in (server.cmd_vaapi_hwaccel, server.cmd_vaapi_upload):
        for target in ("h265", "h264"):
            cmd = b("s", "d", target, "p", quality=quality)
            assert _after(cmd, "-qp") == qp
            assert "-rc_mode" not in cmd and "-global_quality" not in cmd


@pytest.mark.parametrize("target,quality,gq", [
    ("vp9", "fast", "150"), ("vp9", "balanced", "110"), ("vp9", "high", "80"),
    ("av1", "fast", "160"), ("av1", "balanced", "130"), ("av1", "high", "100"),
])
def test_vaapi_global_quality_for_vp9_av1(server, target, quality, gq):
    """S2: vp9_vaapi/av1_vaapi kennen kein -qp – dort muss -rc_mode CQP + -global_quality stehen."""
    for b in (server.cmd_vaapi_hwaccel, server.cmd_vaapi_upload):
        cmd = b("s", "d", target, "p", quality=quality)
        assert "-qp" not in cmd
        assert _after(cmd, "-rc_mode") == "CQP"
        assert _after(cmd, "-global_quality") == gq


def test_vaapi_no_low_power(server):
    """Auf dieser GPU gemessen: -low_power ist für VP9 nicht nötig und wird nicht gesetzt."""
    for t in ("vp9", "av1", "h265"):
        assert "-low_power" not in server.cmd_vaapi_upload("s", "d", t, "p")


def test_vaapi_default_quality_balanced(server):
    assert _after(server.cmd_vaapi_hwaccel("s", "d", "h264", "p"), "-qp") == "22"


@pytest.mark.parametrize("target,quality,crf", [
    ("h264", "fast", "26"), ("h264", "balanced", "22"), ("h264", "high", "18"),
    ("h265", "fast", "28"), ("h265", "balanced", "24"), ("h265", "high", "20"),
    ("av1", "fast", "34"), ("av1", "balanced", "30"), ("av1", "high", "26"),
    ("vp9", "fast", "34"), ("vp9", "balanced", "30"), ("vp9", "high", "26"),
])
def test_cpu_crf_per_quality(server, target, quality, crf):
    cmd = server.cmd_cpu_video("s", "d", target, "p", quality=quality)
    assert _after(cmd, "-crf") == crf
    assert _after(cmd, "-c:v") == server.VID_CODECS[target]["cpu"]


def test_cpu_opts_kept(server):
    cmd = server.cmd_cpu_video("s", "d", "h265", "p")
    assert _after(cmd, "-preset") == "medium"
    cmd = server.cmd_cpu_video("s", "d", "vp9", "p")
    assert _after(cmd, "-b:v") == "0"


@pytest.mark.parametrize("quality,qv", [("fast", "6"), ("balanced", "4"), ("high", "2")])
def test_mpeg4_qv(server, quality, qv):
    cmd = server.cmd_cpu_video("s", "d", "mpeg4", "p", quality=quality)
    assert _after(cmd, "-q:v") == qv
    assert "-crf" not in cmd


def test_upload_10bit_p010_main10(server):
    cmd = server.cmd_vaapi_upload("s", "d", "h265", "p", pix10=True)
    assert _after(cmd, "-vf") == "format=p010,hwupload"
    assert _after(cmd, "-profile:v") == "main10"
    for t in ("vp9", "av1"):
        cmd = server.cmd_vaapi_upload("s", "d", t, "p", pix10=True)
        assert _after(cmd, "-vf") == "format=p010,hwupload"
        assert "-profile:v" not in cmd


def test_upload_8bit_and_h264_nv12(server):
    assert _after(server.cmd_vaapi_upload("s", "d", "h265", "p", pix10=False), "-vf") == "format=nv12,hwupload"
    cmd = server.cmd_vaapi_upload("s", "d", "h264", "p", pix10=True)
    assert _after(cmd, "-vf") == "format=nv12,hwupload"
    assert "-profile:v" not in cmd


def test_audio_builder(server):
    cmd = server.cmd_audio("s.wav", "d.mp3", "mp3", "p")
    assert "-vn" in cmd
    assert "-map_metadata 0" in " ".join(cmd)
    assert "-map 0:a?" not in " ".join(cmd)
    assert _after(cmd, "-c:a") == "libmp3lame"


def test_all_video_targets_mkv(server):
    for k, v in server.VID_CODECS.items():
        assert (v.get("ext") or ".mkv") == ".mkv", k
    assert server.VID_CODECS["av1"]["vaapi"] == "av1_vaapi"


# ─── S1: Untertitel-Behandlung ───────────────────────────────────────────────
def test_subtitle_args_copy(server):
    args, mapped, dropped = server.subtitle_args(
        [{"index": 2, "codec": "subrip"}, {"index": 3, "codec": "ass"},
         {"index": 5, "codec": "hdmv_pgs_subtitle"}])
    assert args == ["-map", "0:2", "-c:s:0", "copy",
                    "-map", "0:3", "-c:s:1", "copy",
                    "-map", "0:5", "-c:s:2", "copy"]
    assert mapped == 3 and dropped == []


def test_subtitle_args_to_srt(server):
    """mov_text (jede MP4 mit Untertiteln) lässt den MKV-Muxer abbrechen → nach srt wandeln."""
    args, mapped, dropped = server.subtitle_args(
        [{"index": 2, "codec": "mov_text"}, {"index": 4, "codec": "tx3g"}])
    assert args == ["-map", "0:2", "-c:s:0", "srt", "-map", "0:4", "-c:s:1", "srt"]
    assert mapped == 2 and dropped == []


def test_subtitle_args_dropped(server):
    for bad in ("eia_608", "eia_708", "dvb_teletext", "hdmv_text_subtitle", "unknown", ""):
        args, mapped, dropped = server.subtitle_args([{"index": 2, "codec": bad}])
        assert args == [] and mapped == 0
        assert dropped == [bad or "unknown"]
    assert server.subtitle_args([]) == ([], 0, [])
    assert server.subtitle_args(None) == ([], 0, [])
    # ohne Stream-Index lässt sich nichts mappen
    assert server.subtitle_args([{"codec": "subrip"}]) == ([], 0, ["subrip"])


def test_subtitle_args_mixed_numbering(server):
    """Gemischt: die Ausgabe-Nummerierung -c:s:N zählt nur die tatsächlich gemappten Spuren."""
    args, mapped, dropped = server.subtitle_args(SUBS_MIX)
    assert args == ["-map", "0:2", "-c:s:0", "copy", "-map", "0:3", "-c:s:1", "srt"]
    assert mapped == 2 and dropped == ["eia_608"]


@pytest.mark.parametrize("builder", ["cmd_vaapi_hwaccel", "cmd_vaapi_upload", "cmd_cpu_video"])
def test_video_builders_mixed_subtitles(server, builder):
    cmd = getattr(server, builder)("s", "d", "h265", "p", sub_streams=SUBS_MIX)
    joined = " ".join(cmd)
    assert "-map 0:2 -c:s:0 copy" in joined
    assert "-map 0:3 -c:s:1 srt" in joined
    assert "0:4" not in joined           # eia_608 wird gar nicht gemappt
    assert cmd[-1] == "d"


def test_info_from_probe_sub_streams(server):
    i = server.info_from_probe({"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        {"index": 2, "codec_type": "subtitle", "codec_name": "mov_text"},
        {"index": 3, "codec_type": "subtitle", "codec_name": "eia_608"},
    ], "format": {}})
    assert i["n_sub"] == 2
    assert i["sub_streams"] == [{"index": 2, "codec": "mov_text"}, {"index": 3, "codec": "eia_608"}]
    assert server.subtitle_args(i["sub_streams"])[1] == 1


# ─── S14/S15: 10-bit-Erkennung und GPU-Versuche ──────────────────────────────
@pytest.mark.parametrize("pf,bits,expected", [
    ("yuv420p10le", None, True), ("yuv420p12be", None, True), ("p010le", None, True),
    ("p012le", None, True), ("yuv422p10le", None, True),
    ("yuv410p", None, False),           # die alte "10 in pix_fmt"-Heuristik lag hier falsch
    ("yuv420p", None, False), ("nv12", None, False), ("", None, False), (None, None, False),
    ("yuv420p", "10", True),            # Fallback über bits_per_raw_sample
    ("yuv420p", "8", False), ("yuv420p", "krumm", False),
])
def test_is_10bit(server, pf, bits, expected):
    assert server.is_10bit(pf, bits) is expected


def test_info_from_probe_bits_fallback(server):
    i = server.info_from_probe({"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264",
         "pix_fmt": "yuv410p", "bits_per_raw_sample": "8"}], "format": {}})
    assert i["is10"] is False and i["pix_fmt"] == "yuv410p"


def _methods(atts):
    return [m for m, _ in atts]


def test_gpu_attempts_default(server):
    info = {"is10": False}
    caps = {"h264": True, "hevc": True, "hevc10": True, "vp9": True, "av1": False}
    assert _methods(server.gpu_attempts("s", "d", "h265", "p", "balanced", info, [], caps)) == ["GPU", "GPU*"]


def test_gpu_attempts_10bit_h264_skips_hwaccel(server):
    """S14: 10-bit → H.264 scheitert im vollen Hardware-Pfad, der Upload-Pfad klappt."""
    info = {"is10": True}
    caps = {"h264": True, "hevc": True, "hevc10": True, "vp9": True, "av1": False}
    atts = server.gpu_attempts("s", "d", "h264", "p", "balanced", info, [], caps)
    assert _methods(atts) == ["GPU*"]
    assert _after(atts[0][1], "-vf") == "format=nv12,hwupload"


def test_gpu_attempts_10bit_hevc_needs_main10(server):
    """S15: ohne HEVC-Main10-Encode-Profil ist jeder GPU-Versuch verlorene Zeit."""
    info = {"is10": True}
    no10 = {"h264": True, "hevc": True, "hevc10": False, "vp9": True, "av1": False}
    assert server.gpu_attempts("s", "d", "h265", "p", "balanced", info, [], no10) == []
    with10 = dict(no10, hevc10=True)
    atts = server.gpu_attempts("s", "d", "h265", "p", "balanced", info, [], with10)
    assert _methods(atts) == ["GPU", "GPU*"]
    assert _after(atts[1][1], "-profile:v") == "main10"
    # 8-bit-Quelle ist von der Main10-Frage nicht betroffen
    assert _methods(server.gpu_attempts("s", "d", "h265", "p", "balanced", {"is10": False}, [], no10)) == ["GPU", "GPU*"]


def test_gpu_attempts_pass_subtitles(server):
    atts = server.gpu_attempts("s", "d", "h265", "p", "balanced", {"is10": False}, SUBS_MIX,
                               {"hevc10": True})
    for _m, cmd in atts:
        assert "-map 0:3 -c:s:1 srt" in " ".join(cmd)


# ─── S3: Zielpfad-Kollisionen ────────────────────────────────────────────────
def test_target_path(server):
    assert server.target_path("/m/a.mp4", ".mkv") == "/m/a.mkv"
    assert server.target_path("/m/a.mkv", ".mkv") == "/m/a.mkv"


def test_find_target_collisions(server):
    paths = ["/m/a.mkv", "/m/a.mp4", "/m/b.avi", "/m/s/a.mp4"]
    c = server.find_target_collisions(paths, ".mkv")
    assert c == {"/m/a.mp4": "a.mkv"}          # die In-place-Datei behält den Zielnamen
    assert server.find_target_collisions(["/m/x.mp4", "/m/x.avi"], ".mkv") == {"/m/x.avi": "x.mp4"}
    assert server.find_target_collisions(["/m/a.mkv", "/m/b.mkv"], ".mkv") == {}


# ─── Spuren-Verifikation ─────────────────────────────────────────────────────
def test_streams_ok_good(server):
    ok, msg = server.streams_ok({"v": 1, "a": 2, "s": 1}, {"v": 1, "a": 2, "s": 1, "size": 10}, 100, 99.5)
    assert ok and msg == ""


def test_streams_ok_more_is_fine(server):
    ok, _ = server.streams_ok({"v": 1, "a": 1, "s": 0}, {"v": 1, "a": 2, "s": 1, "size": 10}, 0, 0)
    assert ok


def test_streams_missing_audio(server):
    ok, msg = server.streams_ok({"v": 1, "a": 2, "s": 1}, {"v": 1, "a": 1, "s": 1, "size": 10}, 100, 100)
    assert not ok
    assert msg == "Spuren fehlen nach Encode (Audio 2→1, Untertitel 1→1)"


def test_streams_missing_sub(server):
    ok, msg = server.streams_ok({"v": 1, "a": 1, "s": 3}, {"v": 1, "a": 1, "s": 1, "size": 10}, 100, 100)
    assert not ok and "Untertitel 3→1" in msg


def test_streams_missing_video(server):
    ok, msg = server.streams_ok({"v": 1, "a": 1, "s": 0}, {"v": 0, "a": 1, "s": 0, "size": 10}, 100, 100)
    assert not ok and "Video" in msg


def test_streams_empty_output(server):
    ok, msg = server.streams_ok({"v": 1, "a": 1, "s": 0}, {"v": 1, "a": 1, "s": 0, "size": 0}, 100, 100)
    assert not ok and "leer" in msg


def test_streams_too_short(server):
    ok, msg = server.streams_ok({"v": 1, "a": 1, "s": 0}, {"v": 1, "a": 1, "s": 0, "size": 5}, 2530, 721)
    assert not ok
    assert msg == "Ausgabe zu kurz (12:01 statt 42:10)"


def test_streams_duration_boundary(server):
    b = {"v": 1, "a": 1, "s": 0}
    a = {"v": 1, "a": 1, "s": 0, "size": 5}
    assert server.streams_ok(b, a, 100, 98)[0]
    assert not server.streams_ok(b, a, 100, 97.9)[0]
    assert not server.streams_ok(b, a, 100, 0)[0]


def test_streams_audio_mode_ignores_video(server):
    ok, _ = server.streams_ok({"v": 1, "a": 1, "s": 0}, {"v": 0, "a": 1, "s": 0, "size": 5}, 10, 10, is_video=False)
    assert ok
    ok, _ = server.streams_ok({"v": 0, "a": 1, "s": 0}, {"v": 0, "a": 0, "s": 0, "size": 5}, 10, 10, is_video=False)
    assert not ok


def test_info_from_probe(server):
    info = {"streams": [
        {"codec_type": "video", "codec_name": "H264", "pix_fmt": "yuv420p10le"},
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        {"codec_type": "audio", "codec_name": "aac"},
        {"codec_type": "audio", "codec_name": "ac3"},
        {"codec_type": "subtitle", "codec_name": "subrip"},
    ], "format": {"duration": "12.5", "size": "777"}}
    i = server.info_from_probe(info)
    assert i["vc"] == "h264" and i["ac"] == "aac"
    assert i["n_video"] == 1 and i["n_audio"] == 2 and i["n_sub"] == 1
    assert i["is10"] is True and i["pix_fmt"] == "yuv420p10le"
    assert i["dur"] == 12.5 and i["size"] == 777
    empty = server.info_from_probe(None)
    assert empty["vc"] is None and empty["dur"] == 0.0 and empty["is10"] is False


# ─── Progress-Parser ─────────────────────────────────────────────────────────
def test_read_progress(server, tmp_path):
    p = tmp_path / "p.log"
    p.write_text("frame=10\nfps=20.5\nout_time_us=1000\nspeed=0.9x\nprogress=continue\n"
                 "frame=20\nfps=41.0\nout_time_us=2000000\nspeed=1.53x\nprogress=continue\n")
    r = server.read_progress(str(p))
    assert r == {"us": 2000000, "fps": 41.0, "speed": 1.53}


def test_read_progress_missing(server, tmp_path):
    assert server.read_progress(str(tmp_path / "nope")) == {"us": 0, "fps": 0.0, "speed": 0.0}


def test_parse_progress_garbage(server):
    r = server.parse_progress("fps=abc\nspeed=N/A\nout_time_us=-5\nout_time_us=x\n")
    assert r == {"us": 0, "fps": 0.0, "speed": 0.0}


# ─── LoginGuard ──────────────────────────────────────────────────────────────
class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_guard_lock_after_5(server):
    clk = Clock()
    g = server.LoginGuard(now=clk)
    for _ in range(4):
        g.fail("a")
        assert g.retry_after("a") == 0
    g.fail("a")
    assert g.retry_after("a") == 60
    assert g.retry_after("b") == 0
    clk.t += 61
    assert g.retry_after("a") == 0


def test_guard_doubling_and_cap(server):
    clk = Clock()
    g = server.LoginGuard(now=clk)
    expected = [60, 120, 240, 480, 900, 900]
    for exp in expected:
        for _ in range(5):
            g.fail("a")
        assert g.retry_after("a") == exp
        clk.t += exp + 1


def test_guard_reset_on_success(server):
    clk = Clock()
    g = server.LoginGuard(now=clk)
    for _ in range(5):
        g.fail("a")
    assert g.retry_after("a") > 0
    g.success("a")
    assert g.retry_after("a") == 0
    for _ in range(5):
        g.fail("a")
    assert g.retry_after("a") == 60    # no doubling after reset


def test_guard_window_expiry(server):
    clk = Clock()
    g = server.LoginGuard(now=clk)
    for _ in range(4):
        g.fail("a")
    clk.t += 601
    g.fail("a")
    assert g.retry_after("a") == 0


def test_guard_global(server):
    clk = Clock()
    g = server.LoginGuard(now=clk)
    for i in range(30):
        g.fail(f"ip{i}")
    assert g.retry_after("fresh") == 0
    g.fail("ip31")
    assert g.retry_after("fresh") == 60
    clk.t += 61
    assert g.retry_after("fresh") == 0


# ─── History-Migration / JSONL ──────────────────────────────────────────────
def test_history_migrated_at_import(server, dirs, legacy_history):
    cfg = dirs["config"]
    assert not os.path.exists(os.path.join(cfg, "history.json"))
    assert os.path.exists(os.path.join(cfg, "history.json.migrated"))
    assert server.HISTORY_FILE == os.path.join(cfg, "history.jsonl")
    entries = server.load_history()
    assert len(entries) == 3
    assert entries[0]["run_id"] == "legacy-20260810"
    assert entries[1]["run_id"] == "legacy-20260810"
    assert entries[2]["run_id"] == "legacy-20260812"
    for k in ("run_id", "name", "path", "rel_dir", "size_before", "size_after", "method",
              "target", "quality", "timestamp", "duration_s", "speed"):
        assert k in entries[0]


def test_migrate_history_function(server, tmp_path):
    legacy = tmp_path / "history.json"
    jsonl = tmp_path / "history.jsonl"
    legacy.write_text(json.dumps([{"name": "x", "timestamp": 0}, "junk"]))
    assert server.migrate_history(str(legacy), str(jsonl)) == 1
    assert not legacy.exists() and (tmp_path / "history.json.migrated").exists()
    assert server.migrate_history(str(legacy), str(jsonl)) == 0   # idempotent
    # existing jsonl → no migration even if legacy exists
    legacy.write_text("[]")
    assert server.migrate_history(str(legacy), str(jsonl)) == 0
    assert legacy.exists()


def test_history_runs(server):
    entries = [
        {"run_id": "r1", "timestamp": 20, "target": "h265", "size_before": 10, "size_after": 4},
        {"run_id": "r1", "timestamp": 10, "target": "h265", "size_before": 10, "size_after": 6},
        {"run_id": "r2", "timestamp": 30, "target": "h264", "size_before": 5, "size_after": 5},
    ]
    runs = server.history_runs(entries)
    assert [r["run_id"] for r in runs] == ["r2", "r1"]
    r1 = runs[1]
    assert r1 == {"run_id": "r1", "timestamp": 10, "target": "h265", "count": 2, "bytes_before": 20, "bytes_after": 10}


# ─── Byte-gewichteter Fortschritt ────────────────────────────────────────────
def test_calc_progress_byte_weighted(server):
    files = [
        {"status": "done", "size_before": 800, "size_after": 300},
        {"status": "converting", "size_before": 200, "progress": 50},
        {"status": "pending", "size_before": 1000},
    ]
    r = server.calc_progress(files, 2000, 10.0)
    assert r["progress"] == 45.0          # (800 + 100) / 2000
    assert r["bytes_done"] == 900
    assert r["eta"] == 12                 # 1100 left at 90 B/s
    assert r["bytes_before_done"] == 800 and r["bytes_after_done"] == 300 and r["bytes_saved"] == 500


def test_calc_progress_edge(server):
    r = server.calc_progress([], 0, 5)
    assert r["progress"] == 0 and r["eta"] == 0
    files = [{"status": "skipped", "size_before": 5, "size_after": 5}, {"status": "error", "size_before": 5}]
    r = server.calc_progress(files, 10, 1)
    assert r["progress"] == 100.0 and r["eta"] == 0
    assert r["bytes_before_done"] == 5      # errors not counted in before/after
    r = server.calc_progress([{"status": "done", "size_before": 0, "size_after": 0}, {"status": "pending", "size_before": 0}], 0, 1)
    assert r["progress"] == 50.0            # fallback: count-based when no bytes


# ─── SYSTEM_DIRS-Filter ──────────────────────────────────────────────────────
def test_split_dirs(server):
    names = ["Filme", "etc", "@eaDir", ".hidden", "usr", "Serien", "#recycle"]
    folders, system = server.split_dirs(names, True)
    assert folders == ["Filme", "Serien"]
    assert system == ["#recycle", ".hidden", "@eaDir", "etc", "usr"]
    folders, system = server.split_dirs(names, False)
    assert folders == ["#recycle", "@eaDir", "Filme", "Serien", "etc", "usr"]
    assert system == [".hidden"]


def test_folders_endpoint(server, logged_in, dirs):
    media = dirs["media"]
    for d in ("Filme", "etc", ".snap"):
        os.makedirs(os.path.join(media, d), exist_ok=True)
    os.makedirs(os.path.join(media, "Filme", "etc"), exist_ok=True)
    with open(os.path.join(media, "bzImage"), "wb") as f:
        f.write(b"x")
    with open(os.path.join(media, "Filme", "a.mkv"), "wb") as f:
        f.write(b"abcd")
    r = logged_in.get("/api/folders?path=").get_json()
    assert r["folders"] == ["Filme"] and r["system"] == [".snap", "etc"]
    assert r["current"] == "" and r["media_count"] == 0
    r = logged_in.get("/api/folders?path=Filme").get_json()
    assert r["folders"] == ["etc"] and r["system"] == []
    assert r["media_count"] == 1 and r["media_bytes"] == 4
    assert logged_in.get("/api/folders?path=../..").status_code == 400
    r = logged_in.post("/api/scan", json={"folder": "Filme"}).get_json()
    assert r["total"] == 1 and r["bytes"] == 4


# ─── delete-file ─────────────────────────────────────────────────────────────
def test_delete_file_rules(server, logged_in, dirs):
    media = dirs["media"]
    txt = os.path.join(media, "notes.txt")
    mkv = os.path.join(media, "del.mkv")
    lnk = os.path.join(media, "link.mkv")
    for p in (txt, mkv):
        with open(p, "wb") as f:
            f.write(b"x")
    os.symlink(mkv, lnk)
    r = logged_in.post("/api/delete-file", json={"path": txt})
    assert r.status_code == 400 and r.get_json()["error"] == "Nur Mediendateien können gelöscht werden"
    r = logged_in.post("/api/delete-file", json={"path": lnk})
    assert r.status_code == 400
    assert os.path.lexists(lnk) and os.path.exists(mkv)
    r = logged_in.post("/api/delete-file", json={"path": "/etc/passwd"})
    assert r.status_code == 400
    r = logged_in.post("/api/delete-file", json={"path": mkv})
    assert r.status_code == 200 and not os.path.exists(mkv)
    assert logged_in.post("/api/delete-file", json={"path": mkv}).status_code == 404


# ─── Flask-Testclient: Auth ──────────────────────────────────────────────────
def test_login_flow_401_then_429(client):
    for i in range(4):
        r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401, i
    r = client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 429
    j = r.get_json()
    assert j["retry_after"] == 60 and j["error"] == "Zu viele Versuche – warte 60 s"
    # even the right password is blocked while locked
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 429


def test_login_ignores_forwarded_for_by_default(client, server, monkeypatch):
    """S10: X-Forwarded-For ist frei fälschbar – ohne TRUST_PROXY zählt nur remote_addr."""
    monkeypatch.setattr(server, "TRUST_PROXY", False)
    for i in range(5):
        # Rotierende XFF-IPs dürfen die Sperre NICHT umgehen
        client.post("/api/login", json={"username": "x", "password": "y"},
                    headers={"X-Forwarded-For": f"9.9.9.{i}"})
    r = client.post("/api/login", json={"username": "admin", "password": "admin"},
                    headers={"X-Forwarded-For": "9.9.9.99"})
    assert r.status_code == 429


def test_login_trusted_proxy_uses_rightmost_xff(client, server, monkeypatch):
    monkeypatch.setattr(server, "TRUST_PROXY", True)
    for _ in range(5):
        # linkeste IP ist Client-Text, rechteste hat der Proxy angehängt
        client.post("/api/login", json={"username": "x", "password": "y"},
                    headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1"})
    r = client.post("/api/login", json={"username": "admin", "password": "admin"},
                    headers={"X-Forwarded-For": "7.7.7.7, 10.0.0.1"})
    assert r.status_code == 429                      # gesperrt ist 10.0.0.1
    r = client.post("/api/login", json={"username": "admin", "password": "admin"},
                    headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.2"})
    assert r.status_code == 200


def test_login_success_and_cookie(client):
    assert client.get("/api/status").status_code == 401
    r = client.post("/api/login", data="username=admin", content_type="application/x-www-form-urlencoded")
    assert r.status_code == 401
    r = client.post("/api/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Expires=" in cookie
    assert client.get("/api/auth-check").get_json() == {"authenticated": True}
    client.post("/api/logout")
    assert client.get("/api/auth-check").get_json() == {"authenticated": False}


def test_secret_key_file(server, dirs):
    p = os.path.join(dirs["config"], "secret.key")
    assert os.path.exists(p) and os.path.getsize(p) == 32
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert server.load_secret_key(p) == server.app.secret_key


# ─── Flask-Testclient: API-Shapes ────────────────────────────────────────────
def test_codecs_shape(server, logged_in):
    j = logged_in.get("/api/codecs").get_json()
    assert set(j) == {"video", "audio", "tvfix", "gpu", "gpu_caps", "qualities", "containers"}
    assert set(j["video"]) == {"h265", "h264", "av1", "vp9", "mpeg4"}
    for v in j["video"].values():
        assert set(v) == {"label", "gpu", "hint", "saving", "containers"} and isinstance(v["gpu"], bool)
        # Jeder Codec muss mindestens MKV anbieten, und zwar an erster Stelle:
        # die Oberflaeche nimmt den ersten Eintrag als Vorauswahl.
        assert v["containers"] and v["containers"][0] == "mkv"
        assert set(v["containers"]) <= set(j["containers"])
    assert j["video"]["h265"]["label"] == "H.265 (HEVC)"
    # Muxer-Grenzen: VP9/AV1 gehoeren in WebM, H.264/H.265 nicht.
    assert j["video"]["h265"]["containers"] == ["mkv", "mp4", "mov"]
    assert j["video"]["vp9"]["containers"] == ["mkv", "webm"]
    assert "webm" not in j["video"]["h264"]["containers"]
    assert "mp4" not in j["video"]["vp9"]["containers"]
    for c in j["containers"].values():
        assert set(c) == {"label", "ext", "hint"} and c["ext"].startswith(".")
    for a in j["audio"].values():
        assert set(a) == {"label", "hint", "ext"}
    assert j["gpu"] is False and set(j["gpu_caps"]) == {"h264", "hevc", "hevc10", "vp9", "av1"}
    assert [q["key"] for q in j["qualities"]] == ["fast", "balanced", "high"]
    assert j["qualities"][1]["label"] == "Ausgewogen"


def test_check_vaapi_without_vainfo(server):
    caps = server.check_vaapi()
    assert caps == {"h264": False, "hevc": False, "hevc10": False, "vp9": False, "av1": False}
    out = "VAProfileH264Main : VAEntrypointVLD\nVAProfileH264Main : VAEntrypointEncSlice\nVAProfileHEVCMain10 : VAEntrypointEncSliceLP\nVAProfileAV1Profile0 : VAEntrypointVLD\n"
    assert server.parse_vainfo(out) == {"h264": True, "hevc": True, "hevc10": True, "vp9": False, "av1": False}
    # HEVC nur als Main-Profil (ältere GPU) → kein Main10 (S15)
    only_main = "VAProfileHEVCMain : VAEntrypointEncSlice\nVAProfileVP9Profile0 : VAEntrypointEncSliceLP\n"
    assert server.parse_vainfo(only_main) == {"h264": False, "hevc": True, "hevc10": False,
                                              "vp9": True, "av1": False}


def test_status_empty(server, logged_in):
    j = logged_in.get("/api/status").get_json()
    assert j == {"active": False, "task": None, "queue": [], "last": None}


def test_stats_and_history_endpoints(server, logged_in):
    j = logged_in.get("/api/stats").get_json()
    assert "bytes_saved" in j and "percent_saved" in j
    j = logged_in.get("/api/history?limit=2&page=1").get_json()
    assert set(j) == {"entries", "total", "filtered_total", "list_total", "page", "pages", "limit",
                      "totals", "runs"}
    assert j["total"] == 3 and j["pages"] == 2 and len(j["entries"]) == 2 and j["limit"] == 2
    assert j["entries"][0]["name"] == "c.mp4"
    assert {k: j["totals"][k] for k in ("bytes_before", "bytes_after", "count")} \
        == {"bytes_before": 6000, "bytes_after": 2300, "count": 3}
    assert "last_run" in j["totals"]          # "zuletzt" kommt aus der Gesamtbilanz
    assert [r["run_id"] for r in j["runs"]] == ["legacy-20260812", "legacy-20260810"]
    assert j["runs"][1]["count"] == 2
    j = logged_in.get("/api/history?q=A.MKV").get_json()
    assert j["total"] == 1 and j["entries"][0]["name"] == "a.mkv"
    assert len(j["runs"]) == 2                     # runs always complete
    j = logged_in.get("/api/history?method=CPU").get_json()
    assert j["total"] == 1
    j = logged_in.get("/api/history?run=legacy-20260810&limit=all").get_json()
    assert j["total"] == 2 and j["pages"] == 1


def test_history_totals_unfiltered(server, logged_in):
    """S11: totals beschreiben immer den ganzen Verlauf, nur `total`/`filtered_total` filtern mit."""
    full = logged_in.get("/api/history").get_json()["totals"]
    j = logged_in.get("/api/history?q=A.MKV").get_json()
    assert j["totals"] == full
    assert {k: full[k] for k in ("bytes_before", "bytes_after", "count")} \
        == {"bytes_before": 6000, "bytes_after": 2300, "count": 3}
    assert j["total"] == 1 and j["filtered_total"] == 1
    assert j["list_total"] == 3                    # „Treffer von N" meint die ganze Liste
    j = logged_in.get("/api/history?q=gibtesnicht").get_json()
    assert j["total"] == 0 and j["filtered_total"] == 0 and j["entries"] == []
    assert j["totals"] == full                     # Leerzustand darf die Kacheln nicht nullen
    assert len(j["runs"]) == 2


def test_history_clear_last(server, logged_in):
    """Runs late alphabetically? No – pytest runs in file order; keep it after the read tests.

    Seit dem Lauf-Speicher räumt `clear` nur noch die Dateiliste: die Bilanz aus
    runs.jsonl bleibt stehen, sonst wäre nach einem Klick die ganze Statistik weg."""
    assert logged_in.post("/api/history/clear").status_code == 200
    j = logged_in.get("/api/history").get_json()
    assert j["total"] == 0 and j["entries"] == []
    # Die Liste ist leer, die Bilanz nicht – der Listenzähler muss das auseinanderhalten
    assert j["list_total"] == 0 and j["totals"]["count"] == 3
    # Der geleerte Stand bleibt als Kopie liegen – ein zweites Leeren der schon
    # leeren Liste darf sie nicht mit nichts überschreiben
    geleert = server.HISTORY_FILE + ".geleert"
    assert len(server.load_history(geleert)) == 3
    assert logged_in.post("/api/history/clear").status_code == 200
    assert len(server.load_history(geleert)) == 3
    assert [r["run_id"] for r in j["runs"]] == ["legacy-20260812", "legacy-20260810"]
    assert {k: j["totals"][k] for k in ("bytes_before", "bytes_after", "count")} \
        == {"bytes_before": 6000, "bytes_after": 2300, "count": 3}
    server.append_history({"run_id": "r9", "name": "n.mkv", "path": "/p", "rel_dir": "", "size_before": 2,
                           "size_after": 1, "method": "GPU", "target": "h265", "quality": "balanced",
                           "timestamp": 5, "duration_s": 1, "speed": 2.0})
    j = logged_in.get("/api/history").get_json()
    assert j["total"] == 1 and j["list_total"] == 1 and j["totals"]["count"] == 3


def test_history_delete_entry_endpoint(server, logged_in):
    """Einzelne Zeile entfernen – die Bilanz rührt das nicht an."""
    before = logged_in.get("/api/history").get_json()
    r = logged_in.post("/api/history/delete",
                       json={"run_id": "r9", "path": "/p", "timestamp": 5})
    assert r.status_code == 200 and r.get_json()["removed"] == 1
    after = logged_in.get("/api/history").get_json()
    assert after["total"] == before["total"] - 1
    assert after["totals"] == before["totals"]
    assert [x["run_id"] for x in after["runs"]] == [x["run_id"] for x in before["runs"]]


def test_history_delete_entry_reports_miss(server, logged_in):
    r = logged_in.post("/api/history/delete",
                       json={"run_id": "gibtesnicht", "path": "/p", "timestamp": 1})
    assert r.status_code == 200 and r.get_json()["removed"] == 0


def test_history_delete_entry_requires_all_keys(server, logged_in):
    assert logged_in.post("/api/history/delete", json={"run_id": "r9"}).status_code == 400
    assert logged_in.post("/api/history/delete", json={}).status_code == 400


def test_history_delete_run_endpoint(server, logged_in):
    for i in range(3):
        server.append_history({"run_id": "sammel", "name": f"s{i}.mkv", "path": f"/s{i}", "rel_dir": "",
                               "size_before": 10, "size_after": 4, "method": "GPU", "target": "h265",
                               "quality": "balanced", "timestamp": 7 + i, "duration_s": 1, "speed": 2.0})
    server.append_history({"run_id": "bleibt", "name": "x.mkv", "path": "/x", "rel_dir": "",
                           "size_before": 10, "size_after": 4, "method": "GPU", "target": "h265",
                           "quality": "balanced", "timestamp": 20, "duration_s": 1, "speed": 2.0})
    before = logged_in.get("/api/history").get_json()["totals"]
    r = logged_in.post("/api/history/delete-run", json={"run_id": "sammel"})
    assert r.status_code == 200 and r.get_json()["removed"] == 3
    j = logged_in.get("/api/history?limit=all").get_json()
    assert [e["run_id"] for e in j["entries"]] == ["bleibt"]
    assert j["totals"] == before
    logged_in.post("/api/history/delete-run", json={"run_id": "bleibt"})


def test_history_delete_run_requires_run_id(server, logged_in):
    assert logged_in.post("/api/history/delete-run", json={}).status_code == 400


def test_history_delete_needs_login(client):
    assert client.post("/api/history/delete",
                       json={"run_id": "r", "path": "/p", "timestamp": 1}).status_code == 401
    assert client.post("/api/history/delete-run", json={"run_id": "r"}).status_code == 401


# ─── Convert / Queue ─────────────────────────────────────────────────────────
def test_convert_validation(server, logged_in, dirs):
    assert logged_in.post("/api/convert", json={"folder": "../x", "codec": "h265"}).status_code == 400
    assert logged_in.post("/api/convert", json={"folder": "", "codec": "nope"}).status_code == 400
    r = logged_in.post("/api/convert", json={"folder": "", "codec": "h265", "quality": "ultra"})
    assert r.status_code == 400 and "Qualität" in r.get_json()["error"]
    os.makedirs(os.path.join(dirs["media"], "Leer"), exist_ok=True)
    assert logged_in.post("/api/convert", json={"folder": "Leer", "codec": "h265"}).status_code == 404


def test_convert_queue_and_task_shape(server, logged_in, dirs, monkeypatch):
    media = dirs["media"]
    sub = os.path.join(media, "Q", "S01")
    os.makedirs(sub, exist_ok=True)
    f1 = os.path.join(sub, "e1.mkv")
    with open(f1, "wb") as f:
        f.write(b"\x1aE\xdf\xa3" + b"0" * 100)

    # Block the worker from actually running ffmpeg: make run_task wait for our event.
    import threading
    gate = threading.Event()
    seen = []

    def fake_run(t):
        seen.append(t["id"])
        gate.wait(5)
        with server.lock:
            t["status"] = "cancelled" if server.state["cancel"] else "done"
            t["files"][0]["status"] = "done"
            t["files"][0]["size_after"] = 40
            t["completed"] = 1
            server._recalc(t)
            t["progress"] = 100
            t["finished_at"] = time.time()

    monkeypatch.setattr(server, "run_task", fake_run)

    r = logged_in.post("/api/convert", json={"folder": "Q", "codec": "h265", "type": "video",
                                              "quality": "high", "files": [f1]})
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] and j["position"] == 0
    jid = j["job_id"]

    # Second job waits at position 1
    r2 = logged_in.post("/api/convert", json={"folder": "Q", "codec": "h264"}).get_json()
    assert r2["position"] == 1

    deadline = time.time() + 3
    while time.time() < deadline and not seen:
        time.sleep(0.02)
    assert seen == [jid]

    st = logged_in.get("/api/status").get_json()
    assert st["active"] is True
    t = st["task"]
    expected_keys = {"id", "status", "folder", "target", "target_label", "type", "quality", "total",
                     "completed", "skipped", "failed", "progress", "eta", "bytes_total", "bytes_done",
                     "bytes_before_done", "bytes_after_done", "bytes_saved", "started_at", "finished_at", "files"}
    assert expected_keys <= set(t) and "_procs" not in t and "t0" not in t
    assert t["status"] == "running" and t["quality"] == "high" and t["target_label"] == "H.265 (HEVC)"
    assert t["bytes_total"] == 104
    fi = t["files"][0]
    assert set(fi) >= {"name", "path", "rel_dir", "status", "progress", "eta", "method", "error",
                       "size_before", "size_after", "speed", "fps", "streams_before", "streams_after"}
    assert fi["rel_dir"] == "S01" and fi["name"] == "e1.mkv"
    assert len(st["queue"]) == 1
    assert set(st["queue"][0]) == {"id", "folder", "folders", "target_label", "total",
                                   "bytes_total", "quality", "container"}
    # Die Warteschlange braucht die Ordnerliste selbst – aus `folder` (Anzeigename)
    # laesst sich bei mehreren Ordnern kein Pfad bauen.
    assert st["queue"][0]["folders"] == ["Q"]

    # Remove waiting job
    qid = st["queue"][0]["id"]
    assert logged_in.delete(f"/api/queue/{qid}").status_code == 200
    assert logged_in.delete(f"/api/queue/{qid}").status_code == 404
    assert logged_in.get("/api/status").get_json()["queue"] == []

    # Cancel running job → last
    logged_in.post("/api/cancel")
    gate.set()
    deadline = time.time() + 3
    while time.time() < deadline and logged_in.get("/api/status").get_json()["active"]:
        time.sleep(0.02)
    st = logged_in.get("/api/status").get_json()
    assert st["active"] is False and st["task"] is None
    assert st["last"]["id"] == jid and st["last"]["status"] == "cancelled"
    assert st["last"]["bytes_saved"] == 64

    # clear → last gone
    assert logged_in.post("/api/clear").status_code == 200
    assert logged_in.get("/api/status").get_json() == {"active": False, "task": None, "queue": [], "last": None}


def test_events_stream_headers_and_first_frame(server, logged_in):
    r = logged_in.get("/api/events", buffered=False)
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    assert r.headers["Cache-Control"] == "no-cache" and r.headers["X-Accel-Buffering"] == "no"
    it = r.response
    first = next(iter(it))
    if isinstance(first, bytes):
        first = first.decode()
    assert first.startswith("data: ")
    j = json.loads(first[len("data: "):].strip())
    assert set(j) == {"active", "task", "queue", "last"}
    r.close()


# ─── Prescan (asynchron) ─────────────────────────────────────────────────────
def test_prescan_async(server, logged_in, dirs, monkeypatch):
    media = dirs["media"]
    d = os.path.join(media, "P", "sub")
    os.makedirs(d, exist_ok=True)
    good = os.path.join(d, "g.mp4")
    bad = os.path.join(media, "P", "bad.mkv")
    skip = os.path.join(media, "P", "already.mkv")
    with open(good, "wb") as f:
        f.write(b"ftyp" + b"1" * 50)
    with open(bad, "wb") as f:
        f.write(b"\x00\x00\x00\x00" + b"0" * 10)
    with open(skip, "wb") as f:
        f.write(b"\x1aE\xdf\xa3" + b"2" * 20)

    def fake_info(fp):
        vc = "hevc" if fp.endswith("already.mkv") else "h264"
        return {"vc": vc, "ac": "aac", "dur": 1.0, "pix_fmt": "yuv420p", "is10": False,
                "n_video": 1, "n_audio": 1, "n_sub": 0, "size": 1}
    monkeypatch.setattr(server, "get_info", fake_info)

    assert logged_in.post("/api/prescan", json={"folder": "../", "codec": "h265"}).status_code == 400
    assert logged_in.post("/api/prescan", json={"folder": "P", "codec": "zzz"}).status_code == 400
    r = logged_in.post("/api/prescan", json={"folder": "P", "codec": "h265", "type": "video"})
    assert r.status_code == 202
    sid = r.get_json()["scan_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        j = logged_in.get(f"/api/prescan/{sid}").get_json()
        if j["status"] != "running":
            break
        time.sleep(0.02)
    assert j["status"] == "done" and j["done"] == 3 and j["total"] == 3 and j["error"] is None
    res = j["result"]
    # "ordner" (Aufschlüsselung je Quellordner) ist mit dem Sammelauftrag dazugekommen (A2)
    assert set(res) == {"to_convert", "to_skip", "corrupt", "total", "bytes_convert",
                        "bytes_skip", "ordner"}
    assert res["ordner"] == [{"folder": "P", "video": 3, "audio": 0, "total": 3, "bytes": 92}]
    assert [i["name"] for i in res["to_convert"]] == ["g.mp4"]
    assert res["to_convert"][0]["rel_dir"] == "sub" and res["to_convert"][0]["size"] == 54
    assert res["to_convert"][0]["src"] == "P"
    assert [i["name"] for i in res["to_skip"]] == ["already.mkv"] and res["to_skip"][0]["codec"] == "hevc"
    assert [i["name"] for i in res["corrupt"]] == ["bad.mkv"]
    assert res["bytes_convert"] == 54 and res["bytes_skip"] == 24
    assert logged_in.get("/api/prescan/nope").status_code == 404
    # S8: die Aufbewahrung zählt ab ENDE der Analyse, nicht ab Start –
    # eine lange Analyse darf beim ersten Abruf nicht schon verfallen sein
    assert server.prescans[sid]["finished"] >= server.prescans[sid]["created"]
    server.prescans[sid]["created"] -= server.PRESCAN_KEEP_S + 1
    assert logged_in.get(f"/api/prescan/{sid}").status_code == 200
    server.prescans[sid]["finished"] -= server.PRESCAN_KEEP_S + 1
    assert logged_in.get(f"/api/prescan/{sid}").status_code == 404
    assert logged_in.post("/api/prescan", json={"folder": "Leer", "codec": "h265"}).status_code == 404


def test_prescan_only_one_at_a_time(server, logged_in):
    server.prescans["busy"] = {"status": "running", "done": 0, "total": 1, "result": None, "error": None, "created": time.time()}
    try:
        r = logged_in.post("/api/prescan", json={"folder": "P", "codec": "h265"})
        assert r.status_code == 409
    finally:
        server.prescans.pop("busy", None)


# ─── Sonstiges ───────────────────────────────────────────────────────────────
def test_rel_dir_and_fmt_dur(server):
    assert server.rel_dir_of("/m/a/b/c.mkv", "/m/a") == "b"
    assert server.rel_dir_of("/m/a/c.mkv", "/m/a") == ""
    assert server.fmt_dur(2530) == "42:10" and server.fmt_dur(3725) == "1:02:05" and server.fmt_dur(0) == "0:00"


def test_transfer_ownership(server, tmp_path):
    src = tmp_path / "s"
    dst = tmp_path / "d"
    src.write_text("a")
    dst.write_text("b")
    os.chmod(src, 0o640)
    os.utime(src, (1000000000, 1000000000))
    st = src.stat()
    server.transfer_ownership(str(dst), st)
    d = dst.stat()
    assert oct(d.st_mode & 0o777) == "0o640" and int(d.st_mtime) == 1000000000


def test_change_credentials_and_auth_info(server, logged_in):
    assert logged_in.get("/api/auth-info").get_json() == {"username": "admin"}
    r = logged_in.post("/api/change-credentials", json={"current_password": "nope", "password": "abcdef"})
    assert r.status_code == 401
    r = logged_in.post("/api/change-credentials", json={"current_password": "admin", "password": "abc"})
    assert r.status_code == 400
    r = logged_in.post("/api/change-credentials", json={"current_password": "admin", "username": "admin"})
    assert r.status_code == 200


# ─── Konvertier-Ablauf: convert_one (S1/S3/S4/S5) ────────────────────────────
def _mk_task(paths, target="h265", ctype="video", quality="balanced", tid="job1"):
    files = [{"name": os.path.basename(p), "path": p, "rel_dir": "", "status": "pending",
              "progress": 0, "eta": 0, "method": "", "error": "",
              "size_before": os.path.getsize(p) if os.path.exists(p) else 0,
              "size_after": None, "speed": 0.0, "fps": 0.0,
              "streams_before": None, "streams_after": None, "subs_dropped": 0}
             for p in paths]
    return {"id": tid, "status": "running", "folder": "", "target": target, "target_label": "L",
            "type": ctype, "quality": quality, "total": len(files), "completed": 0, "skipped": 0,
            "failed": 0, "progress": 0, "eta": 0,
            "bytes_total": sum(f["size_before"] for f in files), "bytes_done": 0,
            "bytes_before_done": 0, "bytes_after_done": 0, "bytes_saved": 0,
            "started_at": time.time(), "finished_at": None, "t0": time.time(),
            "_procs": {}, "files": files}


def _probe_json(n_audio=1, subs=(), dur=10.0, size=100, pix="yuv420p"):
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264", "pix_fmt": pix}]
    for i in range(n_audio):
        streams.append({"index": 1 + i, "codec_type": "audio", "codec_name": "aac"})
    for j, c in enumerate(subs):
        streams.append({"index": 1 + n_audio + j, "codec_type": "subtitle", "codec_name": c})
    return {"streams": streams, "format": {"duration": str(dur), "size": str(size)}}


def _fake_ffmpeg(server, monkeypatch, rc=0, payload=b"x" * 50):
    """Ersetzt subprocess.Popen: schreibt sofort die Zieldatei und endet mit rc."""
    calls = []

    class P:
        def __init__(self, cmd, **kw):
            self.returncode = rc
            calls.append(list(cmd))
            if rc == 0:
                with open(cmd[-1], "wb") as f:
                    f.write(payload)

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kw: P(cmd, **kw))
    return calls


def _no_ffmpeg(server, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("ffmpeg hätte nicht gestartet werden dürfen")
    monkeypatch.setattr(server.subprocess, "Popen", boom)


@pytest.fixture
def workdir(dirs, request):
    d = os.path.join(dirs["media"], "W_" + request.node.name[:40])
    os.makedirs(d, exist_ok=True)
    return d


def _src(path, data=b"quelle" * 20):
    with open(path, "wb") as f:
        f.write(data)
    return path


def test_convert_one_success_uses_tmp_and_replaces(server, workdir, monkeypatch):
    """S3b/S4: es wird immer in .converting encodiert und erst danach atomar ersetzt."""
    src = _src(os.path.join(workdir, "a.mp4"))
    t = _mk_task([src])
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json(n_audio=2)))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(n_audio=2, dur=9.99, size=50))
    calls = _fake_ffmpeg(server, monkeypatch)
    before_hist = len(server.load_history())

    server.convert_one(t, 0, src, "h265", True, "balanced")

    assert calls and calls[0][-1] == os.path.join(workdir, "a.converting.mkv")
    fi = t["files"][0]
    assert fi["status"] == "done" and fi["error"] == "" and fi["size_after"] == 50
    assert not os.path.exists(src)
    assert not os.path.exists(os.path.join(workdir, "a.converting.mkv"))
    with open(os.path.join(workdir, "a.mkv"), "rb") as f:
        assert f.read() == b"x" * 50
    assert fi["streams_before"] == {"a": 2, "s": 0} and fi["streams_after"] == {"a": 2, "s": 0}
    assert len(server.load_history()) == before_hist + 1


def test_convert_one_inplace_replaces_source(server, workdir, monkeypatch):
    src = _src(os.path.join(workdir, "b.mkv"))
    t = _mk_task([src])
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(dur=10.0, size=50))
    calls = _fake_ffmpeg(server, monkeypatch)
    server.convert_one(t, 0, src, "h265", True, "balanced")
    assert calls[0][-1] == os.path.join(workdir, "b.converting.mkv")
    assert t["files"][0]["status"] == "done"
    with open(src, "rb") as f:
        assert f.read() == b"x" * 50


def test_convert_one_subtitles_expectation(server, workdir, monkeypatch):
    """S1: erwartet werden nur die GEMAPPTEN Untertitel – sonst schlägt die Prüfung
    fälschlich an und die korrekt konvertierte Datei landet als Fehler."""
    src = _src(os.path.join(workdir, "c.mp4"))
    t = _mk_task([src])
    monkeypatch.setattr(server, "get_info",
                        lambda fp: server.info_from_probe(_probe_json(subs=("mov_text", "eia_608"))))
    # Ausgabe hat nur einen Untertitel (der eia_608 wurde bewusst weggelassen)
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(subs=("subrip",), dur=10.0, size=50))
    calls = _fake_ffmpeg(server, monkeypatch)

    server.convert_one(t, 0, src, "h265", True, "balanced")

    joined = " ".join(calls[0])
    assert "-map 0:2 -c:s:0 srt" in joined and "0:3" not in joined
    fi = t["files"][0]
    assert fi["status"] == "done", fi["error"]
    assert fi["subs_dropped"] == 1
    # Vorher/Nachher zählen gleich: der eia_608 wird bewusst nicht gemappt und darf
    # nicht als verlorene Spur erscheinen – die Zahl steht in subs_dropped (R4).
    assert fi["streams_before"] == {"a": 1, "s": 1} and fi["streams_after"] == {"a": 1, "s": 1}


def test_convert_one_missing_subtitle_still_fails(server, workdir, monkeypatch):
    """Gegenprobe: geht ein MKV-fähiger Untertitel verloren, muss es ein Fehler sein."""
    src = _src(os.path.join(workdir, "d.mp4"))
    t = _mk_task([src])
    monkeypatch.setattr(server, "get_info",
                        lambda fp: server.info_from_probe(_probe_json(subs=("subrip", "ass"))))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(subs=("subrip",), dur=10.0, size=50))
    _fake_ffmpeg(server, monkeypatch)
    server.convert_one(t, 0, src, "h265", True, "balanced")
    fi = t["files"][0]
    assert fi["status"] == "error" and "Untertitel 2→1" in fi["error"]
    assert os.path.exists(src)                       # Quelle bleibt unangetastet
    assert not os.path.exists(os.path.join(workdir, "d.mkv"))


def test_convert_one_existing_target_refused(server, workdir, monkeypatch):
    """S3a: eine fremde a.mkv darf nicht von der Konvertierung von a.mp4 überschrieben werden."""
    src = _src(os.path.join(workdir, "e.mp4"))
    foreign = _src(os.path.join(workdir, "e.mkv"), b"fremd")
    t = _mk_task([src])
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    _no_ffmpeg(server, monkeypatch)

    server.convert_one(t, 0, src, "h265", True, "balanced")

    fi = t["files"][0]
    assert fi["status"] == "error" and fi["error"] == "Zieldatei existiert bereits: e.mkv"
    assert t["failed"] == 1
    with open(foreign, "rb") as f:
        assert f.read() == b"fremd"
    assert os.path.exists(src)


def test_convert_one_job_internal_collision(server, workdir, monkeypatch):
    """S3c: zwei Quellen desselben Jobs mit gleichem Zielnamen – die zweite wird abgelehnt."""
    keep = _src(os.path.join(workdir, "f.mkv"))
    other = _src(os.path.join(workdir, "f.mp4"))
    t = _mk_task([keep, other])
    t["_collisions"] = server.find_target_collisions([keep, other], ".mkv")
    assert t["_collisions"] == {other: "f.mkv"}
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    _no_ffmpeg(server, monkeypatch)

    server.convert_one(t, 1, other, "h265", True, "balanced")

    fi = t["files"][1]
    assert fi["status"] == "error" and fi["error"] == "Zielname doppelt belegt: f.mkv gehört zu f.mkv"
    assert os.path.exists(other) and os.path.exists(keep)


def test_run_task_computes_collisions(server, workdir, monkeypatch):
    a = _src(os.path.join(workdir, "g.mkv"))
    b = _src(os.path.join(workdir, "g.mp4"))
    t = _mk_task([a, b])
    monkeypatch.setattr(server, "convert_one", lambda *args, **kw: None)
    monkeypatch.setattr(server, "update_cumulative_stats", lambda t_: None)
    server.run_task(t)
    assert t["_collisions"] == {b: "g.mkv"}
    assert "_collisions" not in server._task_copy(t)      # bleibt aus der API-Shape raus


def test_convert_one_replace_failure_is_error(server, workdir, monkeypatch):
    """S4: schlägt das Ersetzen fehl, ist die Datei ein Fehler – keine Historie, Quelle bleibt."""
    src = _src(os.path.join(workdir, "h.mp4"))
    t = _mk_task([src])
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(dur=10.0, size=50))
    _fake_ffmpeg(server, monkeypatch)

    # Nicht in-place läuft das Ersetzen über os.link (Rückfall os.replace) – beide scheitern.
    def boom(a, b, *rest, **kw):
        raise OSError("Read-only file system")
    monkeypatch.setattr(server.os, "link", boom)
    monkeypatch.setattr(server.os, "replace", boom)
    before_hist = len(server.load_history())

    server.convert_one(t, 0, src, "h265", True, "balanced")

    fi = t["files"][0]
    assert fi["status"] == "error"
    assert fi["error"] == "Ersetzen fehlgeschlagen: Read-only file system"
    assert t["failed"] == 1 and t["completed"] == 0
    assert os.path.exists(src)                                   # Quelle nicht gelöscht
    assert not os.path.exists(os.path.join(workdir, "h.converting.mkv"))
    assert len(server.load_history()) == before_hist             # kein History-Eintrag


def test_convert_one_ffprobe_none(server, workdir, monkeypatch):
    """S5: ohne lesbare Quelle gibt es keine Erwartungswerte → gar nicht erst konvertieren."""
    src = _src(os.path.join(workdir, "i.mp4"))
    t = _mk_task([src])
    monkeypatch.setattr(server, "probe", lambda fp: None)
    _no_ffmpeg(server, monkeypatch)

    server.convert_one(t, 0, src, "h265", True, "balanced")

    fi = t["files"][0]
    assert fi["status"] == "error" and fi["error"] == "ffprobe konnte die Datei nicht lesen"
    assert t["failed"] == 1 and os.path.exists(src)


def test_get_info_none_on_probe_failure(server, monkeypatch):
    monkeypatch.setattr(server, "probe", lambda fp: None)
    assert server.get_info("/nix.mkv") is None
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json())
    assert server.get_info("/da.mkv")["n_audio"] == 1


def test_prescan_check_unreadable_is_corrupt(server, workdir, monkeypatch):
    """S5: im Prescan zählen ffprobe-Ausfälle als defekt, nicht als 'zu konvertieren'."""
    f = _src(os.path.join(workdir, "j.mkv"))
    monkeypatch.setattr(server, "get_info", lambda fp: None)
    kind, item = server.prescan_check(f, workdir, "h265", True)
    assert kind == "corrupt" and item["codec"] == "error"


def test_convert_one_audio_path_untouched(server, workdir, monkeypatch):
    """-vn-Pfad für reine Audiodateien bleibt wie er war (kein -map, keine Untertitel)."""
    src = _src(os.path.join(workdir, "k.wav"))
    t = _mk_task([src], target="mp3", ctype="audio")
    monkeypatch.setattr(server, "get_info",
                        lambda fp: {"vc": None, "ac": "pcm_s16le", "dur": 10.0, "pix_fmt": "",
                                    "is10": False, "bits": None, "n_video": 0, "n_audio": 1,
                                    "n_sub": 0, "size": 120, "sub_streams": []})
    monkeypatch.setattr(server, "probe", lambda fp: {
        "streams": [{"index": 0, "codec_type": "audio", "codec_name": "mp3"}],
        "format": {"duration": "10.0", "size": "50"}})
    calls = _fake_ffmpeg(server, monkeypatch)

    server.convert_one(t, 0, src, "mp3", False, "balanced")

    joined = " ".join(calls[0])
    assert "-vn" in joined and "-map 0:V:0" not in joined and "-c:s" not in joined
    assert calls[0][-1] == os.path.join(workdir, "k.converting.mp3")
    assert t["files"][0]["status"] == "done"
    assert os.path.exists(os.path.join(workdir, "k.mp3")) and not os.path.exists(src)


# ─── S6: Endungsfilter für die Client-Dateiliste ─────────────────────────────
def _drain_queue(server, logged_in):
    deadline = time.time() + 3
    while time.time() < deadline and logged_in.get("/api/status").get_json()["active"]:
        time.sleep(0.02)
    logged_in.post("/api/clear")


def test_convert_rejects_non_media_files(server, logged_in, dirs, monkeypatch):
    """S6: die Dateiliste kommt vom Client – ffmpeg (root) darf nur Mediendateien sehen."""
    d = os.path.join(dirs["media"], "S6")
    os.makedirs(d, exist_ok=True)
    mkv = _src(os.path.join(d, "film.mp4"))
    conf = _src(os.path.join(d, "daemon.json"))
    sh = _src(os.path.join(d, "start.sh"))
    import threading as _th
    gate = _th.Event()
    monkeypatch.setattr(server, "run_task", lambda t: gate.wait(3))

    r = logged_in.post("/api/convert", json={"folder": "S6", "codec": "h265", "type": "video",
                                             "files": [conf, sh, mkv, "/etc/passwd"]})
    assert r.status_code == 200
    assert r.get_json()["total"] == 1
    deadline = time.time() + 3
    while time.time() < deadline and not logged_in.get("/api/status").get_json()["active"]:
        time.sleep(0.02)
    task = logged_in.get("/api/status").get_json()["task"]
    assert [f["name"] for f in task["files"]] == ["film.mp4"]
    gate.set()

    # Nur Nicht-Mediendateien → gar kein Auftrag
    r = logged_in.post("/api/convert", json={"folder": "S6", "codec": "h265", "files": [conf, sh]})
    assert r.status_code == 404
    _drain_queue(server, logged_in)


def test_convert_audio_job_accepts_video_files(server, logged_in, dirs, monkeypatch):
    """Ton aus Video: der Musik-Modus nimmt Videodateien als Quelle an (27.09.2026 –
    vorher 404 „keine Dateien“, der Betreiber konnte kein Video zu Musik machen)."""
    d = os.path.join(dirs["media"], "S6b")
    os.makedirs(d, exist_ok=True)
    _src(os.path.join(d, "v.mkv"))
    import threading as _th
    gate = _th.Event()
    monkeypatch.setattr(server, "run_task", lambda t: gate.wait(3))
    r = logged_in.post("/api/convert", json={"folder": "S6b", "codec": "mp3", "type": "audio",
                                             "files": [os.path.join(d, "v.mkv")]})
    assert r.status_code == 200 and r.get_json()["total"] == 1
    gate.set()
    _drain_queue(server, logged_in)
    # Ohne Dateiliste ebenso: der Ordner enthält nur ein Video
    monkeypatch.setattr(server, "run_task", lambda t: gate.wait(3))
    r = logged_in.post("/api/convert", json={"folder": "S6b", "codec": "mp3", "type": "audio"})
    assert r.status_code == 200 and r.get_json()["total"] == 1
    _drain_queue(server, logged_in)


# ─── S9: keine Massenaktionen auf der NAS-Wurzel ─────────────────────────────
@pytest.mark.parametrize("folder", ["", "./", "Filme/..", "Filme/../"])
def test_root_folder_refused(server, logged_in, folder):
    for url, body in (("/api/scan", {"folder": folder}),
                      ("/api/prescan", {"folder": folder, "codec": "h265"}),
                      ("/api/convert", {"folder": folder, "codec": "h265", "quality": "balanced"})):
        r = logged_in.post(url, json=body)
        assert r.status_code == 400, (url, folder)
        assert r.get_json()["error"] == "Bitte einen Unterordner wählen", url


def test_root_folder_browser_still_works(server, logged_in):
    """Der Ordner-Browser darf die Wurzel weiterhin listen."""
    r = logged_in.get("/api/folders?path=")
    assert r.status_code == 200 and "folders" in r.get_json()


# ─── S7: SSE-Verbindungen gedeckelt ──────────────────────────────────────────
def test_sse_connection_limit(server, logged_in):
    open_resps = []
    try:
        for i in range(server.SSE_MAX):
            r = logged_in.get("/api/events", buffered=False)
            assert r.status_code == 200, i
            open_resps.append(r)
        r = logged_in.get("/api/events", buffered=False)
        assert r.status_code == 503
        assert "Live-Verbindungen" in r.get_json()["error"]
        r.close()
    finally:
        for r in open_resps:
            r.close()
    # nach dem Schließen sind die Plätze wieder frei
    r = logged_in.get("/api/events", buffered=False)
    assert r.status_code == 200
    r.close()


# ─── S12: ausgeblendeter Job kommt nicht als `last` zurück ───────────────────
def test_clear_marks_discard_id(server, logged_in, dirs, monkeypatch):
    d = os.path.join(dirs["media"], "S12")
    os.makedirs(d, exist_ok=True)
    _src(os.path.join(d, "x.mkv"))
    import threading as _th
    gate = _th.Event()
    monkeypatch.setattr(server, "run_task", lambda t: gate.wait(3))
    jid = logged_in.post("/api/convert", json={"folder": "S12", "codec": "h265"}).get_json()["job_id"]
    deadline = time.time() + 3
    while time.time() < deadline and not logged_in.get("/api/status").get_json()["active"]:
        time.sleep(0.02)
    gate.set()
    assert logged_in.post("/api/clear").status_code == 200
    st = logged_in.get("/api/status").get_json()
    assert st == {"active": False, "task": None, "queue": [], "last": None}
    assert jid


def test_worker_drops_discarded_job(server, monkeypatch):
    """S12: hängt der Worker beim Ausblenden noch, darf er `last` danach nicht neu setzen."""
    monkeypatch.setattr(server, "run_task", lambda t: None)
    t = _mk_task([], tid="discard-me")
    t["status"] = "queued"
    with server.queue_cv:
        server.state["last"] = None
        server.state["discard_id"] = "discard-me"
        server.state["queue"].append(t)
        server.queue_cv.notify_all()
    deadline = time.time() + 3
    while time.time() < deadline:
        with server.lock:
            if server.state["current"] is None and not server.state["queue"] and t["status"] != "queued":
                break
        time.sleep(0.02)
    with server.lock:
        assert server.state["last"] is None
        assert server.state["discard_id"] is None

    # Gegenprobe: ein nicht verworfener Job wird sehr wohl zu `last`
    t2 = _mk_task([], tid="keep-me")
    t2["status"] = "queued"
    with server.queue_cv:
        server.state["queue"].append(t2)
        server.queue_cv.notify_all()
    deadline = time.time() + 3
    while time.time() < deadline:
        with server.lock:
            if server.state["last"] is not None:
                break
        time.sleep(0.02)
    with server.lock:
        assert server.state["last"]["id"] == "keep-me"
        server.state["last"] = None


# ─── S13: Datei-I/O nicht unter dem globalen Lock ────────────────────────────
def test_stats_written_outside_lock(server, monkeypatch):
    """Ein hängender /config-Mount darf /api/status, SSE und Abbrechen nicht einfrieren."""
    seen = {}

    def fake_stats(t):
        got = server.lock.acquire(blocking=False)
        seen["free"] = got
        if got:
            server.lock.release()
    monkeypatch.setattr(server, "update_cumulative_stats", fake_stats)
    server.run_task(_mk_task([], tid="lockcheck"))
    assert seen["free"] is True


def test_stats_error_does_not_break_task(server, monkeypatch):
    def boom(t):
        raise OSError("/config hängt")
    monkeypatch.setattr(server, "update_cumulative_stats", boom)
    t = _mk_task([], tid="statsboom")
    server.run_task(t)
    assert t["status"] == "done" and t["finished_at"]


# ─── S17: Rechte der bestehenden secret.key ──────────────────────────────────
def test_secret_key_permissions_repaired(server, tmp_path):
    p = tmp_path / "secret.key"
    p.write_bytes(b"k" * 32)
    os.chmod(p, 0o644)
    assert server.load_secret_key(str(p)) == b"k" * 32
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"


def test_secret_key_created_with_0600(server, tmp_path):
    p = tmp_path / "neu.key"
    key = server.load_secret_key(str(p))
    assert len(key) == 32 and oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert server.load_secret_key(str(p)) == key      # stabil über Neustarts


# ─── Fehler-, Abbruch- und Fallback-Pfad von convert_one ─────────────────────
GPU_ALL = {"h264": True, "hevc": True, "hevc10": True, "vp9": True, "av1": True}


def _fake_ffmpeg_seq(server, monkeypatch, rcs, payload=b"x" * 50, stderr_text=""):
    """Wie _fake_ffmpeg, aber mit einem Rückgabecode je Versuch und echtem stderr."""
    calls = []
    seq = list(rcs)

    class P:
        def __init__(self, cmd, **kw):
            self.returncode = seq[len(calls)] if len(calls) < len(seq) else seq[-1]
            calls.append(list(cmd))
            fh = kw.get("stderr")
            if stderr_text and hasattr(fh, "write"):
                fh.write(stderr_text)
            if self.returncode == 0:
                with open(cmd[-1], "wb") as f:
                    f.write(payload)

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

        def kill(self):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kw: P(cmd, **kw))
    return calls


def _leftovers(server, tid):
    """Progress-/stderr-Dateien dieses Jobs, die liegen geblieben sind."""
    try:
        return sorted(f for f in os.listdir(server.PROGRESS_DIR) if tid in f)
    except OSError:
        return []


def _enc(calls):
    """Encoder-Argument (-c:v …) je Versuch – so lässt sich die Kette ablesen."""
    return [c[c.index("-c:v") + 1] for c in calls if "-c:v" in c]


def test_convert_one_falls_back_through_gpu_chain(server, workdir, monkeypatch):
    """Scheitert ffmpeg mit rc!=0, läuft die Kette GPU → GPU* → CPU wirklich durch."""
    src = _src(os.path.join(workdir, "chain.mp4"))
    t = _mk_task([src], tid="chainjob")
    monkeypatch.setattr(server, "GPU_CAPS", dict(GPU_ALL))
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(dur=10.0, size=50))
    calls = _fake_ffmpeg_seq(server, monkeypatch, [1, 1, 0])

    server.convert_one(t, 0, src, "h265", True, "balanced")

    assert _enc(calls) == ["hevc_vaapi", "hevc_vaapi", "libx265"]
    assert "-hwaccel" in calls[0] and "-hwaccel" not in calls[1]
    fi = t["files"][0]
    assert fi["status"] == "done" and fi["method"] == "CPU"
    assert os.path.exists(os.path.join(workdir, "chain.mkv"))
    assert not os.path.exists(os.path.join(workdir, "chain.converting.mkv"))
    assert _leftovers(server, "chainjob") == []


def test_convert_one_all_attempts_fail_keeps_source(server, workdir, monkeypatch):
    """Kein Versuch klappt: Quelle bleibt, kein Ziel, keine Zwischen-Datei, kein Verlauf."""
    src = _src(os.path.join(workdir, "allfail.mp4"))
    t = _mk_task([src], tid="failjob")
    monkeypatch.setattr(server, "GPU_CAPS", dict(GPU_ALL))
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    before_hist = len(server.load_history())
    calls = _fake_ffmpeg_seq(server, monkeypatch, [1, 1, 1])

    server.convert_one(t, 0, src, "h265", True, "balanced")

    assert len(calls) == 3
    fi = t["files"][0]
    assert fi["status"] == "error" and t["failed"] == 1
    assert os.path.exists(src) and not os.path.exists(os.path.join(workdir, "allfail.mkv"))
    assert not os.path.exists(os.path.join(workdir, "allfail.converting.mkv"))
    assert len(server.load_history()) == before_hist
    assert _leftovers(server, "failjob") == []


def test_convert_one_error_summary_from_stderr(server, workdir, monkeypatch):
    """Die Fehlermeldung kommt aus der ffmpeg-Ausgabe, nicht aus einem Platzhalter."""
    src = _src(os.path.join(workdir, "stderr.mp4"))
    t = _mk_task([src], tid="errjob")
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    _fake_ffmpeg_seq(server, monkeypatch, [1], stderr_text=(
        "frame=   12 fps=0.0\n"
        "[matroska @ 0x1] Subtitle codec mov_text (94213) is not supported\n"
        "Could not write header for output file #0 (incorrect codec parameters ?)\n"))

    server.convert_one(t, 0, src, "h265", True, "balanced")

    fi = t["files"][0]
    assert fi["status"] == "error"
    assert fi["error"] == "Could not write header for output file #0 (incorrect codec parameters ?)"
    assert os.path.exists(src)
    assert _leftovers(server, "errjob") == []


def test_convert_one_no_fallback_after_verify_fail(server, workdir, monkeypatch):
    """Fehlt nach dem Encode eine Spur, wird NICHT weiterprobiert – der Mapping-Fehler
    wäre im nächsten Versuch derselbe (SPEC A2)."""
    src = _src(os.path.join(workdir, "verify.mp4"))
    t = _mk_task([src], tid="verifyjob")
    monkeypatch.setattr(server, "GPU_CAPS", dict(GPU_ALL))
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json(n_audio=2)))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(n_audio=1, dur=10.0, size=50))
    calls = _fake_ffmpeg_seq(server, monkeypatch, [0, 0, 0])

    server.convert_one(t, 0, src, "h265", True, "balanced")

    assert len(calls) == 1                       # genau ein Versuch, kein Fallback
    fi = t["files"][0]
    assert fi["status"] == "error" and "Audio 2→1" in fi["error"]
    assert fi["method"] == "GPU"
    assert os.path.exists(src)
    assert not os.path.exists(os.path.join(workdir, "verify.mkv"))
    assert not os.path.exists(os.path.join(workdir, "verify.converting.mkv"))
    assert _leftovers(server, "verifyjob") == []


def test_convert_one_unreadable_output_fails(server, workdir, monkeypatch):
    """Lässt sich die Ausgabe nicht proben, gilt der Versuch als gescheitert (S5 am Ausgang)."""
    src = _src(os.path.join(workdir, "noprobe.mp4"))
    t = _mk_task([src], tid="noprobejob")
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    monkeypatch.setattr(server, "probe", lambda fp: None)
    _fake_ffmpeg_seq(server, monkeypatch, [0])

    server.convert_one(t, 0, src, "h265", True, "balanced")

    fi = t["files"][0]
    assert fi["status"] == "error" and "Video 1→0" in fi["error"]
    assert os.path.exists(src)
    assert not os.path.exists(os.path.join(workdir, "noprobe.mkv"))
    assert not os.path.exists(os.path.join(workdir, "noprobe.converting.mkv"))


def test_convert_one_cancel_mid_encode(server, workdir, monkeypatch):
    """Abbruch mitten im Encode: Prozess getötet, halbfertige Datei weg, Quelle heil."""
    src = _src(os.path.join(workdir, "cancel.mp4"))
    t = _mk_task([src], tid="canceljob")
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    calls, killed = [], []

    class P:
        def __init__(self, cmd, **kw):
            calls.append(list(cmd))
            with open(cmd[-1], "wb") as f:
                f.write(b"halbfertig")
            self.returncode = None

        def poll(self):
            server.state["cancel"] = True      # Abbruch trifft während des Encodes ein
            return self.returncode

        def wait(self):
            return -9

        def kill(self):
            killed.append(True)
            self.returncode = -9

    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kw: P(cmd, **kw))
    try:
        server.convert_one(t, 0, src, "h265", True, "balanced")
    finally:
        server.state["cancel"] = False

    assert len(calls) == 1 and killed == [True]
    assert os.path.exists(src)
    assert not os.path.exists(os.path.join(workdir, "cancel.converting.mkv"))
    assert not os.path.exists(os.path.join(workdir, "cancel.mkv"))
    assert t["failed"] == 0 and t["completed"] == 0
    assert _leftovers(server, "canceljob") == []


def test_convert_one_exception_leaves_nothing_behind(server, workdir, monkeypatch):
    """R3: auch wenn Popen selbst fliegt, bleiben keine Dateien in /tmp/mc_progress."""
    src = _src(os.path.join(workdir, "boom.mp4"))
    t = _mk_task([src], tid="boomjob")
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))

    def boom(cmd, **kw):
        raise OSError("Exec format error")
    monkeypatch.setattr(server.subprocess, "Popen", boom)

    server.convert_one(t, 0, src, "h265", True, "balanced")

    fi = t["files"][0]
    assert fi["status"] == "error" and t["failed"] == 1
    assert os.path.exists(src)
    assert _leftovers(server, "boomjob") == []


def test_convert_one_unlink_failure_stays_successful(server, workdir, monkeypatch):
    """R2: liegt das geprüfte Ergebnis am Ziel, macht ein Fehler beim Löschen der alten
    Quelle daraus keinen Fehlschlag mehr."""
    src = _src(os.path.join(workdir, "keepsrc.mp4"))
    t = _mk_task([src], tid="unlinkjob")
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(dur=10.0, size=50))
    _fake_ffmpeg_seq(server, monkeypatch, [0])

    real_unlink = server.Path.unlink

    def flaky(self, *a, **kw):
        if self.name == "keepsrc.mp4":
            raise OSError("Device or resource busy")
        return real_unlink(self, *a, **kw)
    monkeypatch.setattr(server.Path, "unlink", flaky)
    before_hist = len(server.load_history())

    server.convert_one(t, 0, src, "h265", True, "balanced")

    fi = t["files"][0]
    assert fi["status"] == "done" and fi["error"] == "" and t["failed"] == 0
    with open(os.path.join(workdir, "keepsrc.mkv"), "rb") as f:
        assert f.read() == b"x" * 50
    assert os.path.exists(src)                                   # Quelle blieb liegen
    assert len(server.load_history()) == before_hist + 1         # Erfolg zählt trotzdem


# ─── R1: eigene .converting-Reste sind niemals Quelle ────────────────────────
def test_is_temp_name(server):
    assert server.is_temp_name("/m/a.converting.mkv")
    assert server.is_temp_name("/m/Der Film.CONVERTING.MKV")
    assert not server.is_temp_name("/m/a.mkv")
    assert not server.is_temp_name("/m/converting.mkv")
    assert server.tmp_path("/m/a.mp4", ".mkv") == "/m/a.converting.mkv"


def test_scan_skips_converting_leftovers(server, dirs):
    d = os.path.join(dirs["media"], "R1scan", "sub")
    os.makedirs(d, exist_ok=True)
    good = _src(os.path.join(d, "film.mkv"))
    _src(os.path.join(d, "film.converting.mkv"))
    _src(os.path.join(d, "andere.converting.mp4"))
    assert server.scan(os.path.join(dirs["media"], "R1scan")) == [good]


def test_folders_endpoint_ignores_converting_leftovers(server, logged_in, dirs):
    d = os.path.join(dirs["media"], "R1count")
    os.makedirs(d, exist_ok=True)
    _src(os.path.join(d, "film.mkv"))
    _src(os.path.join(d, "film.converting.mkv"))
    r = logged_in.get("/api/folders?path=R1count")
    assert r.get_json()["media_count"] == 1


def test_convert_rejects_converting_leftover(server, logged_in, dirs, monkeypatch):
    """Eine Leiche aus einem abgestürzten Lauf darf nicht als Quelle eingeplant werden."""
    d = os.path.join(dirs["media"], "R1conv")
    os.makedirs(d, exist_ok=True)
    leftover = _src(os.path.join(d, "film.converting.mkv"))
    monkeypatch.setattr(server, "run_task", lambda t: None)
    r = logged_in.post("/api/convert", json={"folder": "R1conv", "codec": "h265",
                                             "type": "video", "files": [leftover]})
    assert r.status_code == 404
    _drain_queue(server, logged_in)


def test_convert_one_removes_stale_tmp(server, workdir, monkeypatch):
    """Ein Rest aus einem abgestürzten Lauf wird vor dem Encode entfernt."""
    src = _src(os.path.join(workdir, "stale.mp4"))
    stale = _src(os.path.join(workdir, "stale.converting.mkv"), b"reste")
    t = _mk_task([src], tid="stalejob")
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(dur=10.0, size=50))
    _fake_ffmpeg_seq(server, monkeypatch, [0])

    server.convert_one(t, 0, src, "h265", True, "balanced")

    assert t["files"][0]["status"] == "done"
    assert not os.path.exists(stale)
    with open(os.path.join(workdir, "stale.mkv"), "rb") as f:
        assert f.read() == b"x" * 50


# ─── S7: der SSE-Platz wird genau EINMAL freigegeben ─────────────────────────
def test_sse_slot_released_exactly_once(server, logged_in):
    """Generator-Ende UND Response-Close geben frei – zusammen darf das den Zähler
    nicht über den Deckel heben (BoundedSemaphore würde sonst schon vorher werfen)."""
    r = logged_in.get("/api/events", buffered=False)
    assert r.status_code == 200
    assert next(iter(r.response)).startswith(b"data: ")   # Generator lief wirklich an
    r.close()

    got = [server.SSE_SLOTS.acquire(blocking=False) for _ in range(server.SSE_MAX)]
    try:
        assert all(got), "nach dem Schließen müssen alle Plätze frei sein"
        assert server.SSE_SLOTS.acquire(blocking=False) is False
    finally:
        for ok in got:
            if ok:
                server.SSE_SLOTS.release()
    with pytest.raises(ValueError):
        server.SSE_SLOTS.release()      # Beleg: der Zähler steht exakt auf SSE_MAX


# ─── I1b: Ordnername stammt nie aus dem Rohtext des Clients ──────────────────

def test_folder_label_pure(server):
    """folder_label leitet den Anzeigenamen aus dem geprüften Pfad ab."""
    root = os.path.realpath(server.MEDIA_ROOT)
    assert server.folder_label(root) == ""
    assert server.folder_label(os.path.join(root, "Serien", "S01")) == "Serien/S01"
    # Anfuehrungszeichen im echten Ordnernamen bleiben erhalten (der Name kommt
    # aus dem Dateisystem), aber der Wert ist immer ein Pfad unterhalb der Wurzel
    assert server.folder_label(os.path.join(root, 'a"b')) == 'a"b'
    # Ein Pfad ausserhalb der Wurzel ergibt nie einen Namen mit Wurzel-Ausbruch
    assert not server.folder_label("/etc").startswith("/")


def test_convert_folder_not_taken_from_body(server, logged_in, dirs, monkeypatch):
    """Ein Ordnername mit Markup darf nicht unveraendert in den Auftrag wandern.

    Der Wert landet sonst dauerhaft in der Warteschlangen-Anzeige jedes
    Betrachters (persistente Einschleusung).
    """
    boese = 'Serien" onmouseover="alert(1)'
    ordner = os.path.join(dirs["media"], boese)
    os.makedirs(ordner, exist_ok=True)
    datei = os.path.join(ordner, "film.mkv")
    with open(datei, "wb") as f:
        f.write(b"\x1aE\xdf\xa3" + b"0" * 100)

    import threading
    gate = threading.Event()

    def fake_run(t):
        gate.wait(5)
        with server.lock:
            t["status"] = "done"

    monkeypatch.setattr(server, "run_task", fake_run)
    r = logged_in.post("/api/convert", json={"folder": boese, "codec": "h265",
                                             "type": "video", "quality": "fast",
                                             "files": [datei]})
    assert r.status_code == 200, r.get_json()
    time.sleep(0.3)
    st = logged_in.get("/api/status").get_json()
    aufgabe = st["task"] or (st["queue"][0] if st["queue"] else None)
    assert aufgabe is not None
    # Der Name kommt aus dem Dateisystem, ist also identisch - aber er wurde
    # aus dem geprueften Pfad abgeleitet und nicht aus dem Anfragetext.
    assert aufgabe["folder"] == boese
    gate.set()
    time.sleep(0.3)
    logged_in.post("/api/clear")


def test_convert_folder_ignores_body_text_variants(server, logged_in, dirs, monkeypatch):
    """Gleicher Ordner, unterschiedlich geschriebener Anfragetext -> gleicher Anzeigename."""
    ordner = os.path.join(dirs["media"], "Doku")
    os.makedirs(ordner, exist_ok=True)
    datei = os.path.join(ordner, "d1.mkv")
    with open(datei, "wb") as f:
        f.write(b"\x1aE\xdf\xa3" + b"0" * 100)

    import threading
    gate = threading.Event()
    monkeypatch.setattr(server, "run_task", lambda t: gate.wait(5))

    namen = []
    for schreibweise in ("Doku", "Doku/", "./Doku", "Doku/../Doku"):
        r = logged_in.post("/api/convert", json={"folder": schreibweise, "codec": "h265",
                                                 "type": "video", "files": [datei]})
        assert r.status_code == 200, (schreibweise, r.get_json())
        st = logged_in.get("/api/status").get_json()
        alle = ([st["task"]] if st["task"] else []) + st["queue"]
        namen.append(alle[-1]["folder"])
    gate.set()
    time.sleep(0.3)
    logged_in.post("/api/clear")
    assert set(namen) == {"Doku"}, namen


def test_convert_absolute_folder_rejected(server, logged_in, dirs):
    """Absolute Pfade im Feld "folder" werden abgelehnt (os.path.join wuerde die
    Wurzel verwerfen) - die API erwartet ausschliesslich relative Angaben."""
    os.makedirs(os.path.join(dirs["media"], "Doku"), exist_ok=True)
    r = logged_in.post("/api/convert", json={"folder": "/Doku/", "codec": "h265"})
    assert r.status_code == 400 and "Pfad" in r.get_json()["error"]
    r = logged_in.post("/api/scan", json={"folder": "/etc"})
    assert r.status_code == 400


# ─── A: Sammelauftrag über mehrere Ordner ────────────────────────────────────
def _ordner(dirs, *teile):
    p = os.path.join(dirs["media"], *teile)
    os.makedirs(p, exist_ok=True)
    return p


@pytest.fixture
def sammel(dirs):
    """Zwei Quellordner in verschiedenen Zweigen (wie zwei Volumes), einer mit Unterordner."""
    a = _ordner(dirs, "SamA", "Serien", "S01")
    a_sub = _ordner(dirs, "SamA", "Serien", "S01", "extras")
    b = _ordner(dirs, "SamB", "Filme")
    _src(os.path.join(a, "e1.mkv"), b"\x1aE\xdf\xa3" + b"0" * 96)          # 100
    _src(os.path.join(a_sub, "e2.mkv"), b"\x1aE\xdf\xa3" + b"0" * 196)     # 200
    _src(os.path.join(b, "f1.mp4"), b"ftyp" + b"0" * 296)                  # 300
    return {"a": a, "b": b, "a_rel": "SamA/Serien/S01", "b_rel": "SamB/Filme"}


def _fange_task(server, monkeypatch):
    """run_task ersetzen: der Auftrag wird nur kopiert, nie wirklich ausgeführt."""
    gefangen = []
    monkeypatch.setattr(server, "run_task", lambda t: gefangen.append(server._task_copy(t)))
    return gefangen


def _warte_auf(liste, sekunden=3):
    deadline = time.time() + sekunden
    while time.time() < deadline and not liste:
        time.sleep(0.02)
    assert liste, "Auftrag wurde nicht an run_task übergeben"
    return liste[0]


# ── A1: sammle_ordner ────────────────────────────────────────────────────────
def test_sammle_ordner_duplikate(server, sammel):
    ordner, fehler = server.sammle_ordner(["SamA/Serien/S01", "SamA/Serien/S01/",
                                           "./SamA/Serien/S01", "SamA/Serien/S01/extras/.."])
    assert ordner == [sammel["a"]] and fehler == []


def test_sammle_ordner_verschachtelt_wird_zusammengefasst(server, sammel):
    """A und A/B gewählt → nur A. Sonst liefen zwei Quellen desselben Auftrags
    auf dieselbe Datei."""
    for reihenfolge in (["SamA/Serien", "SamA/Serien/S01/extras"],
                        ["SamA/Serien/S01/extras", "SamA/Serien"]):
        ordner, fehler = server.sammle_ordner(reihenfolge)
        assert ordner == [os.path.join(server.MEDIA_ROOT, "SamA", "Serien")], reihenfolge
        assert fehler == []
    # Geschwister bleiben beide erhalten
    ordner, _ = server.sammle_ordner(["SamA/Serien/S01", "SamB/Filme"])
    assert ordner == [sammel["a"], sammel["b"]]


def test_sammle_ordner_wurzel_abgelehnt(server, sammel):
    ordner, fehler = server.sammle_ordner(["", "./", "SamA/..", "SamA/Serien/S01"])
    assert ordner == [sammel["a"]]
    assert len(fehler) == 3 and all(f.startswith("Bitte einen Unterordner wählen") for f in fehler)


def test_sammle_ordner_fehlerhafte_eintraege(server, sammel, dirs):
    datei = os.path.join(dirs["media"], "SamB", "Filme", "f1.mp4")
    ordner, fehler = server.sammle_ordner(
        ["SamA/Serien/S01", "gibtsnicht", "../oben", "/etc", "SamB/Filme/f1.mp4", 42, None])
    assert ordner == [sammel["a"]]
    assert fehler[0].startswith("Ordner nicht gefunden: gibtsnicht")
    assert fehler[1].startswith("Ungültiger Pfad: ../oben")
    assert fehler[2].startswith("Ungültiger Pfad: /etc")
    assert fehler[3].startswith("Kein Ordner: SamB/Filme/f1.mp4")
    assert len(fehler) == 6 and os.path.isfile(datei)


def test_sammle_ordner_obergrenze(server, sammel):
    assert server.MAX_ORDNER == 50
    ordner, _ = server.sammle_ordner(["SamA/Serien/S01"] * 50)
    assert ordner == [sammel["a"]]
    with pytest.raises(server.OrdnerAuswahlFehler) as e:
        server.sammle_ordner(["SamA/Serien/S01"] * 51)
    assert str(e.value) == "Höchstens 50 Ordner pro Auftrag"


def test_sammle_ordner_reihenfolge_stabil(server, sammel):
    erwartet = [sammel["a"], sammel["b"]]
    assert server.sammle_ordner(["SamB/Filme", "SamA/Serien/S01"])[0] == erwartet
    assert server.sammle_ordner(["SamA/Serien/S01", "SamB/Filme"])[0] == erwartet
    assert server.sammle_ordner([])[0] == []


def test_ordner_eingabe_folders_gewinnt(server):
    assert server.ordner_eingabe({"folder": "A", "folders": ["B", "C"]}) == ["B", "C"]
    assert server.ordner_eingabe({"folder": "A"}) == ["A"]
    assert server.ordner_eingabe({"folders": "B"}) == ["B"]
    assert server.ordner_eingabe({}) == [""]


def test_ordner_anzeige(server):
    assert server.ordner_anzeige([]) == ""
    assert server.ordner_anzeige(["Avatar/S01"]) == "Avatar/S01"
    assert server.ordner_anzeige(["Avatar/S01", "Filme", "Musik"]) == "Avatar/S01 + 2 weitere"


# ── A1: dateien_sammeln ──────────────────────────────────────────────────────
def test_dateien_sammeln_reihenfolge_und_ordnerbezug(server, sammel):
    paare = server.dateien_sammeln([sammel["b"], sammel["a"]], server.VID_EXT)
    assert [(os.path.basename(p), q) for p, q in paare] == [
        ("f1.mp4", sammel["b"]), ("e1.mkv", sammel["a"]), ("e2.mkv", sammel["a"])]


def test_dateien_sammeln_dedupliziert_ueber_realpath(server, dirs):
    echt = _ordner(dirs, "SamD", "echt")
    link = _ordner(dirs, "SamD", "verlinkt")
    quelle = _src(os.path.join(echt, "x.mkv"))
    ziel = os.path.join(link, "auch_x.mkv")
    if not os.path.lexists(ziel):
        os.symlink(quelle, ziel)
    paare = server.dateien_sammeln([echt, link], server.VID_EXT)
    assert paare == [(quelle, echt)]
    paare = server.dateien_sammeln([link, echt], server.VID_EXT)
    assert paare == [(ziel, link)]


def test_dateien_sammeln_leere_ordner_und_endungen(server, dirs, sammel):
    leer = _ordner(dirs, "SamLeer")
    assert server.dateien_sammeln([leer], server.VID_EXT) == []
    assert server.dateien_sammeln([], server.VID_EXT) == []
    _src(os.path.join(sammel["b"], "ton.mp3"))
    nur_audio = server.dateien_sammeln([sammel["b"]], server.AUD_EXT)
    assert [os.path.basename(p) for p, _ in nur_audio] == ["ton.mp3"]
    alle = server.dateien_sammeln([sammel["b"]])
    assert [os.path.basename(p) for p, _ in alle] == ["f1.mp4", "ton.mp3"]
    os.remove(os.path.join(sammel["b"], "ton.mp3"))


# ── A2: /api/scan ────────────────────────────────────────────────────────────
def test_scan_zwei_ordner_summe_und_aufschluesselung(server, logged_in, sammel):
    r = logged_in.post("/api/scan", json={"folders": ["SamB/Filme", "SamA/Serien/S01"]})
    assert r.status_code == 200
    j = r.get_json()
    assert j["total"] == 3 and j["video_count"] == 3 and j["audio_count"] == 0
    assert j["bytes"] == 600 and j["fehler"] == []
    assert j["ordner"] == [
        {"folder": "SamA/Serien/S01", "video": 2, "audio": 0, "total": 2, "bytes": 300},
        {"folder": "SamB/Filme", "video": 1, "audio": 0, "total": 1, "bytes": 300},
    ]


def test_scan_einzelordner_unveraendert(server, logged_in, sammel):
    j = logged_in.post("/api/scan", json={"folder": "SamA/Serien/S01"}).get_json()
    assert j["total"] == 2 and j["bytes"] == 300
    assert j["ordner"] == [{"folder": "SamA/Serien/S01", "video": 2, "audio": 0,
                            "total": 2, "bytes": 300}]


def test_scan_fehlerhafte_eintraege_scheitern_nicht_den_aufruf(server, logged_in, sammel):
    r = logged_in.post("/api/scan", json={"folders": ["SamA/Serien/S01", "gibtsnicht", ""]})
    assert r.status_code == 200
    j = r.get_json()
    assert j["total"] == 2 and [e["folder"] for e in j["ordner"]] == ["SamA/Serien/S01"]
    assert len(j["fehler"]) == 2


@pytest.mark.parametrize("koerper,meldung", [
    ({"folders": []}, "Kein gültiger Ordner ausgewählt"),
    ({"folders": ["nix1", "nix2"]}, "Kein gültiger Ordner ausgewählt"),
    ({"folders": ["gibtsnicht"]}, "Ordner nicht gefunden"),
    ({"folders": [""]}, "Bitte einen Unterordner wählen"),
    ({"folder": "/etc"}, "Ungültiger Pfad"),
])
def test_scan_ohne_gueltigen_ordner_400(server, logged_in, koerper, meldung):
    r = logged_in.post("/api/scan", json=koerper)
    # Jede Meldung nennt den Grund zuerst und danach eine Handlung (B7)
    assert r.status_code == 400 and r.get_json()["error"].startswith(meldung)


@pytest.mark.parametrize("url,extra", [("/api/scan", {}),
                                       ("/api/prescan", {"codec": "h265"}),
                                       ("/api/convert", {"codec": "h265"})])
def test_obergrenze_50_ordner_endpunkte(server, logged_in, sammel, url, extra):
    r = logged_in.post(url, json={"folders": ["SamA/Serien/S01"] * 51, **extra})
    assert r.status_code == 400
    assert r.get_json()["error"] == "Höchstens 50 Ordner pro Auftrag"


# ── A2/A4: /api/prescan ──────────────────────────────────────────────────────
def test_prescan_zwei_ordner_setzt_src_und_rel_dir(server, logged_in, sammel, monkeypatch):
    monkeypatch.setattr(server, "get_info",
                        lambda fp: {"vc": "h264", "ac": "aac", "dur": 1.0, "pix_fmt": "yuv420p",
                                    "is10": False, "n_video": 1, "n_audio": 1, "n_sub": 0, "size": 1})
    r = logged_in.post("/api/prescan", json={"folders": ["SamB/Filme", "SamA/Serien/S01"],
                                             "codec": "h265", "type": "video"})
    assert r.status_code == 202
    j = r.get_json()
    assert j["total"] == 3 and j["fehler"] == []
    sid = j["scan_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        st = logged_in.get(f"/api/prescan/{sid}").get_json()
        if st["status"] != "running":
            break
        time.sleep(0.02)
    assert st["status"] == "done"
    res = st["result"]
    nach_name = {i["name"]: i for i in res["to_convert"]}
    assert set(nach_name) == {"e1.mkv", "e2.mkv", "f1.mp4"}
    assert (nach_name["e1.mkv"]["src"], nach_name["e1.mkv"]["rel_dir"]) == ("SamA/Serien/S01", "")
    assert (nach_name["e2.mkv"]["src"], nach_name["e2.mkv"]["rel_dir"]) == ("SamA/Serien/S01", "extras")
    assert (nach_name["f1.mp4"]["src"], nach_name["f1.mp4"]["rel_dir"]) == ("SamB/Filme", "")
    assert res["bytes_convert"] == 600 and res["bytes_skip"] == 0
    assert [e["folder"] for e in res["ordner"]] == ["SamA/Serien/S01", "SamB/Filme"]


def test_prescan_ohne_passende_dateien_meldet_auswahl(server, logged_in, dirs, sammel):
    _ordner(dirs, "SamLeer2")
    _ordner(dirs, "SamLeer3")
    # (Bis 27.09. stand hier ein Video-Ordner im Musik-Modus – der zählt jetzt als Quelle.)
    r = logged_in.post("/api/prescan", json={"folders": ["SamLeer2", "SamLeer3"],
                                             "codec": "mp3", "type": "audio"})
    assert r.status_code == 404
    assert r.get_json()["error"] == "Keine passenden Dateien in den gewählten Ordnern"
    r = logged_in.post("/api/prescan", json={"folder": "SamLeer2", "codec": "h265"})
    assert r.status_code == 404
    assert r.get_json()["error"] == "Keine passenden Dateien in diesem Ordner"


# ── A3: /api/convert ─────────────────────────────────────────────────────────
def test_convert_sammelauftrag_task_shape(server, logged_in, sammel, monkeypatch):
    gefangen = _fange_task(server, monkeypatch)
    r = logged_in.post("/api/convert", json={"folders": ["SamB/Filme", "SamA/Serien/S01"],
                                             "codec": "h265", "type": "video", "quality": "balanced"})
    assert r.status_code == 200 and r.get_json()["total"] == 3
    t = _warte_auf(gefangen)
    assert t["folders"] == ["SamA/Serien/S01", "SamB/Filme"]
    assert t["folder"] == "SamA/Serien/S01 + 1 weitere"
    assert t["bytes_total"] == 600
    nach_name = {f["name"]: f for f in t["files"]}
    assert (nach_name["e1.mkv"]["src"], nach_name["e1.mkv"]["rel_dir"]) == ("SamA/Serien/S01", "")
    assert (nach_name["e2.mkv"]["src"], nach_name["e2.mkv"]["rel_dir"]) == ("SamA/Serien/S01", "extras")
    assert (nach_name["f1.mp4"]["src"], nach_name["f1.mp4"]["rel_dir"]) == ("SamB/Filme", "")
    logged_in.post("/api/clear")


def test_convert_einzelordner_rueckwaertskompatibel(server, logged_in, sammel, monkeypatch):
    gefangen = _fange_task(server, monkeypatch)
    r = logged_in.post("/api/convert", json={"folder": "SamA/Serien/S01", "codec": "h265"})
    assert r.status_code == 200
    t = _warte_auf(gefangen)
    assert t["folder"] == "SamA/Serien/S01" and t["folders"] == ["SamA/Serien/S01"]
    assert sorted(f["rel_dir"] for f in t["files"]) == ["", "extras"]
    assert {f["src"] for f in t["files"]} == {"SamA/Serien/S01"}
    logged_in.post("/api/clear")


def test_convert_dateiliste_bekommt_richtigen_quellordner(server, logged_in, sammel, monkeypatch):
    """Die Liste aus der Analyse darf nur Dateien unterhalb der GEWÄHLTEN Ordner
    enthalten – jede Datei kennt danach ihren eigenen Quellordner."""
    gefangen = _fange_task(server, monkeypatch)
    drin = [os.path.join(sammel["a"], "extras", "e2.mkv"),
            os.path.join(sammel["b"], "f1.mp4"),
            os.path.join(sammel["b"], "f1.mp4")]        # Doppelung
    draussen = os.path.join(server.MEDIA_ROOT, "SamA", "Serien", "S01")
    r = logged_in.post("/api/convert", json={"folders": ["SamB/Filme"], "codec": "h265",
                                             "files": drin + [os.path.join(draussen, "e1.mkv")]})
    assert r.status_code == 200 and r.get_json()["total"] == 1
    t = _warte_auf(gefangen)
    assert [f["name"] for f in t["files"]] == ["f1.mp4"]
    assert t["files"][0]["src"] == "SamB/Filme" and t["files"][0]["rel_dir"] == ""
    logged_in.post("/api/clear")


def test_convert_ohne_gueltigen_ordner_400(server, logged_in, sammel):
    r = logged_in.post("/api/convert", json={"folders": ["nix1", "nix2"], "codec": "h265"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "Kein gültiger Ordner ausgewählt"
    assert len(r.get_json()["fehler"]) == 2
    r = logged_in.post("/api/convert", json={"folders": [""], "codec": "h265"})
    assert r.status_code == 400 and r.get_json()["error"] == "Bitte einen Unterordner wählen"


def test_convert_verschachtelte_auswahl_erfasst_jede_datei_einmal(server, logged_in, sammel, monkeypatch):
    gefangen = _fange_task(server, monkeypatch)
    r = logged_in.post("/api/convert", json={"folders": ["SamA/Serien", "SamA/Serien/S01",
                                                         "SamA/Serien/S01/extras"],
                                             "codec": "h265"})
    assert r.status_code == 200
    t = _warte_auf(gefangen)
    assert t["folders"] == ["SamA/Serien"] and t["folder"] == "SamA/Serien"
    assert sorted(f["name"] for f in t["files"]) == ["e1.mkv", "e2.mkv"]
    assert sorted(f["rel_dir"] for f in t["files"]) == ["S01", "S01/extras"]
    logged_in.post("/api/clear")


# ── A4: Kollisionsprüfung über die gemischte Liste ───────────────────────────
def test_kollision_ueber_gemischte_ordnerliste(server, dirs, logged_in, monkeypatch):
    k1 = _ordner(dirs, "SamK1")
    k2 = _ordner(dirs, "SamK2")
    behalten = _src(os.path.join(k1, "a.mkv"))
    doppelt = _src(os.path.join(k1, "a.mp4"))
    anderer_ordner = _src(os.path.join(k2, "a.mp4"))
    # Pur: gleicher Zielname nur innerhalb desselben Verzeichnisses
    assert server.find_target_collisions([behalten, doppelt, anderer_ordner], ".mkv") == \
        {doppelt: "a.mkv"}
    # Und am fertigen Auftrag aus beiden Ordnern
    gefangen = _fange_task(server, monkeypatch)
    r = logged_in.post("/api/convert", json={"folders": ["SamK1", "SamK2"], "codec": "h265"})
    assert r.status_code == 200 and r.get_json()["total"] == 3
    t = _warte_auf(gefangen)
    kollisionen = server.find_target_collisions([f["path"] for f in t["files"]], ".mkv")
    assert kollisionen == {doppelt: "a.mkv"}
    logged_in.post("/api/clear")


# ── A3: Verlaufseintrag mit src ──────────────────────────────────────────────
def test_history_eintrag_bekommt_src(server, workdir, monkeypatch):
    src = _src(os.path.join(workdir, "hist.mp4"))
    t = _mk_task([src])
    t["files"][0]["src"] = "SamA/Serien/S01"
    t["files"][0]["rel_dir"] = "extras"
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json())
    _fake_ffmpeg(server, monkeypatch)
    eintraege = []
    monkeypatch.setattr(server, "append_history", lambda e: eintraege.append(e))
    server.convert_one(t, 0, src, "h265", True, "balanced")
    assert t["files"][0]["status"] == "done"
    assert eintraege[0]["src"] == "SamA/Serien/S01" and eintraege[0]["rel_dir"] == "extras"


def test_migrate_history_kennt_src(server, tmp_path):
    legacy = tmp_path / "h.json"
    jsonl = tmp_path / "h.jsonl"
    legacy.write_text(json.dumps([{"name": "x", "timestamp": 0, "src": "Filme", "rel_dir": "S01"}]))
    assert server.migrate_history(str(legacy), str(jsonl)) == 1
    eintrag = json.loads(jsonl.read_text().strip())
    assert eintrag["src"] == "Filme" and eintrag["rel_dir"] == "S01"


def test_history_route_ergaenzt_src_bei_altbestand(server, logged_in, monkeypatch):
    monkeypatch.setattr(server, "load_history",
                        lambda: [{"name": "alt.mkv", "timestamp": 1, "size_before": 2,
                                  "size_after": 1, "method": "GPU"}])
    j = logged_in.get("/api/history?limit=all").get_json()
    assert j["entries"][0]["src"] == "" and j["entries"][0]["run_id"].startswith("legacy-")


# ─── Nachbesserungen aus der Gegenprüfung ────────────────────────────────────
def test_nullbyte_reisst_die_auswahl_nicht_ab(server, logged_in, sammel):
    """Ein Nullbyte lässt `realpath` mit ValueError platzen – ein fauler Eintrag darf
    den ganzen 50er-Aufruf nicht mit 500 abreißen, er gehört in `fehler`."""
    assert server._safe_media_path("x\x00y") is None
    ordner, fehler = server.sammle_ordner(["SamA/Serien/S01", "x\x00y"])
    assert ordner == [sammel["a"]]
    assert len(fehler) == 1 and fehler[0].startswith("Ungültiger Pfad")
    r = logged_in.post("/api/scan", json={"folders": ["SamA/Serien/S01", "x\x00y"]})
    assert r.status_code == 200 and len(r.get_json()["fehler"]) == 1
    r = logged_in.post("/api/scan", json={"folder": "x\x00y"})
    assert r.status_code == 400


def test_dateien_sammeln_ueberspringt_symlink_aus_der_wurzel(server, dirs, tmp_path):
    """Die Zieldatei ersetzt die Quelle – ein Datei-Symlink aus der Wurzel heraus
    würde ffmpeg (als root) außerhalb von /media schreiben lassen."""
    draussen = tmp_path / "draussen.mkv"
    draussen.write_bytes(b"\x1aE\xdf\xa3" + b"0" * 40)
    d = _ordner(dirs, "SamLink")
    drin = _src(os.path.join(d, "drin.mkv"))
    ziel = os.path.join(d, "zeigt_raus.mkv")
    if not os.path.lexists(ziel):
        os.symlink(str(draussen), ziel)
    assert server.dateien_sammeln([d], server.VID_EXT) == [(drin, d)]


def test_convert_reicht_verlorene_ordner_durch(server, logged_in, sammel, monkeypatch):
    """Verschwindet ein gewählter Ordner zwischen Analyse und Start, läuft der Auftrag
    mit weniger Ordnern – das muss die Oberfläche erfahren können."""
    gefangen = _fange_task(server, monkeypatch)
    r = logged_in.post("/api/convert", json={"folders": ["SamA/Serien/S01", "weg"],
                                             "codec": "h265"})
    assert r.status_code == 200
    j = r.get_json()
    assert len(j["fehler"]) == 1 and j["fehler"][0].startswith("Ordner nicht gefunden: weg")
    t = _warte_auf(gefangen)
    assert t["folders"] == ["SamA/Serien/S01"]
    logged_in.post("/api/clear")


def test_fehlermeldungen_nennen_eine_handlung(server, sammel):
    """B7: jede Meldung sagt, was zu tun ist – Grund bleibt vorn."""
    assert server.mit_hinweis(server.NICHT_GEFUNDEN_ERR) == \
        "Ordner nicht gefunden – prüfe, ob das Volume noch eingebunden ist"
    assert server.mit_hinweis(server.KEIN_ORDNER_ERR) == server.KEIN_ORDNER_ERR
    _, fehler = server.sammle_ordner(["gibtsnicht"])
    assert fehler == ["Ordner nicht gefunden: gibtsnicht – prüfe, ob das Volume noch eingebunden ist"]


# ─── Seiten werden nicht zwischengespeichert (sonst zeigt der Browser nach einem
#     Update die alte Oberfläche und redet mit einem neueren Server) ───────────

def test_seiten_ohne_zwischenspeicher(server, client):
    for pfad in ("/", "/history"):
        r = client.get(pfad)
        assert r.status_code == 200, pfad
        cc = r.headers.get("Cache-Control", "")
        assert "no-cache" in cc, (pfad, cc)
        # Revalidierung bleibt möglich, damit unveränderte Seiten billig sind
        assert r.headers.get("ETag") or r.headers.get("Last-Modified"), pfad


# ─── Lauf-Speicher: Abschluss eines echten Laufs ─────────────────────────────
def test_record_run_writes_summary_of_finished_task(server, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS_FILE", str(tmp_path / "runs.jsonl"))
    t = _mk_task([], tid="fertig")
    t.update({"completed": 3, "skipped": 1, "finished_at": 1234.5,
              "bytes_before_done": 900, "bytes_after_done": 300, "target": "h265"})
    server.record_run(t)
    r = server.load_runs()[0]
    assert r["run_id"] == "fertig" and r["count"] == 2      # 3 fertig minus 1 übersprungen
    assert r["bytes_before"] == 900 and r["bytes_after"] == 300
    assert r["timestamp"] == 1234.5 and r["target"] == "h265"


def test_record_run_ignores_runs_without_conversion(server, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNS_FILE", str(tmp_path / "runs.jsonl"))
    t = _mk_task([], tid="leer")
    t.update({"completed": 2, "skipped": 2, "finished_at": 1.0})
    server.record_run(t)
    assert server.load_runs() == []


def test_run_task_records_the_run(server, workdir, monkeypatch, tmp_path):
    """Ende zu Ende: eine konvertierte Datei erzeugt genau eine Laufzusammenfassung."""
    monkeypatch.setattr(server, "RUNS_FILE", str(tmp_path / "runs.jsonl"))
    src = _src(os.path.join(workdir, "lauf.mp4"))
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(dur=10.0, size=50))
    _fake_ffmpeg(server, monkeypatch)

    server.run_task(_mk_task([src], tid="e2e"))

    runs = server.load_runs()
    assert [r["run_id"] for r in runs] == ["e2e"]
    assert runs[0]["count"] == 1 and runs[0]["bytes_after"] == 50


def test_run_task_survives_broken_run_log(server, workdir, monkeypatch):
    """Ein hängendes /config darf den Auftrag nicht scheitern lassen."""
    def boom(t):
        raise OSError("/config hängt")
    monkeypatch.setattr(server, "record_run", boom)
    t = _mk_task([], tid="runsboom")
    server.run_task(t)
    assert t["status"] == "done" and t["finished_at"]


def test_history_totals_carry_the_real_last_run(server, logged_in):
    """Die Verlaufsseite nahm als "zuletzt" den juengsten Lauf, den sie kennt.
    Nach einer Teilwiederherstellung ist das aelter als der letzte echte Lauf --
    Startseite und Verlauf widersprachen sich dadurch."""
    with server.stats_lock:
        s = server.load_stats()
        s["last_run"] = 1789030285.5
        server.save_stats(s)
    t = logged_in.get("/api/history").get_json()["totals"]
    assert t["last_run"] == 1789030285.5


# ─── Prüfung 17.09.2026 ──────────────────────────────────────────────────────
# Fund 1: Ziel darf nicht überschrieben, Quelle nicht ersetzt werden, wenn sich
# während des Encodes etwas geändert hat.
class _PopenMitNebenwirkung:
    """Fake-ffmpeg: schreibt die Zieldatei und tut nebenbei, was `dazwischen` sagt."""
    def __init__(self, cmd, dazwischen, **kw):
        with open(cmd[-1], "wb") as f:
            f.write(b"x" * 50)
        dazwischen()
        self.returncode = 0

    def poll(self):
        return 0

    def wait(self):
        return 0

    def kill(self):
        pass


def test_convert_one_target_appearing_during_encode_is_not_overwritten(server, workdir, monkeypatch):
    """Zwischen der Prüfung vor dem Encode und dem Ersetzen entsteht eine fremde
    Zieldatei – sie darf nicht überschrieben werden; Quelle bleibt, Rest ist weg."""
    src = _src(os.path.join(workdir, "race.mp4"))
    foreign = os.path.join(workdir, "race.mkv")
    t = _mk_task([src])
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(dur=10.0, size=50))

    def fremde_datei():
        with open(foreign, "wb") as f:
            f.write(b"fremd")
    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda cmd, **kw: _PopenMitNebenwirkung(cmd, fremde_datei, **kw))
    before_hist = len(server.load_history())

    server.convert_one(t, 0, src, "h265", True, "balanced")

    fi = t["files"][0]
    assert fi["status"] == "error"
    assert fi["error"] == "Zieldatei ist inzwischen entstanden: race.mkv"
    assert t["failed"] == 1 and t["completed"] == 0
    with open(foreign, "rb") as f:
        assert f.read() == b"fremd"
    with open(src, "rb") as f:
        assert f.read() == b"quelle" * 20
    assert not os.path.exists(os.path.join(workdir, "race.converting.mkv"))
    assert len(server.load_history()) == before_hist


@pytest.mark.parametrize("name", ["inplace.mkv", "extern.mp4"])     # in-place / nicht in-place
def test_convert_one_source_changed_during_encode_is_not_replaced(server, workdir, monkeypatch, name):
    """Die Quelle wächst während des Encodes (neu heruntergeladen, angehängt): das
    Ergebnis der alten Fassung darf sie nicht ersetzen. (Beide Fälle teilen sich den
    workdir – der Knotenname wird auf 40 Zeichen gekappt – deshalb verschiedene Stämme.)"""
    src = _src(os.path.join(workdir, name))
    stamm = os.path.splitext(name)[0]
    t = _mk_task([src])
    monkeypatch.setattr(server, "get_info", lambda fp: server.info_from_probe(_probe_json()))
    monkeypatch.setattr(server, "probe", lambda fp: _probe_json(dur=10.0, size=50))

    def quelle_waechst():
        with open(src, "ab") as f:
            f.write(b"NEU")
    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda cmd, **kw: _PopenMitNebenwirkung(cmd, quelle_waechst, **kw))
    before_hist = len(server.load_history())

    server.convert_one(t, 0, src, "h265", True, "balanced")

    fi = t["files"][0]
    assert fi["status"] == "error" and fi["error"].startswith("Quelle hat sich während der Konvertierung geändert")
    assert t["failed"] == 1 and t["completed"] == 0
    with open(src, "rb") as f:
        assert f.read() == b"quelle" * 20 + b"NEU"                # Quelle unangetastet
    assert not os.path.exists(os.path.join(workdir, stamm + ".converting.mkv"))
    if name.endswith(".mp4"):
        assert not os.path.exists(os.path.join(workdir, stamm + ".mkv"))
    assert len(server.load_history()) == before_hist


def test_ziel_anlegen_ohne_ueberschreiben(server, tmp_path, monkeypatch):
    tmp, dst = tmp_path / "a.converting.mkv", tmp_path / "a.mkv"
    tmp.write_bytes(b"neu")
    dst.write_bytes(b"fremd")
    with pytest.raises(server.ErsetzenAbgelehnt):
        server.ziel_anlegen_ohne_ueberschreiben(str(tmp), str(dst))
    assert dst.read_bytes() == b"fremd" and tmp.exists()
    dst.unlink()
    server.ziel_anlegen_ohne_ueberschreiben(str(tmp), str(dst))
    assert dst.read_bytes() == b"neu" and not tmp.exists()

    # Dateisystem ohne Hardlinks: Rückfall auf os.replace – aber nie über eine vorhandene Datei
    def kein_link(a, b, *rest, **kw):
        raise PermissionError("Operation not permitted")
    monkeypatch.setattr(server.os, "link", kein_link)
    tmp.write_bytes(b"neu2")
    with pytest.raises(server.ErsetzenAbgelehnt):
        server.ziel_anlegen_ohne_ueberschreiben(str(tmp), str(dst))
    assert dst.read_bytes() == b"neu" and tmp.exists()
    dst.unlink()
    server.ziel_anlegen_ohne_ueberschreiben(str(tmp), str(dst))
    assert dst.read_bytes() == b"neu2" and not tmp.exists()


# Fund 3: SIGTERM (docker stop / Update) bricht sauber ab und verbucht den Lauf.
def _job_einreihen_und_warten(server, t):
    t["status"] = "queued"
    with server.queue_cv:
        server.state["queue"].append(t)
        server.queue_cv.notify_all()
    deadline = time.time() + 3
    while time.time() < deadline:
        with server.lock:
            if server.state["current"] is t:
                return
        time.sleep(0.01)
    raise AssertionError("Worker hat den Job nicht übernommen")


def _warten_bis_frei(server):
    deadline = time.time() + 6
    while time.time() < deadline:
        with server.lock:
            if server.state["current"] is None:
                server.state["last"] = None
                return
        time.sleep(0.01)
    raise AssertionError("Worker wurde nicht fertig")


def test_laufenden_auftrag_abbrechen_verbucht_den_lauf(server, monkeypatch):
    """ffmpeg wird getötet, der Worker verbucht den Lauf (record_run) – erst dann Ende."""
    killed, verbucht = [], []

    class P:
        def kill(self):
            killed.append(True)

    def fake_run(t):
        deadline = time.time() + 3
        while not server.state["cancel"] and time.time() < deadline:
            time.sleep(0.01)
        server.record_run(t)
    monkeypatch.setattr(server, "run_task", fake_run)
    monkeypatch.setattr(server, "record_run", lambda t: verbucht.append(t["id"]))
    t = _mk_task([], tid="stopjob")
    t["_procs"] = {0: P()}
    _job_einreihen_und_warten(server, t)

    assert server.laufenden_auftrag_abbrechen(warte_s=3) is True
    assert killed == [True] and verbucht == ["stopjob"]
    with server.lock:
        assert server.state["current"] is None and server.state["cancel"] is False
        server.state["last"] = None


def test_laufenden_auftrag_abbrechen_gibt_nach_frist_auf(server, monkeypatch):
    """docker stop schickt nach 10 s SIGKILL – länger als die Frist wird nie gewartet."""
    import threading as _th
    gate = _th.Event()
    monkeypatch.setattr(server, "run_task", lambda t: gate.wait(5))
    t = _mk_task([], tid="zaehjob")
    _job_einreihen_und_warten(server, t)
    t0 = time.time()
    assert server.laufenden_auftrag_abbrechen(warte_s=0.3) is False
    assert time.time() - t0 < 2
    gate.set()
    _warten_bis_frei(server)


def test_laufenden_auftrag_abbrechen_ohne_auftrag(server):
    with server.lock:
        assert server.state["current"] is None
    assert server.laufenden_auftrag_abbrechen(warte_s=0.1) is True


def test_sigterm_handler_beendet_mit_systemexit(server):
    """SystemExit lässt waitress sauber schließen – kein os._exit, kein Hänger."""
    with pytest.raises(SystemExit):
        server._sigterm(15, None)


def test_converting_reste_aufraeumen(server, tmp_path):
    """Beim Start: alte X.converting.<ext> weg – nicht aber frische (laufender Encode),
    Systemordner, Punkt-Ordner, Symlinks oder Dateien ohne Medienendung."""
    alt, frisch = server._START_TS - 60, server._START_TS + 60
    root = tmp_path / "medien"
    for d in ("Filme", ".versteckt", "proc"):
        (root / d).mkdir(parents=True)
    rest = root / "Filme" / "a.converting.mkv"
    laeuft = root / "Filme" / "b.converting.mkv"
    echt = root / "Filme" / "film.mkv"
    fremd = root / "Filme" / "notiz.converting.txt"
    versteckt = root / ".versteckt" / "c.converting.mkv"
    system = root / "proc" / "d.converting.mkv"
    for pfad in (rest, laeuft, echt, fremd, versteckt, system):
        pfad.write_bytes(b"x")
        os.utime(pfad, (alt, alt))
    os.utime(laeuft, (frisch, frisch))
    link = root / "Filme" / "l.converting.mkv"
    link.symlink_to(echt)

    weg = server.converting_reste_aufraeumen(str(root))

    assert weg == [str(rest)]
    assert not rest.exists()
    for pfad in (laeuft, echt, fremd, versteckt, system):
        assert pfad.exists(), pfad
    assert link.is_symlink()


# Fund 5: reine Skip-Läufe zählen weder in stats.json noch in runs.jsonl.
def test_skip_only_run_counts_nowhere(server, tmp_path, monkeypatch):
    stats, runs = tmp_path / "stats.json", tmp_path / "runs.jsonl"
    monkeypatch.setattr(server, "STATS_FILE", str(stats))
    monkeypatch.setattr(server, "RUNS_FILE", str(runs))
    t = _mk_task([], tid="nurskip")
    t.update(completed=2, skipped=2, finished_at=123.0)
    server.update_cumulative_stats(t)
    server.record_run(t)
    assert not stats.exists() and not runs.exists()

    t2 = _mk_task([], tid="gemischt")
    t2.update(completed=3, skipped=2, bytes_before_done=100, bytes_after_done=40, finished_at=124.0)
    server.update_cumulative_stats(t2)
    server.record_run(t2)
    s = server.load_stats(str(stats))
    assert (s["runs"], s["files_converted"], s["files_skipped"]) == (1, 1, 2)
    r = server.load_runs(str(runs))
    assert len(r) == 1 and r[0]["count"] == 1


# Fund 6: stats.json atomar; beim Start aus runs.jsonl nachrechnen.
def test_save_stats_is_atomic(server, tmp_path, monkeypatch):
    stats = tmp_path / "stats.json"
    stats.write_text('{"runs": 7}')
    monkeypatch.setattr(server, "STATS_FILE", str(stats))
    echt = server.json.dump

    def boom(*a, **kw):
        raise OSError("No space left on device")
    monkeypatch.setattr(server.json, "dump", boom)
    with pytest.raises(OSError):
        server.save_stats({"runs": 8})
    assert json.loads(stats.read_text()) == {"runs": 7}          # alter Stand unversehrt

    monkeypatch.setattr(server.json, "dump", echt)
    server.save_stats({"runs": 8})
    assert json.loads(stats.read_text()) == {"runs": 8}
    assert not (tmp_path / "stats.json.tmp").exists()


def test_stats_reparieren_rebuilds_from_runs(server, tmp_path):
    stats, runs = tmp_path / "stats.json", tmp_path / "runs.jsonl"
    stats.write_text("{kaputt")
    runs.write_text(json.dumps({"run_id": "r1", "timestamp": 100, "count": 2,
                                "bytes_before": 900, "bytes_after": 400}) + "\n"
                    + json.dumps({"run_id": "vor-verlauf", "timestamp": 50, "count": 5,
                                  "bytes_before": 5000, "bytes_after": 2000, "runs": 3}) + "\n")
    assert server.stats_reparieren(str(stats), str(runs)) is True
    assert json.loads(stats.read_text()) == {
        "runs": 4, "files_converted": 7, "files_skipped": 0,
        "bytes_before": 5900, "bytes_after": 2400, "last_run": 100}
    assert server.stats_reparieren(str(stats), str(runs)) is False   # jetzt intakt


def test_stats_reparieren_laesst_intakte_und_leere_in_ruhe(server, tmp_path):
    stats, runs = tmp_path / "stats.json", tmp_path / "runs.jsonl"
    stats.write_text('{"runs": 1, "files_converted": 1}')
    runs.write_text(json.dumps({"run_id": "r1", "timestamp": 1, "count": 9,
                                "bytes_before": 9, "bytes_after": 1}) + "\n")
    assert server.stats_reparieren(str(stats), str(runs)) is False
    assert json.loads(stats.read_text()) == {"runs": 1, "files_converted": 1}
    leer = tmp_path / "leer.json"
    assert server.stats_reparieren(str(leer), str(tmp_path / "fehlt.jsonl")) is False
    assert not leer.exists()


# Fund 7: Nullbyte im Pfad darf keinen 500er auslösen.
def test_dateien_aus_auswahl_ignores_nullbyte_paths(server, dirs):
    ordner = os.path.join(dirs["media"], "NB")
    os.makedirs(ordner, exist_ok=True)
    ok = _src(os.path.join(ordner, "ok.mp4"))
    try:
        treffer = server.dateien_aus_auswahl(
            [os.path.join(ordner, "a\x00b.mp4"), ok], [ordner], server.VID_EXT)
        assert [p for p, _ in treffer] == [os.path.realpath(ok)]
    finally:
        os.remove(ok)


def test_delete_file_nullbyte_gives_400(server, logged_in, dirs):
    r = logged_in.post("/api/delete-file", json={"path": os.path.join(dirs["media"], "a\x00b.mp4")})
    assert r.status_code == 400


# Fund 9: auth.hash nur für den Eigentümer lesbar – beim Anlegen und beim Passwortwechsel.
def _modus(p):
    return oct(os.stat(p).st_mode & 0o777)


def test_init_auth_creates_hash_with_0600(server, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(server, "AUTH_HASH_FILE", str(tmp_path / "auth.hash"))
    monkeypatch.setattr(server, "AUTH_USER_FILE", str(tmp_path / "auth.user"))
    user, h = server.init_auth()
    assert user == "admin" and server.bcrypt.checkpw(b"admin", h)
    assert _modus(tmp_path / "auth.hash") == "0o600"
    os.chmod(tmp_path / "auth.hash", 0o644)               # zu weit – wird beim Start repariert
    server.init_auth()
    assert _modus(tmp_path / "auth.hash") == "0o600"


def test_change_password_keeps_hash_private(server, logged_in, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AUTH_HASH_FILE", str(tmp_path / "auth.hash"))
    alt = server.PW_HASH
    try:
        r = logged_in.post("/api/change-credentials",
                           json={"current_password": "admin", "password": "admin123"})
        assert r.status_code == 200
        assert _modus(tmp_path / "auth.hash") == "0o600"
        assert server.bcrypt.checkpw(b"admin123", (tmp_path / "auth.hash").read_bytes())
    finally:
        server.PW_HASH = alt          # die anderen Tests melden sich weiter mit admin/admin an


# ─── MC_ROOTS: nur freigegebene Wurzelordner sichtbar und nutzbar ────────────
def test_mc_roots_versteckt_fremde_volumes_auf_der_wurzel(server, monkeypatch):
    monkeypatch.setattr(server, "MC_ROOTS", ("volume1",))
    folders, system = server.split_dirs(["volume1", "volume2", "volume3", "etc"], True)
    assert folders == ["volume1"]
    assert set(system) == {"volume2", "volume3", "etc"}
    # unterhalb der Wurzel greift die Liste nicht
    folders, _ = server.split_dirs(["Filme", "volume2"], False)
    assert folders == ["Filme", "volume2"]


def test_mc_roots_sperrt_pfade_ausserhalb(server, tmp_path, monkeypatch):
    (tmp_path / "volume1" / "Filme").mkdir(parents=True)
    (tmp_path / "volume2" / "Backups").mkdir(parents=True)
    monkeypatch.setattr(server, "MEDIA_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "MC_ROOTS", ("volume1",))
    assert server._safe_media_path("") == os.path.realpath(str(tmp_path))
    assert server._safe_media_path("volume1/Filme") == os.path.realpath(str(tmp_path / "volume1" / "Filme"))
    assert server._safe_media_path("volume2") is None
    assert server._safe_media_path("volume2/Backups") is None
    assert server._safe_media_path("volume1/../volume2") is None


def test_mc_roots_leer_erlaubt_alles(server, tmp_path, monkeypatch):
    (tmp_path / "volume2").mkdir()
    monkeypatch.setattr(server, "MEDIA_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "MC_ROOTS", ())
    assert server._safe_media_path("volume2") is not None
    folders, _ = server.split_dirs(["volume1", "volume2"], True)
    assert folders == ["volume1", "volume2"]


# ─── Ton aus Video (27.09.2026) ──────────────────────────────────────────────
def _video_info(ac="aac", audio_streams=None):
    return {"vc": "h264", "ac": ac, "dur": 10.0, "pix_fmt": "yuv420p", "is10": False,
            "bits": None, "n_video": 1, "n_audio": len(audio_streams or [1]), "n_sub": 1,
            "size": 120, "sub_streams": [{"index": 2, "codec": "subrip"}],
            "audio_streams": audio_streams or [{"index": 1, "codec": ac, "disp": {"default": True}}]}

def _audio_probe(codec="mp3"):
    return {"streams": [{"index": 0, "codec_type": "audio", "codec_name": codec}],
            "format": {"duration": "10.0", "size": "50"}}


def test_ton_herausloesen_nur_video_im_audio_modus(server):
    assert server.ton_herausloesen("/m/film.mkv", False) is True
    assert server.ton_herausloesen("/m/film.MP4", False) is True
    assert server.ton_herausloesen("/m/k.wav", False) is False
    assert server.ton_herausloesen("/m/film.mkv", True) is False


def test_quell_endungen_audio_modus_nimmt_videos(server):
    assert server.quell_endungen("video") == server.VID_EXT
    assert server.quell_endungen("tvfix") == server.VID_EXT
    assert server.quell_endungen("audio") == server.VID_EXT | server.AUD_EXT


def test_cmd_audio_kopie_und_spurwahl(server):
    cmd = server.cmd_audio("s.mkv", "d.m4a", "aac", "p", kopie=True, spur=1)
    j = " ".join(cmd)
    assert "-c:a copy" in j and "-b:a" not in j
    assert "-map 0:a:1" in j and "-vn" in j and "-sn" in j and "-dn" in j
    # Reine Audiodateien: unverändert ohne -map, mit Encoder
    cmd = server.cmd_audio("s.wav", "d.mp3", "mp3", "p")
    assert "-map 0:a" not in " ".join(cmd) and _after(cmd, "-c:a") == "libmp3lame"


def test_prescan_check_video_im_audio_modus_nie_uebersprungen(server, workdir, monkeypatch):
    v = _src(os.path.join(workdir, "film.mkv"), b"\x1aE\xdf\xa3" + b"2" * 20)
    w = _src(os.path.join(workdir, "k.wav"), b"RIFF" + b"1" * 20)
    monkeypatch.setattr(server, "get_info",
                        lambda fp: _video_info("aac") if fp.endswith(".mkv") else
                        {"vc": None, "ac": "aac", "dur": 1.0, "n_video": 0, "n_audio": 1, "n_sub": 0, "size": 1})
    kind, item = server.prescan_check(v, workdir, "aac", False)
    assert kind == "convert" and item["codec"] == "aac"
    # Echte Audiodatei im Zielformat wird weiterhin übersprungen
    kind, _ = server.prescan_check(w, workdir, "aac", False)
    assert kind == "skip"


def test_convert_one_ton_aus_video_quelle_bleibt(server, workdir, monkeypatch):
    """Video → MP3: Ergebnis liegt neben dem Video, das Video bleibt, die Bilanz meldet
    keine Schein-Ersparnis (Video 120 B → MP3 50 B wäre −58 %, gespart ist nichts)."""
    src = _src(os.path.join(workdir, "film.mkv"))
    t = _mk_task([src], target="mp3", ctype="audio")
    streams = [{"index": 1, "codec": "ac3", "disp": {"default": False}},
               {"index": 2, "codec": "aac", "disp": {"default": True}}]
    monkeypatch.setattr(server, "get_info", lambda fp: _video_info("ac3", streams))
    monkeypatch.setattr(server, "probe", lambda fp: _audio_probe("mp3"))
    calls = _fake_ffmpeg(server, monkeypatch)
    hist = []
    monkeypatch.setattr(server, "append_history", lambda e: hist.append(e))

    server.convert_one(t, 0, src, "mp3", False, "balanced")

    j = " ".join(calls[0])
    assert "-vn" in j and "-map 0:a:1" in j and "-c:a libmp3lame" in j
    assert calls[0][-1] == os.path.join(workdir, "film.converting.mp3")
    fi = t["files"][0]
    assert fi["status"] == "done" and fi["quelle_bleibt"] is True
    assert os.path.exists(os.path.join(workdir, "film.mp3")) and os.path.exists(src)
    assert t["bytes_saved"] == 0 and t["bytes_before_done"] == 0 and t["progress"] == 100
    assert hist[0]["quelle_bleibt"] is True and hist[0]["size_before"] == hist[0]["size_after"] == 50
    assert hist[0]["path"] == os.path.join(workdir, "film.mp3")


def test_convert_one_ton_aus_video_gleicher_codec_wird_kopiert(server, workdir, monkeypatch):
    """Video mit AAC-Ton → AAC: Spur wird kopiert, nicht neu kodiert (nie schlechter)."""
    src = _src(os.path.join(workdir, "clip.mp4"))
    t = _mk_task([src], target="aac", ctype="audio")
    monkeypatch.setattr(server, "get_info", lambda fp: _video_info("aac"))
    monkeypatch.setattr(server, "probe", lambda fp: _audio_probe("aac"))
    calls = _fake_ffmpeg(server, monkeypatch)

    server.convert_one(t, 0, src, "aac", False, "balanced")

    assert len(calls) == 1 and "-c:a copy" in " ".join(calls[0])
    fi = t["files"][0]
    assert fi["status"] == "done" and fi["method"] == "Kopie"
    assert os.path.exists(os.path.join(workdir, "clip.m4a")) and os.path.exists(src)


def test_convert_one_ton_aus_video_kopie_faellt_auf_encode_zurueck(server, workdir, monkeypatch):
    src = _src(os.path.join(workdir, "clip.mkv"))
    t = _mk_task([src], target="aac", ctype="audio")
    monkeypatch.setattr(server, "get_info", lambda fp: _video_info("aac"))
    monkeypatch.setattr(server, "probe", lambda fp: _audio_probe("aac"))
    calls, rcs = [], [1, 0]

    class P:
        def __init__(self, cmd, **kw):
            self.returncode = rcs.pop(0)
            calls.append(list(cmd))
            if self.returncode == 0:
                with open(cmd[-1], "wb") as f:
                    f.write(b"x" * 50)
        def poll(self): return self.returncode
        def wait(self): return self.returncode
        def kill(self): pass
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kw: P(cmd, **kw))

    server.convert_one(t, 0, src, "aac", False, "balanced")

    assert len(calls) == 2
    assert "-c:a copy" in " ".join(calls[0]) and "-c:a aac" in " ".join(calls[1])
    assert t["files"][0]["status"] == "done" and t["files"][0]["method"] == "CPU"
    assert os.path.exists(src)


def test_convert_one_ton_aus_video_zieldatei_vorhanden_ist_fehler(server, workdir, monkeypatch):
    src = _src(os.path.join(workdir, "film.mkv"))
    _src(os.path.join(workdir, "film.mp3"))
    t = _mk_task([src], target="mp3", ctype="audio")
    monkeypatch.setattr(server, "get_info", lambda fp: _video_info("aac"))
    calls = _fake_ffmpeg(server, monkeypatch)
    server.convert_one(t, 0, src, "mp3", False, "balanced")
    assert not calls and t["files"][0]["status"] == "error"
    assert "existiert bereits" in t["files"][0]["error"] and os.path.exists(src)


def test_prescan_audio_modus_zaehlt_videos(server, logged_in, dirs, monkeypatch):
    media = dirs["media"]
    d = os.path.join(media, "PA")
    os.makedirs(d, exist_ok=True)
    _src(os.path.join(d, "film.mp4"), b"ftyp" + b"1" * 50)
    _src(os.path.join(d, "song.mp3"), b"ID3" + b"1" * 20)
    monkeypatch.setattr(server, "get_info",
                        lambda fp: _video_info("aac") if fp.endswith(".mp4") else
                        {"vc": None, "ac": "mp3", "dur": 1.0, "n_video": 0, "n_audio": 1, "n_sub": 0, "size": 1})
    r = logged_in.post("/api/prescan", json={"folder": "PA", "codec": "mp3", "type": "audio"})
    assert r.status_code == 202
    sid = r.get_json()["scan_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        j = logged_in.get(f"/api/prescan/{sid}").get_json()
        if j["status"] != "running":
            break
        time.sleep(0.02)
    res = j["result"]
    assert [i["name"] for i in res["to_convert"]] == ["film.mp4"]
    assert [i["name"] for i in res["to_skip"]] == ["song.mp3"]
    assert res["ordner"][0]["video"] == 1 and res["ordner"][0]["audio"] == 1


def test_convert_one_ton_aus_video_kopie_richtet_sich_nach_standardspur(server, workdir, monkeypatch):
    """Erste Spur AC3, Standard-Spur AAC → Ziel AAC: die STANDARD-Spur zählt, also Kopie.
    (Echt-Probe im Container fand am 27.09. den Fehler: es wurde info["ac"] = erste Spur geprüft.)"""
    src = _src(os.path.join(workdir, "film.mkv"))
    t = _mk_task([src], target="aac", ctype="audio")
    streams = [{"index": 1, "codec": "ac3", "disp": {"default": False}},
               {"index": 2, "codec": "aac", "disp": {"default": True}}]
    monkeypatch.setattr(server, "get_info", lambda fp: _video_info("ac3", streams))
    monkeypatch.setattr(server, "probe", lambda fp: _audio_probe("aac"))
    calls = _fake_ffmpeg(server, monkeypatch)
    server.convert_one(t, 0, src, "aac", False, "balanced")
    j = " ".join(calls[0])
    assert "-c:a copy" in j and "-map 0:a:1" in j and t["files"][0]["method"] == "Kopie"
    # und der Prescan nennt den Codec der Standard-Spur
    kind, item = server.prescan_check(src, workdir, "aac", False)
    assert kind == "convert" and item["codec"] == "aac"
