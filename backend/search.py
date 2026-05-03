"""
search.py

Retrieval layer: given a German user question, returns the top-N most
relevant manual topics by combining fuzzy title matching, keyword
frequency in body text, and a semantic phase boost.

Usage (standalone test):
    python backend/search.py "Getriebeölstand prüfen"
    python backend/search.py "Fehler Hydraulikdruck"
"""

import json
import re
import sys
from pathlib import Path

from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Paths (relative to repo root; overridable for tests)
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
METADATA_INDEX = _ROOT / "data" / "metadata_index.json"
CONTENT_INDEX  = _ROOT / "data" / "content_index.json"
TOC_INDEX      = _ROOT / "data" / "toc_index.json"

# ---------------------------------------------------------------------------
# Keyword sets for semantic phase boosting
# ---------------------------------------------------------------------------
_FAULT_KW = {
    "fehler", "störung", "defekt", "alarm", "warnung", "meldung",
    "ausfall", "problem", "error", "fault", "diagnose",
}
_MAINTENANCE_KW = {
    "wartung", "inspektion", "service", "öl", "filter", "wechsel",
    "prüfen", "reinigen", "schmieren", "intervall",
}
_ASSEMBLY_KW = {
    "aufrüsten", "aufbau", "montage", "demontage", "transport",
    "einrichten", "aufstellen",
}


def _phase_boost(query_words: set[str], lifecycle_phases: list[str], topic_type: str) -> float:
    phases = set(lifecycle_phases)
    if query_words & _FAULT_KW:
        if phases & {"Fault", "Diagnostics"} or topic_type == "GenericTroubleshooting":
            return 25.0
    if query_words & _MAINTENANCE_KW:
        if "Maintenance" in phases:
            return 15.0
    if query_words & _ASSEMBLY_KW:
        if phases & {"Assembly", "GenericPuttingToUse"}:
            return 15.0
    return 0.0


def _keyword_score(query_words: set[str], text: str, word_count: int) -> float:
    if word_count == 0:
        return 0.0
    text_lower = text.lower()
    hits = sum(text_lower.count(w) for w in query_words if len(w) > 3)
    # Normalise: cap at 20 hits to avoid huge maintenance-table pages dominating
    return min(hits, 20) / 20 * 40.0


def _title_score(query: str, title: str) -> float:
    return fuzz.partial_ratio(query.lower(), title.lower())


def _tokenize(query: str) -> set[str]:
    return {t.lower() for t in re.split(r"\W+", query) if t}


# ---------------------------------------------------------------------------
# Index loading (lazy, cached at module level)
# ---------------------------------------------------------------------------
_index: list[dict] | None = None


def _load_index(
    metadata_path: Path = METADATA_INDEX,
    content_path: Path  = CONTENT_INDEX,
) -> list[dict]:
    global _index
    if _index is not None:
        return _index

    meta    = json.loads(metadata_path.read_text(encoding="utf-8"))
    content = json.loads(content_path.read_text(encoding="utf-8"))

    # TOC index is optional — provides authoritative breadcrumbs when present
    toc_by_file: dict = {}
    toc_path = TOC_INDEX
    if toc_path.exists():
        for entry in json.loads(toc_path.read_text(encoding="utf-8")):
            toc_by_file[entry["filename"]] = entry

    merged = []
    for filename, m in meta.items():
        c = content.get(filename)
        if c is None:
            continue
        toc = toc_by_file.get(filename, {})
        # TOC breadcrumb is authoritative; fall back to HTML-extracted one
        breadcrumb = toc.get("breadcrumb") or c.get("breadcrumb", [])
        merged.append({
            "filename":        filename,
            "title":           m.get("title") or c.get("title", ""),
            "topic_type":      m.get("topic_type", ""),
            "lifecycle_phases": m.get("lifecycle_phases", []),
            "breadcrumb":      breadcrumb,
            "depth":           toc.get("depth", 0),
            "text":            c.get("text", ""),
            "warnings":        c.get("warnings", []),
            "steps":           c.get("steps", []),
            "word_count":      c.get("word_count", 0),
        })

    _index = merged
    return _index


def reset_index() -> None:
    global _index
    _index = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search(
    query: str,
    top_n: int = 5,
    metadata_path: Path = METADATA_INDEX,
    content_path: Path  = CONTENT_INDEX,
) -> list[dict]:
    """
    Return the top_n most relevant index entries for query.

    Each result dict contains: filename, title, breadcrumb, text,
    warnings, steps, topic_type, lifecycle_phases, score.
    """
    if not query.strip():
        return []

    index = _load_index(metadata_path, content_path)
    query_words = _tokenize(query)

    scored = []
    for entry in index:
        ts = _title_score(query, entry["title"])
        ks = _keyword_score(query_words, entry["text"], entry["word_count"])
        pb = _phase_boost(query_words, entry["lifecycle_phases"], entry["topic_type"])
        # Title match dominates; keyword and phase scores break ties
        score = ts * 0.55 + ks * 0.30 + pb * 0.15
        if score > 10:
            scored.append({**entry, "score": round(score, 2)})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

def _print_result(r: dict, rank: int) -> None:
    bc = " › ".join(r["breadcrumb"]) if r["breadcrumb"] else "—"
    print(f"\n#{rank}  [{r['score']}]  {r['title']}")
    print(f"     Breadcrumb : {bc}")
    print(f"     Type       : {r['topic_type']}  |  Phases: {', '.join(r['lifecycle_phases']) or '—'}")
    print(f"     Warnings   : {len(r['warnings'])}  |  Steps: {len(r['steps'])}")
    print(f"     Text[0:120]: {r['text'][:120]}")


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Getriebeölstand prüfen"
    print(f'Query: "{query}"')
    results = search(query)
    if not results:
        print("No results.")
    else:
        for i, r in enumerate(results, 1):
            _print_result(r, i)
    print()
