"""
build_embeddings.py — Einmaliger Preprocessing-Schritt für Semantic Search.

Liest alle Manual-Einträge aus dem Content-Index, vektorisiert die Texte mit
paraphrase-multilingual-MiniLM-L12-v2 und speichert:
  - data/embeddings.npy   (float32-Matrix, shape: [N, 384])
  - data/embedding_ids.json (Liste der Filenames in gleicher Reihenfolge)

Ausführen:
    python -m backend.build_embeddings

Dauer: ~2 min für 2.180 Seiten auf CPU.
"""

import json
import os
import sys
from pathlib import Path

# Inject Windows/macOS system cert store so requests trusts corporate proxies
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# Fallback: wenn truststore nicht hilft, SSL-Verifikation über Umgebungsvariable deaktivieren
# (nur wenn DISABLE_SSL_VERIFY=1 gesetzt ist)
if os.environ.get("DISABLE_SSL_VERIFY") == "1":
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context
    os.environ.setdefault("CURL_CA_BUNDLE", "")
    os.environ.setdefault("REQUESTS_CA_BUNDLE", "")
    print("WARNUNG: SSL-Verifikation deaktiviert.")

import numpy as np
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).parent.parent
CONTENT_INDEX = ROOT / "data" / "content_index.json"
METADATA_INDEX = ROOT / "data" / "metadata_index.json"
OUT_NPY = ROOT / "data" / "embeddings.npy"
OUT_IDS = ROOT / "data" / "embedding_ids.json"

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"


def _build_doc_text(filename: str, meta: dict, content: dict) -> str:
    """Combine title (3×) + warnings + steps + body text for embedding.

    Titel wird dreifach wiederholt damit er bei der Ähnlichkeitsberechnung
    stärker gewichtet wird. Warnungen und Schritte kommen vor dem Fließtext
    weil sie die wichtigsten Informationen enthalten.
    """
    title = meta.get("title") or content.get("title", "")
    breadcrumb = " > ".join(content.get("breadcrumb", []))
    warnings = " ".join(content.get("warnings", []))[:400]
    steps = " ".join(content.get("steps", [])[:10])[:300]
    text = content.get("text", "")[:600]
    return f"{title}\n{breadcrumb}\n{warnings}\n{steps}\n{text}".strip()


def main() -> None:
    print(f"Lade Indizes…")
    meta_all = json.loads(METADATA_INDEX.read_text(encoding="utf-8"))
    content_all = json.loads(CONTENT_INDEX.read_text(encoding="utf-8"))

    filenames = [f for f in meta_all if f in content_all]
    texts = [_build_doc_text(f, meta_all[f], content_all[f]) for f in filenames]
    print(f"{len(texts)} Dokumente gefunden.")

    print(f"Lade Modell '{MODEL_NAME}'…")
    model = SentenceTransformer(MODEL_NAME)

    print("Berechne Embeddings (kann ~2 min dauern)…")
    embeddings = model.encode(
        texts,
        batch_size=64,
        show_progress_bar=True,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )

    np.save(OUT_NPY, embeddings.astype("float32"))
    OUT_IDS.write_text(json.dumps(filenames, ensure_ascii=False), encoding="utf-8")
    print(f"Fertig: {OUT_NPY} ({embeddings.shape}), {OUT_IDS}")


if __name__ == "__main__":
    main()
