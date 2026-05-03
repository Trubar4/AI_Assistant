"""
extract_errorcodes.py

Scans Liebherr HTML manual pages for error codes -> data/errorcodes.json.

Three extraction strategies (in priority order):
  1. Table extraction  -- finds 2-6 column tables whose first column looks like
                          error codes (digits or short letter+digit codes).
  2. Definition lists  -- <dl><dt>code</dt><dd>description</dd></dl> patterns.
  3. Regex fallback    -- inline "1042 : Hydrauliktemperatur zu hoch" patterns;
                          only applied on TOC-identified error pages to reduce noise.

TOC-guided filtering: pages whose title matches error/alarm keywords are
scanned with all three strategies. All other pages only get table extraction.

Usage:
    python -m preprocessing.extract_errorcodes
    python -m preprocessing.extract_errorcodes --manuals path/to/manuals
    python -m preprocessing.extract_errorcodes --merge        # keep existing
    python -m preprocessing.extract_errorcodes --debug        # show table details
    python -m preprocessing.extract_errorcodes --dump-page ID_abc...html
"""

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

_ROOT = Path(__file__).parent.parent

# Safe print that never crashes on Windows charmap
def _print(*args, **kwargs):
    text = " ".join(str(a) for a in args)
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        safe = text.encode(sys.stdout.encoding or "ascii", errors="replace").decode(
            sys.stdout.encoding or "ascii"
        )
        print(safe, **kwargs)


# ---------------------------------------------------------------------------
# Page classification
# ---------------------------------------------------------------------------

_ERROR_PAGE_RE = re.compile(
    r"Fehlermeldung|Betriebsstoerung|Betriebsst.rung|Fehleranzeige|Fehlercode|"
    r"Fehler am Bildschirm|Alarm|Diagno",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Code pattern -- 0-2 letters, optional separator, 3-5 digits, optional suffix
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"^[A-Za-z]{0,2}[-.\s]?\d{3,5}[A-Za-z]?$")


def _is_code(text: str) -> bool:
    t = text.strip()
    return bool(_CODE_RE.match(t)) and len(t) <= 10


def _is_text_description(text: str) -> bool:
    """Return True when text looks like a natural-language description.

    Rejects pure measurements ('1140 mm 3.74 ft') and part numbers.
    Real Liebherr error descriptions always start with a letter.
    """
    t = text.strip()
    return bool(t) and t[0].isalpha()


def _normalise_code(raw: str) -> str:
    """Remove internal whitespace, uppercase."""
    return re.sub(r"\s+", "", raw).upper()


# ---------------------------------------------------------------------------
# Indicator page classification
# ---------------------------------------------------------------------------

_INDICATOR_RE = re.compile(
    r"Rote Fehleranzeige|Gelbe Warnanzeige|Fehler am Hochvoltsystem",
    re.IGNORECASE,
)

_REF_RE = re.compile(r"\s*\(Weitere Informationen[^)]*\)", re.IGNORECASE)


def _indicator_prefix(title: str) -> str | None:
    """Return code prefix for indicator/warning pages, or None."""
    t = title.lower()
    if "rote fehleranzeige" in t:
        return "R"
    if "gelbe warnanzeige" in t:
        return "G"
    if "hochvoltsystem" in t:
        return "HV"
    return None


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

_SKIP_HEADER_KW = [
    "baugruppe", "taetigkeit", "tätigkeit", "tätigkeiten", "intervall",
    "abmessung", "masse", "maße", "dimension",
]

_LABEL_HEADER_KW = ["code", "fehler", "nr.", "nummer", "meldung", "ursache"]


