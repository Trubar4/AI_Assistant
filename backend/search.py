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
import os
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
COMPOSITIONS   = _ROOT / "data" / "compositions.json"

_EMB_NPY = _ROOT / "data" / "embeddings.npy"
_EMB_IDS = _ROOT / "data" / "embedding_ids.json"
SYNONYMS_PATH  = _ROOT / "data" / "search_synonyms.json"

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

# Generische Aktions-/Bedienverben: sehr hohe Dokumentfrequenz (>7 % der Seiten),
# praktisch kein Unterscheidungswert. Sie erzeugen Fehltreffer, wenn eine Frage
# nur über das Verb matcht — z. B. „Seile *wählen*" bei „Wie *wähle* ich …?" oder
# „*Fahren* über Geländekuppe" bei „Mit welchem Schalter *fahre* ich …?".
# Werden – wie Stoppwörter – aus Query UND Index-Tokens gefiltert. Kuratiert &
# erweiterbar; enthält die Flexionsformen UND die vom leichten Stemmer erzeugten
# Stämme ("wahl"/"fahr"), sonst bliebe die Stamm-Brücke bestehen.
_GENERIC_VERBS = {
    "wählen", "wähle", "wählt", "wählst", "gewählt", "wahl",   # wählen (+ Stamm)
    "fahren", "fahre", "fahrst", "gefahren", "fahr",           # fahren (+ Stamm)
}


_UNIT_RE = re.compile(r"^(\d+)(m|ft|t|kg|h|hz|kw|kn|bar|mm|cm|rpm)$")

# Sehr leichte deutsche Morphologie (generisch, kein Themen-Hardcoding).
# Additiv: der Stamm wird ZUSÄTZLICH zum Originaltoken emittiert (wie bei
# Einheiten/Bindestrich), nie als Ersatz — exakte Treffer bleiben also stark
# (Titel×3), der Stamm liefert nur die Flexions-/Plural-Brücke.
_UMLAUT_MAP = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss"})
# Bewusst konservativ: nur -en/-er/-e (kein -n/-s), damit Einzahl↔Mehrzahl
# konvergiert (einscherplan ↔ einscherpläne, traglast ↔ traglasten) OHNE
# Singularformen auf -n/-s zu zerlegen. Immer nur EINE Endung, Stamm ≥ 4 Zeichen.
_DE_STEM_SUFFIXES = ("en", "er", "e")


def _stem_de(token: str) -> str:
    """Faltet Umlaute und streift höchstens eine häufige Endung (-en/-er/-e) ab."""
    s = token.translate(_UMLAUT_MAP)
    for suf in _DE_STEM_SUFFIXES:
        if s.endswith(suf) and len(s) - len(suf) >= 4:
            return s[: -len(suf)]
    return s


def _tokenize(text: str) -> list[str]:
    """Tokenisiert Text für BM25-Index und Queries.

    Besonderheiten:
    - Zahl+Einheit wie "74m" oder "12t" wird als "74m" UND "74" emittiert,
      damit "74m" (Query) und "74 m" (Manual-Text) einander matchen.
    - Bindestriche innerhalb alphanumerischer Cluster bleiben erhalten
      ("6-fach", "Hauptausleger-Zwischenstück").
    - Für rein alphabetische Tokens wird zusätzlich eine leichte deutsche
      Stammform emittiert (Umlaut-Faltung + -en/-er/-e), damit Einzahl/Mehrzahl
      und Umlaut-Varianten matchen ("Einscherplan" ↔ "Einscherpläne").
    """
    raw = re.findall(r"[a-zäöüß0-9]+(?:[-][a-zäöüß0-9]+)*", text.lower())
    result = []

    def _add(tok: str) -> None:
        result.append(tok)
        # Stammform nur für alphabetische Tokens (keine Zahlen/Einheiten), additiv.
        # Generische-Verb-Stämme (wahl/fahr) werden NICHT emittiert, sonst würde
        # z. B. „Fahrer" über den Stamm „fahr" doch wieder als Verb matchen.
        if tok.isalpha():
            stem = _stem_de(tok)
            if stem != tok and stem not in _GENERIC_VERBS:
                result.append(stem)

    for t in raw:
        if len(t) <= 1 or t in _STOPWORDS_DE or t in _GENERIC_VERBS:
            continue
        _add(t)
        m = _UNIT_RE.match(t)
        if m:
            result.append(m.group(1))   # "74m" → auch "74" emittieren
        if "-" in t:
            # "hauptausleger-kopf" → auch "hauptauslegerkopf" emittieren,
            # damit Queries ohne Bindestrich auf bindestrich-indizierte Begriffe treffen.
            _add(t.replace("-", ""))
    return result


