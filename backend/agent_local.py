"""
agent_local.py — Modus 3 (Agent) für den lokalen LLM-Betrieb (qwen3:4b via Ollama).

Der Anthropic-Agent in agent.py ist ein echter Tool-Calling-Loop über mehrere
Runden. Das ist für ein kleines lokales 4B-Modell zu fragil. Diese Variante ist
stattdessen eine überwiegend DETERMINISTISCHE Pipeline mit einer einzigen
Modell-Synthese — plus einer eng begrenzten Eskalation, wenn die erste Antwort
schwach aussieht (Hybrid-Ansatz).

Ablauf:
  1. Clarification-Check   — Keyword-Regeln aus Modus 1 (KEIN Modell)
  2. Retrieval             — Fusion aus normalisierter + roher Query (KEIN Modell)
  3. Confidence-Gate       — zu schwacher Score → "nicht gefunden" (KEIN Modell)
  4. Kontext-Aufbereitung  — Top-Seite lesen, Tabelle auf relevante Zeilen
                             eindampfen (KEIN Modell)
  5. Synthese              — EIN lokaler Modell-Call (1–2 Sätze, exakter Wert)
  6. Eskalation (optional) — bei schwacher Antwort EIN gezielter Lookup
                             (bal_search / grep_manual) + zweite Synthese

Damit: max. 2 Modell-Calls (statt bis zu 6), kein Tool-Calling-Protokoll,
kleiner Kontext. answer/verify/Modus-1/Render bleiben unberührt.
"""

import logging
import os
import re

from backend.rule_agent import (
    _needs_clarification,
    _normalize_query,
    _filter_by_score_gap,
    _STOPWORDS,
)
from backend.search import search, _load_index
from backend.agent_tools import read_page, grep_manual, bal_search
from backend.claude_client import local_complete

logger = logging.getLogger(__name__)

# Tuning — Schwellen auf der RRF×1000-Skala (wie in ask()); je nach aktiver
# semantischer Suche verschiebt sich die Skala, daher per Env kalibrierbar.
TOP_N_SEARCH       = 15
MAX_CONTEXT_CHARS  = int(os.environ.get("AGENT_LOCAL_MAX_CONTEXT", "3500"))
LOW_SCORE_NOMODEL  = float(os.environ.get("AGENT_LOCAL_MIN_SCORE", "8.0"))   # darunter: kein Modell-Call
ESCALATE_SCORE     = float(os.environ.get("AGENT_LOCAL_ESCALATE_SCORE", "25.0"))  # darunter: Eskalation

AGENT_LOCAL_SYSTEM = """\
Du bist ein Assistent für Liebherr-Kranbediener und Servicetechniker (LR 1104).
Antworte AUSSCHLIESSLICH auf Basis des gegebenen Seiten-Materials.

Regeln:
- Antworte auf Deutsch in 1–2 kurzen Sätzen. Kein Fließtext, keine Methodik.
- Nenne den gefundenen Wert direkt. Bei Abbildungen die Fig.-Nummer.
- Tabellen stehen als Zeilen "Zeilenkopf: Spalte=Wert | Spalte=Wert". Lies die
  passende Zeile exakt ab. Ist die gesuchte Zelle "—" oder leer, schreibe:
  "In der Tabelle ist für diese Konfiguration kein Wert eingetragen."
- Steht die Information nicht im Material, schreibe genau:
  "NICHT_IM_MATERIAL" — erfinde nichts.\
"""

# Negativ-Signale in der Antwort → Eskalation sinnvoll
_WEAK_ANSWER_RE = re.compile(
    r"nicht_im_material|nicht im material|nicht gefunden|keine? (angabe|information|wert)|"
    r"kann ich nicht|konnte ich nicht|nicht ersichtlich|nicht enthalten",
    re.I,
)

# Frage zielt auf einen exakten Wert (Zahl/Einheit) ab
_VALUE_QUESTION_RE = re.compile(
    r"\b(traglast|tragf|gewicht|länge|laenge|meter|\bm\b|tonnen|\bt\b|wert|"
    r"teilenummer|nummer|winkel|druck|bar|einscherung|wieviel|wie viel|wie lang|wie schwer)",
    re.I,
)

# Konfig-Werte aus dem Kontext: "74 m", "6x", "124 t", "6-fach"
_CONFIG_TOKEN_RE = re.compile(
    r"\d+\s*(?:m|t|x|fach|kg|bar|°)\b|\d+\s*[-]\s*fach|\b\d+x\b",
    re.I,
)


