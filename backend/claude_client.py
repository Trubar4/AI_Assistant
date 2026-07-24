"""
claude_client.py

Two Claude API calls per /ask request:
  1. answer()   — generates a structured answer from retrieved context
  2. verify()   — checks whether the answer is grounded in the context

Returns a VerifiedAnswer dataclass with the answer text, source links,
and a grounding status: BELEGT | TEILWEISE | NICHT_BELEGT.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from fastapi import HTTPException

logger = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


ANSWER_MODEL   = os.environ.get("ANSWER_MODEL",   "claude-haiku-4-5-20251001")
VERIFIER_MODEL = os.environ.get("VERIFIER_MODEL", "claude-haiku-4-5-20251001")
EXPAND_MODEL   = os.environ.get("EXPAND_MODEL",   "claude-haiku-4-5-20251001")

# ---------------------------------------------------------------------------
# LLM-Provider für die zwei Modus-2-Calls (expand_query, rerank)
#
# LLM_PROVIDER=anthropic (Default)  → Anthropic API wie bisher.
# LLM_PROVIDER=local                → OpenAI-kompatibler Client gegen
#                                     LOCAL_BASE_URL (z. B. Ollama).
#
# Nur diese beiden tool-freien, zustandslosen Calls werden abgezweigt.
# answer()/verify()/parse_context() bleiben immer auf Anthropic.
# ---------------------------------------------------------------------------
LLM_PROVIDER      = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
LOCAL_BASE_URL    = os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1")
LOCAL_MODEL_EXPAND = os.environ.get("LOCAL_MODEL_EXPAND", "qwen3:4b")
# Ollama ignoriert den Key, das OpenAI-SDK verlangt aber einen nicht-leeren Wert.
LOCAL_API_KEY     = os.environ.get("LOCAL_API_KEY", "ollama")

_local_client = None


def log_mode2_provider() -> None:
    """Loggt beim Serverstart, welcher Provider Modus 2 und Modus 3 bedient."""
    if LLM_PROVIDER == "local":
        agent_mode = os.environ.get("AGENT_LOCAL_MODE", "sources").strip().lower()
        logger.info(
            "LLM-Provider=local model=%s url=%s | Modus 2 (expand/rerank) + "
            "Modus 3 (agent_local, mode=%s) lokal; parse_context bleibt Anthropic",
            LOCAL_MODEL_EXPAND, LOCAL_BASE_URL, agent_mode,
        )
    else:
        logger.info("LLM-Provider=anthropic (Modus 2 + Modus 3 über Anthropic)")


def _get_local_client():
    """OpenAI-kompatibler Client für den lokalen LLM-Server (z. B. Ollama)."""
    global _local_client
    if _local_client is None:
        from openai import OpenAI
        _local_client = OpenAI(base_url=LOCAL_BASE_URL, api_key=LOCAL_API_KEY)
    return _local_client


# Qwen3 gibt seine Gedankengänge in <think>…</think> aus. Trotz /no_think kann
# ein leerer oder befüllter Block auftauchen — serverseitig entfernen.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def local_complete(system: str, user_msg: str, max_tokens: int) -> str:
    """Ein einzelner, zustandsloser Completion-Call gegen den lokalen LLM-Server.

    Für Qwen3 wird der Thinking-Mode via /no_think abgeschaltet und ein etwaiger
    <think>…</think>-Block aus der Antwort entfernt. Kein Fallback: ist der Server
    nicht erreichbar, wird ein klarer Fehler (HTTPException) geworfen.

    Wird sowohl von Modus 2 (expand_query, rerank) als auch vom lokalen
    Modus-3-Agenten (agent_local) genutzt.
    """
    from openai import APIConnectionError, APIError

    try:
        response = _get_local_client().chat.completions.create(
            model=LOCAL_MODEL_EXPAND,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"{user_msg}\n\n/no_think"},
            ],
        )
    except APIConnectionError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Lokaler LLM-Server nicht erreichbar unter {LOCAL_BASE_URL}. "
                f"Läuft der Server (z. B. Ollama)? ({exc})"
            ),
        )
    except APIError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Fehler vom lokalen LLM-Server ({LOCAL_MODEL_EXPAND}): {exc}",
        )
    text = response.choices[0].message.content or ""
    return _strip_think(text)


def _mode2_complete(system: str, user_msg: str, max_tokens: int) -> str:
    """Ein einzelner, zustandsloser, tool-freier Completion-Call für Modus 2.

    Routet je nach LLM_PROVIDER an Anthropic oder den lokalen Endpunkt.
    """
    if LLM_PROVIDER == "local":
        return local_complete(system, user_msg, max_tokens)

    # Default: Anthropic
    response = _get_client().messages.create(
        model=EXPAND_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text.strip()


HYDE_SYSTEM = """\
Du bist ein Liebherr-Kran-Experte. Deine einzige Aufgabe: Schreibe einen kurzen
hypothetischen Textausschnitt (2-3 Sätze) so, wie er in einer Liebherr-Bedienungs-
anleitung stehen könnte, um die gestellte Frage zu beantworten.

