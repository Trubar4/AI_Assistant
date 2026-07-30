"""
Retrieval-Regressions-Eval: reale Fragen → erwartete Soll-Quelle.

Fährt die deterministische „Regelbasiert"-Pipeline (Fast-Paths zuerst, sonst
retrieve_fusion — inkl. Hebel A–D in backend/search.py) und prüft, ob die
erwartete Seite im erlaubten Rang steht. Die Fälle stehen in eval_questions.json
und sind dort manuell pflegbar/erweiterbar.

Braucht die volle Such-Umgebung (numpy/rank_bm25/Embeddings) → ohne diese Pakete
werden die Tests übersprungen. Läuft in der Backend-Umgebung / CI.
Bekannt offene Fälle sind als xfail markiert (known_gap in der JSON).
"""
import json
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("rank_bm25")
# Genau die Funktion, die die App im Regelbasiert-Modus nutzt (/ask_agent →
# run_agent_local(mode="sources")). importorskip überspringt, falls der volle
# Agenten-Stack (search/fastapi/claude_client …) in dieser Umgebung fehlt.
_agent_local = pytest.importorskip("backend.agent_local")

_CASES_FILE = Path(__file__).parent / "eval_questions.json"


def _load_cases():
    data = json.loads(_CASES_FILE.read_text(encoding="utf-8"))
    params = []
    for c in data["cases"]:
        marks = []
        if c.get("known_gap"):
            marks.append(pytest.mark.xfail(reason=c.get("note", "known gap"), strict=False))
        params.append(pytest.param(c, id=c["id"], marks=marks))
    return params


@pytest.mark.parametrize("case", _load_cases())
def test_retrieval_eval(case):
    res = _agent_local.run_agent_local(case["question"], case.get("context", ""), mode="sources")

    # Mehrdeutige Fragen: der Assistent soll RÜCKFRAGEN, nicht raten.
    if "expect_clarification" in case:
        assert res.get("type") == "clarification", (
            f"[{case['id']}] erwartete Rückfrage, bekam type={res.get('type')!r}")
        q = (res.get("question") or "").lower()
        assert case["expect_clarification"].lower() in q, (
            f"[{case['id']}] Rückfrage ohne '{case['expect_clarification']}': {res.get('question')!r}")
        return

    titles = [s.get("title", "") for s in res.get("sources", [])]
    needle = case["expect"].lower()
    rank = next((i for i, t in enumerate(titles) if needle in (t or "").lower()), None)
    assert rank is not None and rank < case["max_rank"], (
        f"[{case['id']}] '{case['expect']}' nicht in Top-{case['max_rank']} "
        f"(gefunden auf Rang {None if rank is None else rank + 1}). "
        f"Top-5: {titles[:5]}"
    )
