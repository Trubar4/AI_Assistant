"""
Regressionstest für Hebel E: Komponenten-Scope-Abgleich.

Nennt die Frage genau EINE Auslegerkomponente (Hauptausleger XOR Nadelausleger),
werden Kandidaten unter der ANDEREN Komponente abgewertet. Konkreter Fall:
„Wie sieht der Einscherplan am Hauptausleger aus?" — die fünf (Lastort-2-)Kopien
liegen unter „… Nadelausleger …"-Sektionen, die richtige (Lastort-1-)Seite unter
„Hauptausleger 2320".

backend/search.py importiert numpy/rank_bm25 auf Modulebene → ohne diese Pakete
werden die Tests übersprungen.
"""
import json

import pytest

pytest.importorskip("numpy")
pytest.importorskip("rank_bm25")

from backend import search as S  # noqa: E402


def _pages(prefix):
    idx = json.loads(S.CONTENT_INDEX.read_text(encoding="utf-8"))
    return [v for v in idx.values() if v.get("title", "").startswith(prefix)]


def test_query_component_detects_single_component():
    assert S._query_component("Wie sieht der Einscherplan am Hauptausleger aus?") == "hauptausleger"
    assert S._query_component("Wo Seilführung am Nadelausleger einbauen?") == "nadelausleger"
    assert S._query_component("Wie hebe ich die Last?") is None            # keine Komponente
    assert S._query_component("Hauptausleger oder Nadelausleger?") is None  # beide → kein Scope


def test_lastort2_under_nadelausleger_penalized_lastort1_not():
    pages = _pages("Einscherpläne für ein Seil über Hauptausleger-Kopf 2320")
    l2 = [p for p in pages if "Lastort 2" in p.get("title", "")]
    l1 = [p for p in pages if "Lastort 1" in p.get("title", "")]
    assert l2 and l1
    # Für eine Hauptausleger-Frage: Lastort-2-Kopien (Nadelausleger-Sektion) abgewertet …
    assert all(S._under_other_component(p, "hauptausleger") for p in l2)
    # … die Lastort-1-Seite (Hauptausleger-Sektion) NICHT.
    assert not any(S._under_other_component(p, "hauptausleger") for p in l1)


def test_no_penalty_when_query_names_no_or_both_components():
    pages = _pages("Einscherpläne für ein Seil über Hauptausleger-Kopf 2320")
    assert pages
    # Ohne erkannte Komponente greift der Hebel nicht (die Penalty-Schleife wird
    # in search() gar nicht betreten) — hier nur die Vorbedingung geprüft.
    assert S._query_component("Wie sieht der Einscherplan aus?") is None
