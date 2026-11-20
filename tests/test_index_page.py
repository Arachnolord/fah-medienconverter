# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Die Startseite darf beim Laden nicht sterben -- und muss die Bilanz zeigen.

Dieselbe DOM-Attrappe wie beim Verlauf: das Seitenskript laeuft wirklich, und
`getElementById` kennt nur die IDs, die im HTML VOR dem Skript stehen. Genau so
ist die Verlaufsseite schon einmal beim Laden abgestuerzt.

Dazu die Umstellung der Bilanz: die Punkte-Leiste war nicht lesbar, an ihre
Stelle treten die drei Kacheln aus dem Verlauf.
"""
from pathlib import Path

import pytest

from domattrappe import NODE, fuehre_seite_aus, skript_und_start

SEITE = Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"


@pytest.mark.skipif(not NODE, reason="node nicht vorhanden")
def test_startseite_ueberlebt_das_laden(tmp_path):
    r = fuehre_seite_aus(SEITE, tmp_path)
    assert r.returncode == 0, (
        "Das Seitenskript stirbt beim Laden -- die Startseite bliebe leer:\n"
        + (r.stderr or r.stdout)[-1500:])
    assert '"ok":true' in r.stdout


def test_kein_toplevel_zugriff_auf_spaetere_elemente():
    """Zweite Sicherung ohne node -- der Absturzgrund von damals."""
    html, skript, start = skript_und_start(SEITE)
    import re
    danach = {m.group(1) for m in re.finditer(r'\bid="([^"]+)"', html[start:])}
    vorher = {m.group(1) for m in re.finditer(r'\bid="([^"]+)"', html[:start])}
    nur_danach = danach - vorher
    schuldig = []
    for zeile in skript.split("\n"):
        if zeile.startswith((" ", "\t", "}", "/", "*")) or not zeile.strip():
            continue
        for eid in nur_danach:
            if f"$('{eid}')" in zeile or f'getElementById("{eid}")' in zeile:
                schuldig.append(zeile.strip()[:80])
    assert not schuldig, f"Top-Level-Zugriff auf spaeter definierte Elemente: {schuldig}"


# ─── Bilanz: Kacheln statt Punkte ───────────────────────────────────────────
def test_punkte_leiste_ist_verschwunden():
    """Die 6px-Bloecke sagten niemandem etwas -- weder Markup noch CSS bleiben uebrig."""
    html = SEITE.read_text(encoding="utf-8")
    assert 'id="rcGrid"' not in html
    assert '.reclaim-grid' not in html


def test_startseite_hat_die_drei_kacheln():
    html = SEITE.read_text(encoding="utf-8")
    for eid in ("tFiles", "tSaved", "tPct", "tFilesSub", "tSavedSub", "tPctSub",
                "tSavedLbl", "tPctLbl"):
        assert f'id="{eid}"' in html, f"Kachel-Element {eid} fehlt"


def test_kacheln_zeigen_dieselben_begriffe_wie_der_verlauf():
    """Beide Seiten muessen dasselbe sagen, sonst wirken es zwei Statistiken."""
    html = SEITE.read_text(encoding="utf-8")
    verlauf = (SEITE.parent / "history.html").read_text(encoding="utf-8")
    for wort in ("Dateien", "Gespart", "Reduktion"):
        assert wort in html and wort in verlauf


def test_kacheln_werden_beim_zeichnen_gefuellt():
    """renderTiles muss aus renderReclaim heraus laufen, sonst blieben die Kacheln leer."""
    html = SEITE.read_text(encoding="utf-8")
    assert "function renderTiles(" in html
    assert "renderTiles()" in html


def test_zuwachs_bekommt_eigene_beschriftung():
    """Wird es groesser statt kleiner, darf unter 'Gespart' kein Pluswert stehen."""
    html = SEITE.read_text(encoding="utf-8")
    assert "Zusätzlich belegt" in html and "Zuwachs" in html


# ─── Breite Monitore ────────────────────────────────────────────────────────
def test_bilanzleiste_und_inhalt_haben_denselben_deckel():
    """Die Leiste lief über die volle Fensterbreite, der Inhalt darunter war auf
    1480 px zentriert -- auf einem 3440-px-Schirm stand die Auftragsspalte dadurch
    mitten im Bild. Beide brauchen dieselbe Begrenzung."""
    html = SEITE.read_text(encoding="utf-8")
    assert ".reclaim-in{" in html, "die Leiste hat keinen begrenzten Innenbereich"
    import re
    deckel = set(re.findall(r"max-width:(\d+)px", html))
    assert "1480" not in deckel, "der alte 1480er-Deckel steht noch"
    assert "1900" in deckel


def test_auftragsspalte_waechst_auf_sehr_breiten_schirmen():
    html = SEITE.read_text(encoding="utf-8")
    assert "min-width:1800px" in html


def test_kopfzeile_ist_buendig_zum_inhalt():
    """Auf 3440 px klebte das Logo am Fensterrand, waehrend die Bilanz darunter
    erst bei 770 px begann. Der Streifen bleibt vollflaechig, sein Inhalt endet
    dort, wo auch der Hauptbereich endet."""
    html = SEITE.read_text(encoding="utf-8")
    regel = html[html.index(".top{"):]
    regel = regel[:regel.index("}")]
    assert "1900px" in regel, "die Kopfzeile kennt den Deckel des Hauptbereichs nicht"


# ─── TV-Reparatur wählbar (Prüfbefund K1, 24.09.2026) ───────────────────────
_CODECS_MIT_TV = {
    "video": {"h265": {"label": "H.265 (HEVC)", "gpu": False, "hint": "", "saving": "", "containers": ["mkv"]}},
    "audio": {"aac": {"label": "AAC", "hint": "", "ext": ".m4a"}},
    "tvfix": {"tvton": {"label": "TV-Ton ergänzen (Dolby Digital)", "hint": "Erklärung", "ext": ".mkv",
                        "saving": "+ ca. 5 MB je Minute"}},
    "containers": {"mkv": {"label": "MKV", "ext": ".mkv", "hint": ""}},
    "gpu": False, "gpu_caps": {},
    "qualities": [{"key": "fast", "label": "S", "hint": ""}, {"key": "balanced", "label": "A", "hint": ""}],
    "authenticated": True,
}
_TV_NACHLAUF = r"""
document.createTextNode=(t)=>{const e=mkEl('#text');e.textContent=String(t);return e;};
(async()=>{
  await initApp();
  picks=[{path:'volume1/Filme',total:3,bytes:100,video:3,audio:0}];
  setType('tvfix');
  const out={codec, fmtCards: $('fmt').children.length, goDisabled: $('goBtn').disabled,
             qualHidden: $('qualSec').classList.contains('hidden'), warn: $('warnTxt').textContent};
  setType('video');
  out.zurueck={codec, qualHidden: $('qualSec').classList.contains('hidden'), goDisabled: $('goBtn').disabled};
  console.log('PROBE '+JSON.stringify(out));process.exit(0);
})().catch(e=>(console.log('PROBE-ERR '+e.stack),process.exit(0)));
"""


@pytest.mark.skipif(not NODE, reason="node nicht vorhanden")
def test_tv_reparatur_ist_waehlbar_und_startbar(tmp_path):
    import json
    r = fuehre_seite_aus(SEITE, tmp_path, antwort=_CODECS_MIT_TV, nachlauf=_TV_NACHLAUF)
    zeile = [z for z in r.stdout.splitlines() if z.startswith(("PROBE ", "PROBE-ERR"))]
    assert zeile and zeile[-1].startswith("PROBE "), r.stdout[-1500:] + r.stderr[-800:]
    o = json.loads(zeile[-1][6:])
    assert o["codec"] == "tvton" and o["fmtCards"] == 1, "Profil muss als Karte erscheinen und gewählt sein"
    assert o["goDisabled"] is False, "Analyse muss startbar sein"
    assert o["qualHidden"] is True and "Lippensynchron" in o["warn"]
    assert o["zurueck"]["codec"] == "h265" and o["zurueck"]["qualHidden"] is False
