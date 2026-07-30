"""
Regressionstest für Hebel B: generische Verben ("wählen", "fahren") tragen keinen
Unterscheidungswert und dürfen keine Fehltreffer mehr erzeugen.

Getestet wird der Tokenizer (die deterministische Kern-Mechanik). Ist der Verb-
Token weder in der Query noch im Index-Titel, kann er auch nicht mehr matchen.

Beobachtete Fälle:
  * „Wie wähle ich die Montagefunktion vor?"  zog „Seile wählen"  (nur via „wählen").
  * „…fahre ich Winde 1 heben?"               zog „Fahren über Geländekuppe" (via „fahren").

backend/search.py importiert numpy/rank_bm25 auf Modulebene → in einer Umgebung
ohne diese Pakete werden die Tests übersprungen (skip), laufen aber in der
regulären Backend-Umgebung.
"""
import pytest

pytest.importorskip("numpy")
pytest.importorskip("rank_bm25")

from backend.search import _tokenize, _GENERIC_VERBS  # noqa: E402


def _tokset(s):
    return set(_tokenize(s))


# ── Rauschen verschwindet ───────────────────────────────────────────────────

def test_waehlen_no_longer_matches_seile_waehlen():
    q = "Wie wähle ich die Montagefunktion vor?"
    assert _tokset(q).isdisjoint(_tokset("Seile wählen"))


def test_fahren_no_longer_matches_gelaendekuppe():
    q = "Mit welchem Schalter fahre ich Winde 1 heben?"
    assert _tokset(q).isdisjoint(_tokset("Fahren über Geländekuppe"))


# ── Recall bleibt erhalten ──────────────────────────────────────────────────

def test_recall_distinctive_noun_survives():
    q = "Wie wähle ich die Montagefunktion vor?"
    # Das Thema wird weiter über das distinktive Nomen getroffen.
    assert "montagefunktion" in _tokset(q) & _tokset("Montagefunktionen einschalten")


# ── Verbformen und ihre Stämme werden gefiltert ─────────────────────────────

def test_generic_verb_forms_and_stems_removed():
    toks = _tokenize("Seile wählen und fahren")
    for v in ("wählen", "wähle", "fahren", "fahre", "wahl", "fahr"):
        assert v not in toks
    # Das eigentliche Sachwort bleibt.
    assert "seile" in toks


def test_noun_keeps_surface_but_loses_verb_stem_bridge():
    # „Fahrer" behält die Oberflächenform, wird aber nicht mehr über den
    # generischen Stamm „fahr" mit dem Verb „fahren" verbrückt.
    toks = _tokenize("Fahrer")
    assert "fahrer" in toks
    assert "fahr" not in toks


def test_generic_verbs_set_contains_stems():
    # Die Stämme müssen im Set stehen, sonst bliebe die Stamm-Brücke.
    assert {"wahl", "fahr"} <= _GENERIC_VERBS