WICHTIG: Schreibe IMMER einen Textausschnitt — auch wenn du die exakte Antwort
nicht kennst. Verwende dann Platzhalter wie "[Wert laut Tabelle]" oder beschreibe
welche Tabelle/Seite die Information enthalten würde.

Nutze die gegebenen Seitenüberschriften als Vokabular-Hilfe.
Keine Einleitungen, keine Erklärungen — nur den Textausschnitt.
Maximal 60 Wörter.\
"""


def expand_query(query: str, context: str = "") -> str:
    """Generate a TOC-guided hypothetical document (HyDE) for BM25+semantic retrieval.

    1. BM25 title-scan gives the LLM vocabulary hints — uses only the actual question
       (not the machine context) so context tokens don't pollute the title-scan scoring.
    2. LLM generates a short passage using those titles + the machine context as background.
    3. BM25+Semantic searches for that passage → finds pages with matching vocabulary.

    Falls back to original query on any error.
    """
    from backend.search import bm25_candidate_titles
    try:
        titles = bm25_candidate_titles(query, top_k=25)
        titles_block = "\n".join(f"- {t}" for t in titles) if titles else "(keine Kandidaten)"
        context_block = f"\nMaschinenkonfiguration: {context}\n" if context else ""

        user_msg = (
            f"Mögliche relevante Seitenüberschriften aus dem Manual:\n"
            f"{titles_block}\n"
            f"{context_block}\n"
            f"Frage: {query}"
        )
        hypothesis = _mode2_complete(HYDE_SYSTEM, user_msg, max_tokens=120)
        logger.info("HYDE '%s' → '%s'", query[:60], hypothesis[:120])
        return hypothesis
    except HTTPException:
        # Lokaler Modus ohne Fallback: klaren Fehler durchreichen.
        raise
    except Exception as exc:
        logger.warning("HyDE fehlgeschlagen: %s", exc)
        return query


RERANKER_SYSTEM = """\
Du bist ein Relevanz-Filter für Liebherr-Kranbedienungsanleitungen.

Dir wird eine Benutzerfrage und eine nummerierte Liste von Seitenüberschriften
mit kurzem Textauszug gezeigt. Wähle die 5 Seiten aus, die die Frage am
wahrscheinlichsten beantworten.

Antworte NUR mit den Nummern der 5 besten Seiten, kommagetrennt, in absteigender
Relevanz. Beispiel: 3,7,1,12,5\
"""


def rerank(query: str, candidates: list[dict], top_n: int = 5) -> list[dict]:
    """LLM-based reranker: selects top_n from candidates by reading title+snippet.

    Falls back to returning candidates[:top_n] on any error.
    """
    if len(candidates) <= top_n:
        return candidates
    try:
        lines = []
        for i, c in enumerate(candidates, 1):
            snippet = c.get("text", "")[:120].replace("\n", " ")
            lines.append(f"{i}. {c['title']} — {snippet}")
        candidates_block = "\n".join(lines)

        user_msg = f"Frage: {query}\n\nSeiten:\n{candidates_block}"
        raw = _mode2_complete(RERANKER_SYSTEM, user_msg, max_tokens=30)
        indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip().isdigit()]
        # Keep only valid indices, deduplicate, limit to top_n
        seen: set[int] = set()
        selected = []
        for idx in indices:
            if 0 <= idx < len(candidates) and idx not in seen:
                seen.add(idx)
                selected.append(candidates[idx])
            if len(selected) == top_n:
                break
        # Fill remaining slots from original order if LLM returned fewer than top_n
        for c in candidates:
            if len(selected) == top_n:
                break
            if c not in selected:
                selected.append(c)
        logger.info("RERANK '%s' → %s", query[:60], raw[:40])
        return selected
    except HTTPException:
        # Lokaler Modus ohne Fallback: klaren Fehler durchreichen.
        raise
    except Exception as exc:
        logger.warning("Reranker fehlgeschlagen: %s", exc)
        return candidates[:top_n]

_VOCAB_RULES_PATH = Path(__file__).parent.parent / "data" / "vocab_rules.json"

PARSE_CONTEXT_SYSTEM = """\
Du bist ein Konfigurations-Parser für Liebherr-Kranhandbücher (LR 1104).
Zerlege den Freitext in strukturierte Felder und normalisiere die Schreibweise
nach den Manual-Konventionen unten.

