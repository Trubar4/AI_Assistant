# KI-Assistent LR 1104.03.08 — Modi & Verfahren (PPT-Vorlage)

> Foliengerechte Aufbereitung aller Tabs, Modi und Backends. Jede Folie = eine
> `##`-Überschrift. Aufzählungspunkte sind Bullet-Text; `> Notiz:` sind
> Sprechernotizen. Reihenfolge ist präsentationsfertig. Quelle der Details:
> `docs/local-llm-status.md`.

---

## Folie 1 — Titel

**KI-Assistent für das Kran-Manual LR 1104.03.08**
Deterministisch. Halluzinationsfrei. Offline-fähig.

- Frage/Antwort, Fehlercode-Lookup und Wartungs-Checkliste in einer App
- Lokales Modell (qwen3:4b) **nur** für die Suche, nie für die Antwort
- Design: Liebherr Design System (LDS)

> Notiz: Ein Werkzeug für Bediener/Service am Kran — beantwortet Fragen aus dem
> Manual, schlägt Meldungen nach und führt durch die Wartung.

---

## Folie 2 — Überblick: drei Tabs

| Tab | Zweck | Endpunkt(e) |
|-----|-------|-------------|
| **Assistent** | Frage in natürlicher Sprache → Antwort/Quellen | `/ask`, `/ask_agent` |
| **Meldungen** | Fehler-/Meldungscode oder Stichwort nachschlagen | `/errorcode` |
| **Wartungen** | Interaktive Wartungs-Checkliste nach Betriebsstunden | `/maintenance/*.json` |

- **Assistent** hat 2 Modi mit Backend-Umschaltern (Folien 6–7)
- **Meldungen** und **Wartungen** sind rein deterministisch (kein LLM)

> Notiz: Alles Sicherheitsrelevante bleibt am Original-Manual prüfbar — die App
> zeigt immer die Quelle.

---

## Folie 3 — Kernprinzip

**Antworten sind bewusst deterministisch und halluzinationsfrei.**

- Tabellenwerte, Zählungen, Anordnung, Seilführung werden **exakt** aus Tabellen
  bzw. `data/compositions.json` extrahiert — mit Quellenangabe
- Sonst: **Quellen + wörtlicher Snippet**, kein frei formulierter LLM-Text
- Deterministische Extraktion mit Quelle ist **keine** Halluzination
- Das kleine Modell **qwen3:4b** wird nur dort eingesetzt, wo es nicht
  halluzinieren kann: **Retrieval** (HyDE + Reranking) — nie zum Formulieren

> Notiz: Lieber ehrlich „nicht gefunden" oder nur Quellen als eine
> selbstbewusst-falsche Antwort.

---

## Folie 4 — Architektur & Endpunkte

- **Frontend**: eine self-contained HTML (`frontend/MaschinenAssistent.html`),
  LDS-Styling, Tabs über `switchView()`
- **Backend**: FastAPI (`backend/main.py`)
  - `POST /ask` — Klassisch (Retrieval + Konfidenz + Quellen)
  - `POST /ask_agent` — Assistent (Fast-Paths, Regelbasiert/QWEN/Claude)
  - `POST /errorcode` — Meldungs-Lookup
  - `GET /maintenance/{tasks|instructions}.json` — Wartungsdaten
  - `GET /manuals/<datei>` — Original-Handbuchseiten (Iframe-Viewer)
- **Provider-Schalter** `LLM_PROVIDER=anthropic|local` (Default `anthropic`)

> Notiz: `parse_context` (Konfig-Analyse) läuft immer über Anthropic bzw. bei
> `local` regelbasiert — nie über qwen zum Formulieren.

---

## Folie 5 — Retrieval-Fundament (für alle Assistent-Modi)

- **BM25** (lexikalisch) + **TF-IDF-Char-N-Gram-Fallback** (Komposita/Flexion)
- **Semantische Suche** optional (sentence-transformers) — auf Render aus
- **Multi-Query-Fusion**: Suche über HyDE-Passage **und** normalisierte
  Originalfrage, Ergebnisse gemergt (höherer Score gewinnt)
- **Leichte deutsche Morphologie**: Umlaut-Faltung + Endungs-Stemming
  (Einzahl/Mehrzahl konvergieren)
- **Dokumenttyp-Boost, fragesensitiv**: Referenz-/Konfig-Seiten nur anheben,
  wenn die Frage ein nicht-generisches Sachwort mit dem Titel teilt
