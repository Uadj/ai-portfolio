"""#09 MCP 서버 — 팀 노트를 Claude Desktop / Claude Code / IDE에 연결.

실행 (stdio):  python mcp_server/server.py
Claude Code 등록:  claude mcp add team-notes -- python <절대경로>/mcp_server/server.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402  (mcp 2.x)

import notes_store  # noqa: E402

mcp = MCPServer("team-notes")


@mcp.tool()
def list_notes(tag: str | None = None) -> list[dict]:
    """팀 노트 목록을 반환한다. tag를 주면 해당 태그만."""
    return notes_store.list_notes(tag)


@mcp.tool()
def search_notes(query: str) -> list[dict]:
    """키워드로 노트를 검색한다 (제목/본문/태그)."""
    return notes_store.search_notes(query)


@mcp.tool()
def add_note(title: str, body: str, tags: list[str] | None = None) -> dict:
    """새 노트를 추가한다."""
    return notes_store.add_note(title, body, tags)


@mcp.tool()
def delete_note(note_id: int) -> bool:
    """노트를 삭제한다. 삭제되면 true."""
    return notes_store.delete_note(note_id)


@mcp.resource("notes://all")
def all_notes() -> str:
    """전체 노트를 마크다운으로."""
    return "\n\n".join(f"## {n['title']} (#{n['id']})\n{n['body']}\n태그: {', '.join(n['tags'])}" for n in notes_store.list_notes())


@mcp.prompt()
def weekly_digest() -> str:
    """이번 주 노트를 요약하는 프롬프트 템플릿."""
    return "notes://all 리소스를 읽고 이번 주 핵심 결정사항과 후속 조치를 5줄로 요약해줘."


if __name__ == "__main__":
    mcp.run()
