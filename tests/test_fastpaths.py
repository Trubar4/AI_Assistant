"""
Regressionstests für die deterministischen Fast-Paths (backend/fastpaths.py).

Schwerpunkt: die Maschinenkonfiguration darf einen Fast-Path nicht mehr kippen,
nur weil ein *irrelevantes* Feld (z. B. ein aktiver Nadelausleger) im Kontext
steht. Ausgangspunkt war die reale Beobachtung, dass die Frage
„An welcher Stelle im Hauptausleger muss die Seilführung eingebaut werden?"
nur nach Abwählen des Nadelauslegers die präzise Antwort lieferte.

Die Fast-Paths brauchen nur data/compositions.json (kein numpy/Retrieval), die
Volltextsuche wird in fastpaths.py lazy importiert — daher laufen diese Tests
ohne ML-Abhängigkeiten.
"""
import backend.fastpaths as fp

# Vollständiger Konfig-Kontext MIT aktivem Nadelausleger (kanonisches Format der App).
CTX_MIT_NADEL = (
    "Auslegerkonfiguration: Hauptausleger + feststehender Nadelausleger / "
    "Hauptausleger: 74 m / Nadelausleger: 20 m (fest) / Heckballast: 12 t / Einscherung: 6x"
)
CTX_NUR_HAUPT = "Auslegerkonfiguration: Nur Hauptausleger / Hauptausleger: 74 m / Einscherung: 6x"
CTX_NUR_NADEL = "Auslegerkonfiguration: Nadelausleger / Nadelausleger: 26 m"


# ── _resolve_boom: die Frage hat Vorrang ────────────────────────────────────

def test_resolve_boom_question_wins_over_context():
    q = "An welcher Stelle im Hauptausleger muss die Seilführung eingebaut werden?"
    assert fp._resolve_boom(q, CTX_MIT_NADEL) == "hauptausleger"


def test_resolve_boom_question_names_nadelausleger():
    q = "Wo muss die Seilführung im Nadelausleger eingebaut werden?"
    assert fp._resolve_boom(q, CTX_MIT_NADEL) == "nadelausleger"


def test_resolve_boom_no_boom_in_question_defaults_to_haupt_when_configured():
    q = "Wie viele 12 m Zwischenstücke gibt es?"
    assert fp._resolve_boom(q, CTX_MIT_NADEL) == "hauptausleger"


def test_resolve_boom_only_nadel_configured():
    q = "Wie viele 12 m Zwischenstücke gibt es?"
    assert fp._resolve_boom(q, CTX_NUR_NADEL) == "nadelausleger"


# ── Länge feldbezogen statt „erste Meterangabe" ─────────────────────────────

def test_boom_field_length_picks_matching_field():
    assert fp._boom_field_length("hauptausleger", CTX_MIT_NADEL) == 74
    assert fp._boom_field_length("nadelausleger", CTX_MIT_NADEL) == 20


def test_length_for_boom_question_length_wins():
    assert fp._length_for_boom("hauptausleger", "Seilführung bei 50 m?", CTX_MIT_NADEL) == 50


# ── Tabellen-Fast-Path: Zeile/Spalte feldbezogen (#2) ───────────────────────

def test_extract_row_col_uses_haupt_field_not_first_meter():
    row, col = fp._extract_row_col("Wie schwer darf die Last bei 6-fach sein?", CTX_MIT_NADEL)
    assert row == "74 m"      # HA-Feld, nicht Nadelausleger 20 m
    assert col == "6"


def test_extract_row_col_question_length_wins():
    row, col = fp._extract_row_col("Traglast bei 50 m und 4-fach?", CTX_MIT_NADEL)
    assert row == "50 m"
    assert col == "4"


def test_extract_row_col_nadelausleger_question():
    row, _ = fp._extract_row_col("Traglast am Nadelausleger?", CTX_MIT_NADEL)
    assert row == "20 m"


# ── Seilführungs-Fast-Path: die eigentliche Regression ──────────────────────

def test_seilfuehrung_haupt_not_broken_by_active_nadelausleger():
    """Der gemeldete Fehler: mit aktivem Nadelausleger fiel der Fast-Path in den
    Fallback (None). Jetzt muss er die präzise Hauptausleger-Antwort liefern."""
    q = "An welcher Stelle im Hauptausleger muss die Seilführung eingebaut werden?"
    res = fp.seilfuehrung_fastpath(q, CTX_MIT_NADEL)
    assert res is not None
    assert res["type"] == "answer"
    assert "74 m" in res["answer"]
    assert "Hauptausleger" in res["answer"]
    # Zwei Konfigurations-Positionen (S / N Marker) müssen benannt sein.
    assert "Auslegerkonfiguration 1/3" in res["answer"]
    assert "Auslegerkonfiguration 4" in res["answer"]
    assert res["sources"] and res["sources"][0].get("filename")


def test_seilfuehrung_identical_with_and_without_nadelausleger():
    """Ob der Nadelausleger im Kontext steht oder nicht, darf die Hauptausleger-
    Antwort nicht verändern — genau das war vorher nicht der Fall."""
    q = "An welcher Stelle im Hauptausleger muss die Seilführung eingebaut werden?"
    a = fp.seilfuehrung_fastpath(q, CTX_MIT_NADEL)
    b = fp.seilfuehrung_fastpath(q, CTX_NUR_HAUPT)
    assert a is not None and b is not None
    assert a["answer"] == b["answer"]


def test_seilfuehrung_length_from_question_without_context():
    q = "Wo muss die Seilführung beim Hauptausleger bei 74 m eingebaut werden?"
    res = fp.seilfuehrung_fastpath(q, "")
    assert res is not None
    assert "74 m" in res["answer"]


# ── Transparenz-Zusatz (#3) ─────────────────────────────────────────────────

def test_used_config_note_appended():
    q = "An welcher Stelle im Hauptausleger muss die Seilführung eingebaut werden?"
    res = fp.seilfuehrung_fastpath(q, CTX_MIT_NADEL)
    assert "Verwendete Konfiguration: Hauptausleger 74 m" in res["answer"]
