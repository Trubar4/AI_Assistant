# Kran-Konfigurator – Analyse & Designvorschlag

Status: Entwurf zur Abstimmung · Maschine (Demo): **LR 1300 SX**, Manual DE 04/2025
Bezug: `reference/Maschinen-Assistent.html`, `data/compositions.json`, Manual-Tabellen 317 / 326 / 1916 / 316 / 353 / 351

---

## 1. Ziel

Der Nutzer soll eine gültige **Rüst-/Auslegerkonfiguration** der gewählten Maschine
zusammenstellen können – nicht durch freies Ausfüllen von Feldern, sondern durch
*intelligente, geführte Auswahl*: Jede Entscheidung schränkt die nächsten auf die laut
Manual zulässigen Optionen ein, und jede Option ist mit der exakten Manual-Stelle belegt
(gleiche Belegbarkeits-Logik wie der bestehende Frage-Assistent).

Kurz: **Konfigurator = strukturierte Auswahl, wo Daten vorliegen; RAG-Assistent als Erklär-
und Rückfall-Ebene, wo nicht.** Beides ist ein Produkt.

---

## 2. Analyse der Konfig-Möglichkeiten

Die sechs Themen bilden **keine flache Formularmaske**, sondern eine Abhängigkeitskette
(Constraint-Kette). Das ist die zentrale Erkenntnis für das Design.

| # | Thema | Manual-Quelle | Charakter | Abhängig von |
|---|---|---|---|---|
| 1 | **Auslegerkonfiguration** (3 Varianten) | Auslegerkonfig. 1/3/5 | Wurzel-Entscheidung, wenige Optionen | – |
| 2 | **Hauptausleger** (Länge → Segmente) | Tab. 317 (2320) | 24 diskrete Längen (20–89 m) | Konfig |
| 3 | **Nadelausleger** (Länge → Segmente) | Tab. 326 fest / Tab. 1916 verstellbar | diskrete Längen; nur bei Konfig 3/5 | Konfig, HA |
| 4 | **Ballast** | Tab. 316 | Eingabe *mit sofortiger Folge*: zulässige Oberwagen-Drehung | Konfig, Gesamtsystem |
| 5 | **Einscherung** | Tab. 353 (HA-Kopf 2320, 1–20fach) / Tab. 351 (NA-Kopf 1916, 1–6fach) | Stepper, begrenzt durch Tabelle | Lastort, vorhandener Ausleger |
| 6 | **Weitere** (Vorschlag §4) | diverse | optional/Rahmenbedingungen | – |

**Datenreife heute**
- ✅ *Strukturiert vorhanden* (`compositions.json`): Länge → Segment-Zusammenstellung für HA 2320
  und Nadelausleger; Seilführungs-Marker (S/N) für den HA.
- ⚠️ *Als Manual-Seite vorhanden, noch nicht strukturiert*: Einscherpläne 1–20fach / 1–6fach
  (Tab. 353/351), Ballast→Drehung (Tab. 316), Gültigkeitsregeln zwischen HA und NA.
  → Diese müssen extrahiert **oder** über den RAG-Assistenten live beantwortet werden (siehe §5).

**Auslegerkonfiguration – die 3 Wurzel-Varianten**
1. **Nur Hauptausleger** (Konfig 1)
2. **Hauptausleger + feststehender Nadelausleger** (Konfig 3) → Nadelausleger aus Tab. 326
3. **Hauptausleger + verstellbarer Nadelausleger** (Konfig 5) → Nadelausleger aus Tab. 1916

