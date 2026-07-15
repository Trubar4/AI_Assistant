#!/usr/bin/env python3
"""
copilot_summary_batch.py — Erzeugt Prompt-Batches für M365 Copilot.

Verwendung:
  python scripts/copilot_summary_batch.py [--batch-size 20] [--out summaries.json]
                                           [--start 0] [--end 100]

Ablauf:
  1. Liest alle Manual-HTML-Seiten (ID_*.html)
  2. Teilt sie in Batches auf
  3. Gibt für jeden Batch einen kopierbaren Prompt aus
  4. Liest die Copilot-Antwort (Paste in Terminal), parst JSON
  5. Fügt Ergebnisse in summaries.json ein
  6. Am Ende: vollständige JSON-Datei mit {filename: {title, summary, keywords}}

Ziel-JSON-Format:
  {
    "ID_001926caf7a711ec9dc0c85d66bbc552-....html": {
      "title": "Hauptausleger Montage – Schritt 1",
      "summary": "Beschreibt die Montage des Hauptauslegers in der ersten Phase.",
      "keywords": ["Hauptausleger", "Montage", "Bolzen", "Sicherung"]
    },
    ...
  }
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_MANUALS = _ROOT / "manuals"


# ---------------------------------------------------------------------------
# HTML → Klartext
# ---------------------------------------------------------------------------

def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_title(raw: str) -> str:
    """Versucht den Seitentitel aus <title> oder <h1>/<h2> zu lesen."""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        t = _strip_html(m.group(1)).strip()
        if t:
            return t
    for tag in ("h1", "h2"):
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", raw, re.S | re.I)
        if m:
            t = _strip_html(m.group(1)).strip()
            if t:
                return t
    return ""


def _page_text(path: Path, max_chars: int = 1500) -> tuple[str, str]:
    """Returns (title, text_excerpt)."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    title = _extract_title(raw)
    text = _strip_html(raw)[:max_chars]
    return title, text


# ---------------------------------------------------------------------------
# Prompt-Generierung
# ---------------------------------------------------------------------------

_PROMPT_HEADER = """\
Ich schicke dir Auszüge aus dem Betriebshandbuch eines Liebherr LR 1104 Raupenkrans.
Bitte erstelle für jede Seite eine strukturierte Zusammenfassung im JSON-Format.

Antworte NUR mit einem gültigen JSON-Objekt. Kein Markdown, kein Text davor oder danach.

Format:
{
  "<filename>": {
    "title": "<Seitentitel, 1 Zeile, max 80 Zeichen>",
    "summary": "<1–2 Sätze, was diese Seite beschreibt>",
    "keywords": ["<Begriff1>", "<Begriff2>", "<Begriff3>", "<Begriff4>", "<Begriff5>"]
  },
  ...
}

Seiteninhalt:
"""

_PROMPT_PAGE_TEMPLATE = """\
--- DATEI: {filename} ---
{text}
"""


def _build_prompt(pages: list[tuple[str, str, str]]) -> str:
    """pages: list of (filename, title, text)"""
    parts = [_PROMPT_HEADER]
    for filename, title, text in pages:
        prefix = f"[Titel: {title}]\n" if title else ""
        parts.append(_PROMPT_PAGE_TEMPLATE.format(
            filename=filename,
            text=prefix + text[:1200],
        ))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Copilot-Prompt-Batches für Manual-Zusammenfassungen")
    parser.add_argument("--batch-size", type=int, default=15, help="Seiten pro Batch (Standard: 15)")
    parser.add_argument("--out", default="summaries.json", help="Ausgabe-JSON-Datei (Standard: summaries.json)")
    parser.add_argument("--start", type=int, default=0, help="Erste Seite (0-basierter Index)")
    parser.add_argument("--end",   type=int, default=None, help="Letzte Seite (exklusiv); leer = alle")
    args = parser.parse_args()

    out_path = Path(args.out)
    # Bestehende Zusammenfassungen laden (für Fortführung nach Unterbrechung)
    existing: dict = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            print(f"[INFO] {len(existing)} bestehende Einträge in {out_path} geladen.")
        except Exception as e:
            print(f"[WARN] Konnte {out_path} nicht lesen: {e}")

    # Alle Manual-Seiten einlesen
    all_files = sorted(_MANUALS.glob("ID_*.html"))
    subset = all_files[args.start : args.end]
    # Bereits verarbeitete überspringen
    pending = [f for f in subset if f.name not in existing]
    print(f"[INFO] {len(pending)} Seiten zu verarbeiten (von {len(subset)} insgesamt, {len(subset)-len(pending)} bereits vorhanden).")

    if not pending:
        print("[INFO] Alle Seiten bereits zusammengefasst. Fertig.")
        return

    batch_size = args.batch_size
    batches = [pending[i : i + batch_size] for i in range(0, len(pending), batch_size)]
    print(f"[INFO] {len(batches)} Batches à max. {batch_size} Seiten.\n")

    for batch_num, batch in enumerate(batches, start=1):
        print(f"\n{'='*70}")
        print(f"BATCH {batch_num}/{len(batches)}  ({len(batch)} Seiten)")
        print(f"{'='*70}\n")

        pages = []
        for path in batch:
            title, text = _page_text(path)
            pages.append((path.name, title, text))

        prompt = _build_prompt(pages)

        print(">>> KOPIERE DIESEN PROMPT IN COPILOT:\n")
        print(prompt)
        print("\n>>> ENDE DES PROMPTS")
        print("\nFüge die JSON-Antwort von Copilot hier ein (leere Zeile + ENTER zum Beenden):\n")

        # Mehrzeilige Eingabe lesen bis Leerzeile
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "" and lines:
                # Prüfe ob letzter nicht-leerer Block vollständiges JSON ist
                candidate = "\n".join(lines).strip()
                try:
                    json.loads(candidate)
                    break
                except json.JSONDecodeError:
                    # Noch nicht fertig — weiter lesen
                    lines.append(line)
                    continue
            lines.append(line)

        raw_json = "\n".join(lines).strip()

        # JSON parsen
        # Copilot wickelt manchmal in ```json ... ``` ein — bereinigen
        raw_json = re.sub(r"^```json\s*", "", raw_json, flags=re.M)
        raw_json = re.sub(r"^```\s*$", "", raw_json, flags=re.M)
        raw_json = raw_json.strip()

        try:
            parsed: dict = json.loads(raw_json)
        except json.JSONDecodeError as e:
            print(f"[FEHLER] Ungültiges JSON: {e}")
            print("[INFO] Batch wird übersprungen. Bitte manuell wiederholen.")
            continue

        added = 0
        for filename, entry in parsed.items():
            if isinstance(entry, dict):
                existing[filename] = entry
                added += 1

        # Zwischenspeichern
        out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] {added} Einträge hinzugefügt. Gesamt: {len(existing)}. Gespeichert in {out_path}.")

    print(f"\n[FERTIG] {len(existing)} Zusammenfassungen in {out_path} gespeichert.")


if __name__ == "__main__":
    main()