# ---------------------------------------------------------------------------
# Index loading (lazy, cached)
# ---------------------------------------------------------------------------
_index: list[dict] | None = None
_bm25: BM25Okapi | None = None           # full-text BM25 (title×3 + body)
_bm25_title: BM25Okapi | None = None     # title-only BM25
_bm25_filenames: list[str] | None = None


def _composition_index_terms() -> dict[str, str]:
    """{filename: Zusatz-Suchbegriffe} aus data/compositions.json (OCR-Bauteile).
    Macht grafische Zusammenstellungsseiten per Freitext auffindbar."""
    try:
        data = json.loads(COMPOSITIONS.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, str] = {}
    for fname, p in data.get("pages", {}).items():
        lengths = sorted({x for row in p.get("rows", {}).values() for x in row})
        terms = ["Zwischenstück", "Zwischenstücke", "Ausleger-Zwischenstück", "Segmente",
                 "Anlenkstück", "Auslegerkopf", "Zusammenstellung", p.get("boom", "")]
        terms += [f"{n} m" for n in lengths]
        if p.get("seilfuehrung"):
            terms += ["Seilführung", "Einbauposition"]
        out[fname] = " ".join(terms)
    return out


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

    # Composition-Keywords: grafische Zusammenstellungsseiten tragen ihre
    # Bauteil-Begriffe ("Zwischenstück", Segmentlängen, "Seilführung") nur im
    # Bild. Aus data/compositions.json (OCR) speisen wir sie in den Suchtext ein,
    # damit die Seiten per Freitext auffindbar werden — NUR in den BM25-Korpus,
    # nicht in den angezeigten Seitentext (keine künstlichen Snippets).
    comp_terms = _composition_index_terms()

    corpus_full  = []
    corpus_title = []
    _bm25_filenames = []
    for entry in merged:
        title_tokens = _tokenize(entry["title"])
        warn_tokens  = _tokenize(" ".join(entry["warnings"]))
        step_tokens  = _tokenize(" ".join(entry["steps"][:15]))
        body_tokens  = _tokenize(entry["text"])
        aug_tokens   = _tokenize(comp_terms.get(entry["filename"], ""))
        corpus_full.append(title_tokens * 3 + warn_tokens + step_tokens + body_tokens + aug_tokens)
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


_DIM_TO_MODEL = {
    768:  "paraphrase-multilingual-mpnet-base-v2",
    1024: "BAAI/bge-m3",
}


def _load_semantic() -> bool:
    global _sem_model, _sem_matrix, _sem_ids
    # SEMANTIC_SEARCH=off deaktiviert die semantische Suche komplett
    # (z. B. auf Speicher-limitierten Instanzen); BM25 + TF-IDF bleiben aktiv.
    if os.environ.get("SEMANTIC_SEARCH", "").lower() in ("0", "off", "false"):
        return False
    if _sem_matrix is not None and _sem_model is not None:
        return True
    if _sem_matrix is not None and _sem_model is None:
        return False  # matrix loaded but model unavailable
    if not (_EMB_NPY.exists() and _EMB_IDS.exists()):
        return False
    try:
        from sentence_transformers import SentenceTransformer
        matrix = np.load(str(_EMB_NPY))
        dim = matrix.shape[1]
        model_name = _DIM_TO_MODEL.get(dim)
        if model_name is None:
            logger.warning("Semantic search: unbekannte Embedding-Dimension %d", dim)
            return False
        model = SentenceTransformer(model_name)
        # Validate dimension before committing globals
        test_vec = model.encode(["test"], normalize_embeddings=True)[0]
        if test_vec.shape[0] != dim:
            logger.warning(
                "Semantic search: Modell %s erzeugt %d-dim, Matrix hat %d-dim — deaktiviert",
                model_name, test_vec.shape[0], dim,
            )
            return False
        _sem_matrix = matrix
        _sem_ids    = json.loads(_EMB_IDS.read_text(encoding="utf-8"))
        _sem_model  = model
        logger.info("Semantic search geladen: %d Dokumente (%d-dim, %s)", len(_sem_ids), dim, model_name)
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