def _from_tables(soup: BeautifulSoup, filename: str,
                 debug: bool = False) -> dict[str, dict]:
    """Extract error codes from HTML tables."""
    results: dict[str, dict] = {}

    for t_idx, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        parsed: list[list[str]] = []
        for row in rows:
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
            if cells:
                parsed.append(cells)

        if not parsed:
            continue

        ncols = max(len(r) for r in parsed)

        if debug:
            # Show ALL tables on error pages so we can understand the structure
            h = parsed[0][:4]
            s = parsed[1][:4] if len(parsed) > 1 else []
            _print(f"    [table {t_idx}] {ncols} cols x {len(parsed)} rows")
            _print(f"       header:  {h}")
            _print(f"       row[1]:  {s}")

        if ncols < 2 or ncols > 6:
            if debug:
                _print(f"       => SKIP ncols={ncols} out of range")
            continue

        header_text = " ".join(parsed[0]).lower()

        if any(kw in header_text for kw in _SKIP_HEADER_KW):
            if debug:
                _print(f"       => SKIP blacklist header keyword")
            continue

        start = 1 if any(kw in header_text for kw in _LABEL_HEADER_KW) else 0
        data_rows = parsed[start:]
        if not data_rows:
            continue

        code_rows = [r for r in data_rows if r and _is_code(r[0])]
        if len(code_rows) < 2:
            if debug:
                col0 = [r[0] for r in data_rows[:4] if r]
                _print(f"       => SKIP code_hits={len(code_rows)}<2 | col0: {col0}")
            continue

        if debug:
            samples = [(r[0], r[1] if len(r) > 1 else "") for r in code_rows[:3]]
            _print(f"       => CANDIDATE code_hits={len(code_rows)} | samples: {samples}")

        accepted = 0
        for row in data_rows:
            if not row or not row[0].strip():
                continue
            if not _is_code(row[0]):
                continue

            code = _normalise_code(row[0])
            if ncols >= 4:
                desc   = row[1].strip() if len(row) > 1 else ""
                cause  = row[2].strip() if len(row) > 2 else ""
                action = row[3].strip() if len(row) > 3 else ""
            elif ncols == 3:
                desc   = row[1].strip() if len(row) > 1 else ""
                cause  = ""
                action = row[2].strip() if len(row) > 2 else ""
            else:
                desc   = row[1].strip() if len(row) > 1 else ""
                cause  = ""
                action = ""

            if code and desc and _is_text_description(desc):
                results[code] = {
                    "description": desc,
                    "cause":       cause,
                    "action":      action,
                    "_source":     filename,
                }
                accepted += 1

        if debug:
            _print(f"       => accepted {accepted} / {len(code_rows)} rows")

    return results


def _from_indicator_tables(soup: BeautifulSoup, filename: str,
                            prefix: str, counter: list) -> dict[str, dict]:
    """Extract warning/error indicators from Liebherr indicator pages.

    Liebherr DITA HTML export structure:
      Row 0: ['Ursache', 'Abhilfe', ...]   header
      Row 1: [description, combined_action, '', action_1, ...]
      Row 2+: ['', individual_action_step]

    Targets only the main tables (ncols >= 4) to skip the small
    2-col fragment tables that duplicate individual action items.
    """
    results: dict[str, dict] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        parsed: list[list[str]] = []
        for row in rows:
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
            if cells:
                parsed.append(cells)

        if not parsed or len(parsed) < 2:
            continue

        ncols = max(len(r) for r in parsed)
        if ncols < 4:
            continue  # skip 2-col fragment tables

        # Must have 'Ursache' in col-0 of header row
        if parsed[0][0].strip().lower() != "ursache":
            continue

        description = parsed[1][0].strip() if parsed[1] else ""
        if not description or not description[0].isalpha():
            continue

        # Collect unique action steps from col-1 of rows 2+
        seen: set[str] = set()
        steps: list[str] = []
        for row in parsed[2:]:
            if len(row) > 1 and row[1].strip():
                step = _REF_RE.sub("", row[1]).strip().rstrip(" .")
                if step and step not in seen:
                    seen.add(step)
                    steps.append(step)

        # Fallback: use col-1 of data row
        if not steps and len(parsed[1]) > 1 and parsed[1][1].strip():
            raw = _REF_RE.sub("", parsed[1][1]).strip()
            if raw:
                steps = [raw]

        action = ". ".join(steps) + ("." if steps else "")

        code = f"{prefix}-{counter[0]:03d}"
        counter[0] += 1
        results[code] = {
            "description": description,
            "cause":       "",
            "action":      action,
            "_source":     filename,
        }

    return results