- **Composition-Index-Augmentation**: grafische Bauteil-Seiten per Freitext
  auffindbar

> Notiz: Retrieval ist der Dauer-Engpass — generisch verbessert, kein
> Themen-Hardcoding.

---

## Folie 6 — Tab „Assistent": Modus **Klassisch**

**Backends: QWEN · Claude** (Umschalter). Beide liefern **keinen** LLM-Text.

Ablauf (`/ask`):
1. Relevanten Konfig-Kontext zur Frage bestimmen
2. **HyDE**: hypothetische Passage erzeugen (nur für die Suche)
3. **Fusion**-Retrieval (HyDE + normalisierte Frage) → Kandidaten
4. **Reranking** (Frage + Kontext) → Top-Quellen
5. Antwort = **Konfidenzmeldung** (Score-basiert) + **gerankte Quellen + Snippet**

| Backend | HyDE + Reranking | Finale Antwort |
|---------|------------------|----------------|
| QWEN | lokal (qwen3:4b) | Konfidenz + Quellen (kein LLM-Text) |
| Claude | Anthropic | Konfidenz + Quellen (kein LLM-Text) |

> Notiz: Der Backend-Schalter ändert nur den **Such-Anbieter**, nicht die
> Antwortform. Nutzen: schnelle, günstige Quellensuche.

---

## Folie 7 — Tab „Assistent": Modus **Assistent**

**Backends: Regelbasiert · QWEN · Claude** (Umschalter). Endpunkt `/ask_agent`.

Gemeinsam: **deterministische Fast-Paths laufen VOR dem LLM** (Folie 8).

Alle drei nutzen dieselbe **Basis-Suche** (Titel-BM25 + Volltext-BM25 +
**Semantik**, TF-IDF nur als Fallback wenn Semantik fehlt — Folie 5). Der
Backend-Schalter steuert nur den **LLM-Assist** (HyDE/Rerank), nicht die Semantik.

| Backend | Zusätzlicher LLM-Assist | Finale Antwort |
|---------|-------------------------|----------------|
| **Regelbasiert** | **keiner** (kein qwen/Claude) | deterministisch (Fast-Paths + Quellen) |
| **QWEN** | qwen-HyDE + qwen-Rerank (nur Suche) | deterministisch (kein qwen-Text) |
| **Claude** | agentischer Tool-Loop | Claude **formuliert** (nach Fast-Paths) |

- „kein LLM" = **kein generatives Modell** legt sich auf die Suche (kein HyDE/
  Rerank). Die **semantische Suche** ist ein *Embedding*-Modell und läuft
  backend-unabhängig, sobald verfügbar — **auch bei Regelbasiert**.
- **Lokal**: Regelbasiert = BM25 + **Semantik**. **Render Free** (keine
  sentence-transformers): BM25 + **TF-IDF-Fallback** (siehe Folie 13).
- Regelbasiert öffnet die Top-Quelle automatisch im Handbuch-Viewer
- QWEN: fällt der lokale Server aus, läuft es ohne Assist weiter (Antwort ist
  ohnehin deterministisch)
- Claude: beste Qualität bei prozeduralen Fragen, benötigt Internet + API-Key

> Notiz: Zwei getrennte Achsen — (1) LLM-Assist = der Backend-Schalter,
> (2) Semantik = env-gesteuert und modusunabhängig. „QWEN" hilft nur suchen;
> frei formuliert nur „Claude", und auch nur, wenn kein Fast-Path schon
> deterministisch geantwortet hat.

---

## Folie 7b — Regelbasiert: Ablauf Schritt für Schritt

`/ask_agent` → `run_agent_local(mode="sources", assist=None)` — **0 Modell-Calls**.

1. **Clarification** (reine Regeln): Wert-/Tabellenfrage ohne **Länge UND
   Einscherung** → gezielte Rückfrage, Abbruch.
2. **Query bauen** (bei Folgerunde + letzte Nutzerantwort).
3. **Retrieval / Fusion**: mehrere `search()`-Aufrufe (roh, normalisiert,
   Kontext+normalisiert) → `_merge`. `search()` = Titel-BM25 + Volltext-BM25 +
   Semantik (RRF), TF-IDF-Fallback. **Kein** qwen-HyDE/Rerank.
