"""#12 소형 모델 파인튜닝 — 웹에서는 데이터 합성(교사 모델)을 실시간 실행하고, 학습/평가 결과는 results.json에서 읽는다."""
from __future__ import annotations

import importlib.util
import json
import sys

from fastapi import APIRouter
from pydantic import BaseModel

from core import llm

router = APIRouter(prefix="/api/p12")
FT = llm.ROOT / "p12_finetune"


def _synth():
    spec = importlib.util.spec_from_file_location("synth", FT / "synth.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["synth"] = mod  # Pydantic이 문자열 어노테이션을 해석하려면 모듈 등록이 필요
    spec.loader.exec_module(mod)
    return mod


@router.get("/status")
def status():
    def count(p):
        return sum(1 for _ in open(p, encoding="utf-8")) if p.exists() else 0

    results = json.loads((FT / "results.json").read_text(encoding="utf-8")) if (FT / "results.json").exists() else None
    return {
        "docs": sorted(p.name for p in (llm.ROOT / "data" / "handbook").glob("*.md")),
        "train": count(FT / "data" / "train.jsonl"),
        "test": count(FT / "data" / "test.jsonl"),
        "torch_installed": importlib.util.find_spec("torch") is not None,
        "results": results,
        "scripts": {name: (FT / name).read_text(encoding="utf-8") for name in ("build_dataset.py", "train_lora.py", "evaluate.py")},
    }


class GenReq(BaseModel):
    doc: str
    n: int = 5


@router.post("/generate")
def generate(req: GenReq):
    items, usage = _synth().synthesize(req.doc, max(1, min(req.n, 10)))
    return {"items": items, "usage": usage, "grounded_rate": round(sum(i["grounded"] for i in items) / max(len(items), 1), 3)}
