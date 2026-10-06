"""AI 포트폴리오 서버.

실행:  python server.py   →  http://localhost:8000
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import threading
import time
from collections import defaultdict, deque

import anthropic
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import projects
from core import llm

app = FastAPI(title="AI Engineering Portfolio")

for mod in pkgutil.iter_modules(projects.__path__):
    m = importlib.import_module(f"projects.{mod.name}")
    if hasattr(m, "router"):
        app.include_router(m.router)


# 공개 배포 시 남용 방지: IP당 시간당 POST 요청 수 제한 (0 = 무제한)
RATE_PER_HOUR = int(os.getenv("LLM_REQUESTS_PER_HOUR", "0") or 0)
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    path = request.url.path
    if RATE_PER_HOUR and request.method == "POST" and path.startswith("/api/") and not path.endswith("/github-webhook"):
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "anon")
        now = time.time()
        with _hits_lock:
            q = _hits[ip]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= RATE_PER_HOUR:
                wait = int(3600 - (now - q[0])) // 60 + 1
                return JSONResponse({"detail": f"요청이 너무 많습니다. 약 {wait}분 뒤 다시 시도해 주세요 (IP당 시간당 {RATE_PER_HOUR}회)."}, status_code=429)
            q.append(now)
    return await call_next(request)


@app.exception_handler(anthropic.APIStatusError)
async def _api_error(_: Request, e: anthropic.APIStatusError):
    return JSONResponse({"detail": f"Claude API 오류 ({e.status_code}): {e.message}"}, status_code=502)


@app.exception_handler(anthropic.APIConnectionError)
async def _conn_error(_: Request, e: anthropic.APIConnectionError):
    return JSONResponse({"detail": "Claude API에 연결할 수 없습니다. 네트워크를 확인하세요."}, status_code=502)


@app.get("/api/status")
def status():
    return {"llm_ready": llm.has_credentials(), "model": llm.MODEL, "fast_model": llm.FAST_MODEL,
            **llm.budget_status(), "rate_per_hour": RATE_PER_HOUR or None}


app.mount("/static", StaticFiles(directory=llm.ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(llm.ROOT / "static" / "index.html")


if __name__ == "__main__":
    # Render 등 PaaS는 PORT 환경변수를 주고 외부 바인딩(0.0.0.0)을 요구한다
    port = int(os.getenv("PORT", "8000"))
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    uvicorn.run("server:app", host=host, port=port, reload=False, proxy_headers=True, forwarded_allow_ips="*")
