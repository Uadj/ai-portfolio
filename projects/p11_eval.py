"""#11 LLM 평가 프레임워크 — 프롬프트 A/B 비교, 회귀 탐지, CI 게이트."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import llm
from evals import engine

router = APIRouter(prefix="/api/p11")
log = logging.getLogger(__name__)
REPORTS = llm.ROOT / "evals" / "reports"

DEFAULT_LIMIT = 6           # 화면 기본값. 0/빈 값도 이 값으로 (전체 12건은 비용이 두 배)
MAX_CANDIDATE_CHARS = 4000  # 후보 프롬프트는 케이스마다 system으로 들어가 (케이스 수 x 2) 번 과금된다
MAX_REPORTS = 50


def _summary(p: Path) -> dict | None:
    """리포트 요약. 깨졌거나 쓰는 중인 파일은 건너뛴다 (기록 하나 때문에 데모 전체가 안 뜨지 않게)."""
    try:
        s = json.loads(p.read_text(encoding="utf-8"))["summary"]
        return s if isinstance(s, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


@router.get("/info")
def info():
    reports = sorted(REPORTS.glob("*.json"), reverse=True)[:20] if REPORTS.exists() else []
    history = [s for s in map(_summary, reports) if s][:10]
    return {"prompts": engine.list_prompts(), "cases": engine.load_cases(), "history": history}


class RunReq(BaseModel):
    baseline: str = "v1"
    candidate: str = "v2"
    candidate_text: str | None = None  # 웹에서 프롬프트를 직접 수정해 실험
    limit: int = DEFAULT_LIMIT


def _save_report(payload: dict) -> None:
    """임시 파일(*.json.tmp — /info의 glob에 안 걸림)에 쓴 뒤 원자적으로 교체. 파일명은 마이크로초까지."""
    REPORTS.mkdir(parents=True, exist_ok=True)
    final = REPORTS / f"{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    tmp = final.with_name(final.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, final)
    for old in sorted(REPORTS.glob("*.json"), reverse=True)[MAX_REPORTS:]:
        try:
            old.unlink()
        except OSError:
            pass


@router.post("/run")
def run(req: RunReq):
    # LLM을 한 번도 부르기 전에 모두 검증한다 (잘못된 후보 때문에 기준 실행 비용만 날리지 않게)
    names = engine.prompt_names()
    for label, v in (("기준", req.baseline), ("후보", req.candidate)):
        if v not in names:
            raise HTTPException(400, f"알 수 없는 {label} 프롬프트입니다: {v!r} (사용 가능: {', '.join(names)})")
    custom = req.candidate_text is not None
    if custom and not req.candidate_text.strip():
        raise HTTPException(400, "후보 프롬프트가 비어 있습니다.")
    if custom and len(req.candidate_text) > MAX_CANDIDATE_CHARS:
        raise HTTPException(400, f"후보 프롬프트는 {MAX_CANDIDATE_CHARS}자 이내로 입력해 주세요 (현재 {len(req.candidate_text)}자).")
    total = len(engine.load_cases())
    limit = max(1, min(req.limit or DEFAULT_LIMIT, total))  # 음수 → 1, 초과 → 전체

    try:
        base = engine.run_suite(req.baseline, limit)
        cand = engine.run_suite("custom" if custom else req.candidate, limit, req.candidate_text if custom else None)
    except engine.SuiteFailed as e:
        raise HTTPException(502, str(e)) from e
    cmp = engine.compare(base, cand)
    summary = {"at": datetime.now().isoformat(timespec="seconds"), "baseline": req.baseline,
               "candidate": "custom" if custom else req.candidate, "n": base["n"],
               "base_score": base["score"], "cand_score": cand["score"], **cmp,
               "base_errors": base["errors"], "cand_errors": cand["errors"]}
    try:
        _save_report({"summary": summary, "baseline": base, "candidate": cand})
    except OSError as e:  # 기록 실패로 이미 과금된 결과를 버리지 않는다
        log.warning("eval 리포트 저장 실패: %s", e)
    return {"baseline": base, "candidate": cand, "compare": cmp, "summary": summary}
