"""
search.py

Retrieval layer: Hybrid BM25 + Semantic Search mit Reciprocal Rank Fusion (RRF).

BM25 deckt exakte Keyword-Matches ab (z. B. "Leckage" → Umwelt-Seite).
Semantic Search deckt Synonyme und Umschreibungen ab (z. B. "tropft" → Leckage).
RRF kombiniert beide Rankings skalenfrei.

Usage (standalone test):
    python backend/search.py "Getriebeölstand prüfen"
    python backend/search.py "Leckage"
    python backend/search.py "Was tun wenn es tropft?"
"""

import json
import logging
import re
import sys
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
METADATA_INDEX = _ROOT / "data" / "metadata_index.json"
CONTENT_INDEX  = _ROOT / "data" / "content_index.json"
TOC_INDEX      = _ROOT / "data" / "toc_index.json"

_EMB_NPY = _ROOT / "data" / "embeddings.npy"
_EMB_IDS = _ROOT / "data" / "embedding_ids.json"

# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------
_STOPWORDS_DE = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem",
    "eines", "und", "oder", "aber", "wenn", "dann", "also", "weil", "dass",
    "sich", "ist", "sind", "war", "wird", "werden", "hat", "haben", "sein",
    "tun", "was", "wie", "wer", "man", "kann", "muss", "soll", "darf",
    "nicht", "kein", "keine", "bei", "mit", "von", "aus", "nach", "vor",
    "über", "unter", "durch", "für", "ohne", "gegen", "bis", "seit",
    "noch", "auch", "schon", "nur", "sehr", "mehr", "alle", "hier",
}


def _tokenize(text: str) -> list[str]:
    tokens = re.split(r"\W+", text.lower())
    return [t for t in tokens if len(t) > 2 and t not in _STOPWORDS_DE]


# ---------------------------------------------------------------------------
# Index loading (lazy, cached)
# ---------------------------------------------------------------------------
_index: list[dict] | None = None
_bm25: BM25Okapi | None = None           # full-text BM25 (title×3 + body)
_bm25_title: BM25Okapi | None = None     # title-only BM25
_bm25_filenames: list[str] | None = None


def _load_index(
    metadata_path: Path = METADATA_INDEX,
    content_path: Path = CONTENT_INDEX,
) -> list[dict]:
    global _index, _bm25, _bm25_title, _bm25_filenames
    if _index is not None and _bm25_title is not None:
        return _index

    meta    = json.loads(metadata_path.read_text(encoding="utf-8"))
    content = json.loads(content_path.read_text(encoding="utf-8"))

    toc_by_file: dict = {}
    if TOC_INDEX.exists():
        for entry in json.loads(TOC_INDEX.read_text(encoding="utf-8")):
            toc_by_file[entry["filename"]] = entry

    merged = []
    for filename, m in meta.items():
        c = content.get(filename)
        if c is None:
            continue
        toc = toc_by_file.get(filename, {})
        breadcrumb = toc.get("breadcrumb") or c.get("breadcrumb", [])
        merged.append({
            "filename":         filename,
            "title":            m.get("title") or c.get("title", ""),
            "topic_type":       m.get("topic_type", ""),
            "lifecycle_phases": m.get("lifecycle_phases", []),
            "breadcrumb":       breadcrumb,
            "depth":            toc.get("depth", 0),
            "text":             c.get("text", ""),
            "warnings":         c.get("warnings", []),
            "steps":            c.get("steps", []),
            "word_count":       c.get("word_count", 0),
        })

    _index = merged

    corpus_full  = []
    corpus_title = []
    _bm25_filenames = []
    for entry in merged:
        title_tokens = _tokenize(entry["title"])
        warn_tokens  = _tokenize(" ".join(entry["warnings"]))
        step_tokens  = _tokenize(" ".join(entry["steps"][:15]))
        body_tokens  = _tokenize(entry["text"])
        corpus_full.append(title_tokens * 3 + warn_tokens + step_tokens + body_tokens)
        corpus_title.append(title_tokens)
        _bm25_filenames.append(entry["filename"])

    _bm25       = BM25Okapi(corpus_full)
    _bm25_title = BM25Okapi(corpus_title)
    logger.info("BM25 indices gebaut: %d Dokumente (full + title)", len(merged))
    return _index


def reset_index() -> None:
    global _index, _bm25, _bm25_title, _bm25_filenames, _sem_model, _sem_matrix, _sem_ids
    _index = _bm25 = _bm25_title = _bm25_filenames = None
    _sem_model = _sem_matrix = _sem_ids = None


# ---------------------------------------------------------------------------
# Semantic search (optional — only active when embeddings are pre-built)
# ---------------------------------------------------------------------------
_sem_model  = None
_sem_matrix = None
_sem_ids    = None


def _load_semantic() -> bool:
    global _sem_model, _sem_matrix, _sem_ids
    if _sem_matrix is not None:
        return True
    if not (_EMB_NPY.exists() and _EMB_IDS.exists()):
        return False
    try:
        from sentence_transformers import SentenceTransformer
        _sem_matrix = np.load(str(_EMB_NPY))
        _sem_ids    = json.loads(_EMB_IDS.read_text(encoding="utf-8"))
        _sem_model  = SentenceTransformer("paraphrase-multilingual-mpnet-base-v2")
        logger.info("Semantic search geladen: %d Dokumente", len(_sem_ids))
        return True
    except Exception as exc:
        logger.warning("Semantic search nicht verfügbar: %s", exc)
        return False


