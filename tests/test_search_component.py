"""
Regressionstest für Hebel E (Komponenten-Scope, titel-bewusst) und
Hebel F (Lastort-Varianten-Scope).

E: Nennt die Frage genau EINE Auslegerkomponente, werden Seiten unter der ANDEREN
   Komponente abgewertet — ABER nur, wenn ihr Titel nicht selbst von der gefragten
   Komponente handelt. „…Hauptausleger-Kopf … (Lastort 2)" liegt zwar unter einer
   Nadelausleger-Sektion, handelt aber vom Hauptausleger → NICHT abwerten.
F: Nennt die Frage einen konkreten „Lastort N" (z. B. Followup „Lastort 2"),
   werden Kandidaten mit abweichendem Lastort im Titel abgewertet.

backend/search.py importiert numpy/rank_bm25 → ohne diese Pakete: Skip.
"""
import json

import pytest

pytest.importorskip("numpy")
pytest.importorskip("rank_bm25")

from backend import search as S  # noqa: E402


def _pages(prefix):
    idx = json.loads(S.CONTENT_INDEX.read_text(encoding="utf-8"))
    return [v for v in idx.values() if v.get("title", "").startswith(prefix)]


# ── Hebel E ─────────────────────────────────────────────────────────────────

def test_query_component_detects_single_component():
    assert S._query_component("Wie sieht der Einscherplan am Hauptausleger aus?") == "hauptausleger"
    assert S._query_component("Wo Seilführung am Nadelausleger einbauen?") == "nadelausleger"
    assert S._query_component("Wie hebe ich die Last?") is None
    assert S._query_component("Hauptausleger oder Nadelausleger?") is None


def test_component_scope_is_title_aware():
    # HA-Kopf-Einscherpläne (auch unter Nadelausleger-Sektionen) handeln vom
    # Hauptausleger → bei einer Hauptausleger-Frage NICHT abwerten.
    l2 = [p for p in _pages("Einscherpläne für ein Seil über Hauptausleger-Kopf 2320")
          if "Lastort 2" in p.get("title", "")]
    assert l2
    assert not any(S._under_other_component(p, "hauptausleger") for p in l2)


def test_component_scope_penalizes_genuine_other_component():
    entry = {
        "title": "Nadelausleger-Verstellwinde bedienen",
        "breadcrumb": ["Bedienung, Betrieb", "Verstellbarer Nadelausleger 1916",
                       "Nadelausleger-Verstellwinde bedienen"],
    }
    assert S._under_other_component(entry, "hauptausleger")      # unter Nadel, Titel nicht HA
    assert not S._under_other_component(entry, "nadelausleger")  # ist ja Nadel


# ── Hebel F ─────────────────────────────────────────────────────────────────

def test_query_lastort_extraction():
    assert S._query_lastort("Wie sieht der Einscherplan am Hauptausleger aus? Lastort 2") == "2"
    assert S._query_lastort("... für Lastort 1 ...") == "1"
    assert S._query_lastort("Wie sieht der Einscherplan aus?") is None
