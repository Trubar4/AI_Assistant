"""
agent.py — Agentic RAG Loop für den Maschinen-Assistenten.

Der Agent erhält eine Frage + optionalen Kontext und entscheidet selbst:
  1. Brauche ich mehr Info vom User?       → gibt type="clarification" zurück
  2. Suchen mit diesem Query?              → tool_use: search()
  3. Seite vollständig lesen?             → tool_use: read_page()
  4. Exakte Werte aus Tabellen holen?     → tool_use: grep_manual()
  5. BAL-Suchindex als Kreuzcheck nutzen? → tool_use: bal_search()
  6. Gut genug? Fertig.                   → gibt type="answer" + sources zurück

Maximale Runden: MAX_ROUNDS (verhindert Endlosschleifen).
"""

import json
import logging
import os

import anthropic

from backend.agent_tools import TOOL_SCHEMAS, TOOL_FN

logger = logging.getLogger(__name__)

AGENT_MODEL   = os.environ.get("AGENT_MODEL", "claude-haiku-4-5-20251001")
MAX_ROUNDS    = int(os.environ.get("AGENT_MAX_ROUNDS", "5"))

AGENT_SYSTEM = """\
Du bist ein Assistent für Liebherr-Kranbediener und Servicetechniker (LR 1104).
Deine Aufgabe: Die EXAKTE Manual-Seite finden, die die gestellte Frage beantwortet.

Vorgehen:
1. Analysiere die Frage. Fehlt eine wichtige Information (z. B. Konfiguration, Ausleger-Typ)?
   → Stelle EINE gezielte Rückfrage, bevor du suchst.
2. Suche mit dem relevantesten Query. Bewerte die Ergebnisse.
3. Reicht der Textauszug nicht? Lies die Seite vollständig (read_page).
4. Suchst du nach einem exakten Wert in einer Tabelle? Nutze grep_manual.
5. Unsicher ob du die richtige Seite hast? Kreuzcheck mit bal_search.
6. Wenn du 1–3 gute Seiten gefunden hast: Antworte abschließend.

Regeln:
- Maximal 2 Suchschritte, dann antworte.
- Rückfragen nur wenn WIRKLICH nötig — nicht bei jeder Frage.
- Antworte auf Deutsch, sachlich, knapp.
- Erkläre in 1–2 Sätzen WARUM die gefundenen Seiten die Frage beantworten.

Schreibe am Ende deiner finalen Antwort einen JSON-Block:
```json
{"sources": [{"filename": "...", "title": "..."}]}
```
"""


def _get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY nicht gesetzt.")
    return anthropic.Anthropic(api_key=api_key)


def _dispatch_tool(name: str, inputs: dict) -> str:
    fn = TOOL_FN.get(name)
    if fn is None:
        return json.dumps({"error": f"Unbekanntes Tool: {name}"})
    try:
        result = fn(**inputs)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        logger.warning("Tool %s fehlgeschlagen: %s", name, exc)
        return json.dumps({"error": str(exc)})


def _extract_sources(text: str) -> list[dict]:
    """Extrahiert den JSON-Sources-Block aus der finalen Antwort."""
    m = None
    # Suche nach ```json ... ``` Block
    m = __import__("re").search(r"```json\s*(\{.*?\})\s*```", text, __import__("re").S)
    if not m:
        m = __import__("re").search(r'\{"sources":\s*\[.*?\]\s*\}', text, __import__("re").S)
    if m:
        try:
            data = json.loads(m.group(1) if m.lastindex else m.group(0))
            return data.get("sources", [])
        except Exception:
            pass
    return []


def _clean_answer(text: str) -> str:
    """Entfernt den JSON-Sources-Block aus dem Anzeigetext."""
    text = __import__("re").sub(r"```json.*?```", "", text, flags=__import__("re").S)
    return text.strip()


def run_agent(
    question: str,
    context: str = "",
    conversation: list[dict] | None = None,
) -> dict:
    """
    Führt den Agentic-RAG-Loop aus.

    Returns:
      {"type": "clarification", "question": "..."}
      {"type": "answer", "answer": "...", "sources": [...], "rounds": N}
    """
    client = _get_client()

    # Aufbau der initialen Nachricht
    user_content = question
    if context:
        user_content = f"Maschinenkonfiguration: {context}\n\nFrage: {question}"

    # Konversations-History (für Clarification-Runden)
    messages: list[dict] = list(conversation or [])
    messages.append({"role": "user", "content": user_content})

    rounds = 0
    while rounds < MAX_ROUNDS:
        rounds += 1

        response = client.messages.create(
            model=AGENT_MODEL,
            max_tokens=1024,
            system=AGENT_SYSTEM,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        logger.info(
            "Agent Runde %d: stop_reason=%s tool_calls=%d",
            rounds,
            response.stop_reason,
            sum(1 for b in response.content if b.type == "tool_use"),
        )

        # Tool-Calls ausführen
        if response.stop_reason == "tool_use":
            # Antwort in History aufnehmen
            messages.append({"role": "assistant", "content": response.content})

            # Alle Tool-Calls dieser Runde ausführen
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                logger.info("Tool: %s(%s)", block.name, str(block.input)[:120])
                result_text = _dispatch_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

            messages.append({"role": "user", "content": tool_results})
            continue

        # Finale Textantwort
        answer_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                answer_text += block.text

        # Prüfen ob Agent eine Rückfrage stellt
        # Heuristik: kurze Antwort ohne Quellen = wahrscheinlich Rückfrage
        sources = _extract_sources(answer_text)
        cleaned = _clean_answer(answer_text)

        is_clarification = (
            not sources
            and len(cleaned) < 300
            and any(cleaned.rstrip().endswith(c) for c in ["?", "?\"", "?'"])
        )

        if is_clarification:
            logger.info("Agent stellt Rückfrage: %s", cleaned[:100])
            return {
                "type": "clarification",
                "question": cleaned,
                "messages": messages,   # für nächsten Call mitgeben
            }

        logger.info("Agent fertig: %d Quellen, %d Runden", len(sources), rounds)
        return {
            "type": "answer",
            "answer": cleaned,
            "sources": sources,
            "rounds": rounds,
        }

    # Fallback wenn MAX_ROUNDS erreicht
    return {
        "type": "answer",
        "answer": "Die Suche hat das Maximum an Runden erreicht. Bitte Frage präzisieren.",
        "sources": [],
        "rounds": rounds,
    }
