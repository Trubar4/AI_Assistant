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


_UNIT_RE = re.compile(r"^(\d+)(m|ft|t|kg|h|hz|kw|kn|bar|mm|cm|rpm)$")


def _tokenize(text: str) -> list[str]:
    """Tokenisiert Text für BM25-Index und Queries.

    Besonderheiten:
    - Zahl+Einheit wie "74m" oder "12t" wird als "74m" UND "74" emittiert,
      damit "74m" (Query) und "74 m" (Manual-Text) einander matchen.
    - Bindestriche innerhalb alphanumerischer Cluster bleiben erhalten
      ("6-fach", "Hauptausleger-Zwischenstück").
    """
    raw = re.findall(r"[a-zäöüß0-9]+(?:[-][a-zäöüß0-9]+)*", text.lower())
    result = []
    for t in raw:
        if len(t) <= 1 or t in _STOPWORDS_DE:
            continue
        result.append(t)
        m = _UNIT_RE.match(t)
        if m:
            result.append(m.group(1))   # "74m" → auch "74" emittieren
        if "-" in t:
            # "hauptausleger-kopf" → auch "hauptauslegerkopf" emittieren,
            # damit Queries ohne Bindestrich auf bindestrich-indizierte Begriffe treffen.
            result.append(t.replace("-", ""))
    return result


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
    _build_dynamic_title_facet_words(merged)
    return _index


def reset_index() -> None:
    global _index, _bm25, _bm25_title, _bm25_filenames, _sem_model, _sem_matrix, _sem_ids
    _index = _bm25 = _bm25_title = _bm25_filenames = None
    _sem_model = _sem_matrix = _sem_ids = None


# ---------------------------------------------------------------------------
# Semantic search — sentence-transformers (optional, requires model download)
# ---------------------------------------------------------------------------
_sem_model  = None
_sem_matrix = None
_sem_ids    = None


def _load_semantic() -> bool:
    global _sem_model, _sem_matrix, _sem_ids
    if _sem_matrix is not None and _sem_model is not None:
        return True
    if _sem_matrix is not None and _sem_model is None:
        return False  # matrix loaded but model unavailable
    if not (_EMB_NPY.exists() and _EMB_IDS.exists()):
        return False
    try:
        from sentence_transformers import SentenceTransformer
        _sem_matrix = np.load(str(_EMB_NPY))
        _sem_ids    = json.loads(_EMB_IDS.read_text(encoding="utf-8"))
        # Model must produce 768-dim embeddings matching embeddings.npy
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
# TF-IDF character n-gram ranking (fallback when sentence-transformer unavailable)
# Handles German compound words and morphological variants without any model download.
# ---------------------------------------------------------------------------
_tfidf_vectorizer = None
_tfidf_matrix     = None
_tfidf_filenames: list[str] = []


def _load_tfidf() -> bool:
    global _tfidf_vectorizer, _tfidf_matrix, _tfidf_filenames
    if _tfidf_matrix is not None:
        return True
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        index = _load_index()
        docs, fnames = [], []
        for e in index:
            text = e["title"] + " " + e["title"] + " " + e.get("text", "") + " " + " ".join(e.get("steps", []))
            docs.append(text.lower())
            fnames.append(e["filename"])
        # char n-grams (3-6): captures German compound sub-strings
        _tfidf_vectorizer = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(3, 6),
            min_df=2, max_df=0.95, sublinear_tf=True,
        )
        _tfidf_matrix   = _tfidf_vectorizer.fit_transform(docs)
        _tfidf_filenames = fnames
        logger.info("TF-IDF char-ngram index gebaut: %d Dokumente", len(fnames))
        return True
    except Exception as exc:
        logger.warning("TF-IDF nicht verfügbar: %s", exc)
        return False


