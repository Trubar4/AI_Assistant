"""
claude_client.py

Two Claude API calls per /ask request:
  1. answer()   — generates a structured answer from retrieved context
  2. verify()   — checks whether the answer is grounded in the context

Returns a VerifiedAnswer dataclass with the answer text, source links,
and a grounding status: BELEGT | TEILWEISE | NICHT_BELEGT.
"""

import os
from dataclasses import dataclass, field

import anthropic
from fastapi import HTTPException

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY is not set.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


ANSWER_MODEL  = os.environ.get("ANSWER_MODEL",  "claude-haiku-4-5")
VERIFIER_MODEL = os.environ.get("VERIFIER_MODEL", "claude-haiku-4-5")

ANSWER_SYSTEM = """\
Du bist ein Assistent für Liebherr-Maschinenführer und Servicetechniker.
Antworte ausschließlich auf Basis des gegebenen Kontext-Materials.
Wenn die gesuchte Information nicht im Kontext steht, sage das explizit –
erfinde keine Fakten.

Antworte auf Deutsch, präzise und strukturiert:
- Beginne mit einer kurzen Direktantwort (1–2 Sätze).
- Liste Handlungsschritte als nummerierte Liste, falls vorhanden.
- Hebe Sicherheitshinweise (WARNUNG / VORSICHT / GEFAHR) hervor.
- Verweise am Ende auf die Quellseite(n).\
"""

VERIFIER_SYSTEM = """\
Du prüfst, ob eine gegebene Antwort vollständig durch den bereitgestellten
Kontext belegt ist. Antworte ausschließlich mit einem der drei Wörter:
BELEGT
TEILWEISE
NICHT_BELEGT\
"""

FALLBACK_ANSWER = (
    "Diese Information konnte ich im Manual nicht eindeutig finden. "
    "Bitte schlagen Sie direkt in den verlinkten Seiten nach."
)


@dataclass
class VerifiedAnswer:
    answer: str
    grounding: str          # BELEGT | TEILWEISE | NICHT_BELEGT
    sources: list[dict] = field(default_factory=list)
    fallback_used: bool = False


def _build_context(results: list[dict]) -> str:
    parts = []
    for r in results:
        bc = " › ".join(r.get("breadcrumb", [])) or r["title"]
        warnings = "\n".join(f"  ⚠ {w}" for w in r.get("warnings", []))
        steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(r.get("steps", [])[:20], 1))
        section = f"### {r['title']} ({bc})\n"
        if warnings:
            section += warnings + "\n"
        if steps:
            section += steps + "\n"
        # Append remaining free text (capped to keep prompt size manageable)
        text = r.get("text", "")
        section += text[:1500]
        parts.append(section)
    return "\n\n---\n\n".join(parts)


def answer(query: str, results: list[dict]) -> str:
    if not results:
        return FALLBACK_ANSWER

    context = _build_context(results)
    user_message = f"Frage: {query}\n\nKontext-Material:\n\n{context}"

    try:
        response = _get_client().messages.create(
            model=ANSWER_MODEL,
            max_tokens=1024,
            system=ANSWER_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=401, detail="Ungültiger Anthropic API-Key. Bitte ANTHROPIC_API_KEY in .env prüfen.")
    except anthropic.APIConnectionError:
        raise HTTPException(status_code=503, detail="Keine Verbindung zur Anthropic API. Internetverbindung prüfen.")
    return response.content[0].text.strip()


def verify(answer_text: str, results: list[dict]) -> str:
    context = _build_context(results)
    user_message = (
        f"Antwort:\n{answer_text}\n\n"
        f"Kontext-Material:\n\n{context}"
    )

    response = _get_client().messages.create(
        model=VERIFIER_MODEL,
        max_tokens=10,
        system=VERIFIER_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text.strip().upper()
    for status in ("BELEGT", "TEILWEISE", "NICHT_BELEGT"):
        if status in raw:
            return status
    return "TEILWEISE"


def ask(query: str, results: list[dict]) -> VerifiedAnswer:
    """
    Full pipeline: generate answer, verify grounding, apply fallback if needed.
    """
    sources = [
        {"title": r["title"], "filename": r["filename"], "score": r.get("score", 0)}
        for r in results
    ]

    answer_text = answer(query, results)
    grounding   = verify(answer_text, results)

    fallback_used = False
    if grounding == "NICHT_BELEGT":
        answer_text  = FALLBACK_ANSWER
        fallback_used = True

    return VerifiedAnswer(
        answer=answer_text,
        grounding=grounding,
        sources=sources,
        fallback_used=fallback_used,
    )
