"""#08 자율 리서치 에이전트 — 웹 검색 서버 툴로 다단계 조사 후 출처가 달린 보고서 작성 (SSE로 진행 상황 스트리밍)."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from core import llm, sse

router = APIRouter(prefix="/api/p08")

SYSTEM = """당신은 리서치 에이전트입니다.
1) 주제를 3~5개의 하위 질문으로 나누는 짧은 조사 계획을 먼저 쓰세요.
2) 하위 질문마다 web_search로 검색하고, 필요하면 web_fetch로 원문을 읽으세요. 서로 다른 출처를 교차 확인하세요.
3) 마지막에 한국어 마크다운 보고서를 작성하세요: '## 요약', 하위 질문별 섹션, '## 한계와 불확실성'.
   주장마다 출처를 [번호]로 표시하고 마지막에 '## 출처' 목록(제목 - URL)을 붙이세요."""


class ResearchReq(BaseModel):
    topic: str
    max_searches: int = 6


@router.post("/research")
def research(req: ResearchReq):
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max(1, min(req.max_searches, 10))},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3},
    ]

    def gen():
        messages = [{"role": "user", "content": f"조사 주제: {req.topic}"}]
        usages = []
        sources: dict[str, str] = {}
        for _ in range(4):  # pause_turn 재개 최대 횟수
            with llm.stream(messages, system=SYSTEM, tools=tools, effort="medium", max_tokens=32000) as s:
                for ev in s:
                    if ev.type == "text":
                        yield sse.event("delta", {"text": ev.text})
                    elif ev.type == "content_block_stop":
                        b = getattr(ev, "content_block", None)
                        if b is None:
                            continue
                        if b.type == "server_tool_use":
                            inp = b.input if isinstance(b.input, dict) else {}
                            yield sse.event("tool", {"name": b.name, "input": inp})
                        elif b.type == "web_search_tool_result":
                            if isinstance(b.content, list):
                                found = [{"title": r.title, "url": r.url} for r in b.content if getattr(r, "type", "") == "web_search_result"]
                                for f in found:
                                    sources[f["url"]] = f["title"]
                                yield sse.event("results", {"items": found})
                            else:
                                yield sse.event("tool_error", {"error": getattr(b.content, "error_code", "unknown")})
                        elif b.type == "web_fetch_tool_result":
                            url = getattr(getattr(b, "content", None), "url", None)
                            yield sse.event("fetched", {"url": url})
                final = s.get_final_message()
            usages.append(llm.usage_of(final))
            if final.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": final.content})
            yield sse.event("status", {"text": "긴 작업이라 이어서 진행합니다…"})
        yield sse.event("done", {"usage": llm.sum_usage(usages), "sources": [{"url": u, "title": t} for u, t in sources.items()],
                                 "stop_reason": final.stop_reason})

    return sse.response(gen())
