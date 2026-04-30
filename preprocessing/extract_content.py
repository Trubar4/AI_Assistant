"""
extract_content.py

Reads HTML manual files from a folder and produces data/content_index.json.

Output format (keyed by filename):
{
  "ID_xxx-de-DE.html": {
    "filename": "ID_xxx-de-DE.html",
    "title": "Getriebeölstand prüfen",
    "breadcrumb": ["Wartung", "Verteilergetriebe", "Getriebeölstand prüfen"],
    "text": "Sicherstellen, dass ... Schritt 1: Einfüllstutzendeckel lösen ...",
    "warnings": ["VORSICHT: Heiße Getriebeteile ... Schutzausrüstung tragen."],
    "steps": ["Einfüllstutzendeckel lösen.", "Ölmessstab herausziehen.", ...],
    "word_count": 212
  }
}
"""

import argparse
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup, Tag

# Tags that are pure navigation chrome — stripped before any text extraction
_NAV_SELECTORS = [
    "[data-role='header']",
    "[data-role='footer']",
    "[data-role='panel']",
    "[data-role='popup']",
    ".topic-info",
    ".imgzoom",
    "script",
    "style",
    "link",
]


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_breadcrumb(soup: BeautifulSoup) -> list[str]:
    # breadcrumb lives inside the header — extract before nav stripping
    crumb = soup.select_one("[data-role='header'] .breadCrumb ul, .breadCrumb ul")
    if not crumb:
        return []
    items = []
    for li in crumb.find_all("li"):
        t = _clean_text(li.get_text())
        if t and t.lower() != "home":
            items.append(t)
    return items


def _extract_warnings(content_div: Tag) -> list[str]:
    warnings = []
    for block in content_div.select(".safetyadvice, .safety, [class*='warning'], [class*='caution'], [class*='danger']"):
        signal = block.select_one(".signalword")
        signal_text = _clean_text(signal.get_text()) if signal else ""

        parts = []
        for cls in ("cause", "consequences"):
            el = block.select_one(f".{cls}")
            if el:
                parts.append(_clean_text(el.get_text()))

        # Also grab action steps inside the warning block
        for step in block.select("p.step"):
            t = _clean_text(step.get_text())
            if t:
                parts.append(t)

        body = " ".join(parts)
        if signal_text or body:
            entry = f"{signal_text}: {body}" if signal_text else body
            warnings.append(entry)
    return warnings


def _extract_steps(content_div: Tag) -> list[str]:
    steps = []
    for el in content_div.select("p.step, li"):
        if el.find_parent(class_="safetyadvice"):
            continue
        # Skip li elements inside large reference tables (td parent with many siblings)
        td = el.find_parent("td")
        if td and td.find_parent("table") and not el.find_parent(class_=lambda c: c and "action" in c):
            continue
        t = _clean_text(el.get_text())
        if t:
            steps.append(t)
    return steps


def _extract_title(content_div: Tag) -> str:
    for tag in ("h1", "h2", "h3"):
        el = content_div.find(tag)
        if el:
            return _clean_text(el.get_text())
    return ""


def _extract_text(content_div: Tag) -> str:
    # Remove image-zoom popups inside content before full-text extraction
    for el in content_div.select(".imgzoom"):
        el.decompose()
    # Remove figure captions (repetitive noise)
    for el in content_div.select("figcaption, .graphic-info"):
        el.decompose()
    return _clean_text(content_div.get_text(separator=" "))


def parse_file(path: Path) -> dict | None:
    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    # Breadcrumb sits inside the header — extract before nav chrome is stripped
    breadcrumb = _extract_breadcrumb(soup)

    for sel in _NAV_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    content_div = soup.select_one("div.topic-content")
    if not content_div:
        return None

    title = _extract_title(content_div)
    warnings = _extract_warnings(content_div)
    steps = _extract_steps(content_div)
    text = _extract_text(content_div)

    return {
        "filename":   path.name,
        "title":      title,
        "breadcrumb": breadcrumb,
        "text":       text,
        "warnings":   warnings,
        "steps":      steps,
        "word_count": len(text.split()),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Extract content from HTML manual files → data/content_index.json"
    )
    parser.add_argument(
        "--manuals",
        default=str(Path(__file__).parent.parent / "manuals"),
        help="Folder containing the HTML manual files (default: repo_root/manuals/)",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parent.parent / "data" / "content_index.json"),
        help="Output JSON path",
    )
    args = parser.parse_args()

    manuals_path = Path(args.manuals)
    out_path = Path(args.out)

    if not manuals_path.exists():
        raise FileNotFoundError(f"Manuals folder not found: {manuals_path}")

    html_files = sorted(manuals_path.glob("ID_*-de-DE.html"))
    if not html_files:
        raise FileNotFoundError(f"No ID_*-de-DE.html files found in {manuals_path}")

    print(f"Processing {len(html_files)} files from {manuals_path} …")

    index = {}
    skipped = 0
    for f in html_files:
        result = parse_file(f)
        if result is None:
            skipped += 1
            print(f"  [skip] {f.name} — no .topic-content found")
            continue
        index[f.name] = result

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(index, ensure_ascii=False, indent=2))

    total_words = sum(v["word_count"] for v in index.values())
    print(f"\nFiles parsed  : {len(index)}")
    if skipped:
        print(f"Files skipped : {skipped}")
    print(f"Total words   : {total_words:,}")
    print(f"Avg words/doc : {total_words // max(len(index), 1)}")
    print(f"\nOutput → {out_path}")


if __name__ == "__main__":
    main()
