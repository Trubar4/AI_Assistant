# MaintenanceAssistant_v3.html – lokales Setup & Migration

## Ziel des MaintenanceAssistant

Der klassische Wartungsprozess für LR 1104.03.08 über ein
statisches PDF-Handbuch bzw. dessen HTML-Export: eine tabellarische Wartungs- und Inspektionsliste
mit 213 Einzelaufgaben, gruppiert nach Baugruppe und Intervall (8h, 40h,
500h, ... 6000h), plus 126 separate Anleitungsseiten mit den eigentlichen
Arbeitsschritten, Voraussetzungen und Sicherheitshinweisen. Um die
passenden Aufgaben für den aktuellen Betriebsstundenstand zu finden, musste
man bisher manuell in der Tabelle nachschlagen und einzeln durch die
verlinkten Handbuchseiten blättern.

Der MaintenanceAssistant digitalisiert diesen Prozess als interaktive
Checkliste:

- Bediener gibt die aktuellen **Betriebsstunden (Bh)** ein.
- Die App aktiviert automatisch alle fälligen Intervalle und zeigt nur die
  dafür relevanten Aufgaben, gruppiert nach Baugruppe, mit Fortschrittsbalken.
- Jede Aufgabe lässt sich abhaken, kommentieren (z.B. Abweichungen) und mit
  Fotos dokumentieren.
- Pro Aufgabe kann die zugehörige Schritt-für-Schritt-Anleitung (inkl.
  Voraussetzungen und Warnhinweisen) direkt in einem Modal geöffnet werden,
  statt im PDF/Handbuch suchen zu müssen.
- Am Ende der Schicht lässt sich ein **Schichtbericht** (erledigt/offen/
  kommentiert) generieren.

`MaintenanceAssistant_v3.html` ist dazu aktuell eine **Single-Machine-Demo**
(kein Login/Backend, State nur im Browser-Speicher
zur Laufzeit) – ein Proof-of-Concept, der zeigt, wie aus der starren
Handbuch-Tabelle plus Anleitungsseiten eine bedienbare Tages-Checkliste wird.

## Was ist das (technisch)?

Die Datei ist vollständig self-contained: CSS und
JavaScript stecken inline in der HTML (kein CDN, keine externen
Style-/Script-Dateien, keine Bilder – Icons sind HTML-Entities).

Beim Laden holt sich die Seite per `fetch()` zwei Datendateien aus
demselben Verzeichnis:

- `maintenance_tasks.json` – Wartungsaufgaben
- `maintenance_instructions.json` – zugehörige Schritt-für-Schritt-Anleitungen

## Lokal starten (VS Code)

`fetch()` auf lokale Dateien wird von Browsern über `file://` aus
CORS-Gründen blockiert. Die Seite muss daher über einen lokalen Webserver
aufgerufen werden, nicht per Doppelklick geöffnet werden.

**Option A – Live Server Extension**
1. Ordner `MaintenanceAssistant/` in VS Code öffnen.
2. Extension „Live Server" (Ritwick Dey) installieren.
3. Rechtsklick auf `MaintenanceAssistant_v3.html` → **Open with Live Server**.

**Option B – Python-Webserver**
```bash
cd MaintenanceAssistant
python3 -m http.server 8000
```
Danach im Browser: `http://localhost:8000/MaintenanceAssistant_v3.html`

## Versionsstand

`MaintenanceAssistant_v3.html` ist die aktuelle/einzige Version des
Assistenten (letzte Änderung: Umstellung auf `fetch()` aus den JSONs,
Commit `1c8a8cc`). Es gibt keine v4. Spätere Commits im Ordner
(`index.html`) betreffen nur die unabhängige Liebherr-Doku-Coverseite,
nicht den Assistenten.

## Datenextraktion – wie die JSONs entstanden sind

Quelle ist das offizielle Liebherr-Doku-Export-Format (LiRS 2.0 – dasselbe
Format wie `index.html`): HTML-Seiten mit fixen CSS-Klassen für Tabellen,
Sicherheitshinweise etc.

**Quelldateien** (beide nicht mehr für v3 selbst nötig, siehe unten):
- `ID_5dc5858072f74b66af724fb563f3d267-...-de-DE.html` – der
  „Wartungs- und Inspektionsplan": eine Tabelle (`taskintervals-dynamic`)
  mit allen 213 Aufgaben (laufende Nummer, Baugruppe, Tätigkeit, Symbol,
  Intervall).
