"""#13 로컬 LLM 서빙 벤치마크 — Ollama에 동시 요청을 보내 TTFT, 처리량(tokens/s), p50/p95 지연시간을 측정."""
from __future__ import annotations

import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/p13")
OLLAMA = os.getenv("OLLAMA_URL", "http://localhost:11434")

PROMPTS = [
    "RAG가 무엇인지 3문장으로 설명해줘.",
    "파이썬에서 리스트와 튜플의 차이를 설명해줘.",
    "양자화(quantization)가 LLM 추론 속도에 주는 영향은?",
    "좋은 API 설계 원칙 5가지를 알려줘.",
]


@router.get("/status")
def status():
    try:
        r = httpx.get(f"{OLLAMA}/api/tags", timeout=3)
        models = [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 2),
                   "quant": m.get("details", {}).get("quantization_level"),
                   "params": m.get("details", {}).get("parameter_size")} for m in r.json().get("models", [])]
        return {"running": True, "url": OLLAMA, "models": models}
    except Exception as e:
        return {"running": False, "url": OLLAMA, "error": str(e)}


def _one(model: str, prompt: str, max_tokens: int) -> dict:
    t0 = time.perf_counter()
    ttft = None
    final = {}
    with httpx.stream("POST", f"{OLLAMA}/api/generate", timeout=300,
                      json={"model": model, "prompt": prompt, "stream": True, "options": {"num_predict": max_tokens}}) as r:
        for line in r.iter_lines():
            if not line:
                continue
            d = json.loads(line)
            if ttft is None and d.get("response"):
                ttft = time.perf_counter() - t0
            if d.get("done"):
                final = d
    total = time.perf_counter() - t0
    out_tokens = final.get("eval_count", 0)
    gen_s = final.get("eval_duration", 0) / 1e9 or total
    return {"ttft_s": ttft or total, "total_s": total, "output_tokens": out_tokens,
            "decode_tps": out_tokens / gen_s if gen_s else 0,
            "prompt_tokens": final.get("prompt_eval_count", 0)}


class BenchReq(BaseModel):
    model: str
    concurrency: list[int] = [1, 2, 4]
    requests_per_level: int = 4
    max_tokens: int = 128


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


@router.post("/bench")
def bench(req: BenchReq):
    if not status()["running"]:
        raise HTTPException(503, f"Ollama가 {OLLAMA} 에서 실행 중이 아닙니다. `ollama serve` 후 `ollama pull qwen2.5:0.5b` 하세요.")
    _one(req.model, "hi", 8)  # 워밍업 (모델 로딩 시간 제외)
    levels = []
    for c in req.concurrency:
        jobs = [PROMPTS[i % len(PROMPTS)] for i in range(max(req.requests_per_level, c))]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=c) as ex:
            res = list(ex.map(lambda p: _one(req.model, p, req.max_tokens), jobs))
        wall = time.perf_counter() - t0
        levels.append({
            "concurrency": c, "requests": len(res),
            "throughput_tps": round(sum(r["output_tokens"] for r in res) / wall, 1),
            "req_per_s": round(len(res) / wall, 2),
            "ttft_p50": round(statistics.median(r["ttft_s"] for r in res), 3),
            "ttft_p95": round(_pct([r["ttft_s"] for r in res], 95), 3),
            "latency_p50": round(statistics.median(r["total_s"] for r in res), 3),
            "latency_p95": round(_pct([r["total_s"] for r in res], 95), 3),
            "decode_tps_per_req": round(statistics.mean(r["decode_tps"] for r in res), 1),
        })
    return {"model": req.model, "levels": levels}
