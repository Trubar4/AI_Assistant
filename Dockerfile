FROM python:3.12-slim

WORKDIR /app

# System deps for lxml / sentence-transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libxml2-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-download the sentence-transformers model so runtime needs no outbound HF
# access — but ONLY if the package is actually installed. The base image ships
# requirements.txt (no sentence-transformers → BM25/TF-IDF-Fallback); die ML-
# Extras liegen in requirements-ml.txt. Ohne diesen Guard scheiterte der Build
# hier mit ModuleNotFoundError, sobald sentence-transformers ausgelagert wurde.
RUN if python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('sentence_transformers') else 1)"; then \
      python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-mpnet-base-v2')"; \
    else \
      echo "sentence-transformers nicht installiert — Modell-Vorladung übersprungen (BM25/TF-IDF-Fallback aktiv)"; \
    fi

# Cloud Run injects PORT; uvicorn binds to it
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
