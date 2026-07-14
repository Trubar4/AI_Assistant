"""
prepare_msgcodes.py

Konvertiert die Fehlermeldungs-Tabelle (Excel, Langformat) → data/msgcodes.json.

Erwartete Spalten (Namen werden automatisch erkannt, Groß-/Kleinschreibung egal):
  MsgCodeHex     — Code, z. B. 0x00000004
  Language       — Sprache der Zeile (es wird standardmäßig nur "deutsch" übernommen)
  ActivationText — Beschreibung der Meldung
  TipText        — einzelner Hinweis
  TipType        — Art des Hinweises: Ursache | Problemlösung | Auswirkung

Eine Meldung besteht aus mehreren Zeilen mit gleichem Code. Die Zeilen werden
pro Code gruppiert; TipTexte gleichen Typs werden mit Zeilenumbruch verbunden:

  0x00000004  deutsch  Ölfilter +4A-S57 verschmutzt  Elektronik-Modul defekt  Ursache
  0x00000004  deutsch  Ölfilter +4A-S57 verschmutzt  Verkabelung prüfen       Problemlösung
  ...
  →  {"0x00000004": {"description": "Ölfilter +4A-S57 verschmutzt",
                     "effect": "…", "solution": "…", "causes": "…"}}

Die Codes werden kanonisch als 0x + 8 Hex-Stellen gespeichert (0x00000004).

Verwendung:
  pip install openpyxl
  python preprocessing/prepare_msgcodes.py --src MsgTypes_CrawlerCrane.xlsx
  python preprocessing/prepare_msgcodes.py --src MsgTypes_CrawlerCrane.xlsx --sheet "Tabelle1"
  python preprocessing/prepare_msgcodes.py --src MsgTypes_CrawlerCrane.xlsx --language englisch

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
    "language":    ("language", "sprache"),
    "description": ("activationtext", "beschreibung", "description", "meldetext"),
    "tiptext":     ("tiptext",),
    "tiptype":     ("tiptype",),
}

# TipType → Zielfeld (Teilstring-Match, lowercase)
TIPTYPE_FIELDS = (
    ("ursach", "causes"),          # Ursache, Mögliche Ursachen
    ("lösung", "solution"),        # Problemlösung, Lösung
    ("loesung", "solution"),
    ("problem", "solution"),
    ("auswirkung", "effect"),
)


def norm_hex(value) -> str | None:
    """'0x00000004', '0x4', '4', 4 (int) → '0x00000004'. None wenn kein Hex-Code."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Excel kann die Zelle als Zahl interpretiert haben — wir behandeln
        # die Ziffernfolge als Hex-String.
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
        if "code" in col and "tiptext" in col and "tiptype" in col:
            return r_idx, col
    raise ValueError(
        "Header-Zeile nicht gefunden. Erwartet werden Spalten wie "
        "'MsgCodeHex', 'TipText' und 'TipType'. Bitte HEADER_CANDIDATES im "
        "Skript an die tatsächlichen Spaltennamen anpassen."
    )


def _tip_field(tiptype: str) -> str | None:
    t = tiptype.strip().lower()
    for key, field in TIPTYPE_FIELDS:
        if key in t:
            return field
    return None


def from_excel(path: Path, sheet: str | None = None, language: str = "deutsch") -> dict:
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
    if "language" not in col:
        print("WARNUNG: Language-Spalte nicht gefunden — es werden alle Zeilen übernommen.")

    # Aggregation: code → {"description", "effect": [...], "solution": [...], "causes": [...]}
    agg: dict = {}
    skipped_lang = 0
    skipped_code = 0
    unknown_tiptypes: dict = {}

    for row in rows[header_idx + 1:]:
        code = norm_hex(row[col["code"]] if col["code"] < len(row) else None)
        if code is None:
            skipped_code += 1
            continue
        if "language" in col:
            lang = _cell(row, col["language"]).lower()
            if lang and lang != language.lower():
                skipped_lang += 1
                continue

        entry = agg.setdefault(code, {"description": "", "effect": [], "solution": [], "causes": []})

        desc = _cell(row, col.get("description"))
        if desc:
            if not entry["description"]:
                entry["description"] = desc
            elif entry["description"] != desc:
                print(f"WARNUNG: {code} hat abweichende ActivationTexte — "
                      f"{entry['description']!r} wird behalten, {desc!r} ignoriert.")

        tiptext = _cell(row, col.get("tiptext"))
        tiptype = _cell(row, col.get("tiptype"))
        if not tiptext:
            continue
        field = _tip_field(tiptype)
        if field is None:
            unknown_tiptypes[tiptype] = unknown_tiptypes.get(tiptype, 0) + 1
            continue
        if tiptext not in entry[field]:
            entry[field].append(tiptext)

    if skipped_code:
        print(f"{skipped_code} Zeilen ohne gültigen Hex-Code übersprungen (Leerzeilen o. ä.).")
    if skipped_lang:
        print(f"{skipped_lang} Zeilen anderer Sprachen übersprungen (--language {language}).")
    for t, n in unknown_tiptypes.items():
        print(f"WARNUNG: Unbekannter TipType {t!r} ({n} Zeilen) — nicht übernommen. "
              f"Ggf. TIPTYPE_FIELDS im Skript ergänzen.")

    # Listen → mehrzeilige Strings
    result = {
        code: {
            "description": e["description"],
            "effect":      "\n".join(e["effect"]),
            "solution":    "\n".join(e["solution"]),
            "causes":      "\n".join(e["causes"]),
        }
        for code, e in agg.items()
    }
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Fehlermeldungs-Excel (MsgCodeHex-Langformat) → data/msgcodes.json"
    )
    parser.add_argument("--src", required=True, help="Quelldatei (.xlsx)")
    parser.add_argument("--sheet", default=None, help="Name des Arbeitsblatts (Standard: aktives Blatt)")
    parser.add_argument("--language", default="deutsch",
                        help="Nur Zeilen dieser Sprache übernehmen (Standard: deutsch)")
    parser.add_argument(
        "--out",
        default=str(_ROOT / "data" / "msgcodes.json"),
        help="Ausgabepfad (Standard: data/msgcodes.json)",
    )
    args = parser.parse_args()

    src = Path(args.src)
    if not src.exists():
        raise FileNotFoundError(f"Quelldatei nicht gefunden: {src}")

    codes = from_excel(src, sheet=args.sheet, language=args.language)
    if not codes:
        raise SystemExit("Keine Codes gefunden — Abbruch, es wurde nichts geschrieben.")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(codes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(codes)} Meldungen → {out_path}")


if __name__ == "__main__":
    main()
