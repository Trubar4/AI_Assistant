# Maschinen-Assistent – Projekt-Briefing für Claude Code

## Ziel

Click-Demo eines KI-gestützten Assistenten für Liebherr-Maschinenführer und Servicetechniker.  
Der User stellt eine Frage in natürlicher Sprache (Text oder Sprache), das System antwortet aus dem maschinenspezifischen HTML-Manual – mit Link zur exakten Stelle im Dokument.

---

## Use Cases (Demo-Scope)

### Use Case 1 – Manual-Frage
- User fragt: *„Welche Schritte muss ich beim Aufrüsten mit xx Metern befolgen?"*
- System findet relevante HTML-Seiten im Manual
- Gibt strukturierte Antwort + klickbaren Link zur HTML-Seite
- Bei Unsicherheit: Disclaimer statt erfundener Antwort

### Use Case 2 – Fehlercode
- User gibt Fehlercode ein (Text oder Sprache)
- System schlägt in vorbereiteter Fehlercode-Datenbank nach
- Gibt Handlungsempfehlung zurück
- (In Demo: manuelle Fehlercode-Eingabe; kein Live-API zur Maschine nötig)

---

## Architektur

### Schicht 1 – Python Preprocessing (einmalig, offline)

Eingabe: `toc-de-de.js` + `metadata.rdf` + ~2000 HTML-Files

**Script 1: `parse_toc.py`**
- Parst `toc-de-de.js` (HTML-String mit `<a href="ID_xxx-de-DE.html">` und `<span class="toc-text">`)
- Extrahiert: Titel, Dateiname, Hierarchie (Breadcrumb), Eltern-Kind-Beziehung
- Ausgabe: `toc_index.json`

```json
[
  {
    "title": "Aufrüsten des Auslegers",
    "filename": "ID_abc123-de-DE.html",
    "breadcrumb": ["Inbetriebnahme", "Ausleger", "Aufrüsten des Auslegers"],
    "depth": 3
  }
]
```

**Script 2: `parse_metadata.py`**  
- Parst `metadata.rdf` (iiRDS-Standard)
- Extrahiert semantische Tags pro Topic (z. B. `#Maintenance`, `#Fault`, `#Diagnostics`)
- Merged mit `toc_index.json` → ergänzt `semantic_tags` pro Eintrag
- Nutzen: Verbessert Retrieval (z. B. Fehler-Fragen filtern auf `#Fault`-Topics)

**Script 3: `extract_content.py`**
- Lädt jede HTML-Datei aus dem Manual-Ordner
- Extrahiert: Titel (`<h1>`/`<h2>`), Fliesstext, Warnhinweise, Schritte (geordnete Listen)
- Bereinigt: Navigation, Menüs, redundante Tags entfernen
- Ausgabe: `content_index.json`

```json
{
  "ID_abc123-de-DE.html": {
    "title": "Aufrüsten des Auslegers",
    "text": "Voraussetzungen: ... Schritt 1: ... Schritt 2: ...",
    "warnings": ["WARNUNG: Abstützung prüfen bevor ..."],
    "word_count": 340
  }
}
```

**Script 4: `prepare_errorcodes.py`** *(falls Fehlercode-Dokumente vorhanden)*
- Konvertiert Fehlercode-Dokumente (Excel/PDF/HTML) → `errorcodes.json`

```json
{
  "E1042": {
    "description": "Hydraulikdruck zu niedrig",
    "cause": "...",
    "action": "..."
  }
}
```

---

### Schicht 2 – Backend (Python / FastAPI)

**`search.py` – Retrieval**
- Nimmt User-Frage
- Step 1: Keyword-/Titel-Matching gegen `toc_index.json` (Top 5 Treffer)
- Step 2: Lädt Inhalt der 5 Treffer aus `content_index.json`
- Für Demo: kein Vector-DB nötig. Einfaches TF-IDF oder rapidfuzz-Matching reicht.
- Rückgabe: Liste `[{title, filename, text, breadcrumb}]`

**`claude_client.py` – LLM-Calls**

*Call 1 – Antwort generieren:*
```
System: Du bist ein Assistent für Liebherr-Maschinen. Antworte NUR auf Basis 
        des gegebenen Kontexts. Wenn die Antwort nicht im Kontext steht, 
        sage das explizit.
User:   [Frage] + [Top-5 Kontext-Texte]
```

*Call 2 – Verifier:*
```
System: Prüfe, ob die folgende Antwort vollständig durch den gegebenen Kontext 
        belegt ist. Antworte nur mit: BELEGT / TEILWEISE / NICHT_BELEGT
User:   [Antwort] + [Kontext]
```

- Bei `NICHT_BELEGT`: Antwort durch Standardtext ersetzen:  
  *„Diese Information konnte ich im Manual nicht eindeutig finden. Bitte direkt nachschlagen."* + Link zur nächstbesten HTML-Seite

