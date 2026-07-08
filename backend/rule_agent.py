"""
rule_agent.py — Regelbasierter RAG-Agent ohne LLM-Calls.

Ablauf:
  1. Prüfe ob eine ESSENTIELLE Information fehlt (Keyword-Regeln).
     → Wenn ja UND Kontext enthält sie nicht: Rückfrage zurückgeben.
  2. Frage normalisieren (Fragesatz → Kernbegriffe).
  3. Suche mit BM25+Semantic.
  4. Score-Gap-Filter: nur Quellen die nah genug am Top-Treffer sind.
  5. Wenn Score gut: Snippet aus Top-Seite extrahieren und anzeigen.
  6. Wenn alle Scores schwach: zweite Suche mit normalisierten Keywords.

Aktivierung: Umgebungsvariable RULE_AGENT=true  oder  AGENT_MODE=rule
"""

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning-Parameter
# ---------------------------------------------------------------------------
TOP_N_SEARCH        = 10
TOP_N_SOURCES       = 3

# Score-Gap: eine Quelle wird nur angezeigt wenn ihr Score ≥ GAP_RATIO * top_score
SCORE_GAP_RATIO     = 0.65   # z. B. Top=0.045 → min=0.029

# Schwache-Suche-Schwelle: wenn top_score < LOW_SCORE → zweite Suche
LOW_SCORE_THRESHOLD = 0.020

# Snippet-Länge (Zeichen vor/nach dem ersten Keyword-Match)
SNIPPET_BEFORE      = 100
SNIPPET_AFTER       = 300

# ---------------------------------------------------------------------------
# Fragesatz-Normalisierung
# Fragesatz-Präfixe werden abgeschnitten, damit BM25 die Kernbegriffe trifft.
# ---------------------------------------------------------------------------
_QUESTION_PREFIXES = re.compile(
    r"^(wie\s+(\w+\s+ich\s+|kann\s+ich\s+|funktioniert\s+|wird\s+|ist\s+)|"
    r"was\s+(ist|sind|bedeutet|mache\s+ich)\s+|"
    r"welche[rns]?\s+|"
    r"wo\s+(finde\s+ich|ist|sind)\s+|"
    r"wann\s+|warum\s+|wozu\s+|"
    r"kann\s+ich\s+|"
    r"muss\s+ich\s+)",
    re.I,
)

# Stoppwörter die nach dem Präfix noch übrig bleiben können
_STOPWORDS = {"die", "der", "das", "den", "dem", "ein", "eine", "einen", "einem",
              "einer", "ich", "an", "auf", "in", "mit", "zu", "für", "bei",
              "von", "aus", "nach", "über", "unter", "zwischen", "durch"}


def _normalize_query(question: str) -> str:
    """Entfernt Fragesatz-Präfixe und Stoppwörter → Kernbegriffe für BM25."""
    q = question.strip().rstrip("?").strip()
    q = _QUESTION_PREFIXES.sub("", q).strip()
    # Einzelne kurze Stoppwörter am Anfang entfernen
    words = q.split()
    while words and words[0].lower() in _STOPWORDS:
        words = words[1:]
    # Kurze Anhängewörter am Ende entfernen (z. B. "vor", "an", "ab")
    while words and len(words[-1]) <= 3 and words[-1].lower() in _STOPWORDS | {"vor", "ab", "an", "aus"}:
        words = words[:-1]
    normalized = " ".join(words).strip()
    return normalized if normalized else question


# ---------------------------------------------------------------------------
# Clarification-Regeln
# Jede Regel: (trigger_keywords, context_satisfiers, rückfrage)
#   trigger_keywords   — mind. eines muss in der Frage vorkommen
#   context_satisfiers — wenn mind. eines davon im Kontext steht → KEINE Rückfrage
#   question           — generische Rückfrage an den Bediener
# ---------------------------------------------------------------------------
_CLARIFICATION_RULES: list[tuple[list[str], list[str], str]] = [
    (
        ["traglast", "tragfähigkeit", "tragen", "heben", "last", "kapazität"],
        ["ausleger", "einscherung", "meter", " m ", "länge", "50m", "60m", "70m", "74m", "75m", "80m"],
        "Welche Auslegerlänge und Einscherung (z. B. 74 m, 6-fach) verwenden Sie?",
    ),
    (
        ["lasthaken", "unterflasche", "eigengewicht", "mindestgewicht", "hakengewicht"],
        ["einscherung", "meter", " m ", "länge", "ausleger"],
        "Welche Auslegerlänge und Einscherung verwenden Sie?",
    ),
    (
        ["zwischenstück", "zwischenstueck", "zwischenstücke", "bauteile", "konfiguration"],
        ["meter", " m ", "länge", "ausleger", "hauptausleger", "nadelausleger"],
        "Welche Gesamt-Auslegerlänge und welche Auslegervariante (Hauptausleger / Nadelausleger) benötigen Sie?",
    ),
    (
        ["einscherplan", "einscherpläne", "einscheren", "einscherung wählen"],
        ["einscherung", "fach", "winde", "lastort"],
        "Wie viele Einscherungen (z. B. 4-fach) und welcher Lastort (Hauptausleger-Kopf / Nadelausleger-Kopf)?",
    ),
    (
        ["seillänge", "seillänge berechnen", "benötigte seillänge"],
        ["einscherung", "fach", "meter", " m ", "länge", "ausleger"],
        "Welche Auslegerlänge und Einscherung verwenden Sie?",
    ),
]


