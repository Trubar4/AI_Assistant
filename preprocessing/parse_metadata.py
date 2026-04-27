"""
parse_metadata.py

Parses the iiRDS metadata.rdf and produces data/metadata_index.json.

Output format (keyed by HTML filename):
{
  "ID_xxx-de-DE.html": {
    "filename": "ID_xxx-de-DE.html",
    "title": "Aufrüsten des Auslegers",
    "topic_type": "GenericTask",
    "lifecycle_phases": ["Maintenance", "Operation"]
  },
  ...
}
"""

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

IIRDS = "http://iirds.tekom.de/iirds#"

NS = {
    "iirds": IIRDS,
    "rdf":   "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}


def _local(uri: str) -> str:
    """Extract the local name from a full iiRDS URI."""
    return uri.split("#")[-1]


def parse(rdf_path: Path) -> dict:
    tree = ET.parse(rdf_path)
    root = tree.getroot()

    index = {}

    for topic in root.findall("iirds:Topic", NS):
        title_el = topic.find("iirds:title", NS)
        if title_el is None:
            continue
        title = title_el.text or ""

        # HTML source file — strip the CONTENT/ prefix if present
        source_el = topic.find(".//iirds:Rendition/iirds:source", NS)
        if source_el is None:
            continue
        filename = Path(source_el.text.strip()).name  # drops CONTENT/ prefix

        # Topic type — take the local name from the URI attribute
        topic_type_el = topic.find("iirds:has-topic-type", NS)
        topic_type = ""
        if topic_type_el is not None:
            uri = topic_type_el.get(f"{{{NS['rdf']}}}resource", "")
            topic_type = _local(uri)

        # Lifecycle phases — can be multiple
        phases = []
        for phase_el in topic.findall("iirds:relates-to-product-lifecycle-phase", NS):
            uri = phase_el.get(f"{{{NS['rdf']}}}resource", "")
            phases.append(_local(uri))

        index[filename] = {
            "filename":        filename,
            "title":           title,
            "topic_type":      topic_type,
            "lifecycle_phases": phases,
        }

    return index


def main():
    parser = argparse.ArgumentParser(description="Parse iiRDS metadata.rdf → data/metadata_index.json")
    parser.add_argument(
        "--rdf",
        default=str(Path(__file__).parent.parent / "metadata.rdf"),
        help="Path to metadata.rdf (default: repo root)",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parent.parent / "data" / "metadata_index.json"),
        help="Output JSON path",
    )
    args = parser.parse_args()

    rdf_path = Path(args.rdf)
    out_path = Path(args.out)

    if not rdf_path.exists():
        raise FileNotFoundError(f"metadata.rdf not found at {rdf_path}")

    print(f"Parsing {rdf_path} …")
    index = parse(rdf_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(index, ensure_ascii=False, indent=2))

    # Summary
    from collections import Counter
    type_counts = Counter(v["topic_type"] for v in index.values())
    phase_counts = Counter(p for v in index.values() for p in v["lifecycle_phases"])

    print(f"\nTopics written : {len(index)}")
    print("\nBy topic type:")
    for t, n in type_counts.most_common():
        print(f"  {n:>5}  {t}")
    print("\nBy lifecycle phase (top 10):")
    for p, n in phase_counts.most_common(10):
        print(f"  {n:>5}  {p}")
    print(f"\nOutput → {out_path}")


if __name__ == "__main__":
    main()