Die Wurzel-Wahl bestimmt, welche Folgeschritte überhaupt erscheinen (Schritt „Nadelausleger"
und „Lastort NA-Kopf" nur bei 3/5).

---

## 3. Designvorschlag – UX-Konzept

### 3.1 Grundmuster: Konfigurator als Dialog aus „Konfiguration aktiv"

Der Konfigurator ist **kein eigenständiger Screen**, sondern ein **modaler Dialog**. Im
Assistent-Kontext gibt es das Panel **„Konfiguration aktiv"** mit einem **Zahnrad-/Settings-Button**;
dieser öffnet den Dialog. „Übernehmen" speichert die gewählten Werte zurück in „Konfiguration
aktiv" – von dort aus kontextualisiert der Assistent seine Antworten.

```
HOST (Assistent)                          DIALOG (Konfigurator, modal)
┌ Konfiguration aktiv        [⚙] ┐        ┌ Konfigurator · LR 1300 SX   [✕] ┐
│ Ausleger: HA + fester NA       │  ⚙ →   ├ Stepper (links) ┬ Arbeitsbereich ┤
│ HA 47 m · NA 20 m · …          │        │ ① Auslegerkonfig │ Auswahl-Karten │
│ (belegt · Tab. 317/326/…)      │        │ ② Hauptausleger  │ Längen-Picker  │
└────────────────────────────────┘        │ …                │ …              │
                                          └ [Übernehmen] [Abbrechen] ──────────┘
```

Der **Stepper links** ist zugleich der rote Faden: Er zeigt pro Schritt den aktuellen Stand
(Konfig-Typ, HA-/NA-Länge, Ballast, Einscherung). Die vollständige, je Zeile mit der Manual-Tabelle
**belegte** Zusammenstellung steht im Ergebnis-Schritt und – nach „Übernehmen" – im Panel
„Konfiguration aktiv". Wird der Dialog erneut geöffnet, sind die gespeicherten Werte vorbelegt.

### 3.2 Drei „intelligente" Auswahl-Mechanismen

Das ist der Unterschied zu „ein paar Dropdowns":

1. **Abhängigkeitsgesteuerte Schritte (Progressive Disclosure).** Es erscheinen nur Schritte
   und Optionen, die zur Wurzel-Wahl passen.
2. **Constraint-Propagation.** Unzulässige Optionen werden deaktiviert *mit Begründung*
   („nicht kombinierbar mit HA 89 m – Tab. 316"), nicht kommentarlos versteckt.
3. **Ziel-zuerst-Abkürzung (optional, das eigentliche Alleinstellungsmerkmal).** Statt vorwärts
   zu wählen, gibt der Nutzer den *Lastfall* ein (Traglast, Ausladung, Hubhöhe) und der
   Konfigurator **schlägt gültige Konfigurationen vor** (Rückwärts-Konfiguration). Siehe offene
   Frage §6.2.

### 3.3 Schritte im Detail

**① Auslegerkonfiguration** – 3 große Auswahl-Karten (kein Dropdown: wenige Optionen, hohe
Tragweite), je mit Schema-Piktogramm des Auslegers. Auswahl = eine Karte aktiv.

**② Hauptausleger** – **Längen-Picker** statt nacktem Dropdown: horizontale Skala, die auf die
gültigen diskreten Längen (20 … 89 m) einrastet. Live-Vorschau der Segment-Zusammenstellung
(z. B. „10 + 3 + 6 + 7 m", direkt aus `compositions.json`) + Quelle „Tab. 317". Nur für die
gewählte Konfig gültige Längen sind aktiv.

**③ Nadelausleger** *(nur Konfig 3/5)* – gleiches Picker-Muster, Datenquelle Tab. 326 (fest)
bzw. 1916 (verstellbar). Anzeige der resultierenden Systemlänge/-geometrie.

**④ Ballast** – da Tab. 316 Ballast → *zulässige Oberwagen-Drehung* verknüpft, wird die
Auswahl mit **sofortiger Folge-Anzeige** gekoppelt: Ballast-Variante wählen → resultierender
zulässiger Schwenkbereich (z. B. „360° frei" vs. „eingeschränkt"), visualisiert als kleines
Radial-/Zifferblatt. Aus einem Tabellen-Lookup wird eine sichtbare Randbedingung.

**⑤ Einscherung** – zweiteilig:
- **Lastort** wählen: HA-Kopf 2320 oder (falls NA vorhanden) NA-Kopf 1916.
- **Einscherfaktor**: Stepper 1×–20× (HA, Tab. 353) bzw. 1×–6× (NA, Tab. 351), durch die
  Tabelle begrenzt. Anzeige der resultierenden max. zulässigen Last je Einscherung.

**⑥ Weiteres – Vorschlag „was sonst noch Sinn macht"**
- **Abstützbasis / Raupen-Spurweite** (Aufstellung) – beeinflusst Standsicherheit & Traglast.
- **Zentral-/Derrickballast** (falls für Konfig relevant).
- **Windbedingungen** – max. zulässige Windgeschwindigkeit für die Konfiguration.
- **Betriebsmodus / Traglasttabelle** – welche Tabelle für den Rüstzustand gilt.
- **Hakenflasche / Lasthaken** passend zur Einscherung.

**Ergebnis – „Konfiguration prüfen & übernehmen"**
- Vollständige Zusammenstellung als Spec-Sheet.
- **Gültigkeitsprüfung** (`lds-alert`): „Kombination laut Manual zulässig" bzw. Konflikt-Hinweis.
- Manual-Links zu jeder Entscheidung.
- Aktionen: **„Konfiguration übernehmen"** (nur bei vollständiger Konfig aktiv) speichert die Werte
  als aktive Konfiguration und schließt den Dialog · „Abbrechen" verwirft. **Kein** Rüstplan-Export
  oder Traglasttabellen-Aufruf – der Konfigurator dient allein dem Setzen der aktiven Konfiguration.

---

## 4. Verbindung zum bestehenden Assistenten

Konfigurator und Frage-Assistent sind ein Produkt mit zwei Datenschichten:

1. **Strukturierte Schicht** (`compositions.json`, erweitert um Einscher-/Ballast-/Regel-Daten):
   liefert exakte, deterministische Auswahl + Belege.
2. **Assistenz-Schicht** (bestehendes RAG): erklärt „warum", beantwortet Rand-/Sonderfälle und
   liefert den Manual-Link, wo (noch) keine Struktur existiert.

Damit ist der Konfigurator sofort demonstrierbar (auf Basis vorhandener Daten) und wächst mit
der Datenextraktion, ohne UX-Bruch.

---

## 5. Design-System-Konformität

Wiederverwendung ausschließlich aus dem LDS: `lds-card`, `lds-btn`, `lds-alert`, `lds-pill`,
`lds-badge`, `lds-input`; Farben/Spacing/Typo nur über Tokens.

**Neu zu ergänzende Komponenten** (nach Erweiterungs-Regeln: `design-system/components/<name>.css`,
in `lds.css` registrieren; neue Tokens als Rolle in jeder `roles-*.css`):
- `lds-stepper` – vertikaler Fortschritts-Stepper (zeigt zugleich den laufenden Stand je Schritt).
- `lds-option-card` – große, selektierbare Auswahlkarte (Schritt ①).
- `lds-length-picker` – einrastende Längen-Skala mit Segment-Vorschau.

---

## 6. Getroffene Entscheidungen

1. **Umfang:** interaktiver Klick-Prototyp der Schritte ①–⑥ → umgesetzt (§7).
2. **Einstiegsmodell:** **Vorwärts-Konfiguration**. Ziel-zuerst wurde verworfen, weil die
   Traglasttabellen im Manual nur als Bildschirm-/UI-Seiten, nicht als strukturierte
   Zahlentabellen (Last × Ausladung × Höhe) vorliegen – eine datenbasierte Rückwärts-Konfiguration
   ist damit nicht belegbar.
3. **Datenquelle:** Constraints **vorab statisch extrahiert** → `data/config_constraints.json`
   (aus `compositions.json` + Manual-Tabellen 316/317/326/351/353/1916).
4. **Maschinenumfang:** **LR 1200.1** für die Demo fix (passend zu den Manual-Files; Datenschema
   ist maschinen-generisch angelegt). Schritt ⑥ „Aufstellung/Rahmenbedingungen" wurde entfernt.

## 7. Umsetzungsstand (Prototyp)

- `data/config_constraints.json` – statisch extrahierte Konfigurationsdaten (Längen + Segmente,
  Ballast → zulässige Drehung inkl. Fußnoten A/B/C, Einscher-Grenzen HA 1–20 / NA 1–6).
- `frontend/konfig-data.js` – einbindbare Daten (Prototyp läuft ohne Server per `file://`).
- `frontend/Konfigurator.html` – Klick-Prototyp, ausschließlich `design-system/lds.css` + Tokens,
  Vanilla JS. **Host-Ansicht** mit Panel „Konfiguration aktiv" (Zahnrad-Button, Leer- und
  Konfiguriert-Zustand) + **modaler Konfigurator-Dialog** (Stepper · Arbeitsbereich). „Übernehmen"
  speichert die Werte in „Konfiguration aktiv", erneutes Öffnen belegt vor; Schließen via X, Scrim
  oder Escape. Realisierte Muster: Option-Karten, einrastender Längen-Picker mit Segment-Vorschau,
  Ballast-Radial mit Fußnoten, Einscher-Stepper mit Lastort-Umschaltung, belegte Zusammenstellung
  mit Gültigkeitsprüfung.

**Bekannte Vereinfachungen (Demo):** Ballast-Tabelle zeigt für LR 1200.1 (breite Spur) durchgängig
360° mit Rüst-Fußnoten; NA-fest/-verstellbar sind auf zwei repräsentative Datensätze aus
`compositions.json` gemappt. Nächster Schritt bei Freigabe: Überführung der `cfg-*`-Muster in echte
LDS-Komponenten (§5) und Anbindung an echte Manual-Links.

## 8. Integration in die App (`frontend/MaschinenAssistent.html`)

Der Konfigurator ist per **iframe** (`Konfigurator.html?embed=1`) eingebettet — eine Codebasis,
keine Duplikate. Bei „Übernehmen" meldet er die Werte per `postMessage` zurück und die App füllt
damit das bestehende **„Maschinenkonfiguration"-Kontextpanel** (Chips), das den Assistenten steuert.

- **Neues Tab „Maschinen"** (erste Position) mit einer Maschinen-Kachel (`LR 1200.1`, SN 137187,
  Icon `frontend/assets/lr1200-1.png`). Solange keine Maschine gewählt ist, sind die übrigen Tabs
  (Assistent/Meldungen/Wartungen) **ausgegraut**. Auswahl per Klick auf die Kachel; **Abwählen**
  setzt zurück (Tabs wieder gesperrt). Der Konfigurator startet über den ⚙-Button der Kachel.
- **Assistent-Tab:** ⚙-Button „Konfigurator" im Kopf des „Maschinenkonfiguration"-Panels.
- **Konfigurator:** „Zurücksetzen" im Dialog-Header verwirft alle Eingaben; „Abbrechen"/X schließt.
- **Wartungen-Tab:** Betriebsstunden-Button **oben rechts im App-Header** (nur in diesem Tab, öffnet
  das Betriebsstunden-Popup, zeigt den aktuellen Bh-Wert); Schichtbericht & Zurücksetzen bleiben auf
  der Seite. Neuer Filter **„mit Kommentar/Foto"** neben den Intervall-Chips.
- Maschinenname **LR 1200.1** durchgängig (Titel, Appbar, Hero, Wartungen).
