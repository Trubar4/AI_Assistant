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
from backend.fastpaths import run_fastpaths, retrieve_fusion

logger = logging.getLogger(__name__)

AGENT_MODEL   = os.environ.get("AGENT_MODEL",   "claude-haiku-4-5-20251001")
ANSWER_MODEL  = os.environ.get("ANSWER_MODEL",  "claude-sonnet-5")
MAX_ROUNDS    = int(os.environ.get("AGENT_MAX_ROUNDS", "5"))

MAX_TOOL_ROUNDS = int(os.environ.get("AGENT_MAX_TOOL_ROUNDS", "3"))

# Phase 1: decide clarification vs search (no tools)
_TRIAGE_SYSTEM = """\
Du bist ein Assistent für Liebherr-Kranbediener (LR 1104).

Entscheide: Kann ich die Frage im Manual suchen, oder fehlt eine ESSENTIELLE Information?

Essentielle Informationen die fehlen können:
- Konfiguration (z. B. Ausleger-Länge, Einscherung), wenn die Frage explizit davon abhängt
  Beispiel: "Welche Zwischenstücke brauche ich?" → Länge unbekannt → Rückfrage nötig
- Ausleger-Typ (Hauptausleger / Nadelausleger), wenn relevant für die Antwort
  Beispiel: "Was ist die Traglast?" → Konfiguration unbekannt → Rückfrage nötig

NICHT nachfragen wenn:
- Die Frage allgemein im Manual beantwortbar ist
  Beispiel: "Welche Winde ist Winde 1?" → Suche direkt
- Der Kontext bereits ausreichend ist

Antworte in einem Wort:
- "SUCHE" → direkt im Manual suchen
- "FRAGE: <deine präzise Rückfrage auf Deutsch>" → fehlende Info einholen
"""

