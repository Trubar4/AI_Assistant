"""
fastpaths.py — Deterministische, halluzinationsfreie Fast-Paths (backend-neutral).

Diese Extraktion aus agent_local.py macht die deterministische Kern-Logik für
BEIDE Modus-3-Agenten nutzbar (lokal *und* Claude): Bevor ein LLM überhaupt
formuliert, wird geprüft, ob die Frage exakt aus Tabellen bzw. der OCR-
Zusammenstellung (data/compositions.json) beantwortbar ist. Ist sie das, kommt
die Antwort deterministisch mit Quelle — kein Modell, keine Halluzination.

Leitidee (unverändert): exakte Extraktion mit Quelle ist KEINE Halluzination;
das kleine/große Modell wird nur dort eingesetzt, wo es nicht raten kann.

Fast-Paths (Reihenfolge in run_fastpaths):
  1. Composition-Zählung      "wie viele 12 m Zwischenstücke bei 74 m" → Anzahl
  2. Composition-Anordnung    "Reihenfolge/Aufbau …"                  → Segmentfolge
  3. Seilführungs-Position    "wo Seilführung einbauen …"             → S/N-Marker
  4. Tabellenwert             "Lasthaken-Gewicht 75 m / 5-fach"       → Zellwert

Alle Funktionen liefern das einheitliche Agent-Antwort-Dict
  {"type": "answer", "answer": str, "sources": [{filename,title}], "rounds": int,
   "confidence": float}
oder None (kein Fast-Path zuständig → normaler Agenten-Ablauf).
"""

import logging
import re
from collections import Counter

from backend.agent_tools import (
    lookup_table, composition_count, composition_arrangement, composition_seilfuehrung,
)
from backend.search import search
from backend.rule_agent import _normalize_query, _STOPWORDS

logger = logging.getLogger(__name__)

# ── Muster (verbatim aus agent_local.py) ────────────────────────────────────

# Frage zielt auf einen exakten Wert (Zahl/Einheit) ab. Bewusst OHNE führende
# Wortgrenze, damit "gewicht" auch in Komposita greift (Mindestgewicht,
# Hakengewicht, Eigengewicht) — sonst schlägt der Tabellen-Fast-Path fehl, wenn
# die Frage nur "Welches Lasthaken-Mindestgewicht?" lautet (Werte im Kontext).
_VALUE_QUESTION_RE = re.compile(
    r"(traglast|tragf|gewicht|länge|laenge|meter|\bm\b|tonnen|\bt\b|wert|"
    r"teilenummer|nummer|winkel|druck|\bbar\b|einscherung|wieviel|wie viel|wie lang|wie schwer)",
    re.I,
)

# Konfig-Werte aus dem Kontext: "74 m", "6x", "124 t", "6-fach"
_CONFIG_TOKEN_RE = re.compile(
    r"\d+\s*(?:m|t|x|fach|kg|bar|°)\b|\d+\s*[-]\s*fach|\b\d+x\b",
    re.I,
)

_COUNT_Q_RE = re.compile(r"\bwie\s*viele?\b|\banzahl\b|\bwieviele?\b", re.I)
_ARRANGE_Q_RE = re.compile(r"anordnung|reihenfolge|aufbau|zusammenges|zusammenstell|"
                           r"aus welchen|welche zwischenst|zusammensetz", re.I)
_SEIL_Q_RE = re.compile(r"seilf[üu]hrung", re.I)

# Zu generische Substantive: taugen NICHT als Relevanzbeleg (stehen in halbem Manual)
_GENERIC_TOKENS = {"hauptausleger", "nadelausleger", "ausleger", "maschine",
                   "konfiguration", "manual", "seite", "tabelle", "wert", "werte"}


# ── Helfer (verbatim aus agent_local.py) ────────────────────────────────────

