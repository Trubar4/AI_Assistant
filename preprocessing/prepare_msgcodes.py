"""
prepare_msgcodes.py

Konvertiert die Fehlermeldungs-Tabelle (Excel) → data/msgcodes.json.

Erwartete Spalten (Namen werden automatisch erkannt, Groß-/Kleinschreibung egal):
  MsgCodeHex        — Code, z. B. 0x00000035 (auch "35" oder "0x35" wird erkannt)
  Beschreibung      — Kurztext der Meldung
  Auswirkung        — Auswirkung auf die Maschine
  Problemlösung     — Abhilfemaßnahmen (mehrzeilig)
  Mögliche Ursachen — mögliche Ursachen (mehrzeilig)

Zeilenumbrüche innerhalb der Zellen bleiben erhalten.
Die Codes werden kanonisch als 0x + 8 Hex-Stellen gespeichert (0x00000035).

Verwendung:
  pip install openpyxl
  python preprocessing/prepare_msgcodes.py --src Fehlermeldungen.xlsx
  python preprocessing/prepare_msgcodes.py --src Fehlermeldungen.xlsx --sheet "Tabelle1"

Danach data/msgcodes.json committen — das Backend lädt die Datei beim Start.
"""

import argparse
import json
import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent

# Kandidaten für die Header-Erkennung (Teilstring-Match, lowercase)
HEADER_CANDIDATES = {
    "code":        ("msgcodehex", "msgcode", "hexcode", "code"),
    "description": ("beschreibung", "description", "meldetext", "meldung", "text"),
    "effect":      ("auswirkung", "effect", "reaktion"),
    "solution":    ("problemlösung", "problemloesung", "lösung", "loesung",
                    "abhilfe", "solution", "massnahme", "maßnahme", "remedy"),
    "causes":      ("mögliche ursachen", "moegliche ursachen", "ursache", "cause"),
}


def norm_hex(value) -> str | None:
    """'0x00000035', '0x35', '35', 53 (int) → '0x00000035'. None wenn kein Hex-Code."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Excel kann die Zelle als Zahl interpretiert haben — dann ist der
        # Wert bereits dezimal falsch; wir behandeln Ziffern als Hex-String.
        value = format(int(value), "d")
    s = str(value).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if not re.fullmatch(r"[0-9a-f]{1,8}", s):
        return None
    return "0x" + s.zfill(8)


def _cell(row, idx) -> str:
    if idx is None or idx >= len(row) or row[idx] is None:
        return ""
    return str(row[idx]).strip()


def _detect_header(rows) -> tuple[int, dict]:
    """Sucht in den ersten 15 Zeilen die Header-Zeile und mappt die Spalten."""
    for r_idx, row in enumerate(rows[:15]):
        cells = [str(c).strip().lower() if c is not None else "" for c in row]
        col = {}
        for field, keys in HEADER_CANDIDATES.items():
            for i, cell in enumerate(cells):
                if cell and any(k in cell for k in keys) and i not in col.values():
                    col[field] = i
                    break
        if "code" in col and "description" in col:
            return r_idx, col
    raise ValueError(
        "Header-Zeile nicht gefunden. Erwartet werden Spalten wie "
        "'MsgCodeHex' und 'Beschreibung'. Bitte HEADER_CANDIDATES im Skript "
        "an die tatsächlichen Spaltennamen anpassen."
    )


def from_excel(path: Path, sheet: str | None = None) -> dict:
    try:
        import openpyxl
    except ImportError:
        raise ImportError("openpyxl wird benötigt: pip install openpyxl")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.active
    print(f"Arbeitsblatt: {ws.title}")

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {}

    header_idx, col = _detect_header(rows)
    header = rows[header_idx]
    print("Erkannte Spalten:")
    for field, i in sorted(col.items(), key=lambda kv: kv[1]):
        print(f"  {field:12s} ← Spalte {i + 1}: {header[i]!r}")
    missing = set(HEADER_CANDIDATES) - set(col)
    if missing:
        print(f"WARNUNG: Spalten nicht gefunden (bleiben leer): {', '.join(sorted(missing))}")

    result: dict = {}
    skipped = 0
    for row in rows[header_idx + 1:]:
        code = norm_hex(row[col["code"]] if col["code"] < len(row) else None)
        if code is None:
            skipped += 1
            continue
        entry = {
            "description": _cell(row, col.get("description")),
            "effect":      _cell(row, col.get("effect")),
            "solution":    _cell(row, col.get("solution")),
            "causes":      _cell(row, col.get("causes")),
        }
        if code in result and result[code] != entry:
            print(f"WARNUNG: Duplikat {code} — erste Zeile wird behalten.")
            continue
        result[code] = entry

    if skipped:
        print(f"{skipped} Zeilen ohne gültigen Hex-Code übersprungen (Leerzeilen o. ä.).")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Fehlermeldungs-Excel → data/msgcodes.json"
    )
    parser.add_argument("--src", required=True, help="Quelldatei (.xlsx)")
    parser.add_argument("--sheet", default=None, help="Name des Arbeitsblatts (Standard: aktives Blatt)")
    parser.add_argument(
        "--out",
        default=str(_ROOT / "data" / "msgcodes.json"),
        help="Ausgabepfad (Standard: data/msgcodes.json)",
    )
    args = parser.parse_args()

    src = Path(args.src)
    if not src.exists():
        raise FileNotFoundError(f"Quelldatei nicht gefunden: {src}")

    codes = from_excel(src, sheet=args.sheet)
    if not codes:
        raise SystemExit("Keine Codes gefunden — Abbruch, es wurde nichts geschrieben.")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(codes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(codes)} Meldungen → {out_path}")


if __name__ == "__main__":
    main()
