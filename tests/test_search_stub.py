"""
Regressionstest für Hebel D: Stub-/Kanonik-Unterscheidung.

Von zwei gleichnamigen Seiten „Montagefunktionen einschalten" ist eine der
inhaltliche Kanonik-Eintrag (Bedienung, Betrieb › Montagefunktionen; „Taste …
am Steuerpult X23 drücken", 68 Wörter) und eine ein kurzer Verweis-Stub
(Auf- und Abbau › Grundgerät aufbauen; „… Weitere Informationen siehe: …",
9 Wörter). BM25 bevorzugt den kurzen Stub — die Stub-Abwertung dreht das um.

backend/search.py importiert numpy/rank_bm25 auf Modulebene → ohne diese Pakete
werden die Tests übersprungen. _is_stub selbst braucht nur den Eintrag.
"""
import json

import pytest

pytest.importorskip("numpy")
pytest.importorskip("rank_bm25")

from backend import search as S  # noqa: E402


@pytest.fixture(scope="module")
def montage_pages():
    idx = json.loads(S.CONTENT_INDEX.read_text(encoding="utf-8"))
    return [v for v in idx.values()
            if v.get("title", "").strip().lower() == "montagefunktionen einschalten"]


def test_pointer_page_is_stub(montage_pages):
    assert len(montage_pages) >= 2
    stub = min(montage_pages, key=lambda p: p.get("word_count", 10**6))
    assert S._is_stub(stub)


def test_substantive_page_is_not_stub(montage_pages):
    canonical = max(montage_pages, key=lambda p: p.get("word_count", 0))
    assert not S._is_stub(canonical)


def test_short_page_without_reference_is_not_stub():
    # Kurz, aber echte Anweisung ohne „siehe" → KEIN Stub.
    assert not S._is_stub({"word_count": 8, "text": "Rechten Kreuz-Bedienhebel nach hinten bewegen."})


def test_long_page_with_reference_is_not_stub():
    # Verweis vorhanden, aber viel Inhalt → KEIN Stub (nur kurze Pointer zählen).
    assert not S._is_stub({"word_count": 200, "text": "… ausführlich … Weitere Informationen siehe: X …"})
