"""Server-Sent Events 헬퍼."""
import json

from fastapi.responses import StreamingResponse


def event(name: str, data) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def response(gen) -> StreamingResponse:
    def safe():
        try:
            yield from gen
        except Exception as e:  # 스트림 중간 오류도 클라이언트에 전달
            detail = getattr(e, "detail", None) or getattr(e, "message", None) or str(e)
            yield event("error", {"detail": detail})

    return StreamingResponse(safe(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