def _title_map() -> dict:
    try:
        return {e["filename"]: e["title"] for e in (_load_index() or [])}
    except Exception:
        return {}


def _merge(*lists: list[dict], top_n: int = TOP_N_SEARCH) -> list[dict]:
    """Union mehrerer search()-Listen nach filename; höheren Score behalten."""
    by_fname: dict[str, dict] = {}
    for lst in lists:
        for c in lst:
            fn = c["filename"]
            prev = by_fname.get(fn)
            if prev is None or c.get("score", 0) > prev.get("score", 0):
                by_fname[fn] = c
    return sorted(by_fname.values(), key=lambda c: c.get("score", 0), reverse=True)[:top_n]


def _config_tokens(context: str) -> list[str]:
    """Extrahiert Konfig-Werte (74 m, 6x, 124 t …) aus dem Kontext."""
    toks = [re.sub(r"\s+", " ", m.group(0)).strip() for m in _CONFIG_TOKEN_RE.finditer(context)]
    # dedup, Reihenfolge erhalten
    seen: set[str] = set()
    out = []
    for t in toks:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def _focus_context(page_text: str, context: str) -> str:
    """Dampft den Seitentext auf das Wesentliche ein, um dem Modell die Arbeit
    zu erleichtern:
      - Fließtext-Zeilen bleiben erhalten (kurzer Kontext).
      - Bei Tabellen mit Konfig-Werten im Kontext nur die passenden Zeilen +
        Tabellenkopf behalten; findet sich keine passende Zeile, bleibt der Block
        (gekappt) erhalten, damit nichts Wichtiges verloren geht.
    Immer auf MAX_CONTEXT_CHARS begrenzt.
    """
    tokens = [t.lower() for t in _config_tokens(context)]
    if not tokens:
        return page_text[:MAX_CONTEXT_CHARS]

    lines = page_text.splitlines()
    kept: list[str] = []
    any_row_matched = False
    for ln in lines:
        stripped = ln.strip()
        is_table_row = bool(re.match(r"^[^:]+:\s.*=", stripped))     # "74 m: colA=… | …"
        is_table_head = stripped.startswith("[Tabelle:")
        if is_table_row:
            if any(tok in stripped.lower() for tok in tokens):
                kept.append(ln)
                any_row_matched = True
            # nicht-passende Tabellenzeilen weglassen
        else:
            kept.append(ln)                                          # Prosa + Kopfzeilen behalten
        _ = is_table_head
    focused = "\n".join(kept).strip()
    # Wenn der Zeilenfilter nichts getroffen hat, lieber den vollen (gekappten) Text
    if not any_row_matched:
        return page_text[:MAX_CONTEXT_CHARS]
    return focused[:MAX_CONTEXT_CHARS]


def _build_user(compact_context: str, context: str, question: str) -> str:
    ctx_block = f"Maschinenkonfiguration: {context}\n\n" if context else ""
    return f"{ctx_block}Seiten-Material:\n{compact_context}\n\nFrage: {question}"


def _synthesize(top: dict, context: str, question: str) -> str:
    page = read_page(top["filename"])
    page_text = page.get("text", "") if isinstance(page, dict) else ""
    compact = _focus_context(page_text, context)
    answer = local_complete(AGENT_LOCAL_SYSTEM, _build_user(compact, context, question), max_tokens=256)
    return answer.strip()


def _needs_escalation(answer: str, question: str, top_score: float) -> bool:
    if top_score < ESCALATE_SCORE:
        return True
    if _WEAK_ANSWER_RE.search(answer):
        return True
    # Wertfrage, aber keine Zahl in der Antwort → vermutlich Wert nicht gefunden
    if _VALUE_QUESTION_RE.search(question) and not re.search(r"\d", answer):
        return True
    return False