4. **Score/Gap**: `top_score`, Score-Gap-Filter, `confidence`.
5. **Deterministische Fast-Paths** (score-unabhängig, **vor** dem Gate):
   Tabellenwert / Composition-Zählung / -Anordnung / -Seilführung mit
   Präzisions-Gates. Greift einer → exakter Wert + Quelle, **fertig**.
6. **Confidence-Gate**: kein Fast-Path & Score zu schwach → ehrlich
   „nicht gefunden" + Top-3-Quellen.
7. **Default „sources"**: Top-Quellen + wörtlicher Snippet, kein Modelltext.

- **Keine LLM-Tool-Calls** — die Fast-Paths rufen die Funktionen (`lookup_table`,
  `composition_*`) *direkt* auf, kein Modell entscheidet (Details Folie 8b).
- **Anzeige immer „(1 Suchschritt)"**: `rounds` ist im `sources`-Pfad stets 0
  (Quellen/Confidence/Composition) oder 1 (Tabelle); die UI zeigt beide als
  „1 Suchschritt". Mehrere Suchschritte gibt es nur im **Claude**-Loop.

> Notiz: Deterministisch von A bis Z — sofort, offline, kostenlos, kein API-Key.

---

## Folie 8 — Deterministische Fast-Paths (Herzstück)

Laufen in **allen** Assistent-Backends **vor** dem LLM (`backend/fastpaths.py`):

- **Tabellenwert**: Zeile (z. B. Auslegerlänge) × Spalte (z. B. Einscherung)
  → exakter Zellwert. *Bsp.: 75 m / 5-fach → **1900 kg***
- **Composition-Zählung**: *„Wie viele 12-m-Zwischenstücke bei 74 m?" → **4***
- **Composition-Anordnung**: *Anlenk 10 → 3 → 6 → 12 → 12 → 12 → 12 → Kopf 7*
- **Seilführungs-Position** (S/N-Marker): *Konfig 1/3 am 5. Segment (S),
  Konfig 4 am 4. (N)*

Datenbasis: HTML-Tabellen der Seiten + `data/compositions.json` (aus OCR/Vision
der Zusammenstellungs-Grafiken, jede Zeile plausibilisiert).

> Notiz: 0 Modell-Calls, kein API-Key nötig — auch „Claude" beantwortet diese
> Fälle exakt über den Fast-Path.

---

## Folie 8b — Tool-Calls: wer, was, wann

**Zwei Arten von „Tools" — nicht verwechseln:**
- **A) LLM-Tool-Calling**: ein *Modell entscheidet*, ein Tool aufzurufen
  (Function-Calling-Loop). → **nur Claude**.
- **B) Deterministische Funktionsaufrufe**: die Fast-Paths rufen dieselben
  Funktionen *direkt* auf, kein Modell dazwischen. → alle Assistent-Backends.

**Die Tools** (`agent_tools.py`):

| Tool | Für Claude sichtbar? (`TOOL_SCHEMAS`) | Deterministisch (Fast-Path) |
|------|:--:|:--:|
| `search` | ja | ja (Retrieval) |
| `read_page` | ja | — |
| `grep_manual` | ja | — |
| `bal_search` | ja | — |
| `lookup_table` | **nein** (bewusst) | ja (Tabellen-Fast-Path) |
| `composition_count/arrangement/seilfuehrung` | **nein** | ja (Composition) |

**Pro Modus:**

| Modus / Backend | LLM-Tool-Calls (A) | Deterministisch (B) | Modell-Calls |
|---|:--:|---|---|
| Klassisch · QWEN/Claude | keine | nur `search` (Lib-Aufruf) | HyDE + Rerank |
| Assistent · **Regelbasiert** | **keine** | `search`, `lookup_table`, `composition_*` | **0** |
| Assistent · **QWEN** | **keine** | wie Regelbasiert (+ qwen-HyDE/Rerank nur fürs Retrieval) | nur Retrieval-Assist |
| Assistent · **Claude** | **ja** (Loop) | Fast-Paths **zuerst** | 0 bei Fast-Path, sonst Loop |

**Wann ruft Claude Tools?** (`run_agent`)
1. Zuerst deterministische Fast-Paths — greift einer → **0 LLM-/Tool-Calls**.
2. Sonst Tool-Loop: `search` → `read_page` → `grep_manual` → `bal_search`
   (bis `MAX_ROUNDS`/`MAX_TOOL_ROUNDS`), dann formulieren. `lookup_table`/
   `composition_*` bekommt Claude **nicht** — die laufen immer über den Fast-Path.
