#!/usr/bin/env python3
# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Welche Release-ZIP ist die neueste? Wird von manage.sh benutzt.

Browser haengen an eine erneut heruntergeladene Datei " (1)", " (2)", ... an,
statt sie zu ueberschreiben. Die Datei mit der HOECHSTEN Nummer ist damit
immer der juengste Download -- und zwar unabhaengig von der Aenderungszeit:
wer eine aeltere ZIP anfasst oder kopiert, verschiebt deren Zeitstempel, die
Nummer bleibt. Deshalb entscheidet zuerst die Nummer, die Zeit nur bei
gleicher Nummer.

Aufruf:
    release_zip.py latest <verzeichnis> <basisname.zip>      -> Pfad oder nichts
    release_zip.py list   <verzeichnis> <basisname.zip>      -> alle, neueste zuerst
    release_zip.py cleanup <verzeichnis> <basisname.zip> <behalte-pfad>
                                                             -> aeltere loeschen
"""
import os
import re
import sys


def _muster(basisname: str):
    """Regex fuer 'basis.zip', 'basis (1).zip', 'basis(2).zip', 'basis (3) (1).zip'."""
    stamm = basisname[:-4] if basisname.lower().endswith(".zip") else basisname
    return re.compile(r"^" + re.escape(stamm) + r"(?:\s*\((\d+)\))*\.zip$",
                      re.IGNORECASE)


def nummer(dateiname: str, basisname: str):
    """(Nummer, passt) -- 0 fuer die nackte Datei, sonst die hoechste Klammerzahl.

    'basis (3) (1).zip' kommt vor, wenn der Browser eine bereits nummerierte
    Datei ein weiteres Mal ablegt; dann zaehlt die Summe, damit sie nach (3)
    einsortiert wird.
    """
    m = _muster(basisname).match(dateiname)
    if not m:
        return None
    zahlen = [int(z) for z in re.findall(r"\((\d+)\)", dateiname)]
    return sum(zahlen) if zahlen else 0


def kandidaten(verzeichnis: str, basisname: str) -> list:
    """Alle passenden ZIPs, NEUESTE ZUERST: hoechste Nummer, dann juengste Zeit."""
    treffer = []
    try:
        namen = os.listdir(verzeichnis)
    except OSError:
        return []
    for name in namen:
        n = nummer(name, basisname)
        if n is None:
            continue
        pfad = os.path.join(verzeichnis, name)
        if not os.path.isfile(pfad):
            continue
        try:
            mtime = os.path.getmtime(pfad)
        except OSError:
            mtime = 0.0
        treffer.append((n, mtime, pfad))
    treffer.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [t[2] for t in treffer]


def neueste(verzeichnis: str, basisname: str):
    k = kandidaten(verzeichnis, basisname)
    return k[0] if k else None


def aufraeumen(verzeichnis: str, basisname: str, behalten: str) -> list:
    """Alle aelteren Fassungen entfernen -- die eingespielte bleibt liegen.

    Sonst wuerde beim naechsten Aufruf ohne Argument dieselbe (N)-Datei erneut
    gewinnen und ein zweites Mal deployt, obwohl laengst eine neuere ohne
    Klammer daneben liegt.
    """
    weg = []
    behalten_real = os.path.realpath(behalten)
    for pfad in kandidaten(verzeichnis, basisname):
        if os.path.realpath(pfad) == behalten_real:
            continue
        try:
            os.remove(pfad)
            weg.append(pfad)
        except OSError:
            pass
    return weg


def main(argv) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2
    befehl, verzeichnis, basisname = argv[1], argv[2], argv[3]
    if befehl == "latest":
        p = neueste(verzeichnis, basisname)
        if p:
            print(p)
        return 0
    if befehl == "list":
        for p in kandidaten(verzeichnis, basisname):
            print(p)
        return 0
    if befehl == "cleanup":
        if len(argv) < 5:
            print("cleanup braucht den Pfad der Datei, die bleiben soll")
            return 2
        for p in aufraeumen(verzeichnis, basisname, argv[4]):
            print(f"  [ZIP] aeltere Fassung entfernt: {os.path.basename(p)}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