# Dokumenttyp-Prior (generisch, kein Themen-Hardcoding): Titel-Wörter, die eine
# Referenz-/Konfigurations-/Tabellenseite kennzeichnen — dort stehen die Werte,
# die Nutzer nachschlagen. Boost per Env kalibrierbar (1.0 = aus).
_DOCTYPE_BOOST = float(os.environ.get("SEARCH_DOCTYPE_BOOST", "1.25"))
_DOCTYPE_WORDS = (
    "zusammenstellung", "übersicht", "wahl", "auslegerkonfiguration",
    "traglasttabelle", "einscherplan", "längen", "gewichte",
)


def _is_reference_page(title: str) -> bool:
    t = title.lower()
    return any(w in t for w in _DOCTYPE_WORDS)


# Zu generische Titel-/Fragewörter: taugen NICHT als Topik-Beleg für den Boost
# (stehen in vielen Titeln, z. B. jede „Bildschirmseite …" / jeder „…ausleger").
_DOCTYPE_GENERIC = {
    "bildschirmseite", "hauptausleger", "nadelausleger", "ausleger",
    "maschine", "seite", "tabelle", "übersicht", "konfiguration",
}


def _doctype_query_terms(query: str) -> set[str]:
    """Sachwörter der Frage (≥4 Zeichen, keine Stopp-/Generikwörter)."""
    return {w for w in re.findall(r"[a-zäöüß]{4,}", query.lower())
            if w not in _STOPWORDS_DE and w not in _DOCTYPE_GENERIC}


def _page_matches_query_doctype(title: str, q_terms: set[str]) -> bool:
    """True, wenn (1) der Titel eine Referenz-/Tabellenseite kennzeichnet
    (Dokumenttyp-Wort) UND (2) die Frage ein aussagekräftiges (nicht-generisches)
    Sachwort mit dem Titel teilt. Der Boost wirkt so fragesensitiv:
    - „Wahl des richtigen Lasthakens" wird bei einer Lasthaken-Frage angehoben
      (gemeinsames „lasthaken"), obwohl das Dokumenttyp-Wort „wahl" nicht in der
      Frage steht.
    - „Bildschirmseite Auslegerkonfiguration" wird bei einer Lastort-Frage NICHT
      angehoben (nur das generische „bildschirmseite" wäre gemeinsam)."""
    t = title.lower()
    if not any(w in t for w in _DOCTYPE_WORDS):
        return False
    title_terms = {w for w in re.findall(r"[a-zäöüß]{4,}", t)
                   if w not in _STOPWORDS_DE and w not in _DOCTYPE_GENERIC}
    for qt in q_terms:
        for tt in title_terms:
            if qt == tt or (len(qt) >= 5 and qt in tt) or (len(tt) >= 5 and tt in qt):
                return True
    return False


