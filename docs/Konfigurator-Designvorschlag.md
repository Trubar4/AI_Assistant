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

### 3.1 Grundmuster: geführte Konfiguration als neue Ansicht im bestehenden Shell

Neue Sidebar-Ansicht **„Konfiguration"** (Icon `settings`), neben Assistent / Fehlercodes /
Verlauf. Layout dreispaltig, konsistent mit der bestehenden App:

```
┌ Header (Maschine LR 1300 SX) ───────────────────────────────────────┐
├ Stepper (links) ┬ Arbeitsbereich (Mitte) ┬ Zusammenstellung (rechts) ┤
│ ① Auslegerkonfig │  aktueller Schritt      │  Live-Summary + Schema-   │
│ ② Hauptausleger  │  (Auswahl-Karten,       │  Silhouette des Auslegers │
│ ③ Nadelausleger  │   Längen-Picker …)      │  füllt sich pro Schritt   │
│ ④ Ballast        │                         │  + „Belegt durch Tab. xx" │
│ ⑤ Einscherung    │                         │                           │
│ ⑥ Weiteres       │                         │  [Traglasttabelle] [Teilen]│
└──────────────────┴─────────────────────────┴───────────────────────────┘
```

Die rechte **Zusammenstellungs-Leiste** ist der rote Faden: Sie zeigt jederzeit den aktuellen
Stand (Konfig-Typ, HA-Länge + Segmente, NA-Länge, Ballast, Einscherung) und pro Zeile den
Link zur belegenden Manual-Seite (wiederverwendetes `SourceCard`-Muster).

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

**Ergebnis – „Konfiguration abgeschlossen"**
- Vollständige Zusammenstellung als Spec-Sheet.
- **Gültigkeitsprüfung** (`lds-alert`): „Kombination laut Manual zulässig" bzw. Konflikt-Hinweis.
- Manual-Links zu jeder Entscheidung.
- Aktionen: „Traglasttabelle öffnen", „Als Rüstplan speichern/teilen", **„Im Assistent fragen"**
  (Brücke zurück zum Q&A: z. B. „Welche Schritte beim Aufrüsten dieser Konfiguration?").

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
4. **Maschinenumfang:** LR 1300 SX für die Demo fix (Datenschema ist maschinen-generisch angelegt).

## 7. Umsetzungsstand (Prototyp)

- `data/config_constraints.json` – statisch extrahierte Konfigurationsdaten (Längen + Segmente,
  Ballast → zulässige Drehung inkl. Fußnoten A/B/C, Einscher-Grenzen HA 1–20 / NA 1–6).
- `frontend/konfig-data.js` – einbindbare Daten (Prototyp läuft ohne Server per `file://`).
- `frontend/Konfigurator.html` – Klick-Prototyp, zweispaltig (Stepper · Arbeitsbereich),
  Vanilla JS, ausschließlich `design-system/lds.css` + Tokens. Realisierte Muster:
  Option-Karten, einrastender Längen-Picker mit Segment-Vorschau, Ballast-Radial mit Fußnoten,
  Einscher-Stepper mit Lastort-Umschaltung, belegte Zusammenstellung im Ergebnis-Schritt mit
  Gültigkeitsprüfung und Brücke zum Frage-Assistenten. Der Stepper trägt zugleich den laufenden
  Stand je Schritt.

**Bekannte Vereinfachungen (Demo):** Schritt ⑥ als einfache Auswahl; Ballast-Tabelle zeigt für
LR 1300 SX (breite Spur) durchgängig 360° mit Rüst-Fußnoten; NA-fest/-verstellbar sind auf zwei
repräsentative Datensätze aus `compositions.json` gemappt. Nächster Schritt bei Freigabe:
Überführung der `cfg-*`-Muster in echte LDS-Komponenten (§5) und Anbindung an echte Manual-Links.
