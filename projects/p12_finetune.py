"""#12 소형 모델 파인튜닝 — 웹에서는 데이터 합성(교사 모델)을 실시간 실행하고, 학습/평가 결과는 results.json에서 읽는다."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core import llm

router = APIRouter(prefix="/api/p12")
FT = llm.ROOT / "p12_finetune"
HANDBOOK = llm.ROOT / "data" / "handbook"
# 합성 대상 문서 허용 목록 — 핸드북은 정적 파일이라 import 시 한 번만 계산 (status 목록과 검증이 어긋나지 않도록 공유)
DOCS = frozenset(p.name for p in HANDBOOK.glob("*.md"))

_SYNTH = None
_SYNTH_LOCK = threading.Lock()


def _synth():
    """synth.py를 한 번만 로드 (동시 요청이 반쯤 만들어진 모듈을 보지 않도록 잠금)."""
    global _SYNTH
    with _SYNTH_LOCK:
        if _SYNTH is None:
            spec = importlib.util.spec_from_file_location("synth", FT / "synth.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["synth"] = mod  # Pydantic이 문자열 어노테이션을 해석하려면 모듈 등록이 필요
            try:
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop("synth", None)  # 실패 시 캐시하지 않음 → 다음 요청에서 재시도
                raise
            _SYNTH = mod
    return _SYNTH


@router.get("/status")
def status():
    def count(p):
        return sum(1 for _ in open(p, encoding="utf-8")) if p.exists() else 0

    results = json.loads((FT / "results.json").read_text(encoding="utf-8")) if (FT / "results.json").exists() else None
    return {
        "docs": sorted(DOCS),
        "train": count(FT / "data" / "train.jsonl"),
        "test": count(FT / "data" / "test.jsonl"),
        "torch_installed": importlib.util.find_spec("torch") is not None,
        "results": results,
        "scripts": {name: (FT / name).read_text(encoding="utf-8") for name in ("build_dataset.py", "train_lora.py", "evaluate.py")},
    }


class GenReq(BaseModel):
    doc: str = Field(max_length=100)
    n: int = 5  # 범위 밖 값은 거부하지 않고 1~10으로 보정 (기존 동작 유지)


@router.post("/generate")
def generate(req: GenReq):
    # 경로 조작 방지: 핸드북 파일명만 허용 (모듈 로드·예산 사용 전에 거른다)
    if req.doc not in DOCS:
        raise HTTPException(400, "알 수 없는 문서입니다.")
    items, usage = _synth().synthesize(req.doc, max(1, min(req.n, 10)))
    return {"items": items, "usage": usage, "grounded_rate": round(sum(i["grounded"] for i in items) / max(len(items), 1), 3)}
