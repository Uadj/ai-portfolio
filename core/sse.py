"""Server-Sent Events 헬퍼."""
import json
import logging

import anthropic
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

log = logging.getLogger("uvicorn.error")


def event(name: str, data) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def error_detail(e: Exception) -> str:
    """예외를 사용자에게 보여줄 한 줄 메시지로 (server.py의 JSON 오류 핸들러와 같은 문구)."""
    if isinstance(e, HTTPException):
        d = e.detail
        return d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude API 오류 ({e.status_code}): {e.message}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Claude API에 연결할 수 없습니다. 네트워크를 확인하세요."
    # 예외 메시지는 내부 경로·값을 담을 수 있으므로 브라우저에는 보내지 않고 로그에만 남긴다
    log.error("SSE 스트림 처리 중 예외", exc_info=e)
    return f"서버 내부 오류 ({type(e).__name__}). 잠시 후 다시 시도해 주세요."


def response(gen) -> StreamingResponse:
    def safe():
        try:
            yield from gen
        except Exception as e:  # 스트림 중간 오류도 클라이언트에 전달
            yield event("error", {"detail": error_detail(e)})

    # X-Accel-Buffering: 중간 프록시가 이벤트를 모아 보내지 않도록
    return StreamingResponse(safe(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
