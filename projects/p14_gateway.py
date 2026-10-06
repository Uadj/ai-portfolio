"""#14 LLM 게이트웨이 — 라우팅 · 응답 캐시 · 프롬프트 캐싱 · 레이트 리밋 · 폴백 · 비용 대시보드."""
from __future__ import annotations

import hashlib
import re
import statistics
import threading
import time
from collections import defaultdict, deque

import anthropic
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core import llm

router = APIRouter(prefix="/api/p14")

# 프롬프트 캐싱 대상: 매 요청 동일한 긴 시스템 프롬프트 (사내 규정 전체)
KNOWLEDGE = "\n\n".join(p.read_text(encoding="utf-8") for p in sorted((llm.ROOT / "data" / "handbook").glob("*.md")))
SYSTEM = [{"type": "text", "text": "당신은 루미나랩스 사내 도우미입니다. 아래 규정을 근거로 간결히 한국어로 답하세요.\n\n" + KNOWLEDGE,
           "cache_control": {"type": "ephemeral"}}]

COMPLEX_HINTS = re.compile(r"(분석|비교|설계|전략|왜|이유|코드|계획|단계별|장단점|추론)")


class TokenBucket:
    def __init__(self, rate_per_min: float, capacity: int):
        self.rate, self.capacity = rate_per_min / 60, capacity
        self.state: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def take(self, key: str) -> tuple[bool, float]:
        with self.lock:
            tokens, ts = self.state.get(key, (self.capacity, time.time()))
            now = time.time()
            tokens = min(self.capacity, tokens + (now - ts) * self.rate)
            if tokens < 1:
                self.state[key] = (tokens, now)
                return False, (1 - tokens) / self.rate
            self.state[key] = (tokens - 1, now)
            return True, 0.0


class Gateway:
    def __init__(self):
        self.cache: dict[str, tuple[float, dict]] = {}
        self.ttl = 600
        self.bucket = TokenBucket(rate_per_min=12, capacity=6)
        self.log: deque = deque(maxlen=50)
        self.stats = defaultdict(float)
        self.by_model = defaultdict(lambda: {"requests": 0, "cost_usd": 0.0})
        self.latencies: deque = deque(maxlen=200)
        self.lock = threading.Lock()

    @staticmethod
    def key(prompt: str) -> str:
        norm = re.sub(r"[\s?!.]+", " ", prompt.lower()).strip()
        return hashlib.sha256(norm.encode()).hexdigest()[:16]

    @staticmethod
    def route(prompt: str, requested: str) -> tuple[str, str]:
        if requested != "auto":
            return requested, "사용자 지정"
        if len(prompt) > 300 or COMPLEX_HINTS.search(prompt):
            return llm.MODEL, "복잡한 요청(길이/키워드) → 상위 모델"
        return llm.FAST_MODEL, "단순 조회 → 경량 모델"

    def record(self, entry: dict):
        with self.lock:
            self.log.appendleft(entry)
            self.stats["requests"] += 1
            if entry["status"] == "cache_hit":
                self.stats["cache_hits"] += 1
                self.stats["saved_usd"] += entry.get("saved_usd", 0)
            elif entry["status"] == "rate_limited":
                self.stats["rate_limited"] += 1
            elif entry["status"] == "ok":
                self.stats["cost_usd"] += entry["usage"]["cost_usd"]
                self.stats["cache_read_tokens"] += entry["usage"]["cache_read_tokens"]
                self.stats["cache_write_tokens"] += entry["usage"]["cache_write_tokens"]
                if entry.get("fallback_used"):
                    self.stats["fallbacks"] += 1
                m = self.by_model[entry["model"]]
                m["requests"] += 1
                m["cost_usd"] += entry["usage"]["cost_usd"]
                self.latencies.append(entry["latency_ms"])


gw = Gateway()


