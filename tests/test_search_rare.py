"""
Regressionstest für Hebel A: Seltene-Wort-Bonus.

Ein distinktives Frage-Wort (z. B. „Lastort", ~2,5 % der Seiten) soll Seiten, die
es wörtlich enthalten, gegenüber semantischen Fast-Duplikaten anheben. Belegt:
- „Bildschirmseite Windenkonfiguration" (richtig) enthält „Lastort".
- „Bildschirmseite Arbeitsbereichsbegrenzung" (Fehl-#1) enthält es NICHT.

backend/search.py importiert numpy/rank_bm25 auf Modulebene → ohne diese Pakete
werden die Tests übersprungen. `_load_index()` baut nur den BM25-Index (kein
Embedding-Modell), ist also schnell.
"""
import pytest

pytest.importorskip("numpy")
pytest.importorskip("rank_bm25")

from backend import search as S  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _index():
    S._load_index()


def _rank(titles, needle):
    for i, t in enumerate(titles):
        if needle.lower() in t.lower():
            return i
    return 10**6


# ── Welche Wörter gelten als „selten"? ──────────────────────────────────────

def test_distinctive_term_flagged_rare():
    rare = S._rare_query_terms("Auf welcher Bildschirmseite kann ich den Lastort konfigurieren?")
    assert "lastort" in rare


def test_common_terms_not_flagged_rare():
    rare = S._rare_query_terms("Wie bediene ich den Hauptausleger auf der Bildschirmseite?")
    assert "hauptausleger" not in rare      # df ~460 → nicht distinktiv
    assert "bildschirmseite" not in rare    # df ~250 → nicht distinktiv


def test_absent_word_is_not_rare():
    # „Meisterschalter" steht in ~0 Seiten → IDF 0 → KEIN seltener Treffer.
    # Dieser Fall braucht Synonym-Expansion (Hebel C), keinen Bonus.
    rare = S._rare_query_terms("Mit welchem Meisterschalter fahre ich Winde1?")
    assert "meisterschalter" not in rare


def test_rare_terms_are_capped():
    rare = S._rare_query_terms(
        "Lastort Seilführung Windenkonfiguration Lasthaken Symbol Montagefunktion"
    )
    assert len(rare) <= S._RARE_TERM_MAX


def test_interrogatives_and_ultrarare_artifacts_excluded():
    # Sprachunabhängige Absicherung: Interrogativa/generische Frage-Verben
    # (Stoppliste) und 1–2-Seiten-Artefakte (IDF-Fenster-Obergrenze) dürfen NICHT
    # als seltenes Sachwort gelten; das distinktive Wort bleibt.
    rare = S._rare_query_terms("Auf welcher Bildschirmseite kann ich den Lastort konfigurieren?")
    assert "lastort" in rare
    for w in ("welcher", "konfigurieren", "bildschirmseite"):
        assert w not in rare


# ── Wirkung auf das Ranking (tolerant, richtungsbasiert) ────────────────────

def test_rare_boost_helps_lastort_over_arbeitsbereich(monkeypatch):
    q = "Auf welcher Bildschirmseite kann ich den Lastort konfigurieren?"

    monkeypatch.setattr(S, "_RARE_TERM_BOOST", 1.0)   # Bonus aus
    off = [r["title"] for r in S.search(q, top_n=12)]

    monkeypatch.setattr(S, "_RARE_TERM_BOOST", 1.6)   # Bonus an
    on = [r["title"] for r in S.search(q, top_n=12)]

    # Abstand (Arbeitsbereichsbegrenzung-Rang − Windenkonfiguration-Rang):
    # mit Bonus soll die richtige Seite relativ NICHT schlechter stehen.
    off_gap = _rank(off, "Arbeitsbereichsbegrenzung") - _rank(off, "Windenkonfiguration")
    on_gap = _rank(on, "Arbeitsbereichsbegrenzung") - _rank(on, "Windenkonfiguration")
    assert on_gap >= off_gap
