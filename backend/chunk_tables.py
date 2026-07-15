"""
chunk_tables.py — Einmaliger Preprocessing-Schritt für große Tabellenseiten.

Parst die Wartungs- und Inspektionsplan-Seite (und künftige ähnliche Seiten)
und erzeugt pro Wartungsintervall einen eigenen Index-Eintrag. Diese Chunks
werden zusätzlich (nicht ersetzend) in content_index.json und
metadata_index.json eingetragen.

Ausführen (nach dem ersten Build oder nach Manual-Update):
    python -m backend.chunk_tables

Danach build_embeddings.py neu ausführen, damit die neuen Einträge
vektorisiert werden.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANUALS_DIR   = ROOT / "manuals"
CONTENT_INDEX = ROOT / "data" / "content_index.json"
METADATA_INDEX = ROOT / "data" / "metadata_index.json"

# Dateien, die in Intervall-Chunks aufgeteilt werden sollen.
# Erweiterbar für künftige Tabellenseiten.
CHUNK_TARGETS = [
    "ID_5dc5858072f74b66af724fb563f3d267-92e6f22468ac45428dfadbacfe729685-de-DE.html",
]

# Chunk-IDs beginnen mit diesem Prefix, damit sie von echten Seiten unterscheidbar sind.
CHUNK_ID_PREFIX = "__chunk__"


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("\xa0", " ").strip()


_SYMBOL_MAP = {
    "●": "Bedienpersonal",
    "■": "Wartungspersonal",
    "○": "Spezialpersonal",
    "□": "Spezialpersonal",
    "✦": "bei_Bedarf_Bedienpersonal",
    "⟡": "bei_Bedarf_Wartungspersonal",
    "✔": "Hauptuntersuchung",
    "❄": "Konservierung",
}


def _interval_id(heading: str) -> str:
    """Erzeugt eine stabile, dateisystem-sichere ID aus der Intervall-Überschrift."""
    # Symbol am Anfang in lesbares Kürzel übersetzen
    prefix = ""
    for sym, name in _SYMBOL_MAP.items():
        if sym in heading:
            prefix = name + "__"
            break
    # Zahl und Einheit extrahieren, z. B. "500 h" → "500h"
    m = re.search(r"([0-9]+)\s*(h|Jahre?|Monat[e]?)", heading, re.I)
    if m:
        return prefix + m.group(1) + m.group(2).replace(" ", "")
    # Freitext-Fallback: alles außer Wörtern entfernen
    clean = re.sub(r"\W+", "_", heading.strip()).strip("_")
    return prefix + (clean or "sonstige")


def _parse_interval_chunks(html: str, filename: str) -> list[dict]:
    """Zerlegt eine Tabellenseite in einen Chunk pro Wartungsintervall."""
    chunks = []
    seen_ids: dict[str, int] = {}    # base_id → Anzahl Vorkommen (für Counter-Suffix)
    seen_texts: set[str] = set()     # Deduplizierung identischer Inhalte

    # Aufteilen nach h2–h6-Überschriften
    segments = re.split(r"(?=<h[2-6][^>]*>)", html)

    for seg in segments:
        hm = re.match(r"<h[2-6][^>]*>(.*?)</h[2-6]>", seg, re.I | re.S)
        if not hm:
            continue
        heading = _strip_tags(hm.group(1))

        # Nur Intervall-Überschriften verarbeiten
        if not re.search(
            r"[0-9]+\s*h|bei Bedarf|Hauptuntersuchung|täglich|wöchentlich|jährlich",
            heading,
            re.I,
        ):
            continue

        # Intervall-Label säubern: führende Whitespace entfernen, Inhalt behalten
        # Symbole (●=Bedienpersonal, ■=Wartungspersonal, ○=Spezialpersonal) bewusst erhalten,
        # da sie unterschiedliche Personalgruppen für dasselbe Intervall kennzeichnen.
        interval_label = heading.strip()

        tables = re.findall(
            r'<table class="taskintervals taskinterval-task".*?</table>',
            seg,
            re.I | re.S,
        )
        for table in tables:
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.I | re.S)
            lines = []
            for row in rows:
                cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.I | re.S)
                cell_texts = [_strip_tags(c) for c in cells if _strip_tags(c)]
                # Kopfzeile überspringen ("Baugruppe" / "Durchzuführende Tätigkeiten")
                if len(cell_texts) >= 2 and cell_texts[0] != "Baugruppe":
                    lines.append(f"{cell_texts[0]}: {cell_texts[1]}")

            if not lines:
                continue

            text_key = "\n".join(lines)
            if text_key in seen_texts:
                continue
            seen_texts.add(text_key)

            # Eindeutige Chunk-ID: Basis-Dateiname + Intervall + Personalgruppe + Counter
            base_id = f"{CHUNK_ID_PREFIX}{filename}__{_interval_id(heading)}"
            count = seen_ids.get(base_id, 0)
            seen_ids[base_id] = count + 1
            chunk_id = base_id if count == 0 else f"{base_id}__{count}"

            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "interval": interval_label,
                    "tasks": lines,
                }
            )

    return chunks


def build_chunks(filename: str, content_entry: dict, meta_entry: dict) -> list[tuple[str, dict, dict]]:
    """Gibt [(chunk_id, content_dict, meta_dict), ...] zurück."""
    html_path = MANUALS_DIR / filename
    if not html_path.exists():
        print(f"  WARNUNG: {filename} nicht gefunden, übersprungen.")
        return []

    html = html_path.read_text(encoding="utf-8", errors="replace")
    raw_chunks = _parse_interval_chunks(html, filename)
    if not raw_chunks:
        print(f"  Keine Intervall-Chunks in {filename} gefunden.")
        return []

    parent_title     = content_entry.get("title", "")
    parent_breadcrumb = content_entry.get("breadcrumb", [])
    parent_topic_type = meta_entry.get("topic_type", "task")

    results = []
    for c in raw_chunks:
        chunk_title = f"{parent_title} — {c['interval']}"
        text = "\n".join(c["tasks"])
        word_count = len(text.split())

        content_dict = {
            "title":      chunk_title,
            "breadcrumb": parent_breadcrumb + [c["interval"]],
            "text":       text,
            "warnings":   [],
            "steps":      c["tasks"][:30],
            "word_count": word_count,
        }
        meta_dict = {
            "title":            chunk_title,
            "topic_type":       parent_topic_type,
            "lifecycle_phases": meta_entry.get("lifecycle_phases", []),
        }
        results.append((c["chunk_id"], content_dict, meta_dict))

    return results


def main() -> None:
    content_all  = json.loads(CONTENT_INDEX.read_text(encoding="utf-8"))
    metadata_all = json.loads(METADATA_INDEX.read_text(encoding="utf-8"))

    # Vorhandene Chunks entfernen (sauberer Neuaufbau bei Wiederholung)
    content_all  = {k: v for k, v in content_all.items()  if not k.startswith(CHUNK_ID_PREFIX)}
    metadata_all = {k: v for k, v in metadata_all.items() if not k.startswith(CHUNK_ID_PREFIX)}

    total = 0
    for filename in CHUNK_TARGETS:
        print(f"Verarbeite: {filename}")
        ce = content_all.get(filename)
        me = metadata_all.get(filename)
        if ce is None or me is None:
            print(f"  Nicht im Index — übersprungen.")
            continue

        chunks = build_chunks(filename, ce, me)
        for chunk_id, c_dict, m_dict in chunks:
            content_all[chunk_id]  = c_dict
            metadata_all[chunk_id] = m_dict
            total += 1
        print(f"  {len(chunks)} Chunks erzeugt.")

    CONTENT_INDEX.write_text(
        json.dumps(content_all, ensure_ascii=False, indent=None),
        encoding="utf-8",
    )
    METADATA_INDEX.write_text(
        json.dumps(metadata_all, ensure_ascii=False, indent=None),
        encoding="utf-8",
    )
    print(f"\nFertig: {total} Chunks in Index geschrieben.")
    print("Nächster Schritt: python -m backend.build_embeddings")


if __name__ == "__main__":
    main()
