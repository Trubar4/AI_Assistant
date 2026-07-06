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


def _table_to_markdown(table_html: str) -> str:
    """Convert a single HTML table to a Markdown table, preserving empty cells as '—'."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I)
    md_rows = []
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip() or "—"
                 for c in cells]
        md_rows.append("| " + " | ".join(cells) + " |")
    if len(md_rows) > 1:
        # Insert separator after header row
        col_count = md_rows[0].count("|") - 1
        separator = "|" + "|".join(["---"] * col_count) + "|"
        md_rows.insert(1, separator)
    return "\n".join(md_rows)


def _html_to_text(raw: str) -> str:
    """Convert HTML to readable text, preserving table structure as Markdown."""
    # Replace each table block with its Markdown equivalent before stripping
    def replace_table(m: re.Match) -> str:
        return "\n\n" + _table_to_markdown(m.group(0)) + "\n\n"

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
