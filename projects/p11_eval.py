"""#11 LLM 평가 프레임워크 — 프롬프트 A/B 비교, 회귀 탐지, CI 게이트."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from core import llm
from evals import engine

router = APIRouter(prefix="/api/p11")
REPORTS = llm.ROOT / "evals" / "reports"


@router.get("/info")
def info():
    reports = sorted(REPORTS.glob("*.json"), reverse=True)[:10] if REPORTS.exists() else []
    return {"prompts": engine.list_prompts(), "cases": engine.load_cases(),
            "history": [json.loads(p.read_text(encoding="utf-8"))["summary"] for p in reports]}


class RunReq(BaseModel):
    baseline: str = "v1"
    candidate: str = "v2"
    candidate_text: str | None = None  # 웹에서 프롬프트를 직접 수정해 실험
    limit: int = 6


@router.post("/run")
def run(req: RunReq):
    base = engine.run_suite(req.baseline, req.limit)
    cand = engine.run_suite(req.candidate if not req.candidate_text else "custom", req.limit, req.candidate_text or engine.load_prompt(req.candidate))
    cmp = engine.compare(base, cand)
    summary = {"at": datetime.now().isoformat(timespec="seconds"), "baseline": req.baseline,
               "candidate": "custom" if req.candidate_text else req.candidate, "n": base["n"],
               "base_score": base["score"], "cand_score": cand["score"], **cmp}
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{datetime.now():%Y%m%d-%H%M%S}.json").write_text(
        json.dumps({"summary": summary, "baseline": base, "candidate": cand}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"baseline": base, "candidate": cand, "compare": cmp, "summary": summary}