def _escalate(question: str, context: str, seen: set[str]) -> list[dict]:
    """Gezielter Lookup mit den exakten Werkzeugen (Titel-Index + Volltext-Grep)."""
    titles = _title_map()
    keys = [w for w in _normalize_query(question).split()
            if len(w) > 3 and w.lower() not in _STOPWORDS]
    hits: list[dict] = []

    # 1) Exakter Titel-Index (BAL)
    if keys:
        for h in bal_search(" ".join(keys[:3])):
            fn = h.get("filename")
            if fn and "error" not in h and fn not in seen:
                hits.append({"filename": fn, "title": h.get("title", titles.get(fn, fn))})

    # 2) Exakter Wert per Grep (nur wenn Titel-Index nichts Neues brachte)
    if not hits:
        for tok in _config_tokens(context):
            for g in grep_manual(re.escape(tok)):
                fn = g.get("filename")
                if fn and "error" not in g and fn not in seen:
                    hits.append({"filename": fn, "title": titles.get(fn, fn)})
            if hits:
                break

    return hits[:2]


def run_agent_local(question: str, context: str = "", conversation: list[dict] | None = None) -> dict:
    """Lokaler Modus-3-Agent: deterministische Pipeline + max. 2 Modell-Calls."""
    is_followup = bool(conversation)

    # Phase 1: Clarification (kein Modell)
    if not is_followup:
        clar = _needs_clarification(question, context)
        if clar:
            logger.info("AgentLocal: Rückfrage → %s", clar)
            return {
                "type": "clarification",
                "question": clar,
                "messages": [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": clar},
                ],
            }

    # Query zusammenbauen (bei Followup letzte User-Antwort anhängen)
    if is_followup and conversation:
        last_user = next(
            (m["content"] for m in reversed(conversation) if m.get("role") == "user"), ""
        )
        raw_query = f"{question} {last_user}".strip() if last_user and last_user != question else question
    else:
        raw_query = question

    # Phase 2: Retrieval — Fusion (kein Modell)
    normalized = _normalize_query(raw_query)
    lists = [search(raw_query, top_n=TOP_N_SEARCH)]
    if normalized and normalized.lower() != raw_query.lower():
        lists.append(search(normalized, top_n=TOP_N_SEARCH))
    if context:
        lists.append(search(f"{context} {normalized}", top_n=TOP_N_SEARCH))
    candidates = _merge(*lists)

    if not candidates:
        return {"type": "answer", "answer": "Keine passenden Seiten im Manual gefunden.",
                "sources": [], "rounds": 0, "confidence": 0.0}

    top_score = candidates[0].get("score", 0)
    filtered = _filter_by_score_gap(candidates)

    # Phase 3: Confidence-Gate — zu schwach → kein Modell-Call
    if top_score < LOW_SCORE_NOMODEL:
        logger.info("AgentLocal: Score %.1f zu schwach — ohne Modell 'nicht gefunden'", top_score)
        return {
            "type": "answer",
            "answer": "Dazu konnte ich im Manual keine eindeutige Seite finden. "
                      "Bitte präzisieren Sie die Frage oder schlagen Sie in den Quellen nach.",
            "sources": [{"filename": r["filename"], "title": r["title"]} for r in filtered[:3]],
            "rounds": 0,
            "confidence": round(min(top_score / 40.0, 1.0), 2),
        }

    # Phase 4+5: Kontext aufbereiten + EINE Synthese
    top = filtered[0]
    answer = _synthesize(top, context, question)
    rounds = 1
    used = list(filtered[:3])
    logger.info("AgentLocal: Synthese (Score %.1f) → %s", top_score, answer[:100])

    # Phase 6: Eskalation — EIN gezielter Lookup bei schwacher Antwort
    if _needs_escalation(answer, question, top_score):
        seen = {r["filename"] for r in filtered}
        esc = _escalate(question, context, seen)
        if esc:
            logger.info("AgentLocal: Eskalation → %s", esc[0]["title"][:60])
            answer2 = _synthesize(esc[0], context, question)
            rounds = 2
            # Eskalierte Seite als primäre Quelle voranstellen
            used = esc[:1] + used
            # Bessere Antwort bevorzugen: die eskalierte, wenn sie nicht schwach ist
            if not _WEAK_ANSWER_RE.search(answer2):
                answer = answer2

    # NICHT_IM_MATERIAL sauber in Anzeigetext übersetzen
    if _WEAK_ANSWER_RE.search(answer):
        answer = ("Diese Information ließ sich auf den gefundenen Seiten nicht eindeutig "
                  "belegen. Bitte schlagen Sie in den verlinkten Quellen nach.")

    sources = [{"filename": r["filename"], "title": r["title"]} for r in used[:3]]
    return {
        "type": "answer",
        "answer": answer,
        "sources": sources,
        "rounds": rounds,
        "confidence": round(min(top_score / 40.0, 1.0), 2),
    }
