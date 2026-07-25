"""
main.py — FastAPI application

Endpoints:
  POST /ask         — answer a manual question
  POST /errorcode   — look up an error code
  GET  /health      — liveness check
"""

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

from dotenv import load_dotenv

# Inject Windows/macOS system cert store so Python's httpx trusts
# corporate SSL-inspection proxies (no effect on Railway/Linux).
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from backend.search import search, reset_index, extract_facets, count_hits
from backend.claude_client import ask, expand_query, rerank, parse_context, VerifiedAnswer, log_mode2_provider, LLM_PROVIDER
from backend.agent import run_agent
from backend.rule_agent import _normalize_query
from backend.fastpaths import relevant_context

# ---------------------------------------------------------------------------
# Error code database (optional — loaded once at startup)
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent
_ERRORCODES: dict = {}
_MSGCODES: dict = {}   # canonical key "0x00000035" → {description, effect, solution, causes}


def _norm_hex(value: str) -> str | None:
    """'0x00000035', '0X35', '35' → '0x00000035'. None wenn kein Hex-Code."""
    s = str(value).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if not re.fullmatch(r"[0-9a-f]{1,8}", s):
        return None
    return "0x" + s.zfill(8)


def _load_errorcodes() -> None:
    global _ERRORCODES, _MSGCODES
    path = _ROOT / "data" / "errorcodes.json"
    if path.exists():
        _ERRORCODES = json.loads(path.read_text(encoding="utf-8"))

    msg_path = _ROOT / "data" / "msgcodes.json"
    if msg_path.exists():
        raw = json.loads(msg_path.read_text(encoding="utf-8"))
        # Keys beim Laden normalisieren, damit Lookup unabhängig vom
        # gespeicherten Format ("0x35" vs. "0x00000035") funktioniert.
        _MSGCODES = {}
        for key, entry in raw.items():
            canon = _norm_hex(key)
            if canon:
                _MSGCODES[canon] = entry


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_errorcodes()
    log_mode2_provider()
    # Pre-warm semantic model so first request isn't slow
    try:
        from backend.search import _load_semantic
        _load_semantic()
    except Exception:
        pass
    yield
    reset_index()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
# AGENT_MODE=true  → Standard "agent"
# AGENT_MODE=rule  → regelbasierter Agent ohne LLM (RULE_AGENT=true hat gleichen Effekt)
_agent_mode_env = os.environ.get("AGENT_MODE", "").lower()
_rule_mode = _agent_mode_env == "rule" or os.environ.get("RULE_AGENT", "").lower() in ("1", "true")
_DEFAULT_MODE = "rule" if _rule_mode else ("agent" if _agent_mode_env in ("1", "true") else "classic")

# Lokales Modus-3-Backend (der "Lokal"-Umschalter der UI) ist nur aktiv, wenn
# ausdrücklich gewünscht. Default: nur wenn der Provider ohnehin lokal läuft.
# Auf einer Anthropic-Deployment-Instanz (z. B. Render) ist damit ALLES Lokale
# sicher aus — ein versehentlicher "Lokal"-Klick landet nicht auf localhost:11434,
# und die UI blendet den Button aus (siehe /config → local_backend_enabled).
_local_backend_enabled = os.environ.get(
    "ENABLE_LOCAL_BACKEND",
    "true" if LLM_PROVIDER == "local" else "false",
).strip().lower() in ("1", "true", "yes", "on")

app = FastAPI(
    title="Maschinen-Assistent API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files — API routes are registered first so they take priority
_frontend = _ROOT / "frontend"
_ds       = _ROOT / "design-system"
_manuals  = _ROOT / "manuals"

if _frontend.exists():
    app.mount("/frontend",      StaticFiles(directory=str(_frontend)), name="frontend")
if _ds.exists():
    app.mount("/design-system", StaticFiles(directory=str(_ds)),       name="design-system")
if _manuals.exists():
    app.mount("/manuals",       StaticFiles(directory=str(_manuals)),   name="manuals")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url=f"/frontend/MaschinenAssistent.html?mode={_DEFAULT_MODE}")


