"""
enrich_index.py — Ergänzt content_index.json um Tabellen-Textinhalte.

Seiten, die primär aus HTML-Tabellen bestehen (word_count < 500), haben oft
kaum durchsuchbaren Fließtext im Index. Dieses Script öffnet die Original-HTML,
extrahiert alle Tabellenzellen (außer bereits durch chunk_tables abgedeckten
Wartungsplan-Tabellen) und hängt den Text an das `text`-Feld an.

Ausführen (nach Manual-Update oder nach chunk_tables.py):
    python -m backend.enrich_index

Danach build_embeddings.py neu ausführen.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANUALS_DIR = ROOT / "manuals"
CONTENT_INDEX = ROOT / "data" / "content_index.json"

# Tabellen-CSS-Klassen, die chunk_tables bereits separat verarbeitet.
# Diese werden hier übersprungen um Duplikate zu vermeiden.
_SKIP_TABLE_CLASSES = {"taskintervals", "taskinterval-task"}

# Seiten mit mehr als diesem Schwellwert haben genug Fließtext — überspringen.
_WORD_COUNT_THRESHOLD = 500

# Chunk-Einträge überspringen (haben kein echtes HTML-Gegenstück)
_CHUNK_PREFIX = "__chunk__"


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("\xa0", " ").strip()


def _has_skip_class(tag: str) -> bool:
    cls_match = re.search(r'class="([^"]*)"', tag)
    if not cls_match:
        return False
    classes = set(cls_match.group(1).split())
    return bool(classes & _SKIP_TABLE_CLASSES)


def _extract_table_text(html: str) -> str:
    """Extrahiert Zellinhalte aus allen Tabellen außer taskintervals-Tabellen."""
    tables = re.findall(r"(<table[^>]*>.*?</table>)", html, re.I | re.S)
    lines = []
    for table_match in tables:
        table_tag = re.match(r"<table[^>]*>", table_match, re.I)
        if table_tag and _has_skip_class(table_tag.group(0)):
            continue
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_match, re.I | re.S)
        for row in rows:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.I | re.S)
            cell_texts = [_strip_tags(c) for c in cells]
            cell_texts = [c for c in cell_texts if c]
            if cell_texts:
                lines.append(" | ".join(cell_texts))
    return "\n".join(lines)


def main() -> None:
    content_all = json.loads(CONTENT_INDEX.read_text(encoding="utf-8"))

    enriched = 0
    skipped_threshold = 0
    skipped_no_file = 0
    skipped_no_tables = 0

    for filename, entry in content_all.items():
        if filename.startswith(_CHUNK_PREFIX):
            continue

        word_count = entry.get("word_count", 0)
        if word_count >= _WORD_COUNT_THRESHOLD:
            skipped_threshold += 1
            continue

        html_path = MANUALS_DIR / filename
        if not html_path.exists():
            skipped_no_file += 1
            continue

        html = html_path.read_text(encoding="utf-8", errors="replace")
        table_text = _extract_table_text(html)

        if not table_text:
            skipped_no_tables += 1
            continue

        existing_text = entry.get("text", "")

        # Nur anhängen wenn der Tabelleninhalt nicht schon im Text enthalten ist
        # (grobe Prüfung: erste 80 Zeichen der ersten Tabellenzeile)
        first_line = table_text.split("\n")[0][:80].lower()
        if first_line and first_line in existing_text.lower():
            skipped_no_tables += 1
            continue

        sep = "\n\n" if existing_text else ""
        entry["text"] = existing_text + sep + table_text
        entry["word_count"] = len(entry["text"].split())
        enriched += 1

    CONTENT_INDEX.write_text(
        json.dumps(content_all, ensure_ascii=False, indent=None),
        encoding="utf-8",
    )

    print(f"Fertig: {enriched} Einträge angereichert.")
    print(f"  Übersprungen (genug Text): {skipped_threshold}")
    print(f"  Übersprungen (kein HTML):  {skipped_no_file}")
    print(f"  Übersprungen (keine neuen Tabellen): {skipped_no_tables}")
    print("Nächster Schritt: python -m backend.build_embeddings")


if __name__ == "__main__":
    main()
