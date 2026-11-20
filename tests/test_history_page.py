# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Die Verlaufsseite darf beim Laden nicht sterben.

Genau das ist passiert: Das Player-Skript griff beim Laden auf das Overlay
zu, das im HTML erst NACH dem Skript steht. `null.addEventListener` warf, das
ganze Skript brach ab -- und der Verlauf blieb leer, obwohl alle Daten da
waren. `node --check` findet so etwas nicht, das ist ein Laufzeitfehler.

Deshalb wird das Skript hier in einer schlanken DOM-Attrappe wirklich
AUSGEFUEHRT: getElementById kennt nur die Elemente, die im HTML VOR dem
Skript stehen -- alles andere liefert null, wie im echten Browser.
"""
import re
from pathlib import Path

import pytest

from domattrappe import NODE, fuehre_seite_aus, skript_und_start

SEITE = Path(__file__).resolve().parent.parent / "app" / "static" / "history.html"


def _skript_und_start():
    return skript_und_start(SEITE)


@pytest.mark.skipif(not NODE, reason="node nicht vorhanden")
def test_verlaufsseite_ueberlebt_das_laden(tmp_path):
    r = fuehre_seite_aus(SEITE, tmp_path)
    assert r.returncode == 0, (
        "Das Seitenskript stirbt beim Laden -- der Verlauf bliebe leer:\n"
        + (r.stderr or r.stdout)[-1500:])
    assert '"ok":true' in r.stdout


# Nach dem Laden (fetch ist async) den Stand von Listenzaehler, Loeschknopf und
# Dateien-Kachel ausgeben. setTimeout ist in der Attrappe stillgelegt, darum globalThis.
_NACH_DEM_LADEN = """
globalThis.setTimeout(() => {
  const g = id => document.getElementById(id);
  console.log('STAND ' + JSON.stringify({ sub: g('listSub').textContent,
    aus: g('clearBtn').disabled, kachel: g('tFiles').textContent }));
}, 50);
"""


def _stand_nach_dem_laden(tmp_path, antwort):
    import json
    r = fuehre_seite_aus(SEITE, tmp_path, antwort=antwort, nachlauf=_NACH_DEM_LADEN)
    assert r.returncode == 0, (r.stderr or r.stdout)[-1500:]
    zeile = [z for z in r.stdout.splitlines() if z.startswith("STAND ")]
    assert zeile, "die Seite hat nach dem Laden nichts gerendert:\n" + r.stdout[-800:]
    return json.loads(zeile[-1][len("STAND "):])


def _antwort(list_total, bilanz):
    return {"entries": [], "runs": [], "total": 0, "filtered_total": 0,
            "list_total": list_total, "page": 1, "pages": 1, "limit": 20,
            "totals": {"count": bilanz, "bytes_before": 196656371099,
                       "bytes_after": 56672246362, "last_run": 1789112862.9}}


@pytest.mark.skipif(not NODE, reason="node nicht vorhanden")
def test_geleerte_liste_zeigt_keinen_dateizaehler(tmp_path):
    """Nach "Verlauf löschen" stand "87 Dateien im Verlauf" über der leeren Liste --
    der Zähler las die Bilanz statt der Liste. Die Bilanz-Kachel behält ihre 87,
    der Listenzähler verschwindet, und zum Leeren gibt es nichts mehr."""
    s = _stand_nach_dem_laden(tmp_path, _antwort(list_total=0, bilanz=87))
    assert s["sub"] == "", f"über der leeren Liste steht noch {s['sub']!r}"
    assert s["aus"] is True, "„Verlauf löschen“ bleibt bei leerer Liste klickbar"
    assert s["kachel"] == "87", "die Bilanz-Kachel darf das Leeren nicht mitmachen"


@pytest.mark.skipif(not NODE, reason="node nicht vorhanden")
def test_listenzaehler_zaehlt_eintraege_nicht_bilanz(tmp_path):
    """Die Bilanz kennt auch Läufe ohne Dateieinträge (Sammeleintrag) -- der
    Listenzähler darf nur zählen, was in der Liste steht."""
    s = _stand_nach_dem_laden(tmp_path, _antwort(list_total=74, bilanz=87))
    assert s["sub"] == "74 Dateien im Verlauf"
    assert s["aus"] is False
    assert s["kachel"] == "87"


def test_overlay_wird_nicht_beim_laden_angefasst():
    """Zweite Sicherung, ohne node: kein Top-Level-Zugriff auf Elemente, die
    erst nach dem Skript im HTML stehen."""
    html, skript, start = _skript_und_start()
    danach = {m.group(1) for m in re.finditer(r'\bid="([^"]+)"', html[start:])}
    vorher = {m.group(1) for m in re.finditer(r'\bid="([^"]+)"', html[:start])}
    nur_danach = danach - vorher
    schuldig = []
    for zeile in skript.split("\n"):
        # Nur Zeilen, die beim Laden laufen: keine Einrueckung = Top-Level.
        if zeile.startswith((" ", "\t", "}", "/", "*")) or not zeile.strip():
            continue
        for eid in nur_danach:
            if f"$('{eid}')" in zeile or f'getElementById("{eid}")' in zeile:
                schuldig.append(zeile.strip()[:80])
    assert not schuldig, f"Top-Level-Zugriff auf spaeter definierte Elemente: {schuldig}"


def test_zeile_zeigt_die_konvertierte_datei():
    """Der Eintrag traegt den Pfad der NEUEN Datei -- die Zeile muss ihn zeigen,
    nicht den alten Quellnamen mit der alten Endung."""
    html = SEITE.read_text(encoding="utf-8")
    assert "split('/').pop()" in html
    assert "'aus '+f.name" in html


# ─── Gezieltes Löschen ───────────────────────────────────────────────────────
def test_einzelne_zeile_laesst_sich_loeschen():
    """Bisher gab es nur "alles weg". Jede Zeile braucht ihren eigenen Papierkorb."""
    html = SEITE.read_text(encoding="utf-8")
    assert "function delEntry(" in html
    assert "'/history/delete'" in html


def test_einzelloeschung_schickt_alle_drei_schluessel():
    """Nur run_id + path + timestamp zusammen treffen genau eine Zeile -- Dateinamen
    wiederholen sich ueber Laeufe hinweg."""
    html = SEITE.read_text(encoding="utf-8")
    stelle = html[html.index("function delEntry("):]
    rumpf = stelle[:stelle.index("\n}")]
    for schluessel in ("run_id", "path", "timestamp"):
        assert schluessel in rumpf, f"delEntry sendet {schluessel} nicht"


def test_ganzer_lauf_laesst_sich_aus_der_liste_nehmen():
    html = SEITE.read_text(encoding="utf-8")
    assert "function delRun(" in html
    assert "'/history/delete-run'" in html


def test_loeschen_fragt_vorher_nach():
    """Ein Fehlklick darf keinen Verlauf kosten."""
    html = SEITE.read_text(encoding="utf-8")
    for fn in ("function delEntry(", "function delRun("):
        stelle = html[html.index(fn):]
        rumpf = stelle[:stelle.index("\n}")]
        assert "confirm(" in rumpf, f"{fn} fragt nicht nach"


def test_leere_liste_erklaert_die_stehengebliebene_bilanz():
    """Nach dem Leeren stehen die Kacheln weiter -- die Liste muss sagen, warum,
    sonst sieht der geleerte Verlauf neben einer vollen Bilanz nach Fehler aus.

    Geprueft wird der eigene Leerzustand, nicht statisches Markup: die Seite baut
    alle Leerzustaende bewusst per DOM-API, ohne HTML-Strings."""
    html = SEITE.read_text(encoding="utf-8")
    assert "function emptyCleared(" in html
    assert "emptyCleared()" in html
    stelle = html[html.index("function emptyCleared("):]
    rumpf = stelle[:stelle.index("\n}")]
    assert "Bilanz" in rumpf, "der Leerzustand erklaert die stehengebliebenen Zahlen nicht"


def test_sammellauf_wird_als_frueher_ausgewiesen():
    """Der Sammeleintrag aus der Migration fasst viele Laeufe zusammen. Als
    "Lauf vom 10.09." getarnt behauptet ein 130-GB-Balken, an einem Tag entstanden
    zu sein -- er muss als frueherer Bestand kenntlich sein und darf nicht filtern
    (er hat naturgemaess keine Dateieintraege)."""
    html = SEITE.read_text(encoding="utf-8")
    assert "vor-verlauf" in html, "die Seite kennt den Sammeleintrag nicht"
    assert "Frühere Läufe" in html
    stelle = html[html.index("function runLabel("):]
    rumpf = stelle[:stelle.index("\n}")]
    assert "GAP_RUN" in rumpf, "runLabel behandelt den Sammeleintrag nicht gesondert"
    # der Sammelbalken darf nicht filtern -- er traegt keine Dateieintraege
    chart = html[html.index("function renderChart("):]
    assert "GAP_RUN" in chart[:chart.index("chartBars=bars")], \
        "das Diagramm zeichnet den Sammeleintrag wie einen normalen Lauf"


def test_kaestchen_leiste_ist_verschwunden():
    """Die 6px-Blöcke samt Legende sagten niemandem etwas -- im Verlauf so wenig
    wie auf der Startseite. Die Bilanz steht in den Kacheln."""
    html = SEITE.read_text(encoding="utf-8")
    assert 'id="blocks"' not in html
    assert ".reclaim-blocks" not in html
    assert "function blockUnit(" not in html
    assert "BLOCK_UNITS" not in html


def test_kopfzeile_ist_buendig_zum_inhalt():
    """Wie auf der Startseite: der Kopfstreifen laeuft voll, sein Inhalt nicht."""
    html = SEITE.read_text(encoding="utf-8")
    regel = html[html.index(".top{"):]
    regel = regel[:regel.index("}")]
    assert "1400px" in regel


def test_sammeleintrag_erfindet_keinen_zeitraum():
    """Der Migrationseintrag kennt die Daten der zusammengefassten Laeufe nicht.
    Der Tooltip zeigte trotzdem "10.09.2026 - 10.09.2026" und behauptete damit,
    alle zehn Laeufe waeren an einem Tag gewesen. Er muss den fehlenden Zeitraum
    benennen statt einen zu erfinden -- und den Titel des Balkens uebernehmen,
    nicht fest "aeltere Laeufe" schreiben."""
    html = SEITE.read_text(encoding="utf-8")
    chart = html[html.index("function renderChart("):html.index("chartBars=bars")]
    assert "first:r.timestamp" not in chart, "der Sammeleintrag bekommt einen erfundenen Zeitraum"
    assert "gap:true" in chart, "der Sammeleintrag ist nicht als solcher gekennzeichnet"
    tip = html[html.index("function showTip("):]
    tip = tip[:tip.index("\n}")]
    assert "b.gap" in tip, "der Tooltip unterscheidet die beiden Sammelarten nicht"
    assert "ältere Läufe (" not in tip, "der Tooltip schreibt den Titel fest, statt b.label zu nehmen"
    assert "Zeitraum unbekannt" in html


def test_zuletzt_kommt_aus_der_gesamtbilanz():
    """Sonst behauptet der Verlauf ein aelteres Datum als die Startseite."""
    html = SEITE.read_text(encoding="utf-8")
    rumpf = html[html.index("function renderReclaim(){"):]
    rumpf = rumpf[:rumpf.index("\n}")]
    assert "last_run" in rumpf, "renderReclaim nimmt weiter den juengsten bekannten Lauf"


def test_laufzahl_rechnet_den_sammeleintrag_voll_mit():
    """Der Sammeleintrag steht fuer mehrere Laeufe. Als einer gezaehlt meldete der
    Verlauf "6 Laeufe", waehrend die Startseite aus derselben Bilanz "10" las."""
    html = SEITE.read_text(encoding="utf-8")
    assert "function laufZahl(" in html
    rumpf = html[html.index("function laufZahl("):]
    rumpf = rumpf[:rumpf.index("\n}") if "\n}" in rumpf[:400] else 400]
    assert "runs" in rumpf
    # keine Stelle zaehlt mehr blind die Eintraege der Liste
    assert "plural(runsAll.length" not in html


# ─── Seitenzahlen im Verlauf (Wunsch 24.09.2026) ────────────────────────────
_SEITEN_NACHLAUF = """
globalThis.setTimeout(() => {
  const g = id => document.getElementById(id);
  const knoepfe = g('pgNums').children.map(k => ({ t: k.textContent, an: k.className.includes('on'),
                                                   aus: !!k.disabled, klick: typeof k.onclick === 'function' }));
  const listen = [[1,1],[1,5],[3,5],[6,12],[1,12],[12,12],[4,12]].map(([c,t]) => seitenListe(c,t).join(' '));
  let gesprungen = null;
  const alt = gotoPage; gotoPage = p => { gesprungen = p; };
  springeZuSeite('999'); const zuGross = gesprungen;
  springeZuSeite('3'); const drei = gesprungen; gesprungen = null;
  springeZuSeite('abc'); const quatsch = gesprungen;
  gotoPage = alt;
  console.log('SEITEN ' + JSON.stringify({ knoepfe, sichtbar: g('pager').style.display,
    feld: g('pgJump').value, max: g('pgJump').max, tot: g('pgTot').textContent,
    listen, zuGross, drei, quatsch }));
}, 50);
"""


@pytest.mark.skipif(not NODE, reason="node nicht vorhanden")
def test_seitenzahlen_direkt_anspringen(tmp_path):
    import json
    antwort = _antwort(list_total=240, bilanz=240)
    antwort.update({"page": 6, "pages": 12, "limit": 20})
    r = fuehre_seite_aus(SEITE, tmp_path, antwort=antwort, nachlauf=_SEITEN_NACHLAUF)
    assert r.returncode == 0, (r.stderr or r.stdout)[-1500:]
    z = [x for x in r.stdout.splitlines() if x.startswith("SEITEN ")]
    assert z, r.stdout[-800:]
    s = json.loads(z[-1][len("SEITEN "):])
    assert s["sichtbar"] == "flex"
    assert [k["t"] for k in s["knoepfe"]] == ["1", "…", "4", "5", "6", "7", "8", "…", "12"]
    aktuell = [k for k in s["knoepfe"] if k["an"]]
    assert len(aktuell) == 1 and aktuell[0]["t"] == "6" and aktuell[0]["aus"], "aktuelle Seite markiert, nicht klickbar"
    assert all(k["klick"] for k in s["knoepfe"] if k["t"] not in ("6", "…")), "jede andere Seitenzahl anspringbar"
    assert (s["feld"], s["max"], s["tot"]) == ("6", "12", "12")
    assert s["listen"] == ["1", "1 2 3 4 5", "1 2 3 4 5", "1 … 4 5 6 7 8 … 12",
                           "1 2 3 … 12", "1 … 10 11 12", "1 2 3 4 5 6 … 12"]
    assert s["zuGross"] == 12, "Eingabe über der letzten Seite springt auf die letzte"
    assert s["drei"] == 3
    assert s["quatsch"] is None, "keine Zahl → kein Sprung"