def _semantic_ranking(query: str, top_k: int = 50) -> list[tuple[str, float]]:
    """Returns [(filename, similarity), ...] sorted descending."""
    if not _load_semantic():
        return []
    q_vec = _sem_model.encode([query], normalize_embeddings=True)[0]
    sims  = (_sem_matrix @ q_vec).tolist()
    max_sim = max(sims) if sims else 0.0
    logger.info("Semantic max_sim=%.3f query='%s'", max_sim, query[:50])
    ranked = sorted(zip(_sem_ids, sims), key=lambda x: x[1], reverse=True)
    return ranked[:top_k]


# ---------------------------------------------------------------------------
# BM25 ranking
# ---------------------------------------------------------------------------

def _bm25_ranking(query: str, top_k: int = 50) -> list[tuple[str, float]]:
    """Full-text BM25 ranking (title×3 + body)."""
    _load_index()
    tokens = _tokenize(query)
    if not tokens:
        return []
    scores = _bm25.get_scores(tokens)
    ranked = sorted(zip(_bm25_filenames, scores), key=lambda x: x[1], reverse=True)
    return [r for r in ranked[:top_k] if r[1] > 0]


def _bm25_title_ranking(query: str, top_k: int = 50) -> list[tuple[str, float]]:
    """Title-only BM25 ranking — high precision, surfaces exact title matches."""
    _load_index()
    tokens = _tokenize(query)
    if not tokens:
        return []
    scores = _bm25_title.get_scores(tokens)
    ranked = sorted(zip(_bm25_filenames, scores), key=lambda x: x[1], reverse=True)
    return [r for r in ranked[:top_k] if r[1] > 0]


def bm25_candidate_titles(query: str, top_k: int = 25) -> list[str]:
    """Return up to top_k unique page titles ranked by BM25 score.

    Used by HyDE to give the LLM vocabulary hints from the actual manual index.
    """
    index = _load_index()
    index_by_filename = {e["filename"]: e for e in index}
    ranking = _bm25_ranking(query, top_k=top_k * 2)  # fetch extra to survive dedup
    seen: set[str] = set()
    titles: list[str] = []
    for fname, _ in ranking:
        entry = index_by_filename.get(fname)
        if entry is None:
            continue
        t = entry["title"].strip()
        if t and t not in seen:
            seen.add(t)
            titles.append(t)
        if len(titles) == top_k:
            break
    return titles


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------
_RRF_K = 60


def _rrf(rankings: list[list[tuple[str, float]]]) -> dict[str, float]:
    """Combine ranked lists via RRF. Each list is [(filename, score), ...]."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, (fname, _) in enumerate(ranking):
            scores[fname] = scores.get(fname, 0.0) + 1.0 / (_RRF_K + rank)
    return scores


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search(
    query: str,
    top_n: int = 20,
    metadata_path: Path = METADATA_INDEX,
    content_path: Path = CONTENT_INDEX,
) -> list[dict]:
    """
    Return top_n candidates via Triple-RRF:
      1. Title-only BM25   — high precision for title matches
      2. Full-text BM25    — broad keyword recall
      3. Semantic search   — synonym / paraphrase matching

    top_n defaults to 20 so the LLM reranker in main.py can select the best 5.
    """
    if not query.strip():
        return []

    index = _load_index(metadata_path, content_path)
    index_by_filename = {e["filename"]: e for e in index}

    title_rank = _bm25_title_ranking(query, top_k=50)
    full_rank  = _bm25_ranking(query, top_k=50)
    sem_rank   = _semantic_ranking(query, top_k=50)

    use_sem  = bool(sem_rank)
    rankings = [r for r in [title_rank, full_rank, sem_rank] if r]

    rrf_scores = _rrf(rankings)
    if not rrf_scores:
        return []

    sorted_filenames = sorted(rrf_scores, key=rrf_scores.get, reverse=True)

    logger.info(
        "TOP-10 für '%s' (title=%d full=%d sem=%s): %s",
        query[:50],
        len(title_rank),
        len(full_rank),
        use_sem,
        " | ".join(
            f"{index_by_filename[f]['title'][:18]}={rrf_scores[f]:.4f}"
            for f in sorted_filenames[:10]
            if f in index_by_filename
        ),
    )

    # Deduplizieren: pro Titel nur den höchsten RRF-Score behalten
    seen_titles: set[str] = set()
    results = []
    for fname in sorted_filenames:
        entry = index_by_filename.get(fname)
        if entry is None:
            continue
        t = entry["title"].strip().lower()
        if t not in seen_titles:
            seen_titles.add(t)
            results.append({**entry, "score": round(rrf_scores[fname] * 1000, 2)})
        if len(results) == top_n:
            break

    return results


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
    logging.basicConfig(level=logging.INFO)
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Getriebeölstand prüfen"
    print(f'Query: "{query}"')
    results = search(query)
    if not results:
        print("No results.")
    else:
        for i, r in enumerate(results, 1):
            _print_result(r, i)
    print()