def _config_tokens(context: str) -> list[str]:
    """Extrahiert Konfig-Werte (74 m, 6x, 124 t …) aus dem Kontext."""
    toks = [re.sub(r"\s+", " ", m.group(0)).strip() for m in _CONFIG_TOKEN_RE.finditer(context)]
    seen: set[str] = set()
    out = []
    for t in toks:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def _resolve_boom(question: str, context: str) -> str:
    """Auslegertyp bestimmen — die FRAGE hat Vorrang. Nennt die Frage einen
    Ausleger („Hauptausleger"/„Nadelausleger"), entscheidet er; nur wenn die Frage
    keinen nennt, zählt der Kontext. Verhindert, dass ein irrelevantes Konfig-Feld
    (z. B. ein aktiver Nadelausleger) den Fast-Path einer Hauptausleger-Frage kippt."""
    if re.search(r"nadelausleger", question, re.I):
        return "nadelausleger"
    if re.search(r"hauptausleger", question, re.I):
        return "hauptausleger"
    # Frage nennt keinen Ausleger → Kontext. Existiert ein Hauptausleger-Feld, ist
    # der Hauptausleger der plausible Default (Hauptstruktur); ein Nadelausleger
    # wird in Fragen praktisch immer explizit genannt. Nur wenn ausschließlich ein
    # Nadelausleger konfiguriert ist, greift dieser.
    keys = " ".join(k for k, _ in _split_context_fields(context)).lower()
    if "haupt" in keys:
        return "hauptausleger"
    if "nadel" in keys or re.search(r"nadelausleger", context or "", re.I):
        return "nadelausleger"
    return "hauptausleger"


def _boom_field_length(boom: str, context: str) -> int | None:
    """Auslegerlänge aus dem zum boom PASSENDEN Konfig-Feld (Hauptausleger/
    Nadelausleger), statt „erste Meterangabe im Kontext". Fällt auf die erste
    Kontext-Länge zurück, wenn kein passendes Feld existiert."""
    needle = "nadel" if boom == "nadelausleger" else "haupt"
    for k, v in _split_context_fields(context):
        if needle in k.lower():
            m = re.search(r"(\d+)\s*m\b", v)
            if m:
                return int(m.group(1))
    c_lens = [int(x) for x in re.findall(r"(\d+)\s*m\b", context or "")]
    return c_lens[0] if c_lens else None


def _length_for_boom(boom: str, question: str, context: str) -> int | None:
    """Länge für Anordnungs-/Seilführungs-Fragen: nennt die Frage selbst eine
    Länge, gewinnt sie; sonst das passende Konfig-Feld."""
    q_lens = [int(x) for x in re.findall(r"(\d+)\s*m\b", question)]
    if q_lens:
        return q_lens[0]
    return _boom_field_length(boom, context)


def _used_config_note(boom: str, length: int) -> str:
    """Transparenz: welche Konfiguration die deterministische Antwort getrieben hat."""
    return f" Verwendete Konfiguration: {boom.capitalize()} {length} m."


def _extract_row_col(question: str, context: str) -> tuple[str | None, str | None]:
    """Zeilen-/Spaltenwert für die Tabellensuche aus Frage+Kontext ableiten:
    Länge in Metern → Zeile, Einscherung (…-fach / …x) → Spalte."""
    text = f"{context} {question}"
    row = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m\b", text, re.I)
    if m:
        row = f"{m.group(1)} m"
    col = None
    m = re.search(r"(\d+)\s*-?\s*fach", text, re.I) or re.search(r"(\d+)\s*x\b", text, re.I)
    if m:
        col = m.group(1)
    return row, col


def _content_tokens(text: str) -> set[str]:
    """Sachwörter (≥5 Zeichen, keine Stoppwörter) einer Zeichenkette."""
    return {w for w in re.findall(r"[a-zäöüß]{5,}", text.lower()) if w not in _STOPWORDS}


def _topically_related(query: str, title: str) -> bool:
    """True, wenn Frage und Seitentitel ein aussagekräftiges Sachwort teilen
    (Substring-Match in beide Richtungen, damit Flexionsformen wie
    'Lasthaken'/'Lasthakens' greifen). Rein generisch, kein Themen-Hardcoding."""
    q_tokens = _content_tokens(query) - _GENERIC_TOKENS
    t_tokens = _content_tokens(title)
    for q in q_tokens:
        for t in t_tokens:
            if q == t or (len(q) >= 6 and q in t) or (len(t) >= 6 and t in q):
                return True
    return False


def _clean_answer_text(t: str) -> str:
    """Entfernt Markdown-Ballast (Codefences, Blockquotes, **fett**) und Umbrüche."""
    if not t:
        return t
    t = re.sub(r"```.*?```", "", t, flags=re.S)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"\n{2,}", " ", t).strip()
    return t


# ── Tabellenwert-Fast-Path ──────────────────────────────────────────────────

