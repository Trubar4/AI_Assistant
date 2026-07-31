# Retrieval-Verbesserungen (Regelbasiert-Modus)

Status: umgesetzt · betrifft `backend/search.py`, `backend/fastpaths.py`,
`backend/rule_agent.py`, `data/search_synonyms.json`, `tests/`

Diese Doku beschreibt die Retrieval-Qualitäts-Hebel, die im **Regelbasiert-Modus**
(deterministisch, ohne LLM) die richtige Manual-Seite nach oben bringen, plus die
**Rückfrage-Logik** bei echter Mehrdeutigkeit. Ausgangspunkt waren reale Testfragen,
bei denen die passende Seite nicht (oder nicht oben) in den Quellen stand.

---

## 1. Ablauf einer Frage (Regelbasiert)

`/ask_agent` → `run_agent_local(mode="sources")` (`backend/agent_local.py`):

1. **Rückfrage-Gate** (`_needs_clarification`, nur beim ersten Turn) — fehlt eine
   entscheidende Angabe, wird nachgefragt statt geraten (§4).
2. **Retrieval** — `search()` wird mehrfach aufgerufen (Rohfrage, normalisiert,
   relevanter Konfig-Kontext) und per Score gemerged.
3. **Fast-Paths** (`backend/fastpaths.py`) — deterministische Antworten aus
   `data/compositions.json`/Tabellen (Zählung, Anordnung, Seilführung, Tabellenwert).
   Greift einer, ist dessen Quelle maßgeblich.
4. Sonst: **Quellen-Antwort** — die Top-Kandidaten aus `search()`.

Die Hebel A–F wirken innerhalb von `search()`. Reihenfolge der Score-Anpassung:
**RRF-Fusion → Doctype-Boost → A → D → E → F → Titel-Deduplizierung**.

---

## 2. Such-Hebel (`backend/search.py`)

| Hebel | Problem | Mechanik | Env (Default) |
|---|---|---|---|
| **A – Seltene-Wort-Bonus** | Ein distinktives Wort (z. B. „Lastort") geht in der flachen RRF-Fusion gegen semantische Fast-Duplikate unter | Kandidaten, die ein **seltenes** Frage-Wort wörtlich enthalten, bekommen einen festen Boost. Seltenheit = BM25-IDF im Fenster `[MIN, MAX]`; Interrogativa/generische Frage-Verben ausgeschlossen (`_RARE_TERM_STOP`). Bewusst **binär** — die Basis-Relevanz rankt unter den geboosteten Seiten weiter. Sprachunabhängig (keine Großschreibungs-Heuristik). | `SEARCH_RARE_TERM_BOOST=1.6`, `SEARCH_RARE_TERM_IDF_MIN=3.0`, `SEARCH_RARE_TERM_IDF_MAX=6.0`, `SEARCH_RARE_TERM_MAX=3` |
| **B – Generische Verben abwerten** | „Seile **wählen**" / „**Fahren** über Geländekuppe" matchten nur über das Verb | Kuratierte Verben (`_GENERIC_VERBS`, inkl. Stemmer-Stämme `wahl`/`fahr`) werden — wie Stoppwörter — aus Query **und** Index gefiltert | — (kuratierte Menge im Code) |
| **C – Synonym-/Query-Expansion** | Wortschatz-Lücke: „Meisterschalter" steht in 0 Seiten, das Manual sagt „Bedienhebel" | Query wird vor der Suche um kuratierte Manual-Begriffe ergänzt (`_expand_synonyms`) → BM25 **und** Semantik matchen | — (Tabelle: `data/search_synonyms.json`) |
| **D – Stub-/Kanonik-Unterscheidung** | Ein kurzer Verweis-Stub („… siehe: …") verdrängt die inhaltliche gleichnamige Seite (BM25 bevorzugt kurze Dokumente) | Kurze Verweis-Seiten (`_is_stub`: wenig Inhalt **und** „siehe"-Muster) werden vor der Titel-Dedup abgewertet | `SEARCH_STUB_PENALTY=0.5`, `SEARCH_STUB_MAX_WORDS=18` |
| **E – Komponenten-Scope** | „Einscherplan am **Hauptausleger**" zog Kopien unter *Nadelausleger*-Sektionen | Nennt die Frage genau EINE Auslegerkomponente, werden Seiten unter der ANDEREN abgewertet — **titel-bewusst**: eine Seite, deren *Titel* von der gefragten Komponente handelt (z. B. „Hauptausleger-Kopf … (Lastort 2)"), bleibt unberührt | `SEARCH_COMPONENT_PENALTY=0.6` |
| **F – Lastort-Varianten-Scope** | Followup „Lastort 2" lieferte Lastort-1-Seiten (die Ziffer fällt als 1-Zeichen-Token weg) | Nennt die Frage „Lastort N", werden Kandidaten mit abweichendem „Lastort M" im Titel abgewertet | `SEARCH_LASTORT_PENALTY=0.4` |

Zusätzlich vorbestehend: **Doctype-Boost** (`SEARCH_DOCTYPE_BOOST=1.25`) — hebt
Referenz-/Tabellenseiten fragesensitiv an.

Alle Boosts/Penalties wirken **nur auf bereits gefundene Kandidaten** und loggen
ihre Wirkung (`Seltene-Wort-Bonus …`, `Stub-Abwertung …`, `Komponenten-Scope …`,
`Lastort-Scope …`).

