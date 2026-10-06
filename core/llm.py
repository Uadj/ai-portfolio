"""Claude API 공통 래퍼.

- 모든 프로젝트가 이 모듈을 통해 Claude를 호출한다.
- 기본 모델은 claude-opus-5-5, 서버측 refusal fallback("default")을 켠다.
- 토큰 사용량과 비용을 표준 형태(dict)로 돌려준다.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

import anthropic
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """ai-portfolio/.env 를 읽어 환경변수로 등록 (python-dotenv 없이)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
FAST_MODEL = os.getenv("CLAUDE_FAST_MODEL", "claude-haiku-4-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# 입력/출력 $ per 1M tokens, 캐시 읽기
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}

_client: anthropic.Anthropic | None = None

# 공개 배포 시 비용 보호: 하루 누적 비용이 한도를 넘으면 LLM 호출을 막는다 (프로세스 메모리 기준)
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "0") or 0)  # 0 = 무제한
_spend = {"day": "", "usd": 0.0}
_spend_lock = threading.Lock()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def spent_today() -> float:
    with _spend_lock:
        return _spend["usd"] if _spend["day"] == _today() else 0.0


def _record_spend(usd: float) -> None:
    with _spend_lock:
        if _spend["day"] != _today():
            _spend.update(day=_today(), usd=0.0)
        _spend["usd"] += usd


def budget_status() -> dict:
    return {"daily_budget_usd": DAILY_BUDGET_USD or None, "spent_today_usd": round(spent_today(), 4)}


def has_credentials() -> bool:
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()


def client() -> anthropic.Anthropic:
    global _client
    if not has_credentials():
        raise HTTPException(
            503,
            "Claude API 키가 설정되지 않았습니다. ai-portfolio/.env 에 ANTHROPIC_API_KEY=... 를 넣고 서버를 재시작하세요.",
        )
    if DAILY_BUDGET_USD and spent_today() >= DAILY_BUDGET_USD:
        raise HTTPException(429, f"오늘의 데모 사용 한도(${DAILY_BUDGET_USD:g})를 모두 썼습니다. 내일 다시 시도해 주세요. LLM 없이 동작하는 측정 기능은 계속 쓸 수 있습니다.")
    if _client is None:
        _client = anthropic.Anthropic(max_retries=3)
    return _client


def _supports_fallback(model: str) -> bool:
    return model in {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


def _supports_effort(model: str) -> bool:
    return not model.startswith("claude-haiku")


def build_params(
    messages: list,
    *,
    system: Any = None,
    model: str | None = None,
    max_tokens: int = 16000,
    effort: str | None = "low",
    output_config: dict | None = None,
    **extra,
) -> dict:
    model = model or MODEL
    params: dict = {"model": model, "max_tokens": max_tokens, "messages": messages, **extra}
    if system is not None:
        params["system"] = system
    oc = dict(output_config or {})
    if effort and _supports_effort(model):
        oc.setdefault("effort", effort)
    if oc:
        params["output_config"] = oc
    if _supports_fallback(model):
        params["betas"] = [FALLBACK_BETA, *params.get("betas", [])]
        params["fallbacks"] = "default"
    return params


def check_refusal(msg) -> None:
    if msg.stop_reason == "refusal":
        cat = getattr(getattr(msg, "stop_details", None), "category", None)
        raise HTTPException(422, f"모델이 요청을 거절했습니다 (category={cat}).")


def create(messages: list, **kw):
    """비스트리밍 호출. 응답 메시지를 반환."""
    msg = client().beta.messages.create(**build_params(messages, **kw))
    check_refusal(msg)
    return msg


def stream(messages: list, **kw):
    """스트리밍 컨텍스트 매니저를 반환 (with llm.stream(...) as s:)."""
    return client().beta.messages.stream(**build_params(messages, **kw))


def parse(messages: list, output_format, **kw):
    """Pydantic 모델로 구조화 출력."""
    msg = client().beta.messages.parse(output_format=output_format, **build_params(messages, **kw))
    check_refusal(msg)
    return msg


def text_of(msg) -> str:
    return "".join(b.text for b in msg.content if b.type == "text")


def usage_of(msg) -> dict:
    u = msg.usage
    model = getattr(msg, "model", MODEL)
    pin, pout, pcache = PRICES.get(model, PRICES["claude-opus-5-5"])
    inp = u.input_tokens or 0
    out = u.output_tokens or 0
    cread = getattr(u, "cache_read_input_tokens", 0) or 0
    cwrite = getattr(u, "cache_creation_input_tokens", 0) or 0
    cost = (inp * pin + out * pout + cread * pcache + cwrite * pin * 1.25) / 1_000_000
    _record_spend(cost)
    return {
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cread,
        "cache_write_tokens": cwrite,
        "cost_usd": round(cost, 6),
    }


def sum_usage(items: Iterable[dict]) -> dict:
    total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0}
    for u in items:
        for k in total:
            total[k] += u.get(k, 0)
    total["cost_usd"] = round(total["cost_usd"], 6)
    return total