→ Echte Tool-Calls also nur bei **prozeduralen/Freitext-Fragen ohne Fast-Path**.

> Notiz: `agent_local` hat einen experimentellen qwen-Tool-Loop
> (`mode="tools"`), aber `main.py` erzwingt `mode="sources"` — über die UI ist
> er nie aktiv. „QWEN" ändert damit nur das Retrieval, nie die Antworterzeugung.

---

## Folie 9 — Schutz vor „selbstbewusst-falsch"

**Präzisions-Gates** (sonst Fall-through auf Quellen):
- **Zeile UND Spalte** müssen echt getroffen sein (echter Zellzugriff)
- **Relevanz-Gate**: getroffene Seite muss ein Sachwort mit der Frage teilen
- **Eindeutigkeits-Guard**: mehrere Kandidatenseiten (z. B. Nadelausleger-
  Varianten) → **nicht raten**

**Dimensionsgenaue Rückfrage** (Clarification):
- Wert-/Tabellenfragen brauchen **Länge UND Einscherung**
- Fehlt eine (auch bei aktivem Kontext) → gezielte Rückfrage
  *(„Bitte noch angeben: Einscherung …")*
- **Confidence-Gate**: zu schwacher Score → ehrlich „nicht gefunden"

> Notiz: Im Zweifel keine Antwort, sondern Quellen oder Rückfrage.

---

## Folie 10 — Kontext / Maschinenkonfiguration

- **Konfig-Panel** im Assistent-Tab: Freitext eingeben
  *(z. B. „Hauptausleger 74 m / Heckballast 124 t / Einscherung 6x")*
- **Analyse** → Felder als an-/abhakbare **Chips**; nur angehakte fließen in
  Suche/HyDE/Rerank ein
- Analyse via `parse_context` (Anthropic; bei `LLM_PROVIDER=local` rein
  regelbasiert — kein LLM)
- **Per-Frage-Relevanz**: nur zur Frage passende Konfig-Felder gehen ins
  Retrieval; Fast-Paths lesen den vollen Kontext
- Persistenz in `localStorage` (An-/Abhaken ohne Re-Analyse)

> Notiz: Verhindert, dass irrelevanter Kontext die richtige Seite verdrängt.

---

## Folie 11 — Tab „Meldungen" (Fehlercode-Lookup)

Ablauf (`/errorcode`):
1. Code eingeben *(z. B. `0x00000035` oder `R-003`)* oder Stichwort
   *(z. B. „Batterie", „Hydraulik")*
2. Optional per **Spracheingabe** oder **Kamera-Scan** erfassen
3. Ausgabe: **Beschreibung, Auswirkung, Problemlösung, mögliche Ursachen**
4. Plus verwandte **Handbuch-Quellen** (Retrieval auf die Beschreibung)

- Codes werden normalisiert (`0x35` ↔ `0x00000035`)
- Rein deterministisch (Datenbank `data/errorcodes.json` / `msgcodes.json`)

> Notiz: Schneller Griff für den Bediener an der Maschine — kein LLM.

---

## Folie 12 — Tab „Wartungen" (NEU)

Interaktive Wartungs-Checkliste, deterministisch & offline.

Ablauf:
1. **Betriebsstunden (Bh)** eingeben → fällige Stunden-Intervalle
   (8/40/500/1000/2000/4000 h) aktivieren sich automatisch
2. Aufgaben **nach Baugruppe gruppiert**, mit Fortschrittsbalken
3. **Abhaken / kommentieren / Foto** je Aufgabe
4. **Anleitung** (Voraussetzungen/Warnungen/Schritte) im Modal — oder
   **Handbuch** direkt im Iframe-Viewer
5. **Schichtbericht** (erledigt/offen/kommentiert)

- Zusatzfilter „Bei Bedarf" und „Hauptuntersuchung"
- **Handbuch-Links funktionieren** (alle 345 Seiten in `manuals/`)
- **Persistent** in `localStorage` (+ **Reset**-Button)
- Daten: `maintenance_tasks.json` (545 Tasks), `maintenance_instructions.json`

> Notiz: Ersetzt das manuelle Blättern in Plan-Tabelle + Handbuchseiten.

---

## Folie 13 — Render (Free) vs. Lokal

**Render Free**: kein Ollama, keine Semantik → QWEN ausgegraut, nur BM25 + TF-IDF.

| Tab / Modus | Render (Anthropic) | Lokal (Ollama) |
|-------------|--------------------|----------------|
| Klassisch | **Claude** (QWEN aus) | QWEN oder Claude |
| Assistent | **Regelbasiert** aktiv, Claude wählbar (QWEN aus) | Regelbasiert / QWEN / Claude |
| Meldungen · Wartungen | voll | voll |

- Gating über `ENABLE_LOCAL_BACKEND`: false → QWEN aus, Anfragen sicher
  heruntergestuft (Assistent→Regelbasiert, Klassisch→Claude), kein Call auf
  `localhost:11434`
- **Offener Punkt**: Semantik auf Render (768-dim passt nicht in 512 MB) →
  größere Instanz / ONNX / kleines MiniLM

> Notiz: „ohne Semantik" trifft alle Render-Modi; Regelbasiert kompensiert am
> wenigsten.

---

## Folie 14 — Wichtige Umgebungsvariablen (Kurzreferenz)

```bash
LLM_PROVIDER=anthropic|local          # globaler Default (UI überschreibt pro Request)
ENABLE_LOCAL_BACKEND=true|false       # QWEN/lokales Backend freischalten
AGENT_BACKEND=rule|qwen|anthropic     # Default-Backend im Assistent-Tab
LOCAL_BASE_URL=http://localhost:11434/v1
LOCAL_MODEL_EXPAND=qwen3:4b           # lokales Retrieval-Modell
SEARCH_DOCTYPE_BOOST=1.25             # Referenz-/Konfig-Seiten (1.0 = aus)
SEMANTIC_SEARCH=off                   # nur BM25 + TF-IDF
AGENT_LOCAL_MODE=sources|tools|pipeline  # Default sources (0 Modell-Calls)
```

- UI-Umschalter senden pro Request: `backend` (Klassisch), `agent_backend`
  (Assistent)

> Notiz: Vollständige Liste in `docs/local-llm-status.md` §2.

---

## Folie 15 — Gelöste Beispielfragen (0 Modell-Calls)

| Frage | Antwort |
|-------|---------|
| Lasthaken-Mindestgewicht 75 m / 5-fach | **1900 kg** |
| 74 m / 6-fach | kein Wert eingetragen (Spalte 6 leer) |
| Wie viele 12-m-Zwischenstücke bei 74 m | **4** |
| Anordnung Zwischenstücke 74 m | Anlenk 10 → 3 → 6 → 12 → 12 → 12 → 12 → Kopf 7 |
| Wo Seilführung bei 74 m einbauen | Konfig 1/3 am 5. Segment (S), Konfig 4 am 4. (N) |

> Notiz: Alle deterministisch aus Tabelle/Grafik mit Quelle — die einzige
> „formulierte" Antwortform, die wir zulassen.

---

## Folie 16 — Bekannte Grenzen & Ausblick

Grenzen:
- **Prozedurale Fragen** ohne Datenquelle → Quellen + Snippet (frei formuliert
  nur mit Claude bzw. `AGENT_LOCAL_MODE=tools/pipeline`)
- **Nadelausleger-Composition**: mehrere Varianten + 2 abweichende
  Tabellenstrukturen → aktuell kein Rateschluss
- **OCR neuer Manuals**: Tesseract nötig (Skript vorhanden)

Ausblick:
- Semantik auf Render (MiniLM/ONNX)
- Nadelausleger-Disambiguierung über die Konfiguration

> Notiz: Roadmap-Folie — was bewusst offen ist und warum.

---

## Folie 17 — Entscheidungshilfe: welcher Modus wann?

- **Schnell Quelle finden, günstig** → Assistent · **Regelbasiert**
  (oder Klassisch)
- **Exakter Tabellen-/Zusammenstellungswert** → jeder Assistent-Modus
  (Fast-Path greift; auch Regelbasiert)
- **Beste Formulierung bei prozeduralen Fragen** → Assistent · **Claude**
- **Bessere Suche lokal, ohne Internet** → **QWEN** (nur Retrieval)
- **Fehler-/Meldungscode** → Tab **Meldungen**
- **Wartung nach Betriebsstunden** → Tab **Wartungen**

> Notiz: Faustregel — Deterministisch zuerst; Claude nur, wenn wirklich
> formuliert werden muss.
