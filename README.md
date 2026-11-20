# Media Converter

## Was es tut

Kleines Web-Werkzeug für das NAS: Es konvertiert deine Videos (H.265, H.264, AV1, VP9, MPEG-4) und
Audiodateien (MP3, AAC, FLAC, Opus …) mit ffmpeg – über die Intel-GPU (VAAPI), wenn sie den Codec kann,
sonst über den Prozessor. Du wählst im Browser einen Ordner, ein Zielformat und eine Qualitätsstufe.
Der Konverter analysiert erst, was überhaupt zu tun ist, zeigt dann live den Fortschritt je Datei und
führt Verlauf und Statistik („zurückgewonnener Speicherplatz“).

Mehrere Aufträge landen in einer Warteschlange und laufen nacheinander ab. Dateien, die schon im
Zielformat vorliegen, werden übersprungen; defekte Dateien werden gemeldet und lassen sich aus der
Oberfläche löschen.

## Start & Update

Release-Datei ist **`fah-converter.zip`**. Ins Deploy-Verzeichnis legen, dann:

```sh
# Erstinstallation: manage.sh aus der ZIP holen (kein unzip nötig)
python3 -c "import zipfile;zipfile.ZipFile('fah-converter.zip').extract('manage.sh')"
chmod +x manage.sh
./manage.sh update            # neueste fah-converter*.zip einspielen
./manage.sh update datei.zip  # eine bestimmte ZIP
./manage.sh watch             # auf neue ZIPs warten und automatisch deployen
./manage.sh start|stop|restart|status|logs|url
```

- **Welche ZIP gewinnt?** Browser hängen beim erneuten Herunterladen ` (1)`,
  ` (2)` … an. Die **höchste Nummer** ist der jüngste Download und wird
  bevorzugt – unabhängig von der Änderungszeit. Nach dem Einspielen räumt
  `manage.sh` die älteren Fassungen weg; eine ausdrücklich angegebene Datei
  bleibt immer liegen.
- Der Container wird mit **`--force-recreate`** ersetzt, damit neue Funktionen
  aus der ZIP sicher ankommen.
- `config/` (Zugangsdaten, Verlauf, Statistik) und `docker-compose.yaml`
  werden **nie** angefasst; vorher wird nach `_backup_<stempel>/` gesichert.
- Läuft gerade eine Konvertierung, bricht das Update ab, statt sie zu
  vernichten.

## Sicherheit bei Internet-Freigabe

- **Nur hinter einem Reverse-Proxy mit HTTPS freigeben**, dazu `COOKIE_SECURE=1` und `TRUST_PROXY=1` setzen.
  Der Port 8686 selbst gehört nicht direkt ins Internet.
- Der Container läuft als root und sieht das gesamte gemountete Dateisystem – wer sich anmeldet, kann
  Mediendateien im ganzen Mount konvertieren und ersetzen. Behandle den Zugang wie einen Admin-Zugang.
- Login ist Pflicht. Nach 5 Fehlversuchen in 10 Minuten wird die IP für 60 s gesperrt, jede weitere
  Sperre verdoppelt sich (max. 15 min). Über alle IPs zusammen greift zusätzlich eine globale Bremse.
- Sitzungs-Cookies sind `HttpOnly` und `SameSite=Lax`; der Secret Key liegt persistent in
  `config/secret.key` (Rechte 0600), Sitzungen überleben also einen Neustart.
- Löschen aus der Oberfläche ist auf Video-/Audio-Endungen beschränkt, Symlinks werden nie gelöscht.
- Passwort später ändern: Zahnrad-Menü → Zugangsdaten.

## Was beim Konvertieren passiert

- **Alle Spuren bleiben erhalten.** Sämtliche Audiospuren, Untertitel, Anhänge (Schriften für ASS-Untertitel),
  Kapitel und Metadaten werden mitgenommen.