**`main.py` – FastAPI Endpunkte**
- `POST /ask` → Frage stellen, Antwort + Links zurückbekommen
- `POST /errorcode` → Fehlercode nachschlagen
- `GET /health` → Status

---

### Schicht 3 – Frontend (single HTML-App)

Analog zu `MaintenanceAssistant_v3.html`, neue Datei: `MaschinenAssistent.html`

**Features:**
- Maschinen-Auswahl oben (für Demo: fix vorgewählt)
- Texteingabe-Feld + Senden-Button
- Voice Input: Web Speech API (`SpeechRecognition`) – ~20 Zeilen JS, kein externer Service
- Voice Output: Web Speech API (`SpeechSynthesis`) – liest Antwort vor
- Antwort-Panel: Fliesstext + strukturierte Schritte falls vorhanden
- „Im Manual öffnen"-Button → öffnet direkte HTML-Seite (Link)
- Disclaimer-Banner bei niedriger Verifier-Konfidenz
- Fehlercode-Tab: separates Eingabefeld, Ergebnis-Panel

**Hinweis Voice:**  
Web Speech API funktioniert in Chrome/Edge auf Android-Tablets ohne Zusatzkosten.  
Bei Lärmproblemen in der Fahrerkabine: Upgrade auf OpenAI Whisper API (Pay-per-Minute).  
→ Das ist ein bewusstes MTP-Learning: erst testen, dann entscheiden.

---

## Datei- und Ordnerstruktur (Git-Repo)

```
/
├── preprocessing/
│   ├── parse_toc.py
│   ├── parse_metadata.py
│   ├── extract_content.py
│   └── prepare_errorcodes.py
│
├── backend/
│   ├── main.py              # FastAPI App
│   ├── search.py            # Retrieval-Logik
│   └── claude_client.py     # Anthropic API Calls
│
├── frontend/
│   └── MaschinenAssistent.html
│
├── data/                    # generiert durch Preprocessing, nicht im Repo
│   ├── toc_index.json
│   ├── content_index.json
│   └── errorcodes.json
│
├── manuals/                 # nicht im Repo – lokal oder Cloud-Storage
│   └── [2000 HTML-Files]
│
├── .env.example             # ANTHROPIC_API_KEY=...
├── requirements.txt
└── README.md
```

---

## Technologie

| Komponente | Technologie |
|---|---|
| Backend | Python 3.11+, FastAPI, uvicorn |
| LLM | Anthropic Claude API (claude-sonnet-4-5 oder claude-haiku für Speed) |
| Retrieval | rapidfuzz (Fuzzy-Matching) oder TF-IDF – kein Vector-DB für Demo |
| Frontend | Vanilla HTML/CSS/JS (single file, kein Framework) |
| Voice | Web Speech API (Browser-nativ, Chrome/Edge) |
| Deployment (Demo) | Lokal via `uvicorn main:app` – kein Cloud-Deployment nötig |

---

## Kosten (Anthropic API)

- `claude-haiku-4-5`: ~$0.001 pro Anfrage → für Demo vernachlässigbar
- Empfehlung: Mit Haiku starten (Speed), bei Qualitätsproblemen auf Sonnet wechseln
- API-Key anlegen auf console.anthropic.com, $10 aufladen reicht für Wochen

---

## Nicht in Scope für Demo

- Live-API zur Maschine (Fehlercodes kommen aus statischem JSON)
- Mehrsprachigkeit (nur Deutsch)
- Mehrere Maschinen (fix: eine Maschine)
- Vector-DB / echtes semantisches RAG
- Authentifizierung / Nutzerverwaltung
- Cloud-Deployment

---

## Risiken

| Risiko | Wahrscheinlichkeit | Mitigation |
|---|---|---|
| Schlechte Retrieval-Qualität bei vagen Fragen | Mittel | Nutzer zu präziseren Fragen anleiten; Beispielfragen im UI zeigen |
| Verifier erkennt Lücken im Manual nicht | Mittel | Expliziter Hinweis im UI: „Antwort basiert auf Manual, keine Garantie" |
| Voice Input versagt bei Kabinenlärm | Hoch | Bewusstes MTP-Learning – erst testen, dann Whisper evaluieren |
| HTML-Struktur der 2000 Files inkonsistent | Mittel | `extract_content.py` robust gegen fehlende Tags schreiben |
| Preprocessing bei 2000 Files langsam | Niedrig | Einmalig, parallel verarbeitbar |

---

## Nächste Schritte

1. Git-Repo anlegen
2. Einen Beispiel-HTML-Ordner (z. B. 50 Files) für Entwicklung bereitstellen
3. Anthropic API-Key in `.env` hinterlegen
4. Mit `parse_toc.py` starten → `toc_index.json` validieren
5. Danach `extract_content.py` → `content_index.json`
6. Backend + einfaches Frontend für ersten End-to-End-Test