- `merged_tasks.html` – viele einzelne Anleitungsseiten des Handbuchs,
  zu einer Datei zusammengefügt, mit Marker-Kommentaren
  `<!-- ID_xxx-de-DE.html -->` als Trenner zwischen den Seiten.

**Extraktionsscript:** `extract_maintenance_data.py` (Python, BeautifulSoup)
parst beide Quellen und erzeugt daraus die zwei JSONs:

1. **`maintenance_tasks.json`** (JSON1) – aus der Plan-Tabelle:
   - Zeilen mit Symbol (`● ■ ✦ ❄ ○ □ ⟡`) werden zu Haupt-Tasks; das Symbol
     wird über `SYMBOL_MAP` in `responsible` (customer/service) und
     `task_kind` (recurring/once/on_demand/seasonal) übersetzt.
   - Zeilen ohne Symbol werden als `sub_items` der vorherigen Aufgabe
     zugeordnet (Zusatzinfos, eingebettete Sub-Intervalle wie
     „1000 h / jährlich: …").
   - Intervalle (`"1000 h"`, `"bei Bedarf"`, …) werden per Regex in
     strukturierte `{type, hours, label}`-Objekte geparst.
   - Der `href` jedes Tätigkeits-Links wird als `instruction_link`
     übernommen (Verweis auf die jeweilige Anleitungsseite).

2. **`maintenance_instructions.json`** (JSON2) – aus `merged_tasks.html`:
   - Das File wird anhand der `<!-- ID_xxx.html -->`-Marker in einzelne
     Seiten-Abschnitte gesplittet.
   - Pro Seite werden Titel (`h2`), Breadcrumb, Voraussetzungen
     (`p.listintro` + `table.list`), Sicherheitshinweise
     (`div.safetyadvice` → Signalwort/Ursache/Folgen/Maßnahmen) und
     nummerierte Arbeitsschritte (`table.action`, `table.result`)
     extrahiert.
   - Cross-Referenz: Für jeden Task/Sub-Item wird geprüft, ob sein
     `instruction_link` tatsächlich eine Seite in `merged_tasks.html` hat
     (`has_instruction`).

Kleine Historie dazu: Im ersten Anlauf (`3043ad2`) wurden `get_text()`-Aufrufe
ohne Separator gemacht, wodurch Wörter aus benachbarten HTML-Elementen
zusammenklebten (z.B. „TasteMotorölfüllstand"). Fix in `1c8a8cc`:
`get_text(separator=' ')`, JSONs neu generiert, und v3 gleichzeitig von
hartcodierten Daten auf `fetch()` der JSONs umgestellt (Dateigröße
192 KB → 21 KB).

Das Script läuft einmalig offline (`python3 extract_maintenance_data.py`)
und wird zur Laufzeit der App nicht mehr gebraucht – die App liest nur die
fertigen JSONs.

## Migration in ein anderes Repo

Nur folgende Dateien werden benötigt, im selben Verzeichnis:

- `MaintenanceAssistant_v3.html`
- `maintenance_tasks.json`
- `maintenance_instructions.json`

**Nicht nötig:**
- `extract_maintenance_data.py` (nur Pre-Processing zur JSON-Erzeugung)
- separate Styles (alles inline in der HTML)
- `index.html`, `css/`, `js/`, `images/` (Liebherr-Doku-Coverseite, unabhängig)
- `ID_*-de-DE.html`, das PDF, `merged_tasks.html` (Rohmaterial der Extraktion)

**Bekannte Einschränkung:** Die JSONs referenzieren pro Task/Sub-Item einen
`instruction_link` auf ~123 Handbuch-Seiten (`ID_*-de-DE.html`), die im
Repo nicht vorhanden sind – die „Im Handbuch öffnen"-Buttons sind daher
bereits im Ausgangsrepo tot. Das ist kein durch die Migration neu
entstandenes Problem. Nur falls das Zielrepo den vollständigen
LiRS-Manual-Export enthält, würden diese Links funktionieren.

Im Zielrepo gilt dieselbe Regel wie lokal: Die Seite muss über
`http(s)://` (nicht `file://`) ausgeliefert werden, damit `fetch()` der
JSONs funktioniert.