def _from_deflists(soup: BeautifulSoup, filename: str) -> dict[str, dict]:
    """Extract codes from <dl><dt>code</dt><dd>description</dd> patterns."""
    results: dict[str, dict] = {}

    for dl in soup.find_all("dl"):
        items = dl.find_all(["dt", "dd"])
        i = 0
        while i < len(items):
            if items[i].name == "dt":
                code_raw = items[i].get_text(" ", strip=True)
                if _is_code(code_raw):
                    code = _normalise_code(code_raw)
                    desc = ""
                    if i + 1 < len(items) and items[i + 1].name == "dd":
                        desc = items[i + 1].get_text(" ", strip=True)
                        i += 1
                    if code and desc:
                        results[code] = {
                            "description": desc,
                            "cause":       "",
                            "action":      "",
                            "_source":     filename,
                        }
            i += 1

    return results


def _from_regex(soup: BeautifulSoup, filename: str,
                debug: bool = False) -> dict[str, dict]:
    """
    Regex fallback -- matches patterns like:
      1042 : Hydrauliktemperatur zu hoch
      E1042 - Hydrauliktemperatur zu hoch
    Only used on TOC-identified error pages to limit false positives.
    """
    results: dict[str, dict] = {}
    text = soup.get_text("\n", strip=True)

    if debug:
        # Show first lines that contain 3+ digit numbers to understand format
        digit_lines = [l.strip() for l in text.splitlines()
                       if re.search(r'\d{3,5}', l) and l.strip()][:10]
        _print(f"    [regex] first lines with 3+ digit numbers:")
        for l in digit_lines:
            _print(f"       {l[:100]}")

    for m in re.finditer(
        r"(?:^|\n)\s*([A-Za-z]{0,2}[-.\s]?\d{3,5}[A-Za-z]?)\s*[:\s–-]+\s*([^\n]{10,250})",
        text,
        re.MULTILINE,
    ):
        code_raw = m.group(1).strip()
        if not _is_code(code_raw):
            continue
        code = _normalise_code(code_raw)
        desc = m.group(2).strip()
        if code and desc and _is_text_description(desc) and code not in results:
            results[code] = {
                "description": desc,
                "cause":       "",
                "action":      "",
                "_source":     filename,
            }

    if debug:
        _print(f"    [regex] found {len(results)} code(s)")

    return results


# ---------------------------------------------------------------------------
# Main extraction loop
# ---------------------------------------------------------------------------

def dump_page(manuals_dir: Path, filename: str) -> None:
    """Print the raw text and table structure of a single HTML page."""
    path = manuals_dir / filename
    if not path.exists():
        _print(f"File not found: {path}")
        return

    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")
    text = soup.get_text("\n", strip=True)

    _print(f"\n=== TEXT CONTENT (first 3000 chars) ===")
    _print(text[:3000])

    _print(f"\n=== TABLES ({len(soup.find_all('table'))} total) ===")
    for i, table in enumerate(soup.find_all("table")):
        rows = table.find_all("tr")
        parsed = []
        for row in rows:
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
            if cells:
                parsed.append(cells)
        if not parsed:
            continue
        ncols = max(len(r) for r in parsed)
        _print(f"\n[table {i}] {ncols} cols x {len(parsed)} rows")
        for r in parsed[:5]:
            _print(f"  {r[:5]}")
        if len(parsed) > 5:
            _print(f"  ... ({len(parsed)-5} more rows)")


