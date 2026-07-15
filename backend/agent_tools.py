"""
agent_tools.py — Tool-Implementierungen für den Agentic-RAG-Loop.

Tools:
  search(query)          — BM25 + Semantic, gibt top-N Kandidaten zurück
  read_page(filename)    — vollständigen Text einer Manual-Seite lesen
  grep_manual(pattern)   — Regex-Suche über alle 2000 HTML-Dateien
  bal_search(keywords)   — BAL-eigenen Suchindex durchsuchen (alphabetische Titelliste)
"""

import html
import json
import re
from pathlib import Path

_ROOT      = Path(__file__).parent.parent
_MANUALS   = _ROOT / "manuals"
_BAL_JS    = _MANUALS / "search-de-de.js"

# ---------------------------------------------------------------------------
# BAL-Suchindex — einmalig beim Import parsen
# ---------------------------------------------------------------------------
_BAL_ENTRIES: list[tuple[str, str]] = []   # [(filename, title), ...]

def _load_bal_index() -> None:
    global _BAL_ENTRIES
    if _BAL_ENTRIES:
        return
    if not _BAL_JS.exists():
        return
    raw = _BAL_JS.read_text(encoding="utf-8", errors="replace")
    entries = re.findall(r'href="([^"]+)"[^>]*><span>([^<]+)</span>', raw)
    _BAL_ENTRIES = [
        (fname, html.unescape(title).replace("\xa0", " ").replace("\\'", "'"))
        for fname, title in entries
    ]

_load_bal_index()


def _extract_cells(row_html: str) -> list[tuple[str, int]]:
    """Return list of (text, colspan) for each cell in a table row."""
    result = []
    for tag, content in re.findall(r"(<t[dh][^>]*>)(.*?)</t[dh]>", row_html, re.S | re.I):
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", content))).strip() or "—"
        colspan_m = re.search(r'colspan=["\']?(\d+)["\']?', tag, re.I)
        result.append((text, int(colspan_m.group(1)) if colspan_m else 1))
    return result


def _table_to_text(table_html: str) -> str:
    """Convert an HTML table to annotated key=value rows so no column counting is needed.

    Each data row is emitted as:
      ROW <row_label>: <col_header_1>=<value> | <col_header_2>=<value> | ...

    This eliminates off-by-one errors from multi-row or colspan headers.
    Falls back to a simple pipe-delimited table when no clear header is found.
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I)
    if not rows:
        return ""

    # Collect all rows as flat cell lists (expanding colspan)
    parsed: list[list[str]] = []
    for row in rows:
        cells_with_span = _extract_cells(row)
        flat: list[str] = []
        for text, span in cells_with_span:
            flat.append(text)
            flat.extend([""] * (span - 1))
        parsed.append(flat)

    if not parsed:
        return ""

    # Use the first row as column headers; skip any subsequent all-th sub-header rows
    headers = parsed[0]
    n_cols = len(headers)

    # Find data rows: rows that aren't sub-headers (have real values, not all —/empty)
    data_rows: list[list[str]] = []
    for row_cells in parsed[1:]:
        # Pad or trim to match header count
        row_cells = (row_cells + ["—"] * n_cols)[:n_cols]
        row_cells = [c if c else "—" for c in row_cells]
        # Skip rows that look like sub-headers (no numeric/kg/m content)
        content = " ".join(row_cells)
        if not re.search(r'\d', content):
            continue
        data_rows.append(row_cells)

    if not data_rows:
        # Fallback: plain markdown
        md = ["| " + " | ".join(headers) + " |",
              "|" + "|".join(["---"] * n_cols) + "|"]
        for r in parsed[1:]:
            r = (r + ["—"] * n_cols)[:n_cols]
            md.append("| " + " | ".join(c or "—" for c in r) + " |")
        return "\n".join(md)

    # Annotated format: ROW <label>: col2_header=value | col3_header=value | ...
    row_label_header = headers[0] if headers else "Row"
    col_headers = headers[1:]  # skip row-label column
    lines = [f"[Tabelle: {row_label_header} × {' / '.join(col_headers[:6])}{'...' if len(col_headers) > 6 else ''}]"]
    for row_cells in data_rows:
        label = row_cells[0]
        values = row_cells[1:]
        parts = [f"{col_headers[i] if i < len(col_headers) else f'col{i+2}'}={values[i] if i < len(values) else '—'}"
                 for i in range(len(col_headers))]
        lines.append(f"  {label}: " + " | ".join(parts))
    return "\n".join(lines)


def _html_to_text(raw: str) -> str:
    """Convert HTML to readable text, preserving table structure as annotated key=value rows."""
    def replace_table(m: re.Match) -> str:
        return "\n\n" + _table_to_text(m.group(0)) + "\n\n"

    processed = re.sub(r"<table[^>]*>.*?</table>", replace_table, raw, flags=re.S | re.I)
    processed = re.sub(r"<[^>]+>", " ", processed)
    processed = html.unescape(processed)
    processed = re.sub(r" {2,}", " ", processed)
    processed = re.sub(r"\n{3,}", "\n\n", processed)
    return processed.strip()


def _strip_html(text: str) -> str:
    """Plain text extraction (used for search index snippets)."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Tool: search
