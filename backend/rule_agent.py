"""
rule_agent.py — Regelbasierter RAG-Agent ohne LLM-Calls.

Ablauf:
  1. Prüfe ob eine ESSENTIELLE Information fehlt (Keyword-Regeln).
     → Wenn ja UND Kontext enthält sie nicht: Rückfrage zurückgeben.
  2. Suche mit BM25+Semantic (search).
  3. Lies die beste Seite vollständig wenn Score > Schwelle (read_page).
  4. Gibt Quellen zurück — keine LLM-Synthese.

Aktivierung: Umgebungsvariable RULE_AGENT=true  oder  AGENT_MODE=rule
"""

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Score-Schwelle für read_page
# ---------------------------------------------------------------------------
READ_PAGE_THRESHOLD = 0.025   # BM25-Score ab dem die Top-Seite vollständig gelesen wird
TOP_N_SEARCH       = 10
TOP_N_SOURCES      = 3

# ---------------------------------------------------------------------------
# Clarification-Regeln
# Jede Regel: (trigger_keywords, context_keywords_die_reichen, rückfrage)
#
#   trigger_keywords   — mind. eines muss in der Frage vorkommen
#   context_satisfiers — wenn mind. eines davon im Kontext steht → KEINE Rückfrage
#   question           — generische Rückfrage an den Bediener
# ---------------------------------------------------------------------------
_CLARIFICATION_RULES: list[tuple[list[str], list[str], str]] = [
    (
        # Fragen zu Traglast / Hubkapazität
        ["traglast", "tragfähigkeit", "tragen", "heben", "last", "kapazität"],
        # Kontext reicht wenn Auslegerlänge + Einscherung angegeben
        ["ausleger", "einscherung", "meter", " m ", "länge", "50m", "60m", "70m", "74m", "75m", "80m"],
        "Welche Auslegerlänge und Einscherung (z. B. 74 m, 6-fach) verwenden Sie?",
    ),
    (
        # Fragen zu Lasthaken / Unterflasche / Eigengewicht
        ["lasthaken", "unterflasche", "eigengewicht", "mindestgewicht", "hakengewicht"],
        ["einscherung", "meter", " m ", "länge", "ausleger"],
        "Welche Auslegerlänge und Einscherung verwenden Sie?",
    ),
    (
        # Fragen zu Zwischenstücken / Auslegerkonfiguration
        ["zwischenstück", "zwischenstueck", "zwischenstücke", "bauteile", "konfiguration"],
        ["meter", " m ", "länge", "ausleger", "hauptausleger", "nadelausleger"],
        "Welche Gesamt-Auslegerlänge und welche Auslegervariante (Hauptausleger / Nadelausleger) benötigen Sie?",
    ),
    (
        # Fragen zu Einscherplan / Einscherpläne
        ["einscherplan", "einscherpläne", "einscheren", "einscherung wählen"],
        ["einscherung", "fach", "winde", "lastort"],
        "Wie viele Einscherungen (z. B. 4-fach) und welcher Lastort (Hauptausleger-Kopf / Nadelausleger-Kopf)?",
    ),
    (
        # Seillänge (nicht "Winde" allein — zu generisch)
        ["seillänge", "seillänge berechnen", "benötigte seillänge"],
        ["einscherung", "fach", "meter", " m ", "länge", "ausleger"],
        "Welche Auslegerlänge und Einscherung verwenden Sie?",
    ),
]


def _keywords_in_text(keywords: list[str], text: str) -> bool:
    """True wenn mind. ein Keyword im Text vorkommt (Groß-/Kleinschreibung egal)."""
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _needs_clarification(question: str, context: str) -> str | None:
    """
    Gibt die Rückfrage zurück, wenn eine essentielle Information fehlt —
    aber nur wenn der Kontext die nötigen Infos NICHT bereits enthält.
    """
    combined_context = context.lower()

    for trigger_kws, satisfier_kws, question_text in _CLARIFICATION_RULES:
        if _keywords_in_text(trigger_kws, question):
            # Prüfe ob Kontext die fehlende Info schon enthält
            if not _keywords_in_text(satisfier_kws, combined_context):
                return question_text
    return None


# ---------------------------------------------------------------------------
# Haupt-Funktion
# ---------------------------------------------------------------------------

def run_rule_agent(
    question: str,
    context: str = "",
    conversation: list[dict] | None = None,
) -> dict:
    """
    Regelbasierter Agent ohne LLM.

    Returns:
      {"type": "clarification", "question": "...", "messages": [...]}
      {"type": "answer", "answer": "", "sources": [...], "rounds": N}
    """
    from backend.agent_tools import search, read_page

    # Nach einer Rückfrage enthält conversation die User-Antwort bereits —
    # dann direkt suchen, keine erneute Clarification-Prüfung.
    is_followup = bool(conversation)

    # Phase 1: Clarification (nur beim ersten Call, nicht bei Folgeantworten)
    if not is_followup:
        clarification = _needs_clarification(question, context)
        if clarification:
            messages = [
                {"role": "user",      "content": question},
                {"role": "assistant", "content": clarification},
            ]
            logger.info("RuleAgent: Rückfrage → %s", clarification)
            return {
                "type":     "clarification",
                "question": clarification,
                "messages": messages,
            }

    # Phase 2: Suchquery aufbauen — NUR die Frage, nicht den Kontext.
    # Der Kontext enthält Maschinendaten (z. B. "Heckballast: 124 t / Stahl-Haltestangen")
    # die sonst die Suchergebnisse in die falsche Richtung lenken.
    # Bei Folgeantwort auf eine Rückfrage: Nutzerantwort + ursprüngliche Frage kombinieren.
    if is_followup and conversation:
        last_user = next(
            (m["content"] for m in reversed(conversation) if m.get("role") == "user"),
            "",
        )
        query = f"{question} {last_user}".strip() if last_user != question else question
    else:
        query = question

    logger.info("RuleAgent: Suche → %s", query[:120])
    candidates = search(query, top_n=TOP_N_SEARCH)

    if not candidates:
        return {
            "type":    "answer",
            "answer":  "Keine passenden Seiten im Manual gefunden.",
            "sources": [],
            "rounds":  1,
        }

    # Phase 3: Top-Seite vollständig lesen wenn Score ausreichend
    top = candidates[0]
    if top.get("score", 0) >= READ_PAGE_THRESHOLD:
        logger.info("RuleAgent: read_page(%s) score=%.4f", top["filename"], top["score"])
        read_page(top["filename"])   # Ergebnis wird nicht ausgewertet — nur für Logging/Cache

    sources = [
        {"filename": r["filename"], "title": r["title"]}
        for r in candidates[:TOP_N_SOURCES]
    ]

    logger.info("RuleAgent: fertig, %d Quellen", len(sources))
    return {
        "type":    "answer",
        "answer":  "",        # Keine LLM-Synthese — Nutzer liest die Quelle selbst
        "sources": sources,
        "rounds":  1,
    }
