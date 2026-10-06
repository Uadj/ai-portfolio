"""#08 자율 리서치 에이전트 — 웹 검색·열람 서버 툴로 다단계 조사 후 출처가 달린 보고서 작성 (SSE로 진행 상황 스트리밍).

비용 상한 (공개 데모, 일일 예산 공유):
- 주제 300자, 검색 1~10회. max_uses는 'API 요청 1건당' 한도라 pause_turn으로 재개할 때마다 초기화되므로,
  이미 쓴 횟수(usage.server_tool_use)를 빼서 다음 요청의 max_uses로 넘긴다.
- web_fetch는 실행당 최대 2회, 페이지당 max_content_tokens=6000 (긴 문서 한 건이 입력 토큰을 폭증시키지 않게).
- pause_turn 재개는 최대 2회, 요청당 max_tokens 16000(사고 + 보고서).
출처 검증: 보고서 '## 출처'의 URL을 이번 실행에서 실제로 검색·열람·인용된 URL과 대조해 verified/unverified로 나눈다.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import llm, sse

router = APIRouter(prefix="/api/p08")

MAX_TOPIC = 300
MAX_SEARCHES = 10
MAX_FETCHES = 2
FETCH_MAX_TOKENS = 6000
MAX_RESUMES = 2

SYSTEM = """당신은 리서치 에이전트입니다.
1) 주제를 3~5개의 하위 질문으로 나누는 짧은 조사 계획을 먼저 쓰세요.
2) 하위 질문마다 web_search로 검색하고, 필요하면 web_fetch로 원문을 읽으세요. 서로 다른 출처를 교차 확인하세요.
   검색 횟수가 제한되어 있으니 계획에 맞춰 아껴 쓰세요.
3) 마지막에 한국어 마크다운 보고서를 작성하세요: '## 요약', 하위 질문별 섹션, '## 한계와 불확실성'.
   주장마다 출처를 [번호]로 표시하고 마지막에 '## 출처' 목록(제목 - URL)을 붙이세요.
   출처 목록에는 이번 조사에서 실제로 검색 결과로 받았거나 열람한 URL만 쓰세요."""

_URL = re.compile(r"https?://[^\s)\]<>\"'`]+")
_TRAIL = ".,;:!?*_>'\"`"


class ResearchReq(BaseModel):
    topic: str
    max_searches: int = 6


def _tools(searches_left: int, fetches_left: int) -> list[dict]:
    # 한도를 다 써도 도구 정의는 남긴다: 대화 기록의 server_tool_use 블록이 정의를 필요로 한다 (max_uses 최소 1)
    return [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max(1, searches_left)},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max(1, fetches_left),
         "max_content_tokens": FETCH_MAX_TOKENS},
    ]


def _echoable(content: list) -> list:
    """pause_turn 재개 시 되돌려 보낼 assistant content.

    서버측 fallback이 출력 도중에 일어났으면 마지막 fallback 블록 이전의 thinking·짝 없는 server_tool_use 등
    모델 내부 블록은 빼야 한다 (텍스트와 짝이 맞는 서버 툴 블록, 경계 이후 블록은 그대로)."""
    idx = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
    if idx < 0:
        return content
    head = content[:idx]
    paired = {getattr(b, "tool_use_id", None) for b in head if b.type.endswith("_tool_result")}
    keep = [b for b in head if b.type == "text" or b.type.endswith("_tool_result")
            or (b.type == "server_tool_use" and b.id in paired)]
    return keep + list(content[idx:])


def _clean_url(u: str) -> str:
    return u.strip().rstrip(_TRAIL)


def _norm(u: str) -> str:
    """비교용 URL 키: 스킴·www·끝 슬래시·#조각·퍼센트 인코딩 차이를 무시한다."""
    p = urlsplit(_clean_url(u))
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    key = host + unquote(p.path).rstrip("/")
    return key + ("?" + unquote(p.query) if p.query else "")


def _check_sources(report: str, known_urls: set[str]) -> tuple[list[str], list[str]]:
    """보고서 '## 출처' 절(없으면 전체)의 URL을 이번 실행에서 실제로 받은 URL과 대조."""
    tail = report.rsplit("## 출처", 1)[-1]
    listed = list(dict.fromkeys(_clean_url(u) for u in _URL.findall(tail)))
    known = {_norm(u) for u in known_urls}
    verified = [u for u in listed if _norm(u) in known]
    return verified, [u for u in listed if u not in verified]


