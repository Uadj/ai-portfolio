"""AI 포트폴리오 서버.

실행:  python server.py   →  http://localhost:8000
"""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import threading
import time
from collections import deque

import anthropic
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import projects
from core import llm, net

log = logging.getLogger("uvicorn.error")

app = FastAPI(title="AI Engineering Portfolio")

for mod in pkgutil.iter_modules(projects.__path__):
    m = importlib.import_module(f"projects.{mod.name}")
    if hasattr(m, "router"):
        app.include_router(m.router)


# 공개 배포 시 남용 방지: IP당 시간당 POST 요청 수 제한 (0 = 무제한)
RATE_PER_HOUR = int(os.getenv("LLM_REQUESTS_PER_HOUR", "0") or 0)
# 요청 본문 상한 (Content-Length 기준 1차 방어선; 업로드별 세부 상한은 각 라우트가 따로 검사)
MAX_BODY_MB = float(os.getenv("MAX_BODY_MB", "12") or 12)
MAX_BODY_BYTES = int(MAX_BODY_MB * 1024 * 1024)
_HITS_SWEEP_AT = 5000  # 이 이상 키가 쌓이면 한 시간 넘게 조용한 IP를 정리
_hits: dict[str, deque] = {}
_hits_lock = threading.Lock()
_layout_logged = False


def _too_large(request: Request) -> bool:
    if MAX_BODY_BYTES <= 0:  # MAX_BODY_MB=0 → 다른 한도 변수들과 같이 '무제한' (0바이트 상한으로 모든 POST를 막지 않게)
        return False
    try:
        return int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES
    except ValueError:
        return False


def _rate_limited(ip: str) -> int:
    """허용이면 0, 초과면 다시 시도까지 남은 분."""
    now = time.time()
    with _hits_lock:
        if len(_hits) > _HITS_SWEEP_AT:  # 위조 IP를 바꿔 가며 보내도 메모리가 무한히 늘지 않게
            for k in [k for k, d in _hits.items() if not d or now - d[-1] > 3600]:
                del _hits[k]
        q = _hits.setdefault(ip, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= RATE_PER_HOUR:
            return int(3600 - (now - q[0])) // 60 + 1
        q.append(now)
    return 0


@app.middleware("http")
async def guard(request: Request, call_next):
    global _layout_logged
    path = request.url.path
    if request.method == "POST" and path.startswith("/api/"):
        if _too_large(request):
            return JSONResponse({"detail": f"요청이 너무 큽니다 (최대 {MAX_BODY_MB:g}MB)."}, status_code=413)
        if RATE_PER_HOUR and not path.endswith("/github-webhook"):
            if not _layout_logged:  # 배포 환경의 프록시 헤더 구성을 한 번만 기록 (IP 값은 남기지 않음)
                _layout_logged = True
                log.info("rate-limit 프록시 헤더: %s", net.header_layout(request))
            wait = _rate_limited(net.client_ip(request))
            if wait:
                return JSONResponse({"detail": f"요청이 너무 많습니다. 약 {wait}분 뒤 다시 시도해 주세요 (IP당 시간당 {RATE_PER_HOUR}회)."}, status_code=429)
    return await call_next(request)


@app.exception_handler(anthropic.APIStatusError)
async def _api_error(_: Request, e: anthropic.APIStatusError):
    return JSONResponse({"detail": f"Claude API 오류 ({e.status_code}): {e.message}"}, status_code=502)


@app.exception_handler(anthropic.APIConnectionError)
async def _conn_error(_: Request, e: anthropic.APIConnectionError):
    return JSONResponse({"detail": "Claude API에 연결할 수 없습니다. 네트워크를 확인하세요."}, status_code=502)


@app.exception_handler(Exception)
async def _unhandled(_: Request, e: Exception):
    # 프론트가 '⚠ {}' 대신 원인을 보여줄 수 있도록 JSON으로. (ServerErrorMiddleware가 이후 다시 raise해 트레이스백은 로그에 남는다)
    return JSONResponse({"detail": f"서버 내부 오류 ({type(e).__name__}). 잠시 후 다시 시도해 주세요."}, status_code=500)


@app.get("/api/status")
def status():
    return {"llm_ready": llm.has_credentials(), "model": llm.MODEL, "fast_model": llm.FAST_MODEL,
            **llm.budget_status(), "rate_per_hour": RATE_PER_HOUR or None}


app.mount("/static", StaticFiles(directory=llm.ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(llm.ROOT / "static" / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # 아이콘은 index.html의 data URI. 크롤러·Safari의 기본 요청에 404 로그가 쌓이지 않게 빈 응답
    return Response(status_code=204)


if __name__ == "__main__":
    # Render 등 PaaS는 PORT 환경변수를 주고 외부 바인딩(0.0.0.0)을 요구한다
    port = int(os.getenv("PORT", "8000"))
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    # forwarded_allow_ips="*": request.client.host가 X-Forwarded-For의 왼쪽 값(위조 가능)이 된다.
    # 레이트 리밋 등 신뢰가 필요한 곳은 core.net.client_ip()를 쓴다.
    uvicorn.run("server:app", host=host, port=port, reload=False, proxy_headers=True, forwarded_allow_ips="*")