def _prelookup_table(intent_text: str, context: str, candidates: list[dict],
                     search_query: str | None = None) -> tuple[dict | None, dict | None]:
    """Wenn die Frage nach einem Wert klingt und eine Länge/Einscherung vorkommt,
    deterministisch die passende Tabelle suchen. Die richtige Tabellenseite ist
    oft NICHT der Top-Treffer der (durch die Konfig verwässerten) Hauptsuche,
    darum breiter Pool: Fusion-Kandidaten + eine gezielte, nach Relevanz
    gerankte Suche über die reine Sach-Frage (search_query, ohne Konfig-Ballast).
    lookup_table ist präzise (Zeile im Zahlbereich UND Spalte muss existieren),
    Fehltreffer sind damit unwahrscheinlich. Exakte Treffer schlagen gerundete."""
    if not _VALUE_QUESTION_RE.search(intent_text):
        return None, None
    row, col = _extract_row_col(intent_text, context)
    # Präzisions-Gate 1: nur echter Zellen-Zugriff (Zeile UND Spalte). Ohne
    # Spaltenachse (z. B. "wie viele …") ist "irgendeine Tabelle mit dieser
    # Zeile" zu schwach → selbstbewusst-falsche Antworten. Dann lieber Loop.
    if not row or not col:
        return None, None

    rel_ref = f"{search_query or ''} {intent_text}"
    pool: list[tuple[str, str]] = [(c["filename"], c["title"]) for c in candidates[:20]]
    # Gezielte, gerankte Suche nach der Tabellenseite über die reine Sach-Frage.
    for r in search(search_query or intent_text, top_n=25):
        pool.append((r["filename"], r["title"]))

    best: tuple[dict, dict] | None = None
    seen: set[str] = set()
    for fn, title in pool:
        if fn in seen:
            continue
        seen.add(fn)
        # Präzisions-Gate 2: Seite muss thematisch zur Frage passen (gemeinsames
        # Sachwort) — verhindert Treffer auf einer fremden Tabelle mit passender Zeile.
        if not _topically_related(rel_ref, title):
            continue
        res = lookup_table(fn, row, col)
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


def table_fastpath(intent_text: str, context: str, candidates: list[dict],
                   search_query: str | None = None, filtered: list[dict] | None = None,
                   conf: float = 1.0) -> dict | None:
    """Deterministischer Tabellenwert (wörtlich aus der Tabelle + Quelle)."""
    if not candidates:
        return None
    pre, page = _prelookup_table(intent_text, context, candidates, search_query=search_query)
    if not (pre and page):
        return None
    logger.info("Fast-Path Tabelle: '%s' (%s)", page["title"][:50], pre["treffer"])
    answer = _clean_answer_text(_answer_from_table(pre))
    srcs = [{"filename": page["filename"], "title": page["title"]}]
    for r in (filtered or candidates)[:2]:
        if r["filename"] != page["filename"]:
            srcs.append({"filename": r["filename"], "title": r["title"]})
    return {"type": "answer", "answer": answer, "sources": srcs[:3],
            "rounds": 1, "confidence": conf}


# ── Composition-Fast-Paths (Zählung / Anordnung / Seilführung) ──────────────

def composition_fastpath(question: str, context: str, conf: float = 1.0) -> dict | None:
    """Deterministische Zählung aus der Auslegerzusammenstellung (OCR-Daten).
    Trigger: "wie viele … Zwischenstücke/Segmente" + Segmentlänge + Auslegerlänge.
    Kein Modell — Zählung aus data/compositions.json."""
    text = f"{question} {context}"
    if not _COUNT_Q_RE.search(question):
        return None
    if not re.search(r"zwischenstück|segment|bauteil", text, re.I):
        return None
    q_lens = [int(x) for x in re.findall(r"(\d+)\s*m\b", question)]
    boom = _resolve_boom(question, context)
    # Auslegerlänge bevorzugt aus dem passenden Konfig-Feld, sonst größte Länge in der Frage.
    length = _boom_field_length(boom, context)
    if length is None:
        length = max(q_lens) if q_lens else None
    segment = next((n for n in q_lens if n != length), None)
    if length is None or segment is None:
        return None
    res = composition_count(boom, length, segment)
    if "error" in res:
        if res.get("error") == "length_not_found":
            avail = ", ".join(f"{n} m" for n in res["available_lengths"])
            logger.info("Fast-Path Composition — Länge %d m nicht gelistet", length)
            return {
                "type": "answer",
                "answer": (f"Für {length} m gibt es in „{res['title']}“ keine Zeile. "
                           f"Verfügbare Auslegerlängen: {avail}."),
                "sources": [{"filename": res["filename"], "title": res["title"]}],
                "rounds": 0, "confidence": conf,
            }
        return None
    logger.info("Fast-Path Composition %d×%d m bei %d m", res["count"], segment, length)
    answer = (f"{res['count']} Zwischenstück(e) à {segment} m in der {length}-m-"
              f"Zusammenstellung ({boom.capitalize()}). "
              f"Segmente (ohne Anlenkstück/Kopf): "
              f"{', '.join(str(x) + ' m' for x in res['zwischenstuecke'])}."
              + _used_config_note(boom, length))
    return {
        "type": "answer", "answer": answer,
        "sources": [{"filename": res["filename"], "title": res["title"]}],
        "rounds": 0, "confidence": conf,
    }