class ChatReq(BaseModel):
    prompt: str
    model: str = "auto"
    use_cache: bool = True
    simulate_primary_failure: bool = False  # 폴백 데모용: 1차 모델 호출을 인위적으로 실패시킴


@router.post("/chat")
def chat(req: ChatReq, request: Request):
    client_id = request.client.host if request.client else "anon"
    t0 = time.perf_counter()
    ok, wait = gw.bucket.take(client_id)
    if not ok:
        gw.record({"status": "rate_limited", "prompt": req.prompt[:60], "at": time.strftime("%H:%M:%S")})
        raise HTTPException(429, f"레이트 리밋 초과 — {wait:.1f}초 후 다시 시도하세요 (분당 12회, 버스트 6회).")

    k = gw.key(req.prompt)
    if req.use_cache and k in gw.cache and time.time() - gw.cache[k][0] < gw.ttl:
        cached = gw.cache[k][1]
        entry = {"status": "cache_hit", "prompt": req.prompt[:60], "model": cached["model"], "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                 "saved_usd": cached["usage"]["cost_usd"], "at": time.strftime("%H:%M:%S")}
        gw.record(entry)
        return {**cached, "cache": "hit", "latency_ms": entry["latency_ms"], "route_reason": "응답 캐시 적중 (LLM 호출 없음)"}

    primary, reason = gw.route(req.prompt, req.model)
    chain = [primary] + [m for m in (llm.MODEL, llm.FAST_MODEL) if m != primary]
    attempts = []
    msg = None
    for i, model in enumerate(chain):
        try:
            if i == 0 and req.simulate_primary_failure:
                raise RuntimeError("simulated 529 overloaded")
            msg = llm.create([{"role": "user", "content": req.prompt}], system=SYSTEM, model=model, effort="low", max_tokens=2000)
            attempts.append({"model": model, "ok": True})
            break
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError, RuntimeError) as e:
            attempts.append({"model": model, "ok": False, "error": str(e)[:120]})
        except anthropic.APIStatusError as e:
            if e.status_code < 500:
                raise
            attempts.append({"model": model, "ok": False, "error": f"{e.status_code}"})
    if msg is None:
        raise HTTPException(502, f"모든 모델 호출 실패: {attempts}")

    usage = llm.usage_of(msg)
    result = {"answer": llm.text_of(msg), "model": usage["model"], "usage": usage, "attempts": attempts}
    if req.use_cache:
        gw.cache[k] = (time.time(), result)
    latency = round((time.perf_counter() - t0) * 1000, 1)
    gw.record({"status": "ok", "prompt": req.prompt[:60], "model": usage["model"], "usage": usage, "latency_ms": latency,
               "fallback_used": len(attempts) > 1, "at": time.strftime("%H:%M:%S")})
    return {**result, "cache": "miss", "latency_ms": latency, "route_reason": reason}


@router.get("/stats")
def stats():
    s = dict(gw.stats)
    lat = list(gw.latencies)
    hit_rate = s.get("cache_hits", 0) / s["requests"] if s.get("requests") else 0
    return {
        "requests": int(s.get("requests", 0)), "cache_hits": int(s.get("cache_hits", 0)), "cache_hit_rate": round(hit_rate, 3),
        "rate_limited": int(s.get("rate_limited", 0)), "fallbacks": int(s.get("fallbacks", 0)),
        "cost_usd": round(s.get("cost_usd", 0), 6), "saved_usd": round(s.get("saved_usd", 0), 6),
        "prompt_cache_read_tokens": int(s.get("cache_read_tokens", 0)), "prompt_cache_write_tokens": int(s.get("cache_write_tokens", 0)),
        "latency_p50_ms": round(statistics.median(lat), 1) if lat else None,
        "by_model": {k: {"requests": v["requests"], "cost_usd": round(v["cost_usd"], 6)} for k, v in gw.by_model.items()},
        "log": list(gw.log)[:20],
    }


@router.post("/reset")
def reset():
    global gw
    gw = Gateway()
    return {"ok": True}
