"""
ocr_compositions.py — OCR der Zusammenstellungs-Symbole → data/compositions.json

Die "Zusammenstellung des …auslegers"-Seiten zeigen die Auslegerzusammenstellung
je Länge NICHT als Text, sondern als Folge kleiner beschrifteter Symbol-Grafiken
(Kästchen mit "12m", "6m", Keil "10m" = Anlenkstück, Pentagon "7m" = Kopf; dazu
Marker S/N = Einbauposition der Seilführung). Dieses Skript liest die Beschriftung
per OCR aus und legt eine strukturierte Zusammenstellung je Auslegerlänge ab.

Bildauswahl — WELCHE Grafiken OCRt werden (und welche NICHT):
  1. Seitentyp-Filter:  nur Seiten, deren Titel "Zusammenstellung" enthält.
  2. Struktur-Filter:   nur <img> INNERHALB von Tabellenzellen (<td>) der
                        Zusammenstellungs-Tabelle — keine Logos, Warn-Icons,
                        Foto-Abbildungen, Kopf-/Fußzeilen.
  3. Dedup:             jede eindeutige Bilddatei wird nur EINMAL OCRt (die
                        Symbole wiederholen sich massiv über Zeilen/Längen).
  4. Validierungs-Filter: OCR-Ergebnis nur übernehmen, wenn es zum Symbol-Muster
                        passt (eine Länge wie "12m"/"40 ft", optional Marker
                        S/N/X…). Alles andere (Rauschen, Nicht-Symbole) wird
                        verworfen. Ein Foto/Icon OCRt niemals sauber zu "12m".
  5. Plausibilitäts-Check: pro Zeile müssen die Segmentlängen auf die
                        Auslegerlänge der Zeile aufsummieren — sonst wird die
                        Zeile als unsicher markiert. So fliegen OCR-Fehler auf.

So bleibt die OCR auf ~einige Dutzend klar beschriftete Symbole beschränkt,
nicht auf die hunderten dekorativen Bilder pro Seite.

Aufruf (lokal, benötigt Tesseract):
    pip install pytesseract pillow      # + System-Tesseract (apt/brew)
    python -m preprocessing.ocr_compositions
"""

import json
import re
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_MANUALS = _ROOT / "manuals"
_OUT = _ROOT / "data" / "compositions.json"

# Nur Seiten dieses Typs (Filter 1)
_PAGE_TITLE_MARKER = "zusammenstellung"

# Gültiges Symbol-Label (Filter 4): eine Länge in m (ft optional), + Marker
_LEN_RE = re.compile(r"(\d+)\s*m\b", re.I)
_MARKER_RE = re.compile(r"\b([SNX]\d?)\b")


def _title_of(html: str) -> str:
    m = re.search(r"<h2>(.*?)</h2>", html, re.S)
    return re.sub(r"<[^>]+>", " ", m.group(1)).strip() if m else ""


def _boom_of(title: str) -> str:
    t = title.lower()
    if "nadelausleger" in t:
        return "nadelausleger"
    if "hauptausleger" in t:
        return "hauptausleger"
    return "unbekannt"


def _ocr_symbol(img_path: Path, cache: dict) -> dict | None:
    """OCRt ein Symbol-Bild → {'len': int, 'markers': [...]}  oder None.

    Ergebnis wird pro Datei gecacht (Filter 3). Übernahme nur bei gültigem
    Längen-Label (Filter 4)."""
    key = img_path.name
    if key in cache:
        return cache[key]
    import pytesseract
    from PIL import Image

    result = None
    try:
        # Kleines Bild vergrößern → stabilere OCR bei den winzigen Symbolen.
        im = Image.open(img_path).convert("L")
        if im.width < 200:
            im = im.resize((im.width * 3, im.height * 3))
        text = pytesseract.image_to_string(im, config="--psm 6")
        lm = _LEN_RE.search(text.replace(" ", ""))
        if lm:
            result = {"len": int(lm.group(1)),
                      "markers": sorted(set(_MARKER_RE.findall(text)))}
    except Exception as exc:
        print(f"  OCR-Fehler {key}: {exc}")
    cache[key] = result
    return result


def _row_symbols(row_html: str) -> tuple[int | None, list[str]]:
    """(Auslegerlänge, [Bilddateinamen je Zelle]) einer Tabellenzeile.

    Pro Zelle nur die ERSTE <img> (die Seiten doppeln jedes Symbol für eine
    Lightbox — sonst würde alles doppelt gezählt)."""
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
    if not cells:
        return None, []
    m = _LEN_RE.search(re.sub(r"<[^>]+>", " ", cells[0]))
    if not m:
        return None, []
    symbols: list[str] = []
    for c in cells[1:]:
        ims = re.findall(r'<img src="[^"]*?/([^"/]+\.png)"', c)   # Filter 2: img in <td>
        if ims:
            symbols.append(ims[0])
    return int(m.group(1)), symbols


def build() -> dict:
    cache: dict[str, dict | None] = {}
    pages: dict[str, dict] = {}

    for html_file in sorted(_MANUALS.glob("ID_*.html")):
        raw = html_file.read_text(encoding="utf-8", errors="replace")
        title = _title_of(raw)
        if _PAGE_TITLE_MARKER not in title.lower():        # Filter 1
            continue
        table_m = re.search(r'<table class="table">.*?</table>', raw, re.S)
        if not table_m:
            continue

        rows_out: dict[str, list[int]] = {}
        seil_out: dict[str, list[dict]] = {}
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table_m.group(0), re.S):
            length, symbols = _row_symbols(row)
            if length is None or not symbols:
                continue
            seg_lens: list[int] = []
            seil: list[dict] = []
            ok = True
            for idx, fname in enumerate(symbols, 1):
                sym = _ocr_symbol(_MANUALS / "images" / "content" / fname, cache)
                if sym is None:
                    ok = False
                    break
                seg_lens.append(sym["len"])
                for mk in sym["markers"]:               # S = Konfig 1/3, N = Konfig 4
                    if mk in ("S", "N"):
                        seil.append({"marker": mk, "segment_index": idx, "segment_m": sym["len"]})
            # Filter 5: Segmentlängen müssen auf die Auslegerlänge summieren.
            if ok and sum(seg_lens) == length:
                rows_out[str(length)] = seg_lens
                if seil:
                    seil_out[str(length)] = seil
            else:
                print(f"  {html_file.name} {length} m: Plausibilität verfehlt "
                      f"(Summe {sum(seg_lens)} ≠ {length}) — Zeile übersprungen")

        if rows_out:
            pages[html_file.name] = {
                "title": title,
                "boom": _boom_of(title),
                "rows": rows_out,
                "seilfuehrung": seil_out,   # S/N → Position + Segmentlänge
            }
            print(f"✓ {title}: {len(rows_out)} Auslegerlängen")

    data = {"_note": "Zusammenstellungs-Symbole per OCR; Zeilen plausibilisiert "
                     "(Segmentsumme == Auslegerlänge).", "pages": pages}
    _OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {_OUT}  ({len(pages)} Seiten)")
    return data


if __name__ == "__main__":
    build()