- **Das Dateiformat ist wählbar.** Neben dem Codec bestimmst du den Zielcontainer. Angeboten wird nur,
  was der jeweilige Codec auch verträgt – MKV steht überall zur Verfügung und ist die Vorauswahl:

  | Codec | Mögliche Zielformate |
  |---|---|
  | H.265 (HEVC) | MKV · MP4 · MOV |
  | H.264 (AVC) | MKV · MP4 · MOV |
  | AV1 | MKV · MP4 · WebM |
  | VP9 | MKV · WebM |
  | MPEG-4 | MKV · MP4 · AVI |

  Der Container entscheidet mit, was mit Ton und Untertiteln passiert – nicht jeder nimmt jeden Codec auf.
  Tonspuren, die das Zielformat nicht kopieren kann, werden **umgewandelt statt weggelassen**
  (MP4/MOV → AAC, WebM → Opus, AVI → MP3); alle anderen werden unverändert übernommen.
  Untertitel folgen derselben Logik: MKV kopiert, was geht, und wandelt Textformate nach SRT;
  MP4/MOV wandeln Text nach `mov_text`, WebM nach WebVTT, AVI kann keine. Bildbasierte Untertitel
  (PGS, DVD) passen nur in MKV. Anhänge/Schriften nimmt ebenfalls nur MKV auf.
  Was weggelassen werden musste, steht in der Datei-Info – gezählt wird nur, was auch geplant war,
  ein bewusst verworfener Untertitel gilt also nicht als Spurenverlust.

  > Im Zweifel **MKV**: nimmt jeden Codec, jede Tonspur und jeden Untertitel unverändert auf.
  > **MP4** nur, wenn ein Gerät oder Programm es verlangt.
- **Danach wird nachgezählt.** Fehlt hinterher eine Audio- oder Untertitelspur oder ist die Ausgabe deutlich
  kürzer als die Quelle, gilt der Versuch als fehlgeschlagen: Die temporäre Datei wird gelöscht,
  **das Original bleibt unangetastet** und die Datei erscheint als Fehler.
- **Das Original wird ersetzt.** Erst nach bestandener Prüfung schiebt der Konverter die neue Datei
  atomar an die Stelle der Quelle. Es gibt kein Papierkorb-Original – die alte Datei ist danach weg.
  Eine bereits vorhandene fremde Zieldatei wird nie überschrieben, die Datei wird stattdessen gemeldet –
  auch dann, wenn sie erst **während** der Konvertierung entstanden ist. Ändert sich die Quelle
  währenddessen (neu heruntergeladen, angehängt), wird ebenfalls nicht ersetzt: das Ergebnis der alten
  Fassung wandert in den Papierkorb, die neue Quelle bleibt.
- **Zwischen-Dateien heißen `<name>.converting.<endung>`.** Sie entstehen neben der Quelle und werden
  nach der Prüfung an deren Stelle geschoben. Der Konverter behandelt solche Dateien nie als Quelle –
  bleibt nach einem Absturz eine liegen, taucht sie in Analyse und Ordnerzählung nicht auf und wird
  beim nächsten Lauf derselben Datei sowie beim nächsten Start des Containers entfernt.
- **`docker stop` und Updates brechen sauber ab.** Der Server fängt SIGTERM: laufende ffmpeg-Prozesse
  werden beendet, der Lauf wird als abgebrochen in Bilanz und Verlauf verbucht, die Quellen bleiben
  unangetastet. Das muss in den 10 s passieren, die Docker vor dem SIGKILL lässt.
- **Besitzer und Zeitstempel bleiben.** Die neue Datei übernimmt Besitzer, Gruppe, Rechte und
  Änderungszeit der Quelle – Freigaben und „zuletzt geändert“-Sortierungen bleiben so, wie sie waren.

## Ton aus Video (Musik-Modus)

Seit 27.09.2026 nimmt die Auswahl *Audio* auch Videodateien als Quelle. Aus jedem
Video wird die **Standard-Tonspur** (sonst die erste) als eigene Datei in das gewählte
Format geschrieben – `Film.mkv` → `Film.mp3` im selben Ordner. **Das Video bleibt
unverändert liegen**; nur echte Audiodateien werden wie bisher ersetzt. Hat die Spur
schon den Zielcodec (AAC-Ton → AAC), wird sie unverändert kopiert statt neu kodiert
(Verfahren „Kopie“, bei Muxer-Problemen Rückfall auf Neukodierung). Bild, Untertitel
und Datenspuren kommen nicht mit. Liegt die Tondatei schon da, ist das ein Fehler –
nichts wird überschrieben. In Bilanz und Verlauf zählt so ein Lauf **nicht** als
Ersparnis (das Video ist ja noch da); der Verlauf zeigt „Ton herausgelöst, Video bleibt“.
Echt-Probe mit ffmpeg: `tools/pruef_ton_aus_video.py` (im Container, siehe Kopfkommentar).