# ---------------------------------------------------------------------------

def search(query: str, top_n: int = 15) -> list[dict]:
    """BM25 + Semantic search. Returns top_n candidates with title, filename, score, snippet."""
    from backend.search import search as _search
    results = _search(query, top_n=top_n)
    return [
        {
            "filename": r["filename"],
            "title":    r["title"],
            "score":    r.get("score", 0),
            "snippet":  (r.get("text") or " ".join(r.get("steps", [])))[:200],
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# Tool: read_page
# ---------------------------------------------------------------------------

def read_page(filename: str, max_chars: int = 6000) -> dict:
    """Read the full text content of a manual HTML page."""
    path = _MANUALS / filename
    if not path.exists():
        return {"error": f"Datei nicht gefunden: {filename}"}
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = _html_to_text(raw)
    return {
        "filename": filename,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


# ---------------------------------------------------------------------------
# Tool: grep_manual
# ---------------------------------------------------------------------------

def grep_manual(pattern: str, max_results: int = 10) -> list[dict]:
    """Case-insensitive regex search across all HTML files. Returns matching files + context."""
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        return [{"error": f"Ungültiges Regex-Muster: {e}"}]

    results = []
    for html_file in _MANUALS.glob("ID_*.html"):
        try:
            raw = html_file.read_text(encoding="utf-8", errors="replace")
            text = _strip_html(raw)
            m = rx.search(text)
            if m:
                start = max(0, m.start() - 80)
                snippet = text[start: m.end() + 80].strip()
                results.append({
                    "filename": html_file.name,
                    "snippet":  snippet,
                })
        except Exception:
            continue
        if len(results) >= max_results:
            break

    return results


# ---------------------------------------------------------------------------
# Tool: bal_search
# ---------------------------------------------------------------------------

def bal_search(keywords: str, max_results: int = 10) -> list[dict]:
    """Search the BAL's built-in title index (2053 entries, alphabetically sorted).

    Splits keywords and returns titles where ALL keywords appear (case-insensitive).
    Falls back to ANY keyword match if nothing found.
    """
    if not _BAL_ENTRIES:
        return [{"error": "BAL-Suchindex nicht verfügbar."}]

    tokens = [t.lower() for t in re.split(r"\s+", keywords.strip()) if t]
    if not tokens:
        return []

    # ALL-match pass
    results = [
        {"filename": fname, "title": title}
        for fname, title in _BAL_ENTRIES
        if all(t in title.lower() for t in tokens)
    ]
    # ANY-match fallback
    if not results:
        results = [
            {"filename": fname, "title": title}
            for fname, title in _BAL_ENTRIES
            if any(t in title.lower() for t in tokens)
        ]

    return results[:max_results]


# ---------------------------------------------------------------------------
# Tool-Schemas für Claude tool_use API
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "search",
        "description": (
            "Durchsucht den Manual-Index mit BM25 + Semantic Search. "
            "Gibt die relevantesten Seiten mit Titel, Dateiname und Textauszug zurück. "
            "Nutze dies als ersten Schritt bei jeder Frage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Suchanfrage auf Deutsch, möglichst spezifisch"},
                "top_n": {"type": "integer", "description": "Anzahl Ergebnisse (Standard: 15)", "default": 15},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_page",
        "description": (
            "Liest den vollständigen Text einer Manual-Seite. "
            "Nutze dies wenn der Suchausschnitt nicht reicht oder du Tabellenwerte brauchst."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "HTML-Dateiname aus den Suchergebnissen"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "grep_manual",
        "description": (
            "Regex-Suche im Volltext aller 2000 HTML-Dateien. "
            "Nutze dies für exakte Werte (z. B. '6x', '74 m', Teilenummern) "
            "die im Index-Snippet nicht sichtbar sind."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python-Regex, z. B. r'6\\s*x.*einscherung' oder '74\\s*m'"},
                "max_results": {"type": "integer", "description": "Max. Treffer (Standard: 10)", "default": 10},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "bal_search",
        "description": (
            "Durchsucht den eingebauten Suchindex der Bedienungsanleitung (2053 Seitentitel). "
            "Schnell für exakte Seitentitel-Treffer. Nutze dies als Kreuzcheck oder "
            "wenn du einen genauen Seitentitel kennst."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "Suchbegriffe (Leerzeichen-getrennt), z. B. 'Hauptausleger Montage'"},
                "max_results": {"type": "integer", "description": "Max. Treffer (Standard: 10)", "default": 10},
            },
            "required": ["keywords"],
        },
    },
]

# Dispatch-Map für den Agent-Loop
TOOL_FN = {
    "search":      search,
    "read_page":   read_page,
    "grep_manual": grep_manual,
    "bal_search":  bal_search,
}