WICHTIG:
- Verwende IMMER die Manual-Schreibweise aus den Regeln (z. B. "6x" statt "6-fach").
- Kombiniere Typ und Wert in einem Feld (z. B. "Hauptausleger 74 m", nicht zwei Felder).
- Felder ohne klar erkennbaren Wert weglassen.
- Antworte NUR als JSON-Array: [{"schluessel": "...", "wert": "..."}, ...]
  Keine Erklärungen, kein Markdown, nur das JSON-Array.\
"""


def _vocab_rules_text() -> str:
    try:
        rules = json.loads(_VOCAB_RULES_PATH.read_text(encoding="utf-8"))
        lines = ["Manual-Konventionen (LR 1104):"]
        for k in rules.get("konventionen", []):
            ex = ", ".join(k.get("beispiele", [])[:4])
            avoid = ", ".join(k.get("nicht_verwenden", [])[:3])
            line = f"- {k['feld']}: Beispiele: {ex}"
            if avoid:
                line += f" | NICHT: {avoid}"
            if k.get("hinweis"):
                line += f" | {k['hinweis']}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        return ""


def parse_context(raw: str) -> list[dict]:
    """LLM-based context parser: free text → structured fields with canonical vocabulary.

    Returns list of {"schluessel": ..., "wert": ...} dicts.
    Falls back to splitting by / on any error.
    """
    vocab = _vocab_rules_text()
    system = PARSE_CONTEXT_SYSTEM
    if vocab:
        system += f"\n\n{vocab}"

    try:
        response = _get_client().messages.create(
            model=EXPAND_MODEL,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": f"Konfigurationstext:\n{raw}"}],
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [
                {"schluessel": str(d.get("schluessel", "")), "wert": str(d.get("wert", ""))}
                for d in parsed
                if d.get("schluessel") and d.get("wert")
            ]
    except Exception as exc:
        logger.warning("parse_context fehlgeschlagen: %s", exc)

    # Fallback: split by /
    fields = []
    for part in re.split(r"[/,;]+", raw):
        part = part.strip()
        if part:
            fields.append({"schluessel": "Konfiguration", "wert": part})
    return fields


ANSWER_SYSTEM = """\
Du bist ein Assistent für Liebherr-Maschinenführer und Servicetechniker.
Antworte ausschließlich auf Basis des gegebenen Kontext-Materials.
Wenn die gesuchte Information nicht im Kontext steht, sage das explizit –
erfinde keine Fakten.

Antworte auf Deutsch in GENAU EINEM Satz (maximal 2). Kein Fließtext,
keine Listen. Die Quellen werden dem Nutzer separat angezeigt.\
"""

VERIFIER_SYSTEM = """\
Du prüfst, ob eine KI-Antwort korrekt und vollständig durch den gegebenen
Kontext belegt ist – bezogen auf die gestellte Frage.

Regeln:
- BELEGT: Kontext enthält die gesuchte Information, Antwort gibt sie korrekt wieder.
- TEILWEISE: Kontext enthält nur einen Teil der Antwort, oder Antwort ist vage.
- NICHT_BELEGT: Kontext enthält die Information NICHT, ABER die Antwort behauptet
  trotzdem etwas (erfindet Fakten) – ODER der Kontext enthält die Info, die Antwort
  sagt aber fälschlicherweise "nicht im Kontext gefunden".

Sonderfall: Wenn die Antwort korrekt aussagt, dass die Information nicht im Kontext
vorhanden ist, und der Kontext sie tatsächlich nicht enthält → BELEGT.

