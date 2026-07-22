# Plan: Lokales LLM für Modus 2 (konfigurierbar)

> Planungsdokument – noch nicht implementiert. Grundlage für einen separaten
> Umsetzungs-Auftrag. Stand: 2026-07.

## Ziel & Scope
- **Modus 2 (`/ask`)** soll wahlweise mit einem **lokalen Modell auf dem Notebook**
  statt mit Anthropic laufen — per Konfiguration umschaltbar.
- **Render-Deployment bleibt unverändert** (Provider = Anthropic). Der lokale
  Betrieb ist ausschließlich für das Notebook gedacht.
- **Modus 1** (Regelbasiert) ist ohnehin LLM-frei — nicht betroffen.
- **Modus 3** (Agent) bleibt **vorerst außen vor** (separates, größeres Thema
  wegen Tool-Calling; siehe „Später").

## Architekturentscheidungen (bereits geklärt)
1. **Standort:** Modell + Backend beide auf `localhost` (Notebook). Kein
   Cloud→On-Prem-Networking nötig.
2. **Runtime: Ollama.** Einfachste Installation auf dem Notebook,
   OpenAI-kompatibler Endpunkt (`http://localhost:11434/v1`), eingebaute
   JSON-Schema-Erzwingung, plattformübergreifend. (vLLM wäre produktiv-schneller,
   für ein Notebook aber Overkill.)
3. **Modell: Qwen3 8B** (Ollama-Tag `qwen3:8b`, Q4 ~5–6 GB) — beste
   Mehrsprachigkeit/Deutsch der kleinen Modelle; die zwei Modus-2-Calls sind
   trivial dafür. Leichtere Alternative zum Testen: `qwen3:4b` (~2,5 GB).
   (Hinweis: Qwen3 hat **kein** 7B — die dichten Größen sind 0.6b/1.7b/4b/8b/14b.)
4. **Client-Pfad: zweiter, OpenAI-kompatibler Client** (nicht LiteLLM-Proxy).
5. **Kein Fallback:** Ist Provider = local gesetzt und der lokale Server nicht
   erreichbar → **harter Fehler mit klarer Meldung**, kein stiller Rückfall auf
   Anthropic.

## Was Modus 2 technisch braucht (aus dem Code verifiziert)
- `/ask` macht genau **zwei LLM-Calls**, beide in `backend/claude_client.py`,
  beide **zustandslos** (keine Historie, kein Tool-Calling):
  1. `expand_query()` — HyDE-Passage (kurze deutsche Generierung)
  2. `rerank()` — wählt aus einer Nummernliste die besten 5
- Beide nutzen aktuell die Konstante `EXPAND_MODEL` und den lokalen
  `_get_client()` in `claude_client.py`.
- **Wichtig:** Der Agent (`agent.py`) hat einen **eigenen** `_get_client()`.
  Der Modus-2-Umbau berührt `agent.py` also **nicht** — saubere Isolation.

## Umzusetzen
1. **Client-Fabrik** in `claude_client.py`: liefert je nach `LLM_PROVIDER`
   entweder den Anthropic-Client (wie bisher) oder einen OpenAI-kompatiblen
   Client gegen den lokalen Endpunkt.
2. **Aufruf-Abzweigung** für `expand_query()` und `rerank()`:
   Anthropic-Messages-Format vs. OpenAI `chat.completions`. Da beide Calls
   tool-frei sind (nur system + user), ist die Abzweigung schlank.
3. **Konfiguration** (Env-Variablen), Vorschlag:
   - `LLM_PROVIDER=anthropic|local` (Default: `anthropic` → Render unverändert)
   - `LOCAL_BASE_URL=http://localhost:11434/v1`
   - `LOCAL_API_KEY=ollama` (Dummy)
   - `LOCAL_MODEL_EXPAND=qwen3:8b`
4. **Abhängigkeit:** `openai`-SDK zu `requirements.txt` (klein, kein PyTorch).

## Nicht anfassen
- `backend/agent.py` (Modus 3), `backend/rule_agent.py` (Modus 1), das
  Render-Setup, die semantische Suche.

## Setup-Schritte auf dem Notebook (für die Umsetzung)
1. Ollama installieren, `ollama pull qwen3:8b` (Tag ist 8b, nicht 7b)
2. `.env` lokal: `LLM_PROVIDER=local` (+ die vier Local-Variablen)
3. Auf Render: nichts ändern (kein `LLM_PROVIDER` gesetzt → Default Anthropic)

## Test
- Lokal: `/ask` mit `LLM_PROVIDER=local` → prüfen, dass HyDE + Reranking
  sinnvolle deutsche Ergebnisse liefern und Quellen erscheinen.
- Gegenprobe: `LLM_PROVIDER=anthropic` → Verhalten identisch zu heute.
- Server aus → klare Fehlermeldung statt Absturz/Timeout.

## Später (Modus 3, separater Auftrag)
- Agent-Loop lokal erfordert **zuverlässiges Tool-Calling über mehrere Runden**
  + wachsenden Tool-Kontext (Historie ist hier aktiv).
- Empfehlung dann: **Qwen 3.6** (2026er Leader für Function-Calling +
  strukturierte Ausgabe), 16–24 GB VRAM.
- Hebel: Tabellen per Python in modellfreundliches JSON umformen +
  **JSON-Schema-Erzwingung** (Ollama/llama.cpp) statt stärkeres Modell.
- Offen zu entscheiden: Tool-Call-Format-Handling im zweiten Client-Pfad,
  Kontextfenster-Größe, Modellwahl pro Rolle.