def _tfidf_ranking(query: str, top_k: int = 50) -> list[tuple[str, float]]:
    """TF-IDF cosine similarity ranking as fallback for semantic search."""
    if not _load_tfidf():
        return []
    try:
        from sklearn.metrics.pairwise import cosine_similarity
        q_vec = _tfidf_vectorizer.transform([query.lower()])
        sims  = cosine_similarity(q_vec, _tfidf_matrix).flatten()
        ranked = sorted(zip(_tfidf_filenames, sims.tolist()), key=lambda x: x[1], reverse=True)
        return [(f, s) for f, s in ranked[:top_k] if s > 0]
    except Exception:
        return []


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


def count_hits(term: str) -> int:
    """How many index entries contain all BM25 tokens of the term (for context validation)."""
    _load_index()
    tokens = _tokenize(term)
    if not tokens:
        return 0
    scores = _bm25.get_scores(tokens)
    return int(sum(1 for s in scores if s > 0))


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
    # TF-IDF char-ngram as fallback when sentence-transformer model is unavailable
    tfidf_rank = _tfidf_ranking(query, top_k=50) if not sem_rank else []

    use_sem  = bool(sem_rank)
    rankings = [r for r in [title_rank, full_rank, sem_rank, tfidf_rank] if r]

    rrf_scores = _rrf(rankings)
    if not rrf_scores:
        return []

    sorted_filenames = sorted(rrf_scores, key=rrf_scores.get, reverse=True)

    logger.info(
        "TOP-10 für '%s' (title=%d full=%d sem=%s tfidf=%d): %s",
        query[:50],
        len(title_rank),
        len(full_rank),
        use_sem,
        len(tfidf_rank),
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
# Facet extraction (for "Suche verfeinern" UI)
# ---------------------------------------------------------------------------

# Seed-Whitelist: Dokumenttyp-Wörter die sicher als Facetten taugen.
# Wird beim ersten Index-Load mit Wörtern aus den echten Titeln erweitert.
_TITLE_FACET_WORDS_SEED = {
    "zusammenstellung", "übersicht",                        # Tabellen / Konfigurationsseiten
    "montage", "demontage", "einbau", "ausbau",            # Montage-Verfahren
    "inspektion", "prüfung", "überprüfung", "kontrolle",   # Prüfung / Wartung
    "einscherung", "ausscherung",                          # Seil-Operationen
    "schmierung", "wartung",                               # Wartung
    "inbetriebnahme", "außerbetriebnahme",
    "transport", "lagerung", "entsorgung",
    "sicherheitshinweise", "fehlersuche", "störung",
    "einstellung", "justierung", "kalibrierung",
    "parkposition", "zusammenbauen",                       # Auf-/Abbau
}

_TITLE_FACET_WORDS: set[str] = set(_TITLE_FACET_WORDS_SEED)

# Wörter die in fast allen Titeln vorkommen und keine Unterscheidung liefern
_TITLE_NOISE = _STOPWORDS_DE | {
    "des", "der", "die", "und", "mit", "für", "beim", "zur", "zum",
    "nach", "vor", "am", "im", "an", "auf", "in",
}


def _build_dynamic_title_facet_words(index: list[dict]) -> None:
    """Extend _TITLE_FACET_WORDS with words found in 2–15 % of all titles.

    Words in this frequency band are specific enough to be useful filters
    (not universal headings like "Hauptausleger") but common enough to produce
    multiple hits when selected as a facet.
    """
    global _TITLE_FACET_WORDS
    n = len(index)
    if n < 10:
        return
    word_counts: dict[str, int] = {}
    for entry in index:
        words = set(re.split(r"\W+", entry["title"].lower()))
        for w in words:
            if len(w) >= 5 and w not in _TITLE_NOISE:
                word_counts[w] = word_counts.get(w, 0) + 1
    low, high = max(2, int(n * 0.02)), int(n * 0.15)
    dynamic = {w for w, cnt in word_counts.items() if low <= cnt <= high}
    _TITLE_FACET_WORDS = _TITLE_FACET_WORDS_SEED | dynamic
    logger.info("Titel-Facet-Wörter: %d seed + %d dynamisch = %d gesamt",
                len(_TITLE_FACET_WORDS_SEED), len(dynamic - _TITLE_FACET_WORDS_SEED),
                len(_TITLE_FACET_WORDS))


def _title_facets(subset: list[dict], min_count: int = 2) -> list[str]:
    """Findet Titelwörter, die in manchen (nicht allen) Kandidaten-Titeln vorkommen.

    Bevorzugt bekannte Dokumenttyp-Wörter (Zusammenstellung, Montage, …),
    fällt auf allgemeine unterscheidende Nomen zurück wenn keine bekannten gefunden.
    """
    titles = [c.get("title", "") for c in subset]
    n = len(titles)
    if n < min_count:
        return []

    # Wortfrequenz über alle Titel
    word_counts: dict[str, int] = {}
    for title in titles:
        words = set(re.split(r"\W+", title.lower()))
        for w in words:
            if len(w) >= 4 and w not in _TITLE_NOISE:
                word_counts[w] = word_counts.get(w, 0) + 1

    # Wörter die in 2..n-1 Titeln vorkommen (nicht universal, nicht einmalig)
    distinguishing = {w for w, cnt in word_counts.items() if min_count <= cnt < n}

    # Nur bekannte Dokumenttypen — allgemeine Nomen sind zu unspezifisch
    known = sorted(w for w in distinguishing if w in _TITLE_FACET_WORDS)
    if not known:
        return []
    candidates_words = known[:6]

    # Originalschreibweise aus erstem Trefftitel wiederherstellen
    result = []
    for w in candidates_words:
        for title in titles:
            for token in re.split(r"\W+", title):
                if token.lower() == w:
                    result.append(token)
                    break
            else:
                continue
            break

    return result


def extract_facets(candidates: list[dict], top_k: int = 10, min_values: int = 2) -> list[dict]:
    """Derive refine-search checkbox options from two sources:

    1. Breadcrumb divergence — findet die flachste Tiefe, an der die Top-k
       Kandidaten auseinandergehen (z. B. Hauptausleger vs. Nadelausleger).
    2. Titelwort-Facetten — Dokumenttyp-Wörter (Zusammenstellung, Montage, …)
       die in manchen aber nicht allen Titeln vorkommen.

    Gibt bis zu zwei Facetten-Gruppen zurück.
    """
    subset = candidates[:top_k]
    facets = []

    # 1. Breadcrumb-Facette (nur top_k)
    breadcrumbs = [c.get("breadcrumb") or [] for c in subset if c.get("breadcrumb")]
    if len(breadcrumbs) >= min_values:
        max_depth = max(len(b) for b in breadcrumbs)
        for depth in range(max_depth):
            values_at_depth = [b[depth] for b in breadcrumbs if len(b) > depth]
            unique = sorted(set(values_at_depth))
            if len(unique) >= min_values and len(unique) < len(values_at_depth):
                facets.append({"label": "Bereich", "options": unique})
                break

    # 2. Titelwort-Facette — scannt alle Kandidaten (nicht nur top_k),
    #    damit Dokumenttypen wie "Zusammenstellung" auch gefunden werden,
    #    wenn sie erst auf Rang 20–50 liegen.
    title_opts = _title_facets(candidates, min_count=min_values)
    # 3. Tab./Fig.-Erkennung: wenn Kandidaten-Texte auf Tabellen oder
    #    Abbildungen verweisen, diese als explizite Schlüsselwörter anbieten.
    tab_found = any("Tab." in (c.get("text") or "") or "Tabelle" in (c.get("text") or "") for c in candidates)
    fig_found = any("Fig." in (c.get("text") or "") for c in candidates)
    content_type_opts = []
    if tab_found:
        content_type_opts.append("Tabelle")
    if fig_found:
        content_type_opts.append("Abbildung")

    # Titelwort-Facetten + Inhaltstypen zusammenführen
    bc_values = {v.lower() for f in facets for v in f["options"]}
    title_opts = [o for o in title_opts if o.lower() not in bc_values]
    kw_opts = title_opts + [o for o in content_type_opts if o.lower() not in bc_values]
    if kw_opts:
        facets.append({"label": "Schlüsselwörter", "options": kw_opts})

    return facets


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