## TV-Reparatur: „TV-Ton ergänzen“

Für Fernseher-Apps, die DTS/TrueHD nicht abspielen können (z. B. Samsung ab
Modelljahr 2023 – der Film bleibt bei Direct Play **stumm**, und nur den Ton
umrechnen lassen friert dort häufig ein). Auswahl unter *Format → TV-Reparatur*.

- Betroffen ist eine MKV, wenn ihre **Haupttonspur** nicht TV-tauglich ist (DTS,
  TrueHD, AC3/E-AC3 mit mehr als 5.1 Kanälen) oder eine andere Spur die
  Standard-Markierung trägt. Haupttonspur = erste *normale* deutsche Spur (sonst
  die Sprache der ersten normalen Spur); Kommentar-, Audiodeskriptions- und
  Hörgeschädigten-Spuren (Markierung oder Titel) werden nie Haupttonspur. Die
  Analyse zeigt alle anderen als „Bereits TV-tauglich“; andere Container als MKV
  werden übersprungen, die Endung bleibt exakt erhalten (auch „.MKV“).
- Gibt es in der Sprache schon eine taugliche Spur (z. B. AC3 hinter DTS), wird sie
  **verlustfrei nach vorn gezogen** und Standard. Sonst entsteht aus der
  Haupttonspur eine **Dolby-Digital-Spur (AC3, 640 kbit/s, max. 5.1)** als **erste
  Spur + Standard** – die Samsung-App spielt nur die erste/Standard-Spur direkt ab,
  Plex wählt die erste Spur der bevorzugten Sprache.
- **Bild, Originalton (bleibt dahinter erhalten), Untertitel, Anhänge, Cover,
  Kapitel** werden nur kopiert – nie neu kodiert (Cover-Bilder werden vorab
  gesichert und wieder angehängt). Die Datei wird um ca. 5 MB je Minute größer;
  vorher wird geprüft, dass Platz für Datei + 10 % + 5 GB Reserve frei ist.
- **Prüfung vor dem Ersetzen** – jede Auffälligkeit verwirft das Ergebnis, das
  Original bleibt unberührt und die Laufanzeige nennt den Grund:
  - Spuren (Video, Ton +1, Untertitel, Anhänge, Cover), erste Spur = AC3 mit
    Standard-Markierung, Länge der neuen Spur = Länge der Vorlage (±0,5 s),
    keine Dekodierfehler der Vorlage (Lücken würden als Stille hörbar);
  - **Lippensynchronität:** an sechs Stellen (nie direkt am Dateianfang) je 8 s
    der neuen Spur und ihrer Vorlage dekodieren und über die Lautstärke-Hüllkurve
    (2,5-ms-Raster) vergleichen. Verworfen wird bei mehr als **20 ms** Versatz,
    schlechter Übereinstimmung (< 0,75), stummer neuer Spur bei lauter Vorlage oder
    fehlgeschlagener Messung; mindestens zwei Stellen müssen bewertbar sein.
  - Erfolgreiche Dateien tragen das Ergebnis im Verlauf, z. B. „synchron (max.
    2.5 ms an 6 Stellen)“. Ein Abbruch während der Prüfung ersetzt nichts.
- Echt-Probe im Container: `tools/pruef_tvton.py` (erzeugt DTS-Testfilme, prüft
  Spuren und Synchronität; Gegenproben mit konstantem 60/40-ms-Versatz, Teil-Versatz
  und Teil-Stille ab Filmmitte müssen abgelehnt werden; Cover-Anhang, kurze Datei;
  optional Trockenlauf an einer echten Datei, Original wird nie angefasst).

## Bilanz und Verlauf

Die Seiten zeigen zwei verschiedene Dinge, die bisher aneinanderhingen:

- **Die Bilanz** – zurückgewonnener Speicherplatz, Anzahl Dateien, Reduktion – lebt in
  `config/stats.json` und `config/runs.jsonl`. Je abgeschlossenem Lauf kommt eine
  Zusammenfassung dazu (Zeitpunkt, Ziel, Anzahl, Bytes vorher/nachher).
- **Die Dateiliste** steht in `config/history.jsonl`, eine Zeile je konvertierter Datei.

