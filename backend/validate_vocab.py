"""
validate_vocab.py — Analysiert den Manual-Index nach technischem Vokabular.

Zwei Modi:

1. ANALYSE (Standard): Scannt content_index.json nach technischen Mustern
   (Einscherung, Ausleger-Längen, Gewichte, Seile usw.) und gibt aus,
   welche Begriffe tatsächlich im Manual vorkommen.

   python -m backend.validate_vocab

2. KONTEXT-PRÜFUNG: Nimmt einen Konfigurationstext und prüft für jeden
   extrahierten Begriff, ob er im Manual vorkommt — mit Alternativen.

   python -m backend.validate_vocab "74m Hauptausleger / 6-fach Einscherung / 124t Heckballast"
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent.parent
CONTENT_INDEX = ROOT / "data" / "content_index.json"


# ---------------------------------------------------------------------------
# Muster für technische Begriffe im Manual
# ---------------------------------------------------------------------------

_PATTERNS = {
    "Einscherung":    re.compile(r"\b(\d+)\s*[xX×]\b|\b(\d+)[-\s]fach\b", re.I),
    "Länge_m":        re.compile(r"\b(\d{2,3})\s*m\b"),
    "Gewicht_t":      re.compile(r"\b(\d{2,4})\s*t\b"),
    "Gewicht_kg":     re.compile(r"\b(\d{3,5})\s*kg\b"),
    "Zeit_h":         re.compile(r"\b(\d{1,4})\s*h\b"),
    "Einscherung_x":  re.compile(r"\b(\d+)\s*[xX×]\b"),
    "Einscherung_fach": re.compile(r"\b(\d+)[-\s]fach\b", re.I),
    "Rollen":         re.compile(r"\b(\d+)\s*Rollen?\b", re.I),
    "Seil":           re.compile(r"\b(\d+)\s*Seil(?:e|strang|stränge)?\b", re.I),
}

# Technische Schlüsselbegriffe die häufig in Konfigurationskontext stehen
_KEY_TERMS = [
    r"hauptausleger", r"nadelausleger", r"derrickausleger",
    r"heckballast", r"gegengewicht", r"superlift",
    r"einscherung", r"ausscherung", r"einscherseil",
    r"lasthaken", r"hubwerk", r"ausleger(?:kopf|fuß|zwischenstück)",
    r"zwischenstück", r"ballastierung",
]


def _load_index() -> dict:
    return json.loads(CONTENT_INDEX.read_text(encoding="utf-8"))


def _all_texts(content_all: dict) -> list[str]:
    return [
        f"{e.get('title', '')} {e.get('text', '')} {' '.join(e.get('steps', []))}"
        for e in content_all.values()
    ]


# ---------------------------------------------------------------------------
# Modus 1: Vokabular-Analyse
# ---------------------------------------------------------------------------

def analyse() -> None:
    content_all = _load_index()
    all_text = " ".join(_all_texts(content_all))
    all_text_lower = all_text.lower()

    print("=" * 60)
    print("VOKABULAR-ANALYSE des Manuals")
    print("=" * 60)

    # Einscherung: x vs -fach
    x_vals = Counter(re.findall(r"\b(\d+)\s*[xX×]\b", all_text))
    fach_vals = Counter(re.findall(r"\b(\d+)[-\s]fach\b", all_text_lower))
    print("\n[Einscherung / Seilführung]")
    print(f"  Schreibweise 'Nx'    : {sorted(x_vals.items(), key=lambda x: -x[1])[:10]}")
    print(f"  Schreibweise 'N-fach': {sorted(fach_vals.items(), key=lambda x: -x[1])[:10]}")

    # Ausleger-Längen
    laengen = Counter(re.findall(r"\b(\d{2,3})\s*m\b", all_text))
    print(f"\n[Längen in Metern] Top-15: {sorted(laengen.items(), key=lambda x: -x[1])[:15]}")

    # Gewichte
    gewichte_t = Counter(re.findall(r"\b(\d{2,4})\s*t\b", all_text))
    print(f"\n[Gewichte in Tonnen] Top-15: {sorted(gewichte_t.items(), key=lambda x: -x[1])[:15]}")

    # Rollen
    rollen = Counter(re.findall(r"\b(\d+)\s*[Rr]ollen?\b", all_text))
    print(f"\n[Rollenzahl] {sorted(rollen.items(), key=lambda x: -x[1])}")

    # Schlüsselbegriffe
    print("\n[Technische Schlüsselbegriffe — Vorkommen]")
    for term in _KEY_TERMS:
        count = len(re.findall(term, all_text_lower))
        if count > 0:
            # Finde Beispiel-Kontext
            m = re.search(rf".{{0,30}}{term}.{{0,40}}", all_text_lower)
            ctx = m.group(0).strip() if m else ""
            print(f"  {term:<35} {count:>5}x    z.B.: '{ctx}'")

    # Einscherung-Kontext (wie steht es im Satz?)
    print("\n[Einscherung — Kontext-Beispiele aus dem Manual]")
    matches = re.finditer(r".{0,50}einscherung.{0,80}", all_text_lower)
    seen = set()
    for i, m in enumerate(matches):
        ctx = m.group(0).strip()
        key = ctx[:40]
        if key not in seen:
            seen.add(key)
            print(f"  • {ctx}")
        if i > 12:
            break


# ---------------------------------------------------------------------------
# Modus 2: Kontext-Prüfung
# ---------------------------------------------------------------------------

def _extract_terms(context: str) -> list[str]:
    """Zerlegt Konfigurationstext in einzelne Suchbegriffe."""
    # Splits an Schrägstrichen, Kommas, Semikolons, Zeilenumbrüchen
    parts = re.split(r"[/,;\n]+", context)
    terms = [p.strip() for p in parts if p.strip()]
    return terms


def _bm25_score(term: str, content_all: dict) -> tuple[int, list[str]]:
    """Wie viele Index-Einträge enthalten den Term? Gibt (count, titles) zurück."""
    term_lower = term.lower()
    # Einfache Token-Suche ohne BM25-Bibliothek (für Standalone-Script)
    tokens = set(re.findall(r"[a-zäöüß0-9]+", term_lower))
    tokens = {t for t in tokens if len(t) >= 3}
    if not tokens:
        return 0, []
    matches = []
    for filename, entry in content_all.items():
        haystack = (
            entry.get("title", "") + " " +
            entry.get("text", "") + " " +
            " ".join(entry.get("steps", []))
        ).lower()
        if all(t in haystack for t in tokens):
            matches.append(entry.get("title", filename))
    return len(matches), matches[:5]


def _suggest_alternatives(term: str, content_all: dict) -> list[str]:
    """Sucht nach ähnlichen Begriffen im Manual-Index."""
    # Zahlen aus dem Term extrahieren
    numbers = re.findall(r"\d+", term)
    if not numbers:
        return []
    suggestions = []
    for number in numbers:
        # Verschiedene Schreibweisen mit dieser Zahl suchen
        all_text = " ".join(_all_texts(content_all))
        patterns_to_try = [
            rf"\b{number}\s*[xX×]\b",
            rf"\b{number}[-\s]fach\b",
            rf"\b{number}\s*m\b",
            rf"\b{number}\s*t\b",
        ]
        for pat in patterns_to_try:
            found = re.findall(pat, all_text, re.I)
            if found:
                example = found[0].strip()
                if example and example.lower() != term.lower():
                    suggestions.append(example)
    return list(dict.fromkeys(suggestions))[:5]  # dedupliziert


def check_context(context: str) -> None:
    content_all = _load_index()
    terms = _extract_terms(context)

    print("=" * 60)
    print("KONTEXT-VALIDIERUNG")
    print("=" * 60)
    print(f"Eingabe: {context}\n")

    ok_count = 0
    warn_count = 0

    for term in terms:
        count, titles = _bm25_score(term, content_all)
        if count >= 3:
            print(f"  ✓  '{term}' — {count} Treffer im Manual")
            ok_count += 1
        elif count > 0:
            print(f"  △  '{term}' — nur {count} Treffer (schwach)")
            print(f"     Fundstellen: {', '.join(titles[:3])}")
            warn_count += 1
        else:
            print(f"  ✗  '{term}' — NICHT im Manual gefunden!")
            alts = _suggest_alternatives(term, content_all)
            if alts:
                print(f"     Alternativen im Manual: {alts}")
            warn_count += 1

    print(f"\nErgebnis: {ok_count} OK, {warn_count} Warnung(en)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        check_context(" ".join(sys.argv[1:]))
    else:
        analyse()