def extract(manuals_dir: Path, toc_path: Path,
            debug: bool = False) -> dict[str, dict]:
    toc_titles: dict[str, str] = {}
    if toc_path.exists():
        for entry in json.loads(toc_path.read_text(encoding="utf-8")):
            toc_titles[entry["filename"]] = entry.get("title", "")

    all_codes: dict[str, dict] = {}
    html_files = sorted(manuals_dir.glob("*.html"))

    if not html_files:
        _print(f"No HTML files found in {manuals_dir}")
        return all_codes

    _print(f"Scanning {len(html_files)} HTML file(s) in {manuals_dir} ...\n")

    error_pages_seen = 0
    ind_counters: dict[str, list] = {}  # prefix -> [counter]

    for html_file in html_files:
        filename = html_file.name
        title = toc_titles.get(filename, "")
        is_error_page = bool(_ERROR_PAGE_RE.search(title))
        if is_error_page:
            error_pages_seen += 1

        ind_prefix = _indicator_prefix(title)

        try:
            soup = BeautifulSoup(
                html_file.read_text(encoding="utf-8", errors="replace"), "lxml"
            )
        except Exception as exc:
            _print(f"  [SKIP] {filename}: {exc}")
            continue

        if debug and is_error_page:
            _print(f"\n  [ERROR PAGE] {filename[:70]}")
            _print(f"    title: {title!r}")

        found: dict[str, dict] = {}
        found.update(_from_deflists(soup, filename))
        found.update(_from_tables(soup, filename, debug=debug and is_error_page))

        if is_error_page:
            regex_hits = _from_regex(soup, filename, debug=debug)
            for code, entry in regex_hits.items():
                if code not in found:
                    found[code] = entry

        # Indicator extraction (Rote/Gelbe Fehleranzeigen pages)
        if ind_prefix:
            if ind_prefix not in ind_counters:
                ind_counters[ind_prefix] = [1]
            ind_hits = _from_indicator_tables(
                soup, filename, ind_prefix, ind_counters[ind_prefix]
            )
            for code, entry in ind_hits.items():
                if code not in found:
                    found[code] = entry

        if found:
            label = f" [{ind_prefix}]" if ind_prefix else (" [error page]" if is_error_page else "")
            _print(f"  {filename[:60]}{label}")
            _print(f"    -> {len(found)} code(s): {', '.join(sorted(found)[:6])}"
                   + ("..." if len(found) > 6 else ""))

        all_codes.update(found)

    if debug:
        _print(f"\nTOC-matched error pages : {error_pages_seen}")

    return all_codes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract error codes from HTML manuals -> data/errorcodes.json"
    )
    parser.add_argument("--manuals", default=str(_ROOT / "manuals"),
        help="Directory containing HTML manual files (default: repo/manuals)")
    parser.add_argument("--toc", default=str(_ROOT / "data" / "toc_index.json"),
        help="Path to toc_index.json (default: data/toc_index.json)")
    parser.add_argument("--out", default=str(_ROOT / "data" / "errorcodes.json"),
        help="Output path (default: data/errorcodes.json)")
    parser.add_argument("--merge", action="store_true",
        help="Merge with existing errorcodes.json; HTML data wins on conflict")
    parser.add_argument("--debug", action="store_true",
        help="Print full table diagnostics for TOC-identified error pages")
    parser.add_argument("--dump-page", metavar="FILENAME",
        help="Dump text and table structure of a single HTML file, then exit")
    args = parser.parse_args()

    manuals_dir = Path(args.manuals)
    toc_path    = Path(args.toc)
    out_path    = Path(args.out)

    if args.dump_page:
        dump_page(manuals_dir, args.dump_page)
        return

    extracted = extract(manuals_dir, toc_path, debug=args.debug)

    if args.merge and out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        merged = {**existing, **extracted}
        _print(f"\nMerge: {len(existing)} existing + {len(extracted)} new -> {len(merged)} total")
        output = merged
    else:
        output = extracted

    clean = {
        code: {k: v for k, v in entry.items() if not k.startswith("_")}
        for code, entry in sorted(output.items())
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _print(f"\nTotal codes written : {len(clean)}")
    _print(f"Output              -> {out_path}")

    if len(clean) == 0:
        _print(
            "\nHint: 0 codes found."
            "\n  1. Run --debug to see all table structures on error pages."
            "\n  2. Run --dump-page <filename> to see raw content of one page."
            "\n     e.g.: python -m preprocessing.extract_errorcodes"
            "\n           --dump-page ID_38ece9c5...html"
        )


if __name__ == "__main__":
    main()
