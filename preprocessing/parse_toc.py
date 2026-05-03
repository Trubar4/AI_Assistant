"""
parse_toc.py

Parses toc-de-de.js (HTML string inside a JS variable) → data/toc_index.json.

Output: list ordered by TOC position, each entry:
{
  "filename":   "ID_xxx-de-DE.html",
  "title":      "Aufrüsten des Auslegers",
  "breadcrumb": ["Inbetriebnahme", "Ausleger", "Aufrüsten des Auslegers"],
  "depth":      3
}
"""

import argparse
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup, Tag


def _extract_html(js_path: Path) -> str:
    src = js_path.read_text(encoding="utf-8")
    m = re.search(r"var\s+toc_contents\s*=\s*'(.*?)'\s*;?\s*$", src, re.DOTALL)
    if not m:
        raise ValueError(f"Could not find toc_contents variable in {js_path}")
    return m.group(1)


def _walk(ul: Tag, parent_crumb: list[str], depth: int, results: list) -> None:
    for li in ul.find_all("li", recursive=False):
        # Navigation-only items (back-button row at the top of each sub-panel).
        # These have drilldown-back as a DIRECT child — check recursive=False
        # to avoid matching the back-link nested inside a child drilldown-sub.
        if li.find("a", class_="drilldown-back", recursive=False):
            continue

        # The actual page link
        link = li.find("a", class_="toc-link-direct")
        if link:
            href = link.get("href", "")
            filename = Path(href).name if href and href != "#" else None

            title_el = link.find("span", class_="toc-text") \
                    or link.find("span", class_="toc-text-first")
            title = title_el.get_text(strip=True) if title_el else ""

            if filename and title:
                crumb = parent_crumb + [title]
                results.append({
                    "filename":   filename,
                    "title":      title,
                    "breadcrumb": crumb,
                    "depth":      depth,
                })
                # Recurse into children using this item as the new parent
                sub = li.find("ul", class_="drilldown-sub")
                if sub:
                    _walk(sub, crumb, depth + 1, results)
            else:
                # Section with children but no own page
                sub = li.find("ul", class_="drilldown-sub")
                if sub:
                    _walk(sub, parent_crumb, depth, results)
        else:
            # No direct link — just a container; recurse keeping same crumb
            sub = li.find("ul", class_="drilldown-sub")
            if sub:
                _walk(sub, parent_crumb, depth, results)


def parse(js_path: Path) -> list[dict]:
    html = _extract_html(js_path)
    soup = BeautifulSoup(html, "html.parser")
    root_ul = soup.find("ul", class_="drilldown-root")
    if not root_ul:
        raise ValueError("drilldown-root <ul> not found in toc HTML")

    results: list[dict] = []
    _walk(root_ul, [], 1, results)
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Parse toc-de-de.js → data/toc_index.json"
    )
    parser.add_argument(
        "--toc",
        default=str(Path(__file__).parent.parent / "toc-de-de.js"),
        help="Path to toc-de-de.js (default: repo root)",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parent.parent / "data" / "toc_index.json"),
        help="Output JSON path",
    )
    args = parser.parse_args()

    toc_path = Path(args.toc)
    out_path = Path(args.out)

    if not toc_path.exists():
        raise FileNotFoundError(f"toc file not found: {toc_path}")

    print(f"Parsing {toc_path} …")
    index = parse(toc_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    depths = {}
    for e in index:
        depths[e["depth"]] = depths.get(e["depth"], 0) + 1

    print(f"\nEntries written : {len(index)}")
    print("By depth:")
    for d in sorted(depths):
        print(f"  depth {d} : {depths[d]:>5}")
    print(f"\nOutput → {out_path}")


if __name__ == "__main__":
    main()