def arrangement_fastpath(question: str, context: str, conf: float = 1.0) -> dict | None:
    """Deterministische Segment-Reihenfolge aus der Zusammenstellung.
    Trigger: "Anordnung/Reihenfolge/Aufbau …" + Zwischenstück/Ausleger + Länge."""
    text = f"{question} {context}"
    if not _ARRANGE_Q_RE.search(question):
        return None
    if not re.search(r"zwischenst|segment|ausleger|zusammenstell", text, re.I):
        return None
    boom = _resolve_boom(question, context)
    length = _length_for_boom(boom, question, context)
    if length is None:
        return None
    res = composition_arrangement(boom, length)
    if "error" in res:
        if res.get("error") == "length_not_found":
            avail = ", ".join(f"{n} m" for n in res["available_lengths"])
            return {"type": "answer",
                    "answer": (f"Für {length} m gibt es in „{res['title']}“ keine Zeile. "
                               f"Verfügbare Auslegerlängen: {avail}."),
                    "sources": [{"filename": res["filename"], "title": res["title"]}],
                    "rounds": 0, "confidence": conf}
        return None
    grp = ", ".join(f"{c}× {L} m" for L, c in sorted(Counter(res["zwischenstuecke"]).items()))
    logger.info("Fast-Path Anordnung %d m (%d Segmente)", length, len(res["segments"]))
    answer = (f"Zusammenstellung {length} m {boom.capitalize()} (von unten nach oben): "
              f"Anlenkstück {res['anlenkstueck']} m → {' → '.join(f'{x} m' for x in res['zwischenstuecke'])} "
              f"→ Kopf {res['kopf']} m. Zwischenstücke: {grp}."
              + _used_config_note(boom, length))
    return {"type": "answer", "answer": answer,
            "sources": [{"filename": res["filename"], "title": res["title"]}],
            "rounds": 0, "confidence": conf}


def seilfuehrung_fastpath(question: str, context: str, conf: float = 1.0) -> dict | None:
    """Deterministische Seilführungs-Position aus den S/N-Markern der
    Zusammenstellung. Trigger: "Seilführung" + "wo/Stelle/Position" + Auslegerlänge."""
    text = f"{question} {context}"
    if not _SEIL_Q_RE.search(text):
        return None
    if not re.search(r"\bwo\b|stelle|position|einbau|einbauen|welche", text, re.I):
        return None
    boom = _resolve_boom(question, context)
    length = _length_for_boom(boom, question, context)
    if length is None:
        return None
    res = composition_seilfuehrung(boom, length)
    if "error" in res:
        if res.get("error") == "length_not_found":
            avail = ", ".join(f"{n} m" for n in res["available_lengths"])
            return {"type": "answer",
                    "answer": (f"Für {length} m gibt es in „{res['title']}“ keine Zeile. "
                               f"Verfügbare Auslegerlängen: {avail}."),
                    "sources": [{"filename": res["filename"], "title": res["title"]}],
                    "rounds": 0, "confidence": conf}
        return None
    parts = []
    for p in res["positions"]:
        cfg = "Auslegerkonfiguration 1/3" if p["marker"] == "S" else "Auslegerkonfiguration 4"
        parts.append(f"{cfg}: am {p['segment_index']}. Segment von {res['n_segments']} "
                     f"(ein {p['segment_m']}-m-Zwischenstück, Markierung „{p['marker']}“)")
    logger.info("Fast-Path Seilführung %d m → %d Position(en)", length, len(parts))
    answer = (f"Einbauposition der Seilführung bei {length} m {boom.capitalize()} — "
              + "; ".join(parts) + ". Genaue Lage siehe Grafik auf der Quellseite."
              + _used_config_note(boom, length))
    return {"type": "answer", "answer": answer,
            "sources": [{"filename": res["filename"], "title": res["title"]}],
            "rounds": 0, "confidence": conf}


# ── Per-Frage-Relevanz der Konfig ───────────────────────────────────────────