def _rrf(rankings: list[list[tuple[str, float]]]) -> dict[str, float]:
    """Combine ranked lists via RRF. Each list is [(filename, score), ...]."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, (fname, _) in enumerate(ranking):
            scores[fname] = scores.get(fname, 0.0) + 1.0 / (_RRF_K + rank)
    return scores


# ── Seltene-Wort-Bonus (Hebel A) ─────────────────────────────────────────────
# Kandidaten, die ein SELTENES (distinktives) Frage-Wort wörtlich enthalten,
# werden angehoben. Fängt den Fall ab, dass ein klarer Schlüsselbegriff (z. B.
# „Lastort", ~2,5 % der Seiten) in der flachen RRF-Fusion gegen semantische
# Fast-Duplikate untergeht. RRF verwirft die Trefferstärke — dieser Bonus holt
# sie gezielt für seltene Begriffe zurück. Alles per Env kalibrierbar (1.0 = aus).
_RARE_TERM_BOOST   = float(os.environ.get("SEARCH_RARE_TERM_BOOST", "1.6"))
_RARE_TERM_IDF_MIN = float(os.environ.get("SEARCH_RARE_TERM_IDF_MIN", "3.0"))  # ~ df ≤ 5 %
# Obergrenze: Wörter in nur 1–2 Seiten (IDF > ~6) sind meist OCR-/Frageartefakte
# („welches", „konfigurieren"), keine distinktiven Sachbegriffe → nicht boosten.
_RARE_TERM_IDF_MAX = float(os.environ.get("SEARCH_RARE_TERM_IDF_MAX", "6.0"))  # ~ df ≥ 4
_RARE_TERM_MAX     = int(os.environ.get("SEARCH_RARE_TERM_MAX", "3"))          # nur die K seltensten

# Frage-/Funktionswörter, die im TECHNISCHEN Korpus selten sind (Interrogativa,
# generische Verben/Adverbien), aber keinen Sachbezug tragen — sonst würden sie
# als „seltenes Wort" Seiten anheben, die zufällig „welcher"/„konfigurieren"
# enthalten. Nur für den Seltene-Wort-Picker (kein globaler BM25-Eingriff).
_RARE_TERM_STOP = {
    "welch", "welche", "welcher", "welches", "welchem", "welchen",
    "wo", "wohin", "woher", "womit", "wodurch", "warum", "wann", "wieso", "weshalb",
    "konfigurieren", "konfiguriere", "einstellen", "einstelle", "vorwählen",
    "mindestens", "höchstens", "maximal", "minimal", "benötige", "brauche",
}


def _rare_query_terms(query: str) -> list[str]:
    """Distinktive (seltene) Sachwörter der Frage — Seltenheit aus der BM25-IDF
    des Index (kein Themen-Hardcoding, sprachunabhängige Mechanik).

    Alphabetische Tokens ≥4 Zeichen im IDF-Fenster [MIN, MAX]; die K seltensten.
    Ausgeschlossen: Interrogativa/generische Frage-Verben (_RARE_TERM_STOP) sowie
    – über die Fenster-Obergrenze – 1–2-Seiten-Artefakte. Wörter außerhalb des
    Korpus (IDF 0, z. B. „Meisterschalter") sind KEINE seltenen Treffer → Hebel C.

    Bewusst KEINE Großschreibungs-Heuristik: die Manuals (und damit Fragen) können
    in Sprachen ohne Substantiv-Großschreibung vorliegen. Ein gelegentlich
    mitgezogenes generisches Wort ist unkritisch, weil der Bonus binär ist und nur
    bereits gefundene Kandidaten anhebt (die Basis-Relevanz rankt darunter weiter)."""
    if _bm25 is None:
        return []
    idf = getattr(_bm25, "idf", {})
    toks = {t for t in _tokenize(query)
            if t.isalpha() and len(t) >= 4 and t not in _RARE_TERM_STOP}
    rare = [t for t in toks if _RARE_TERM_IDF_MIN <= idf.get(t, 0.0) <= _RARE_TERM_IDF_MAX]
    rare.sort(key=lambda t: idf.get(t, 0.0), reverse=True)
    return rare[:_RARE_TERM_MAX]


# ── Synonym-/Query-Expansion (Hebel C) ───────────────────────────────────────
# Schließt Wortschatz-Lücken zwischen Nutzer- und Manual-Vokabular: „Meisterschalter"
# steht in 0 Seiten, das Manual sagt „Bedienhebel"/„Kreuz-Bedienhebel". Die Frage
# wird vor der Suche um die passenden Manual-Begriffe ergänzt — davon profitieren
# BM25 UND Semantik. Kuratiert & manuell pflegbar in data/search_synonyms.json.
_SYNONYMS: dict[str, list[str]] | None = None


def _load_synonyms() -> dict[str, list[str]]:
    global _SYNONYMS
    if _SYNONYMS is not None:
        return _SYNONYMS
    try:
        data = json.loads(SYNONYMS_PATH.read_text(encoding="utf-8"))
        raw = data.get("synonyms", {}) if isinstance(data, dict) else {}
        _SYNONYMS = {str(k).lower(): [str(x).lower() for x in v]
                     for k, v in raw.items() if isinstance(v, list)}
    except Exception:
        _SYNONYMS = {}   # Datei optional — ohne sie einfach keine Expansion
    return _SYNONYMS


def _expand_synonyms(query: str) -> str:
    """Ergänzt die Query um kuratierte Manual-Synonyme, wenn ein Schlüsselwort
    vorkommt. Additiv (Original bleibt), damit exakte Treffer stark bleiben."""
    syn = _load_synonyms()
    if not syn:
        return query
    toks = set(_tokenize(query))
    extra: list[str] = []
    for key, vals in syn.items():
        if key in toks:
            extra.extend(vals)
    if not extra:
        return query
    extra = list(dict.fromkeys(extra))
    logger.info("Synonym-Expansion: %s", extra)
    return query + " " + " ".join(extra)


# ── Stub-/Kanonik-Unterscheidung (Hebel D) ───────────────────────────────────
# Kurze Verweis-Seiten („… Weitere Informationen siehe: …") sind fast reine
# Pointer ohne Sachinhalt. BM25 bevorzugt sie (kurzes Dokument → hoher Score pro
# Term), sodass bei GLEICHNAMIGEN Seiten der Stub die inhaltliche Kanonik-Seite
# aus der Titel-Deduplizierung verdrängt. Solche Seiten werden abgewertet, damit
# die substanzielle Seite gewinnt. Signal: wenig Inhalt UND Verweismuster
# (sprachrobust: primär die Wortzahl, das Verweiswort als Bestätigung). 1.0 = aus.
_STUB_PENALTY   = float(os.environ.get("SEARCH_STUB_PENALTY", "0.5"))
_STUB_MAX_WORDS = int(os.environ.get("SEARCH_STUB_MAX_WORDS", "18"))
_STUB_REF_RE = re.compile(
    r"weitere informationen siehe|\(siehe|siehe:|→\s*siehe|\bsee also\b|voir aussi|véase|vedi",
    re.I,
)


def _is_stub(entry: dict) -> bool:
    """Kurze Verweis-/Pointer-Seite: wenig Inhalt (≤ _STUB_MAX_WORDS Wörter) UND
    ein „siehe …"-Verweismuster."""
    wc = entry.get("word_count")
    if not isinstance(wc, int) or wc <= 0:
        wc = len((entry.get("text") or "").split())
    if wc > _STUB_MAX_WORDS:
        return False
    return bool(_STUB_REF_RE.search(entry.get("text", "") or ""))


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

    query = _expand_synonyms(query)   # Hebel C: Nutzer- → Manual-Vokabular

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

    # Dokumenttyp-Prior (FRAGESENSITIV): Referenz-/Konfigurationsseiten
    # (Zusammenstellung, Übersicht, Wahl, Traglast-/Einscherung-Tabellen …) tragen
    # die Werte, die Nutzer suchen. Der Boost greift aber NUR, wenn die Frage auch
    # von diesem Dokumenttyp handelt (gemeinsames Sachwort) — sonst würden z. B.
    # „Auslegerkonfiguration"-Seiten jede Frage dominieren (Lastort-Fall). Generisch
    # über den Seitentyp, kein Themen-Hardcoding; wirkt nur auf bereits gefundene
    # Kandidaten.
    if _DOCTYPE_BOOST != 1.0:
        q_terms = _doctype_query_terms(query)
        if q_terms:
            for fname in rrf_scores:
                entry = index_by_filename.get(fname)
                if entry and _page_matches_query_doctype(entry.get("title", ""), q_terms):
                    rrf_scores[fname] *= _DOCTYPE_BOOST

    # Seltene-Wort-Bonus (Hebel A): Kandidaten, die ein seltenes Frage-Wort
    # WÖRTLICH enthalten, bekommen einen festen Boost. Bewusst binär (nicht nach
    # Trefferstärke gewichtet), damit eine themenfremde Seite mit dem Wort im Titel
    # (z. B. „Einscherpläne … (Lastort 2)") nicht stärker angehoben wird als die
    # eigentlich passende Seite — unter den geboosteten Seiten entscheidet weiter
    # die Basis-Relevanz (RRF). Wirkt nur auf bereits gefundene Kandidaten.
    rare_terms = _rare_query_terms(query) if _RARE_TERM_BOOST != 1.0 else []
    if rare_terms:
        boosted: set[str] = set()
        for t in rare_terms:
            scores = _bm25.get_scores([t])
            for i, s in enumerate(scores):
                if s > 0:
                    fname = _bm25_filenames[i]
                    if fname in rrf_scores:
                        boosted.add(fname)
        for fname in boosted:
            rrf_scores[fname] *= _RARE_TERM_BOOST
        logger.info("Seltene-Wort-Bonus (×%.2f) für %s → %d Kandidat(en) angehoben",
                    _RARE_TERM_BOOST, rare_terms, len(boosted))

    # Stub-Abwertung (Hebel D): kurze Verweis-Seiten zurückstufen, damit bei
    # gleichnamigen Seiten die inhaltliche Kanonik-Seite die Titel-Dedup gewinnt.
    if _STUB_PENALTY != 1.0:
        n_stub = 0
        for fname in rrf_scores:
            entry = index_by_filename.get(fname)
            if entry and _is_stub(entry):
                rrf_scores[fname] *= _STUB_PENALTY
                n_stub += 1
        if n_stub:
            logger.info("Stub-Abwertung (×%.2f) für %d Verweis-Seite(n)", _STUB_PENALTY, n_stub)

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
