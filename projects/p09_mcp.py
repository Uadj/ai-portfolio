"""#09 MCP 서버 — 실제 MCP 서버를 stdio로 띄워 도구 목록을 조회하고, Claude가 그 도구를 쓰도록 연결한다."""
from __future__ import annotations

import asyncio
import json
import sys

from fastapi import APIRouter
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import BaseModel

from core import llm

router = APIRouter(prefix="/api/p09")
SERVER = llm.ROOT / "mcp_server" / "server.py"
PARAMS = StdioServerParameters(command=sys.executable, args=[str(SERVER)])


async def _with_session(fn):
    try:
        async with stdio_client(PARAMS) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)
    except Exception as e:
        # anyio TaskGroup이 감싼 ExceptionGroup을 풀어 원래 오류(예: Claude API 401)를 드러낸다
        while hasattr(e, "exceptions") and len(e.exceptions) == 1:
            e = e.exceptions[0]
        raise e


def _tool_text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


@router.get("/tools")
async def tools():
    """MCP 프로토콜로 서버에 접속해 tools/resources/prompts 목록을 가져온다."""
    async def f(s: ClientSession):
        t = await s.list_tools()
        r = await s.list_resources()
        p = await s.list_prompts()
        return {
            "tools": [{"name": x.name, "description": x.description, "input_schema": x.input_schema} for x in t.tools],
            "resources": [{"uri": str(x.uri), "name": x.name} for x in r.resources],
            "prompts": [{"name": x.name, "description": x.description} for x in p.prompts],
        }
    out = await _with_session(f)
    out["config"] = {"mcpServers": {"team-notes": {"command": sys.executable, "args": [str(SERVER)]}}}
    return out


class CallReq(BaseModel):
    name: str
    arguments: dict = {}


@router.post("/call")
async def call(req: CallReq):
    """도구를 MCP로 직접 호출 (Claude 없이)."""
    async def f(s):
        res = await s.call_tool(req.name, req.arguments)
        return {"is_error": res.is_error, "text": _tool_text(res)}
    return await _with_session(f)


class AgentReq(BaseModel):
    prompt: str


@router.post("/agent")
async def agent(req: AgentReq):
    """Claude ↔ MCP 브릿지: MCP 서버의 도구 정의를 그대로 Claude tool로 노출하고, tool_use를 MCP call_tool로 실행."""
    async def f(s: ClientSession):
        listed = await s.list_tools()
        tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in listed.tools]
        messages = [{"role": "user", "content": req.prompt}]
        trace, usages = [], []
        msg = None
        for _ in range(6):
            msg = await asyncio.to_thread(llm.create, messages, tools=tools, effort="low", max_tokens=4000,
                                          system="당신은 팀 노트 비서입니다. 필요한 경우 도구를 사용하고 한국어로 답하세요.")
            usages.append(llm.usage_of(msg))
            uses = [b for b in msg.content if b.type == "tool_use"]
            if not uses:
                break
            messages.append({"role": "assistant", "content": msg.content})
            results = []
            for u in uses:
                res = await s.call_tool(u.name, u.input)
                text = _tool_text(res)
                trace.append({"tool": u.name, "input": u.input, "output": text[:800], "is_error": res.is_error})
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": text or "(빈 결과)", "is_error": bool(res.is_error)})
            messages.append({"role": "user", "content": results})
        return {"answer": llm.text_of(msg) if msg else "", "trace": trace, "usage": llm.sum_usage(usages)}
    return await _with_session(f)
