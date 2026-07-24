# Lokales LLM & deterministischer Modus-3-Agent — Stand & Erkenntnisse

Arbeitsstand des Feature-Branches `claude/local-llm-mode-2-switch-g6kw3n`. -> gemergt mit MAIN
Dieses Dokument fasst **alle** Anpassungen und Erkenntnisse zusammen, damit sie
in einer neuen Konversation sofort verfügbar sind.

---

## 1. Überblick: die drei Modi

Die UI hat **zwei** Modi mit jeweils Backend-Umschaltern:

| UI | Backend | Endpoint | Retrieval | Finale Antwort |
|----|---------|----------|-----------|----------------|
| **Klassisch** | QWEN | `/ask` (backend=qwen) | HyDE+Rerank via qwen | Score + Quellen (kein LLM-Text) |
| **Klassisch** | Claude | `/ask` (backend=anthropic) | HyDE+Rerank via Claude | Score + Quellen |
| **Assistent** | Regelbasiert | `/ask_agent` (rule) | BM25/TF-IDF (kein LLM) | **deterministisch** (Fast-Paths+Quellen) |
| **Assistent** | QWEN | `/ask_agent` (qwen) | + qwen-HyDE + qwen-Rerank | **deterministisch** (kein qwen-Text) |
| **Assistent** | Claude | `/ask_agent` (anthropic) | agentischer Tool-Loop | Claude formuliert |

> **QWEN nur fürs Retrieval, nie fürs Formulieren.** HyDE erzeugt eine hypothetische
> Passage, die *nur die Suche speist* (nie angezeigt); Reranking ist reine Auswahl —
> beides ohne Halluzinationsrisiko. Die finale Antwort bleibt in Regelbasiert **und**
> QWEN deterministisch (Fast-Paths + Quellen). qwen formuliert ausschließlich, wenn
> man `AGENT_LOCAL_MODE=tools|pipeline` explizit setzt (experimentell).
>
> **Provider-Override:** `expand_query`/`rerank` (`claude_client`) nehmen pro Request
> einen `provider` (`local`=qwen, `anthropic`=Claude); `_mode2_complete` routet danach.
> `run_agent_local(assist="qwen")` ergänzt HyDE+Rerank best-effort (fällt der lokale
> Server aus, wird ohne Assist fortgesetzt — die Antwort ist ohnehin deterministisch).
>
> **Render-Matrix** (kein Ollama, kein `sentence-transformers`; QWEN ausgegraut):
> Klassisch → **Claude** (QWEN aus); Assistent → **Regelbasiert** (aktiv) / QWEN (aus) /
> Claude (wählbar). „ohne Semantic" gilt für **alle** Render-Modi (nur BM25+TF-IDF);
> Regelbasiert hat davon am wenigsten Kompensation.
>
> **Merge Modus 1:** Der frühere eigenständige „Lokal"-Modus ist als Assistent-Backend
> **Regelbasiert** aufgegangen; `mode=rule` bleibt rückwärtskompatibel. `rule_agent.py`
> bleibt geteilte Helfer-Bibliothek. Gating: `ENABLE_LOCAL_BACKEND=false` graut alle
> QWEN-Optionen aus und stuft QWEN-Anfragen sicher herunter (Assistent→rule, Klassisch→Claude).

**Provider-Schalter** `LLM_PROVIDER=anthropic|local` (Default `anthropic`):
- `anthropic`: alles wie bisher, Render unverändert.
- `local`: Modus 2 (`expand_query`/`rerank`) und Modus 3 laufen lokal
  (Ollama, OpenAI-kompatibel). `parse_context` bleibt immer Anthropic.

---

## 2. Alle Umgebungsvariablen