Antworte ausschließlich mit einem der drei Wörter:
BELEGT
TEILWEISE
NICHT_BELEGT\
"""

FALLBACK_ANSWER = (
    "Diese Information konnte ich im Manual nicht eindeutig finden. "
    "Bitte schlagen Sie direkt in den verlinkten Seiten nach."
)


@dataclass
class VerifiedAnswer:
    answer: str
    grounding: str          # BELEGT | TEILWEISE | NICHT_BELEGT
    sources: list[dict] = field(default_factory=list)
    fallback_used: bool = False


def _build_context(results: list[dict]) -> str:
    parts = []
    for r in results:
        bc = " › ".join(r.get("breadcrumb", [])) or r["title"]
        warnings = "\n".join(f"  ⚠ {w}" for w in r.get("warnings", []))
        steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(r.get("steps", [])[:20], 1))
        section = f"### {r['title']} ({bc})\n"
        if warnings:
            section += warnings + "\n"
        if steps:
            section += steps + "\n"
        # Append remaining free text (capped to keep prompt size manageable)
        text = r.get("text", "")
        section += text[:1500]
        parts.append(section)
    return "\n\n---\n\n".join(parts)


def answer(query: str, results: list[dict]) -> str:
    if not results:
        return FALLBACK_ANSWER

    context = _build_context(results)

    try:
        response = _get_client().messages.create(
            model=ANSWER_MODEL,
            max_tokens=256,
            system=[{
                "type": "text",
                "text": ANSWER_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": [
                {
                    "type": "text",
                    "text": f"Kontext-Material:\n\n{context}",
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": f"Frage: {query}"},
            ]}],
        )
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=401, detail="Ungültiger Anthropic API-Key. Bitte ANTHROPIC_API_KEY in .env prüfen.")
    except anthropic.APIConnectionError:
        raise HTTPException(status_code=503, detail="Keine Verbindung zur Anthropic API. Internetverbindung prüfen.")
    answer_text = response.content[0].text.strip()
    logger.info("ANSWER [%s]: %s", query[:60], answer_text[:120])
    return answer_text


def verify(query: str, answer_text: str, results: list[dict]) -> str:
    context = _build_context(results)

    response = _get_client().messages.create(
        model=VERIFIER_MODEL,
        max_tokens=10,
        system=[{
            "type": "text",
            "text": VERIFIER_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": [
            {
                "type": "text",
                "text": f"Kontext-Material:\n\n{context}",
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": f"Frage: {query}\n\nAntwort:\n{answer_text}"},
        ]}],
    )
    raw = response.content[0].text.strip().upper()
    logger.info("VERIFY [%s] → raw=%r", query[:60], raw)
    for status in ("BELEGT", "TEILWEISE", "NICHT_BELEGT"):
        if status in raw:
            return status
    return "TEILWEISE"


# Threshold für "wahrscheinlich relevante Treffer" (RRF-Score × 1000)
_HIGH_CONFIDENCE_THRESHOLD = 18.0

MSG_HIGH = "Wahrscheinlich relevante Seiten gefunden — bitte direkt nachschlagen:"
MSG_LOW  = "Keine eindeutige Übereinstimmung — diese Seiten könnten trotzdem helfen:"


def ask(query: str, results: list[dict]) -> VerifiedAnswer:
    """Score-based confidence message. No LLM calls — answer/verify removed."""
    sources = [
        {
            "title": r["title"],
            "filename": r["filename"],
            "score": r.get("score", 0),
            "snippet": _extract_snippet(query, r),
        }
        for r in results
    ]

    top_score = results[0].get("score", 0) if results else 0
    high = top_score >= _HIGH_CONFIDENCE_THRESHOLD

    answer_text   = MSG_HIGH if high else MSG_LOW
    grounding     = "BELEGT" if high else "NICHT_BELEGT"
    fallback_used = not high

    logger.info("ASK score=%.1f confidence=%s", top_score, "HOCH" if high else "NIEDRIG")

    return VerifiedAnswer(
        answer=answer_text,
        grounding=grounding,
        sources=sources,
        fallback_used=fallback_used,
    )


def _extract_snippet(query: str, result: dict, max_len: int = 160) -> str:
    """Extract a short text passage from result that best matches the query words."""
    import re
    text = result.get("text", "")
    if not text:
        steps = result.get("steps", [])
        return steps[0][:max_len] if steps else ""

    query_words = {w.lower() for w in re.split(r"\W+", query) if len(w) > 3}
    # Score each sentence by how many query words it contains
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]
    if not sentences:
        return text[:max_len]

    best = max(sentences, key=lambda s: sum(w in s.lower() for w in query_words))
    return best[:max_len]