def _keywords_in_text(keywords: list[str], text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _needs_clarification(question: str, context: str) -> str | None:
    """Rückfrage wenn eine essentielle Info fehlt und weder Kontext noch Frage sie enthält."""
    # Satisfier-Check über Kontext UND Frage — Frage kann die Info schon enthalten
    combined = (context + " " + question).lower()
    for trigger_kws, satisfier_kws, question_text in _CLARIFICATION_RULES:
        if _keywords_in_text(trigger_kws, question):
            if not _keywords_in_text(satisfier_kws, combined):
                return question_text
    return None


# ---------------------------------------------------------------------------
# Snippet-Extraktion — direkt aus dem Such-Index (kein HTML-Re-Read)
# ---------------------------------------------------------------------------

def _extract_snippet(filename: str, keywords: list[str]) -> str:
    """
    Gibt den relevantesten Textabschnitt zurück:
    - Für Anleitungsseiten: die ersten Handlungsschritte (steps)
    - Für Informationsseiten: Keyword-naher Ausschnitt aus dem Indextext
    Liest NICHT die HTML-Datei — nutzt ausschließlich den vorverarbeiteten Index.
    """
    try:
        from backend.search import _load_index
        idx = _load_index()
        entry = next((e for e in idx if e["filename"] == filename), None)
        if not entry:
            return ""

        # Anleitungsseiten: Schritte sind bereits sauber extrahiert
        steps = entry.get("steps") or []
        if steps:
            return "\n".join(f"• {s}" for s in steps[:8])

        text = entry.get("text", "").strip()
        if not text:
            return ""

        lower = text.lower()
        best_pos = len(text)
        for kw in keywords:
            pos = lower.find(kw.lower())
            if 0 <= pos < best_pos:
                best_pos = pos

        if best_pos == len(text):
            return text[:SNIPPET_AFTER].strip()

        start = max(0, best_pos - SNIPPET_BEFORE)
        end   = min(len(text), best_pos + SNIPPET_AFTER)
        return text[start:end].strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Score-Gap-Filter
# ---------------------------------------------------------------------------

def _filter_by_score_gap(candidates: list[dict]) -> list[dict]:
    """Behält nur Kandidaten deren Score ≥ GAP_RATIO * top_score."""
    if not candidates:
        return []
    top_score = candidates[0].get("score", 0)
    if top_score <= 0:
        return candidates[:TOP_N_SOURCES]
    min_score = top_score * SCORE_GAP_RATIO
    filtered = [c for c in candidates if c.get("score", 0) >= min_score]
    return filtered[:TOP_N_SOURCES]


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
      {"type": "answer", "answer": "...", "sources": [...], "rounds": N}
    """
    from backend.agent_tools import search

    is_followup = bool(conversation)

    # Phase 1: Clarification
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

    # Phase 2: Query normalisieren
    if is_followup and conversation:
        last_user = next(
            (m["content"] for m in reversed(conversation) if m.get("role") == "user"),
            "",
        )
        raw_query = f"{question} {last_user}".strip() if last_user != question else question
    else:
        raw_query = question

    normalized = _normalize_query(raw_query)
    logger.info("RuleAgent: Suche → '%s' (normalisiert: '%s')", raw_query[:80], normalized[:80])

    # Phase 3a: BAL-Titelindex (deterministisch, keine Scores nötig)
    from backend.agent_tools import bal_search
    bal_hits = bal_search(normalized, max_results=TOP_N_SOURCES)
    # Filtere Fehler-Einträge aus
    bal_hits = [h for h in bal_hits if "error" not in h and h.get("filename")]

    if bal_hits:
        logger.info("RuleAgent: BAL-Treffer %d für '%s'", len(bal_hits), normalized[:60])
        filtered = bal_hits[:TOP_N_SOURCES]
        rounds = 1
    else:
        # Phase 3b: BM25+Semantic als Fallback
        candidates = search(normalized, top_n=TOP_N_SEARCH)
        rounds = 1

        # Phase 4: Schwache Ergebnisse → zweite Suche mit Original-Frage
        top_score = candidates[0].get("score", 0) if candidates else 0
        if top_score < LOW_SCORE_THRESHOLD and normalized != raw_query:
            logger.info("RuleAgent: Score schwach (%.4f), zweite Suche mit Original-Query", top_score)
            candidates2 = search(raw_query, top_n=TOP_N_SEARCH)
            rounds = 2
            if candidates2 and candidates2[0].get("score", 0) > top_score:
                candidates = candidates2

        if not candidates:
            return {
                "type":    "answer",
                "answer":  "Keine passenden Seiten im Manual gefunden.",
                "sources": [],
                "rounds":  rounds,
            }

        # Phase 5: Score-Gap-Filter
        filtered = _filter_by_score_gap(candidates)

    # Phase 6: Snippet aus Top-Treffer
    top = filtered[0]
    keywords = [w for w in normalized.split() if len(w) > 3 and w.lower() not in _STOPWORDS]
    snippet = _extract_snippet(top["filename"], keywords)
    logger.info("RuleAgent: fertig, %d Quellen, snippet=%d Zeichen", len(filtered), len(snippet))

    sources = [{"filename": r["filename"], "title": r["title"]} for r in filtered]
    return {
        "type":    "answer",
        "answer":  snippet,
        "sources": sources,
        "rounds":  rounds,
    }
