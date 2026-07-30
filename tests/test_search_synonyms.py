"""
Regressionstest für Hebel C: Synonym-/Query-Expansion.

Schließt Wortschatz-Lücken Nutzer→Manual. Beobachteter Fall: „Meisterschalter"
steht in ~0 Manual-Seiten; das Manual sagt „Bedienhebel"/„Kreuz-Bedienhebel". Die
Query wird vor der Suche entsprechend ergänzt.

backend/search.py importiert numpy/rank_bm25 auf Modulebene → ohne diese Pakete
werden die Tests übersprungen. _expand_synonyms/_load_synonyms brauchen aber nur
data/search_synonyms.json.
"""
import pytest

pytest.importorskip("numpy")
pytest.importorskip("rank_bm25")

from backend import search as S  # noqa: E402


def test_synonyms_loaded():
    syn = S._load_synonyms()
    assert "meisterschalter" in syn
    assert "bedienhebel" in syn["meisterschalter"]


def test_meisterschalter_expands_to_manual_vocab():
    q = "Mit welchem Meisterschalter fahre ich Winde 1 Heben?"
    expanded = S._expand_synonyms(q)
    assert q in expanded                      # additiv: Original bleibt
    assert "bedienhebel" in expanded.lower()
    assert "kreuz-bedienhebel" in expanded.lower()


def test_non_synonym_query_unchanged():
    q = "Wie hebe ich die Last an Winde1?"
    assert S._expand_synonyms(q) == q


def test_expansion_is_additive_not_replacing():
    # Das Schlüsselwort selbst darf nicht verschwinden (exakte Treffer bleiben stark).
    q = "Meisterschalter Winde1"
    assert "meisterschalter" in S._expand_synonyms(q).lower()
