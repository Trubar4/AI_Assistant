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
    _STOPWORDS,
)
from backend.search import search, _load_index
from backend.agent_tools import (
    read_page, grep_manual, bal_search, lookup_table,
    TOOL_SCHEMAS, TOOL_FN, LOOKUP_TABLE_SCHEMA,
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

# pipeline = deterministische Pipeline + 1 Synthese (robust, wenig Modell-Last)
# tools    = Seeded Tool-Loop: Retrieval vorgeben, dann darf das Modell mit
#            read_page/grep_manual/bal_search/search über mehrere Runden nachfassen
AGENT_LOCAL_MODE   = os.environ.get("AGENT_LOCAL_MODE", "tools").strip().lower()

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


def _oai_tools() -> list[dict]:
    """Übersetzt die lokalen Tool-Schemas ins OpenAI-Function-Format."""
    return [
        {"type": "function", "function": {
            "name": s["name"],
            "description": s["description"],
            "parameters": s["input_schema"],
        }}
        for s in _LOCAL_TOOL_SCHEMAS
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


def _extract_row_col(question: str, context: str) -> tuple[str | None, str | None]:
    """Zeilen-/Spaltenwert für die Tabellensuche aus Frage+Kontext ableiten:
    Länge in Metern → Zeile, Einscherung (…-fach / …x) → Spalte."""
    text = f"{context} {question}"
    row = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\b", text, re.I)
    if m:
        row = f"{m.group(1)} m"
    col = None
    # "6-fach", "6-facher", "6fach", "6-fache Einscherung" oder "6x"
    m = re.search(r"(\d+)\s*-?\s*fach", text, re.I) or re.search(r"(\d+)\s*x\b", text, re.I)
    if m:
        col = m.group(1)
    return row, col


def _prelookup_table(question: str, context: str, candidates: list[dict]) -> tuple[dict | None, dict | None]:
    """Wenn die Frage nach einem Wert klingt und eine Länge/Einscherung vorkommt,
    deterministisch die passende Tabelle suchen. Die richtige Tabellenseite ist
    oft NICHT der Top-Treffer, darum breiter Pool: Fusion-Kandidaten + gezielte
    Titel-Suche. lookup_table ist präzise (Zeile im Zahlbereich UND Spalte muss
    existieren), Fehltreffer sind damit unwahrscheinlich. Exakte Treffer werden
    gerundeten vorgezogen."""
    if not _VALUE_QUESTION_RE.search(question):
        return None, None
    row, col = _extract_row_col(question, context)
    if not row:
        return None, None

    titles = _title_map()
    pool: list[tuple[str, str]] = [(c["filename"], c["title"]) for c in candidates[:12]]
    keys = [w for w in _normalize_query(question).split()
            if len(w) > 3 and w.lower() not in _STOPWORDS]
    if keys:
        for h in bal_search(" ".join(keys[:3])):
            fn = h.get("filename")
            if fn and "error" not in h:
                pool.append((fn, h.get("title", titles.get(fn, fn))))

    best: tuple[dict, dict] | None = None
    seen: set[str] = set()
    for fn, title in pool:
        if fn in seen:
            continue
        seen.add(fn)
        res = lookup_table(fn, row, col or "")
        if "error" in res or not res.get("zeilen"):
            continue
        hit = (res, {"filename": fn, "title": title})
        if res.get("treffer") == "exakt":
            return hit                       # exakter Zeilentreffer gewinnt sofort
        if best is None:
            best = hit
    return best if best else (None, None)


def _answer_from_table(pre: dict) -> str:
    """Baut die Antwort DETERMINISTISCH aus den extrahierten Tabellenzeilen —
    kein Modell, damit der vorgelegte Wert nicht ignoriert/halluziniert wird."""
    picks = pre.get("zeilen", [])
    row_q = pre["gesucht"]["zeile"]
    col_q = pre["gesucht"]["spalte"]
    exact = pre.get("treffer") == "exakt"
    if col_q:
        einsch = f"{col_q}-facher Einscherung"
        vals = [(z.get("row", ""), z.get("value", "—")) for z in picks]
        nonempty = [(r, v) for r, v in vals if v not in ("—", "", "-")]
        if not nonempty:
            rows = " und ".join(r for r, _ in vals) or row_q
            return (f"Für die Auslegerlänge {row_q} ist bei {einsch} kein Wert in der "
                    f"Tabelle eingetragen (geprüft: {rows}).")
        if exact and len(nonempty) == 1:
            r, v = nonempty[0]
            return f"Laut Tabelle: {v} (Auslegerlänge {r}, {einsch})."
        parts = "; ".join(f"{r} → {v}" for r, v in nonempty)
        return (f"Für {row_q} gibt es keinen exakten Tabellenwert; nächstgelegene "
                f"Zeilen bei {einsch}: {parts}.")
    parts = "; ".join(f"{z.get('row', '')}: {z.get('cells', '')}" for z in picks)
    return f"Tabellenwerte nahe {row_q}: {parts}."


def _clean_answer_text(t: str) -> str:
    """Entfernt Markdown-Ballast (Codefences, Blockquotes, **fett**) und Umbrüche."""
    if not t:
        return t
    t = re.sub(r"```.*?```", "", t, flags=re.S)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"\n{2,}", " ", t).strip()
    return t


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


def _run_tool_loop(question: str, context: str, candidates: list[dict]) -> tuple[str, list[dict], int]:
    """Seeded Tool-Loop. Gibt (Antworttext, gelesene Quellen, Runden) zurück."""
    tools = _oai_tools()
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
        if not answer:
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


def run_agent_local(question: str, context: str = "", conversation: list[dict] | None = None) -> dict:
    """Lokaler Modus-3-Agent.

    Gemeinsame modellfreie Vorstufe (Clarification, Fusion-Retrieval,
    Confidence-Gate), danach je nach AGENT_LOCAL_MODE:
      - "tools"    (Default): Seeded Tool-Loop — Modell fasst mit Tools nach.
      - "pipeline"          : deterministische Pipeline + max. 2 Modell-Calls.
    """
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

    # Seeded Tool-Loop: Retrieval vorgeben, dann darf das Modell nachfassen.
    if AGENT_LOCAL_MODE == "tools":
        conf = round(min(top_score / 40.0, 1.0), 2)

        # Fast-Path: Tabellenwert deterministisch → kein Loop, kein Abdriften.
        pre, page = _prelookup_table(question, context, candidates)
        if pre and page:
            logger.info("AgentLocal[tools]: Tabellen-Fast-Path '%s' (%s)",
                        page["title"][:50], pre["treffer"])
            answer = _clean_answer_text(_answer_from_table(pre))
            srcs = [{"filename": page["filename"], "title": page["title"]}]
            for r in filtered[:2]:
                if r["filename"] != page["filename"]:
                    srcs.append({"filename": r["filename"], "title": r["title"]})
            return {"type": "answer", "answer": answer, "sources": srcs[:3],
                    "rounds": 1, "confidence": conf}

        logger.info("AgentLocal[tools]: Seed %d Kandidaten (Top-Score %.1f)", len(candidates), top_score)
        answer, srcs, rounds = _run_tool_loop(question, context, candidates)
        answer = _ensure_german(_clean_answer_text(answer))
        if not srcs:
            srcs = [{"filename": r["filename"], "title": r["title"]} for r in filtered[:3]]
        return {"type": "answer", "answer": answer, "sources": srcs[:3],
                "rounds": rounds, "confidence": conf}

    # Phase 4+5: Kontext aufbereiten + EINE Synthese
    top = filtered[0]
    answer = _synthesize(top, context, question)
    rounds = 1
    used = list(filtered[:3])
    logger.info("AgentLocal[pipeline]: Synthese (Score %.1f) → %s", top_score, answer[:100])

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
