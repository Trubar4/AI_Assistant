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

import json
import logging
import os
import re

from backend.rule_agent import (
    _needs_clarification,
    _normalize_query,
    _filter_by_score_gap,
    _extract_snippet,
    _STOPWORDS,
)
from backend.search import search, _load_index
from backend.agent_tools import (
    read_page, grep_manual, bal_search, lookup_table,
    TOOL_SCHEMAS, TOOL_FN, LOOKUP_TABLE_SCHEMA,
)
from backend.fastpaths import (
    run_fastpaths,
    _config_tokens, _extract_row_col, _clean_answer_text, _VALUE_QUESTION_RE,
)
from backend.claude_client import (
    local_complete,
    _get_local_client,
    _strip_think,
    LOCAL_MODEL_EXPAND,
    LOCAL_BASE_URL,
)
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Verhalten für NICHT-Tabellenfragen (Tabellenwerte laufen immer über den
# deterministischen Fast-Path):
#   sources  (Default) = Quellen + wörtlicher Snippet, KEIN LLM-Fließtext.
#                        Anti-Halluzination: richtige Seiten zeigen, Nutzer liest.
#   tools              = Seeded Tool-Loop (Modell formuliert; Halluzinationsrisiko).
#   pipeline           = eine Modell-Synthese aus der Top-Seite (+ Eskalation).
AGENT_LOCAL_MODE   = os.environ.get("AGENT_LOCAL_MODE", "sources").strip().lower()

# Tuning — Schwellen auf der RRF×1000-Skala (wie in ask()); je nach aktiver
# semantischer Suche verschiebt sich die Skala, daher per Env kalibrierbar.
TOP_N_SEARCH       = 15
MAX_CONTEXT_CHARS  = int(os.environ.get("AGENT_LOCAL_MAX_CONTEXT", "3500"))
LOW_SCORE_NOMODEL  = float(os.environ.get("AGENT_LOCAL_MIN_SCORE", "8.0"))   # darunter: kein Modell-Call
ESCALATE_SCORE     = float(os.environ.get("AGENT_LOCAL_ESCALATE_SCORE", "25.0"))  # darunter: Eskalation

# Tool-Loop-Grenzen (bewusst höher als die Pipeline, aber gedeckelt)
TOOL_MAX_ROUNDS      = int(os.environ.get("AGENT_LOCAL_MAX_ROUNDS", "6"))
TOOL_MAX_TOOL_ROUNDS = int(os.environ.get("AGENT_LOCAL_MAX_TOOL_ROUNDS", "4"))
TOOL_READ_MAX_CHARS  = int(os.environ.get("AGENT_LOCAL_READ_CHARS", "3000"))

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

# _VALUE_QUESTION_RE, _config_tokens, _extract_row_col, _clean_answer_text sowie
# die deterministischen Fast-Paths liegen jetzt in backend/fastpaths.py (geteilt
# mit dem Claude-Agenten) und werden oben importiert.


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


# ═══════════════════════════════════════════════════════════════════════════
# Seeded Tool-Loop (AGENT_LOCAL_MODE=tools)
#
# Das deterministische Retrieval liefert die erste Trefferliste vor (der Schritt,
# an dem ein 4B-Modell am ehesten scheitert). Danach darf das Modell über native
# Function-Calls (Ollama, OpenAI-Format) selbst read_page/grep_manual/bal_search/
# search aufrufen, um nachzufassen, und antwortet abschließend als Text.
# ═══════════════════════════════════════════════════════════════════════════

AGENT_TOOLLOOP_SYSTEM = """\
Du bist ein Assistent für Liebherr-Kranbediener und Servicetechniker (LR 1104).
Deine Aufgabe: die EXAKTE Manual-Seite finden und den gefragten Wert nennen.

Werkzeuge:
- lookup_table(filename, row_value, col_value): Tabellenwert exakt nachschlagen.
  IMMER für Zahlenwerte aus Tabellen (Gewicht, Traglast, Länge) nutzen —
  z. B. lookup_table("ID_...html", "74 m", "6"). Niemals Tabellen selbst zählen.
- read_page(filename): liest eine Seite vollständig (Fließtext, Schritte).
- grep_manual(pattern): Volltext-Regex für exakte Werte.
- bal_search(keywords): exakter Seitentitel-Index.
- search(query): erneute Suche mit besseren Begriffen (HÖCHSTENS einmal).

Vorgehen:
1. Dir werden Kandidaten-Seiten (und ggf. bereits extrahierte Tabellenzeilen)
   genannt. Für einen Zahlenwert aus einer Tabelle rufe lookup_table auf der
   passendsten Seite auf. Sonst read_page.
2. Reicht es nicht, gezielt grep_manual/bal_search/search.
3. Wenige Runden, dann ANTWORTE.

HARTE REGELN:
- Antworte NIE allein aus Titeln oder Suchsnippets — die enthalten oft nicht
  den Wert. Immer erst lookup_table oder read_page.
- Gib den WERT an, nicht eine Beschreibung, wo die Info steht.
- Die gesuchte Größe (z. B. Gewicht in kg/t) darf NICHT mit einem Wert aus der
  Frage (z. B. Auslegerlänge in m) verwechselt werden.
- Leere Zelle ("—"): "Für diese Konfiguration ist kein Wert eingetragen."
- Nicht im Manual: sage das klar, erfinde nichts.

Finale Antwort: Deutsch, 1–2 kurze Sätze, nur der Wert. KEIN Tool-Aufruf mehr.
/no_think\
"""


# Tools für den lokalen Loop: die geteilten + das Tabellen-Tool (nur lokal).
_LOCAL_TOOL_SCHEMAS = TOOL_SCHEMAS + [LOOKUP_TABLE_SCHEMA]
_LOCAL_TOOL_FN = {**TOOL_FN, "lookup_table": lookup_table}


def _oai_tools(include_table: bool = True) -> list[dict]:
    """Übersetzt die lokalen Tool-Schemas ins OpenAI-Function-Format.

    lookup_table wird nur bei Wertfragen angeboten — sonst ruft das Modell es
    auch bei Wo-/Wie-Fragen auf, bekommt '—' und behauptet fälschlich
    'kein Wert eingetragen'."""
    schemas = _LOCAL_TOOL_SCHEMAS if include_table else TOOL_SCHEMAS
    return [
        {"type": "function", "function": {
            "name": s["name"],
            "description": s["description"],
            "parameters": s["input_schema"],
        }}
        for s in schemas
    ]


def _dispatch_local_tool(name: str, args: dict) -> str:
    """Führt ein Tool aus (read_page im Loop mit kleinerer Obergrenze für 4B)."""
    fn = _LOCAL_TOOL_FN.get(name)
    if fn is None:
        return json.dumps({"error": f"Unbekanntes Tool: {name}"})
    try:
        if name == "read_page":
            args = {**args, "max_chars": TOOL_READ_MAX_CHARS}
        result = fn(**args)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        logger.warning("AgentLocal Tool %s fehlgeschlagen: %s", name, exc)
        return json.dumps({"error": str(exc)})


def _local_chat(messages: list[dict], tools: list[dict] | None, max_tokens: int):
    """Ein Chat-Call gegen den lokalen Server; klarer Fehler statt Fallback."""
    from openai import APIConnectionError, APIError
    kwargs: dict = {"model": LOCAL_MODEL_EXPAND, "max_tokens": max_tokens, "messages": messages}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    try:
        return _get_local_client().chat.completions.create(**kwargs)
    except APIConnectionError as exc:
        raise HTTPException(status_code=503, detail=(
            f"Lokaler LLM-Server nicht erreichbar unter {LOCAL_BASE_URL}. "
            f"Läuft der Server (z. B. Ollama)? ({exc})"))
    except APIError as exc:
        raise HTTPException(status_code=502,
                            detail=f"Fehler vom lokalen LLM-Server ({LOCAL_MODEL_EXPAND}): {exc}")


# _extract_row_col, _content_tokens, _topically_related, _prelookup_table,
# _answer_from_table, _clean_answer_text → nach backend/fastpaths.py verschoben
# (geteilt mit dem Claude-Agenten), oben importiert.


# Distinkt englische Wörter (nicht auch deutsch) → Sprach-Erkennung
_EN_RE = re.compile(
    r"\b(the|and|this|that|must|with|for|which|required|provided|value|specific|"
    r"according|further|additional|would|need|following|length|main|after|be|is|"
    r"are|of|to|installation|cable|boom|consult)\b", re.I)

_GERMAN_REFORMAT_SYSTEM = (
    "Du bist Redakteur. Gib den folgenden Inhalt in EINEM knappen, sachlichen "
    "deutschen Satz wieder. Keine Anführungszeichen, kein Markdown, keine Zusätze, "
    "kein Englisch. Sagt der Inhalt, dass keine genaue Angabe vorhanden ist, "
    "formuliere genau das kurz auf Deutsch.\n/no_think")


def _ensure_german(answer: str) -> str:
    """Englische Modell-Antworten einmal auf Deutsch umformulieren."""
    if not answer:
        return answer
    hits = len({m.group(0).lower() for m in _EN_RE.finditer(answer)})
    if hits < 4:
        return answer
    logger.info("AgentLocal: Antwort wirkt englisch (%d Marker) → Umformulierung", hits)
    try:
        de = local_complete(_GERMAN_REFORMAT_SYSTEM,
                            f"Inhalt:\n{answer}", max_tokens=180)
        de = _clean_answer_text(de)
        return de or answer
    except HTTPException:
        raise
    except Exception:
        return answer


def _seed_message(candidates: list[dict], context: str, question: str,
                  prelookup: dict | None = None, prelookup_page: dict | None = None) -> str:
    lines = [f"{i}. {c['title']} [{c['filename']}]" for i, c in enumerate(candidates[:8], 1)]
    ctx_block = f"Maschinenkonfiguration: {context}\n\n" if context else ""
    seed = (f"{ctx_block}Bereits gefundene Kandidaten-Seiten (Titel [Dateiname]):\n"
            + "\n".join(lines))
    if prelookup and prelookup_page:
        seed += (
            f"\n\nVorab aus der Tabelle auf \"{prelookup_page['title']}\" "
            f"[{prelookup_page['filename']}] extrahiert ({prelookup['treffer']}):\n"
            f"{prelookup['text']}\n"
            "Wenn diese Zeilen die Frage beantworten, nutze sie direkt."
        )
    return seed + f"\n\nFrage: {question}"


def _looks_like_nonanswer(answer: str, question: str) -> bool:
    """Erkennt Nicht-Antworten: leer, zu kurz oder bloße Wiederholung der Frage."""
    a = (answer or "").strip().lower().rstrip("?.! ")
    if len(a) < 12:
        return True
    q = question.strip().lower().rstrip("?.! ")
    if a == q or (len(a) > 20 and a in q) or (len(q) > 20 and q in a):
        return True
    return False


def _run_tool_loop(question: str, context: str, candidates: list[dict],
                   include_table: bool = True) -> tuple[str, list[dict], int]:
    """Seeded Tool-Loop. Gibt (Antworttext, gelesene Quellen, Runden) zurück."""
    tools = _oai_tools(include_table=include_table)
    titles = _title_map()
    messages: list[dict] = [
        {"role": "system", "content": AGENT_TOOLLOOP_SYSTEM},
        {"role": "user", "content": _seed_message(candidates, context, question)},
    ]
    read_order: list[str] = []          # Reihenfolge gelesener Seiten → Quellen
    rounds = 0
    tool_rounds = 0
    nudged = False                      # Lesezwang: max. ein Anstoß

    while rounds < TOOL_MAX_ROUNDS:
        rounds += 1
        force_final = tool_rounds >= TOOL_MAX_TOOL_ROUNDS
        resp = _local_chat(messages, tools=None if force_final else tools, max_tokens=700)
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if tool_calls and not force_final:
            tool_rounds += 1
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                if name in ("read_page", "lookup_table") and args.get("filename"):
                    fn = args["filename"]
                    if fn not in read_order:
                        read_order.append(fn)
                logger.info("AgentLocal Tool: %s(%s)", name, str(args)[:100])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _dispatch_local_tool(name, args),
                })
            continue

        # Lesezwang: keine Antwort aus Titeln/Snippets ohne gelesene Seite.
        if not read_order and not nudged and not force_final:
            nudged = True
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user", "content": (
                "Du hast noch KEINE Seite gelesen und darfst nicht aus Titeln oder "
                "Snippets antworten. Lies zuerst die vielversprechendste Kandidaten-"
                "Seite mit read_page (oder nutze lookup_table), dann antworte.")})
            continue

        # Finale Textantwort
        answer = _strip_think(msg.content or "").strip()
        if _looks_like_nonanswer(answer, question):
            answer = ("Ich konnte auf den gefundenen Seiten keine eindeutige Antwort "
                      "formulieren. Bitte in den verlinkten Quellen nachschlagen.")
        # Quellen: gelesene Seiten, sonst der beste Seed-Kandidat
        srcs = [{"filename": fn, "title": titles.get(fn, fn)} for fn in read_order[:3]]
        if not srcs and candidates:
            c = candidates[0]
            srcs = [{"filename": c["filename"], "title": c["title"]}]
        return answer, srcs, rounds

    # Runden erschöpft ohne finale Antwort
    srcs = [{"filename": fn, "title": titles.get(fn, fn)} for fn in read_order[:3]]
    return ("Die Suche hat das Rundenlimit erreicht. Bitte die Frage präzisieren "
            "oder in den Quellen nachschlagen."), srcs, rounds


