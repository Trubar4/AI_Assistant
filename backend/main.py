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
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from backend.search import search, reset_index, extract_facets, count_hits
from backend.claude_client import ask, expand_query, rerank, parse_context, VerifiedAnswer, log_mode2_provider
from backend.agent import run_agent
from backend.rule_agent import run_rule_agent

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


@app.get("/config", include_in_schema=False)
async def config() -> dict:
    return {"default_mode": _DEFAULT_MODE}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str
    top_n: int = 5
    context: str = ""   # persistenter Maschinen-/Konfigurations-Kontext


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


class AgentSource(BaseModel):
    filename: str
    title: str


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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/ask", response_model=AskResponse)
async def ask_question(req: AskRequest) -> AskResponse:
    q = req.question.strip()
    if not q:
        raise HTTPException(status_code=422, detail="question must not be empty")

    ctx = req.context.strip()

    # HyDE: BM25-Titelscan nur gegen die echte Frage — Kontext-Tokens würden
    # die Titeltreffer verzerren. Kontext fließt aber in den HyDE-Prompt ein,
    # damit die hypothetische Passage konfigurationsrelevant ist.
    expanded_q = expand_query(q, context=ctx)

    candidates = search(expanded_q, top_n=50)   # Triple-RRF → 50 candidates
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
    rerank_q = f"{ctx}\n\n{q}" if ctx else q
    results = rerank(rerank_q, candidates, top_n=req.top_n)
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

    use_rule = (req.mode == "rule") or (_rule_mode and req.mode != "agent")

    if use_rule:
        result = run_rule_agent(
            question=q,
            context=req.context.strip(),
            conversation=req.conversation or [],
        )
    else:
        result = run_agent(
            question=q,
            context=req.context.strip(),
            conversation=req.conversation or [],
        )

    return AgentResponse(
        type=result["type"],
        answer=result.get("answer", ""),
        question=result.get("question", ""),
        sources=[AgentSource(**s) for s in result.get("sources", [])],
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
