"""#03 스트리밍 챗봇 — SSE 스트리밍 + 컨텍스트 초과 시 자동 요약 압축."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from core import llm, sse

router = APIRouter(prefix="/api/p03")

# 데모에서 압축이 잘 보이도록 낮게 설정 (실서비스라면 토큰 기준 수만~수십만)
COMPRESS_CHARS = 4000
KEEP_RECENT = 4  # 최근 메시지는 원문 유지


class ChatReq(BaseModel):
    messages: list[dict]  # [{role, content}]
    summary: str = ""
    system: str = "당신은 친절하고 간결한 한국어 AI 어시스턴트입니다."


def _compress(summary: str, old: list[dict]) -> tuple[str, dict]:
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in old)
    prompt = (f"기존 요약:\n{summary or '(없음)'}\n\n추가 대화:\n{convo}\n\n"
              "이후 대화를 이어가는 데 필요한 사실(사용자 이름·선호·결정사항·진행 중 작업)을 빠짐없이 담아 "
              "한국어 bullet로 갱신된 요약만 출력하세요.")
    msg = llm.create([{"role": "user", "content": prompt}], effort="low", max_tokens=2000)
    return llm.text_of(msg), llm.usage_of(msg)


@router.post("/chat")
def chat(req: ChatReq):
    def gen():
        messages = req.messages
        summary = req.summary
        total = sum(len(m["content"]) for m in messages)
        if total > COMPRESS_CHARS and len(messages) > KEEP_RECENT:
            old, messages = messages[:-KEEP_RECENT], messages[-KEEP_RECENT:]
            if messages[0]["role"] != "user":  # 첫 메시지는 user여야 함
                old, messages = old + messages[:1], messages[1:]
            summary, u = _compress(summary, old)
            yield sse.event("compressed", {"summary": summary, "messages": messages, "dropped": len(old), "usage": u})

        system = req.system + (f"\n\n[이전 대화 요약]\n{summary}" if summary else "")
        with llm.stream(messages, system=system, effort="low", max_tokens=8000) as s:
            for t in s.text_stream:
                yield sse.event("delta", {"text": t})
            final = s.get_final_message()
        yield sse.event("done", {"usage": llm.usage_of(final), "stop_reason": final.stop_reason})

    return sse.response(gen())
