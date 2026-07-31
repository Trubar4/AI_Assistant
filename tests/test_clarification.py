"""
Regressionstest für die Einscherplan-Rückfragen (Disambiguierung).

Bei echter Mehrdeutigkeit soll der Assistent NACHFRAGEN statt zu raten:
- fehlt die Auslegerkomponente → „Hauptausleger oder Nadelausleger?"
- Hauptausleger genannt, aber kein Lastort → „Lastort 1 oder Lastort 2?"

Ein aktives Konfig-Feld (z. B. „Einscherung: 6x") darf die Rückfrage nicht
verdecken — sie ist bewusst frage-basiert.

backend.rule_agent ist ML-frei → dieser Test läuft ohne numpy/Embeddings.
"""
from backend.rule_agent import _needs_clarification as clarify


def test_einscherplan_hauptausleger_asks_lastort():
    r = clarify("Wie sieht der Einscherplan am Hauptausleger aus?", "")
    assert r and "lastort" in r.lower()


def test_einscherplan_without_component_asks_ausleger():
    r = clarify("Wie sieht der Einscherplan aus?", "")
    assert r and "ausleger" in r.lower()


def test_einscherplan_fully_specified_no_clarification():
    assert clarify("Wie sieht der Einscherplan am Hauptausleger für Lastort 1 aus?", "") is None


def test_einscherplan_nadelausleger_no_lastort_question():
    # Lastort 1/2 ist hauptauslegerspezifisch → beim Nadelausleger keine Lastort-Rückfrage.
    assert clarify("Wie sieht der Einscherplan am Nadelausleger aus?", "") is None


def test_active_config_einscherung_does_not_suppress_clarification():
    r = clarify("Wie sieht der Einscherplan am Hauptausleger aus?", "Einscherung: 6x / Hauptausleger: 74 m")
    assert r and "lastort" in r.lower()


def test_unrelated_query_has_no_clarification():
    assert clarify("Wie bediene ich die Seileinziehwinde?", "") is None
