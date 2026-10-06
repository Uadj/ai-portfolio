"""Claude API 공통 래퍼.

- 모든 프로젝트가 이 모듈을 통해 Claude를 호출한다.
- 기본 모델은 claude-opus-5-5, 서버측 refusal fallback("default")을 켠다.
- 토큰 사용량과 비용을 표준 형태(dict)로 돌려준다.
- 비용은 create/parse/stream 안에서 일일 예산에 바로 기록한다 (거절·잘림·중단된 스트림 포함).
  usage_of()는 순수 함수라 라우트에서 몇 번 불러도 이중 집계되지 않는다.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

import anthropic
import pydantic
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

# 입력/출력 $ per 1M tokens, 캐시 읽기 (캐시 쓰기는 입력 단가 x1.25)
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
WEB_SEARCH_USD = 0.01  # 웹 검색 1회당 (web_fetch는 토큰 비용만)

_client: anthropic.Anthropic | None = None

# 공개 배포 시 비용 보호: 하루 누적 비용이 한도를 넘으면 LLM 호출을 막는다.
# - 프로세스 메모리 기준: 재시작/슬립/재배포 시 0으로 초기화된다 (영구 상한은 Anthropic Console의 월 한도로).
# - 날짜 경계는 서버 로컬 시각 (Render는 UTC → 한국 시각 09:00).
# - 호출 전에 검사하고 호출 후에 기록하는 '소프트' 한도라, 동시에 진행 중인 호출만큼은 넘을 수 있다.
#   그래서 엔드포인트마다 입력 크기·호출 횟수 상한을 따로 둔다.
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
    return {"daily_budget_usd": DAILY_BUDGET_USD or None, "spent_today_usd": round(spent_today(), 4),
            "budget_scope": "process"}  # 재시작 시 초기화되는 프로세스 메모리 집계


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
    _record_spend(_cost(msg))  # 거절돼도 과금된 만큼은 예산에 반영
    check_refusal(msg)
    return msg


@contextlib.contextmanager
def stream(messages: list, **kw) -> Iterator[Any]:
    """스트리밍 컨텍스트 매니저 (with llm.stream(...) as s:).

    종료 시 비용을 기록한다. 클라이언트가 중간에 연결을 끊거나(GeneratorExit) 스트림 도중 오류가 나도
    그때까지 과금된 입력·출력이 예산에 잡힌다.
    """
    with client().beta.messages.stream(**build_params(messages, **kw)) as s:
        try:
            yield s
        finally:
            _record_spend(_stream_cost(s))


def parse(messages: list, output_format, **kw):
    """Pydantic 모델로 구조화 출력. 거절/잘림/스키마 불일치는 HTTPException으로 변환.

    SDK의 beta.messages.parse는 응답 검증을 호출 내부에서 해서, 잘린 JSON이나 중간 거절이면
    stop_reason을 보기 전에 ValidationError(→ 500)가 난다. 그래서 create로 받고 직접 검증한다.
    반환 메시지에는 기존과 같이 .parsed_output 속성이 붙는다.
    """
    adapter = pydantic.TypeAdapter(output_format)
    params = build_params(messages, **kw)
    oc = dict(params.get("output_config") or {})
    oc["format"] = {"type": "json_schema", "schema": anthropic.transform_schema(adapter.json_schema())}
    params["output_config"] = oc
    msg = client().beta.messages.create(**params)
    _record_spend(_cost(msg))
    check_refusal(msg)
    if msg.stop_reason == "max_tokens":
        raise HTTPException(502, "모델 출력이 max_tokens에서 잘렸습니다. 입력을 줄이거나 다시 시도해 주세요.")
    try:
        msg.parsed_output = adapter.validate_json(text_of(msg))
    except pydantic.ValidationError as e:
        raise HTTPException(502, "모델 출력이 스키마에 맞지 않습니다. 다시 시도해 주세요.") from e
    return msg


def text_of(msg) -> str:
    return "".join(b.text for b in msg.content if b.type == "text")


def _price(model: str | None) -> tuple[float, float, float]:
    """모델 단가. 날짜가 붙은 id(claude-haiku-4-5-20251001 등)는 가장 긴 접두사로 찾는다."""
    model = model or MODEL
    if model in PRICES:
        return PRICES[model]
    keys = [k for k in PRICES if model.startswith(k + "-")]
    if keys:
        return PRICES[max(keys, key=len)]
    return PRICES.get(MODEL) or PRICES["claude-opus-5-5"]


def _tokens(u) -> tuple[int, int, int, int]:
    return (getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0,
            getattr(u, "cache_read_input_tokens", 0) or 0, getattr(u, "cache_creation_input_tokens", 0) or 0)


def _tokens_cost(u, model: str | None) -> float:
    pin, pout, pcache = _price(model)
    inp, out, cread, cwrite = _tokens(u)
    return (inp * pin + out * pout + cread * pcache + cwrite * pin * 1.25) / 1_000_000


def _cost(msg) -> float:
    """실제 과금액 추정. fallbacks 사용 시 top-level usage는 '응답을 만든 시도'만 담으므로
    usage.iterations(시도별 기록: 거절된 시도 + 폴백 시도)가 있으면 그것을 합산한다."""
    u = getattr(msg, "usage", None)
    if u is None:
        return 0.0
    model = getattr(msg, "model", None) or MODEL
    its = [e for e in (getattr(u, "iterations", None) or [])
           if getattr(e, "type", "") in ("message", "fallback_message", "advisor_message", "compaction")]
    cost = sum(_tokens_cost(e, getattr(e, "model", None) or model) for e in its) if its else _tokens_cost(u, model)
    searches = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
    return cost + searches * WEB_SEARCH_USD


def _stream_cost(s) -> float:
    """스트림 종료 시점의 비용. message_start 전에 끊겼으면 0."""
    try:
        snap = s.current_message_snapshot
    except Exception:  # message_start 이전 (스냅샷 없음)
        return 0.0
    cost = _cost(snap)
    if snap.stop_reason is None:
        # 중간에 끊긴 스트림: output_tokens는 message_start 값에 머물러 있으므로 받은 텍스트 길이로 보수적으로 추정
        streamed = sum(len(getattr(b, "text", "") or "") for b in snap.content)
        cost += max(0, streamed // 2 - (snap.usage.output_tokens or 0)) * _price(snap.model)[1] / 1_000_000
    return cost


def usage_of(msg) -> dict:
    """응답의 토큰·비용 요약 (순수 함수 — 예산 기록은 create/parse/stream이 한다)."""
    inp, out, cread, cwrite = _tokens(msg.usage)
    return {
        "model": getattr(msg, "model", None) or MODEL,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cread,
        "cache_write_tokens": cwrite,
        "cost_usd": round(_cost(msg), 6),
    }


def sum_usage(items: Iterable[dict]) -> dict:
    total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0}
    models: list[str] = []
    for u in items:
        for k in total:
            total[k] += u.get(k, 0)
        m = u.get("model")
        if m and m not in models:
            models.append(m)
    total["cost_usd"] = round(total["cost_usd"], 6)
    if models:  # 표시용 (여러 모델이 섞이면 모두 나열). 항목에 모델이 없으면 키를 넣지 않는다
        total["model"] = " + ".join(models)
    return total
