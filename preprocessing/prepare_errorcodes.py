"""
prepare_errorcodes.py

Converts an error code source file → data/errorcodes.json.

Supported input formats (auto-detected by extension):
  .xlsx / .xls  — Excel (columns: Code, Beschreibung, Ursache, Massnahme)
  .csv          — CSV  (same columns)
  .json         — Already in {code: {description, cause, action}} format

If no source file is given (or --demo flag is set), a demo dataset with
plausible Liebherr crane error codes is written directly — useful for
development before the real error code documents are available.

Usage:
  python preprocessing/prepare_errorcodes.py               # writes demo data
  python preprocessing/prepare_errorcodes.py --src errorcodes.xlsx
  python preprocessing/prepare_errorcodes.py --src errorcodes.csv
"""

import argparse
import json
from pathlib import Path

_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Demo dataset — plausible codes for a Liebherr LR crawler crane
# ---------------------------------------------------------------------------
DEMO_CODES: dict = {
    "E1001": {
        "description": "Hydraulikdruck zu niedrig",
        "cause": "Hydraulikpumpe verschlissen oder Hydraulikfilter verstopft. Ölstand zu niedrig.",
        "action": "Hydraulikölstand prüfen und ggf. auffüllen. Hydraulikfilter prüfen und wechseln. Bei anhaltendem Fehler Liebherr-Kundendienst kontaktieren.",
    },
    "E1042": {
        "description": "Hydrauliktemperatur zu hoch",
        "cause": "Hydraulikkühler verschmutzt oder defekt. Umgebungstemperatur zu hoch. Überlastbetrieb.",
        "action": "Maschine abkühlen lassen. Hydraulikkühler reinigen. Betriebsbedingungen prüfen.",
    },
    "E1105": {
        "description": "Motoröltemperatur kritisch",
        "cause": "Motorkühlsystem defekt. Kühlmittelstand zu niedrig. Kühler verschmutzt.",
        "action": "Motor sofort abstellen. Kühlmittelstand prüfen. Kühler reinigen. Vor Neustart Ursache beheben.",
    },
    "E1210": {
        "description": "Lastmomentbegrenzer — Überlast",
        "cause": "Zulässige Traglast überschritten. Ausleger-/Auslegerlängenkombination nicht zulässig.",
        "action": "Last reduzieren. Traglasttabelle für aktuelle Konfiguration prüfen. Ausleger-/Einscherkonfiguration anpassen.",
    },
    "E1215": {
        "description": "Lastmomentbegrenzer — Vorwarnung 90 %",
        "cause": "Annäherung an die zulässige Traglast (90 % erreicht).",
        "action": "Achtung — Traglastgrenze nahezu erreicht. Lastaufnahme stoppen oder Last reduzieren.",
    },
    "E2001": {
        "description": "Verteilergetriebe — Öltemperatur zu hoch",
        "cause": "Getriebekühlung unzureichend. Getriebeöl zu wenig oder verschmutzt.",
        "action": "Getriebeölstand prüfen. Ölwechsel durchführen falls überfällig. Kühlung prüfen.",
    },
    "E2045": {
        "description": "Winde 1 — Überdrehzahl",
        "cause": "Bremse schleift oder defekt. Fahrfehler: unkontrolliertes Absenken.",
        "action": "Sofort stoppen. Windenbremsanlage prüfen. Liebherr-Kundendienst kontaktieren.",
    },
    "E2048": {
        "description": "Winde 1 — Endschalter untere Position",
        "cause": "Unterste zulässige Seilposition erreicht.",
        "action": "Haken nicht weiter absenken. Haken einhängen und Winde aufwickeln.",
    },
    "E3010": {
        "description": "CAN-Bus Kommunikationsfehler",
        "cause": "Kabelverbindung unterbrochen. Steuergerät defekt oder Steckverbinder oxidiert.",
        "action": "Kabelbaum und Steckverbinder prüfen. Maschine neu starten. Bei Wiederholung Liebherr-Kundendienst kontaktieren.",
    },
    "E3025": {
        "description": "Neigungssensor — Messwert außerhalb Bereich",
        "cause": "Sensor defekt oder Kabelverbindung unterbrochen. Maschine auf sehr unebenem Untergrund.",
        "action": "Untergrund prüfen und Maschine ggf. ausrichten. Sensor und Kabelverbindung prüfen.",
    },
    "E4001": {
        "description": "Batteriespannung zu niedrig",
        "cause": "Batterie entladen oder defekt. Lichtmaschine defekt.",
        "action": "Batterie laden oder tauschen. Lichtmaschine prüfen.",
    },
    "E4020": {
        "description": "Sicherheitsabschaltung — Notaus betätigt",
        "cause": "Not-Aus-Taster wurde gedrückt.",
        "action": "Ursache für Notabschaltung klären. Not-Aus-Taster entriegeln. Maschine neu starten.",
    },
    "E5001": {
        "description": "Windmesser — Windgeschwindigkeit kritisch",
        "cause": "Windgeschwindigkeit überschreitet zulässigen Grenzwert für den Betrieb.",
        "action": "Betrieb sofort einstellen. Ausleger ablegen oder Maschine in sichere Stellung bringen. Wetterlage beobachten.",
    },
    "E5010": {
        "description": "Auslegerwinkelsensor — Grenzwert überschritten",
        "cause": "Ausleger in unzulässiger Stellung. Sensor defekt.",
        "action": "Ausleger in zulässigen Arbeitsbereich bringen. Winkelsensor prüfen.",
    },
}