# Die deterministischen Fast-Paths (Composition-Zählung/-Anordnung, Seilführung,
# Tabellenwert) liegen jetzt in backend/fastpaths.py und werden über
# run_fastpaths() aufgerufen — dieselbe Logik nutzt auch der Claude-Agent.


_SOURCES_LEAD = "Wahrscheinlich relevante Manual-Seiten — bitte dort nachschlagen:"


_SOURCES_GAP_RATIO = 0.65   # Quelle zeigen, wenn Score ≥ 65 % des Top-Scores
_SOURCES_MAX       = 5      # bis zu 5 (statt 3): die richtige Seite fällt sonst
                            # leicht aus den Top-3 (z. B. Lastort → Windenkonfiguration #5)


def _sources_answer(candidates: list[dict], question: str, conf: float) -> dict:
    """Anti-Halluzinations-Antwort: Quellen + wörtlicher Snippet, KEIN Modell.

    Der Snippet ist verbatim aus dem Seitentext (kein generierter Fließtext),
    dient nur als Vorschau. Die eigentliche Antwort sind die verlinkten Seiten.
    Es werden bis zu _SOURCES_MAX Seiten oberhalb des Score-Gaps gezeigt, damit
    die tatsächlich passende Seite nicht knapp aus den Top-3 fällt. Breadcrumb/
    Kurzbeschreibung ergänzt main._enrich_sources deterministisch.
    """
    top = candidates[0]
    top_score = top.get("score", 0)
    min_score = top_score * _SOURCES_GAP_RATIO
    picks = [c for c in candidates if c.get("score", 0) >= min_score][:_SOURCES_MAX]
    if not picks:
        picks = candidates[:_SOURCES_MAX]
    keywords = [w for w in _normalize_query(question).split()
                if len(w) > 3 and w.lower() not in _STOPWORDS]
    snippet = _extract_snippet(top["filename"], keywords)
    answer = _SOURCES_LEAD
    if snippet:
        answer = f"{_SOURCES_LEAD}\n\nAuszug aus „{top['title']}“ (wörtlich):\n{snippet}"
    return {
        "type": "answer",
        "answer": answer,
        "sources": [{"filename": r["filename"], "title": r["title"]} for r in picks],
        "rounds": 0,
        "confidence": conf,
    }