def _split_context_fields(context: str) -> list[tuple[str, str]]:
    """Zerlegt den kanonischen Kontext ("Schlüssel: Wert / Schlüssel: Wert") in
    (Schlüssel, Wert)-Paare. Teile ohne ": " werden als ("", Wert) geführt."""
    fields: list[tuple[str, str]] = []
    for part in re.split(r"\s*/\s*", context or ""):
        part = part.strip()
        if not part:
            continue
        if ": " in part:
            k, v = part.split(": ", 1)
            fields.append((k.strip(), v.strip()))
        else:
            fields.append(("", part))
    return fields


def relevant_context(question: str, context: str) -> str:
    """Reduziert den Konfig-Kontext auf die zur Frage passenden Felder — nur fürs
    Retrieval (Suche/HyDE/Rerank), NICHT für die Fast-Paths (die lesen den vollen
    Kontext weiter). Generisch, kein Themen-Hardcoding:

    Ein Feld bleibt, wenn
      (a) es ein aussagekräftiges Sachwort mit der Frage teilt (topische Überlappung), ODER
      (b) die Frage nach einem Wert/einer Menge fragt UND das Feld numerisch ist
          (Länge/Einscherung/Gewicht sind die Dimensionen der Traglast-/Tabellenfragen).

    So wird z. B. bei „Auf welcher Bildschirmseite konfiguriere ich den Lastort?"
    der irrelevante Kontext „Hauptausleger 74 m" NICHT injiziert (verdrängt sonst
    die richtige „Windenkonfiguration"-Seite)."""
    context = (context or "").strip()
    if not context:
        return ""
    fields = _split_context_fields(context)
    if not fields:
        return context
    q_tokens = _content_tokens(question) - _GENERIC_TOKENS
    q_value = bool(_VALUE_QUESTION_RE.search(question) or _COUNT_Q_RE.search(question))
    kept: list[str] = []
    for k, v in fields:
        f_tokens = _content_tokens(f"{k} {v}") - _GENERIC_TOKENS
        overlap = any(
            a == b or (len(a) >= 6 and a in b) or (len(b) >= 6 and b in a)
            for a in q_tokens for b in f_tokens
        )
        numeric = bool(re.search(r"\d", v))
        if overlap or (q_value and numeric):
            kept.append(f"{k}: {v}" if k else v)
    return " / ".join(kept)


# ── Retrieval-Helfer (für Agenten ohne eigenes Vorab-Retrieval) ─────────────

def retrieve_fusion(raw_query: str, context: str = "", top_n: int = 15) -> list[dict]:
    """Deterministische Fusions-Suche (roh + normalisiert + Kontext), wie im
    lokalen Agenten. Liefert dem Tabellen-Fast-Path des Claude-Agenten die
    Kandidatenliste, die er selbst (Tool-Loop) sonst nicht deterministisch hätte."""
    normalized = _normalize_query(raw_query)
    lists = [search(raw_query, top_n=top_n)]
    if normalized and normalized.lower() != raw_query.lower():
        lists.append(search(normalized, top_n=top_n))
    if context:
        lists.append(search(f"{context} {normalized}", top_n=top_n))
    by_fname: dict[str, dict] = {}
    for lst in lists:
        for c in lst:
            fn = c["filename"]
            prev = by_fname.get(fn)
            if prev is None or c.get("score", 0) > prev.get("score", 0):
                by_fname[fn] = c
    return sorted(by_fname.values(), key=lambda c: c.get("score", 0), reverse=True)[:top_n]


# ── Orchestrator ────────────────────────────────────────────────────────────

def run_fastpaths(question: str, context: str = "", candidates: list[dict] | None = None,
                  search_query: str | None = None, filtered: list[dict] | None = None,
                  conf: float = 1.0, table_intent: str | None = None) -> dict | None:
    """Führt alle deterministischen Fast-Paths in fester Reihenfolge aus und gibt
    die erste zuständige Antwort zurück, sonst None.

    Composition/Anordnung/Seilführung brauchen NUR Frage+Kontext (treffen direkt
    data/compositions.json) — daher auch ohne Retrieval nutzbar. Der Tabellenwert-
    Fast-Path braucht Kandidaten (candidates); fehlen sie, wird er übersprungen.

    table_intent: Text, auf dem der Tabellen-Fast-Path Zeile/Spalte erkennt
    (Default = question). Der lokale Agent reicht hier die zusammengesetzte
    raw_query (inkl. Rückfrage-Antwort) durch, exakt wie bisher.
    """
    for fp in (composition_fastpath, arrangement_fastpath, seilfuehrung_fastpath):
        hit = fp(question, context, conf)
        if hit is not None:
            return hit
    if candidates:
        return table_fastpath(table_intent or question, context, candidates,
                              search_query=search_query or question,
                              filtered=filtered, conf=conf)
    return None
