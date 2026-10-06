"""#09 MCP 서버 — 팀 노트를 Claude Desktop / Claude Code / IDE에 연결.

실행 (stdio):  python mcp_server/server.py
Claude Code 등록:  claude mcp add team-notes -- python <절대경로>/mcp_server/server.py

웹 데모(projects/p09_mcp.py)는 요청 _meta의 "team-notes/sandbox"로 방문자별 노트 파일을 고른다.
_meta는 도구 입력 스키마에 없으므로 Claude(모델)가 다른 방문자의 샌드박스를 지정할 수 없다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import Context, MCPServer  # noqa: E402  (mcp 2.x)
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

import notes_store  # noqa: E402

mcp = MCPServer("team-notes")


def _call(fn, ctx: Context, *args):
    """저장소 호출. 입력 제한 위반(ValueError)은 ToolError로 바꿔 메시지가 클라이언트(Claude)에 그대로 보이게 한다
    (일반 예외는 'Error executing tool <name>'으로만 전달된다)."""
    meta = ctx.request_context.meta or {}
    try:
        return fn(*args, store=notes_store.store_for(meta.get(notes_store.SANDBOX_META_KEY)))
    except ValueError as e:
        raise ToolError(str(e)) from e


@mcp.tool()
def list_notes(ctx: Context, tag: str | None = None) -> list[dict]:
    """팀 노트 목록을 반환한다. tag를 주면 해당 태그만."""
    return _call(notes_store.list_notes, ctx, tag)


@mcp.tool()
def search_notes(query: str, ctx: Context) -> list[dict]:
    """키워드로 노트를 검색한다 (제목/본문/태그, 공백으로 여러 키워드, 200자 이내)."""
    return _call(notes_store.search_notes, ctx, query)


@mcp.tool()
def add_note(title: str, body: str, ctx: Context, tags: list[str] | None = None) -> dict:
    """새 노트를 추가한다. 제목 100자, 본문 2000자, 태그 5개(각 30자) 이내. 노트는 최대 30개."""
    return _call(notes_store.add_note, ctx, title, body, tags)


@mcp.tool()
def delete_note(note_id: int, ctx: Context) -> bool:
    """노트를 삭제한다. 삭제되면 true, 해당 id가 없으면 false."""
    return _call(notes_store.delete_note, ctx, note_id)


@mcp.resource("notes://all")
def all_notes() -> str:
    """전체 노트를 마크다운으로 (기본 저장소)."""
    return "\n\n".join(f"## {n['title']} (#{n['id']})\n{n['body']}\n태그: {', '.join(n['tags'])}" for n in notes_store.list_notes())


@mcp.prompt()
def weekly_digest() -> str:
    """이번 주 노트를 요약하는 프롬프트 템플릿."""
    return "notes://all 리소스를 읽고 이번 주 핵심 결정사항과 후속 조치를 5줄로 요약해줘."


if __name__ == "__main__":
    mcp.run()