# Wartungsdaten (Maintenance-Assistent) — die beiden JSON-Dateien liegen im
# Repo-Root und werden vom Wartungen-Tab per fetch geladen. Explizite Routen statt
# Static-Mount, damit nur genau diese Dateien (nicht das ganze Root) erreichbar sind.
_MAINTENANCE_FILES = {
    "tasks": _ROOT / "maintenance_tasks.json",
    "instructions": _ROOT / "maintenance_instructions.json",
}


@app.get("/maintenance/{name}.json", include_in_schema=False)
async def maintenance_data(name: str):
    path = _MAINTENANCE_FILES.get(name)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Wartungsdaten nicht gefunden")
    return FileResponse(str(path), media_type="application/json")


@app.get("/config", include_in_schema=False)
async def config() -> dict:
    return {
        "default_mode": _DEFAULT_MODE,
        "local_backend_enabled": _local_backend_enabled,
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str
    top_n: int = 5
    context: str = ""   # persistenter Maschinen-/Konfigurations-Kontext
    backend: str = ""   # "qwen" | "anthropic" | "" → HyDE/Rerank-Anbieter (Klassisch)


class SourceLink(BaseModel):
    title: str
    filename: str
    score: float
    snippet: str = ""


class Facet(BaseModel):
    label: str
    options: list[str]


class AskResponse(BaseModel):
    answer: str
    grounding: str          # BELEGT | TEILWEISE | NICHT_BELEGT
    fallback_used: bool
    sources: list[SourceLink]
    facets: list[Facet] = []


class ParseContextRequest(BaseModel):
    raw: str


class ParsedField(BaseModel):
    key: str
    value: str
    hits: int = 0
    valid: bool = True


class ParseContextResponse(BaseModel):
    fields: list[ParsedField]
    canonical: str


class AgentRequest(BaseModel):
    question: str
    context: str = ""
    conversation: list[dict] = []   # History für Clarification-Runden
    mode: str = ""                  # "rule" → regelbasierter Agent; leer → globaler Default
    agent_backend: str = ""         # "anthropic" | "local" | leer → AGENT_BACKEND-Env/auto


class AgentSource(BaseModel):
    filename: str
    title: str
    breadcrumb: list[str] = []   # deterministisch aus dem Index (unterscheidet gleichnamige Seiten)
    snippet: str = ""            # kurzer Kontext-Auszug (kein LLM)


class AgentResponse(BaseModel):
    type: str                        # "answer" | "clarification"
    answer: str = ""
    question: str = ""              # bei type="clarification"
    sources: list[AgentSource] = []
    rounds: int = 0
    conversation: list[dict] = []   # zurück an Frontend für nächsten Call
    confidence: float = 1.0         # 0.0–1.0; nur rule-agent befüllt dieses Feld


class ErrorCodeRequest(BaseModel):
    code: str


class ErrorCodeMatch(BaseModel):
    code: str
    description: str = ""
    action: str = ""


class ErrorCodeResponse(BaseModel):
    code: str
    found: bool
    description: str = ""
    cause: str = ""
    action: str = ""
    effect: str = ""           # Auswirkung (Meldungen / MsgCodeHex)
    solution: str = ""         # Problemlösung (Meldungen / MsgCodeHex)
    causes: str = ""           # Mögliche Ursachen (Meldungen / MsgCodeHex)
    required_action: str = ""  # Erforderliche Aktion (Meldungen / MsgCodeHex)
    relation: str = ""         # Beziehung (Meldungen / MsgCodeHex)
    related: list[SourceLink] = []
    matches: list[ErrorCodeMatch] = []  # populated on keyword search


def _keyword_search(query: str, limit: int = 8) -> list[ErrorCodeMatch]:
    """Search error/message codes by keyword in description or action text."""
    q = query.lower()
    results = []
    for code, entry in _ERRORCODES.items():
        if (q in entry.get("description", "").lower()
                or q in entry.get("action", "").lower()):
            results.append(ErrorCodeMatch(
                code=code,
                description=entry.get("description", ""),
                action=entry.get("action", ""),
            ))
    for code, entry in _MSGCODES.items():
        haystack = " ".join((
            entry.get("description", ""),
            entry.get("solution", ""),
            entry.get("causes", ""),
            entry.get("required_action", ""),
        )).lower()
        if q in haystack:
            results.append(ErrorCodeMatch(
                code=code,
                description=entry.get("description", ""),
                action=entry.get("solution", ""),
            ))
    return results[:limit]


def _merge_candidates(*lists: list[dict], top_n: int = 50) -> list[dict]:
    """Führt mehrere search()-Kandidatenlisten zusammen (Union nach filename).

    Pro Seite wird der höhere der beiden search()-Scores behalten und danach
    absteigend sortiert. Die Skala (RRF × 1000) bleibt exakt wie bei einem
    einzelnen search()-Aufruf — die Konfidenz-Schwelle in ask() (18.0) bleibt
    also gültig. Zweck ist reiner Recall-Gewinn: eine Seite, die nur eine der
    Queries findet, landet trotzdem im Topf für den Reranker.
    """
    by_fname: dict[str, dict] = {}
    for lst in lists:
        for c in lst:
            fn = c["filename"]
            prev = by_fname.get(fn)
            if prev is None or c.get("score", 0) > prev.get("score", 0):
                by_fname[fn] = c
    merged = sorted(by_fname.values(), key=lambda c: c.get("score", 0), reverse=True)
    return merged[:top_n]


def _enrich_sources(sources: list[dict]) -> list[dict]:
    """Reichert Agent-Quellen deterministisch (KEIN LLM) mit Breadcrumb + kurzem
    Snippet aus dem Suchindex an. So sind gleichnamige Seiten (z. B. zwei
    verschiedene „Montagefunktionen ausschalten") an ihrem Pfad unterscheidbar."""
    if not sources:
        return sources
    try:
        from backend.search import _load_index
        idx = {e["filename"]: e for e in (_load_index() or [])}
    except Exception:
        return sources
    out = []
    for s in sources:
        e = idx.get(s.get("filename", ""))
        enriched = dict(s)
        if e:
            if not enriched.get("breadcrumb"):
                # letzte bis zu 3 Ebenen (ohne den Blatt-Titel selbst, wenn identisch)
                bc = list(e.get("breadcrumb") or [])
                if bc and bc[-1] == e.get("title"):
                    bc = bc[:-1]
                enriched["breadcrumb"] = bc[-3:]
            if not enriched.get("snippet"):
                text = (e.get("text") or "").strip()
                if not text and e.get("steps"):
                    text = e["steps"][0]
                if text:
                    enriched["snippet"] = text[:140].rsplit(" ", 1)[0] + ("…" if len(text) > 140 else "")
        out.append(enriched)
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/ask", response_model=AskResponse)
async def ask_question(req: AskRequest) -> AskResponse:
    q = req.question.strip()
    if not q:
        raise HTTPException(status_code=422, detail="question must not be empty")

    ctx = req.context.strip()

    # Anbieter für HyDE/Rerank (Klassisch): "qwen"→lokal, "anthropic"→Claude,
    # leer→globaler LLM_PROVIDER. Ohne lokales Backend (Render) wird QWEN sicher
    # auf Anthropic heruntergestuft (kein Ollama erreichbar).
    _backend = (req.backend or "").strip().lower()
    provider = "local" if _backend == "qwen" else ("anthropic" if _backend == "anthropic" else None)
    if provider == "local" and not _local_backend_enabled:
        logger.info("Klassisch: QWEN angefragt, lokales Backend aus → Anthropic")
        provider = "anthropic"

    # Per-Frage-Relevanz: nur die zur Frage passenden Konfig-Felder ins Retrieval
    # (HyDE/Rerank) geben. Verhindert, dass irrelevanter Kontext (z. B. „Hauptausleger
    # 74 m" bei einer Bildschirm-Navigationsfrage) die richtige Seite verdrängt.
    rel_ctx = relevant_context(q, ctx)

    # HyDE: BM25-Titelscan nur gegen die echte Frage — Kontext-Tokens würden
    # die Titeltreffer verzerren. Der (relevante) Kontext fließt aber in den
    # HyDE-Prompt ein, damit die hypothetische Passage konfigurationsrelevant ist.
    expanded_q = expand_query(q, context=rel_ctx, provider=provider)

    # Multi-Query-Fusion (Trick aus Modus 1): Die Suche darf sich nicht allein
    # auf die HyDE-Passage verlassen — driftet die Hypothese thematisch ab, fällt
    # die richtige Seite sonst komplett aus den Kandidaten und der Reranker kann
    # sie nicht mehr retten. Deshalb zusätzlich mit der normalisierten Original-
    # frage (Fragesatz-Ballast entfernt) suchen und beide Trefferlisten mergen.
    # So garantiert der direkte Titel-BM25-Treffer den Recall, HyDE liefert die
    # Paraphrasen-/Synonym-Recall obendrauf.
    normalized_q = _normalize_query(q)
    cand_hyde = search(expanded_q, top_n=50)    # Triple-RRF → 50 candidates
    cand_norm = (
        search(normalized_q, top_n=50)
        if normalized_q and normalized_q.lower() != expanded_q.lower()
        else []
    )
    candidates = _merge_candidates(cand_hyde, cand_norm, top_n=50)
    logger.info(
        "FUSION hyde=%d norm=%d ('%s') → %d Kandidaten",
        len(cand_hyde), len(cand_norm), normalized_q[:50], len(candidates),
    )
    if not candidates:
        return AskResponse(
            answer=(
                "Zu dieser Frage wurden keine passenden Seiten im Manual gefunden. "
                "Bitte präzisieren Sie Ihre Anfrage."
            ),
            grounding="NICHT_BELEGT",
            fallback_used=True,
            sources=[],
        )

    facets = extract_facets(candidates, top_k=10)
    # Reranker bekommt Kontext + Frage explizit, damit konfigurationsrelevante
    # Seiten (z. B. "74m Hauptausleger") höher bewertet werden.
    rerank_q = f"{rel_ctx}\n\n{q}" if rel_ctx else q
    results = rerank(rerank_q, candidates, top_n=req.top_n, provider=provider)
    va: VerifiedAnswer = ask(q, results)                         # original query für Anzeige
    return AskResponse(
        answer=va.answer,
        grounding=va.grounding,
        fallback_used=va.fallback_used,
        sources=[SourceLink(**s) for s in va.sources],
        facets=[Facet(**f) for f in facets],
    )


@app.post("/context/parse", response_model=ParseContextResponse)
async def parse_context_endpoint(req: ParseContextRequest) -> ParseContextResponse:
    raw = req.raw.strip()
    if not raw:
        raise HTTPException(status_code=422, detail="raw must not be empty")

    parsed = parse_context(raw)

    fields_out = []
    for f in parsed:
        hits = count_hits(f["wert"])
        fields_out.append(ParsedField(
            key=f["schluessel"],
            value=f["wert"],
            hits=hits,
            valid=hits > 0,
        ))

    canonical = " / ".join(f"{f.key}: {f.value}" for f in fields_out)
    return ParseContextResponse(fields=fields_out, canonical=canonical)


@app.post("/ask_agent", response_model=AgentResponse)
async def ask_agent(req: AgentRequest) -> AgentResponse:
    q = req.question.strip()
    if not q:
        raise HTTPException(status_code=422, detail="question must not be empty")

    # Assistent-Backend bestimmen. Drei Optionen:
    #   rule      — deterministisch, KEIN LLM (Fast-Paths + Quellen)
    #   qwen      — deterministische Antwort, aber qwen-Assist im Retrieval
    #               (HyDE + Rerank); braucht ein lokales Backend (Ollama)
    #   anthropic — agentischer Claude-Loop (formuliert)
    # Reihenfolge: Request-Feld agent_backend, sonst Env AGENT_BACKEND. Legacy:
    # mode=rule / "local" / "auto" / leer → rule (deterministischer Default,
    # läuft überall, auch auf Render ohne API-Key).
    backend = (req.agent_backend or os.environ.get("AGENT_BACKEND", "")).strip().lower()
    if req.mode == "rule" or backend in ("", "auto", "local"):
        backend = "rule"
    if backend not in ("rule", "qwen", "anthropic"):
        backend = "rule"
    # QWEN braucht ein lokales Backend (Ollama). Fehlt es (z. B. Render), sicher
    # auf Regelbasiert herunterstufen — kein 503 gegen localhost.
    if backend == "qwen" and not _local_backend_enabled:
        logger.info("Assistent: QWEN angefragt, lokales Backend aus → Regelbasiert")
        backend = "rule"

    if backend == "anthropic":
        result = run_agent(
            question=q,
            context=req.context.strip(),
            conversation=req.conversation or [],
        )
    else:
        from backend.agent_local import run_agent_local
        result = run_agent_local(
            question=q,
            context=req.context.strip(),
            conversation=req.conversation or [],
            mode="sources",
            assist=("qwen" if backend == "qwen" else None),
        )

    return AgentResponse(
        type=result["type"],
        answer=result.get("answer", ""),
        question=result.get("question", ""),
        sources=[AgentSource(**s) for s in _enrich_sources(result.get("sources", []))],
        rounds=result.get("rounds", 0),
        conversation=result.get("messages", []),
        confidence=result.get("confidence", 1.0),
    )


@app.post("/errorcode", response_model=ErrorCodeResponse)
async def lookup_errorcode(req: ErrorCodeRequest) -> ErrorCodeResponse:
    code = req.code.strip().upper()
    if not code:
        raise HTTPException(status_code=422, detail="code must not be empty")

    # Hex-Meldecodes (MsgCodeHex, z. B. 0x00000035) zuerst prüfen
    hex_code = _norm_hex(req.code)
    if hex_code and hex_code in _MSGCODES:
        entry = _MSGCODES[hex_code]
        related_results = search(entry.get("description", ""), top_n=3)
        related = [
            SourceLink(title=r["title"], filename=r["filename"], score=r.get("score", 0))
            for r in related_results
        ]
        return ErrorCodeResponse(
            code=hex_code,
            found=True,
            description=entry.get("description", ""),
            effect=entry.get("effect", ""),
            solution=entry.get("solution", ""),
            causes=entry.get("causes", ""),
            required_action=entry.get("required_action", ""),
            relation=entry.get("relation", ""),
            related=related,
        )

    entry = _ERRORCODES.get(code) or _ERRORCODES.get(req.code.strip())
    if entry is None:
        # Keyword search fallback
        matches = _keyword_search(req.code.strip())
        return ErrorCodeResponse(code=req.code.strip(), found=False, matches=matches)

    query = f"{code} {entry.get('description', '')}".strip()
    related_results = search(query, top_n=3)
    related = [
        SourceLink(title=r["title"], filename=r["filename"], score=r.get("score", 0))
        for r in related_results
    ]

    return ErrorCodeResponse(
        code=code,
        found=True,
        description=entry.get("description", ""),
        cause=entry.get("cause", ""),
        action=entry.get("action", ""),
        related=related,
    )


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "errorcodes_loaded": len(_ERRORCODES),
        "msgcodes_loaded": len(_MSGCODES),
    }