Löschen räumt immer nur die Liste, nie die Bilanz. Das gilt für den Papierkorb an
jeder Zeile, für **Lauf leeren** im Gruppenkopf und für **Verlauf löschen**: die Kacheln,
die Rückgewinn-Leiste und das Diagramm bleiben danach unverändert stehen. Ein geleerter
Lauf behält deshalb seinen Balken im Diagramm.

Beim ersten Start nach dem Update legt der Server `runs.jsonl` einmalig an – aus dem
vorhandenen Verlauf, plus einem Sammeleintrag für alles, was `stats.json` darüber hinaus
kennt. Der erscheint im Diagramm als **Frühere Läufe (N)** und ist nicht anklickbar, weil
zu ihm keine Einzeleinträge mehr existieren. Wer den Verlauf vorher geleert hatte, bekommt
so seine Gesamtzahlen zurück, nicht aber die einzelnen Läufe.

Die Startseite zeigt dieselben drei Kacheln wie der Verlauf (Dateien, Gespart, Reduktion);
ein laufender Auftrag wird dort mitgezählt.

## Ansehen & anhören im Verlauf

Jede konvertierte Datei hat im Verlauf einen **▶-Knopf** (bei Tonspuren ein
Noten-Symbol). Er öffnet die Datei direkt im Browser – kein Download, kein
Umweg über einen externen Player.

MP4, WebM, MOV und alle gängigen Audioformate gehen unverändert an den Browser,
inklusive Spulen (HTTP-Range). **MKV kann kein verbreiteter Browser abspielen** –
genau der Container, der beim Konvertieren am robustesten ist. Statt eines
schwarzen Players, der die Datei stumpf lädt, packt der Server sie vorher um:
Bild wird Byte für Byte übernommen (`-c:v copy`), nur die Hülle wechselt nach
MP4 bzw. WebM. Das dauert Sekunden statt Stunden und kostet keine Qualität. Nur
eine Tonspur, die in der Zielhülle nicht erlaubt ist (AC3, DTS, TrueHD), wird –
und ausschließlich sie – nach AAC bzw. Opus gewandelt.

Das Ergebnis liegt als Zwischendatei in `config/playcache`. Erst dadurch lässt
sich **spulen**: ein direkt durchgereichter ffmpeg-Strom kann keine
Range-Anfragen beantworten. Der Zwischenspeicher räumt sich selbst auf, älteste
Datei zuerst; die Obergrenze steht in `MC_PLAY_CACHE_GB` (Standard 4 GB).

**HEVC funktioniert** – und das ist hier der Normalfall, denn der Konverter gibt
standardmäßig H.265 aus. Safari spielt es seit je, Chrome/Edge ab Version 107,
Firefox ab 137, jeweils mit Hardware-Decoder. Die MP4-Hülle bekommt dabei den
Tag `hvc1`; ohne ihn schreibt ffmpeg `hev1` und **Safari weigert sich, die Datei
überhaupt anzufassen**. Ob der Browser vor Ort wirklich kann, fragt die
Oberfläche ihn selbst (`canPlayType`) – ist die Antwort negativ, sagt sie es
offen, statt minutenlang umsonst umzupacken; ein Versuch bleibt trotzdem
möglich.

Bei Bildspuren, die wirklich kein Browser darstellt (MPEG-4 Part 2, WMV), wäre
das keine Umpackung mehr, sondern echtes Neukodieren – dann bietet die
Oberfläche den Download an.

> Mit `MC_ROOTS=volume1,volume2` (kommagetrennt) siehst und nutzt du nur diese Ordner direkt
> unter `/media` – sinnvoll, wenn das ganze NAS eingehängt ist; leer = alle Ordner.
>
> Ausgeliefert wird ausschließlich, was unterhalb von `MC_MEDIA_ROOT` liegt und
> eine bekannte Medienendung trägt. Symbolische Links werden aufgelöst und
> geprüft, ein Nullbyte im Pfad ergibt einen sauberen Fehler statt eines
> Serverabsturzes.

## Qualitätsstufen

