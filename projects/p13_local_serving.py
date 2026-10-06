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
# 입력 상한 — 스레드 폭주·장시간 점유 방지
MAX_CONCURRENCY, MAX_LEVELS, MAX_REQUESTS, MAX_TOKENS = 16, 6, 64, 512


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
                      json={"model": model, "prompt": prompt, "stream": True,
                            "options": {"num_predict": max_tokens, "temperature": 0, "seed": 0}}) as r:  # 레벨 간 출력 길이를 고정
        if r.status_code != 200:  # 예: 받지 않은 모델 → 404 {"error": "model ... not found"}
            r.read()
            try:
                err = r.json().get("error")
            except ValueError:
                err = None
            raise RuntimeError(err or f"HTTP {r.status_code}")
        for line in r.iter_lines():
            if not line:
                continue
            d = json.loads(line)
            if d.get("error"):  # 스트림 도중 오류 (예: 메모리 부족)
                raise RuntimeError(d["error"])
            if ttft is None and (d.get("response") or d.get("thinking")):  # 추론 모델은 thinking 토큰이 먼저 나온다
                ttft = time.perf_counter() - t0
            if d.get("done"):
                final = d
    if not final:
        raise RuntimeError("응답이 완료 신호(done) 없이 끝났습니다")
    total = time.perf_counter() - t0
    out_tokens = final.get("eval_count", 0)
    gen_s = final.get("eval_duration", 0) / 1e9 or total
    return {"ttft_s": ttft or total, "total_s": total, "output_tokens": out_tokens,
            "decode_tps": out_tokens / gen_s if gen_s else 0,
            "prompt_tokens": final.get("prompt_eval_count", 0)}


def _try_one(model: str, prompt: str, max_tokens: int) -> dict:
    try:
        return _one(model, prompt, max_tokens)
    except Exception as e:  # 한 요청 실패가 레벨 전체를 0으로 만들지 않게 오류로 기록
        return {"error": str(e)[:200]}


class BenchReq(BaseModel):
    model: str
    concurrency: list[int] = [1, 2, 4]
    requests_per_level: int = 4
    max_tokens: int = 128


def _validate(req: BenchReq, models: list[str]) -> None:
    if not req.concurrency or len(req.concurrency) > MAX_LEVELS or any(not 1 <= c <= MAX_CONCURRENCY for c in req.concurrency):
        raise HTTPException(400, f"동시성 레벨은 1~{MAX_CONCURRENCY} 사이 정수를 쉼표로 구분해 최대 {MAX_LEVELS}개까지 입력하세요 (예: 1,2,4).")
    if not 1 <= req.requests_per_level <= MAX_REQUESTS:
        raise HTTPException(400, f"레벨당 요청 수는 1~{MAX_REQUESTS} 사이로 입력하세요.")
    if not 8 <= req.max_tokens <= MAX_TOKENS:
        raise HTTPException(400, f"max tokens는 8~{MAX_TOKENS} 사이로 입력하세요.")
    if req.model not in models and f"{req.model}:latest" not in models:
        raise HTTPException(400, f"Ollama에 없는 모델입니다: {req.model[:100]} (`ollama pull`로 먼저 받으세요)")


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


@router.post("/bench")
def bench(req: BenchReq):
    s = status()
    if not s["running"]:
        raise HTTPException(503, f"Ollama가 {OLLAMA} 에서 실행 중이 아닙니다. `ollama serve` 후 `ollama pull qwen2.5:0.5b` 하세요.")
    _validate(req, [m["name"] for m in s["models"]])
    try:
        _one(req.model, "hi", 8)  # 워밍업 (모델 로딩 시간 제외) — 모델 로드 실패를 여기서 먼저 알린다
    except Exception as e:
        raise HTTPException(502, f"Ollama 오류: {e}")
    levels = []
    for c in req.concurrency:
        jobs = [PROMPTS[i % len(PROMPTS)] for i in range(max(req.requests_per_level, c))]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=c) as ex:
            res = list(ex.map(lambda p: _try_one(req.model, p, req.max_tokens), jobs))
        wall = time.perf_counter() - t0
        ok = [r for r in res if "error" not in r]
        if not ok:
            raise HTTPException(502, f"동시성 {c}에서 모든 요청 실패: {res[0]['error']}")
        levels.append({
            "concurrency": c, "requests": len(res), "n": len(ok), "errors": len(res) - len(ok),
            "throughput_tps": round(sum(r["output_tokens"] for r in ok) / wall, 1),
            "req_per_s": round(len(ok) / wall, 2),
            "ttft_p50": round(statistics.median(r["ttft_s"] for r in ok), 3),
            "ttft_p95": round(_pct([r["ttft_s"] for r in ok], 95), 3),  # 표본이 20개 미만이면 사실상 최댓값
            "latency_p50": round(statistics.median(r["total_s"] for r in ok), 3),
            "latency_p95": round(_pct([r["total_s"] for r in ok], 95), 3),
            "decode_tps_per_req": round(statistics.mean(r["decode_tps"] for r in ok), 1),
        })
    return {"model": req.model, "levels": levels}
