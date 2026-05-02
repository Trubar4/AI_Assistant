"""
extract_errorcodes.py

Scans Liebherr HTML manual pages for error codes → data/errorcodes.json.

Three extraction strategies (in priority order):
  1. Table extraction  — finds 2-5 column tables whose first column looks like
                         error codes (digits or short letter+digit codes).
  2. Definition lists  — <dl><dt>code</dt><dd>description</dd></dl> patterns.
  3. Regex fallback    — inline "1042 : Hydrauliktemperatur zu hoch" patterns;
                         only applied on TOC-identified error pages to reduce noise.

TOC-guided filtering: pages whose title matches error/alarm keywords are
scanned with all three strategies. All other pages only get table extraction.

Usage:
    python -m preprocessing.extract_errorcodes
    python -m preprocessing.extract_errorcodes --manuals path/to/manuals
    python -m preprocessing.extract_errorcodes --merge   # keep existing entries
"""

import argparse
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Page classification
# ---------------------------------------------------------------------------

_ERROR_PAGE_RE = re.compile(
    r"Fehlermeldung|Betriebsstörung|Fehleranzeige|Fehlercode|"
    r"Fehler am Bildschirm|Alarm|Diagno",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Code pattern — 0–2 letters, optional separator, 3–5 digits, optional suffix
# ---------------------------------------------------------------------------

_CODE_RE = re.compile(r"^[A-Za-z]{0,2}[-.\s]?\d{3,5}[A-Za-z]?$")


def _is_code(text: str) -> bool:
    t = text.strip()
    return bool(_CODE_RE.match(t)) and len(t) <= 10


def _normalise_code(raw: str) -> str:
    """Remove internal whitespace, uppercase."""
    return re.sub(r"\s+", "", raw).upper()


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _from_tables(soup: BeautifulSoup, filename: str) -> dict[str, dict]:
    """Extract error codes from HTML tables."""
    results: dict[str, dict] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Collect all cell texts per row
        parsed: list[list[str]] = []
        for row in rows:
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
            if cells:
                parsed.append(cells)

        if not parsed:
            continue

        ncols = max(len(r) for r in parsed)
        if ncols < 2 or ncols > 6:
            continue

        # Skip header row if first row looks like column labels
        start = 0
        header_text = " ".join(parsed[0]).lower()
        if any(kw in header_text for kw in ["code", "fehler", "nr.", "nummer", "meldung", "ursache"]):
            start = 1

        data_rows = parsed[start:]
        if not data_rows:
            continue

        # Require at least 2 rows where column 0 looks like a code
        code_hits = sum(1 for r in data_rows if r and _is_code(r[0]))
        if code_hits < 2:
            continue

        # Map columns to roles
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

            if code and desc:
                results[code] = {
                    "description": desc,
                    "cause":       cause,
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


def _from_regex(soup: BeautifulSoup, filename: str) -> dict[str, dict]:
    """
    Regex fallback — matches patterns like:
      1042 : Hydrauliktemperatur zu hoch
      E1042 – Hydrauliktemperatur zu hoch
    Only used on TOC-identified error pages to limit false positives.
    """
    results: dict[str, dict] = {}
    text = soup.get_text("\n", strip=True)

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
        if code and desc and code not in results:
            results[code] = {
                "description": desc,
                "cause":       "",
                "action":      "",
                "_source":     filename,
            }

    return results


# ---------------------------------------------------------------------------
# Main extraction loop
# ---------------------------------------------------------------------------

def extract(manuals_dir: Path, toc_path: Path) -> dict[str, dict]:
    # Build TOC lookup: filename → title
    toc_titles: dict[str, str] = {}
    if toc_path.exists():
        for entry in json.loads(toc_path.read_text(encoding="utf-8")):
            toc_titles[entry["filename"]] = entry.get("title", "")

    all_codes: dict[str, dict] = {}
    html_files = sorted(manuals_dir.glob("*.html"))

    if not html_files:
        print(f"No HTML files found in {manuals_dir}")
        return all_codes

    print(f"Scanning {len(html_files)} HTML file(s) in {manuals_dir} …\n")

    for html_file in html_files:
        filename = html_file.name
        title = toc_titles.get(filename, "")
        is_error_page = bool(_ERROR_PAGE_RE.search(title))

        try:
            soup = BeautifulSoup(
                html_file.read_text(encoding="utf-8", errors="replace"), "lxml"
            )
        except Exception as exc:
            print(f"  [SKIP] {filename}: {exc}")
            continue

        # Always try table + deflist extraction
        found: dict[str, dict] = {}
        found.update(_from_deflists(soup, filename))
        found.update(_from_tables(soup, filename))

        # Regex fallback only on identified error pages (too noisy otherwise)
        if is_error_page:
            regex_hits = _from_regex(soup, filename)
            # Regex only fills gaps, not overwrite structured data
            for code, entry in regex_hits.items():
                if code not in found:
                    found[code] = entry

        if found:
            label = " [error page]" if is_error_page else ""
            print(f"  {filename[:60]}{label}")
            print(f"    → {len(found)} code(s): {', '.join(sorted(found)[:6])}"
                  + ("…" if len(found) > 6 else ""))

        # Later files win on conflict (more specific pages override earlier)
        all_codes.update(found)

    return all_codes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract error codes from HTML manuals → data/errorcodes.json"
    )
    parser.add_argument(
        "--manuals",
        default=str(_ROOT / "manuals"),
        help="Directory containing HTML manual files (default: repo/manuals)",
    )
    parser.add_argument(
        "--toc",
        default=str(_ROOT / "data" / "toc_index.json"),
        help="Path to toc_index.json (default: data/toc_index.json)",
    )
    parser.add_argument(
        "--out",
        default=str(_ROOT / "data" / "errorcodes.json"),
        help="Output path (default: data/errorcodes.json)",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Merge with existing errorcodes.json; extracted HTML data wins on conflict",
    )
    args = parser.parse_args()

    manuals_dir = Path(args.manuals)
    toc_path    = Path(args.toc)
    out_path    = Path(args.out)

    extracted = extract(manuals_dir, toc_path)

    # Optionally merge with existing file
    if args.merge and out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        merged = {**existing, **extracted}
        print(f"\nMerge: {len(existing)} existing + {len(extracted)} new → {len(merged)} total")
        output = merged
    else:
        output = extracted

    # Strip internal _source key before writing
    clean = {
        code: {k: v for k, v in entry.items() if not k.startswith("_")}
        for code, entry in sorted(output.items())
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\nTotal codes written : {len(clean)}")
    print(f"Output              → {out_path}")

    if len(clean) == 0:
        print(
            "\nHint: No codes found. Error code pages (Fehlermeldungen, Betriebsstörungen)\n"
            "      were not among the loaded HTML files. Upload the full manual and re-run."
        )


if __name__ == "__main__":
    main()