| Stufe | Wofür | Technisch |
|---|---|---|
| **Schnell** | größte Ersparnis, sichtbar weicher | VAAPI H.264/H.265 `-qp 26`, VP9/AV1 `-global_quality 150/160`, CPU `crf` Basis +4 |
| **Ausgewogen** (Standard) | kaum vom Original zu unterscheiden | VAAPI `-qp 22`, VP9/AV1 `-global_quality 110/130`, CPU `crf` Basis |
| **Hoch** | Archivqualität, größere Dateien | VAAPI `-qp 18`, VP9/AV1 `-global_quality 80/100`, CPU `crf` Basis −4 |

Codec-Wahl: **H.265** ist die beste Wahl für Filme und Serien (etwa halb so groß wie H.264, läuft auf
jedem aktuellen Gerät). **AV1** spart am meisten, ist ohne GPU-Unterstützung aber sehr langsam –
die Oberfläche zeigt je Codec an, ob diese GPU ihn beschleunigen kann.

## Fehlersuche

- **Kein GPU-Chip in der Kopfzeile / alles läuft auf dem Prozessor:**
  `docker compose exec media-converter vainfo` prüfen. Fehlt `/dev/dri` oder passen die
  `group_add`-GIDs (`video`, `render`) nicht zum Host, findet der Container die GPU nicht.
  Auch normal: Diese GPU kann nicht jeden Codec – AV1 fehlt z. B. auf älteren Intel-Generationen.
- **Datei endet als Fehler „Spuren fehlen nach Encode“ / „Ausgabe zu kurz“:** Die Quelle ist noch da,
  es ist nichts verloren. Meist ist die Quelldatei beschädigt – im Zweifel mit einem anderen Codec oder Zielformat
  oder mit der Qualitätsstufe *Hoch* erneut versuchen.
- **„ffprobe konnte die Datei nicht lesen“:** Die Datei ist defekt oder der Mount hängt.
  Solche Dateien tauchen in der Analyse als „defekt“ auf und lassen sich dort löschen.
- **„Zieldatei existiert bereits“:** Neben `film.mp4` liegt schon eine `film.mkv`. Aufräumen und erneut starten.
- **„Zu viele Versuche – warte 60 s“ beim Login:** Rate-Limit. Kurz warten. Hinter einem Reverse-Proxy
  ohne `TRUST_PROXY=1` sperrt die Bremse alle Nutzer gemeinsam aus, weil nur die Proxy-IP ankommt.
- **„Zu viele Live-Verbindungen“:** Es sind zu viele Tabs offen. Die Seite aktualisiert trotzdem weiter,
  nur im Sekundentakt per Abfrage statt per Live-Strom. Überflüssige Tabs schließen.
- **Logs und Zustand:** `docker compose logs -f media-converter`, Zustand des Healthchecks über
  `docker compose ps`. Der Healthcheck ruft `/api/auth-check` auf und braucht keine Anmeldung.

## Lizenz des Basisimages – bewusste Entscheidung

Das Image baut auf `lscr.io/linuxserver/ffmpeg` auf. Dieses ffmpeg ist ein **GPL-Build mit
`--enable-nonfree`** (u. a. libx264, libx265, libfdk-aac). Der CPU-Rückfall des Konverters braucht
genau libx264/libx265 – ein lizenzfreier LGPL-Build könnte H.264/H.265 ohne GPU nicht kodieren.

Festlegung vom 17.09.2026: **bewusst GPL-Build, ausschließlich Eigenbetrieb auf dem eigenen NAS,
nicht weitergeben** – weder das Image noch die ZIP mit dem Dockerfile an Dritte. Wer das Basisimage
ändert, muss die Lizenzfrage neu beantworten; `tests/test_release_zip.py` pinnt die `FROM`-Zeile,
damit eine Änderung nicht unbemerkt durchrutscht.

## Dateien

```
app/server.py           Flask-Backend (waitress, Port 8686)
app/static/index.html   Konverter (Ordnerwahl, Codec, Zielformat, Qualität)
app/static/history.html Verlauf (mit eingebautem Player)
app/playback.py         Ansehen im Browser: Entscheidung + verlustfreies Umpacken
config/                 Zugangsdaten, secret.key, history.jsonl, runs.jsonl, stats.json
tests/                  pytest (Befehls-Builder, Container-Regeln, Spuren-Prüfung, Login-Sperre, Player …)
tools/                  release_zip.py (ZIP-Auswahl für manage.sh), Prüfskript für den Player-Remux
manage.sh               Update/Start/Stop/Watch – siehe „Start & Update"
```