# ---------------------------------------------------------------------------
# Parsers for different source formats
# ---------------------------------------------------------------------------

def _from_excel(path: Path) -> dict:
    try:
        import openpyxl
    except ImportError:
        raise ImportError("openpyxl required for Excel import: pip install openpyxl")

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {}

    # Detect header row
    header = [str(c).strip().lower() if c else "" for c in rows[0]]
    col = {
        "code":        next((i for i, h in enumerate(header) if "code" in h), 0),
        "description": next((i for i, h in enumerate(header) if any(k in h for k in ("beschr", "desc", "titel", "title", "text"))), 1),
        "cause":       next((i for i, h in enumerate(header) if any(k in h for k in ("ursach", "cause", "grund"))), 2),
        "action":      next((i for i, h in enumerate(header) if any(k in h for k in ("massnahm", "action", "massnahme", "behebung"))), 3),
    }

    result = {}
    for row in rows[1:]:
        code = str(row[col["code"]]).strip().upper() if row[col["code"]] else None
        if not code:
            continue
        result[code] = {
            "description": str(row[col["description"]] or "").strip(),
            "cause":       str(row[col["cause"]]       or "").strip(),
            "action":      str(row[col["action"]]      or "").strip(),
        }
    return result


def _from_csv(path: Path) -> dict:
    import csv
    result = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keys = {k.strip().lower(): v for k, v in row.items()}
            code = (keys.get("code") or "").strip().upper()
            if not code:
                continue
            result[code] = {
                "description": (keys.get("beschreibung") or keys.get("description") or keys.get("titel") or "").strip(),
                "cause":       (keys.get("ursache") or keys.get("cause") or "").strip(),
                "action":      (keys.get("massnahme") or keys.get("action") or keys.get("massnahmen") or "").strip(),
            }
    return result


def _from_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert error code source → data/errorcodes.json"
    )
    parser.add_argument(
        "--src",
        default=None,
        help="Source file (.xlsx, .csv, .json). Omit to write demo data.",
    )
    parser.add_argument(
        "--out",
        default=str(_ROOT / "data" / "errorcodes.json"),
        help="Output JSON path",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Force write demo data even if --src is given",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.demo or args.src is None:
        codes = DEMO_CODES
        print(f"Writing {len(codes)} demo error codes …")
    else:
        src = Path(args.src)
        if not src.exists():
            raise FileNotFoundError(f"Source file not found: {src}")
        ext = src.suffix.lower()
        if ext in (".xlsx", ".xls"):
            codes = _from_excel(src)
        elif ext == ".csv":
            codes = _from_csv(src)
        elif ext == ".json":
            codes = _from_json(src)
        else:
            raise ValueError(f"Unsupported file type: {ext}")
        print(f"Loaded {len(codes)} codes from {src}")

    out_path.write_text(json.dumps(codes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Output → {out_path}")


if __name__ == "__main__":
    main()