---

## 3. Kuratierte Tabellen (ohne Codeänderung pflegbar)

| Tabelle | Ort | Zweck |
|---|---|---|
| **Synonyme (C)** | `data/search_synonyms.json` | Nutzer- → Manual-Vokabular. Reine JSON-Pflege; Werte müssen echte Manual-Begriffe sein. Nach Änderung Backend neu starten (wird gecacht). |
| **Generische Verben (B)** | `_GENERIC_VERBS` in `backend/search.py` | Verben ohne Unterscheidungswert (+ deren Stämme). |
| **Rückfrage-Regeln (§4)** | `_CLARIFICATION_RULES` + `_needs_clarification` in `backend/rule_agent.py` | Trigger → Satisfier → Rückfragetext. |

---

## 4. Rückfragen bei Mehrdeutigkeit (`backend/rule_agent.py`)

Statt zu raten, fragt der Assistent nach, wenn eine entscheidende Angabe fehlt.
Die Rückfrage-Plumbing (Backend `type:"clarification"` → Frontend
`renderAgentClarification` → Followup mit `conversation`) existiert bereits; die
Antwort des Nutzers hängt sich als Followup an die Frage.

**Einscherplan-Disambiguierung** (frage-basiert, damit ein aktives Konfig-Feld die
Rückfrage nicht verdeckt):
- fehlt die Auslegerkomponente → „Hauptausleger oder Nadelausleger?"
- Hauptausleger genannt, aber kein Lastort → „Lastort 1 (nur Hauptausleger-Kopf)
  oder Lastort 2 (Hauptausleger-Kopf bei angebautem Nadelausleger)?"

Nach der Antwort greift bei „Lastort N" **Hebel F**, sodass die richtige Variante
oben steht.

Die generische Regeltabelle `_CLARIFICATION_RULES` deckt weitere Fälle ab
(z. B. Zwischenstück-/Wertfragen ohne Länge/Einscherung) und ist erweiterbar.

---

## 5. Env-Regler (Kalibrierung ohne Codeänderung)

| Variable | Default | Wirkung |
|---|---|---|
| `SEARCH_RARE_TERM_BOOST` | 1.6 | Stärke des Seltene-Wort-Bonus (1.0 = aus) |
| `SEARCH_RARE_TERM_IDF_MIN` / `_MAX` | 3.0 / 6.0 | IDF-Fenster „selten" (untere/obere Grenze) |
| `SEARCH_RARE_TERM_MAX` | 3 | max. Anzahl seltener Wörter |
| `SEARCH_STUB_PENALTY` | 0.5 | Abwertung Verweis-Stubs (1.0 = aus) |
| `SEARCH_STUB_MAX_WORDS` | 18 | Wortgrenze „Stub" |
| `SEARCH_COMPONENT_PENALTY` | 0.6 | Abwertung falscher Auslegerkomponente |
| `SEARCH_LASTORT_PENALTY` | 0.4 | Abwertung falscher Lastort-Variante |
| `SEARCH_DOCTYPE_BOOST` | 1.25 | Referenz-/Tabellenseiten-Boost |

---

## 6. Tests

Test-Set in `tests/`. Ausführen:

```bash
pip install -r requirements-dev.txt
pytest -v
```

| Datei | Inhalt | ML-Deps? |
|---|---|---|
| `test_fastpaths.py` | Fast-Paths (Ausleger/Länge feldbezogen, Seilführungs-Regression) | nein |
| `test_clarification.py` | Einscherplan-Rückfragen (Lastort 1/2, Komponente) | nein |
| `test_search_verbs.py` | Hebel B (Verb-Rauschen) | ja (`importorskip`) |
| `test_search_rare.py` | Hebel A (seltene Wörter) | ja |
| `test_search_stub.py` | Hebel D (Stub-Erkennung) | ja |
| `test_search_component.py` | Hebel E (titel-bewusst) + F (Lastort) | ja |
| `test_retrieval_eval.py` + `eval_questions.json` | End-to-End-Eval realer Fragen → Soll-Quelle bzw. Rückfrage (`run_agent_local`) | ja |

`fastpaths.py` importiert die Volltextsuche **lazy**, damit die deterministische
Kern-Logik (und ihre Tests) ohne numpy/Embeddings läuft. Tests, die den vollen
Such-Stack brauchen, überspringen per `importorskip`, wenn die Pakete fehlen.

**Eval-Set** (`tests/eval_questions.json`) ist manuell erweiterbar: pro Fall
`question`, optional `context`, dann entweder `expect` (Titel-Teilstring) +
`max_rank` **oder** `expect_clarification` (erwartete Rückfrage). `known_gap: true`
markiert bekannt offene Fälle als `xfail`.

---

## 7. Bekannt offen

- **„Wie wähle ich die Montagefunktion vor?"** — die Kanonik-Seite steht auf
  Rang 4 (innerhalb Top-5, aber nicht #1). Der Intent „vorwählen = einschalten"
  geht bei der Tokenisierung verloren (B entfernt „wähle", „vor" ist Stoppwort);
  ohne risikoreiche Phrasenerkennung nicht sauber auf #1 zu heben. Als
  Intent-Ambiguität akzeptiert.