@router.post("/research")
def research(req: ResearchReq):
    topic = req.topic.strip()
    if not topic or len(topic) > MAX_TOPIC:
        raise HTTPException(400, f"조사 주제는 1~{MAX_TOPIC}자로 입력해 주세요.")
    budget = max(1, min(req.max_searches, MAX_SEARCHES))

    def gen():
        messages = [{"role": "user", "content": f"조사 주제: {topic}\n(웹 검색은 최대 {budget}회까지 쓸 수 있습니다.)"}]
        usages, parts = [], []
        sources: dict[str, str] = {}  # url → 제목 (검색 결과 + 열람한 페이지)
        cited: set[str] = set()       # API citations(web_search_result_location)의 URL
        searches = fetches = 0
        filtering_shown = False
        final = None
        for i in range(MAX_RESUMES + 1):
            with llm.stream(messages, system=SYSTEM, tools=_tools(budget - searches, MAX_FETCHES - fetches),
                            effort="medium", max_tokens=16000) as s:
                for ev in s:
                    if ev.type == "text":
                        parts.append(ev.text)
                        yield sse.event("delta", {"text": ev.text})
                    elif ev.type == "content_block_stop":
                        b = getattr(ev, "content_block", None)
                        if b is None:
                            continue
                        if b.type == "server_tool_use":
                            if b.name in ("web_search", "web_fetch"):
                                inp = b.input if isinstance(b.input, dict) else {}
                                yield sse.event("tool", {"name": b.name, "input": inp})
                            elif not filtering_shown:  # 동적 필터링: 검색 결과를 코드로 거르는 단계
                                filtering_shown = True
                                yield sse.event("status", {"text": "⚙️ 검색 결과를 코드로 필터링하는 중"})
                        elif b.type == "web_search_tool_result":
                            if isinstance(b.content, list):
                                found = [{"title": r.title, "url": r.url} for r in b.content if getattr(r, "type", "") == "web_search_result"]
                                for f in found:
                                    sources[f["url"]] = f["title"]
                                yield sse.event("results", {"items": found})
                            else:
                                yield sse.event("tool_error", {"error": f"web_search: {getattr(b.content, 'error_code', 'unknown')}"})
                        elif b.type == "web_fetch_tool_result":
                            c = getattr(b, "content", None)
                            if getattr(c, "type", "") == "web_fetch_tool_result_error":
                                yield sse.event("tool_error", {"error": f"web_fetch: {getattr(c, 'error_code', 'unknown')}"})
                            else:
                                url = getattr(c, "url", None)
                                title = getattr(getattr(c, "content", None), "title", None) or url
                                if url:
                                    sources.setdefault(url, title)  # 검색 결과 제목이 있으면 유지
                                yield sse.event("fetched", {"url": url, "title": title})
                        elif b.type == "text":
                            cited.update(u for u in (getattr(c, "url", None) for c in (getattr(b, "citations", None) or [])) if u)
                final = s.get_final_message()
            usages.append(llm.usage_of(final))  # 웹 검색 요금은 core.llm이 usage.server_tool_use로 반영
            stu = getattr(final.usage, "server_tool_use", None)
            searches += getattr(stu, "web_search_requests", 0) or 0
            fetches += getattr(stu, "web_fetch_requests", 0) or 0
            if final.stop_reason != "pause_turn" or i == MAX_RESUMES:
                break
            messages.append({"role": "assistant", "content": _echoable(final.content)})
            yield sse.event("status", {"text": "긴 작업이라 이어서 진행합니다…"})
        llm.check_refusal(final)  # 폴백까지 모두 거절 → 422 → SSE error 이벤트 (완료로 보이지 않게)

        verified, unverified = _check_sources("".join(parts), set(sources) | cited)
        if unverified:
            yield sse.event("status", {"text": f"⚠️ 출처 목록의 URL {len(unverified)}개는 이번 실행의 검색/열람 결과에 없습니다"})
        yield sse.event("done", {
            "usage": llm.sum_usage(usages), "sources": [{"url": u, "title": t} for u, t in sources.items()],
            "stop_reason": final.stop_reason, "web_searches": searches, "web_fetches": fetches,
            "verified_urls": verified, "unverified_urls": unverified, "api_cited_urls": len(cited),
        })

    return sse.response(gen())