def run_agent_local(question: str, context: str = "", conversation: list[dict] | None = None,
                    mode: str | None = None) -> dict:
    """Lokaler Modus-3-Agent.

    Modellfreie Vorstufe (Clarification, Fusion-Retrieval, Confidence-Gate),
    dann deterministischer Tabellen-Fast-Path (wörtlicher Wert + Quelle). Ist
    das keine Tabellenfrage, entscheidet der Modus (Parameter `mode`, sonst
    AGENT_LOCAL_MODE):
      - "sources" (Default): Quellen + wörtlicher Snippet, KEIN LLM-Fließtext.
      - "tools"            : Seeded Tool-Loop (Modell formuliert, Risiko).
      - "pipeline"         : eine Modell-Synthese aus der Top-Seite.

    `mode="sources"` erzwingt den vollständig modellfreien Betrieb — so wird der
    frühere Modus 1 ("Regelbasiert") als „Modus 3 lokal ohne Modell" bedient
    (deterministische Fast-Paths inklusive), unabhängig von AGENT_LOCAL_MODE.
    """
    effective_mode = (mode or AGENT_LOCAL_MODE).strip().lower()
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
    conf = round(min(top_score / 40.0, 1.0), 2)

    # ── Deterministische Fast-Paths (score-unabhängig, keine Halluzination) ───
    # Laufen VOR dem Confidence-Gate: sie hängen an der Frage/den Daten, nicht am
    # Retrieval-Score. "Wie viele 12 m Zwischenstücke bei 74 m?" → compositions.json.
    # Dieselbe Logik nutzt der Claude-Agent (backend/fastpaths.py). Der Tabellen-
    # Fast-Path erkennt Zeile/Spalte auf raw_query (inkl. Rückfrage-Antwort), die
    # gezielte Tabellensuche nutzt die reine Sach-Frage (bei Rückfrage die ursprüngliche).
    content_q = next((m["content"] for m in (conversation or [])
                      if m.get("role") == "user"), question)
    fp = run_fastpaths(question, context, candidates=candidates,
                       search_query=content_q, filtered=filtered,
                       conf=conf, table_intent=raw_query)
    if fp is not None:
        return fp

    # ── Confidence-Gate für den retrieval-basierten Pfad ──────────────────────
    # Greift erst hier: die deterministischen Fast-Paths oben sind score-unabhängig.
    if top_score < LOW_SCORE_NOMODEL:
        logger.info("AgentLocal: Score %.1f zu schwach — 'nicht gefunden'", top_score)
        return {
            "type": "answer",
            "answer": "Dazu konnte ich im Manual keine eindeutige Seite finden. "
                      "Bitte präzisieren Sie die Frage oder schlagen Sie in den Quellen nach.",
            "sources": [{"filename": r["filename"], "title": r["title"]} for r in filtered[:3]],
            "rounds": 0, "confidence": conf,
        }

    # ── Nicht-Tabellenfrage ───────────────────────────────────────────────────
    if effective_mode == "tools":
        # Experimentell: Modell darf mit Tools nachfassen (Halluzinationsrisiko).
        _row, _col = _extract_row_col(raw_query, context)
        has_cell = bool(_row and _col)
        logger.info("AgentLocal[tools]: Seed %d Kandidaten (Score %.1f, cell=%s)",
                    len(candidates), top_score, has_cell)
        answer, srcs, rounds = _run_tool_loop(question, context, candidates, include_table=has_cell)
        answer = _ensure_german(_clean_answer_text(answer))
        if not srcs:
            srcs = [{"filename": r["filename"], "title": r["title"]} for r in filtered[:3]]
        return {"type": "answer", "answer": answer, "sources": srcs[:3],
                "rounds": rounds, "confidence": conf}

    if effective_mode == "pipeline":
        # Experimentell: eine Modell-Synthese aus der Top-Seite (+ Eskalation).
        top = filtered[0]
        answer = _synthesize(top, context, question)
        rounds = 1
        used = list(filtered[:3])
        logger.info("AgentLocal[pipeline]: Synthese (Score %.1f) → %s", top_score, answer[:80])
        if _needs_escalation(answer, question, top_score):
            seen = {r["filename"] for r in filtered}
            esc = _escalate(question, context, seen)
            if esc:
                logger.info("AgentLocal[pipeline]: Eskalation → %s", esc[0]["title"][:60])
                answer2 = _synthesize(esc[0], context, question)
                rounds = 2
                used = esc[:1] + used
                if not _WEAK_ANSWER_RE.search(answer2):
                    answer = answer2
        if _WEAK_ANSWER_RE.search(answer):
            answer = ("Diese Information ließ sich auf den gefundenen Seiten nicht eindeutig "
                      "belegen. Bitte schlagen Sie in den verlinkten Quellen nach.")
        return {"type": "answer", "answer": answer,
                "sources": [{"filename": r["filename"], "title": r["title"]} for r in used[:3]],
                "rounds": rounds, "confidence": conf}

    # ── DEFAULT "sources": Quellen + wörtlicher Snippet, KEIN LLM-Fließtext ────
    # Anti-Halluzination: die richtigen Seiten zeigen, der Nutzer liest selbst.
    # Kein Modell-Call → keine erfundenen Werte, kein Englisch, keine Frage-Echos.
    # Volle Kandidatenliste übergeben, damit bis zu 5 Treffer oberhalb des Gaps
    # gezeigt werden (die passende Seite fällt sonst leicht aus den Top-3).
    return _sources_answer(candidates, question, conf)