AGENT_SYSTEM = """\
Du bist ein Assistent für Liebherr-Kranbediener und Servicetechniker (LR 1104).
Deine Aufgabe: Die EXAKTE Manual-Seite finden, die die gestellte Frage beantwortet.

Vorgehen:
1. Suche mit dem relevantesten Query. Bewerte die Ergebnisse.
2. Reicht der Textauszug nicht? Lies die Seite vollständig (read_page).
3. Suchst du nach einem exakten Wert in einer Tabelle? Nutze grep_manual.
4. Unsicher ob du die richtige Seite hast? Kreuzcheck mit bal_search.
5. Wenn du 1–3 gute Seiten gefunden hast: Antworte abschließend.

Antwortformat (KURZ):
- 1–3 Sätze auf Deutsch. Kein Fließtext, keine Erklärungen zur Methode.
- Nenne den gefundenen Wert direkt. Bei Abbildungen die Fig.-Nummer.
- KRITISCH bei Tabellenwerten: Tabellen sind Markdown. Zähle Spalten anhand der
  HEADER-ZEILE. Lies Zeile × Spalte exakt ab.
  Wenn die gesuchte Zelle "—" oder leer ist: schreibe "In der Tabelle ist für diese
  Konfiguration kein Wert eingetragen." NIEMALS einen Wert aus einer anderen Spalte
  als Ersatz nennen.

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


def _fallback_sources(messages: list[dict]) -> list[dict]:
    """Extract sources from read_page tool calls in message history."""
    seen: dict[str, str] = {}
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                # SDK object
                try:
                    if block.type == "tool_use" and block.name == "read_page":
                        fn = block.input.get("filename", "")
                        if fn and fn not in seen:
                            seen[fn] = fn
                except Exception:
                    pass
                continue
            if block.get("type") == "tool_use" and block.get("name") == "read_page":
                fn = block.get("input", {}).get("filename", "")
                if fn and fn not in seen:
                    seen[fn] = fn
    try:
        from backend.search import _index
        title_map = {e["filename"]: e["title"] for e in (_index or [])}
        return [{"filename": fn, "title": title_map.get(fn, fn)} for fn in seen]
    except Exception:
        return [{"filename": fn, "title": fn} for fn in seen]


def _fastpath_answer(question: str, context: str, conversation: list[dict] | None) -> dict | None:
    """Deterministische Fast-Paths vor dem LLM-Loop — identisch zum lokalen Agenten.

    So beantwortet auch "Claude" Zusammenstellung/Tabellen exakt & halluzinationsfrei
    (statt sie aus dem OCR-losen Seitentext zu raten, siehe Test 2). Schlägt kein
    Fast-Path an, läuft der normale Tool-Loop wie bisher. Fehler hier sind nie fatal.
    """
    raw_query = question
    if conversation:
        last_user = next(
            (m["content"] for m in reversed(conversation)
             if m.get("role") == "user" and isinstance(m.get("content"), str)),
            "",
        )
        if last_user and last_user != question:
            raw_query = f"{question} {last_user}".strip()
    candidates = retrieve_fusion(raw_query, context)
    return run_fastpaths(question, context, candidates=candidates,
                         search_query=question, filtered=candidates,
                         table_intent=raw_query)


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
    # Deterministische Fast-Paths zuerst (kein LLM, keine Halluzination). Nur wenn
    # keiner greift, folgt der eigentliche Claude-Tool-Loop.
    try:
        fp = _fastpath_answer(question, context, conversation)
        if fp is not None:
            logger.info("Agent: Fast-Path-Antwort (kein LLM-Loop)")
            return fp
    except Exception as exc:
        logger.warning("Fast-Path übersprungen (%s) → normaler Loop", exc)

    client = _get_client()

    # Aufbau der initialen Nachricht
    user_content = question
    if context:
        user_content = f"Maschinenkonfiguration: {context}\n\nFrage: {question}"

    # Konversations-History (für Clarification-Runden)
    messages: list[dict] = list(conversation or [])

    # Phase 1: Triage — nur wenn keine laufende Clarification-Konversation
    if not conversation:
        triage_resp = client.messages.create(
            model=AGENT_MODEL,
            max_tokens=120,
            system=_TRIAGE_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        triage_text = "".join(
            b.text for b in triage_resp.content if hasattr(b, "text")
        ).strip()
        logger.info("Triage: %s", triage_text[:120])

        if triage_text.upper().startswith("FRAGE"):
            clarification = triage_text[triage_text.index(":")+1:].strip() if ":" in triage_text else triage_text
            messages.append({"role": "user", "content": user_content})
            messages.append({"role": "assistant", "content": triage_text})
            return {
                "type": "clarification",
                "question": clarification,
                "messages": messages,
            }

    messages.append({"role": "user", "content": user_content})

    rounds = 0
    tool_rounds = 0
    forced_answer = False
    while rounds < MAX_ROUNDS:
        rounds += 1

        # After hitting tool round limit, inject reminder then disable tools
        # Use ANSWER_MODEL (Sonnet) for the final synthesis — more reliable for
        # table lookups and strict "no interpolation" compliance than Haiku.
        is_forced_final = tool_rounds >= MAX_TOOL_ROUNDS
        # Use Sonnet for any answer round that follows tool use — Haiku miscounts
        # table columns and interpolates empty cells even when instructed not to.
        use_answer_model = tool_rounds > 0
        call_kwargs: dict = dict(
            model=ANSWER_MODEL if use_answer_model else AGENT_MODEL,
            max_tokens=1500,
            system=AGENT_SYSTEM,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )
        if is_forced_final and not forced_answer:
            forced_answer = True
            messages.append({
                "role": "user",
                "content": (
                    "Genug Suchrunden. Fasse jetzt die Ergebnisse zusammen und antworte "
                    "abschließend auf Deutsch. Vergiss nicht den JSON-Quellen-Block am Ende:\n"
                    "```json\n{\"sources\": [{\"filename\": \"...\", \"title\": \"...\"}]}\n```"
                ),
            })
            call_kwargs["tool_choice"] = {"type": "none"}

        response = client.messages.create(**call_kwargs)

        logger.info(
            "Agent Runde %d: stop_reason=%s tool_calls=%d",
            rounds,
            response.stop_reason,
            sum(1 for b in response.content if b.type == "tool_use"),
        )

        # Tool-Calls ausführen
        if response.stop_reason == "tool_use":
            tool_rounds += 1
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

        sources = _extract_sources(answer_text)
        if not sources:
            sources = _fallback_sources(messages)
        cleaned = _clean_answer(answer_text)

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
