"""#14 LLM 게이트웨이 — 라우팅 · 응답 캐시 · 프롬프트 캐싱 · 레이트 리밋 · 폴백 · 비용 대시보드.

공개 데모라서 상태를 둘로 나눈다.
- 전역 공유: 응답 캐시(CACHE), 레이트 리밋 버킷(BUCKET) — 누구도 초기화할 수 없다.
- 방문자별: 대시보드 로그·집계(View) — 쿠키로 구분해 다른 방문자의 프롬프트가 보이지 않게 한다.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import statistics
import threading
import time
import unicodedata
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

import anthropic
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from core import llm, net

router = APIRouter(prefix="/api/p14")

# ---------- 프롬프트 캐싱 ----------
# 매 요청 바이트 단위로 동일한 시스템 프리픽스 = 운영 지침 + 사내 규정 전체 (시각·ID 등 가변 값을 넣지 않는다).
# 모델별 최소 캐시 길이(Haiku 4.5는 4096토큰)를 넘어야 실제로 캐시되므로, 답변 품질에 쓰이는 지침·예시를 함께 둔다.
GUIDE = (Path(__file__).with_name("p14_gateway_guide.md")).read_text(encoding="utf-8").strip()
KNOWLEDGE = "\n\n".join(p.read_text(encoding="utf-8") for p in sorted((llm.ROOT / "data" / "handbook").glob("*.md")))
SYSTEM_TEXT = GUIDE + "\n\n<규정 문서>\n" + KNOWLEDGE + "\n</규정 문서>"
SYSTEM = [{"type": "text", "text": SYSTEM_TEXT, "cache_control": {"type": "ephemeral"}}]

# 모델별 최소 캐시 프리픽스 (claude-api shared/prompt-caching.md 기준). 이보다 짧으면 표시가 있어도 조용히 캐시되지 않는다.
CACHE_MIN_TOKENS = {
    "claude-opus-5-5": 512, "claude-opus-5": 512, "claude-fable-5-1": 512, "claude-fable-5": 512, "claude-sonnet-5-5": 512,
    "claude-sonnet-5": 1024, "claude-opus-4-8": 1024, "claude-sonnet-4-6": 1024, "claude-sonnet-4-5": 1024,
    "claude-opus-4-7": 2048, "claude-opus-4-6": 4096, "claude-opus-4-5": 4096, "claude-haiku-4-5": 4096,
}


def cache_min_tokens(model: str) -> int | None:
    """날짜가 붙은 id(claude-haiku-4-5-20251001)도 가장 긴 접두사로 찾는다."""
    keys = [k for k in CACHE_MIN_TOKENS if model == k or model.startswith(k + "-")]
    return CACHE_MIN_TOKENS[max(keys, key=len)] if keys else None


# 실측: 모델별 마지막 호출의 프롬프트 토큰과 캐시된 프리픽스 토큰 (usage가 유일한 근거)
_prefix: dict[str, dict] = {}
_prefix_lock = threading.Lock()


def _measure(model: str, usage: dict) -> None:
    cached = usage["cache_read_tokens"] + usage["cache_write_tokens"]
    with _prefix_lock:
        m = _prefix.setdefault(model, {"prefix_tokens": None})
        m["prompt_tokens"] = usage["input_tokens"] + cached
        m["cache_active"] = cached > 0
        if cached:
            m["prefix_tokens"] = cached  # 캐시 지점이 시스템 끝 하나뿐이라 읽기/쓰기 토큰 = 프리픽스 길이


def prompt_cache_info() -> dict:
    models = {}
    for model in dict.fromkeys((llm.MODEL, llm.FAST_MODEL)):
        mn = cache_min_tokens(model)
        with _prefix_lock:
            m = dict(_prefix.get(model, {}))
        if not m:
            note = f"아직 측정 전 (최소 {mn:,}토큰)" if mn else "아직 측정 전"
        elif m["cache_active"]:
            note = f"적용됨 — 프리픽스 {m['prefix_tokens']:,}토큰" + (f" ≥ 최소 {mn:,}" if mn else "")
        elif mn and m["prompt_tokens"] < mn:
            note = f"미적용 — 프롬프트 {m['prompt_tokens']:,}토큰 < 최소 {mn:,}"
        else:
            note = "미적용 — 캐시 기록이 없습니다"
        models[model] = {"min_tokens": mn, "prefix_tokens": m.get("prefix_tokens"), "last_prompt_tokens": m.get("prompt_tokens"),
                         "cache_active": m.get("cache_active"), "note": note}
    return {"prefix_chars": len(SYSTEM_TEXT), "models": models}


# ---------- 라우팅 · 응답 캐시 · 레이트 리밋 ----------
COMPLEX_HINTS = re.compile(r"(분석|비교|설계|전략|왜|이유|코드|계획|단계별|장단점|추론)")
ALLOWED_MODELS = {"auto", llm.MODEL, llm.FAST_MODEL}  # 임의 모델 id(고가 모델)로 예산을 우회하지 못하게
MAX_PROMPT = 2000
MAX_TOKENS = 8000  # Opus는 사고 토큰도 max_tokens에 포함 — 단계별 장문 답변이 잘리지 않을 여유 (비스트리밍 안전 범위)


def route(prompt: str, requested: str) -> tuple[str, str]:
    if requested != "auto":
        return requested, "사용자 지정"
    if len(prompt) > 300 or COMPLEX_HINTS.search(prompt):
        return llm.MODEL, "복잡한 요청(길이/키워드) → 상위 모델"
    return llm.FAST_MODEL, "단순 조회 → 경량 모델"


def cache_key(model: str, prompt: str) -> str:
    """모델 + 정규화한 프롬프트(NFKC, 대소문자·공백·문장부호 무시)의 해시."""
    norm = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", prompt).casefold()) or prompt
    return hashlib.sha256(f"{model}\x00{norm}".encode()).hexdigest()[:16]


class ResponseCache:
    def __init__(self, ttl: float = 600, max_items: int = 500):
        self.ttl, self.max_items = ttl, max_items
        self.data: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, k: str) -> dict | None:
        with self.lock:
            hit = self.data.get(k)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            self.data.pop(k, None)
            return None

    def put(self, k: str, value: dict) -> None:
        with self.lock:
            self.data[k] = (time.time(), value)
            self.data.move_to_end(k)
            while len(self.data) > self.max_items:  # 오래된 항목부터 제거 (메모리 상한)
                self.data.popitem(last=False)


class TokenBucket:
    MAX_KEYS = 5000

    def __init__(self, rate_per_min: float, capacity: int):
        self.rate, self.capacity = rate_per_min / 60, capacity
        self.state: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def take(self, key: str) -> tuple[bool, float]:
        with self.lock:
            now = time.time()
            if len(self.state) > self.MAX_KEYS:  # 다 채워진 버킷은 '키 없음'과 같으므로 지워도 동작이 같다
                full = self.capacity / self.rate
                self.state = {k: v for k, v in self.state.items() if now - v[1] < full}
            tokens, ts = self.state.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - ts) * self.rate)
            if tokens < 1:
                self.state[key] = (tokens, now)
                return False, (1 - tokens) / self.rate
            self.state[key] = (tokens - 1, now)
            return True, 0.0


CACHE = ResponseCache(ttl=600)
BUCKET = TokenBucket(rate_per_min=12, capacity=6)


# ---------- 방문자별 대시보드 ----------
class View:
    def __init__(self):
        self.log: deque = deque(maxlen=50)
        self.stats = defaultdict(float)
        self.by_model = defaultdict(lambda: {"requests": 0, "cost_usd": 0.0})
        self.latencies: deque = deque(maxlen=200)
        self.lock = threading.Lock()

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

    def snapshot(self) -> dict:
        with self.lock:
            s, lat = dict(self.stats), list(self.latencies)
            by_model = {k: {"requests": v["requests"], "cost_usd": round(v["cost_usd"], 6)} for k, v in self.by_model.items()}
            log = list(self.log)[:20]
        hit_rate = s.get("cache_hits", 0) / s["requests"] if s.get("requests") else 0
        return {
            "requests": int(s.get("requests", 0)), "cache_hits": int(s.get("cache_hits", 0)), "cache_hit_rate": round(hit_rate, 3),
            "rate_limited": int(s.get("rate_limited", 0)), "fallbacks": int(s.get("fallbacks", 0)),
            "cost_usd": round(s.get("cost_usd", 0), 6), "saved_usd": round(s.get("saved_usd", 0), 6),
            "prompt_cache_read_tokens": int(s.get("cache_read_tokens", 0)), "prompt_cache_write_tokens": int(s.get("cache_write_tokens", 0)),
            "latency_p50_ms": round(statistics.median(lat), 1) if lat else None,
            "by_model": by_model, "log": log,
        }


MAX_VIEWS = 1000
VIEWS: OrderedDict[str, View] = OrderedDict()
_views_lock = threading.Lock()
SID_COOKIE = "gw_sid"
_SID_RE = re.compile(r"[A-Za-z0-9_-]{8,32}")


def _sid(request: Request, response: Response) -> str:
    """방문자 id 쿠키 (IP는 X-Forwarded-For로 위조할 수 있어 대시보드 구분에 쓰지 않는다)."""
    sid = request.cookies.get(SID_COOKIE, "")
    if _SID_RE.fullmatch(sid):
        return sid
    sid = secrets.token_urlsafe(9)
    response.set_cookie(SID_COOKIE, sid, max_age=86400, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return sid


def _view(sid: str, create: bool = True) -> View | None:
    with _views_lock:
        v = VIEWS.get(sid)
        if v is None:
            if not create:
                return None
            v = VIEWS[sid] = View()
        VIEWS.move_to_end(sid)
        while len(VIEWS) > MAX_VIEWS:  # 오래 안 온 방문자부터 정리
            VIEWS.popitem(last=False)
        return v


def _cookie_header(response: Response) -> dict | None:
    """예외 응답에는 주입된 Response의 헤더가 실리지 않으므로 새로 발급한 쿠키를 직접 옮긴다."""
    c = response.headers.get("set-cookie")
    return {"set-cookie": c} if c else None


class ChatReq(BaseModel):
    prompt: str
    model: str = "auto"
    use_cache: bool = True
    simulate_primary_failure: bool = False  # 폴백 데모용: 1차 모델 호출을 인위적으로 실패시킴


@router.post("/chat")
def chat(req: ChatReq, request: Request, response: Response):
    # 입력 검증은 버킷·캐시보다 먼저 (잘못된 요청이 토큰을 쓰거나 로그를 남기지 않게)
    if req.model not in ALLOWED_MODELS:
        raise HTTPException(400, "지원하지 않는 모델입니다.")
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(400, "프롬프트를 입력하세요.")
    if len(prompt) > MAX_PROMPT:
        raise HTTPException(400, f"입력이 너무 깁니다 (최대 {MAX_PROMPT}자).")

    view = _view(_sid(request, response))
    client_id = net.client_ip(request)  # request.client.host는 X-Forwarded-For 왼쪽 값(위조 가능)이라 쓰지 않는다
    t0 = time.perf_counter()
    ok, wait = BUCKET.take(client_id)
    if not ok:
        view.record({"status": "rate_limited", "prompt": prompt[:60], "at": time.strftime("%H:%M:%S")})
        raise HTTPException(429, f"레이트 리밋 초과 — {wait:.1f}초 후 다시 시도하세요 (분당 12회, 버스트 6회).", headers=_cookie_header(response))

    # 라우팅을 먼저 해서 캐시 키에 모델을 포함 — 모델을 바꾸면 다른 모델의 답이 나오지 않는다
    primary, reason = route(prompt, req.model)
    k = cache_key(primary, prompt)
    use_cache = req.use_cache and not req.simulate_primary_failure  # 장애 시뮬레이션은 항상 실제 폴백 경로를 탄다
    cached = CACHE.get(k) if use_cache else None
    if cached:
        entry = {"status": "cache_hit", "prompt": prompt[:60], "model": cached["model"], "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                 "saved_usd": cached["usage"]["cost_usd"], "at": time.strftime("%H:%M:%S")}
        view.record(entry)
        return {**cached, "cache": "hit", "latency_ms": entry["latency_ms"], "route_reason": "응답 캐시 적중 (LLM 호출 없음)"}

    chain = [primary] + [m for m in (llm.MODEL, llm.FAST_MODEL) if m != primary]
    attempts = []
    msg = used = None
    for i, model in enumerate(chain):
        try:
            if i == 0 and req.simulate_primary_failure:
                raise RuntimeError("simulated 529 overloaded")
            msg = llm.create([{"role": "user", "content": prompt}], system=SYSTEM, model=model, effort="low", max_tokens=MAX_TOKENS)
            attempts.append({"model": model, "ok": True})
            used = model
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
    _measure(used, usage)
    truncated = msg.stop_reason == "max_tokens"
    answer = llm.text_of(msg)
    if truncated:  # 잘린 답을 완결된 답처럼 보이지 않게 (자동 재시도는 비용이 두 배라 하지 않음)
        answer += "\n\n_(응답이 길이 제한으로 잘렸습니다)_"
    result = {"answer": answer, "model": usage["model"], "usage": usage, "attempts": attempts,
              "stop_reason": msg.stop_reason, "truncated": truncated}
    # 1차 모델이 정상 종료한 답만 그 모델의 키로 캐시 (폴백 답·잘린 답은 캐시하지 않음)
    if use_cache and len(attempts) == 1 and msg.stop_reason == "end_turn":
        CACHE.put(k, result)
    latency = round((time.perf_counter() - t0) * 1000, 1)
    view.record({"status": "ok", "prompt": prompt[:60], "model": usage["model"], "usage": usage, "latency_ms": latency,
                 "fallback_used": len(attempts) > 1, "truncated": truncated, "at": time.strftime("%H:%M:%S")})
    return {**result, "cache": "miss", "latency_ms": latency, "route_reason": reason}


@router.get("/stats")
def stats(request: Request, response: Response):
    # 대시보드가 열릴 때 호출되므로 여기서 방문자 쿠키를 먼저 발급해 둔다
    v = _view(_sid(request, response), create=False) or View()
    return {**v.snapshot(), "prompt_cache": prompt_cache_info()}


@router.post("/reset")
def reset(request: Request, response: Response):
    """내 대시보드만 초기화 — 공유 응답 캐시와 레이트 리밋 상태는 건드리지 않는다."""
    sid = _sid(request, response)
    with _views_lock:
        VIEWS.pop(sid, None)
    return {"ok": True}