```bash
# Provider
LLM_PROVIDER=anthropic|local          # Default anthropic
LOCAL_BASE_URL=http://localhost:11434/v1
LOCAL_MODEL_EXPAND=qwen3:4b           # lokales Modell (Ollama-Tag)
LOCAL_API_KEY=ollama                  # Dummy, Ollama ignoriert ihn

# Modus 3: welcher Agent bedient "Assistent"
AGENT_BACKEND=auto|anthropic|local    # Default auto (folgt LLM_PROVIDER)
                                      # auch per Request-Feld agent_backend (UI-Umschalter)

# Modus 3 lokal: Verhalten bei NICHT-deterministischen Fragen
AGENT_LOCAL_MODE=sources|tools|pipeline   # Default sources
AGENT_LOCAL_MIN_SCORE=8.0             # darunter: "nicht gefunden" ohne Modell
AGENT_LOCAL_ESCALATE_SCORE=25.0       # (nur pipeline)
AGENT_LOCAL_MAX_CONTEXT=3500          # (nur pipeline)
AGENT_LOCAL_MAX_ROUNDS=6              # (nur tools)
AGENT_LOCAL_MAX_TOOL_ROUNDS=4         # (nur tools)
AGENT_LOCAL_READ_CHARS=3000           # (nur tools) read_page-Obergrenze

# Retrieval
SEARCH_DOCTYPE_BOOST=1.25             # Referenz-/Konfig-Seiten anheben (1.0 = aus)

# OCR-Preprocessing (nur wenn Composition-Daten neu erzeugt werden)
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

---

## 3. Modus 2 — lokaler LLM-Schalter (`claude_client.py`)

- Client-Fabrik: bei `local` ein OpenAI-kompatibler Client
  (`local_complete()`), sonst Anthropic. Nur die zwei tool-freien,
  zustandslosen Calls `expand_query()` (HyDE) und `rerank()` werden abgezweigt.
- **Qwen3-Thinking abschalten:** `/no_think` im Prompt **und** `<think>…</think>`
  serverseitig strippen (`_strip_think`).
- **Kein Fallback:** lokaler Server nicht erreichbar → klarer `HTTPException`
  (503/502), kein stiller Rückfall auf Anthropic.
- Startup-Log (`log_mode2_provider`) zeigt aktiven Provider + Agent-Modus.

---

## 4. Modus 3 lokal — Philosophie & Ablauf (`agent_local.py`)

**Leitidee (zurück zum ursprünglichen Ziel):** korrekte **Quellen** liefern,
nicht Prosa erfinden. Das kleine Modell (qwen3:4b) formuliert unzuverlässig
(falsche Seite, papageit Tabellen, englischer Murks, Frage-Echos). Daher:

- **Deterministisch, wo es geht** — exakte Extraktion mit Quelle ist *keine*
  Halluzination.
- **Sonst: Quellen + wörtlicher Snippet, KEIN LLM-Fließtext** (Default-Modus
  `sources`).

### Ablauf `run_agent_local()`
1. **Clarification** (Regeln aus Modus 1) — z. B. „Welche Auslegerlänge?"
2. **Retrieval** — Fusion aus roher + normalisierter (+ Kontext-)Query.
3. **Deterministische Fast-Paths** (score-unabhängig, kein Modell):
   - Composition-**Zählung** („wie viele 12 m Zwischenstücke bei 74 m")
   - Composition-**Anordnung** („Reihenfolge/Aufbau der Zwischenstücke")
   - **Seilführungs-Position** (S/N-Marker)
   - **Tabellenwert** (Lasthaken-Gewicht: Zeile Auslegerlänge × Spalte Einscherung)
4. **Confidence-Gate** — zu schwacher Score → „nicht gefunden".
5. **Nicht-Tabellenfrage** je `AGENT_LOCAL_MODE`:
   - `sources` (Default): Top-Seiten + wörtlicher Snippet, **0 Modell-Calls**.
   - `tools`: Seeded Tool-Loop (Modell mit read_page/grep/bal_search/lookup_table).
   - `pipeline`: eine Modell-Synthese aus der Top-Seite.

> **Warum das LLM in deinen Beispielen nie lief:** Im Default-Modus `sources`
> ist Modus 3 vollständig deterministisch — Tabellen/Zählung/Anordnung/Seilführung
> per Fast-Path, alles andere per Quellen+Snippet. Das ist beabsichtigt
> (Halluzinationsfreiheit). Willst du, dass qwen formuliert →
> `AGENT_LOCAL_MODE=tools` bzw. `pipeline`; für beste Qualität den Anthropic-
> Agenten via `AGENT_BACKEND=anthropic` (oder UI-Umschalter „Claude").

---

## 5. Deterministische Fast-Paths im Detail

### 5a. Tabellenwert (`_prelookup_table` / `lookup_table`)
- `lookup_table(filename, row_value, col_value)` in `agent_tools.py`: findet in
  den HTML-Tabellen einer Seite die Zeile mit Zahlenwert (z. B. „74 m") und
  optional die Spalte (z. B. „6" = 6-fach). Kein exakter Zeilenwert → **nächst
  kleinere UND größere Zeile**. Werte werden auf den metrischen Anteil gekürzt
  (`690 kg` statt `690 kg1,521 lb`).
- **Präzisions-Gates** (gegen selbstbewusst-falsche Antworten):
  1. Fast-Path nur bei **Zeile UND Spalte** (echter Zellen-Zugriff).
  2. **Relevanz-Gate**: getroffene Seite muss ein Sachwort mit der Frage teilen
     (Substring, für Flexionen; generische Wörter wie „Hauptausleger" zählen nicht).
- **Seitenfindung**: die richtige Tabellenseite ist oft nicht Top-Treffer →
  Pool aus Fusion-Kandidaten + gezielter, nach Relevanz gerankter Suche über die
  reine Sach-Frage (bei Rückfragen die ursprüngliche Frage aus der Konversation).
- Beispiel: „Lasthaken-Mindestgewicht bei 75 m / 5-fach" → **1900 kg**.

### 5b. Composition (Zählung / Anordnung / Seilführung)
Basis: `data/compositions.json` (siehe §6). Funktionen in `agent_tools.py`:
`composition_count`, `composition_arrangement`, `composition_seilfuehrung`.
- **Zählung**: „wie viele 12 m Zwischenstücke bei 74 m" → **4** (mittlere
  Segmente ohne Anlenkstück/Kopf).
- **Anordnung**: „Reihenfolge/Aufbau" → `Anlenkstück 10 m → 3 → 6 → 12 → 12 →
  12 → 12 → Kopf 7 m`.
- **Seilführung**: aus S/N-Markern → „Konfig 1/3 am 5. Segment (12-m-Zwischenstück,
  S); Konfig 4 am 4. Segment (N)".
- **Eindeutigkeits-Guard**: gibt es mehrere Seiten pro Auslegertyp (die
  Nadelausleger-Varianten!), wird **nicht** geraten (`ambiguous_boom` →
  Fall-through auf Quellen). Der Hauptausleger ist eindeutig.

---

## 6. OCR der Zusammenstellungs-Grafiken

### Das Problem
Die „Zusammenstellung des …auslegers"-Seiten kodieren die Bauteilfolge je
Auslegerlänge **als Grafik**, nicht als Text: kleine beschriftete Symbole
(Kästchen „12m", Keil „10m" = Anlenkstück, Pentagon „7m" = Kopf; Marker
**S/N** = Einbauposition der Seilführung, S = Konfig 1/3, N = Konfig 4). Das Wort
„Zwischenstück" steht **nur im Bild** → per Volltext bisher unauffindbar/unzählbar.

### Erkenntnis (wichtig!)
- Über **alle** Zusammenstellungsseiten gibt es nur **24 eindeutige Symbole**.
- Jedes Symbol trägt sauberen Klartext (Segmentlänge + Marker).
- **Plausibilitäts-Beweis**: die Segmentlängen einer Zeile summieren exakt auf
  die Auslegerlänge der Zeile (74 m = 10+3+6+12+12+12+12+7). Damit ist jede
  Zeile automatisch verifizierbar.

### Lösung ohne Tesseract
Die 24 Symbole wurden per Vision aufgelöst und `data/compositions.json` direkt
erzeugt. **Für das vorliegende Manual ist KEIN Tesseract nötig.**
- Enthalten: **5 Seiten, 79 Zeilen**, jede plausibilisiert. Nur Seiten mit
  **100 % validen Zeilen** aufgenommen; 2 Nadelausleger-Seiten mit abweichender
  Tabellenstruktur bewusst ausgelassen (keine unsicheren Daten).

### `preprocessing/ocr_compositions.py` (für neue Manuals)
Regeneriert/erweitert `compositions.json` per OCR. **Bildauswahl** (welche
Grafiken OCRt werden):
1. Seitentyp „Zusammenstellung", 2. nur `<img>` in Tabellenzellen, 3. Dedup,
4. Label muss zum Segment-Muster passen, 5. Zeile muss aufsummieren.
Benötigt die **Tesseract-Engine** (nicht nur pytesseract):
```
pip install pytesseract pillow
# Windows: Installer https://github.com/UB-Mannheim/tesseract/wiki (+ 'deu')
#          set TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
# macOS:   brew install tesseract tesseract-lang
# Linux:   sudo apt install tesseract-ocr tesseract-ocr-deu
python -m preprocessing.ocr_compositions
```
Das Skript prüft die Engine **vorab** und bricht sonst mit klarer Anleitung ab.

### Die „Zusammenstellung"-Seiten im Manual (13 gesamt)
- **1×** `Zusammenstellung des Hauptauslegers 2320` — die eigentliche Datenseite
  (18 Symbole, 24 Auslegerlängen). **Vollständig erfasst.**
- **6×** `Zusammenstellung des Hauptauslegers` — Querverweis-Stubs (0 Symbole),
  verweisen nur auf die 2320-Seite. Keine Daten.
- **6×** `Zusammenstellung des Nadelauslegers` — je Nadelausleger-Variante
  (0806, 0906, 1008, 1713, 1916, 1916+Midfall). Mehrere Varianten → der
  Composition-Fast-Path rät hier nicht (Eindeutigkeits-Guard), 4 davon sind in
  `compositions.json` (Index-Auffindbarkeit), 2 strukturell abweichende fehlen.
> „7 Seiten" = die mit echter Symbol-Tabelle (1 Haupt + 6 Nadel); die 6
> Hauptausleger-Stubs haben keine Symbole.

---

## 7. Retrieval-Verbesserungen (`search.py`)

- **Multi-Query-Fusion** in `/ask` (Modus 2) und im lokalen Agenten: Suche über
  HyDE-Passage **und** normalisierte Originalfrage, Ergebnisse gemergt (Union
  nach filename, höherer Score gewinnt). Verhindert, dass driftende HyDE-Passagen
  die richtige Seite ganz verdrängen.
- **Dokumenttyp-Prior** (`SEARCH_DOCTYPE_BOOST`, Default 1.25): Referenz-/Konfig-
  Seiten (Titel enthält zusammenstellung/übersicht/wahl/traglasttabelle/
  einscherplan/längen/gewichte/auslegerkonfiguration) werden moderat angehoben —
  generisch über den Seitentyp, nur auf bereits gefundene Kandidaten.
- **Composition-Index-Augmentation**: Bauteilbegriffe der Zusammenstellungsseiten
  („Zwischenstück", Segmentlängen, „Seilführung") werden in den **BM25-Korpus**
  gespeist (nicht in den Anzeigetext) → grafische Seiten werden per Freitext
  auffindbar.
- **Leichte deutsche Morphologie** (`_stem_de` in `_tokenize`, additiv): Umlaut-
  Faltung + Abstreifen einer Endung `-en/-er/-e` (bewusst kein `-n/-s`). Der Stamm
  wird ZUSÄTZLICH zum Originaltoken indexiert → Einzahl/Mehrzahl konvergieren
  („Einscherplan" ↔ „Einscherpläne", „Traglast" ↔ „Traglasten"). Rein generisch.
  *Grenze:* Titel, die beide Suchbegriffe wörtlich führen (z. B. „Einscherplan …
  über Hauptausleger-Kopf" der Nadelausleger-Seiten), bleiben lexikalisch echte
  Treffer — diese Intent-Unterscheidung löst erst die semantische Suche.
- **TF-IDF-Fallback jetzt auf Render aktiv**: `scikit-learn` ist in
  `requirements.txt` aufgenommen (~30 MB). Ohne semantische Suche (Render Free,
  kein sentence-transformers) greift damit der Char-N-Gram-Fallback statt reinem
  BM25 — überbrückt Komposita/Flexion zusätzlich zur Tokenizer-Morphologie.

---

## 8. Frontend (`frontend/MaschinenAssistent.html`)

- **iPhone-Portrait-Fix**: `height:100dvh` (statt 100vh) + `min-height:0` entlang
  app-layout/main/scroll + `viewport-fit=cover`. Header bricht auf ≤768px um
  (Mode-Umschalter in eigene Zeile), damit nichts rechts abgeschnitten wird.
- **Meldungen-Tab**: `errorcode-row` bricht auf Mobile um (Nachschlagen-Button
  war abgeschnitten).
- **Backend-Umschalter**: im Assistent-Modus erscheint Auto/Lokal/Claude; die
  Wahl geht als `agent_backend` an `/ask_agent` (persistent in localStorage).

---

## 9. Zentrale Erkenntnisse

1. **qwen3:4b ist der unzuverlässige Teil.** Alles Deterministische (Retrieval,
   Tabellen-Lookup, OCR-Composition) funktioniert; sobald das Modell selbst
   entscheidet/formuliert, entstehen Fehler. → Modell nur, wo es nicht
   halluzinieren kann, sonst Quellen.
2. **Deterministische Extraktion ≠ Halluzination.** Ein wörtlich aus Tabelle/
   Grafik gezogener Wert mit Quelle ist sicher — das ist die einzige „formulierte"
   Antwort, die wir zulassen.
3. **Selbstbewusst-falsch vermeiden.** Fast-Paths brauchen Präzisions-Gates
   (Zeile+Spalte, Relevanz, Eindeutigkeit) und melden im Zweifel ehrlich
   „nicht gefunden" / zeigen Quellen — lieber keine Antwort als eine falsche.
4. **Retrieval ist der Dauer-Engpass**, generisch angreifbar über Dokumenttyp-
   Boost, Fusion und Index-Augmentation — nicht über Themen-Hardcoding.
5. **Grafik-Daten sind erschließbar**, wenn die Grafiken Klartext tragen
   (Segment-Labels) — mit Plausibilitätsprüfung sogar selbstverifizierend.

---

## 10. Gelöste Beispielfragen (alle deterministisch, 0 Modell-Calls)

| Frage | Antwort |
|------|---------|
| Lasthaken-Mindestgewicht 75 m / 5-fach | 1900 kg |
| 74 m / 6-fach | kein Wert eingetragen (70/75 m, Spalte 6 leer) |
| Wie viele 12 m Zwischenstücke bei 74 m | 4 |
| Anordnung/Reihenfolge Zwischenstücke 74 m | Anlenk 10 → 3 → 6 → 12 → 12 → 12 → 12 → Kopf 7 |
| Wo Seilführung einbauen bei 74 m | Konfig 1/3 am 5. Segment (S), Konfig 4 am 4. (N) |

---

## 11. Bekannte Grenzen

- **Nadelausleger-Zählung/-Anordnung**: mehrere Varianten (0806/0906/1008/1713/
  1916/…) → müsste über die Konfiguration disambiguiert werden; 2 Seiten haben
  eine abweichende Tabellenstruktur (Parser-Erweiterung nötig). Aktuell:
  Fall-through auf Quellen (kein Rateschluss).
- **Prozedurale Fragen** („wie baue ich X ein") ohne deterministische Datenquelle:
  liefern Quellen + Snippet (kein LLM-Fließtext im Default). Für formulierte
  Antworten `AGENT_LOCAL_MODE=tools`/`pipeline` oder Anthropic-Backend.
- **OCR für neue Manuals**: Tesseract nötig (Skript vorhanden, prüft Engine vorab).

---

## 12. Wichtige Dateien

```
backend/claude_client.py   Modus 2 lokal + local_complete + Fabrik
backend/fastpaths.py       GETEILTE deterministische Fast-Paths (lokal + Claude)
backend/agent_local.py     Modus 3 lokal: run_fastpaths + sources/tools/pipeline
backend/agent.py           Modus 3 Claude: run_fastpaths VOR dem Tool-Loop
backend/agent_tools.py     lookup_table + composition_* + Tools
backend/search.py          Fusion, Dokumenttyp-Boost, Index-Augmentation
backend/main.py            /ask_agent-Routing + AGENT_BACKEND + ENABLE_LOCAL_BACKEND
preprocessing/ocr_compositions.py   OCR-Generator für compositions.json
data/compositions.json     Zusammenstellungs-Daten (5 Seiten, 79 Zeilen)
frontend/MaschinenAssistent.html    Responsive-Fixes + Backend-Umschalter
```

---

## 13. Geteilte Fast-Paths (Modus 3 lokal UND Claude)

Die deterministischen Fast-Paths (Composition-Zählung/-Anordnung, Seilführungs-
Position, Tabellenwert) lagen früher nur in `agent_local.py`. Sie sind jetzt in
`backend/fastpaths.py` extrahiert und laufen in **beiden** Modus-3-Agenten
**vor** der LLM-Schleife (`run_fastpaths()`):

- **Lokal** (`agent_local.run_agent_local`): unverändertes Verhalten, ruft nur
  noch `run_fastpaths(...)` statt der lokalen Kopien.
- **Claude** (`agent.run_agent`): `_fastpath_answer()` macht ein Fusions-Retrieval
  (`retrieve_fusion`) und ruft `run_fastpaths(...)` **vor** Triage/Tool-Loop. Greift
  ein Fast-Path, kommt die Antwort deterministisch (0 LLM-Calls, kein API-Key nötig);
  sonst normaler Loop wie bisher. Fehler im Fast-Path sind nie fatal (Fall-through).

Damit beantwortet auch „Claude" Zusammenstellung/Tabellen exakt & halluzinationsfrei
(löst den Test-2-Fall „Claude kann die Symbol-Tabelle nicht lesen"). Verifiziert:
„74 m / 12 m Zwischenstücke" → 4 (0 LLM); „75 m / 5-fach Lasthaken" → 1900 kg (0 LLM);
Nicht-Fast-Path-Fragen fallen sauber in den Loop.

---

## 14. Erledigt / geplant

**Erledigt:**
- ✅ **Regelbasierter Konfig-Parser für LOKAL** (`claude_client._rule_parse_context`):
  bei `LLM_PROVIDER=local` läuft `parse_context` rein regelbasiert — kein Anthropic
  UND kein lokales LLM. Normalisierung nach `vocab_rules.json` (74m→„74 m",
  6-fach→„6x", …), generische Klassifikation, atomare Zerlegung trennerloser Eingaben.
- ✅ **Konfig-Checkboxen** (Frontend): geparste Felder sind an-/abhakbare Chips; nur
  angehakte fließen in den Kontext (`acceptContext` filtert nach `enabled`). Der
  vollständige Feld-Satz inkl. abgehakter Elemente wird in `localStorage`
  (`ma_context_fields`) gehalten → An-/Abhaken **ohne** Re-Analyse, übersteht Reload.

**Erledigt (Forts.):**
- ✅ **Modus 1 verschmolzen**: eigener UI-Modus entfernt; „Assistent" hat jetzt nur
  noch die Backends **Lokal / Claude** (kein „Auto"), Standard Lokal (siehe §1).
- ✅ **Quellen mit Breadcrumb + Kurz-Snippet** (`main._enrich_sources`, deterministisch,
  KEIN LLM): gleichnamige Seiten (z. B. zwei „Montagefunktionen ausschalten") sind an
  ihrem Pfad unterscheidbar. Frontend zeigt Breadcrumb über dem Titel.
- ✅ **Bis zu 5 Quellen** im lokalen Quellen-Modus (statt 3, oberhalb des Score-Gaps),
  damit die passende Seite nicht knapp aus den Top-3 fällt (z. B. Lastort →
  „Bildschirmseite Windenkonfiguration" ist jetzt enthalten).
- ✅ **Clarification-Präzision**: Trigger „last" entfernt (matchte als Substring
  „Lastort"/„Ballast" → falsche Rückfragen).
- ✅ **3-Backend-Struktur** (siehe §1): Assistent = Regelbasiert / QWEN / Claude,
  Klassisch = QWEN / Claude. QWEN hilft nur beim Retrieval (HyDE+Rerank), formuliert
  nie. Provider-Override in `expand_query`/`rerank`; `/ask` bekam `backend`-Feld;
  `run_agent_local(assist="qwen")`. Render graut QWEN aus (Gating).

**Erledigt (Forts.):**
- ✅ **Per-Frage-Relevanz der Konfig** (`fastpaths.relevant_context`, deterministisch):
  nur die zur Frage passenden Konfig-Felder gehen ins **Retrieval** (Suche/HyDE/Rerank
  in `/ask` und `agent_local`); die **Fast-Paths lesen weiter den vollen Kontext**.
  Regel: Feld bleibt bei topischer Wort-Überlappung ODER wenn die Frage nach Wert/Menge
  fragt und das Feld numerisch ist. Beispiel: „Auf welcher Bildschirmseite konfiguriere
  ich den Lastort?" — „Hauptausleger 74 m" wird nicht mehr injiziert → „Bildschirmseite
  Windenkonfiguration" ist wieder unter den Treffern (vorher durch den Kontext verdrängt).

**Geplant / offen:**
- **Dokumenttyp-Boost feinjustieren**: im Lastort-Fall stehen die „Auslegerkonfiguration"-
  Seiten weiter auf #1–4 (Boost auf „auslegerkonfiguration"); „Windenkonfiguration" ist
  jetzt zwar enthalten (#5), aber nicht oben. Ggf. Boost kontext-/fragesensitiv machen.
- **Semantik auf Render**: siehe §11 — 768-dim-Modell passt nicht in 512 MB Free;
  Optionen: größere Instanz, ONNX-Query-Encoder oder kleines MiniLM (384-dim,
  Embeddings neu erzeugen).
- **Per-Frage-Relevanz der Konfig**: nur die zur Frage passenden Konfig-Werte in
  Suche/HyDE injizieren (statt immer den ganzen Kontext).
