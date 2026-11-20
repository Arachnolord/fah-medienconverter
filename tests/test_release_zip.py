# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Welche Release-ZIP gewinnt? Die mit der hoechsten (N) -- nicht die juengste.

"fah-converter (1).zip" ist NEUER als "fah-converter.zip", auch wenn deren
Aenderungszeit spaeter liegt. Dazu ein Ganzlauf von manage.sh gegen ein
nachgebautes docker: hoechste (N) wird eingespielt, --force-recreate greift,
aeltere Fassungen werden weggeraeumt, eine ausdruecklich genannte ZIP bleibt.
"""
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "tools"))
import release_zip                                           # noqa: E402

B = "fah-converter.zip"


def _ordner(tmp_path, *dateien):
    jetzt = time.time()
    for name, alter in dateien:
        p = tmp_path / name
        p.write_bytes(b"PK")
        os.utime(p, (jetzt - alter, jetzt - alter))
    return str(tmp_path)


def _name(p):
    return os.path.basename(p) if p else None


@pytest.mark.parametrize("datei,erwartet", [
    ("fah-converter.zip", 0), ("fah-converter (1).zip", 1),
    ("fah-converter(2).zip", 2), ("fah-converter (12).zip", 12),
    ("fah-converter (3) (1).zip", 4), ("FAH-Converter (1).ZIP", 1),
    ("fah-downloader (1).zip", None), ("medienconverter_release.zip", None),
    ("fah-converter.zip.bak", None),
])
def test_namensmuster(datei, erwartet):
    assert release_zip.nummer(datei, B) == erwartet


def test_hoechste_nummer_schlaegt_juengere_zeit(tmp_path):
    d = _ordner(tmp_path, ("fah-converter.zip", 10), ("fah-converter (1).zip", 3600))
    assert _name(release_zip.neueste(d, B)) == "fah-converter (1).zip"


def test_reihenfolge(tmp_path):
    d = _ordner(tmp_path, ("fah-converter.zip", 1), ("fah-converter (1).zip", 7200),
                ("fah-converter (2).zip", 3600))
    assert [_name(p) for p in release_zip.kandidaten(d, B)] == [
        "fah-converter (2).zip", "fah-converter (1).zip", "fah-converter.zip"]


def test_numerisch_nicht_alphabetisch(tmp_path):
    d = _ordner(tmp_path, ("fah-converter (10).zip", 100), ("fah-converter (9).zip", 1))
    assert _name(release_zip.neueste(d, B)) == "fah-converter (10).zip"


def test_gleiche_nummer_dann_zeit(tmp_path):
    d = _ordner(tmp_path, ("fah-converter (3).zip", 100), ("fah-converter(3).zip", 5))
    assert _name(release_zip.neueste(d, B)) == "fah-converter(3).zip"


def test_fremde_dateien_zaehlen_nicht(tmp_path):
    d = _ordner(tmp_path, ("fah-converter (1).zip", 10), ("fah-downloader (5).zip", 1),
                ("medienconverter_release_20260829.zip", 1))
    assert _name(release_zip.neueste(d, B)) == "fah-converter (1).zip"


def test_leer_und_fehlend():
    assert release_zip.neueste("/gibt/es/nicht", B) is None


def test_aufraeumen_behaelt_die_eingespielte(tmp_path):
    d = _ordner(tmp_path, ("fah-converter.zip", 300), ("fah-converter (1).zip", 200),
                ("fah-converter (2).zip", 100), ("fah-downloader (1).zip", 50))
    sieger = release_zip.neueste(d, B)
    weg = release_zip.aufraeumen(d, B, sieger)
    assert sorted(_name(p) for p in weg) == ["fah-converter (1).zip", "fah-converter.zip"]
    assert os.path.isfile(sieger)
    assert (tmp_path / "fah-downloader (1).zip").is_file()


def test_gegenprobe_alte_zeitregel_waere_falsch(tmp_path):
    d = _ordner(tmp_path, ("fah-converter.zip", 10), ("fah-converter (1).zip", 3600))
    nach_zeit = max(release_zip.kandidaten(d, B), key=os.path.getmtime)
    assert _name(nach_zeit) == "fah-converter.zip"
    assert _name(release_zip.neueste(d, B)) == "fah-converter (1).zip"


# ── manage.sh im Ganzen ─────────────────────────────────────────────────────
@pytest.fixture
def werkbank(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    protokoll = tmp_path / "docker.log"
    (fake_bin / "docker").write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{protokoll}"\n'
        'case "$1" in\n'
        '  compose) [ "$2" = "version" ] && exit 0 ;;\n'
        '  exec) exit 1 ;;\n'          # kein ffmpeg, keine config-Sicherung
        '  port) echo "0.0.0.0:8686"; exit 0 ;;\n'
        'esac\nexit 0\n', encoding="utf-8")
    (fake_bin / "docker").chmod(0o755)
    (fake_bin / "docker-compose").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (fake_bin / "docker-compose").chmod(0o755)
    shutil.copy(BASE / "manage.sh", tmp_path / "manage.sh")
    (tmp_path / "manage.sh").chmod(0o755)
    (tmp_path / "docker-compose.yaml").write_text(
        "services:\n  media-converter:\n    container_name: media-converter\n",
        encoding="utf-8")
    return tmp_path, fake_bin, protokoll


def _release(pfad, marke):
    with zipfile.ZipFile(pfad, "w") as z:
        for f in ("manage.sh", "Dockerfile", "docker-compose.yaml.example",
                  "tools/release_zip.py", "app/playback.py"):
            z.write(BASE / f, f)
        z.writestr("app/MARKE.txt", marke)
        z.writestr("config/geheim.txt", "DARF NICHT LANDEN")
        z.writestr("docker-compose.yaml", "DARF NICHT LANDEN")


def _lauf(werk, fake_bin, *args):
    return subprocess.run(
        ["sh", "-c", f'PATH="{fake_bin}:$PATH" MC_START_VERSUCHE=1 ./manage.sh update '
         + " ".join(args)],
        cwd=werk, capture_output=True, text=True, timeout=180)


def test_manage_sh_ganzlauf(werkbank):
    werk, fake_bin, protokoll = werkbank
    alt, neu = werk / "fah-converter.zip", werk / "fah-converter (1).zip"
    _release(alt, "ALT")
    _release(neu, "NEU")
    os.utime(alt, (time.time(), time.time()))
    os.utime(neu, (time.time() - 3600, time.time() - 3600))
    # Das nachgebaute docker "antwortet" nie -- der Lauf endet mit der
    # Warnung und Rueckgabe 1. Alles davor muss trotzdem passiert sein.
    r = _lauf(werk, fake_bin)
    ausgabe = r.stdout + r.stderr
    assert "Update aus:" in ausgabe and "(1).zip" in ausgabe.split("Update aus:")[1].split("\n")[0]
    assert (werk / "app" / "MARKE.txt").read_text() == "NEU"
    assert "--force-recreate" in protokoll.read_text()
    assert neu.is_file() and not alt.exists()
    assert not (werk / "config" / "geheim.txt").exists()
    assert (werk / "docker-compose.yaml").read_text().startswith("services:")
    assert (werk / "tools" / "release_zip.py").is_file()


def test_manage_sh_ausdrueckliche_zip_bleibt(werkbank):
    werk, fake_bin, protokoll = werkbank
    (werk / "tools").mkdir(exist_ok=True)
    shutil.copy(BASE / "tools" / "release_zip.py", werk / "tools" / "release_zip.py")
    eigene = werk / "fah-converter-eigene.zip"
    _release(eigene, "EIGENE")
    _release(werk / "fah-converter (4).zip", "VIER")
    _lauf(werk, fake_bin, f'"{eigene}"')
    assert (werk / "app" / "MARKE.txt").read_text() == "EIGENE"
    assert eigene.is_file()


# ── Lizenz-Entscheidung: Basisimage bewusst gepinnt (17.09.2026) ───────────
def test_basisimage_bewusst_gepinnt():
    """lscr.io/linuxserver/ffmpeg ist ein GPL+nonfree-Build; der CPU-Rueckfall
    braucht libx264/libx265 daraus. Bewusst so belassen -- nur Eigenbetrieb, nie
    weitergeben. Wer das Image wechselt, muss die Lizenzfrage neu beantworten:
    deshalb faellt jede Aenderung an dieser Zeile hier auf."""
    erste = (BASE / "Dockerfile").read_text(encoding="utf-8").splitlines()[0]
    assert erste == "FROM lscr.io/linuxserver/ffmpeg:8.1.2-cli-ls73"
    readme = (BASE / "README.md").read_text(encoding="utf-8")
    assert "GPL" in readme and "nicht weitergeben" in readme
